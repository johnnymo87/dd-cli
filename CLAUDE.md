# dd-cli

CLI for Datadog APIs (incidents, logs, and more).

## Quick Start

### Workstation-managed (primary)

If you use the workstation repo (macOS or cloudbox),
`dd-cli` is already installed via home-manager activation (`uv tool install --editable`) and credentials
(`DD_SITE`, `DD_API_KEY`, `DD_APP_KEY`) are exported by shell init.

```bash
# Open a new terminal — dd-cli is already available
dd-cli validate

# Search logs
dd-cli search-logs 'env:prod service:my-service error' --from now-1h

# Get incident
dd-cli get-incident 152 --enrich
```

Source changes are reflected immediately (editable install). Only re-run `home-manager switch` if
`pyproject.toml` dependencies change.

### Standalone (fallback)

For users without the workstation setup:

```bash
# Install
uv venv && uv pip install -e .[dev]

# Configure (copy .envrc.example to .envrc and set values)
export DD_SITE="us3.datadoghq.com"
export DD_API_KEY="<32-hex>"
export DD_APP_KEY="<40-hex>"

# Validate credentials
dd-cli validate

# Search logs
dd-cli search-logs 'env:prod service:my-service error' --from now-1h

# Get incident
dd-cli get-incident 152 --enrich
```

## Commands

| Command | Description |
| --- | --- |
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
| `dd-cli list-teams` | List/search Datadog Teams by name, handle, or member email |
| `dd-cli find-user-teams MEMBER` | Find Datadog Teams matching a user/member email or name |
| `dd-cli list-team-notification-rules HANDLE` | List team notification routing rules, including PagerDuty handles |
| `dd-cli list-slos` | List SLOs with optional tag filtering |
| `dd-cli get-slo ID` | Get SLO details and history (SLI value, error budget) |
| `dd-cli get-workflow ID_OR_URL` | Get a workflow definition by ID or URL |
| `dd-cli search-et-issues QUERY` | Search error tracking issues by service/error type |
| `dd-cli get-et-issue ID` | Get a single error tracking issue with details |
| `dd-cli update-et-issue-state ID STATE` | Update issue state (OPEN, RESOLVED, IGNORED) |
| `dd-cli validate-catalog` | Validate local Software Catalog PagerDuty metadata |
| `dd-cli list-catalog-pagerduty-links` | List local Catalog PagerDuty service URLs |
| `dd-cli list-catalog-entities` | List Software Catalog entities with optional filters |
| `dd-cli get-catalog-entity REF` | Get one Software Catalog entity by ref or name |
| `dd-cli get-catalog-oncall REF` | Read Datadog's on-call relationship for one Catalog entity |
| `dd-cli check-pagerduty-service SERVICE_NAME` | Check a known Datadog PagerDuty integration service handle |

Software Catalog commands are read-only. Source-of-truth changes should happen through repository-backed `entity.datadog.yaml` PRs, not through Datadog write APIs.

Teams commands are read-only. Examples:

```bash
dd-cli list-teams --query platform
dd-cli find-user-teams user@example.com
```

PagerDuty helpers are Datadog-only and read-only. `validate-catalog` expects
Software Catalog v3 PagerDuty links to use `integrations.pagerduty.serviceURL`.
Authoritative PagerDuty schedules and current PagerDuty responders require
PagerDuty API credentials, so `dd-cli` does not expose them yet.

### search-logs Options

| Option | Default | Description |
| --- | --- | --- |
| `--from` | `now-15m` | Start time (e.g., `now-1h`, `now-7d`) |
| `--to` | `now` | End time |
| `--limit` | `100` | Max logs per page |
| `--storage-tier` | - | Storage tier: `indexes`, `flex`, `online-archives` |
| `--all-pages` | - | Fetch all pages (up to 50) |
| `--max-results` | - | Stop after N results (use with `--all-pages`) |
| `--timeout` | `15` | Request timeout in seconds (increase for flex) |
| `--format` | `json` | Output: `json`, `jsonl`, `messages` |

Run `dd-cli --help` or `dd-cli <command> --help` for details.

## Configuration

| Env Var | Description |
| --- | --- |
| `DD_SITE` | Datadog site (e.g., `us3.datadoghq.com`) |
| `DD_API_KEY` | API key (32-hex value, not UUID) |
| `DD_APP_KEY` | Application key (40-hex value, not UUID) |

## Skills (Detailed Guides)

These are available as skills in `.claude/skills/` (Claude Code) and `.opencode/skills/` (OpenCode):

- **datadog-auth** - Troubleshoot 401/403 errors, understand keys and regions
- **datadog-logs** - Log search syntax, storage tiers (flex), pagination
- **datadog-incidents** - Incident enrichment, update fields, API patterns
- **datadog-log-metrics** - Log-based count metrics (ingestion-time, works with flex tier)
- **datadog-monitors** - Create and inspect monitors (thresholds, group states, Slack notifications)
- **datadog-slos** - List SLOs, inspect SLI values, error budgets, and threshold history
- **datadog-workflows** - Fetch workflow definitions and execution instances
- **datadog-error-tracking** - Search/manage error tracking issues, resolve/ignore states

## Development

If you use the workstation setup, `dd-cli` itself is already installed — the steps below are only needed
for setting up pre-commit hooks and running linting from a local virtualenv.

```bash
# Install with dev deps (creates local .venv for pre-commit etc.)
uv venv && uv pip install -e .[dev]

# Run linting/formatting
uv run pre-commit run --all-files

# Install pre-commit hooks
uv run pre-commit install
```

## Architecture

```
dd_cli/
├── cli.py    # Click commands
└── http.py   # DatadogClient class (httpx-based)
```

The `DatadogClient` class handles authentication, request/response, and error handling. CLI commands are thin wrappers that format output.
