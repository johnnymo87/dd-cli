# Datadog PagerDuty On-Call Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Datadog-only commands for validating local Software Catalog PagerDuty metadata and reading PagerDuty-related Datadog routing/on-call relationships without introducing PagerDuty API credentials.

**Architecture:** Keep API wrappers in `dd_cli/http.py` and Click command wiring in `dd_cli/cli.py`. Add small reusable local YAML helpers in `dd_cli/cli.py` because they are shared by `validate-catalog` and `list-catalog-pagerduty-links`, but do not introduce a larger module split unless the implementation becomes unwieldy.

**Tech Stack:** Python 3.11+, Click, httpx, pytest, PyYAML for local Datadog YAML parsing.

---

### Task 1: Add Local Catalog YAML Parsing And Validation Helpers

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `dd_cli/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing tests**

Add tests near the catalog CLI tests in `tests/test_cli.py`:

```python
class TestCatalogYamlHelpers:
    def test_validate_catalog_accepts_v3_pagerduty_service_url(self, runner):
        with runner.isolated_filesystem():
            Path("service.datadog.yaml").write_text(
                """
schema-version: v3.0
kind: service
metadata:
  name: auth-service
  owner: identity-ops
  tags:
    - team:identity-ops
integrations:
  pagerduty:
    serviceURL: https://www.pagerduty.com/service-directory/P123456
""".lstrip()
            )

            result = runner.invoke(cli, ["validate-catalog"])

            assert result.exit_code == 0, result.output
            output = json.loads(result.output)
            assert output["ok"] is True
            assert output["errors"] == []

    def test_validate_catalog_rejects_v3_pagerduty_service_name(self, runner):
        with runner.isolated_filesystem():
            Path("service.datadog.yaml").write_text(
                """
schema-version: v3.0
kind: service
metadata:
  name: auth-service
integrations:
  pagerduty:
    service-name: datadog-routing-hub
""".lstrip()
            )

            result = runner.invoke(cli, ["validate-catalog"])

            assert result.exit_code == 1
            output = json.loads(result.output)
            assert output["ok"] is False
            assert output["errors"][0]["field"] == "integrations.pagerduty.service-name"
```

Add `from pathlib import Path` at the top of `tests/test_cli.py`.

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestCatalogYamlHelpers -v`

Expected: FAIL because `validate-catalog` does not exist.

**Step 3: Add dependency and minimal helpers**

Add PyYAML to `pyproject.toml` dependencies:

```toml
  "pyyaml>=6.0.2,<7.0.0",
```

Run: `uv lock`

In `dd_cli/cli.py`, add imports:

```python
from pathlib import Path

import yaml
```

Add helper functions near the existing catalog command helpers:

```python
CATALOG_FILE_PATTERNS = ("*.datadog.yaml", "*.datadog.yml")


def _discover_catalog_yaml_files(paths: tuple[str, ...]) -> list[Path]:
    roots = paths or (".",)
    files: list[Path] = []
    seen: set[Path] = set()
    for raw_path in roots:
        path = Path(raw_path)
        candidates: list[Path]
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = []
            for pattern in CATALOG_FILE_PATTERNS:
                candidates.extend(path.rglob(pattern))
        else:
            candidates = [path]

        for candidate in candidates:
            normalized = candidate.resolve()
            if normalized not in seen:
                seen.add(normalized)
                files.append(candidate)
    return sorted(files)


def _load_catalog_yaml_documents(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    try:
        documents = list(yaml.safe_load_all(path.read_text()))
    except OSError as e:
        return [], [_catalog_validation_error(path, None, None, str(e))]
    except yaml.YAMLError as e:
        return [], [_catalog_validation_error(path, None, None, f"Invalid YAML: {e}")]

    loaded: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        if document is None:
            continue
        if not isinstance(document, dict):
            errors.append(_catalog_validation_error(path, index, None, "YAML document must be an object"))
            continue
        loaded.append({"path": str(path), "document": index, "data": document})
    return loaded, errors


def _catalog_validation_error(
    path: Path | str,
    document: int | None,
    field: str | None,
    message: str,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "document": document,
        "field": field,
        "message": message,
    }


def _catalog_schema_version(document: dict[str, Any]) -> str | None:
    value = document.get("schema-version") or document.get("apiVersion")
    return str(value) if value is not None else None


def _is_v3_catalog_schema(document: dict[str, Any]) -> bool:
    version = _catalog_schema_version(document)
    return bool(version and version.startswith("v3"))


def _validate_catalog_document(entry: dict[str, Any]) -> list[dict[str, Any]]:
    document = entry["data"]
    path = entry["path"]
    doc_number = entry["document"]
    integrations = document.get("integrations") or {}
    if not isinstance(integrations, dict):
        return []

    pagerduty = integrations.get("pagerduty")
    if pagerduty is None:
        return []

    if not _is_v3_catalog_schema(document):
        return []

    if not isinstance(pagerduty, dict):
        return [
            _catalog_validation_error(
                path,
                doc_number,
                "integrations.pagerduty",
                "Catalog v3 PagerDuty metadata must be an object with serviceURL.",
            )
        ]

    errors: list[dict[str, Any]] = []
    for invalid_field in ("service-name", "serviceName", "service-url"):
        if invalid_field in pagerduty:
            errors.append(
                _catalog_validation_error(
                    path,
                    doc_number,
                    f"integrations.pagerduty.{invalid_field}",
                    "Catalog v3 uses integrations.pagerduty.serviceURL, not this field.",
                )
            )

    if "serviceURL" not in pagerduty:
        errors.append(
            _catalog_validation_error(
                path,
                doc_number,
                "integrations.pagerduty.serviceURL",
                "Catalog v3 PagerDuty metadata requires serviceURL.",
            )
        )
    return errors
```

