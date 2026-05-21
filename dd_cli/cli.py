from __future__ import annotations

import json
from pathlib import Path
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

    Example: dd-cli search-logs 'env:prod service:(svc1 OR svc2) order-123'
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
    help="Notification message (supports @slack, @pagerduty, template vars)",
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

        dd-cli create-monitor \\
            --name 'My Service: Kafka topic errors' \\
            --type 'query alert' \\
            --query 'sum(last_5m):sum:kafka.errors{env:prod}.as_count() > 100' \\
            --message '{{#is_alert}}Kafka errors > {{threshold}}{{/is_alert}} @slack' \\
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
def list_monitors_cmd(
    tags: tuple[str, ...],
    name: str | None,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
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

                if len(monitors) >= max_results:
                    monitors = monitors[:max_results]
                    break

                # A short page means we've reached the end.
                if len(batch) < page_size:
                    break

                page += 1
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    _output_monitors(monitors, output_format)


def _output_monitors(monitors: list[dict[str, Any]], output_format: str) -> None:
    """Output monitors in the specified format."""
    if output_format == "summary":
        summary = [_monitor_summary(m) for m in monitors]
        click.echo(json.dumps({"count": len(summary), "data": summary}, indent=2))
    elif output_format == "json":
        click.echo(json.dumps({"count": len(monitors), "data": monitors}, indent=2))
    elif output_format == "jsonl":
        for m in monitors:
            click.echo(json.dumps(m))


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
) -> None:
    """List Software Catalog entities."""
    page_size = 100
    entities: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    include_list = list(includes) if includes else None

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
                    entities = entities[:max_results]
                    break
                if len(batch) < limit:
                    break
                offset += len(batch)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    _output_catalog_entities(entities, included, output_format)


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
        raise click.ClickException(str(e)) from None

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


def _output_catalog_entities(
    entities: list[dict[str, Any]],
    included: list[dict[str, Any]],
    output_format: str,
) -> None:
    if output_format == "jsonl":
        for entity in entities:
            click.echo(json.dumps(entity))
        return

    if output_format == "summary":
        data = [_catalog_entity_summary(entity) for entity in entities]
        click.echo(json.dumps({"count": len(data), "data": data}, indent=2))
        return

    output: dict[str, Any] = {"count": len(entities), "data": entities}
    if included:
        output["included"] = included
    click.echo(json.dumps(output, indent=2))


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
        raise click.ClickException(str(e)) from None

    _output_teams(teams, output_format)


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
def find_user_teams_cmd(
    member: str,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
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
        raise click.ClickException(str(e)) from None

    _output_teams(teams, output_format)


def _fetch_teams(
    dd: DatadogClient,
    *,
    keyword: str | None,
    me: bool,
    include: list[str] | None,
    fields: list[str] | None,
    sort: str | None,
    max_results: int,
) -> list[dict[str, Any]]:
    page_size = 100
    teams: list[dict[str, Any]] = []
    page_number = 0

    while len(teams) < max_results:
        limit = min(page_size, max_results - len(teams))
        if limit <= 0:
            break

        page = dd.list_teams(
            keyword=keyword,
            me=me,
            include=include,
            fields=fields,
            page_number=page_number,
            page_size=limit,
            sort=sort,
        )
        batch = page.get("data", [])
        teams.extend(batch)

        if len(batch) < limit:
            break
        page_number += 1

    return teams[:max_results]


def _output_teams(teams: list[dict[str, Any]], output_format: str) -> None:
    if output_format == "jsonl":
        for team in teams:
            click.echo(json.dumps(team))
        return

    if output_format == "json":
        click.echo(json.dumps({"count": len(teams), "data": teams}, indent=2))
        return

    data = [_team_summary(team) for team in teams]
    click.echo(json.dumps({"count": len(data), "data": data}, indent=2))


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
        dd-cli update-monitor 16440468 \\
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

    Example: dd-cli list-slos --tags 'env:prod,team:backend'
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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


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
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps(data, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
