"""Unit tests for assessment_reporter.py."""

from __future__ import annotations

import json
import os

from dse_jg_migrator.models import AssessmentFinding, AssessmentResult, IndexMapping


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

    assert data["schema_version"] == "1.1"  # 1.1 adds index_analysis
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


# ---------------------------------------------------------------------------
# Cluster Overview and Index Analysis (plan Step 0.3 §1 and §4)
# ---------------------------------------------------------------------------


def _with_indexes(result: AssessmentResult) -> AssessmentResult:
    result.index_analysis = [
        IndexMapping(
            "recipe_search",
            "searchIndex",
            "recipe",
            ["instructions"],
            "recipe_search",
            "mixed",
            "OpenSearch / Elasticsearch",
            "instructions: `Search.token*()` → `textContains`",
        ),
        IndexMapping(
            "person_by_name",
            "materializedView",
            "person",
            ["name"],
            "person_by_name",
            "composite",
            "HCD (storage backend)",
            "equality on name",
        ),
    ]
    result.cluster_overview = {
        "vertex_label_count": 3,
        "edge_label_count": 3,
        "property_key_count": 12,
        "index_count": 2,
        "indexes_by_kind": {"materializedView": 1, "searchIndex": 1},
        "schema_mode": "unknown",
        "olap_enabled": False,
    }
    return result


def test_markdown_shows_where_the_search_backend_is_needed(tmp_path: os.PathLike) -> None:
    """Plan Step 0.3 §4: each DSE index, its JanusGraph index and backend — missing from the live report."""
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    paths = AssessmentReporter(_with_indexes(_make_result()), output_dir=str(tmp_path)).write_all()
    md = open(paths["markdown"], encoding="utf-8").read()
    assert "| Search backend (OpenSearch / Elasticsearch) | Required — 1 mixed index(es) |" in md
    assert "**OpenSearch / Elasticsearch is required** for 1 mixed index(es): `recipe_search`" in md
    assert "## Index Analysis" in md
    assert "| recipe_search | searchIndex | recipe | instructions | recipe_search (mixed) | 🔎 OpenSearch" in md
    assert "| person_by_name | materializedView | person | name | person_by_name (composite) | HCD" in md
    assert "## Cluster Overview" in md
    assert "| Indexes | 2 (1 materializedView, 1 searchIndex) |" in md
    assert "| OLAP Enabled | no |" in md


def test_json_and_html_carry_the_index_analysis(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    paths = AssessmentReporter(_with_indexes(_make_result()), output_dir=str(tmp_path)).write_all()
    data = json.load(open(paths["json"], encoding="utf-8"))
    first = data["index_analysis"][0]
    assert (first["dse_index"], first["jg_kind"], first["needs_search_backend"]) == ("recipe_search", "mixed", True)
    assert data["index_analysis"][1]["needs_search_backend"] is False
    html = open(paths["html"], encoding="utf-8").read()
    assert "<h2>Index Analysis</h2>" in html
    assert "<td>OpenSearch / Elasticsearch</td>" in html


def test_search_backend_not_required_without_mixed_indexes(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _with_indexes(_make_result())
    result.index_analysis = result.index_analysis[1:]
    md = open(AssessmentReporter(result, output_dir=str(tmp_path)).write_all()["markdown"], encoding="utf-8").read()
    assert "| Search backend (OpenSearch / Elasticsearch) | Not required by this schema's indexes |" in md
    assert "is required" not in md


def test_no_index_sections_when_no_schema_was_read(tmp_path: os.PathLike) -> None:
    from dse_jg_migrator.assessment_reporter import AssessmentReporter

    result = _make_result()
    result.cluster_overview = {}  # the source schema could not be read (CF-006)
    md = open(AssessmentReporter(result, output_dir=str(tmp_path)).write_all()["markdown"], encoding="utf-8").read()
    assert "| Search backend (OpenSearch / Elasticsearch) | Unknown — no indexes were read |" in md
    assert "## Index Analysis" not in md
    assert "## Cluster Overview" not in md