**Step 4: Add minimal command for tests**

Add `validate-catalog` command near the catalog commands:

```python
@cli.command("validate-catalog")
@click.argument("paths", nargs=-1, type=click.Path())
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json"]),
    default="json",
    show_default=True,
)
def validate_catalog_cmd(paths: tuple[str, ...], output_format: str) -> None:
    """Validate local Datadog Software Catalog YAML files."""
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in _discover_catalog_yaml_files(paths):
        loaded, load_errors = _load_catalog_yaml_documents(path)
        entries.extend(loaded)
        errors.extend(load_errors)

    for entry in entries:
        errors.extend(_validate_catalog_document(entry))

    output = {"ok": not errors, "count": len(entries), "errors": errors}
    if output_format == "json":
        click.echo(json.dumps(output, indent=2))
    else:
        _output_catalog_validation_summary(output)
    if errors:
        raise click.exceptions.Exit(1)
```

Add summary helper later if needed; for this task it can print a concise OK/error list.

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py::TestCatalogYamlHelpers -v`

Expected: PASS.

**Step 6: Commit**

Run:

```bash
git add pyproject.toml uv.lock dd_cli/cli.py tests/test_cli.py
git commit -m "Add catalog PagerDuty validation"
```

### Task 2: Add `list-catalog-pagerduty-links`

**Files:**
- Modify: `dd_cli/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing tests**

Add tests for JSON and JSONL output:

```python
def test_list_catalog_pagerduty_links_outputs_service_urls(self, runner):
    with runner.isolated_filesystem():
        Path("service.datadog.yaml").write_text(
            """
schema-version: v3.0
kind: service
metadata:
  name: auth-service
  owner: identity-ops
  tags:
    - team:identity-ops
integrations:
  pagerduty:
    serviceURL: https://www.pagerduty.com/service-directory/P123456
""".lstrip()
        )

        result = runner.invoke(cli, ["list-catalog-pagerduty-links"])

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["count"] == 1
        assert output["data"][0]["ref"] == "service:auth-service"
        assert output["data"][0]["pagerduty_service_url"].endswith("/P123456")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestCatalogYamlHelpers -v`

Expected: FAIL because `list-catalog-pagerduty-links` does not exist.

**Step 3: Implement extraction helper and command**

Add helper:

```python
def _catalog_pagerduty_link(entry: dict[str, Any]) -> dict[str, Any] | None:
    document = entry["data"]
    integrations = document.get("integrations") or {}
    if not isinstance(integrations, dict):
        return None
    pagerduty = integrations.get("pagerduty")
    if not isinstance(pagerduty, dict):
        return None
    service_url = pagerduty.get("serviceURL")
    if not service_url:
        return None

    metadata = document.get("metadata") or {}
    kind = document.get("kind")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    ref = f"{kind}:{name}" if kind and name else None
    return {
        "path": entry["path"],
        "document": entry["document"],
        "kind": kind,
        "name": name,
        "ref": ref,
        "owner": metadata.get("owner") if isinstance(metadata, dict) else None,
        "tags": metadata.get("tags", []) if isinstance(metadata, dict) else [],
        "pagerduty_service_url": service_url,
    }
```

Add command:

