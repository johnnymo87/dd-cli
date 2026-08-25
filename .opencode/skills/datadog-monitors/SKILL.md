---
name: datadog-monitors
description: Create, inspect, update and delete Datadog monitors via API - get monitor details by ID or URL, create or update metric/query alerts with thresholds and Slack notifications, delete a monitor safely. Use when setting up alerting, tuning monitors, investigating monitor triggers, checking monitor group states, or removing a monitor you created by mistake.
---

# Datadog Monitors

## List Monitors

```bash
# All monitors managed by dd-cli (most common discovery query)
dd-cli list-monitors --tag managed-by:dd-cli

# Multiple tags AND together (DD-side comma-join)
dd-cli list-monitors --tag managed-by:dd-cli --tag team:platform

# The monitor's own tags (--tag) vs the scope it watches (--scope-tag)
dd-cli list-monitors --tag team:platform          # tagged as owned by the team
dd-cli list-monitors --scope-tag env:prod         # queries {env:prod}
dd-cli list-monitors --tag team:platform --scope-tag env:prod   # both (AND)

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
| `--tag` | - | Filter by the monitor's **own** tag (repeatable, AND-combined). Sent as DD's `monitor_tags`. |
| `--scope-tag` | - | Filter by the **scope the monitor watches** -- a tag inside its query (repeatable, AND-combined). Sent as DD's `tags`. |
| `--name` | - | Filter by name substring (server-side, case-insensitive) |
| `--max-results` | `10000` | Cap total results. Auto-pagination stops here even if more pages exist, and marks the answer truncated (exit 3). |
| `--format` | `summary` | Output: `summary` (id/name/type/overall_state/tags), `json` (full, wrapped), `jsonl` (full, one per line) |
| `--timeout` | `15` | Request timeout in seconds |

**Tag flavors gotcha — read this before trusting an empty result.** Datadog's
`GET /api/v1/monitor` takes two tag parameters that answer different questions,
and this description used to be backwards in both this skill and the CLI:

| Parameter | dd-cli flag | Matches |
| --- | --- | --- |
| `monitor_tags` | `--tag` | the monitor's **own** tags -- where ownership lives (`team:`, `managed-by:`, `feature:`, `product:`, `domain:`) |
| `tags` | `--scope-tag` | the **scope** the monitor watches, i.e. tags appearing inside its query (`env:prod`, `service:foo`) |

Until 2026-08-09 `--tag` was wired to `tags`, so every ownership-tag audit
returned an **empty set that looked exactly like a clean result**. Measured in
prod at the time: `--tag env:prod` returned 221 monitors, all 221 of which had
`env:prod` in their *query* but only 191 of which carried it as a tag (one had
no tags at all); `--tag team:ba-fulfillment` returned 0 while 39 monitors
carried that tag. **Any conclusion of the form "we checked, nothing matched
team:X" made with an older dd-cli is worthless — re-run it.**

A tag-filtered run returning 0 now prints a stderr note naming which predicate
ran, and `--format json`/`summary` carries a `filters` object echoing it.

**Pagination:** auto-paginates 1000 monitors per page until `--max-results` is reached or a short page is returned. The default cap is 10000, i.e. up to 10 API calls.

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

# DEAD-MAN SWITCH (no-signal monitor). BOTH halves are required:
# a count query that goes silent evaluates to No Data, NOT to 0, so the
# '< 1' threshold alone never fires. notify_no_data + no_data_timeframe
# is what actually pages.
dd-cli create-monitor \
  --name 'offset scanner: NO SIGNAL — silent >45m [{{env.name}}]' \
  --type 'trace-analytics alert' \
  --query 'trace-analytics("service:offset-scanner operation_name:scan").rollup("count").by("env").last("45m") < 1' \
  --message '{{#is_alert}}Scanner silent for 45m (expected every 15m){{/is_alert}} @slack-alerts' \
  --notify-no-data --no-data-timeframe 60 \
  --new-group-delay 300 --renotify-interval 60 \
  --priority 2
```

