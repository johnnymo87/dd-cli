# Metrics Timeseries Query Design

Date: 2026-07-31

## Summary

Add two read-only metrics commands:

- `dd-cli query-metrics QUERY` — query a metric timeseries over a time window and
  print a per-series summary (scope, first/last/min/max/avg, last non-null
  timestamp), with the raw API response available on request.
- `dd-cli search-metrics TERM` — find metric names by substring.

Motivation: answering a routine deploy-verification question ("what is the
consumer lag per consumer group over the last 20 minutes") previously required
dropping to raw `curl` against the Datadog v1 query API. A secondary, recurring
failure is guessing the metric *name* wrong: a metric spelled with an underscore
has live series while the intuitive dotted spelling has none org-wide, and a
monitor built on the dotted spelling silently never fires.

## Research Findings

All of the following was verified against the live API before implementation.

### Authentication

`DatadogClient` already sends `Authorization: Bearer <DD_PAT>` for every request
(`dd_cli/http.py`), including existing v1 endpoints (monitors, dashboards, SLOs).
That header returns 200 on both `GET /api/v1/query` and `GET /api/v1/search`.

No auth change is required. Callers keep using the credential dd-cli already
resolves (`DD_PAT`); they never need to think about which header a raw `curl`
would want.

### `GET /api/v1/query`

Takes `query`, `from`, `to`. **`from`/`to` are epoch seconds** (not milliseconds).

Success response:

```json
{
  "status": "ok",
  "res_type": "time_series",
  "query": "...",
  "series": [
    {
      "scope": "host:example",
      "metric": "system.cpu.user",
      "query_index": 0,
      "interval": 2,
      "unit": [{"name": "percent", "short_name": "%"}, null],
      "pointlist": [[1700000000000.0, 5.9], [1700000002000.0, null]]
    }
  ],
  "message": "",
  "group_by": []
}
```

Point values may be `null`. `pointlist` timestamps are epoch **milliseconds**,
even though the request takes seconds.

### Failure shapes (the important part)

| Input | HTTP | Body |
| --- | --- | --- |
| Malformed query | **200** | `status: "error"`, `series: []`, `error: "Error parsing query: ..."` |
| Valid syntax, nonexistent metric | 200 | `status: "ok"`, `series: []`, no `error` |
| Valid metric, nonexistent tag scope | 200 | `status: "ok"`, `series: []` |
| Window outside retention | 200 | `status: "error"`, `error: "Invalid query input: queries ending outside the retention date are invalid"` |
| Epoch **milliseconds** passed as `from`/`to` | 200 | `status: "error"`, `error: "Internal error"` |

A fatal error therefore arrives as **HTTP 200**, so `DatadogAPIError` never
fires and a naive implementation exits 0 on garbage. The body's `status` must be
checked explicitly.

Two consequences for output design:

1. "Nonexistent metric" and "metric exists but this tag scope/window has no
   data" are **indistinguishable** — both are `status: "ok"` with zero series.
   The zero-series note must therefore name both hypotheses (name/tags *and*
   time window) rather than blaming spelling alone.
2. A series whose points are *all null* is a real shape, but it arises from
   formula/division/offset queries (e.g. `a{*}/a{host:nonexistent}` returned one
   series with 239 points, none non-null), not from "the metric is quiet". Its
   note is worded accordingly and does not promise a metric-existence signal.

### `GET /api/v1/search`

`q=metrics:<term>` returns `{"results": {"metrics": [names...]}}`.

- Matching is **literal substring**; `.` is not a wildcard (`pu.use` matches
  `container.cpu.user`). A dotted guess can never surface an underscored metric
  name, so the useful technique is to search a short distinctive token.
- **No result cap**: a one-letter term returned over 20,000 names. The command
  needs its own `--limit`.
- Zero matches returns `"metrics": null`, **not** `[]`.
- The index only covers recently-reporting metrics, so absence is not proof a
  metric does not exist.

## Design

### `dd_cli/http.py`

Two thin methods, matching the existing style:

- `query_timeseries(*, query, from_ts, to_ts)` → `GET /api/v1/query`
- `search_metrics(*, term)` → `GET /api/v1/search`, `q=f"metrics:{term}"`

### `dd-cli query-metrics QUERY`

Options follow the existing command set: `--from` (default `now-1h`), `--to`
(default now), `--format summary|json|jsonl` (default `summary`), `--site`,
`--timeout`.

`--to` uses `default=None` and falls back to the current time, because the
shared time parser accepts `now-1h` and bare epoch digits but **rejects the
literal string `"now"`**.

Order of operations:

1. Parse the window; reject epoch-millisecond input and inverted windows with a
   `UsageError` rather than letting them become an unactionable "Internal error"
   or a misattributed zero-series note.
2. Call the API.
3. Check `status` **before** branching on output format, so no format can
   silently emit nothing and exit 0. Error text falls back `error` → `message` →
   raw body snippet.
4. Format.

`summary` (default) emits compact JSON, matching the repo's existing meaning of
"summary" (see `_output_monitors`) rather than an ASCII table — this keeps
output pipeable into `jq`:

```json
{
  "query": "...",
  "from": 1700000000,
  "to": 1700001200,
  "res_type": "time_series",
  "count": 1,
  "series": [
    {
      "scope": "consumer_group:example",
      "metric": "...",
      "query_index": 0,
      "interval": 20,
      "unit": [null, null],
      "points": 60,
      "non_null_points": 58,
      "first": 12.0,
      "last": 0.0,
      "last_ts": 1700001180000,
      "min": 0.0,
      "max": 42.0,
      "avg": 7.5
    }
  ]
}
```

Aggregates are computed over non-null, finite points only; an empty or fully
null `pointlist` yields `null` for every aggregate instead of raising. `last_ts`
is the timestamp of the last non-null point, which distinguishes "lag is zero
now" from "no data has arrived for five minutes" — the exact ambiguity that
matters when verifying a deploy.

`unit` is passed through verbatim rather than unwrapped, since it can be
`[unit, null]`, `[null, null]`, or absent.

`json` emits the raw API response; `jsonl` emits one raw series per line.

### `dd-cli search-metrics TERM`

Emits `{term, total, count, data}` with `--limit` (default 100) so a broad term
cannot dump 20,000 names. `--help` and the skill state the substring, recency,
and absence-is-not-proof caveats.

### Shared time-parser fix

`_parse_time_to_epoch_s` and `_parse_time_to_epoch_ms` match their `now-N[mhd]`
pattern unanchored, so `now-1h30m` silently parses as `now-1h` — a silently
wrong time window, the same failure class as a monitor that never fires. Each
helper has exactly one existing caller (`get-slo`, `search-et-issues`). Both are
switched to a fullmatch, with an error message that points at the equivalent
supported spelling (`now-90m`). The grammar is not widened; only the silence is
removed.

## Documentation

Command-table rows in `README.md`, `CLAUDE.md`, and `AGENTS.md`, plus a
`datadog-metrics` skill mirrored into `.claude/skills/` and `.opencode/skills/`.
The skill documents the rollup trap: the v1 query API auto-rolls up long windows
and defaults to averaging, so the reported `max` is a max of interval averages
and understates short spikes. `.rollup(max, 60)` is the fix, and the `interval`
field in the summary makes the granularity visible.

## Testing

Tests use `CliRunner` with `DatadogClient` patched, per the existing suite; no
live calls. Windows are passed as explicit epoch values so no test depends on
the wall clock. The two new `http.py` methods are tested at the `_request`
level, since patching `dd_cli.cli.DatadogClient` would otherwise leave the
seconds-not-milliseconds contract and the `from`/`to` parameter names entirely
uncovered.
