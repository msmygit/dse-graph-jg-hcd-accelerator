# docker/README.md — E2E Docker Compose Stack

## Overview

The E2E stack runs five services:

| Service | Image | Purpose | Host Port |
|---------|-------|---------|-----------|
| `hcd` | `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi` (override the tag with `HCD_IMAGE_TAG`) | Target CQL store | 9042 |
| `opensearch` | `opensearchproject/opensearch:3` | JanusGraph index backend | 9200 |
| `janusgraph` | `janusgraph/janusgraph:latest` | Target graph engine | 8182 |
| `dse-graph-source` | `datastax/dse-server:6.9.26-ubi` (6.8: `6.8.65-ubi7` on native amd64) | Source DSE Graph | 9043 (CQL), 8183 (Gremlin) |
| `janusgraph-visualizer` | `janusgraph/janusgraph-visualizer:latest` | Graph UI (optional) | 3000, 3001 |

---

## Prerequisites

### 0. Container runtime — Docker or Podman

Either works; every command below is written with `docker`. If you use Podman, replace
`docker` with `podman` (or alias it: `alias docker=podman`) and `docker compose` with
`podman compose` (or `podman-compose`). The E2E test suite auto-detects the runtime
(`docker` first, then `podman`); force one with `CONTAINER_RUNTIME=podman|docker`.

```bash
# Pick whichever is installed
if command -v docker >/dev/null 2>&1; then RT=docker; else RT=podman; fi
if $RT compose version >/dev/null 2>&1; then COMPOSE="$RT compose"; else COMPOSE="${RT}-compose"; fi
echo "runtime=$RT compose=$COMPOSE"
```

On macOS, Podman runs containers in a VM. Give it enough memory and CPUs **before** starting the stack
(a container cannot have more CPUs than the VM, and the DSE service is limited to `cpus: 8`):

```bash
podman machine stop
podman machine set --memory 20480 --cpus 10   # MiB; HCD (6 GB) + DSE (6 GB) + JanusGraph + OpenSearch
podman machine start                          # then re-apply the QEMU binfmt registration (caveat 1 below)
```

`podman-compose` does not reliably honour `depends_on: condition: service_healthy`, and `up <service>`
also (re)creates that service's dependencies — even healthy ones. Use `docker/up.sh`, which starts each
service with `--no-deps` in dependency order and blocks until its healthcheck passes:

```bash
./docker/up.sh                          # dse-graph-source (in the background) + hcd → opensearch → janusgraph
./docker/up.sh hcd opensearch           # only the named services, in the given order
./docker/up.sh janusgraph-visualizer    # optional UI, after janusgraph
```

It skips services that are already healthy, waits on (never restarts) a service that is still starting, and
prints the last log lines if a container exits. Override the tools with `COMPOSE=…` / `RUNTIME=…` and the
per-service wait with `HEALTH_TIMEOUT=<seconds>` (default 1800). It works with Docker Compose v2 too.

A container keeps the healthcheck it was **created** with: after editing the compose file, recreate the
service (`podman-compose -f docker/docker-compose-e2e.yml up -d --no-deps --force-recreate <service>`);
`podman restart` keeps the old configuration.

### 1. IBM Container Registry authentication

The HCD image is gated behind IBM Entitled Registry. You must authenticate before pulling:

```bash
docker login cp.icr.io --username cp      # or: podman login cp.icr.io --username cp
# When prompted for Password, paste your IBM Entitled Registry entitlement key.
# Key available at: https://myibm.ibm.com/products-services/containerlibrary
```

Verify authentication:
```bash
docker pull cp.icr.io/cp/ibm-hyper-converged-database/hcd:${HCD_IMAGE_TAG:-2.0.8-ubi}
```

### 2. System requirements

| Resource | Minimum |
|----------|---------|
| Docker Desktop / Podman machine memory | **≈ 20 GiB** (HCD and DSE are each capped at 6 GB; DSE was OOM-killed at 2 GB and 4 GB) |
| Podman machine CPUs | **≥ 10** (the DSE service has `cpus: 8`) |
| Disk space | **≥ 15 GB** free |
| Platform | `linux/amd64` (native) or Apple Silicon with emulation (Rosetta / QEMU) |

---

## Apple Silicon (M-series) caveat

HCD 2.x and DSE 6.8/6.9 are `linux/amd64` images. Findings from running them on an M-series Mac under Podman
(each verified on a live stack):

