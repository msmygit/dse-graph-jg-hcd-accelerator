# dse-jg-migrator

**DSE Graph → JanusGraph / HCD Migration Accelerator**

Analyse, translate, initialise, and migrate DataStax Enterprise Graph workloads to
JanusGraph backed by IBM Hyper-Converged Database (HCD) 2.x.

For the full implementation plan see
[docs/DSEGraph_Migration_Accelerator.md](../docs/DSEGraph_Migration_Accelerator.md).

---

## Installation

```bash
cd dse-jg-migrator
pip install -e .
```

### IBM ICR authentication (required for HCD Docker image)

```bash
docker login cp.icr.io --username cp
# When prompted for password, paste your IBM Entitled Registry entitlement key.
# Key available at: https://myibm.ibm.com/products-services/containerlibrary
```

---

## Quick start (zero-to-migration demo)

```bash
# 1. Start the E2E stack
docker compose -f docker/docker-compose-e2e.yml up -d

# 2. Seed the source DSE Graph with the food sample workload
dse-jg-migrator seed \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --template food \
  --confirm

# 3. Run the full migration pipeline
dse-jg-migrator run-all \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --target-cql-host localhost --target-cql-port 9042 \
  --skip-assessment-gate
```

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
  --map-strategy flat
```

Options: `--udt-strategy flat|json`, `--map-strategy flat|json`

### `init-target` — Apply JanusGraph schema to target cluster

```bash
dse-jg-migrator init-target \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --schema food-janusgraph-schema.json
```

### `migrate` — Move vertex and edge data

```bash
dse-jg-migrator migrate \
  --analysis food-analysis.json \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --cql-fetch-size 1000 \
  --write-batch 100
```

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

## Running tests

```bash
# Unit tests only (no external dependencies)
pytest tests/unit/ -v

# With coverage gate (≥ 80%)
pytest tests/unit/ --cov=dse_jg_migrator --cov-report=term-missing --cov-fail-under=80

# E2E tests (requires live Docker Compose stack)
E2E_ENABLED=1 pytest tests/e2e/test_e2e.py -v
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
