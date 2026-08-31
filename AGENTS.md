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
| `dd-cli count-logs QUERY` | Count logs, optionally bucketed (`--bucket 1h`) in one invocation |
| `dd-cli get-incident ID` | Get incident by ID (with optional `--enrich`) |
| `dd-cli update-incident ID` | Update incident fields |
| `dd-cli create-log-metric ID` | Create a log-based metric -- `count` or `distribution` (works with flex tier) |
| `dd-cli list-log-metrics` | List every log-based metric, asserting the harvest is complete |
| `dd-cli get-log-metric ID` | Get one log-based metric, with its quoted anchor phrases |
| `dd-cli audit-log-metric-anchors STRING` | Check a proposed log string against every live metric anchor (positive control + denominator) |
| `dd-cli update-log-metric ID` | PATCH a log metric filter/group_by/percentiles (`--dry-run`); metrics do NOT backfill |
| `dd-cli delete-log-metric ID` | Delete a log-based metric (requires `--yes`) |
| `dd-cli create-monitor` | Create a monitor (metric/query/trace-analytics alert) with full `options` support |
| `dd-cli get-monitor ID_OR_URL` | Get a monitor's details by ID or URL |
| `dd-cli list-monitors` | List monitors by the monitor's own tag (`--tag`), the scope it watches (`--scope-tag`), and/or name (auto-paginates) |
| `dd-cli update-monitor ID_OR_URL` | Update a monitor's query, name, tags, thresholds and `options` (merge, not clobber) |
| `dd-cli delete-monitor ID_OR_URL` | Delete a monitor by ID or URL (requires `--yes`; `--force` for SLO/composite refs) |
| `dd-cli mute-monitor ID_OR_URL` | Mute a monitor until an expiry you name (`--until 4h`, `--until '2026-09-08T00:00:00Z'`, `--scope`); refuses an indefinite mute without `--forever` |
| `dd-cli unmute-monitor ID_OR_URL` | Unmute a monitor (the ONLY way to clear `silenced`), verified by re-reading the monitor |
| `dd-cli create-dashboard` | Create a dashboard from a `--spec` JSON body (+ title/tags flags) |
| `dd-cli get-dashboard ID_OR_URL` | Get a dashboard's full definition by ID or URL |
| `dd-cli update-dashboard ID_OR_URL` | Update (full replace) a dashboard from a `--spec` JSON body (+ title/tags flags) |
| `dd-cli list-dashboards` | List dashboards, optionally filtered by title |
| `dd-cli list-team-members HANDLE` | List a Datadog team's members (email, name, role) by team handle |
| `dd-cli list-slos` | List SLOs, optionally filtered by the SLOs' own tags |
| `dd-cli get-slo ID` | Get SLO details and history (SLI value, error budget) |
| `dd-cli query-metrics QUERY` | Query a metric timeseries; per-series scope with first/last/min/max/avg |
| `dd-cli search-metrics TERM` | Find metric names containing TERM |
| `dd-cli get-workflow ID_OR_URL` | Get a workflow definition by ID or URL |
| `dd-cli search-et-issues QUERY` | Search error tracking issues by service/error type |
| `dd-cli get-et-issue ID` | Get a single error tracking issue with details |
| `dd-cli update-et-issue-state ID STATE` | Update issue state (OPEN, RESOLVED, IGNORED) |
| `dd-cli list-catalog-entities` | List Software Catalog entities with optional filters |
| `dd-cli get-catalog-entity REF` | Get one Software Catalog entity by ref or name |

Software Catalog commands are read-only. Source-of-truth changes should happen through repository-backed `entity.datadog.yaml` PRs, not through Datadog write APIs.

Log-metric filters match quoted phrases as **case-insensitive substrings at
intake**, so a new log string containing another metric's anchor silently feeds
that metric -- and log metrics never backfill, so it cannot be undone. Run
`audit-log-metric-anchors` on a proposed string *before* it ships; the run
carries a positive control (a hit that must happen, or the whole audit reports
`ok: false`) and the denominator it checked against. For the same
no-backfill reason, prefer creating a narrowed metric alongside a live one over
`update-log-metric` on the metric a monitor depends on.

