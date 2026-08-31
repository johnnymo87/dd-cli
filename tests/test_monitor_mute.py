"""Tests for `dd-cli mute-monitor` / `unmute-monitor`, and for the
`update-monitor` path that used to mute a monitor with no way back.

The incident these pin: muting had to go through the deprecated
`update-monitor --option 'silenced={"*": <epoch>}'`, and *unmuting* was
impossible -- `--option silenced=null` and `--option 'silenced={}'` both
returned 200, left `silenced` untouched, and exited 0. A silent no-op is the
worst shape available here, because the operator walks away believing a paging
monitor is alerting again while it is still gagged.

So the properties worth pinning are not "does the verb work":

**A success claim must rest on the artifact, not on the status code.** Every
test that asserts a mute or an unmute succeeded also has a sibling where the
server answers 200 and does *not* change `options.silenced`. Those must fail.

**An indefinite mute must be asked for out loud.** A mute with no expiry
removes alert coverage with no moment at which anyone finds out.

**A wrong expiry is a wrong answer, not a rounding error.** A server that
accepts the mute but records no expiry has muted a prod monitor forever; that
must fail rather than print a reassuring end time it did not get.

The fake API is driven through a real httpx transport, not a MagicMock: a mock
records whichever kwarg it is handed and reports success, which cannot see an
`end` that never made it onto the wire, nor a PUT that Datadog ignored.
"""

from __future__ import annotations

import datetime
import json
import re
from functools import partial
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from dd_cli.cli import cli
from dd_cli.http import DatadogClient

MONITOR_PATH = "/api/v1/monitor"
MONITOR_ID = 25447403

# The monitor from the incident: a paging monitor that was muted through the
# legacy `silenced` field and could not be unmuted again.
FIXTURE_MONITOR: dict[str, Any] = {
    "id": MONITOR_ID,
    "name": "[prod] checkout error rate",
    "type": "query alert",
    "query": "sum(last_5m):sum:checkout.errors{env:prod}.as_count() > 25",
    "message": "Checkout errors @slack-checkout-alerts",
    "tags": ["team:checkout", "managed-by:dd-cli"],
    "options": {"thresholds": {"critical": 25}, "silenced": {}},
}


