"""DSE Graph → JanusGraph schema translator.

Pure data transformation — no network calls. Consumes an AnalysisResult and
produces a TranslationResult (JanusGraph JSON schema + translation notes).
"""

from __future__ import annotations

import json
import os
from typing import Any

from dse_jg_migrator.models import (
    AnalysisResult,
    IndexDefinition,
    JGCompositeIndex,
    JGEdgeLabel,
    JGIndexKey,
    JGMixedIndex,
    JGPropertyKey,
    JGVertexCentricIndex,
    JGVertexLabel,
    PropertyDefinition,
    TranslationResult,
    UDTDefinition,
)

# ---------------------------------------------------------------------------
# Type mapping table (DSE → JanusGraph Java class name)
# ---------------------------------------------------------------------------

TYPE_MAP: dict[str, str] = {
    "Text": "java.lang.String",
    "Varchar": "java.lang.String",
    "Ascii": "java.lang.String",
    "Int": "java.lang.Integer",
    "Integer": "java.lang.Integer",
    "Bigint": "java.lang.Long",
    "Long": "java.lang.Long",
    "Smallint": "java.lang.Short",
    "Tinyint": "java.lang.Byte",
    "Boolean": "java.lang.Boolean",
    "Float": "java.lang.Float",
    "Double": "java.lang.Double",
    "Decimal": "java.math.BigDecimal",
    "Varint": "java.math.BigInteger",
    "Uuid": "java.util.UUID",
    "Timeuuid": "java.util.UUID",
    "Timestamp": "java.util.Date",
    "Date": "java.time.LocalDate",
    "Time": "java.time.LocalTime",
    "Instant": "java.util.Date",
    "Blob": "byte[]",
    "Inet": "java.net.InetAddress",
    "Duration": "java.time.Duration",
    # Geospatial — stored as WKT strings; geo mixed index auto-generated for Point
    "Point": "java.lang.String",
    "Polygon": "java.lang.String",
    "Linestring": "java.lang.String",
}

GEO_TYPES = {"Point", "Polygon", "Linestring"}
VERTEX_TYPE_CLASS = "org.apache.tinkerpop.gremlin.structure.Vertex"
EDGE_TYPE_CLASS = "org.apache.tinkerpop.gremlin.structure.Edge"


def map_type(dse_type: str) -> str:
    """Map a DSE type string to a JanusGraph Java class name.

    Args:
        dse_type: DSE type string (e.g. "Text", "Uuid", "setOf(Text)").

    Returns:
        Java class name string.
    """
    return TYPE_MAP.get(dse_type, "java.lang.String")