`delete-monitor` is irreversible and Datadog will not hand the monitor back,
so it requires `--yes`, reads the monitor first, and prints the full definition
it destroyed (on the failure envelope too -- a DELETE that fails may still have
landed). A 404 is reported as an error, never as an idempotent success: the
same 404 covers "already deleted", "wrong ID" and "`DD_SITE` points at the
wrong region", and only the first of those makes success a true statement.
Datadog refuses (400) when an SLO or composite monitor references the monitor;
`--force` deletes anyway and leaves that reference dangling.

Muting is the one monitor state that cannot be round-tripped through
`update-monitor`. A PUT carrying `options.silenced` mutes the monitor, but a
PUT carrying `silenced: {}` or `silenced: null` answers 200 and leaves it
muted -- a silent no-op that tells an operator a paging monitor is alerting
again while it is still gagged (verified against the live API, 2026-08-31).
`update-monitor` therefore refuses `--option silenced=...` outright and names
`mute-monitor` / `unmute-monitor`, which are the only pair that can both set
and clear it. `--option device_ids=...` is refused for the same reason: it is
read-only in Datadog's schema, so the PUT would report success and change
nothing. As a backstop for any option that behaves this way and is not yet
catalogued, `update-monitor` compares the monitor Datadog returns against what
it sent and fails on an option that did not take.

`mute-monitor` requires an expiry (`--until 4h`, `--until
'2026-09-08T00:00:00Z'`, or epoch seconds) unless `--forever` is passed: an
un-expiring mute on a paging monitor is alert coverage removed with no moment
at which anybody finds out. Both commands verify the artifact rather than the
status code -- they re-read the monitor afterwards and check
`options.silenced`, so a 200 that did not mute (or did not unmute) fails
instead of reporting success.

Run `dd-cli --help` or `dd-cli <command> --help` for details.

## Skills

| Skill | Description |
|-------|-------------|
| [datadog-auth](.opencode/skills/datadog-auth/SKILL.md) | Troubleshoot 401/403 errors, understand keys and regions |
| [datadog-logs](.opencode/skills/datadog-logs/SKILL.md) | Log search syntax, storage tiers (flex), pagination |
| [datadog-incidents](.opencode/skills/datadog-incidents/SKILL.md) | Incident enrichment, update fields, API patterns |
| [datadog-log-metrics](.opencode/skills/datadog-log-metrics/SKILL.md) | Log-based metrics: create/list/audit/update/delete, the anchor-collision trap, and the '@'-prefix trap |
| [datadog-monitors](.opencode/skills/datadog-monitors/SKILL.md) | Create, inspect, and update monitors (thresholds, group states, notifications) |
| [datadog-slos](.opencode/skills/datadog-slos/SKILL.md) | List SLOs, inspect SLI values, error budgets, and threshold history |
| [datadog-metrics](.opencode/skills/datadog-metrics/SKILL.md) | Query metric timeseries, find metric names, rollup and null-handling traps |
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

### A filter aimed at the wrong parameter is an error represented as data

The envelope rules above cover a request that *fails*. The nastier case is a
request that **succeeds while answering a different question**. `list-monitors
--tag team:X` sent Datadog's `tags` (which filters by the scope a monitor
watches) instead of `monitor_tags` (the monitor's own tags), so every
ownership-tag audit returned `[]` with `ok: true` and exit 0. `list-slos --tags`
sent a parameter Datadog does not define, and unknown query parameters are
*ignored*, so it returned the entire org as though filtered. Neither is
detectable by any check on the output alone.

Rules that follow:

1. **Name the kwarg for the predicate, not for the wire parameter**, and do not
   offer an ambiguous one. `list_monitors` takes `monitor_tags=` and
   `scope_tags=`, and has no `tags=`, so a confused caller fails loudly.
2. **A filter test that mocks the client cannot see this bug.** A `MagicMock`
   records whichever kwarg it is handed and reports success. Filter semantics
   must be pinned against a fake server driven through a real transport, and
   the fixture must contain a record where the two predicates *disagree* --
   where an object's tags and its query scope coincide, the bug is invisible.
3. **Echo the predicate back with the answer.** `list-monitors` emits a
   `filters` object, and a tag-filtered run returning 0 prints a stderr note
   naming which question was asked. An empty set that cannot say what it
   searched for is indistinguishable from a clean result.

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
