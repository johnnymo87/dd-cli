from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx


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
    """Exception for Datadog API errors."""

    status_code: int
    message: str
    response_body: str | None = None

    def __str__(self) -> str:
        return f"{self.message} (status={self.status_code})"


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
        api_key: str,
        app_key: str,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=_api_host(site),
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "DD-API-KEY": api_key,
                "DD-APPLICATION-KEY": app_key,
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a request and return parsed JSON response.

        Raises:
            DatadogAPIError: On 4xx/5xx responses
            RuntimeError: On network errors or invalid JSON
        """
        try:
            resp = self._client.request(method, path, params=params, json=json_body)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Try to extract error message from Datadog's response
            msg = "Datadog API error"
            body = e.response.text
            try:
                payload = e.response.json()
                if isinstance(payload, dict) and payload.get("errors"):
                    msg = "; ".join(str(err) for err in payload["errors"])
            except Exception:
                pass
            raise DatadogAPIError(e.response.status_code, msg, body) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Network error: {e}") from e

        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e.msg}") from e

    def get_incident(
        self,
        incident_id: str,
        *,
        include: str | None = None,
    ) -> dict[str, Any]:
        """Get incident by ID."""
        params = {"include": include} if include else None
        return self._request("GET", f"/api/v2/incidents/{incident_id}", params=params)

    def get_incident_type(self, incident_type_uuid: str) -> dict[str, Any]:
        """Get incident type configuration by UUID."""
        return self._request(
            "GET", f"/api/v2/incidents/config/types/{incident_type_uuid}"
        )

    def get_incident_integrations(self, incident_id: str) -> dict[str, Any]:
        """Get incident integrations (Slack, Jira, etc.)."""
        return self._request(
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
        return self._request(
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

        return self._request("POST", "/api/v2/logs/events/search", json_body=body)

    def validate(self) -> dict[str, Any]:
        """Validate API key. Note: only requires API key, not app key."""
        return self._request("GET", "/api/v1/validate")

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

        return self._request("GET", "/api/v2/catalog/entity", params=params)

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

        return self._request("GET", "/api/v2/team", params=params)

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

        return self._request(
            "GET",
            f"/api/v2/team/{team_id}/memberships",
            params=params,
        )

    def list_team_notification_rules(self, team_id: str) -> dict[str, Any]:
        """List notification rules for one Datadog Team."""
        return self._request("GET", f"/api/v2/team/{team_id}/notification-rules")

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
        return self._request("POST", "/api/v2/logs/config/metrics", json_body=payload)

    def get_log_metric(self, metric_id: str) -> dict[str, Any]:
        """Get a log-based metric by ID."""
        return self._request("GET", f"/api/v2/logs/config/metrics/{metric_id}")

    def list_log_metrics(self) -> dict[str, Any]:
        """List all log-based metrics."""
        return self._request("GET", "/api/v2/logs/config/metrics")

    def delete_log_metric(self, metric_id: str) -> dict[str, Any]:
        """Delete a log-based metric by ID."""
        return self._request("DELETE", f"/api/v2/logs/config/metrics/{metric_id}")

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
        return self._request("POST", "/api/v1/monitor", json_body=payload)

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
        return self._request("PUT", f"/api/v1/monitor/{monitor_id}", json_body=payload)

    def search_monitors(
        self,
        *,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Search monitors."""
        params = {}
        if query:
            params["query"] = query
        return self._request("GET", "/api/v1/monitor/search", params=params)

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
        # _request is typed as -> dict for the common case; cast accordingly.
        result: Any = self._request("GET", "/api/v1/monitor", params=params)
        return result if isinstance(result, list) else []

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
        return self._request(
            "GET", f"/api/v1/monitor/{monitor_id}", params=params or None
        )

    # ── Dashboards (v1) ─────────────────────────────────────────────

    def create_dashboard(self, *, body: dict[str, Any]) -> dict[str, Any]:
        """Create a dashboard (POST /api/v1/dashboard).

        The body is the full Datadog dashboard request object (title,
        layout_type, widgets, template_variables, etc.). It is sent as-is
        so callers can supply the exact widget/layout definition.
        """
        return self._request("POST", "/api/v1/dashboard", json_body=body)

    def get_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Get a dashboard's full definition by ID."""
        return self._request("GET", f"/api/v1/dashboard/{dashboard_id}")

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
        return self._request("GET", "/api/v1/dashboard", params=params or None)

    def delete_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Delete a dashboard by ID."""
        return self._request("DELETE", f"/api/v1/dashboard/{dashboard_id}")

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

        return self._request(
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
        return self._request(
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
        return self._request(
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
        return self._request("GET", "/api/v1/slo", params=params or None)

    def get_slo(self, slo_id: str) -> dict[str, Any]:
        """Get a single SLO by ID."""
        return self._request("GET", f"/api/v1/slo/{slo_id}")

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
        return self._request("GET", f"/api/v1/slo/{slo_id}/history", params=params)

    # ── Workflows (v2) ──────────────────────────────────────────────

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Get a workflow definition by ID."""
        return self._request("GET", f"/api/v2/workflows/{workflow_id}")

    def get_workflow_instance(
        self, workflow_id: str, instance_id: str
    ) -> dict[str, Any]:
        """Get a workflow execution instance."""
        return self._request(
            "GET",
            f"/api/v2/workflows/{workflow_id}/instances/{instance_id}",
        )

    # ── PagerDuty (v1) ──────────────────────────────────────────────

    def get_pagerduty_integration_service(self, service_name: str) -> dict[str, Any]:
        """Get PagerDuty integration service by name."""
        escaped_name = urllib.parse.quote(service_name, safe="")
        return self._request(
            "GET",
            f"/api/v1/integration/pagerduty/configuration/services/{escaped_name}",
        )
