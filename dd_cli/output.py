"""Output contract: an error must never be representable as data.

The rule this module enforces:

    A failed request must never surface as ``0``, ``[]``, ``null``, or a short
    page. If a call fails, fail loudly with a non-zero exit. If a result is
    incomplete, mark it.

The non-obvious part is *why* stdout matters. On failure the CLI used to write
to stderr and exit non-zero, leaving **stdout empty**. A shell caller then does::

    n=$(dd-cli search-logs ... | jq '.count')   # empty stdin -> no output, exit 0
    total=$(( total + ${n:-0} ))                # <- the 0 is manufactured here

The pipeline's exit status is ``jq``'s, not dd-cli's. So a failure that prints
nothing is a zero generator. Every data-producing command therefore emits a
parseable envelope on stdout even when it fails.

Known limit: no value printed on stdout can defeat a caller doing shell
arithmetic on scraped text -- bash evaluates ``$((0 + null))`` as 0. The
envelope protects consumers that read *structure* (agents, ``jq -e``), which is
this tool's actual audience.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

import click

SCHEMA_VERSION = 2

#: Exit code meaning "the command worked, but the answer is incomplete".
EXIT_TRUNCATED = 3

# Truncation reasons. The boolean is deliberately conservative -- it is true
# whenever a cap bit -- and the precision lives here instead.
#: A cursor was still outstanding. We are *certain* more data exists.
REASON_MORE_AVAILABLE = "more_available"
#: The page cap bit while a cursor was still outstanding.
REASON_MAX_PAGES = "max_pages"
#: A page/offset paginator hit its cap. Landing exactly on a page boundary is
#: indistinguishable from more-data-exists without spending another request, so
#: this reason does not claim to know which happened.
REASON_MAX_RESULTS_UNKNOWN = "max_results_boundary_unknown"
#: Datadog returned HTTP 200 with a short body (server-side query timeout).
#: Immune to retries and to cap detection -- there is no error and no cap.
REASON_SERVER_TIMEOUT = "server_timeout"


class PagedResult(NamedTuple):
    """The outcome of a paginated fetch, including whether it is complete."""

    items: list[Any]
    truncated: bool = False
    truncation_reason: str | None = None
    pages_fetched: int = 0
    next_cursor: str | None = None
    warnings: list[Any] | None = None

    def limited(self, max_results: int | None, reason: str) -> PagedResult:
        """Slice to ``max_results``, marking truncation if that dropped data."""
        if max_results is None or len(self.items) <= max_results:
            return self
        return self._replace(
            items=self.items[:max_results],
            truncated=True,
            truncation_reason=self.truncation_reason or reason,
        )


def warn(message: str) -> None:
    click.echo(f"dd-cli: {message}", err=True)


def failure_envelope(
    error: Exception,
    *,
    status: int | None = None,
    attempts: int | None = None,
    elapsed_s: float | None = None,
    body: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the envelope printed on stdout when a command fails.

    ``data`` and ``count`` are ``None``, never ``[]`` or ``0``: a zero is a
    claim about the world, and a failed request has made no such observation.
    """
    detail: dict[str, Any] = {"message": str(error)}
    if status is not None:
        detail["status"] = status
    if attempts is not None:
        detail["attempts"] = attempts
    if elapsed_s is not None:
        detail["elapsed_s"] = round(elapsed_s, 3)
    if body:
        detail["body"] = body
    payload: dict[str, Any] = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "data": None,
        "count": None,
        "truncated": None,
        "truncation_reason": None,
        "error": detail,
    }
    if extra:
        payload.update(extra)
    return payload


def success_envelope(
    data: Any,
    *,
    result: PagedResult | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "count": len(data) if isinstance(data, list) else None,
        "truncated": bool(result.truncated) if result else False,
        "truncation_reason": result.truncation_reason if result else None,
        "data": data,
    }
    if result and result.warnings:
        payload["warnings"] = result.warnings
    if extra:
        payload.update(extra)
    return payload


def emit(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, indent=2))


def finish(
    result: PagedResult,
    payload: dict[str, Any] | None,
    *,
    on_truncation: str,
    describe: str = "result",
) -> None:
    """Emit the payload, warn about truncation, and apply the exit behaviour.

    Emission happens here rather than in the formatter so that
    ``--on-truncation error`` can mark the envelope ``ok: false`` instead of
    printing ``ok: true`` and then failing.

    ``payload`` is None for text formats (``jsonl``/``messages``), whose records
    have already been streamed; those signal only out of band.
    """
    failing = result.truncated and on_truncation == "error"

    if payload is not None:
        if failing:
            payload = {
                **payload,
                "ok": False,
                "error": {
                    "message": (f"result is incomplete ({result.truncation_reason})"),
                    "truncation_reason": result.truncation_reason,
                },
            }
        emit(payload)

    if not result.truncated:
        return

    warn(
        f"{describe} is TRUNCATED ({result.truncation_reason}): "
        f"returned {len(result.items)} record(s); more data exists or may exist. "
        f"Do not treat this count as a total."
    )

    if on_truncation == "warn":
        return
    raise SystemExit(1 if failing else EXIT_TRUNCATED)
