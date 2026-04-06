from __future__ import annotations

import json
from typing import Any

import click

from .http import DatadogAPIError, DatadogClient, env


def _default_site() -> str:
    return env("DD_SITE", "us3.datadoghq.com") or "us3.datadoghq.com"


def _get_client(site: str, timeout: float = 15.0) -> DatadogClient:
    """Create a DatadogClient, raising UsageError if credentials are missing."""
    api_key = env("DD_API_KEY")
    app_key = env("DD_APP_KEY")

    if not api_key or not app_key:
        raise click.UsageError(
            "DD_API_KEY and DD_APP_KEY must be set. The v2 APIs require both."
        )

    return DatadogClient(site=site, api_key=api_key, app_key=app_key, timeout=timeout)


def _handle_api_error(e: DatadogAPIError) -> None:
    """Convert DatadogAPIError to ClickException with JSON output."""
    error_output = json.dumps(
        {"error": str(e), "status": e.status_code, "body": e.response_body},
        indent=2,
    )
    raise click.ClickException(error_output)


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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


def _enrich_incident(dd: DatadogClient, data: dict[str, Any]) -> None:
    """Add enrichment data to incident response (modifies data in place)."""
    try:
        incident_type_uuid = (
            data.get("data", {}).get("attributes", {}).get("incident_type_uuid")
        )

        if incident_type_uuid:
            try:
                type_data = dd.get_incident_type(incident_type_uuid)
                data.setdefault("enrichment", {})["incident_type"] = type_data
            except DatadogAPIError:
                pass  # Don't fail if type lookup fails

        try:
            incident_id = data.get("data", {}).get("id", "")
            if incident_id:
                integrations_data = dd.get_incident_integrations(incident_id)
                data.setdefault("enrichment", {})["integrations"] = integrations_data
        except DatadogAPIError:
            pass  # Don't fail if integrations lookup fails

    except Exception as e:
        data.setdefault("enrichment", {})["errors"] = f"Enrichment failed: {e}"


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
        raise click.ClickException(str(e)) from None

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
    """Validate DD_API_KEY against /api/v1/validate."""
    api_key = env("DD_API_KEY")
    if not api_key:
        raise click.UsageError("DD_API_KEY must be set")

    # validate only needs API key, but we still use the client for consistency
    # (app_key is required by client but validate endpoint doesn't check it)
    app_key = env("DD_APP_KEY") or "unused"

    try:
        with DatadogClient(site=site, api_key=api_key, app_key=app_key) as dd:
            data = dd.validate()
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

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
@click.option("--all-pages", is_flag=True, help="Fetch all pages (up to 50)")
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
def search_logs_cmd(
    query: str,
    site: str,
    time_from: str,
    time_to: str,
    limit: int,
    storage_tier: str | None,
    all_pages: bool,
    timeout: float,
    max_results: int | None,
    output_format: str,
) -> None:
    """Search logs with Datadog query syntax.

    Example: dd-incidents search-logs 'env:prod service:(svc1 OR svc2) order-123'
    """
    max_pages = 50 if all_pages else 1
    cursor: str | None = None
    all_logs: list[dict[str, Any]] = []

    try:
        with _get_client(site, timeout=timeout) as dd:
            for _ in range(max_pages):
                data = dd.search_logs(
                    query=query,
                    time_from=time_from,
                    time_to=time_to,
                    limit=limit,
                    cursor=cursor,
                    storage_tier=storage_tier,
                )

                logs = data.get("data", [])
                if isinstance(logs, list):
                    all_logs.extend(logs)

                # Check if we've hit max_results
                if max_results and len(all_logs) >= max_results:
                    all_logs = all_logs[:max_results]
                    break

                cursor = (data.get("meta") or {}).get("page", {}).get("after")
                if not cursor:
                    break

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    _output_logs(all_logs, output_format)


