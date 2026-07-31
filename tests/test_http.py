"""Tests for dd_cli.http -- retry, backoff, and error-never-as-data guarantees.

These drive the retry layer directly through httpx.MockTransport rather than
patching DatadogClient wholesale, so the transport-level behaviour is actually
exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from dd_cli.http import DatadogAPIError, DatadogClient


class FakeClock:
    """Deterministic clock: sleeping advances monotonic time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def make_client(handler, *, clock=None, **kwargs) -> DatadogClient:
    clock = clock or FakeClock()
    return DatadogClient(
        site="us3.datadoghq.com",
        pat="ddpat_test",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def responder(*responses):
    """Return a handler that yields the given responses in order."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def rate_limited(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        429,
        headers=headers or {},
        json={"errors": ["Too many requests"]},
    )


def ok(payload=None) -> httpx.Response:
    return httpx.Response(200, json=payload if payload is not None else {"data": []})


class TestRetryOn429:
    def test_retries_429_then_succeeds(self):
        handler = responder(rate_limited(), rate_limited(), ok({"data": [1]}))
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        result = dd._read("GET", "/api/v2/thing")

        assert result == {"data": [1]}
        assert handler.calls["n"] == 3
        assert len(clock.slept) == 2

    def test_honors_retry_after_seconds(self):
        handler = responder(rate_limited({"Retry-After": "2"}), ok())
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        dd._read("GET", "/api/v2/thing")

        assert clock.slept == [2.0]

    def test_honors_retry_after_http_date(self):
        when = datetime.now(UTC) + timedelta(seconds=7)
        handler = responder(
            rate_limited({"Retry-After": format_datetime(when, usegmt=True)}), ok()
        )
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        dd._read("GET", "/api/v2/thing")

        assert len(clock.slept) == 1
        # HTTP-date resolution is whole seconds; allow a small window.
        assert 5.0 <= clock.slept[0] <= 8.0

    def test_honors_x_ratelimit_reset(self):
        handler = responder(rate_limited({"X-RateLimit-Reset": "3"}), ok())
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        dd._read("GET", "/api/v2/thing")

        assert clock.slept == [3.0]

    def test_retry_after_takes_precedence_over_ratelimit_reset(self):
        handler = responder(
            rate_limited({"Retry-After": "2", "X-RateLimit-Reset": "30"}), ok()
        )
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        dd._read("GET", "/api/v2/thing")

        assert clock.slept == [2.0]

    def test_hostile_retry_after_is_clamped(self):
        """A server (or proxy) must not be able to hang the CLI for a day."""
        handler = responder(rate_limited({"Retry-After": "86400"}), ok())
        clock = FakeClock()
        dd = make_client(handler, clock=clock, backoff_max=30.0)

        dd._read("GET", "/api/v2/thing")

        assert clock.slept == [30.0]

    def test_negative_retry_after_is_ignored(self):
        handler = responder(rate_limited({"Retry-After": "-5"}), ok())
        clock = FakeClock()
        dd = make_client(handler, clock=clock)

        dd._read("GET", "/api/v2/thing")

        assert clock.slept[0] >= 0.0

    def test_garbage_retry_after_falls_back_to_backoff(self):
        handler = responder(rate_limited({"Retry-After": "soon"}), ok())
        clock = FakeClock()
        dd = make_client(handler, clock=clock, backoff_base=0.5, backoff_max=30.0)

        dd._read("GET", "/api/v2/thing")

        assert len(clock.slept) == 1
        assert 0.0 <= clock.slept[0] <= 0.5

    def test_jitter_stays_within_exponential_bounds(self):
        handler = responder(
            rate_limited(), rate_limited(), rate_limited(), rate_limited(), ok()
        )
        clock = FakeClock()
        dd = make_client(handler, clock=clock, backoff_base=0.5, backoff_max=30.0)

        dd._read("GET", "/api/v2/thing")

        assert len(clock.slept) == 4
        for attempt, delay in enumerate(clock.slept):
            cap = min(30.0, 0.5 * (2**attempt))
            assert 0.0 <= delay <= cap


class TestRetryExhaustion:
    def test_exhaustion_raises_never_returns_empty(self):
        """The whole point: a persistent 429 must not become a zero."""
        handler = responder(rate_limited())
        dd = make_client(handler, max_retries=3)

        with pytest.raises(DatadogAPIError) as exc:
            dd._read("GET", "/api/v2/thing")

        assert exc.value.status_code == 429
        assert exc.value.attempts == 4  # initial + 3 retries

    def test_exhaustion_reports_attempts_and_elapsed(self):
        handler = responder(rate_limited({"Retry-After": "2"}))
        clock = FakeClock()
        dd = make_client(handler, clock=clock, max_retries=2)

        with pytest.raises(DatadogAPIError) as exc:
            dd._read("GET", "/api/v2/thing")

        assert exc.value.attempts == 3
        assert exc.value.elapsed_s == pytest.approx(4.0)
        assert "after 3 attempts" in str(exc.value)

    def test_retry_budget_stops_retrying(self):
        handler = responder(rate_limited({"Retry-After": "30"}))
        clock = FakeClock()
        dd = make_client(
            handler, clock=clock, max_retries=100, retry_budget=60.0, backoff_max=30.0
        )

        with pytest.raises(DatadogAPIError):
            dd._read("GET", "/api/v2/thing")

        assert clock.now <= 60.0

    def test_retry_budget_scales_with_timeout(self):
        """Flex users pass --timeout 120; a flat 120s budget would mean no retries."""
        dd = make_client(responder(ok()), timeout=120.0)
        assert dd.retry_budget >= 4 * 120.0

    def test_no_retry_disables_retrying(self):
        handler = responder(rate_limited())
        dd = make_client(handler, max_retries=0)

        with pytest.raises(DatadogAPIError) as exc:
            dd._read("GET", "/api/v2/thing")

        assert handler.calls["n"] == 1
        assert exc.value.attempts == 1


class TestRetryOn5xx:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_read_retries_5xx(self, status):
        handler = responder(httpx.Response(status, text="boom"), ok())
        dd = make_client(handler)

        dd._read("GET", "/api/v2/thing")

        assert handler.calls["n"] == 2

    def test_read_does_not_retry_4xx(self):
        handler = responder(httpx.Response(403, json={"errors": ["Forbidden"]}))
        dd = make_client(handler)

        with pytest.raises(DatadogAPIError) as exc:
            dd._read("GET", "/api/v2/thing")

        assert exc.value.status_code == 403
        assert handler.calls["n"] == 1

    def test_write_never_retries_5xx(self):
        """A 5xx on a write may mean the write landed. Never silently double-create."""
        handler = responder(httpx.Response(503, text="unavailable"), ok())
        dd = make_client(handler)

        with pytest.raises(DatadogAPIError) as exc:
            dd._write("POST", "/api/v1/monitor", json_body={"name": "x"})

        assert exc.value.status_code == 503
        assert handler.calls["n"] == 1


class TestWriteRetrySemantics:
    def test_write_retries_429_with_datadog_ratelimit_headers(self):
        handler = responder(
            rate_limited({"X-RateLimit-Reset": "1", "X-RateLimit-Limit": "100"}), ok()
        )
        dd = make_client(handler)

        dd._write("POST", "/api/v1/monitor", json_body={"name": "x"})

        assert handler.calls["n"] == 2

    def test_write_does_not_retry_bare_429(self):
        """A bare 429 may come from an intermediary AFTER the origin accepted."""
        handler = responder(httpx.Response(429, text="<html>rate limited</html>"), ok())
        dd = make_client(handler)

        with pytest.raises(DatadogAPIError) as exc:
            dd._write("POST", "/api/v1/monitor", json_body={"name": "x"})

        assert exc.value.status_code == 429
        assert handler.calls["n"] == 1

    def test_write_429_retry_is_capped_low(self):
        handler = responder(rate_limited({"X-RateLimit-Reset": "1"}))
        dd = make_client(handler, max_retries=10)

        with pytest.raises(DatadogAPIError) as exc:
            dd._write("POST", "/api/v1/monitor", json_body={"name": "x"})

        assert exc.value.attempts == 2


class TestNetworkErrorRetry:
    def test_read_retries_transport_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection reset", request=request)
            return ok()

        dd = make_client(handler)
        dd._read("GET", "/api/v2/thing")

        assert calls["n"] == 2

    def test_write_does_not_retry_transport_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            raise httpx.ConnectError("connection reset", request=request)

        dd = make_client(handler)
        with pytest.raises(RuntimeError):
            dd._write("POST", "/api/v1/monitor", json_body={"name": "x"})

        assert calls["n"] == 1


class TestRetryReporting:
    def test_on_retry_callback_receives_context(self):
        events = []
        handler = responder(rate_limited({"Retry-After": "2"}), ok())
        dd = make_client(handler, on_retry=events.append)

        dd._read("GET", "/api/v2/logs/events/search")

        assert len(events) == 1
        event = events[0]
        assert event.status == 429
        assert event.attempt == 1
        assert event.delay == 2.0
        assert "/api/v2/logs/events/search" in event.path


class TestNeverEmpty:
    def test_list_monitors_raises_on_non_list_response(self):
        """A non-list response must not become an empty list."""
        handler = responder(ok({"errors": ["something odd"]}))
        dd = make_client(handler)

        with pytest.raises(RuntimeError, match="expected a JSON array"):
            dd.list_monitors()

    def test_list_monitors_returns_list(self):
        handler = responder(httpx.Response(200, json=[{"id": 1}]))
        dd = make_client(handler)

        assert dd.list_monitors() == [{"id": 1}]

    def test_search_logs_uses_read_policy(self):
        """Log search is a POST but is a read -- it must retry."""
        handler = responder(rate_limited(), ok({"data": [], "meta": {}}))
        dd = make_client(handler)

        dd.search_logs(query="foo")

        assert handler.calls["n"] == 2

    def test_create_monitor_uses_write_policy(self):
        handler = responder(httpx.Response(503, text="nope"), ok())
        dd = make_client(handler)

        with pytest.raises(DatadogAPIError):
            dd.create_monitor(
                name="n", monitor_type="metric alert", query="q", message="m"
            )

        assert handler.calls["n"] == 1
