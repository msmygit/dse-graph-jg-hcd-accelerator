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
