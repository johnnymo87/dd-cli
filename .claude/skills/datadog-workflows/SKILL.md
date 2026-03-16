---
name: datadog-workflows
description: Fetch Datadog Workflow Automation definitions and instances via API. Use when inspecting workflow configurations or execution history.
---

# Datadog Workflows

## CLI Command

```bash
# Get workflow by UUID
dd get-workflow aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee

# Get workflow from full URL (parses workflow ID automatically)
dd get-workflow 'https://us3.datadoghq.com/workflow/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

# Include instance details (if URL has ?instance=... param)
dd get-workflow 'https://us3.datadoghq.com/workflow/aaaaaaaa-...?instance=11111111-...' --instance
```

## Options

| Option | Description |
| --- | --- |
| `WORKFLOW` | Workflow UUID or full Datadog URL |
| `--instance` | Also fetch the execution instance details |
| `--timeout` | Request timeout in seconds (default: 15) |

## Understanding the Response

A workflow definition contains:

- `attributes.name` / `attributes.description` -- what it does
- `attributes.spec.triggers` -- what triggers it (monitor, webhook, schedule)
- `attributes.spec.steps` -- the step definitions (Slack messages, Jira tickets, PagerDuty pages, etc.)
- `attributes.spec.inputSchema.parameters` -- configurable inputs (Slack channel, thresholds, etc.)
- `attributes.spec.handle` -- unique trigger handle

## API Details

- **Endpoint**: `GET /api/v2/workflows/{workflow_id}`
- **Instances**: `GET /api/v2/workflows/{workflow_id}/instances/{instance_id}`
- **No list-all endpoint** -- you need the workflow UUID in advance
- **Two UUID types**: `workflow_id` (definition) vs `instance_id` (execution run)

## URL Parsing

The CLI parses Datadog workflow URLs automatically:

```
https://us3.datadoghq.com/workflow/<workflow_id>?instance=<instance_id>
                                   ^^^^^^^^^^^^           ^^^^^^^^^^^
                                   extracted              extracted (optional)
```
