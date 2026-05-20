# Datadog Software Catalog Read Design

## Goal

Add read-only Software Catalog visibility to `dd-cli` so mono ownership audits can compare local `entity.datadog.yaml` metadata, deployable artifacts, and Datadog's ingested catalog state.

## Decision

`dd-cli` should not write Software Catalog entities. The write path for mono remains changing `entity.datadog.yaml` through a normal PR. `dd-cli` only reads Datadog's current catalog state and formats it for humans and audit scripts.

## Context

Datadog Software Catalog v3 entities use `apiVersion: v3`, `kind`, `metadata.name`, `metadata.owner`, `metadata.additionalOwners`, `spec.components`, and `datadog.codeLocations`. Datadog's docs describe entity definitions as Kubernetes-style YAML files and support repository-backed ingestion through the source-code/GitHub integration.

Relevant docs:

- Software Catalog overview: https://docs.datadoghq.com/internal_developer_portal/software_catalog/
- Setup and repository-backed entity definitions: https://docs.datadoghq.com/internal_developer_portal/software_catalog/set_up/
- Entity model and v3 fields: https://docs.datadoghq.com/internal_developer_portal/software_catalog/entity_model/
- Ownership guidance: https://docs.datadoghq.com/internal_developer_portal/software_catalog/set_up/ownership/
- Software Catalog API: https://docs.datadoghq.com/api/latest/software-catalog/

## Interface

Add these read-only commands:

- `dd-cli list-catalog-entities`
- `dd-cli get-catalog-entity REF`

Useful options:

- `--kind service|system|datastore|queue|...`
- `--owner TEAM_HANDLE`
- `--name NAME`
- `--ref REF`
- `--include schema`, repeatable
- `--include raw_schema`, repeatable
- `--include-discovered`
- `--max-results N`
- `--format json|summary|jsonl`

Default output should be raw JSON to match the approved read-only visibility design. `summary` is an optional convenience projection for audits.

## API Mapping

Use `GET /api/v2/catalog/entity`.

Supported query parameters:

- `page[offset]`
- `page[limit]`
- `filter[kind]`
- `filter[owner]`
- `filter[name]`
- `filter[ref]`
- `include`
- `includeDiscovered`

Pagination is offset based. Increment `page[offset]` by the number of entities returned until the page is short or `--max-results` is reached.

## Mono Audit Usage

The first consumers are manual audits and future LGTM ownership routing checks. They can compare:

- local `entity.datadog.yaml` definitions
- local deployable artifacts and Helm release names
- Datadog's ingested catalog entities
- expected ownership overrides such as `wonder/data/**`, `wonder/supplychain/productcatalog/**`, and `wonder/blueapron/**`

The read API is enough to verify whether YAML PRs have actually propagated into Datadog.

## Non-Goals

- Do not add `POST /api/v2/catalog/entity` support in the first cut.
- Do not mutate Datadog Teams or Software Catalog metadata.
- Do not parse mono YAML inside `dd-cli` yet.
- Do not implement LGTM reviewer routing in `dd-cli`.

## Open Follow-Ups

- A later mono-specific audit can join Datadog catalog output with local path/deployable discovery.
- A later `--validate-teams` mode can call `GET /api/v2/team` to check that owners match Datadog Team handles.
