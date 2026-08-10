"""Collision analysis for the quoted anchor phrases inside log-metric filters.

Why this module exists
----------------------

A Datadog log-based metric filters the *ingest stream* with a log-search query.
When that query pins a literal phrase -- ``"Refusing to reserve inventory"`` -- the
match is a **case-insensitive substring** of the log line, evaluated at
**intake**. Two consequences follow, and both are silent:

1. Any log string that merely *contains* another metric's anchor phrase is
   counted by that metric. The producing service never learns it happened.
2. Because the metric is computed at intake, the contamination is permanent.
   Rewording the log fixes the future and cannot fix the history.

This is not hypothetical. A service introduced a log line for a *retirement*
code path whose wording happened to contain the quoted anchor of an unrelated
metric counting a *no-op* code path. Tens of thousands of retirement events
were counted as no-ops before anyone noticed, and that metric's history is
contaminated for good.

So the check has to happen *before* a new log string ships, and it has to run
in **both** directions: an existing anchor contained in the candidate (the
candidate feeds someone else's counter) and the candidate contained in an
existing anchor (a metric you build on the candidate would swallow their
events).

Matching rules
--------------

Comparison is case-insensitive substring, matching Datadog's own semantics.
Phrases carrying ``*`` are treated as wildcard patterns: the literal segments
between the stars must appear **in order**. That is deliberately generous --
this module's job is to raise suspicion cheaply, and a false positive costs a
human ten seconds while a false negative costs a contaminated metric.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

#: Direction: an existing metric's anchor is contained in the candidate, so
#: shipping the candidate string starts feeding that metric.
ANCHOR_IN_CANDIDATE = "anchor_in_candidate"
#: Direction: the candidate is contained in an existing metric's anchor, so a
#: metric built on the candidate would also count that metric's events.
CANDIDATE_IN_ANCHOR = "candidate_in_anchor"

# Double-quoted spans, honouring a backslash-escaped quote inside them. Datadog
# log search uses double quotes for literal phrases; a bare token is not an
# anchor in the sense this module cares about (it is tokenized, not substring
# matched), so single quotes and bare words are deliberately not harvested.
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def extract_quoted_phrases(query: str | None) -> list[str]:
    """Return the literal phrases quoted inside a log-search query.

    Order is preserved and duplicates are kept, so a caller can report how many
    phrases a single filter carries. Empty quotes (``""``) are dropped: they
    anchor nothing.
    """
    if not query:
        return []
    phrases: list[str] = []
    for raw in _QUOTED.findall(query):
        # Reported verbatim (minus the backslash escaping), NOT stripped: the
        # phrase printed in a collision report has to be the text a human can
        # find in the filter. Normalisation belongs in the matcher, not here.
        phrase = raw.replace('\\"', '"')
        if phrase.strip():
            phrases.append(phrase)
    return phrases


def _segments(phrase: str) -> list[str]:
    """Literal segments of a wildcard phrase, lowercased, stars removed.

    Each segment is stripped, so ``"foo * bar"`` looks for "foo" then "bar"
    rather than for "foo " immediately followed by " bar". Without that, a
    wildcard standing in for zero characters between two spaces produces a
    false NEGATIVE -- the one direction of error this module must not make.
    """
    return [stripped for seg in phrase.lower().split("*") if (stripped := seg.strip())]


def _contains_in_order(haystack: str, segments: list[str]) -> bool:
    pos = 0
    for seg in segments:
        found = haystack.find(seg, pos)
        if found < 0:
            return False
        pos = found + len(seg)
    return True


def collision_direction(candidate: str, phrase: str) -> str | None:
    """How ``phrase`` and ``candidate`` collide, or None if they do not.

    Returns :data:`ANCHOR_IN_CANDIDATE`, :data:`CANDIDATE_IN_ANCHOR`, or None.
    When both hold (identical strings, or a phrase that is a pure wildcard),
    :data:`ANCHOR_IN_CANDIDATE` wins: that is the direction that silently
    contaminates an *existing* metric, which is the worse outcome.
    """
    cand = candidate.strip().lower()
    if not cand:
        return None
    segs = _segments(phrase)
    if not segs:
        return None

    if _contains_in_order(cand, segs):
        return ANCHOR_IN_CANDIDATE
    if any(cand in seg for seg in segs):
        return CANDIDATE_IN_ANCHOR
    return None


class Anchor(NamedTuple):
    """One quoted phrase, and the metric whose filter it came from."""

    metric_id: str
    query: str
    phrase: str

    @property
    def wildcard(self) -> bool:
        return "*" in self.phrase


def harvest_anchors(metrics: list[dict[str, Any]]) -> list[Anchor]:
    """Pull every quoted phrase out of every metric's filter query.

    ``metrics`` are raw ``logs_metrics`` resource objects as Datadog returns
    them. A metric whose filter carries no quoted phrase contributes nothing --
    it is still counted in the denominator by the caller, because "checked 70
    metrics" and "checked 70 phrases" are different claims.
    """
    anchors: list[Anchor] = []
    for metric in metrics:
        metric_id = str(metric.get("id", ""))
        query = ((metric.get("attributes") or {}).get("filter") or {}).get(
            "query"
        ) or ""
        for phrase in extract_quoted_phrases(query):
            anchors.append(Anchor(metric_id=metric_id, query=query, phrase=phrase))
    return anchors


def find_collisions(candidate: str, anchors: list[Anchor]) -> list[dict[str, Any]]:
    """Every anchor that collides with ``candidate``, in either direction."""
    hits: list[dict[str, Any]] = []
    for anchor in anchors:
        direction = collision_direction(candidate, anchor.phrase)
        if direction is None:
            continue
        hits.append(
            {
                "metric_id": anchor.metric_id,
                "phrase": anchor.phrase,
                "filter_query": anchor.query,
                "direction": direction,
                "wildcard": anchor.wildcard,
                "explanation": (
                    f"the existing anchor {anchor.phrase!r} of metric "
                    f"{anchor.metric_id!r} occurs inside the candidate string, "
                    f"so shipping it would feed that metric at intake"
                    if direction == ANCHOR_IN_CANDIDATE
                    else f"the candidate occurs inside the anchor "
                    f"{anchor.phrase!r} of metric {anchor.metric_id!r}, so a "
                    f"metric filtering on the candidate would also count that "
                    f"metric's events"
                ),
            }
        )
    return hits
