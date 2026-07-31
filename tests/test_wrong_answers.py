"""Tests for bugs that produce a confidently WRONG answer.

Distinct from the truncation tests: these paths do not merely return less than
they should, they return data that is incorrect, with no failure anywhere.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dd_cli.cli import (
    _fetch_teams,
    _parse_time_to_epoch_ms,
    _parse_time_to_epoch_s,
    cli,
)
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


class TestTeamsPaginationArithmetic:
    """Page-NUMBER paging with a VARYING page size addresses a moving window."""

    def test_no_duplicates_when_max_results_is_not_a_page_multiple(self):
        """At max_results=150 the old code re-fetched items 50-99.

        page 0 @ size 100 -> items 0-99; then limit shrinks to 50 and
        page 1 @ size 50 -> items 50-99 AGAIN. 150 'teams' = 50 duplicates,
        and items 100-149 were never fetched at all.
        """
        dd = MagicMock()

        def list_teams(*, page_number, page_size, **kwargs):
            start = page_number * page_size
            return {
                "data": [
                    {"id": str(i)} for i in range(start, min(start + page_size, 300))
                ]
            }

        dd.list_teams.side_effect = list_teams

        result = _fetch_teams(
            dd,
            keyword=None,
            me=False,
            include=None,
            fields=None,
            sort=None,
            max_results=150,
        )

        ids = [t["id"] for t in result.items]
        assert len(ids) == 150
        assert len(set(ids)) == 150, "duplicate teams returned"
        assert ids == [str(i) for i in range(150)]

    def test_stops_at_short_page(self):
        dd = MagicMock()
        dd.list_teams.return_value = {"data": [{"id": "1"}]}

        result = _fetch_teams(
            dd,
            keyword=None,
            me=False,
            include=None,
            fields=None,
            sort=None,
            max_results=1000,
        )

        assert len(result.items) == 1
        assert result.truncated is False


class TestResolveTeamByHandleUnderTruncation:
    def test_truncated_fetch_does_not_report_a_flat_not_found(self, runner, mock_env):
        """'Not found' from a truncated search is an error, not an answer."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.list_teams.side_effect = lambda *, page_number, page_size, **kw: {
                "data": [
                    {
                        "id": str(page_number * page_size + i),
                        "attributes": {"handle": f"other-{page_number}-{i}"},
                    }
                    for i in range(page_size)
                ]
            }
            cls.return_value = client

            result = runner.invoke(
                cli, ["list-team-notification-rules", "missing-handle"]
            )

        assert result.exit_code != 0
        combined = result.output.lower()
        assert "truncat" in combined or "incomplete" in combined


class TestTimeParsing:
    """An unanchored regex silently misreads the window, so the count is wrong."""

    @pytest.mark.parametrize(
        "parser", [_parse_time_to_epoch_s, _parse_time_to_epoch_ms]
    )
    def test_rejects_compound_duration_instead_of_silently_truncating(self, parser):
        """'now-1h30m' used to silently mean 1h."""
        with pytest.raises(Exception, match="(?i)invalid time"):
            parser("now-1h30m")

    @pytest.mark.parametrize(
        "parser", [_parse_time_to_epoch_s, _parse_time_to_epoch_ms]
    )
    def test_rejects_trailing_garbage(self, parser):
        """'now-7days' used to silently mean 7d."""
        with pytest.raises(Exception, match="(?i)invalid time"):
            parser("now-7days")

    @pytest.mark.parametrize(
        "parser", [_parse_time_to_epoch_s, _parse_time_to_epoch_ms]
    )
    def test_accepts_bare_now(self, parser):
        assert parser("now") > 0

    @pytest.mark.parametrize(
        "parser", [_parse_time_to_epoch_s, _parse_time_to_epoch_ms]
    )
    def test_accepts_seconds_and_weeks(self, parser):
        assert parser("now-90s") > 0
        assert parser("now-2w") > 0

    def test_units_scale_correctly(self):
        now = _parse_time_to_epoch_s("now")
        hour_ago = _parse_time_to_epoch_s("now-1h")
        assert 3595 <= now - hour_ago <= 3605

    def test_ms_parser_is_the_s_parser_times_1000(self):
        s = _parse_time_to_epoch_s("now-1h")
        ms = _parse_time_to_epoch_ms("now-1h")
        assert abs(ms - s * 1000) < 5000


class TestEnrichmentFailuresAreRecorded:
    def test_incident_type_failure_is_recorded_not_swallowed(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.get_incident.return_value = {
                "data": {"id": "1", "attributes": {"incident_type_uuid": "uuid-1"}}
            }
            client.get_incident_type.side_effect = DatadogAPIError(
                403, "Forbidden", "{}"
            )
            client.get_incident_integrations.return_value = {"data": []}
            cls.return_value = client

            result = runner.invoke(cli, ["get-incident", "1", "--enrich"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        enrichment = payload["enrichment"]
        assert enrichment["partial"] is True
        errors = enrichment["errors"]
        assert any(e["step"] == "incident_type" and e["status"] == 403 for e in errors)

    def test_integrations_failure_is_recorded(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.get_incident.return_value = {"data": {"id": "1", "attributes": {}}}
            client.get_incident_integrations.side_effect = DatadogAPIError(
                429, "Too many requests", "{}"
            )
            cls.return_value = client

            result = runner.invoke(cli, ["get-incident", "1", "--enrich"])

        assert result.exit_code == 0
        enrichment = json.loads(result.stdout)["enrichment"]
        assert enrichment["partial"] is True
        assert any(e["step"] == "integrations" for e in enrichment["errors"])

    def test_successful_enrichment_is_not_marked_partial(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.get_incident.return_value = {
                "data": {"id": "1", "attributes": {"incident_type_uuid": "uuid-1"}}
            }
            client.get_incident_type.return_value = {"data": {"id": "uuid-1"}}
            client.get_incident_integrations.return_value = {"data": []}
            cls.return_value = client

            result = runner.invoke(cli, ["get-incident", "1", "--enrich"])

        assert result.exit_code == 0
        enrichment = json.loads(result.stdout)["enrichment"]
        assert enrichment.get("partial", False) is False
        assert "incident_type" in enrichment


class TestSloHistoryFailure:
    def test_history_failure_records_status_and_marks_partial(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.get_slo.return_value = {"data": {"id": "slo-1"}}
            client.get_slo_history.side_effect = DatadogAPIError(
                429, "Too many requests", "{}"
            )
            cls.return_value = client

            result = runner.invoke(cli, ["get-slo", "slo-1", "--from", "now-1d"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["partial"] is True
        assert payload["history"]["error"]["status"] == 429


class TestUnpaginatedCommandsAdmitIt:
    def test_search_et_issues_marks_single_page_result(self, runner, mock_env):
        """One request, no cursor handling: page 1 of N is not a total."""
        with patch("dd_cli.cli.DatadogClient") as cls:
            client = fake_client()
            client.search_error_tracking_issues.return_value = {
                "data": [{"id": "issue-1"}],
                "meta": {},
            }
            cls.return_value = client

            result = runner.invoke(
                cli, ["search-et-issues", "service:foo", "--from", "now-1d"]
            )

        payload = json.loads(result.stdout)
        assert payload["complete"] is False
