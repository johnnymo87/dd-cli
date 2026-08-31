from __future__ import annotations

import datetime
import json
import math
import re
import time
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

import click

from .anchors import (
    Anchor,
    collision_direction,
    extract_quoted_phrases,
    find_collisions,
    harvest_anchors,
)
from .http import (
    DatadogAPIError,
    DatadogClient,
    LogMetricPathError,
    RetryEvent,
    env,
    validate_log_metric_path,
    validate_log_metric_spec,
)
from .output import (
    EXIT_TRUNCATED,
    REASON_MAX_PAGES,
    REASON_MAX_RESULTS_UNKNOWN,
    REASON_MORE_AVAILABLE,
    REASON_SERVER_TIMEOUT,
    SCHEMA_VERSION,
    PagedResult,
    emit,
    failure_envelope,
    finish,
    success_envelope,
    warn,
)


def _default_site() -> str:
    return env("DD_SITE", "us3.datadoghq.com") or "us3.datadoghq.com"


def _report_retry(event: RetryEvent) -> None:
    """Make waiting visible instead of looking like a hang."""
    warn(str(event))


def _get_client(
    site: str,
    timeout: float = 15.0,
    *,
    max_retries: int | None = None,
) -> DatadogClient:
    """Create a DatadogClient, raising UsageError if DD_PAT is not set.

    Authenticates with a Datadog Personal Access Token (``DD_PAT``, sent as a
    Bearer token).
    """
    pat = env("DD_PAT")
    if not pat:
        raise click.UsageError("DD_PAT (a Datadog Personal Access Token) must be set.")

    if max_retries is None:
        raw = env("DD_MAX_RETRIES")
        try:
            max_retries = int(raw) if raw else 5
        except ValueError as exc:
            raise click.UsageError(
                f"DD_MAX_RETRIES must be an integer, got {raw!r}"
            ) from exc

    return DatadogClient(
        site=site,
        pat=pat,
        timeout=timeout,
        max_retries=max_retries,
        on_retry=_report_retry,
    )


class _ApiFailure(click.ClickException):
    """Failure that still prints a parseable envelope on stdout.

    Click's default behaviour writes only to stderr, leaving stdout empty --
    and empty stdout is exactly how a 429 becomes a 0 in a caller's shell loop.
    """

    exit_code = 1

    def __init__(self, payload: dict[str, Any], message: str) -> None:
        super().__init__(message)
        self.payload = payload

    def show(self, file: Any = None) -> None:
        emit(self.payload)
        click.echo(f"dd-cli: {self.message}", err=True)


def _handle_api_error(
    e: DatadogAPIError, *, extra: dict[str, Any] | None = None
) -> NoReturn:
    """Fail loudly, on stdout as well as stderr, never as an empty result."""
    raise _ApiFailure(
        failure_envelope(
            e,
            status=e.status_code,
            attempts=e.attempts,
            elapsed_s=e.elapsed_s,
            body=e.response_body,
            extra=extra,
        ),
        str(e),
    )


def _handle_runtime_error(
    e: Exception, *, extra: dict[str, Any] | None = None
) -> NoReturn:
    raise _ApiFailure(failure_envelope(e, extra=extra), str(e))


def truncation_option(f: Any) -> Any:
    """Add ``--on-truncation`` to a command that can return a partial answer.

    Deliberately *not* keyed on whether the user passed ``--max-results``
    explicitly. Click can tell the difference, but an exported ``DD_MAX_RESULTS``
    would then silently downgrade every truncation forever -- the exact silent
    partial this exists to prevent. It is also the wrong question: an agent
    reading ``count: 50`` cannot tell whether 50 is the answer or the ceiling,
    and who typed the ceiling does not change that.
    """
    return click.option(
        "--on-truncation",
        type=click.Choice(["exit3", "warn", "error"]),
        default="exit3",
        show_default=True,
        help=(
            "What to do when the answer is incomplete: exit3 (warn + exit 3), "
            "warn (warn + exit 0), or error (fail)."
        ),
    )(f)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """CLI for Datadog APIs (incidents, logs, and more)."""


@cli.command("get-incident")
@click.argument("incident_id", metavar="INCIDENT_ID")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--include",
    "include_param",
    default=None,
    help="Comma-separated related objects to include",
)
@click.option(
    "--enrich",
    is_flag=True,
    help="Fetch additional details (incident type, integrations)",
)
def get_incident_cmd(
    incident_id: str,
    site: str,
    include_param: str | None,
    enrich: bool,
) -> None:
    """Get the details of an incident by ID and print JSON."""
    try:
        with _get_client(site) as dd:
            data = dd.get_incident(incident_id, include=include_param)

            if enrich:
                _enrich_incident(dd, data)

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


def _enrich_incident(dd: DatadogClient, data: dict[str, Any]) -> None:
    """Add enrichment data to incident response (modifies data in place).

    Enrichment is optional, so a failure here does not fail the command -- but
    it is recorded rather than swallowed. A silently missing ``enrichment`` key
    is indistinguishable from an incident that genuinely has no incident type,
    which is the same error-as-absence trap this codebase is closing elsewhere.
    """
    enrichment = data.setdefault("enrichment", {})
    errors: list[dict[str, Any]] = []

    def record(step: str, exc: Exception) -> None:
        errors.append(
            {
                "step": step,
                "status": getattr(exc, "status_code", None),
                "message": str(exc),
            }
        )

    try:
        incident_type_uuid = (
            data.get("data", {}).get("attributes", {}).get("incident_type_uuid")
        )

        if incident_type_uuid:
            try:
                enrichment["incident_type"] = dd.get_incident_type(incident_type_uuid)
            except (DatadogAPIError, RuntimeError) as e:
                record("incident_type", e)

        incident_id = data.get("data", {}).get("id", "")
        if incident_id:
            try:
                enrichment["integrations"] = dd.get_incident_integrations(incident_id)
            except (DatadogAPIError, RuntimeError) as e:
                record("integrations", e)

    except Exception as e:  # pragma: no cover - defensive
        record("enrichment", e)

    enrichment["partial"] = bool(errors)
    if errors:
        enrichment["errors"] = errors
        warn(
            f"incident enrichment is INCOMPLETE: "
            f"{', '.join(e['step'] for e in errors)} failed"
        )


@cli.command("update-incident")
@click.argument("incident_id", metavar="INCIDENT_ID")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option("--title", help="Update incident title")
@click.option("--severity", help="Update incident severity (e.g., SEV-1, SEV-2)")
@click.option("--state", help="Update incident state (active, stable, resolved)")
@click.option("--customer-impacted", type=bool, help="Update customer impact flag")
@click.option("--customer-impact-scope", help="Update customer impact description")
@click.option(
    "--field",
    multiple=True,
    help="Update custom field (format: key=value, can be used multiple times)",
)
def update_incident_cmd(
    incident_id: str,
    site: str,
    title: str | None,
    severity: str | None,
    state: str | None,
    customer_impacted: bool | None,
    customer_impact_scope: str | None,
    field: tuple[str, ...],
) -> None:
    """Update an incident by ID."""
    attributes = _build_update_attributes(
        title=title,
        severity=severity,
        state=state,
        customer_impacted=customer_impacted,
        customer_impact_scope=customer_impact_scope,
        field=field,
    )

    if not attributes:
        raise click.UsageError(
            "No updates specified. Use --help to see available options."
        )

    try:
        with _get_client(site) as dd:
            data = dd.update_incident(incident_id, attributes=attributes)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


def _build_update_attributes(
    *,
    title: str | None,
    severity: str | None,
    state: str | None,
    customer_impacted: bool | None,
    customer_impact_scope: str | None,
    field: tuple[str, ...],
) -> dict[str, Any]:
    """Build the attributes dict for incident update."""
    attributes: dict[str, Any] = {}

    if title is not None:
        attributes["title"] = title
    if severity is not None:
        attributes["severity"] = severity
    if state is not None:
        attributes["state"] = state
    if customer_impacted is not None:
        attributes["customer_impacted"] = customer_impacted
    if customer_impact_scope is not None:
        attributes["customer_impact_scope"] = customer_impact_scope

    if field:
        fields = _parse_custom_fields(field)
        if fields:
            attributes["fields"] = fields

    return attributes


def _parse_custom_fields(field: tuple[str, ...]) -> dict[str, Any]:
    """Parse --field key=value arguments into Datadog field format."""
    fields: dict[str, Any] = {}

    for f in field:
        if "=" not in f:
            raise click.UsageError(f"Invalid field format: {f}. Use key=value format.")

        key, value = f.split("=", 1)

        # Determine field type based on field name
        field_type = "textbox"
        if key in ["severity", "state", "detection_method"]:
            field_type = "dropdown"
        elif key in ["teams", "services"]:
            field_type = "autocomplete"
        elif key in ["trigger", "root_cause_type", "impact_type"]:
            field_type = "multiselect"

        # Convert value based on field type
        if field_type == "multiselect":
            field_value: Any = [value] if value else None
        elif field_type == "autocomplete" and value:
            field_value = [value] if not value.startswith("[") else value
        else:
            field_value = value if value else None

        fields[key] = {"type": field_type, "value": field_value}

    return fields


@cli.command("validate")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
def validate_cmd(site: str) -> None:
    """Validate the active Datadog credential against /api/v1/validate.

    Uses DD_PAT (Bearer) when set, otherwise the DD_API_KEY / DD_APP_KEY pair.
    """
    try:
        with _get_client(site) as dd:
            data = dd.validate()
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps({"status": 200, **data}, indent=2))


