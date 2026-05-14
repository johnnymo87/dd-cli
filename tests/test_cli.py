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
            "DD_API_KEY": "a" * 32,
            "DD_APP_KEY": "b" * 40,
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
