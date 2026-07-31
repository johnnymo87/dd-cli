from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

import click

from .http import DatadogAPIError, DatadogClient, RetryEvent, env
from .output import (
    REASON_MAX_PAGES,
    REASON_MAX_RESULTS_UNKNOWN,
    REASON_MORE_AVAILABLE,
    REASON_SERVER_TIMEOUT,
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


def _handle_api_error(e: DatadogAPIError) -> NoReturn:
    """Fail loudly, on stdout as well as stderr, never as an empty result."""
    raise _ApiFailure(
        failure_envelope(
            e,
            status=e.status_code,
            attempts=e.attempts,
            elapsed_s=e.elapsed_s,
            body=e.response_body,
        ),
        str(e),
    )


def _handle_runtime_error(e: Exception) -> NoReturn:
    raise _ApiFailure(failure_envelope(e), str(e))


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
    help="Group by attribute path (repeatable, e.g., --group-by service)",
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
    timeout: float,
) -> None:
    """Create a log-based count metric (computed at ingestion time).

    Works with all storage tiers including flex. The metric counts matching
    logs and is available as a custom metric for dashboards and monitors.

    Example: dd-cli create-log-metric kafka.unknown_topic_errors \\
        --query 'service:my-worker UNKNOWN_TOPIC_OR_PARTITION' \\
        --group-by service --group-by env
    """
    group_by_list = [{"path": g, "tag_name": g.lstrip("@")} for g in group_by] or None

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.create_log_metric(
                metric_id=metric_id,
                query=query,
                group_by=group_by_list,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    click.echo(json.dumps(data, indent=2))


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


_MONITOR_SUMMARY_FIELDS = ("id", "name", "type", "overall_state", "tags")


def _monitor_summary(monitor: dict[str, Any]) -> dict[str, Any]:
    """Project a monitor down to the summary fields used by the default
    --format summary output."""
    return {field: monitor.get(field) for field in _MONITOR_SUMMARY_FIELDS}


@cli.command("list-monitors")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "Filter by monitor tag (repeatable, AND-combined). "
        "E.g., --tag managed-by:dd-cli --tag team:platform. "
        "These are the monitor's own tags, not tags on the watched resources."
    ),
)
@click.option(
    "--name",
    default=None,
    help="Filter by monitor name (substring, case-insensitive, server-side).",
)
@click.option(
    "--max-results",
    type=int,
    default=1000,
    show_default=True,
    help="Stop fetching after this many results.",
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
    tags: tuple[str, ...],
    name: str | None,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
    on_truncation: str,
) -> None:
    """List monitors, optionally filtered by tag and/or name.

    Auto-paginates through all results up to --max-results (default 1000).

    \b
    Examples:
      # All monitors managed by dd-cli
      dd-cli list-monitors --tag managed-by:dd-cli

      # Monitors for a team, by name substring
      dd-cli list-monitors --tag team:platform --name kafka

      # Bulk dump for jq processing
      dd-cli list-monitors --tag managed-by:dd-cli --format jsonl | \\
        jq 'select(.overall_state == "Alert") | .id'
    """
    page_size = 1000
    tag_list = list(tags) if tags else None
    monitors: list[dict[str, Any]] = []
    truncated = False
    reason: str | None = None
    pages = 0

    try:
        with _get_client(site, timeout=timeout) as dd:
            page = 0
            while True:
                batch = dd.list_monitors(
                    tags=tag_list,
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
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    result = PagedResult(
        items=monitors,
        truncated=truncated,
        truncation_reason=reason,
        pages_fetched=pages,
    )
    payload = _output_monitors(result, output_format)
    finish(result, payload, on_truncation=on_truncation, describe="list-monitors")


def _output_monitors(result: PagedResult, output_format: str) -> dict[str, Any] | None:
    """Output monitors in the specified format."""
    monitors = result.items
    if output_format == "summary":
        return success_envelope([_monitor_summary(m) for m in monitors], result=result)
    if output_format == "json":
        return success_envelope(monitors, result=result)

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

    _output_dashboards(dashboards, output_format)


def _output_dashboards(dashboards: list[dict[str, Any]], output_format: str) -> None:
    """Output dashboards in the specified format."""
    if output_format == "summary":
        summary = [_dashboard_summary(d) for d in dashboards]
        click.echo(json.dumps({"count": len(summary), "data": summary}, indent=2))
    elif output_format == "json":
        click.echo(json.dumps({"count": len(dashboards), "data": dashboards}, indent=2))
    elif output_format == "jsonl":
        for d in dashboards:
            click.echo(json.dumps(d))


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
        fields=["handle", "name"],
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

    click.echo(json.dumps(data, indent=2))


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
    help="Comma-separated tags to filter by (e.g., 'env:prod,team:backend')",
)
@click.option("--limit", type=int, default=None, help="Max number of SLOs to return")
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
    limit: int | None,
    offset: int | None,
    timeout: float,
) -> None:
    """List SLOs with optional tag filtering.

    Example: dd-cli list-slos --tags 'env:prod,team:backend'
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.list_slos(tags=tags, limit=limit, offset=offset)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        _handle_runtime_error(e)

    # Extract and format a summary table
    slos = data.get("data", [])
    if not slos:
        click.echo(json.dumps({"data": [], "count": 0}, indent=2))
        return

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

    click.echo(json.dumps({"data": summary, "count": len(summary)}, indent=2))


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


def _parse_relative_seconds(value: str) -> float:
    """Resolve 'now', 'now-90s', 'now-2w', or epoch seconds to epoch seconds."""
    value = value.strip()
    if value.isdigit():
        return float(value)

    if value == "now":
        return time.time()

    m = _RELATIVE_TIME_RE.fullmatch(value)
    if not m:
        raise click.UsageError(
            f"Invalid time format: {value!r}. Use 'now', 'now-1h', 'now-7d' "
            "(units s/m/h/d/w), or epoch seconds. Compound durations like "
            "'now-1h30m' are not supported -- they used to be silently "
            "truncated to the first unit."
        )
    return time.time() - int(m.group(1)) * _TIME_MULTIPLIERS[m.group(2)]


def _parse_time_to_epoch_s(value: str) -> int:
    """Convert a relative time string (now-1h, now-7d) or epoch seconds to int."""
    return int(_parse_relative_seconds(value))


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
