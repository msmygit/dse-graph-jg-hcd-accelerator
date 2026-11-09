"""Unit tests for reporter.py (migration report), incl. Appendix E.6 / E.7."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dse_jg_migrator.reporter import Reporter


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _report(tmp_path: Path) -> str:
    out = Reporter(graph_name="food", output_dir=str(tmp_path)).generate({"migration": {"ok": False, "duration": "3s"}})
    return Path(out).read_text(encoding="utf-8")


def test_generate_minimal(tmp_path: Path) -> None:
    content = _report(tmp_path)
    assert "# Migration Report: food" in content
    assert "| Migration | ❌ FAIL | 3s |" in content
    assert "RLAC" not in content


def test_generate_includes_counts_events_validation_and_notes(tmp_path: Path) -> None:
    audit = [
        {"kind": "vertex", "label": "person", "migrated": 10, "failed": 1},
        {"kind": "SKIPPED_OLAP", "label": "a", "message": "OLAP traversal source 'a' is not migrated"},
    ]
    _write(tmp_path / "food-migration-audit.jsonl", "\n".join(json.dumps(e) for e in audit) + "\n")
    _write(tmp_path / "food-validation-report.md", "# V\n| Type | Label |\n|---|---|\n")
    _write(tmp_path / "food-translation-report.md", "# T\n## Notes\nnote one\n## Other\nignored\n")
    content = _report(tmp_path)
    assert "| vertex | person | 10 | 1 |" in content
    assert "## Migration Events" in content
    assert "| SKIPPED_OLAP | a |" in content
    assert "## Validation Summary" in content
    assert "note one" in content
    assert "ignored" not in content
    # Event entries must not appear in the counts table with bogus zero counts.
    assert "| SKIPPED_OLAP | a | 0 | 0 |" not in content


def test_rlac_section_from_analysis_config(tmp_path: Path) -> None:
    analysis = {
        "config": {"rlac_enabled": True},
        "vertex_labels": [{"label": "person"}, {"label": "recipe"}],
        "edge_labels": [{"label": "knows"}],
    }
    _write(tmp_path / "food-analysis.json", json.dumps(analysis))
    content = _report(tmp_path)
    assert "Row-Level Access Control (RLAC)" in content
    assert "manual action required" in content
    assert "person, recipe" in content
    assert "knows" in content


def test_rlac_section_from_assessment_finding(tmp_path: Path) -> None:
    _write(tmp_path / "food-analysis.json", json.dumps({"vertex_labels": [{"label": "person"}]}))
    _write(tmp_path / "food-assessment-report.json", json.dumps({"findings": [{"rule_id": "SE-001"}]}))
    assert "RLAC" in _report(tmp_path)


def test_rlac_section_absent_when_not_flagged(tmp_path: Path) -> None:
    _write(tmp_path / "food-analysis.json", json.dumps({"config": {"rlac_enabled": False}}))
    _write(tmp_path / "food-assessment-report.json", json.dumps({"findings": [{"rule_id": "QP-001"}]}))
    assert "RLAC" not in _report(tmp_path)


def test_corrupt_json_files_are_tolerated(tmp_path: Path) -> None:
    _write(tmp_path / "food-analysis.json", "{not json")
    _write(tmp_path / "food-migration-audit.jsonl", "{not json\n")
    content = _report(tmp_path)
    assert os.path.exists(tmp_path / "food-migration-report.md")
    assert "RLAC" not in content


def _file_report(tmp_path: Path) -> str:
    """The report as `dse-jg-migrator report` writes it: no pipeline stats, only the files in output_dir."""
    return Path(Reporter(graph_name="food", output_dir=str(tmp_path)).generate()).read_text(encoding="utf-8")


def test_counts_are_one_total_per_label(tmp_path: Path) -> None:
    """Live finding: one row per batch listed `person` four times and no partial batches, edges or small labels."""
    audit = [
        {"kind": "vertex", "label": "person", "batch_start": 0, "migrated": 100, "failed": 0},
        {"kind": "vertex", "label": "person", "batch_start": 100, "migrated": 98, "failed": 2},
        {"kind": "vertex", "label": "location", "batch_start": 0, "migrated": 30, "failed": 0},
        {"kind": "BATCH_FAILED", "label": "person", "message": "boom"},
        {"kind": "edge", "label": "knows", "batch_start": 0, "migrated": 434, "failed": 0},
    ]
    _write(tmp_path / "food-migration-audit.jsonl", "\n".join(json.dumps(e) for e in audit) + "\n")
    content = _file_report(tmp_path)
    counts = content.split("## Migration Counts")[1].split("##")[0]
    rows = [ln for ln in counts.splitlines() if ln.startswith("| ") and "Kind" not in ln]
    assert rows == ["| vertex | person | 198 | 2 |", "| vertex | location | 30 | 0 |", "| edge | knows | 434 | 0 |"]


def test_phase_status_comes_from_the_output_files(tmp_path: Path) -> None:
    """Live finding: every phase always read "✅ OK" (nothing passed stats), even an assessment with RED findings."""
    findings = [{"severity": "RED"}, {"severity": "RED"}, {"severity": "YELLOW"}]
    _write(tmp_path / "food-assessment-report.json", json.dumps({"overall_risk": "HIGH", "findings": findings}))
    _write(tmp_path / "food-analysis.json", "{}")
    _write(tmp_path / "food-janusgraph-schema.json", "{}")
    _write(tmp_path / "food-migration-audit.jsonl", json.dumps({"kind": "vertex", "label": "p", "migrated": 5}) + "\n")
    _write(tmp_path / "food-validation-report.md", "# V\n\n**Overall:** ✅ PASS\n")
    content = _file_report(tmp_path)
    assert "| Assessment | ⚠️ HIGH risk — 2 RED finding(s), see the assessment report |" in content
    for phase in ("Analysis", "Translation", "Init Target", "Migration"):
        assert f"| {phase} | ✅ OK |" in content
    assert "| Validation | ✅ PASS |" in content
    assert "Duration" not in content  # no timings are known when reading files


def test_phase_status_shows_failures_and_phases_not_run(tmp_path: Path) -> None:
    audit = [
        {"kind": "vertex", "label": "p", "migrated": 0, "failed": 5},
        {"kind": "BATCH_FAILED", "label": "p", "message": "boom"},
    ]
    _write(tmp_path / "food-migration-audit.jsonl", "\n".join(json.dumps(e) for e in audit) + "\n")
    _write(tmp_path / "food-validation-report.md", "**Overall:** ❌ FAIL\n")
    content = _file_report(tmp_path)
    for phase in ("Assessment", "Analysis", "Translation", "Init Target"):
        assert f"| {phase} | — not run |" in content
    assert "| Migration | ❌ 1 failed batch(es) or read(s) — re-run migrate |" in content
    assert "| Validation | ❌ FAIL |" in content


def test_assessment_without_red_findings_shows_its_risk(tmp_path: Path) -> None:
    _write(tmp_path / "food-assessment-report.json", json.dumps({"overall_risk": "LOW", "findings": []}))
    assert "| Assessment | ✅ LOW risk |" in _file_report(tmp_path)