#: Distinguishes "use the fixture monitor" from "this monitor does not exist".
_DEFAULT: Any = object()


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
    """A fake monitor API that models Datadog's real mute semantics.

    Notably: the PUT *cannot* clear `silenced`. That is not a simplification,
    it is the behaviour verified against the live API (monitor 25447403) and
    the whole reason these commands exist.
    """

    def __init__(
        self,
        monitor: dict[str, Any] | None = _DEFAULT,
        *,
        honor_end: bool = True,
        honor_mute: bool = True,
        honor_unmute: bool = True,
        mute_status: int = 200,
        mute_errors: list[str] | None = None,
        silenced: dict[str, Any] | None = None,
    ) -> None:
        base = FIXTURE_MONITOR if monitor is _DEFAULT else monitor
        self.monitor = json.loads(json.dumps(base)) if base else None
        if self.monitor is not None and silenced is not None:
            self.monitor["options"]["silenced"] = dict(silenced)
        self.honor_end = honor_end
        self.honor_mute = honor_mute
        self.honor_unmute = honor_unmute
        self.mute_status = mute_status
        self.mute_errors = mute_errors
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def request(self, suffix: str) -> httpx.Request:
        return next(r for r in self.requests if r.url.path.endswith(suffix))

    def body(self, suffix: str) -> Any:
        for request, body in zip(self.requests, self.bodies, strict=False):
            if request.url.path.endswith(suffix):
                return body
        raise AssertionError(f"no request to {suffix}")

    @property
    def silenced(self) -> dict[str, Any]:
        assert self.monitor is not None
        return self.monitor["options"]["silenced"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.content
        self.bodies.append(json.loads(raw) if raw else None)
        path = request.url.path

        if self.monitor is None:
            return httpx.Response(404, json={"errors": ["Monitor not found"]})

        if request.method == "GET":
            return httpx.Response(200, json=self.monitor)

        if request.method == "POST" and path.endswith("/mute"):
            if self.mute_errors is not None:
                return httpx.Response(
                    self.mute_status, json={"errors": self.mute_errors}
                )
            body = self.bodies[-1] or {}
            if self.honor_mute:
                scope = body.get("scope", "*")
                end = body.get("end") if self.honor_end else None
                self.monitor["options"]["silenced"][scope] = end
            return httpx.Response(200, json=self.monitor)

        if request.method == "POST" and path.endswith("/unmute"):
            if self.mute_errors is not None:
                return httpx.Response(
                    self.mute_status, json={"errors": self.mute_errors}
                )
            body = self.bodies[-1] or {}
            if self.honor_unmute:
                scope = body.get("scope")
                if scope is None:
                    self.monitor["options"]["silenced"] = {}
                else:
                    self.monitor["options"]["silenced"].pop(scope, None)
            return httpx.Response(200, json=self.monitor)

        if request.method == "PUT":
            body = self.bodies[-1] or {}
            options = dict(body.get("options") or {})
            # Datadog's PUT will not clear a mute: whatever `silenced` the
            # caller sends, the stored value is kept.
            options["silenced"] = self.monitor["options"].get("silenced", {})
            self.monitor = {**self.monitor, **body, "options": options}
            return httpx.Response(200, json=self.monitor)

        raise AssertionError(f"unexpected {request.method} {path}")


def run(runner: CliRunner, api: FakeMonitorAPI, args: list[str]):
    with patch(
        "dd_cli.cli.DatadogClient", partial(DatadogClient, transport=api.transport)
    ):
        return runner.invoke(cli, args)


def envelope(result) -> dict[str, Any]:
    return json.loads(result.stdout)


def epoch(iso: str) -> int:
    return int(datetime.datetime.fromisoformat(iso).timestamp())


class TestMuteRequiresAnExpiry:
    """An un-expiring mute is how coverage disappears permanently."""

    def test_refuses_a_mute_with_no_expiry(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID)])

        assert result.exit_code != 0
        assert "--forever" in result.output
        # Nothing was muted: the refusal happens before any request.
        assert api.requests == []

    def test_forever_is_accepted_when_asked_for_explicitly(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--forever"])

        assert result.exit_code == 0, result.output
        assert api.body("/mute") is None or "end" not in api.body("/mute")
        assert api.silenced == {"*": None}
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["data"]["indefinite"] is True
        assert "NO EXPIRY" in result.stderr

    def test_until_and_forever_contradict_each_other(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "4h", "--forever"],
        )

        assert result.exit_code != 0
        assert api.requests == []


class TestExpiryParsing:
    def test_relative_duration_becomes_a_future_epoch(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "4h"])

        assert result.exit_code == 0, result.output
        end = api.body("/mute")["end"]
        now = int(datetime.datetime.now(tz=datetime.UTC).timestamp())
        assert 4 * 3600 - 60 <= end - now <= 4 * 3600 + 60

    def test_iso_timestamp_becomes_the_epoch_datadog_wants(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "2099-09-08T00:00:00Z"],
        )

        assert result.exit_code == 0, result.output
        assert api.body("/mute")["end"] == epoch("2099-09-08T00:00:00+00:00")
        payload = envelope(result)
        assert payload["data"]["applied_end_utc"] == "2099-09-08T00:00:00Z"

    def test_an_offset_is_honoured_not_dropped(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "2099-09-08T00:00:00+02:00"],
        )

        assert result.exit_code == 0, result.output
        assert api.body("/mute")["end"] == epoch("2099-09-08T00:00:00+02:00")

    def test_epoch_seconds_pass_through(self, runner, mock_env):
        api = FakeMonitorAPI()
        end = epoch("2099-09-08T00:00:00+00:00")

        result = run(
            runner, api, ["mute-monitor", str(MONITOR_ID), "--until", str(end)]
        )

        assert result.exit_code == 0, result.output
        assert api.body("/mute")["end"] == end

    def test_a_naive_timestamp_is_refused_rather_than_guessed(self, runner, mock_env):
        """'Midnight' is a different instant in every timezone, and guessing
        wrong means the monitor comes back hours early or late."""
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "2099-09-08T00:00:00"],
        )

        assert result.exit_code != 0
        assert "timezone" in result.output
        assert api.requests == []

    def test_a_past_expiry_is_refused(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "2020-01-01T00:00:00Z"],
        )

        assert result.exit_code != 0
        assert "past" in result.output
        assert api.requests == []

    def test_epoch_milliseconds_are_refused(self, runner, mock_env):
        """A ms timestamp in a seconds flag is a mute until the year 57000."""
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "1788000000000"],
        )

        assert result.exit_code != 0
        assert "milliseconds" in result.output
        assert api.requests == []

    def test_gibberish_is_refused(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "next tuesday"]
        )

        assert result.exit_code != 0
        assert api.requests == []


