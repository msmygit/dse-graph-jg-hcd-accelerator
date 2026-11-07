# AGENTS.md — Rules for AI and Human Agents

> This file governs how any agent (human or AI) contributes to the
> **DSE Graph → JanusGraph / HCD Migration Accelerator** project.
> Read it fully before touching any file in this repository.

---

## 1. Canonical Plan — Single Source of Truth

The authoritative implementation plan is:

```
docs/DSEGraph_Migration_Accelerator.md
```

**Rules:**
- Every sub-task, code decision, and design choice traces back to this document.
- Before writing any code, read the relevant sub-task section **in full**.
- After completing a sub-task, update its `**Status:**` marker from `` `[ ] pending` `` to
  `` `[x] done` `` in the plan document and commit that change **in the same PR** as the
  implementation.
- Never implement something not described in the plan without first adding it to the plan
  (with a rationale) and getting it reviewed.
- If you discover a discrepancy between the plan and reality (e.g., an API that works
  differently than documented), update the plan with the finding before proceeding.

---

## 2. Branch and PR Rules

### 2.1 One sub-task = one branch = one PR

| Sub-task | Branch name |
|----------|-------------|
| Sub-Task 0 — Assessment | `feat/st0-compatibility-assessor` |
| Sub-Task 1 — Scaffold | `feat/st1-project-scaffold` |
| Sub-Task 2 — Analyser | `feat/st2-dse-analyser` |
| Sub-Task 3 — Translator | `feat/st3-schema-translator` |
| Sub-Task 4 — Initialiser | `feat/st4-jg-hcd-initialiser` |
| Sub-Task 5 — Migrator | `feat/st5-data-migrator` |
| Sub-Task 6 — Validator | `feat/st6-traversal-validator` |
| Sub-Task 7 — CLI + Reports | `feat/st7-cli-reporter` |
| Sub-Task 8 — Docker E2E | `feat/st8-docker-e2e` |
| Sub-Task 9 — Sample Seeder | `feat/st9-sample-seeder` |
| Bug fixes | `fix/<short-description>` |
| Docs-only changes | `docs/<short-description>` |

Branch off `main`. Rebase (do not merge) to keep `main` up to date before opening a PR.

### 2.2 PR requirements — all must be true before merging

- [ ] Branch is rebased on latest `main`.
- [ ] All unit tests pass: `pytest tests/unit/ -v`.
- [ ] No new linting or security-rule errors: `ruff check dse_jg_migrator/ tests/`.
- [ ] No new type errors: `mypy dse_jg_migrator/ --strict`.
- [ ] Dependency vulnerability scan passes: `pip-audit -r requirements.lock`.
- [ ] `docs/DSEGraph_Migration_Accelerator.md` sub-task status updated to `[x] done`.
- [ ] `dse-jg-migrator/CHANGELOG.md` has a new entry under `## [Unreleased]`.
- [ ] PR description uses the template in §2.3.
- [ ] No secrets, credentials, or entitlement keys in any committed file (see §6).

### 2.3 PR description template

Every PR must include:

```
## Sub-Task
ST-N — <name>

## What changed
- <file added or modified, one bullet per file>

## Why
<one sentence linking to the plan section>

## Tests added / modified
- <list test files>

## Plan updated
- [ ] docs/DSEGraph_Migration_Accelerator.md status set to [x] done
- [ ] CHANGELOG.md entry added

## Checklist
- [ ] pytest tests/unit/ passes
- [ ] ruff check passes (including S rules)
- [ ] mypy --strict passes
- [ ] pip-audit passes (no CVSS >= 7.0)
- [ ] No secrets committed
```

---

## 3. Project Structure Rules

The project lives under `dse-jg-migrator/`. The directory layout is fixed by the plan and
must not be changed without a plan update:

