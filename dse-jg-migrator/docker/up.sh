#!/usr/bin/env bash
# docker/up.sh — start the E2E stack one service at a time, waiting for each to be healthy.
#
# Why: podman-compose does not reliably honour `depends_on: condition: service_healthy`, and
# `up <svc>` also (re)creates the service's dependencies. This script starts each service with
# `--no-deps`, in dependency order, and blocks until its healthcheck passes. It works with
# Docker Compose v2 too. Services that are already healthy are skipped, so re-running is safe.
#
# Usage (from dse-jg-migrator/):
#   ./docker/up.sh                          # dse-graph-source + hcd → opensearch → janusgraph
#   ./docker/up.sh hcd opensearch           # only the named services, in the given order
#   ./docker/up.sh janusgraph-visualizer    # optional UI (start after janusgraph)
#
# Environment:
#   COMPOSE         compose command (default: podman-compose if installed, else "docker compose")
#   RUNTIME         container CLI used to inspect health (default: podman if installed, else docker)
#   HEALTH_TIMEOUT  seconds to wait per service (default: 1800 — DSE under QEMU can take 20 min)
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE_FILE="docker/docker-compose-e2e.yml"

if [[ -z "${COMPOSE:-}" ]]; then
  if command -v podman-compose >/dev/null 2>&1; then COMPOSE="podman-compose"; else COMPOSE="docker compose"; fi
fi
if [[ -z "${RUNTIME:-}" ]]; then
  if command -v podman >/dev/null 2>&1; then RUNTIME="podman"; else RUNTIME="docker"; fi
fi
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-1800}"

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
  $COMPOSE -f "$COMPOSE_FILE" up -d --no-deps "$svc" >/dev/null
}

wait_ready() {
  local svc="$1" ctr state
  ctr="$(container_for "$svc")"
  SECONDS=0
  while ! is_ready "$ctr"; do
    state="$(state_of "$ctr")"
    case "$state" in
      exited | missing | dead)
        echo "✘ $svc is $state — last log lines:" >&2
        $RUNTIME logs --tail 20 "$ctr" >&2 || true
        exit 1 ;;
    esac
    if (( SECONDS > HEALTH_TIMEOUT )); then
      echo "✘ $svc not healthy after ${HEALTH_TIMEOUT}s (state: $state)" >&2
      exit 1
    fi
    sleep 10
  done
  echo "✔ $svc $(state_of "$ctr") (${SECONDS}s)"
}

if [[ $# -gt 0 ]]; then
  SERVICES="$*"
else
  SERVICES="$INDEPENDENT hcd opensearch janusgraph"
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
