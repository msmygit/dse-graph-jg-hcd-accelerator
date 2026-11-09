"""Unit tests for migrator.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

from dse_jg_migrator.migrator import Migrator
from dse_jg_migrator.models import AnalysisResult, EdgeLabelDefinition, PartitionKey, VertexLabelDefinition


def _make_analysis() -> AnalysisResult:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    an.vertex_labels.append(vl)
    el = EdgeLabelDefinition(label="knows", from_label="person", to_label="person")
    an.edge_labels.append(el)
    return an


def _row_values(bindings: dict) -> list:  # type: ignore[type-arg]
    """All property values carried in the batched ``rows`` binding."""
    return [p[1] for r in bindings["rows"] for p in r["props"]]


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
    m = Migrator(**defaults)
    m._sleep = lambda _delay: None  # no real backoff waits in unit tests
    return m


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


def test_init_checkpoint_creates_tables(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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
    injection_in_bindings = any("DROP KEYSPACE" in str(v) for b in submitted_bindings for v in b.values())
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


def test_every_edge_batch_is_audited(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: edge batches were never audited, so the report's counts table listed no edges."""
    m = _make_migrator(tmp_path)
    client = MagicMock(**{"submit.return_value.all.return_value.result.return_value": ["e0", "e1"]})
    m._flush_edge_batch(client, [("knows", "1", "2", {}), ("knows", "2", "1", {})])
    m._flush_edge_batch(client, [("knows", "1", "2", {}), ("knows", "2", "1", {})])
    batches = [(e["kind"], e["label"], e["batch_start"], e["migrated"]) for e in _read_audit(tmp_path)]
    assert batches == [("edge", "knows", 0, 2), ("edge", "knows", 2, 2)]


def _run_with_one_vertex_batch(m: Migrator) -> None:
    from unittest.mock import patch

    def migrate(label, conn, client):  # type: ignore[no-untyped-def]
        m._flush_vertex_batch(client, [(label, {"name": "a"})])
        return 1

    client = MagicMock(**{"submit.return_value.all.return_value.result.return_value": ["n0"]})
    with (
        patch.object(m, "_make_gremlin_client", return_value=client),
        patch.object(m, "_migrate_vertices", side_effect=migrate),
        patch.object(m, "_migrate_edges", return_value=0),
    ):
        m.run()


