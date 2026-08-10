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
