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
