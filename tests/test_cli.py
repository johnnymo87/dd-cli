"""Tests for dd_cli."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dd_cli.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_env():
    """Mock environment variables for tests."""
    with patch.dict(
        "os.environ",
        {
            "DD_SITE": "us3.datadoghq.com",
            "DD_PAT": "ddpat_test",
        },
    ):
        yield


class TestGetMonitor:
    """Tests for get-monitor command."""

    def test_get_monitor_by_id(self, runner, mock_env):
        """Verify get-monitor fetches a monitor by numeric ID."""
        monitor_response = {
            "id": 12345678,
            "name": "High CPU usage",
            "type": "query alert",
            "query": "avg(last_5m):...",
            "message": "CPU is too high",
            "overall_state": "OK",
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_monitor.return_value = monitor_response
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-monitor", "12345678"])

            assert result.exit_code == 0
            mock_client.get_monitor.assert_called_once_with(
                "12345678", group_states=None
            )
            output = json.loads(result.output)
            assert output["id"] == 12345678
            assert output["name"] == "High CPU usage"

    def test_get_monitor_with_group_states(self, runner, mock_env):
        """Verify --group-states is passed through to the API."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_monitor.return_value = {"id": 123, "overall_state": "Alert"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["get-monitor", "123", "--group-states", "alert,warn"]
            )

            assert result.exit_code == 0
            mock_client.get_monitor.assert_called_once_with(
                "123", group_states="alert,warn"
            )

    def test_get_monitor_from_url(self, runner, mock_env):
        """Verify get-monitor extracts monitor ID from a full Datadog URL."""
        url = (
            "https://us3.datadoghq.com/monitors/12345678?group=deployment%3Amy-service"
        )
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_monitor.return_value = {"id": 12345678}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-monitor", url])

            assert result.exit_code == 0
            mock_client.get_monitor.assert_called_once_with(
                "12345678", group_states=None
            )


