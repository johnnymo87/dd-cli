# dd-cli

CLI for Datadog APIs -- incidents, logs, monitors, error tracking, workflows, and more.

## Quick Start

**Workstation-managed** (recommended):
DD env vars (`DD_SITE`, `DD_API_KEY`, `DD_APP_KEY`) are set by shell init. `dd-cli` is installed as an editable tool via home-manager activation.

```bash
# Validate credentials
dd-cli validate

# Search logs
dd-cli search-logs 'env:prod service:my-service error' --from now-1h
```

**Standalone:**
```bash
uv venv && uv pip install -e .[dev]

# Configure (copy .envrc.example to .envrc and set values)
export DD_SITE="us3.datadoghq.com"
export DD_API_KEY="<32-hex>"
export DD_APP_KEY="<40-hex>"

dd-cli validate
```

## Commands

| Command | Description |
|---------|-------------|
| `dd-cli validate` | Validate API key |
| `dd-cli search-logs QUERY` | Search logs with Datadog query syntax |
| `dd-cli get-incident ID` | Get incident by ID (with optional `--enrich`) |
| `dd-cli update-incident ID` | Update incident fields |
| `dd-cli create-log-metric ID` | Create a log-based count metric (works with flex tier) |
| `dd-cli create-monitor` | Create a monitor (metric alert, query alert, etc.) |
| `dd-cli get-monitor ID_OR_URL` | Get a monitor's details by ID or URL |
| `dd-cli list-monitors` | List monitors, filtered by tag and/or name (auto-paginates) |
| `dd-cli update-monitor ID_OR_URL` | Update a monitor's query, name, thresholds, etc. |
| `dd-cli create-dashboard` | Create a dashboard from a `--spec` JSON body (+ title/tags flags) |
| `dd-cli get-dashboard ID_OR_URL` | Get a dashboard's full definition by ID or URL |
| `dd-cli list-dashboards` | List dashboards, optionally filtered by title |
| `dd-cli list-slos` | List SLOs with optional tag filtering |
| `dd-cli get-slo ID` | Get SLO details and history (SLI value, error budget) |
| `dd-cli get-workflow ID_OR_URL` | Get a workflow definition by ID or URL |
| `dd-cli search-et-issues QUERY` | Search error tracking issues by service/error type |
| `dd-cli get-et-issue ID` | Get a single error tracking issue with details |
| `dd-cli update-et-issue-state ID STATE` | Update issue state (OPEN, RESOLVED, IGNORED) |
| `dd-cli list-catalog-entities` | List Software Catalog entities with optional filters |
| `dd-cli get-catalog-entity REF` | Get one Software Catalog entity by ref or name |

Software Catalog commands are read-only. Source-of-truth changes should happen through repository-backed `entity.datadog.yaml` PRs, not through Datadog write APIs.

Run `dd-cli --help` or `dd-cli <command> --help` for details.

## Skills

| Skill | Description |
|-------|-------------|
| [datadog-auth](.opencode/skills/datadog-auth/SKILL.md) | Troubleshoot 401/403 errors, understand keys and regions |
| [datadog-logs](.opencode/skills/datadog-logs/SKILL.md) | Log search syntax, storage tiers (flex), pagination |
| [datadog-incidents](.opencode/skills/datadog-incidents/SKILL.md) | Incident enrichment, update fields, API patterns |
| [datadog-log-metrics](.opencode/skills/datadog-log-metrics/SKILL.md) | Log-based count metrics (ingestion-time, works with flex tier) |
| [datadog-monitors](.opencode/skills/datadog-monitors/SKILL.md) | Create, inspect, and update monitors (thresholds, group states, notifications) |
| [datadog-slos](.opencode/skills/datadog-slos/SKILL.md) | List SLOs, inspect SLI values, error budgets, and threshold history |
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