def test_fresh_checkpoint_starts_a_fresh_audit_log(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: the log kept the batches of earlier migrations, so the report counted them again."""
    (tmp_path / "food-migration-audit.jsonl").write_text('{"kind": "vertex", "label": "old", "migrated": 9}\n')
    _run_with_one_vertex_batch(_make_migrator(tmp_path))
    assert [e["label"] for e in _read_audit(tmp_path)] == ["person"]


def test_resumed_checkpoint_appends_to_the_audit_log(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(m.checkpoint_db)
    m._persist_id_map_raw(conn, ["person:u0"], ["n9"])  # an earlier, interrupted run migrated one vertex
    conn.close()
    (tmp_path / "food-migration-audit.jsonl").write_text('{"kind": "vertex", "label": "person", "migrated": 1}\n')
    _run_with_one_vertex_batch(m)
    assert [e.get("migrated") for e in _read_audit(tmp_path)] == [1, 1]


# ---------------------------------------------------------------------------
# Appendix E.9 — id-map archive
# ---------------------------------------------------------------------------


def test_archive_id_map_creates_standalone_sqlite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["person:1", "person:2"], ["100", "200"])

    path = m._archive_id_map(conn)
    # idempotent: a second archive replaces rather than duplicates / errors
    path = m._archive_id_map(conn)
    conn.close()

    assert path == str(tmp_path / "food-id-map.sqlite")
    arch = sqlite3.connect(path)
    rows = dict(arch.execute("SELECT old_id, new_id FROM id_map").fetchall())
    arch.close()
    assert rows == {"person:1": "100", "person:2": "200"}


# ---------------------------------------------------------------------------
# run() orchestration, Appendix E.7 (SKIPPED_OLAP) and E.9 (archive)
# ---------------------------------------------------------------------------


def _read_audit(tmp_path) -> list[dict]:  # type: ignore[no-untyped-def]
    import json
    import os

    path = os.path.join(str(tmp_path), "food-migration-audit.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_run_logs_skipped_olap_when_olap_enabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    analysis = _make_analysis()
    analysis.config.olap_enabled = True
    m = _make_migrator(tmp_path, analysis=analysis)
    with (
        patch.object(m, "_make_gremlin_client", return_value=MagicMock()),
        patch.object(m, "_migrate_vertices", return_value=0),
        patch.object(m, "_migrate_edges", return_value=0),
    ):
        m.run()
    events = [e for e in _read_audit(tmp_path) if e["kind"] == "SKIPPED_OLAP"]
    assert len(events) == 1
    assert "OLAP" in events[0]["message"]


def test_run_no_skipped_olap_when_olap_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path)
    with (
        patch.object(m, "_make_gremlin_client", return_value=MagicMock()),
        patch.object(m, "_migrate_vertices", return_value=0),
        patch.object(m, "_migrate_edges", return_value=0),
    ):
        m.run()
    assert not any(e["kind"] == "SKIPPED_OLAP" for e in _read_audit(tmp_path))


def test_run_collects_stats_archives_id_map_and_resumes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import os
    from unittest.mock import patch

    m = _make_migrator(tmp_path)
    client = MagicMock()
    with (
        patch.object(m, "_make_gremlin_client", return_value=client),
        patch.object(m, "_migrate_vertices", return_value=7) as mv,
        patch.object(m, "_migrate_edges", return_value=3) as me,
    ):
        stats = m.run()
        assert stats["vertices"] == {"person": 7}
        assert stats["edges"] == {"knows": 3}
        assert os.path.exists(stats["id_map_archive"])
        client.close.assert_called()

        # second run resumes from checkpoint: nothing re-migrated
        m.run()
        assert mv.call_count == 1
        assert me.call_count == 1


# ---------------------------------------------------------------------------
# Appendix E.2 — meta-properties
# ---------------------------------------------------------------------------


class _Meta:
    def __init__(self, key: str, value: object) -> None:
        self.key = key
        self.value = value


class _VProp:
    def __init__(self, vid: str, key: str, value: object, metas: list[_Meta]) -> None:
        self.element = MagicMock(id=vid)
        self.key = key
        self.value = value
        self.properties = metas


def _client_returning(*pages: list) -> MagicMock:  # type: ignore[type-arg]
    client = MagicMock()
    results = []
    for page in pages:
        rs = MagicMock()
        rs.all.return_value.result.return_value = page
        results.append(rs)
    client.submit.side_effect = results
    return client


def test_fetch_meta_properties_converts_to_json_and_warns(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import json
    import logging

    m = _make_migrator(tmp_path)
    vp = _VProp("v1", "name", "Alice", [_Meta("source", "crm"), _Meta("confidence", 0.9)])
    plain = _VProp("v2", "name", "Bob", [])
    client = _client_returning([vp, plain])

    with caplog.at_level(logging.WARNING, logger="dse_jg_migrator.migrator"):
        out = m._fetch_meta_properties(client, "person", 0)

    assert set(out) == {"v1"}
    assert json.loads(out["v1"]["name"]) == {"value": "Alice", "meta": {"source": "crm", "confidence": 0.9}}
    assert any("Meta-properties" in r.message and "person.name" in r.message for r in caplog.records)
    events = [e for e in _read_audit(tmp_path) if e["kind"] == "META_PROPERTY_JSON"]
    assert len(events) == 1


def test_meta_property_warning_logged_once_per_label_key(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    m = _make_migrator(tmp_path)
    pages = [[_VProp(f"v{i}", "name", "x", [_Meta("k", 1)]) for i in range(3)]]
    with caplog.at_level(logging.WARNING, logger="dse_jg_migrator.migrator"):
        m._fetch_meta_properties(_client_returning(*pages), "person", 0)
    assert sum("Meta-properties" in r.message for r in caplog.records) == 1


def test_fetch_meta_properties_query_failure_returns_empty(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    m = _make_migrator(tmp_path)
    client = MagicMock()
    client.submit.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.WARNING, logger="dse_jg_migrator.migrator"):
        assert m._fetch_meta_properties(client, "person", 0) == {}
    assert any("Meta-property query failed" in r.message for r in caplog.records)


def test_gremlin_vertex_migration_merges_meta_properties_for_classic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    from unittest.mock import patch

    analysis = _make_analysis()
    analysis.engine = "classic"
    m = _make_migrator(tmp_path, analysis=analysis, gremlin_batch=2)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))

    element_maps = [{"~id": "v1", "~label": "person", "name": "Alice"}]
    vps = [_VProp("v1", "name", "Alice", [_Meta("source", "crm")])]
    source = _client_returning(element_maps, vps, [])  # elementMap page, properties() page, empty => stop

    target = MagicMock()
    flush_result = MagicMock()
    flush_result.all.return_value.result.return_value = ["jg-1"]
    target.submit.return_value = flush_result

    with patch.object(m, "_make_gremlin_client", return_value=source):
        total = m._migrate_vertices_gremlin("person", conn, target)

    assert total == 1
    bindings = target.submit.call_args.args[1]
    meta_values = [v for v in _row_values(bindings) if isinstance(v, str) and v.startswith("{")]
    assert json.loads(meta_values[0])["meta"] == {"source": "crm"}
    assert m._lookup_id(conn, "v1") == "jg-1"
    conn.close()


def test_gremlin_vertex_migration_skips_meta_query_for_dsg(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, gremlin_batch=2)  # engine == "dsg"
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    source = _client_returning([{"~id": "v1", "name": "A"}], [])
    target = MagicMock()
    target.submit.return_value.all.return_value.result.return_value = ["jg-1"]
    with patch.object(m, "_make_gremlin_client", return_value=source):
        m._migrate_vertices_gremlin("person", conn, target)
    assert not any("properties()" in c.args[0] for c in source.submit.call_args_list)
    conn.close()


# ---------------------------------------------------------------------------
# Batch failure handling, edges, CQL paths
# ---------------------------------------------------------------------------


def test_flush_vertex_batch_failure_returns_empty_and_logs(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    m = _make_migrator(tmp_path)
    client = MagicMock()
    client.submit.side_effect = RuntimeError("server down")
    with caplog.at_level(logging.WARNING, logger="dse_jg_migrator.migrator"):
        assert m._flush_vertex_batch(client, [("person", {"name": "A"})]) == []
    assert any("Vertex batch" in r.message and "server down" in r.message for r in caplog.records)


def test_flush_edge_batch_success_and_bindings(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    client = MagicMock()
    client.submit.return_value.all.return_value.result.return_value = ["e1", "e2"]
    buf = [("knows", "1", "2", {"since": "x'; drop"}), ("knows", "2", "3", {})]
    assert m._flush_edge_batch(client, buf) == ["e1", "e2"]
    script, bindings = client.submit.call_args.args
    assert "drop" not in script
    assert "x'; drop" in _row_values(bindings)
    assert set(bindings) == {"rows", "geo"}  # server limit: <= 16 bindings per request


def test_flush_edge_batch_empty_and_failure(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    m = _make_migrator(tmp_path)
    client = MagicMock()
    assert m._flush_edge_batch(client, []) == []
    client.submit.side_effect = RuntimeError("nope")
    with caplog.at_level(logging.WARNING, logger="dse_jg_migrator.migrator"):
        assert m._flush_edge_batch(client, [("knows", "1", "2", {})]) == []
    assert any("Edge batch" in r.message for r in caplog.records)


def test_migrate_edges_gremlin_maps_ids_and_skips_unmapped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, gremlin_batch=5)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["a", "b"], ["10", "20"])
    page = [
        {"~outV": "a", "~inV": "b", "since": "2020"},
        {"~outV": "a", "~inV": "zzz"},  # unmapped target -> skipped
    ]
    source = _client_returning(page, [])
    target = MagicMock()
    target.submit.return_value.all.return_value.result.return_value = ["e1"]
    with patch.object(m, "_make_gremlin_client", return_value=source):
        total = m._migrate_edges_gremlin("knows", conn, target)
    assert total == 1
    bindings = target.submit.call_args.args[1]
    assert [(r["src"], r["dst"]) for r in bindings["rows"]] == [("10", "20")]
    assert "2020" in _row_values(bindings)
    conn.close()


def test_dispatch_uses_cql_for_dsg_with_host_else_gremlin(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    conn = MagicMock()
    m_cql = _make_migrator(tmp_path, source_cql_host="h")
    with (
        patch.object(m_cql, "_migrate_vertices_cql", return_value=1) as v_cql,
        patch.object(m_cql, "_migrate_edges_cql", return_value=2) as e_cql,
    ):
        assert m_cql._migrate_vertices("person", conn, None) == 1
        assert m_cql._migrate_edges(_make_analysis().edge_labels[0], conn, None) == 2
        v_cql.assert_called_once()
        e_cql.assert_called_once()

    m_gr = _make_migrator(tmp_path)
    with (
        patch.object(m_gr, "_migrate_vertices_gremlin", return_value=3) as v_gr,
        patch.object(m_gr, "_migrate_edges_gremlin", return_value=4) as e_gr,
    ):
        assert m_gr._migrate_vertices("person", conn, None) == 3
        assert m_gr._migrate_edges(_make_analysis().edge_labels[0], conn, None) == 4
        v_gr.assert_called_once()
        e_gr.assert_called_once()


def _fake_cql(rows: list[dict]):  # type: ignore[no-untyped-def,type-arg]
    """Build a patched cassandra.cluster.Cluster whose session yields namedtuple-like rows."""
    from collections import namedtuple

    cluster = MagicMock()
    session = cluster.connect.return_value
    if rows:
        row_cls = namedtuple("Row", rows[0].keys())  # type: ignore[misc]  # noqa: PYI024
        session.execute.return_value = [row_cls(**r) for r in rows]
    else:
        session.execute.return_value = []
    return cluster


def test_migrate_vertices_cql_flushes_in_batches_and_maps_ids(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h", write_batch=2)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    rows = [{"person_id": f"u{i}", "name": f"N{i}"} for i in range(3)]
    target = MagicMock()
    target.submit.side_effect = [
        MagicMock(**{"all.return_value.result.return_value": ["n0", "n1"]}),
        MagicMock(**{"all.return_value.result.return_value": ["n2"]}),
    ]
    with patch("cassandra.cluster.Cluster", return_value=_fake_cql(rows)) as cls:
        total = m._migrate_vertices_cql("person", conn, target)
    assert total == 3
    cls.return_value.shutdown.assert_called_once()
    assert m._lookup_id(conn, "person:u0") == "n0"
    assert m._lookup_id(conn, "person:u2") == "n2"
    # Live finding: only full batches were audited, so the last, partial batch never reached the report.
    batches = [(e["batch_start"], e["batch_size"], e["migrated"]) for e in _read_audit(tmp_path)]
    assert batches == [(0, 2, 2), (2, 1, 1)]
    conn.close()


def _fake_cql_with_edge_table(rows: list[dict], table: str, pk: str, ck: str) -> MagicMock:  # type: ignore[type-arg]
    """Cluster mock whose metadata describes an edge table (partition key `pk`, clustering `ck`)."""
    cluster = _fake_cql(rows)
    table_meta = MagicMock()
    table_meta.partition_key = [SimpleNamespace(name=pk)]
    table_meta.clustering_key = [SimpleNamespace(name=ck)]
    cluster.metadata.keyspaces = {"food": MagicMock(tables={table: table_meta})}
    return cluster


def test_migrate_edges_cql_uses_real_dse_table_and_column_names(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """DSE 6.8 DSG: table ``person__knows__person`` with ``out_person_id`` / ``in_person_id`` columns."""
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h", write_batch=10)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["person:u0", "person:u1"], ["n0", "n1"])
    rows = [
        {"out_person_id": "u0", "in_person_id": "u1", "since": "2020"},
        {"out_person_id": "u1", "in_person_id": "u0", "since": "2021"},
    ]
    cluster = _fake_cql_with_edge_table(rows, "person__knows__person", "out_person_id", "in_person_id")
    target = MagicMock()
    target.submit.return_value.all.return_value.result.return_value = ["e0", "e1"]
    el = _make_analysis().edge_labels[0]
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        total = m._migrate_edges_cql(el, conn, target)
    assert total == 2
    bindings = target.submit.call_args.args[1]
    assert (bindings["rows"][0]["src"], bindings["rows"][0]["dst"]) == ("n0", "n1")
    # key columns are not written as edge properties; only `since` is
    assert {p[0] for r in bindings["rows"] for p in r["props"]} == {"since"}
    conn.close()


def test_migrate_edges_cql_skips_unmapped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h")
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    target = MagicMock()
    el = _make_analysis().edge_labels[0]
    cluster = _fake_cql_with_edge_table(
        [{"out_person_id": "u9", "in_person_id": "u8"}], "person__knows__person", "out_person_id", "in_person_id"
    )
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        assert m._migrate_edges_cql(el, conn, target) == 0
    target.submit.assert_not_called()
    conn.close()


def test_row_to_props_converts_dse_types() -> None:
    import datetime as dt

    class Date:  # mimics cassandra.util.Date
        def date(self) -> dt.date:
            return dt.date(2024, 1, 2)

    props = Migrator._row_to_props(
        {
            "solr_query": None,
            "name": "A",
            "skip": None,
            "tags": {"b", "a"},
            "badge": {"k": Date()},
            "since": Date(),
        }
    )
    assert "solr_query" not in props and "skip" not in props
    assert sorted(props["tags"]) == ["a", "b"]
    assert props["badge_key"] == ["k"] and len(props["badge_value"]) == 1
    assert props["since"] == dt.datetime(2024, 1, 2)  # naive midnight: gremlin-python cannot send datetime.date


def test_prop_pairs_expands_lists_into_repeated_properties() -> None:
    assert Migrator._prop_pairs({"name": "A", "tags": ["x", "y"]}) == [("name", "A"), ("tags", "x"), ("tags", "y")]


def test_persist_id_map_uses_analysed_partition_key_not_dict_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    m.analysis.vertex_labels[0].partition_keys = [PartitionKey(name="person_id", type="Uuid")]
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    # Real CQL rows list columns alphabetically: `age` precedes the partition key `person_id`.
    m._persist_id_map(conn, [("person", {"age": 3, "person_id": "u1"})], ["n1"])
    assert m._lookup_id(conn, "person:u1") == "n1"
    conn.close()


def test_persist_id_map_uses_label_and_first_property(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map(conn, [("person", {"person_id": "u1", "name": "A"})], ["n1"])
    assert m._lookup_id(conn, "person:u1") == "n1"
    conn.close()


def test_make_gremlin_client_passes_credentials_only_for_target(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types
    from unittest.mock import patch

    m = _make_migrator(tmp_path, target_username="u", target_password="p")  # noqa: S106
    fake_client_mod = types.ModuleType("gremlin_python.driver.client")
    fake_client_mod.Client = MagicMock()  # type: ignore[attr-defined]
    pkg = types.ModuleType("gremlin_python")
    drv = types.ModuleType("gremlin_python.driver")
    drv.client = fake_client_mod  # type: ignore[attr-defined]
    fake_serializer = types.ModuleType("gremlin_python.driver.serializer")
    fake_serializer.GraphSONSerializersV3d0 = MagicMock(return_value="graphson3")  # type: ignore[attr-defined]
    drv.serializer = fake_serializer  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {
            "gremlin_python": pkg,
            "gremlin_python.driver": drv,
            "gremlin_python.driver.client": fake_client_mod,
            "gremlin_python.driver.serializer": fake_serializer,
        },
    ):
        m._make_gremlin_client(m.target_gremlin_url)
        fake_client_mod.Client.assert_called_with(
            m.target_gremlin_url,
            "g",
            transport_factory=ANY,
            username="u",
            password="p",  # noqa: S106 — test value
        )
        m._make_gremlin_client(m.source_gremlin_url)
        # DSE requires the "<graph>.g" traversal source (found against a live DSE 6.8).
        # GraphSON v3 for the source: DSE's GraphBinary uses types gremlin-python cannot decode (found live).
        fake_client_mod.Client.assert_called_with(
            m.source_gremlin_url, "food.g", transport_factory=ANY, message_serializer="graphson3"
        )


def test_row_to_props_normalises_python_dates_and_aware_datetimes() -> None:
    """Live finding: JanusGraph's java.util.Date rejects tz-aware (OffsetDateTime) values."""
    import datetime as dt

    props = Migrator._row_to_props(
        {
            "plain_date": dt.date(2024, 1, 2),
            "aware": dt.datetime(2024, 1, 2, 5, 0, tzinfo=dt.timezone(dt.timedelta(hours=5))),
            "naive": dt.datetime(2024, 1, 2, 3, 4),
        }
    )
    assert props["plain_date"] == dt.datetime(2024, 1, 2)
    assert props["aware"] == dt.datetime(2024, 1, 2, 0, 0)  # converted to UTC, tz stripped
    assert props["naive"] == dt.datetime(2024, 1, 2, 3, 4)


def test_batches_call_next_before_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: a list of un-iterated traversals cannot be serialised by the server."""
    m = _make_migrator(tmp_path)
    client = MagicMock()
    client.submit.return_value.all.return_value.result.return_value = [1]
    m._flush_vertex_batch(client, [("person", {"name": "a"})])
    m._flush_edge_batch(client, [("knows", "1", "2", {})])
    for call in client.submit.call_args_list:
        assert ".next().id()" in call.args[0]
        assert ").id()" not in call.args[0].replace(".next().id()", "")
        assert "from(g.V(r.src))" not in call.args[0]  # an un-iterated inline traversal is rejected by the server


def test_batch_uses_at_most_two_bindings_regardless_of_size(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: the server rejects >16 bindings per request, so batches must not scale bindings with rows."""
    m = _make_migrator(tmp_path)
    client = MagicMock()
    client.submit.return_value.all.return_value.result.return_value = list(range(100))
    m._flush_vertex_batch(client, [("person", {"a": i, "b": i, "c": i}) for i in range(100)])
    assert len(client.submit.call_args.args[1]) <= 2


# ---------------------------------------------------------------------------
# Type conversions for JanusGraph-unsupported / schema-aware types
# ---------------------------------------------------------------------------


def _typed_analysis() -> AnalysisResult:
    from dse_jg_migrator.models import PropertyDefinition

    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="place")
    vl.properties = [
        PropertyDefinition(name="geo", type="Point"),
        PropertyDefinition(name="big", type="Varint"),
        PropertyDefinition(name="n", type="Int"),
    ]
    an.vertex_labels.append(vl)
    return an


