#!/usr/bin/env bash
# docker/up.sh — the one command to start the E2E stack: checks prerequisites, then starts every service in
# dependency order and waits until each is healthy.
#
# Why a script: podman-compose (what `docker compose` runs when docker is an alias for podman) has no `--wait`,
# does not reliably honour `depends_on: condition: service_healthy`, and `up <svc>` also recreates the service's
# dependencies. This script starts each service with `--no-deps`, in dependency order, and blocks until its
# healthcheck passes. It works with Docker Compose v2 too. Healthy services are skipped, so re-running is safe.
# Fallback without this script (Docker Compose v2 only): docker compose -f docker/docker-compose-e2e.yml up -d --wait
#
# Usage (from dse-jg-migrator/):
#   ./docker/up.sh                          # preflight, then dse-graph-source + hcd → opensearch → janusgraph
#   ./docker/up.sh hcd opensearch           # only the named services, in the given order
#   ./docker/up.sh janusgraph-visualizer    # optional UI (start after janusgraph)
#   ./docker/up.sh --check                  # preflight only
#   ./docker/up.sh --reset-target [--yes]   # empty the local JanusGraph target (asks first unless --yes)
#
# Environment:
#   COMPOSE         compose command (default: podman-compose if installed, else "docker compose")
#   RUNTIME         container CLI used to inspect health (default: podman if installed, else docker)
#   HEALTH_TIMEOUT  seconds to wait per service (default: 1800 — first boots under emulation are slow)
#   SKIP_PREFLIGHT  set to 1 to skip the prerequisite checks
#   HCD_IMAGE_TAG   HCD image tag (default 2.0.8-ubi); also read by docker-compose-e2e.yml
#   HCD_CQL_USERNAME / HCD_CQL_PASSWORD  credentials for --reset-target, if HCD enforces authentication
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE_FILE="docker/docker-compose-e2e.yml"
HCD_IMAGE="cp.icr.io/cp/ibm-hyper-converged-database/hcd:${HCD_IMAGE_TAG:-2.0.8-ubi}"   # same default as docker-compose-e2e.yml
MIN_CPUS=10        # the DSE service is limited to `cpus: 8`; HCD, JanusGraph and OpenSearch need the rest
MIN_MEMORY_MIB=19000   # a VM configured with 20 GiB reports ~19.5 GiB usable

if [[ -z "${RUNTIME:-}" ]]; then
  if command -v podman >/dev/null 2>&1; then RUNTIME="podman"
  elif command -v docker >/dev/null 2>&1; then RUNTIME="docker"
  else echo "✘ Neither podman nor docker is installed. Install Docker Desktop or Podman, then re-run." >&2; exit 1
  fi
fi
if [[ -z "${COMPOSE:-}" ]]; then
  if command -v podman-compose >/dev/null 2>&1; then COMPOSE="podman-compose"; else COMPOSE="docker compose"; fi
fi
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-1800}"
# podman-compose honours stop_grace_period only in its own stop/down commands; give the containers themselves the
# same 120 s so a plain `podman stop` or a VM shutdown also lets HCD and DSE drain (Docker Compose sets it natively).
STOP_ARGS=""
[[ "$COMPOSE" == podman-compose* ]] && STOP_ARGS="--podman-run-args=--stop-timeout=120"

