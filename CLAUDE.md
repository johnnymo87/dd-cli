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
| `dd-cli count-logs QUERY` | Count logs, optionally bucketed (`--bucket 1h`) in one invocation |
| `dd-cli get-incident ID` | Get incident by ID (with optional `--enrich`) |
| `dd-cli update-incident ID` | Update incident fields |
| `dd-cli create-log-metric ID` | Create a log-based count metric (works with flex tier) |
| `dd-cli create-monitor` | Create a monitor (metric/query/trace-analytics alert) with full `options` support |
| `dd-cli get-monitor ID_OR_URL` | Get a monitor's details by ID or URL |
| `dd-cli list-monitors` | List monitors, filtered by tag and/or name (auto-paginates) |
| `dd-cli update-monitor ID_OR_URL` | Update a monitor's query, name, tags, thresholds and `options` (merge, not clobber) |
| `dd-cli create-dashboard` | Create a dashboard from a `--spec` JSON body (+ title/tags flags) |
| `dd-cli get-dashboard ID_OR_URL` | Get a dashboard's full definition by ID or URL |
| `dd-cli update-dashboard ID_OR_URL` | Update (full replace) a dashboard from a `--spec` JSON body (+ title/tags flags) |
| `dd-cli list-dashboards` | List dashboards, optionally filtered by title |
| `dd-cli list-teams` | List/search Datadog Teams by name, handle, or member email |
| `dd-cli find-user-teams MEMBER` | Find Datadog Teams matching a user/member email or name |
| `dd-cli list-team-notification-rules HANDLE` | List team notification routing rules, including PagerDuty handles |
| `dd-cli list-slos` | List SLOs with optional tag filtering |
| `dd-cli get-slo ID` | Get SLO details and history (SLI value, error budget) |
| `dd-cli query-metrics QUERY` | Query a metric timeseries; per-series scope with first/last/min/max/avg |
| `dd-cli search-metrics TERM` | Find metric names containing TERM |
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
| `--all-pages` | - | Fetch all pages (up to `--max-pages`) |
| `--max-pages` | `50` | Page cap; hitting it marks the result truncated |
| `--on-truncation` | `exit3` | `exit3`, `warn`, or `error` when the answer is incomplete |
| `--max-results` | - | Stop after N results (use with `--all-pages`) |
| `--timeout` | `15` | Request timeout in seconds (increase for flex) |
| `--format` | `json` | Output: `json`, `jsonl`, `messages` |

### query-metrics Options

| Option | Default | Description |
| --- | --- | --- |
| `--from` | `now-1h` | Start time (e.g., `now-20m`, `now-7d`, or epoch seconds) |
| `--to` | `now` | End time |
| `--format` | `summary` | Output: `summary`, `json` (raw response), `jsonl` (one raw series per line) |
| `--timeout` | `30` | Request timeout in seconds |

The `summary` format reports each series by tag scope with `first`, `last`,
`last_ts`, `min`, `max`, `avg`, and how many of its points were non-null. See
the **datadog-metrics** skill for the rollup, null, and metric-name traps.

### search-metrics Options

| Option | Default | Description |
| --- | --- | --- |
| `--limit` | `100` | Max names to print; `total` still reports every match |
| `--timeout` | `15` | Request timeout in seconds |

Matching is a literal substring over recently-reporting metrics, so `.` is not
a wildcard and absence is not proof a metric does not exist.

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
- **datadog-metrics** - Query metric timeseries, find metric names, rollup and null-handling traps
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

## Output Contract: Errors Must Never Be Representable As Data

**A failed request must never surface as `0`, `[]`, `null`, or a short page.**

This is not a style preference. A Datadog 429 recorded as a count of 0 once led
an analysis to conclude a failure population stopped 23 hours *before* the
deploy that fixed it. The zero is not usually manufactured inside dd-cli -- it
is manufactured at the process boundary:

```bash
n=$(dd-cli search-logs ... | jq '.count')   # on failure: empty stdin, jq prints nothing, exits 0
total=$(( total + ${n:-0} ))                # <- the 0 is manufactured HERE
```

The pipeline's exit status is `jq`'s, not dd-cli's. So:

1. **Every data-producing command emits a parseable envelope on stdout even
   when it fails**, with `"ok": false` and `data`/`count` set to `null` (never
   `[]` or `0` -- a zero is a claim about the world, and a failed request
   observed nothing). Exit status is still non-zero.
2. **Incompleteness is machine-detectable.** `--format json` always carries
   `truncated` and `truncation_reason`. Stopping short exits **3**.
   `--on-truncation=exit3|warn|error` overrides.
3. **Never `except: pass` around a data-producing call.** Record the failure in
   the output (see `enrichment.errors[]` / `partial`).
4. **Never count lines of a text output format.** `jsonl` and `messages` cannot
   carry the flag in-band; they signal via stderr and exit code. For
   programmatic or agent consumption use `--format json`.
5. **Prefer one invocation over a shell loop.** `count-logs --bucket 1h` exists
   because per-iteration invocations each get their own chance to fail silently.

The `truncated` boolean is deliberately conservative -- true whenever a cap bit.
Page/offset paginators cannot distinguish "exactly full" from "more exists"
without another request, so the precision lives in `truncation_reason`
(`more_available`, `max_pages`, `max_results_boundary_unknown`,
`server_timeout`), not in the boolean.

### What the envelope does and does not protect

Verified against the real CLI with a forced failure:

| Caller pattern | Detects the failure? |
| --- | --- |
| `dd-cli ...; echo $?` | yes (exit 1) |
| `set -o pipefail` around the pipeline | yes (exit 1) |
| `... \| jq -e '.count'` | yes (jq -e exits 1 on null) |
| `.ok` / `.truncated` field check | yes |
| `n=$(... \| jq '.count'); total=$((total + ${n:-0}))` | **NO -- still 0** |

The last row is the incident-1 pattern and it is *not* fixed by the envelope.
`jq '.count'` now prints `null` instead of nothing, but bash evaluates
`$((0 + null))` as `0` because it treats a bare identifier as an unset variable.
No value dd-cli can print on stdout defeats shell arithmetic on scraped text.

So the envelope protects consumers that read *structure* -- which is this tool's
actual audience. If you must scrape in shell, use `set -o pipefail` and check
`$?`, or branch on `.ok`. Better: use `count-logs --bucket` and do not write the
loop at all.

### Reliability

`http.py` retries 429/5xx with backoff, honoring `Retry-After` and
`X-RateLimit-Reset`. Use `_read()` for reads and `_write()` for state-changing
calls -- never call `_request()` directly. `_write` retries only a 429 carrying
Datadog's rate-limit headers, and only once, because a bare 429 may come from an
intermediary that emitted it after the origin already accepted the write.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DD_MAX_RETRIES` | `5` | Retries after the initial attempt |
| retry budget | `max(120, 4 x timeout)` | Total wall-clock ceiling for sleeps |
| exit `3` | - | Command worked; the answer is incomplete |
