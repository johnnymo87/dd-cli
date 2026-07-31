"""Tests for the 'errors must never be representable as data' guarantees.

Covers the stdout failure envelope, truncation signalling, and the 200-partial
(server-side timeout) case.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dd_cli.cli import cli
from dd_cli.http import DatadogAPIError

EXIT_TRUNCATED = 3


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_env():
    with patch.dict(
        "os.environ",
        {"DD_SITE": "us3.datadoghq.com", "DD_PAT": "ddpat_test"},
    ):
        yield


def fake_client():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


def log_page(n, cursor=None, meta_extra=None):
    meta = {"page": {"after": cursor}} if cursor else {"page": {}}
    if meta_extra:
        meta.update(meta_extra)
    return {
        "data": [{"attributes": {"message": f"msg{i}"}} for i in range(n)],
        "meta": meta,
    }


class TestFailureEnvelope:
    """A failure must not be zero bytes on stdout.

    Empty stdout is the zero factory: `n=$(dd-cli ... | jq '.count')` gives an
    empty string, and `${n:-0}` turns that into 0.
    """

    def test_search_logs_failure_emits_parseable_envelope_on_stdout(
        self, runner, mock_env
    ):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.side_effect = DatadogAPIError(
                429, "Too many requests", "{}", attempts=5, elapsed_s=41.2
            )
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["schema_version"] == 2
        assert payload["count"] is None
        assert payload["data"] is None
        assert payload["error"]["status"] == 429
        assert payload["error"]["attempts"] == 5

    def test_failure_envelope_never_reports_a_zero_count(self, runner, mock_env):
        """count must be null, never 0 -- 0 is a claim about the world."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.side_effect = DatadogAPIError(429, "rate limited", "{}")
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        payload = json.loads(result.stdout)
        assert payload["count"] is None

    def test_list_monitors_failure_emits_envelope(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.list_monitors.side_effect = DatadogAPIError(
                429, "rate limited", "{}"
            )
            cls.return_value = client

            result = runner.invoke(cli, ["list-monitors"])

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["count"] is None


class TestSuccessEnvelope:
    def test_success_marks_ok_and_not_truncated(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(3)
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["schema_version"] == 2
        assert payload["count"] == 3
        assert payload["truncated"] is False
        assert payload["truncation_reason"] is None

    def test_legitimate_zero_is_still_expressible(self, runner, mock_env):
        """A real empty result must remain distinguishable from a failure."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(0)
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["count"] == 0
        assert payload["truncated"] is False


class TestCursorTruncation:
    def test_page_cap_with_cursor_outstanding_is_loud(self, runner, mock_env):
        """Stopping at the page cap with a live cursor is a short answer."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(2, cursor="more")
            cls.return_value = client

            result = runner.invoke(
                cli, ["search-logs", "foo", "--all-pages", "--max-pages", "3"]
            )

        assert result.exit_code == EXIT_TRUNCATED
        payload = json.loads(result.stdout)
        assert payload["truncated"] is True
        assert payload["truncation_reason"] == "max_pages"
        assert "truncat" in result.stderr.lower()

    def test_max_results_with_cursor_outstanding_reports_more_available(
        self, runner, mock_env
    ):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(10, cursor="more")
            cls.return_value = client

            result = runner.invoke(
                cli, ["search-logs", "foo", "--all-pages", "--max-results", "5"]
            )

        assert result.exit_code == EXIT_TRUNCATED
        payload = json.loads(result.stdout)
        assert payload["count"] == 5
        assert payload["truncated"] is True
        assert payload["truncation_reason"] == "more_available"

    def test_exhausted_cursor_is_not_truncated(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(2, cursor=None)
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo", "--all-pages"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)["truncated"] is False


class TestOnTruncationFlag:
    def test_warn_downgrades_exit_code_but_keeps_the_flag(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(10, cursor="more")
            cls.return_value = client

            result = runner.invoke(
                cli,
                [
                    "search-logs",
                    "foo",
                    "--all-pages",
                    "--max-results",
                    "5",
                    "--on-truncation",
                    "warn",
                ],
            )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["truncated"] is True
        assert "truncat" in result.stderr.lower()

    def test_error_mode_fails_hard(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(10, cursor="more")
            cls.return_value = client

            result = runner.invoke(
                cli,
                [
                    "search-logs",
                    "foo",
                    "--all-pages",
                    "--max-results",
                    "5",
                    "--on-truncation",
                    "error",
                ],
            )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["truncated"] is True


class TestServerSidePartial:
    """HTTP 200 with a short body -- immune to retries and to cap detection."""

    def test_meta_status_timeout_marks_truncated(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(
                4, meta_extra={"status": "timeout"}
            )
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code == EXIT_TRUNCATED
        payload = json.loads(result.stdout)
        assert payload["truncated"] is True
        assert payload["truncation_reason"] == "server_timeout"

    def test_meta_warnings_are_surfaced(self, runner, mock_env):
        warning = {"code": "flex_timeout", "detail": "query exceeded budget"}
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(
                4, meta_extra={"warnings": [warning]}
            )
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code == EXIT_TRUNCATED
        payload = json.loads(result.stdout)
        assert payload["truncated"] is True
        assert payload["warnings"] == [warning]

    def test_meta_status_done_is_clean(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(4, meta_extra={"status": "done"})
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)["truncated"] is False


class TestNonListDataIsNotDropped:
    def test_non_list_data_raises_rather_than_counting_zero(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = {"data": {"unexpected": "shape"}}
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo"])

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["count"] is None


class TestListMonitorsTruncation:
    def test_cap_truncation_reports_boundary_unknown(self, runner, mock_env):
        """Page/offset paging cannot tell 'exactly full' from 'more exists'."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.list_monitors.return_value = [{"id": i} for i in range(1000)]
            cls.return_value = client

            result = runner.invoke(
                cli, ["list-monitors", "--max-results", "10", "--format", "json"]
            )

        assert result.exit_code == EXIT_TRUNCATED
        payload = json.loads(result.stdout)
        assert payload["count"] == 10
        assert payload["truncated"] is True
        assert payload["truncation_reason"] == "max_results_boundary_unknown"

    def test_short_page_is_complete(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.list_monitors.return_value = [{"id": 1}]
            cls.return_value = client

            result = runner.invoke(cli, ["list-monitors", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["truncated"] is False
        assert payload["count"] == 1


class TestTextFormatsSignalOutOfBand:
    def test_jsonl_truncation_warns_on_stderr_and_exits_3(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = log_page(10, cursor="more")
            cls.return_value = client

            result = runner.invoke(
                cli,
                [
                    "search-logs",
                    "foo",
                    "--all-pages",
                    "--max-results",
                    "5",
                    "--format",
                    "jsonl",
                ],
            )

        assert result.exit_code == EXIT_TRUNCATED
        assert "truncat" in result.stderr.lower()
        # The stream itself stays clean: 5 records, no sentinel object.
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 5
        for line in lines:
            assert "truncated" not in json.loads(line)

    def test_messages_format_preserves_record_count(self, runner, mock_env):
        """An empty message must not silently vanish from the line count."""
        page = {
            "data": [
                {"attributes": {"message": "one"}},
                {"attributes": {"message": ""}},
                {"attributes": {"message": "three"}},
            ],
            "meta": {"page": {}},
        }
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_logs.return_value = page
            cls.return_value = client

            result = runner.invoke(cli, ["search-logs", "foo", "--format", "messages"])

        assert result.exit_code == 0
        assert len(result.stdout.splitlines()) == 3
        assert "count=3" in result.stderr
