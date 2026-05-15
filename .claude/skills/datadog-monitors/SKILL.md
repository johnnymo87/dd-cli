---
name: datadog-monitors
description: Create, inspect, and update Datadog monitors via API - get monitor details by ID or URL, create or update metric/query alerts with thresholds and Slack notifications. Use when setting up alerting, tuning monitors, investigating monitor triggers, or checking monitor group states.
---

# Datadog Monitors

## List Monitors

```bash
# All monitors managed by dd-cli (most common discovery query)
dd-cli list-monitors --tag managed-by:dd-cli

# Multiple tags AND together (DD-side comma-join)
dd-cli list-monitors --tag managed-by:dd-cli --tag team:platform

# Substring name search (server-side, case-insensitive)
dd-cli list-monitors --name kafka --max-results 50

# Bulk dump for jq processing -- jsonl emits one full monitor per line
dd-cli list-monitors --tag managed-by:dd-cli --format jsonl | \
  jq -r 'select(.overall_state == "Alert") | "\(.id)\t\(.name)"'

# Full payloads in a single JSON wrapper
dd-cli list-monitors --tag team:platform --format json
```

### list-monitors Options

| Option | Default | Description |
| --- | --- | --- |
| `--tag` | - | Filter by *monitor's own* tag (repeatable, AND-combined). Not the watched-resource tags. |
| `--name` | - | Filter by name substring (server-side, case-insensitive) |
| `--max-results` | `1000` | Cap total results. Auto-pagination stops here even if more pages exist. |
| `--format` | `summary` | Output: `summary` (id/name/type/overall_state/tags), `json` (full, wrapped), `jsonl` (full, one per line) |
| `--timeout` | `15` | Request timeout in seconds |

**Tag flavors gotcha:** `--tag` filters by the monitor's own tags (e.g., `managed-by:dd-cli`, `team:platform`). The Datadog API distinguishes these from `monitor_tags` (tags on the resources the monitor watches, like `env:prod`); the latter is not exposed yet — file an issue if you need it.

**Pagination:** auto-paginates 1000 monitors per page until `--max-results` is reached or a short page is returned. With the default cap of 1000, only one API call is made.

### API Details

- **Endpoint**: `GET /api/v1/monitor`
- **Auth**: API key + App key
- **Returns**: bare JSON array (no wrapper), unlike most v2 endpoints

## Get a Monitor

```bash
# By numeric ID
dd-cli get-monitor 12345678

# From a full Datadog URL (monitor ID extracted automatically)
dd-cli get-monitor 'https://us3.datadoghq.com/monitors/12345678?group=deployment%3Amy-service'

# With group state details (all, alert, warn, no data)
dd-cli get-monitor 12345678 --group-states all
```

### get-monitor Options

| Option | Default | Description |
| --- | --- | --- |
| `--group-states` | - | Comma-separated group states to include (`all`, `alert`, `warn`, `no data`) |
| `--timeout` | `15` | Request timeout in seconds |

### Key Response Fields

| Field | Description |
| --- | --- |
| `overall_state` | Current state: `OK`, `Alert`, `Warn`, `No Data` |
| `state.groups` | Per-group state with `last_triggered_ts`, `last_resolved_ts` (requires `--group-states`) |
| `query` | The monitor query (metric, threshold, grouping) |
| `options.silenced` | Muted groups with expiry timestamps |
| `options.thresholds` | Critical/warning threshold values |
| `message` | Notification template with Slack/PagerDuty targets |

### API Details

- **Endpoint**: `GET /api/v1/monitor/{monitor_id}`
- **Auth**: Requires API key + App key

## Create a Monitor

```bash
# Metric monitor (on a log-based metric)
dd-cli create-monitor \
  --name 'My Service: High error rate' \
  --type 'query alert' \
  --query 'sum(last_10m):sum:my_service.errors{env:prod}.as_count() >= 3' \
  --message '{{#is_alert}}Error rate exceeded threshold{{/is_alert}} @slack-my-alerts' \
  --critical 3 --warning 1 \
  --tag team:my-team --tag service:my-service --tag env:prod

# With re-notification
dd-cli create-monitor \
  --name 'Critical: DB CPU' \
  --type 'query alert' \
  --query 'avg(last_5m):avg:system.cpu.user{service:my-db} > 90' \
  --message '{{#is_alert}}DB CPU > 90%{{/is_alert}} @slack-incidents' \
  --critical 90 --warning 75 \
  --priority 1 \
  --renotify-interval 30
```

## Update a Monitor

```bash
# Change the query (e.g., tune aggregator and window)
dd-cli update-monitor 12345678 \
  --query 'min(last_15m):sum:my.metric{env:prod} by {host} > 0'

# Update name and thresholds
dd-cli update-monitor 12345678 \
  --name 'My Service: Updated alert' \
  --critical 5 --warning 2

# From a full Datadog URL
dd-cli update-monitor 'https://us3.datadoghq.com/monitors/12345678' \
  --renotify-interval 30
```

Only the specified fields are updated; everything else is left unchanged.

### update-monitor Options

| Option | Description |
| --- | --- |
| `--name` | Update monitor name |
| `--query` | Update monitor query |
| `--message` | Update notification message |
| `--critical` | Update critical threshold |
| `--warning` | Update warning threshold |
| `--priority` | Update priority (1-5) |
| `--renotify-interval` | Minutes between re-notifications (0 to disable) |
| `--timeout` | Request timeout in seconds |

### API Details

- **Endpoint**: `PUT /api/v1/monitor/{monitor_id}`
- **Auth**: Requires API key + App key
- **Response**: Returns full updated monitor object

### Common Tuning Patterns

**Reduce noise from rolling deploys** (e.g., Kubernetes pod unavailability):

```bash
# Before: max(last_10m) fires on ANY brief spike
# After:  min(last_15m) fires only if unavailable for the ENTIRE window
dd-cli update-monitor 12345678 \
  --query 'min(last_15m):sum:kubernetes_state.deployment.replicas_unavailable{kube_namespace:prod} by {deployment} > 0'
```

**Aggregator cheat sheet for tuning sensitivity:**

| Aggregator | Behavior | Best for |
| --- | --- | --- |
| `max` | Fires on any spike in the window | High-sensitivity, never-miss alerts |
| `avg` | Fires when average exceeds threshold | Sustained-load alerts |
| `min` | Fires only if threshold exceeded for entire window | Filtering transient blips (deploys, restarts) |

## create-monitor Options

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
dd-cli create-log-metric my_app.errors \
  --query 'service:my-app status:error' \
  --group-by service --group-by env

# 2. Create metric monitor on it
dd-cli create-monitor \
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
dd-cli create-monitor \
  --tag managed-by:dd-cli \
  --tag monitor-key:my-unique-key \
  ...
```