fail() { echo "✘ $*" >&2; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────────────────────────────────────
preflight() {
  echo "Checking prerequisites ($RUNTIME, $COMPOSE)…"
  $COMPOSE version >/dev/null 2>&1 \
    || fail "'$COMPOSE' is not available. Install podman-compose (brew install podman-compose) or Docker Compose v2."

  if ! $RUNTIME info >/dev/null 2>&1; then
    [[ "$RUNTIME" == "podman" ]] && fail "Podman is not running. Start its VM: podman machine start"
    fail "Docker is not running. Start Docker Desktop (or the docker daemon) and re-run."
  fi

  # Registry login: only needed until the HCD image has been pulled once.
  if ! $RUNTIME image inspect "$HCD_IMAGE" >/dev/null 2>&1; then
    local logged_in=""
    if [[ "$RUNTIME" == "podman" ]]; then
      logged_in="$(podman login --get-login cp.icr.io 2>/dev/null || true)"
    elif grep -q '"cp.icr.io"' "${DOCKER_CONFIG:-$HOME/.docker}/config.json" 2>/dev/null; then
      logged_in="yes"
    fi
    [[ -n "$logged_in" ]] || fail "Not logged in to the IBM registry, which hosts the HCD image. Run:
    $RUNTIME login cp.icr.io --username cp
  and paste your entitlement key (https://myibm.ibm.com/products-services/containerlibrary) when prompted."
  fi

  # CPU / memory available to containers (the Podman VM or Docker Desktop's VM).
  local cpus mem_mib
  if [[ "$RUNTIME" == "podman" ]]; then
    cpus="$(podman info --format '{{.Host.CPUs}}' 2>/dev/null || echo 0)"
    mem_mib="$(( $(podman info --format '{{.Host.MemTotal}}' 2>/dev/null || echo 0) / 1048576 ))"
  else
    cpus="$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 0)"
    mem_mib="$(( $(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0) / 1048576 ))"
  fi
  if (( cpus < MIN_CPUS || mem_mib < MIN_MEMORY_MIB )); then
    local fix="In Docker Desktop: Settings → Resources → CPUs ≥ $MIN_CPUS, Memory ≥ 20 GB, then Apply & restart."
    [[ "$RUNTIME" == "podman" ]] && fix="Resize the Podman VM (this restarts it):
    podman machine stop && podman machine set --cpus $MIN_CPUS --memory 20480 && podman machine start"
    fail "Containers can use $cpus CPUs / ${mem_mib} MiB; the stack needs ≥ $MIN_CPUS CPUs and ≥ 20 GiB. $fix"
  fi

  # Apple Silicon + Podman: HCD and DSE are amd64-only and need QEMU emulation (Rosetta lacks the CPU features
  # HCD requires). The registration lives in the VM's kernel and is lost on every VM restart, so re-apply it.
  if [[ "$RUNTIME" == "podman" && "$(uname -s)/$(uname -m)" == "Darwin/arm64" ]]; then
    if [[ "$(podman machine ssh 'head -1 /proc/sys/fs/binfmt_misc/qemu-x86_64 2>/dev/null' 2>/dev/null | tr -d '\r')" != "enabled" ]]; then
      echo "  Enabling QEMU emulation for amd64 images in the Podman VM (needed after every VM restart)…"
      podman machine ssh 'echo -1 | sudo tee /proc/sys/fs/binfmt_misc/rosetta >/dev/null 2>&1 || true' >/dev/null 2>&1 || true
      # The entry is either missing (register it) or present but disabled (enable it); registering twice fails.
      podman machine ssh 'if [ -e /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then echo 1 | sudo tee /proc/sys/fs/binfmt_misc/qemu-x86_64; else sudo sh -c "cat /usr/lib/binfmt.d/qemu-x86_64-static.conf > /proc/sys/fs/binfmt_misc/register"; fi' \
        >/dev/null 2>&1 || true
      [[ "$(podman machine ssh 'head -1 /proc/sys/fs/binfmt_misc/qemu-x86_64 2>/dev/null' 2>/dev/null | tr -d '\r')" == "enabled" ]] \
        || fail "Could not enable QEMU emulation in the Podman VM. See docker/README.md → Apple Silicon caveat 1."
    fi
  fi
  echo "✔ Prerequisites OK ($cpus CPUs, ${mem_mib} MiB)"
}

# ── Start and wait ───────────────────────────────────────────────────────────────────────────────────────────
# Independent services are started first and waited on last, so their slow boot overlaps the chain.
INDEPENDENT="dse-graph-source"

container_for() {
  case "$1" in
    hcd) echo "migrator-hcd" ;;
    opensearch) echo "migrator-opensearch" ;;
    janusgraph) echo "migrator-janusgraph" ;;
    dse-graph-source) echo "migrator-dse-source" ;;
    janusgraph-visualizer) echo "migrator-visualizer" ;;
    *) echo "unknown service: $1" >&2; exit 2 ;;
  esac
}

