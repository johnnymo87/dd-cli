---
name: datadog-log-metrics
description: Create log-based count metrics via API - works with all storage tiers including flex. Use when you need to monitor log patterns but logs are in flex tier (where log monitors don't work).
---

# Datadog Log-Based Metrics

## Why Log-Based Metrics?

Log monitors (`type: "log alert"`) only work on **Standard Tier indexed logs**. If your logs are in Flex Tier (check with `GET /api/v1/logs/config/indexes`), you need an alternative:

1. Create a **log-based metric** (computed at ingestion time, before storage routing)
2. Create a **metric monitor** (`type: "query alert"`) on that metric

This two-step approach works regardless of storage tier.

## CLI Command

```bash
# Create a count metric from matching logs
dd create-log-metric my_service.error_count \
  --query 'service:my-service status:error' \
  --group-by service --group-by env

# Multiple group-by dimensions
dd create-log-metric kafka.topic_errors \
  --query 'service:my-worker "not present in metadata after 60000 ms"' \
  --group-by service --group-by env --group-by @topic
```

## Options

| Option | Required | Description |
| --- | --- | --- |
| `METRIC_ID` | yes | The metric name (e.g., `my_service.error_count`) |
| `--query` | yes | Log search query (same syntax as Log Explorer) |
| `--group-by` | no | Attribute path to group by (repeatable) |
| `--timeout` | no | Request timeout in seconds (default: 15) |

## Key Facts

- Metrics are computed at **ingestion time** (before flex/standard routing)
- Only `count` aggregation is supported via CLI (API also supports `distribution`)
- Metric appears as a custom metric in dashboards and monitors
- 10-second granularity, retained 15 months
- Billed as Custom Metrics
- The `--query` uses standard Datadog log search syntax (same as Log Explorer)
- Do NOT include `index:` in the query -- metrics run on the full ingest stream

## Pairing with a Monitor

After creating the metric, create a monitor to alert on it:

```bash
# Step 1: Create the metric
dd create-log-metric my_app.kafka_errors \
  --query 'service:my-worker "UNKNOWN_TOPIC_OR_PARTITION"' \
  --group-by service --group-by env

# Step 2: Create a metric monitor (see datadog-monitors skill)
dd create-monitor \
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

## curl Example

```bash
curl -X POST "https://api.$DD_SITE/api/v2/logs/config/metrics" \
  -H "DD-API-KEY: $DD_API_KEY" \
  -H "DD-APPLICATION-KEY: $DD_APP_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "id": "my_service.error_count",
      "type": "logs_metrics",
      "attributes": {
        "compute": { "aggregation_type": "count" },
        "filter": { "query": "service:my-service status:error" },
        "group_by": [
          { "path": "service", "tag_name": "service" },
          { "path": "env", "tag_name": "env" }
        ]
      }
    }
  }'
```