def test_target_classes_follow_translator_resolution() -> None:
    classes = Migrator._target_classes(_typed_analysis())
    assert classes == {
        "geo": "org.janusgraph.core.attribute.Geoshape",
        "big": "java.lang.String",
        "n": "java.lang.Integer",
    }


def test_point_values_are_rebuilt_as_geoshape_from_wkt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path, analysis=_typed_analysis())
    client = MagicMock()
    client.submit.return_value.all.return_value.result.return_value = [1]
    m._flush_vertex_batch(client, [("place", {"geo": "POINT (30.0 10.0)", "n": 1})])
    script, bindings = client.submit.call_args.args
    assert "Geoshape.fromWkt(p[1])" in script  # the WKT is a binding, never interpolated
    assert "POINT" not in script
    assert bindings["geo"] == ["geo"]  # only Point keys are rebuilt as Geoshape
    assert ["geo", "POINT (30.0 10.0)"] in bindings["rows"][0]["props"]
    assert ["n", 1] in bindings["rows"][0]["props"]


def test_varint_ints_become_decimal_strings(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path, analysis=_typed_analysis())
    client = MagicMock()
    client.submit.return_value.all.return_value.result.return_value = [1]
    m._flush_vertex_batch(client, [("place", {"big": 2**80, "n": 5})])
    bindings = client.submit.call_args.args[1]
    props = bindings["rows"][0]["props"]
    assert ["big", str(2**80)] in props
    assert ["n", 5] in props  # a plain Int is left alone


