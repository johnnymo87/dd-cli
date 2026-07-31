# Errors Must Never Be Representable As Data

Date: 2026-07-31
Branch: `rate-limit-honesty`

## Problem

An API failure or a truncated read must never be representable as a zero, an
empty result, or a silently-short list. Two real incidents motivated this work.

**Incident 1.** An agent analyzing a failure population hour-by-hour concluded
the failures stopped 23 hours *before* the deploy that supposedly fixed them —
which would have meant the fix proved nothing. Root cause: a Datadog 429 inside
the agent's polling loop was recorded as a count of 0. It was caught only by
luck, by reconciling the hourly bucket sum (354) against an independently
measured 48h total (3,626).

**Incident 2.** A session probing the Logs Search API saw `hits=0` for both its
negative control and its target. Both were actually "Too many requests"
rendered as a zero. Datadog log search rate-limits hard: 429s on 4 of 6
sequential probes, up to 4 retries each.

## Mechanism (corrected)

The initial diagnosis was that dd-cli fabricates zeros. It does not.
`http.py::_request` is correct: it calls `raise_for_status()` and converts
4xx/5xx into `DatadogAPIError`.

The actual mechanism is the **process boundary**. On failure `_handle_api_error`
writes to stderr and exits 1, so **stdout is zero bytes**. A shell caller then
does:

```bash
n=$(dd-cli search-logs ... | jq '.count')   # jq reads empty stdin, prints nothing, exits 0
total=$(( total + ${n:-0} ))                # ← the 0 is manufactured HERE
```

The pipeline's exit status is `jq`'s, not dd-cli's. Empty stdout is the zero
factory.

Note the hard limit of any stdout-side fix: no value we can print defeats a
caller doing shell arithmetic on scraped text (`$((0 + null))` is 0; bash
treats bare identifiers as unset). The envelope below protects consumers that
read *structure* — agents, `jq -e` — which is dd-cli's actual audience. It does
not protect `${n:-0}`. The structural answer to that is to remove the reason to
write the loop (see `count-logs`).

## Design

### 1. Retry layer (`http.py`)

`_request` becomes a shared private core wrapped by two helpers:

- `_read()` — retries 429, 500/502/503/504, and `httpx.RequestError`.
- `_write()` — retries 429 **only when Datadog rate-limit headers are present**,
  capped at 2 attempts. Never retries 5xx.

Two helpers instead of an `idempotent=True` kwarg: a defaulted boolean across
~40 call sites rots silently, and a forgotten flag means quietly losing
retries — the exact failure family being outlawed. With two helpers there is no
default to forget and the call site documents intent.

`_write` checks headers because a 429 from an edge/WAF can be emitted *after*
the origin accepted a write. Status code alone cannot distinguish an origin
limiter from an intermediary.

Delay: honor `Retry-After` (integer-seconds and HTTP-date forms), then
`X-RateLimit-Reset`, else full-jitter exponential
`min(backoff_max, backoff_base * 2**attempt)`. Header values are clamped to
`backoff_max` so a hostile or absurd header cannot hang the CLI.

Budget: `retry_budget = max(120.0, 4 * timeout)`. A flat 120s ceiling would give
*zero* retries to flex users, who are told to pass `--timeout 120` — the
slowest and most rate-limited queries would be the least protected.

On exhaustion, raise `DatadogAPIError` carrying `attempts` and `elapsed_s`.
Never a zero. Each retry emits a stderr line.

Config: `--max-retries` / `DD_MAX_RETRIES`, `--no-retry`.

### 2. Honest envelope on stdout

For `--format json`, a failure emits a parseable object on **stdout** and still
exits non-zero:

```json
{"ok": false, "schema_version": 2, "data": null, "count": null,
 "truncated": null,
 "error": {"status": 429, "message": "...", "attempts": 5, "elapsed_s": 41.2}}
```

`schema_version` is explicit rather than inferred from the absence of a key.

**Scope boundary.** The failure envelope applies to every command. On the
*success* path, envelope keys (`ok`, `schema_version`, `truncated`,
`truncation_reason`) are added only to list/search/count commands, alongside the
existing `data`/`count` keys — additive and non-breaking. Single-resource
getters (`get-monitor`, `get-incident`, ...) keep their raw resource shape on
success; wrapping them would churn the test suite for little gain, since a
single-resource getter cannot manufacture a count. Detection rule for
consumers: `.ok == false` means failure; non-zero exit always means failure.

`jsonl` and `messages` are structurally incapable of honest failure signalling —
there is nowhere to put an envelope without corrupting the stream. Documented as
human / `set -o pipefail` use only.

### 3. HTTP 200 partial results

A flex query that times out server-side returns **200 with a short `data`
array**. `search-logs` currently reads `meta` only for the cursor. This is
immune to retries (no error), to exit codes (no cap hit), and to truncation
plumbing (no cap bit).

Treat `meta.status != "done"` or a non-empty `meta.warnings` as incomplete:
`truncated: true`, `truncation_reason: "server_timeout"`, warnings passed
through, stderr warning, exit 3.

### 4. Truncation as a first-class result

A `PagedResult` NamedTuple (`items`, `truncated`, `truncation_reason`,
`pages_fetched`, `next_cursor`) returned by every paginator.

