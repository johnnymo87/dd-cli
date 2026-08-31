from __future__ import annotations

import email.utils
import json
import os
import random
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

# Status codes worth retrying for a read. 429 is handled separately because
# its retry rules differ between reads and writes.
RETRYABLE_SERVER_ERRORS = frozenset({500, 502, 503, 504})

# Datadog's genuine rate-limit responses carry these. A bare 429 with none of
# them is more likely to have come from an intermediary (edge/WAF/proxy), which
# may have emitted it *after* the origin already accepted a write.
_DD_RATELIMIT_HEADERS = (
    "x-ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-period",
    "x-ratelimit-name",
)

# A write may be retried on 429, but only barely: each extra attempt is another
# chance to duplicate a create if the limiter sat in front of a server that had
# already accepted the request.
_WRITE_MAX_RETRIES = 1

# Resolution failures that will never succeed on a retry: the name does not
# exist, or the resolver gave an authoritative "no". Notably EAI_AGAIN is NOT
# here -- it means "temporary failure in name resolution" (resolver down, VPN
# still coming up), which is exactly the transient case retries exist for.
_PERMANENT_DNS_ERRNOS = frozenset(
    code
    for code in (
        getattr(socket, name, None) for name in ("EAI_NONAME", "EAI_FAIL", "EAI_NODATA")
    )
    if code is not None
)


