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
dd-cli validate

# Search logs
dd-cli search-logs 'env:prod error' --from now-1h

# Get incident
dd-cli get-incident 152 --enrich

# List Software Catalog services owned by a team
dd-cli list-catalog-entities --kind service --owner platform-team --include raw_schema

# Fetch one catalog entity by ref
dd-cli get-catalog-entity service:example-service --include raw_schema

# Search Datadog Teams and find a user's teams
dd-cli list-teams --query platform
dd-cli find-user-teams user@example.com
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for full documentation, including:
- Command reference
- Configuration details
- Detailed guides for auth, logs, and incidents
