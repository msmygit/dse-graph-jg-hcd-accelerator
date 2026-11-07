"""DSE Graph schema analyser — read-only extraction of schema, indexes, and statistics."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

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
    VertexLabelDefinition,
)


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
        from gremlin_python.driver import client as gremlin_client

        kwargs: dict[str, Any] = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password

        c = gremlin_client.Client(self.gremlin_url, "g", **kwargs)
        try:
            return c.submit(script, bindings or {}).all().result()
        finally:
            c.close()

    def _detect_engine(self) -> str:
        """Detect engine type ('classic' or 'dsg') via Gremlin system.graph().describe()."""
        try:
            desc = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            if "Core" in str(desc) or "DSG" in str(desc):
                return "dsg"
        except Exception:  # noqa: BLE001, S110
            pass
        return "dsg"

    def _extract_raw_schema(self) -> str:
        """Fetch the full schema DSL string from DSE Graph via schema.describe()."""
        try:
            parts = self._gremlin_submit("schema.describe()")
            return "\n".join(str(p) for p in (parts or []))
        except Exception:  # noqa: BLE001, S110
            return ""

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
        except Exception:  # noqa: BLE001, S110
            pass
        return config

    # ──────────────────────────────────────────────────────────────────────────
    # DSL parser helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_vertex_labels(self, schema: str) -> list[VertexLabelDefinition]:
        """Parse schema.vertexLabel() blocks from the DSL string.

        Only includes primary label definition blocks (those containing
        .partitionBy() or .property() after the label name), not index
        definition blocks like materializedView() or searchIndex().
        """
        labels: list[VertexLabelDefinition] = []
        seen: set[str] = set()

        # Match each vertexLabel block (up to the next schema. call or end)
        blocks = re.split(r"(?=schema\.vertexLabel\()", schema)
        for block in blocks:
            if not block.startswith("schema.vertexLabel("):
                continue
            # Skip index-definition blocks (materializedView, searchIndex, secondaryIndex)
            index_keywords = ("materializedView(", "searchIndex(", "secondaryIndex(")
            if any(kw in block for kw in index_keywords):
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
        blocks = re.split(r"(?=schema\.edgeLabel\()", schema)
        for block in blocks:
            if not block.startswith("schema.edgeLabel("):
                continue
            m = re.match(r"schema\.edgeLabel\('([^']+)'\)", block)
            if not m:
                continue
            label_name = m.group(1)
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
        blocks = re.split(r"(?=schema\.type\()", schema)
        for block in blocks:
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

        # materializedView indexes
        for m in re.finditer(
            r"schema\.(?:vertex|edge)Label\('([^']+)'\)\."
            r"materializedView\('([^']+)'\)([^;]*?)\.create\(\)",
            schema,
            re.DOTALL,
        ):
            label_name, idx_name, body = m.group(1), m.group(2), m.group(3)
            keys = [
                IndexKey(name=km.group(1))
                for km in re.finditer(r"\.partitionBy\('([^']+)'\)", body)
            ]
            inverse = ".inverse()" in body
            element = "edge" if "edgeLabel" in m.group(0) else "vertex"
            indexes.append(
                IndexDefinition(
                    kind="materializedView",
                    name=idx_name,
                    element=element,
                    label=label_name,
                    keys=keys,
                    inverse=inverse,
                )
            )

        # searchIndex
        for m in re.finditer(
            r"schema\.vertexLabel\('([^']+)'\)\.searchIndex\(\)([^;]*?)\.create\(\)",
            schema,
            re.DOTALL,
        ):
            label_name, body = m.group(1), m.group(2)
            search_keys: list[IndexKey] = []
            for bm in re.finditer(r"\.by\('([^']+)'\)(?:\.(asText|asString)\(\))?", body):
                prop_name = bm.group(1)
                as_type = bm.group(2) if bm.group(2) else None
                search_keys.append(IndexKey(name=prop_name, as_mapping=as_type))
            idx_name = f"{label_name}_search"
            indexes.append(
                IndexDefinition(
                    kind="searchIndex",
                    name=idx_name,
                    element="vertex",
                    label=label_name,
                    keys=search_keys,
                )
            )

        # secondaryIndex
        for m in re.finditer(
            r"schema\.vertexLabel\('([^']+)'\)\.secondaryIndex\('([^']+)'\)([^;]*?)\.create\(\)",
            schema,
            re.DOTALL,
        ):
            label_name, idx_name, body = m.group(1), m.group(2), m.group(3)
            keys = [
                IndexKey(name=bm.group(1))
                for bm in re.finditer(r"\.by\('([^']+)'\)", body)
            ]
            indexes.append(
                IndexDefinition(
                    kind="secondaryIndex",
                    name=idx_name,
                    element="vertex",
                    label=label_name,
                    keys=keys,
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
        for m in re.finditer(r"\.property\('([^']+)',\s*([^)]+)\)", block):
            name = m.group(1)
            raw_type = m.group(2).strip()
            cardinality = "SINGLE"
            collection: str | None = None

            if raw_type.startswith("setOf("):
                cardinality = "SET"
                collection = "set"
                inner = re.search(r"setOf\((\w+)\)", raw_type)
                type_str = inner.group(1) if inner else "Text"
            elif raw_type.startswith("listOf("):
                cardinality = "LIST"
                collection = "list"
                inner = re.search(r"listOf\((\w+)\)", raw_type)
                type_str = inner.group(1) if inner else "Text"
            elif raw_type.startswith("mapOf("):
                cardinality = "LIST"
                collection = "map"
                type_str = raw_type
            else:
                type_str = raw_type

            props.append(
                PropertyDefinition(
                    name=name,
                    type=type_str,
                    cardinality=cardinality,
                    collection=collection,
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

    def _enrich_counts_from_cql(self, result: AnalysisResult) -> None:
        """Enrich vertex/edge counts via CQL COUNT queries (faster for DSG)."""
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