class TestListMonitors:
    """Tests for list-monitors command."""

    def _full_monitor(self, monitor_id: int, **overrides: object) -> dict:
        """Build a representative full-shape monitor object."""
        m = {
            "id": monitor_id,
            "name": f"Monitor {monitor_id}",
            "type": "query alert",
            "overall_state": "OK",
            "tags": ["managed-by:dd-cli", "team:platform"],
            "query": "avg(last_5m):avg:system.cpu.user{*} > 80",
            "message": "CPU is high @slack-alerts",
            "options": {"thresholds": {"critical": 80, "warning": 60}},
            "creator": {"name": "dev"},
            "created": "2024-01-01T00:00:00Z",
        }
        m.update(overrides)
        return m

    def test_list_monitors_default_summary_format(self, runner, mock_env):
        """Default --format summary returns only id/name/type/overall_state/tags."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.return_value = [
                self._full_monitor(1),
                self._full_monitor(2, overall_state="Alert"),
            ]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["count"] == 2
            assert output["data"] == [
                {
                    "id": 1,
                    "name": "Monitor 1",
                    "type": "query alert",
                    "overall_state": "OK",
                    "tags": ["managed-by:dd-cli", "team:platform"],
                },
                {
                    "id": 2,
                    "name": "Monitor 2",
                    "type": "query alert",
                    "overall_state": "Alert",
                    "tags": ["managed-by:dd-cli", "team:platform"],
                },
            ]

    def test_list_monitors_tag_filter_passed_through(self, runner, mock_env):
        """Multiple --tag flags are joined with commas (AND semantics)."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.return_value = []
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "list-monitors",
                    "--tag",
                    "managed-by:dd-cli",
                    "--tag",
                    "team:platform",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_monitors.assert_called_once_with(
                tags=["managed-by:dd-cli", "team:platform"],
                name=None,
                page=0,
                page_size=1000,
            )

    def test_list_monitors_name_filter_passed_through(self, runner, mock_env):
        """--name is forwarded as the name kwarg."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.return_value = []
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors", "--name", "kafka"])

            assert result.exit_code == 0, result.output
            mock_client.list_monitors.assert_called_once_with(
                tags=None,
                name="kafka",
                page=0,
                page_size=1000,
            )

    def test_list_monitors_auto_paginates(self, runner, mock_env):
        """When --max-results allows it, a full first page triggers fetching
        the next page until a short page is returned."""
        full_page = [self._full_monitor(i) for i in range(1000)]
        partial_page = [self._full_monitor(i) for i in range(1000, 1050)]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.side_effect = [full_page, partial_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors", "--max-results", "5000"])

            assert result.exit_code == 0, result.output
            assert mock_client.list_monitors.call_count == 2
            # Page numbers should advance 0 -> 1
            assert mock_client.list_monitors.call_args_list[0].kwargs["page"] == 0
            assert mock_client.list_monitors.call_args_list[1].kwargs["page"] == 1
            output = json.loads(result.output)
            assert output["count"] == 1050

    def test_list_monitors_default_max_results_caps_at_1000(self, runner, mock_env):
        """Default --max-results=1000 caps after the first full page,
        even if more pages exist on the API side."""
        full_page = [self._full_monitor(i) for i in range(1000)]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.side_effect = [full_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors"])

            assert result.exit_code == 0, result.output
            # Only one API call — we hit the cap and stopped.
            assert mock_client.list_monitors.call_count == 1
            output = json.loads(result.output)
            assert output["count"] == 1000

    def test_list_monitors_max_results_truncates(self, runner, mock_env):
        """--max-results caps the total returned, even when more would fit
        in the next page."""
        page1 = [self._full_monitor(i) for i in range(1000)]
        page2 = [self._full_monitor(i) for i in range(1000, 2000)]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.side_effect = [page1, page2]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors", "--max-results", "1500"])

            assert result.exit_code == 0, result.output
            # Both pages fetched (first was full, can't tell if more without asking)
            assert mock_client.list_monitors.call_count == 2
            output = json.loads(result.output)
            assert output["count"] == 1500

    def test_list_monitors_format_jsonl(self, runner, mock_env):
        """--format jsonl emits one full monitor per line, no wrapper."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.return_value = [
                self._full_monitor(1),
                self._full_monitor(2),
            ]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors", "--format", "jsonl"])

            assert result.exit_code == 0, result.output
            lines = result.output.strip().split("\n")
            assert len(lines) == 2
            m1 = json.loads(lines[0])
            m2 = json.loads(lines[1])
            assert m1["id"] == 1
            # Full payload, not summary
            assert "query" in m1 and "message" in m1
            assert m2["id"] == 2

    def test_list_monitors_format_json_full(self, runner, mock_env):
        """--format json emits {count, data: [<full monitor>, ...]}."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.return_value = [self._full_monitor(1)]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors", "--format", "json"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["count"] == 1
            # Full payload, not summary
            assert "query" in output["data"][0]
            assert "message" in output["data"][0]
            assert "options" in output["data"][0]


class TestListCatalogEntities:
    """Tests for list-catalog-entities command."""

    def _entity(
        self, name: str, *, kind: str = "service", owner: str = "platform-team"
    ):
        return {
            "id": f"{kind}:{name}",
            "type": "entity",
            "attributes": {
                "kind": kind,
                "name": name,
                "owner": owner,
            },
            "meta": {"ingestionSource": "github"},
        }

    def test_list_catalog_entities_passes_filters_and_includes(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [self._entity("example-service")]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "list-catalog-entities",
                    "--kind",
                    "service",
                    "--owner",
                    "platform-team",
                    "--name",
                    "example-service",
                    "--ref",
                    "service:example-service",
                    "--include",
                    "schema",
                    "--include",
                    "raw_schema",
                    "--include-discovered",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind="service",
                owner="platform-team",
                name="example-service",
                ref="service:example-service",
                include=["schema", "raw_schema"],
                include_discovered=True,
                offset=0,
                limit=100,
            )
            output = json.loads(result.output)
            assert output["data"][0]["attributes"]["name"] == "example-service"

    def test_list_catalog_entities_auto_paginates_until_short_page(
        self, runner, mock_env
    ):
        first_page = {"data": [self._entity(f"svc-{i}") for i in range(100)]}
        second_page = {"data": [self._entity("last")]}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.side_effect = [first_page, second_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-catalog-entities", "--max-results", "500"]
            )

            assert result.exit_code == 0, result.output
            assert mock_client.list_catalog_entities.call_count == 2
            assert (
                mock_client.list_catalog_entities.call_args_list[0].kwargs["offset"]
                == 0
            )
            assert (
                mock_client.list_catalog_entities.call_args_list[1].kwargs["offset"]
                == 100
            )
            output = json.loads(result.output)
            assert output["count"] == 101

    def test_list_catalog_entities_summary_handles_null_fields(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [
                    {
                        "id": "service:empty",
                        "type": "entity",
                        "attributes": None,
                        "meta": None,
                    }
                ]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-catalog-entities", "--format", "summary"]
            )

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["count"] == 1
            assert output["data"][0]["id"] == "service:empty"
            assert output["data"][0]["name"] is None

    def test_list_catalog_entities_pagination_limits_last_page(self, runner, mock_env):
        first_page = {"data": [self._entity(f"svc-{i}") for i in range(100)]}
        second_page = {"data": [self._entity("last")]}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.side_effect = [first_page, second_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-catalog-entities", "--max-results", "101"]
            )

            assert result.exit_code == 0, result.output
            assert mock_client.list_catalog_entities.call_count == 2
            assert (
                mock_client.list_catalog_entities.call_args_list[0].kwargs["limit"]
                == 100
            )
            assert (
                mock_client.list_catalog_entities.call_args_list[1].kwargs["limit"] == 1
            )
            output = json.loads(result.output)
            assert output["count"] == 101

    def test_list_catalog_entities_format_jsonl(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [
                    self._entity("svc-1"),
                    self._entity("svc-2"),
                ]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-catalog-entities", "--format", "jsonl"])

            assert result.exit_code == 0, result.output
            lines = result.output.strip().split("\n")
            assert len(lines) == 2
            assert json.loads(lines[0])["attributes"]["name"] == "svc-1"
            assert json.loads(lines[1])["attributes"]["name"] == "svc-2"


class TestGetCatalogEntity:
    def test_get_catalog_entity_uses_ref_filter(self, runner, mock_env):
        entity = {
            "id": "service:example-service",
            "type": "entity",
            "attributes": {
                "kind": "service",
                "name": "example-service",
                "owner": "platform-team",
            },
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": [entity]}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "get-catalog-entity",
                    "service:example-service",
                    "--include",
                    "raw_schema",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind=None,
                owner=None,
                name=None,
                ref="service:example-service",
                include=["raw_schema"],
                include_discovered=False,
                offset=0,
                limit=2,
            )
            output = json.loads(result.output)
            assert output["data"]["id"] == "service:example-service"

    def test_get_catalog_entity_errors_when_not_found(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": []}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-catalog-entity", "service:missing"])

            assert result.exit_code != 0
            assert "No catalog entity found" in result.output

    def test_get_catalog_entity_help_text(self, runner):
        result = runner.invoke(cli, ["get-catalog-entity", "--help"])
        assert result.exit_code == 0
        assert "Include relationship data; repeatable." in result.output

    def test_get_catalog_entity_bare_name_fallback(self, runner, mock_env):
        entity = {
            "id": "service:example-service",
            "type": "entity",
            "attributes": {
                "kind": "service",
                "name": "example-service",
                "owner": "platform-team",
            },
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": [entity]}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["get-catalog-entity", "example-service", "--kind", "service"]
            )

            assert result.exit_code == 0
            mock_client.list_catalog_entities.assert_called_once_with(
                kind="service",
                owner=None,
                name="example-service",
                ref=None,
                include=None,
                include_discovered=False,
                offset=0,
                limit=2,
            )

    def test_get_catalog_entity_errors_on_multiple_matches(self, runner, mock_env):
        entity1 = {"id": "service:example-service", "type": "entity", "attributes": {}}
        entity2 = {"id": "db:example-service", "type": "entity", "attributes": {}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [entity1, entity2]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-catalog-entity", "example-service"])

            assert result.exit_code != 0
            assert "Multiple catalog entities matched example-service" in result.output


class TestGetCatalogOncall:
    """Tests for get-catalog-oncall command."""

    def test_get_catalog_oncall_uses_include_oncall(self, runner, mock_env):
        entity = {
            "id": "entity-1",
            "type": "entity",
            "attributes": {
                "ref": "service:auth-service",
                "kind": "service",
                "name": "auth-service",
            },
            "relationships": {
                "oncall": {"data": [{"id": "oncall-1", "type": "oncalls"}]}
            },
        }
        included = [
            {
                "id": "oncall-1",
                "type": "oncalls",
                "attributes": {
                    "provider": "PagerDuty",
                    "name": "Jane User",
                    "email": "jane@example.com",
                },
            }
        ]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [entity],
                "included": included,
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-catalog-oncall", "service:auth-service"])

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind=None,
                owner=None,
                name=None,
                ref="service:auth-service",
                include=["oncall"],
                include_discovered=False,
                offset=0,
                limit=2,
            )
            output = json.loads(result.output)
            assert output["entity"]["ref"] == "service:auth-service"
            assert output["oncall"][0]["id"] == "oncall-1"
            assert output["included"][0]["attributes"]["provider"] == "PagerDuty"


class TestListTeams:
    """Tests for list-teams command."""

    def _team(self, team_id: str, **overrides: object) -> dict:
        team: dict[str, object] = {
            "id": team_id,
            "type": "team",
            "attributes": {
                "name": f"Team {team_id}",
                "handle": f"team-{team_id}",
                "user_count": 3,
                "is_managed": False,
            },
        }
        team.update(overrides)
        return team

    def test_list_teams_default_summary_format(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {
                "data": [
                    self._team("abc"),
                    self._team(
                        "def",
                        attributes={
                            "name": "Example Team",
                            "handle": "example-team",
                            "user_count": 5,
                            "is_managed": True,
                        },
                    ),
                ]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-teams"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output == {
                "count": 2,
                "data": [
                    {
                        "id": "abc",
                        "name": "Team abc",
                        "handle": "team-abc",
                        "user_count": 3,
                        "is_managed": False,
                    },
                    {
                        "id": "def",
                        "name": "Example Team",
                        "handle": "example-team",
                        "user_count": 5,
                        "is_managed": True,
                    },
                ],
            }

    def test_list_teams_query_and_me_passed_through(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {"data": []}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "list-teams",
                    "--query",
                    "user@example.com",
                    "--me",
                    "--max-results",
                    "25",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_teams.assert_called_once_with(
                keyword="user@example.com",
                me=True,
                include=None,
                fields=None,
                page_number=0,
                page_size=25,
                sort=None,
            )

    def test_list_teams_auto_paginates_until_short_page(self, runner, mock_env):
        first_page = {"data": [self._team(str(i)) for i in range(100)]}
        second_page = {"data": [self._team("last")]}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.side_effect = [first_page, second_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-teams", "--max-results", "500"])

            assert result.exit_code == 0, result.output
            assert mock_client.list_teams.call_count == 2
            assert mock_client.list_teams.call_args_list[0].kwargs["page_number"] == 0
            assert mock_client.list_teams.call_args_list[1].kwargs["page_number"] == 1
            output = json.loads(result.output)
            assert output["count"] == 101

    def test_list_teams_format_jsonl(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {
                "data": [self._team("abc"), self._team("def")]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-teams", "--format", "jsonl"])

            assert result.exit_code == 0, result.output
            lines = result.output.strip().split("\n")
            assert len(lines) == 2
            assert json.loads(lines[0])["id"] == "abc"
            assert json.loads(lines[1])["id"] == "def"


class TestFindUserTeams:
    """Tests for find-user-teams command."""

    def test_find_user_teams_uses_member_search_keyword(self, runner, mock_env):
        team = {
            "id": "team-1",
            "type": "team",
            "attributes": {
                "name": "Example Team",
                "handle": "example-team",
                "user_count": 12,
                "is_managed": True,
            },
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {"data": [team]}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["find-user-teams", "user@example.com"])

            assert result.exit_code == 0, result.output
            mock_client.list_teams.assert_called_once_with(
                keyword="user@example.com",
                me=False,
                include=None,
                fields=None,
                page_number=0,
                page_size=100,
                sort=None,
            )
            output = json.loads(result.output)
            assert output["count"] == 1
            assert output["data"][0]["handle"] == "example-team"


class TestListTeamNotificationRules:
    """Tests for list-team-notification-rules command."""

    def test_list_team_notification_rules_resolves_handle(self, runner, mock_env):
        team = {
            "id": "team-123",
            "type": "team",
            "attributes": {"name": "Supply Chain", "handle": "supply-chain"},
        }
        rules = {
            "data": [
                {
                    "id": "rule-1",
                    "type": "team_notification_rules",
                    "attributes": {
                        "pagerduty": {"service_name": "datadog-routing-hub"},
                        "email": {"enabled": False},
                    },
                }
            ]
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {"data": [team]}
            mock_client.list_team_notification_rules.return_value = rules
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-team-notification-rules", "supply-chain"]
            )

            assert result.exit_code == 0, result.output
            mock_client.list_teams.assert_called_once_with(
                keyword="supply-chain",
                me=False,
                include=None,
                fields=["handle", "name"],
                page_number=0,
                page_size=100,
                sort=None,
            )
            mock_client.list_team_notification_rules.assert_called_once_with("team-123")
            output = json.loads(result.output)
            assert output["count"] == 1
            assert output["data"][0]["id"] == "rule-1"
            assert output["data"][0]["pagerduty_service_name"] == "datadog-routing-hub"

    def test_list_team_notification_rules_paginates_team_lookup(self, runner, mock_env):
        first_page = {
            "data": [
                {
                    "id": f"team-{i}",
                    "type": "team",
                    "attributes": {"handle": f"platform-{i}"},
                }
                for i in range(100)
            ]
        }
        second_page = {
            "data": [
                {
                    "id": "team-123",
                    "type": "team",
                    "attributes": {"handle": "platform"},
                }
            ]
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.side_effect = [first_page, second_page]
            mock_client.list_team_notification_rules.return_value = {"data": []}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-team-notification-rules", "platform"])

            assert result.exit_code == 0, result.output
            assert mock_client.list_teams.call_count == 2
            mock_client.list_team_notification_rules.assert_called_once_with("team-123")


class TestGetEtIssue:
    """Tests for get-et-issue command."""

    def test_get_et_issue_default_include(self, runner, mock_env):
        """Verify the default include uses the GET-endpoint's valid relationship
        names (assignee, case, team_owners), not the search endpoint's prefixed
        names (issue.assignee, issue.case). The wrong values cause a 400
        'invalid include' from the Datadog API.
        """
        issue_response = {
            "data": {
                "id": "1a14f4fc-182a-11f1-994a-da7ad0900003",
                "type": "issue",
                "attributes": {"state": "OPEN"},
            }
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_error_tracking_issue.return_value = issue_response
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["get-et-issue", "1a14f4fc-182a-11f1-994a-da7ad0900003"]
            )

            assert result.exit_code == 0, result.output
            mock_client.get_error_tracking_issue.assert_called_once_with(
                "1a14f4fc-182a-11f1-994a-da7ad0900003",
                include="assignee,case,team_owners",
            )

    def test_get_et_issue_custom_include(self, runner, mock_env):
        """Verify --include overrides the default and is passed through."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_error_tracking_issue.return_value = {"data": {}}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "get-et-issue",
                    "abc-123",
                    "--include",
                    "assignee",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.get_error_tracking_issue.assert_called_once_with(
                "abc-123",
                include="assignee",
            )

    def test_get_et_issue_no_include(self, runner, mock_env):
        """Verify --include '' suppresses the include query param entirely."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_error_tracking_issue.return_value = {"data": {}}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-et-issue", "abc-123", "--include", ""])

            assert result.exit_code == 0, result.output
            mock_client.get_error_tracking_issue.assert_called_once_with(
                "abc-123",
                include=None,
            )


class TestSearchLogsTimeout:
    """Tests for --timeout option."""

    def test_timeout_option_passed_to_client(self, runner, mock_env):
        """Verify --timeout value is passed to DatadogClient."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.search_logs.return_value = {"data": [], "meta": {}}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["search-logs", "test query", "--timeout", "120"]
            )

            assert result.exit_code == 0
            mock_client_class.assert_called_once()
            call_kwargs = mock_client_class.call_args.kwargs
            assert call_kwargs["timeout"] == 120.0

    def test_default_timeout_is_15_seconds(self, runner, mock_env):
        """Verify default timeout is 15 seconds when not specified."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.search_logs.return_value = {"data": [], "meta": {}}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["search-logs", "test query"])

            assert result.exit_code == 0
            call_kwargs = mock_client_class.call_args.kwargs
            assert call_kwargs["timeout"] == 15.0


class TestSearchLogsMaxResults:
    """Tests for --max-results option."""

    def test_max_results_stops_pagination_early(self, runner, mock_env):
        """Verify --max-results stops fetching when limit reached."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            # First page returns 100 logs with cursor for more
            mock_client.search_logs.side_effect = [
                {
                    "data": [{"id": str(i)} for i in range(100)],
                    "meta": {"page": {"after": "cursor1"}},
                },
                {
                    "data": [{"id": str(i)} for i in range(100, 200)],
                    "meta": {"page": {"after": "cursor2"}},
                },
            ]
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                ["search-logs", "test query", "--all-pages", "--max-results", "50"],
            )

            assert result.exit_code == 0
            # Should only call search_logs once since first page has >= 50 results
            assert mock_client.search_logs.call_count == 1

            output = json.loads(result.output)
            assert output["count"] == 50
            assert len(output["data"]) == 50

    def test_max_results_fetches_multiple_pages_if_needed(self, runner, mock_env):
        """Verify --max-results fetches more pages when first page is insufficient."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            # Each page returns 50 logs
            mock_client.search_logs.side_effect = [
                {
                    "data": [{"id": str(i)} for i in range(50)],
                    "meta": {"page": {"after": "cursor1"}},
                },
                {
                    "data": [{"id": str(i)} for i in range(50, 100)],
                    "meta": {"page": {"after": "cursor2"}},
                },
                {
                    "data": [{"id": str(i)} for i in range(100, 150)],
                    "meta": {"page": {"after": "cursor3"}},
                },
            ]
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                ["search-logs", "test query", "--all-pages", "--max-results", "120"],
            )

            assert result.exit_code == 0
            # Should call search_logs 3 times to get 120 results
            assert mock_client.search_logs.call_count == 3

            output = json.loads(result.output)
            assert output["count"] == 120


class TestSearchLogsFormat:
    """Tests for --format option."""

    def test_format_jsonl_outputs_one_object_per_line(self, runner, mock_env):
        """Verify --format jsonl outputs one JSON object per line."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.search_logs.return_value = {
                "data": [
                    {"id": "1", "attributes": {"message": "log 1"}},
                    {"id": "2", "attributes": {"message": "log 2"}},
                ],
                "meta": {},
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["search-logs", "test query", "--format", "jsonl"]
            )

            assert result.exit_code == 0
            lines = result.output.strip().split("\n")
            assert len(lines) == 2

            log1 = json.loads(lines[0])
            log2 = json.loads(lines[1])
            assert log1["id"] == "1"
            assert log2["id"] == "2"

    def test_format_messages_outputs_message_field_only(self, runner, mock_env):
        """Verify --format messages outputs only the message field."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.search_logs.return_value = {
                "data": [
                    {"id": "1", "attributes": {"message": "First log message"}},
                    {"id": "2", "attributes": {"message": "Second log message"}},
                ],
                "meta": {},
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["search-logs", "test query", "--format", "messages"]
            )

            assert result.exit_code == 0
            lines = result.output.strip().split("\n")
            assert lines == ["First log message", "Second log message"]

    def test_default_format_is_json(self, runner, mock_env):
        """Verify default format is json with data and count."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.search_logs.return_value = {
                "data": [{"id": "1"}],
                "meta": {},
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["search-logs", "test query"])

            assert result.exit_code == 0
            output = json.loads(result.output)
            assert "data" in output
            assert "count" in output
            assert output["count"] == 1


