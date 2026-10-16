---
name: datadog-metrics
description: Query Datadog metric timeseries and find metric names via API - per-series summaries, null handling, rollup traps, and name spelling. Use when verifying a deploy, checking a metric's recent values, or confirming a metric name before writing a query or monitor.
---

# Datadog Metrics

## Query a Timeseries

```bash
# Lag per consumer group over the last 20 minutes
dd-cli query-metrics 'avg:kafka.consumer_lag{*} by {consumer_group}' --from now-20m

# Default window is the last hour
dd-cli query-metrics 'avg:system.cpu.user{*}'

# Absolute window (epoch seconds)
dd-cli query-metrics 'avg:system.cpu.user{*}' --from 1700000000 --to 1700001200

# Raw points for scripting
dd-cli query-metrics 'avg:system.cpu.user{*}' --format json | jq '.data.series[0].pointlist'
```

### query-metrics Options

| Option | Default | Description |
| --- | --- | --- |
| `QUERY` | (required) | Datadog metric query, e.g. `avg:my.metric{*} by {tag}` |
| `--from` | `now-1h` | Start time (`now-20m`, `now-7d`, or epoch seconds) |
| `--to` | `now` | End time |
| `--format` | `summary` | `summary`, `json` (raw response), `jsonl` (one raw series per line) |
| `--timeout` | `30` | Request timeout in seconds |

### summary Response

Rides the standard output envelope (`ok`, `schema_version`, `count`,
`truncated`, `data`), so a failure is never readable as an empty result.

| Field | Description |
| --- | --- |
| `count` | Number of series returned (`null` if the request failed) |
| `note` | Present only when the result needs explaining (see below) |
| `data[].scope` | The tag scope of the series (e.g. `consumer_group:example`) |
| `data[].points` | Total points in the window |
| `data[].non_null_points` | How many of those points had a value |
| `data[].first` / `last` | First and last **non-null** values |
| `data[].last_ts` | Epoch **ms** of the last non-null point |
| `data[].min` / `max` / `avg` | Aggregates over non-null, finite points |
| `data[].interval` | Seconds per point (the rollup granularity in use) |
| `data[].unit` | Raw unit array from the API |

`--format json` puts the raw API response under `data`, so raw points are at
`.data.series[0].pointlist`.

`last_ts` is the field that separates "the value is zero right now" from
"nothing has reported for five minutes". Check it before concluding a deploy
drained a queue.

## Find a Metric Name

```bash
dd-cli search-metrics lag
dd-cli search-metrics consumer --limit 50
```

| Option | Default | Description |
| --- | --- | --- |
| `TERM` | (required) | Literal substring to match against metric names |
| `--limit` | `100` | Max names to print (`total` still reports all matches) |
| `--on-truncation` | `exit3` | What to do when the cap bit: `exit3`, `warn`, or `error` |

The endpoint returns every match, so hitting `--limit` marks the result
`truncated` with reason `more_available` and exits **3**. That is a complete
command with an incomplete answer, not a failure -- but it is not a total
either, which is what `total` is for.

## Traps

### Metric names are separator-sensitive

A name spelled with an underscore and the same name spelled with dots are
different metrics. Only one of them exists. A monitor built on the
non-existent spelling is valid, evaluates forever against zero series, and
**silently never fires**.

Always confirm the spelling before writing a query or monitor:

```bash
dd-cli search-metrics lag        # then read the real names off the list
```

Search matching is a literal substring, so `.` is not a wildcard and a dotted
guess will never surface an underscored name. Search the shortest distinctive
token, not a full guessed name.

The search index only covers recently-reporting metrics, so absence is not
proof that a metric does not exist -- only that it has not reported lately.

### Long windows hide spikes

The query API rolls long windows up automatically and averages by default, so
the reported `max` is a max of interval *averages* and understates short
spikes. The `interval` field shows the granularity in use. For true peaks:

```bash
dd-cli query-metrics 'max:my.metric{*}.rollup(max, 60)' --from now-1d
```

### Zero series is ambiguous

Zero series means one of three things, and the API does not distinguish them:

1. The metric name matches nothing.
2. The metric exists but the tag filter matches nothing.
3. Both exist but the time window contains no data.

The command's note says so. Check the name with `search-metrics`, then widen
the window, then relax the tag filter.

### All-null series

A series can come back with points that are all null. This is normal for
formula, division, and timeshift queries. It is *not* the signal for "the
metric exists but is quiet" -- that case returns zero series instead.

## API Details

- **Query**: `GET /api/v1/query` -- params `query`, `from`, `to`
- **Search**: `GET /api/v1/search` -- param `q=metrics:<term>`
- **Auth**: `DD_PAT`, exactly as every other `dd-cli` command. No separate
  API key or application key is needed.
- **Timestamps**: the request takes epoch **seconds**; the points it returns
  are timestamped in **milliseconds**. Passing milliseconds to `--from` is
  rejected up front, because the API answers such a window with an
  unactionable `Internal error`.
- **Errors arrive as HTTP 200**: a malformed query returns `200` with
  `status: "error"` and an `error` message in the body. `query-metrics`
  checks this and fails through the same envelope as a transport error --
  `ok: false`, `count: null`, non-zero exit -- so a scripted check cannot
  read a broken query as "no data".

## Common Patterns

### Verify a deploy drained a queue

```bash
# 1. Confirm the metric name
dd-cli search-metrics lag

# 2. Watch it per consumer group across the deploy window
dd-cli query-metrics 'avg:kafka.consumer_lag{*} by {consumer_group}' --from now-20m

# 3. Read last + last_ts together: a low 'last' with a stale 'last_ts'
#    means no fresh data, not a drained queue.
```

### Script a threshold check

```bash
dd-cli query-metrics 'avg:my.metric{*}' --from now-5m \
  | jq -e '.data[0].max < 100'
```