def test_row_to_props_converts_geometry_decimal_inet_time() -> None:
    import datetime as dt
    import ipaddress
    from decimal import Decimal

    from cassandra.util import LineString, Point, Polygon

    props = Migrator._row_to_props(
        {
            "pt": Point(30.0, 10.0),
            "ls": LineString([(0, 0), (1, 1)]),
            "pg": Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]),
            "price": Decimal("12.50"),
            "ip": ipaddress.ip_address("10.0.0.1"),
            "t": dt.time(1, 2, 3),
        }
    )
    assert props["pt"] == "POINT (30.0 10.0)"
    assert props["ls"] == "LINESTRING (0 0, 1 1)"
    assert props["pg"].startswith("POLYGON")
    assert props["price"] == Decimal("12.50")  # converted per target key class at flush time (_target_value)
    assert props["ip"] == "10.0.0.1"
    assert props["t"] == "01:02:03"


# ---------------------------------------------------------------------------
# Retry, idempotent resume and failure reporting (Sub-Task 5, steps 6 and 10)
# ---------------------------------------------------------------------------


def _ok(ids: list) -> MagicMock:  # type: ignore[type-arg]
    return MagicMock(**{"all.return_value.result.return_value": ids})


def test_failed_batch_is_retried_with_backoff_then_succeeds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    delays: list[float] = []
    m._sleep = delays.append
    client = MagicMock()
    client.submit.side_effect = [RuntimeError("timeout"), RuntimeError("timeout"), _ok(["n1"])]
    assert m._flush_vertex_batch(client, [("person", {"name": "A"})]) == ["n1"]
    assert delays == [1.0, 2.0]
    assert not m._failed and not m._errors