`truncated` is set conservatively — true whenever a cap bit — because for
page/offset paginators (`list-monitors` breaks on `len(batch) < page_size`)
landing exactly on a boundary is indistinguishable from more-data-exists without
spending an extra request. The *precision* lives in the reason string, not the
boolean:

- `more_available` — cursor still outstanding; certain there is more.
- `max_results_boundary_unknown` — page/offset cap bit; cannot distinguish.
- `max_pages` — page cap hit.
- `server_timeout` — 200-partial.

Exit behavior: one flag `--on-truncation=exit3|warn|error`, default `exit3`.

Rejected: making the exit code depend on whether the user passed `--max-results`
explicitly. `ctx.get_parameter_source` can distinguish it, but an exported
`DD_MAX_RESULTS` would silently convert every truncation to exit 0 forever — the
precise silent-partial this work exists to kill. It is also backwards
semantically: an agent reading `count: 50` cannot tell whether 50 is the answer
or the ceiling, and who typed the ceiling is irrelevant to that question.

Known consequence: `list-monitors` default `max_results=1000` equals Datadog's
max `page_size=1000`, so an org with >=1000 monitors exits 3 on every default
invocation. Correct and loud, accepted deliberately.

### 5. Wrong-answer fixes

- `_fetch_teams` uses page-*number* pagination with a *varying* page size, which
  addresses a moving window. At `--max-results 150`: page 0 size 100 returns
  items 0-99, then page 1 size 50 returns items **50-99 again**. Result: 50
  duplicates and items 100-149 never fetched. Fix: constant page size, slice at
  the end.
- `_parse_time_to_epoch_s` / `_parse_time_to_epoch_ms` (duplicated, differing
  only by x1000) use an **unanchored** `re.match(r"now-(\d+)([mhd])")`, so
  `now-1h30m` silently means 1h and `now-7days` silently means 7d. A wrong
  window yields a confidently wrong count with no API failure involved. Fix:
  anchor, unify, accept `s`/`w` and bare `now`.
- `http.py:434` `return result if isinstance(result, list) else []` — a non-list
  response becomes an empty list. Raise instead.
- `search-logs` silently drops a non-list `data`. Raise instead.
- `search-et-issues`, `list-slos`, `list-dashboards` send no pagination
  parameters and present page 1 of N as complete. Mark as incomplete where the
  API exposes no cursor.

### 6. Swallowed errors

- `_enrich_incident`: two bare `except DatadogAPIError: pass` plus an outer
  `except Exception` that stringifies into free text. All three become
  structured `enrichment.errors[]` entries (`{step, status, message}`) with
  `enrichment.partial = true`.
- `get-slo`: record the status code and set a top-level `partial`.
- `--format messages` skips logs with an empty message, so line count != record
  count (and an embedded newline breaks it permanently). Emit blank lines and a
  `count=N` stderr trailer; document as never-countable.

### 7. `count-logs --bucket`

```
dd-cli count-logs QUERY --from now-48h --to now --bucket 1h
→ {"ok": true, "schema_version": 2, "total": 3626,
   "buckets": [{"from": "...", "to": "...", "count": 354, "complete": true}, ...],
   "truncated": false, "attempts": 7}
```

Incident 1 happened because an agent wrote a shell loop over hours around
dd-cli. Every iteration is an independent chance to convert a failure into a
zero, and no in-process guarantee survives the process boundary. Bucketing
in-process removes the reason to write the loop: a 429 becomes a retry, an
exhausted retry becomes an exception, and a bucket is never `0` unless it is
really 0. Kept as a separate commit — a sibling worktree (`ddcli-timeseries-query`)
may supersede it.

### 8. Documented principle

Added to `CLAUDE.md` and `AGENTS.md`:

> **Errors must never be representable as data.** A failed request must never
> surface as `0`, `[]`, `null`, or a short page. If a call fails, fail loudly
> with a non-zero exit. If a result is incomplete, mark it: `"truncated": true`
> with a reason, a stderr warning, and exit 3. Never `except: pass` around a
> data-producing call — record the failure in the output. A data-producing
> command must emit a parseable envelope on stdout even when it fails. Never
> count lines of a text output format.

## Testing

`http.py` has no direct coverage today — every test patches `DatadogClient`
wholesale. Add a `transport=` injection point to the constructor so
`httpx.MockTransport` can drive the retry layer.

Tests written first: 429 with `Retry-After: 2`; 429 with HTTP-date
`Retry-After`; 429 with `X-RateLimit-Reset`; 429 with no headers (jitter
bounds); retry exhaustion raises rather than empties; 5xx retried by `_read` but
not by `_write`; 429 on `_write` retried only with DD headers; cursor
outstanding at page cap sets truncated + exit 3; cap truncation sets
`max_results_boundary_unknown`; 200-partial via `meta.status`; `list_monitors`
non-list raises; enrichment failure recorded not swallowed; `_fetch_teams`
returns no duplicates at `--max-results 150`; time parser rejects `now-1h30m`.

Patch both `time.sleep` and the monotonic clock, or the budget-exhaustion test
hangs.

## WISHLIST

Item 2 (progress indicator for slow flex queries) is partially addressed: retry
and wait messages now go to stderr. The spinner itself is cosmetic and out of
family; left open.
