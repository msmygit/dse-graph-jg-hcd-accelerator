"""Unit tests for assessment_reporter.py."""

from __future__ import annotations

import json
import os

from dse_jg_migrator.models import AssessmentFinding, AssessmentResult


def _make_result(risk: str = "HIGH") -> AssessmentResult:
    return AssessmentResult(
        graph_name="food",
        engine="dsg",
        assessed_at="2026-11-07T16:31:30Z",
        overall_risk=risk,
        findings=[
            AssessmentFinding(
                rule_id="OL-001",
                category="olap",
                severity="RED",
                title="OLAP traversal source 'a' is active",
                detail="The 'a' traversal source responded to probe.",
                remediation="Deploy JanusGraph with HadoopGraph.",
                docs_link="https://docs.janusgraph.org/advanced-topics/hadoop/",
            ),
            AssessmentFinding(
                rule_id="QP-001",
                category="query_pattern",
                severity="YELLOW",
                title="g.with('allow-filtering') usage",
                detail="Pattern detected in schema.",
                remediation="Remove g.with('allow-filtering').",
                docs_link="https://docs.janusgraph.org/",
            ),
            AssessmentFinding(
                rule_id="HC-006",
                category="hcd_target",
                severity="GREEN",
                title="HCD CQL port 9042, datacenter dc1",
                detail="Fully compatible.",
                remediation="No action needed.",
                docs_link="https://docs.janusgraph.org/",
            ),
        ],
        summary={
            "total_findings": 3,
            "by_severity": {"RED": 1, "YELLOW": 1, "GREEN": 1},
            "by_category": {"olap": 1, "query_pattern": 1, "hcd_target": 1},
        },
        cluster_overview={"vertex_label_count": 2},
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_write_markdown_contains_table_headers(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    md_path = paths["markdown"]
    assert os.path.exists(md_path)
    content = open(md_path, encoding="utf-8").read()
    assert "## Executive Summary" in content
    assert "| Metric | Value |" in content
    assert "OL-001" in content
    assert "OLAP" in content.upper() or "olap" in content


def test_write_markdown_contains_blockers_section(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result("HIGH")
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    content = open(paths["markdown"], encoding="utf-8").read()
    assert "Blockers" in content


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_write_json_valid_schema(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    with open(paths["json"], encoding="utf-8") as fh:
        data = json.load(fh)

    assert data["schema_version"] == "1.0"
    assert data["graph_name"] == "food"
    assert data["overall_risk"] == "HIGH"
    assert isinstance(data["findings"], list)
    assert len(data["findings"]) == 3

    # Validate first finding structure
    finding = data["findings"][0]
    assert "rule_id" in finding
    assert "category" in finding
    assert "severity" in finding
    assert "title" in finding
    assert "detail" in finding
    assert "remediation" in finding
    assert "docs_link" in finding


def test_write_json_counts_by_severity(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    with open(paths["json"], encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["summary"]["by_severity"]["RED"] == 1
    assert data["summary"]["by_severity"]["YELLOW"] == 1
    assert data["summary"]["by_severity"]["GREEN"] == 1


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_write_html_contains_table_tags(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    content = open(paths["html"], encoding="utf-8").read()
    assert "<table>" in content
    assert "<th>" in content
    assert "OL-001" in content
    assert "food" in content


def test_write_html_risk_badge_class(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result("CRITICAL")
    result.overall_risk = "CRITICAL"
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    content = open(paths["html"], encoding="utf-8").read()
    assert "risk-CRITICAL" in content


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------


def test_output_files_named_correctly(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    assert paths["markdown"].endswith("food-assessment-report.md")
    assert paths["json"].endswith("food-assessment-report.json")
    assert paths["html"].endswith("food-assessment-report.html")


# ---------------------------------------------------------------------------
# LOW risk — no blockers section
# ---------------------------------------------------------------------------


def test_low_risk_report_no_blockers_section(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result("LOW")
    result.findings = [
        AssessmentFinding(
            rule_id="HC-006",
            category="hcd_target",
            severity="GREEN",
            title="Compatible",
            detail="OK",
            remediation="None",
            docs_link="L",
        )
    ]
    result.summary = {"total_findings": 1, "by_severity": {"GREEN": 1, "RED": 0, "YELLOW": 0}, "by_category": {}}
    reporter = AssessmentReporter(result, output_dir=str(tmp_path))
    paths = reporter.write_all()
    content = open(paths["markdown"], encoding="utf-8").read()
    # The executive summary table contains "Blockers (RED)" as a metric name —
    # but the full "## Blockers" section header should NOT appear for LOW risk.
    assert "## Blockers" not in content