class TestMuteVerifiesTheArtifact:
    def test_reads_the_monitor_back_after_muting(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "4h"])

        assert result.exit_code == 0, result.output
        assert api.paths() == [
            f"POST {MONITOR_PATH}/{MONITOR_ID}/mute",
            f"GET {MONITOR_PATH}/{MONITOR_ID}",
        ]
        payload = envelope(result)
        assert payload["data"]["applied_end"] == api.silenced["*"]
        assert payload["data"]["applied_end_utc"].endswith("Z")

    def test_a_200_that_did_not_mute_is_a_failure(self, runner, mock_env):
        """The status code is not the artifact. A monitor that is still
        alerting must not be reported as muted."""
        api = FakeMonitorAPI(honor_mute=False)

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "4h"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "not muted" in payload["error"]["message"]

    def test_a_mute_that_ignored_the_expiry_is_a_failure(self, runner, mock_env):
        """Muted forever when an expiry was asked for is the dangerous
        direction: nothing later brings the monitor back."""
        api = FakeMonitorAPI(honor_end=False)

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "4h"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "INDEFINITELY" in payload["error"]["message"]
        assert "unmute-monitor" in payload["error"]["message"]

    def test_a_failed_read_back_does_not_claim_success(self, runner, mock_env):
        """The write landed and its result is unknown. Saying so beats both a
        success claim and a bare failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=FIXTURE_MONITOR)
            return httpx.Response(403, json={"errors": ["Forbidden"]})

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(
                cli, ["mute-monitor", str(MONITOR_ID), "--until", "4h"]
            )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "unknown" in payload["hint"]

    def test_an_unreadable_monitor_is_not_read_as_unmuted(self, runner, mock_env):
        """A shape we cannot parse is not an observation of 'no mute'."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=FIXTURE_MONITOR)
            return httpx.Response(200, json=[1, 2, 3])

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(
                cli, ["mute-monitor", str(MONITOR_ID), "--until", "4h"]
            )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "options" in payload["error"]["message"]


class TestMuteScope:
    def test_scope_is_sent_and_verified(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            [
                "mute-monitor",
                str(MONITOR_ID),
                "--until",
                "2h",
                "--scope",
                "host:web-01",
            ],
        )

        assert result.exit_code == 0, result.output
        assert api.body("/mute")["scope"] == "host:web-01"
        assert set(api.silenced) == {"host:web-01"}
        payload = envelope(result)
        assert payload["data"]["scope"] == "host:web-01"

    def test_a_scoped_mute_that_landed_on_the_wrong_scope_fails(self, runner, mock_env):
        """A server that ignores `scope` mutes the WHOLE monitor. Reporting
        that as a narrow mute would overstate the coverage that remains."""

        class IgnoresScope(FakeMonitorAPI):
            def handle(self, request: httpx.Request) -> httpx.Response:
                if request.method == "POST" and request.url.path.endswith("/mute"):
                    assert self.monitor is not None
                    self.requests.append(request)
                    self.bodies.append(json.loads(request.content))
                    self.monitor["options"]["silenced"] = {"*": 1788000000}
                    return httpx.Response(200, json=self.monitor)
                return super().handle(request)

        api = IgnoresScope()

        result = run(
            runner,
            api,
            [
                "mute-monitor",
                str(MONITOR_ID),
                "--until",
                "2h",
                "--scope",
                "host:web-01",
            ],
        )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "not muted on scope" in payload["error"]["message"]

    def test_an_empty_scope_is_refused(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["mute-monitor", str(MONITOR_ID), "--until", "2h", "--scope", "  "],
        )

        assert result.exit_code != 0
        assert api.requests == []


