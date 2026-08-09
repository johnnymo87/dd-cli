"""Tests for bugs that produce a confidently WRONG answer.

Distinct from the truncation tests: these paths do not merely return less than
they should, they return data that is incorrect, with no failure anywhere.
"""

from __future__ import annotations

import json
from functools import partial
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from dd_cli.cli import (
    _fetch_teams,
    _parse_time_to_epoch_ms,
    _parse_time_to_epoch_s,
    cli,
)
from dd_cli.http import DatadogAPIError, DatadogClient


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


def _client_factory(transport: httpx.BaseTransport):
    """A DatadogClient constructor bound to a fake transport.

    Used where the assertion is about the *request dd-cli actually sends*. A
    MagicMock client would happily record a call with the wrong parameter name
    and report success, which is precisely the class of bug under test here.
    """
    return partial(DatadogClient, transport=transport)


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


class TestTagFilterMatchesTheMonitorsOwnTags:
    """`--tag` must match the monitor's OWN tags, not its query scope.

    Datadog's ``GET /api/v1/monitor`` takes two different tag parameters:

    * ``tags`` filters *by scope* -- the tags on the resources the monitor
      watches, i.e. what appears inside its query string.
    * ``monitor_tags`` filters by the monitor's own tag list.

    dd-cli sent ``tags``, so every ownership-tag audit (``team:``,
    ``managed-by:``, ``feature:``) returned an EMPTY SET that was
    indistinguishable from a genuine "nothing there" result.

    The fake Datadog below implements both predicates *differently*, because a
    fixture in which a monitor's tags and its query scope coincide cannot see
    this bug at all -- that coincidence is exactly what hid it in production.
    """

    # Carries the ownership tag; the tag appears NOWHERE in its query.
    TAGGED = {
        "id": 1,
        "name": "owned by the team",
        "type": "query alert",
        "overall_state": "Alert",
        "tags": ["team:ba-fulfillment", "env:prod"],
        "query": "sum(last_5m):sum:some.metric{env:prod}.as_count() > 0",
    }
    # The mirror image: scopes the string in its query, carries no such tag.
    SCOPED_ONLY = {
        "id": 2,
        "name": "scopes the team in its query only",
        "type": "query alert",
        "overall_state": "OK",
        "tags": [],
        "query": "avg(last_5m):avg:other.metric{team:ba-fulfillment} > 1",
    }

    def _transport(self, seen: list[dict[str, str]]) -> httpx.MockTransport:
        monitors = [self.TAGGED, self.SCOPED_ONLY]

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen.append(params)
            out = monitors
            if params.get("monitor_tags"):
                for t in params["monitor_tags"].split(","):
                    out = [m for m in out if t in m["tags"]]
            if params.get("tags"):
                for t in params["tags"].split(","):
                    out = [m for m in out if t in m["query"]]
            return httpx.Response(200, json=out)

        return httpx.MockTransport(handler)

    def _run(self, runner, seen, args):
        with patch("dd_cli.cli.DatadogClient", _client_factory(self._transport(seen))):
            return runner.invoke(cli, args)

    def test_tag_finds_a_monitor_whose_query_does_not_mention_the_tag(
        self, runner, mock_env
    ):
        """The exact case that fooled us: tagged monitor, tag absent from query."""
        seen: list[dict[str, str]] = []
        result = self._run(
            runner, seen, ["list-monitors", "--tag", "team:ba-fulfillment"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert [m["id"] for m in payload["data"]] == [1]
        assert seen[0].get("monitor_tags") == "team:ba-fulfillment"
        assert "tags" not in seen[0]

    def test_tag_does_not_match_on_query_scope(self, runner, mock_env):
        """A monitor that only *scopes* the tag must not be returned by --tag."""
        seen: list[dict[str, str]] = []
        result = self._run(
            runner, seen, ["list-monitors", "--tag", "team:ba-fulfillment"]
        )

        payload = json.loads(result.stdout)
        assert 2 not in [m["id"] for m in payload["data"]]

    def test_scope_tag_still_reaches_the_query_scope_predicate(self, runner, mock_env):
        """The old behaviour stays available, under an honestly-named flag."""
        seen: list[dict[str, str]] = []
        result = self._run(
            runner, seen, ["list-monitors", "--scope-tag", "team:ba-fulfillment"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert [m["id"] for m in payload["data"]] == [2]
        assert seen[0].get("tags") == "team:ba-fulfillment"
        assert "monitor_tags" not in seen[0]

    def test_both_predicates_can_be_combined(self, runner, mock_env):
        seen: list[dict[str, str]] = []
        result = self._run(
            runner,
            seen,
            [
                "list-monitors",
                "--tag",
                "team:ba-fulfillment",
                "--scope-tag",
                "env:prod",
            ],
        )

        payload = json.loads(result.stdout)
        assert [m["id"] for m in payload["data"]] == [1]
        assert seen[0].get("monitor_tags") == "team:ba-fulfillment"
        assert seen[0].get("tags") == "env:prod"

    def test_repeated_tags_are_and_combined(self, runner, mock_env):
        seen: list[dict[str, str]] = []
        result = self._run(
            runner,
            seen,
            [
                "list-monitors",
                "--tag",
                "team:ba-fulfillment",
                "--tag",
                "team:nobody",
            ],
        )

        payload = json.loads(result.stdout)
        assert payload["data"] == []
        assert seen[0].get("monitor_tags") == "team:ba-fulfillment,team:nobody"

    def test_envelope_names_the_predicate_that_ran(self, runner, mock_env):
        """An empty result must say which question was actually asked."""
        seen: list[dict[str, str]] = []
        result = self._run(
            runner,
            seen,
            ["list-monitors", "--tag", "team:nobody", "--format", "json"],
        )

        payload = json.loads(result.stdout)
        assert payload["count"] == 0
        assert payload["filters"] == {
            "monitor_tags": ["team:nobody"],
            "scope_tags": [],
            "name": None,
        }

    def test_empty_tag_result_warns_instead_of_silently_reassuring(
        self, runner, mock_env
    ):
        """Zero is the failure mode here, so zero gets an explanation."""
        seen: list[dict[str, str]] = []
        result = self._run(runner, seen, ["list-monitors", "--tag", "team:nobody"])

        assert "monitor's own tags" in result.stderr
        assert "--scope-tag" in result.stderr


class TestSloTagFilterIsActuallyApplied:
    """`list-slos --tags` was sent as ``tags``, which Datadog ignores outright.

    The monitor bug returns too little; this one returns too much -- every SLO
    in the org, presented as if it had been filtered. Both are silent. The
    correct parameter is ``tags_query``.
    """

    SLOS = [
        {"id": "a", "name": "owned", "type": "metric", "tags": ["team:consumer"]},
        {"id": "b", "name": "not owned", "type": "metric", "tags": ["team:other"]},
    ]

    def _transport(self, seen: list[dict[str, str]]) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen.append(params)
            out = self.SLOS
            # Datadog ignores `tags` here entirely -- reproduce that faithfully.
            if params.get("tags_query"):
                for t in params["tags_query"].split(","):
                    out = [s for s in out if t in s["tags"]]
            return httpx.Response(200, json={"data": out})

        return httpx.MockTransport(handler)

    def test_tags_filter_reaches_the_parameter_datadog_honours(self, runner, mock_env):
        seen: list[dict[str, str]] = []
        with patch("dd_cli.cli.DatadogClient", _client_factory(self._transport(seen))):
            result = runner.invoke(cli, ["list-slos", "--tags", "team:consumer"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert [s["id"] for s in payload["data"]] == ["a"]
        assert seen[0].get("tags_query") == "team:consumer"
        assert "tags" not in seen[0]


class TestEmptyFilteredResultExplainsItself:
    """A zero must name the question, and must not blame the wrong filter.

    Filters are ANDed. Attributing an empty conjunction to one of its terms
    ("nothing carries team:x") is a fresh instance of the bug this command was
    fixed for: a confident claim the observation does not support.
    """

    TAGGED = {
        "id": 1,
        "name": "kafka consumer lag",
        "type": "query alert",
        "overall_state": "OK",
        "tags": ["team:platform"],
        "query": "avg(last_5m):avg:kafka.lag{env:uat} > 1",
    }

    def _transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            out = [self.TAGGED]
            if params.get("monitor_tags"):
                for t in params["monitor_tags"].split(","):
                    out = [m for m in out if t in m["tags"]]
            if params.get("tags"):
                for t in params["tags"].split(","):
                    out = [m for m in out if t in m["query"]]
            if params.get("name"):
                out = [m for m in out if params["name"].lower() in m["name"].lower()]
            return httpx.Response(200, json=out)

        return httpx.MockTransport(handler)

    def _run(self, runner, args):
        with patch("dd_cli.cli.DatadogClient", _client_factory(self._transport())):
            return runner.invoke(cli, args)

    def test_conjunction_of_two_tag_flags_does_not_blame_one_of_them(
        self, runner, mock_env
    ):
        """team:platform DOES exist; only the conjunction is empty."""
        result = self._run(
            runner,
            [
                "list-monitors",
                "--tag",
                "team:platform",
                "--scope-tag",
                "env:prod",
            ],
        )

        assert json.loads(result.stdout)["count"] == 0
        note = result.stderr
        assert "--tag team:platform" in note
        assert "--scope-tag env:prod" in note
        assert "ANDed" in note
        # The false claim the old wording made.
        assert "0 monitors carry ALL of team:platform" not in note

    def test_name_filter_is_named_in_the_note(self, runner, mock_env):
        result = self._run(
            runner, ["list-monitors", "--tag", "team:platform", "--name", "postgres"]
        )

        assert json.loads(result.stdout)["count"] == 0
        assert "--name postgres" in result.stderr
        assert "--tag team:platform" in result.stderr

    def test_no_note_when_no_filter_was_given(self, runner, mock_env):
        """An unfiltered empty org is not a filter mistake."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        with patch(
            "dd_cli.cli.DatadogClient",
            _client_factory(httpx.MockTransport(handler)),
        ):
            result = runner.invoke(cli, ["list-monitors"])

        assert json.loads(result.stdout)["count"] == 0
        assert "0 monitors match" not in result.stderr

    def test_note_fires_for_jsonl_too(self, runner, mock_env):
        result = self._run(
            runner, ["list-monitors", "--tag", "team:nobody", "--format", "jsonl"]
        )

        assert "0 monitors match ALL of: --tag team:nobody" in result.stderr

    def test_failure_envelope_still_names_the_filters(self, runner, mock_env):
        """A failure is an answer too, and it should carry the question."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"errors": ["Forbidden"]})

        with patch(
            "dd_cli.cli.DatadogClient",
            _client_factory(httpx.MockTransport(handler)),
        ):
            result = runner.invoke(cli, ["list-monitors", "--tag", "team:platform"])

        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["data"] is None
        assert payload["filters"]["monitor_tags"] == ["team:platform"]

    @pytest.mark.parametrize("flag", ["--tag", "--scope-tag"])
    def test_empty_tag_value_is_rejected_not_sent(self, runner, mock_env, flag):
        """An empty tag is not a filter -- Datadog would ignore it."""
        result = self._run(runner, ["list-monitors", flag, ""])

        assert result.exit_code != 0
        assert "empty" in result.output.lower() or "empty" in result.stderr.lower()


class TestSloListingCarriesItsOwnQuestion:
    def _transport(self, slos, seen=None):
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if seen is not None:
                seen.append(params)
            out = slos
            if params.get("tags_query"):
                for t in params["tags_query"].split(","):
                    out = [s for s in out if t in s.get("tags", [])]
            limit = int(params.get("limit", 1000))
            return httpx.Response(200, json={"data": out[:limit]})

        return httpx.MockTransport(handler)

    def test_envelope_echoes_the_filter(self, runner, mock_env):
        slos = [{"id": "a", "name": "n", "type": "metric", "tags": ["team:x"]}]
        with patch("dd_cli.cli.DatadogClient", _client_factory(self._transport(slos))):
            result = runner.invoke(cli, ["list-slos", "--tags", "team:x"])

        payload = json.loads(result.stdout)
        assert payload["filters"]["tags_query"] == "team:x"

    def test_empty_filtered_result_says_what_it_searched_for(self, runner, mock_env):
        slos = [{"id": "a", "name": "n", "type": "metric", "tags": ["team:x"]}]
        with patch("dd_cli.cli.DatadogClient", _client_factory(self._transport(slos))):
            result = runner.invoke(cli, ["list-slos", "--tags", "team:nobody"])

        assert json.loads(result.stdout)["count"] == 0
        assert "team:nobody" in result.stderr
        assert "tags_query" in result.stderr

    def test_a_full_page_is_reported_as_truncated(self, runner, mock_env):
        """No --limit given: the cap must still be detectable, not silent."""
        slos = [
            {"id": str(i), "name": "n", "type": "metric", "tags": []}
            for i in range(1500)
        ]
        seen: list[dict[str, str]] = []
        with patch(
            "dd_cli.cli.DatadogClient", _client_factory(self._transport(slos, seen))
        ):
            result = runner.invoke(cli, ["list-slos"])

        payload = json.loads(result.stdout)
        assert seen[0].get("limit") == "1000"
        assert payload["count"] == 1000
        assert payload["truncated"] is True
        assert payload["truncation_reason"] == "max_results_boundary_unknown"