```python
@cli.command("list-catalog-pagerduty-links")
@click.argument("paths", nargs=-1, type=click.Path())
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="json",
    show_default=True,
)
def list_catalog_pagerduty_links_cmd(paths: tuple[str, ...], output_format: str) -> None:
    """List PagerDuty links declared in local Software Catalog YAML."""
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in _discover_catalog_yaml_files(paths):
        loaded, load_errors = _load_catalog_yaml_documents(path)
        entries.extend(loaded)
        errors.extend(load_errors)
    if errors:
        click.echo(json.dumps({"ok": False, "errors": errors}, indent=2))
        raise click.exceptions.Exit(1)

    links = [link for entry in entries if (link := _catalog_pagerduty_link(entry))]
    _output_catalog_pagerduty_links(links, output_format)
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_cli.py::TestCatalogYamlHelpers -v`

Expected: PASS.

**Step 5: Commit**

Run:

```bash
git add dd_cli/cli.py tests/test_cli.py
git commit -m "List catalog PagerDuty links"
```

### Task 3: Add PagerDuty Integration Service Lookup

**Files:**
- Modify: `dd_cli/http.py`
- Modify: `dd_cli/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing client test**

Add under client tests:

```python
def test_get_pagerduty_integration_service_escapes_service_name(self):
    from dd_cli.http import DatadogClient

    dd = DatadogClient(site="us3.datadoghq.com", api_key="a", app_key="b")
    try:
        dd._request = MagicMock(return_value={"service_name": "Supply Chain"})

        result = dd.get_pagerduty_integration_service("Supply Chain")

        assert result == {"service_name": "Supply Chain"}
        dd._request.assert_called_once_with(
            "GET",
            "/api/v1/integration/pagerduty/configuration/services/Supply%20Chain",
        )
    finally:
        dd.close()
```

**Step 2: Write failing CLI test**

```python
class TestCheckPagerDutyService:
    def test_check_pagerduty_service_calls_client(self, runner, mock_env):
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get_pagerduty_integration_service.return_value = {
                "service_name": "datadog-routing-hub"
            }
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["check-pagerduty-service", "datadog-routing-hub"]
            )

            assert result.exit_code == 0, result.output
            mock_client.get_pagerduty_integration_service.assert_called_once_with(
                "datadog-routing-hub"
            )
            assert json.loads(result.output)["service_name"] == "datadog-routing-hub"
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestCheckPagerDutyService tests/test_cli.py::TestTeamsClient -v`

Expected: FAIL because methods/command do not exist.

**Step 4: Implement HTTP method**

In `dd_cli/http.py`:

```python
def get_pagerduty_integration_service(self, service_name: str) -> dict[str, Any]:
    """Get a Datadog PagerDuty integration service by configured service name."""
    quoted_name = urllib.parse.quote(service_name, safe="")
    return self._request(
        "GET",
        f"/api/v1/integration/pagerduty/configuration/services/{quoted_name}",
    )
