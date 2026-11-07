# docker/README.md — E2E Docker Compose Stack

## Overview

The E2E stack runs five services:

| Service | Image | Purpose | Host Port |
|---------|-------|---------|-----------|
| `hcd` | `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi` | Target CQL store | 9042 |
| `opensearch` | `opensearchproject/opensearch:3` | JanusGraph index backend | 9200 |
| `janusgraph` | `janusgraph/janusgraph:latest` | Target graph engine | 8182 |
| `dse-graph-source` | `datastax/dse-server:6.8.65-ubi7` | Source DSE Graph | 9043 (CQL), 8183 (Gremlin) |
| `janusgraph-visualizer` | `janusgraph/janusgraph-visualizer:latest` | Graph UI (optional) | 3000, 3001 |

---

## Prerequisites

### 1. IBM Container Registry authentication

The HCD image is gated behind IBM Entitled Registry. You must authenticate before pulling:

```bash
docker login cp.icr.io --username cp
# When prompted for Password, paste your IBM Entitled Registry entitlement key.
# Key available at: https://myibm.ibm.com/products-services/containerlibrary
```

Verify authentication:
```bash
docker pull cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi
```

### 2. System requirements

| Resource | Minimum |
|----------|---------|
| Docker memory allocation | **≥ 8 GB** |
| Disk space | **≥ 15 GB** free |
| Platform | `linux/amd64` (native) or Apple Silicon with Rosetta emulation |

---

## Apple Silicon (M-series) caveat

> HCD 2.x (`linux/amd64`) runs under Rosetta emulation on Apple M-series chips.
> Enable Rosetta emulation in Docker Desktop → Settings → Features in Development.
> Allocate **≥ 8 GB RAM**. Expect **~2× longer startup time** vs native `amd64`.

---

## Starting the stack

```bash
# From the dse-jg-migrator/ directory:
docker compose -f docker/docker-compose-e2e.yml up -d

# Verify all services are healthy (may take 2-5 minutes on first start):
docker compose -f docker/docker-compose-e2e.yml ps
```

All five services must show `(healthy)` before running migrations.

---

## Seeding the source DSE graph

Option A — use the `dse-jg-migrator seed` command (recommended):
```bash
dse-jg-migrator seed \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --template food \
  --confirm
```

Option B — use the one-off Docker seed script:
```bash
docker exec -i migrator-dse-source dse gremlin-console -e /dev/stdin \
  < docker/seed_dse.groovy
```

---

## Running the full migration

```bash
dse-jg-migrator run-all \
  --source-gremlin-url ws://localhost:8183/gremlin \
  --source-cql-host localhost --source-cql-port 9043 \
  --graph-name food \
  --target-gremlin-url ws://localhost:8182/gremlin \
  --target-cql-host localhost --target-cql-port 9042 \
  --skip-assessment-gate
```

---

## Teardown

```bash
docker compose -f docker/docker-compose-e2e.yml down -v
# -v removes all named volumes (hcd-data, opensearch-data, dse-data)
```

---

## Port reference

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
