"""Tests for log-based metrics: count, distribution, and '@'-prefix validation.

The '@' rule is the reason most of this file exists. Datadog accepts a compute
or group_by path that names a custom log attribute *without* the leading '@',
returns 200 OK, creates the metric -- and the metric then silently produces no
data (or, for group_by, collapses every value into one 'N/A' bucket). There is
no error anywhere, so the only place that failure can be caught is here,
client-side, before the request is sent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from dd_cli.cli import cli
from dd_cli.http import DatadogClient, LogMetricPathError


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


def capture_client() -> tuple[DatadogClient, list[httpx.Request]]:
    """A client whose transport records requests and always returns 200."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"id": "x"}})

    client = DatadogClient(
        site="us3.datadoghq.com",
        pat="ddpat_test",
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)["data"]["attributes"]


def cli_client_mock():
    """Standard cli.DatadogClient patch shape used across this repo's tests."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.create_log_metric.return_value = {"data": {"id": "ok"}}
    return mock_client


class TestCreateLogMetricPayload:
    """http-layer: the request body Datadog actually receives."""

    def test_count_metric_sends_no_path(self):
        dd, seen = capture_client()

        dd.create_log_metric(metric_id="a.b", query="service:x status:error")

        attrs = body_of(seen[0])
        assert attrs["compute"] == {"aggregation_type": "count"}
        assert "path" not in attrs["compute"]
        assert "include_percentiles" not in attrs["compute"]
        assert attrs["filter"] == {"query": "service:x status:error"}

    def test_distribution_metric_sends_path(self):
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            aggregation_type="distribution",
            path="@fbm.attention_open",
        )

        assert body_of(seen[0])["compute"] == {
            "aggregation_type": "distribution",
            "path": "@fbm.attention_open",
        }

    def test_distribution_with_include_percentiles(self):
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            aggregation_type="distribution",
            path="@duration",
            include_percentiles=True,
        )

        assert body_of(seen[0])["compute"] == {
            "aggregation_type": "distribution",
            "path": "@duration",
            "include_percentiles": True,
        }

    def test_include_percentiles_false_is_sent(self):
        """False is a real choice, not 'unset' -- it must reach the API."""
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            aggregation_type="distribution",
            path="@duration",
            include_percentiles=False,
        )

        assert body_of(seen[0])["compute"]["include_percentiles"] is False

    def test_group_by_passed_through(self):
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            group_by=[
                {"path": "service", "tag_name": "service"},
                {"path": "@topic", "tag_name": "topic"},
            ],
        )

        assert body_of(seen[0])["group_by"] == [
            {"path": "service", "tag_name": "service"},
            {"path": "@topic", "tag_name": "topic"},
        ]


class TestCreateLogMetricValidation:
    """Nothing invalid may reach the network."""

    def test_distribution_without_path_raises_before_http(self):
        dd, seen = capture_client()

        with pytest.raises(ValueError, match="requires a compute path"):
            dd.create_log_metric(
                metric_id="a.b", query="service:x", aggregation_type="distribution"
            )

        assert seen == []

    def test_count_with_path_raises(self):
        dd, seen = capture_client()

        with pytest.raises(ValueError, match="only valid"):
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                aggregation_type="count",
                path="@duration",
            )

        assert seen == []

    def test_count_with_include_percentiles_raises(self):
        dd, seen = capture_client()

        with pytest.raises(ValueError, match="include_percentiles"):
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                include_percentiles=True,
            )

        assert seen == []

    def test_unknown_aggregation_type_raises(self):
        dd, seen = capture_client()

        with pytest.raises(ValueError, match="aggregation_type"):
            dd.create_log_metric(
                metric_id="a.b", query="service:x", aggregation_type="gauge"
            )

        assert seen == []

    def test_bare_custom_compute_path_raises_before_http(self):
        dd, seen = capture_client()

        with pytest.raises(LogMetricPathError) as exc:
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                aggregation_type="distribution",
                path="fbm.attention_open",
            )

        assert seen == []
        msg = str(exc.value)
        # The message must teach the consequence, not just the rule.
        assert "@fbm.attention_open" in msg
        assert "200" in msg
        assert "no data" in msg.lower() or "no points" in msg.lower()

    def test_bare_custom_group_by_path_raises_before_http(self):
        dd, seen = capture_client()

        with pytest.raises(LogMetricPathError) as exc:
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                group_by=[
                    {"path": "fbm.tenant", "tag_name": "fbm.tenant"},
                ],
            )

        assert seen == []
        assert "@fbm.tenant" in str(exc.value)

    @pytest.mark.parametrize("reserved", ["service", "env", "host", "status", "source"])
    def test_reserved_group_by_paths_are_allowed_bare(self, reserved):
        """Today's working callers pass these bare. They must keep working."""
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            group_by=[{"path": reserved, "tag_name": reserved}],
        )

        assert len(seen) == 1
        assert body_of(seen[0])["group_by"][0]["path"] == reserved

    def test_allow_bare_paths_escape_hatch(self):
        """Infrastructure tag keys are legitimately bare -- but opt-in."""
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            group_by=[{"path": "kube_namespace", "tag_name": "kube_namespace"}],
            allow_bare_paths=True,
        )

        assert len(seen) == 1

    @pytest.mark.parametrize("reserved", ["service", "status", "env"])
    def test_reserved_path_rejected_as_distribution_compute_path(self, reserved):
        """Reserved attributes are strings; a distribution needs a number.

        They are legitimate bare for group_by, but as a compute path they are
        the same silent-empty-metric failure wearing a different hat.
        """
        dd, seen = capture_client()

        with pytest.raises(LogMetricPathError, match="numeric"):
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                aggregation_type="distribution",
                path=reserved,
            )

        assert seen == []

    def test_reserved_compute_path_allowed_with_escape_hatch(self):
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            aggregation_type="distribution",
            path="status",
            allow_bare_paths=True,
        )

        assert len(seen) == 1

    def test_lone_at_sign_is_not_a_compute_path(self):
        """'@' alone names no attribute."""
        dd, seen = capture_client()

        with pytest.raises(LogMetricPathError):
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                aggregation_type="distribution",
                path="@",
            )

        assert seen == []

    def test_lone_at_sign_is_not_a_group_by_path(self):
        """'@' alone would also send an empty tag_name."""
        dd, seen = capture_client()

        with pytest.raises(LogMetricPathError):
            dd.create_log_metric(
                metric_id="a.b",
                query="service:x",
                group_by=[{"path": "@", "tag_name": ""}],
            )

        assert seen == []

    def test_at_prefixed_paths_always_pass(self):
        dd, seen = capture_client()

        dd.create_log_metric(
            metric_id="a.b",
            query="service:x",
            aggregation_type="distribution",
            path="@anything.at.all",
            group_by=[{"path": "@tenant", "tag_name": "tenant"}],
        )

        assert len(seen) == 1