class Translator:
    """Translates a DSE Graph AnalysisResult into a JanusGraph TranslationResult.

    No network I/O — pure in-memory transformation.
    """

    def __init__(
        self,
        analysis: AnalysisResult,
        udt_strategy: str = "flat",
        map_strategy: str = "flat",
        output_dir: str = ".",
    ) -> None:
        """
        Args:
            analysis: The AnalysisResult to translate.
            udt_strategy: 'flat' (expand UDT fields) or 'json' (single JSON string).
            map_strategy: 'flat' (parallel _key/_value LIST) or 'json' (JSON string).
            output_dir: Directory to write output files.
        """
        self.analysis = analysis
        self.udt_strategy = udt_strategy
        self.map_strategy = map_strategy
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> TranslationResult:
        """Execute the full translation and write output files."""
        result = TranslationResult()
        an = self.analysis

        # Collect all seen property key names to avoid duplicates
        seen_keys: dict[str, JGPropertyKey] = {}

        # 1. Translate vertex labels and their property keys
        for vl in an.vertex_labels:
            result.vertexLabels.append(JGVertexLabel(label=vl.label))
            for pk in vl.partition_keys:
                self._add_property_key(seen_keys, pk.name, pk.type, "SINGLE", result)
            for ck in vl.clustering_keys:
                self._add_property_key(seen_keys, ck.name, ck.type, "SINGLE", result)
            for prop in vl.properties:
                self._translate_property(prop, seen_keys, result, vl.label)

        # 2. Translate edge labels and their property keys
        for el in an.edge_labels:
            result.edgeLabels.append(JGEdgeLabel(label=el.label))
            for ep in el.properties:
                self._translate_property(ep, seen_keys, result)

        # 3. UDTs — expand or serialise
        for udt in an.udts:
            self._translate_udt(udt, seen_keys, result)

        # Flush accumulated property keys
        result.propertyKeys = list(seen_keys.values())

        # 4. Translate indexes
        self._translate_indexes(an.indexes, seen_keys, result)

        # Write output
        self._write_schema_json(result)
        self._write_translation_report(result)
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Property key helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _add_property_key(
        self,
        seen: dict[str, JGPropertyKey],
        name: str,
        dse_type: str,
        cardinality: str,
        result: TranslationResult,
    ) -> None:
        if name not in seen:
            jg_type = map_type(dse_type)
            seen[name] = JGPropertyKey(key=name, className=jg_type, cardinality=cardinality)
            if dse_type == "Point":
                result.translation_notes.append(
                    f"Property '{name}' (Point): stored as WKT string; "
                    "geo mixed index with Mapping.PREFIX_TREE auto-generated."
                )
            elif dse_type in ("Polygon", "Linestring"):
                result.translation_notes.append(
                    f"Property '{name}' ({dse_type}): stored as WKT string; "
                    "geo predicates require manual mixed index configuration."
                )

    def _translate_property(
        self,
        prop: PropertyDefinition,
        seen: dict[str, JGPropertyKey],
        result: TranslationResult,
        vertex_label: str | None = None,
    ) -> None:
        """Translate a single property definition to a JanusGraph property key."""
        if prop.collection == "map":
            # mapOf(K,V) — flat or json strategy
            if self.map_strategy == "json":
                self._add_property_key(seen, prop.name, "Text", "SINGLE", result)
                result.translation_notes.append(
                    f"Property '{prop.name}' (mapOf): json strategy — stored as JSON string."
                )
            else:
                key_name = f"{prop.name}_key"
                val_name = f"{prop.name}_value"
                self._add_property_key(seen, key_name, "Text", "LIST", result)
                self._add_property_key(seen, val_name, "Text", "LIST", result)
                result.translation_notes.append(
                    f"Property '{prop.name}' (mapOf): flat strategy — "
                    f"expanded to '{key_name}' LIST + '{val_name}' LIST."
                )
        else:
            cardinality = prop.cardinality or "SINGLE"
            self._add_property_key(seen, prop.name, prop.type, cardinality, result)

        # Auto-generate geo mixed index for Point properties on vertex labels
        if prop.type == "Point" and vertex_label:
            geo_idx_name = f"{vertex_label}_geo_{prop.name}"
            result.mixedIndexes.append(
                JGMixedIndex(
                    name=geo_idx_name,
                    typeClass=VERTEX_TYPE_CLASS,
                    indexOnly=vertex_label,
                    indexBackend="search",
                    keys=[
                        JGIndexKey(
                            propertyKey=prop.name,
                            parameters=[
                                {
                                    "key": "mapping",
                                    "value": "org.janusgraph.core.schema.Mapping.PREFIX_TREE",
                                    "parser": "enum",
                                }
                            ],
                        )
                    ],
                )
            )

    # ──────────────────────────────────────────────────────────────────────────
    # UDT translation
    # ──────────────────────────────────────────────────────────────────────────

    def _translate_udt(
        self,
        udt: UDTDefinition,
        seen: dict[str, JGPropertyKey],
        result: TranslationResult,
    ) -> None:
        """Expand or serialise a UDT definition."""
        if self.udt_strategy == "json":
            self._add_property_key(seen, udt.name, "Text", "SINGLE", result)
            result.translation_notes.append(
                f"UDT '{udt.name}': json strategy — serialised to a single JSON string property."
            )
        else:
            for field in udt.fields:
                flat_name = f"{udt.name}_{field.name}"
                self._add_property_key(seen, flat_name, field.type, "SINGLE", result)
            result.translation_notes.append(
                f"UDT '{udt.name}': flat strategy — expanded to "
                + ", ".join(f"'{udt.name}_{f.name}'" for f in udt.fields)
                + "."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Index translation
    # ──────────────────────────────────────────────────────────────────────────

    def _translate_indexes(
        self,
        indexes: list[IndexDefinition],
        seen: dict[str, JGPropertyKey],
        result: TranslationResult,
    ) -> None:
        for idx in indexes:
            if idx.kind == "materializedView":
                self._translate_mv_index(idx, result)
            elif idx.kind == "searchIndex":
                self._translate_search_index(idx, result)
            elif idx.kind == "secondaryIndex":
                self._translate_secondary_index(idx, result)
            elif idx.kind == "vertexCentric":
                self._translate_vertex_centric_index(idx, result)

    def _translate_mv_index(self, idx: IndexDefinition, result: TranslationResult) -> None:
        if idx.inverse:
            # Inverse MV → vertex-centric edge index with Direction.IN
            result.vertexCentricEdgeIndexes.append(
                JGVertexCentricIndex(
                    name=idx.name,
                    indexedEdgeLabel=idx.label,
                    direction="IN",
                    propertyKeys=[k.name for k in idx.keys],
                )
            )
        else:
            type_class = EDGE_TYPE_CLASS if idx.element == "edge" else VERTEX_TYPE_CLASS
            result.compositeIndexes.append(
                JGCompositeIndex(
                    name=idx.name,
                    typeClass=type_class,
                    indexOnly=idx.label,
                    keys=[JGIndexKey(propertyKey=k.name) for k in idx.keys],
                )
            )

    def _translate_search_index(self, idx: IndexDefinition, result: TranslationResult) -> None:
        jg_keys: list[JGIndexKey] = []
        for k in idx.keys:
            params: list[dict[str, str]] = []
            if k.as_mapping == "asText":
                params = [
                    {
                        "key": "mapping",
                        "value": "org.janusgraph.core.schema.Mapping.TEXT",
                        "parser": "enum",
                    }
                ]
            elif k.as_mapping == "asString":
                params = [
                    {
                        "key": "mapping",
                        "value": "org.janusgraph.core.schema.Mapping.STRING",
                        "parser": "enum",
                    }
                ]
            jg_keys.append(JGIndexKey(propertyKey=k.name, parameters=params))
        result.mixedIndexes.append(
            JGMixedIndex(
                name=idx.name,
                typeClass=VERTEX_TYPE_CLASS,
                indexOnly=idx.label,
                indexBackend="search",
                keys=jg_keys,
            )
        )

    def _translate_secondary_index(self, idx: IndexDefinition, result: TranslationResult) -> None:
        result.compositeIndexes.append(
            JGCompositeIndex(
                name=idx.name,
                typeClass=VERTEX_TYPE_CLASS,
                indexOnly=idx.label,
                keys=[JGIndexKey(propertyKey=k.name) for k in idx.keys],
            )
        )

    def _translate_vertex_centric_index(
        self, idx: IndexDefinition, result: TranslationResult
    ) -> None:
        result.vertexCentricEdgeIndexes.append(
            JGVertexCentricIndex(
                name=idx.name,
                indexedEdgeLabel=idx.label,
                direction=idx.direction or "BOTH",
                propertyKeys=[k.name for k in idx.keys],
                order=idx.order or "asc",
            )
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Output helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _write_schema_json(self, result: TranslationResult) -> str:
        path = os.path.join(
            self.output_dir, f"{self.analysis.graph_name}-janusgraph-schema.json"
        )
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result.to_schema_dict(), fh, indent=2)
        return path

    def _write_translation_report(self, result: TranslationResult) -> str:
        an = self.analysis
        lines = [
            f"# Translation Report: {an.graph_name} → JanusGraph",
            "",
            f"**UDT strategy:** {self.udt_strategy}",
            f"**Map strategy:** {self.map_strategy}",
            "",
            "## Vertex Labels",
            "",
            "| DSE Label | JanusGraph Label |",
            "|-----------|-----------------|",
        ]
        for vl in result.vertexLabels:
            lines.append(f"| {vl.label} | {vl.label} |")
        lines += [
            "",
            "## Property Keys",
            "",
            "| Key | JanusGraph Class | Cardinality |",
            "|-----|-----------------|-------------|",
        ]
        for pk in result.propertyKeys:
            lines.append(f"| {pk.key} | {pk.className} | {pk.cardinality} |")
        lines += [
            "",
            "## Indexes",
            "",
            "| Name | Kind | Label |",
            "|------|------|-------|",
        ]
        for ci in result.compositeIndexes:
            lines.append(f"| {ci.name} | composite | {ci.indexOnly} |")
        for mi in result.mixedIndexes:
            lines.append(f"| {mi.name} | mixed | {mi.indexOnly} |")
        for vc in result.vertexCentricEdgeIndexes:
            lines.append(f"| {vc.name} | vertex-centric | {vc.indexedEdgeLabel} |")
        if result.translation_notes:
            lines += ["", "## Notes", ""]
            for note in result.translation_notes:
                lines.append(f"- {note}")

        path = os.path.join(self.output_dir, f"{an.graph_name}-translation-report.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path

    def load_analysis(self, json_path: str) -> AnalysisResult:
        """Load an AnalysisResult from a previously-written analysis JSON file."""
        with open(json_path, encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
        # Simple reconstruction — sufficient for translator use
        from dse_jg_migrator.models import GraphConfig  # local to avoid circular

        an = AnalysisResult(
            graph_name=data.get("graph_name", ""),
            engine=data.get("engine", "dsg"),
            extracted_at=data.get("extracted_at", ""),
        )
        from dse_jg_migrator.models import (
            EdgeLabelDefinition,
            IndexDefinition,
            IndexKey,
            PartitionKey,
            PropertyDefinition,
            UDTDefinition,
            UDTField,
            VertexLabelDefinition,
        )

        for vl_d in data.get("vertex_labels", []):
            vl = VertexLabelDefinition(label=vl_d["label"])
            vl.partition_keys = [PartitionKey(**pk) for pk in vl_d.get("partition_keys", [])]
            vl.properties = [
                PropertyDefinition(**p) for p in vl_d.get("properties", [])
            ]
            an.vertex_labels.append(vl)
        for el_d in data.get("edge_labels", []):
            el = EdgeLabelDefinition(
                label=el_d["label"],
                from_label=el_d.get("from_label", ""),
                to_label=el_d.get("to_label", ""),
            )
            el.properties = [PropertyDefinition(**p) for p in el_d.get("properties", [])]
            an.edge_labels.append(el)
        for udt_d in data.get("udts", []):
            udt = UDTDefinition(name=udt_d["name"])
            udt.fields = [UDTField(**f) for f in udt_d.get("fields", [])]
            an.udts.append(udt)
        for idx_d in data.get("indexes", []):
            idx = IndexDefinition(
                kind=idx_d["kind"],
                name=idx_d["name"],
                element=idx_d.get("element", "vertex"),
                label=idx_d["label"],
            )
            idx.keys = [IndexKey(**k) for k in idx_d.get("keys", [])]
            idx.inverse = idx_d.get("inverse", False)
            an.indexes.append(idx)
        config_d = data.get("config", {})
        an.config = GraphConfig(
            schema_mode=config_d.get("schema_mode", "Development"),
            allow_scan=config_d.get("allow_scan", True),
            replication=config_d.get("replication", ""),
        )
        return an
