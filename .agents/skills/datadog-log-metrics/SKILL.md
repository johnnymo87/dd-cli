---
name: datadog-log-metrics
description: Create, list, audit, update and delete log-based count and distribution metrics via API - works with all storage tiers including flex. Use when you need to monitor log patterns but logs are in flex tier (where log monitors don't work), when you need to measure a numeric value carried on a log line, or BEFORE shipping a new log string, to check it against every live metric's quoted anchor phrases.
---

# Datadog Log-Based Metrics

## Why Log-Based Metrics?

Log monitors (`type: "log alert"`) only work on **Standard Tier indexed logs**. If your logs are in Flex Tier (check with `GET /api/v1/logs/config/indexes`), you need an alternative:

1. Create a **log-based metric** (computed at ingestion time, before storage routing)
2. Create a **metric monitor** (`type: "query alert"`) on that metric

This two-step approach works regardless of storage tier.

## The Anchor Collision Trap (read this first)

A quoted phrase in a log-metric filter is matched as a **case-insensitive
substring**, at **intake**. So any log line anywhere in the org that happens to
contain another metric's anchor phrase starts feeding that metric's counter.
Nothing warns anyone: not the service that emitted the line, not the metric's
owner, not the monitor built on it.

This is measured, not hypothetical (details below are anonymised; the shape and
the magnitude are real). A service added a log line for an order-*retirement*
code path, worded "...refusing to reserve inventory...". That happens to contain
the `"Refusing to reserve inventory"` anchor of an unrelated metric counting a
*no-op* code path, so **tens of thousands** of retirement events were counted as
no-ops, in a metric a live monitor alerted on. The string has since been
reworded, and the old wording is now 0 in 24h while the underlying event still
runs at its full rate -- but because log metrics are computed at intake and
never backfill, that metric's history is contaminated permanently.

So check a new log string **before it ships**:

```bash
dd-cli audit-log-metric-anchors 'Order retired: refusing to reserve inventory for order 123'
```

```json
{
  "ok": true,
  "count": 1,
  "checked": {"metrics": 62, "metrics_with_quoted_phrase": 48,
              "phrases": 59, "distinct_phrases": 45},
  "positive_control": {"ok": true, "source": "derived_longest_phrase"},
  "data": [{"metric_id": "orders.reserve_inventory_noop",
            "phrase": "Refusing to reserve inventory",
            "direction": "anchor_in_candidate"}]
}
```

Two properties make that output worth believing, and both exist because the
alternative failure is silent:

* **Positive control.** The run also audits a string that MUST hit (by default
  the longest phrase in the harvest; override with `--positive-control`). If
  the control misses, the whole run reports `ok: false` and exits non-zero --
  because a harvest that came back empty and an org with no collisions produce
  the *same* empty answer.
* **Denominator.** `checked` reports metrics, phrases and distinct phrases. The
  real case was 1 hit in 62 filters, a density that survives eyeballing; "0
  collisions" means nothing without the number of things checked next to it.

Collisions are reported in both directions:

| direction | meaning |
| --- | --- |
| `anchor_in_candidate` | an existing anchor occurs inside your string -- shipping it feeds their metric |
| `candidate_in_anchor` | your string occurs inside an existing anchor -- a metric on your string would eat their events |

`CLAUDE.md` in the fulfillment repo says to derive the metric list from
`GET /api/v2/logs/config/metrics` and never from a checked-in file, and to
audit it mechanically. That is what this command is.

## The '@' Trap (read this second)

A path that names a **custom log attribute** must be written with a leading
`@`:

```
@fbm.attention_open     correct
fbm.attention_open      accepted by the API, then produces NOTHING
```

Datadog returns **200 OK** for the bare form and creates the metric. It then
silently produces zero points forever -- for `group_by`, every value collapses
into a single `N/A` bucket. There is no error in the API response, in the UI,
or in any log. The only symptom is an empty graph, days later.

This has already caused a real multi-week failure: three distribution metrics
were created with bare paths, produced no data for 7 days, and the team
concluded "distribution log metrics do not materialize in this org" and wrote
that into their doctrine. It was false; re-creating them with `@` worked
immediately.

**Reserved attributes and tags are correctly bare** -- `service`, `env`,
`host`, `status`, `source`, `version`, `message`, `ddsource`, `ddtags`, `date`,
`timestamp`. Everything your application logged lives under `@`.

`dd-cli` rejects a bare, non-reserved path **before** sending the request. If
the path really is a tag key (e.g. `kube_namespace`), pass `--allow-bare-path`.

Reserved names are bare-legal for `--group-by` but NOT for a distribution's
`--path`: they hold strings, and a distribution needs a number, so
`--path status` is the same empty-metric failure and is rejected too.

## CLI Commands

| Command | Purpose |
| --- | --- |
| `dd-cli list-log-metrics` | Every log metric in the org, with completeness assertion |
| `dd-cli get-log-metric ID` | One metric, plus its extracted anchor phrases |
| `dd-cli audit-log-metric-anchors STRING` | Collision check with positive control (see above) |
| `dd-cli create-log-metric ID` | Create a count or distribution metric |
| `dd-cli update-log-metric ID` | PATCH filter / group_by / include_percentiles (`--dry-run`) |
| `dd-cli delete-log-metric ID --yes` | Delete; requires the confirmation flag |

### Completeness: a short harvest reports "clean" just as confidently

`GET /api/v2/logs/config/metrics` is **unpaginated** -- no cursor, no `meta`,
so nothing in a truncated answer looks truncated. A per-metric fetch loop once
retrieved **41 of 62** filters and reported "no collisions" with exactly the
confidence of a complete run.

`list-log-metrics` and `audit-log-metric-anchors` therefore assert that what
they fetched equals what the org enumerated, id for id, and emit the claim:

```json
"completeness": {"enumerated": 70, "fetched": 70,
                 "per_metric_fetches": 70, "asserted_equal": true}
```

A mismatch is `ok: false` with `data: null` and a non-zero exit -- never a
short list. The endpoint also 429s readily (a live 70-metric `--detail` run hit
one); `http.py` retries with backoff and announces the wait on stderr, and the
assertion still has to pass afterwards.

```bash
# Cheap: the list already carries every metric's full attributes
dd-cli list-log-metrics --format json | jq '.completeness'

# Paranoid: also GET each id and check the two views agree (1 request/metric)
dd-cli list-log-metrics --detail --format json
```

```bash
# Count metric from matching logs
dd-cli create-log-metric my_service.error_count \
  --query 'service:my-service status:error' \
  --group-by service --group-by env

# Group by a custom attribute -- note the '@'
dd-cli create-log-metric kafka.topic_errors \
  --query 'service:my-worker "not present in metadata after 60000 ms"' \
  --group-by service --group-by env --group-by @topic

# Distribution metric measuring a numeric attribute
dd-cli create-log-metric fbm.attention_open \
  --query 'service:fbm @fbm.attention_open:*' \
  --aggregation-type distribution \
  --path '@fbm.attention_open' \
  --include-percentiles \
  --group-by service --group-by env

# Bare path that is genuinely an infra tag key
dd-cli create-log-metric my_service.error_count \
  --query 'service:my-service status:error' \
  --group-by kube_namespace --allow-bare-path
```

## Options

| Option | Required | Description |
| --- | --- | --- |
| `METRIC_ID` | yes | The metric name (e.g., `my_service.error_count`) |
| `--query` | yes | Log search query (same syntax as Log Explorer) |
| `--aggregation-type` | no | `count` (default) or `distribution` |
| `--path` | for distribution | Numeric attribute to aggregate (e.g. `@duration`); rejected for `count` |
| `--include-percentiles` / `--no-include-percentiles` | no | p50-p99 aggregations; distribution only |
| `--group-by` | no | Attribute path to group by (repeatable) |
| `--allow-bare-path` | no | Permit a path with no leading `@` (tag keys) |
| `--timeout` | no | Request timeout in seconds (default: 15) |

## Key Facts

- Metrics are computed at **ingestion time** (before flex/standard routing)
- `count` counts matching logs; `distribution` measures a numeric value off each log via `compute.path`
- `include_percentiles` is only valid when `aggregation_type` is `distribution`
- A distribution's `path` must point at a **numeric** value; a non-numeric path yields no points
- Metric appears as a custom metric in dashboards and monitors
- 10-second granularity, retained 15 months
- Billed as Custom Metrics (percentiles cost more)
- The `--query` uses standard Datadog log search syntax (same as Log Explorer)
- Do NOT include `index:` in the query -- metrics run on the full ingest stream

## Updating a Metric (and why you probably should not)

`PATCH /api/v2/logs/config/metrics/{id}` accepts **exactly three** fields --
this is `LogsMetricUpdateAttributes` in Datadog's OpenAPI spec, confirmed live
against us3 on a throwaway metric:

| Field | Notes |
| --- | --- |
| `filter.query` | Replaces the query entirely |
| `group_by` | Replaces the **whole list**; there is no per-entry merge. Send `[]` to clear |
| `compute.include_percentiles` | Distribution metrics only |

`compute.aggregation_type` and `compute.path` are **not patchable**. A metric
that needs a different one has to be recreated under a new name. The update
payload also carries no `id` (only `type` + `attributes`).

Omitted fields are left alone: a PATCH sending only `filter` preserved the
existing `group_by` (verified live).

**The danger.** A log metric is computed at INTAKE and does not backfill, so:

* an edited filter can never be validated against historical data -- only
  against logs that have not arrived yet;
* a mistyped filter produces a permanently empty series, which is
  indistinguishable from a healthy quiet one. A monitor watching it does not
  alert. It just stops having anything to say.

So mutating a metric a live monitor depends on is **riskier than creating a
narrowed metric alongside it** and repointing the monitor only after the new
metric has produced a real emission. That path is reversible; the PATCH is not.

```bash
# Always look first
dd-cli update-log-metric my.metric --query 'service:x "new anchor"' --dry-run

# Then, if you must
dd-cli update-log-metric my.metric --query 'service:x "new anchor"'
dd-cli query-metrics 'sum:my.metric{*}.as_count()' --from now-1h   # within the hour
```

## Verifying a New Metric Actually Works

The '@' trap means "the API said 200" is not evidence. Confirm points exist:

```bash
dd-cli query-metrics 'avg:fbm.attention_open{*}' --from now-1h
```

Do this within an hour of creation, not a week later.

## Pairing with a Monitor

After creating the metric, create a monitor to alert on it:

```bash
# Step 1: Create the metric
dd-cli create-log-metric my_app.kafka_errors \
  --query 'service:my-worker "UNKNOWN_TOPIC_OR_PARTITION"' \
  --group-by service --group-by env

# Step 2: Create a metric monitor (see datadog-monitors skill)
dd-cli create-monitor \
  --name 'My App: Kafka topic errors' \
  --type 'query alert' \
  --query 'sum(last_10m):sum:my_app.kafka_errors{env:prod}.as_count() >= 3' \
  --message '{{#is_alert}}Kafka errors detected{{/is_alert}} @slack-my-alerts' \
  --critical 3 --warning 1
```

## API Details

- **Endpoints**: `GET|POST /api/v2/logs/config/metrics`,
  `GET|PATCH|DELETE /api/v2/logs/config/metrics/{metric_id}`
- **Permission**: Requires `logs_generate_metrics`
- **409 Conflict**: Metric with this ID already exists
- **404** on GET/PATCH/DELETE of an unknown id, with
  `not_found(Metric with name '...' not found)`
- **DELETE answers 204 with an empty body** -- do not parse it as JSON, and note
  that deletion stops future points without removing emitted history
- The list endpoint is unpaginated and rate-limits readily (429)
- `compute` shape: `{aggregation_type, path, include_percentiles}`; `path` and
  `include_percentiles` are only used when `aggregation_type` is `distribution`

## curl Example

```bash
curl -X POST "https://api.$DD_SITE/api/v2/logs/config/metrics" \
  -H "DD-API-KEY: $DD_API_KEY" \
  -H "DD-APPLICATION-KEY: $DD_APP_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "id": "fbm.attention_open",
      "type": "logs_metrics",
      "attributes": {
        "compute": {
          "aggregation_type": "distribution",
          "path": "@fbm.attention_open",
          "include_percentiles": true
        },
        "filter": { "query": "service:fbm @fbm.attention_open:*" },
        "group_by": [
          { "path": "service", "tag_name": "service" },
          { "path": "env", "tag_name": "env" }
        ]
      }
    }
  }'
```

Note the `@` in both the compute path and the query. Raw curl has no client-side
guard -- prefer `dd-cli create-log-metric`, which refuses a bare custom path.
