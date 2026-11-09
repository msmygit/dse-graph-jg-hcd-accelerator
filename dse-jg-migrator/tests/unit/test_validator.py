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

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
        label = bindings.get("lbl", "")
        counts = {"person": 100, "recipe": 50, "knows": 200}
        return [counts.get(label, 0)]

    with patch.object(v, "_submit", side_effect=mock_submit):
        results = v._validate_counts()

    assert all(r["match"] for r in results)


def test_validate_counts_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)
    call_count = [0]

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
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

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
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

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
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

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
        return [100]

    with patch.object(v, "_submit", side_effect=mock_submit):
        report = v.run()

    assert report["overall_pass"]


def test_run_overall_pass_false_on_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)
    call_count = [0]

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
        call_count[0] += 1
        return [100 if "8183" in url else 90]

    with patch.object(v, "_submit", side_effect=mock_submit):
        report = v.run()

    assert not report["overall_pass"]


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def test_smoke_tests_none_without_queries_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert _make_validator(tmp_path)._validate_smoke_tests() == []


def test_smoke_tests_missing_file_returns_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path, validation_queries_path=str(tmp_path / "missing.groovy"))
    assert v._validate_smoke_tests() == []


def test_smoke_tests_match_mismatch_and_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    qfile = tmp_path / "q.groovy"
    qfile.write_text("// comment\n\ng.V().count()\ng.E().count()\ng.boom()\n", encoding="utf-8")
    v = _make_validator(tmp_path, validation_queries_path=str(qfile))

    def mock_submit(url: str, script: str, bindings: dict, username, password) -> list:
        if "boom" in script:
            raise RuntimeError("bad query")
        if "E()" in script:
            return [1] if "8183" in url else [2]
        return [5]

    with patch.object(v, "_submit", side_effect=mock_submit):
        results = v._validate_smoke_tests()

    assert [r["match"] for r in results] == [True, False, False]
    assert results[2]["error"] == "bad query"


def test_report_includes_smoke_section(tmp_path) -> None:  # type: ignore[no-untyped-def]
    qfile = tmp_path / "q.groovy"
    qfile.write_text("g.V().count()\n", encoding="utf-8")
    v = _make_validator(tmp_path, validation_queries_path=str(qfile))
    with patch.object(v, "_submit", return_value=[100]):
        v.run()
    content = open(os.path.join(str(tmp_path), "food-validation-report.md"), encoding="utf-8").read()
    assert "## Smoke Tests" in content


def test_submit_passes_credentials_and_closes_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types
    from unittest.mock import MagicMock

    v = _make_validator(tmp_path)
    fake = types.ModuleType("gremlin_python.driver.client")
    fake.Client = MagicMock()  # type: ignore[attr-defined]
    fake.Client.return_value.submit.return_value.all.return_value.result.return_value = [7]
    drv = types.ModuleType("gremlin_python.driver")
    drv.client = fake  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {
            "gremlin_python": types.ModuleType("gremlin_python"),
            "gremlin_python.driver": drv,
            "gremlin_python.driver.client": fake,
        },
    ):
        assert v._submit("ws://x", "g.V().count()", {}, "u", "p") == [7]
    fake.Client.assert_called_once_with("ws://x", "g", username="u", password="p")  # noqa: S106
    fake.Client.return_value.close.assert_called_once()


def test_empty_analysis_never_passes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: an unparsed (Classic) schema gave 0 labels and validate reported '0/0 passed'."""
    v = _make_validator(tmp_path, analysis=AnalysisResult(graph_name="g", engine="classic", extracted_at="x"))
    report = v.run()
    assert report["overall_pass"] is False
    assert "nothing was validated" in report["reason"]


def test_two_failed_counts_are_not_a_match(tmp_path) -> None:  # type: ignore[no-untyped-def]
    v = _make_validator(tmp_path)
    with patch.object(v, "_submit", side_effect=RuntimeError("both endpoints down")):
        report = v.run()
    assert all(r["source"] == -1 and r["target"] == -1 for r in report["count_results"])
    assert not any(r["match"] for r in report["count_results"])
    assert report["overall_pass"] is False
