# delete-monitor design

Date: 2026-08-25
Bead: dd-vhl
Status: approved (adversarial-reviewer-fable, with three must-fix conditions, all folded in below)

## Why

dd-cli could create, read, list and update monitors but not delete one. During a
triage on 2026-08-24 a duplicate monitor (25391362 — same metric, same window,
strictly less sensitive threshold than the existing 24875007) had to be removed.
With no delete verb the agent shipped a workaround: it renamed the monitor
`[DEPRECATED - DELETE ME]`, stripped its notification handles, and asked a human
to finish the job in the UI. An incomplete tool turned into a chore handed to a
person, and into a recommendation that was worse than the one the tool should
have made.

## Command

```
dd-cli delete-monitor MONITOR [--yes] [--force] [--site] [--timeout]
```

`MONITOR` is a numeric ID or a Datadog monitor URL, the same as `get-monitor`
and `update-monitor`.

## Decisions

**Confirmation is `--yes`, required, with no interactive prompt.** This is the
house convention set by `delete-log-metric`. An interactive prompt would be
worse than useless here: dd-cli is driven by agents and scripts, where stdin is
usually not a TTY. `--force` is deliberately *not* the confirmation flag,
because `force` is already a wire parameter on this endpoint meaning something
else entirely (below); overloading it would make the dangerous option the one
people type by reflex.

**Capture before destroy.** The command GETs the monitor, then DELETEs it. The
success envelope carries the full definition under `data.definition`, so the
operator keeps a copy in scrollback of what they destroyed. If the GET does not
return a monitor object, the delete does not happen — the same refusal
`delete-log-metric` makes.

The captured definition is also attached to the *failure* envelope when the
DELETE phase fails. That case is not hypothetical: a DELETE that lands but is
masked by a headered 429 gets retried once by `_write`, and the retry sees a
monitor that is already gone. Dropping the backup in exactly the run where the
monitor may have died anyway would defeat the reason for reading it first.

**GET-then-DELETE has no meaningful TOCTOU.** Datadog does not reuse monitor
IDs, so a DELETE after a GET either hits the same object or 404s. It cannot
delete a different monitor. The worst outcome is a marginally stale copy of the
definition.

**The ID is validated as digits-only before any request is sent** (not
range-checked against int64 — an over-long number simply 404s on the read).
`_parse_monitor_ref` returns non-URL input unchanged, and `get_monitor`
interpolates the ID straight into the request path. For a read, junk input is a
404 nuisance; for a DELETE it is request-forging surface — `delete-monitor
'123?force=true'` must not be expressible. A URL like `.../monitors/manage`
parses to `"manage"` and is rejected for the same reason. The client method
percent-escapes the ID as well, matching `delete_log_metric`.

**References: surface the refusal, expose `--force`.** Datadog refuses to delete
a monitor referenced by an SLO or a composite monitor:

```
400 {"errors":["monitor [16195606,name] is referenced in slos: [34dbd...,test slo]"]}
400 {"errors":["monitor [19443597,name] is referenced in composite monitors: [37050226,...]"]}
```

`?force=true` (a *string* per Datadog's OpenAPI spec) deletes anyway. `--force`
sends it; the default path sends no `force` parameter at all. When a 400 names
either reference kind, the failure envelope gains a `hint` naming `--force` and
saying what forcing costs: the SLO or composite keeps its dangling reference,
Datadog does not clean it up.

The hint is decoration on an already-correct failure, not a semantic branch —
`ok:false` and the non-zero exit do not depend on the match. That is what keeps
this from being another "error represented as data": if Datadog rewords its
message, the worst outcome is a missing hint, and a test pins that an
unrecognized 400 still fails cleanly with no hint.

**No bulk form.** Partial failure has no honest envelope — "3 of 5 deleted,
ok:false" is the ambiguous half-answer the output contract exists to ban. A
single-ID command composes into a shell loop where each iteration carries its
own exit code, and mass monitor deletion is precisely where a typo should be
expensive.

**404 is an error, never idempotent success.** The GET runs first, so a 404
means the definition was never captured. Three different causes produce it —
already deleted, wrong ID, wrong `DD_SITE` — and the error message names all
three. Reporting success would let an invocation pointed at the wrong site
print "deleted" about a monitor that is alive in another region.

**The 200 body is cross-checked.** Datadog answers `{"deleted_monitor_id": N}`;
a mismatch against the requested ID is warned about rather than assumed away.

## Tests

All HTTP is mocked through a fake monitor API on a real `httpx` transport, not a
`MagicMock` — a mock records whichever kwarg it is handed and reports success,
which cannot see a query parameter that was never sent. No real monitor is
deleted.

Covered: refusal without `--yes`; URL parsing; non-numeric and path-bearing refs
rejected with zero requests sent; definition captured then DELETE, in that
order; 404 on GET fails with no DELETE sent; DELETE-phase failure still carries
the definition; SLO and composite 400s produce the `--force` hint; an
unrecognized 400 produces none; `--force` sends `force=true` and the default
path sends no `force` at all; `deleted_monitor_id` mismatch warns.