class TestCreateLogMetricCli:
    """CLI wiring: flags map to the client call, errors exit non-zero."""

    def test_default_is_count(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--group-by",
                    "service",
                    "--group-by",
                    "env",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.create_log_metric.assert_called_once_with(
                metric_id="a.b",
                query="service:x",
                group_by=[
                    {"path": "service", "tag_name": "service"},
                    {"path": "env", "tag_name": "env"},
                ],
                aggregation_type="count",
                path=None,
                include_percentiles=None,
                allow_bare_paths=False,
            )

    def test_distribution_flags(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--aggregation-type",
                    "distribution",
                    "--path",
                    "@fbm.attention_open",
                    "--include-percentiles",
                ],
            )

            assert result.exit_code == 0, result.output
            kwargs = mock_client.create_log_metric.call_args.kwargs
            assert kwargs["aggregation_type"] == "distribution"
            assert kwargs["path"] == "@fbm.attention_open"
            assert kwargs["include_percentiles"] is True

    def test_no_include_percentiles_flag(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--aggregation-type",
                    "distribution",
                    "--path",
                    "@d",
                    "--no-include-percentiles",
                ],
            )

            assert result.exit_code == 0, result.output
            assert (
                mock_client.create_log_metric.call_args.kwargs["include_percentiles"]
                is False
            )

    def test_distribution_without_path_fails_without_calling_api(
        self, runner, mock_env
    ):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--aggregation-type",
                    "distribution",
                ],
            )

            assert result.exit_code != 0
            assert "--path" in result.output
            mock_client.create_log_metric.assert_not_called()

    def test_count_with_path_names_the_flag_not_the_kwarg(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                ["create-log-metric", "a.b", "--query", "q", "--path", "@duration"],
            )

            assert result.exit_code != 0
            assert "--path" in result.output
            assert "--aggregation-type" in result.output
            assert "compute path" not in result.output
            mock_client.create_log_metric.assert_not_called()

    def test_count_with_include_percentiles_fails(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                ["create-log-metric", "a.b", "--query", "q", "--include-percentiles"],
            )

            assert result.exit_code != 0
            assert "--include-percentiles" in result.output
            mock_client.create_log_metric.assert_not_called()

    def test_bare_custom_path_fails_with_actionable_message(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--aggregation-type",
                    "distribution",
                    "--path",
                    "fbm.attention_open",
                ],
            )

            assert result.exit_code != 0
            assert "@fbm.attention_open" in result.output
            assert "--allow-bare-path" in result.output
            mock_client.create_log_metric.assert_not_called()

    def test_bare_custom_group_by_fails(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--group-by",
                    "fbm.tenant",
                ],
            )

            assert result.exit_code != 0
            assert "@fbm.tenant" in result.output
            mock_client.create_log_metric.assert_not_called()

    def test_allow_bare_path_flag_passes_through(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--group-by",
                    "kube_namespace",
                    "--allow-bare-path",
                ],
            )

            assert result.exit_code == 0, result.output
            assert (
                mock_client.create_log_metric.call_args.kwargs["allow_bare_paths"]
                is True
            )

    def test_tag_name_strips_at_prefix(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as klass:
            mock_client = cli_client_mock()
            klass.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "create-log-metric",
                    "a.b",
                    "--query",
                    "service:x",
                    "--group-by",
                    "@fbm.tenant",
                ],
            )

            assert result.exit_code == 0, result.output
            assert mock_client.create_log_metric.call_args.kwargs["group_by"] == [
                {"path": "@fbm.tenant", "tag_name": "fbm.tenant"}
            ]
