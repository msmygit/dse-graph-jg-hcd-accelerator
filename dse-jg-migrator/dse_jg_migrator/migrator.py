"""Data migrator — moves vertices and edges from DSE Graph to JanusGraph / HCD.

Supports two strategies:
  - CQL-direct (DSG): reads via cassandra-driver token-range paging (fast).
  - Gremlin-paged (Classic): reads via Gremlin range() queries (fallback).

Uses SQLite for checkpoint/resume and writes an audit JSONL log.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from dse_jg_migrator.models import AnalysisResult


class Migrator:
    """Migrates vertex and edge data from DSE Graph to JanusGraph.

    Reads from: source DSE CQL endpoint (DSG) or source DSE Gremlin server (Classic).
    Writes to: target JanusGraph Gremlin server, local SQLite checkpoint DB,
               local JSONL audit log.
    """

    def __init__(
        self,
        analysis: AnalysisResult,
        source_gremlin_url: str,
        target_gremlin_url: str,
        source_cql_host: str = "",
        source_cql_port: int = 9043,
        source_cql_username: str | None = None,
        source_cql_password: str | None = None,
        target_username: str | None = None,
        target_password: str | None = None,
        cql_fetch_size: int = 1000,
        gremlin_batch: int = 200,
        write_batch: int = 100,
        parallel_labels: int = 1,
        sqlite_wal: bool = True,
        checkpoint_db: str = "migration-checkpoint.db",
        audit_log: str = "",
        output_dir: str = ".",
    ) -> None:
        self.analysis = analysis
        self.source_gremlin_url = source_gremlin_url
        self.target_gremlin_url = target_gremlin_url
        self.source_cql_host = source_cql_host
        self.source_cql_port = source_cql_port
        self.source_cql_username = source_cql_username
        self.source_cql_password = source_cql_password
        self.target_username = target_username
        self.target_password = target_password
        self.cql_fetch_size = cql_fetch_size
        self.gremlin_batch = gremlin_batch
        self.write_batch = write_batch
        self.parallel_labels = parallel_labels
        self.sqlite_wal = sqlite_wal
        self.checkpoint_db = checkpoint_db
        self.audit_log = audit_log or f"{analysis.graph_name}-migration-audit.jsonl"
        self.output_dir = output_dir

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """Execute the full migration. Returns a summary dict."""
        conn = self._init_checkpoint(self.checkpoint_db)
        target_client = self._make_gremlin_client(self.target_gremlin_url)
        stats: dict[str, Any] = {"vertices": {}, "edges": {}, "errors": []}

        try:
            # Phase 1: migrate all vertices
            for vl in self.analysis.vertex_labels:
                label = vl.label
                if self._is_completed(conn, label, "vertex"):
                    continue
                count = self._migrate_vertices(label, conn, target_client)
                stats["vertices"][label] = count
                self._mark_completed(conn, label, "vertex")

            # Phase 2: migrate all edges (after all vertices)
            for el in self.analysis.edge_labels:
                label = el.label
                if self._is_completed(conn, label, "edge"):
                    continue
                count = self._migrate_edges(el, conn, target_client)
                stats["edges"][label] = count
                self._mark_completed(conn, label, "edge")

        finally:
            target_client.close()
            conn.close()

        return stats

    # ──────────────────────────────────────────────────────────────────────────
    # Vertex migration
    # ──────────────────────────────────────────────────────────────────────────

    def _migrate_vertices(
        self,
        label: str,
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """Migrate all vertices for a label. Returns count migrated."""
        if self.analysis.engine == "dsg" and self.source_cql_host:
            return self._migrate_vertices_cql(label, conn, target_client)
        return self._migrate_vertices_gremlin(label, conn, target_client)

    def _migrate_vertices_cql(
        self,
        label: str,
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """CQL-direct vertex migration for DSG graphs."""
        from cassandra.cluster import Cluster
        from cassandra.query import SimpleStatement

        cluster = Cluster([self.source_cql_host], port=self.source_cql_port)
        session = cluster.connect()
        keyspace = self.analysis.graph_name

        stmt = SimpleStatement(
            f"SELECT * FROM {keyspace}.{label}",  # noqa: S608
            fetch_size=self.cql_fetch_size,
        )

        write_buf: list[tuple[str, dict[str, Any]]] = []
        total = 0

        try:
            for row in session.execute(stmt):
                props = {k: v for k, v in row._asdict().items() if v is not None}
                write_buf.append((label, props))
                if len(write_buf) >= self.write_batch:
                    new_ids = self._flush_vertex_batch(target_client, write_buf)
                    self._persist_id_map(conn, write_buf, new_ids)
                    self._append_audit(
                        "vertex", label, total, len(write_buf), len(new_ids),
                        len(write_buf) - len(new_ids)
                    )
                    total += len(new_ids)
                    write_buf.clear()
            if write_buf:
                new_ids = self._flush_vertex_batch(target_client, write_buf)
                self._persist_id_map(conn, write_buf, new_ids)
                total += len(new_ids)
        finally:
            cluster.shutdown()

        return total

    def _migrate_vertices_gremlin(
        self,
        label: str,
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """Gremlin-paged vertex migration for Classic graphs."""
        source_client = self._make_gremlin_client(self.source_gremlin_url)
        offset = 0
        total = 0

        try:
            while True:
                results = source_client.submit(
                    "g.with('allow-filtering').V().hasLabel(lbl)"
                    ".range(off, end).elementMap()",
                    {"lbl": label, "off": offset, "end": offset + self.gremlin_batch},
                ).all().result()

                if not results:
                    break

                write_buf: list[tuple[str, dict[str, Any]]] = []
                for vm in results:
                    vm_dict = dict(vm)
                    old_id = str(vm_dict.pop("~id", vm_dict.pop("id", f"{label}_{offset}")))
                    vm_dict.pop("~label", None)
                    vm_dict.pop("label", None)
                    write_buf.append((old_id, vm_dict))

                for chunk_start in range(0, len(write_buf), self.write_batch):
                    chunk = write_buf[chunk_start : chunk_start + self.write_batch]
                    labeled_chunk = [(label, props) for _, props in chunk]
                    new_ids = self._flush_vertex_batch(target_client, labeled_chunk)
                    old_ids = [old_id for old_id, _ in chunk]
                    self._persist_id_map_raw(conn, old_ids, new_ids)
                    total += len(new_ids)

                offset += self.gremlin_batch
        finally:
            source_client.close()

        return total

    def _flush_vertex_batch(
        self,
        client: Any,  # noqa: ANN401
        buf: list[tuple[str, dict[str, Any]]],
    ) -> list[str]:
        """Submit a batch of addV() calls via Gremlin bindings. Returns new JG IDs."""
        script_parts: list[str] = []
        bindings: dict[str, Any] = {}

        for i, (label, props) in enumerate(buf):
            prop_chain = "".join(
                f".property(k_{i}_{j}, v_{i}_{j})" for j in range(len(props))
            )
            for j, (k, v) in enumerate(props.items()):
                bindings[f"k_{i}_{j}"] = k
                bindings[f"v_{i}_{j}"] = v
            bindings[f"lbl_{i}"] = label
            script_parts.append(f"g.addV(lbl_{i}){prop_chain}.id()")

        if not script_parts:
            return []

        script = "[" + ",".join(script_parts) + "]"
        try:
            result = client.submit(script, bindings).all().result()
            return [str(r) for r in (result or [])]
        except Exception:  # noqa: BLE001, S110
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # Edge migration
    # ──────────────────────────────────────────────────────────────────────────

    def _migrate_edges(
        self,
        el: Any,  # noqa: ANN401
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """Migrate all edges for an edge label. Vertices must be fully committed first."""
        if self.analysis.engine == "dsg" and self.source_cql_host:
            return self._migrate_edges_cql(el, conn, target_client)
        return self._migrate_edges_gremlin(el.label, conn, target_client)

    def _migrate_edges_cql(
        self,
        el: Any,  # noqa: ANN401
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """CQL-direct edge migration for DSG graphs."""
        from cassandra.cluster import Cluster
        from cassandra.query import SimpleStatement

        table_name = el.table_name or f"{el.from_label}_{el.label}_{el.to_label}"
        keyspace = self.analysis.graph_name

        cluster = Cluster([self.source_cql_host], port=self.source_cql_port)
        session = cluster.connect()

        stmt = SimpleStatement(
            f"SELECT * FROM {keyspace}.{table_name}",  # noqa: S608
            fetch_size=self.cql_fetch_size,
        )

        write_buf: list[tuple[str, str, str, dict[str, Any]]] = []
        total = 0
        missing = 0

        try:
            for row in session.execute(stmt):
                row_dict = row._asdict()
                out_old = f"{el.from_label}:{row_dict.get(f'{el.from_label}_id', '')}"
                in_old = f"{el.to_label}:{row_dict.get(f'{el.to_label}_id', '')}"
                out_new = self._lookup_id(conn, out_old)
                in_new = self._lookup_id(conn, in_old)
                if out_new is None or in_new is None:
                    missing += 1
                    continue
                edge_props = {
                    k: v for k, v in row_dict.items() if v is not None
                }
                write_buf.append((el.label, out_new, in_new, edge_props))
                if len(write_buf) >= self.write_batch:
                    flushed = self._flush_edge_batch(target_client, write_buf)
                    total += flushed
                    write_buf.clear()
            if write_buf:
                total += self._flush_edge_batch(target_client, write_buf)
        finally:
            cluster.shutdown()

        return total

    def _migrate_edges_gremlin(
        self,
        edge_label: str,
        conn: sqlite3.Connection,
        target_client: Any,  # noqa: ANN401
    ) -> int:
        """Gremlin-paged edge migration fallback."""
        source_client = self._make_gremlin_client(self.source_gremlin_url)
        offset = 0
        total = 0

        try:
            while True:
                results = source_client.submit(
                    "g.with('allow-filtering').E().hasLabel(lbl)"
                    ".range(off, end).elementMap()",
                    {"lbl": edge_label, "off": offset, "end": offset + self.gremlin_batch},
                ).all().result()

                if not results:
                    break

                write_buf: list[tuple[str, str, str, dict[str, Any]]] = []
                for em in results:
                    em_dict = dict(em)
                    out_id = str(em_dict.get("~outV") or em_dict.get("outV", ""))
                    in_id = str(em_dict.get("~inV") or em_dict.get("inV", ""))
                    out_new = self._lookup_id(conn, out_id)
                    in_new = self._lookup_id(conn, in_id)
                    if out_new and in_new:
                        props = {
                            k: v for k, v in em_dict.items()
                            if not k.startswith("~") and k not in ("id", "label", "outV", "inV")
                        }
                        write_buf.append((edge_label, out_new, in_new, props))

                if write_buf:
                    total += self._flush_edge_batch(target_client, write_buf)
                offset += self.gremlin_batch
        finally:
            source_client.close()

        return total

    def _flush_edge_batch(
        self,
        client: Any,  # noqa: ANN401
        buf: list[tuple[str, str, str, dict[str, Any]]],
    ) -> int:
        """Submit a batch of addE() calls. Returns count successfully added."""
        script_parts: list[str] = []
        bindings: dict[str, Any] = {}

        for i, (label, out_id, in_id, props) in enumerate(buf):
            prop_chain = "".join(
                f".property(ek_{i}_{j}, ev_{i}_{j})" for j in range(len(props))
            )
            for j, (k, v) in enumerate(props.items()):
                bindings[f"ek_{i}_{j}"] = k
                bindings[f"ev_{i}_{j}"] = v
            bindings[f"elbl_{i}"] = label
            bindings[f"eout_{i}"] = out_id
            bindings[f"ein_{i}"] = in_id
            script_parts.append(
                f"g.addE(elbl_{i}).from(g.V(eout_{i})).to(g.V(ein_{i})){prop_chain}.id()"
            )

        if not script_parts:
            return 0

        script = "[" + ",".join(script_parts) + "]"
        try:
            result = client.submit(script, bindings).all().result()
            return len(result or [])
        except Exception:  # noqa: BLE001, S110
            return 0

    # ──────────────────────────────────────────────────────────────────────────
    # Checkpoint / ID map helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _init_checkpoint(self, db_path: str) -> sqlite3.Connection:
        """Initialise or reopen the SQLite checkpoint database."""
        conn = sqlite3.connect(db_path)
        if self.sqlite_wal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA page_size=8192")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-65536")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS progress ("
            "label TEXT, kind TEXT, last_offset INTEGER, completed INTEGER DEFAULT 0,"
            "PRIMARY KEY (label, kind))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS id_map ("
            "old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_id_map ON id_map(old_id)")
        conn.commit()
        return conn

    def _is_completed(self, conn: sqlite3.Connection, label: str, kind: str) -> bool:
        row = conn.execute(
            "SELECT completed FROM progress WHERE label=? AND kind=?", (label, kind)
        ).fetchone()
        return bool(row and row[0])

    def _mark_completed(self, conn: sqlite3.Connection, label: str, kind: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO progress (label, kind, completed) VALUES (?,?,1)",
            (label, kind),
        )
        conn.commit()

    def _persist_id_map(
        self,
        conn: sqlite3.Connection,
        buf: list[tuple[str, dict[str, Any]]],
        new_ids: list[str],
    ) -> None:
        for (label, props), new_id in zip(buf, new_ids):
            pk_key = next(iter(props), "")
            pk_val = str(props.get(pk_key, ""))
            old_id = f"{label}:{pk_val}"
            conn.execute(
                "INSERT OR IGNORE INTO id_map (old_id, new_id) VALUES (?,?)",
                (old_id, new_id),
            )
        conn.commit()

    def _persist_id_map_raw(
        self,
        conn: sqlite3.Connection,
        old_ids: list[str],
        new_ids: list[str],
    ) -> None:
        for old_id, new_id in zip(old_ids, new_ids):
            conn.execute(
                "INSERT OR IGNORE INTO id_map (old_id, new_id) VALUES (?,?)",
                (old_id, new_id),
            )
        conn.commit()

    def _lookup_id(self, conn: sqlite3.Connection, old_id: str) -> str | None:
        row = conn.execute(
            "SELECT new_id FROM id_map WHERE old_id=?", (old_id,)
        ).fetchone()
        return row[0] if row else None

    # ──────────────────────────────────────────────────────────────────────────
    # Audit log
    # ──────────────────────────────────────────────────────────────────────────

    def _append_audit(
        self,
        kind: str,
        label: str,
        batch_start: int,
        batch_size: int,
        migrated: int,
        failed: int,
    ) -> None:
        entry = {
            "ts": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "kind": kind,
            "label": label,
            "batch_start": batch_start,
            "batch_size": batch_size,
            "migrated": migrated,
            "failed": failed,
        }
        import os

        audit_path = os.path.join(self.output_dir, self.audit_log)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    # ──────────────────────────────────────────────────────────────────────────
    # Gremlin client factory
    # ──────────────────────────────────────────────────────────────────────────

    def _make_gremlin_client(self, url: str) -> Any:  # noqa: ANN401
        """Create a Gremlin Python client for the given URL."""
        from gremlin_python.driver import client as gremlin_client

        kwargs: dict[str, Any] = {}
        if url == self.target_gremlin_url:
            if self.target_username:
                kwargs["username"] = self.target_username
            if self.target_password:
                kwargs["password"] = self.target_password
        return gremlin_client.Client(url, "g", **kwargs)
