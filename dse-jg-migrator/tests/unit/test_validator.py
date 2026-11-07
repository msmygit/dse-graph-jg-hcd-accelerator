"""Unit tests for validator.py."""

from __future__ import annotations

import os
from unittest.mock import patch

from dse_jg_migrator.models import AnalysisResult, EdgeLabelDefinition, VertexLabelDefinition
from dse_jg_migrator.validator import Validator


def _make_analysis() -> AnalysisResult:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    an.vertex_labels.append(VertexLabelDefinition(label="person", count=100))
    an.vertex_labels.append(VertexLabelDefinition(label="recipe", count=50))
    an.edge_labels.append(EdgeLabelDefinition(label="knows", count=200))
    return an


def _make_validator(tmp_path, **kwargs) -> Validator:  # type: ignore[no-untyped-def]
    defaults = {
        "analysis": _make_analysis(),
        "source_gremlin_url": "ws://localhost:8183/gremlin",
        "target_gremlin_url": "ws://localhost:8182/gremlin",
        "output_dir": str(tmp_path),
    }
    defaults.update(kwargs)
    return Validator(**defaults)


# ---------------------------------------------------------------------------
# Count validation
# ---------------------------------------------------------------------------


def test_validate_counts_all_match(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        label = bindings.get("lbl", "")
        counts = {"person": 100, "recipe": 50, "knows": 200}
        return [counts.get(label, 0)]

    with patch.object(v, "_submit", side_effect=mock_submit):
        results = v._validate_counts()

    assert all(r["match"] for r in results)


def test_validate_counts_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)
    call_count = [0]

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        call_count[0] += 1
        # Source returns 100 for person, target returns 90
        label = bindings.get("lbl", "")
        if "8183" in url:
            return [100 if label == "person" else 50]
        return [90 if label == "person" else 50]

    with patch.object(v, "_submit", side_effect=mock_submit):
        results = v._validate_counts()

    person_result = next(r for r in results if r["label"] == "person")
    assert not person_result["match"]
    assert person_result["source"] == 100
    assert person_result["target"] == 90


def test_validate_counts_gremlin_error_returns_minus_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        raise RuntimeError("connection refused")

    with patch.object(v, "_submit", side_effect=mock_submit):
        results = v._validate_counts()

    for r in results:
        assert r["source"] == -1


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def test_run_writes_report_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        return [100]

    with patch.object(v, "_submit", side_effect=mock_submit):
        v.run()

    report_file = os.path.join(str(tmp_path), "food-validation-report.md")
    assert os.path.exists(report_file)
    content = open(report_file, encoding="utf-8").read()
    assert "# Validation Report" in content
    assert "| Type |" in content


def test_run_overall_pass_true_when_all_match(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        return [100]

    with patch.object(v, "_submit", side_effect=mock_submit):
        report = v.run()

    assert report["overall_pass"]


def test_run_overall_pass_false_on_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)
    call_count = [0]

    def mock_submit(url: str, script: str, bindings: dict,
                    username, password) -> list:
        call_count[0] += 1
        return [100 if "8183" in url else 90]

    with patch.object(v, "_submit", side_effect=mock_submit):
        report = v.run()

    assert not report["overall_pass"]
