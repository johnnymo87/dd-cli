# mute-monitor / unmute-monitor design

Date: 2026-08-31
Status: implemented

## Why

During a production incident on 2026-08-31, dd-cli could mute a monitor only
through the deprecated legacy field:

```bash
dd-cli update-monitor 25447403 --option 'silenced={"*": 1788000000}'   # works
```

and could not unmute it at all. Both documented clear forms answered 200,
changed nothing, and exited 0:

```bash
dd-cli update-monitor 25447403 --option 'silenced=null'   # no-op, exit 0
dd-cli update-monitor 25447403 --option 'silenced={}'     # no-op, exit 0
```

`--option KEY=null` is documented in dd-cli's own `update-monitor --help` as
the way to clear an option, so this was a documented feature that silently did
not work for this field. The operator got the worst possible signal: a success
that says a paging prod monitor is alerting again while it is still gagged.
The incident was resolved only by bypassing dd-cli entirely:

```bash
curl -X POST "https://api.$DD_SITE/api/v1/monitor/25447403/unmute" \
  -H "Authorization: Bearer $DD_PAT"      # 200; silenced back to {}
```

## Where the no-op actually comes from

The working hypothesis going in was that the read-modify-write option merge in
`update-monitor` dropped falsy/empty values. **It does not.** Checked directly
against the shipped functions:

```
'silenced=null' -> parsed {'silenced': None} -> merged {'silenced': None, ...}
'silenced={}'   -> parsed {'silenced': {}}   -> merged {'silenced': {}, ...}
```

`_parse_monitor_option_overrides` keeps the key, and the `if value is not None`
filter in `_build_monitor_options` applies only to the first-class *flags*, not
to `--option` overrides. So the clear does reach the wire; **Datadog's PUT
ignores it**. `options.silenced` is deprecated and, in practice,
write-to-mute-only: the mute/unmute endpoints are the only pair that can both
set and clear it.

This matters for the fix. Nothing dd-cli can do to the payload will make the
PUT clear a mute, so the only honest options were "fail loudly" and "provide
the endpoint that works". This change does both.

## Commands

```
dd-cli mute-monitor   MONITOR (--until WHEN | --forever) [--scope TAG] [--site] [--timeout]
dd-cli unmute-monitor MONITOR [--scope TAG | --all-scopes] [--site] [--timeout]
```

`MONITOR` is a numeric ID or a monitor URL, validated by the same
`_parse_monitor_id` used by `delete-monitor` (a ref carrying `/` or `?` can
otherwise rewrite the request path).

## Decisions

**An expiry is mandatory unless `--forever` is passed.** An un-expiring mute on
a paging monitor is alert coverage removed with no scheduled moment at which
anybody finds out; Datadog will not remind anyone. `--forever` (alias
`--no-expiry`) exists so the indefinite case is expressible, but has to be
said out loud, and the run warns about it on stderr.

**`--until` accepts an epoch, an offset-qualified ISO-8601 timestamp, or a
forward duration (`7d`).** A *naive* timestamp is refused rather than assigned
a timezone: the same wall-clock time is a different instant in every zone, and
the guess would be discovered only when coverage came back at the wrong hour.
An expiry in the past, and one that looks like epoch milliseconds, are refused
for the same class of reason — both produce a mute nobody asked for.

**Verify the artifact, not the status code.** Both commands re-read the monitor
after the write and check `options.silenced`:

| Observed | Result |
| --- | --- |
| scope absent after a mute | fail: the monitor is still alerting |
| scope muted with no expiry when one was requested | fail: muted **indefinitely** |
| scope muted at an expiry other than the requested one | fail, naming both |
| scope still present after an unmute | fail: **STILL MUTED** |
| read-back itself fails | fail, with a hint that the resulting state is unknown |
| monitor shape unreadable | fail — an unparseable body is not an observation of "not muted" |

A command that reports success on an unverified write is the same bug one
level up, which is exactly what this change exists to remove.

**`update-monitor` refuses the options a PUT accepts and ignores.** Two are
catalogued, and both were checked rather than assumed:

- `silenced` — verified against the live API (2026-08-31, monitor 25447403).
  The refusal names `mute-monitor` / `unmute-monitor`.
- `device_ids` — the only property marked `readOnly: true` in Datadog's own
  `MonitorOptions` schema. A PUT carrying it reports success and changes
  nothing.

Every other `MonitorOptions` property is writable. The deprecated-but-writable
ones (`groupby_simple_monitor`, `locked`, `new_host_delay`,
`synthetics_check_id`) are deliberately left alone: they still take effect, and
refusing them would break callers for no gain.

**A generic backstop, because the catalogue cannot be complete.** Datadog's PUT
answers with the updated monitor, so `update-monitor` now compares the options
it sent against the ones that came back — at no extra request — and fails on a
key that did not take. Two false-alarm traps are handled explicitly: a partial
`thresholds` patch is compared against the merged object actually sent, and a
`--option KEY=null` clear is satisfied by the key being *absent* from the
response (Datadog omits a cleared option rather than echoing `null`). A
response with no readable `options` object produces no finding, since an
unrecognised shape is not evidence that the write was ignored.

**The mute is applied at the whole monitor by default, `--scope` narrows it.**
Datadog records an unscoped mute under the `*` key, which is what the
verification checks. A scoped mute that Datadog applied to `*` instead fails,
because reporting it as narrow would overstate the coverage that remains.

## API notes

- `POST /api/v1/monitor/{id}/mute` and `POST /api/v1/monitor/{id}/unmute`.
- Parameters (`end`, `scope`, `all_scopes`) are sent in the **JSON body**,
  matching Datadog's own `datadogpy` client (`Monitor.mute` /`Monitor.unmute`
  post them as a body).
- Both endpoints are **absent from Datadog's generated OpenAPI spec** (checked
  against `datadog-api-client-go`'s `v1/openapi.yaml`, current and older tags),
  while continuing to work against the live API. That is an additional reason
  the commands do not trust their own 200: the contract is not published, so
  the artifact is the only thing worth believing.
- `end` is epoch **seconds**. Omitting it mutes indefinitely, which is why
  omitting it requires `--forever` here.
