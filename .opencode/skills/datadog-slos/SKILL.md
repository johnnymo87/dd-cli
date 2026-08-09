---
name: datadog-slos
description: List and inspect Datadog SLOs via API - view SLI values, error budgets, thresholds, and history. Use when checking SLO compliance, reviewing error budgets, or finding SLOs by tag.
---

# Datadog SLOs

## List SLOs

```bash
# List all SLOs
dd-cli list-slos

# Filter by tags
dd-cli list-slos --tags 'env:prod,team:backend'

# With pagination
dd-cli list-slos --limit 10 --offset 20
```

### list-slos Options

| Option | Default | Description |
| --- | --- | --- |
| `--tags` | - | Comma-separated tags to filter by, AND-combined (e.g., `env:prod,team:backend`). These are the SLO's **own** tags. Sent as DD's `tags_query`. |
| `--limit` | `1000` | Max number of SLOs to return. Always sent, so a full page is reported as `truncated` (exit 3) instead of passing for a complete answer. |
| `--offset` | - | Pagination offset |
| `--timeout` | `15` | Request timeout in seconds |

**`--tags` was a no-op before 2026-08-09.** dd-cli sent it as `tags`, which
`GET /api/v1/slo` does not define -- and Datadog **ignores** unknown query
parameters rather than rejecting them. The command therefore returned *every*
SLO in the org while presenting it as a filtered result (measured: 442 with no
filter, 442 for `--tags team:ba-fulfillment`, 442 for a deliberately nonsense
tag). The correct parameter is `tags_query`. Any count taken from an older
dd-cli `list-slos --tags` is the org-wide total (capped at the page size), not a filtered one.

### list-slos Response

Returns a summary array with `id`, `name`, `type`, `tags`, and `thresholds` for each SLO.

## Get SLO Details + History

```bash
# Get SLO with default 7-day history
dd-cli get-slo abc123def456

# Get SLO with 30-day history
dd-cli get-slo abc123def456 --from now-30d

# Custom time range
dd-cli get-slo abc123def456 --from now-1d --to now
```

### get-slo Options

| Option | Default | Description |
| --- | --- | --- |
| `SLO_ID` | (required) | The SLO identifier (hex string) |
| `--from` | `now-7d` | History start time (e.g., `now-7d`, `now-30d`, or epoch seconds) |
| `--to` | `now` | History end time |
| `--timeout` | `30` | Request timeout in seconds |

### Key Response Fields

| Field | Description |
| --- | --- |
| `data.name` | SLO name |
| `data.type` | SLO type: `metric` or `time_slice` |
| `data.tags` | Tags (e.g., `env:prod`, `service:my-service`) |
| `data.thresholds` | Target percentage per timeframe (e.g., `99.9%` over `7d`) |
| `data.query` | Numerator/denominator metric queries (for metric SLOs) |
| `history.data.overall.sli_value` | Current SLI value (e.g., `99.9999`) |
| `history.data.overall.state` | Current state: `ok`, `breached`, `no_data` |

## SLO Types

| Type | Description |
| --- | --- |
| `metric` | Based on metric queries (good events / total events ratio) |
| `time_slice` | Based on time slices where a condition is met |

## API Details

- **List**: `GET /api/v1/slo` -- supports `tags_query`, `limit`, `offset` params. There is **no** `tags` parameter; sending one is silently ignored.
- **Get**: `GET /api/v1/slo/{slo_id}` -- returns SLO definition
- **History**: `GET /api/v1/slo/{slo_id}/history` -- returns SLI value, error budget over time range
- **Auth**: Requires API key + App key

## Common Patterns

### Find SLOs for a Service and Check Error Budgets

```bash
# 1. Find SLOs by service tag
dd-cli list-slos --tags 'service:my-service'

# 2. Check error budget for a specific SLO (30-day window)
dd-cli get-slo abc123def456 --from now-30d
# Look at: history.data.overall.sli_value and history.data.overall.state
```

### Pre-deploy SLO Check

```bash
# Check all SLOs for a team before deploying
dd-cli list-slos --tags 'team:my-team'
# Then inspect any that look concerning:
dd-cli get-slo <id> --from now-7d
```
