# Datadog Software Catalog Read Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add read-only `dd-cli` commands for listing and fetching Datadog Software Catalog entities.

**Architecture:** Keep `dd-cli` as a thin Click CLI over `DatadogClient`. Add v2 Software Catalog client methods in `dd_cli/http.py`, then add CLI commands in `dd_cli/cli.py` that handle filters, offset pagination, and output formats. Catalog mutation stays out of scope; mono writes catalog state through YAML PRs.

**Tech Stack:** Python 3.12, Click, httpx, pytest, `uv`.

---

### Task 1: Add HTTP Client Coverage For Catalog Listing

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `dd_cli/http.py`

**Step 1: Write failing tests for `DatadogClient.list_catalog_entities`**

Add a new test class near the other client/CLI tests in `tests/test_cli.py`:

```python
class TestCatalogClient:
    """Tests for Software Catalog client methods."""

    def test_list_catalog_entities_builds_query_params(self):
        from dd_cli.http import DatadogClient

        dd = DatadogClient(site="us3.datadoghq.com", api_key="a", app_key="b")
        try:
            dd._request = MagicMock(return_value={"data": []})

            result = dd.list_catalog_entities(
                kind="service",
                owner="supply-chain",
                name="dispatcher",
                ref="service:dispatcher",
                include=["schema", "raw_schema"],
                include_discovered=True,
                offset=100,
                limit=50,
            )

            assert result == {"data": []}
            dd._request.assert_called_once_with(
                "GET",
                "/api/v2/catalog/entity",
                params={
                    "page[offset]": 100,
                    "page[limit]": 50,
                    "filter[kind]": "service",
                    "filter[owner]": "supply-chain",
                    "filter[name]": "dispatcher",
                    "filter[ref]": "service:dispatcher",
                    "include": "schema,raw_schema",
                    "includeDiscovered": True,
                },
            )
        finally:
            dd.close()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestCatalogClient::test_list_catalog_entities_builds_query_params -v`

Expected: FAIL with `AttributeError: 'DatadogClient' object has no attribute 'list_catalog_entities'`.

**Step 3: Add minimal client implementation**

In `dd_cli/http.py`, add this method near the other v2 API methods, before log metrics or near the validation method:

```python
    def list_catalog_entities(
        self,
        *,
        kind: str | None = None,
        owner: str | None = None,
        name: str | None = None,
        ref: str | None = None,
        include: list[str] | None = None,
        include_discovered: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List Software Catalog entities using the v3 catalog API."""
        params: dict[str, Any] = {
            "page[offset]": offset,
            "page[limit]": limit,
        }
        if kind:
            params["filter[kind]"] = kind
        if owner:
            params["filter[owner]"] = owner
        if name:
            params["filter[name]"] = name
        if ref:
            params["filter[ref]"] = ref
        if include:
            params["include"] = ",".join(include)
        if include_discovered:
            params["includeDiscovered"] = True

        return self._request("GET", "/api/v2/catalog/entity", params=params)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::TestCatalogClient::test_list_catalog_entities_builds_query_params -v`

Expected: PASS.

**Step 5: Run full tests**

Run: `uv run pytest`

Expected: all tests pass.

**Step 6: Commit if requested**

Only create a commit if the user explicitly asks for commits in the execution session.

---

### Task 2: Add `list-catalog-entities` CLI Command

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `dd_cli/cli.py`

**Step 1: Write failing CLI tests**

Add this test class after `TestListMonitors` in `tests/test_cli.py`:

```python
class TestListCatalogEntities:
    """Tests for list-catalog-entities command."""

    def _entity(self, name: str, *, kind: str = "service", owner: str = "supply-chain"):
        return {
            "id": f"{kind}:{name}",
            "type": "entity",
            "attributes": {
                "kind": kind,
                "name": name,
                "owner": owner,
            },
            "meta": {"ingestionSource": "github"},
        }

    def test_list_catalog_entities_passes_filters_and_includes(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {
                "data": [self._entity("dispatcher")]
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                [
                    "list-catalog-entities",
                    "--kind",
                    "service",
                    "--owner",
                    "supply-chain",
                    "--name",
                    "dispatcher",
                    "--ref",
                    "service:dispatcher",
                    "--include",
                    "schema",
                    "--include",
                    "raw_schema",
                    "--include-discovered",
                ],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind="service",
                owner="supply-chain",
                name="dispatcher",
                ref="service:dispatcher",
                include=["schema", "raw_schema"],
                include_discovered=True,
                offset=0,
                limit=100,
            )
            output = json.loads(result.output)
            assert output["data"][0]["attributes"]["name"] == "dispatcher"

    def test_list_catalog_entities_auto_paginates_until_short_page(self, runner, mock_env):
        first_page = {"data": [self._entity(f"svc-{i}") for i in range(100)]}
        second_page = {"data": [self._entity("last")]}
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.side_effect = [first_page, second_page]
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-catalog-entities", "--max-results", "500"]
            )

            assert result.exit_code == 0, result.output
            assert mock_client.list_catalog_entities.call_count == 2
            assert mock_client.list_catalog_entities.call_args_list[0].kwargs["offset"] == 0
            assert mock_client.list_catalog_entities.call_args_list[1].kwargs["offset"] == 100
            output = json.loads(result.output)
            assert output["count"] == 101
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestListCatalogEntities -v`