```

**Step 5: Implement CLI command**

In `dd_cli/cli.py` near Teams/Catalog commands:

```python
@cli.command("check-pagerduty-service")
@click.argument("service_name", metavar="SERVICE_NAME")
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def check_pagerduty_service_cmd(service_name: str, site: str, timeout: float) -> None:
    """Validate a known Datadog PagerDuty integration service name."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            data = dd.get_pagerduty_integration_service(service_name)
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None
    click.echo(json.dumps(data, indent=2))
```

**Step 6: Run tests**

Run: `uv run pytest tests/test_cli.py::TestCheckPagerDutyService tests/test_cli.py::TestTeamsClient -v`

Expected: PASS.

**Step 7: Commit**

Run:

```bash
git add dd_cli/http.py dd_cli/cli.py tests/test_cli.py
git commit -m "Check PagerDuty integration services"
```

### Task 4: Add Team Notification Rule Reads

**Files:**
- Modify: `dd_cli/http.py`
- Modify: `dd_cli/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing client test**

```python
def test_list_team_notification_rules(self):
    from dd_cli.http import DatadogClient

    dd = DatadogClient(site="us3.datadoghq.com", api_key="a", app_key="b")
    try:
        dd._request = MagicMock(return_value={"data": []})

        result = dd.list_team_notification_rules("team-123")

        assert result == {"data": []}
        dd._request.assert_called_once_with(
            "GET",
            "/api/v2/team/team-123/notification-rules",
        )
    finally:
        dd.close()
```

**Step 2: Write failing CLI test**

```python
class TestListTeamNotificationRules:
    def test_list_team_notification_rules_resolves_handle(self, runner, mock_env):
        team = {
            "id": "team-123",
            "type": "team",
            "attributes": {"name": "Supply Chain", "handle": "supply-chain"},
        }
        rules = {
            "data": [
                {
                    "id": "rule-1",
                    "type": "team_notification_rules",
                    "attributes": {
                        "pagerduty": {"service_name": "datadog-routing-hub"},
                        "email": {"enabled": False},
                    },
                }
            ]
        }
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_teams.return_value = {"data": [team]}
            mock_client.list_team_notification_rules.return_value = rules
            mock_client_class.return_value = mock_client

            result = runner.invoke(
                cli, ["list-team-notification-rules", "supply-chain"]
            )

            assert result.exit_code == 0, result.output
            mock_client.list_team_notification_rules.assert_called_once_with("team-123")
            output = json.loads(result.output)
            assert output["data"][0]["pagerduty_service_name"] == "datadog-routing-hub"
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::TestListTeamNotificationRules tests/test_cli.py::TestTeamsClient -v`

Expected: FAIL.

**Step 4: Implement HTTP method**

In `dd_cli/http.py`:

```python
def list_team_notification_rules(self, team_id: str) -> dict[str, Any]:
    """List notification rules for a Datadog team."""
    return self._request("GET", f"/api/v2/team/{team_id}/notification-rules")
```

**Step 5: Implement team resolution and output helpers**

In `dd_cli/cli.py`:

```python
def _resolve_team_by_handle(dd: DatadogClient, handle: str) -> dict[str, Any]:
    page = dd.list_teams(keyword=handle, fields=["handle", "name"], page_number=0, page_size=100)
    matches = [
        team
        for team in page.get("data", [])
        if (team.get("attributes") or {}).get("handle") == handle
    ]
    if not matches:
        raise click.ClickException(f"No Datadog team found with handle {handle}")
    if len(matches) > 1:
        raise click.ClickException(f"Multiple Datadog teams matched handle {handle}")
    return matches[0]


def _team_notification_rule_summary(rule: dict[str, Any]) -> dict[str, Any]:
    attrs = rule.get("attributes") or {}
    pagerduty = attrs.get("pagerduty") or {}
    return {
        "id": rule.get("id"),
        "pagerduty_service_name": pagerduty.get("service_name") if isinstance(pagerduty, dict) else None,
        "email": attrs.get("email"),
        "slack": attrs.get("slack"),
        "ms_teams": attrs.get("ms_teams"),
    }
```

Add command:

```python
@cli.command("list-team-notification-rules")
@click.argument("handle", metavar="HANDLE")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json", "jsonl"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def list_team_notification_rules_cmd(
    handle: str,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """List Datadog team notification routing rules."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            team = _resolve_team_by_handle(dd, handle)
            data = dd.list_team_notification_rules(team["id"])
    except DatadogAPIError as e:
        _handle_api_error(e)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None

    rules = data.get("data", [])
    if output_format == "json":
        click.echo(json.dumps(data, indent=2))
    elif output_format == "jsonl":
        for rule in rules:
            click.echo(json.dumps(rule))
    else:
        summaries = [_team_notification_rule_summary(rule) for rule in rules]
        click.echo(json.dumps({"count": len(summaries), "data": summaries}, indent=2))
```

**Step 6: Run tests**

Run: `uv run pytest tests/test_cli.py::TestListTeamNotificationRules tests/test_cli.py::TestTeamsClient -v`

Expected: PASS.

**Step 7: Commit**

Run:

```bash
git add dd_cli/http.py dd_cli/cli.py tests/test_cli.py
git commit -m "List team notification rules"
```

### Task 5: Add Catalog On-Call Relationship Read

**Files:**
- Modify: `dd_cli/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing CLI tests**

```python
class TestGetCatalogOncall:
    def test_get_catalog_oncall_uses_include_oncall(self, runner, mock_env):
        entity = {
            "id": "entity-1",
            "type": "entity",
            "attributes": {"ref": "service:auth-service", "kind": "service", "name": "auth-service"},
            "relationships": {"oncall": {"data": [{"id": "oncall-1", "type": "oncalls"}]}},
        }
        included = [
            {
                "id": "oncall-1",
                "type": "oncalls",
                "attributes": {"provider": "PagerDuty", "name": "Jane User", "email": "jane@example.com"},
            }
        ]
        with patch("dd_cli.cli.DatadogClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.list_catalog_entities.return_value = {"data": [entity], "included": included}
            mock_client_class.return_value = mock_client

            result = runner.invoke(cli, ["get-catalog-oncall", "service:auth-service"])

            assert result.exit_code == 0, result.output
            mock_client.list_catalog_entities.assert_called_once_with(
                kind=None,
                owner=None,
                name=None,
                ref="service:auth-service",
                include=["oncall"],
                include_discovered=False,
                offset=0,
                limit=2,
            )
            output = json.loads(result.output)
            assert output["entity"]["ref"] == "service:auth-service"
            assert output["oncall"][0]["id"] == "oncall-1"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::TestGetCatalogOncall -v`