class TestForeverIsAlsoVerified:
    def test_a_forever_mute_that_came_back_with_an_expiry_fails(self, runner, mock_env):
        """--forever asked for a mute nothing lifts on its own. A monitor that
        un-mutes itself at an hour nobody chose is not that, and the operator
        who typed --forever is the one least likely to be watching for it."""

        class AddsAnExpiry(FakeMonitorAPI):
            def handle(self, request: httpx.Request) -> httpx.Response:
                if request.method == "POST" and request.url.path.endswith("/mute"):
                    assert self.monitor is not None
                    self.requests.append(request)
                    self.bodies.append(
                        json.loads(request.content) if request.content else None
                    )
                    self.monitor["options"]["silenced"] = {"*": 1788000000}
                    return httpx.Response(200, json=self.monitor)
                return super().handle(request)

        api = AddsAnExpiry()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--forever"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "no expiry (--forever)" in payload["error"]["message"]

    def test_a_boolean_in_the_mute_map_is_not_read_as_an_expiry(self, runner, mock_env):
        """`bool` is a subclass of `int`: a `true` here would otherwise render
        as 1970-01-01 and read as a real (long-past) expiry."""

        class AnswersTrue(FakeMonitorAPI):
            def handle(self, request: httpx.Request) -> httpx.Response:
                if request.method == "POST" and request.url.path.endswith("/mute"):
                    assert self.monitor is not None
                    self.requests.append(request)
                    self.bodies.append(
                        json.loads(request.content) if request.content else None
                    )
                    self.monitor["options"]["silenced"] = {"*": True}
                    return httpx.Response(200, json=self.monitor)
                return super().handle(request)

        api = AnswersTrue()

        result = run(runner, api, ["mute-monitor", str(MONITOR_ID), "--until", "4h"])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "not an expiry at all" in payload["error"]["message"]
        assert "1970" not in payload["error"]["message"]


class TestUnmute:
    def test_unmutes_and_verifies(self, runner, mock_env):
        api = FakeMonitorAPI(silenced={"*": 1788000000})

        result = run(runner, api, ["unmute-monitor", str(MONITOR_ID)])

        assert result.exit_code == 0, result.output
        assert api.paths() == [
            f"GET {MONITOR_PATH}/{MONITOR_ID}",
            f"POST {MONITOR_PATH}/{MONITOR_ID}/unmute",
            f"GET {MONITOR_PATH}/{MONITOR_ID}",
        ]
        assert api.silenced == {}
        payload = envelope(result)
        assert payload["ok"] is True
        assert payload["data"]["was_muted"] is True
        assert payload["data"]["still_muted_scopes"] == []
        assert payload["data"]["silenced_before"] == [
            {
                "scope": "*",
                "end": 1788000000,
                "end_utc": "2026-08-29T10:40:00Z",
                "indefinite": False,
            }
        ]
        assert "alerting again" in result.stderr

    def test_a_200_that_left_the_monitor_muted_is_a_failure(self, runner, mock_env):
        """The incident, one level up: a success that leaves a prod monitor
        gagged is worse than an error, because the operator acts on it."""
        api = FakeMonitorAPI(silenced={"*": 1788000000}, honor_unmute=False)

        result = run(runner, api, ["unmute-monitor", str(MONITOR_ID)])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "STILL MUTED" in payload["error"]["message"]

    def test_unmuting_one_scope_leaves_the_others(self, runner, mock_env):
        api = FakeMonitorAPI(silenced={"host:web-01": 1788000000, "env:staging": None})

        result = run(
            runner,
            api,
            ["unmute-monitor", str(MONITOR_ID), "--scope", "host:web-01"],
        )

        assert result.exit_code == 0, result.output
        assert api.body("/unmute")["scope"] == "host:web-01"
        assert set(api.silenced) == {"env:staging"}
        payload = envelope(result)
        assert payload["data"]["still_muted_scopes"] == []
        assert "env:staging" in result.stderr

    def test_unmuting_everything_asks_for_all_scopes(self, runner, mock_env):
        api = FakeMonitorAPI(silenced={"host:web-01": 1788000000, "*": None})

        result = run(runner, api, ["unmute-monitor", str(MONITOR_ID)])

        assert result.exit_code == 0, result.output
        assert api.body("/unmute")["all_scopes"] is True
        assert api.silenced == {}

    def test_an_unmute_of_a_monitor_that_was_not_muted_says_so(self, runner, mock_env):
        """Idempotent, but not silently: 'it was already unmuted' and 'I
        unmuted it' are different facts."""
        api = FakeMonitorAPI()

        result = run(runner, api, ["unmute-monitor", str(MONITOR_ID)])

        assert result.exit_code == 0, result.output
        payload = envelope(result)
        assert payload["data"]["was_muted"] is False
        assert "was not muted" in result.stderr

    def test_a_wildcard_mute_still_gags_the_scope_that_was_unmuted(
        self, runner, mock_env
    ):
        """Unmuting `host:web-01` while the whole monitor is muted on `*`
        changes nothing that pages. Reporting it as a success would tell an
        operator that a group is alerting again when it is not."""
        api = FakeMonitorAPI(silenced={"*": 1788000000})

        result = run(
            runner,
            api,
            ["unmute-monitor", str(MONITOR_ID), "--scope", "host:web-01"],
        )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "STILL MUTED" in payload["error"]["message"]
        assert "without --scope" in payload["error"]["message"]

    def test_a_failed_unmute_carries_what_was_muted_before_it(self, runner, mock_env):
        """The POST failed, so the monitor is still muted -- and the envelope
        has to say what it is still muted on."""
        api = FakeMonitorAPI(
            silenced={"*": 1788000000}, mute_status=500, mute_errors=["Internal error"]
        )

        result = run(runner, api, ["unmute-monitor", str(MONITOR_ID)])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["silenced_before"][0]["scope"] == "*"

    def test_scope_and_all_scopes_contradict_each_other(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            [
                "unmute-monitor",
                str(MONITOR_ID),
                "--scope",
                "host:web-01",
                "--all-scopes",
            ],
        )

        assert result.exit_code != 0
        assert api.requests == []

    @pytest.mark.parametrize(
        ("command", "extra"),
        [("mute-monitor", ["--until", "4h"]), ("unmute-monitor", [])],
    )
    def test_404_is_an_error_not_a_quiet_success(
        self, runner, mock_env, command, extra
    ):
        """The same 404 covers 'wrong ID' and 'DD_SITE points at the wrong
        region while the monitor is alive -- and alerting -- in another one'."""
        api = FakeMonitorAPI(monitor=None)

        result = run(runner, api, [command, str(MONITOR_ID), *extra])

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert "wrong Datadog region" in payload["hint"]
        assert "Nothing was muted or unmuted" in payload["hint"]