def test_batch_failing_after_three_retries_is_recorded_and_audited(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = _make_migrator(tmp_path)
    delays: list[float] = []
    m._sleep = delays.append
    client = MagicMock()
    client.submit.side_effect = RuntimeError("server down")
    assert m._flush_edge_batch(client, [("knows", "1", "2", {}), ("knows", "2", "1", {})]) == []
    assert client.submit.call_count == 4  # first attempt + 3 retries
    assert delays == [1.0, 2.0, 4.0]
    assert m._failed == {("edge", "knows"): 2}
    assert "ws://localhost:8182/gremlin" in m._errors[0] and "server down" in m._errors[0]
    audit = _read_audit(tmp_path)
    assert [e["kind"] for e in audit] == ["BATCH_FAILED", "edge"]
    assert (audit[1]["batch_size"], audit[1]["migrated"], audit[1]["failed"]) == (2, 0, 2)


def test_run_keeps_failed_vertex_label_incomplete_and_defers_edges(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path)

    def failing_vertices(label, conn, client):  # type: ignore[no-untyped-def]
        m._failed[("vertex", label)] = 1
        return 4

    with (
        patch.object(m, "_make_gremlin_client", return_value=MagicMock()),
        patch.object(m, "_migrate_vertices", side_effect=failing_vertices),
        patch.object(m, "_migrate_edges", return_value=3) as me,
    ):
        stats = m.run()
    me.assert_not_called()  # endpoints may be missing from id_map, so edges wait
    assert stats["vertices"] == {"person": 4}
    assert any("Edges not migrated" in e and "person" in e for e in stats["errors"])

    # The re-run retries the incomplete label and, once it succeeds, migrates edges.
    with (
        patch.object(m, "_make_gremlin_client", return_value=MagicMock()),
        patch.object(m, "_migrate_vertices", return_value=1) as mv,
        patch.object(m, "_migrate_edges", return_value=3),
    ):
        stats = m.run()
    mv.assert_called_once()
    assert stats["edges"] == {"knows": 3}
    assert stats["errors"] == []


def test_cql_vertex_rerun_skips_rows_already_in_id_map(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h", write_batch=10)
    m.analysis.vertex_labels[0].partition_keys = [PartitionKey(name="person_id", type="Uuid")]
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["person:u0"], ["n0"])  # written by an earlier, interrupted run
    rows = [{"person_id": f"u{i}", "name": f"N{i}"} for i in range(3)]
    target = MagicMock()
    target.submit.return_value = _ok(["n1", "n2"])
    with patch("cassandra.cluster.Cluster", return_value=_fake_cql(rows)):
        assert m._migrate_vertices_cql("person", conn, target) == 2
    written = [r["props"] for r in target.submit.call_args.args[1]["rows"]]
    assert ["person_id", "u0"] not in [p for props in written for p in props]
    assert m._skipped == 1
    assert m._lookup_id(conn, "person:u2") == "n2"
    conn.close()


def test_cql_edges_are_recorded_in_edge_map_and_skipped_on_rerun(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h", write_batch=10)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["person:u0", "person:u1"], ["n0", "n1"])
    rows = [{"out_person_id": "u0", "in_person_id": "u1", "since": "2020"}]
    el = _make_analysis().edge_labels[0]
    target = MagicMock()
    target.submit.return_value = _ok(["e0"])
    for _ in range(2):
        cluster = _fake_cql_with_edge_table(rows, "person__knows__person", "out_person_id", "in_person_id")
        with patch("cassandra.cluster.Cluster", return_value=cluster):
            m._migrate_edges_cql(el, conn, target)
    target.submit.assert_called_once()  # the second pass found the edge in edge_map
    assert m._lookup_edge(conn, 'person__knows__person:["u0", "u1"]') == "e0"
    conn.close()


def test_dangling_edges_are_counted_and_audited(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h")
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    cluster = _fake_cql_with_edge_table(
        [{"out_person_id": "u9", "in_person_id": "u8"}], "person__knows__person", "out_person_id", "in_person_id"
    )
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        m._migrate_edges_cql(_make_analysis().edge_labels[0], conn, MagicMock())
    assert m._missing_endpoints == {"knows": 1}
    assert [e["kind"] for e in _read_audit(tmp_path)] == ["MISSING_ENDPOINTS"]
    conn.close()


# ---------------------------------------------------------------------------
# Classic (Gremlin-paged) path: gremlin-python returns T / Direction tokens as elementMap() keys
# ---------------------------------------------------------------------------


def test_split_element_map_reads_gremlin_python_tokens() -> None:
    from gremlin_python.process.traversal import Direction, T

    edge = {
        T.id: "e-1",
        T.label: "knows",
        Direction.OUT: {T.id: "v-1", T.label: "person"},
        Direction.IN: {T.id: "v-2", T.label: "person"},
        "since": 2020,
    }
    assert Migrator._split_element_map(edge) == ("e-1", {"since": 2020}, "v-1", "v-2")
    vertex = {T.id: 7, T.label: "person", "name": "A"}
    assert Migrator._split_element_map(vertex) == ("7", {"name": "A"}, "", "")


def test_gremlin_edges_with_token_keys_are_migrated_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    from gremlin_python.process.traversal import Direction, T

    m = _make_migrator(tmp_path, gremlin_batch=5)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    m._persist_id_map_raw(conn, ["v-1", "v-2"], ["10", "20"])
    page = [{T.id: "e-1", T.label: "knows", Direction.OUT: {T.id: "v-1"}, Direction.IN: {T.id: "v-2"}}]
    target = MagicMock()
    target.submit.return_value = _ok(["jg-e"])
    for _ in range(2):
        with patch.object(m, "_make_gremlin_client", return_value=_client_returning(page, [])):
            m._migrate_edges_gremlin("knows", conn, target)
    target.submit.assert_called_once()
    assert [(r["src"], r["dst"]) for r in target.submit.call_args.args[1]["rows"]] == [("10", "20")]
    assert m._lookup_edge(conn, "e-1") == "jg-e"
    conn.close()


# ---------------------------------------------------------------------------
# Target-type value conversion: Decimal strategy, Duration per engine (Sub-Task 5, step 11)
# ---------------------------------------------------------------------------


def _analysis_with(engine: str, **props: str) -> AnalysisResult:
    from dse_jg_migrator.models import PropertyDefinition

    an = AnalysisResult(graph_name="t", engine=engine, extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="x")
    vl.properties = [PropertyDefinition(name=k, type=t) for k, t in props.items()]
    an.vertex_labels.append(vl)
    return an


def test_decimal_double_counts_only_inexact_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from decimal import Decimal

    m = _make_migrator(tmp_path, analysis=_analysis_with("dsg", price="Decimal"))
    assert m._target_value("price", Decimal("19.99")) == 19.99
    assert m._target_value("price", Decimal("2.50")) == 2.5  # same value, scale not kept
    assert m._lossy_decimals == {}
    assert m._target_value("price", Decimal("1234567890.123456789")) == 1234567890.1234567
    assert m._lossy_decimals == {"price": 1}


def test_decimal_string_strategy_keeps_exact_value(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from decimal import Decimal

    m = _make_migrator(tmp_path, analysis=_analysis_with("dsg", price="Decimal"), decimal_strategy="string")
    assert m._target_value("price", Decimal("1234567890.123456789")) == "1234567890.123456789"
    assert m._lossy_decimals == {}


def test_lossy_decimals_are_reported_by_run(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path)

    def vertices(label, conn, client):  # type: ignore[no-untyped-def]
        m._lossy_decimals["price"] = 2
        return 1

    with (
        patch.object(m, "_make_gremlin_client", return_value=MagicMock()),
        patch.object(m, "_migrate_vertices", side_effect=vertices),
        patch.object(m, "_migrate_edges", return_value=0),
    ):
        stats = m.run()
    assert stats["lossy_decimals"] == {"price": 2}
    assert any(e["kind"] == "LOSSY_DECIMAL" and e["label"] == "price" for e in _read_audit(tmp_path))


def test_core_cql_duration_becomes_iso8601_string(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from cassandra.util import Duration

    m = _make_migrator(tmp_path, analysis=_analysis_with("dsg", ttl="Duration"))
    assert m._target_value("ttl", Duration(1, 2, 3_000_000_004)) == "P1M2DT3.000000004S"
    assert m._target_value("ttl", Duration(0, 0, 36_000 * 10**9)) == "PT10H"
    assert m._target_value("ttl", Duration(0, 0, 90 * 10**9 + 5 * 10**8)) == "PT1M30.5S"
    assert m._target_value("ttl", Duration(0, -3, 0)) == "-P3D"
    assert m._target_value("ttl", Duration(0, 0, 0)) == "PT0S"


def test_classic_duration_timedelta_becomes_millis(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import datetime as dt

    m = _make_migrator(tmp_path, analysis=_analysis_with("classic", ttl="Duration"))
    assert m._target_value("ttl", dt.timedelta(hours=10, milliseconds=5)) == 36_000_005


def test_checkpoint_defaults_to_per_graph_file_in_output_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A CWD-relative default made a second run (or another graph) look already complete."""
    import os

    m = Migrator(
        analysis=_make_analysis(),
        source_gremlin_url="ws://s:8183/gremlin",
        target_gremlin_url="ws://t:8182/gremlin",
        output_dir=str(tmp_path),
    )
    assert m.checkpoint_db == os.path.join(str(tmp_path), "food-migration-checkpoint.db")


def test_long_key_ints_are_sent_as_int64(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: gremlin-python packs a plain int as int32, so a Bigint value near Long.MAX failed client-side."""
    import datetime as dt
    import struct

    import pytest
    from gremlin_python.statics import long
    from gremlin_python.structure.io.graphbinaryV1 import GraphBinaryWriter

    big = 2**62
    with pytest.raises(struct.error):
        GraphBinaryWriter().write_object(big)  # the original failure

    m = _make_migrator(tmp_path, analysis=_analysis_with("classic", raw="Bigint", n="Int", ttl="Duration"))
    converted = m._target_value("raw", big)
    assert type(converted) is long and converted == big
    GraphBinaryWriter().write_object(converted)  # now serialises
    assert type(m._target_value("raw", 5)) is long  # Long key: always int64, even when small
    assert type(m._target_value("n", 5)) is int  # Int key: untouched
    assert type(m._target_value("unknown", 2**40)) is long  # safety net outside int32
    assert type(m._target_value("ttl", dt.timedelta(seconds=1))) is long


def test_row_to_props_flattens_cassandra_ordered_maps() -> None:
    """Live finding: CQL map columns arrive as OrderedMapSerializedKey (a Mapping, not a dict). Unflattened, its
    bytes were undecodable by the server, which then never answered the request."""
    from cassandra.cqltypes import UTF8Type
    from cassandra.util import Date, OrderedMapSerializedKey

    badge = OrderedMapSerializedKey(UTF8Type, 3)
    badge._insert_unchecked("gold", b"gold", Date(20766))
    props = Migrator._row_to_props({"badge": badge})
    assert props == {"badge_key": ["gold"], "badge_value": [str(Date(20766))]}


def test_each_attempt_has_a_timeout_and_timeouts_are_retried(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from concurrent.futures import TimeoutError as FutureTimeout

    from dse_jg_migrator.migrator import _REQUEST_TIMEOUT_S

    m = _make_migrator(tmp_path)
    hung = MagicMock()
    hung.all.return_value.result.side_effect = FutureTimeout()
    client = MagicMock()
    client.submit.side_effect = [hung, _ok(["n1"])]
    assert m._flush_vertex_batch(client, [("person", {"name": "A"})]) == ["n1"]
    hung.all.return_value.result.assert_called_once_with(timeout=_REQUEST_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Structured values follow the translate strategies (Sub-Task 5, step 12)
# ---------------------------------------------------------------------------


def _structured_analysis() -> AnalysisResult:
    from dse_jg_migrator.models import PropertyDefinition, UDTDefinition, UDTField

    an = AnalysisResult(graph_name="enterprise", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="employee", partition_keys=[PartitionKey(name="employee_id", type="Uuid")])
    vl.properties = [
        PropertyDefinition(name="home_address", type="frozen(typeOf('address'))"),
        PropertyDefinition(name="salary_range", type="tupleOf(Int, Int)"),
    ]
    an.vertex_labels.append(vl)
    an.udts.append(
        UDTDefinition(name="address", fields=[UDTField(name="street", type="Text"), UDTField("zip", "Text")])
    )
    return an


def _address(street: str, zip_code: str) -> object:
    from collections import namedtuple

    return namedtuple("address", ["street", "zip"])(street, zip_code)  # what cassandra-driver returns for a UDT


def test_udt_flat_and_tuple_json_instead_of_repeated_single_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: a UDT/tuple was list-expanded into one SINGLE key, keeping only its last field."""
    m = _make_migrator(tmp_path, analysis=_structured_analysis())
    props = m._row_to_props(
        m._expand_structured({"home_address": _address("1 Main", "12345"), "salary_range": (10, 20)})
    )
    assert props == {"address_street": "1 Main", "address_zip": "12345", "salary_range": "[10, 20]"}
    assert m._key_classes["address_zip"] == "java.lang.String"


def test_udt_json_strategy_writes_one_json_string(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    m = _make_migrator(tmp_path, analysis=_structured_analysis(), udt_strategy="json")
    props = m._expand_structured({"home_address": _address("1 Main", "12345")})
    assert json.loads(props["address"]) == {"street": "1 Main", "zip": "12345"}


def test_map_json_strategy_writes_one_json_object() -> None:
    import json

    props = Migrator._row_to_props({"badge": {"gold": "2026-01-01"}}, map_strategy="json")
    assert json.loads(props["badge"]) == {"gold": "2026-01-01"}


def test_cql_vertex_path_applies_structured_strategies(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, analysis=_structured_analysis(), source_cql_host="h", write_batch=10)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    rows = [{"employee_id": "e1", "home_address": _address("1 Main", "12345"), "salary_range": (10, 20)}]
    target = MagicMock()
    target.submit.return_value = _ok(["n1"])
    with patch("cassandra.cluster.Cluster", return_value=_fake_cql(rows)):
        assert m._migrate_vertices_cql("employee", conn, target) == 1
    written = dict(target.submit.call_args.args[1]["rows"][0]["props"])
    assert written == {
        "employee_id": "e1",
        "address_street": "1 Main",
        "address_zip": "12345",
        "salary_range": "[10, 20]",
    }
    conn.close()


# ---------------------------------------------------------------------------
# Classic read path over GraphSON (found live on DSE 6.9 Classic)
# ---------------------------------------------------------------------------


def test_from_graphson_decodes_raw_typed_values() -> None:
    import datetime as dt

    from dse_jg_migrator.migrator import _from_graphson

    assert _from_graphson({"@type": "gx:Instant", "@value": "2026-11-09T12:34:45.659Z"}) == dt.datetime(
        2026, 11, 9, 12, 34, 45, 659000
    )
    assert _from_graphson({"@type": "gx:LocalDate", "@value": "2026-11-09"}) == dt.datetime(2026, 11, 9)
    assert _from_graphson({"@type": "dse:Point", "@value": "POINT (1 2)"}) == "POINT (1 2)"
    assert _from_graphson([{"@type": "gx:LocalTime", "@value": "01:02:03"}, "x"]) == ["01:02:03", "x"]
    assert _from_graphson({"k": 1}) == {"k": 1}  # an ordinary map is left alone


def test_classic_vertices_keep_every_value_of_multi_valued_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """elementMap() kept one value of a multi-valued Classic property; valueMap(true) keeps all (found live)."""
    from unittest.mock import patch

    from gremlin_python.process.traversal import T

    from dse_jg_migrator.models import PropertyDefinition

    analysis = _make_analysis()
    analysis.engine = "classic"
    analysis.vertex_labels[0].properties = [
        PropertyDefinition(name="name", type="Text"),
        PropertyDefinition(name="nick", type="Text", cardinality="LIST", collection="list"),
    ]
    m = _make_migrator(tmp_path, analysis=analysis, gremlin_batch=5)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    page = [{T.id: {"~label": "person", "member_id": 0}, T.label: "person", "name": ["p0"], "nick": ["n0", "m0"]}]
    source = _client_returning(page, [], [])  # valueMap page, properties() page (meta), empty page => stop
    target = MagicMock()
    target.submit.return_value = _ok(["jg-1"])
    with patch.object(m, "_make_gremlin_client", return_value=source):
        assert m._migrate_vertices_gremlin("person", conn, target) == 1
    assert "valueMap(true)" in source.submit.call_args_list[0].args[0]
    written = target.submit.call_args.args[1]["rows"][0]["props"]
    assert ["name", "p0"] in written and ["nick", "n0"] in written and ["nick", "m0"] in written
    assert m._lookup_id(conn, str({"~label": "person", "member_id": 0})) == "jg-1"
    conn.close()


# ---------------------------------------------------------------------------
# Source read failures are recorded, never raised (AGENTS.md §4.6; a raw traceback was seen live)
# ---------------------------------------------------------------------------


def test_unreadable_gremlin_page_is_retried_then_leaves_label_incomplete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, gremlin_batch=5)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    source = MagicMock()
    source.submit.side_effect = ValueError("131 is not a valid DataType")
    with patch.object(m, "_make_gremlin_client", return_value=source):
        assert m._migrate_vertices_gremlin("person", conn, MagicMock()) == 0
    assert source.submit.call_count == 4  # first attempt + 3 retries
    assert m._failed == {("vertex", "person"): 5}
    assert "page read" in m._errors[0] and "ws://localhost:8183/gremlin" in m._errors[0]
    assert [e["kind"] for e in _read_audit(tmp_path)] == ["SOURCE_READ_FAILED"]
    conn.close()


def test_cql_connection_failure_is_recorded_with_host_and_port(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="dse-host", source_cql_port=9043)
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    cluster = MagicMock()
    cluster.connect.side_effect = RuntimeError("NoHostAvailable")
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        assert m._migrate_vertices_cql("person", conn, MagicMock()) == 0
    assert "dse-host:9043" in m._errors[0] and "food.person" in m._errors[0]
    assert m._failed == {("vertex", "person"): 1}
    cluster.shutdown.assert_called_once()
    conn.close()


def test_missing_cql_edge_table_is_recorded_not_raised(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    m = _make_migrator(tmp_path, source_cql_host="h")
    conn = m._init_checkpoint(str(tmp_path / "ckpt.db"))
    cluster = _fake_cql([])
    cluster.metadata.keyspaces = {"food": MagicMock(tables={})}  # table person__knows__person does not exist
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        assert m._migrate_edges_cql(_make_analysis().edge_labels[0], conn, MagicMock()) == 0
    assert "food.person__knows__person" in m._errors[0] and "KeyError" in m._errors[0]
    assert m._failed == {("edge", "knows"): 1}
    conn.close()
