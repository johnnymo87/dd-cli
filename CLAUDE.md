# dd-cli

CLI for Datadog APIs (incidents, logs, and more).

## Quick Start

```bash
# Install
uv venv && uv pip install -e .[dev]

# Configure (copy .envrc.example to .envrc and set values)
export DD_SITE="us3.datadoghq.com"
export DD_API_KEY="<32-hex>"
export DD_APP_KEY="<40-hex>"

# Validate credentials
dd validate

# Search logs
dd search-logs 'env:prod service:my-service error' --from now-1h

# Get incident
dd get-incident 152 --enrich
```

## Commands

| Command | Description |
| --- | --- |
| `dd validate` | Validate API key |
| `dd search-logs QUERY` | Search logs with Datadog query syntax |
| `dd get-incident ID` | Get incident by ID (with optional `--enrich`) |
| `dd update-incident ID` | Update incident fields |

Run `dd --help` or `dd <command> --help` for details.

## Configuration

| Env Var | Description |
| --- | --- |
| `DD_SITE` | Datadog site (e.g., `us3.datadoghq.com`) |
| `DD_API_KEY` | API key (32-hex value, not UUID) |
| `DD_APP_KEY` | Application key (40-hex value, not UUID) |

## Skills (Detailed Guides)

These are available as Claude Code skills in `.claude/skills/`:

- **datadog-auth** - Troubleshoot 401/403 errors, understand keys and regions
- **datadog-logs** - Log search syntax, storage tiers (flex), pagination
- **datadog-incidents** - Incident enrichment, update fields, API patterns

## Development

```bash
# Install with dev deps
uv pip install -e .[dev]

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