class TestCatalogClient:
    """Tests for Software Catalog client methods."""

    def test_list_catalog_entities_defaults(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_catalog_entities()

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/catalog/entity",
                params={
                    "page[offset]": 0,
                    "page[limit]": 100,
                },
            )
        finally:
            dd.close()


class TestTeamsClient:
    """Tests for DatadogClient Teams methods."""

    def test_list_teams_defaults(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_teams()

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/team",
                params={
                    "page[number]": 0,
                    "page[size]": 100,
                },
            )
        finally:
            dd.close()

    def test_list_teams_builds_query_params(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_teams(
                keyword="user@example.com",
                me=True,
                include=["team_links"],
                fields=["handle", "name", "user_count"],
                page_number=2,
                page_size=50,
                sort="name",
            )

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/team",
                params={
                    "page[number]": 2,
                    "page[size]": 50,
                    "filter[keyword]": "user@example.com",
                    "filter[me]": True,
                    "include": "team_links",
                    "fields[team]": "handle,name,user_count",
                    "sort": "name",
                },
            )
        finally:
            dd.close()

    def test_list_team_memberships_builds_query_params(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_team_memberships(
                "team-123",
                keyword="Jane User",
                page_number=1,
                page_size=25,
                sort="email",
            )

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/team/team-123/memberships",
                params={
                    "page[number]": 1,
                    "page[size]": 25,
                    "filter[keyword]": "Jane User",
                    "sort": "email",
                },
            )
        finally:
            dd.close()

    def test_list_team_notification_rules(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_team_notification_rules("team-123")

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/team/team-123/notification-rules",
            )
        finally:
            dd.close()

    def test_list_catalog_entities_builds_query_params(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_catalog_entities(
                kind="service",
                owner="platform-team",
                name="example-service",
                ref="service:example-service",
                include=["schema", "raw_schema"],
                include_discovered=True,
                offset=100,
                limit=50,
            )

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/catalog/entity",
                params={
                    "page[offset]": 100,
                    "page[limit]": 50,
                    "filter[kind]": "service",
                    "filter[owner]": "platform-team",
                    "filter[name]": "example-service",
                    "filter[ref]": "service:example-service",
                    "include": "schema,raw_schema",
                    "includeDiscovered": True,
                },
            )
        finally:
            dd.close()


