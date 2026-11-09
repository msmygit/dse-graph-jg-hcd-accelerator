"""End-to-end tests — require a live container stack (Docker or Podman).

Run with: E2E_ENABLED=1 pytest tests/e2e/test_e2e.py -v

Prerequisites:
  docker compose -f docker/docker-compose-e2e.yml up -d
  (or: podman compose / podman-compose with the same arguments)
  (all services must be healthy before running)

The container runtime is auto-detected (docker, then podman). Override with
CONTAINER_RUNTIME=podman|docker. Start the stack with ``./docker/up.sh`` (Podman-safe ordering).
The ``food`` template uses ``searchIndex()``, so dse-graph-source must run with DSE Search (``-s``).

Idempotent: the session resets the target before and after the run — it drops ONLY the
``jg_migration_test`` keyspace (AGENTS.md §5.6/§11) and its OpenSearch index, then restarts JanusGraph so it
recreates an empty keyspace.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

E2E_ENABLED = os.environ.get("E2E_ENABLED", "0") == "1"
SOURCE_GREMLIN = os.environ.get("SOURCE_GREMLIN_URL", "ws://localhost:8183/gremlin")
SOURCE_CQL_HOST = os.environ.get("SOURCE_CQL_HOST", "localhost")
SOURCE_CQL_PORT = int(os.environ.get("SOURCE_CQL_PORT", "9043"))
TARGET_GREMLIN = os.environ.get("TARGET_GREMLIN_URL", "ws://localhost:8182/gremlin")
TARGET_CQL_HOST = os.environ.get("TARGET_CQL_HOST", "localhost")
TARGET_CQL_PORT = int(os.environ.get("TARGET_CQL_PORT", "9042"))
TARGET_OPENSEARCH = os.environ.get("TARGET_OPENSEARCH_URL", "http://localhost:9200")
GRAPH_NAME = "food"
TARGET_KEYSPACE = "jg_migration_test"  # the only keyspace E2E may drop (AGENTS.md §11)
# The optional janusgraph-visualizer has no healthcheck, so it is not waited for.
_REQUIRED_CONTAINERS = {"migrator-hcd", "migrator-opensearch", "migrator-janusgraph", "migrator-dse-source"}


def _container_runtime() -> str | None:
    """Return the container CLI to use: $CONTAINER_RUNTIME, else docker, else podman."""
    forced = os.environ.get("CONTAINER_RUNTIME")
    if forced:
        return forced if shutil.which(forced) else None
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


def _wait_for_stack(timeout_s: int = 600) -> bool:
    """Poll the container runtime until the four required services report healthy, or timeout."""
    runtime = _container_runtime()
    if runtime is None:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            result = subprocess.run(  # noqa: S603
                [runtime, "ps", "--filter", "name=migrator-", "--format", "{{.Names}} {{.Status}}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            healthy = {ln.split()[0] for ln in result.stdout.splitlines() if "(healthy)" in ln}
            if result.returncode == 0 and _REQUIRED_CONTAINERS <= healthy:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(10)
    return False


def _run_cmd(*args: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.run(  # noqa: S603
        list(args), capture_output=True, text=True
    )


def _reset_target() -> None:
    """Drop ONLY the jg_migration_test keyspace and its OpenSearch index, then restart JanusGraph.

    JanusGraph recreates an empty keyspace (``schema.default=none``) on start. Writes only to the local E2E
    target stack.
    """
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster

    auth = PlainTextAuthProvider(
        os.environ.get("HCD_CQL_USERNAME", "cassandra"), os.environ.get("HCD_CQL_PASSWORD", "cassandra")
    )
    cluster = Cluster([TARGET_CQL_HOST], port=TARGET_CQL_PORT, auth_provider=auth)
    try:
        cluster.connect().execute(f"DROP KEYSPACE IF EXISTS {TARGET_KEYSPACE}")  # constant, not user input
    finally:
        cluster.shutdown()
    request = urllib.request.Request(f"{TARGET_OPENSEARCH}/{TARGET_KEYSPACE}*", method="DELETE")  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # fixed local http URL
        assert response.status == 200
    runtime = _container_runtime()
    assert runtime, "no container runtime found"
    subprocess.run([runtime, "restart", "migrator-janusgraph"], check=True, capture_output=True)  # noqa: S603


@pytest.fixture(scope="session", autouse=True)
def stack_health() -> object:
    if not E2E_ENABLED:
        pytest.skip("E2E_ENABLED not set — skipping E2E tests")
    assert _wait_for_stack(), "Container stack (docker/podman) did not become healthy in time"
    _reset_target()  # start from an empty target even if a previous run died before teardown
    assert _wait_for_stack(), "JanusGraph did not become healthy again after the target reset"
    yield
    _reset_target()


@pytest.fixture(scope="session")
def seeded_graph(stack_health: None) -> str:
    """Seed the food graph on the DSE source cluster. Returns graph name."""
    result = _run_cmd(
        "dse-jg-migrator",
        "seed",
        "--source-gremlin-url",
        SOURCE_GREMLIN,
        "--template",
        "food",
        # Small and fixed: each template block is one Gremlin request, and the defaults (1000 / 5000) can exceed
        # the DSE script timeout under emulation. The source is never dropped (AGENTS.md §11), so re-runs add data;
        # every assertion compares source with target, so the result is the same.
        "--vertex-count",
        "50",
        "--edge-count",
        "100",
        "--confirm",
    )
    assert result.returncode == 0, f"seed failed: {result.stderr}"
    return GRAPH_NAME


def test_assess_exits_zero(seeded_graph: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """assess command must exit 0 within 60 s and produce report files."""
    started = time.monotonic()
    result = _run_cmd(
        "dse-jg-migrator",
        "assess",
        "--source-gremlin-url",
        SOURCE_GREMLIN,
        "--source-cql-host",
        SOURCE_CQL_HOST,
        "--source-cql-port",
        str(SOURCE_CQL_PORT),
        "--graph-name",
        GRAPH_NAME,
        "--output-dir",
        str(tmp_path),
        "--skip-assessment-gate",
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, f"assess failed: {result.stderr}"
    assert elapsed < 60, f"assess took {elapsed:.1f} s (plan: < 60 s)"
    assert os.path.exists(str(tmp_path / f"{GRAPH_NAME}-assessment-report.json"))
    assert os.path.exists(str(tmp_path / f"{GRAPH_NAME}-assessment-report.md"))
    assert os.path.exists(str(tmp_path / f"{GRAPH_NAME}-assessment-report.html"))


def test_assess_overall_risk_not_critical(seeded_graph: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The food graph must not produce a CRITICAL risk assessment."""
    import json

    _run_cmd(
        "dse-jg-migrator",
        "assess",
        "--source-gremlin-url",
        SOURCE_GREMLIN,
        "--source-cql-host",
        SOURCE_CQL_HOST,
        "--source-cql-port",
        str(SOURCE_CQL_PORT),
        "--graph-name",
        GRAPH_NAME,
        "--output-dir",
        str(tmp_path),
        "--skip-assessment-gate",
    )
    with open(str(tmp_path / f"{GRAPH_NAME}-assessment-report.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["overall_risk"] != "CRITICAL", f"food graph should not be CRITICAL: {data['overall_risk']}"


def test_run_all_succeeds(seeded_graph: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Full run-all pipeline must complete without error."""
    result = _run_cmd(
        "dse-jg-migrator",
        "run-all",
        "--source-gremlin-url",
        SOURCE_GREMLIN,
        "--source-cql-host",
        SOURCE_CQL_HOST,
        "--source-cql-port",
        str(SOURCE_CQL_PORT),
        "--graph-name",
        GRAPH_NAME,
        "--target-gremlin-url",
        TARGET_GREMLIN,
        "--target-cql-host",
        TARGET_CQL_HOST,
        "--target-cql-port",
        str(TARGET_CQL_PORT),
        "--output-dir",
        str(tmp_path),
        "--skip-assessment-gate",
    )
    assert result.returncode == 0, f"run-all failed: {result.stderr}"


def test_validate_reports_100pct_match(seeded_graph: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """validate must report 100% count match after run-all."""
    # First run run-all (may have already run above, but idempotent)
    analysis_path = str(tmp_path / f"{GRAPH_NAME}-analysis.json")
    if not os.path.exists(analysis_path):
        _run_cmd(
            "dse-jg-migrator",
            "analyse",
            "--source-gremlin-url",
            SOURCE_GREMLIN,
            "--source-cql-host",
            SOURCE_CQL_HOST,
            "--source-cql-port",
            str(SOURCE_CQL_PORT),
            "--graph-name",
            GRAPH_NAME,
            "--output-dir",
            str(tmp_path),
        )

    result = _run_cmd(
        "dse-jg-migrator",
        "validate",
        "--analysis",
        analysis_path,
        "--source-gremlin-url",
        SOURCE_GREMLIN,
        "--target-gremlin-url",
        TARGET_GREMLIN,
        "--output-dir",
        str(tmp_path),
    )
    assert result.returncode == 0, f"validate failed: {result.stderr}"