Expected: FAIL because command does not exist.

**Step 3: Implement command**

In `dd_cli/cli.py` near `get-catalog-entity`:

```python
@cli.command("get-catalog-oncall")
@click.argument("ref", metavar="REF")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["summary", "json"]),
    default="summary",
    show_default=True,
)
@click.option("--site", envvar="DD_SITE", default=_default_site, show_default=True)
@click.option("--timeout", type=float, default=15.0, show_default=True)
def get_catalog_oncall_cmd(
    ref: str,
    output_format: str,
    site: str,
    timeout: float,
) -> None:
    """Get Datadog's on-call relationship for one Software Catalog entity."""
    try:
        with _get_client(site, timeout=timeout) as dd:
            page = dd.list_catalog_entities(
                kind=None,
                owner=None,
                name=None,
                ref=ref,
                include=["oncall"],
                include_discovered=False,
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
        raise click.ClickException(f"Multiple catalog entities matched {ref}; use an entity ref")

    entity = entities[0]
    included = page.get("included", [])
    if output_format == "json":
        click.echo(json.dumps({"data": entity, "included": included}, indent=2))
        return

    oncall = ((entity.get("relationships") or {}).get("oncall") or {}).get("data", [])
    click.echo(
        json.dumps(
            {
                "entity": _catalog_entity_summary(entity),
                "oncall": oncall,
                "included": included,
            },
            indent=2,
        )
    )
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_cli.py::TestGetCatalogOncall -v`

Expected: PASS.

**Step 5: Commit**

Run:

```bash
git add dd_cli/cli.py tests/test_cli.py
git commit -m "Read catalog on-call relationships"
```

### Task 6: Update Documentation And Run Full Verification

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/plans/2026-05-21-datadog-pagerduty-oncall-design.md` if implementation names changed
- Test: full suite

**Step 1: Update docs**

Add command references to `README.md` examples and `CLAUDE.md` command table:

```markdown
| `dd-cli validate-catalog` | Validate local Software Catalog PagerDuty metadata |
| `dd-cli list-catalog-pagerduty-links` | List local Catalog PagerDuty service URLs |
| `dd-cli check-pagerduty-service SERVICE_NAME` | Check a known Datadog PagerDuty integration service handle |
| `dd-cli list-team-notification-rules HANDLE` | List team notification routing rules, including PagerDuty handles |
| `dd-cli get-catalog-oncall REF` | Read Datadog's on-call relationship for one Catalog entity |
```

Add a short note that PagerDuty schedules/current on-call users are skipped without PagerDuty API credentials.

**Step 2: Run formatting/linting/tests**

Run:

```bash
uv run ruff format .
uv run ruff check .
uv run mypy dd_cli tests
uv run pytest
```

Expected: all pass.

**Step 3: Fix any failures**

Apply the smallest correct changes and rerun the failing command until it passes.

**Step 4: Commit**

Run:

```bash
git add README.md CLAUDE.md docs/plans/2026-05-21-datadog-pagerduty-oncall-design.md pyproject.toml uv.lock dd_cli/cli.py dd_cli/http.py tests/test_cli.py
git commit -m "Document PagerDuty catalog commands"
```

If no docs changed beyond earlier tasks, skip this commit.

### Task 7: Prepare PR

**Files:**
- None unless verification reveals issues.

**Step 1: Check branch state**

Run:

```bash
git status --short
git log --oneline origin/main..HEAD
git diff origin/main...HEAD --stat
```

Expected: clean worktree except no unrelated files; commits include design and implementation.

**Step 2: Push and open PR**

Run:

```bash
git push -u origin feature/datadog-pagerduty-oncall
gh pr create --title "Add Datadog PagerDuty catalog helpers" --body "$(cat <<'EOF'
## Summary
- Add local Software Catalog PagerDuty metadata validation and link listing
- Add Datadog-only reads for PagerDuty integration service checks, team notification rules, and catalog on-call relationships
- Document unsupported PagerDuty schedule/current-on-call operations without PagerDuty API credentials

## Tests
- uv run ruff check .
- uv run mypy dd_cli tests
- uv run pytest
EOF
)"
```

**Step 3: Report PR URL**

Return the PR URL and any skipped API surfaces.
