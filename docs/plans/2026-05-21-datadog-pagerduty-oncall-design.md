# Datadog PagerDuty On-Call Catalog Design

Date: 2026-05-21

## Summary

Add low-risk Datadog-only commands for discovering and validating PagerDuty-related Software Catalog metadata. Do not add write commands for on-call linkage in this phase: the source of truth is repository-managed `*.datadog.yaml`, and Datadog does not document PagerDuty schedule/current-on-call APIs behind Datadog credentials.

## Research Findings

### Software Catalog PagerDuty Metadata

Current Software Catalog v3 schema uses `integrations.pagerduty.serviceURL` for PagerDuty linkage. The older v2.2 spelling is `service-url`; `service-name` and `serviceName` are not current v3 Catalog fields.

Example v3 shape:

```yaml
schema-version: v3.0
kind: service
metadata:
  name: auth-service
  tags:
    - team:identity-ops
integrations:
  pagerduty:
    serviceURL: https://www.pagerduty.com/service-directory/P123456
```

This field links a Catalog entity to a PagerDuty service-directory URL. It is distinct from the Datadog PagerDuty integration tile's service name / notification handle.

### PagerDuty Integration Tile API

Datadog documents PagerDuty integration service CRUD, but not list-all:

- `POST /api/v1/integration/pagerduty/configuration/services`
- `GET /api/v1/integration/pagerduty/configuration/services/{service_name}`
- `PUT /api/v1/integration/pagerduty/configuration/services/{service_name}`
- `DELETE /api/v1/integration/pagerduty/configuration/services/{service_name}`

The `service_name` here is the Datadog-side notification handle used as `@pagerduty-[serviceName]`. It is not the Software Catalog v3 `serviceURL` field.

### Teams Notification Rules

Teams v2 notification rules can include:

```json
{
  "pagerduty": {
    "service_name": "Datadog-prod"
  }
}
```

This is team notification routing to a Datadog-configured PagerDuty handle. It does not expose PagerDuty schedules or current on-call users, and it is not the same as Catalog entity PagerDuty metadata.

### Datadog-Native On-Call

Datadog's `/api/v2/on-call/...` endpoints manage Datadog-native On-Call schedules, routing rules, and responders. They are not a PagerDuty schedules API.

For Datadog-native On-Call, a future command could resolve a Datadog team to `/api/v2/on-call/teams/{team_id}/on-call`. That should be labeled Datadog-native only.

### PagerDuty Schedules

Datadog does not document a Datadog API to list PagerDuty schedules or resolve current PagerDuty on-call users. Authoritative PagerDuty schedule discovery requires PagerDuty API credentials, such as a read-only PagerDuty REST API key.

## Command Surface

### `validate-catalog`

Validate local Datadog entity YAML files.

Command:

```bash
dd-cli validate-catalog [PATH...] --format summary|json
```

Behavior:

- Scan explicit files/directories, defaulting to the current directory.
- Match `*.datadog.yaml`, `*.datadog.yml`, and `entity.datadog.yaml`.
- Parse YAML and validate PagerDuty-related fields.
- For v3 (`schema-version: v3.0` or `apiVersion: v3`), require `integrations.pagerduty.serviceURL` when PagerDuty metadata is present.
- Flag `service-name`, `serviceName`, and `service-url` under v3 as invalid or likely wrong.
- Return non-zero when validation errors are found.

Notes:

- This is separate from existing `dd-cli validate`, which validates Datadog API credentials.
- Add `PyYAML` for robust local YAML parsing.

### `list-catalog-pagerduty-links`

List PagerDuty links declared in local Datadog entity YAML.

Command:

```bash
dd-cli list-catalog-pagerduty-links [PATH...] --format summary|json|jsonl
```

Behavior:

- Reuse the same file discovery and YAML parsing as `validate-catalog`.
- Output entity kind, name/ref, owner, tags, and PagerDuty `serviceURL`.
- Support many entities pointing at one PagerDuty routing-hub service.

