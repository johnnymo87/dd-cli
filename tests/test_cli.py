"""Tests for dd_cli."""

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

            import json

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

            import json

            output = json.loads(result.output)
            assert output["count"] == 120