def _output_logs(logs: list[dict[str, Any]], output_format: str) -> None:
    """Output logs in the specified format."""
    if output_format == "json":
        click.echo(json.dumps({"data": logs, "count": len(logs)}, indent=2))
    elif output_format == "jsonl":
        for log in logs:
            click.echo(json.dumps(log))
    elif output_format == "messages":
        for log in logs:
            message = log.get("attributes", {}).get("message", "")
            if message:
                click.echo(message)


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
    help="Group by attribute path (can be repeated, e.g., --group-by service --group-by env)",
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

    Example: dd create-log-metric kafka.unknown_topic_errors \\
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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


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
    help="Notification message (supports @slack-channel, @pagerduty-service, template vars)",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Monitor tag (can be repeated, e.g., --tag team:my-team --tag env:prod)",
)
@click.option("--critical", type=float, help="Critical threshold")
@click.option("--warning", type=float, help="Warning threshold")
@click.option("--priority", type=int, help="Monitor priority (1-5)")
@click.option(
    "--renotify-interval",
    type=int,
    help="Minutes between re-notifications (0 to disable)",
)
@click.option(
    "--notify-no-data/--no-notify-no-data",
    default=False,
    help="Alert when no data is received",
)
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
    critical: float | None,
    warning: float | None,
    priority: int | None,
    renotify_interval: int | None,
    notify_no_data: bool,
    timeout: float,
) -> None:
    """Create a Datadog monitor.

    Example (metric monitor on a log-based metric):

        dd create-monitor \\
            --name 'My Service: Kafka topic errors' \\
            --type 'query alert' \\
            --query 'sum(last_5m):sum:kafka.unknown_topic_errors{env:prod}.as_count() > 100' \\
            --message '{{#is_alert}}Kafka UNKNOWN_TOPIC errors > {{threshold}}{{/is_alert}} @slack-alerts' \\
            --critical 100 --warning 50 \\
            --tag team:my-team --tag service:my-service
    """
    options: dict[str, Any] = {
        "notify_no_data": notify_no_data,
        "include_tags": True,
    }
    thresholds: dict[str, float] = {}
    if critical is not None:
        thresholds["critical"] = critical
    if warning is not None:
        thresholds["warning"] = warning
    if thresholds:
        options["thresholds"] = thresholds
    if renotify_interval is not None:
        options["renotify_interval"] = renotify_interval

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
        raise click.ClickException(str(e)) from None

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

        dd get-monitor 12345678

        dd get-monitor 'https://us3.datadoghq.com/monitors/12345678?...'
    """
    monitor_id = _parse_monitor_ref(monitor_id_or_url)

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_monitor(monitor_id, group_states=group_states)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

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
@click.option("--critical", type=float, help="Update critical threshold")
@click.option("--warning", type=float, help="Update warning threshold")
@click.option("--priority", type=int, help="Update priority (1-5)")
@click.option(
    "--renotify-interval",
    type=int,
    help="Minutes between re-notifications (0 to disable)",
)
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
    critical: float | None,
    warning: float | None,
    priority: int | None,
    renotify_interval: int | None,
    timeout: float,
) -> None:
    """Update a Datadog monitor by ID or URL.

    Only the specified fields are updated; others are left unchanged.

    Example:

    \b
        dd update-monitor 16440468 \\
            --query 'min(last_15m):sum:my.metric{*} > 0'
    """
    monitor_id = _parse_monitor_ref(monitor_id_or_url)

    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if query is not None:
        payload["query"] = query
    if message is not None:
        payload["message"] = message
    if priority is not None:
        payload["priority"] = priority

    options: dict[str, Any] = {}
    thresholds: dict[str, float] = {}
    if critical is not None:
        thresholds["critical"] = critical
    if warning is not None:
        thresholds["warning"] = warning
    if thresholds:
        options["thresholds"] = thresholds
    if renotify_interval is not None:
        options["renotify_interval"] = renotify_interval
    if options:
        payload["options"] = options

    if not payload:
        raise click.UsageError(
            "No updates specified. Use --help to see available options."
        )

    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.update_monitor(monitor_id, payload=payload)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

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

    Example: dd list-slos --tags 'env:prod,team:backend'
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.list_slos(tags=tags, limit=limit, offset=offset)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

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
        dd get-slo abc123def456
        dd get-slo abc123def456 --from now-30d
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
            except DatadogAPIError:
                slo_data["history"] = {"error": "Failed to fetch SLO history"}

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(slo_data, indent=2))


def _parse_time_to_epoch_s(value: str) -> int:
    """Convert a relative time string (now-1h, now-7d) or epoch seconds to int."""
    import re
    import time

    if value.isdigit():
        return int(value)

    m = re.match(r"now-(\d+)([mhd])", value)
    if not m:
        raise click.UsageError(
            f"Invalid time format: {value}. Use 'now-1h', 'now-7d', or epoch seconds."
        )
    amount = int(m.group(1))
    unit = m.group(2)
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    offset_s = amount * multipliers[unit]
    return int(time.time() - offset_s)


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

        dd get-workflow aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee

        dd get-workflow 'https://us3.datadoghq.com/workflow/aaaaaaaa-...' --instance
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
        raise click.ClickException(str(e)) from None

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
    import re
    import time

    if value.isdigit():
        return int(value)

    m = re.match(r"now-(\d+)([mhd])", value)
    if not m:
        raise click.UsageError(
            f"Invalid time format: {value}. Use 'now-1h', 'now-7d', or epoch ms."
        )
    amount = int(m.group(1))
    unit = m.group(2)
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    offset_s = amount * multipliers[unit]
    return int((time.time() - offset_s) * 1000)


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

    Example: dd search-et-issues 'service:my-service-*' --from now-7d

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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


@cli.command("get-et-issue")
@click.argument("issue_id", metavar="ISSUE_ID")
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
    site: str,
    timeout: float,
) -> None:
    """Get a single error tracking issue by ID.

    Example: dd get-et-issue aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
    """
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_error_tracking_issue(
                issue_id,
                include="issue,issue.assignee,issue.case",
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

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

    Example: dd update-et-issue-state aaaaaaaa-... RESOLVED
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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
