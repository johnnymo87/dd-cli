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
| `dd-cli list-teams` | List/search Datadog Teams by name, handle, or member email |
| `dd-cli find-user-teams MEMBER` | Find Datadog Teams matching a user/member email or name |
| `dd-cli list-team-members HANDLE` | List a team's members (email, name, role) by team handle |
| `dd-cli list-team-notification-rules HANDLE` | List team notification routing rules, including PagerDuty handles |
| `dd-cli list-slos` | List SLOs, optionally filtered by the SLOs' own tags |
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
dd-cli list-team-members supplychain --format json
```

`list-team-members` exists so that hand-maintained team->reviewer maps can be
re-derived instead of trusted. Datadog returns membership rows carrying only a
user UUID and the identities separately under `included`; the command performs
that join, marks any membership it could not join with `user_resolved: false`
plus a `warnings[]` entry, and warns on stderr when the row count disagrees
with the team's own `user_count` — a short list that looked authoritative is
exactly the failure mode this command is meant to remove, not introduce.

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
| `--format` | `summary` | Output: `summary`, `json` (raw response under `data`), `jsonl` (one raw series per line) |
| `--timeout` | `30` | Request timeout in seconds |

The `summary` format reports each series by tag scope with `first`, `last`,
`last_ts`, `min`, `max`, `avg`, and how many of its points were non-null. A
query error arrives as HTTP 200 with `status: "error"` in the body; it is
surfaced through the standard failure envelope (`ok: false`, `count: null`)
rather than as an empty result. See the **datadog-metrics** skill for the
rollup, null, and metric-name traps.

### search-metrics Options

| Option | Default | Description |
| --- | --- | --- |
| `--limit` | `100` | Max names to print; `total` still reports every match |
| `--on-truncation` | `exit3` | What to do when the cap bit: `exit3`, `warn`, `error` |
| `--timeout` | `15` | Request timeout in seconds |

Matching is a literal substring over recently-reporting metrics, so `.` is not
a wildcard and absence is not proof a metric does not exist.

### create-log-metric Options

| Option | Default | Description |
| --- | --- | --- |
| `--query` | required | Log search query (Log Explorer syntax) |
| `--aggregation-type` | `count` | `count` matching logs, or `distribution` of a numeric value |
| `--path` | - | Attribute to aggregate; **required** for `distribution`, rejected for `count` |
| `--include-percentiles/--no-include-percentiles` | unset | p50-p99 aggregations (distribution only) |
| `--group-by` | - | Attribute path to tag by (repeatable) |
| `--allow-bare-path` | off | Permit a path with no leading `@` |
| `--timeout` | `15` | Request timeout in seconds |

**A custom log attribute path must be written with a leading `@`** --
`@fbm.attention_open`, not `fbm.attention_open`. Datadog accepts the bare form
with 200 OK and then the metric silently produces no points forever (for
`--group-by`, every value collapses into one `N/A` bucket). No error appears
anywhere. That failure mode once cost multiple weeks and produced a false
"distribution log metrics don't work in this org" conclusion.

dd-cli therefore **refuses** a bare path before making any request. Reserved
attributes and tags are correctly bare, so a small allow-list (`service`,
`env`, `host`, `status`, `source`, `version`, `message`, `ddsource`, `ddtags`,
`date`, `timestamp`) passes silently -- `--group-by service --group-by env`
keeps working. Any other bare path needs the explicit `--allow-bare-path`,
which is the right answer for infrastructure tag keys like `kube_namespace`.
Those reserved names are allowed bare for `--group-by` only: as a
distribution's `--path` they are strings, not numbers, and would produce the
same empty metric, so they are rejected there too.
Rejection is preferred over auto-prefixing because auto-prefixing would rewrite
a caller's intent for the one class of path (tag keys) that dd-cli cannot
distinguish from a custom attribute.

### audit-log-metric-anchors: check a log string before you ship it

A quoted phrase in a log-metric filter is matched as a **case-insensitive
substring**, at **intake**. Any new log line that happens to contain another
metric's anchor silently starts feeding that metric, and because log metrics
never backfill, the contamination cannot be undone by rewording later.

That is a measured failure (anonymised here; the shape is real). A log line for
an order-*retirement* path was worded "...refusing to reserve inventory...",
which contains the `"Refusing to reserve inventory"` anchor of an unrelated
metric counting a *no-op* path; tens of thousands of retirement events were
counted as no-ops, in a metric a live monitor alerted on.

```bash
dd-cli audit-log-metric-anchors 'Order retired: refusing to reserve inventory'
```

Collisions are reported in both directions -- `anchor_in_candidate` (your
string feeds their metric) and `candidate_in_anchor` (a metric on your string
would eat their events) -- each with the metric id, its full filter query, and
the offending phrase.

Two output properties do the real work:

* **`positive_control`** -- a string that MUST hit (default: the longest phrase
  in the harvest; override with `--positive-control`). If it misses, the run is
  `ok: false` with `data: null` and a non-zero exit. A harvest that came back
  empty and an org with no collisions produce the *same* empty answer, so
  without this the command's most confident output is also its least
  trustworthy.
* **`checked`** -- metrics, phrases, distinct phrases. The production case was
  1 hit in 62 filters; "0 collisions" means nothing without its denominator.

### Completeness: `GET /api/v2/logs/config/metrics` is unpaginated

There is no cursor whose absence could prove the answer complete, and the
endpoint 429s readily. A per-metric fetch loop once retrieved **41 of 62**
filters and reported "no collisions" exactly as confidently as a complete run.

`list-log-metrics` and `audit-log-metric-anchors` therefore assert that the set
fetched equals the set enumerated, id for id, and publish the claim as
`completeness: {enumerated, fetched, per_metric_fetches, asserted_equal}`. A
mismatch fails the command rather than shortening the answer. `--detail` adds a
per-metric GET of every id as a second opinion (one request per metric).

### update-log-metric: what PATCH accepts, and why to avoid it

`PATCH /api/v2/logs/config/metrics/{id}` accepts exactly three fields --
`filter.query`, `group_by`, and `compute.include_percentiles`. Confirmed
against Datadog's `LogsMetricUpdateAttributes` schema and live on us3 with a
throwaway metric. `aggregation_type` and `compute.path` are fixed at creation.
`group_by` is replaced wholesale (no per-entry merge); omitted fields are left
untouched.

**Log metrics are computed at INTAKE and do NOT backfill.** An edited filter can
therefore never be validated against historical data, and a mistyped filter
yields a permanently empty series that reads exactly like "healthy" -- a monitor
on it does not alert, it simply goes quiet. Mutating a metric a live monitor
depends on is riskier than creating a narrowed metric alongside it and
repointing the monitor only after the new one has produced a real emission.
`--dry-run` prints the before/after and sends nothing; a real run re-reads the
metric afterwards rather than printing its own prediction as an observation.

`delete-log-metric` requires `--yes`, prints the definition it is about to
delete, and warns that emitted history stays while any monitor on the metric
goes silently no-data.

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
- **datadog-log-metrics** - Log-based metrics: create/list/audit/update/delete, the anchor-collision trap, and the '@'-prefix trap
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
