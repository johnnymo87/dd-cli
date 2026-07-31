"""Tests for count-logs, the in-process replacement for a shell bucketing loop.

Incident 1 happened because an agent looped over hours in bash, invoking dd-cli
once per hour. Every iteration was an independent chance to convert a failure
into a zero. Bucketing in-process means a 429 is a retry and an exhausted retry
is an exception -- a bucket is never 0 unless it is really 0.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dd_cli.cli import cli
from dd_cli.http import DatadogAPIError


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


class TestBucketing:
    def test_buckets_cover_the_range_and_total_reconciles(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.return_value = 7
            cls.return_value = client

            result = runner.invoke(
                cli, ["count-logs", "service:x", "--from", "now-4h", "--bucket", "1h"]
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert len(payload["buckets"]) == 4
        assert payload["total"] == 28
        assert sum(b["count"] for b in payload["buckets"]) == payload["total"]
        assert all(b["complete"] for b in payload["buckets"])

    def test_buckets_are_contiguous_and_ordered(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.return_value = 1
            cls.return_value = client

            result = runner.invoke(
                cli, ["count-logs", "service:x", "--from", "now-3h", "--bucket", "1h"]
            )

        buckets = json.loads(result.stdout)["buckets"]
        for earlier, later in zip(buckets, buckets[1:], strict=False):
            assert earlier["to"] == later["from"]

    def test_without_bucket_returns_a_single_total(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.return_value = 42
            cls.return_value = client

            result = runner.invoke(cli, ["count-logs", "service:x", "--from", "now-1h"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["total"] == 42
        assert payload["buckets"] is None


class TestFailureIsNeverAZero:
    def test_a_failing_bucket_fails_the_command(self, runner, mock_env):
        """The whole point: one bad bucket must not silently read as 0."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.side_effect = [
                5,
                DatadogAPIError(429, "Too many requests", "{}", attempts=5),
                5,
                5,
            ]
            cls.return_value = client

            result = runner.invoke(
                cli, ["count-logs", "service:x", "--from", "now-4h", "--bucket", "1h"]
            )

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["total"] is None
        # No bucket may claim a count of 0 from a failed request.
        assert payload.get("buckets") is None
        assert payload["error"]["status"] == 429

    def test_partial_mode_marks_the_failed_bucket_not_zero(self, runner, mock_env):
        """--allow-partial keeps going, but a failed bucket is null, never 0."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.side_effect = [
                5,
                DatadogAPIError(429, "Too many requests", "{}"),
                5,
                5,
            ]
            cls.return_value = client

            result = runner.invoke(
                cli,
                [
                    "count-logs",
                    "service:x",
                    "--from",
                    "now-4h",
                    "--bucket",
                    "1h",
                    "--allow-partial",
                ],
            )

        assert result.exit_code == 3
        payload = json.loads(result.stdout)
        assert payload["truncated"] is True
        buckets = payload["buckets"]
        failed = [b for b in buckets if not b["complete"]]
        assert len(failed) == 1
        assert failed[0]["count"] is None
        assert failed[0]["error"]["status"] == 429
        # A total that silently omits a failed bucket would understate reality.
        assert payload["total"] is None
        assert payload["partial_total"] == 15

    def test_zero_is_still_reportable_when_it_is_real(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.count_logs.return_value = 0
            cls.return_value = client

            result = runner.invoke(
                cli, ["count-logs", "service:x", "--from", "now-2h", "--bucket", "1h"]
            )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["total"] == 0
        assert all(b["count"] == 0 and b["complete"] for b in payload["buckets"])


class TestBucketValidation:
    def test_rejects_bucket_larger_than_range(self, runner, mock_env):
        result = runner.invoke(
            cli, ["count-logs", "service:x", "--from", "now-1h", "--bucket", "1d"]
        )
        assert result.exit_code != 0
        assert "bucket" in result.output.lower()

    def test_rejects_absurd_bucket_count(self, runner, mock_env):
        result = runner.invoke(
            cli, ["count-logs", "service:x", "--from", "now-7d", "--bucket", "1s"]
        )
        assert result.exit_code != 0
        assert "too many buckets" in result.output.lower()