class TestCatalogYamlHelpers:
    """Tests for local catalog YAML validation and validate-catalog command."""

    def test_validate_catalog_accepts_v3_pagerduty_service_url(self, runner):
        """Verify that validate-catalog accepts v3 PagerDuty
        integrations.pagerduty.serviceURL.
        """
        from pathlib import Path

        with runner.isolated_filesystem():
            content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: my-service
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P123456
"""
            Path("service.datadog.yaml").write_text(content.strip())

            result = runner.invoke(cli, ["validate-catalog", "service.datadog.yaml"])
            assert result.exit_code == 0, result.output

            # Check JSON output format which is default
            output = json.loads(result.output)
            assert output["ok"] is True
            assert output["count"] == 1
            assert len(output["errors"]) == 0

    def test_validate_catalog_rejects_v3_pagerduty_invalid_fields(self, runner):
        """Verify that validate-catalog rejects v3 PagerDuty service-name."""
        from pathlib import Path

        with runner.isolated_filesystem():
            content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: my-service
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P123456
    service-name: my-pd-service
"""
            Path("service.datadog.yaml").write_text(content.strip())

            result = runner.invoke(cli, ["validate-catalog", "service.datadog.yaml"])
            assert result.exit_code == 1, result.output

            output = json.loads(result.output)
            assert output["ok"] is False
            assert output["count"] == 1
            assert len(output["errors"]) == 1

            err = output["errors"][0]
            assert err["path"] == "service.datadog.yaml"
            assert err["field"] == "integrations.pagerduty.service-name"
            assert "service-name" in err["message"]


