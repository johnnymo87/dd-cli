# dd-cli

CLI for Datadog APIs (incidents, logs, and more).

## Quick Start

```bash
# Install
uv venv && uv pip install -e .[dev]

# Configure
export DD_SITE="us3.datadoghq.com"
export DD_API_KEY="<your-api-key>"
export DD_APP_KEY="<your-app-key>"

# Validate
dd validate

# Search logs
dd search-logs 'env:prod error' --from now-1h

# Get incident
dd get-incident 152 --enrich
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for full documentation, including:
- Command reference
- Configuration details
- Detailed guides for auth, logs, and incidents