@cli.command("search-logs")
@click.argument("query", metavar="QUERY")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--from",
    "time_from",
    default="now-15m",
    show_default=True,
    help="Start time (e.g., now-1h, now-15m)",
)
@click.option(
    "--to",
    "time_to",
    default="now",
    show_default=True,
    help="End time (e.g., now)",
)
@click.option("--limit", default=100, show_default=True, help="Max logs per page")
@click.option(
    "--storage-tier",
    type=click.Choice(["indexes", "online-archives", "flex"]),
    help="Storage tier to search",
)
@click.option("--all-pages", is_flag=True, help="Fetch all pages (up to --max-pages)")
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Page cap for --all-pages. Hitting it marks the result truncated.",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds (increase for flex tier)",
)
@click.option(
    "--max-results",
    type=int,
    default=None,
    help="Stop fetching after this many results (use with --all-pages)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "jsonl", "messages"]),
    default="json",
    show_default=True,
    help="Output format: json, jsonl (one per line), messages (message only)",
)
@truncation_option
def search_logs_cmd(
    query: str,
    site: str,
    time_from: str,
    time_to: str,
    limit: int,
    storage_tier: str | None,
    all_pages: bool,
    max_pages: int,
    timeout: float,
    max_results: int | None,
    output_format: str,
    on_truncation: str,
) -> None:
    """Search logs with Datadog query syntax.

    Example: dd-cli search-logs 'env:prod service:(svc1 OR svc2) order-123'
    """
    page_cap = max_pages if all_pages else 1

    try:
        with _get_client(site, timeout=timeout) as dd:
            result = _paginate_logs(
                dd,
                query=query,
                time_from=time_from,
                time_to=time_to,
                limit=limit,
                storage_tier=storage_tier,
                page_cap=page_cap,
                max_results=max_results,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    payload = _output_logs(result, output_format)
    finish(result, payload, on_truncation=on_truncation, describe="search-logs")


def _paginate_logs(
    dd: DatadogClient,
    *,
    query: str,
    time_from: str,
    time_to: str,
    limit: int,
    storage_tier: str | None,
    page_cap: int,
    max_results: int | None,
) -> PagedResult:
    """Fetch log pages, tracking whether the answer ended up complete."""
    cursor: str | None = None
    logs: list[dict[str, Any]] = []
    warnings: list[Any] = []
    truncated = False
    reason: str | None = None
    pages = 0

    for _ in range(page_cap):
        data = dd.search_logs(
            query=query,
            time_from=time_from,
            time_to=time_to,
            limit=limit,
            cursor=cursor,
            storage_tier=storage_tier,
        )
        pages += 1

        batch = data.get("data", [])
        if not isinstance(batch, list):
            # Never let an unexpected shape contribute 0 to a count.
            raise RuntimeError(
                "logs search expected 'data' to be a JSON array, got "
                f"{type(batch).__name__}: {str(batch)[:200]}"
            )
        logs.extend(batch)

        meta = data.get("meta") or {}

        # HTTP 200 with a short body: a flex query that timed out server-side.
        # No error to retry, no cap to detect -- only meta says so.
        page_warnings = meta.get("warnings") or []
        if page_warnings:
            warnings.extend(page_warnings)
        status = meta.get("status")
        if (status is not None and status != "done") or page_warnings:
            truncated = True
            reason = REASON_SERVER_TIMEOUT

        cursor = (meta.get("page") or {}).get("after")

        if max_results is not None and len(logs) >= max_results:
            if len(logs) > max_results or cursor:
                truncated = True
                reason = reason or REASON_MORE_AVAILABLE
            logs = logs[:max_results]
            cursor = cursor if cursor else None
            break

        if not cursor:
            break
    else:
        # Loop ran to the page cap. A live cursor means a short answer.
        if cursor:
            truncated = True
            reason = REASON_MAX_PAGES

    return PagedResult(
        items=logs,
        truncated=truncated,
        truncation_reason=reason,
        pages_fetched=pages,
        next_cursor=cursor,
        warnings=warnings or None,
    )


def _output_logs(result: PagedResult, output_format: str) -> dict[str, Any] | None:
    """Output logs in the specified format.

    Only the ``json`` format can carry the truncation flag in-band. ``jsonl``
    and ``messages`` signal out of band (stderr + exit code); injecting a
    sentinel record would corrupt a jq pipeline.
    """
    logs = result.items
    if output_format == "json":
        # Returned, not emitted: finish() prints it, so that --on-truncation
        # error can mark the envelope ok:false rather than contradicting itself.
        return success_envelope(logs, result=result)

    if output_format == "jsonl":
        for log in logs:
            click.echo(json.dumps(log))
    elif output_format == "messages":
        for log in logs:
            # Emit even an empty message: dropping it would make the line count
            # disagree with the record count. (A message containing a newline
            # already makes line-counting unsafe -- never count these lines.)
            click.echo(log.get("attributes", {}).get("message", ""))

    warn(f"count={len(logs)} truncated={str(result.truncated).lower()}")
    return None


# http.py speaks in keyword-argument names; the CLI user typed flags. Rewrite
# the library's vocabulary so the advice names something they can actually pass.
_LOG_METRIC_FLAG_NAMES: dict[str, str] = {
    "aggregation_type": "--aggregation-type",
    "compute path": "--path",
    "include_percentiles": "--include-percentiles",
    "allow_bare_paths=True": "--allow-bare-path",
}


def _log_metric_flag_advice(message: str) -> str:
    for kwarg, flag in _LOG_METRIC_FLAG_NAMES.items():
        message = message.replace(kwarg, flag)
    return message


@cli.command("create-log-metric")
@click.argument("metric_id", metavar="METRIC_ID")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--query",
    required=True,
    help="Log search query (same syntax as Log Explorer)",
)
@click.option(
    "--group-by",
    multiple=True,
    help=(
        "Group by attribute path (repeatable, e.g., --group-by service). "
        "Custom log attributes need a leading '@' (e.g. --group-by @topic)"
    ),
)
@click.option(
    "--aggregation-type",
    type=click.Choice(["count", "distribution"]),
    default="count",
    show_default=True,
    help="count matching logs, or measure a numeric value as a distribution",
)
@click.option(
    "--path",
    default=None,
    help=(
        "Log attribute to aggregate, required for --aggregation-type "
        "distribution (e.g. '@duration'). Not valid for count"
    ),
)
@click.option(
    "--include-percentiles/--no-include-percentiles",
    "include_percentiles",
    default=None,
    help="Include p50/p75/p90/p95/p99 aggregations (distribution only)",
)
@click.option(
    "--allow-bare-path",
    "allow_bare_paths",
    is_flag=True,
    default=False,
    help=(
        "Permit a path with no leading '@'. Only for tag keys or reserved "
        "attributes -- a bare CUSTOM attribute silently yields no data"
    ),
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def create_log_metric_cmd(
    metric_id: str,
    site: str,
    query: str,
    group_by: tuple[str, ...],
    aggregation_type: str,
    path: str | None,
    include_percentiles: bool | None,
    allow_bare_paths: bool,
    timeout: float,
) -> None:
    """Create a log-based metric: count, or distribution of a numeric value.

    Computed at ingestion time, so it works with all storage tiers including
    flex, and is available as a custom metric for dashboards and monitors.

    A distribution measures a numeric attribute off each matching log and
    needs --path. Custom log attributes MUST be written with a leading '@'
    ('@duration', not 'duration'): Datadog accepts a bare path with 200 OK and
    then silently produces no data forever. dd-cli refuses those up front --
    use --allow-bare-path if the path really is a tag key or reserved
    attribute (service, env, host, status, ...).

    Example: dd-cli create-log-metric kafka.unknown_topic_errors \\
        --query 'service:my-worker UNKNOWN_TOPIC_OR_PARTITION' \\
        --group-by service --group-by env

    Example: dd-cli create-log-metric fbm.attention_open \\
        --query 'service:fbm @fbm.attention_open:*' \\
        --aggregation-type distribution --path '@fbm.attention_open' \\
        --include-percentiles --group-by service --group-by env
    """
    group_by_list = [{"path": g, "tag_name": g.lstrip("@")} for g in group_by] or None

    # Validate here, not only inside the client: a bad path must never cost a
    # request, and the failure must read as a usage error rather than an API
    # error.
    try:
        validate_log_metric_spec(
            aggregation_type=aggregation_type,
            path=path,
            include_percentiles=include_percentiles,
            group_by=group_by_list,
            allow_bare_paths=allow_bare_paths,
        )
    except LogMetricPathError as e:
        raise click.UsageError(str(e)) from None
    except ValueError as e:
        raise click.UsageError(_log_metric_flag_advice(str(e))) from None

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.create_log_metric(
                metric_id=metric_id,
                query=query,
                group_by=group_by_list,
                aggregation_type=aggregation_type,
                path=path,
                include_percentiles=include_percentiles,
                allow_bare_paths=allow_bare_paths,
            )
    except LogMetricPathError as e:
        raise click.UsageError(str(e)) from None
    except ValueError as e:
        raise click.UsageError(_log_metric_flag_advice(str(e))) from None
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


# ── Reading log metrics (list / get / audit) ────────────────────────────
#
# The documented trap for everything below is a fetch loop that quietly comes
# back short. A per-metric enumeration once retrieved 41 of 62 filters and
# reported "no anchor collisions" with exactly the confidence of a complete
# run. Every read path here therefore asserts that what it fetched matches what
# the org enumerated, and fails loudly instead of answering from a subset.


class LogMetricHarvest(NamedTuple):
    """Every log metric in the org, plus proof the harvest was complete."""

    metrics: list[dict[str, Any]]
    enumerated_ids: list[str]
    detail_fetched: int


class IncompleteHarvest(RuntimeError):
    """The set fetched does not match the set the org enumerated."""


def _harvest_log_metrics(
    dd: DatadogClient, *, detail: bool = False
) -> LogMetricHarvest:
    """Fetch all log metrics, refusing to return a set it cannot vouch for.

    ``GET /api/v2/logs/config/metrics`` already returns each metric's full
    attributes, so ``detail`` (a per-metric GET of every id) is off by default:
    it costs one request per metric against an endpoint that rate-limits
    readily, and buys nothing but a second opinion. When it is on, the second
    opinion is checked -- id for id, not merely counted -- because the failure
    this guards against is a loop that ends early, and a loop that ends early
    still returns a perfectly well-formed list.
    """
    listed = dd.list_log_metrics()

    enumerated_ids: list[str] = []
    for entry in listed:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise IncompleteHarvest(
                "a log-metric record came back with no 'id'; refusing to audit "
                f"an unidentifiable set. Record: {str(entry)[:200]}"
            )
        enumerated_ids.append(str(entry["id"]))

    duplicates = sorted({i for i in enumerated_ids if enumerated_ids.count(i) > 1})
    if duplicates:
        raise IncompleteHarvest(
            "the log-metric list contained duplicate ids "
            f"({', '.join(duplicates)}); a count-based completeness check "
            "cannot be trusted against it."
        )

    if not detail:
        return LogMetricHarvest(
            metrics=listed, enumerated_ids=enumerated_ids, detail_fetched=0
        )

    fetched: list[dict[str, Any]] = []
    for metric_id in enumerated_ids:
        # No try/except here on purpose. Swallowing a per-metric failure is
        # precisely how 41-of-62 happened: the loop kept going, the answer kept
        # its shape, and nothing in the output said a word about it.
        payload = dd.get_log_metric(metric_id)
        data = (payload or {}).get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise IncompleteHarvest(
                f"GET of log metric {metric_id!r} returned no usable 'data'; "
                f"refusing to audit a partial set. Response: {str(payload)[:200]}"
            )
        fetched.append(data)

    fetched_ids = [str(m["id"]) for m in fetched]
    if sorted(fetched_ids) != sorted(enumerated_ids):
        missing = sorted(set(enumerated_ids) - set(fetched_ids))
        unexpected = sorted(set(fetched_ids) - set(enumerated_ids))
        raise IncompleteHarvest(
            f"fetched {len(fetched_ids)} of {len(enumerated_ids)} enumerated "
            f"log metrics. Missing: {missing or 'none'}. Unexpected: "
            f"{unexpected or 'none'}. A short harvest reports 'no collisions' "
            "exactly as confidently as a complete one, so this run is failed "
            "rather than answered."
        )

    return LogMetricHarvest(
        metrics=fetched, enumerated_ids=enumerated_ids, detail_fetched=len(fetched)
    )


def _completeness(harvest: LogMetricHarvest) -> dict[str, Any]:
    """The completeness claim, spelled out in the envelope."""
    return {
        "enumerated": len(harvest.enumerated_ids),
        "fetched": len(harvest.metrics),
        "per_metric_fetches": harvest.detail_fetched,
        "asserted_equal": True,
    }


def _log_metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
    attrs = metric.get("attributes") or {}
    compute = attrs.get("compute") or {}
    query = (attrs.get("filter") or {}).get("query") or ""
    return {
        "id": metric.get("id"),
        "aggregation_type": compute.get("aggregation_type"),
        "path": compute.get("path"),
        "include_percentiles": compute.get("include_percentiles"),
        "query": query,
        "group_by": [g.get("path") for g in (attrs.get("group_by") or [])],
        "quoted_phrases": extract_quoted_phrases(query),
    }


@cli.command("list-log-metrics")
@click.option(
    "--contains",
    default=None,
    help=(
        "Keep only metrics whose id or filter query contains this substring "
        "(case-insensitive, applied client-side after the full fetch)."
    ),
)
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help=(
        "Additionally GET each metric individually and assert the two views "
        "agree. Costs one request per metric; the list already carries full "
        "attributes, so this is a second opinion, not a completeness fix."
    ),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
    help=(
        "summary: {id, aggregation_type, path, query, group_by, "
        "quoted_phrases} per metric. json: full resource objects. jsonl: one "
        "full resource per line, no wrapper."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def list_log_metrics_cmd(
    contains: str | None,
    detail: bool,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """List every log-based metric in the org.

    The endpoint is unpaginated, so there is no cursor whose absence could
    prove the answer complete. dd-cli therefore checks the shape of the
    response and, with --detail, that a per-metric fetch of every enumerated id
    returns exactly that set -- a fetch loop that ends early is the documented
    way this audit goes quietly wrong.

    \b
    Examples:
      dd-cli list-log-metrics
      dd-cli list-log-metrics --contains fulfillment --format json
      dd-cli list-log-metrics --detail | jq '.completeness'
    """
    filters = {"contains": contains, "detail": detail}
    try:
        with _get_client(site, timeout=timeout) as dd:
            harvest = _harvest_log_metrics(dd, detail=detail)
    except DatadogAPIError as e:
        _handle_api_error(e, extra={"filters": filters})
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"filters": filters})

    metrics = harvest.metrics
    if contains:
        needle = contains.lower()
        metrics = [
            m
            for m in metrics
            if needle in str(m.get("id", "")).lower()
            or needle
            in str(
                ((m.get("attributes") or {}).get("filter") or {}).get("query") or ""
            ).lower()
        ]
        if not metrics:
            warn(
                f"0 of {len(harvest.metrics)} log metrics match --contains "
                f"{contains!r} (matched against the metric id and its filter "
                "query). The fetch itself was complete."
            )

    extra = {
        "filters": filters,
        "completeness": _completeness(harvest),
        "matched": len(metrics),
    }

    if output_format == "jsonl":
        for metric in metrics:
            click.echo(json.dumps(metric))
        warn(
            f"count={len(metrics)} of {len(harvest.enumerated_ids)} enumerated; "
            "use --format json for the machine-readable envelope."
        )
        return

    data = (
        metrics
        if output_format == "json"
        else [_log_metric_summary(m) for m in metrics]
    )
    emit(success_envelope(data, extra=extra))


@cli.command("get-log-metric")
@click.argument("metric_id", metavar="METRIC_ID")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_log_metric_cmd(metric_id: str, site: str, timeout: float) -> None:
    """Get one log-based metric by ID, with its quoted anchor phrases.

    The anchor phrases are pulled out of the filter query because they are the
    part with blast radius: Datadog matches them as case-insensitive substrings
    at intake, so any log line that happens to contain one feeds this metric.

    Example: dd-cli get-log-metric orders.reserve_inventory_noop
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            payload = dd.get_log_metric(metric_id)
    except DatadogAPIError as e:
        _handle_api_error(e, extra={"metric_id": metric_id})
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"metric_id": metric_id})

    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        _handle_runtime_error(
            RuntimeError(
                f"GET of log metric {metric_id!r} returned no 'data' object: "
                f"{str(payload)[:200]}"
            ),
            extra={"metric_id": metric_id},
        )

    query = ((data.get("attributes") or {}).get("filter") or {}).get("query") or ""
    emit(
        success_envelope(
            data,
            extra={
                "metric_id": metric_id,
                "anchor_phrases": extract_quoted_phrases(query),
            },
        )
    )


def _pick_positive_control(anchors: list[Anchor]) -> str | None:
    """Choose a control string that MUST collide if the harvest is real.

    The longest harvested phrase, ties broken lexicographically for
    determinism. It is a self-test of the harvest and the matcher, not of
    completeness: it proves the machinery is looking at real anchors, which is
    the difference between "no collisions" and "no data".

    Only phrases that collide with *themselves* are eligible. A phrase of pure
    wildcards ("*") has no literal segment to match on, so choosing it would
    fail the control and void a perfectly good audit -- a false alarm in the
    one mechanism whose job is to be trusted.
    """
    self_matching = [
        a.phrase for a in anchors if collision_direction(a.phrase, a.phrase) is not None
    ]
    if not self_matching:
        return None
    return max(self_matching, key=lambda p: (len(p), p))


@cli.command("audit-log-metric-anchors")
@click.argument("candidate", metavar="CANDIDATE_STRING")
@click.option(
    "--positive-control",
    default=None,
    help=(
        "A string that MUST collide. If it does not, the whole run is reported "
        "ok:false, because a silently-empty harvest is indistinguishable from a "
        "clean audit. Defaults to the longest phrase found in the harvest."
    ),
)
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help=(
        "Also GET each metric individually and assert the per-metric fetch "
        "returns exactly the enumerated set."
    ),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json"]),
    default="summary",
    show_default=True,
    help="summary and json carry the same envelope; json adds every anchor.",
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def audit_log_metric_anchors_cmd(
    candidate: str,
    positive_control: str | None,
    detail: bool,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Check a proposed log string against every live log-metric anchor.

    Datadog matches a quoted phrase in a log-metric filter as a
    CASE-INSENSITIVE SUBSTRING, at INTAKE. So a new log line that merely
    contains someone else's anchor starts feeding their counter, silently and
    permanently -- log metrics do not backfill, so the contamination cannot be
    undone by fixing the string later.

    That is a measured failure, not a hypothetical: a new log line for a
    retirement code path was worded so that it contained the quoted anchor of
    an unrelated metric counting a no-op code path. Tens of thousands of
    retirement events were counted as no-ops, in a metric a monitor alerted on.

    \b
    Collisions are reported in BOTH directions:
      anchor_in_candidate  an existing anchor occurs inside your string --
                           shipping it feeds that metric.
      candidate_in_anchor  your string occurs inside an existing anchor --
                           a metric you build on it would eat their events.

    The run carries a POSITIVE CONTROL: a string that must produce a hit. If
    the control misses, the audit reports ok:false and exits non-zero, because
    an empty harvest and a clean org look identical in the output.

    \b
    Examples:
      dd-cli audit-log-metric-anchors 'Order retired: declining to reserve inventory'
      dd-cli audit-log-metric-anchors 'Order retired: refusing to reserve inventory' \\
        --positive-control 'Refusing to reserve inventory'
    """
    if not candidate.strip():
        raise click.UsageError(
            "CANDIDATE_STRING is empty. An empty string is a substring of "
            "every anchor, so the audit would report every metric as a "
            "collision and mean nothing."
        )

    try:
        with _get_client(site, timeout=timeout) as dd:
            harvest = _harvest_log_metrics(dd, detail=detail)
    except DatadogAPIError as e:
        _handle_api_error(e, extra={"candidate": candidate})
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"candidate": candidate})

    anchors = harvest_anchors(harvest.metrics)
    distinct_phrases = sorted({a.phrase for a in anchors})
    with_phrases = len({a.metric_id for a in anchors})

    hits = find_collisions(candidate, anchors)

    control = positive_control or _pick_positive_control(anchors)
    control_hits = find_collisions(control, anchors) if control else []
    control_ok = bool(control_hits)

    checked = {
        "metrics": len(harvest.metrics),
        "metrics_with_quoted_phrase": with_phrases,
        "phrases": len(anchors),
        "distinct_phrases": len(distinct_phrases),
    }
    extra: dict[str, Any] = {
        "candidate": candidate,
        "checked": checked,
        "completeness": _completeness(harvest),
        "positive_control": {
            "value": control,
            "source": "explicit" if positive_control else "derived_longest_phrase",
            "ok": control_ok,
            "hit_count": len(control_hits),
            "hit_metric_ids": sorted({h["metric_id"] for h in control_hits}),
        },
    }
    if output_format == "json":
        extra["anchors"] = [
            {"metric_id": a.metric_id, "phrase": a.phrase, "wildcard": a.wildcard}
            for a in anchors
        ]

    payload = success_envelope(hits, extra=extra)

    # Report the denominator on stderr too: 1 hit in 62 filters is a density
    # that survives eyeballing, and "0 hits" is only meaningful next to the
    # number of things actually checked.
    warn(
        f"audit checked {checked['metrics']} metrics / "
        f"{checked['distinct_phrases']} distinct quoted phrases "
        f"({checked['phrases']} total, {checked['metrics_with_quoted_phrase']} "
        f"metrics carry one); {len(hits)} collision(s)."
    )

    if not control_ok:
        reason = (
            "no log-metric filter carries a quoted phrase, so there was "
            "nothing to audit against"
            if control is None
            else f"positive control {control!r} produced no hit"
        )
        payload = {
            **payload,
            "ok": False,
            "data": None,
            "count": None,
            "error": {
                "message": (
                    f"positive control FAILED: {reason}. The collision result "
                    "above is not trustworthy -- an empty harvest and a clean "
                    "org produce the same empty answer, so this run refuses to "
                    "claim the candidate is safe."
                ),
            },
        }
        raise _ApiFailure(payload, "positive control failed; audit result void")

    emit(payload)


# ── Writing log metrics (update / delete) ───────────────────────────────
#
# Both commands carry the same warning, for the same reason: a log metric is
# computed at INTAKE and never backfills. Nothing you do here can be validated
# against history, and a mistyped filter produces a permanently empty series
# that is indistinguishable from a healthy quiet one.

_NO_BACKFILL_WARNING = (
    "log metrics are computed at INTAKE and do NOT backfill: this change "
    "cannot be validated against historical data, and a mistyped filter "
    "yields a permanently empty series that reads exactly like 'healthy'. "
    "Verify with `dd-cli query-metrics` within the hour, before you trust it."
)


