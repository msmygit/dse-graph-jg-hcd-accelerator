"""Unit tests for migrator.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from dse_jg_migrator.migrator import Migrator
from dse_jg_migrator.models import AnalysisResult, EdgeLabelDefinition, VertexLabelDefinition


def _make_analysis() -> AnalysisResult:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    an.vertex_labels.append(vl)
    el = EdgeLabelDefinition(label="knows", from_label="person", to_label="person")
    an.edge_labels.append(el)
    return an


def _make_migrator(tmp_path, **kwargs) -> Migrator:  # type: ignore[no-untyped-def]
    defaults = {
        "analysis": _make_analysis(),
        "source_gremlin_url": "ws://localhost:8183/gremlin",
        "target_gremlin_url": "ws://localhost:8182/gremlin",
        "checkpoint_db": str(tmp_path / "ckpt.db"),
        "audit_log": "food-migration-audit.jsonl",
        "output_dir": str(tmp_path),
    }
    defaults.update(kwargs)
    return Migrator(**defaults)


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


def test_init_checkpoint_creates_tables(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {r[0] for r in tables}
    assert "progress" in table_names
    assert "id_map" in table_names
    conn.close()


def test_mark_and_check_completed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    assert not m._is_completed(conn, "person", "vertex")
    m._mark_completed(conn, "person", "vertex")
    assert m._is_completed(conn, "person", "vertex")
    conn.close()


def test_id_map_persist_and_lookup(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["old_1", "old_2"], ["new_1", "new_2"])
    assert m._lookup_id(conn, "old_1") == "new_1"
    assert m._lookup_id(conn, "old_2") == "new_2"
    assert m._lookup_id(conn, "old_99") is None
    conn.close()


# ---------------------------------------------------------------------------
# Vertex batch flushing
# ---------------------------------------------------------------------------


def test_flush_vertex_batch_calls_submit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    mock_client = MagicMock()
    mock_result_set = MagicMock()
    mock_result_set.all.return_value.result.return_value = ["new_id_1"]
    mock_client.submit.return_value = mock_result_set

    buf = [("person", {"name": "Alice", "person_id": "uuid-1"})]
    new_ids = m._flush_vertex_batch(mock_client, buf)

    assert mock_client.submit.call_count == 1
    assert new_ids == ["new_id_1"]


def test_flush_vertex_batch_empty_buf(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    mock_client = MagicMock()
    new_ids = m._flush_vertex_batch(mock_client, [])
    assert new_ids == []
    mock_client.submit.assert_not_called()


def test_flush_vertex_batch_uses_bindings_not_fstrings(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Ensure no f-string interpolation into Gremlin — must use bindings."""
    m = _make_migrator(tmp_path)
    submitted_scripts: list[str] = []
    submitted_bindings: list[dict] = []

    mock_result_set = MagicMock()
    mock_result_set.all.return_value.result.return_value = ["id_1"]
    mock_client = MagicMock()

    def capture_submit(script: str, bindings: dict) -> MagicMock:
        submitted_scripts.append(script)
        submitted_bindings.append(bindings)
        return mock_result_set

    mock_client.submit.side_effect = capture_submit

    # Inject a potential injection payload as property value
    buf = [("person", {"name": "'; DROP KEYSPACE food; --", "age": 99})]
    m._flush_vertex_batch(mock_client, buf)

    # The injection payload must appear only in bindings, NOT interpolated in script
    assert len(submitted_scripts) == 1
    assert "DROP KEYSPACE" not in submitted_scripts[0]
    injection_in_bindings = any(
        "DROP KEYSPACE" in str(v) for b in submitted_bindings for v in b.values()
    )
    assert injection_in_bindings  # value safely passed as binding


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_append_audit_creates_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    import os

    m = _make_migrator(tmp_path)
    m._append_audit("vertex", "person", 0, 100, 99, 1)

    audit_path = os.path.join(str(tmp_path), "food-migration-audit.jsonl")
    assert os.path.exists(audit_path)
    with open(audit_path, encoding="utf-8") as fh:
        entry = json.loads(fh.readline())
    assert entry["kind"] == "vertex"
    assert entry["label"] == "person"
    assert entry["migrated"] == 99
    assert entry["failed"] == 1
