---
name: datadog-monitors
description: Create Datadog monitors via API - metric alerts, query alerts, with thresholds and Slack notifications. Use when setting up alerting on metrics or log-based metrics.
---

# Datadog Monitors

## CLI Command

```bash
# Metric monitor (on a log-based metric)
dd create-monitor \
  --name 'My Service: High error rate' \
  --type 'query alert' \
  --query 'sum(last_10m):sum:my_service.errors{env:prod}.as_count() >= 3' \
  --message '{{#is_alert}}Error rate exceeded threshold{{/is_alert}} @slack-my-alerts' \
  --critical 3 --warning 1 \
  --tag team:my-team --tag service:my-service --tag env:prod

# With re-notification
dd create-monitor \
  --name 'Critical: DB CPU' \
  --type 'query alert' \
  --query 'avg(last_5m):avg:system.cpu.user{service:my-db} > 90' \
  --message '{{#is_alert}}DB CPU > 90%{{/is_alert}} @slack-incidents' \
  --critical 90 --warning 75 \
  --priority 1 \
  --renotify-interval 30
```

## Options

| Option | Required | Description |
| --- | --- | --- |
| `--name` | yes | Monitor name |
| `--type` | yes | Monitor type (see below) |
| `--query` | yes | Monitor query |
| `--message` | yes | Notification message |
| `--tag` | no | Monitor tag (repeatable) |
| `--critical` | no | Critical threshold |
| `--warning` | no | Warning threshold |
| `--priority` | no | Priority 1-5 |
| `--renotify-interval` | no | Minutes between re-notifications |
| `--notify-no-data` | no | Alert on missing data |

## Monitor Types

| Type string | Use case |
| --- | --- |
| `query alert` | Metric queries, log-based metric queries |
| `metric alert` | Simple metric threshold |
| `log alert` | Log count (Standard Tier only, not Flex) |

**For Flex Tier logs**: Use `query alert` on a log-based metric (see `datadog-log-metrics` skill).

## Query Syntax for Metric Monitors

```
# Count metric over time window
sum(last_10m):sum:my_metric{env:prod}.as_count() >= 3

# Average metric
avg(last_5m):avg:system.cpu.user{service:my-db} > 90

# With group-by (multi-alert)
sum(last_10m):sum:my_metric{env:prod} by {host}.as_count() >= 3
```

## Notification Message Syntax

```
{{#is_alert}}Alert: {{value}} > {{threshold}}{{/is_alert}}
{{#is_warning}}Warning: {{value}} > {{warn_threshold}}{{/is_warning}}
{{#is_recovery}}Recovered{{/is_recovery}}

@slack-channel-name
@pagerduty-ServiceName
@user@example.com
```

## API Details

- **Endpoint**: `POST /api/v1/monitor`
- **Auth**: Requires unscoped App Key for log monitors
- **Response**: Returns full monitor object with `id` field

## Common Patterns

### Flex Tier Log Alerting (two-step)

```bash
# 1. Create log-based metric (ingestion-time, works with flex)
dd create-log-metric my_app.errors \
  --query 'service:my-app status:error' \
  --group-by service --group-by env

# 2. Create metric monitor on it
dd create-monitor \
  --name 'My App: Error rate' \
  --type 'query alert' \
  --query 'sum(last_10m):sum:my_app.errors{env:prod}.as_count() >= 10' \
  --message '{{#is_alert}}Errors > {{threshold}} in 10m @slack-alerts{{/is_alert}}' \
  --critical 10 --warning 5 \
  --tag service:my-app --tag managed-by:dd-cli
```

### Tagging for Idempotency

Use `managed-by:dd-cli` and a stable `monitor-key:*` tag to find monitors later:

```bash
dd create-monitor \
  --tag managed-by:dd-cli \
  --tag monitor-key:my-unique-key \
  ...
```
