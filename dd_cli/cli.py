from __future__ import annotations

import json
from typing import Any

import click

from .http import DatadogAPIError, DatadogClient, env


def _default_site() -> str:
    return env("DD_SITE", "us3.datadoghq.com") or "us3.datadoghq.com"


def _get_client(site: str) -> DatadogClient:
    """Create a DatadogClient, raising UsageError if credentials are missing."""
    api_key = env("DD_API_KEY")
    app_key = env("DD_APP_KEY")

    if not api_key or not app_key:
        raise click.UsageError(
            "DD_API_KEY and DD_APP_KEY must be set. The v2 APIs require both."
        )

    return DatadogClient(site=site, api_key=api_key, app_key=app_key)


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
def search_logs_cmd(
    query: str,
    site: str,
    time_from: str,
    time_to: str,
    limit: int,
    storage_tier: str | None,
    all_pages: bool,
) -> None:
    """Search logs with Datadog query syntax.

    Example: dd-incidents search-logs 'env:prod service:(svc1 OR svc2) order-123'
    """
    max_pages = 50 if all_pages else 1
    cursor: str | None = None
    all_logs: list[dict[str, Any]] = []

    try:
        with _get_client(site) as dd:
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

                cursor = (data.get("meta") or {}).get("page", {}).get("after")
                if not cursor:
                    break

    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    click.echo(json.dumps({"data": all_logs, "count": len(all_logs)}, indent=2))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