@cli.command("update-log-metric")
@click.argument("metric_id", metavar="METRIC_ID")
@click.option(
    "--query",
    default=None,
    help="New filter query (replaces the existing filter.query entirely).",
)
@click.option(
    "--group-by",
    multiple=True,
    help=(
        "Group-by attribute path (repeatable). REPLACES the existing group_by "
        "list wholesale -- Datadog's PATCH has no per-entry merge. Custom "
        "attributes need a leading '@'."
    ),
)
@click.option(
    "--clear-group-by",
    is_flag=True,
    default=False,
    help="Remove every group_by entry (sends an empty list).",
)
@click.option(
    "--include-percentiles/--no-include-percentiles",
    "include_percentiles",
    default=None,
    help="Toggle p50-p99 aggregations. Distribution metrics only.",
)
@click.option(
    "--allow-bare-path",
    "allow_bare_paths",
    is_flag=True,
    default=False,
    help="Permit a group-by path with no leading '@' (tag keys only).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the before/after and send nothing.",
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def update_log_metric_cmd(
    metric_id: str,
    query: str | None,
    group_by: tuple[str, ...],
    clear_group_by: bool,
    include_percentiles: bool | None,
    allow_bare_paths: bool,
    dry_run: bool,
    site: str,
    timeout: float,
) -> None:
    """Update a log-based metric's filter, group_by, or percentiles.

    \b
    SAFETY -- read before mutating a metric a monitor depends on:
      A log metric is computed at INTAKE and does NOT backfill. Editing the
      filter can therefore never be validated against historical data, and a
      mistyped filter yields a permanently empty series that looks exactly like
      a healthy quiet one. A monitor watching it will not alert; it will simply
      stop having anything to say.
      Prefer creating a NARROWED metric alongside the existing one and
      repointing the monitor only after the new metric has produced a real
      emission. That path is reversible; this one is not.

    \b
    Datadog's PATCH accepts three fields and no others:
      filter.query, group_by, compute.include_percentiles
    compute.aggregation_type and compute.path are fixed at creation. A metric
    that needs a different one must be recreated under a new name.

    \b
    Examples:
      dd-cli update-log-metric my.metric --query 'service:x "new phrase"' --dry-run
      dd-cli update-log-metric my.metric --group-by service --group-by env
    """
    if group_by and clear_group_by:
        raise click.UsageError(
            "--group-by and --clear-group-by contradict each other; pass one."
        )
    if query is not None and not query.strip():
        raise click.UsageError(
            "--query is empty. Datadog treats a missing filter as '*', so an "
            "empty query would silently widen the metric to every log in the "
            "org rather than narrow it."
        )

    group_by_list: list[dict[str, str]] | None = None
    if clear_group_by:
        group_by_list = []
    elif group_by:
        group_by_list = [{"path": g, "tag_name": g.lstrip("@")} for g in group_by]

    if query is None and group_by_list is None and include_percentiles is None:
        raise click.UsageError(
            "Nothing to update. Pass --query, --group-by/--clear-group-by, or "
            "--include-percentiles/--no-include-percentiles. (Datadog's PATCH "
            "accepts only those; aggregation type and compute path are fixed "
            "at creation.)"
        )

    for entry in group_by_list or []:
        try:
            validate_log_metric_path(
                entry["path"], kind="group_by path", allow_bare=allow_bare_paths
            )
        except LogMetricPathError as e:
            raise click.UsageError(str(e)) from None

    extra_ctx = {"metric_id": metric_id, "dry_run": dry_run}
    try:
        with _get_client(site, timeout=timeout) as dd:
            before_payload = dd.get_log_metric(metric_id)
            before = (before_payload or {}).get("data")
            if not isinstance(before, dict):
                _handle_runtime_error(
                    RuntimeError(
                        f"GET of log metric {metric_id!r} returned no 'data'; "
                        "refusing to PATCH a metric whose current state is "
                        f"unknown. Response: {str(before_payload)[:200]}"
                    ),
                    extra=extra_ctx,
                )

            aggregation = ((before.get("attributes") or {}).get("compute") or {}).get(
                "aggregation_type"
            )
            if include_percentiles is not None and aggregation != "distribution":
                raise click.UsageError(
                    f"--include-percentiles applies to distribution metrics; "
                    f"{metric_id!r} is a {aggregation!r} metric. Datadog would "
                    "either reject this or accept it and change nothing."
                )

            after_preview = _preview_log_metric_update(
                before,
                query=query,
                group_by=group_by_list,
                include_percentiles=include_percentiles,
            )
            changes = _log_metric_changes(before, after_preview)

            if dry_run:
                emit(
                    success_envelope(
                        {
                            "dry_run": True,
                            "before": before,
                            "after": after_preview,
                            "changes": changes,
                            "sent_nothing": True,
                        },
                        extra={**extra_ctx, "warning": _NO_BACKFILL_WARNING},
                    )
                )
                warn(_NO_BACKFILL_WARNING)
                return

            dd.update_log_metric(
                metric_id,
                query=query,
                group_by=group_by_list,
                include_percentiles=include_percentiles,
                allow_bare_paths=allow_bare_paths,
            )
            # Re-read rather than trusting the PATCH response: what the server
            # now holds is the only after-state worth printing.
            after_payload = dd.get_log_metric(metric_id)
            after = (after_payload or {}).get("data")
            if not isinstance(after, dict):
                _handle_runtime_error(
                    RuntimeError(
                        f"the PATCH of {metric_id!r} was sent, but re-reading "
                        "the metric returned no 'data'. The change may have "
                        f"landed. Response: {str(after_payload)[:200]}"
                    ),
                    extra=extra_ctx,
                )
    except LogMetricPathError as e:
        raise click.UsageError(str(e)) from None
    except ValueError as e:
        raise click.UsageError(_log_metric_flag_advice(str(e))) from None
    except DatadogAPIError as e:
        _handle_api_error(e, extra=extra_ctx)
    except RuntimeError as e:
        _handle_runtime_error(e, extra=extra_ctx)

    emit(
        success_envelope(
            {
                "dry_run": False,
                "before": before,
                "after": after,
                "changes": _log_metric_changes(before, after),
            },
            extra={**extra_ctx, "warning": _NO_BACKFILL_WARNING},
        )
    )
    warn(_NO_BACKFILL_WARNING)


def _preview_log_metric_update(
    before: dict[str, Any],
    *,
    query: str | None,
    group_by: list[dict[str, str]] | None,
    include_percentiles: bool | None,
) -> dict[str, Any]:
    """The state a successful PATCH would produce, computed locally.

    Only used for --dry-run. A real run re-reads the metric instead of trusting
    this, because a prediction that is printed as an observation is its own
    small lie.
    """
    after = json.loads(json.dumps(before))
    attrs = after.setdefault("attributes", {})
    if query is not None:
        attrs["filter"] = {"query": query}
    if group_by is not None:
        attrs["group_by"] = group_by
    if include_percentiles is not None:
        attrs.setdefault("compute", {})["include_percentiles"] = include_percentiles
    return after


def _log_metric_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    """Field-level before/after for the three patchable fields."""
    b_attrs = before.get("attributes") or {}
    a_attrs = after.get("attributes") or {}
    fields = {
        "filter.query": (
            (b_attrs.get("filter") or {}).get("query"),
            (a_attrs.get("filter") or {}).get("query"),
        ),
        "group_by": (b_attrs.get("group_by"), a_attrs.get("group_by")),
        "compute.include_percentiles": (
            (b_attrs.get("compute") or {}).get("include_percentiles"),
            (a_attrs.get("compute") or {}).get("include_percentiles"),
        ),
    }
    return [
        {"field": name, "before": old, "after": new}
        for name, (old, new) in fields.items()
        if old != new
    ]


@cli.command("delete-log-metric")
@click.argument("metric_id", metavar="METRIC_ID")
@click.option(
    "--yes",
    "confirmed",
    is_flag=True,
    default=False,
    help="Confirm the deletion. Required; there is no interactive prompt.",
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def delete_log_metric_cmd(
    metric_id: str, confirmed: bool, site: str, timeout: float
) -> None:
    """Delete a log-based metric. Requires --yes.

    Deletion stops future points; it does not remove the points already
    emitted, and it does not warn you that a monitor is watching. Any monitor
    querying this metric simply goes quiet -- which, for a monitor, reads as
    good news. Check first:

        dd-cli list-monitors --name <metric name fragment>

    The metric's definition is printed before deletion so the deleted state is
    recoverable by hand from the output.
    """
    if not confirmed:
        raise click.UsageError(
            f"refusing to delete log metric {metric_id!r} without --yes. "
            "Deletion cannot be undone, the emitted history stays, and any "
            "monitor on this metric goes silently no-data."
        )

    extra_ctx = {"metric_id": metric_id}
    try:
        with _get_client(site, timeout=timeout) as dd:
            before_payload = dd.get_log_metric(metric_id)
            before = (before_payload or {}).get("data")
            if not isinstance(before, dict):
                _handle_runtime_error(
                    RuntimeError(
                        f"GET of log metric {metric_id!r} returned no 'data'; "
                        "refusing to delete a metric whose definition could "
                        f"not be captured first. Response: "
                        f"{str(before_payload)[:200]}"
                    ),
                    extra=extra_ctx,
                )
            dd.delete_log_metric(metric_id)
    except DatadogAPIError as e:
        _handle_api_error(e, extra=extra_ctx)
    except RuntimeError as e:
        _handle_runtime_error(e, extra=extra_ctx)

    emit(
        success_envelope({"deleted": metric_id, "definition": before}, extra=extra_ctx)
    )
    warn(
        f"deleted log metric {metric_id!r}. Points already emitted remain in "
        "the metric's history; only future points stop."
    )


# ── Monitor options (shared by create-monitor / update-monitor) ─────────
#
# Datadog keeps almost every monitor behaviour knob inside the `options`
# object of the monitor payload. dd-cli exposes the commonly-needed ones as
# first-class flags, plus a generic `--option KEY=VALUE` escape hatch so a
# missing flag can never block a user again.
#
# Precedence (documented in both commands' --help):
#   1. first-class flags (e.g. --no-data-timeframe) -- highest
#   2. --option KEY=VALUE overrides
#   3. the monitor's existing options (update-monitor only)
#   4. Datadog's own defaults

# Flag dest name -> Datadog options key. All of these are 1:1 today, but the
# indirection keeps the mapping explicit and greppable.
_MONITOR_SIMPLE_OPTION_FLAGS: dict[str, str] = {
    "notify_no_data": "notify_no_data",
    "no_data_timeframe": "no_data_timeframe",
    "on_missing_data": "on_missing_data",
    "new_group_delay": "new_group_delay",
    "evaluation_delay": "evaluation_delay",
    "notify_audit": "notify_audit",
    "include_tags": "include_tags",
    "require_full_window": "require_full_window",
    "timeout_h": "timeout_h",
    "renotify_interval": "renotify_interval",
    "renotify_occurrences": "renotify_occurrences",
    "escalation_message": "escalation_message",
    "group_retention_duration": "group_retention_duration",
    "notification_preset_name": "notification_preset_name",
}

# Flag dest name -> key inside options["thresholds"].
_MONITOR_THRESHOLD_FLAGS: dict[str, str] = {
    "critical": "critical",
    "warning": "warning",
    "critical_recovery": "critical_recovery",
    "warning_recovery": "warning_recovery",
}

_ON_MISSING_DATA_CHOICES = (
    "default",
    "show_no_data",
    "show_and_notify_no_data",
    "resolve",
)

_NOTIFICATION_PRESET_CHOICES = (
    "show_all",
    "hide_query",
    "hide_handles",
    "hide_all",
)


def monitor_option_flags(f: Any) -> Any:
    """Attach the shared monitor `options` flags to a command.

    The decorated command should accept `**monitor_opts` and pass it to
    `_build_monitor_options`.
    """
    options = [
        click.option("--critical", type=float, help="Critical threshold"),
        click.option("--warning", type=float, help="Warning threshold"),
        click.option(
            "--critical-recovery",
            type=float,
            help="Critical recovery threshold (monitor resolves below/above this)",
        ),
        click.option(
            "--warning-recovery", type=float, help="Warning recovery threshold"
        ),
        click.option(
            "--notify-no-data/--no-notify-no-data",
            default=None,
            help=(
                "Alert when no data is received. Requires --no-data-timeframe "
                "(dd-cli refuses the combination without it)."
            ),
        ),
        click.option(
            "--no-data-timeframe",
            type=int,
            help=(
                "Minutes of missing data before the no-data alert fires. "
                "Datadog recommends at least 2x the query's evaluation window."
            ),
        ),
        click.option(
            "--on-missing-data",
            type=click.Choice(_ON_MISSING_DATA_CHOICES),
            help=(
                "Newer missing-data behaviour. Supersedes notify_no_data / "
                "no_data_timeframe on the monitor types that support it."
            ),
        ),
        click.option(
            "--new-group-delay",
            type=int,
            help="Seconds to skip evaluation for newly-detected groups",
        ),
        click.option(
            "--evaluation-delay",
            type=int,
            help="Seconds to delay evaluation (for late-arriving/backfilled data)",
        ),
        click.option(
            "--notify-audit/--no-notify-audit",
            default=None,
            help="Notify tagged handles when the monitor itself is modified",
        ),
        click.option(
            "--include-tags/--no-include-tags",
            default=None,
            help="Include triggering tags in the notification title "
            "(Datadog default: true)",
        ),
        click.option(
            "--require-full-window/--no-require-full-window",
            default=None,
            help="Only evaluate once the full window has data (sparse metrics: off)",
        ),
        click.option(
            "--timeout-h",
            type=int,
            help="Hours before a triggered monitor auto-resolves (options.timeout_h)",
        ),
        click.option(
            "--renotify-interval",
            type=int,
            help="Minutes between re-notifications (0 to disable)",
        ),
        click.option(
            "--renotify-occurrences",
            type=int,
            help="Max number of re-notifications (requires --renotify-interval)",
        ),
        click.option(
            "--renotify-status",
            "renotify_status",
            multiple=True,
            type=click.Choice(["alert", "warn", "no data"]),
            help="Status that triggers re-notification (repeatable)",
        ),
        click.option(
            "--escalation-message",
            help="Message sent on re-notification (requires --renotify-interval)",
        ),
        click.option(
            "--group-retention-duration",
            help="How long inactive groups are retained, e.g. '2d' (60m-72h)",
        ),
        click.option(
            "--notification-preset-name",
            type=click.Choice(_NOTIFICATION_PRESET_CHOICES),
            help="How much monitor detail to show in notifications",
        ),
        click.option(
            "--option",
            "option",
            multiple=True,
            metavar="KEY=VALUE",
            help=(
                "Escape hatch: set any monitor option not covered by a flag. "
                "Repeatable. VALUE is JSON-parsed when it looks like JSON "
                "(numbers, true/false, null, arrays, objects), otherwise it is "
                "kept as a string. First-class flags win on conflict."
            ),
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


# Bare values that are almost certainly a typo rather than data. Python
# spellings of the JSON literals would otherwise be kept as strings, which is
# how `--option notify_no_data=True` could sneak past the no-data guard as the
# string "True"; NaN/Infinity are accepted by json.loads but are not valid
# JSON on the wire.
_LITERAL_LOOKALIKE_VALUES = frozenset(
    {"true", "false", "null", "none", "nan", "infinity", "-infinity"}
)
_JSON_LITERAL_VALUES = frozenset({"true", "false", "null"})


def _reject_json_constant(name: str) -> Any:
    raise click.UsageError(
        f"Malformed --option value: {name} is not valid JSON and cannot be "
        "sent to Datadog."
    )


def _parse_monitor_option_overrides(pairs: tuple[str, ...]) -> dict[str, Any]:
    """Parse repeated ``--option KEY=VALUE`` pairs into an options dict.

    Values are JSON-parsed so numbers, booleans, null, arrays and objects all
    work. A value that is not valid JSON is kept as a plain string (so
    ``--option on_missing_data=resolve`` does the obvious thing), *unless* it
    starts with a JSON structural character, in which case malformed JSON is a
    hard error rather than a silently-stringified typo.

    Bare values that only *look* like literals (Python's ``True``/``None``,
    or ``NaN``/``Infinity``) are rejected outright: silently shipping the
    string ``"True"`` as ``notify_no_data`` would defeat the no-data guard.
    """
    parsed: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw_value = pair.partition("=")
        if not sep:
            raise click.UsageError(
                f"Malformed --option {pair!r}: expected KEY=VALUE, "
                "e.g. --option no_data_timeframe=60 or --option 'notify_by=[\"env\"]'"
            )
        key = key.strip()
        if not key:
            raise click.UsageError(
                f"Malformed --option {pair!r}: the key must not be empty."
            )
        stripped = raw_value.strip()
        if (
            stripped.lower() in _LITERAL_LOOKALIKE_VALUES
            and stripped not in _JSON_LITERAL_VALUES
        ):
            raise click.UsageError(
                f"Malformed --option {pair!r}: {raw_value!r} is not valid JSON. "
                "Use lowercase true/false/null (JSON spelling, not Python's), "
                "and note that NaN/Infinity cannot be sent to Datadog. "
                "If you really mean the literal string, quote it as JSON: "
                f"--option '{key}=\"{raw_value}\"'"
            )
        looks_like_json = raw_value[:1] in {"{", "[", '"'}
        try:
            parsed[key] = json.loads(raw_value, parse_constant=_reject_json_constant)
        except json.JSONDecodeError:
            if looks_like_json:
                raise click.UsageError(
                    f"Malformed --option {pair!r}: the value starts with "
                    f"{raw_value[:1]!r} so it is parsed as JSON, but it is not "
                    "valid JSON."
                ) from None
            parsed[key] = raw_value
    return parsed


# Monitor options that a PUT cannot honestly apply. Both were checked rather
# than assumed: `silenced` against the live API (a PUT carrying
# `silenced: {}` or `silenced: null` answers 200 and leaves the monitor muted
# -- 2026-08-31, monitor 25447403), `device_ids` against Datadog's own OpenAPI
# schema for MonitorOptions, where it is the only property marked
# `readOnly: true`. Every other MonitorOptions property is writable; the
# deprecated-but-writable ones (`groupby_simple_monitor`, `locked`,
# `new_host_delay`, `synthetics_check_id`) are left alone deliberately -- they
# still take effect, and refusing them would break callers for no gain.
#
# The generic backstop for anything not listed here is
# `_reject_unapplied_options`, which compares what came back against what was
# sent. This list is only for the cases where the right answer is a usage
# error naming the command that does work.
_UNAPPLIABLE_MONITOR_OPTIONS: dict[str, str] = {
    "silenced": (
        "options.silenced cannot be written through update-monitor. A PUT that "
        "sets it mutes the monitor, but a PUT that sends {} or null to clear "
        "it returns 200 and leaves the monitor muted -- a silent no-op that "
        "tells an operator a paging monitor is alerting again while it is "
        "still gagged.\n"
        "\n"
        "Use the endpoints that can do both, and that verify the result:\n"
        "  dd-cli mute-monitor MONITOR --until 4h\n"
        "  dd-cli unmute-monitor MONITOR"
    ),
    "device_ids": (
        "options.device_ids is read-only in Datadog's monitor schema, so a PUT "
        "carrying it reports success and changes nothing. Synthetics devices "
        "are managed through the Synthetics API, not the monitor."
    ),
}


def _reject_unappliable_options(options: dict[str, Any]) -> None:
    """Refuse option keys a PUT would accept and then ignore.

    An update that returns 200 without applying the change is worse than a
    failure: the operator gets a success to act on. Fail here instead, naming
    the command that does work.
    """
    for key, explanation in _UNAPPLIABLE_MONITOR_OPTIONS.items():
        if key in options:
            raise click.UsageError(explanation)


def _values_agree(sent: Any, returned: Any) -> bool:
    """Whether Datadog's echoed option value matches what was sent.

    Numeric equality only (10 vs 10.0): anything cleverer would start
    excusing differences, which is the failure this check exists to catch.
    """
    if isinstance(sent, bool) != isinstance(returned, bool):
        return False
    if isinstance(sent, (int, float)) and isinstance(returned, (int, float)):
        return float(sent) == float(returned)
    return bool(sent == returned)


def _unapplied_options(sent: dict[str, Any], returned: Any) -> list[str]:
    """Option keys the PUT response does not show as having been applied.

    Datadog's PUT answers with the updated monitor, so this costs no extra
    request. Only the keys the caller explicitly asked for are compared; the
    merged-in existing options are not the caller's claim.

    A response with no readable `options` object yields no findings: this is a
    check for silent no-ops, and it must not manufacture a failure out of a
    response shape it does not understand (the write may well have landed).
    """
    options = returned.get("options") if isinstance(returned, dict) else None
    if not isinstance(options, dict):
        return []

    missing = object()
    ignored: list[str] = []
    for key, value in sent.items():
        got = options.get(key, missing)
        if value is None:
            # `--option KEY=null` clears the option, and Datadog answers a
            # cleared option by omitting the key rather than echoing null.
            if got is missing or got is None:
                continue
            ignored.append(key)
        elif got is missing or not _values_agree(value, got):
            ignored.append(key)
    return ignored


class MonitorOptionUpdates(NamedTuple):
    """Caller-specified monitor options, plus how to apply them.

    `replace_keys` holds the option keys the caller supplied as a whole object
    (today: `thresholds` via `--option`), which must replace rather than merge
    into the monitor's existing value -- otherwise there is no way to *remove*
    a threshold.
    """

    options: dict[str, Any]
    replace_keys: frozenset[str]


def _build_monitor_options(monitor_opts: dict[str, Any]) -> MonitorOptionUpdates:
    """Assemble the `options` dict from the shared monitor option flags.

    Only options the caller actually specified are included; unset flags are
    omitted entirely so the caller never clobbers a Datadog default (or, on
    update, an existing value) by accident.
    """
    overrides = _parse_monitor_option_overrides(monitor_opts.get("option") or ())
    _reject_unappliable_options(overrides)
    options: dict[str, Any] = dict(overrides)
    replace_keys = frozenset(overrides) & {"thresholds"}

    for dest, key in _MONITOR_SIMPLE_OPTION_FLAGS.items():
        value = monitor_opts.get(dest)
        if value is not None:
            options[key] = value

    statuses = monitor_opts.get("renotify_status") or ()
    if statuses:
        options["renotify_statuses"] = list(statuses)

    thresholds: dict[str, float] = {}
    for dest, key in _MONITOR_THRESHOLD_FLAGS.items():
        value = monitor_opts.get(dest)
        if value is not None:
            thresholds[key] = value
    if thresholds:
        existing = options.get("thresholds")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(thresholds)
        options["thresholds"] = merged

    return MonitorOptionUpdates(options, replace_keys)


def _existing_monitor_options(dd: DatadogClient, monitor_id: str) -> dict[str, Any]:
    """Read a monitor's current `options` object (for read-modify-write)."""
    monitor = dd.get_monitor(monitor_id)
    existing = monitor.get("options")
    return dict(existing) if isinstance(existing, dict) else {}


def _merge_monitor_options(
    existing: dict[str, Any],
    updates: MonitorOptionUpdates,
) -> dict[str, Any]:
    """Merge caller-specified options onto a monitor's existing options.

    Datadog's PUT /api/v1/monitor/{id} replaces the whole `options` object
    rather than merging into it, so sending only the changed keys silently
    resets everything else (thresholds, no_data_timeframe, evaluation_delay
    ...). Merging here keeps the update PATCH-shaped from the caller's point
    of view. `thresholds` is merged one level deeper for the same reason --
    unless the caller passed the whole object via `--option thresholds=...`,
    which replaces (the only way to remove a threshold).
    """
    merged = dict(existing)
    merged.update(updates.options)

    if "thresholds" in updates.options and "thresholds" not in updates.replace_keys:
        prior = existing.get("thresholds")
        base = dict(prior) if isinstance(prior, dict) else {}
        base.update(updates.options["thresholds"])
        merged["thresholds"] = base

    return _drop_conflicting_no_data_family(merged, updates.options)


# Datadog rejects on_missing_data combined with either legacy no-data option
# (verified against POST /api/v1/monitor/validate):
#   "The notify_no_data option is deprecated and cannot be used in
#    combination with the on_missing_data option."
#   "The no_data_timeframe option is deprecated and cannot be used in
#    combination with the on_missing_data option"
# notify_no_data=false alongside on_missing_data IS accepted.
_LEGACY_NO_DATA_KEYS = ("notify_no_data", "no_data_timeframe")
_NO_DATA_KEYS = frozenset({*_LEGACY_NO_DATA_KEYS, "on_missing_data"})


def _uses_legacy_no_data(options: dict[str, Any]) -> bool:
    """True if these options use the legacy no-data family in a way Datadog
    will not accept next to on_missing_data."""
    return (
        options.get("notify_no_data") is True
        or options.get("no_data_timeframe") is not None
    )


def _drop_conflicting_no_data_family(
    merged: dict[str, Any], updates: dict[str, Any]
) -> dict[str, Any]:
    """Keep a read-modify-write update from producing a payload Datadog 400s.

    Merging the caller's no-data settings onto a monitor configured with the
    *other* no-data mechanism would send both families at once. Whichever
    family the caller just asked for wins; the other is dropped from the
    merged options.
    """
    if updates.get("on_missing_data") is not None:
        for key in _LEGACY_NO_DATA_KEYS:
            merged.pop(key, None)
    elif _uses_legacy_no_data(updates):
        merged.pop("on_missing_data", None)
    return merged


def _validate_no_data_family_exclusivity(options: dict[str, Any]) -> None:
    """Refuse a payload that Datadog would reject as self-contradictory."""
    if options.get("on_missing_data") is None or not _uses_legacy_no_data(options):
        return

    raise click.UsageError(
        "on_missing_data cannot be combined with notify_no_data=true or "
        "no_data_timeframe -- Datadog rejects that payload:\n"
        '  "The notify_no_data option is deprecated and cannot be used in '
        'combination with the on_missing_data option."\n'
        "\n"
        "Pick one mechanism: --on-missing-data show_and_notify_no_data (the "
        "modern one, supported by APM/trace-analytics, log, RUM and CI "
        "monitors), or --notify-no-data --no-data-timeframe N (the legacy "
        "pair, for metric/query alerts and service checks)."
    )


_NO_DATA_TIMEFRAME_ADVICE = (
    "notify_no_data=true with no no_data_timeframe.\n"
    "\n"
    "Why this matters: no_data_timeframe is the window Datadog uses to decide "
    "that data has stopped arriving. Datadog nominally falls back to a default "
    "(2x the evaluation window for query alerts, 24h for service checks), but "
    "that implicit default has been observed NOT to page on a silent "
    "trace-analytics group -- a 'no signal' / dead-man-switch monitor that is "
    "itself silently dead, which is exactly the failure mode it exists to "
    "catch. The threshold does not save you either: for count-style queries "
    '(e.g. trace-analytics(...).rollup("count") < 1) a fully silent group '
    "evaluates to No Data, NOT to 0, so '< 1' never triggers. Set the window "
    "explicitly.\n"
    "\n"
    "Fix: add --no-data-timeframe N (minutes; use at least 2x the query's "
    "evaluation window), or use --on-missing-data show_and_notify_no_data on "
    "monitor types that support it, or drop --notify-no-data."
)


def _no_data_config_is_broken(options: dict[str, Any]) -> bool:
    """True if these options ask for no-data alerting that cannot be trusted
    to fire: notify_no_data without an explicit no_data_timeframe."""
    if options.get("notify_no_data") is not True:
        return False
    if options.get("on_missing_data") is not None:
        # on_missing_data supersedes the legacy pair (and is mutually
        # exclusive with it -- see _validate_no_data_family_exclusivity).
        return False
    return options.get("no_data_timeframe") is None


def _validate_no_data_options(options: dict[str, Any]) -> None:
    """Refuse a monitor whose no-data alerting cannot be trusted to fire.

    `notify_no_data: true` without `no_data_timeframe` is the classic silent
    dead-man-switch bug: the monitor that exists to catch silence is itself
    silently dead.
    """
    if not _no_data_config_is_broken(options):
        return

    raise click.UsageError(_NO_DATA_TIMEFRAME_ADVICE)


@cli.command("create-monitor")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option("--name", required=True, help="Monitor name")
@click.option(
    "--type",
    "monitor_type",
    required=True,
    help="Monitor type (e.g., 'metric alert', 'log alert', 'query alert')",
)
@click.option("--query", required=True, help="Monitor query")
@click.option(
    "--message",
    required=True,
    help="Notification message (supports @slack, @pagerduty, template vars)",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Monitor tag (can be repeated, e.g., --tag team:my-team --tag env:prod)",
)
@click.option("--priority", type=int, help="Monitor priority (1-5)")
@monitor_option_flags
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def create_monitor_cmd(
    site: str,
    name: str,
    monitor_type: str,
    query: str,
    message: str,
    tags: tuple[str, ...],
    priority: int | None,
    timeout: float,
    **monitor_opts: Any,
) -> None:
    """Create a Datadog monitor.

    Monitor `options` come from the first-class flags below plus the generic
    `--option KEY=VALUE` escape hatch. Precedence: first-class flags win over
    `--option`, and anything left unset is left to Datadog's defaults.

    \b
    Example (metric monitor on a log-based metric):
        dd-cli create-monitor \\
            --name 'My Service: Kafka topic errors' \\
            --type 'query alert' \\
            --query 'sum(last_5m):sum:kafka.errors{env:prod}.as_count() > 100' \\
            --message '{{#is_alert}}Kafka errors > {{threshold}}{{/is_alert}} @slack' \\
            --critical 100 --warning 50 \\
            --tag team:my-team --tag service:my-service

    \b
    Example (dead-man switch -- needs BOTH halves to actually fire):
        dd-cli create-monitor \\
            --name 'scanner: NO SIGNAL' \\
            --type 'trace-analytics alert' \\
            --query 'trace-analytics("service:scanner").rollup("count").last("45m") < 1' \\
            --message 'scanner silent @slack-alerts' \\
            --notify-no-data --no-data-timeframe 60 --new-group-delay 300
    """  # noqa: E501
    options = _build_monitor_options(monitor_opts).options
    _validate_no_data_family_exclusivity(options)
    _validate_no_data_options(options)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.create_monitor(
                name=name,
                monitor_type=monitor_type,
                query=query,
                message=message,
                tags=list(tags) or None,
                options=options,
                priority=priority,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


@cli.command("get-monitor")
@click.argument("monitor_id_or_url", metavar="MONITOR")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--group-states",
    default=None,
    help="Comma-separated group states to include (all, alert, warn, no data)",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_monitor_cmd(
    monitor_id_or_url: str,
    site: str,
    group_states: str | None,
    timeout: float,
) -> None:
    """Get a monitor's details by ID or URL.

    Accepts a numeric ID or a full Datadog monitor URL:

        dd-cli get-monitor 12345678

        dd-cli get-monitor 'https://us3.datadoghq.com/monitors/12345678?...'
    """
    monitor_id = _parse_monitor_ref(monitor_id_or_url)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_monitor(monitor_id, group_states=group_states)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


def _parse_monitor_ref(ref: str) -> str:
    """Parse a monitor URL or numeric ID into a monitor ID string.

    Supports:
        - Plain ID: '12345678'
        - Full URL: 'https://us3.datadoghq.com/monitors/12345678?group=...'
    """
    import urllib.parse

    if ref.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(ref)
        # Path is like /monitors/12345678
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "monitors":
            return path_parts[1]
        raise click.UsageError(f"Cannot parse monitor ID from URL: {ref}")

    return ref


def _parse_monitor_id(ref: str) -> str:
    """Parse a monitor ref and hold it to being an actual monitor ID.

    ``_parse_monitor_ref`` hands back non-URL input unchanged, and the client
    interpolates the result into the request path. For a read that means a 404;
    for a DELETE it means a ref carrying ``/`` or ``?`` can rewrite the request
    -- ``delete-monitor '123?force=true'`` must not be expressible. A URL that
    points at a page rather than a monitor (``/monitors/manage``) parses to
    ``"manage"`` and is refused here for the same reason.
    """
    monitor_id = _parse_monitor_ref(ref)
    if not re.fullmatch(r"[0-9]+", monitor_id):
        raise click.UsageError(
            f"{monitor_id!r} is not a monitor ID. Expected the numeric ID or a "
            "Datadog monitor URL like "
            "'https://us3.datadoghq.com/monitors/25391362'."
        )
    return monitor_id


_MONITOR_SUMMARY_FIELDS = ("id", "name", "type", "overall_state", "tags")


def _monitor_summary(monitor: dict[str, Any]) -> dict[str, Any]:
    """Project a monitor down to the summary fields used by the default
    --format summary output."""
    return {field: monitor.get(field) for field in _MONITOR_SUMMARY_FIELDS}


def _reject_empty_tag_values(flag: str, values: tuple[str, ...]) -> None:
    """Refuse an empty tag value instead of sending an empty filter.

    ``--tag ""`` would serialise to ``monitor_tags=``, which Datadog ignores --
    returning the whole org while the invocation claims to be filtered.
    """
    if any(not v.strip() for v in values):
        raise click.UsageError(
            f"{flag} was given an empty value. An empty tag is not a filter: "
            "Datadog would ignore it and return everything."
        )


def _warn_empty_monitor_filter(
    monitor_tags: tuple[str, ...],
    scope_tags: tuple[str, ...],
    name: str | None,
) -> None:
    """Explain an empty filtered result instead of letting it read as clean.

    The note names *every* active filter, because the filters are ANDed and
    attributing the emptiness to one of them would be a fresh instance of the
    bug this command was fixed for: a confident claim ("nothing carries
    team:x") that the observation does not support.
    """
    parts: list[str] = []
    if monitor_tags:
        parts.append(f"--tag {', '.join(monitor_tags)} (the monitor's OWN tags)")
    if scope_tags:
        parts.append(
            f"--scope-tag {', '.join(scope_tags)} (tags inside the monitor's query)"
        )
    if name is not None:
        parts.append(f"--name {name} (name substring)")
    if not parts:
        return

    conjunction = "; ".join(parts)
    hint = ""
    if len(parts) > 1:
        hint = (
            " These are ANDed, so this says nothing about any one of them "
            "on its own -- re-run them separately to find out."
        )
    elif monitor_tags:
        hint = (
            " --tag matches the monitor's own tags; to match the scope a "
            "monitor watches (tags inside its query), use --scope-tag."
        )
    elif scope_tags:
        hint = (
            " --scope-tag matches the monitor's query scope; to match the "
            "monitor's own tags, use --tag."
        )
    warn(f"0 monitors match ALL of: {conjunction}.{hint}")


@cli.command("list-monitors")
@click.option(
    "--tag",
    "monitor_tags",
    multiple=True,
    help=(
        "Filter by the monitor's OWN tag (repeatable, AND-combined). "
        "E.g., --tag managed-by:dd-cli --tag team:platform. "
        "This is where ownership tags live. Sent as Datadog's monitor_tags. "
        "CHANGED 2026-08-09: this used to match the monitor's query scope, so "
        "every ownership-tag filter came back empty. That behaviour is now "
        "--scope-tag; re-run any audit whose emptiness you relied on."
    ),
)
@click.option(
    "--scope-tag",
    "scope_tags",
    multiple=True,
    help=(
        "Filter by the SCOPE the monitor watches, i.e. a tag appearing in its "
        "query (repeatable, AND-combined). E.g., --scope-tag env:prod matches "
        "monitors querying {env:prod} whether or not they carry that tag. "
        "Sent as Datadog's tags."
    ),
)
@click.option(
    "--name",
    default=None,
    help="Filter by monitor name (substring, case-insensitive, server-side).",
)
@click.option(
    "--max-results",
    type=click.IntRange(min=1),
    default=10000,
    show_default=True,
    help=(
        "Stop fetching after this many results. Hitting the cap marks the "
        "result truncated (exit 3), so keep it above the number you expect."
    ),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
    help=(
        "Output format. summary: {id, name, type, overall_state, tags} per "
        "monitor. json: full monitor objects wrapped in {count, data}. "
        "jsonl: one full monitor per line, no wrapper."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
@truncation_option
def list_monitors_cmd(
    monitor_tags: tuple[str, ...],
    scope_tags: tuple[str, ...],
    name: str | None,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """List monitors, optionally filtered by tag and/or name.

    Auto-paginates through all results up to --max-results.

    \b
    Two different tag questions, two different flags:
      --tag        the monitor's OWN tags -- where ownership lives
                   (team:, managed-by:, feature:, product:, domain:).
      --scope-tag  the scope the monitor WATCHES, i.e. tags inside its query.
    A monitor querying {env:prod} that carries no tags at all matches
    --scope-tag env:prod and not --tag env:prod. Both may be combined (AND).

    \b
    Examples:
      # All monitors managed by dd-cli
      dd-cli list-monitors --tag managed-by:dd-cli

      # Monitors for a team, by name substring
      dd-cli list-monitors --tag team:platform --name kafka

      # A team's monitors that watch production
      dd-cli list-monitors --tag team:platform --scope-tag env:prod

      # Bulk dump for jq processing
      dd-cli list-monitors --tag managed-by:dd-cli --format jsonl | \\
        jq 'select(.overall_state == "Alert") | .id'
    """
    page_size = 1000
    _reject_empty_tag_values("--tag", monitor_tags)
    _reject_empty_tag_values("--scope-tag", scope_tags)
    monitor_tag_list = list(monitor_tags) if monitor_tags else None
    scope_tag_list = list(scope_tags) if scope_tags else None
    # Built before the request so a FAILED run also carries the question it
    # was asking. A failure envelope has data: null, so it cannot be misread
    # as a clean empty set -- but the answer should still name the predicate.
    filters = {
        "monitor_tags": list(monitor_tags),
        "scope_tags": list(scope_tags),
        "name": name,
    }
    monitors: list[dict[str, Any]] = []
    truncated = False
    reason: str | None = None
    pages = 0

    try:
        with _get_client(site, timeout=timeout) as dd:
            page = 0
            while True:
                batch = dd.list_monitors(
                    monitor_tags=monitor_tag_list,
                    scope_tags=scope_tag_list,
                    name=name,
                    page=page,
                    page_size=page_size,
                )
                monitors.extend(batch)
                pages += 1

                if len(monitors) >= max_results:
                    # Page/offset paging cannot distinguish "exactly full" from
                    # "more exists" without spending another request, so this
                    # stays conservative and says so in the reason.
                    truncated = True
                    reason = REASON_MAX_RESULTS_UNKNOWN
                    monitors = monitors[:max_results]
                    break

                # A short page means we've reached the end.
                if len(batch) < page_size:
                    break

                page += 1
    except DatadogAPIError as e:
        _handle_api_error(e, extra={"filters": filters})
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"filters": filters})

    result = PagedResult(
        items=monitors,
        truncated=truncated,
        truncation_reason=reason,
        pages_fetched=pages,
    )
    # Echo the predicate that actually ran. An empty list is the dangerous
    # output here -- it reads as "nothing there" no matter which question was
    # asked -- so the answer carries the question with it.
    if not monitors:
        _warn_empty_monitor_filter(monitor_tags, scope_tags, name)
    payload = _output_monitors(result, output_format, filters=filters)
    finish(result, payload, on_truncation=on_truncation, describe="list-monitors")


def _output_monitors(
    result: PagedResult,
    output_format: str,
    *,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Output monitors in the specified format."""
    monitors = result.items
    extra = {"filters": filters} if filters is not None else None
    if output_format == "summary":
        return success_envelope(
            [_monitor_summary(m) for m in monitors], result=result, extra=extra
        )
    if output_format == "json":
        return success_envelope(monitors, result=result, extra=extra)

    for m in monitors:
        click.echo(json.dumps(m))
    warn(f"count={len(monitors)} truncated={str(result.truncated).lower()}")
    return None


@cli.command("create-dashboard")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a JSON file containing the dashboard request body "
        "(title, layout_type, widgets, template_variables, ...). "
        "Convenience flags below override matching keys in the file."
    ),
)
@click.option("--title", default=None, help="Dashboard title (overrides --spec).")
@click.option(
    "--description", default=None, help="Dashboard description (overrides --spec)."
)
@click.option(
    "--layout-type",
    type=click.Choice(["ordered", "free"]),
    default=None,
    help="Layout type. Defaults to 'ordered' when not set here or in --spec.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Dashboard tag (repeatable, e.g., --tag team:fbm --tag managed-by:dd-cli).",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def create_dashboard_cmd(
    site: str,
    spec_path: Path | None,
    title: str | None,
    description: str | None,
    layout_type: str | None,
    tags: tuple[str, ...],
    timeout: float,
) -> None:
    """Create a Datadog dashboard.

    The bulk of the dashboard (widgets, layout, template variables) is
    supplied via --spec, a JSON file holding the dashboard request body.
    Convenience flags let you set or override the title, description,
    layout type, and tags without editing the file.

    \b
    Example:
        dd-cli create-dashboard \\
            --spec dashboard.json \\
            --title 'My service overview' \\
            --tag team:my-team
    """
    body: dict[str, Any] = {}
    if spec_path is not None:
        try:
            with spec_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise click.UsageError(f"Failed to read --spec JSON: {e}") from None
        if not isinstance(loaded, dict):
            raise click.UsageError("--spec JSON must be an object (the request body).")
        body = loaded

    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if layout_type is not None:
        body["layout_type"] = layout_type
    if tags:
        body["tags"] = list(tags)

    body.setdefault("layout_type", "ordered")
    body.setdefault("widgets", [])

    if not body.get("title"):
        raise click.UsageError(
            "A dashboard title is required. Pass --title or include it in --spec."
        )

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.create_dashboard(body=body)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    dashboard_id = data.get("id")
    click.echo(
        json.dumps(
            {
                "id": dashboard_id,
                "url": _dashboard_url(site, data),
                "title": data.get("title"),
            },
            indent=2,
        )
    )


def _dashboard_url(site: str, data: dict[str, Any]) -> str | None:
    """Build a full dashboard URL from an API response.

    Datadog returns a relative ``url`` (e.g. /dashboard/abc-def-ghi/title).
    Fall back to constructing one from the id if ``url`` is absent.
    """
    from .http import _normalize_site

    host = _normalize_site(site)
    url = data.get("url")
    if isinstance(url, str) and url:
        return f"https://{host}{url}"
    dashboard_id = data.get("id")
    if dashboard_id:
        return f"https://{host}/dashboard/{dashboard_id}"
    return None


def _parse_dashboard_ref(ref: str) -> str:
    """Parse a dashboard URL or ID into a dashboard ID string.

    Supports:
        - Plain ID: 'abc-def-ghi'
        - Full URL: 'https://us3.datadoghq.com/dashboard/abc-def-ghi/title-slug'
    """
    import urllib.parse

    if ref.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(ref)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "dashboard":
            return path_parts[1]
        raise click.UsageError(f"Cannot parse dashboard ID from URL: {ref}")

    return ref


@cli.command("get-dashboard")
@click.argument("dashboard_id_or_url", metavar="DASHBOARD")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_dashboard_cmd(
    dashboard_id_or_url: str,
    site: str,
    timeout: float,
) -> None:
    """Get a dashboard's full definition by ID or URL.

    Accepts a dashboard ID or a full Datadog dashboard URL:

    \b
        dd-cli get-dashboard abc-def-ghi

        dd-cli get-dashboard 'https://us3.datadoghq.com/dashboard/abc-def-ghi/title'
    """
    dashboard_id = _parse_dashboard_ref(dashboard_id_or_url)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_dashboard(dashboard_id)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


@cli.command("update-dashboard")
@click.argument("dashboard_id_or_url", metavar="DASHBOARD")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a JSON file containing the full dashboard request body "
        "(title, layout_type, widgets, template_variables, ...). "
        "The PUT is a FULL REPLACE, so the body must be complete. "
        "Convenience flags below override matching keys in the file."
    ),
)
@click.option("--title", default=None, help="Dashboard title (overrides --spec).")
@click.option(
    "--description", default=None, help="Dashboard description (overrides --spec)."
)
@click.option(
    "--layout-type",
    type=click.Choice(["ordered", "free"]),
    default=None,
    help="Layout type. Defaults to 'ordered' when not set here or in --spec.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Dashboard tag (repeatable, e.g., --tag team:fbm --tag managed-by:dd-cli).",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def update_dashboard_cmd(
    dashboard_id_or_url: str,
    site: str,
    spec_path: Path | None,
    title: str | None,
    description: str | None,
    layout_type: str | None,
    tags: tuple[str, ...],
    timeout: float,
) -> None:
    """Update (replace) a Datadog dashboard by ID or URL.

    The Datadog dashboard PUT is a FULL REPLACE: the body must contain the
    complete dashboard definition (title, layout_type, widgets, template
    variables, ...). Supply it via --spec, a JSON file holding the request
    body -- typically the output of `dd-cli get-dashboard` with your edits.
    Convenience flags let you set or override the title, description, layout
    type, and tags without editing the file. The body is otherwise passed
    through as-is, so existing widgets are never silently wiped.

    Accepts a dashboard ID or a full Datadog dashboard URL.

    \b
    Example:
        dd-cli get-dashboard abc-def-ghi > dashboard.json
        # ...edit dashboard.json...
        dd-cli update-dashboard abc-def-ghi \\
            --spec dashboard.json \\
            --title 'My service overview'
    """
    dashboard_id = _parse_dashboard_ref(dashboard_id_or_url)

    body: dict[str, Any] = {}
    if spec_path is not None:
        try:
            with spec_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise click.UsageError(f"Failed to read --spec JSON: {e}") from None
        if not isinstance(loaded, dict):
            raise click.UsageError("--spec JSON must be an object (the request body).")
        body = loaded

    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if layout_type is not None:
        body["layout_type"] = layout_type
    if tags:
        body["tags"] = list(tags)

    # layout_type is required by the DD API; fill only if absent (never
    # overrides a value from --spec). Do NOT setdefault widgets: the PUT is a
    # full replace, so an empty default would wipe the dashboard's widgets.
    body.setdefault("layout_type", "ordered")

    if not body.get("title"):
        raise click.UsageError(
            "A dashboard title is required. Pass --title or include it in --spec."
        )

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.update_dashboard(dashboard_id, body=body)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(
        json.dumps(
            {
                "id": data.get("id"),
                "url": _dashboard_url(site, data),
                "title": data.get("title"),
            },
            indent=2,
        )
    )


_DASHBOARD_SUMMARY_FIELDS = ("id", "title", "url", "layout_type", "author_handle")


def _dashboard_summary(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Project a dashboard list entry down to the summary fields."""
    return {field: dashboard.get(field) for field in _DASHBOARD_SUMMARY_FIELDS}


@cli.command("list-dashboards")
@click.option(
    "--name",
    default=None,
    help="Filter by dashboard title (substring, case-insensitive, client-side).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
    help=(
        "Output format. summary: {id, title, url, layout_type, author_handle} "
        "per dashboard. json: full list objects wrapped in {count, data}. "
        "jsonl: one full dashboard per line, no wrapper."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def list_dashboards_cmd(
    name: str | None,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """List dashboards, optionally filtered by title substring.

    \b
    Examples:
      dd-cli list-dashboards
      dd-cli list-dashboards --name 'FBM canary'
      dd-cli list-dashboards --format jsonl | jq '.id'
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.list_dashboards()
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    dashboards = data.get("dashboards", [])
    if name:
        needle = name.lower()
        dashboards = [
            d for d in dashboards if needle in str(d.get("title", "")).lower()
        ]

    result = PagedResult(items=dashboards)
    payload = _output_dashboards(result, output_format)
    finish(result, payload, on_truncation="warn", describe="list-dashboards")


def _output_dashboards(
    result: PagedResult, output_format: str
) -> dict[str, Any] | None:
    """Output dashboards in the specified format."""
    dashboards = result.items
    if output_format == "summary":
        return success_envelope(
            [_dashboard_summary(d) for d in dashboards], result=result
        )
    if output_format == "json":
        return success_envelope(dashboards, result=result)

    for d in dashboards:
        click.echo(json.dumps(d))
    warn(f"count={len(dashboards)} truncated={str(result.truncated).lower()}")
    return None


@cli.command("list-catalog-entities")
@click.option("--kind", default=None, help="Filter by entity kind, e.g. service.")
@click.option("--owner", default=None, help="Filter by owner/team handle.")
@click.option("--name", default=None, help="Filter by entity name.")
@click.option(
    "--ref",
    "entity_ref",
    default=None,
    help="Filter by entity ref, e.g. service:example-service.",
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(["schema", "raw_schema", "oncall", "incident", "relation"]),
    help="Include relationship data; repeatable.",
)
@click.option("--include-discovered", is_flag=True, help="Include discovered entities.")
@click.option("--max-results", type=int, default=1000, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "summary", "jsonl"]),
    default="json",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
@truncation_option
def list_catalog_entities_cmd(
    kind: str | None,
    owner: str | None,
    name: str | None,
    entity_ref: str | None,
    includes: tuple[str, ...],
    include_discovered: bool,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """List Software Catalog entities."""
    page_size = 100
    entities: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    include_list = list(includes) if includes else None
    truncated = False

    try:
        with _get_client(site, timeout=timeout) as dd:
            offset = 0
            while True:
                limit = min(page_size, max_results - len(entities))
                if limit <= 0:
                    break

                page = dd.list_catalog_entities(
                    kind=kind,
                    owner=owner,
                    name=name,
                    ref=entity_ref,
                    include=include_list,
                    include_discovered=include_discovered,
                    offset=offset,
                    limit=limit,
                )
                batch = page.get("data", [])
                entities.extend(batch)
                included.extend(page.get("included", []))

                if len(entities) >= max_results:
                    # Offset paging: hitting the cap cannot be distinguished
                    # from landing exactly on the end without another request.
                    truncated = True
                    entities = entities[:max_results]
                    break
                if len(batch) < limit:
                    break
                offset += len(batch)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    result = PagedResult(
        items=entities,
        truncated=truncated,
        truncation_reason=REASON_MAX_RESULTS_UNKNOWN if truncated else None,
    )
    payload = _output_catalog_entities(result, included, output_format)
    finish(
        result, payload, on_truncation=on_truncation, describe="list-catalog-entities"
    )


@cli.command("get-catalog-entity")
@click.argument("ref", metavar="REF")
@click.option(
    "--kind", default=None, help="Entity kind to use when REF is a bare name."
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(["schema", "raw_schema", "oncall", "incident", "relation"]),
    help="Include relationship data; repeatable.",
)
@click.option("--include-discovered", is_flag=True, help="Include discovered entities.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "summary"]),
    default="json",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def get_catalog_entity_cmd(
    ref: str,
    kind: str | None,
    includes: tuple[str, ...],
    include_discovered: bool,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Get a single Software Catalog entity by ref or name."""
    entity_ref = ref if ":" in ref else None
    name = None if entity_ref else ref
    include_list = list(includes) if includes else None

    try:
        with _get_client(site, timeout=timeout) as dd:
            page = dd.list_catalog_entities(
                kind=kind,
                owner=None,
                name=name,
                ref=entity_ref,
                include=include_list,
                include_discovered=include_discovered,
                offset=0,
                limit=2,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    entities = page.get("data", [])
    if not entities:
        raise click.ClickException(f"No catalog entity found for {ref}")
    if len(entities) > 1:
        raise click.ClickException(
            f"Multiple catalog entities matched {ref}; add --kind"
        )

    entity = entities[0]
    if output_format == "summary":
        click.echo(json.dumps({"data": _catalog_entity_summary(entity)}, indent=2))
    else:
        output: dict[str, Any] = {"data": entity}
        included = page.get("included", [])
        if included:
            output["included"] = included
        click.echo(json.dumps(output, indent=2))


@cli.command("get-catalog-oncall")
@click.argument("ref", metavar="REF")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def get_catalog_oncall_cmd(
    ref: str,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Get Datadog's on-call relationship for one Software Catalog entity."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            page = dd.list_catalog_entities(
                kind=None,
                owner=None,
                name=None,
                ref=ref,
                include=["oncall"],
                include_discovered=False,
                offset=0,
                limit=2,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    entities = page.get("data", [])
    if not entities:
        raise click.ClickException(f"No catalog entity found for {ref}")
    if len(entities) > 1:
        raise click.ClickException(
            f"Multiple catalog entities matched {ref}; use an entity ref"
        )

    entity = entities[0]
    included = page.get("included", [])
    if output_format == "json":
        click.echo(json.dumps({"data": entity, "included": included}, indent=2))
        return

    oncall = ((entity.get("relationships") or {}).get("oncall") or {}).get("data", [])
    click.echo(
        json.dumps(
            {
                "entity": _catalog_entity_summary(entity),
                "oncall": oncall,
                "included": included,
            },
            indent=2,
        )
    )


def _output_catalog_entities(
    result: PagedResult,
    included: list[dict[str, Any]],
    output_format: str,
) -> dict[str, Any] | None:
    entities = result.items
    if output_format == "jsonl":
        for entity in entities:
            click.echo(json.dumps(entity))
        warn(f"count={len(entities)} truncated={str(result.truncated).lower()}")
        return None

    if output_format == "summary":
        return success_envelope(
            [_catalog_entity_summary(entity) for entity in entities], result=result
        )

    return success_envelope(
        entities, result=result, extra={"included": included} if included else None
    )


def _catalog_entity_summary(entity: dict[str, Any]) -> dict[str, Any]:
    attrs = entity.get("attributes") or {}
    meta = entity.get("meta") or {}
    return {
        "id": entity.get("id"),
        "ref": attrs.get("ref") or entity.get("id"),
        "kind": attrs.get("kind"),
        "name": attrs.get("name"),
        "owner": attrs.get("owner"),
        "tags": attrs.get("tags", []),
        "ingestion_source": meta.get("ingestionSource"),
    }


def discover_catalog_files(
    paths: list[str] | tuple[str, ...] | None = None,
) -> list[Path]:
    if not paths:
        paths = ["."]

    discovered = set()
    for p in paths:
        path_obj = Path(p)
        if path_obj.is_file():
            discovered.add(path_obj)
        elif path_obj.is_dir():
            for ext in ["*.datadog.yaml", "*.datadog.yml"]:
                for found in path_obj.rglob(ext):
                    if found.is_file():
                        discovered.add(found)
    return sorted(discovered, key=lambda x: str(x))


def validate_catalog_file(path: Path) -> list[dict[str, Any]]:
    import yaml  # type: ignore[import-untyped]

    errors = []
    try:
        with path.open("r", encoding="utf-8") as f:
            documents = list(yaml.safe_load_all(f))
    except Exception as e:
        errors.append(
            {
                "path": str(path),
                "document": 1,
                "field": "yaml",
                "message": f"YAML parsing error: {e}",
            }
        )
        return errors

    for idx, doc in enumerate(documents, start=1):
        if not doc:
            continue
        if not isinstance(doc, dict):
            continue

        schema_version = str(doc.get("schema-version") or "")
        api_version = str(doc.get("apiVersion") or "")

        is_v3 = schema_version.startswith("v3") or api_version.startswith("v3")

        if is_v3:
            integrations = doc.get("integrations")
            if isinstance(integrations, dict):
                if "pagerduty" in integrations:
                    pagerduty = integrations["pagerduty"]
                    if not isinstance(pagerduty, dict):
                        errors.append(
                            {
                                "path": str(path),
                                "document": idx,
                                "field": "integrations.pagerduty",
                                "message": "integrations.pagerduty must be an object",
                            }
                        )
                    else:
                        if "serviceURL" not in pagerduty:
                            errors.append(
                                {
                                    "path": str(path),
                                    "document": idx,
                                    "field": "integrations.pagerduty.serviceURL",
                                    "message": (
                                        "integrations.pagerduty.serviceURL "
                                        "is required for v3"
                                    ),
                                }
                            )

                        for invalid_field in [
                            "service-name",
                            "serviceName",
                            "service-url",
                        ]:
                            if invalid_field in pagerduty:
                                errors.append(
                                    {
                                        "path": str(path),
                                        "document": idx,
                                        "field": (
                                            f"integrations.pagerduty.{invalid_field}"
                                        ),
                                        "message": (
                                            f"Field '{invalid_field}' is invalid "
                                            "under integrations.pagerduty in v3"
                                        ),
                                    }
                                )
    return errors


@cli.command("validate-catalog")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "summary"]),
    default="json",
    show_default=True,
)
def validate_catalog_cmd(paths: tuple[str, ...], output_format: str) -> None:
    """Validate local Datadog Software Catalog YAML files."""
    import sys

    discovered_files = discover_catalog_files(paths)
    all_errors = []
    for path in discovered_files:
        all_errors.extend(validate_catalog_file(path))

    ok = len(all_errors) == 0
    count = len(discovered_files)

    if output_format == "json":
        output = {
            "ok": ok,
            "count": count,
            "errors": all_errors,
        }
        click.echo(json.dumps(output, indent=2))
    else:  # summary
        if ok:
            click.echo(f"OK: Validated {count} file(s).")
        else:
            click.echo(f"FAIL: Found {len(all_errors)} error(s) in {count} file(s).")
            sorted_errors = sorted(
                all_errors,
                key=lambda e: (e["path"], e["document"], e["field"]),
            )
            for err in sorted_errors:
                click.echo(
                    f"  - {err['path']}[doc {err['document']}] "
                    f"{err['field']}: {err['message']}"
                )

    if not ok:
        sys.exit(1)


def _catalog_pagerduty_link(
    entry: dict[str, Any], path: Path, doc_idx: int
) -> dict[str, Any] | None:
    """Extract PagerDuty link from a valid local v3 entity dictionary.

    Returns a dictionary of PagerDuty link details, or None if the entry
    is not a valid v3 entity with integrations.pagerduty.serviceURL.
    """
    if not isinstance(entry, dict):
        return None

    schema_version = str(entry.get("schema-version") or "")
    api_version = str(entry.get("apiVersion") or "")
    is_v3 = schema_version.startswith("v3") or api_version.startswith("v3")

    if not is_v3:
        return None

    integrations = entry.get("integrations")
    if not isinstance(integrations, dict):
        return None

    pagerduty = integrations.get("pagerduty")
    if not isinstance(pagerduty, dict):
        return None

    service_url = pagerduty.get("serviceURL")
    if not isinstance(service_url, str):
        return None

    kind = entry.get("kind")
    if not isinstance(kind, str):
        return None

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None

    name = metadata.get("name")
    if not isinstance(name, str):
        return None

    ref = f"{kind}:{name}"

    owner = None
    dd_team = entry.get("dd-team")
    if isinstance(dd_team, dict):
        owner = dd_team.get("team-handle")
    if not owner:
        owner = metadata.get("owner")
    if not owner:
        owner = entry.get("owner")

    raw_tags = metadata.get("tags") or entry.get("tags") or []
    tags = []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str):
                tags.append(t)

    return {
        "kind": kind,
        "name": name,
        "ref": ref,
        "owner": owner,
        "tags": tags,
        "path": str(path),
        "document": doc_idx,
        "serviceURL": service_url,
        "service_url": service_url,
    }


@cli.command("list-catalog-pagerduty-links")
@click.argument("paths", nargs=-1, type=click.Path(exists=True))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="json",
    show_default=True,
)
def list_catalog_pagerduty_links_cmd(
    paths: tuple[str, ...],
    output_format: str,
) -> None:
    """List PagerDuty links declared in local Datadog entity YAML."""
    import sys

    import yaml  # type: ignore[import-untyped]

    discovered_files = discover_catalog_files(paths)
    all_errors = []
    all_links = []
    file_documents = {}

    for path in discovered_files:
        try:
            with path.open("r", encoding="utf-8") as f:
                documents = list(yaml.safe_load_all(f))
                file_documents[path] = documents
        except Exception as e:
            all_errors.append(
                {
                    "path": str(path),
                    "document": 1,
                    "field": "yaml",
                    "message": f"YAML parsing error: {e}",
                }
            )

    if all_errors:
        output = {
            "ok": False,
            "errors": all_errors,
        }
        click.echo(json.dumps(output, indent=2))
        sys.exit(1)

    for path, documents in file_documents.items():
        for idx, doc in enumerate(documents, start=1):
            if not doc or not isinstance(doc, dict):
                continue

            link = _catalog_pagerduty_link(doc, path, idx)
            if link:
                all_links.append(link)

    if output_format == "json":
        output_data = {
            "count": len(all_links),
            "data": all_links,
        }
        click.echo(json.dumps(output_data, indent=2))
    elif output_format == "jsonl":
        for link in all_links:
            click.echo(json.dumps(link))
    elif output_format == "summary":
        summary_links = []
        for link in all_links:
            summary_links.append(
                {
                    "ref": link["ref"],
                    "owner": link["owner"],
                    "serviceURL": link["serviceURL"],
                }
            )
        output_data = {
            "count": len(summary_links),
            "data": summary_links,
        }
        click.echo(json.dumps(output_data, indent=2))


@cli.command("list-teams")
@click.option(
    "--query",
    default=None,
    help="Search by team name, team handle, or member email.",
)
@click.option("--me", is_flag=True, help="Only return teams for the current user.")
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(["team_links", "user_team_permissions"]),
    help="Include related resources; repeatable.",
)
@click.option(
    "--field",
    "fields",
    multiple=True,
    help="Team attribute field to fetch; repeatable.",
)
@click.option(
    "--sort",
    type=click.Choice(["name", "-name", "user_count", "-user_count"]),
    default=None,
    help="Sort returned teams.",
)
@click.option("--max-results", type=int, default=1000, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
@truncation_option
def list_teams_cmd(
    query: str | None,
    me: bool,
    includes: tuple[str, ...],
    fields: tuple[str, ...],
    sort: str | None,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """List Datadog Teams, optionally filtered by search keyword.

    \b
    Examples:
      dd-cli list-teams
      dd-cli list-teams --query platform
      dd-cli list-teams --query user@example.com
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            teams = _fetch_teams(
                dd,
                keyword=query,
                me=me,
                include=list(includes) or None,
                fields=list(fields) or None,
                sort=sort,
                max_results=max_results,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    payload = _output_teams(teams, output_format)
    finish(teams, payload, on_truncation=on_truncation, describe="teams")


@cli.command("find-user-teams")
@click.argument("member", metavar="MEMBER")
@click.option("--max-results", type=int, default=1000, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
@truncation_option
def find_user_teams_cmd(
    member: str,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """Find Datadog Teams matching a user/member email or name.

    Uses the Teams API keyword filter. Datadog documents member email matching;
    display-name matching depends on Datadog's search behavior.

    Example: dd-cli find-user-teams user@example.com
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            teams = _fetch_teams(
                dd,
                keyword=member,
                me=False,
                include=None,
                fields=None,
                sort=None,
                max_results=max_results,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    payload = _output_teams(teams, output_format)
    finish(teams, payload, on_truncation=on_truncation, describe="teams")


@cli.command("list-team-notification-rules")
@click.argument("handle", metavar="HANDLE")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def list_team_notification_rules_cmd(
    handle: str,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """List Datadog team notification routing rules."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            team = _resolve_team_by_handle(dd, handle)
            data = dd.list_team_notification_rules(team["id"])
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    rules = data.get("data", [])
    if output_format == "json":
        click.echo(json.dumps(data, indent=2))
    elif output_format == "jsonl":
        for rule in rules:
            click.echo(json.dumps(rule))
    else:
        summaries = [_team_notification_rule_summary(rule) for rule in rules]
        click.echo(json.dumps({"count": len(summaries), "data": summaries}, indent=2))


def _resolve_team_by_handle(dd: DatadogClient, handle: str) -> dict[str, Any]:
    teams = _fetch_teams(
        dd,
        keyword=handle,
        me=False,
        include=None,
        fields=["handle", "name", "user_count"],
        sort=None,
        max_results=1000,
    )
    matches = [
        team
        for team in teams.items
        if (team.get("attributes") or {}).get("handle") == handle
    ]
    if not matches:
        if teams.truncated:
            # "Not found" within a truncated search is not an answer.
            raise click.ClickException(
                f"No Datadog team with handle {handle} was found, but the team "
                f"list was TRUNCATED at {len(teams.items)} results "
                f"({teams.truncation_reason}), so this is not a reliable "
                "'does not exist'. Narrow the search or raise --max-results."
            )
        raise click.ClickException(f"No Datadog team found with handle {handle}")
    if len(matches) > 1:
        raise click.ClickException(f"Multiple Datadog teams matched handle {handle}")
    return matches[0]


def _team_notification_rule_summary(rule: dict[str, Any]) -> dict[str, Any]:
    attrs = rule.get("attributes") or {}
    pagerduty = attrs.get("pagerduty") or {}
    return {
        "id": rule.get("id"),
        "pagerduty_service_name": (
            pagerduty.get("service_name") if isinstance(pagerduty, dict) else None
        ),
        "email": attrs.get("email"),
        "slack": attrs.get("slack"),
        "ms_teams": attrs.get("ms_teams"),
    }


def _fetch_teams(
    dd: DatadogClient,
    *,
    keyword: str | None,
    me: bool,
    include: list[str] | None,
    fields: list[str] | None,
    sort: str | None,
    max_results: int,
) -> PagedResult:
    # The page size MUST stay constant. Page-number pagination with a varying
    # page size addresses a moving window: at max_results=150 the old code
    # fetched page 0 @ size 100 (items 0-99), then shrank the limit to 50 and
    # fetched page 1 @ size 50 -- which is items 50-99 AGAIN. The caller got 50
    # duplicates and never saw items 100-149.
    # Chosen ONCE and then held constant for the whole sequence.
    page_size = min(100, max_results)
    teams: list[dict[str, Any]] = []
    page_number = 0
    truncated = False
    pages = 0

    while len(teams) < max_results:
        page = dd.list_teams(
            keyword=keyword,
            me=me,
            include=include,
            fields=fields,
            page_number=page_number,
            page_size=page_size,
            sort=sort,
        )
        pages += 1
        batch = page.get("data", [])
        if not isinstance(batch, list):
            raise RuntimeError(
                "teams list expected 'data' to be a JSON array, got "
                f"{type(batch).__name__}"
            )
        teams.extend(batch)

        if len(batch) < page_size:
            break
        page_number += 1
    else:
        truncated = True

    if len(teams) > max_results:
        truncated = True
        teams = teams[:max_results]

    return PagedResult(
        items=teams,
        truncated=truncated,
        truncation_reason=REASON_MAX_RESULTS_UNKNOWN if truncated else None,
        pages_fetched=pages,
    )


def _output_teams(result: PagedResult, output_format: str) -> dict[str, Any] | None:
    teams = result.items
    if output_format == "jsonl":
        for team in teams:
            click.echo(json.dumps(team))
        warn(f"count={len(teams)} truncated={str(result.truncated).lower()}")
        return None

    if output_format == "json":
        return success_envelope(teams, result=result)

    return success_envelope([_team_summary(team) for team in teams], result=result)


@cli.command("list-team-members")
@click.argument("handle", metavar="HANDLE")
@click.option(
    "--query",
    default=None,
    help="Filter members by name or email keyword.",
)
@click.option(
    "--sort",
    type=click.Choice(["name", "-name", "email", "-email", "role", "-role"]),
    default=None,
    help="Sort returned members.",
)
@click.option("--max-results", type=int, default=1000, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
@truncation_option
def list_team_members_cmd(
    handle: str,
    query: str | None,
    sort: str | None,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """List the members of a Datadog Team, by team HANDLE.

    Takes a handle (as written in *.datadog.yaml), not a UUID, and resolves
    the team id internally.

    \b
    Datadog returns membership records carrying only a user UUID; the email
    arrives separately under 'included'. This command performs that join, so a
    member whose user record is missing is reported with user_resolved=false
    and a warning rather than a bare null. Without a --query, a row count that
    disagrees with the team's own user_count is warned about on stderr.

    \b
    Examples:
      dd-cli list-team-members supplychain
      dd-cli list-team-members supplychain --format json
      dd-cli list-team-members supplychain --query ana@example.com
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            team = _resolve_team_by_handle(dd, handle)
            members = _fetch_team_members(
                dd,
                team["id"],
                keyword=query,
                sort=sort,
                max_results=max_results,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    team_attrs = team.get("attributes") or {}
    extra = {
        "team": {
            "id": team.get("id"),
            "handle": team_attrs.get("handle"),
            "name": team_attrs.get("name"),
            "user_count": team_attrs.get("user_count"),
        },
        "filters": {"handle": handle, "query": query},
    }
    if not members.items:
        # An empty set that cannot say what it searched for is
        # indistinguishable from a clean result.
        warn(
            f"no members matched team handle={handle!r} query={query!r}; "
            "this is an answer about that question only."
        )

    declared = team_attrs.get("user_count")
    # Only meaningful for an unfiltered, complete listing: a --query is
    # *supposed* to return fewer rows than the team has members.
    if (
        query is None
        and not members.truncated
        and isinstance(declared, int)
        and declared != len(members.items)
    ):
        warn(
            f"team {handle!r} declares user_count={declared} but "
            f"{len(members.items)} membership row(s) were returned. Do not "
            "treat this list as the complete team until the difference is "
            "explained."
        )

    payload = _output_team_members(members, output_format, extra=extra)
    finish(members, payload, on_truncation=on_truncation, describe="team members")


def _fetch_team_members(
    dd: DatadogClient,
    team_id: str,
    *,
    keyword: str | None,
    sort: str | None,
    max_results: int,
) -> PagedResult:
    """Page through a team's memberships, joining each to its user record.

    The page size is chosen once and held constant: page-number pagination with
    a shrinking page size re-reads rows already seen and skips the rest (see
    the same note on :func:`_fetch_teams`).
    """
    page_size = min(100, max_results)
    members: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    page_number = 0
    truncated = False
    pages = 0

    while len(members) < max_results:
        page = dd.list_team_memberships(
            team_id,
            keyword=keyword,
            page_number=page_number,
            page_size=page_size,
            sort=sort,
        )
        pages += 1
        batch = page.get("data", [])
        if not isinstance(batch, list):
            raise RuntimeError(
                "team memberships expected 'data' to be a JSON array, got "
                f"{type(batch).__name__}"
            )

        users = {
            user.get("id"): (user.get("attributes") or {})
            for user in (page.get("included") or [])
            if isinstance(user, dict) and user.get("type") == "users"
        }
        for membership in batch:
            member, missing = _team_member_record(membership, users)
            members.append(member)
            if missing is not None:
                warnings.append(missing)

        if len(batch) < page_size:
            break
        page_number += 1
    else:
        truncated = True

    if len(members) > max_results:
        truncated = True
        members = members[:max_results]

    return PagedResult(
        items=members,
        truncated=truncated,
        truncation_reason=REASON_MAX_RESULTS_UNKNOWN if truncated else None,
        pages_fetched=pages,
        warnings=warnings or None,
    )


def _team_member_record(
    membership: dict[str, Any],
    users: dict[str | None, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Join one membership to its user, reporting an unjoinable one."""
    attrs = membership.get("attributes") or {}
    rel = ((membership.get("relationships") or {}).get("user") or {}).get("data") or {}
    user_id = rel.get("id")
    user = users.get(user_id)
    resolved = user is not None
    user = user or {}

    record = {
        "membership_id": membership.get("id"),
        "user_id": user_id,
        "user_resolved": resolved,
        "email": user.get("email"),
        "name": user.get("name"),
        "handle": user.get("handle"),
        "status": user.get("status"),
        "disabled": user.get("disabled"),
        "service_account": user.get("service_account"),
        "role": attrs.get("role"),
        "provisioned_by": attrs.get("provisioned_by"),
    }
    if resolved:
        return record, None
    return record, {
        "kind": "unresolved_user",
        "user_id": user_id,
        "membership_id": membership.get("id"),
        "message": (
            f"membership {membership.get('id')!r} references user {user_id!r}, "
            "which Datadog did not return in 'included'; this member has no "
            "email and must not be treated as absent."
        ),
    }


def _output_team_members(
    result: PagedResult,
    output_format: str,
    *,
    extra: dict[str, Any],
) -> dict[str, Any] | None:
    members = result.items
    if output_format == "jsonl":
        for member in members:
            click.echo(json.dumps(member))
        warn(f"count={len(members)} truncated={str(result.truncated).lower()}")
        return None

    if output_format == "json":
        return success_envelope(members, result=result, extra=extra)

    return success_envelope(
        [_team_member_summary(member) for member in members],
        result=result,
        extra=extra,
    )


def _team_member_summary(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": member["email"],
        "name": member["name"],
        "user_id": member["user_id"],
        "user_resolved": member["user_resolved"],
        "role": member["role"],
        "disabled": member["disabled"],
    }


def _team_summary(team: dict[str, Any]) -> dict[str, Any]:
    attrs = team.get("attributes") or {}
    return {
        "id": team.get("id"),
        "name": attrs.get("name"),
        "handle": attrs.get("handle"),
        "user_count": attrs.get("user_count"),
        "is_managed": attrs.get("is_managed"),
    }


@cli.command("update-monitor")
@click.argument("monitor_id_or_url", metavar="MONITOR")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option("--name", help="Update monitor name")
@click.option("--query", help="Update monitor query")
@click.option("--message", help="Update notification message")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "Monitor tag (repeatable). REPLACES the monitor's existing tag list; "
        "pass every tag you want to keep."
    ),
)
@click.option("--priority", type=int, help="Update priority (1-5)")
@monitor_option_flags
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def update_monitor_cmd(
    monitor_id_or_url: str,
    site: str,
    name: str | None,
    query: str | None,
    message: str | None,
    tags: tuple[str, ...],
    priority: int | None,
    timeout: float,
    **monitor_opts: Any,
) -> None:
    """Update a Datadog monitor by ID or URL.

    Top-level fields you do not pass are left unchanged.

    Options are merged, not clobbered: a PUT that carries a partial `options`
    object resets every option it omits, so whenever any option flag is given
    dd-cli first GETs the monitor and PUTs its existing options with your
    changes applied on top. Precedence: first-class flags > --option
    KEY=VALUE > the monitor's existing options.

    That read-modify-write is not atomic (the Datadog monitor API has no
    ETag/If-Match), so an option changed by someone else between the GET and
    the PUT is overwritten. Use --option KEY=null to clear an option, and
    --option 'thresholds={...}' to replace the threshold object wholesale
    (--critical/--warning only patch individual thresholds).

    Not every option can be written through this endpoint. `silenced`
    (muting) and `device_ids` (read-only) are refused up front, naming the
    command that does work -- a PUT accepts them, answers 200, and changes
    nothing. As a backstop for any option that behaves the same way, the
    monitor Datadog returns is compared against what was sent, and an option
    that did not take makes this command fail instead of reporting success.

    \b
    Examples:
        dd-cli update-monitor 16440468 \\
            --query 'min(last_15m):sum:my.metric{*} > 0'
    \b
        # Make an existing no-signal monitor actually able to fire.
        dd-cli update-monitor 16440468 \\
            --notify-no-data --no-data-timeframe 60
    """
    monitor_id = _parse_monitor_ref(monitor_id_or_url)

    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if query is not None:
        payload["query"] = query
    if message is not None:
        payload["message"] = message
    if tags:
        payload["tags"] = list(tags)
    if priority is not None:
        payload["priority"] = priority

    updates = _build_monitor_options(monitor_opts)

    if not payload and not updates.options:
        raise click.UsageError(
            "No updates specified. Use --help to see available options."
        )

    _validate_no_data_family_exclusivity(updates.options)

    try:
        with _get_client(site, timeout=timeout) as dd:
            if updates.options:
                existing_options = _existing_monitor_options(dd, monitor_id)
                merged = _merge_monitor_options(existing_options, updates)
                if _NO_DATA_KEYS & updates.options.keys():
                    # The caller is touching no-data config: hold them to it.
                    _validate_no_data_options(merged)
                elif _no_data_config_is_broken(merged):
                    # Pre-existing breakage the caller did not ask about.
                    # Warn loudly, but do not block an unrelated update.
                    click.echo(
                        f"WARNING: monitor {monitor_id} already has "
                        f"{_NO_DATA_TIMEFRAME_ADVICE}",
                        err=True,
                    )
                payload["options"] = merged
            data = dd.update_monitor(monitor_id, payload=payload)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    # Datadog's PUT answers with the updated monitor, so the change can be
    # checked against the artifact instead of against the status code. A 200
    # that did not apply the change is the failure this command shipped with
    # for `silenced`; the named refusals above cover the two known keys, and
    # this covers whatever else turns out to behave that way.
    sent_options = payload.get("options") or {}
    checked = {k: v for k, v in sent_options.items() if k in updates.options}
    ignored = _unapplied_options(checked, data)
    if ignored:
        _handle_runtime_error(
            RuntimeError(
                f"Datadog accepted the update of monitor {monitor_id} but the "
                "monitor it returned does not show "
                f"{', '.join(sorted(ignored))} applied. Treat the change as "
                "NOT made: re-read with `dd-cli get-monitor "
                f"{monitor_id}` before relying on it. (If Datadog merely "
                "normalised the value, the write did land -- the comparison "
                "is exact apart from int/float.)"
            ),
            extra={
                "monitor_id": monitor_id,
                "unapplied_options": sorted(ignored),
                "requested_options": updates.options,
                "monitor": data,
            },
        )

    click.echo(json.dumps(data, indent=2))


_MONITOR_404_AMBIGUITY = (
    "Datadog has no monitor {monitor_id}. Three different situations produce "
    "this same 404 and they are not interchangeable: the monitor was already "
    "deleted, the ID is wrong, or DD_SITE points at the wrong Datadog region "
    "and the monitor is alive in another one."
)

_MONITOR_NOT_FOUND_HINT = _MONITOR_404_AMBIGUITY + " Nothing was deleted."

#: The same ambiguity, for the commands where nothing was muted or unmuted.
#: A mute that 404s leaves a monitor that may be alerting *somewhere else*.
_MONITOR_MUTE_NOT_FOUND_HINT = _MONITOR_404_AMBIGUITY + " Nothing was muted or unmuted."

# Datadog's two reference refusals, verified against its API:
#   monitor [<id>,<name>] is referenced in slos: [<slo id>,<name>]
#   monitor [<id>,<name>] is referenced in composite monitors: [<id>,<name>]
_MONITOR_REFERENCE_REFUSAL = re.compile(
    r"referenced in (slos|composite monitors)", re.IGNORECASE
)

_FORCE_HINT = (
    "Datadog refuses to delete a monitor that something else points at. "
    "--force deletes it anyway, and that is not a cleanup: the SLO or "
    "composite monitor is left with a dangling reference to a monitor that no "
    "longer exists. Fix the referencing resource first unless you know you "
    "want the dangling reference."
)


def _reference_refusal_hint(e: DatadogAPIError) -> str | None:
    """Explain a 400 that refuses the delete because something references it.

    Decoration only. The failure is already a failure -- ``ok: false`` and the
    non-zero exit do not depend on this match -- so if Datadog rewords its
    refusal the cost is a missing hint, never a misclassified result.
    """
    if e.status_code != 400:
        return None
    haystack = f"{e} {e.response_body or ''}"
    return _FORCE_HINT if _MONITOR_REFERENCE_REFUSAL.search(haystack) else None


@cli.command("delete-monitor")
@click.argument("monitor_id_or_url", metavar="MONITOR")
@click.option(
    "--yes",
    "confirmed",
    is_flag=True,
    default=False,
    help="Confirm the deletion. Required; there is no interactive prompt.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Delete even when an SLO or composite monitor references this monitor. "
        "This does not clean the reference up -- it leaves it dangling. Still "
        "requires --yes."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def delete_monitor_cmd(
    monitor_id_or_url: str,
    confirmed: bool,
    force: bool,
    site: str,
    timeout: float,
) -> None:
    """Delete a monitor by ID or URL. Requires --yes.

    There is no undo. Datadog will not hand the monitor back through the API,
    so the definition is read and printed before the delete: the envelope's
    `data.definition` is the only copy of what you destroyed, and it is
    attached to the failure envelope too, for the runs where the DELETE itself
    goes wrong.

    Deleting a monitor also silently removes things that were attached to it --
    its downtimes stop mattering, and anything watching it simply goes quiet,
    which for alerting reads as good news. Datadog does refuse (400) when an
    SLO or a composite monitor references the monitor; --force overrides that
    and leaves the reference dangling.

    \b
    Examples:
        dd-cli delete-monitor 25391362 --yes
    \b
        dd-cli delete-monitor 'https://us3.datadoghq.com/monitors/25391362' --yes
    """
    if not confirmed:
        raise click.UsageError(
            f"refusing to delete monitor {monitor_id_or_url!r} without --yes. "
            "Deletion cannot be undone, and anything that was alerting on this "
            "monitor goes quiet rather than telling you it stopped."
        )

    monitor_id = _parse_monitor_id(monitor_id_or_url)
    extra_ctx: dict[str, Any] = {"monitor_id": monitor_id}

    try:
        with _get_client(site, timeout=timeout) as dd:
            try:
                before = dd.get_monitor(monitor_id)
            except DatadogAPIError as e:
                extra = dict(extra_ctx)
                if e.status_code == 404:
                    extra["hint"] = _MONITOR_NOT_FOUND_HINT.format(
                        monitor_id=monitor_id
                    )
                _handle_api_error(e, extra=extra)

            if not isinstance(before, dict):
                _handle_runtime_error(
                    RuntimeError(
                        f"GET of monitor {monitor_id} returned no monitor "
                        "object; refusing to delete a monitor whose definition "
                        f"could not be captured first. Response: "
                        f"{str(before)[:200]}"
                    ),
                    extra=extra_ctx,
                )

            # Every path from here on carries the captured definition, failures
            # included: a DELETE that landed can still be reported as a failure
            # (a headered 429 is retried once, and the retry sees a monitor
            # that is already gone), and that is precisely the run where the
            # only surviving copy of the monitor is this one.
            captured = {**extra_ctx, "definition": before}
            try:
                result = dd.delete_monitor(monitor_id, force=force)
            except DatadogAPIError as e:
                extra = dict(captured)
                hint = _reference_refusal_hint(e)
                if hint:
                    extra["hint"] = hint
                _handle_api_error(e, extra=extra)
            except RuntimeError as e:
                # A network error or an unparseable body. `_write` does not
                # retry either, precisely because the write may have landed --
                # so this run cannot say whether the monitor still exists, and
                # the captured definition matters more here than anywhere.
                _handle_runtime_error(e, extra=captured)
    except DatadogAPIError as e:
        _handle_api_error(e, extra=extra_ctx)
    except RuntimeError as e:
        _handle_runtime_error(e, extra=extra_ctx)

    # A 2xx means Datadog deleted it, whatever the body looked like. An
    # unexpected shape must not become a traceback here: an unhandled exception
    # prints nothing on stdout, and this is a run where the monitor is already
    # gone and the definition below is the only record of it.
    deleted_id = result.get("deleted_monitor_id") if isinstance(result, dict) else None

    emit(
        success_envelope(
            {
                "deleted": monitor_id,
                "deleted_monitor_id": deleted_id,
                "definition": before,
            },
            extra=extra_ctx,
        )
    )

    if deleted_id is None:
        warn(
            f"deleted monitor {monitor_id}, but Datadog's response carried no "
            "deleted_monitor_id, so which monitor it deleted could not be "
            "cross-checked. The definition above is the only copy."
        )
    elif str(deleted_id) != monitor_id:
        warn(
            f"asked Datadog to delete monitor {monitor_id} but it reported "
            f"deleting {deleted_id}. Check which monitor is actually gone "
            "before trusting this run."
        )
    else:
        warn(
            f"deleted monitor {monitor_id}. This cannot be undone; the "
            "definition above is the only copy."
        )


# ── mute-monitor / unmute-monitor ───────────────────────────────────
#
# Muting is the one monitor state that cannot be round-tripped through
# PUT /api/v1/monitor/{id}. Writing `options.silenced` through the PUT does
# mute the monitor, but sending `{}` or `null` for the same key returns 200
# and leaves the monitor muted (verified against the live API on 2026-08-31,
# monitor 25447403). An operator who believes a paging monitor is alerting
# again while it is still gagged is the worst shape this failure can take, so
# `silenced` is refused in update-monitor and handled only here, where the
# result is read back and checked.

#: The key Datadog records a whole-monitor (unscoped) mute under.
_ALL_SCOPES_KEY = "*"

_MUTE_EXPIRY_FORMATS = (
    "an epoch in seconds (1788000000), a UTC/offset-qualified timestamp "
    "('2026-09-08T00:00:00Z', '2026-09-08T00:00:00+02:00'), or a duration "
    "from now ('7d', '4h', '90m')"
)


def _relative_mute_expiry(value: str, now: float) -> int | None:
    """Parse a bare forward duration like ``7d`` / ``+4h`` into an epoch."""
    m = re.fullmatch(r"\+?(\d+)([smhdw])", value)
    if not m:
        return None
    return int(now) + int(m.group(1)) * _TIME_MULTIPLIERS[m.group(2)]


def _parse_mute_expiry(value: str, *, now: float | None = None) -> int:
    """Convert a mute expiry to the epoch-seconds Datadog wants.

    Accepts an epoch, an ISO-8601 timestamp carrying an explicit UTC offset,
    or a forward duration (``7d``). A *naive* timestamp is refused rather than
    guessed at: "midnight" is a different instant in every timezone, and the
    guess would be discovered only when coverage came back at the wrong hour.
    """
    now = time.time() if now is None else now
    raw = value.strip()
    if not raw:
        raise click.UsageError(
            f"--until was given an empty value. Pass {_MUTE_EXPIRY_FORMATS}."
        )

    if re.fullmatch(r"[0-9]+", raw):
        end = _require_epoch_seconds("--until", int(raw))
    else:
        relative = _relative_mute_expiry(raw, now)
        if relative is not None:
            end = relative
        else:
            try:
                parsed = datetime.datetime.fromisoformat(raw)
            except ValueError:
                raise click.UsageError(
                    f"Cannot parse --until {value!r}. Use {_MUTE_EXPIRY_FORMATS}."
                ) from None
            if parsed.tzinfo is None:
                raise click.UsageError(
                    f"--until {value!r} has no timezone, and dd-cli will not "
                    "guess one: the same wall-clock time is a different "
                    "instant in every zone, and guessing wrong means the "
                    "monitor comes back hours early or late. Append 'Z' for "
                    "UTC, or an explicit offset like '+02:00'."
                )
            end = int(parsed.timestamp())

    if end <= now:
        raise click.UsageError(
            f"--until {value!r} resolves to {_human_epoch(end)}, which is in "
            "the past. Datadog would either reject that or expire the mute "
            "immediately; neither is what an expiry is for."
        )
    return end


def _is_epoch(value: Any) -> bool:
    """Whether a `silenced` value is an expiry timestamp.

    `bool` is a subclass of `int`, so it has to be excluded explicitly --
    otherwise a `true` in the mute map would render as 1970-01-01 and read as
    a real expiry.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _human_epoch(epoch: int | None) -> str | None:
    """Render an epoch-seconds value as a UTC ISO-8601 string."""
    if epoch is None:
        return None
    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _humanize_seconds(seconds: int) -> str:
    """Render a duration as a rough human string ('2d 3h', '45m')."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [
        f"{days}d" if days else "",
        f"{hours}h" if hours else "",
        f"{minutes}m" if minutes and not days else "",
    ]
    return " ".join(p for p in parts if p) or "0m"


def _read_silenced(monitor: Any, monitor_id: str, *, action: str) -> dict[str, Any]:
    """Extract ``options.silenced`` from a monitor, or fail loudly.

    A monitor whose shape cannot be read is not evidence of an empty mute
    list. Reporting ``{}`` here would be the same bug one level up: a claim
    that the monitor is unmuted, made without an observation that supports it.
    """
    options = monitor.get("options") if isinstance(monitor, dict) else None
    if not isinstance(options, dict):
        _handle_runtime_error(
            RuntimeError(
                f"cannot verify the {action} of monitor {monitor_id}: the "
                "monitor read back has no options object, so its mute state "
                f"is unknown. Response: {str(monitor)[:200]}"
            ),
            extra={"monitor_id": monitor_id, "monitor": monitor},
        )
    silenced = options.get("silenced")
    if silenced is None:
        # Datadog omits the key (or sends null) on a monitor that has never
        # been muted. That is a real observation of "no mute", unlike the
        # unreadable-shape case above.
        return {}
    if not isinstance(silenced, dict):
        _handle_runtime_error(
            RuntimeError(
                f"cannot verify the {action} of monitor {monitor_id}: "
                f"options.silenced is {type(silenced).__name__}, not an "
                f"object. Value: {str(silenced)[:200]}"
            ),
            extra={"monitor_id": monitor_id, "silenced": silenced},
        )
    return dict(silenced)


def _reread_monitor(dd: DatadogClient, monitor_id: str, *, action: str) -> Any:
    """Re-read a monitor after a write, reporting the write as unverified if
    the read itself fails."""
    try:
        return dd.get_monitor(monitor_id)
    except DatadogAPIError as e:
        _handle_api_error(
            e,
            extra={
                "monitor_id": monitor_id,
                "hint": (
                    f"the {action} was accepted (HTTP 2xx) but reading the "
                    "monitor back to confirm it failed, so the resulting mute "
                    "state is unknown. Re-run `dd-cli get-monitor "
                    f"{monitor_id}` before acting on this."
                ),
            },
        )
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"monitor_id": monitor_id})


def _silenced_summary(silenced: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the silenced map as scope/expiry records, expiries humanised."""
    return [
        {
            "scope": scope,
            "end": end,
            "end_utc": _human_epoch(end) if _is_epoch(end) else None,
            "indefinite": end is None,
        }
        for scope, end in sorted(silenced.items(), key=lambda kv: str(kv[0]))
    ]


@cli.command("mute-monitor")
@click.argument("monitor_id_or_url", metavar="MONITOR")
@click.option(
    "--until",
    "--end",
    "until",
    default=None,
    metavar="WHEN",
    help=(
        "When the mute expires: epoch seconds, an offset-qualified timestamp "
        "('2026-09-08T00:00:00Z'), or a duration from now ('7d', '4h'). "
        "Required unless --forever is passed."
    ),
)
@click.option(
    "--forever",
    "--no-expiry",
    "forever",
    is_flag=True,
    default=False,
    help=(
        "Mute with no expiry. The monitor stays silent until somebody "
        "remembers to unmute it, which is how alert coverage disappears "
        "permanently -- hence the explicit flag."
    ),
)
@click.option(
    "--scope",
    default=None,
    help=(
        "Mute a single group scope (e.g. 'host:web-01', 'env:staging') "
        "instead of the whole monitor."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def mute_monitor_cmd(
    monitor_id_or_url: str,
    until: str | None,
    forever: bool,
    scope: str | None,
    site: str,
    timeout: float,
) -> None:
    """Mute a monitor by ID or URL until an expiry you name.

    An expiry is mandatory unless you pass --forever. An un-expiring mute on a
    paging monitor removes coverage with no scheduled moment at which anyone
    finds out, and nothing in Datadog will remind you.

    The result is verified rather than assumed: after the POST the monitor is
    read back and its `options.silenced` is checked against what you asked
    for. A 200 that did not actually mute the monitor -- or that muted it
    indefinitely when you asked for an expiry -- fails this command.

    \b
    Examples:
        dd-cli mute-monitor 25447403 --until 4h
    \b
        dd-cli mute-monitor 25447403 --until '2026-09-08T00:00:00Z'
    \b
        # One noisy group only; the rest of the monitor keeps alerting.
        dd-cli mute-monitor 25447403 --until 2h --scope host:web-01
    \b
        dd-cli mute-monitor 25447403 --forever   # no expiry: read the warning
    """
    if until and forever:
        raise click.UsageError(
            "--until and --forever contradict each other. Pass an expiry, or "
            "--forever to mute with none."
        )
    if not until and not forever:
        raise click.UsageError(
            "refusing to mute monitor "
            f"{monitor_id_or_url!r} without an expiry. A mute with no end is "
            "not a pause, it is a silent removal of alert coverage that "
            "nothing will remind you about. Pass --until (e.g. --until 4h, "
            "--until '2026-09-08T00:00:00Z') or --forever if you really mean "
            "indefinitely."
        )
    if scope is not None and not scope.strip():
        raise click.UsageError(
            "--scope was given an empty value. Omit --scope to mute the whole "
            "monitor; an empty scope is not a narrower mute."
        )

    monitor_id = _parse_monitor_id(monitor_id_or_url)
    end = _parse_mute_expiry(until) if until else None
    scope_key = scope if scope else _ALL_SCOPES_KEY
    extra_ctx: dict[str, Any] = {
        "monitor_id": monitor_id,
        "scope": scope_key,
        "requested_end": end,
        "requested_end_utc": _human_epoch(end),
    }

    try:
        with _get_client(site, timeout=timeout) as dd:
            try:
                dd.mute_monitor(monitor_id, end=end, scope=scope)
            except DatadogAPIError as e:
                extra = dict(extra_ctx)
                if e.status_code == 404:
                    extra["hint"] = _MONITOR_MUTE_NOT_FOUND_HINT.format(
                        monitor_id=monitor_id
                    )
                _handle_api_error(e, extra=extra)
            except RuntimeError as e:
                _handle_runtime_error(e, extra=extra_ctx)

            after = _reread_monitor(dd, monitor_id, action="mute")
    except DatadogAPIError as e:
        _handle_api_error(e, extra=extra_ctx)
    except RuntimeError as e:
        _handle_runtime_error(e, extra=extra_ctx)

    silenced = _read_silenced(after, monitor_id, action="mute")
    missing = object()
    applied: Any = silenced.get(scope_key, missing)
    applied_epoch = applied if _is_epoch(applied) else None
    payload_data = {
        "monitor_id": monitor_id,
        "name": after.get("name") if isinstance(after, dict) else None,
        "scope": scope_key,
        "requested_end": end,
        "requested_end_utc": _human_epoch(end),
        "applied_end": applied_epoch,
        "applied_end_utc": _human_epoch(applied_epoch),
        "indefinite": applied is None,
        "silenced": _silenced_summary(silenced),
    }

    if applied is missing:
        _handle_runtime_error(
            RuntimeError(
                f"Datadog accepted the mute of monitor {monitor_id} but the "
                f"monitor read back is not muted on scope {scope_key!r} "
                f"(options.silenced = {json.dumps(silenced)}). The monitor is "
                "still alerting; do not treat it as muted."
            ),
            extra={**extra_ctx, "result": payload_data},
        )

    # Compared in both directions. An expiry that is not the one asked for is
    # the obvious half; `--forever` that came back with an expiry is the other,
    # and it is just as wrong -- the monitor comes back on its own at a moment
    # nobody chose, which is the opposite of what --forever was asked to mean.
    if applied != end:
        recorded = (
            "no expiry at all -- this monitor is muted INDEFINITELY"
            if applied is None
            else f"{applied} ({_human_epoch(applied_epoch)})"
            if applied_epoch is not None
            else f"{applied!r}, which is not an expiry at all"
        )
        wanted = (
            "no expiry (--forever)" if end is None else f"{end} ({_human_epoch(end)})"
        )
        _handle_runtime_error(
            RuntimeError(
                f"monitor {monitor_id} was muted on scope {scope_key!r}, but "
                f"not as asked: requested {wanted}, Datadog recorded "
                f"{recorded}. Unmute it with `dd-cli unmute-monitor "
                f"{monitor_id}` and re-mute with the intended expiry."
            ),
            extra={**extra_ctx, "result": payload_data},
        )

    emit(success_envelope(payload_data, extra={"monitor_id": monitor_id}))

    where = "" if scope_key == _ALL_SCOPES_KEY else f" on scope {scope_key}"
    if end is None:
        warn(
            f"monitor {monitor_id} is muted{where} with NO EXPIRY. Nothing "
            "will page from it and nothing will remind you. Restore it with "
            f"`dd-cli unmute-monitor {monitor_id}`."
        )
    else:
        remaining = _humanize_seconds(max(0, end - int(time.time())))
        warn(
            f"monitor {monitor_id} is muted{where} until "
            f"{_human_epoch(end)} ({remaining} from now), verified by "
            "re-reading the monitor."
        )


@cli.command("unmute-monitor")
@click.argument("monitor_id_or_url", metavar="MONITOR")
@click.option(
    "--scope",
    default=None,
    help=(
        "Unmute only this group scope (e.g. 'host:web-01'). Omitted, every "
        "scope on the monitor is unmuted."
    ),
)
@click.option(
    "--all-scopes",
    "all_scopes",
    is_flag=True,
    default=False,
    help=(
        "Explicitly unmute every scope. This is already the default when "
        "--scope is omitted; the flag exists to say so in a script."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def unmute_monitor_cmd(
    monitor_id_or_url: str,
    scope: str | None,
    all_scopes: bool,
    site: str,
    timeout: float,
) -> None:
    """Unmute a monitor by ID or URL, and verify it is really unmuted.

    This is the only way to clear a mute. `update-monitor --option
    silenced=null` (or `silenced={}`) returns 200 and changes nothing, which
    is why dd-cli refuses that payload and sends you here.

    After the POST the monitor is read back and `options.silenced` is checked:
    a scope that is still muted fails this command rather than reporting a
    success that would leave a prod monitor silently gagged.

    \b
    Examples:
        dd-cli unmute-monitor 25447403
    \b
        dd-cli unmute-monitor 'https://us3.datadoghq.com/monitors/25447403'
    \b
        # Bring one group back while the rest stays muted.
        dd-cli unmute-monitor 25447403 --scope host:web-01
    """
    if scope is not None and all_scopes:
        raise click.UsageError(
            "--scope and --all-scopes contradict each other. Pass --scope to "
            "unmute one group, or neither to unmute all of them."
        )
    if scope is not None and not scope.strip():
        raise click.UsageError(
            "--scope was given an empty value. Omit --scope to unmute every "
            "scope; an empty scope is not a narrower unmute."
        )

    monitor_id = _parse_monitor_id(monitor_id_or_url)
    scope_key = scope if scope else None
    extra_ctx: dict[str, Any] = {
        "monitor_id": monitor_id,
        "scope": scope_key or _ALL_SCOPES_KEY,
    }

    try:
        with _get_client(site, timeout=timeout) as dd:
            try:
                before_silenced = _read_silenced(
                    dd.get_monitor(monitor_id), monitor_id, action="unmute"
                )
            except DatadogAPIError as e:
                extra = dict(extra_ctx)
                if e.status_code == 404:
                    extra["hint"] = _MONITOR_MUTE_NOT_FOUND_HINT.format(
                        monitor_id=monitor_id
                    )
                _handle_api_error(e, extra=extra)

            try:
                dd.unmute_monitor(
                    monitor_id, scope=scope_key, all_scopes=scope_key is None
                )
            except DatadogAPIError as e:
                _handle_api_error(
                    e,
                    extra={
                        **extra_ctx,
                        "silenced_before": _silenced_summary(before_silenced),
                    },
                )
            except RuntimeError as e:
                _handle_runtime_error(
                    e,
                    extra={
                        **extra_ctx,
                        "silenced_before": _silenced_summary(before_silenced),
                    },
                )

            after = _reread_monitor(dd, monitor_id, action="unmute")
    except DatadogAPIError as e:
        _handle_api_error(e, extra=extra_ctx)
    except RuntimeError as e:
        _handle_runtime_error(e, extra=extra_ctx)

    silenced = _read_silenced(after, monitor_id, action="unmute")
    # A wildcard mute still gags the scope that was just unmuted, so an
    # unmute of one scope while `*` is muted has not brought anything back.
    # Reporting it as a success would overstate the coverage that exists.
    still_muted: list[Any] = (
        [s for s in (scope_key, _ALL_SCOPES_KEY) if s in silenced]
        if scope_key
        else list(silenced)
    )
    payload_data = {
        "monitor_id": monitor_id,
        "name": after.get("name") if isinstance(after, dict) else None,
        "scope": scope_key or _ALL_SCOPES_KEY,
        "was_muted": bool(before_silenced),
        "silenced_before": _silenced_summary(before_silenced),
        "silenced": _silenced_summary(silenced),
        "still_muted_scopes": sorted(str(s) for s in still_muted),
    }

    if still_muted:
        _handle_runtime_error(
            RuntimeError(
                f"Datadog accepted the unmute of monitor {monitor_id} but the "
                "monitor read back is STILL MUTED on "
                f"{', '.join(sorted(str(s) for s in still_muted))} "
                f"(options.silenced = {json.dumps(silenced)}). Do not treat "
                "this monitor as alerting."
                + (
                    f" The whole-monitor mute ({_ALL_SCOPES_KEY}) covers "
                    f"{scope_key} too; clear it with `dd-cli unmute-monitor "
                    f"{monitor_id}` without --scope."
                    if scope_key and _ALL_SCOPES_KEY in still_muted
                    else ""
                )
            ),
            extra={**extra_ctx, "result": payload_data},
        )

    emit(success_envelope(payload_data, extra={"monitor_id": monitor_id}))

    if not before_silenced:
        warn(
            f"monitor {monitor_id} was not muted to begin with; it is "
            "unmuted now, which was already true before this ran."
        )
    elif scope_key:
        warn(
            f"monitor {monitor_id} is no longer muted on scope {scope_key}, "
            "verified by re-reading the monitor. Other scopes still muted: "
            f"{', '.join(sorted(silenced)) or 'none'}."
        )
    else:
        warn(
            f"monitor {monitor_id} is no longer muted on any scope, verified "
            "by re-reading the monitor. It is alerting again."
        )


@cli.command("list-slos")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--tags",
    default=None,
    help=(
        "Comma-separated tags to filter by, AND-combined "
        "(e.g., 'env:prod,team:backend'). These are the SLO's own tags. "
        "Sent as Datadog's tags_query."
    ),
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=1000,
    show_default=True,
    help=(
        "Max number of SLOs to return. Always sent explicitly (Datadog's own "
        "default is also 1000) so that a full page can be reported as "
        "truncated instead of passing for a complete answer."
    ),
)
@click.option("--offset", type=int, default=None, help="Pagination offset")
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def list_slos_cmd(
    site: str,
    tags: str | None,
    limit: int,
    offset: int | None,
    timeout: float,
) -> None:
    """List SLOs, optionally filtered by the SLOs' own tags.

    --tags matches the tags carried by the SLO itself, AND-combined. It is sent
    as Datadog's `tags_query`; the tool previously sent `tags`, which this
    endpoint does not define and Datadog therefore ignored, so every "filtered"
    listing was really the whole org.

    Example: dd-cli list-slos --tags 'env:prod,team:backend'
    """
    filters = {"tags_query": tags, "limit": limit, "offset": offset}
    if tags is not None and not tags.strip():
        raise click.UsageError(
            "--tags was given an empty value. An empty tag is not a filter: "
            "Datadog would ignore it and return everything."
        )
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.list_slos(tags_query=tags, limit=limit, offset=offset)
    except DatadogAPIError as e:
        _handle_api_error(e, extra={"filters": filters})
    except RuntimeError as e:
        _handle_runtime_error(e, extra={"filters": filters})

    # Extract and format a summary table
    slos = data.get("data", [])
    if not isinstance(slos, list):
        _handle_runtime_error(
            RuntimeError(
                "list-slos expected 'data' to be a JSON array, got "
                f"{type(slos).__name__}"
            )
        )

    # This endpoint is not auto-paginated: a --limit that comes back exactly
    # full may be page 1 of N, and offset paging cannot tell.
    hit_limit = len(slos) >= limit
    slo_result = PagedResult(
        items=slos,
        truncated=hit_limit,
        truncation_reason=REASON_MAX_RESULTS_UNKNOWN if hit_limit else None,
    )

    summary = []
    for slo in slos:
        thresholds = slo.get("thresholds", [])
        threshold_strs = [
            f"{t.get('timeframe', '?')}: {t.get('target', '?')}%" for t in thresholds
        ]
        summary.append(
            {
                "id": slo.get("id"),
                "name": slo.get("name"),
                "type": slo.get("type"),
                "tags": slo.get("tags", []),
                "thresholds": threshold_strs,
            }
        )

    if not slos and tags:
        warn(
            f"0 SLOs carry ALL of: {tags}. --tags matches the SLO's own tags "
            "(sent as Datadog's tags_query)."
        )

    finish(
        slo_result,
        success_envelope(summary, result=slo_result, extra={"filters": filters}),
        on_truncation="warn",
        describe="list-slos",
    )


@cli.command("get-slo")
@click.argument("slo_id", metavar="SLO_ID")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--from",
    "time_from",
    default="now-7d",
    show_default=True,
    help="History start time (e.g., now-7d, now-30d, or epoch seconds)",
)
@click.option(
    "--to",
    "time_to",
    default=None,
    help="History end time (default: now)",
)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_slo_cmd(
    slo_id: str,
    site: str,
    time_from: str,
    time_to: str | None,
    timeout: float,
) -> None:
    """Get a single SLO's details and recent history.

    Fetches the SLO definition and its history (status, error budget,
    SLI value) over the specified time range.

    \b
    Example:
        dd-cli get-slo abc123def456
        dd-cli get-slo abc123def456 --from now-30d
    """
    import time as time_mod

    from_ts = _parse_time_to_epoch_s(time_from)
    to_ts = _parse_time_to_epoch_s(time_to) if time_to else int(time_mod.time())

    try:
        with _get_client(site, timeout=timeout) as dd:
            slo_data = dd.get_slo(slo_id)

            try:
                history_data = dd.get_slo_history(slo_id, from_ts=from_ts, to_ts=to_ts)
                slo_data["history"] = history_data
                slo_data["partial"] = False
            except DatadogAPIError as history_error:
                # Record WHY, and mark the document partial: a history block
                # holding only an error string still reads as a valid SLO.
                slo_data["history"] = {
                    "error": {
                        "status": history_error.status_code,
                        "message": str(history_error),
                        "attempts": history_error.attempts,
                    }
                }
                slo_data["partial"] = True
                warn(f"SLO history unavailable ({history_error}); result is partial")

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(slo_data, indent=2))


#: Anchored on purpose. The previous unanchored pattern let re.match() accept a
#: prefix and silently discard the rest, so "now-1h30m" meant 1h and "now-7days"
#: meant 7d -- a wrong time window, hence a confidently wrong count, with no
#: API failure anywhere to hint that anything went wrong.
_RELATIVE_TIME_RE = re.compile(r"now-(\d+)([smhdw])\Z")

_TIME_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_relative_seconds(value: str, *, now: float | None = None) -> float:
    """Resolve 'now', 'now-90s', 'now-2w', or epoch seconds to epoch seconds.

    ``now`` may be pinned so that several relative values in one command are
    resolved against the same instant.
    """
    value = value.strip()
    if value.isdigit():
        return float(value)

    now = time.time() if now is None else now
    if value == "now":
        return now

    m = _RELATIVE_TIME_RE.fullmatch(value)
    if not m:
        raise click.UsageError(
            f"Invalid time format: {value!r}. Use 'now', 'now-1h', 'now-7d' "
            "(units s/m/h/d/w), or epoch seconds. Compound durations like "
            "'now-1h30m' are not supported -- they used to be silently "
            "truncated to the first unit."
        )
    return now - int(m.group(1)) * _TIME_MULTIPLIERS[m.group(2)]


def _parse_time_to_epoch_s(value: str) -> int:
    """Convert a relative time string (now-1h, now-7d) or epoch seconds to int."""
    return int(_parse_relative_seconds(value))


# 1e11 epoch seconds is the year 5138, so anything at or above it is a
# millisecond timestamp that was passed to a seconds-valued flag. The API
# answers such a window with an unhelpful "Internal error", so catch it here.
_EPOCH_SECONDS_CEILING = 100_000_000_000


def _require_epoch_seconds(flag: str, value: int) -> int:
    """Reject an epoch-millisecond value passed to a seconds-valued flag."""
    if value >= _EPOCH_SECONDS_CEILING:
        raise click.UsageError(
            f"{flag}={value} looks like epoch milliseconds. This flag takes "
            "epoch seconds (or a relative time such as now-1h); divide by 1000."
        )
    return value


def _summarize_metric_series(series: dict[str, Any]) -> dict[str, Any]:
    """Reduce one timeseries to its scope and a few aggregates.

    Aggregates cover only non-null, finite points. A series with no such
    points (an empty pointlist, or one that is entirely null) yields null
    aggregates rather than raising.
    """
    raw_points = series.get("pointlist")
    pointlist = raw_points if isinstance(raw_points, list) else []
    values: list[float] = []
    last_ts: int | None = None

    def _number(candidate: Any) -> float | None:
        # bool is a subclass of int, so it has to be excluded explicitly.
        if not isinstance(candidate, (int, float)) or isinstance(candidate, bool):
            return None
        return float(candidate) if math.isfinite(candidate) else None

    for point in pointlist:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        value = _number(point[1])
        if value is None:
            continue
        values.append(value)
        timestamp = _number(point[0])
        last_ts = int(timestamp) if timestamp is not None else None

    return {
        "scope": series.get("scope"),
        "metric": series.get("metric"),
        "query_index": series.get("query_index"),
        # Seconds per point. Datadog rolls long windows up automatically, so
        # this is what tells you how coarse the aggregates below really are.
        "interval": series.get("interval"),
        # Passed through as-is: this can be [unit, null], [null, null], or absent.
        "unit": series.get("unit"),
        "points": len(pointlist),
        "non_null_points": len(values),
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
        # Timestamp (epoch ms) of the last non-null point. Distinguishes
        # "the value is 0 right now" from "nothing has reported in a while".
        "last_ts": last_ts,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": sum(values) / len(values) if values else None,
    }


def _metric_query_error(data: dict[str, Any]) -> str:
    """Extract the error text from a failed query response body."""
    for key in ("error", "message"):
        text = data.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()

    # Some Datadog error bodies use an 'errors' list instead.
    errors = data.get("errors")
    if isinstance(errors, list):
        joined = "; ".join(str(e) for e in errors if str(e).strip())
        if joined:
            return joined

    return (
        f"Datadog reported status={data.get('status')!r} with no error text. "
        f"Response: {json.dumps(data)[:300]}"
    )


def _metric_query_summary(
    data: dict[str, Any],
    *,
    query: str,
    from_ts: int,
    to_ts: int,
) -> dict[str, Any]:
    """Build the human-scannable summary of a timeseries query response."""
    series = data.get("series") or []
    summaries = [_summarize_metric_series(s) for s in series]

    extra: dict[str, Any] = {
        "query": query,
        "from": from_ts,
        "to": to_ts,
        "res_type": data.get("res_type"),
    }

    message = data.get("message")
    if isinstance(message, str) and message.strip():
        extra["message"] = message

    if not summaries:
        # Zero series is ambiguous: a metric that does not exist and a metric
        # whose tag scope or window has no data look identical here.
        extra["note"] = (
            "No series returned. Either the metric name or the tag filter "
            "matches nothing (names are separator-sensitive, so try "
            "'dd-cli search-metrics'), or the time window covers no data."
        )
    elif all(s["non_null_points"] == 0 for s in summaries):
        extra["note"] = (
            "Series were returned but every point is null. This is common "
            "with formula, division, and timeshift queries."
        )

    return success_envelope(summaries, extra=extra)


@cli.command("query-metrics")
@click.argument("query", metavar="QUERY")
@click.option(
    "--from",
    "time_from",
    default="now-1h",
    show_default=True,
    help="Start time (e.g., now-20m, now-1h, or epoch seconds)",
)
@click.option(
    "--to",
    "time_to",
    default=None,
    help="End time (default: now)",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
    help=(
        "Output format. summary: per-series scope plus first/last/min/max/avg "
        "over the window. json: the raw API response. jsonl: one raw series "
        "per line, no wrapper."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Request timeout in seconds",
)
def query_metrics_cmd(
    query: str,
    time_from: str,
    time_to: str | None,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Query a metric timeseries and summarize each series over the window.

    The default summary gives one line-ish object per series: its tag scope
    plus first/last/min/max/avg over the window, the number of points, and how
    many of them were non-null. Use --format json for the raw API response.

    Datadog rolls long windows up automatically and averages by default, so
    'max' over a multi-day window is a max of interval averages and understates
    short spikes. The 'interval' field shows the granularity in use; add
    '.rollup(max, 60)' to the query when you need true peaks.

    \b
    Examples:
      # Lag per consumer group over the last 20 minutes
      dd-cli query-metrics 'avg:kafka.consumer_lag{*} by {consumer_group}' \\
        --from now-20m

      # Raw points for scripting
      dd-cli query-metrics 'avg:system.cpu.user{*}' --format json | \\
        jq '.series[0].pointlist'
    """
    from_ts = _require_epoch_seconds("--from", _parse_time_to_epoch_s(time_from))
    to_ts = _require_epoch_seconds("--to", _parse_time_to_epoch_s(time_to or "now"))

    if from_ts >= to_ts:
        raise click.UsageError(
            f"Empty time window: --from ({from_ts}) is not before --to ({to_ts})."
        )

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.query_timeseries(query=query, from_ts=from_ts, to_ts=to_ts)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    # A query error arrives as HTTP 200 with status=error in the body, so this
    # has to be checked explicitly -- and before the format branch, so that no
    # output format can turn a failure into a silent, zero-exit no-op. It goes
    # through the same failure envelope as a transport error, because to a
    # caller reading .count there is no difference between the two.
    status = data.get("status")
    has_error_payload = bool(data.get("error")) or bool(data.get("errors"))
    if (status is not None and status != "ok") or has_error_payload:
        _handle_runtime_error(RuntimeError(_metric_query_error(data)))

    if output_format == "json":
        emit(success_envelope(data))
    elif output_format == "jsonl":
        for series in data.get("series") or []:
            click.echo(json.dumps(series))
    else:
        emit(_metric_query_summary(data, query=query, from_ts=from_ts, to_ts=to_ts))


@cli.command("search-metrics")
@click.argument("term", metavar="TERM")
@click.option(
    "--limit",
    type=int,
    default=100,
    show_default=True,
    help="Max metric names to print (the API itself returns every match).",
)
@truncation_option
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def search_metrics_cmd(
    term: str,
    limit: int,
    on_truncation: str,
    site: str,
    timeout: float,
) -> None:
    """Find metric names containing TERM.

    Useful before writing a query or a monitor, because a metric name guessed
    with the wrong separator matches nothing and a monitor built on it never
    fires.

    TERM is matched as a literal substring, so '.' is not a wildcard and a
    dotted guess will not find an underscored name. Search a short distinctive
    token ('lag') rather than a full guessed name ('kafka.consumer.lag').

    The index only covers recently-reporting metrics, so absence here is not
    proof that a metric does not exist.

    \b
    Example:
        dd-cli search-metrics lag --limit 50
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.search_metrics(term=term)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    # A search with no matches returns metrics: null rather than [].
    names = (data.get("results") or {}).get("metrics") or []

    # The endpoint is uncapped, so --limit is the only thing standing between a
    # broad term and tens of thousands of lines. That makes the printed list a
    # partial answer, and the total is known exactly, so say so rather than
    # letting a caller read count as "this is how many matched".
    result = PagedResult(items=list(names), pages_fetched=1).limited(
        limit, REASON_MORE_AVAILABLE
    )

    payload = success_envelope(
        result.items,
        result=result,
        extra={"term": term, "total": len(names)},
    )
    finish(result, payload, on_truncation=on_truncation, describe="search-metrics")


@cli.command("get-workflow")
@click.argument("workflow_url_or_id", metavar="WORKFLOW")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--instance/--no-instance",
    default=False,
    help="Also fetch the instance details (if instance ID is in the URL)",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_workflow_cmd(
    workflow_url_or_id: str,
    site: str,
    instance: bool,
    timeout: float,
) -> None:
    """Get a workflow definition (and optionally an instance) by ID or URL.

    Accepts either a UUID or a full Datadog workflow URL:

        dd-cli get-workflow aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee

        dd-cli get-workflow 'https://us3.datadoghq.com/workflow/aaaaaaaa-...' --instance
    """
    workflow_id, instance_id = _parse_workflow_ref(workflow_url_or_id)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_workflow(workflow_id)

            if instance and instance_id:
                instance_data = dd.get_workflow_instance(workflow_id, instance_id)
                data["instance"] = instance_data

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


def _parse_workflow_ref(ref: str) -> tuple[str, str | None]:
    """Parse a workflow URL or UUID into (workflow_id, instance_id | None).

    Supports:
        - Plain UUID: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        - Full URL:   'https://us3.datadoghq.com/workflow/aaaaaaaa-...?instance=11111111-...'
    """
    import urllib.parse

    if ref.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(ref)
        # Path is like /workflow/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "workflow":
            workflow_id = path_parts[1]
        else:
            raise click.UsageError(f"Cannot parse workflow ID from URL: {ref}")
        qs = urllib.parse.parse_qs(parsed.query)
        instance_id = qs.get("instance", [None])[0]
        return workflow_id, instance_id

    return ref, None


def _parse_time_to_epoch_ms(value: str) -> int:
    """Convert a relative time string (now-1h, now-7d) or epoch ms to int."""
    if value.strip().isdigit():
        # Already epoch milliseconds.
        return int(value.strip())
    return int(_parse_relative_seconds(value) * 1000)


@cli.command("search-et-issues")
@click.argument("query", metavar="QUERY")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--from",
    "time_from",
    default="now-1d",
    show_default=True,
    help="Start time (e.g., now-1h, now-7d, or epoch ms)",
)
@click.option(
    "--to",
    "time_to",
    default=None,
    help="End time (default: now)",
)
@click.option(
    "--track",
    type=click.Choice(["trace", "logs", "rum"]),
    default="trace",
    show_default=True,
    help="Error tracking source",
)
@click.option(
    "--order-by",
    type=click.Choice(["TOTAL_COUNT", "FIRST_SEEN", "IMPACTED_SESSIONS", "PRIORITY"]),
    default=None,
    help="Sort order",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def search_et_issues_cmd(
    query: str,
    site: str,
    time_from: str,
    time_to: str | None,
    track: str,
    order_by: str | None,
    timeout: float,
) -> None:
    """Search error tracking issues.

    Example: dd-cli search-et-issues 'service:my-service-*' --from now-7d

    \b
    Query uses Datadog search syntax:
        service:my-service
        service:my-* AND @error.type:NullPointerException
    """
    import time as time_mod

    from_ms = _parse_time_to_epoch_ms(time_from)
    to_ms = _parse_time_to_epoch_ms(time_to) if time_to else int(time_mod.time() * 1000)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.search_error_tracking_issues(
                query=query,
                time_from=from_ms,
                time_to=to_ms,
                track=track,
                order_by=order_by,
                include="issue,issue.assignee",
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    # This endpoint is called once and exposes no cursor here, so what comes
    # back is page 1 of N. Say so rather than letting it read as a total.
    data["complete"] = False
    data["completeness_note"] = (
        "search-et-issues returns a single unpaginated page; the number of "
        "issues shown is not necessarily the total number of matches."
    )
    click.echo(json.dumps(data, indent=2))
    warn(
        "search-et-issues returned a single page; this is not a complete "
        "count of matching issues."
    )


@cli.command("get-et-issue")
@click.argument("issue_id", metavar="ISSUE_ID")
@click.option(
    "--include",
    "include",
    default="assignee,case,team_owners",
    show_default=True,
    help=(
        "Comma-separated relationship objects to sideload. Valid values: "
        "assignee, case, team_owners. Pass --include '' to omit. "
        "Note: the GET endpoint uses unprefixed names, unlike the search "
        "endpoint which uses 'issue.assignee' etc."
    ),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def get_et_issue_cmd(
    issue_id: str,
    include: str,
    site: str,
    timeout: float,
) -> None:
    """Get a single error tracking issue by ID.

    Example: dd-cli get-et-issue aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_error_tracking_issue(
                issue_id,
                include=include or None,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


@cli.command("update-et-issue-state")
@click.argument("issue_id", metavar="ISSUE_ID")
@click.argument(
    "state",
    type=click.Choice(["OPEN", "RESOLVED", "IGNORED"], case_sensitive=False),
)
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def update_et_issue_state_cmd(
    issue_id: str,
    state: str,
    site: str,
    timeout: float,
) -> None:
    """Update an error tracking issue's state.

    \b
    States:
      OPEN      - Mark issue as open / for review
      RESOLVED  - Mark issue as resolved
      IGNORED   - Suppress from monitors and notifications

    Example: dd-cli update-et-issue-state aaaaaaaa-... RESOLVED
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.update_error_tracking_issue_state(
                issue_id,
                state=state.upper(),
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


@cli.command("check-pagerduty-service")
@click.argument("service_name", metavar="SERVICE_NAME")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--timeout",
    type=float,
    default=15.0,
    show_default=True,
    help="Request timeout in seconds",
)
def check_pagerduty_service_cmd(
    service_name: str,
    site: str,
    timeout: float,
) -> None:
    """Check a PagerDuty integration service config by name."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_pagerduty_integration_service(service_name)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    output_data = data
    if isinstance(data, dict):
        output_data = data.copy()
        output_data.pop("service_key", None)

    click.echo(json.dumps(output_data, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()


# ── count-logs ──────────────────────────────────────────────────────

#: Guard against a typo turning into thousands of API calls (and, at Datadog's
#: rate limits, an hour of retrying).
_MAX_BUCKETS = 500


def _parse_duration_seconds(value: str) -> int:
    """Parse a bare duration like '1h', '15m', '2w' into seconds."""
    m = re.fullmatch(r"(\d+)([smhdw])", value.strip())
    if not m:
        raise click.UsageError(
            f"Invalid bucket duration: {value!r}. Use e.g. '15m', '1h', '1d'."
        )
    return int(m.group(1)) * _TIME_MULTIPLIERS[m.group(2)]


@cli.command("count-logs")
@click.argument("query", metavar="QUERY")
@click.option(
    "--site",
    envvar="DD_SITE",
    default=_default_site,
    show_default=True,
    help="Datadog site, e.g., us3.datadoghq.com",
)
@click.option(
    "--from",
    "time_from",
    default="now-1h",
    show_default=True,
    help="Start time (e.g., now-48h)",
)
@click.option("--to", "time_to", default="now", show_default=True, help="End time")
@click.option(
    "--bucket",
    default=None,
    help="Bucket size (e.g., 1h). Omit for a single total over the whole range.",
)
@click.option(
    "--storage-tier",
    type=click.Choice(["indexes", "online-archives", "flex"]),
    help="Storage tier to search",
)
@click.option(
    "--allow-partial",
    is_flag=True,
    help="Continue past a failing bucket, marking its count null (never 0).",
)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Per-request timeout in seconds (increase for flex tier)",
)
def count_logs_cmd(
    query: str,
    site: str,
    time_from: str,
    time_to: str,
    bucket: str | None,
    storage_tier: str | None,
    allow_partial: bool,
    timeout: float,
) -> None:
    """Count logs, optionally bucketed over time.

    Exists to remove the reason to write a shell loop. Incident-driven: an
    agent looping over hours in bash gave every iteration an independent chance
    to turn a 429 into a 0, and the resulting hourly series pointed at the
    wrong conclusion. Bucketing in-process means a 429 is a retry and an
    exhausted retry is an exception -- a bucket is never 0 unless it really is.

    \b
    Examples:
      dd-cli count-logs 'service:web status:error' --from now-48h --bucket 1h
      dd-cli count-logs 'service:web' --from now-7d
    """
    # Resolve "now" ONCE: parsing --from and --to separately lets them land on
    # different seconds, which would shift a bucket edge.
    now = time.time()
    start = int(_parse_relative_seconds(time_from, now=now))
    end = int(_parse_relative_seconds(time_to, now=now))
    if end <= start:
        raise click.UsageError(f"--to ({time_to}) must be after --from ({time_from}).")

    if bucket:
        width = _parse_duration_seconds(bucket)
        if width > end - start:
            raise click.UsageError(
                f"--bucket {bucket} is larger than the {end - start}s range; "
                "use a smaller bucket or a wider range."
            )
        n_buckets = math.ceil((end - start) / width)
        if n_buckets > _MAX_BUCKETS:
            raise click.UsageError(
                f"too many buckets ({n_buckets} > {_MAX_BUCKETS}) for "
                f"--bucket {bucket} over that range."
            )
        edges = [
            (start + i * width, min(start + (i + 1) * width, end))
            for i in range(n_buckets)
        ]
    else:
        edges = [(start, end)]

    buckets: list[dict[str, Any]] = []
    failures = 0

    try:
        with _get_client(site, timeout=timeout) as dd:
            for lo, hi in edges:
                entry: dict[str, Any] = {
                    "from": _iso(lo),
                    "to": _iso(hi),
                    "from_epoch_s": lo,
                    "to_epoch_s": hi,
                }
                try:
                    entry["count"] = dd.count_logs(
                        query=query,
                        time_from=str(lo * 1000),
                        time_to=str(hi * 1000),
                        storage_tier=storage_tier,
                    )
                    entry["complete"] = True
                except (DatadogAPIError, RuntimeError) as e:
                    if not allow_partial:
                        raise
                    failures += 1
                    # null, never 0: this bucket made no observation.
                    entry["count"] = None
                    entry["complete"] = False
                    entry["error"] = {
                        "status": getattr(e, "status_code", None),
                        "message": str(e),
                    }
                    warn(f"bucket {entry['from']}..{entry['to']} FAILED: {e}")
                buckets.append(entry)
    except (DatadogAPIError, RuntimeError) as e:
        # Null out this command's own result fields too, so a caller reading
        # .total gets null rather than a missing key it might coerce to 0.
        raise _ApiFailure(
            failure_envelope(
                e,
                status=getattr(e, "status_code", None),
                attempts=getattr(e, "attempts", None),
                elapsed_s=getattr(e, "elapsed_s", None),
                extra={"total": None, "buckets": None, "partial_total": None},
            ),
            str(e),
        ) from None

    complete = failures == 0
    counted = sum(b["count"] for b in buckets if b["count"] is not None)

    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "query": query,
        "from": _iso(start),
        "to": _iso(end),
        # A total that silently omits failed buckets would understate reality,
        # so it is null unless every bucket reported.
        "total": counted if complete else None,
        "truncated": not complete,
        "truncation_reason": None if complete else "bucket_failed",
        "buckets": buckets if bucket else None,
    }
    if not complete:
        payload["partial_total"] = counted
        payload["failed_buckets"] = failures

    emit(payload)

    if not complete:
        warn(
            f"{failures} of {len(buckets)} bucket(s) FAILED; 'total' is null and "
            "'partial_total' excludes them. Do not treat partial_total as a total."
        )
        raise SystemExit(EXIT_TRUNCATED)


def _iso(epoch_s: int) -> str:
    return (
        datetime.datetime.fromtimestamp(epoch_s, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