1. **Use QEMU user-mode emulation, not Rosetta.** HCD's glibc requires x86-64-v3 CPU features that
   Rosetta does not provide. Re-apply after **every** `podman machine start` (the setting does not persist):

   ```bash
   podman machine ssh 'echo -1 | sudo tee /proc/sys/fs/binfmt_misc/rosetta'
   podman machine ssh 'sudo sh -c "cat /usr/lib/binfmt.d/qemu-x86_64-static.conf > /proc/sys/fs/binfmt_misc/register"'
   podman machine ssh 'head -2 /proc/sys/fs/binfmt_misc/qemu-x86_64'    # must print: enabled
   ```

2. **Disable jemalloc.** HCD's `cassandra` launcher sets `LD_PRELOAD=libjemalloc.so`, which segfaults the
   JVM under QEMU. The compose file sets `CASSANDRA_LIBJEMALLOC=-` (and `QEMU_CPU=max`) on the HCD service.
3. **HCD's `cassandra.yaml` is not mounted.** `hcd-dev/cassandra.yaml` binds `rpc_address: localhost`,
   which makes CQL unreachable from other containers. Without the mount, the image's auto-configuration
   sets `rpc_address: 0.0.0.0`. Default credentials are `cassandra` / `cassandra`.
4. **DSE starts with `-g -s`** (Graph and Search on top of core Cassandra) and `cpus: 8`. Search is needed by
   `searchIndex()` (the `food` and `enterprise` templates and the E2E test); `social` and `iot` need Graph only.
5. **DSE is slow to boot** under emulation (several minutes). A published host port is always open under
   Podman, so check the container log (`/var/log/cassandra/system.log`) or `docker ps` health rather
   than probing the port. The healthcheck runs `cqlsh` (`nodetool status` reported `UN` while CQL still
   refused connections).
6. **Use DSE 6.9 under QEMU, not 6.8.** DSE 6.8 runs on Java 8, whose JVM wedged at a safepoint on every boot
   (5 attempts: all logs incl. `gc.log` stop at a different startup step, the JVM ignores `SIGQUIT`, no OOM;
   more CPUs and dropping Search did not help). DSE 6.9.26 (Java 11) boots in ~3.5 minutes, so the compose file
   uses it. To test a DSE 6.8 source, use a native `amd64` host and `datastax/dse-server:6.8.65-ubi7`.
7. **OpenSearch** runs with security disabled and `DISABLE_INSTALL_DEMO_CONFIG=true`, so no admin password is
   set or committed. Enable the security plugin (and supply credentials via the environment) for anything shared.

Expect roughly 2× (HCD) to much longer (DSE) startup versus native `amd64`.

---

## Starting the stack

```bash
# From the dse-jg-migrator/ directory (Docker or Podman):
./docker/up.sh            # preflight checks, then start in order and wait until healthy
./docker/up.sh --check    # preflight only
./docker/up.sh --reset-target [--yes]   # empty the local JanusGraph target (asks first unless --yes)

# Fallback, Docker Compose v2 only (it honours the healthcheck chain; podman-compose has no --wait):
docker compose -f docker/docker-compose-e2e.yml up -d --wait --wait-timeout 1800
```

The preflight checks the compose tool, that the runtime is running, registry access for the HCD image, and
≥ 10 CPUs / 20 GiB for containers (printing the exact fix if not). On Apple Silicon with Podman it re-enables the
QEMU emulation that every VM restart drops (caveat 1 below), so you do not have to.

`--reset-target` stops JanusGraph, drops **only** the `jg_migration_test` keyspace and its OpenSearch index, removes
the migration checkpoints in `./out` (otherwise re-running `migrate` would skip everything as already migrated), and
starts JanusGraph again, which recreates an empty keyspace. The DSE source graphs are not touched. If HCD enforces
authentication, set `HCD_CQL_USERNAME` / `HCD_CQL_PASSWORD`. `tests/e2e` uses the same command for its reset.

If a service stops, `up.sh` prints its last log lines — for HCD and DSE also the errors from `system.log`. A DSE
that exits with code 100 while replaying its commit log was stopped uncleanly with an older compose file (no
`stop_grace_period`); reset the stack with `down -v` (deletes its data) and start again.

`hcd`, `opensearch`, `janusgraph` and `dse-graph-source` must show `(healthy)` before running migrations.
The optional `janusgraph-visualizer` has no healthcheck.

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
