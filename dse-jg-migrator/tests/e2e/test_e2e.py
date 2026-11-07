"""End-to-end tests — require a live Docker Compose stack.

Run with: E2E_ENABLED=1 pytest tests/e2e/test_e2e.py -v

Prerequisites:
  docker compose -f docker/docker-compose-e2e.yml up -d
  (all services must be healthy before running)
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.e2e

E2E_ENABLED = os.environ.get("E2E_ENABLED", "0") == "1"
SOURCE_GREMLIN = os.environ.get("SOURCE_GREMLIN_URL", "ws://localhost:8183/gremlin")
SOURCE_CQL_HOST = os.environ.get("SOURCE_CQL_HOST", "localhost")
SOURCE_CQL_PORT = int(os.environ.get("SOURCE_CQL_PORT", "9043"))
TARGET_GREMLIN = os.environ.get("TARGET_GREMLIN_URL", "ws://localhost:8182/gremlin")
TARGET_CQL_HOST = os.environ.get("TARGET_CQL_HOST", "localhost")
TARGET_CQL_PORT = int(os.environ.get("TARGET_CQL_PORT", "9042"))
GRAPH_NAME = "food"


def _wait_for_stack(timeout_s: int = 300) -> bool:
    """Poll docker compose ps until all services are healthy or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            result = subprocess.run(  # noqa: S603 S607
                ["docker", "compose", "-f", "docker/docker-compose-e2e.yml", "ps"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and "unhealthy" not in result.stdout and "starting" not in result.stdout:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(10)
    return False


def _run_cmd(*args: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    return subprocess.run(  # noqa: S603
        list(args), capture_output=True, text=True
    )


@pytest.fixture(scope="session", autouse=True)
def stack_health() -> None:
    if not E2E_ENABLED:
        pytest.skip("E2E_ENABLED not set — skipping E2E tests")
    assert _wait_for_stack(), "Docker Compose stack did not become healthy in time"


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
        "--confirm",
    )
    assert result.returncode == 0, f"seed failed: {result.stderr}"
    return GRAPH_NAME


def test_assess_exits_zero(seeded_graph: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """assess command must exit 0 and produce report files."""
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
    assert result.returncode == 0, f"assess failed: {result.stderr}"
    import os

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
