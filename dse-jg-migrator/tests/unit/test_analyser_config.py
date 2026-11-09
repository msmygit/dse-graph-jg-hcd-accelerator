"""Analyser config extraction: OLAP / RLAC flags feed Appendix E.6 / E.7."""

from __future__ import annotations

from unittest.mock import patch

from dse_jg_migrator.analyser import Analyser


def _analyser() -> Analyser:
    return Analyser("ws://localhost:8183/gremlin", "localhost", 9043, "food")


def test_extract_config_flags_olap_when_probe_responds() -> None:
    a = _analyser()

    def sub(script: str, bindings: dict | None = None) -> list:
        return ["Production allow_scan true"] if "describe" in script else [0]

    with patch.object(a, "_gremlin_submit", side_effect=sub):
        cfg = a._extract_config()
    assert cfg.olap_enabled is True
    assert cfg.schema_mode == "Production"


def test_extract_config_olap_false_when_probe_fails() -> None:
    a = _analyser()

    def sub(script: str, bindings: dict | None = None) -> list:
        if script.startswith("a."):
            raise RuntimeError("no OLAP")
        return ["Development"]

    with patch.object(a, "_gremlin_submit", side_effect=sub):
        cfg = a._extract_config()
    assert cfg.olap_enabled is False
    assert cfg.rlac_enabled is False


def test_extract_config_detects_rlac_marker() -> None:
    a = _analyser()

    def sub(script: str, bindings: dict | None = None) -> list:
        if script.startswith("a."):
            raise RuntimeError("no OLAP")
        return ["Development restrict_lambda true row_level_access enabled"]

    with patch.object(a, "_gremlin_submit", side_effect=sub):
        cfg = a._extract_config()
    assert cfg.rlac_enabled is True
    assert cfg.restrict_lambda is True


def test_analysis_round_trip_preserves_olap_and_rlac(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    from dse_jg_migrator.models import AnalysisResult
    from dse_jg_migrator.translator import Translator

    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="t")
    an.config.olap_enabled = True
    an.config.rlac_enabled = True
    path = tmp_path / "food-analysis.json"
    path.write_text(json.dumps(an.to_dict()), encoding="utf-8")

    loaded = Translator(analysis=an, output_dir=str(tmp_path)).load_analysis(str(path))
    assert loaded.config.olap_enabled is True
    assert loaded.config.rlac_enabled is True