```
dse-jg-migrator/
├── pyproject.toml
├── CHANGELOG.md
├── README.md
├── dse_jg_migrator/
│   ├── __init__.py
│   ├── cli.py
│   ├── assessor.py               # READ-ONLY — no writes to any cluster
│   ├── compatibility_rules.py    # READ-ONLY — static rule catalogue only
│   ├── assessment_reporter.py
│   ├── analyser.py
│   ├── translator.py
│   ├── initialiser.py
│   ├── migrator.py
│   ├── validator.py
│   ├── reporter.py
│   ├── models.py
│   └── config.py
├── tests/
│   ├── unit/
│   │   ├── test_assessor.py
│   │   ├── test_compatibility_rules.py
│   │   ├── test_assessment_reporter.py
│   │   ├── test_analyser.py
│   │   ├── test_translator.py
│   │   ├── test_initialiser.py
│   │   ├── test_migrator.py
│   │   ├── test_validator.py
│   │   └── test_models.py
│   ├── fixtures/                 # Canned schema.describe() outputs, mock JSON, etc.
│   └── e2e/
│       └── test_e2e.py
├── docker/
│   ├── docker-compose-e2e.yml
│   ├── janusgraph-hcd.properties
│   ├── seed_dse.groovy
│   └── README.md
└── examples/
    ├── food-graph-schema.json
    ├── food-assessment-report.md
    ├── food-assessment-report.json
    └── janusgraph-target.json
```

**Rules:**
- Do not create files outside this structure without updating the plan first.
- Do not rename existing modules — other sub-tasks import them by exact name.
- `examples/` contains checked-in sample output files. Update them when output format changes.
- `tests/fixtures/` holds read-only test data. Never write real cluster data here.

---

## 4. Coding Standards

### 4.1 Language and runtime

- Python 3.12+ only. Type annotations are mandatory on every function signature.
- Ensure virtual environment named `dse2jghcd313` exists and is active.
- Use `@dataclass` or Pydantic v2 models for all structured data. No bare `dict` as return type.
- Bare `Any` in type annotations requires an inline comment explaining why it cannot be avoided.

### 4.2 Style — ruff (formatter + linter)

```bash
# Format
ruff format dse_jg_migrator/ tests/

# Lint — S rules cover security checks (replaces bandit)
ruff check dse_jg_migrator/ tests/
```

Configure in `pyproject.toml`:

```toml
[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "S"]
# E = pycodestyle errors
# F = pyflakes
# I = isort
# N = pep8-naming
# UP = pyupgrade
# S = flake8-bandit security rules (covers B105/B107 hardcoded passwords,
#     B608 SQL injection, B501 SSL verification, B110 try/except/pass, etc.)
```

Fix every ruff `S`-rule violation before submitting a PR. Suppressions with
`# noqa: Sxxx` require an inline justification comment.

### 4.3 Type checking

```bash
mypy dse_jg_migrator/ --strict
```

No `type: ignore` suppression without an inline comment.

### 4.4 Dependencies

Allowed runtime dependencies (exact names from `pyproject.toml`):

| Package | Purpose |
|---------|---------|
| `gremlinpython` | Gremlin WebSocket client (source DSE + target JanusGraph) |
| `cassandra-driver` | CQL reads from DSG tables and HCD pre-flight |
| `click` | CLI framework |
| `rich` | Terminal output and progress bars |
| `pydantic` | Data model validation |

**Do not add new runtime dependencies** without a plan update and explicit approval.
`sqlite3` is stdlib — use it directly with no ORM wrapper.

### 4.5 Minimal change principle

- Implement exactly what the plan sub-task describes — no more, no less.
- Do not refactor code in modules unrelated to your sub-task.
- Do not add logging frameworks, caching layers, or abstractions not mentioned in the plan.

### 4.6 Error handling

- Every network call (Gremlin WebSocket, CQL, OpenSearch REST) must be wrapped in a
  `try/except` that emits a descriptive message including host, port, and operation.
- Never swallow exceptions silently (`except Exception: pass` is a ruff `S110` violation).
- `assessor.py` must convert **all** probe failures to `AssessmentFinding` objects.
  It must never propagate an unhandled exception to the CLI layer.

---

## 5. Testing Rules

### 5.1 Coverage gate

Unit test coverage must be ≥ 80% for every new module introduced in a sub-task:

```bash
pytest tests/unit/ \
  --cov=dse_jg_migrator \
  --cov-report=term-missing \
  --cov-fail-under=80
```

This command must pass locally before opening a PR.

### 5.2 Test categories

| Type | Location | External services | CI |
|------|----------|-------------------|----|
| Unit | `tests/unit/` | None — all I/O mocked | Always runs |
| E2E | `tests/e2e/` | Docker Compose stack | Only when `E2E_ENABLED=1` |