### Monitor options: flags, escape hatch, precedence

Both `create-monitor` and `update-monitor` share the same set of
`options` flags:

| Flag | `options` key | Notes |
| --- | --- | --- |
| `--critical` / `--warning` | `thresholds.critical` / `.warning` | |
| `--critical-recovery` / `--warning-recovery` | `thresholds.*_recovery` | Anti-flapping |
| `--notify-no-data` / `--no-notify-no-data` | `notify_no_data` | Requires `--no-data-timeframe` |
| `--no-data-timeframe N` | `no_data_timeframe` | Minutes; use >= 2x the query window |
| `--on-missing-data` | `on_missing_data` | Newer API; supersedes the two above |
| `--new-group-delay N` | `new_group_delay` | Seconds; grace period for new groups |
| `--evaluation-delay N` | `evaluation_delay` | Seconds; for late/backfilled data |
| `--notify-audit` / `--no-notify-audit` | `notify_audit` | |
| `--include-tags` / `--no-include-tags` | `include_tags` | DD default is `true` |
| `--require-full-window` / `--no-require-full-window` | `require_full_window` | Off for sparse metrics |
| `--timeout-h N` | `timeout_h` | Auto-resolve after N hours |
| `--renotify-interval N` | `renotify_interval` | Minutes, 0 disables |
| `--renotify-occurrences N` | `renotify_occurrences` | Needs `--renotify-interval` |
| `--renotify-status S` | `renotify_statuses` | Repeatable: `alert`, `warn`, `no data` |
| `--escalation-message` | `escalation_message` | Needs `--renotify-interval` |
| `--group-retention-duration` | `group_retention_duration` | e.g. `2d` (60m-72h) |
| `--notification-preset-name` | `notification_preset_name` | `show_all`, `hide_query`, ... |
| `--option KEY=VALUE` | any key | Repeatable escape hatch |

`--option` values are JSON-parsed when they look like JSON, so numbers,
booleans, `null`, arrays and objects all work; anything else stays a
string:

```bash
dd-cli create-monitor ... \
  --option 'notify_by=["env"]' \
  --option 'scheduling_options={"evaluation_window": {"day_starts": "04:00"}}' \
  --option min_location_failed=2
```

**Precedence (highest first):**
1. first-class flags (e.g. `--no-data-timeframe 60`)
2. `--option KEY=VALUE`
3. the monitor's existing options (`update-monitor` only)
4. Datadog defaults

### The no-data guard

dd-cli refuses `notify_no_data: true` without an explicit
`no_data_timeframe`. Datadog nominally defaults the timeframe (2x the
evaluation window; 24h for service checks), but that implicit default has
been observed *not* to page on a silent trace-analytics group -- a
dead-man switch that is itself silently dead. Fix with
`--no-data-timeframe N` or `--on-missing-data show_and_notify_no_data`.

On `update-monitor` the guard only fires when you actually touch a no-data
option (`--notify-no-data`, `--no-data-timeframe`, `--on-missing-data`).
If the monitor is already in that broken state and you are changing
something unrelated, dd-cli warns on stderr and proceeds.

### The two no-data mechanisms are mutually exclusive

Verified against `POST /api/v1/monitor/validate` (non-mutating):

```
notify_no_data + no_data_timeframe              -> OK  (legacy pair)
on_missing_data                                 -> OK  (modern)
on_missing_data + notify_no_data:true           -> 400
on_missing_data + no_data_timeframe             -> 400
on_missing_data + notify_no_data:false          -> OK
```

> "The notify_no_data option is deprecated and cannot be used in
> combination with the on_missing_data option."

dd-cli refuses that combination up front, and on update it drops the
*other* family from the merged options so a read-modify-write cannot
produce a payload Datadog rejects: ask for `--on-missing-data` and the
legacy keys are removed; ask for the legacy pair and `on_missing_data` is
removed.

### Other update-monitor semantics

