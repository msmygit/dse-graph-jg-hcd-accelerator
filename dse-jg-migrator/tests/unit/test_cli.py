"""Unit tests for cli.py — every sub-command is invoked via click's CliRunner with the backends mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dse_jg_migrator.cli import _load_assessment_risk, _print_risk_banner, main
from dse_jg_migrator.models import (
    AnalysisResult,
    AssessmentFinding,
    AssessmentResult,
    EdgeLabelDefinition,
    SeederResult,
    VertexLabelDefinition,
)

GREMLIN = "ws://localhost:8183/gremlin"
TARGET = "ws://localhost:8182/gremlin"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _assessment(risk: str = "LOW") -> AssessmentResult:
    return AssessmentResult(
        graph_name="food",
        engine="dsg",
        assessed_at="t",
        findings=[
            AssessmentFinding("QP-001", "query_pattern", "YELLOW", "t", "d", "r", "l"),
            AssessmentFinding("SC-001", "schema", "RED", "t", "d", "r", "l"),
            AssessmentFinding("HC-006", "hcd_target", "GREEN", "t", "d", "r", "l"),
        ],
        overall_risk=risk,
        summary={"total_findings": 3, "by_severity": {"RED": 1, "YELLOW": 1, "GREEN": 1}},
    )


def _analysis_file(tmp_path: Path) -> Path:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="t")
    an.vertex_labels.append(VertexLabelDefinition(label="person"))
    an.edge_labels.append(EdgeLabelDefinition(label="knows", from_label="person", to_label="person"))
    path = tmp_path / "food-analysis.json"
    path.write_text(json.dumps(an.to_dict()), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# helpers & top-level
# ---------------------------------------------------------------------------


def test_help_lists_all_subcommands(runner: CliRunner) -> None:
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    for cmd in ("seed", "assess", "analyse", "translate", "init-target", "migrate", "validate", "report", "run-all"):
        assert cmd in res.output


def test_load_assessment_risk(tmp_path: Path) -> None:
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"overall_risk": "HIGH"}), encoding="utf-8")
    bad = tmp_path / "b.json"
    bad.write_text("{nope", encoding="utf-8")
    assert _load_assessment_risk(str(good)) == "HIGH"
    assert _load_assessment_risk(str(bad)) == "LOW"
    assert _load_assessment_risk(str(tmp_path / "missing.json")) == "LOW"


def test_print_risk_banner_does_not_raise() -> None:
    _print_risk_banner("MEDIUM", {"total_findings": 1, "by_severity": {"RED": 0}})


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


def test_seed_with_confirm(runner: CliRunner) -> None:
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.return_value = SeederResult("food", "food", 10, 20, "next-cmd")
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food", "--confirm"])
    assert res.exit_code == 0, res.output
    assert "seeded successfully" in res.output
    assert seeder_cls.call_args.kwargs["graph_name"] == "food"


def test_seed_without_confirm_declined_does_not_write(runner: CliRunner) -> None:
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food"], input="n\n")
    assert res.exit_code == 0
    seeder_cls.assert_not_called()


def test_seed_without_confirm_accepted(runner: CliRunner) -> None:
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.return_value = SeederResult("g", "iot", 1, 1, "n")
        res = runner.invoke(
            main,
            ["seed", "--source-gremlin-url", GREMLIN, "--template", "iot", "--graph-name", "g"],
            input="y\n",
        )
    assert res.exit_code == 0, res.output
    seeder_cls.assert_called_once()


# ---------------------------------------------------------------------------
# assess
# ---------------------------------------------------------------------------

ASSESS_ARGS = ["assess", "--source-gremlin-url", GREMLIN, "--source-cql-host", "localhost", "--graph-name", "food"]


def test_assess_low_risk_passes_and_writes_reports(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("LOW")
        res = runner.invoke(main, [*ASSESS_ARGS, "--output-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "food-assessment-report.json").exists()
    assert (tmp_path / "food-assessment-report.md").exists()


def test_assess_gate_fails_on_high_risk(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("HIGH")
        res = runner.invoke(main, [*ASSESS_ARGS, "--output-dir", str(tmp_path)])
    assert res.exit_code == 1
    assert "Assessment gate FAILED" in res.output


def test_assess_gate_can_be_skipped(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("CRITICAL")
        res = runner.invoke(main, [*ASSESS_ARGS, "--output-dir", str(tmp_path), "--skip-assessment-gate"])
    assert res.exit_code == 0, res.output


def test_assess_filters_by_category_and_severity(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("LOW")
        res = runner.invoke(
            main,
            [*ASSESS_ARGS, "--output-dir", str(tmp_path), "--filter-category", "schema", "--filter-severity", "RED"],
        )
    assert res.exit_code == 0, res.output
    payload = json.loads((tmp_path / "food-assessment-report.json").read_text(encoding="utf-8"))
    assert [f["rule_id"] for f in payload["findings"]] == ["SC-001"]


def test_assess_target_password_comes_from_hcd_cql_password(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy = "from-env"  # a test value, not a credential  # pragma: allowlist secret
    monkeypatch.setenv("HCD_CQL_PASSWORD", dummy)
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("LOW")
        res = runner.invoke(main, [*ASSESS_ARGS, "--output-dir", str(tmp_path), "--target-cql-host", "hcd"])
    assert res.exit_code == 0, res.output
    assert assessor_cls.call_args.kwargs["target_cql_password"] == dummy


@pytest.mark.parametrize("command", ["assess", "run-all"])
def test_target_host_without_password_fails_clearly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """No built-in password (AGENTS.md §6.2): a target host without HCD_CQL_PASSWORD stops with a hint."""
    monkeypatch.delenv("HCD_CQL_PASSWORD", raising=False)
    args = [command, "--source-gremlin-url", GREMLIN, "--source-cql-host", "localhost", "--graph-name", "food"]
    if command == "run-all":
        args += ["--target-gremlin-url", TARGET]
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        res = runner.invoke(main, [*args, "--output-dir", str(tmp_path), "--target-cql-host", "hcd"])
    assert res.exit_code == 1
    assert "HCD_CQL_PASSWORD" in " ".join(res.output.split())
    assessor_cls.assert_not_called()


# ---------------------------------------------------------------------------
# analyse / translate
# ---------------------------------------------------------------------------


def test_analyse(runner: CliRunner, tmp_path: Path) -> None:
    result = AnalysisResult(graph_name="food", engine="dsg", extracted_at="t")
    with patch("dse_jg_migrator.analyser.Analyser") as analyser_cls:
        analyser_cls.return_value.run.return_value = result
        res = runner.invoke(
            main,
            [
                "analyse",
                "--source-gremlin-url",
                GREMLIN,
                "--source-cql-host",
                "localhost",
                "--graph-name",
                "food",
                "--output-dir",
                str(tmp_path),
            ],
        )
    assert res.exit_code == 0, res.output
    assert "Analysis written" in res.output


def test_translate_end_to_end_writes_schema(runner: CliRunner, tmp_path: Path) -> None:
    analysis = _analysis_file(tmp_path)
    res = runner.invoke(main, ["translate", "--analysis", str(analysis), "--output-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "food-janusgraph-schema.json").exists()
    assert (tmp_path / "food-translation-report.md").exists()


def test_translate_missing_analysis_file(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(main, ["translate", "--analysis", str(tmp_path / "nope.json")])
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# init-target
# ---------------------------------------------------------------------------


def test_init_target_success_and_index_timeout_minutes(runner: CliRunner, tmp_path: Path) -> None:
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    with patch("dse_jg_migrator.initialiser.Initialiser") as init_cls:
        init_cls.return_value.run.return_value = {"status": "ok", "created": ["a", "b"], "errors": []}
        res = runner.invoke(
            main,
            ["init-target", "--target-gremlin-url", TARGET, "--schema", str(schema), "--index-timeout", "5"],
        )
    assert res.exit_code == 0, res.output
    assert "initialised successfully" in res.output
    assert init_cls.call_args.kwargs["index_timeout_s"] == 300


def test_init_target_default_index_timeout_is_30_minutes(runner: CliRunner, tmp_path: Path) -> None:
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    with patch("dse_jg_migrator.initialiser.Initialiser") as init_cls:
        init_cls.return_value.run.return_value = {"created": [], "errors": []}
        runner.invoke(main, ["init-target", "--target-gremlin-url", TARGET, "--schema", str(schema)])
    assert init_cls.call_args.kwargs["index_timeout_s"] == 1800


def test_init_target_reports_errors(runner: CliRunner, tmp_path: Path) -> None:
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    with patch("dse_jg_migrator.initialiser.Initialiser") as init_cls:
        init_cls.return_value.run.return_value = {"created": [], "errors": ["VertexLabel:x - boom"]}
        res = runner.invoke(main, ["init-target", "--target-gremlin-url", TARGET, "--schema", str(schema)])
    assert res.exit_code == 1  # live finding: exit 0 let run-all migrate into a missing schema
    assert "Schema initialisation failed" in res.output
    assert "VertexLabel:x - boom" in res.output


# ---------------------------------------------------------------------------
# migrate / validate / report
# ---------------------------------------------------------------------------


def test_migrate(runner: CliRunner, tmp_path: Path) -> None:
    analysis = _analysis_file(tmp_path)
    with patch("dse_jg_migrator.migrator.Migrator") as mig_cls:
        mig_cls.return_value.run.return_value = {"vertices": {"person": 5}, "edges": {"knows": 2}}
        res = runner.invoke(
            main,
            [
                "migrate",
                "--analysis",
                str(analysis),
                "--source-gremlin-url",
                GREMLIN,
                "--target-gremlin-url",
                TARGET,
                "--output-dir",
                str(tmp_path),
            ],
        )
    assert res.exit_code == 0, res.output
    assert "5 vertices, 2 edges" in res.output


def test_migrate_reports_failures_warnings_and_exits_non_zero(runner: CliRunner, tmp_path: Path) -> None:
    analysis = _analysis_file(tmp_path)
    with patch("dse_jg_migrator.migrator.Migrator") as mig_cls:
        mig_cls.return_value.run.return_value = {
            "vertices": {"person": 5},
            "edges": {},
            "errors": ["Vertex batch of 100 for 'person' failed on ws://t:8182/gremlin after 3 retries: boom"],
            "already_migrated": 7,
            "lossy_decimals": {"price": 2},
            "missing_endpoints": {"knows": 1},
        }
        res = runner.invoke(
            main,
            ["migrate", "--analysis", str(analysis), "--source-gremlin-url", GREMLIN, "--target-gremlin-url", TARGET]
            + ["--decimal-strategy", "string", "--output-dir", str(tmp_path)],
        )
    assert res.exit_code == 1, res.output
    out = " ".join(res.output.split())  # rich wraps long lines at the terminal width
    assert "Migration incomplete" in out and "after 3 retries" in out
    assert "7 element(s) already migrated" in out
    assert "'price' Decimal" in out and "'knows' edge(s) skipped" in out
    assert mig_cls.call_args.kwargs["decimal_strategy"] == "string"
    assert mig_cls.call_args.kwargs["udt_strategy"] == "flat" and mig_cls.call_args.kwargs["map_strategy"] == "flat"


@pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
def test_validate_exit_code_reflects_result(runner: CliRunner, tmp_path: Path, ok: bool, code: int) -> None:
    analysis = _analysis_file(tmp_path)
    report = {"overall_pass": ok, "count_results": [{"match": ok}], "smoke_results": []}
    with patch("dse_jg_migrator.validator.Validator") as val_cls:
        val_cls.return_value.run.return_value = report
        res = runner.invoke(
            main,
            [
                "validate",
                "--analysis",
                str(analysis),
                "--source-gremlin-url",
                GREMLIN,
                "--target-gremlin-url",
                TARGET,
                "--output-dir",
                str(tmp_path),
            ],
        )
    assert res.exit_code == code, res.output


def test_report_command(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(main, ["report", "--graph-name", "food", "--output-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "food-migration-report.md").exists()


# ---------------------------------------------------------------------------
# run-all
# ---------------------------------------------------------------------------

RUN_ALL = [
    "run-all",
    "--source-gremlin-url",
    GREMLIN,
    "--source-cql-host",
    "localhost",
    "--graph-name",
    "food",
    "--target-gremlin-url",
    TARGET,
]


def test_run_all_analyze_only_stops_after_assessment(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("LOW")
        res = runner.invoke(main, [*RUN_ALL, "--output-dir", str(tmp_path), "--analyze-only"])
    assert res.exit_code == 0, res.output
    assert "Stopping after assessment" in res.output


def test_run_all_blocks_on_high_risk(runner: CliRunner, tmp_path: Path) -> None:
    with patch("dse_jg_migrator.assessor.Assessor") as assessor_cls:
        assessor_cls.return_value.run.return_value = _assessment("HIGH")
        res = runner.invoke(main, [*RUN_ALL, "--output-dir", str(tmp_path)])
    assert res.exit_code == 1


def test_run_all_full_pipeline_with_seed(runner: CliRunner, tmp_path: Path) -> None:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="t")
    an.vertex_labels.append(VertexLabelDefinition(label="person"))

    def fake_analyser_run() -> AnalysisResult:
        (tmp_path / "food-analysis.json").write_text(json.dumps(an.to_dict()), encoding="utf-8")
        return an

    with (
        patch("dse_jg_migrator.seeder.Seeder") as seeder_cls,
        patch("dse_jg_migrator.assessor.Assessor") as assessor_cls,
        patch("dse_jg_migrator.analyser.Analyser") as analyser_cls,
        patch("dse_jg_migrator.initialiser.Initialiser") as init_cls,
        patch("dse_jg_migrator.migrator.Migrator") as mig_cls,
        patch("dse_jg_migrator.validator.Validator") as val_cls,
    ):
        seeder_cls.return_value.run.return_value = SeederResult("food", "food", 1, 1, "n")
        assessor_cls.return_value.run.return_value = _assessment("LOW")
        analyser_cls.return_value.run.side_effect = fake_analyser_run
        init_cls.return_value.run.return_value = {"created": ["x"], "errors": []}
        mig_cls.return_value.run.return_value = {"vertices": {"person": 1}, "edges": {}}
        val_cls.return_value.run.return_value = {"overall_pass": True, "count_results": [{"match": True}]}
        res = runner.invoke(main, [*RUN_ALL, "--output-dir", str(tmp_path), "--seed"])

    assert res.exit_code == 0, res.output
    assert "Migration pipeline complete" in res.output
    seeder_cls.return_value.run.assert_called_once()
    init_cls.assert_called_once()
    assert init_cls.call_args.kwargs["index_timeout_s"] == 1800
    assert (tmp_path / "food-migration-report.md").exists()


# ---------------------------------------------------------------------------
# Connection failures: one clear line, exit 1, no traceback (found in a fresh-user test)
# ---------------------------------------------------------------------------


def test_connection_failure_prints_one_line_and_exits_1(runner: CliRunner) -> None:
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.side_effect = ConnectionError(
            "seeding the 'food' template: DSE Gremlin server ws://localhost:8183/gremlin is not reachable"
        )
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food", "--confirm"])
    out = " ".join(res.output.split())
    assert res.exit_code == 1
    assert "Cannot connect" in out and "ws://localhost:8183/gremlin" in out and "./docker/up.sh" in out
    assert "Traceback" not in res.output


def test_driver_connection_errors_are_recognised_by_class_name() -> None:
    from dse_jg_migrator.cli import _is_connection_error

    class NoHostAvailable(Exception):  # noqa: N818  # same name as cassandra.cluster.NoHostAvailable, by design
        pass

    class ClientConnectionError(Exception):  # stands in for aiohttp's base connection error
        pass

    class ClientConnectorError(ClientConnectionError):
        pass

    assert _is_connection_error(NoHostAvailable()) and _is_connection_error(ClientConnectorError())
    assert _is_connection_error(ConnectionRefusedError()) and _is_connection_error(TimeoutError())
    assert not _is_connection_error(ValueError("bug")) and not _is_connection_error(FileNotFoundError())


def test_graph_not_found_is_reported_without_traceback(runner: CliRunner, tmp_path: Path) -> None:
    from dse_jg_migrator.analyser import GraphNotFoundError

    with patch("dse_jg_migrator.analyser.Analyser") as analyser_cls:
        analyser_cls.return_value.run.side_effect = GraphNotFoundError(
            "graph 'nope' on ws://x:8183/gremlin has no schema"
        )
        res = runner.invoke(
            main,
            ["analyse", "--source-gremlin-url", GREMLIN, "--source-cql-host", "h", "--graph-name", "nope"]
            + ["--output-dir", str(tmp_path)],
        )
    assert res.exit_code == 1 and "has no schema" in res.output and "Traceback" not in res.output


def test_unexpected_errors_still_raise(runner: CliRunner) -> None:
    """Only connection / graph-not-found errors are softened; real bugs must surface."""
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.side_effect = ValueError("bug")
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food", "--confirm"])
    assert res.exit_code != 0 and isinstance(res.exception, ValueError)


def test_seed_prints_counted_totals_or_the_requested_sizes(runner: CliRunner) -> None:
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.return_value = SeederResult("food", "food", 100, 200, "next", 210, 500)
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food", "--confirm"])
    out = " ".join(res.output.split())
    assert res.exit_code == 0 and "210 vertices and 500 edges" in out and "per primary label" in out
    with patch("dse_jg_migrator.seeder.Seeder") as seeder_cls:
        seeder_cls.return_value.run.return_value = SeederResult("food", "food", 100, 200, "next")
        res = runner.invoke(main, ["seed", "--source-gremlin-url", GREMLIN, "--template", "food", "--confirm"])
    assert "Requested 100 vertices / 200 edges per primary label" in " ".join(res.output.split())