### 5.3 Mocking rules

- Use `unittest.mock.MagicMock` or `pytest-mock` for all Gremlin and CQL objects.
- **No real network calls from `tests/unit/`** — any test that requires a live cluster
  belongs in `tests/e2e/` and must be decorated `@pytest.mark.e2e`.
- Every fixture file in `tests/fixtures/` must be a static JSON or text file.
  Never generate fixtures dynamically from a live cluster.

### 5.4 Fixture contract

- Every canned DSE schema fixture must exercise at least one instance of every rule
  category (QP, SC, IX, CF, OL, SE, TP, HC).
- When adding a new `CompatibilityRule` to `compatibility_rules.py`, add a corresponding
  test case to `tests/unit/test_compatibility_rules.py` **in the same commit**.

### 5.5 Read-only assertion for the assessor

Unit tests for `assessor.py` must assert that no write operations are invoked on any
mock connection:

```python
# In tests/unit/test_assessor.py
mock_session.execute.assert_called()          # reads are expected
for call in mock_session.execute.call_args_list:
    query = str(call.args[0]).upper()
    assert not any(kw in query for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE")), \
        f"assessor.py issued a write query: {call}"
```

### 5.6 E2E rules

- E2E tests must not run unless `E2E_ENABLED=1` is set in the environment.
- E2E tests must be **idempotent** — running them twice on the same stack produces the
  same result.
- E2E teardown must drop all JanusGraph keyspaces created during the test:
  `DROP KEYSPACE jg_migration_test` via CQL.

---

## 6. Security and Secrets Rules

These rules are non-negotiable. A PR that violates any of them will not be merged.

### 6.1 Never commit secrets

The following must **never** appear in any committed file:

- IBM Entitled Registry credentials or entitlement keys
- Cassandra / HCD passwords (including defaults like `cassandra`) in `.env` files,
  Docker Compose overrides, or test fixture files that are not `.gitignore`d
- OpenSearch admin passwords
- Any API key, token, or bearer credential

### 6.2 How to handle credentials

- All credentials are injected via environment variables at runtime.
- Docker Compose files use `${VARIABLE:-default}` substitution for all passwords.
- `config.py` must load credentials exclusively from environment variables or
  a `--password` CLI flag. No hardcoded credential strings.
- Add the following patterns to `.gitignore`:

  ```
  *.env
  .env.*
  secrets/
  *.pem
  *.key
  *.p12
  # Keep examples/ properties files; block real config overrides
  dse-jg-migrator/*.properties
  docker/*.properties.local
  ```

### 6.3 Secret scanning before every push

Use `detect-secrets` as a pre-commit hook:

```bash
# Install once
pip install detect-secrets pre-commit

# Scan and create baseline (run once after installing)
detect-secrets scan > .secrets.baseline

# pre-commit config (.pre-commit-config.yaml)
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
```

### 6.4 ruff S-rule enforcement (replaces bandit)

The `S` ruleset in ruff covers the security checks most relevant to this project:

| ruff rule | What it catches |
|-----------|----------------|
| `S105/S106/S107` | Hardcoded passwords |
| `S608` | SQL/CQL injection via string formatting |
| `S501/S502` | SSL certificate verification disabled |
| `S110` | `try/except/pass` swallowed exceptions |
| `S324` | Insecure hash functions |
| `S301` | Insecure pickle/deserialisation |

All `S`-rule violations must be fixed. Suppress with `# noqa: Sxxx` only when
the risk is provably absent, and add an inline comment explaining why.

### 6.5 Gremlin injection prevention

All user-supplied or data-derived strings passed to Gremlin scripts must use
**parameter bindings**, never f-string or `%`-interpolation into Groovy:

```python
# CORRECT — use bindings dict
client.submit("g.addV(label).property('name', name_val)",
              {"label": label, "name_val": name})

# WRONG — string interpolation into Groovy is a code injection risk
client.submit(f"g.addV('{label}').property('name', '{name}')")
```

Ruff rule `S608` will catch CQL string interpolation. Gremlin injection must be
caught in code review — check every `client.submit()` call.

### 6.6 CQL injection prevention

All CQL in `migrator.py` and `assessor.py` must use prepared statements or
`cassandra-driver` parameter binding:

```python
# CORRECT
stmt = session.prepare("SELECT * FROM ?.? WHERE id = ?")
session.execute(stmt, [keyspace, table, row_id])

# WRONG — string-formatted CQL
session.execute(f"SELECT * FROM {keyspace}.{table} WHERE id = {row_id}")
```

### 6.7 Read-only enforcement for assessor and compatibility rules

`assessor.py` must carry this header comment:

```python
# READ-ONLY MODULE
# This module connects to clusters for inspection only.
# It must never execute write operations (INSERT, UPDATE, DELETE, addV, addE, etc.)
# Verified by tests/unit/test_assessor.py::test_no_write_operations.
```

`compatibility_rules.py` must contain only dataclass definitions and constants.
No I/O of any kind is permitted.

---

## 7. Documentation Rules

### 7.1 Plan document (`docs/DSEGraph_Migration_Accelerator.md`)

Update this document when:
- A sub-task is completed (status → `[x] done`).
- A design decision deviates from what is written.
- A new gap, caveat, or known issue is discovered during implementation.
- A new `CompatibilityRule` is added to `compatibility_rules.py`.

Do not reformat or restructure sections unrelated to your sub-task.

### 7.2 CHANGELOG.md

Maintain `dse-jg-migrator/CHANGELOG.md` following
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format:

```markdown
## [Unreleased]

### Added
- ST-0: Pre-migration compatibility assessor with 51 rules across 8 categories (#PR_NUMBER)

### Changed

### Fixed
```

Reference the PR number in every entry. Entries without a PR reference will be rejected.

### 7.3 README.md

`dse-jg-migrator/README.md` must always contain:
- Installation: `pip install -e .`
- IBM ICR authentication instructions (command only — no credentials):
  `docker login cp.icr.io --username cp`
- A usage example for every CLI sub-command (`assess`, `translate`, `init-target`,
  `migrate`, `validate`, `report`, `run-all`)
- A link to `docs/DSEGraph_Migration_Accelerator.md`

### 7.4 Inline docstrings

Every public function and class requires a docstring. Specific requirements:

- **`assessor.py` methods** — state: what data they read, which cluster (source DSE or
  target HCD), and which rule IDs (`QP-xxx`, `HC-xxx`, etc.) may be emitted.
- **`migrator.py` methods** — state: read-only or write, which system is written to,
  and checkpoint behaviour on failure.
- **`compatibility_rules.py`** — every `CompatibilityRule` entry is self-documenting via
  its `description` and `remediation` fields; no additional docstring required.

---

## 8. Dependency and Build Rules

### 8.1 Pinned lock file

`pyproject.toml` specifies minimum versions (`>=`). A `requirements.lock` file pinned
with exact versions is required for reproducible CI builds:

```bash
# Generate / regenerate after any version change
pip install pip-tools
pip-compile pyproject.toml -o requirements.lock
```

Commit `requirements.lock` to the repository. Regenerate it in the same PR as any
`pyproject.toml` change.

### 8.2 Vulnerability scanning

Run before every PR:

```bash
pip install pip-audit
pip-audit -r requirements.lock
```

No known vulnerability with CVSS ≥ 7.0 may be present in the dependency tree.
If a transitive dependency has a known CVE and no fixed version exists, document
the risk in `CHANGELOG.md` and open a tracking issue.

### 8.3 Python version matrix

The project must be tested against Python 3.12 and 3.13. CI must run both versions.
Use a matrix in GitHub Actions:

```yaml
strategy:
  matrix:
    python-version: ["3.12", "3.13"]
```

---

## 9. Docker and Infrastructure Rules

### 9.1 Images — use the exact tags from the plan

| Service | Image | Source |
|---------|-------|--------|
| HCD | `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi` | `hcd-dev/compose.yaml` |
| OpenSearch | `opensearchproject/opensearch:3` | Plan §Sub-Task 8 |
| JanusGraph | `janusgraph/janusgraph:latest` | Plan §Sub-Task 8 |
| DSE Graph 6.8 | `datastax/dse-server:6.8.65-ubi7` | Plan §Sub-Task 8 |
| DSE Graph 6.9 | `datastax/dse-server:6.9.26-ubi` | Plan §Sub-Task 8 |

Do not change image tags without updating the plan and all references in compose files.