class TestListCatalogPagerdutyLinks:
    """Tests for list-catalog-pagerduty-links command."""

    def test_list_catalog_pagerduty_links_json_default(self, runner):
        """Verify list-catalog-pagerduty-links JSON output format by default."""
        from pathlib import Path

        with runner.isolated_filesystem():
            content1 = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: auth-service
  tags:
    - team:identity
schema-version: v3
dd-team:
  team-handle: identity-ops
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P123456
"""
            content2 = """
apiVersion: datadoghq.com/v1alpha1
kind: datastore
metadata:
  name: user-db
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P789012
---
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: untracked-service
schema-version: v3
"""
            Path("auth.datadog.yaml").write_text(content1.strip())
            Path("db.datadog.yaml").write_text(content2.strip())

            result = runner.invoke(cli, ["list-catalog-pagerduty-links"])
            assert result.exit_code == 0, result.output

            output = json.loads(result.output)
            assert "count" in output
            assert "data" in output
            assert output["count"] == 2

            links = output["data"]
            # Sorted by file path and document index
            assert links[0]["name"] == "auth-service"
            assert links[0]["kind"] == "service"
            assert links[0]["ref"] == "service:auth-service"
            assert links[0]["owner"] == "identity-ops"
            assert links[0]["tags"] == ["team:identity"]
            assert "auth.datadog.yaml" in links[0]["path"]
            assert links[0]["document"] == 1
            assert (
                links[0]["serviceURL"]
                == "https://example.pagerduty.com/services/P123456"
            )

            assert links[1]["name"] == "user-db"
            assert links[1]["kind"] == "datastore"
            assert links[1]["ref"] == "datastore:user-db"
            assert links[1]["document"] == 1
            assert (
                links[1]["serviceURL"]
                == "https://example.pagerduty.com/services/P789012"
            )

    def test_list_catalog_pagerduty_links_jsonl(self, runner):
        """Verify list-catalog-pagerduty-links with --format jsonl."""
        from pathlib import Path

        with runner.isolated_filesystem():
            content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: billing-service
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P444444
"""
            Path("billing.datadog.yaml").write_text(content.strip())

            result = runner.invoke(
                cli, ["list-catalog-pagerduty-links", "--format", "jsonl"]
            )
            assert result.exit_code == 0, result.output

            lines = result.output.strip().split("\n")
            assert len(lines) == 1
            link = json.loads(lines[0])
            assert link["name"] == "billing-service"
            assert (
                link["serviceURL"] == "https://example.pagerduty.com/services/P444444"
            )

    def test_list_catalog_pagerduty_links_summary(self, runner):
        """Verify list-catalog-pagerduty-links with --format summary."""
        from pathlib import Path

        with runner.isolated_filesystem():
            content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: core-service
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P555555
"""
            Path("core.datadog.yaml").write_text(content.strip())

            result = runner.invoke(
                cli, ["list-catalog-pagerduty-links", "--format", "summary"]
            )
            assert result.exit_code == 0, result.output

            output = json.loads(result.output)
            assert output["count"] == 1
            summary_entry = output["data"][0]
            assert summary_entry["ref"] == "service:core-service"
            assert (
                summary_entry["serviceURL"]
                == "https://example.pagerduty.com/services/P555555"
            )

    def test_list_catalog_pagerduty_links_with_obsolete_keys(self, runner):
        """Verify listing works even with obsolete/invalid fields present."""
        from pathlib import Path

        with runner.isolated_filesystem():
            content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: obsolete-keys-service
schema-version: v3
integrations:
  pagerduty:
    serviceURL: https://example.pagerduty.com/services/P111111
    service-name: my-legacy-service
"""
            Path("obsolete.datadog.yaml").write_text(content.strip())

            result = runner.invoke(cli, ["list-catalog-pagerduty-links"])
            assert result.exit_code == 0, result.output

            output = json.loads(result.output)
            assert output["count"] == 1
            entry = output["data"][0]
            assert entry["name"] == "obsolete-keys-service"
            assert (
                entry["serviceURL"] == "https://example.pagerduty.com/services/P111111"
            )

    def test_list_catalog_pagerduty_links_yaml_error(self, runner):
        """Verify list-catalog-pagerduty-links behavior on YAML loading error."""
        from pathlib import Path

        with runner.isolated_filesystem():
            # Write a malformed YAML file (unbalanced quotes/brackets/indentation)
            bad_content = """
apiVersion: datadoghq.com/v1alpha1
kind: service
metadata:
  name: [unclosed bracket
"""
            Path("bad.datadog.yaml").write_text(bad_content)

            result = runner.invoke(cli, ["list-catalog-pagerduty-links"])
            assert result.exit_code == 1, result.output

            output = json.loads(result.output)
            assert output["ok"] is False
            assert "errors" in output
            assert len(output["errors"]) > 0
            assert "bad.datadog.yaml" in output["errors"][0]["path"]
            assert "YAML parsing error" in output["errors"][0]["message"]