- `--critical` / `--warning` patch individual thresholds; pass
  `--option 'thresholds={"critical": 5}'` to replace the whole object
  (the only way to *remove* a threshold).
- `--option KEY=null` clears an option.
- The read-modify-write GET->PUT is not atomic (no ETag on the monitor
  API): a concurrent edit between the two calls is overwritten.
- `--option` rejects Python-flavoured literals (`True`, `None`) and
  `NaN`/`Infinity` rather than shipping them as strings.

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

Top-level fields you do not pass are left unchanged.

**Options are merged, not clobbered.** Datadog's `PUT /api/v1/monitor/{id}`
replaces the whole `options` object when the body contains one, so a naive
partial update resets everything it omits (thresholds, `no_data_timeframe`,
`evaluation_delay`, ...). Whenever an option flag is passed, dd-cli first
`GET`s the monitor and PUTs its existing options with your changes applied on
top (`thresholds` is merged one level deeper too).

```bash
# Repair a broken dead-man switch without touching its other options
dd-cli update-monitor 12345678 --notify-no-data --no-data-timeframe 60

# Replace the tag list (pass every tag you want to keep)
dd-cli update-monitor 12345678 --tag managed-by:dd-cli --tag team:platform
```

### update-monitor Options

| Option | Description |
| --- | --- |
| `--name` | Update monitor name |
| `--query` | Update monitor query |
| `--message` | Update notification message |
| `--tag` | Monitor tag (repeatable); REPLACES the existing tag list |
| `--priority` | Update priority (1-5) |
| *(all shared option flags)* | See "Monitor options" above -- same flags as `create-monitor` |
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

## Delete a Monitor

```bash
# By numeric ID or by URL. --yes is required; there is no interactive prompt.
dd-cli delete-monitor 25391362 --yes
dd-cli delete-monitor 'https://us3.datadoghq.com/monitors/25391362' --yes

# Only after reading the refusal: delete despite an SLO/composite reference
dd-cli delete-monitor 25391362 --yes --force
```

**There is no undo.** Datadog will not return a deleted monitor through the
API, so dd-cli reads the monitor before deleting it and puts the whole
definition in the envelope under `data.definition` -- and on the *failure*
envelope too, because a DELETE that fails may still have landed. That output is
your only backup; keep it before you close the terminal.

**A 404 is an error, not a quiet success.** The same 404 means "already
deleted", "wrong ID", *and* "`DD_SITE` points at the wrong region while the
monitor is alive in another one". Only one of those makes "deleted" a true
statement, so the command refuses to claim it.

**Reference refusals.** Datadog answers 400 when an SLO or a composite monitor
references the monitor:

```
monitor [25391362,name] is referenced in slos: [34dbd...,availability]
monitor [25391362,name] is referenced in composite monitors: [37050226,rollup]
```

dd-cli attaches a `hint` naming `--force`. Prefer fixing the referencing SLO or
composite first: `--force` does not clean the reference up, it leaves the SLO or
composite pointing at a monitor that no longer exists.

**No bulk form, deliberately.** One monitor per invocation, each with its own
exit code. Loop in the shell if you must -- but a half-finished bulk delete has
no honest way to report itself.

### delete-monitor Options

| Option | Required | Description |
| --- | --- | --- |
| `--yes` | yes | Confirm the deletion. No interactive prompt exists. |
| `--force` | no | Delete despite an SLO/composite reference, leaving it dangling |
| `--site` | no | Datadog site (defaults to `DD_SITE`) |
| `--timeout` | no | Request timeout in seconds |

- **Endpoint**: `DELETE /api/v1/monitor/{monitor_id}` (`?force=true` with `--force`)
- **Response**: `{"deleted_monitor_id": <id>}`, cross-checked against the ID you asked for

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
| *(all shared option flags)* | no | See "Monitor options" above (`--no-data-timeframe`, `--new-group-delay`, `--option KEY=VALUE`, ...) |

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
