---
name: datadog-error-tracking
description: Search and manage Datadog Error Tracking issues via API - find grouped errors by service, resolve/ignore issues. Use when investigating production errors or managing error tracking issue states.
---

# Datadog Error Tracking

## CLI Commands

```bash
# Search issues by service (last 7 days)
dd search-et-issues 'service:my-service-*' --from now-7d

# Search with specific error type
dd search-et-issues 'service:my-service AND @error.type:NullPointerException' --from now-1d

# Search logs-based error tracking (instead of APM traces)
dd search-et-issues 'service:my-service' --track logs

# Sort by most frequent
dd search-et-issues 'service:my-service' --from now-7d --order-by TOTAL_COUNT

# Get a specific issue with details
dd get-et-issue c1726a66-1f64-11ee-b338-da7ad0900002

# Resolve an issue
dd update-et-issue-state c1726a66-... RESOLVED

# Ignore an issue (suppresses monitor notifications)
dd update-et-issue-state c1726a66-... IGNORED

# Reopen a resolved/ignored issue
dd update-et-issue-state c1726a66-... OPEN
```

## search-et-issues Options

| Option | Default | Description |
| --- | --- | --- |
| `QUERY` | (required) | Search query (Datadog search syntax) |
| `--from` | `now-1d` | Start time (`now-1h`, `now-7d`, or epoch ms) |
| `--to` | `now` | End time |
| `--track` | `trace` | Source: `trace` (APM), `logs`, `rum` |
| `--order-by` | - | `TOTAL_COUNT`, `FIRST_SEEN`, `IMPACTED_SESSIONS`, `PRIORITY` |

## Issue States

| State | Meaning |
| --- | --- |
| `OPEN` | New / for review |
| `RESOLVED` | Fixed |
| `IGNORED` | Suppressed from monitor alerts |

**Tip:** Ignoring an issue automatically mutes it from Error Tracking monitor notifications.

## Error Tracking Monitors

Error Tracking has its own monitor type (`error-tracking alert`) separate from metric/log monitors. These use a special query DSL:

```
# New issue monitor (fires on first occurrence or regression)
error-tracking("env:prod service:my-service-*").source("all").new().rollup("count").by("issue.id").last("1d") > 0

# High impact monitor (fires on high error count)
error-tracking("env:prod service:my-service").source("all").impact().rollup("count").by("issue.id").last("5m") > 1
```

**Wildcard tip:** Use `service:my-prefix-*` to automatically cover all current and future deployables without updating the monitor each time a new service is added.

## API Details

- **Endpoint**: `POST /api/v2/error-tracking/issues/search`
- **Get issue**: `GET /api/v2/error-tracking/issues/{issue_id}`
- **Update state**: `PUT /api/v2/error-tracking/issues/{issue_id}/state`
- **Permission**: `error_tracking_read` / `error_tracking_write`

## Common Patterns

```bash
# Triage: find all open issues for a service, sorted by frequency
dd search-et-issues 'service:my-service state:OPEN' --from now-7d --order-by TOTAL_COUNT

# Investigate: get full details on a specific issue
dd get-et-issue <issue-id>

# Bulk resolve: pipe issue IDs through update
dd search-et-issues 'service:my-service' --from now-30d | \
  jq -r '.included[]? | select(.attributes.state == "OPEN") | .id' | \
  xargs -I{} dd update-et-issue-state {} RESOLVED
```