class TestPagerDutyClient:
    """Tests for DatadogClient PagerDuty integration methods."""

    def test_get_pagerduty_integration_service(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            expected_response = {
                "service_name": "datadog-routing-hub",
                "service_key": "abcd1234abcd1234abcd1234abcd1234",
            }
            dd._request = MagicMock(return_value=expected_response)

            result = dd.get_pagerduty_integration_service("datadog-routing-hub")

            assert result == expected_response
            dd._request.assert_called_once_with(
                "GET",
                "/api/v1/integration/pagerduty/configuration/services/datadog-routing-hub",
            )
        finally:
            dd.close()

    def test_get_pagerduty_integration_service_escapes_spaces(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            expected_response = {
                "service_name": "datadog routing hub",
                "service_key": "abcd1234abcd1234abcd1234abcd1234",
            }
            dd._request = MagicMock(return_value=expected_response)

            result = dd.get_pagerduty_integration_service("datadog routing hub")

            assert result == expected_response
            dd._request.assert_called_once_with(
                "GET",
                "/api/v1/integration/pagerduty/configuration/services/datadog%20routing%20hub",
            )
        finally:
            dd.close()


class TestCheckPagerDutyServiceCli:
    """Tests for check-pagerduty-service CLI command."""

    def test_check_pagerduty_service_success(self, runner, mock_env):
        """Verify command calls get_pagerduty_integration_service
        with correct service name.
        """
        service_response = {
            "service_name": "datadog-routing-hub",
            "service_key": "abcd1234abcd1234abcd1234abcd1234",
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_pagerduty_integration_service.return_value = (
                service_response
            )
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["check-pagerduty-service", "datadog-routing-hub"]
            )

            assert result.exit_code == 0
            mock_client.get_pagerduty_integration_service.assert_called_once_with(
                "datadog-routing-hub"
            )
            output = json.loads(result.output)
            assert output["service_name"] == "datadog-routing-hub"
            assert "service_key" not in output


class TestCreateDashboard:
    """Tests for create-dashboard command."""

    def _spec_file(self, tmp_path, body):
        spec = tmp_path / "dashboard.json"
        spec.write_text(json.dumps(body), encoding="utf-8")
        return str(spec)

    def test_create_dashboard_from_spec(self, runner, mock_env, tmp_path):
        """Verify --spec body is sent as-is and id/url are printed."""
        spec_body = {
            "title": "My dashboard",
            "layout_type": "ordered",
            "widgets": [{"definition": {"type": "note", "content": "hi"}}],
        }
        spec_path = self._spec_file(tmp_path, spec_body)
        create_response = {
            "id": "abc-def-ghi",
            "title": "My dashboard",
            "url": "/dashboard/abc-def-ghi/my-dashboard",
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.create_dashboard.return_value = create_response
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["create-dashboard", "--spec", spec_path])

            assert result.exit_code == 0, result.output
            mock_client.create_dashboard.assert_called_once_with(body=spec_body)
            output = json.loads(result.output)
            assert output["id"] == "abc-def-ghi"
            assert output["url"] == (
                "https://us3.datadoghq.com/dashboard/abc-def-ghi/my-dashboard"
            )
            assert output["title"] == "My dashboard"

    def test_create_dashboard_flags_override_spec(self, runner, mock_env, tmp_path):
        """Verify --title/--description/--layout-type/--tag override spec keys."""
        spec_path = self._spec_file(
            tmp_path, {"title": "old", "widgets": [], "layout_type": "free"}
        )
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.create_dashboard.return_value = {"id": "xyz"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-dashboard",
                    "--spec",
                    spec_path,
                    "--title",
                    "new title",
                    "--description",
                    "desc",
                    "--layout-type",
                    "ordered",
                    "--tag",
                    "team:fbm",
                    "--tag",
                    "managed-by:dd-cli",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.create_dashboard.assert_called_once_with(
                body={
                    "title": "new title",
                    "widgets": [],
                    "layout_type": "ordered",
                    "description": "desc",
                    "tags": ["team:fbm", "managed-by:dd-cli"],
                }
            )

    def test_create_dashboard_defaults_layout_and_widgets(self, runner, mock_env):
        """Verify a bare --title creates a valid body with defaults."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.create_dashboard.return_value = {"id": "xyz"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["create-dashboard", "--title", "Just a title"])

            assert result.exit_code == 0, result.output
            mock_client.create_dashboard.assert_called_once_with(
                body={
                    "title": "Just a title",
                    "layout_type": "ordered",
                    "widgets": [],
                }
            )

    def test_create_dashboard_requires_title(self, runner, mock_env, tmp_path):
        """Verify a missing title (no flag, none in spec) is a usage error."""
        spec_path = self._spec_file(tmp_path, {"widgets": []})
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["create-dashboard", "--spec", spec_path])

            assert result.exit_code != 0
            assert "title is required" in result.output
            mock_client.create_dashboard.assert_not_called()

    def test_create_dashboard_url_fallback_from_id(self, runner, mock_env):
        """Verify the URL falls back to /dashboard/{id} when url is absent."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.create_dashboard.return_value = {"id": "no-url-id"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["create-dashboard", "--title", "t"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["url"] == "https://us3.datadoghq.com/dashboard/no-url-id"


class TestGetDashboard:
    """Tests for get-dashboard command."""

    def test_get_dashboard_by_id(self, runner, mock_env):
        """Verify get-dashboard fetches a dashboard by ID."""
        dashboard_response = {
            "id": "abc-def-ghi",
            "title": "My dashboard",
            "widgets": [],
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_dashboard.return_value = dashboard_response
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-dashboard", "abc-def-ghi"])

            assert result.exit_code == 0, result.output
            mock_client.get_dashboard.assert_called_once_with("abc-def-ghi")
            output = json.loads(result.output)
            assert output["id"] == "abc-def-ghi"

    def test_get_dashboard_from_url(self, runner, mock_env):
        """Verify get-dashboard extracts the ID from a full Datadog URL."""
        url = "https://us3.datadoghq.com/dashboard/abc-def-ghi/my-dashboard-slug"
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_dashboard.return_value = {"id": "abc-def-ghi"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-dashboard", url])

            assert result.exit_code == 0, result.output
            mock_client.get_dashboard.assert_called_once_with("abc-def-ghi")


class TestListDashboards:
    """Tests for list-dashboards command."""

    def _full_dashboard(self, dashboard_id, title, **overrides):
        d = {
            "id": dashboard_id,
            "title": title,
            "url": f"/dashboard/{dashboard_id}/slug",
            "layout_type": "ordered",
            "author_handle": "dev@example.com",
            "description": "desc",
        }
        d.update(overrides)
        return d

    def test_list_dashboards_default_summary_format(self, runner, mock_env):
        """Default --format summary returns projected fields only."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_dashboards.return_value = {
                "dashboards": [
                    self._full_dashboard("id1", "One"),
                    self._full_dashboard("id2", "Two"),
                ]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-dashboards"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["count"] == 2
            assert output["data"][0] == {
                "id": "id1",
                "title": "One",
                "url": "/dashboard/id1/slug",
                "layout_type": "ordered",
                "author_handle": "dev@example.com",
            }

    def test_list_dashboards_name_filter(self, runner, mock_env):
        """Verify --name filters client-side by title substring."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_dashboards.return_value = {
                "dashboards": [
                    self._full_dashboard("id1", "FBM canary REJECTED"),
                    self._full_dashboard("id2", "Unrelated dashboard"),
                ]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-dashboards", "--name", "fbm canary"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["count"] == 1
            assert output["data"][0]["id"] == "id1"

    def test_list_dashboards_format_jsonl(self, runner, mock_env):
        """Verify --format jsonl emits one full dashboard per line."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_dashboards.return_value = {
                "dashboards": [self._full_dashboard("id1", "One")]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-dashboards", "--format", "jsonl"])

            assert result.exit_code == 0, result.output
            lines = [ln for ln in result.output.splitlines() if ln.strip()]
            assert len(lines) == 1
            assert json.loads(lines[0])["description"] == "desc"


class TestDashboardClient:
    """Tests for DatadogClient dashboard methods."""

    def test_create_dashboard_posts_body(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            body = {"title": "t", "layout_type": "ordered", "widgets": []}
            dd._request = MagicMock(return_value={"id": "abc"})

            result = dd.create_dashboard(body=body)

            assert result == {"id": "abc"}
            dd._request.assert_called_once_with(
                "POST", "/api/v1/dashboard", json_body=body
            )
        finally:
            dd.close()

    def test_get_dashboard(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"id": "abc"})

            result = dd.get_dashboard("abc")

            assert result == {"id": "abc"}
            dd._request.assert_called_once_with("GET", "/api/v1/dashboard/abc")
        finally:
            dd.close()

    def test_list_dashboards_defaults(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._request = MagicMock(return_value={"dashboards": []})

            result = dd.list_dashboards()

            assert result == {"dashboards": []}
            dd._request.assert_called_once_with("GET", "/api/v1/dashboard", params=None)
        finally:
            dd.close()


class TestAuthHeaders:
    """Tests for how the PAT becomes an HTTP auth header."""

    def test_pat_uses_bearer_authorization(self):
        """A PAT authenticates via `Authorization: Bearer` and sets no
        DD-API-KEY / DD-APPLICATION-KEY headers."""
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_secret")
        try:
            headers = dd._client.headers
            assert headers.get("authorization") == "Bearer ddpat_secret"
            assert "dd-api-key" not in headers
            assert "dd-application-key" not in headers
        finally:
            dd.close()

    def test_missing_pat_raises(self):
        """Constructing a client without a PAT is an error."""
        from dd_cli.http import DatadogClient

        with pytest.raises(ValueError):
            DatadogClient(site="us3.datadoghq.com", pat="")

    def test_validate_uses_current_user(self):
        """validate() hits /api/v2/current_user (a PAT-compatible read; the
        legacy /api/v1/validate rejects a PAT with 403)."""
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_x")
        try:
            dd._request = MagicMock(return_value={"data": {}})
            dd.validate()
            dd._request.assert_called_once_with("GET", "/api/v2/current_user")
        finally:
            dd.close()


class TestGetClientCredentialSelection:
    """Tests for _get_client reading DD_PAT."""

    def test_uses_pat_when_set(self):
        from dd_cli.cli import _get_client

        with patch.dict(
            "os.environ",
            {"DD_PAT": "ddpat_x"},
            clear=True,
        ), patch("dd_cli.cli.DatadogClient") as mock_client_class:
            _get_client("us3.datadoghq.com")
            mock_client_class.assert_called_once_with(
                site="us3.datadoghq.com", pat="ddpat_x", timeout=15.0
            )

    def test_errors_when_pat_missing(self):
        import click

        from dd_cli.cli import _get_client

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(click.UsageError):
                _get_client("us3.datadoghq.com")
