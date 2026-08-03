---
name: datadog-log-metrics
description: Create log-based count and distribution metrics via API - works with all storage tiers including flex. Use when you need to monitor log patterns but logs are in flex tier (where log monitors don't work), or when you need to measure a numeric value carried on a log line.
---

# Datadog Log-Based Metrics

## Why Log-Based Metrics?

Log monitors (`type: "log alert"`) only work on **Standard Tier indexed logs**. If your logs are in Flex Tier (check with `GET /api/v1/logs/config/indexes`), you need an alternative:

1. Create a **log-based metric** (computed at ingestion time, before storage routing)
2. Create a **metric monitor** (`type: "query alert"`) on that metric

This two-step approach works regardless of storage tier.

## The '@' Trap (read this first)

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

## CLI Command

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

- **Endpoint**: `POST /api/v2/logs/config/metrics`
- **Permission**: Requires `logs_generate_metrics`
- **409 Conflict**: Metric with this ID already exists
- **No list-all via CLI yet** -- use curl: `GET /api/v2/logs/config/metrics`
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