### 9.2 No credentials in compose files

`docker-compose-e2e.yml` must not contain the IBM entitlement key.
All passwords use `${VARIABLE:-default}` substitution. The one-time
`docker login cp.icr.io --username cp` step is documented in `docker/README.md`
and `hcd-dev/README.md` — it is never scripted or stored in a file.

### 9.3 Idempotency

`docker compose -f docker/docker-compose-e2e.yml up -d` must be safe to run twice.
`docker compose -f docker/docker-compose-e2e.yml down -v` must cleanly remove all
named volumes (`hcd-data`, `opensearch-data`, `dse-data`).

### 9.4 Health check dependency chain

All services declare `depends_on` with `condition: service_healthy`. The required order:

```
hcd (healthy) ──────────────────┐
                                 ├──► janusgraph (healthy) ──► janusgraph-visualizer
opensearch (healthy) ────────────┘
dse-graph-source   (independent — parallel startup)
```

### 9.5 Port allocation — do not change

| Service | Host port | Container port |
|---------|-----------|----------------|
| HCD CQL | 9042 | 9042 |
| HCD Data API | 8181 | 8181 |
| JanusGraph Gremlin | 8182 | 8182 |
| OpenSearch REST | 9200 | 9200 |
| OpenSearch Perf | 9600 | 9600 |
| DSE CQL | 9043 | 9042 |
| DSE Gremlin | 8183 | 8182 |
| Visualizer UI | 3000 | 3000 |
| Visualizer Proxy | 3001 | 3001 |

---

## 10. Sub-Task Execution Order

Do not start a sub-task until all its prerequisites are merged to `main`.

```
ST-1 (Scaffold) ── prerequisite for all other sub-tasks

ST-1 → ST-0 (Assessor)        [can run in parallel with ST-9]
ST-1 → ST-2 (Analyser)
ST-1 → ST-9 (Sample Seeder)   [independent of ST-0 through ST-8; can run in parallel with ST-0]
ST-2 → ST-3 (Translator)
ST-3 → ST-4 (Initialiser)
ST-4 → ST-5 (Migrator)
ST-5 → ST-6 (Validator)
ST-0, ST-2, ST-3, ST-5, ST-6, ST-9 → ST-7 (CLI + Reports)
ST-7 → ST-8 (Docker E2E)
```

When handing off to the next sub-task:
1. Update `**Status:**` in the plan to `[x] done`.
2. Add a "Handoff notes" entry to `CHANGELOG.md` under `[Unreleased]` describing any
   constraints or discoveries relevant to the next sub-task.
3. Confirm `examples/` fixture files reflect the current output format.

---

## 11. What Agents Must NOT Do

- **Do not** modify `bdp/`, `janusgraph/`, or `dse-docs-new/` — these are reference-only
  directories listed in `.gitignore`.
- **Do not** run destructive CQL (`DROP KEYSPACE`, `TRUNCATE`, `DELETE`) against any
  cluster other than the local Docker E2E stack (`jg_migration_test` keyspace only).
- **Do not** push directly to `main`. All changes go through a reviewed PR.
- **Do not** merge your own PR. A second reviewer must approve.
- **Do not** add runtime dependencies beyond those listed in §4.4 without a plan update.
- **Do not** disable failing tests with `@pytest.mark.skip`, `# noqa`, or `--ignore`.
  Fix the test or fix the code.
- **Do not** commit `.env` files, `*.pem`, `*.key`, `*.p12`, or any file containing
  credentials.
- **Do not** make real network calls from `tests/unit/`.
- **Do not** issue any write operation (`addV`, `addE`, `INSERT`, `UPDATE`, `DELETE`)
  from `assessor.py` or `compatibility_rules.py`.
- **Do not** interpolate user-supplied or data-derived strings directly into Gremlin
  scripts or CQL queries. Always use bindings / prepared statements.
- **Do not** invoke `seeder.py` without the `--confirm` flag or interactive `y` confirmation.
  The seeder writes to the source cluster — it must never run silently or in a non-interactive
  CI pipeline without an explicit override.
- **Do not** use `seeder.py` against a production DSE cluster. It is a demo/dev tool only.
  The `--source-gremlin-url` must resolve to a local Docker network address or a
  clearly non-production host before the seeder is invoked.
