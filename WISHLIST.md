# dd-cli Wishlist

Feature requests and improvements based on real-world usage.

## Completed

### ~~1. Configurable request timeout for flex tier searches~~ ✅

Implemented in v0.3.0. Use `--timeout 120` for slow flex queries.

### ~~3. Limit result count with early termination~~ ✅

Implemented in v0.3.0. Use `--max-results 50` with `--all-pages`.

### ~~4. Output format options~~ ✅

Implemented in v0.3.0. Use `--format jsonl` or `--format messages`.

### ~~7. Read/audit log-based metrics, not just create them~~ ✅

`create-log-metric` existed with no way to list, get, update, audit or delete.
Shipped as `list-log-metrics`, `get-log-metric`,
`audit-log-metric-anchors`, `update-log-metric` and `delete-log-metric`.

The audit is the point. Log-metric filters match quoted phrases as
case-insensitive substrings *at intake*, so a new log string containing another
metric's anchor silently feeds that metric -- tens of thousands of events were
counted under an unrelated metric before anyone noticed, and metrics do not
backfill, so the history stays wrong. Doing that audit by hand was an
instruction in a downstream repo's CLAUDE.md that dd-cli could not serve.

### ~~8. Delete a monitor~~ ✅

`create-monitor`, `get-monitor`, `list-monitors` and `update-monitor` existed
with no way to remove one. A duplicate monitor created during triage therefore
got renamed `[DEPRECATED - DELETE ME]`, had its notification handles stripped,
and was handed to a human to finish in the UI -- a chore manufactured by an
incomplete tool. Shipped as `delete-monitor`, which captures the definition
before destroying it, refuses to call a 404 a success, and explains Datadog's
SLO/composite refusal instead of forcing past it by default.

### ~~9. Mute and unmute a monitor~~ ✅

There was no mute verb at all, so muting went through the deprecated legacy
field (`update-monitor --option 'silenced={"*": <epoch>}'`) -- and *unmuting*
was impossible: `--option silenced=null` and `--option 'silenced={}'` both
returned 200, changed nothing, and exited 0. During a production incident on
2026-08-31 the only way to bring monitor 25447403 back was to bypass dd-cli
with `curl -X POST .../monitor/25447403/unmute`. Shipped as `mute-monitor` and
`unmute-monitor`, which demand an expiry unless `--forever` is passed and
verify `options.silenced` by re-reading the monitor instead of trusting the
status code. `update-monitor` now refuses the options a PUT accepts and
ignores, rather than reporting the no-op as a success.

## Medium Priority

### 2. Progress indicator for slow queries -- partially addressed

**Problem**: Flex tier queries can take 30-60+ seconds with no feedback.

**Suggestion**: Add a spinner or progress dots to stderr.

**Status**: The honest half landed with the retry work. Retries now announce
themselves on stderr (`dd-cli: 429 from /api/v2/logs/events/search, retry 2/5
in 4.0s`), so a long wait is legible rather than looking like a hang. The
spinner itself is still open: it is cosmetic, and it would add a second kind of
stderr traffic alongside the truncation/count warnings that are now part of the
output contract. Worth doing deliberately, not as a side effect.

## Low Priority / Nice to Have

### 5. Time range shortcuts

**Suggestion**: Common time range presets like `--today`, `--yesterday`.

### 6. Save/load query profiles

**Problem**: Complex queries with multiple options are tedious to retype.