class TestRefParsing:
    @pytest.mark.parametrize("command", ["mute-monitor", "unmute-monitor"])
    def test_accepts_a_monitor_url(self, runner, mock_env, command):
        api = FakeMonitorAPI()
        url = f"https://us3.datadoghq.com/monitors/{MONITOR_ID}?group=env%3Aprod"
        args = [command, url] + (["--until", "4h"] if command == "mute-monitor" else [])

        result = run(runner, api, args)

        assert result.exit_code == 0, result.output
        assert all(
            re.match(rf"^{MONITOR_PATH}/{MONITOR_ID}(/(un)?mute)?$", r.url.path)
            for r in api.requests
        )

    @pytest.mark.parametrize("command", ["mute-monitor", "unmute-monitor"])
    @pytest.mark.parametrize(
        "ref",
        [
            "manage",
            "25447403?all_scopes=true",
            "25447403/../../dashboard/abc",
            "",
        ],
    )
    def test_a_ref_that_is_not_an_id_is_refused_before_any_request(
        self, runner, mock_env, command, ref
    ):
        api = FakeMonitorAPI()
        args = [command, ref] + (["--until", "4h"] if command == "mute-monitor" else [])

        result = run(runner, api, args)

        assert result.exit_code != 0
        assert api.requests == []