Expected: FAIL because `list-catalog-entities` does not exist.

**Step 3: Implement command and raw JSON output**

In `dd_cli/cli.py`, add the command near other list commands:

```python
@cli.command("list-catalog-entities")
@click.option("--kind", default=None, help="Filter by entity kind, e.g. service.")
@click.option("--owner", default=None, help="Filter by owner/team handle.")
@click.option("--name", default=None, help="Filter by entity name.")
@click.option("--ref", "entity_ref", default=None, help="Filter by entity ref, e.g. service:dispatcher.")
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(["schema", "raw_schema", "oncall", "incident", "relation"]),
    help="Include relationship data; repeatable.",
)
@click.option("--include-discovered", is_flag=True, help="Include discovered entities.")
@click.option("--max-results", type=int, default=1000, show_default=True)
@click.option("--format", "output_format", type=click.Choice(["json", "summary", "jsonl"]), default="json", show_default=True)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def list_catalog_entities_cmd(
    kind: str | None,
    owner: str | None,
    name: str | None,
    entity_ref: str | None,
    includes: tuple[str, ...],
    include_discovered: bool,
    max_results: int,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """List Software Catalog entities."""
    page_size = 100
    entities: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    include_list = list(includes) if includes else None

    try:
        with _get_client(site, timeout=timeout) as dd:
            offset = 0
            while True:
                page = dd.list_catalog_entities(
                    kind=kind,
                    owner=owner,
                    name=name,
                    ref=entity_ref,
                    include=include_list,
                    include_discovered=include_discovered,
                    offset=offset,
                    limit=page_size,
                )
                batch = page.get("data", [])
                entities.extend(batch)
                included.extend(page.get("included", []))

                if len(entities) >= max_results:
                    entities = entities[:max_results]
                    break
                if len(batch) < page_size:
                    break
                offset += len(batch)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    _output_catalog_entities(entities, included, output_format)
```

Add a helper immediately below the command:

```python
def _output_catalog_entities(
    entities: list[dict[str, Any]],
    included: list[dict[str, Any]],
    output_format: str,
) -> None:
    if output_format == "jsonl":
        for entity in entities:
            click.echo(json.dumps(entity))
        return

    if output_format == "summary":
        data = [_catalog_entity_summary(entity) for entity in entities]
        click.echo(json.dumps({"count": len(data), "data": data}, indent=2))
        return

    output: dict[str, Any] = {"count": len(entities), "data": entities}
    if included:
        output["included"] = included
    click.echo(json.dumps(output, indent=2))


def _catalog_entity_summary(entity: dict[str, Any]) -> dict[str, Any]:
    attrs = entity.get("attributes", {})
    return {
        "id": entity.get("id"),
        "ref": attrs.get("ref") or entity.get("id"),
        "kind": attrs.get("kind"),
        "name": attrs.get("name"),
        "owner": attrs.get("owner"),
        "tags": attrs.get("tags", []),
        "ingestion_source": entity.get("meta", {}).get("ingestionSource"),
    }
```

**Step 4: Run CLI tests**

Run: `uv run pytest tests/test_cli.py::TestListCatalogEntities -v`

Expected: PASS.

**Step 5: Run full tests**

Run: `uv run pytest`

Expected: all tests pass.

**Step 6: Commit if requested**

Only create a commit if the user explicitly asks for commits in the execution session.

---

### Task 3: Add `get-catalog-entity` CLI Command

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `dd_cli/cli.py`

**Step 1: Write failing tests**

Add tests to `TestListCatalogEntities` or a new `TestGetCatalogEntity` class:

```python
class TestGetCatalogEntity:
    def test_get_catalog_entity_uses_ref_filter(self, runner, mock_env):
        entity = {
            "id": "service:dispatcher",
            "type": "entity",
            "attributes": {"kind": "service", "name": "dispatcher", "owner": "supply-chain"},
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": [entity]}
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli,
                ["get-catalog-entity", "service:dispatcher", "--include", "raw_schema"],
            )

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind=None,
                owner=None,
                name=None,
                ref="service:dispatcher",
                include=["raw_schema"],
                include_discovered=False,
                offset=0,
                limit=2,
            )
            output = json.loads(result.output)
            assert output["data"]["id"] == "service:dispatcher"

    def test_get_catalog_entity_errors_when_not_found(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": []}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-catalog-entity", "service:missing"])

            assert result.exit_code != 0
            assert "No catalog entity found" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestGetCatalogEntity -v`

Expected: FAIL because `get-catalog-entity` does not exist.

**Step 3: Implement command**

Add this command in `dd_cli/cli.py` near `list-catalog-entities`:

```python
@cli.command("get-catalog-entity")
@click.argument("ref", metavar="REF")
@click.option("--kind", default=None, help="Entity kind to use when REF is a bare name.")
@click.option(
    "--include",
    "includes",
    multiple=True,
    type=click.Choice(["schema", "raw_schema", "oncall", "incident", "relation"]),
)
@click.option("--include-discovered", is_flag=True, help="Include discovered entities.")
@click.option("--format", "output_format", type=click.Choice(["json", "summary"]), default="json", show_default=True)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def get_catalog_entity_cmd(
    ref: str,
    kind: str | None,
    includes: tuple[str, ...],
    include_discovered: bool,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Get a single Software Catalog entity by ref or name."""
    entity_ref = ref if ":" in ref else None
    name = None if entity_ref else ref
    include_list = list(includes) if includes else None

    try:
        with _get_client(site, timeout=timeout) as dd:
            page = dd.list_catalog_entities(
                kind=kind,
                owner=None,
                name=name,
                ref=entity_ref,
                include=include_list,
                include_discovered=include_discovered,
                offset=0,
                limit=2,
            )
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    entities = page.get("data", [])
    if not entities:
        raise click.ClickException(f"No catalog entity found for {ref}")
    if len(entities) > 1:
        raise click.ClickException(f"Multiple catalog entities matched {ref}; add --kind")

    entity = entities[0]
    if output_format == "summary":
        click.echo(json.dumps({"data": _catalog_entity_summary(entity)}, indent=2))
    else:
        output: dict[str, Any] = {"data": entity}
        included = page.get("included", [])
        if included:
            output["included"] = included
        click.echo(json.dumps(output, indent=2))
```

**Step 4: Run command tests**

Run: `uv run pytest tests/test_cli.py::TestGetCatalogEntity -v`

Expected: PASS.

**Step 5: Run full tests**

Run: `uv run pytest`

Expected: all tests pass.

**Step 6: Commit if requested**

Only create a commit if the user explicitly asks for commits in the execution session.

---

### Task 4: Document The Read-Only Catalog Commands

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`

**Step 1: Write failing docs expectation test only if the project already tests docs**

There is no docs test today. Do not add one just for this small README change.

**Step 2: Update command tables**

In `README.md`, add examples under Quick Start:

```markdown
# List Software Catalog services owned by a team
dd-cli list-catalog-entities --kind service --owner supply-chain --include raw_schema

# Fetch one catalog entity by ref
dd-cli get-catalog-entity service:dispatcher --include raw_schema
```

In `CLAUDE.md` and `AGENTS.md`, add these command-table rows:

```markdown
| `dd-cli list-catalog-entities` | List Software Catalog entities with optional filters |
| `dd-cli get-catalog-entity REF` | Get one Software Catalog entity by ref or name |
```

Add a short note:

```markdown
Software Catalog commands are read-only. Source-of-truth changes should happen through repository-backed `entity.datadog.yaml` PRs, not through Datadog write APIs.
```

**Step 3: Run formatting/linting**

Run: `uv run pre-commit run --all-files`

Expected: PASS, or auto-format then rerun until clean.

**Step 4: Run tests**

Run: `uv run pytest`

Expected: PASS.

**Step 5: Commit if requested**

Only create a commit if the user explicitly asks for commits in the execution session.

---

### Task 5: Manual Smoke Test Against Datadog Credentials

**Files:**
- No file edits.

**Step 1: Validate credentials**

Run: `uv run dd-cli validate`

Expected: JSON with `"valid": true` or equivalent Datadog validation success.

**Step 2: List catalog services**

Run: `uv run dd-cli list-catalog-entities --kind service --max-results 5 --format summary`

Expected: JSON with `count` and up to five entities.

**Step 3: Query mono ownership examples**

Run: `uv run dd-cli list-catalog-entities --kind service --owner supply-chain --include raw_schema --max-results 20 --format summary`

Expected: service summaries, enough to verify the API shape and permissions.

**Step 4: Fetch a known mono entity if present**

Run: `uv run dd-cli get-catalog-entity service:dispatcher --include raw_schema`

Expected: one entity or a clear not-found message if the ref differs in Datadog.

**Step 5: Record smoke-test result in final response**

Mention whether live Datadog calls were run and whether they passed. Do not print secrets.

---

## Completion Criteria

- `list-catalog-entities` supports filters, includes, discovered entities, pagination, and `json|summary|jsonl` output.
- `get-catalog-entity` fetches by entity ref or bare name plus optional kind.
- No Datadog write APIs are exposed.
- `uv run pytest` passes.
- `uv run pre-commit run --all-files` passes or any formatting changes are explained.
- Live smoke tests are run when credentials are available.

## Execution Options

After reviewing this plan, choose one:

1. **Subagent-Driven (this session):** dispatch a fresh implementation subagent per task, review after each task, then verify.
2. **Parallel Session (separate):** launch a new OpenCode session in `/home/dev/projects/dd-cli/.worktrees/software-catalog-read` and have it execute this plan with checkpoints.
