"""Data migrator — moves vertices and edges from DSE Graph to JanusGraph / HCD.

Supports two strategies:
  - CQL-direct (DSG): reads via cassandra-driver token-range paging (fast).
  - Gremlin-paged (Classic): reads via Gremlin range() queries (fallback).

Uses SQLite for checkpoint/resume and writes an audit JSONL log.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from datetime import time as dt_time
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from time import sleep
from typing import Any

from gremlin_python.statics import long as gremlin_long

from dse_jg_migrator.models import AnalysisResult
from dse_jg_migrator.translator import is_tuple_type, resolve_target_class, udt_reference

logger = logging.getLogger(__name__)

_RETRIES = 3  # AGENTS.md §0: retry a failed request three times
_BACKOFF_S = 1.0  # exponential: 1 s, 2 s, 4 s
# A request the server cannot decode gets no response at all (seen live), so never wait unbounded. The slowest
# 100-row batch observed under QEMU emulation took ~7 s.
_REQUEST_TIMEOUT_S = 120.0
_GEOSHAPE = "org.janusgraph.core.attribute.Geoshape"
_STRING = "java.lang.String"
_LONG = "java.lang.Long"
_INSERT_MAP = {
    "id_map": "INSERT OR IGNORE INTO id_map (old_id, new_id) VALUES (?,?)",
    "edge_map": "INSERT OR IGNORE INTO edge_map (old_key, new_id) VALUES (?,?)",
}


def _is_cql_duration(value: Any) -> bool:  # noqa: ANN401
    """True for a ``cassandra.util.Duration`` (months, days, nanoseconds), without importing the driver."""
    return all(hasattr(value, attr) for attr in ("months", "days", "nanoseconds"))


_GRAPHSON_TIME_TYPES = {"gx:Instant", "gx:OffsetDateTime", "gx:ZonedDateTime", "gx:LocalDateTime"}


def _from_graphson(value: Any) -> Any:  # noqa: ANN401
    """Decode a GraphSON value gremlin-python left as a raw ``{'@type', '@value'}`` dict (Java/DSE types it lacks).

    The source is read over GraphSON because DSE's GraphBinary uses types gremlin-python cannot decode at all
    (found live on a Classic ``Timestamp``). Times become naive UTC ``datetime`` (``java.util.Date`` on the target);
    other types (``dse:Point`` WKT, ``gx:LocalTime``, ``gx:InetAddress``) keep their text form.
    """
    if isinstance(value, list):
        return [_from_graphson(v) for v in value]
    if not (isinstance(value, dict) and set(value) == {"@type", "@value"}):
        return value
    kind, raw = value["@type"], str(value["@value"])
    if kind in _GRAPHSON_TIME_TYPES:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    if kind == "gx:LocalDate":
        return datetime.combine(date.fromisoformat(raw), time.min)
    return raw


def _iso8601_duration(months: int, days: int, nanos: int) -> str:
    """Format a CQL duration as ISO-8601, e.g. ``P1M2DT3.000000004S`` (CQL components share one sign)."""
    sign = "-" if min(months, days, nanos) < 0 else ""
    months, days, nanos = abs(months), abs(days), abs(nanos)
    seconds, frac = divmod(nanos, 1_000_000_000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    secs = f"{seconds}.{frac:09d}".rstrip("0") if frac else str(seconds)
    date_part = (f"{months}M" if months else "") + (f"{days}D" if days else "")
    time_part = (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    time_part += f"{secs}S" if seconds or frac else ""
    if not date_part and not time_part:
        return "PT0S"
    return f"{sign}P{date_part}" + (f"T{time_part}" if time_part else "")


# Batched write scripts. ``.next()`` is required — a list of un-iterated traversals cannot be serialised by the
# server — and ``from(g.V(x).next())`` is required because an inline traversal source child is rejected
# ("not spawned anonymously"). Both verified on a live JanusGraph.
_ADD_VERTICES = """
rows.collect { r ->
  def t = g.addV(r.label)
  r.props.each { p -> t = t.property(p[0], (p[0] in geo) ? Geoshape.fromWkt(p[1]) : p[1]) }
  t.next().id()
}
""".strip()

_ADD_EDGES = """
rows.collect { r ->
  def t = g.addE(r.label).from(g.V(r.src).next()).to(g.V(r.dst).next())
  r.props.each { p -> t = t.property(p[0], (p[0] in geo) ? Geoshape.fromWkt(p[1]) : p[1]) }
  t.next().id().toString()
}
""".strip()


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
        checkpoint_db: str = "",
        audit_log: str = "",
        output_dir: str = ".",
        decimal_strategy: str = "double",
        udt_strategy: str = "flat",
        map_strategy: str = "flat",
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
        # Per graph and per output dir: a shared default in the CWD made other runs look already complete.
        self.checkpoint_db = checkpoint_db or os.path.join(output_dir, f"{analysis.graph_name}-migration-checkpoint.db")
        self.audit_log = audit_log or f"{analysis.graph_name}-migration-audit.jsonl"
        self.output_dir = output_dir
        self._meta_warned: set[tuple[str, str]] = set()
        # The three strategies must match the values given to `translate`: the migrator writes the keys it created.
        self.decimal_strategy = decimal_strategy
        self.udt_strategy = udt_strategy
        self.map_strategy = map_strategy
        self._udt_props, self._tuple_props = self._structured_props(analysis)
        # Multi-valued vertex keys: valueMap() returns every value as a list; single-valued keys are unwrapped.
        self._multi_keys = {
            p.name for vl in analysis.vertex_labels for p in vl.properties if p.cardinality in ("LIST", "SET")
        }
        self._key_classes = self._target_classes(analysis, decimal_strategy)
        self._geo_keys = {k for k, cls in self._key_classes.items() if cls == _GEOSHAPE}
        self._sleep: Callable[[float], None] = sleep
        # Per-run state, reset by run().
        self._failed: dict[tuple[str, str], int] = {}
        self._errors: list[str] = []
        self._lossy_decimals: dict[str, int] = {}
        self._missing_endpoints: dict[str, int] = {}
        self._skipped = 0

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """Execute the full migration. Returns a summary dict.

        Writes to: target JanusGraph, the SQLite checkpoint and the audit log. Vertex labels go first; edges start
        only when every vertex label is complete. A label with a batch that failed after all retries is left
        incomplete, so re-running ``migrate`` retries it — elements already in ``id_map``/``edge_map`` are skipped.
        """
        self._failed = {}
        self._errors = []
        self._lossy_decimals = {}
        self._missing_endpoints = {}
        self._skipped = 0
        conn = self._init_checkpoint(self.checkpoint_db)
        target_client = self._make_gremlin_client(self.target_gremlin_url)
        stats: dict[str, Any] = {"vertices": {}, "edges": {}, "errors": self._errors}

        # Appendix E.7: OLAP traversal sources are not migrated — record the skip.
        if self.analysis.config.olap_enabled:
            self._append_audit_event(
                "SKIPPED_OLAP", "a", "OLAP traversal source 'a' is not migrated; see Appendix E.7."
            )

        try:
            # Phase 1: migrate all vertices
            for vl in self.analysis.vertex_labels:
                label = vl.label
                if self._is_completed(conn, label, "vertex"):
                    continue
                stats["vertices"][label] = self._migrate_vertices(label, conn, target_client)
                self._complete_unless_failed(conn, label, "vertex")

            # Phase 2: migrate all edges — only once every vertex label is complete (endpoints must be in id_map)
            pending = [
                vl.label for vl in self.analysis.vertex_labels if not self._is_completed(conn, vl.label, "vertex")
            ]
            if pending:
                self._errors.append(
                    f"Edges not migrated: vertex label(s) {', '.join(pending)} had failed batches; "
                    "re-run migrate to retry them."
                )
            else:
                for el in self.analysis.edge_labels:
                    label = el.label
                    if self._is_completed(conn, label, "edge"):
                        continue
                    stats["edges"][label] = self._migrate_edges(el, conn, target_client)
                    self._complete_unless_failed(conn, label, "edge")

            # Appendix E.9: archive the id_map for post-migration ID lookups.
            stats["id_map_archive"] = self._archive_id_map(conn)

        finally:
            target_client.close()
            conn.close()

        stats["already_migrated"] = self._skipped
        stats["missing_endpoints"] = dict(self._missing_endpoints)
        stats["lossy_decimals"] = dict(self._lossy_decimals)
        for key, count in self._lossy_decimals.items():
            self._append_audit_event(
                "LOSSY_DECIMAL", key, f"{count} Decimal value(s) not exactly representable as Double"
            )
        return stats

    def _complete_unless_failed(self, conn: sqlite3.Connection, label: str, kind: str) -> None:
        """Mark a label complete in the checkpoint unless one of its batches failed after all retries."""
        if not self._failed.get((kind, label)):
            self._mark_completed(conn, label, kind)

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
        keyspace = self.analysis.graph_name

        stmt = SimpleStatement(
            f"SELECT * FROM {keyspace}.{label}",  # noqa: S608
            fetch_size=self.cql_fetch_size,
        )

        write_buf: list[tuple[str, dict[str, Any]]] = []
        total = 0

        try:
            session = cluster.connect()
            for row in session.execute(stmt):
                props = self._row_to_props(self._expand_structured(row._asdict()), self.map_strategy)
                if self._lookup_id(conn, self._cql_old_id(label, props)) is not None:
                    self._skipped += 1  # migrated by an earlier (interrupted or partially failed) run
                    continue
                write_buf.append((label, props))
                if len(write_buf) >= self.write_batch:
                    new_ids = self._flush_vertex_batch(target_client, write_buf)
                    self._persist_id_map(conn, write_buf, new_ids)
                    self._append_audit(
                        "vertex", label, total, len(write_buf), len(new_ids), len(write_buf) - len(new_ids)
                    )
                    total += len(new_ids)
                    write_buf.clear()
            if write_buf:
                new_ids = self._flush_vertex_batch(target_client, write_buf)
                self._persist_id_map(conn, write_buf, new_ids)
                total += len(new_ids)
        except Exception as exc:  # noqa: BLE001  # any source read failure must leave the label incomplete, not crash
            self._record_failure(
                "vertex", label, self._cql_read_error(f"{keyspace}.{label}", exc), event="SOURCE_READ_FAILED"
            )
        finally:
            cluster.shutdown()

        return total

    def _cql_read_error(self, table: str, exc: Exception) -> str:
        """Describe a source CQL failure with host, port and table (AGENTS.md §4.6)."""
        return (
            f"Reading {table} from CQL {self.source_cql_host}:{self.source_cql_port} failed: "
            f"{type(exc).__name__}: {exc}. Re-run migrate to retry; already-migrated elements are skipped."
        )

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
                # valueMap(true), not elementMap(): elementMap() keeps only ONE value of a multi-valued Classic
                # property (found live). A page that cannot be read ends the label, which stays incomplete.
                results = self._submit_with_retry(
                    source_client,
                    "g.with('allow-filtering').V().hasLabel(lbl).range(off, end).valueMap(true)",
                    {"lbl": label, "off": offset, "end": offset + self.gremlin_batch},
                    "vertex",
                    label,
                    self.gremlin_batch,
                    url=self.source_gremlin_url,
                    operation="page read",
                )

                if not results:
                    break

                meta_by_vertex: dict[str, dict[str, str]] = {}
                if self.analysis.engine == "classic":
                    meta_by_vertex = self._fetch_meta_properties(source_client, label, offset)

                write_buf: list[tuple[str, dict[str, Any]]] = []
                for vm in results:
                    element_id, props, _, _ = self._split_element_map(dict(vm))
                    props = {
                        k: _from_graphson(
                            v if k in self._multi_keys or not isinstance(v, list) else (v[0] if v else None)
                        )
                        for k, v in props.items()
                    }
                    old_id = element_id or f"{label}_{offset}"
                    if self._lookup_id(conn, old_id) is not None:
                        self._skipped += 1
                        continue
                    props.update(meta_by_vertex.get(old_id, {}))
                    write_buf.append((old_id, props))

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

    @staticmethod
    def _split_element_map(element_map: dict[Any, Any]) -> tuple[str, dict[str, Any], str, str]:
        """Split an ``elementMap()`` result into (element id, properties, OUT-vertex id, IN-vertex id).

        gremlin-python keys the id/label with ``T.id``/``T.label`` and an edge's endpoints with ``Direction.OUT``/
        ``Direction.IN`` (each a nested ``{T.id, T.label}`` map). Plain-string keys (``~id``, ``id``, ``~outV``,
        ``outV``, …) are accepted as well. Endpoint ids are ``""`` for vertices.
        """
        from gremlin_python.process.traversal import Direction, T

        def ref_id(ref: Any) -> str:  # noqa: ANN401
            return str(ref.get(T.id, ref.get("id", ""))) if isinstance(ref, dict) else str(ref or "")

        element_id, out_id, in_id = "", "", ""
        props: dict[str, Any] = {}
        for key, value in element_map.items():
            if key in (T.id, "~id", "id"):
                element_id = str(value)
            elif key in (Direction.OUT, "~outV", "outV"):
                out_id = ref_id(value)
            elif key in (Direction.IN, "~inV", "inV"):
                in_id = ref_id(value)
            elif key not in (T.label, "~label", "label"):
                props[str(key)] = value
        return element_id, props, out_id, in_id

    def _fetch_meta_properties(
        self,
        source_client: Any,  # noqa: ANN401
        label: str,
        offset: int,
    ) -> dict[str, dict[str, str]]:
        """Collect meta-properties (Classic only) for one page of vertices.

        Read-only: queries the source DSE Gremlin server for ``properties()`` of the
        same ``range()`` page used for ``elementMap()``. Each property that carries
        meta-properties is converted to a JSON string ``{"value": ..., "meta": {...}}``
        (Appendix E.2) and a warning is logged once per label/key.

        Returns: ``{old_vertex_id: {property_key: json_string}}``. A failed query
        returns ``{}`` after logging a warning (the vertex data itself still migrates).
        """
        try:
            vps = (
                source_client.submit(
                    "g.with('allow-filtering').V().hasLabel(lbl).range(off, end).properties()",
                    {"lbl": label, "off": offset, "end": offset + self.gremlin_batch},
                )
                .all()
                .result()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Meta-property query failed for label '%s' at %s (offset %d): %s",
                label,
                self.source_gremlin_url,
                offset,
                exc,
            )
            return {}

        out: dict[str, dict[str, str]] = {}
        for vp in vps or []:
            metas = getattr(vp, "properties", None)
            if not metas:
                continue
            meta = {str(m.key): m.value for m in metas}
            vertex_id = str(vp.element.id)
            key = str(vp.key)
            out.setdefault(vertex_id, {})[key] = json.dumps({"value": vp.value, "meta": meta}, default=str)
            self._warn_meta_property(label, key)
        return out

    def _warn_meta_property(self, label: str, key: str) -> None:
        """Log (and audit) a meta-property conversion once per label/key pair."""
        if (label, key) in self._meta_warned:
            return
        self._meta_warned.add((label, key))
        logger.warning(
            "Meta-properties on '%s.%s' converted to a JSON string (JanusGraph semantics differ; Appendix E.2).",
            label,
            key,
        )
        self._append_audit_event("META_PROPERTY_JSON", label, f"property '{key}' serialised as JSON string")

    def _flush_vertex_batch(
        self,
        client: Any,  # noqa: ANN401
        buf: list[tuple[str, dict[str, Any]]],
    ) -> list[str]:
        """Submit a batch of addV() calls. Returns new JG IDs.

        Writes to: target JanusGraph. The whole batch travels in ONE ``rows`` binding because the server
        rejects requests with more than 16 bindings (observed live, ``maxParameters``). Values are never
        interpolated into the script. A failure is retried (``_submit_with_retry``); a batch that still fails
        returns ``[]`` and leaves its label incomplete in the checkpoint.
        """
        if not buf:
            return []
        rows = [
            {"label": label, "props": [[k, self._target_value(k, v)] for k, v in self._prop_pairs(props)]}
            for label, props in buf
        ]
        bindings = {"rows": rows, "geo": sorted(self._geo_keys)}
        return [str(r) for r in self._submit_with_retry(client, _ADD_VERTICES, bindings, "vertex", buf[0][0], len(buf))]

    def _submit_with_retry(
        self,
        client: Any,  # noqa: ANN401
        script: str,
        bindings: dict[str, Any],
        kind: str,
        label: str,
        size: int,
        *,
        url: str = "",
        operation: str = "batch",
    ) -> list[Any]:
        """Submit one batch write (or source page read), retrying a failure ``_RETRIES`` times with backoff.

        Each attempt waits at most ``_REQUEST_TIMEOUT_S`` for the response, so a request the server never answers
        (e.g. one it cannot decode) is retried instead of blocking forever.

        Writes to: target JanusGraph (``operation="batch"``); a ``"page read"`` reads the source (``url``). A request
        that still fails is recorded (``_record_failure``) so the label is not marked complete.
        Returns the server's result list, or ``[]`` after the final failure.
        """
        url = url or self.target_gremlin_url
        for attempt in range(_RETRIES + 1):
            try:
                return list(client.submit(script, bindings).all().result(timeout=_REQUEST_TIMEOUT_S) or [])
            except Exception as exc:  # noqa: BLE001
                if attempt < _RETRIES:
                    delay = _BACKOFF_S * 2**attempt
                    logger.warning(
                        "%s %s of %d for '%s' failed on %s (attempt %d of %d); retrying in %.0f s: %s",
                        kind.capitalize(),
                        operation,
                        size,
                        label,
                        url,
                        attempt + 1,
                        _RETRIES + 1,
                        delay,
                        exc,
                    )
                    self._sleep(delay)
                    continue
                message = (
                    f"{kind.capitalize()} {operation} of {size} for '{label}' failed on {url} "
                    f"after {_RETRIES} retries: {exc}"
                )
                event = "BATCH_FAILED" if operation == "batch" else "SOURCE_READ_FAILED"
                self._record_failure(kind, label, message, size, event)
        return []

    def _record_failure(self, kind: str, label: str, message: str, size: int = 1, event: str = "BATCH_FAILED") -> None:
        """Log, audit and count a failure against ``(kind, label)`` so the label is left incomplete for a re-run."""
        logger.error(message)
        self._failed[(kind, label)] = self._failed.get((kind, label), 0) + size
        self._errors.append(message)
        self._append_audit_event(event, label, message)

    @staticmethod
    def _target_classes(analysis: AnalysisResult, decimal_strategy: str = "double") -> dict[str, str]:
        """Map each analysed property key to its JanusGraph class, using the translator's resolution rules.

        The first definition of a key wins, in the same order the translator visits them, so the classes match
        the schema written by ``translate`` (given the same ``decimal_strategy``).
        """
        typed: list[tuple[str, str]] = []
        for vl in analysis.vertex_labels:
            typed += [(k.name, k.type) for k in vl.partition_keys]
            typed += [(k.name, k.type) for k in vl.clustering_keys]
            typed += [(p.name, p.type) for p in vl.properties]
        for el in analysis.edge_labels:
            typed += [(p.name, p.type) for p in el.properties]
        for udt in analysis.udts:  # flat strategy: one key per UDT field
            typed += [(f"{udt.name}_{f.name}", f.type) for f in udt.fields]
        classes: dict[str, str] = {}
        for name, dse_type in typed:
            classes.setdefault(name, resolve_target_class(dse_type, analysis.engine, decimal_strategy))
        return classes

    def _target_value(self, key: str, value: Any) -> Any:  # noqa: ANN401
        """Convert a value to the representation of its target property key's class.

        ``Decimal`` → ``float`` (counting values that do not round-trip as ``LOSSY_DECIMAL``) or exact ``str``;
        CQL ``Duration`` → ISO-8601 ``str``; ``timedelta`` (Classic) → milliseconds; ``int`` → ``str`` for String
        keys (``Varint``). Ints for Long keys — and any int outside int32 — are sent as gremlin-python ``long``:
        a plain ``int`` is packed as a 32-bit Int and overflows (found live on a ``Bigint`` key).
        """
        cls = self._key_classes.get(key)
        if isinstance(value, Decimal):
            if cls == _STRING:
                return str(value)
            as_float = float(value)
            if Decimal(repr(as_float)) != value:
                self._lossy_decimals[key] = self._lossy_decimals.get(key, 0) + 1
            return as_float
        if cls == _STRING:
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            if _is_cql_duration(value):
                return _iso8601_duration(value.months, value.days, value.nanoseconds)
        if cls == _LONG and isinstance(value, timedelta):
            return gremlin_long(value // timedelta(milliseconds=1))
        if isinstance(value, int) and not isinstance(value, bool):
            if cls == _LONG or not -(2**31) <= value < 2**31:
                return gremlin_long(value)
        return value

    @staticmethod
    def _prop_pairs(props: dict[str, Any]) -> list[tuple[str, Any]]:
        """Expand list-valued properties into one (key, value) pair per element.

        JanusGraph writes SET/LIST keys with repeated ``.property(k, v)`` calls; passing a list
        as a single value would be rejected by the property key's data type.
        """
        pairs: list[tuple[str, Any]] = []
        for k, v in props.items():
            if isinstance(v, list):
                pairs.extend((k, item) for item in v)
            else:
                pairs.append((k, v))
        return pairs

    @staticmethod
    def _structured_props(analysis: AnalysisResult) -> tuple[dict[str, str], set[str]]:
        """Return ``{property: udt_name}`` for UDT-typed properties and the names of ``tupleOf`` properties."""
        props = [p for vl in analysis.vertex_labels for p in vl.properties]
        props += [p for el in analysis.edge_labels for p in el.properties]
        udts = {p.name: udt for p in props if (udt := udt_reference(p.type)) is not None}
        return udts, {p.name for p in props if is_tuple_type(p.type)}

    def _expand_structured(self, row: dict[str, Any]) -> dict[str, Any]:
        """Rewrite UDT and tuple columns into the keys ``translate`` created (Sub-Task 5, step 12).

        A UDT (a cassandra-driver namedtuple) becomes ``<udt>_<field>`` values (``flat``) or one JSON String
        ``<udt>`` (``json``); a tuple becomes a JSON array String. Unchanged otherwise, they would be expanded into
        repeated values of a SINGLE key and silently reduced to the last field (found live).
        """
        out: dict[str, Any] = {}
        for k, v in row.items():
            udt = self._udt_props.get(k)
            if v is None or (udt is None and k not in self._tuple_props):
                out[k] = v
            elif udt is not None:
                fields = v._asdict() if hasattr(v, "_asdict") else dict(v)
                if self.udt_strategy == "json":
                    out[udt] = json.dumps(fields, default=str)
                else:
                    out.update({f"{udt}_{name}": value for name, value in fields.items()})
            else:
                out[k] = json.dumps(list(v), default=str)
        return out

    @staticmethod
    def _row_to_props(row: dict[str, Any], map_strategy: str = "flat") -> dict[str, Any]:
        """Convert a CQL row into Gremlin-serialisable JanusGraph properties.

        Drops NULLs and DSE Search's ``solr_query`` pseudo-column; converts dates to naive midnight
        ``datetime`` (gremlin-python cannot serialise ``datetime.date``, and JanusGraph's ``java.util.Date``
        rejects tz-aware values — both verified live); converts sets to lists; and writes maps as parallel
        ``<k>_key`` / ``<k>_value`` lists (``flat``, the default) or one JSON object String (``json``).
        """
        out: dict[str, Any] = {}
        for k, v in row.items():
            if v is None or k == "solr_query":
                continue
            if isinstance(v, Mapping):  # cassandra-driver returns OrderedMapSerializedKey — a Mapping, not a dict
                if map_strategy == "json":
                    out[k] = json.dumps({str(key): value for key, value in v.items()}, default=str)
                else:
                    out[f"{k}_key"] = [str(x) for x in v]
                    out[f"{k}_value"] = [str(x) for x in v.values()]
            elif isinstance(v, (set, frozenset, list, tuple)) or type(v).__name__ == "SortedSet":
                out[k] = list(v)
            elif type(v).__name__ == "Date" and hasattr(v, "date"):
                out[k] = datetime.combine(v.date(), time.min)  # cassandra.util.Date -> naive midnight datetime
            elif isinstance(v, datetime):
                out[k] = v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo else v
            elif isinstance(v, date):
                out[k] = datetime.combine(v, time.min)
            elif type(v).__name__ in ("Point", "LineString", "Polygon"):
                out[k] = str(v)  # cassandra.util geometry -> WKT; Point is rebuilt as Geoshape on the target
            elif isinstance(v, dt_time):
                out[k] = v.isoformat()  # Time -> ISO-8601 String (LocalTime is unsupported on JanusGraph)
            elif isinstance(v, (IPv4Address, IPv6Address)) or type(v).__name__ == "Time":  # cassandra.util.Time
                out[k] = str(v)  # Inet / Time -> String
            else:
                out[k] = v
        return out

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

        # DSE 6.8 DSG names edge tables <from>__<label>__<to>.
        table_name = el.table_name or f"{el.from_label}__{el.label}__{el.to_label}"
        keyspace = self.analysis.graph_name

        cluster = Cluster([self.source_cql_host], port=self.source_cql_port)
        stmt = SimpleStatement(
            f"SELECT * FROM {keyspace}.{table_name}",  # noqa: S608
            fetch_size=self.cql_fetch_size,
        )

        write_buf: list[tuple[str, str, str, dict[str, Any]]] = []
        old_keys: list[str] = []
        total = 0
        missing = 0

        try:
            session = cluster.connect()
            # The edge table's partition key columns identify the OUT vertex and its first clustering
            # column the IN vertex (e.g. person_person_id / recipe_recipe_id, or out_person_id / in_person_id).
            table_meta = cluster.metadata.keyspaces[keyspace].tables[table_name]
            out_col = table_meta.partition_key[0].name
            in_col = table_meta.clustering_key[0].name
            # The full primary key identifies one edge row; it keys the edge in edge_map for idempotent resume.
            key_order = [c.name for c in table_meta.partition_key] + [c.name for c in table_meta.clustering_key]
            key_cols = set(key_order)
            for row in session.execute(stmt):
                row_dict = row._asdict()
                old_key = f"{table_name}:{json.dumps([str(row_dict.get(c)) for c in key_order])}"
                if self._lookup_edge(conn, old_key) is not None:
                    self._skipped += 1
                    continue
                out_new = self._lookup_id(conn, f"{el.from_label}:{row_dict.get(out_col, '')}")
                in_new = self._lookup_id(conn, f"{el.to_label}:{row_dict.get(in_col, '')}")
                if out_new is None or in_new is None:
                    missing += 1
                    continue
                edge_props = self._row_to_props(
                    self._expand_structured({k: v for k, v in row_dict.items() if k not in key_cols}), self.map_strategy
                )
                write_buf.append((el.label, out_new, in_new, edge_props))
                old_keys.append(old_key)
                if len(write_buf) >= self.write_batch:
                    total += self._flush_edges(conn, target_client, write_buf, old_keys)
            total += self._flush_edges(conn, target_client, write_buf, old_keys)
        except Exception as exc:  # noqa: BLE001  # any source read failure must leave the label incomplete, not crash
            self._record_failure(
                "edge", el.label, self._cql_read_error(f"{keyspace}.{table_name}", exc), event="SOURCE_READ_FAILED"
            )
        finally:
            cluster.shutdown()

        self._note_missing_endpoints(el.label, missing)
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
        missing = 0

        try:
            while True:
                results = self._submit_with_retry(
                    source_client,
                    "g.with('allow-filtering').E().hasLabel(lbl).range(off, end).elementMap()",
                    {"lbl": edge_label, "off": offset, "end": offset + self.gremlin_batch},
                    "edge",
                    edge_label,
                    self.gremlin_batch,
                    url=self.source_gremlin_url,
                    operation="page read",
                )

                if not results:
                    break

                write_buf: list[tuple[str, str, str, dict[str, Any]]] = []
                old_keys: list[str] = []
                for em in results:
                    edge_id, props, out_id, in_id = self._split_element_map(dict(em))
                    props = {k: _from_graphson(v) for k, v in props.items()}
                    old_key = edge_id or f"{edge_label}:{out_id}->{in_id}"
                    if self._lookup_edge(conn, old_key) is not None:
                        self._skipped += 1
                        continue
                    out_new = self._lookup_id(conn, out_id)
                    in_new = self._lookup_id(conn, in_id)
                    if not (out_new and in_new):
                        missing += 1
                        continue
                    write_buf.append((edge_label, out_new, in_new, props))
                    old_keys.append(old_key)

                total += self._flush_edges(conn, target_client, write_buf, old_keys)
                offset += self.gremlin_batch
        finally:
            source_client.close()

        self._note_missing_endpoints(edge_label, missing)
        return total

    def _flush_edges(
        self,
        conn: sqlite3.Connection,
        client: Any,  # noqa: ANN401
        buf: list[tuple[str, str, str, dict[str, Any]]],
        old_keys: list[str],
    ) -> int:
        """Flush an edge buffer, record the new edge ids in ``edge_map`` and clear both lists. Returns the count."""
        new_ids = self._flush_edge_batch(client, buf)
        self._persist_map(conn, "edge_map", old_keys, new_ids)
        buf.clear()
        old_keys.clear()
        return len(new_ids)

    def _note_missing_endpoints(self, label: str, missing: int) -> None:
        """Report edges skipped because an endpoint vertex is not in ``id_map`` (dangling in the source)."""
        if not missing:
            return
        self._missing_endpoints[label] = self._missing_endpoints.get(label, 0) + missing
        logger.warning("%d '%s' edge(s) skipped: endpoint vertex not in id_map (dangling in source)", missing, label)
        self._append_audit_event("MISSING_ENDPOINTS", label, f"{missing} edge(s) skipped: endpoint vertex not migrated")

    def _flush_edge_batch(
        self,
        client: Any,  # noqa: ANN401
        buf: list[tuple[str, str, str, dict[str, Any]]],
    ) -> list[str]:
        """Submit a batch of addE() calls. Returns the new JanusGraph edge ids, in buffer order.

        Writes to: target JanusGraph, with the whole batch in ONE ``rows`` binding (see
        ``_flush_vertex_batch``). A failure is retried; a batch that still fails returns ``[]``.
        """
        if not buf:
            return []
        rows = [
            {
                "label": label,
                "src": out_id,
                "dst": in_id,
                "props": [[k, self._target_value(k, v)] for k, v in self._prop_pairs(props)],
            }
            for label, out_id, in_id, props in buf
        ]
        bindings = {"rows": rows, "geo": sorted(self._geo_keys)}
        return [str(r) for r in self._submit_with_retry(client, _ADD_EDGES, bindings, "edge", buf[0][0], len(buf))]

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
        conn.execute("CREATE TABLE IF NOT EXISTS id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_id_map ON id_map(old_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS edge_map (old_key TEXT PRIMARY KEY, new_id TEXT NOT NULL)")
        conn.commit()
        return conn

    def _is_completed(self, conn: sqlite3.Connection, label: str, kind: str) -> bool:
        row = conn.execute("SELECT completed FROM progress WHERE label=? AND kind=?", (label, kind)).fetchone()
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
        self._persist_map(conn, "id_map", [self._cql_old_id(label, props) for label, props in buf], new_ids)

    def _cql_old_id(self, label: str, props: dict[str, Any]) -> str:
        """The ``id_map`` key of a DSG vertex row: ``<label>:<value of the label's first partition key>``."""
        pk_key = next(
            (
                vl.partition_keys[0].name
                for vl in self.analysis.vertex_labels
                if vl.label == label and vl.partition_keys
            ),
            None,
        ) or next(iter(props), "")
        return f"{label}:{props.get(pk_key, '')}"

    def _persist_id_map_raw(
        self,
        conn: sqlite3.Connection,
        old_ids: list[str],
        new_ids: list[str],
    ) -> None:
        self._persist_map(conn, "id_map", old_ids, new_ids)

    def _persist_map(self, conn: sqlite3.Connection, table: str, old_ids: list[str], new_ids: list[str]) -> None:
        """Record ``old → new`` id pairs in ``id_map`` or ``edge_map`` (existing entries are kept)."""
        conn.executemany(_INSERT_MAP[table], zip(old_ids, new_ids))
        conn.commit()

    def _lookup_edge(self, conn: sqlite3.Connection, old_key: str) -> str | None:
        row = conn.execute("SELECT new_id FROM edge_map WHERE old_key=?", (old_key,)).fetchone()
        return row[0] if row else None

    def _archive_id_map(self, conn: sqlite3.Connection) -> str:
        """Copy the id_map table to ``<graph>-id-map.sqlite`` in output_dir (Appendix E.9).

        Read-only on the checkpoint DB; writes a standalone SQLite file (replaced if it
        already exists, so re-runs are idempotent). Returns the archive path.
        """
        path = os.path.join(self.output_dir, f"{self.analysis.graph_name}-id-map.sqlite")
        conn.commit()
        conn.execute("ATTACH DATABASE ? AS archive", (path,))
        try:
            conn.execute("DROP TABLE IF EXISTS archive.id_map")
            conn.execute("CREATE TABLE archive.id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)")
            conn.execute("INSERT INTO archive.id_map SELECT old_id, new_id FROM main.id_map")
            conn.commit()
        finally:
            conn.execute("DETACH DATABASE archive")
        return path

    def _lookup_id(self, conn: sqlite3.Connection, old_id: str) -> str | None:
        row = conn.execute("SELECT new_id FROM id_map WHERE old_id=?", (old_id,)).fetchone()
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
        self._write_audit_entry(entry)

    def _append_audit_event(self, event: str, label: str, message: str) -> None:
        """Append a non-batch audit entry (e.g. SKIPPED_OLAP, META_PROPERTY_JSON)."""
        self._write_audit_entry(
            {
                "ts": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "kind": event,
                "label": label,
                "message": message,
            }
        )

    def _write_audit_entry(self, entry: dict[str, Any]) -> None:
        audit_path = os.path.join(self.output_dir, self.audit_log)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    # ──────────────────────────────────────────────────────────────────────────
    # Gremlin client factory
    # ──────────────────────────────────────────────────────────────────────────

    def _make_gremlin_client(self, url: str) -> Any:  # noqa: ANN401
        """Create a Gremlin Python client for the given URL.

        The DSE source is read over GraphSON v3: DSE's GraphBinary uses types gremlin-python cannot decode (found
        live), while GraphSON leaves unknown types as ``{'@type', '@value'}`` dicts that ``_from_graphson`` decodes.
        """
        from gremlin_python.driver import serializer

        from dse_jg_migrator.gremlin_io import open_gremlin_client

        kwargs: dict[str, Any] = {}
        if url == self.target_gremlin_url:
            if self.target_username:
                kwargs["username"] = self.target_username
            if self.target_password:
                kwargs["password"] = self.target_password
        else:
            kwargs["message_serializer"] = serializer.GraphSONSerializersV3d0()
        # DSE requires the "<graph>.g" traversal source; JanusGraph (target) uses plain "g".
        source = "g" if url == self.target_gremlin_url else f"{self.analysis.graph_name}.g"
        return open_gremlin_client(url, source, **kwargs)