class TestUpdateMonitorRefusesTheSilentNoOp:
    """`update-monitor --option silenced=...` is the path from the incident."""

    @pytest.mark.parametrize(
        "value",
        [
            "silenced=null",
            "silenced={}",
            'silenced={"*": 1788000000}',
            'silenced={"host:web-01": null}',
        ],
    )
    def test_silenced_is_refused_and_names_the_command_that_works(
        self, runner, mock_env, value
    ):
        api = FakeMonitorAPI(silenced={"*": 1788000000})

        result = run(
            runner, api, ["update-monitor", str(MONITOR_ID), "--option", value]
        )

        assert result.exit_code != 0
        assert "unmute-monitor" in result.output
        assert "mute-monitor" in result.output
        # And, crucially, nothing was sent: the old behaviour was a PUT that
        # returned 200 having changed nothing.
        assert api.requests == []

    def test_the_pre_fix_behaviour_is_what_the_fake_api_does(self, runner, mock_env):
        """Pin the server behaviour the refusal is based on.

        If Datadog ever starts honouring `silenced` on the PUT, this test is
        the one that should be revisited first -- it is the assumption, stated.
        """
        api = FakeMonitorAPI(silenced={"*": 1788000000})

        result = run(
            runner,
            api,
            ["update-monitor", str(MONITOR_ID), "--option", "renotify_interval=30"],
        )

        assert result.exit_code == 0, result.output
        assert api.silenced == {"*": 1788000000}

    def test_device_ids_is_refused_as_read_only(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["update-monitor", str(MONITOR_ID), "--option", 'device_ids=["mobile"]'],
        )

        assert result.exit_code != 0
        assert "read-only" in result.output
        assert api.requests == []

    def test_create_monitor_refuses_silenced_too(self, runner, mock_env):
        """A monitor created already muted could never be unmuted by the same
        mechanism, and mute-monitor exists for the case where that is meant."""
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            [
                "create-monitor",
                "--name",
                "x",
                "--type",
                "query alert",
                "--query",
                "sum(last_5m):sum:x{*}.as_count() > 1",
                "--message",
                "m",
                "--option",
                "silenced={}",
            ],
        )

        assert result.exit_code != 0
        assert api.requests == []


class TestUpdateMonitorVerifiesWhatItSent:
    """The generic backstop: an option that did not take must not read as
    success, even for a key nobody has catalogued yet."""

    def test_an_ignored_option_fails_instead_of_reporting_success(
        self, runner, mock_env
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=FIXTURE_MONITOR)
            # A PUT that answers 200 while dropping the change on the floor.
            return httpx.Response(200, json=FIXTURE_MONITOR)

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(
                cli,
                ["update-monitor", str(MONITOR_ID), "--option", "renotify_interval=30"],
            )

        assert result.exit_code != 0
        payload = envelope(result)
        assert payload["ok"] is False
        assert payload["unapplied_options"] == ["renotify_interval"]

    def test_an_applied_option_is_a_plain_success(self, runner, mock_env):
        api = FakeMonitorAPI()

        result = run(
            runner,
            api,
            ["update-monitor", str(MONITOR_ID), "--option", "renotify_interval=30"],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["options"]["renotify_interval"] == 30

    def test_a_partial_threshold_patch_is_not_a_false_alarm(self, runner, mock_env):
        """--critical patches one threshold and Datadog echoes the whole
        object. Comparing the caller's fragment against that would fail a
        write that did land."""
        api = FakeMonitorAPI()

        result = run(
            runner, api, ["update-monitor", str(MONITOR_ID), "--critical", "5"]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["options"]["thresholds"]["critical"] == 5

    def test_clearing_an_option_with_null_is_not_a_false_alarm(self, runner, mock_env):
        """Datadog answers a cleared option by omitting the key, not by
        echoing null."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        **FIXTURE_MONITOR,
                        "options": {"evaluation_delay": 300, "silenced": {}},
                    },
                )
            body = json.loads(request.content)
            options = {
                k: v for k, v in (body.get("options") or {}).items() if v is not None
            }
            return httpx.Response(200, json={**FIXTURE_MONITOR, "options": options})

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(
                cli,
                [
                    "update-monitor",
                    str(MONITOR_ID),
                    "--option",
                    "evaluation_delay=null",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "evaluation_delay" not in json.loads(result.stdout)["options"]

    def test_a_response_without_options_does_not_manufacture_a_failure(
        self, runner, mock_env
    ):
        """A shape we cannot read is not evidence the write was ignored."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json=FIXTURE_MONITOR)
            return httpx.Response(200, json={"id": MONITOR_ID})

        with patch(
            "dd_cli.cli.DatadogClient",
            partial(DatadogClient, transport=httpx.MockTransport(handler)),
        ):
            result = runner.invoke(
                cli,
                ["update-monitor", str(MONITOR_ID), "--option", "renotify_interval=30"],
            )

        assert result.exit_code == 0, result.output
