from __future__ import annotations

import email.utils
import json
import os
import random
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
    ) -> bool:
        if status is None:
            # Transport error. Safe to repeat only for reads.
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
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"Invalid JSON response: {e.msg}") from e

            retryable = self._should_retry(write=write, status=status, resp=resp)
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
    ) -> dict[str, Any]:
        """Create a log-based count metric.

        Metrics are computed at ingestion time, so they work regardless
        of whether logs land in standard or flex storage tier.
        """
        attributes: dict[str, Any] = {
            "compute": {"aggregation_type": "count"},
        }
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
        return self._read("GET", f"/api/v2/logs/config/metrics/{metric_id}")

    def list_log_metrics(self) -> dict[str, Any]:
        """List all log-based metrics."""
        return self._read("GET", "/api/v2/logs/config/metrics")

    def delete_log_metric(self, metric_id: str) -> dict[str, Any]:
        """Delete a log-based metric by ID."""
        return self._write("DELETE", f"/api/v2/logs/config/metrics/{metric_id}")

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
        tags: list[str] | None = None,
        name: str | None = None,
        page: int = 0,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """List monitors (single page).

        The v1 list endpoint returns a bare JSON array (not a wrapped object).
        Pagination is page-based: pass `page=N` to fetch the (N+1)th page.
        Datadog silently caps `page_size` at 1000.

        Args:
            tags: Filter by monitor tags. Multiple values are AND-combined
                (DD-side comma join). These are the *monitor's own* tags
                (e.g., 'managed-by:dd-cli'), not the tags on the resources
                the monitor watches.
            name: Substring match on the monitor name (case-insensitive,
                handled DD-side).
            page: Zero-indexed page number.
            page_size: Number of monitors per page (max 1000).
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
        }
        if tags:
            params["tags"] = ",".join(tags)
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
        tags: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List SLOs, optionally filtered by tags.

        Args:
            tags: Comma-separated tags to filter by
                (e.g., 'env:prod,team:backend')
            limit: Max number of SLOs to return
            offset: Pagination offset
        """
        params: dict[str, Any] = {}
        if tags:
            params["tags"] = tags
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
