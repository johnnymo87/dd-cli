# dd-cli Wishlist

Feature requests and improvements based on real-world usage.

## Completed

### ~~1. Configurable request timeout for flex tier searches~~ ✅

Implemented in v0.3.0. Use `--timeout 120` for slow flex queries.

### ~~3. Limit result count with early termination~~ ✅

Implemented in v0.3.0. Use `--max-results 50` with `--all-pages`.

### ~~4. Output format options~~ ✅

Implemented in v0.3.0. Use `--format jsonl` or `--format messages`.

## Medium Priority

### 2. Progress indicator for slow queries

**Problem**: Flex tier queries can take 30-60+ seconds with no feedback.

**Suggestion**: Add a spinner or progress dots to stderr.

## Low Priority / Nice to Have

### 5. Time range shortcuts

**Suggestion**: Common time range presets like `--today`, `--yesterday`.

### 6. Save/load query profiles

**Problem**: Complex queries with multiple options are tedious to retype.
