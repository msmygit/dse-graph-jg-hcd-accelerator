"""DSE Graph schema analyser — read-only extraction of schema, indexes, and statistics."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from dse_jg_migrator.gremlin_io import quiet_gremlin_errors
from dse_jg_migrator.models import (
    AnalysisResult,
    ClusteringKey,
    EdgeLabelDefinition,
    GraphConfig,
    IndexDefinition,
    IndexKey,
    PartitionKey,
    PropertyDefinition,
    UDTDefinition,
    UDTField,
    UserFacingError,
    VertexLabelDefinition,
)

# Each top-level DSL statement starts with one of these; splitting on them keeps a vertex-label block
# from absorbing the edge-label lines that follow it in `schema.describe()` output.
_STATEMENT_SPLIT = re.compile(r"(?=schema\.(?:vertexLabel|edgeLabel|type)\()")
_INDEX_KEYWORDS = ("materializedView(", "searchIndex(", "secondaryIndex(")
# `.property('name', Type)` where Type is a bare type or a collection such as setOf(Varchar) or
# mapOf(Varchar, Date) (one level of nested parentheses is allowed).
_PROPERTY_RE = re.compile(r"\.property\('([^']+)',\s*(\w+(?:\((?:[^()]|\([^()]*\))*\))?)\)")

# Classic-engine `schema.describe()` is a different DSL: double quotes, one statement per line, types declared on
# property keys. Grammar from the DSE source (dse-graph-api .../schema/{PropertyKey,VertexLabel,EdgeLabel}Impl.java
# describe()); verified against a live DSE 6.9 Classic graph (tests/fixtures/dse69_classic_describe.groovy.txt).
_CLASSIC_KEY = re.compile(r'^schema\.propertyKey\("([^"]+)"\)\.(\w+)\(\)(.*)$')
_CLASSIC_VERTEX = re.compile(r'^schema\.vertexLabel\("([^"]+)"\)(.*)$')
_CLASSIC_EDGE = re.compile(r'^schema\.edgeLabel\("([^"]+)"\)(.*)$')
_CLASSIC_INDEX = re.compile(
    r'^\.index\("([^"]+)"\)\.(materialized|secondary|search|outE|inE|bothE|property)\(("[^"]*")?\)(.*)$'
)
_CLASSIC_BY = re.compile(r'\.by\("([^"]+)"\)(?:\.(asText|asString)\(\))?')
_CLASSIC_INDEX_KIND = {"materialized": "materializedView", "secondary": "secondaryIndex", "search": "searchIndex"}


def _quoted(call: str, text: str) -> list[str]:
    """Return the double-quoted arguments of ``.<call>(...)`` in ``text`` (e.g. ``.properties("a", "b")``)."""
    m = re.search(rf"\.{call}\(([^)]*)\)", text)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


class GraphNotFoundError(UserFacingError):
    """The source graph does not exist or has no schema — nothing to analyse (never written as an empty analysis)."""


class Analyser:
    """Read-only DSE Graph schema analyser.

    Connects to the source DSE Graph cluster, extracts the full schema via
    schema.describe(), supplements it with CQL metadata for DSG graphs, and
    collects per-label vertex/edge counts.

    Reads from: source DSE Gremlin server, source DSE CQL endpoint (DSG only).
    Writes: nothing to either cluster; writes <graph_name>-analysis.json to output_dir.
    """

    def __init__(
        self,
        gremlin_url: str,
        cql_host: str,
        cql_port: int = 9042,
        graph_name: str = "",
        username: str | None = None,
        password: str | None = None,
        output_dir: str = ".",
    ) -> None:
        self.gremlin_url = gremlin_url
        self.cql_host = cql_host
        self.cql_port = cql_port
        self.graph_name = graph_name
        self._username = username
        self._password = password
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> AnalysisResult:
        """Execute full analysis. Returns AnalysisResult and writes JSON to output_dir."""
        engine = self._detect_engine()
        raw_schema = self._extract_raw_schema()

        result = AnalysisResult(
            graph_name=self.graph_name,
            engine=engine,
            extracted_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        )

        if engine == "classic":
            result.vertex_labels, result.edge_labels, result.indexes = self._parse_classic(raw_schema)
        else:
            result.vertex_labels = self._parse_vertex_labels(raw_schema)
            result.edge_labels = self._parse_edge_labels(raw_schema)
            result.udts = self._parse_udts(raw_schema)
            result.indexes = self._parse_indexes(raw_schema)
        result.config = self._extract_config()

        if engine == "dsg" and self.cql_host:
            self._enrich_counts_from_cql(result)
        else:
            self._enrich_counts_from_gremlin(result)

        self._write_json(result)
        return result

    def write_json(self, result: AnalysisResult) -> str:
        """Write analysis JSON to output_dir and return the file path."""
        return self._write_json(result)

    # ──────────────────────────────────────────────────────────────────────────
    # Gremlin I/O
    # ──────────────────────────────────────────────────────────────────────────

    def _gremlin_submit(self, script: str, bindings: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        """Submit a read-only Gremlin script to the source DSE cluster."""
        from dse_jg_migrator.gremlin_io import open_gremlin_client

        kwargs: dict[str, Any] = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password

        c = open_gremlin_client(self.gremlin_url, f"{self.graph_name}.g", **kwargs)
        try:
            return c.submit(script, bindings or {}).all().result()
        finally:
            c.close()

    def _detect_engine(self) -> str:
        """Detect engine type ('classic' or 'dsg') via ``system.graph(name).describe()``.

        Verified against DSE 6.8: a Classic graph's describe() contains ``.engine(Classic)``;
        a Core (DSG) graph's contains ``.withReplication(...)`` and neither word "Core" nor "DSG".
        """
        try:
            desc = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            if "Classic" in str(desc):
                return "classic"
        except Exception:  # noqa: BLE001, S110
            pass
        return "dsg"

    def _extract_raw_schema(self) -> str:
        """Fetch the full schema DSL string from DSE Graph via schema.describe().

        Raises ``ConnectionError`` (URL and reason) if the source cannot be read, and ``GraphNotFoundError`` if the
        graph has no schema — an empty analysis was previously written with exit 0 (found in a fresh-user test).
        """
        try:
            parts = self._gremlin_submit("schema.describe()")
        except Exception as exc:
            raise ConnectionError(
                f"analysing graph '{self.graph_name}': schema.describe() on {self.gremlin_url} failed ({exc})"
            ) from exc
        schema = "\n".join(str(p) for p in (parts or []))
        if not schema.strip():
            raise GraphNotFoundError(
                f"graph '{self.graph_name}' on {self.gremlin_url} has no schema — check --graph-name, or seed it "
                "first with 'dse-jg-migrator seed'"
            )
        return schema

    def _extract_config(self) -> GraphConfig:
        """Extract graph configuration from DSE system metadata."""
        config = GraphConfig()
        try:
            parts = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            desc = str(parts or "")
            if "Production" in desc:
                config.schema_mode = "Production"
            elif "Development" in desc:
                config.schema_mode = "Development"
            if "allow_scan" in desc and "true" in desc.lower():
                config.allow_scan = True
            if "restrict_lambda" in desc and "true" in desc.lower():
                config.restrict_lambda = True
            desc_lower = desc.lower()
            if "rlac" in desc_lower or "row_level" in desc_lower or "row-level" in desc_lower:
                config.rlac_enabled = True
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            # OLAP traversal source 'a' responds only when OLAP is configured (read-only probe).
            with quiet_gremlin_errors():  # fails by design when OLAP is not configured
                if self._gremlin_submit("a.V().limit(0).count()") is not None:
                    config.olap_enabled = True
        except Exception:  # noqa: BLE001, S110
            pass  # OLAP not configured
        return config

    # ──────────────────────────────────────────────────────────────────────────
    # DSL parser helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_classic(
        schema: str,
    ) -> tuple[list[VertexLabelDefinition], list[EdgeLabelDefinition], list[IndexDefinition]]:
        """Parse Classic-engine ``schema.describe()`` output into labels and indexes.

        Property types and cardinality come from the ``schema.propertyKey(...)`` statements; ``.multiple()`` keys
        are LIST. Meta-property indexes (``.index(...).property(...)``) are not translatable and are skipped.
        """
        lines = [ln.strip() for ln in schema.splitlines() if ln.strip()]
        keys: dict[str, tuple[str, str]] = {}
        for ln in lines:
            if km := _CLASSIC_KEY.match(ln):
                keys[km.group(1)] = (km.group(2), "LIST" if ".multiple()" in km.group(3) else "SINGLE")

        def prop(name: str) -> PropertyDefinition:
            dse_type, card = keys.get(name, ("Text", "SINGLE"))
            return PropertyDefinition(
                name=name, type=dse_type, cardinality=card, collection="list" if card == "LIST" else None
            )

        vertex_labels: dict[str, VertexLabelDefinition] = {}
        edge_labels: dict[str, EdgeLabelDefinition] = {}
        indexes: list[IndexDefinition] = []
        for ln in lines:
            if vm := _CLASSIC_VERTEX.match(ln):
                label, rest = vm.group(1), vm.group(2)
                if im := _CLASSIC_INDEX.match(rest):
                    name, kind, arg, tail = im.groups()
                    by = list(_CLASSIC_BY.finditer(tail))
                    if kind in _CLASSIC_INDEX_KIND:
                        indexes.append(
                            IndexDefinition(
                                kind=_CLASSIC_INDEX_KIND[kind],
                                name=name,
                                element="vertex",
                                label=label,
                                keys=[IndexKey(name=b.group(1), as_mapping=b.group(2)) for b in by],
                            )
                        )
                    elif kind != "property" and arg:  # outE/inE/bothE("edge") -> vertex-centric edge index
                        indexes.append(
                            IndexDefinition(
                                kind="vertexCentric",
                                name=name,
                                element="edge",
                                label=arg.strip('"'),
                                keys=[IndexKey(name=b.group(1)) for b in by],
                                direction={"outE": "OUT", "inE": "IN"}.get(kind, "BOTH"),
                            )
                        )
                    continue
                if ".cache(" in rest:
                    continue
                vl = vertex_labels.setdefault(label, VertexLabelDefinition(label=label))
                id_keys = set()
                for name in _quoted("partitionKey", rest):
                    vl.partition_keys.append(PartitionKey(name=name, type=keys.get(name, ("Text", ""))[0]))
                    id_keys.add(name)
                for name in _quoted("clusteringKey", rest):
                    vl.clustering_keys.append(ClusteringKey(name=name, type=keys.get(name, ("Text", ""))[0]))
                    id_keys.add(name)
                known = {p.name for p in vl.properties}
                vl.properties += [prop(n) for n in _quoted("properties", rest) if n not in id_keys | known]
            elif em := _CLASSIC_EDGE.match(ln):
                label, rest = em.group(1), em.group(2)
                el = edge_labels.setdefault(label, EdgeLabelDefinition(label=label))
                if conn := re.search(r'\.connection\("([^"]+)",\s*"([^"]+)"\)', rest):
                    el.from_label = el.from_label or conn.group(1)  # extra connections share the label
                    el.to_label = el.to_label or conn.group(2)
                else:
                    known = {p.name for p in el.properties}
                    el.properties += [prop(n) for n in _quoted("properties", rest) if n not in known]
        return list(vertex_labels.values()), list(edge_labels.values()), indexes

    def _parse_vertex_labels(self, schema: str) -> list[VertexLabelDefinition]:
        """Parse schema.vertexLabel() blocks from the DSL string.

        Only includes primary label definition blocks (those containing
        .partitionBy() or .property() after the label name), not index
        definition blocks like materializedView() or searchIndex().
        """
        labels: list[VertexLabelDefinition] = []
        seen: set[str] = set()

        for block in _STATEMENT_SPLIT.split(schema):
            if not block.startswith("schema.vertexLabel("):
                continue
            # Skip index-definition blocks (materializedView, searchIndex, secondaryIndex)
            if any(kw in block for kw in _INDEX_KEYWORDS):
                continue
            m = re.match(r"schema\.vertexLabel\('([^']+)'\)", block)
            if not m:
                continue
            label_name = m.group(1)
            if label_name in seen:
                continue
            seen.add(label_name)
            vl = VertexLabelDefinition(label=label_name)
            vl.partition_keys = self._parse_partition_keys(block)
            vl.clustering_keys = self._parse_clustering_keys(block)
            vl.properties = self._parse_properties(block)
            labels.append(vl)
        return labels

    def _parse_edge_labels(self, schema: str) -> list[EdgeLabelDefinition]:
        """Parse schema.edgeLabel() blocks from the DSL string."""
        labels: list[EdgeLabelDefinition] = []
        seen: set[str] = set()
        for block in _STATEMENT_SPLIT.split(schema):
            if not block.startswith("schema.edgeLabel("):
                continue
            # Index-definition blocks (e.g. an edge materializedView) repeat .from()/.to()
            if any(kw in block for kw in _INDEX_KEYWORDS):
                continue
            m = re.match(r"schema\.edgeLabel\('([^']+)'\)", block)
            if not m:
                continue
            label_name = m.group(1)
            if label_name in seen:
                continue
            seen.add(label_name)
            el = EdgeLabelDefinition(label=label_name)

            from_m = re.search(r"\.from\('([^']+)'\)", block)
            to_m = re.search(r"\.to\('([^']+)'\)", block)
            if from_m:
                el.from_label = from_m.group(1)
            if to_m:
                el.to_label = to_m.group(1)
            el.properties = self._parse_properties(block)
            labels.append(el)
        return labels

    def _parse_udts(self, schema: str) -> list[UDTDefinition]:
        """Parse schema.type() UDT blocks from the DSL string."""
        udts: list[UDTDefinition] = []
        for block in _STATEMENT_SPLIT.split(schema):
            if not block.startswith("schema.type("):
                continue
            m = re.match(r"schema\.type\('([^']+)'\)", block)
            if not m:
                continue
            udt = UDTDefinition(name=m.group(1))
            udt.fields = [
                UDTField(name=pm.group(1), type=pm.group(2))
                for pm in re.finditer(r"\.property\('([^']+)',\s*(\w+)\)", block)
            ]
            udts.append(udt)
        return udts

    def _parse_indexes(self, schema: str) -> list[IndexDefinition]:
        """Parse all index blocks from the DSL string."""
        indexes: list[IndexDefinition] = []

        for block in _STATEMENT_SPLIT.split(schema):
            head = re.match(r"schema\.(vertex|edge)Label\('([^']+)'\)", block)
            if not head:
                continue
            element, label_name = head.group(1), head.group(2)

            mv = re.search(r"\.materializedView\('([^']+)'\)", block)
            if mv:
                if element == "edge":
                    # Edge MVs partition/cluster by the endpoint vertices (OUT/IN, 'vertex_key'); the keys that
                    # matter for a JanusGraph edge index are the plain edge-property columns, e.g.
                    # clusterBy('since', Asc). Endpoint columns are vertex properties and are ignored.
                    keys = [
                        IndexKey(name=km.group(1))
                        for km in re.finditer(r"\.(?:partitionBy|clusterBy)\('([^']+)'(?:,\s*\w+)?\)", block)
                    ]
                else:
                    keys = [IndexKey(name=km.group(1)) for km in re.finditer(r"\.partitionBy\('([^']+)'\)", block)]
                indexes.append(
                    IndexDefinition(
                        kind="materializedView",
                        name=mv.group(1),
                        element=element,
                        label=label_name,
                        keys=keys,
                        inverse=".inverse()" in block,
                    )
                )
            elif element == "vertex" and ".searchIndex()" in block:
                search_keys: list[IndexKey] = []
                for bm in re.finditer(r"\.by\('([^']+)'\)(?:\.(asText|asString)\(\))?", block):
                    search_keys.append(IndexKey(name=bm.group(1), as_mapping=bm.group(2) or None))
                indexes.append(
                    IndexDefinition(
                        kind="searchIndex",
                        name=f"{label_name}_search",
                        element="vertex",
                        label=label_name,
                        keys=search_keys,
                    )
                )
            elif element == "vertex":
                sec = re.search(r"\.secondaryIndex\('([^']+)'\)", block)
                if sec:
                    indexes.append(
                        IndexDefinition(
                            kind="secondaryIndex",
                            name=sec.group(1),
                            element="vertex",
                            label=label_name,
                            keys=[IndexKey(name=bm.group(1)) for bm in re.finditer(r"\.by\('([^']+)'\)", block)],
                        )
                    )

        return indexes

    def _parse_partition_keys(self, block: str) -> list[PartitionKey]:
        return [
            PartitionKey(name=m.group(1), type=m.group(2))
            for m in re.finditer(r"\.partitionBy\('([^']+)',\s*(\w+)\)", block)
        ]

    def _parse_clustering_keys(self, block: str) -> list[ClusteringKey]:
        return [
            ClusteringKey(
                name=m.group(1),
                type=m.group(2),
                order=m.group(3) if m.group(3) else "ASC",
            )
            for m in re.finditer(r"\.clusterBy\('([^']+)',\s*(\w+)(?:,\s*(Asc|Desc))?\)", block)
        ]

    def _parse_properties(self, block: str) -> list[PropertyDefinition]:
        props: list[PropertyDefinition] = []
        for m in _PROPERTY_RE.finditer(block):
            name = m.group(1)
            raw_type = m.group(2).strip()
            cardinality = "SINGLE"
            collection: str | None = None
            map_key: str | None = None
            map_value: str | None = None

            if raw_type.startswith("setOf("):
                cardinality = "SET"
                collection = "set"
                type_str = raw_type[len("setOf(") : -1].strip()
            elif raw_type.startswith("listOf("):
                cardinality = "LIST"
                collection = "list"
                type_str = raw_type[len("listOf(") : -1].strip()
            elif raw_type.startswith("mapOf("):
                cardinality = "LIST"
                collection = "map"
                type_str = raw_type
                parts = [p.strip() for p in raw_type[len("mapOf(") : -1].split(",", 1)]
                if len(parts) == 2:
                    map_key, map_value = parts
            else:
                type_str = raw_type

            props.append(
                PropertyDefinition(
                    name=name,
                    type=type_str,
                    cardinality=cardinality,
                    collection=collection,
                    map_key_type=map_key,
                    map_value_type=map_value,
                )
            )
        return props

    # ──────────────────────────────────────────────────────────────────────────
    # Count enrichment
    # ──────────────────────────────────────────────────────────────────────────

    def _enrich_counts_from_gremlin(self, result: AnalysisResult) -> None:
        """Enrich vertex/edge counts using Gremlin count queries (Classic fallback)."""
        for vl in result.vertex_labels:
            try:
                counts = self._gremlin_submit(
                    "g.V().hasLabel(lbl).count()",
                    {"lbl": vl.label},
                )
                if counts:
                    vl.count = int(counts[0])
            except Exception:  # noqa: BLE001, S110
                pass

        for el in result.edge_labels:
            try:
                counts = self._gremlin_submit(
                    "g.E().hasLabel(lbl).count()",
                    {"lbl": el.label},
                )
                if counts:
                    el.count = int(counts[0])
            except Exception:  # noqa: BLE001, S110
                pass

    @staticmethod
    def edge_table_name(from_label: str, label: str, to_label: str) -> str:
        """DSG edge table name, verified against DSE 6.8: ``<from>__<label>__<to>``."""
        return f"{from_label}__{label}__{to_label}"

    def _enrich_counts_from_cql(self, result: AnalysisResult) -> None:
        """Enrich vertex/edge counts via CQL COUNT queries (faster for DSG).

        Also records each edge label's CQL ``table_name`` for the migrator.
        """
        try:
            from cassandra.cluster import Cluster

            cluster = Cluster([self.cql_host], port=self.cql_port)
            session = cluster.connect()
            keyspace = self.graph_name

            for vl in result.vertex_labels:
                try:
                    rows = session.execute(
                        f"SELECT count(*) FROM {keyspace}.{vl.label}"  # noqa: S608
                    )
                    row = next(iter(rows), None)
                    if row:
                        vl.count = int(row[0])
                except Exception:  # noqa: BLE001, S110
                    pass

            for el in result.edge_labels:
                el.table_name = self.edge_table_name(el.from_label, el.label, el.to_label)
                try:
                    rows = session.execute(
                        f"SELECT count(*) FROM {keyspace}.{el.table_name}"  # noqa: S608
                    )
                    row = next(iter(rows), None)
                    if row:
                        el.count = int(row[0])
                except Exception:  # noqa: BLE001, S110
                    pass

            cluster.shutdown()
        except Exception:  # noqa: BLE001, S110
            self._enrich_counts_from_gremlin(result)

    # ──────────────────────────────────────────────────────────────────────────
    # JSON output
    # ──────────────────────────────────────────────────────────────────────────

    def _write_json(self, result: AnalysisResult) -> str:
        path = os.path.join(self.output_dir, f"{self.graph_name}-analysis.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2)
        return path