# Prints healthy | starting | unhealthy | running (no healthcheck) | exited | missing
state_of() {
  local status health
  status="$($RUNTIME inspect --format '{{.State.Status}}' "$1" 2>/dev/null)" || { echo missing; return; }
  [[ "$status" == "running" ]] || { echo "$status"; return; }
  health="$($RUNTIME inspect --format '{{.State.Health.Status}}' "$1" 2>/dev/null || true)"
  echo "${health:-running}"
}

is_ready() {
  case "$(state_of "$1")" in healthy | running) return 0 ;; *) return 1 ;; esac
}

start() {
  local svc="$1" ctr state
  ctr="$(container_for "$svc")"
  state="$(state_of "$ctr")"
  case "$state" in
    healthy | running)
      echo "✔ $svc already $state — skipping"
      return ;;
    starting | unhealthy)
      # Still booting: calling `up` again could recreate it mid-start, so just wait.
      echo "… $svc is $state — waiting, not restarting"
      return ;;
  esac
  echo "▶ starting $svc"
  $COMPOSE $STOP_ARGS -f "$COMPOSE_FILE" up -d --no-deps "$svc" >/dev/null
}

# Explain a crashed container. HCD and DSE log their real errors to system.log, not to stdout.
explain_exit() {
  local svc="$1" ctr="$2" tmp
  echo "✘ $svc stopped (exit code $($RUNTIME inspect --format '{{.State.ExitCode}}' "$ctr" 2>/dev/null || echo '?')). Last log lines:" >&2
  $RUNTIME logs --tail 15 "$ctr" >&2 2>&1 || true
  case "$svc" in
    hcd | dse-graph-source)
      tmp="$(mktemp)"
      if $RUNTIME cp "$ctr:/var/log/cassandra/system.log" "$tmp" >/dev/null 2>&1; then
        echo "  Errors from $svc's system.log:" >&2
        grep -E "ERROR|Exception" "$tmp" | tail -5 | cut -c1-240 >&2 || true
        if grep -q "CommitLogReplay" "$tmp"; then
          echo "  → Its data volume holds a commit log it cannot replay (left by an unclean stop). Reset it with:" >&2
          echo "      $COMPOSE -f $COMPOSE_FILE down -v   # deletes ALL stack data; re-seed afterwards" >&2
        fi
      fi
      rm -f "$tmp" ;;
  esac
  exit 1
}

wait_ready() {
  local svc="$1" ctr state next_note=60
  ctr="$(container_for "$svc")"
  SECONDS=0
  while ! is_ready "$ctr"; do
    state="$(state_of "$ctr")"
    case "$state" in
      exited | missing | dead | stopped) explain_exit "$svc" "$ctr" ;;
    esac
    if (( SECONDS > HEALTH_TIMEOUT )); then
      fail "$svc not healthy after ${HEALTH_TIMEOUT}s (state: $state). Inspect: $RUNTIME logs $ctr"
    fi
    if (( SECONDS >= next_note )); then
      echo "  … still waiting for $svc ($state, ${SECONDS}s)"
      next_note=$(( next_note + 60 ))
    fi
    sleep 5
  done
  echo "✔ $svc $(state_of "$ctr") (${SECONDS}s)"
}

# ── Reset the target ─────────────────────────────────────────────────────────────────────────────────────────
# Empties the local JanusGraph target so another sample can be migrated: samples that share labels would otherwise
# add up in the one target graph and fail `validate`. Touches ONLY the jg_migration_test keyspace (AGENTS.md §11),
# its OpenSearch index and the migration checkpoints in ./out.
TARGET_KEYSPACE="jg_migration_test"

