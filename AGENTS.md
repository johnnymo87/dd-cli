# dd-cli

CLI for Datadog APIs -- incidents, logs, monitors, error tracking, workflows, and more.

## Quick Start

**Workstation-managed** (recommended):
DD env vars (`DD_SITE`, `DD_API_KEY`, `DD_APP_KEY`) are set by shell init. `dd` is installed as an editable tool via home-manager activation.

```bash
# Validate credentials
dd validate

# Search logs
dd search-logs 'env:prod service:my-service error' --from now-1h
```

**Standalone:**
```bash
uv venv && uv pip install -e .[dev]

# Configure (copy .envrc.example to .envrc and set values)
export DD_SITE="us3.datadoghq.com"
export DD_API_KEY="<32-hex>"
export DD_APP_KEY="<40-hex>"

dd validate
```

## Commands

| Command | Description |
|---------|-------------|
| `dd validate` | Validate API key |
| `dd search-logs QUERY` | Search logs with Datadog query syntax |
| `dd get-incident ID` | Get incident by ID (with optional `--enrich`) |
| `dd update-incident ID` | Update incident fields |
| `dd create-log-metric ID` | Create a log-based count metric (works with flex tier) |
| `dd create-monitor` | Create a monitor (metric alert, query alert, etc.) |
| `dd get-monitor ID_OR_URL` | Get a monitor's details by ID or URL |
| `dd update-monitor ID_OR_URL` | Update a monitor's query, name, thresholds, etc. |
| `dd list-slos` | List SLOs with optional tag filtering |
| `dd get-slo ID` | Get SLO details and history (SLI value, error budget) |
| `dd get-workflow ID_OR_URL` | Get a workflow definition by ID or URL |
| `dd search-et-issues QUERY` | Search error tracking issues by service/error type |
| `dd get-et-issue ID` | Get a single error tracking issue with details |
| `dd update-et-issue-state ID STATE` | Update issue state (OPEN, RESOLVED, IGNORED) |

Run `dd --help` or `dd <command> --help` for details.

## Skills

| Skill | Description |
|-------|-------------|
| [datadog-auth](.opencode/skills/datadog-auth/SKILL.md) | Troubleshoot 401/403 errors, understand keys and regions |
| [datadog-logs](.opencode/skills/datadog-logs/SKILL.md) | Log search syntax, storage tiers (flex), pagination |
| [datadog-incidents](.opencode/skills/datadog-incidents/SKILL.md) | Incident enrichment, update fields, API patterns |
| [datadog-log-metrics](.opencode/skills/datadog-log-metrics/SKILL.md) | Log-based count metrics (ingestion-time, works with flex tier) |
| [datadog-monitors](.opencode/skills/datadog-monitors/SKILL.md) | Create, inspect, and update monitors (thresholds, group states, notifications) |
| [datadog-workflows](.opencode/skills/datadog-workflows/SKILL.md) | Fetch workflow definitions and execution instances |
| [datadog-error-tracking](.opencode/skills/datadog-error-tracking/SKILL.md) | Search/manage error tracking issues, resolve/ignore states |

## Configuration

| Env Var | Description |
|---------|-------------|
| `DD_SITE` | Datadog site (e.g., `us3.datadoghq.com`) |
| `DD_API_KEY` | API key (32-hex value, not UUID) |
| `DD_APP_KEY` | Application key (40-hex value, not UUID) |

## Architecture

```
dd_cli/
├── cli.py    # Click commands
└── http.py   # DatadogClient class (httpx-based)
```

The `DatadogClient` class handles authentication, request/response, and error handling. CLI commands are thin wrappers that format output.
