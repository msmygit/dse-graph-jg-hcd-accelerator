# dse-jg-migrator

**DSE Graph → JanusGraph / HCD Migration Accelerator**

Analyse, translate, initialise, and migrate **DataStax Enterprise** Graph workloads to
_JanusGraph_ backed by IBM's **DataStax Hyper-Converged Database (HCD)** `2.x`.

For the full implementation plan see
[docs/DSEGraph_Migration_Accelerator.md](../docs/DSEGraph_Migration_Accelerator.md).

---

## Quick start — zero to a migrated graph

### Prerequisites (one time)

| You need | How |
|---|---|
| A container runtime | **Docker Desktop**, or **Podman** + podman-compose: `brew install podman podman-compose && podman machine init --cpus 10 --memory 20480 && podman machine start` |
| ≥ 10 CPUs and 20 GiB for containers | Docker Desktop → Settings → Resources (`./docker/up.sh` checks this and prints the fix) |
| Access to the IBM registry (HCD image) | `docker login cp.icr.io --username cp` (Podman: `podman login …`) — paste your entitlement key from [MyIBM](https://myibm.ibm.com/products-services/containerlibrary) |
| Python 3.12+ | `cd dse-jg-migrator && python3 -m venv .venv && source .venv/bin/activate && pip install -e .` |

### Three steps (from `dse-jg-migrator/`)

```bash
# HCD runs with password authentication. Its superuser password on a fresh local stack is `cassandra`; every
# command below (and every compose command) reads it from this variable. Set it in each new terminal.
export HCD_CQL_PASSWORD=cassandra

# 1. Start the stack: checks prerequisites, then starts every service and waits until all are healthy.
#    First run: 5–15 min (image pulls; DSE boots in ~4 min under emulation on Apple Silicon). Later: a few min.
#    Another HCD version? HCD_IMAGE_TAG=<tag> ./docker/up.sh   (default 2.0.8-ubi)
./docker/up.sh

# 2. Seed the source DSE graph with the "food" sample workload (~1 min).
dse-jg-migrator seed \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --template food --vertex-count 100 --edge-count 200 \
  --confirm

# 3. Migrate it: assess → analyse → translate → init-target → migrate → validate → report (~1 min).
dse-jg-migrator run-all \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --target-cql-host localhost --target-cql-port 9042 \
  --output-dir ./out \
  --skip-assessment-gate   # the food sample deliberately includes features the assessment flags RED
```

Results are in `./out/`: start with `food-migration-report.md`, then `food-validation-report.md` (source vs target
counts) and `food-assessment-report.html`.

**Trying another sample?** Seed it under its own name (`--template social --graph-name social`, …); re-seeding the same
name appends data. The local target is one JanusGraph graph, so empty it before migrating the next sample —
otherwise samples with the same labels add up there and `validate` reports a mismatch:

```bash
./docker/up.sh --reset-target   # asks first; empties ONLY the local target + ./out checkpoints (~45 s)
```

Your seeded source graphs are kept, so you can migrate them again at any time.

**Stop / resume / reset**

```bash
podman-compose -f docker/docker-compose-e2e.yml stop     # keep data (Docker: docker compose … stop)
./docker/up.sh                                           # resume
./docker/up.sh --reset-target                            # empty only the migration target (keeps source data)
./docker/up.sh --clean                                   # clean slate: delete everything, including seeded data
```

**Starting from a clean slate** (e.g. a container that cannot start after a crash or power loss):

```bash
./docker/up.sh --clean   # lists, asks, then removes this stack's containers, volumes, network and ./out
./docker/up.sh           # images are kept, so this starts without downloading
```

`--clean` covers what `podman rm --force --all` leaves behind (the data volumes, the network, `./out`) and touches
nothing outside this stack. Details: [`docker/README.md` → Clean slate](docker/README.md#clean-slate).

**Fallback without `up.sh`** (Docker Compose v2 only — podman-compose has no `--wait`):

```bash
docker compose -f docker/docker-compose-e2e.yml up -d --wait --wait-timeout 1800
```

If something fails, the CLI says which host and port it could not reach; `./docker/up.sh` explains a container
that stopped. More detail, including Apple Silicon notes: [`docker/README.md`](docker/README.md).

---

## CLI Reference

### `seed` — Seed a DSE cluster with a sample workload

```bash
dse-jg-migrator seed \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --template food \
  --graph-name food \
  --vertex-count 1000 \
  --edge-count 5000 \
  --confirm
```

Templates: `food`, `social`, `iot`, `enterprise`

### `assess` — Run a read-only pre-migration compatibility assessment

```bash
dse-jg-migrator assess \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food \
  --target-cql-host localhost --target-cql-port 9042 \
  --output-dir ./reports
```

Produces: `food-assessment-report.md`, `food-assessment-report.json`, `food-assessment-report.html`

With `--target-cql-host`, the HCD password comes from `HCD_CQL_PASSWORD` (or `--target-cql-password`; user
`--target-cql-username`, default `cassandra`). There is no built-in default. The HCD pre-flight reports **HC-001**
when HCD rejects an anonymous connection and **HC-009** (RED) when it accepts one.

### `analyse` — Extract DSE Graph schema to JSON

```bash
dse-jg-migrator analyse \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food
```

### `translate` — Translate DSE schema to JanusGraph JSON schema

```bash
dse-jg-migrator translate \
  --analysis food-analysis.json \
  --udt-strategy flat \
  --map-strategy flat \
  --decimal-strategy double
```

Options: `--udt-strategy flat|json`, `--map-strategy flat|json`, `--decimal-strategy double|string`
(`double` keeps Decimal numeric and reports inexact values; `string` stores the exact decimal — pass the
same value to `migrate`).

### `init-target` — Apply JanusGraph schema to target cluster

```bash
dse-jg-migrator init-target \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --schema food-janusgraph-schema.json \
  --index-timeout 30          # minutes to wait for indexes to be ENABLED (default: 30)
```

### `migrate` — Move vertex and edge data

```bash
dse-jg-migrator migrate \
  --analysis food-analysis.json \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --cql-fetch-size 1000 \
  --write-batch 100 \
  --udt-strategy flat --map-strategy flat --decimal-strategy double   # must match translate
```

Core (DSG) graphs are read over CQL (`--source-cql-host`); Classic graphs over Gremlin. The migrator writes exactly
the keys `translate` created, so pass it the same `--udt-strategy`, `--map-strategy` and `--decimal-strategy`
(`run-all` does this for you).

Each failed batch is retried 3 times (1 s/2 s/4 s). If a batch still fails, `migrate` lists the errors and
exits 1; re-run the same command to resume — elements already migrated are skipped.

### `validate` — Compare source and target counts

```bash
dse-jg-migrator validate \
  --analysis food-analysis.json \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --target-gremlin-url ws://localhost:8182/gremlin
```

### `report` — Generate final migration report

```bash
dse-jg-migrator report --graph-name food
```

### `run-all` — Run the complete pipeline

```bash
dse-jg-migrator run-all \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --target-cql-host localhost
```

Flags: `--analyze-only` (stop after assessment), `--skip-assessment-gate`, `--seed --template food`

---

Both `dse-jg-migrator ...` and `python -m dse_jg_migrator ...` invoke the same CLI. Sample outputs are in [`examples/`](examples/).

## Running tests

```bash
# Unit tests only (no external dependencies)
pytest tests/unit/ -v

# With coverage gate (≥ 80%)
pytest tests/unit/ --cov=dse_jg_migrator --cov-report=term-missing --cov-fail-under=80

# E2E tests (requires live Docker Compose stack)
HCD_CQL_PASSWORD=cassandra E2E_ENABLED=1 pytest tests/e2e/test_e2e.py -v
```

---

## Development

```bash
# Lint
ruff check dse_jg_migrator/ tests/

# Type checking
mypy dse_jg_migrator/ --strict

# Security scan
pip-audit -r requirements.lock
```

---

## Architecture

```
DSE Graph (source) ──────────────────────────────────────────────────────────────
    │                                                                             │
    ▼ (read-only)                                                                 │
assessor.py ──── compatibility_rules.py                                           │
    │                                                                             │
    ▼                                                                             │
analyser.py ──── models.py                                                        │
    │                                                                             │
    ▼ (pure transform)                                                            │
translator.py                                                                     │
    │                                                                             │
    ▼ (writes schema)                                                             │
initialiser.py ──────────────────────────────────► JanusGraph / HCD (target)     │
    │                                                                             │
    ▼ (writes data)                                                               │
migrator.py ◄────────────────────────────────────────────────────────────────────┘
    │
    ▼
validator.py ──── reporter.py ──── cli.py
```