hcd_cql() {
  if [[ -n "${HCD_CQL_PASSWORD:-}" ]]; then
    $RUNTIME exec migrator-hcd cqlsh -u "${HCD_CQL_USERNAME:-cassandra}" -p "$HCD_CQL_PASSWORD" -e "$1"
  else
    $RUNTIME exec migrator-hcd cqlsh -e "$1"
  fi
}

reset_target() {
  local assume_yes="$1" answer out code removed
  for svc in hcd opensearch; do
    is_ready "$(container_for "$svc")" || fail "$svc is not running — start the stack first: ./docker/up.sh"
  done
  if [[ "$assume_yes" != "1" ]]; then
    [[ -t 0 ]] || fail "--reset-target deletes data; run it in a terminal, or pass --yes to confirm."
    read -r -p "Delete ALL migrated data in the local target (keyspace $TARGET_KEYSPACE) and the migration checkpoints in ./out? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Aborted — nothing changed."; exit 1; }
  fi

  # Stop JanusGraph first: it cannot write into the keyspace mid-drop, and it deregisters itself cleanly.
  echo "▶ stopping janusgraph"
  $RUNTIME stop -t 120 migrator-janusgraph >/dev/null 2>&1 || true

  echo "▶ dropping keyspace $TARGET_KEYSPACE in HCD"
  if ! out="$(hcd_cql "DROP KEYSPACE IF EXISTS $TARGET_KEYSPACE" 2>&1)"; then
    echo "$out" | grep -v -i "warning\|recommendation" | tail -3 >&2
    fail "Could not drop $TARGET_KEYSPACE. If HCD enforces authentication, set HCD_CQL_USERNAME and HCD_CQL_PASSWORD."
  fi

  echo "▶ deleting OpenSearch index $TARGET_KEYSPACE*"
  code="$($RUNTIME exec migrator-opensearch curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    "http://localhost:9200/${TARGET_KEYSPACE}*" 2>/dev/null || echo 000)"
  [[ "$code" == "200" || "$code" == "404" ]] || fail "Deleting the OpenSearch index failed (HTTP $code)."

  # A re-run of `migrate` would otherwise find every label "already complete" and write nothing.
  removed="$(ls out/*-migration-checkpoint.db* out/*-id-map.sqlite 2>/dev/null || true)"
  if [[ -n "$removed" ]]; then
    rm -f out/*-migration-checkpoint.db* out/*-id-map.sqlite
    echo "▶ removed migration checkpoints: $(echo "$removed" | tr '\n' ' ')"
  fi

  echo "▶ starting janusgraph (it recreates an empty $TARGET_KEYSPACE)"
  $RUNTIME start migrator-janusgraph >/dev/null
  wait_ready janusgraph
  echo "Target reset. Migrate again with: dse-jg-migrator run-all … --output-dir ./out"
}

case "${1:-}" in
  --check)
    preflight
    exit 0 ;;
  --reset-target)
    reset_target "$([[ "${2:-}" == "--yes" ]] && echo 1 || echo 0)"
    exit 0 ;;
esac
[[ "${SKIP_PREFLIGHT:-0}" == "1" ]] || preflight

if [[ $# -gt 0 ]]; then
  SERVICES="$*"
else
  SERVICES="$INDEPENDENT hcd opensearch janusgraph"
  echo "Starting the stack. The first run pulls ~5 GB of images; under emulation (Apple Silicon) DSE then needs"
  echo "about 4 minutes to boot, so expect 5–15 minutes in total. Later starts take a few minutes."
fi

deferred=""
for svc in $SERVICES; do
  start "$svc"
  if [[ " $INDEPENDENT " == *" $svc "* ]]; then
    deferred="$deferred $svc"
  else
    wait_ready "$svc"
  fi
done
for svc in $deferred; do
  wait_ready "$svc"
done
echo "All requested services are up."