def _permanent_dns_error(exc: BaseException | None) -> socket.gaierror | None:
    """Find a permanent ``socket.gaierror`` in an exception's chain.

    httpx does not surface the resolver error directly. The real chain is
    ``httpx.ConnectError -> __cause__ httpcore.ConnectError -> __context__
    socket.gaierror``, so both links are walked. Returns ``None`` for a
    temporary resolution failure, which stays retryable.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, socket.gaierror) and exc.errno in _PERMANENT_DNS_ERRNOS:
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def _normalize_site(site: str) -> str:
    """Normalize site to just the domain (e.g., 'us3.datadoghq.com')."""
    site = site.strip()
    if site.startswith(("http://", "https://")):
        site = urllib.parse.urlparse(site).netloc
    # Handle case where user passes "api.us3.datadoghq.com"
    if site.startswith("api."):
        site = site.removeprefix("api.")
    return site


def _api_host(site: str) -> str:
    return f"https://api.{_normalize_site(site)}"


def env(var: str, default: str | None = None) -> str | None:
    """Get environment variable, treating empty string as unset."""
    v = os.getenv(var)
    if v is None or v == "":
        return default
    return v


@dataclass
class DatadogAPIError(Exception):
    """Exception for Datadog API errors.

    ``attempts`` and ``elapsed_s`` record how hard the client tried before
    giving up, so an exhausted retry loop is legible after the fact rather
    than looking like a single unlucky request.
    """

    status_code: int
    message: str
    response_body: str | None = None
    attempts: int = 1
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        base = f"{self.message} (status={self.status_code})"
        if self.attempts > 1:
            base += f" after {self.attempts} attempts over {self.elapsed_s:.1f}s"
        return base


class LogMetricPathError(ValueError):
    """A log-metric attribute path would silently produce no data.

    Raised *before* any HTTP call. Datadog accepts a bare (``@``-less) custom
    attribute path with 200 OK and then never emits a point for it, so this is
    the only layer that can catch the mistake at all.
    """


#: Paths that are correctly written WITHOUT a leading '@'.
#:
#: Datadog splits a log's searchable keys into two namespaces: reserved
#: attributes and tags live at the top level (``service``, ``env``), while
#: everything the application itself logged lives under ``@`` (``@duration``,
#: ``@fbm.attention_open``). Only the first namespace may appear bare.
#:
#: This set is deliberately small and conservative: it covers Datadog's
#: reserved attributes plus the handful of tag keys that every org has. Any
#: other bare path is rejected with an explanation and an escape hatch
#: (``allow_bare_paths``) rather than guessed at -- see the module docstring
#: of tests/test_log_metrics.py for why guessing is unacceptable here.
RESERVED_LOG_PATHS: frozenset[str] = frozenset(
    {
        "date",
        "ddsource",
        "ddtags",
        "env",
        "host",
        "message",
        "service",
        "source",
        "status",
        "timestamp",
        "version",
    }
)


def _bare_path_error(path: str, *, kind: str) -> LogMetricPathError:
    """Build the loud, consequence-first error for a bare custom path."""
    consequence = (
        "the metric never records a single point"
        if kind == "compute path"
        else "every value collapses into one 'N/A' tag bucket"
    )
    return LogMetricPathError(
        f"Log-metric {kind} '{path}' names a custom log attribute but has no "
        f"leading '@'.\n"
        f"\n"
        f"Datadog will ACCEPT this: the API returns 200 OK and creates the "
        f"metric. It will then silently produce no data forever -- "
        f"{consequence} -- with no error anywhere, in Datadog or here. The "
        f"only way to notice is to look at an empty graph days later.\n"
        f"\n"
        f"Fix it one of two ways:\n"
        f"  * write it as '@{path}' -- correct for any attribute your "
        f"application logged;\n"
        f"  * pass allow_bare_paths=True (CLI flag: --allow-bare-path) if this "
        f"really is a tag key or reserved attribute, which are correctly "
        f"bare.\n"
        f"\n"
        f"Recognised-bare paths: {', '.join(sorted(RESERVED_LOG_PATHS))}."
    )


def validate_log_metric_path(
    path: str,
    *,
    kind: str,
    allow_bare: bool = False,
    allow_reserved: bool = True,
) -> None:
    """Reject a path that would silently yield an empty metric.

    ``kind`` is used in the message: ``"compute path"`` or ``"group_by path"``.
    ``allow_reserved`` is False for a distribution's compute path, where a
    reserved attribute is legal syntax but the wrong kind of value.
    """
    if not path or path == "@":
        raise LogMetricPathError(
            f"Log-metric {kind} must name an attribute; got {path!r}."
        )
    if allow_bare or path.startswith("@"):
        return
    if path in RESERVED_LOG_PATHS:
        if allow_reserved:
            return
        raise LogMetricPathError(
            f"Log-metric {kind} '{path}' is a reserved Datadog attribute, "
            f"which holds a string, not a number.\n"
            f"\n"
            f"A distribution measures a numeric value, so Datadog will accept "
            f"this with 200 OK and then never record a point. Measure an "
            f"attribute your application logged instead -- those are written "
            f"with a leading '@' (e.g. '@duration') -- and use group_by "
            f"(CLI: --group-by {path}) if what you wanted was to split the "
            f"metric by {path}.\n"
            f"\n"
            f"Pass allow_bare_paths=True (CLI flag: --allow-bare-path) to "
            f"override."
        )
    raise _bare_path_error(path, kind=kind)


def validate_log_metric_spec(
    *,
    aggregation_type: str,
    path: str | None = None,
    include_percentiles: bool | None = None,
    group_by: list[dict[str, str]] | None = None,
    allow_bare_paths: bool = False,
) -> None:
    """Check every precondition that must hold before the POST is worth making.

    Lives apart from :meth:`DatadogClient.create_log_metric` so the CLI can run
    the same checks without a client, and so a caller who mocks the client
    still cannot skip them.
    """
    if aggregation_type not in ("count", "distribution"):
        raise ValueError(
            f"aggregation_type must be 'count' or 'distribution', "
            f"got {aggregation_type!r}."
        )

    if aggregation_type == "distribution":
        if not path:
            raise ValueError(
                "aggregation_type 'distribution' requires a compute path "
                "naming the numeric log attribute to measure (e.g. "
                "'@duration'). A distribution has nothing to aggregate "
                "without it."
            )
        validate_log_metric_path(
            path,
            kind="compute path",
            allow_bare=allow_bare_paths,
            allow_reserved=False,
        )
    else:
        if path:
            raise ValueError(
                "A compute path is only valid for aggregation_type "
                "'distribution'; a 'count' metric counts logs and must not "
                "send one."
            )
        if include_percentiles is not None:
            raise ValueError(
                "include_percentiles is only valid for aggregation_type 'distribution'."
            )

    for entry in group_by or []:
        validate_log_metric_path(
            entry.get("path", ""),
            kind="group_by path",
            allow_bare=allow_bare_paths,
        )


@dataclass
class RetryEvent:
    """Reported to the ``on_retry`` callback before each retry sleep."""

    status: int | None
    path: str
    attempt: int
    max_retries: int
    delay: float
    reason: str

    def __str__(self) -> str:
        what = self.status if self.status is not None else self.reason
        return (
            f"{what} from {self.path}, "
            f"retry {self.attempt}/{self.max_retries} in {self.delay:.1f}s"
        )


class DatadogClient:
    """HTTP client for Datadog APIs.

    Usage::

        with DatadogClient(site="us3.datadoghq.com", ...) as dd:
            incident = dd.get_incident("123")
            logs = dd.search_logs(query="env:prod error")
    """

    def __init__(
        self,
        *,
        site: str,
        pat: str,
        timeout: float = 15.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        retry_budget: float | None = None,
        on_retry: Callable[[RetryEvent], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """Create a Datadog HTTP client.

        Authenticates with a Personal Access Token (``pat``), sent as an
        ``Authorization: Bearer`` header. A PAT is a single, scoped, expiring
        credential and does not need to be paired with an API key.

        Args:
            timeout: Per-request timeout in seconds.
            max_retries: Retries *after* the initial attempt.
            backoff_base: Base for the exponential backoff, in seconds.
            backoff_max: Ceiling for any single sleep. Also clamps
                server-supplied ``Retry-After`` values, so a hostile or absurd
                header cannot hang the CLI.
            retry_budget: Total wall-clock ceiling for retry sleeps. Defaults
                to ``max(120, 4 * timeout)`` -- a flat ceiling would give
                effectively zero retries to flex-tier users, who are told to
                raise ``--timeout``, i.e. exactly the slowest and most
                rate-limited queries.
            on_retry: Called with a :class:`RetryEvent` before each sleep.
            transport: Injection point for tests (``httpx.MockTransport``).
            sleep / monotonic: Injection points for a deterministic clock.
        """
        if not pat:
            raise ValueError("DatadogClient requires a PAT (pat=...).")

        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retry_budget = (
            retry_budget if retry_budget is not None else max(120.0, 4 * timeout)
        )
        self._on_retry = on_retry
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

        self._client = httpx.Client(
            base_url=_api_host(site),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {pat}",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DatadogClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    # ── Retry machinery ─────────────────────────────────────────────

    def _parse_retry_after(self, value: str | None) -> float | None:
        """Parse a ``Retry-After`` header (delta-seconds or HTTP-date)."""
        if not value:
            return None
        value = value.strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        import datetime as _dt

        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.UTC)
        delta = (when - _dt.datetime.now(_dt.UTC)).total_seconds()
        return max(0.0, delta)

    def _retry_delay(self, resp: httpx.Response | None, attempt: int) -> float:
        """Seconds to wait before the next attempt.

        Prefers the server's own guidance (``Retry-After``, then
        ``X-RateLimit-Reset``) over guessing, but clamps it to ``backoff_max``
        so a hostile header cannot hang the CLI. With no guidance, uses
        full-jitter exponential backoff.
        """
        if resp is not None:
            hinted = self._parse_retry_after(resp.headers.get("Retry-After"))
            if hinted is None:
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset:
                    try:
                        hinted = max(0.0, float(reset))
                    except ValueError:
                        hinted = None
            if hinted is not None and hinted > 0:
                return min(hinted, self.backoff_max)

        cap = min(self.backoff_max, self.backoff_base * (2**attempt))
        return random.uniform(0.0, cap)

    def _should_retry(
        self,
        *,
        write: bool,
        status: int | None,
        resp: httpx.Response | None,
        error: httpx.RequestError | None = None,
    ) -> bool:
        if status is None:
            # A name that does not resolve will not resolve on the next try
            # either. Retrying only burns the budget and dresses a bad DD_SITE
            # up as a flaky network.
            if _permanent_dns_error(error) is not None:
                return False
            # Any other transport error. Safe to repeat only for reads.
            return not write

        if status == 429:
            if not write:
                return True
            # A bare 429 may have come from an intermediary *after* the origin
            # accepted the write. Only retry when Datadog's own rate-limit
            # headers prove the limiter was Datadog's.
            headers = resp.headers if resp is not None else {}
            return any(h in headers for h in _DD_RATELIMIT_HEADERS)

        # A 5xx on a write may mean the write landed. Never risk a duplicate.
        return status in RETRYABLE_SERVER_ERRORS and not write

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> Any:
        """Make a request with retry and return the parsed JSON response.

        Prefer the :meth:`_read` / :meth:`_write` wrappers over calling this
        directly; they make the retry policy explicit at the call site.

        Raises:
            DatadogAPIError: On non-retryable 4xx/5xx, or on retry exhaustion.
                Never returns an empty or zero value to represent a failure.
            RuntimeError: On network errors or invalid JSON.
        """
        max_retries = (
            min(self.max_retries, _WRITE_MAX_RETRIES) if write else (self.max_retries)
        )
        started = self._monotonic()
        attempt = 0

        while True:
            attempt += 1
            resp: httpx.Response | None = None
            status: int | None = None
            transport_error: httpx.RequestError | None = None

            try:
                sent = self._client.request(method, path, params=params, json=json_body)
                resp = sent
                sent.raise_for_status()
            except httpx.HTTPStatusError as e:
                resp = e.response
                status = e.response.status_code
            except httpx.RequestError as e:
                transport_error = e

            if status is None and transport_error is None:
                assert resp is not None
                if resp.status_code in (204, 205):
                    # "No Content" is the documented success of a DELETE.
                    # Parsing it would raise and dress a completed delete up as
                    # a failure -- the inverse of the usual sin, but still a
                    # wrong answer.
                    return None
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"Invalid JSON response: {e.msg}") from e

            retryable = self._should_retry(
                write=write, status=status, resp=resp, error=transport_error
            )
            elapsed = self._monotonic() - started

            if retryable and attempt <= max_retries:
                delay = self._retry_delay(
                    resp if status is not None else None, attempt - 1
                )
                if elapsed + delay <= self.retry_budget:
                    if self._on_retry is not None:
                        self._on_retry(
                            RetryEvent(
                                status=status,
                                path=path,
                                attempt=attempt,
                                max_retries=max_retries,
                                delay=delay,
                                reason=(
                                    "network error"
                                    if transport_error is not None
                                    else "http error"
                                ),
                            )
                        )
                    self._sleep(delay)
                    continue

            elapsed = self._monotonic() - started

            if transport_error is not None:
                dns_error = _permanent_dns_error(transport_error)
                if dns_error is not None:
                    host = self._client.base_url.host
                    raise RuntimeError(
                        f"Cannot resolve {host!r} ({dns_error.strerror}). "
                        "This is a configuration error, not a network blip -- "
                        "check DD_SITE (e.g. 'us3.datadoghq.com')."
                    ) from transport_error
                raise RuntimeError(
                    f"Network error after {attempt} attempt(s): {transport_error}"
                ) from transport_error

            assert resp is not None and status is not None
            msg = "Datadog API error"
            body = resp.text
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("errors"):
                    msg = "; ".join(str(err) for err in payload["errors"])
            except Exception:
                pass
            raise DatadogAPIError(
                status, msg, body, attempts=attempt, elapsed_s=elapsed
            )

    def _read(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a read-only request. Retries 429, 5xx, and network errors.

        Some Datadog reads are POSTs (log search, error-tracking search), so
        the policy is chosen explicitly rather than inferred from the verb.
        """
        return self._request(
            method, path, params=params, json_body=json_body, write=False
        )

    def _write(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a state-changing request.

        Retries only a 429 that carries Datadog's rate-limit headers, and only
        once. Never retries 5xx or network errors, either of which may mean the
        write actually landed.
        """
        return self._request(
            method, path, params=params, json_body=json_body, write=True
        )

    def get_incident(
        self,
        incident_id: str,
        *,
        include: str | None = None,
    ) -> dict[str, Any]:
        """Get incident by ID."""
        params = {"include": include} if include else None
        return self._read("GET", f"/api/v2/incidents/{incident_id}", params=params)

    def get_incident_type(self, incident_type_uuid: str) -> dict[str, Any]:
        """Get incident type configuration by UUID."""
        return self._read("GET", f"/api/v2/incidents/config/types/{incident_type_uuid}")

    def get_incident_integrations(self, incident_id: str) -> dict[str, Any]:
        """Get incident integrations (Slack, Jira, etc.)."""
        return self._read(
            "GET", f"/api/v2/incidents/{incident_id}/relationships/integrations"
        )

    def update_incident(
        self,
        incident_id: str,
        *,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update incident attributes."""
        payload = {
            "data": {
                "type": "incidents",
                "id": incident_id,
                "attributes": attributes,
            }
        }
        return self._write(
            "PATCH", f"/api/v2/incidents/{incident_id}", json_body=payload
        )

    def search_logs(
        self,
        *,
        query: str,
        time_from: str = "now-15m",
        time_to: str = "now",
        limit: int = 100,
        cursor: str | None = None,
        indexes: list[str] | None = None,
        storage_tier: str | None = None,
    ) -> dict[str, Any]:
        """Search logs with Datadog query syntax."""
        body: dict[str, Any] = {
            "filter": {
                "query": query,
                "from": time_from,
                "to": time_to,
            },
            "sort": "-timestamp",
            "page": {"limit": limit},
        }
        if indexes:
            body["filter"]["indexes"] = indexes
        if storage_tier:
            body["filter"]["storage_tier"] = storage_tier
        if cursor:
            body["page"]["cursor"] = cursor

        return self._read("POST", "/api/v2/logs/events/search", json_body=body)

    def count_logs(
        self,
        *,
        query: str,
        time_from: str,
        time_to: str,
        storage_tier: str | None = None,
    ) -> int:
        """Count matching logs via the aggregate endpoint.

        Returns a real count or raises. An unexpected response shape raises
        rather than returning 0: a zero is a claim that nothing matched, and
        this method must never make that claim on the API's behalf.
        """
        body: dict[str, Any] = {
            "compute": [{"aggregation": "count"}],
            "filter": {"query": query, "from": time_from, "to": time_to},
        }
        if storage_tier:
            body["filter"]["storage_tier"] = storage_tier

        payload = self._read("POST", "/api/v2/logs/analytics/aggregate", json_body=body)
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"logs aggregate returned {type(payload).__name__}, expected an object"
            )

        buckets = (payload.get("data") or {}).get("buckets")
        if buckets is None:
            raise RuntimeError(
                "logs aggregate response contained no 'data.buckets'; refusing to "
                f"report a count. Response: {str(payload)[:200]}"
            )
        if not buckets:
            # A genuinely empty result set: no bucket means nothing matched.
            return 0

        computes = buckets[0].get("computes") or {}
        if "c0" not in computes:
            raise RuntimeError(
                "logs aggregate bucket had no 'c0' compute; refusing to report a "
                f"count. Bucket: {str(buckets[0])[:200]}"
            )
        return int(computes["c0"])

    def validate(self) -> dict[str, Any]:
        """Validate the PAT via /api/v2/current_user.

        (The legacy /api/v1/validate endpoint only validates an API key and
        rejects a PAT with 403, so it is not used.)
        """
        return self._read("GET", "/api/v2/current_user")

    def list_catalog_entities(
        self,
        *,
        kind: str | None = None,
        owner: str | None = None,
        name: str | None = None,
        ref: str | None = None,
        include: list[str] | None = None,
        include_discovered: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List Software Catalog entities using the v2 Software Catalog API."""
        params: dict[str, Any] = {
            "page[offset]": offset,
            "page[limit]": limit,
        }
        if kind:
            params["filter[kind]"] = kind
        if owner:
            params["filter[owner]"] = owner
        if name:
            params["filter[name]"] = name
        if ref:
            params["filter[ref]"] = ref
        if include:
            params["include"] = ",".join(include)
        if include_discovered:
            params["includeDiscovered"] = True

        return self._read("GET", "/api/v2/catalog/entity", params=params)

    # ── Teams (v2) ─────────────────────────────────────────────────

    def list_teams(
        self,
        *,
        keyword: str | None = None,
        me: bool = False,
        include: list[str] | None = None,
        fields: list[str] | None = None,
        page_number: int = 0,
        page_size: int = 100,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """List Datadog Teams using the v2 Teams API."""
        params: dict[str, Any] = {
            "page[number]": page_number,
            "page[size]": page_size,
        }
        if keyword:
            params["filter[keyword]"] = keyword
        if me:
            params["filter[me]"] = True
        if include:
            params["include"] = ",".join(include)
        if fields:
            params["fields[team]"] = ",".join(fields)
        if sort:
            params["sort"] = sort

        return self._read("GET", "/api/v2/team", params=params)

    def list_team_memberships(
        self,
        team_id: str,
        *,
        keyword: str | None = None,
        page_number: int = 0,
        page_size: int = 100,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """List memberships for one Datadog Team."""
        params: dict[str, Any] = {
            "page[number]": page_number,
            "page[size]": page_size,
        }
        if keyword:
            params["filter[keyword]"] = keyword
        if sort:
            params["sort"] = sort

        return self._read(
            "GET",
            f"/api/v2/team/{team_id}/memberships",
            params=params,
        )

    def list_team_notification_rules(self, team_id: str) -> dict[str, Any]:
        """List notification rules for one Datadog Team."""
        return self._read("GET", f"/api/v2/team/{team_id}/notification-rules")

    # ── Log-based metrics (v2) ──────────────────────────────────────

    def create_log_metric(
        self,
        *,
        metric_id: str,
        query: str,
        group_by: list[dict[str, str]] | None = None,
        aggregation_type: str = "count",
        path: str | None = None,
        include_percentiles: bool | None = None,
        allow_bare_paths: bool = False,
    ) -> dict[str, Any]:
        """Create a log-based metric (``count`` or ``distribution``).

        Metrics are computed at ingestion time, so they work regardless
        of whether logs land in standard or flex storage tier.

        A ``distribution`` measures a numeric value off each matching log,
        named by ``path``; ``count`` just counts logs and must not carry a
        path. Every path (compute and group_by) is validated for the leading
        ``@`` that a custom attribute requires, because Datadog accepts a bare
        one and then silently emits nothing. See :func:`validate_log_metric_path`.

        Raises ``ValueError`` (``LogMetricPathError`` for path problems) before
        any request is sent.
        """
        validate_log_metric_spec(
            aggregation_type=aggregation_type,
            path=path,
            include_percentiles=include_percentiles,
            group_by=group_by,
            allow_bare_paths=allow_bare_paths,
        )

        compute: dict[str, Any] = {"aggregation_type": aggregation_type}
        if aggregation_type == "distribution":
            compute["path"] = path
            if include_percentiles is not None:
                compute["include_percentiles"] = include_percentiles

        attributes: dict[str, Any] = {"compute": compute}
        if query:
            attributes["filter"] = {"query": query}
        if group_by:
            attributes["group_by"] = group_by

        payload = {
            "data": {
                "id": metric_id,
                "type": "logs_metrics",
                "attributes": attributes,
            }
        }
        return self._write("POST", "/api/v2/logs/config/metrics", json_body=payload)

    def get_log_metric(self, metric_id: str) -> dict[str, Any]:
        """Get a log-based metric by ID."""
        escaped = urllib.parse.quote(metric_id, safe="")
        return self._read("GET", f"/api/v2/logs/config/metrics/{escaped}")

    def list_log_metrics(self) -> list[dict[str, Any]]:
        """List every log-based metric in the org.

        ``GET /api/v2/logs/config/metrics`` is unpaginated: it returns the whole
        set under ``data`` with no cursor and no ``meta``. That is convenient
        and dangerous in equal measure -- there is no cursor whose absence could
        prove completeness, so a malformed or partial body has nothing to trip
        over. Hence the shape check: anything that is not a list of resource
        objects raises instead of degrading into a short (or empty) list, which
        would read exactly like "this org has few log metrics".
        """
        payload: Any = self._read("GET", "/api/v2/logs/config/metrics")
        if not isinstance(payload, dict):
            raise RuntimeError(
                "GET /api/v2/logs/config/metrics expected a JSON object, got "
                f"{type(payload).__name__}: {str(payload)[:200]}"
            )
        if "data" not in payload:
            raise RuntimeError(
                "GET /api/v2/logs/config/metrics returned no 'data' key; "
                "refusing to report an empty metric list. Response: "
                f"{str(payload)[:200]}"
            )
        data = payload["data"]
        if not isinstance(data, list):
            raise RuntimeError(
                "GET /api/v2/logs/config/metrics returned 'data' of type "
                f"{type(data).__name__}, expected an array: {str(data)[:200]}"
            )
        return data

    def update_log_metric(
        self,
        metric_id: str,
        *,
        query: str | None = None,
        group_by: list[dict[str, str]] | None = None,
        include_percentiles: bool | None = None,
        allow_bare_paths: bool = False,
    ) -> dict[str, Any]:
        """Update a log-based metric (PATCH /api/v2/logs/config/metrics/{id}).

        Datadog's ``LogsMetricUpdateAttributes`` accepts exactly three things:
        ``filter.query``, ``group_by``, and ``compute.include_percentiles``.
        There is deliberately no way to change ``compute.aggregation_type`` or
        ``compute.path`` -- those are fixed at creation, and a metric that needs
        a different one has to be recreated under a new name.

        ``group_by`` is sent whole and replaces the existing list; there is no
        per-entry merge. Pass ``[]`` to clear it.

        A log metric is computed at INTAKE and never backfills, so a changed
        filter can only be judged against data that has not arrived yet. See
        the CLI command's help for what that means in practice.
        """
        attributes: dict[str, Any] = {}
        if query is not None:
            attributes["filter"] = {"query": query}
        if group_by is not None:
            for entry in group_by:
                validate_log_metric_path(
                    entry.get("path", ""),
                    kind="group_by path",
                    allow_bare=allow_bare_paths,
                )
            attributes["group_by"] = group_by
        if include_percentiles is not None:
            attributes["compute"] = {"include_percentiles": include_percentiles}

        if not attributes:
            raise ValueError(
                "update_log_metric was given nothing to change. Datadog's PATCH "
                "accepts only filter.query, group_by, and "
                "compute.include_percentiles; an empty request would report "
                "success while changing nothing."
            )

        payload = {"data": {"type": "logs_metrics", "attributes": attributes}}
        escaped = urllib.parse.quote(metric_id, safe="")
        return self._write(
            "PATCH", f"/api/v2/logs/config/metrics/{escaped}", json_body=payload
        )

    def delete_log_metric(self, metric_id: str) -> dict[str, Any] | None:
        """Delete a log-based metric by ID.

        Datadog answers 204 with no body, so a successful delete returns None.
        Deletion stops future points; it does not remove the points the metric
        already emitted.
        """
        escaped = urllib.parse.quote(metric_id, safe="")
        return self._write("DELETE", f"/api/v2/logs/config/metrics/{escaped}")

    # ── Monitors (v1) ───────────────────────────────────────────────

    def create_monitor(
        self,
        *,
        name: str,
        monitor_type: str,
        query: str,
        message: str,
        tags: list[str] | None = None,
        options: dict[str, Any] | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        """Create a monitor."""
        payload: dict[str, Any] = {
            "name": name,
            "type": monitor_type,
            "query": query,
            "message": message,
        }
        if tags:
            payload["tags"] = tags
        if options:
            payload["options"] = options
        if priority is not None:
            payload["priority"] = priority
        return self._write("POST", "/api/v1/monitor", json_body=payload)

    def update_monitor(
        self,
        monitor_id: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a monitor by ID (PUT /api/v1/monitor/{id}).

        The payload should contain the fields to update (name, query,
        message, options, tags, priority, etc.). Fields not included
        are left unchanged by the API.
        """
        return self._write("PUT", f"/api/v1/monitor/{monitor_id}", json_body=payload)

    def search_monitors(
        self,
        *,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Search monitors."""
        params = {}
        if query:
            params["query"] = query
        return self._read("GET", "/api/v1/monitor/search", params=params)

    def list_monitors(
        self,
        *,
        monitor_tags: list[str] | None = None,
        scope_tags: list[str] | None = None,
        name: str | None = None,
        page: int = 0,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """List monitors (single page).

        The v1 list endpoint returns a bare JSON array (not a wrapped object).
        Pagination is page-based: pass `page=N` to fetch the (N+1)th page.
        Datadog silently caps `page_size` at 1000.

        ``GET /api/v1/monitor`` takes **two** different tag parameters and they
        answer different questions. Per Datadog's own API contract, ``tags``
        filters "by scope" (the tags on the watched resources, e.g.
        ``host:host0``) while ``monitor_tags`` filters by "service and/or custom
        tags" -- the monitor's own tag list. Ownership tags (``team:``,
        ``managed-by:``, ``feature:``) live on the monitor and almost never
        appear in its query, so sending them as ``tags`` returns an empty set
        that is indistinguishable from a genuine "nothing there" result. The two
        parameters are AND-combined when both are sent.

        The kwargs are deliberately named for the predicate rather than for the
        wire parameter, and there is no ``tags=`` kwarg: a caller that means one
        and passes the other now fails loudly instead of quietly.

        Args:
            monitor_tags: Filter by the monitor's *own* tags. Multiple values
                are AND-combined (DD-side comma join).
            scope_tags: Filter by the *scope* the monitor watches, i.e. tags
                appearing in its query. AND-combined the same way.
            name: Substring match on the monitor name (case-insensitive,
                handled DD-side).
            page: Zero-indexed page number.
            page_size: Number of monitors per page (max 1000).
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
        }
        if monitor_tags:
            params["monitor_tags"] = ",".join(monitor_tags)
        if scope_tags:
            params["tags"] = ",".join(scope_tags)
        if name:
            params["name"] = name
        # The v1 list endpoint returns a bare JSON array, not a wrapped object.
        result: Any = self._read("GET", "/api/v1/monitor", params=params)
        if not isinstance(result, list):
            # Never let an unexpected response shape become an empty list --
            # that is an error masquerading as "no monitors matched".
            raise RuntimeError(
                "GET /api/v1/monitor expected a JSON array, got "
                f"{type(result).__name__}: {str(result)[:200]}"
            )
        return result

    def get_monitor(
        self,
        monitor_id: str,
        *,
        group_states: str | None = None,
    ) -> dict[str, Any]:
        """Get a monitor's details by ID.

        Args:
            monitor_id: The numeric monitor ID.
            group_states: Comma-separated list of group states to include
                (all, alert, warn, no data).
        """
        params: dict[str, Any] = {}
        if group_states:
            params["group_states"] = group_states
        return self._read("GET", f"/api/v1/monitor/{monitor_id}", params=params or None)

    def delete_monitor(
        self,
        monitor_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Delete a monitor by ID (DELETE /api/v1/monitor/{id}).

        Answers ``{"deleted_monitor_id": <id>}`` on success. There is no undo:
        Datadog does not keep a deleted monitor around to be restored through
        the API, so the caller is responsible for capturing the definition
        first.

        Datadog refuses (400) to delete a monitor that an SLO or a composite
        monitor references, naming the referencing resource in the error.
        ``force=True`` sends ``?force=true`` and deletes it anyway -- which
        leaves the SLO or composite pointing at a monitor that no longer
        exists; Datadog does not clean the reference up.

        ``force`` is omitted entirely unless asked for. The parameter is a
        *string* on the wire per Datadog's spec, not a bool.

        Args:
            monitor_id: The numeric monitor ID.
            force: Delete even when another resource references the monitor.
        """
        params = {"force": "true"} if force else None
        # Escaped even though the CLI validates the ID first: an unescaped '?'
        # or '/' here would let a caller rewrite the request path or bolt on a
        # query parameter (e.g. force) that nobody asked for.
        escaped = urllib.parse.quote(monitor_id, safe="")
        return self._write("DELETE", f"/api/v1/monitor/{escaped}", params=params)

    def mute_monitor(
        self,
        monitor_id: str,
        *,
        end: int | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Mute a monitor (POST /api/v1/monitor/{id}/mute).

        Muting is *not* symmetric with the monitor update endpoint: a mute can
        be written through ``PUT`` as ``options.silenced``, but the same PUT
        cannot take it away again (see ``unmute_monitor``). Mute and unmute are
        therefore the only pair that can both set and clear the state.

        Parameters go in the JSON body, matching Datadog's own ``datadogpy``
        client (``Monitor.mute(id, scope=..., end=...)`` posts them as a body).
        The endpoint is not in Datadog's generated OpenAPI spec, so the caller
        must not treat a 200 as proof the parameters were honoured -- verify by
        re-reading the monitor.

        Args:
            monitor_id: The numeric monitor ID.
            end: POSIX timestamp in **seconds** at which the mute expires.
                Omitted means muted until explicitly unmuted -- indefinitely.
            scope: A single ``key:value`` group scope to mute. Omitted mutes
                the whole monitor, which Datadog records under the ``*`` key.
        """
        body: dict[str, Any] = {}
        if end is not None:
            body["end"] = end
        if scope is not None:
            body["scope"] = scope
        escaped = urllib.parse.quote(monitor_id, safe="")
        return self._write(
            "POST", f"/api/v1/monitor/{escaped}/mute", json_body=body or None
        )

    def unmute_monitor(
        self,
        monitor_id: str,
        *,
        scope: str | None = None,
        all_scopes: bool = False,
    ) -> dict[str, Any]:
        """Unmute a monitor (POST /api/v1/monitor/{id}/unmute).

        This is the only way to clear ``options.silenced``. ``PUT
        /api/v1/monitor/{id}`` with ``silenced`` set to ``{}`` or ``null``
        returns 200 and leaves the monitor muted -- verified against the live
        API on 2026-08-31 (monitor 25447403), which is why dd-cli refuses that
        payload instead of forwarding it.

        Args:
            monitor_id: The numeric monitor ID.
            scope: A single ``key:value`` group scope to unmute.
            all_scopes: Clear the mute on every scope. Mutually exclusive with
                ``scope`` (the caller is expected to enforce that).
        """
        if scope is not None and all_scopes:
            raise ValueError(
                "unmute_monitor was given both scope and all_scopes. Datadog "
                "would honour one of them and silently ignore the other, "
                "leaving the caller unable to say which scopes are still muted."
            )
        body: dict[str, Any] = {}
        if scope is not None:
            body["scope"] = scope
        if all_scopes:
            body["all_scopes"] = True
        escaped = urllib.parse.quote(monitor_id, safe="")
        return self._write(
            "POST", f"/api/v1/monitor/{escaped}/unmute", json_body=body or None
        )

    # ── Dashboards (v1) ─────────────────────────────────────────────

    def create_dashboard(self, *, body: dict[str, Any]) -> dict[str, Any]:
        """Create a dashboard (POST /api/v1/dashboard).

        The body is the full Datadog dashboard request object (title,
        layout_type, widgets, template_variables, etc.). It is sent as-is
        so callers can supply the exact widget/layout definition.
        """
        return self._write("POST", "/api/v1/dashboard", json_body=body)

    def update_dashboard(
        self, dashboard_id: str, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Update (replace) a dashboard by ID (PUT /api/v1/dashboard/{id}).

        The Datadog dashboard PUT is a full replace: the body must be the
        complete dashboard request object (title, layout_type, widgets,
        template_variables, etc.). It is sent as-is so callers can supply the
        exact widget/layout definition.
        """
        return self._write("PUT", f"/api/v1/dashboard/{dashboard_id}", json_body=body)

    def get_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Get a dashboard's full definition by ID."""
        return self._read("GET", f"/api/v1/dashboard/{dashboard_id}")

    def list_dashboards(
        self,
        *,
        filter_shared: bool | None = None,
        filter_deleted: bool | None = None,
    ) -> dict[str, Any]:
        """List all dashboards (GET /api/v1/dashboard).

        Returns a wrapped object with a ``dashboards`` array. Each entry is a
        summary (id, title, url, ...), not the full widget definition.

        Args:
            filter_shared: When set, filter by shared status.
            filter_deleted: When set, include deleted dashboards.
        """
        params: dict[str, Any] = {}
        if filter_shared is not None:
            params["filter[shared]"] = filter_shared
        if filter_deleted is not None:
            params["filter[deleted]"] = filter_deleted
        return self._read("GET", "/api/v1/dashboard", params=params or None)

    def delete_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Delete a dashboard by ID."""
        return self._write("DELETE", f"/api/v1/dashboard/{dashboard_id}")

    # ── Error Tracking (v2) ────────────────────────────────────────

    def search_error_tracking_issues(
        self,
        *,
        query: str,
        time_from: int,
        time_to: int,
        track: str = "trace",
        order_by: str | None = None,
        include: str | None = None,
    ) -> dict[str, Any]:
        """Search error tracking issues.

        Args:
            query: Search query (e.g., 'service:my-service')
            time_from: Start timestamp in epoch milliseconds
            time_to: End timestamp in epoch milliseconds
            track: 'trace', 'logs', or 'rum'
            order_by: Sort order (TOTAL_COUNT, FIRST_SEEN, etc.)
            include: Comma-separated related objects to sideload
        """
        body: dict[str, Any] = {
            "data": {
                "attributes": {
                    "query": query,
                    "from": time_from,
                    "to": time_to,
                    "track": track,
                },
                "type": "search_request",
            }
        }
        if order_by:
            body["data"]["attributes"]["order_by"] = order_by

        params = {}
        if include:
            params["include"] = include

        return self._read(
            "POST",
            "/api/v2/error-tracking/issues/search",
            json_body=body,
            params=params,
        )

    def get_error_tracking_issue(
        self,
        issue_id: str,
        *,
        include: str | None = None,
    ) -> dict[str, Any]:
        """Get a single error tracking issue by ID."""
        params = {}
        if include:
            params["include"] = include
        return self._read(
            "GET",
            f"/api/v2/error-tracking/issues/{issue_id}",
            params=params,
        )

    def update_error_tracking_issue_state(
        self,
        issue_id: str,
        *,
        state: str,
    ) -> dict[str, Any]:
        """Update error tracking issue state (OPEN, RESOLVED, IGNORED)."""
        body = {
            "data": {
                "attributes": {"state": state},
                "type": "update_request",
            }
        }
        return self._write(
            "PUT",
            f"/api/v2/error-tracking/issues/{issue_id}/state",
            json_body=body,
        )

    # ── SLOs (v1) ─────────────────────────────────────────────────

    def list_slos(
        self,
        *,
        tags_query: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List SLOs, optionally filtered by the SLOs' own tags.

        ``GET /api/v1/slo`` names this parameter ``tags_query``. It has no
        ``tags`` parameter, and an unknown query parameter is **ignored**
        rather than rejected -- so sending ``tags`` returned every SLO in the
        org while looking like a filtered result. That is the same silent class
        of defect as the monitor ``tags``/``monitor_tags`` confusion, inverted:
        too much data rather than too little.

        Args:
            tags_query: Comma-separated tags to filter by, AND-combined
                (e.g., 'env:prod,team:backend').
            limit: Max number of SLOs to return
            offset: Pagination offset
        """
        params: dict[str, Any] = {}
        if tags_query:
            params["tags_query"] = tags_query
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._read("GET", "/api/v1/slo", params=params or None)

    def get_slo(self, slo_id: str) -> dict[str, Any]:
        """Get a single SLO by ID."""
        return self._read("GET", f"/api/v1/slo/{slo_id}")

    def get_slo_history(
        self,
        slo_id: str,
        *,
        from_ts: int,
        to_ts: int,
    ) -> dict[str, Any]:
        """Get SLO history (status & error budget over a time range).

        Args:
            slo_id: The SLO ID
            from_ts: Start timestamp (epoch seconds)
            to_ts: End timestamp (epoch seconds)
        """
        params: dict[str, Any] = {
            "from_ts": from_ts,
            "to_ts": to_ts,
        }
        return self._read("GET", f"/api/v1/slo/{slo_id}/history", params=params)

    # ── Metrics (v1) ────────────────────────────────────────────────

    def query_timeseries(
        self,
        *,
        query: str,
        from_ts: int,
        to_ts: int,
    ) -> dict[str, Any]:
        """Query a metric timeseries over a time window.

        Note that a query error is reported in the response *body* as
        ``status: "error"`` with an ``error`` message, under an HTTP 200.
        Callers must check ``status`` rather than relying on
        ``DatadogAPIError``.

        Args:
            query: A Datadog metric query, e.g. 'avg:my.metric{*} by {tag}'
            from_ts: Start timestamp (epoch seconds, not milliseconds)
            to_ts: End timestamp (epoch seconds, not milliseconds)
        """
        params: dict[str, Any] = {
            "query": query,
            "from": from_ts,
            "to": to_ts,
        }
        return self._read("GET", "/api/v1/query", params=params)

    def search_metrics(self, *, term: str) -> dict[str, Any]:
        """Search metric names for a substring.

        Returns ``{"results": {"metrics": [...]}}``, where ``metrics`` is
        ``null`` (not an empty list) when nothing matches. Matching is a
        literal substring over recently-reporting metrics, so a '.' is not a
        wildcard and absence is not proof that a metric does not exist.
        """
        return self._read("GET", "/api/v1/search", params={"q": f"metrics:{term}"})

    # ── Workflows (v2) ──────────────────────────────────────────────

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Get a workflow definition by ID."""
        return self._read("GET", f"/api/v2/workflows/{workflow_id}")

    def get_workflow_instance(
        self, workflow_id: str, instance_id: str
    ) -> dict[str, Any]:
        """Get a workflow execution instance."""
        return self._read(
            "GET",
            f"/api/v2/workflows/{workflow_id}/instances/{instance_id}",
        )

    # ── PagerDuty (v1) ──────────────────────────────────────────────

    def get_pagerduty_integration_service(self, service_name: str) -> dict[str, Any]:
        """Get PagerDuty integration service by name."""
        escaped_name = urllib.parse.quote(service_name, safe="")
        return self._read(
            "GET",
            f"/api/v1/integration/pagerduty/configuration/services/{escaped_name}",
        )
