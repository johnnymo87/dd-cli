"""Tests for dd_cli."""

import json
from unittest.mock import ANY, MagicMock, patch

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
        """Default --max-results=1000 caps after the first full page.

        The default cap equals Datadog's max page_size, so an org with >=1000
        monitors hits it on every default invocation and is told the answer is
        incomplete. Loud-and-correct is the intended trade here: the previous
        behaviour returned exactly 1000 and looked like a complete list.
        """
        full_page = [self._full_monitor(i) for i in range(1000)]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_monitors.side_effect = [full_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["list-monitors"])

            assert result.exit_code == 3, result.output
            # Only one API call — we hit the cap and stopped.
            assert mock_client.list_monitors.call_count == 1
            output = json.loads(result.stdout)
            assert output["count"] == 1000
            assert output["truncated"] is True

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

            # The cap bit, so the answer is incomplete and says so.
            assert result.exit_code == 3, result.output
            # Both pages fetched (first was full, can't tell if more without asking)
            assert mock_client.list_monitors.call_count == 2
            output = json.loads(result.stdout)
            assert output["count"] == 1500
            assert output["truncated"] is True
            assert output["truncation_reason"] == "max_results_boundary_unknown"

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
            # stdout carries only records; the count trailer goes to stderr.
            lines = result.stdout.strip().split("\n")
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
            output = json.loads(result.stdout)
            assert output["count"] == 101
            # A short final page means the list really did end here.
            assert output["truncated"] is False

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

            # The cap bit exactly, which offset paging cannot distinguish from
            # "the list ended here" -- so it reports incomplete.
            assert result.exit_code == 3, result.output
            assert mock_client.list_catalog_entities.call_count == 2
            assert (
                mock_client.list_catalog_entities.call_args_list[0].kwargs["limit"]
                == 100
            )
            assert (
                mock_client.list_catalog_entities.call_args_list[1].kwargs["limit"] == 1
            )
            output = json.loads(result.stdout)
            assert output["count"] == 101
            # The --max-results cap bit, so the answer admits it is incomplete.
            assert output["truncated"] is True

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
            lines = result.stdout.strip().split("\n")
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
            output = json.loads(result.stdout)
            assert output["ok"] is True
            assert output["truncated"] is False
            assert {"count": output["count"], "data": output["data"]} == {
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
            output = json.loads(result.stdout)
            assert output["count"] == 101
            # A short final page means the list really did end here.
            assert output["truncated"] is False

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
            lines = result.stdout.strip().split("\n")
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

            # A cursor was still outstanding, so the answer is incomplete.
            assert result.exit_code == 3
            # Should only call search_logs once since first page has >= 50 results
            assert mock_client.search_logs.call_count == 1

            output = json.loads(result.stdout)
            assert output["count"] == 50
            assert len(output["data"]) == 50
            assert output["truncated"] is True
            assert output["truncation_reason"] == "more_available"

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

            assert result.exit_code == 3
            # Should call search_logs 3 times to get 120 results
            assert mock_client.search_logs.call_count == 3

            output = json.loads(result.stdout)
            assert output["count"] == 120
            assert output["truncated"] is True


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
            # stdout carries only records; the count trailer goes to stderr.
            lines = result.stdout.strip().split("\n")
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
            lines = result.stdout.strip().split("\n")
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
            dd._read = MagicMock(return_value={"data": []})

            result = dd.list_catalog_entities()

            assert result == {"data": []}
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value={"data": []})

            result = dd.list_teams()

            assert result == {"data": []}
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value={"data": []})

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
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value={"data": []})

            result = dd.list_team_memberships(
                "team-123",
                keyword="Jane User",
                page_number=1,
                page_size=25,
                sort="email",
            )

            assert result == {"data": []}
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value={"data": []})

            result = dd.list_team_notification_rules("team-123")

            assert result == {"data": []}
            dd._read.assert_called_once_with(
                "GET",
                "/api/v2/team/team-123/notification-rules",
            )
        finally:
            dd.close()

    def test_list_catalog_entities_builds_query_params(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._read = MagicMock(return_value={"data": []})

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
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value=expected_response)

            result = dd.get_pagerduty_integration_service("datadog-routing-hub")

            assert result == expected_response
            dd._read.assert_called_once_with(
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
            dd._read = MagicMock(return_value=expected_response)

            result = dd.get_pagerduty_integration_service("datadog routing hub")

            assert result == expected_response
            dd._read.assert_called_once_with(
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


class TestUpdateDashboard:
    """Tests for update-dashboard command."""

    def _spec_file(self, tmp_path, body):
        spec = tmp_path / "dashboard.json"
        spec.write_text(json.dumps(body), encoding="utf-8")
        return str(spec)

    def test_update_dashboard_from_spec(self, runner, mock_env, tmp_path):
        """Verify --spec body is sent as-is (PUT) and id/url/title printed."""
        spec_body = {
            "title": "My dashboard",
            "layout_type": "ordered",
            "widgets": [{"definition": {"type": "note", "content": "hi"}}],
        }
        spec_path = self._spec_file(tmp_path, spec_body)
        update_response = {
            "id": "abc-def-ghi",
            "title": "My dashboard",
            "url": "/dashboard/abc-def-ghi/my-dashboard",
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.update_dashboard.return_value = update_response
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["update-dashboard", "abc-def-ghi", "--spec", spec_path]
            )

            assert result.exit_code == 0, result.output
            mock_client.update_dashboard.assert_called_once_with(
                "abc-def-ghi", body=spec_body
            )
            output = json.loads(result.output)
            assert output["id"] == "abc-def-ghi"
            assert output["url"] == (
                "https://us3.datadoghq.com/dashboard/abc-def-ghi/my-dashboard"
            )
            assert output["title"] == "My dashboard"

    def test_update_dashboard_from_url(self, runner, mock_env, tmp_path):
        """Verify update-dashboard extracts the ID from a full Datadog URL."""
        spec_path = self._spec_file(
            tmp_path, {"title": "t", "layout_type": "ordered", "widgets": []}
        )
        url = "https://us3.datadoghq.com/dashboard/abc-def-ghi/my-dashboard-slug"
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.update_dashboard.return_value = {"id": "abc-def-ghi"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["update-dashboard", url, "--spec", spec_path])

            assert result.exit_code == 0, result.output
            # ID parsed from URL, body passed through as-is.
            mock_client.update_dashboard.assert_called_once_with(
                "abc-def-ghi",
                body={"title": "t", "layout_type": "ordered", "widgets": []},
            )

    def test_update_dashboard_flags_override_spec(self, runner, mock_env, tmp_path):
        """Verify --title/--description/--layout-type/--tag override spec keys."""
        spec_path = self._spec_file(
            tmp_path,
            {
                "title": "old",
                "widgets": [{"definition": {"type": "note", "content": "keep"}}],
                "layout_type": "free",
            },
        )
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.update_dashboard.return_value = {"id": "xyz"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "update-dashboard",
                    "xyz",
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
            # Widgets from the spec are preserved (never wiped by a default).
            mock_client.update_dashboard.assert_called_once_with(
                "xyz",
                body={
                    "title": "new title",
                    "widgets": [{"definition": {"type": "note", "content": "keep"}}],
                    "layout_type": "ordered",
                    "description": "desc",
                    "tags": ["team:fbm", "managed-by:dd-cli"],
                },
            )

    def test_update_dashboard_does_not_wipe_widgets(self, runner, mock_env):
        """Verify no widgets default is injected (full-replace safety)."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.update_dashboard.return_value = {"id": "xyz"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["update-dashboard", "xyz", "--title", "Just a title"]
            )

            assert result.exit_code == 0, result.output
            # layout_type is defaulted, but widgets is NOT added.
            mock_client.update_dashboard.assert_called_once_with(
                "xyz",
                body={"title": "Just a title", "layout_type": "ordered"},
            )

    def test_update_dashboard_requires_title(self, runner, mock_env, tmp_path):
        """Verify a missing title (no flag, none in spec) is a usage error."""
        spec_path = self._spec_file(tmp_path, {"widgets": []})
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["update-dashboard", "xyz", "--spec", spec_path]
            )

            assert result.exit_code != 0
            assert "title is required" in result.output
            mock_client.update_dashboard.assert_not_called()

    def test_update_dashboard_url_fallback_from_id(self, runner, mock_env):
        """Verify the URL falls back to /dashboard/{id} when url is absent."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.update_dashboard.return_value = {"id": "no-url-id"}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["update-dashboard", "no-url-id", "--title", "t"]
            )

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
            dd._write = MagicMock(return_value={"id": "abc"})

            result = dd.create_dashboard(body=body)

            assert result == {"id": "abc"}
            dd._write.assert_called_once_with(
                "POST", "/api/v1/dashboard", json_body=body
            )
        finally:
            dd.close()

    def test_update_dashboard_puts_body(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            body = {
                "title": "t",
                "layout_type": "ordered",
                "widgets": [{"definition": {"type": "note", "content": "hi"}}],
            }
            dd._write = MagicMock(return_value={"id": "abc"})

            result = dd.update_dashboard("abc", body=body)

            assert result == {"id": "abc"}
            dd._write.assert_called_once_with(
                "PUT", "/api/v1/dashboard/abc", json_body=body
            )
        finally:
            dd.close()

    def test_get_dashboard(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._read = MagicMock(return_value={"id": "abc"})

            result = dd.get_dashboard("abc")

            assert result == {"id": "abc"}
            dd._read.assert_called_once_with("GET", "/api/v1/dashboard/abc")
        finally:
            dd.close()

    def test_list_dashboards_defaults(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", pat="ddpat_test")
        try:
            dd._read = MagicMock(return_value={"dashboards": []})

            result = dd.list_dashboards()

            assert result == {"dashboards": []}
            dd._read.assert_called_once_with("GET", "/api/v1/dashboard", params=None)
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
            dd._read = MagicMock(return_value={"data": {}})
            dd.validate()
            dd._read.assert_called_once_with("GET", "/api/v2/current_user")
        finally:
            dd.close()


class TestGetClientCredentialSelection:
    """Tests for _get_client reading DD_PAT."""

    def test_uses_pat_when_set(self):
        from dd_cli.cli import _get_client

        with (
            patch.dict(
                "os.environ",
                {"DD_PAT": "ddpat_x"},
                clear=True,
            ),
            patch("dd_cli.cli.DatadogClient") as mock_client_class,
        ):
            _get_client("us3.datadoghq.com")
            mock_client_class.assert_called_once_with(
                site="us3.datadoghq.com",
                pat="ddpat_x",
                timeout=15.0,
                max_retries=5,
                on_retry=ANY,
            )

    def test_errors_when_pat_missing(self):
        import click

        from dd_cli.cli import _get_client

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(click.UsageError):
                _get_client("us3.datadoghq.com")


class TestMonitorOptionOverrideParser:
    """Tests for the --option KEY=VALUE escape hatch parser."""

    def test_parses_json_scalars(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        parsed = _parse_monitor_option_overrides(
            (
                "no_data_timeframe=60",
                "notify_audit=true",
                "evaluation_delay=0",
                "escalation_message=still broken",
            )
        )
        assert parsed == {
            "no_data_timeframe": 60,
            "notify_audit": True,
            "evaluation_delay": 0,
            "escalation_message": "still broken",
        }

    def test_parses_json_objects_and_arrays(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        parsed = _parse_monitor_option_overrides(
            (
                'scheduling_options={"evaluation_window": {"day_starts": "04:00"}}',
                'renotify_statuses=["alert", "no data"]',
                "notify_by=null",
            )
        )
        assert parsed["scheduling_options"] == {
            "evaluation_window": {"day_starts": "04:00"}
        }
        assert parsed["renotify_statuses"] == ["alert", "no data"]
        assert parsed["notify_by"] is None

    def test_bare_string_value_is_kept_as_string(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        parsed = _parse_monitor_option_overrides(("on_missing_data=resolve",))
        assert parsed == {"on_missing_data": "resolve"}

    def test_value_may_contain_equals_sign(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        parsed = _parse_monitor_option_overrides(("escalation_message=a=b",))
        assert parsed == {"escalation_message": "a=b"}

    def test_missing_equals_is_a_usage_error(self):
        import click

        from dd_cli.cli import _parse_monitor_option_overrides

        with pytest.raises(click.UsageError) as exc:
            _parse_monitor_option_overrides(("no_data_timeframe",))
        assert "KEY=VALUE" in str(exc.value)

    def test_empty_key_is_a_usage_error(self):
        import click

        from dd_cli.cli import _parse_monitor_option_overrides

        with pytest.raises(click.UsageError):
            _parse_monitor_option_overrides(("=5",))

    def test_malformed_json_structure_is_a_usage_error(self):
        import click

        from dd_cli.cli import _parse_monitor_option_overrides

        with pytest.raises(click.UsageError) as exc:
            _parse_monitor_option_overrides(('thresholds={"critical": 1',))
        assert "JSON" in str(exc.value)

    def test_duplicate_key_last_wins(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        parsed = _parse_monitor_option_overrides(
            ("no_data_timeframe=30", "no_data_timeframe=60")
        )
        assert parsed == {"no_data_timeframe": 60}


class TestNoDataTimeframeGuard:
    """Tests for the notify_no_data / no_data_timeframe misconfiguration guard."""

    def test_notify_no_data_without_timeframe_raises(self):
        import click

        from dd_cli.cli import _validate_no_data_options

        with pytest.raises(click.UsageError) as exc:
            _validate_no_data_options({"notify_no_data": True})
        message = str(exc.value)
        assert "no_data_timeframe" in message
        assert "--no-data-timeframe" in message

    def test_notify_no_data_with_timeframe_is_fine(self):
        from dd_cli.cli import _validate_no_data_options

        _validate_no_data_options({"notify_no_data": True, "no_data_timeframe": 60})

    def test_notify_no_data_false_is_fine(self):
        from dd_cli.cli import _validate_no_data_options

        _validate_no_data_options({"notify_no_data": False})

    def test_on_missing_data_supersedes_the_guard(self):
        from dd_cli.cli import _validate_no_data_options

        _validate_no_data_options(
            {"notify_no_data": True, "on_missing_data": "show_and_notify_no_data"}
        )

    def test_guard_fires_via_create_monitor_cli(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            result = runner.invoke(
                cli,
                [
                    "create-monitor",
                    "--name",
                    "dead man switch",
                    "--type",
                    "trace-analytics alert",
                    "--query",
                    'trace-analytics("service:x").rollup("count").last("45m") < 1',
                    "--message",
                    "silent",
                    "--notify-no-data",
                ],
            )

            assert result.exit_code != 0
            assert "no_data_timeframe" in result.output
            mock_client_class.assert_not_called()


class TestCreateMonitorOptions:
    """Tests for create-monitor option flags."""

    @staticmethod
    def _mock_client(mock_client_class):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.create_monitor.return_value = {"id": 1}
        mock_client_class.return_value = mock_client
        return mock_client

    BASE_ARGS = [
        "create-monitor",
        "--name",
        "n",
        "--type",
        "query alert",
        "--query",
        "q",
        "--message",
        "m",
    ]

    def test_first_class_option_flags_are_sent(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + [
                    "--notify-no-data",
                    "--no-data-timeframe",
                    "60",
                    "--new-group-delay",
                    "300",
                    "--evaluation-delay",
                    "120",
                    "--notify-audit",
                    "--no-include-tags",
                    "--require-full-window",
                    "--timeout-h",
                    "4",
                    "--renotify-interval",
                    "60",
                    "--renotify-occurrences",
                    "3",
                    "--renotify-status",
                    "alert",
                    "--renotify-status",
                    "no data",
                    "--escalation-message",
                    "still down",
                    "--group-retention-duration",
                    "2d",
                    "--notification-preset-name",
                    "hide_query",
                    "--critical",
                    "1",
                    "--warning",
                    "2",
                    "--critical-recovery",
                    "0.5",
                ],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {
                "notify_no_data": True,
                "no_data_timeframe": 60,
                "new_group_delay": 300,
                "evaluation_delay": 120,
                "notify_audit": True,
                "include_tags": False,
                "require_full_window": True,
                "timeout_h": 4,
                "renotify_interval": 60,
                "renotify_occurrences": 3,
                "renotify_statuses": ["alert", "no data"],
                "escalation_message": "still down",
                "group_retention_duration": "2d",
                "notification_preset_name": "hide_query",
                "thresholds": {
                    "critical": 1.0,
                    "warning": 2.0,
                    "critical_recovery": 0.5,
                },
            }

    def test_unset_flags_are_omitted(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(cli, self.BASE_ARGS + ["--critical", "3"])

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {"thresholds": {"critical": 3.0}}

    def test_on_missing_data_flag(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(
                cli, self.BASE_ARGS + ["--on-missing-data", "show_and_notify_no_data"]
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {"on_missing_data": "show_and_notify_no_data"}

    def test_option_escape_hatch_merges(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + [
                    "--option",
                    "min_location_failed=2",
                    "--option",
                    'notify_by=["env"]',
                ],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {"min_location_failed": 2, "notify_by": ["env"]}

    def test_first_class_flag_beats_option_escape_hatch(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + [
                    "--notify-no-data",
                    "--no-data-timeframe",
                    "60",
                    "--option",
                    "no_data_timeframe=5",
                ],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options["no_data_timeframe"] == 60

    def test_option_escape_hatch_satisfies_the_guard(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class)

            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + ["--notify-no-data", "--option", "no_data_timeframe=45"],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {"notify_no_data": True, "no_data_timeframe": 45}


class TestUpdateMonitorOptions:
    """Tests for update-monitor parity + merge-not-clobber semantics."""

    EXISTING = {
        "id": 24864134,
        "name": "dead man switch",
        "options": {
            "notify_no_data": True,
            "no_data_timeframe": 60,
            "renotify_interval": 60,
            "notify_audit": False,
            "include_tags": True,
            "new_group_delay": 300,
            "thresholds": {"critical": 1.0, "warning": 2.0},
            "silenced": {},
        },
    }

    @staticmethod
    def _mock_client(mock_client_class, existing):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get_monitor.return_value = existing
        mock_client.update_monitor.return_value = {"id": existing["id"]}
        mock_client_class.return_value = mock_client
        return mock_client

    def test_existing_options_survive_a_partial_update(self, runner, mock_env):
        """A PUT with a partial options dict resets unspecified options DD-side,
        so update-monitor must read-modify-write the full options object."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(
                cli, ["update-monitor", "24864134", "--renotify-interval", "30"]
            )

            assert result.exit_code == 0, result.output
            mock_client.get_monitor.assert_called_once_with("24864134")
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["options"] == {
                "notify_no_data": True,
                "no_data_timeframe": 60,
                "renotify_interval": 30,
                "notify_audit": False,
                "include_tags": True,
                "new_group_delay": 300,
                "thresholds": {"critical": 1.0, "warning": 2.0},
                "silenced": {},
            }

    def test_threshold_update_preserves_sibling_thresholds(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(
                cli, ["update-monitor", "24864134", "--critical", "5"]
            )

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["options"]["thresholds"] == {
                "critical": 5.0,
                "warning": 2.0,
            }

    def test_updates_tags_and_priority(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(
                cli,
                [
                    "update-monitor",
                    "24864134",
                    "--tag",
                    "managed-by:dd-cli",
                    "--tag",
                    "team:platform",
                    "--priority",
                    "2",
                ],
            )

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["tags"] == ["managed-by:dd-cli", "team:platform"]
            assert payload["priority"] == 2

    def test_sets_no_data_options(self, runner, mock_env):
        existing = {"id": 5, "options": {"include_tags": True}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, existing)

            result = runner.invoke(
                cli,
                [
                    "update-monitor",
                    "5",
                    "--notify-no-data",
                    "--no-data-timeframe",
                    "60",
                ],
            )

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["options"] == {
                "include_tags": True,
                "notify_no_data": True,
                "no_data_timeframe": 60,
            }

    def test_guard_uses_merged_options(self, runner, mock_env):
        """Turning notify_no_data on for a monitor with no existing
        no_data_timeframe must be refused."""
        existing = {"id": 5, "options": {"include_tags": True}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, existing)

            result = runner.invoke(cli, ["update-monitor", "5", "--notify-no-data"])

            assert result.exit_code != 0
            assert "no_data_timeframe" in result.output
            mock_client.update_monitor.assert_not_called()

    def test_guard_passes_when_existing_options_supply_the_timeframe(
        self, runner, mock_env
    ):
        existing = {"id": 5, "options": {"no_data_timeframe": 30}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, existing)

            result = runner.invoke(cli, ["update-monitor", "5", "--notify-no-data"])

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["options"] == {
                "no_data_timeframe": 30,
                "notify_no_data": True,
            }

    def test_option_escape_hatch_on_update(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(
                cli,
                [
                    "update-monitor",
                    "24864134",
                    "--option",
                    "evaluation_delay=120",
                    "--option",
                    "new_group_delay=600",
                    "--new-group-delay",
                    "900",
                ],
            )

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload["options"]["evaluation_delay"] == 120
            # First-class flag wins over --option.
            assert payload["options"]["new_group_delay"] == 900

    def test_no_updates_is_a_usage_error(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(cli, ["update-monitor", "24864134"])

            assert result.exit_code != 0
            mock_client.update_monitor.assert_not_called()

    def test_options_untouched_when_only_name_changes(self, runner, mock_env):
        """No option flags -> do not send an options dict at all (and do not
        need to read the monitor first)."""
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = self._mock_client(mock_client_class, self.EXISTING)

            result = runner.invoke(
                cli, ["update-monitor", "24864134", "--name", "new name"]
            )

            assert result.exit_code == 0, result.output
            payload = mock_client.update_monitor.call_args.kwargs["payload"]
            assert payload == {"name": "new name"}
            mock_client.get_monitor.assert_not_called()


class TestNoDataFamilyExclusivity:
    """Datadog rejects on_missing_data combined with the legacy
    notify_no_data / no_data_timeframe options (verified against
    POST /api/v1/monitor/validate):

        "The notify_no_data option is deprecated and cannot be used in
         combination with the on_missing_data option."
        "The no_data_timeframe option is deprecated and cannot be used in
         combination with the on_missing_data option"

    notify_no_data=false alongside on_missing_data IS accepted.
    """

    BASE_ARGS = TestCreateMonitorOptions.BASE_ARGS

    def test_create_refuses_both_families(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + ["--notify-no-data", "--no-data-timeframe", "60"]
                + ["--on-missing-data", "show_and_notify_no_data"],
            )

            assert result.exit_code != 0
            assert "on_missing_data" in result.output
            mock_client_class.assert_not_called()

    def test_create_refuses_timeframe_with_on_missing_data(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + ["--no-data-timeframe", "60", "--on-missing-data", "resolve"],
            )

            assert result.exit_code != 0
            mock_client_class.assert_not_called()

    def test_create_allows_notify_no_data_false_with_on_missing_data(
        self, runner, mock_env
    ):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestCreateMonitorOptions._mock_client(mock_client_class)

            result = runner.invoke(
                cli,
                self.BASE_ARGS
                + ["--no-notify-no-data", "--on-missing-data", "resolve"],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.create_monitor.call_args.kwargs["options"]
            assert options == {"notify_no_data": False, "on_missing_data": "resolve"}

    def test_update_to_legacy_family_drops_on_missing_data(self, runner, mock_env):
        existing = {
            "id": 5,
            "options": {"on_missing_data": "show_no_data", "include_tags": True},
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, existing
            )

            result = runner.invoke(
                cli,
                [
                    "update-monitor",
                    "5",
                    "--notify-no-data",
                    "--no-data-timeframe",
                    "60",
                ],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.update_monitor.call_args.kwargs["payload"]["options"]
            assert "on_missing_data" not in options
            assert options == {
                "include_tags": True,
                "notify_no_data": True,
                "no_data_timeframe": 60,
            }

    def test_update_to_on_missing_data_drops_legacy_keys(self, runner, mock_env):
        existing = {
            "id": 5,
            "options": {
                "notify_no_data": True,
                "no_data_timeframe": 60,
                "include_tags": True,
            },
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, existing
            )

            result = runner.invoke(
                cli,
                ["update-monitor", "5", "--on-missing-data", "show_and_notify_no_data"],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.update_monitor.call_args.kwargs["payload"]["options"]
            assert options == {
                "include_tags": True,
                "on_missing_data": "show_and_notify_no_data",
            }


class TestNoDataGuardScope:
    """The guard must not block updates that do not touch no-data options."""

    def test_unrelated_update_warns_but_succeeds(self, runner, mock_env):
        existing = {"id": 5, "options": {"notify_no_data": True, "include_tags": True}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, existing
            )

            result = runner.invoke(
                cli, ["update-monitor", "5", "--renotify-interval", "30"]
            )

            assert result.exit_code == 0, result.output
            assert "no_data_timeframe" in result.output
            options = mock_client.update_monitor.call_args.kwargs["payload"]["options"]
            # Pre-existing (broken) state is preserved, not silently "fixed".
            assert options["notify_no_data"] is True
            assert "no_data_timeframe" not in options

    def test_touching_the_family_still_refuses(self, runner, mock_env):
        existing = {"id": 5, "options": {"notify_no_data": True}}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, existing
            )

            result = runner.invoke(cli, ["update-monitor", "5", "--notify-no-data"])

            assert result.exit_code != 0
            mock_client.update_monitor.assert_not_called()


class TestOptionParserHardening:
    """Values that look like Python literals or non-finite numbers are typos,
    not data: they must fail loudly rather than reach Datadog."""

    @pytest.mark.parametrize(
        "pair",
        [
            "notify_no_data=True",
            "notify_no_data=False",
            "notify_by=None",
            "no_data_timeframe=NaN",
            "no_data_timeframe=Infinity",
            "no_data_timeframe=-Infinity",
        ],
    )
    def test_pythonish_and_nonfinite_values_rejected(self, pair):
        import click

        from dd_cli.cli import _parse_monitor_option_overrides

        with pytest.raises(click.UsageError):
            _parse_monitor_option_overrides((pair,))

    def test_lowercase_json_literals_still_work(self):
        from dd_cli.cli import _parse_monitor_option_overrides

        assert _parse_monitor_option_overrides(
            ("notify_audit=true", "notify_by=null", "include_tags=false")
        ) == {"notify_audit": True, "notify_by": None, "include_tags": False}

    def test_pythonish_typo_cannot_bypass_the_no_data_guard(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            result = runner.invoke(
                cli,
                TestCreateMonitorOptions.BASE_ARGS
                + ["--option", "notify_no_data=True"],
            )

            assert result.exit_code != 0
            mock_client_class.assert_not_called()


class TestThresholdsReplaceSemantics:
    """--critical/--warning patch individual thresholds; an explicit
    --option thresholds={...} replaces the whole object (the only way to
    remove a threshold)."""

    EXISTING = {
        "id": 5,
        "options": {"thresholds": {"critical": 1.0, "warning": 2.0}},
    }

    def test_option_thresholds_replaces_wholesale(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, self.EXISTING
            )

            result = runner.invoke(
                cli,
                ["update-monitor", "5", "--option", 'thresholds={"critical": 5}'],
            )

            assert result.exit_code == 0, result.output
            options = mock_client.update_monitor.call_args.kwargs["payload"]["options"]
            assert options["thresholds"] == {"critical": 5}

    def test_threshold_flags_still_patch(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = TestUpdateMonitorOptions._mock_client(
                mock_client_class, self.EXISTING
            )

            result = runner.invoke(cli, ["update-monitor", "5", "--critical", "9"])

            assert result.exit_code == 0, result.output
            options = mock_client.update_monitor.call_args.kwargs["payload"]["options"]
            assert options["thresholds"] == {"critical": 9.0, "warning": 2.0}


class TestMonitorOptionFlagCoverage:
    """Every flag the shared decorator adds must actually be turned into an
    option -- a flag added to the decorator but not to the mapping tables
    would be silently ignored."""

    def test_every_decorated_flag_is_handled(self):
        import click

        from dd_cli.cli import (
            _MONITOR_SIMPLE_OPTION_FLAGS,
            _MONITOR_THRESHOLD_FLAGS,
            monitor_option_flags,
        )

        @click.command()
        @monitor_option_flags
        def probe(**kwargs):  # pragma: no cover - never invoked
            pass

        dests = {p.name for p in probe.params}
        handled = (
            set(_MONITOR_SIMPLE_OPTION_FLAGS)
            | set(_MONITOR_THRESHOLD_FLAGS)
            | {"renotify_status", "option"}
        )
        assert dests == handled
