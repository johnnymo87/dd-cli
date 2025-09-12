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
