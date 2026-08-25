"""Tests for `dd-cli delete-monitor`.

Deleting a monitor is irreversible, so the failure modes worth pinning here are
not "does the verb work" but the ones that quietly destroy information:

**A backup that is not there when it matters.** The command reads the monitor
before deleting it so the operator keeps a copy of what they destroyed. The run
that most needs that copy is the one where the DELETE *fails* -- a DELETE that
lands but is masked by a headered 429 gets retried once, and the retry sees a
monitor that is already gone. A failure envelope without the definition would
drop the backup in exactly that case.

**An ID that is not an ID.** `_parse_monitor_ref` hands back non-URL input
unchanged and the client interpolates it into the request path, so a ref
carrying `/` or `?` rewrites the request. For a read that is a 404 nuisance;
for a DELETE it is request forging.

The fake API is driven through a real httpx transport rather than a MagicMock.
A mock records whichever kwarg it is handed and reports success, which cannot
see a `force=true` that was never put on the wire.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from dd_cli.cli import cli
from dd_cli.http import DatadogClient

MONITOR_PATH = "/api/v1/monitor"

# The monitor from the incident, shrunk: a duplicate of an existing monitor on
# the same metric and window, with a strictly less sensitive threshold.
FIXTURE_MONITOR: dict[str, Any] = {
    "id": 25391362,
    "name": "[fbm] shipment incomplete card gap",
    "type": "query alert",
    "query": (
        "sum(last_1h):sum:ba_fulfillment.fbm.shipment_incomplete_card_gap"
        "{*}.as_count() > 10"
    ),
    "message": "Card gap detected @slack-fulfillment-alerts",
    "tags": ["team:fulfillment", "managed-by:dd-cli"],
    "options": {"thresholds": {"critical": 10}, "notify_no_data": False},
}

SLO_REFUSAL = (
    "monitor [25391362,[fbm] shipment incomplete card gap] is referenced in "
    "slos: [34dbd856b8c8591dae09f9db0458adb2,fulfillment availability]"
)
COMPOSITE_REFUSAL = (
    "monitor [25391362,[fbm] shipment incomplete card gap] is referenced in "
    "composite monitors: [37050226,fulfillment rollup]"
)


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


class FakeMonitorAPI:
    """A fake `/api/v1/monitor/{id}` endpoint on a real transport."""

    def __init__(
        self,
        monitor: dict[str, Any] | None = FIXTURE_MONITOR,
        *,
        delete_status: int = 200,
        delete_errors: list[str] | None = None,
        deleted_id: int | None = None,
    ) -> None:
        self.monitor = json.loads(json.dumps(monitor)) if monitor else None
        self.delete_status = delete_status
        self.delete_errors = delete_errors
        self.deleted_id = deleted_id
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def methods(self) -> list[str]:
        return [r.method for r in self.requests]

    def request(self, method: str) -> httpx.Request:
        return next(r for r in self.requests if r.method == method)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.method == "GET":
            if self.monitor is None:
                return httpx.Response(404, json={"errors": ["Monitor not found"]})
            return httpx.Response(200, json=self.monitor)

        if request.method == "DELETE":
            if self.delete_errors is not None:
                return httpx.Response(
                    self.delete_status, json={"errors": self.delete_errors}
                )
            monitor_id = int(request.url.path.rsplit("/", 1)[-1])
            self.monitor = None
            return httpx.Response(
                200,
                json={"deleted_monitor_id": self.deleted_id or monitor_id},
            )

        raise AssertionError(f"unexpected {request.method} {request.url.path}")


def run(runner: CliRunner, api: FakeMonitorAPI, args: list[str]):
    with patch(
        "dd_cli.cli.DatadogClient", partial(DatadogClient, transport=api.transport)
    ):
        return runner.invoke(cli, args)


def envelope(result) -> dict[str, Any]:
    return json.loads(result.stdout)


class TestConfirmation:
    def test_refuses_without_yes(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", "25391362"])

        assert result.exit_code != 0
        assert "--yes" in result.output
        # Not even the read happens: nothing about this invocation was a request
        # to look at the monitor.
        assert api.requests == []

    def test_force_alone_is_not_confirmation(self, runner, mock_env):
        """--force means 'despite references', not 'yes I am sure'."""
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", "25391362", "--force"])

        assert result.exit_code != 0
        assert "--yes" in result.output
        assert api.requests == []


class TestRefParsing:
    def test_accepts_a_monitor_url(self, runner, mock_env):
        api = FakeMonitorAPI()
        url = "https://us3.datadoghq.com/monitors/25391362?group=env%3Aprod"

        result = run(runner, api, ["delete-monitor", url, "--yes"])

        assert result.exit_code == 0, result.output
        assert api.request("DELETE").url.path == f"{MONITOR_PATH}/25391362"

    @pytest.mark.parametrize(
        "ref",
        [
            "manage",
            "25391362?force=true",
            "25391362/../../dashboard/abc",
            "",
            "https://us3.datadoghq.com/monitors/manage",
        ],
    )
    def test_a_ref_that_is_not_an_id_is_refused_before_any_request(
        self, runner, mock_env, ref
    ):
        """A ref carrying '/' or '?' would rewrite the request path."""
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", ref, "--yes"])

        assert result.exit_code != 0
        assert api.requests == []


class TestCaptureBeforeDestroy:
    def test_captures_the_definition_then_deletes(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code == 0, result.output
        assert api.methods() == ["GET", "DELETE"]
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["data"]["deleted"] == "25391362"
        assert payload["data"]["deleted_monitor_id"] == 25391362
        definition = payload["data"]["definition"]
        # The whole monitor, not a summary: this is the only backup.
        assert definition["query"] == FIXTURE_MONITOR["query"]
        assert definition["message"] == FIXTURE_MONITOR["message"]
        assert definition["tags"] == FIXTURE_MONITOR["tags"]
        assert definition["options"] == FIXTURE_MONITOR["options"]

    def test_a_get_that_yields_no_monitor_object_blocks_the_delete(
        self, runner, mock_env
    ):
        api = FakeMonitorAPI(monitor=None)
        api.monitor = "not a monitor"  # type: ignore[assignment]

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        assert "DELETE" not in api.methods()
        assert envelope(result)["ok"] is False

    def test_a_failed_delete_still_carries_the_captured_definition(
        self, runner, mock_env
    ):
        """The run that most needs the backup is the one where DELETE fails.

        `_write` retries a headered 429 once, so a DELETE that landed can be
        reported as a failure. Losing the definition there would defeat the
        reason for reading it first.
        """
        api = FakeMonitorAPI(delete_status=500, delete_errors=["Internal error"])

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["definition"]["query"] == FIXTURE_MONITOR["query"]

    def test_a_network_error_on_delete_still_carries_the_definition(
        self, runner, mock_env
    ):
        """The DELETE may have landed; the response just never came back.

        `_write` does not retry a transport error precisely because the write
        may have happened, so this run ends not knowing whether the monitor
        exists -- which makes the captured definition more valuable here, not
        less.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(200, json=FIXTURE_MONITOR)

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(cli, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["definition"]["query"] == FIXTURE_MONITOR["query"]

    def test_an_unparseable_200_still_carries_the_definition(self, runner, mock_env):
        """The worst run of all: the delete succeeded and the body is garbage.

        A 200 means the monitor is gone, so dropping the definition here would
        lose the only copy of a monitor that no longer exists.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(200, content=b"<html>gateway</html>")
            return httpx.Response(200, json=FIXTURE_MONITOR)

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(cli, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["definition"]["query"] == FIXTURE_MONITOR["query"]

    def test_a_200_of_the_wrong_shape_does_not_crash_into_empty_stdout(
        self, runner, mock_env
    ):
        """An unhandled traceback prints nothing on stdout.

        That is the failure-as-empty-output shape the envelope contract exists
        to ban, and it would land on a run where the monitor is already gone.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(200, json=[1, 2])
            return httpx.Response(200, json=FIXTURE_MONITOR)

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(cli, ["delete-monitor", "25391362", "--yes"])

        payload = envelope(result)
        # A 200 means Datadog deleted it; the definition must survive either way.
        assert payload["data"]["definition"]["query"] == FIXTURE_MONITOR["query"]
        assert payload["data"]["deleted_monitor_id"] is None


class TestNotFound:
    def test_404_is_an_error_not_a_fake_success(self, runner, mock_env):
        api = FakeMonitorAPI(monitor=None)

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["data"] is None
        assert "DELETE" not in api.methods()

    def test_404_names_the_causes_including_the_wrong_site(self, runner, mock_env):
        """A wrong DD_SITE 404s just like an already-deleted monitor does."""
        api = FakeMonitorAPI(monitor=None)

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        combined = result.output
        assert "DD_SITE" in combined
        assert "already deleted" in combined


class TestReferenceRefusal:
    @pytest.mark.parametrize("message", [SLO_REFUSAL, COMPOSITE_REFUSAL])
    def test_a_reference_refusal_explains_force(self, runner, mock_env, message):
        api = FakeMonitorAPI(delete_status=400, delete_errors=[message])

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        hint = payload["hint"]
        assert "--force" in hint
        # Forcing does not clean the reference up; say so.
        assert "dangling" in hint

    def test_an_unrecognized_400_still_fails_cleanly_without_a_hint(
        self, runner, mock_env
    ):
        """The hint decorates a failure; it does not decide one.

        If Datadog rewords its refusal, the cost is a missing hint -- never a
        misclassified result.
        """
        api = FakeMonitorAPI(delete_status=400, delete_errors=["Bad Request"])

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "hint" not in payload
        assert payload["definition"]["query"] == FIXTURE_MONITOR["query"]


class TestForce:
    def test_default_path_sends_no_force_parameter_at_all(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code == 0, result.output
        assert "force" not in api.request("DELETE").url.params

    def test_force_sends_the_string_true(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["delete-monitor", "25391362", "--yes", "--force"])

        assert result.exit_code == 0, result.output
        assert api.request("DELETE").url.params["force"] == "true"


class TestDeletedIdCrossCheck:
    def test_a_mismatched_deleted_monitor_id_is_warned_about(self, runner, mock_env):
        api = FakeMonitorAPI(deleted_id=99999999)

        result = run(runner, api, ["delete-monitor", "25391362", "--yes"])

        assert result.exit_code == 0, result.output
        assert "99999999" in result.stderr
        assert "25391362" in result.stderr


class TestDeleteMonitorClient:
    """http-layer: what actually goes on the wire."""

    def _client(self, handler) -> DatadogClient:
        return DatadogClient(
            site="us3.datadoghq.com",
            pat="ddpat_test",
            transport=httpx.MockTransport(handler),
        )

    def test_delete_returns_the_deleted_id_payload(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"deleted_monitor_id": 25391362})

        dd = self._client(handler)
        assert dd.delete_monitor("25391362") == {"deleted_monitor_id": 25391362}
        assert seen[0].method == "DELETE"
        assert seen[0].url.path == f"{MONITOR_PATH}/25391362"

    def test_the_id_is_percent_escaped(self):
        """Defense in depth: the CLI validates, the client escapes anyway."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"deleted_monitor_id": 1})

        dd = self._client(handler)
        dd.delete_monitor("1?force=true")
        # raw_path, not path: httpx URL-decodes `.path`, which would hide the
        # very escaping this asserts.
        assert seen[0].url.raw_path == f"{MONITOR_PATH}/1%3Fforce%3Dtrue".encode()
        assert "force" not in seen[0].url.params