### `check-pagerduty-service`

Validate a known Datadog PagerDuty integration tile service name.

Command:

```bash
dd-cli check-pagerduty-service SERVICE_NAME --format json
```

Behavior:

- Call `GET /api/v1/integration/pagerduty/configuration/services/{service_name}`.
- Print the Datadog response on success.
- Return a Datadog API error on missing or unauthorized service names.

Important caveat: this checks a Datadog notification handle by known name. It does not list all handles and does not validate Catalog `serviceURL` directly.

### `list-team-notification-rules`

Read a team's Datadog notification routing rules.

Command:

```bash
dd-cli list-team-notification-rules HANDLE --format summary|json|jsonl
```

Behavior:

- Resolve `HANDLE` with `GET /api/v2/team` using the existing team search pattern.
- Require exactly one matching team by handle.
- Call `GET /api/v2/team/{team_id}/notification-rules`.
- In summary output, show rule ID and configured email, Slack, MS Teams, and PagerDuty `service_name` destinations.

This is notification routing, not Software Catalog ownership metadata and not current on-call resolution.

### `get-catalog-oncall`

Read Datadog's on-call relationship for one Catalog entity.

Command:

```bash
dd-cli get-catalog-oncall REF --format summary|json
```

Behavior:

- Call the existing Catalog list endpoint with `filter[ref]=REF` and `include=oncall`.
- Require exactly one matching entity.
- Print `relationships.oncall` and any included on-call objects Datadog returns.

This is safer than `show-team-oncall <handle>` because it matches Datadog's documented entity-level relationship.

## Skipped Commands

### `list-pagerduty-services`

Skipped as an authoritative Datadog-only command. Datadog documents create/get/update/delete by known PagerDuty service name, but no list-all endpoint. A future inferred command could list service names found in Catalog YAML, Teams notification rules, or monitors, but its name must make the source explicit.

### `list-pagerduty-schedules`

Skipped with Datadog-only credentials. PagerDuty schedules require PagerDuty API access. Datadog-native On-Call schedules are a different product surface and should not be labeled PagerDuty schedules.

### `show-team-oncall <handle>` For PagerDuty-Backed Teams

Skipped with Datadog-only credentials. Teams notification rules expose `pagerduty.service_name`, not PagerDuty schedule/current-user data. Authoritative resolution needs PagerDuty-side service, escalation policy, schedule, or `/oncalls` data.

### Write Commands

Skipped/deprioritized. Catalog PagerDuty linkage should be edited in repository YAML and reviewed through normal source-control workflow.

## Error Handling

- Datadog API errors continue to use the existing `DatadogAPIError` to Click exception conversion.
- Local validation errors produce structured JSON in `--format json` and readable summaries in `--format summary`.
- Missing or ambiguous team/entity matches produce Click exceptions.
- No command prints API keys, application keys, PagerDuty tokens, or service keys.

## Testing

- Add temp-file tests for Catalog YAML discovery, valid v3 `serviceURL`, invalid v3 `service-name`, invalid v3 `serviceName`, and invalid v3 `service-url`.
- Add CLI tests for `validate-catalog` non-zero exit on validation errors.
- Add CLI tests for `list-catalog-pagerduty-links` summary/json/jsonl output.
- Add mocked `DatadogClient` CLI tests for `check-pagerduty-service`, `list-team-notification-rules`, and `get-catalog-oncall`.
- Add client method tests for the new HTTP endpoints.

## Open Questions

- Whether to later add PagerDuty API credentials and true `list-pagerduty-schedules` / current on-call commands.
- Whether to add a `--datadog-preview` option that calls `POST /api/v2/catalog/entity/preview` for server-side schema validation.
- Whether local YAML validation should eventually support older Software Catalog schemas beyond flagging obvious v3 PagerDuty field mistakes.
