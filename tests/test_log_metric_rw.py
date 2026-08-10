"""Tests for reading, auditing, updating and deleting log-based metrics.

Two failure modes drive this file, and neither is visible in the output of the
thing that fails.

**A short harvest.** An enumeration of log metrics once retrieved 41 of 62
filters and reported "no anchor collisions" with exactly the confidence of a
complete run. A fetch loop that ends early still returns a well-formed list, so
the only defence is an explicit assertion that what came back matches what the
org enumerated -- and a loud failure when it does not.

**An empty harvest.** A clean audit and a broken one produce the same empty
collision list. Hence the positive control: a string that MUST hit. If it does
not, the run is void, whatever the candidate's result said.

The collision rules themselves come from a measured incident, anonymised here:
a log line for an order-*retirement* path was worded "...refusing to reserve
inventory...", which contains the ``"Refusing to reserve inventory"`` anchor of
an unrelated metric counting a *no-op* path, and tens of thousands of retirement
events were counted as no-ops. The fixtures below are that case, shrunk.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from dd_cli.anchors import (
    ANCHOR_IN_CANDIDATE,
    CANDIDATE_IN_ANCHOR,
    collision_direction,
    extract_quoted_phrases,
    find_collisions,
    harvest_anchors,
)
from dd_cli.cli import _pick_positive_control, cli
from dd_cli.http import DatadogClient

METRICS_PATH = "/api/v2/logs/config/metrics"

# The incident, shrunk to three metrics. Only one carries the anchor that the
# candidate string collides with -- a 1-in-3 density here standing in for the
# 1-in-62 density that survived eyeballing in production.
NOOP_QUERY = (
    'service:orders-service status:warn ("Refusing to reserve inventory" '
    'OR "reserve inventory request arrived for")'
)
FIXTURE_METRICS: list[dict[str, Any]] = [
    {
        "id": "orders.reserve_inventory_noop",
        "type": "logs_metrics",
        "attributes": {
            "filter": {"query": NOOP_QUERY},
            "group_by": [{"path": "service", "tag_name": "service"}],
            "compute": {"aggregation_type": "count"},
        },
    },
    {
        "id": "orders.attention_open",
        "type": "logs_metrics",
        "attributes": {
            "filter": {
                "query": 'service:orders "Opened a new customer attention record"'
            },
            "group_by": [],
            "compute": {
                "aggregation_type": "distribution",
                "path": "@orders.attention_open",
                "include_percentiles": False,
            },
        },
    },
    {
        # No quoted phrase at all: contributes to the metric denominator but
        # not the phrase denominator. The difference is the point.
        "id": "infra.resource_health",
        "type": "logs_metrics",
        "attributes": {
            "filter": {"query": "@service:infra @evt.category:ResourceHealth"},
            "group_by": [{"path": "env", "tag_name": "env"}],
            "compute": {"aggregation_type": "count"},
        },
    },
]

# The real, retired wording. Contains the noop metric's anchor.
BAD_CANDIDATE = "Order retired: refusing to reserve inventory for order 123"
# The reworded replacement. Collides with nothing.
GOOD_CANDIDATE = "Order retired: declining to hold stock for this order"


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


class FakeLogMetricsAPI:
    """A fake Datadog log-metrics endpoint, driven through a real transport.

    Deliberately not a MagicMock. A mock records whichever call it is handed
    and reports success, which cannot distinguish a complete harvest from a
    short one, nor a PATCH that sends the right fields from one that sends
    fields Datadog would reject.
    """

    def __init__(
        self,
        metrics: list[dict[str, Any]] | None = None,
        *,
        list_body: Any = None,
        detail_fails_after: int | None = None,
        detail_status: int = 200,
    ) -> None:
        self.metrics = {m["id"]: json.loads(json.dumps(m)) for m in (metrics or [])}
        self.list_body = list_body
        self.detail_fails_after = detail_fails_after
        self.detail_status = detail_status
        self.requests: list[httpx.Request] = []
        self._detail_served = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def methods(self, path_suffix: str | None = None) -> list[str]:
        return [
            r.method
            for r in self.requests
            if path_suffix is None or r.url.path.endswith(path_suffix)
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == METRICS_PATH:
            if self.list_body is not None:
                return httpx.Response(200, json=self.list_body)
            return httpx.Response(200, json={"data": list(self.metrics.values())})

        metric_id = path.rsplit("/", 1)[-1]

        if request.method == "GET":
            self._detail_served += 1
            if (
                self.detail_fails_after is not None
                and self._detail_served > self.detail_fails_after
            ):
                # The 41-of-62 shape: the endpoint keeps answering, but stops
                # carrying a metric. Nothing about the response says "partial".
                return httpx.Response(self.detail_status, json={"data": None})
            if metric_id not in self.metrics:
                return httpx.Response(404, json={"errors": ["not_found"]})
            return httpx.Response(200, json={"data": self.metrics[metric_id]})

        if request.method == "PATCH":
            body = json.loads(request.content)["data"]["attributes"]
            current = self.metrics[metric_id]["attributes"]
            # Datadog's PATCH is an attribute-level merge: an omitted field is
            # left alone, and a supplied group_by replaces the list wholesale.
            # Verified live against us3 on a throwaway metric.
            if "filter" in body:
                current["filter"] = body["filter"]
            if "group_by" in body:
                current["group_by"] = body["group_by"]
            if "compute" in body:
                current["compute"] = {**current["compute"], **body["compute"]}
            return httpx.Response(200, json={"data": self.metrics[metric_id]})

        if request.method == "DELETE":
            self.metrics.pop(metric_id, None)
            # Datadog answers 204 with an empty body.
            return httpx.Response(204)

        raise AssertionError(f"unexpected {request.method} {path}")


def run(runner: CliRunner, api: FakeLogMetricsAPI, args: list[str]):
    with patch(
        "dd_cli.cli.DatadogClient", partial(DatadogClient, transport=api.transport)
    ):
        return runner.invoke(cli, args)


def envelope(result) -> dict[str, Any]:
    """Parse the JSON envelope every data-producing command prints."""
    text = result.output
    start = text.index("{")
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(text[start:])
    return payload


class TestQuotedPhraseExtraction:
    def test_multiple_phrases_in_one_query(self):
        assert extract_quoted_phrases(NOOP_QUERY) == [
            "Refusing to reserve inventory",
            "reserve inventory request arrived for",
        ]

    def test_no_quotes_yields_nothing(self):
        assert extract_quoted_phrases("service:x status:error") == []

    def test_empty_quotes_are_not_anchors(self):
        assert extract_quoted_phrases('service:x "" "real"') == ["real"]

    def test_escaped_quote_inside_a_phrase(self):
        assert extract_quoted_phrases(r'"say \"hi\" now"') == ['say "hi" now']

    def test_none_query(self):
        assert extract_quoted_phrases(None) == []


class TestCollisionDirection:
    def test_the_actual_incident(self):
        assert (
            collision_direction(BAD_CANDIDATE, "Refusing to reserve inventory")
            == ANCHOR_IN_CANDIDATE
        )

    def test_matching_is_case_insensitive(self):
        """Datadog matches the phrase case-insensitively; so must the audit.

        The production string said 'refusing to reserve inventory' in lower case and
        the anchor said 'Refusing to reserve inventory'. A case-sensitive check would
        have cleared it.
        """
        assert (
            collision_direction(
                "...REFUSING MARKFULFILLABLE...", "refusing markfulfillable"
            )
            == ANCHOR_IN_CANDIDATE
        )

    def test_candidate_inside_anchor_is_also_a_collision(self):
        assert (
            collision_direction("reserve inventory", "Refusing to reserve inventory")
            == CANDIDATE_IN_ANCHOR
        )

    def test_reworded_string_collides_with_nothing(self):
        assert (
            collision_direction(GOOD_CANDIDATE, "Refusing to reserve inventory") is None
        )

    def test_wildcard_segments_must_appear_in_order(self):
        assert (
            collision_direction("an ERROR during Bisync run", "*ERROR*Bisync*")
            == ANCHOR_IN_CANDIDATE
        )
        assert collision_direction("Bisync then ERROR", "*ERROR*Bisync*") is None

    def test_empty_candidate_never_collides(self):
        assert collision_direction("   ", "anything") is None

    def test_wildcard_standing_in_for_nothing_still_collides(self):
        """A false negative here greenlights a colliding string. Never do that.

        Segment-exact matching would look for 'foo ' immediately followed by
        ' bar' and miss 'foo bar', where the wildcard covered zero characters.
        """
        assert (
            collision_direction("foo bar happened", "foo * bar") == ANCHOR_IN_CANDIDATE
        )

    def test_pure_wildcard_anchors_nothing(self):
        assert collision_direction("literally anything", "*") is None


class TestPositiveControlSelection:
    def test_prefers_the_longest_self_matching_phrase(self):
        assert (
            _pick_positive_control(harvest_anchors(FIXTURE_METRICS))
            == "Opened a new customer attention record"
        )

    def test_skips_a_phrase_that_cannot_match_itself(self):
        """A '*'-only anchor would fail the control and void a good audit."""
        metrics = [
            {
                "id": "m.wildcard",
                "attributes": {"filter": {"query": '"*"'}},
            },
            {
                "id": "m.real",
                "attributes": {"filter": {"query": '"short"'}},
            },
        ]

        assert _pick_positive_control(harvest_anchors(metrics)) == "short"

    def test_no_usable_phrase_yields_no_control(self):
        metrics = [{"id": "m", "attributes": {"filter": {"query": '"*"'}}}]

        assert _pick_positive_control(harvest_anchors(metrics)) is None


class TestHarvestAnchors:
    def test_denominator_counts_phrases_not_metrics(self):
        anchors = harvest_anchors(FIXTURE_METRICS)
        assert len(anchors) == 3
        assert len({a.metric_id for a in anchors}) == 2

    def test_find_collisions_reports_the_offending_phrase_and_query(self):
        hits = find_collisions(BAD_CANDIDATE, harvest_anchors(FIXTURE_METRICS))
        assert len(hits) == 1
        assert hits[0]["metric_id"] == "orders.reserve_inventory_noop"
        assert hits[0]["phrase"] == "Refusing to reserve inventory"
        assert hits[0]["filter_query"] == NOOP_QUERY
        assert hits[0]["direction"] == ANCHOR_IN_CANDIDATE


class TestListLogMetrics:
    def test_lists_and_asserts_completeness(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["list-log-metrics", "--format", "json"])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["count"] == 3
        assert payload["completeness"] == {
            "enumerated": 3,
            "fetched": 3,
            "per_metric_fetches": 0,
            "asserted_equal": True,
        }
        # The list endpoint already carries attributes; no per-metric GETs.
        assert api.methods() == ["GET"]

    def test_detail_mode_fetches_every_enumerated_id(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["list-log-metrics", "--detail", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert envelope(result)["completeness"]["per_metric_fetches"] == 3
        assert len(api.requests) == 4  # 1 list + 3 detail

    def test_short_detail_harvest_fails_loudly(self, runner, mock_env):
        """The 41-of-62 case: two of three metrics come back, no error anywhere."""
        api = FakeLogMetricsAPI(FIXTURE_METRICS, detail_fails_after=2)

        result = run(runner, api, ["list-log-metrics", "--detail"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["data"] is None
        assert payload["count"] is None
        assert "refusing to audit a partial set" in payload["error"]["message"]

    def test_missing_data_key_is_not_an_empty_org(self, runner, mock_env):
        api = FakeLogMetricsAPI(list_body={"meta": {}})

        result = run(runner, api, ["list-log-metrics"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["data"] is None
        assert "no 'data' key" in payload["error"]["message"]

    def test_data_of_wrong_type_is_not_an_empty_org(self, runner, mock_env):
        api = FakeLogMetricsAPI(list_body={"data": {"id": "x"}})

        result = run(runner, api, ["list-log-metrics"])

        assert result.exit_code != 0
        assert "expected an array" in envelope(result)["error"]["message"]

    def test_duplicate_ids_defeat_a_count_check_and_are_refused(self, runner, mock_env):
        dupe = json.loads(json.dumps(FIXTURE_METRICS[0]))
        api = FakeLogMetricsAPI(list_body={"data": [FIXTURE_METRICS[0], dupe]})

        result = run(runner, api, ["list-log-metrics"])

        assert result.exit_code != 0
        assert "duplicate ids" in envelope(result)["error"]["message"]

    def test_contains_filter_reports_the_full_denominator(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["list-log-metrics", "--contains", "infra"])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["count"] == 1
        assert payload["completeness"]["enumerated"] == 3
        assert payload["filters"]["contains"] == "infra"

    def test_empty_filtered_result_says_what_it_asked(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["list-log-metrics", "--contains", "nothing-here"])

        assert result.exit_code == 0, result.output
        assert envelope(result)["count"] == 0
        assert "0 of 3 log metrics match --contains" in result.stderr


class TestGetLogMetric:
    def test_returns_the_resource_and_its_anchor_phrases(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["get-log-metric", "orders.reserve_inventory_noop"])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["data"]["id"] == "orders.reserve_inventory_noop"
        assert payload["anchor_phrases"] == [
            "Refusing to reserve inventory",
            "reserve inventory request arrived for",
        ]

    def test_unknown_metric_fails_with_an_envelope(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["get-log-metric", "no.such.metric"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["data"] is None


class TestAuditAnchors:
    def test_finds_the_incident_collision(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["audit-log-metric-anchors", BAD_CANDIDATE])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["count"] == 1
        hit = payload["data"][0]
        assert hit["metric_id"] == "orders.reserve_inventory_noop"
        assert hit["phrase"] == "Refusing to reserve inventory"
        assert hit["filter_query"] == NOOP_QUERY
        assert hit["direction"] == ANCHOR_IN_CANDIDATE

    def test_reports_the_denominator(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["audit-log-metric-anchors", GOOD_CANDIDATE])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["count"] == 0
        assert payload["checked"] == {
            "metrics": 3,
            "metrics_with_quoted_phrase": 2,
            "phrases": 3,
            "distinct_phrases": 3,
        }
        assert "checked 3 metrics / 3 distinct quoted phrases" in result.stderr

    def test_collision_in_the_other_direction(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["audit-log-metric-anchors", "reserve inventory"])

        assert result.exit_code == 0, result.output
        directions = {h["direction"] for h in envelope(result)["data"]}
        assert directions == {CANDIDATE_IN_ANCHOR}

    def test_derived_positive_control_hits(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["audit-log-metric-anchors", GOOD_CANDIDATE])

        control = envelope(result)["positive_control"]
        assert control["source"] == "derived_longest_phrase"
        assert control["ok"] is True
        assert control["hit_count"] >= 1

    def test_explicit_positive_control_that_misses_voids_the_run(
        self, runner, mock_env
    ):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "audit-log-metric-anchors",
                GOOD_CANDIDATE,
                "--positive-control",
                "this phrase is in no filter anywhere",
            ],
        )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        # The clean-looking result must NOT survive a failed control.
        assert payload["data"] is None
        assert payload["count"] is None
        assert payload["positive_control"]["ok"] is False
        assert "positive control FAILED" in payload["error"]["message"]

    def test_empty_harvest_is_not_a_clean_audit(self, runner, mock_env):
        """No anchors anywhere: nothing to audit against, so nothing is proven."""
        api = FakeLogMetricsAPI([FIXTURE_METRICS[2]])

        result = run(runner, api, ["audit-log-metric-anchors", BAD_CANDIDATE])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["positive_control"]["value"] is None
        assert "nothing to audit against" in payload["error"]["message"]

    def test_short_harvest_fails_instead_of_reporting_no_collisions(
        self, runner, mock_env
    ):
        """The dropped metric is the one that collides. Silence would be wrong."""
        api = FakeLogMetricsAPI(FIXTURE_METRICS, detail_fails_after=1)

        result = run(
            runner, api, ["audit-log-metric-anchors", BAD_CANDIDATE, "--detail"]
        )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["data"] is None

    def test_empty_candidate_is_refused(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["audit-log-metric-anchors", "   "])

        assert result.exit_code != 0
        assert "empty" in result.output
        assert api.requests == []


class TestUpdateLogMetric:
    def test_dry_run_sends_no_patch(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--query",
                'service:x "new anchor"',
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert api.methods() == ["GET"]
        payload = envelope(result)
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["sent_nothing"] is True
        assert payload["data"]["changes"] == [
            {
                "field": "filter.query",
                "before": NOOP_QUERY,
                "after": 'service:x "new anchor"',
            }
        ]
        assert "do NOT backfill" in payload["warning"]

    def test_patch_sends_only_the_patchable_fields(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--query",
                'service:x "new anchor"',
            ],
        )

        assert result.exit_code == 0, result.output
        patch_req = next(r for r in api.requests if r.method == "PATCH")
        body = json.loads(patch_req.content)
        assert body == {
            "data": {
                "type": "logs_metrics",
                "attributes": {"filter": {"query": 'service:x "new anchor"'}},
            }
        }
        # No id in the update payload, and no compute: Datadog's
        # LogsMetricUpdateAttributes accepts filter, group_by and
        # compute.include_percentiles, and nothing else.
        assert "id" not in body["data"]

    def test_group_by_only_leaves_the_query_alone(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--group-by",
                "env",
            ],
        )

        assert result.exit_code == 0, result.output
        after = envelope(result)["data"]["after"]["attributes"]
        assert after["filter"]["query"] == NOOP_QUERY
        assert after["group_by"] == [{"path": "env", "tag_name": "env"}]

    def test_after_state_is_re_read_from_the_server(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--query",
                'service:x "new anchor"',
            ],
        )

        assert api.methods() == ["GET", "PATCH", "GET"]

    def test_clear_group_by_sends_an_empty_list(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--clear-group-by",
            ],
        )

        assert result.exit_code == 0, result.output
        patch_req = next(r for r in api.requests if r.method == "PATCH")
        assert json.loads(patch_req.content)["data"]["attributes"]["group_by"] == []

    def test_nothing_to_update_is_refused_before_any_request(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["update-log-metric", "orders.attention_open"])

        assert result.exit_code != 0
        assert "Nothing to update" in result.output
        assert api.requests == []

    def test_empty_query_is_refused(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.attention_open",
                "--query",
                "  ",
            ],
        )

        assert result.exit_code != 0
        assert "every log in the org" in result.output
        assert api.requests == []

    def test_bare_group_by_path_is_refused_before_any_request(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.attention_open",
                "--group-by",
                "fbm.tenant",
            ],
        )

        assert result.exit_code != 0
        assert "@fbm.tenant" in result.output
        assert api.requests == []

    def test_include_percentiles_on_a_count_metric_is_refused(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.reserve_inventory_noop",
                "--include-percentiles",
            ],
        )

        assert result.exit_code != 0
        assert "distribution" in result.output
        assert "PATCH" not in api.methods()

    def test_include_percentiles_on_a_distribution_metric(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            [
                "update-log-metric",
                "orders.attention_open",
                "--include-percentiles",
            ],
        )

        assert result.exit_code == 0, result.output
        patch_req = next(r for r in api.requests if r.method == "PATCH")
        assert json.loads(patch_req.content)["data"]["attributes"]["compute"] == {
            "include_percentiles": True
        }

    def test_help_warns_that_metrics_do_not_backfill(self, runner):
        result = runner.invoke(cli, ["update-log-metric", "-h"])

        assert result.exit_code == 0
        assert "does NOT backfill" in result.output
        assert "NARROWED metric alongside" in result.output


class TestDeleteLogMetric:
    def test_refuses_without_yes(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(runner, api, ["delete-log-metric", "orders.attention_open"])

        assert result.exit_code != 0
        assert "--yes" in result.output
        assert api.requests == []

    def test_captures_the_definition_then_deletes(self, runner, mock_env):
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            ["delete-log-metric", "orders.attention_open", "--yes"],
        )

        assert result.exit_code == 0, result.output
        assert api.methods() == ["GET", "DELETE"]
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["data"]["deleted"] == "orders.attention_open"
        # Printed before deletion so the deleted state is recoverable by hand.
        assert payload["data"]["definition"]["attributes"]["compute"] == {
            "aggregation_type": "distribution",
            "path": "@orders.attention_open",
            "include_percentiles": False,
        }
        assert "orders.attention_open" not in api.metrics

    def test_a_204_is_a_success_not_an_invalid_json_failure(self, runner, mock_env):
        """DELETE answers 204 with no body; parsing it would fail a done delete."""
        api = FakeLogMetricsAPI(FIXTURE_METRICS)

        result = run(
            runner,
            api,
            ["delete-log-metric", "orders.attention_open", "--yes"],
        )

        assert result.exit_code == 0
        assert "Invalid JSON" not in result.output


class TestUpdateLogMetricClient:
    """http-layer: what Datadog's PATCH actually accepts."""

    def test_empty_update_raises_before_any_request(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": {}})

        dd = DatadogClient(
            site="us3.datadoghq.com",
            pat="ddpat_test",
            transport=httpx.MockTransport(handler),
        )

        with pytest.raises(ValueError, match="nothing to change"):
            dd.update_log_metric("a.b")

        assert seen == []

    def test_metric_id_is_url_escaped(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": {}})

        dd = DatadogClient(
            site="us3.datadoghq.com",
            pat="ddpat_test",
            transport=httpx.MockTransport(handler),
        )

        dd.update_log_metric("a b/c", query="service:x")

        # raw_path, not path: httpx decodes the latter, which would hide a
        # metric id whose '/' had escaped into the URL structure.
        assert seen[0].url.raw_path.decode() == f"{METRICS_PATH}/a%20b%2Fc"
