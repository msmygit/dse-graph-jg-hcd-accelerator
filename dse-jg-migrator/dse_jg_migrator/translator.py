"""DSE Graph → JanusGraph schema translator.

Pure data transformation — no network calls. Consumes an AnalysisResult and
produces a TranslationResult (JanusGraph JSON schema + translation notes).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from dse_jg_migrator.models import (
    AnalysisResult,
    IndexDefinition,
    IndexKey,
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

# JanusGraph only accepts a fixed set of property data types. LocalDate, LocalTime, BigDecimal, BigInteger
# and InetAddress are rejected by the JanusGraph server ("Not a supported data type"), so the types that
# have no native equivalent are mapped to the closest supported one (verified against a live JanusGraph).
# Every target must be a type documented at https://docs.janusgraph.org/schema/#property-key-data-type
# (or a primitive array) — java.time.Duration/Instant work only because they are registered internally.
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
    "Decimal": "java.lang.Double",  # --decimal-strategy double (default); see resolve_target_class()
    "Varint": "java.lang.String",  # BigInteger is unsupported; decimal string is lossless
    "Uuid": "java.util.UUID",
    "Timeuuid": "java.util.UUID",
    "Timestamp": "java.util.Date",
    "Date": "java.util.Date",  # LocalDate is unsupported; stored as midnight UTC
    "Time": "java.lang.String",  # LocalTime is unsupported; ISO-8601 string
    "Instant": "java.util.Date",
    "Blob": "byte[]",
    "Inet": "java.lang.String",  # InetAddress is unsupported; textual address
    "Duration": "java.lang.Long",  # Classic stores millis; Core overrides to ISO-8601 String
    # Geospatial — Point becomes a Geoshape (required for a PREFIX_TREE mixed index); the migrator converts
    # DSE WKT to Geoshape. Polygon/Linestring are kept as WKT strings.
    "Point": "org.janusgraph.core.attribute.Geoshape",
    "Polygon": "java.lang.String",
    "Linestring": "java.lang.String",
}

# Core (DSG) stores Duration as CQL duration(months, days, nanos) — calendar units with no fixed length — so no
# numeric type holds it exactly (bdp/dse-graph-server .../schema/Column.java). Classic stores millis.
ENGINE_TYPE_OVERRIDES: dict[str, dict[str, str]] = {"dsg": {"Duration": "java.lang.String"}}

# --decimal-strategy: JanusGraph has no BigDecimal type, so Decimal is either a numeric Double or an exact String.
DECIMAL_STRATEGIES: dict[str, str] = {"double": "java.lang.Double", "string": "java.lang.String"}

# DSE types whose JanusGraph mapping changes representation or loses information.
TYPE_MAP_NOTES: dict[str, str] = {
    "Varint": "stored as a decimal java.lang.String — no numeric comparison",
    "Date": "stored as java.util.Date at midnight UTC — JanusGraph rejects LocalDate",
    "Time": "stored as an ISO-8601 java.lang.String — JanusGraph rejects LocalTime",
    "Inet": "stored as a textual java.lang.String — JanusGraph rejects InetAddress",
}

# Notes for types whose mapping depends on --decimal-strategy (Decimal) or on the source engine (Duration).
_VARIANT_NOTES: dict[tuple[str, str], str] = {
    ("Decimal", "double"): (
        "stored as java.lang.Double — values beyond ~15 significant digits are inexact; "
        "the migrator counts them (LOSSY_DECIMAL). Use --decimal-strategy string for exact values"
    ),
    ("Decimal", "string"): "stored as an exact decimal java.lang.String — no numeric comparison",
    ("Duration", "classic"): "stored as java.lang.Long milliseconds — java.time.Duration is not a documented type",
    ("Duration", "dsg"): (
        "stored as an ISO-8601 java.lang.String (CQL duration has calendar months/days) — no duration comparison"
    ),
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


def resolve_target_class(dse_type: str, engine: str = "dsg", decimal_strategy: str = "double") -> str:
    """Resolve the JanusGraph class for a DSE type, given the source engine and the Decimal strategy.

    Single source of truth shared by the translator (schema) and the migrator (value conversion).
    """
    if dse_type == "Decimal":
        return DECIMAL_STRATEGIES[decimal_strategy]
    return ENGINE_TYPE_OVERRIDES.get(engine, {}).get(dse_type) or map_type(dse_type)


_UDT_REFERENCE = re.compile(r"^(?:frozen\()?typeOf\('(\w+)'\)\)?$")


def udt_reference(dse_type: str) -> str | None:
    """Return the UDT name a property type refers to (``frozen(typeOf('address'))`` -> ``address``), else None.

    Shared by the translator (no key for the property itself; the UDT's ``<udt>_<field>`` / ``<udt>`` keys hold
    its data) and the migrator (which writes those keys).
    """
    match = _UDT_REFERENCE.match(dse_type.replace(" ", ""))
    return match.group(1) if match else None


def is_tuple_type(dse_type: str) -> bool:
    """True for a ``tupleOf(...)`` property type — stored as a JSON array String (rule SC-006)."""
    return dse_type.replace(" ", "").startswith("tupleOf(")


def type_note(dse_type: str, engine: str = "dsg", decimal_strategy: str = "double") -> str | None:
    """Return the translation note for a DSE type whose mapping is lossy or changes representation."""
    variant = decimal_strategy if dse_type == "Decimal" else engine
    return _VARIANT_NOTES.get((dse_type, variant)) or TYPE_MAP_NOTES.get(dse_type)


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
        decimal_strategy: str = "double",
    ) -> None:
        """
        Args:
            analysis: The AnalysisResult to translate.
            udt_strategy: 'flat' (expand UDT fields) or 'json' (single JSON string).
            map_strategy: 'flat' (parallel _key/_value LIST) or 'json' (JSON string).
            output_dir: Directory to write output files.
            decimal_strategy: 'double' (numeric, lossy beyond ~15 digits) or 'string' (exact).
        """
        self.analysis = analysis
        self.udt_strategy = udt_strategy
        self.map_strategy = map_strategy
        self.decimal_strategy = decimal_strategy
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
            engine = self.analysis.engine
            jg_type = resolve_target_class(dse_type, engine, self.decimal_strategy)
            seen[name] = JGPropertyKey(key=name, className=jg_type, cardinality=cardinality)
            note = type_note(dse_type, engine, self.decimal_strategy)
            if note:
                result.translation_notes.append(f"Property '{name}' ({dse_type}): {note}.")
            if dse_type == "Point":
                result.translation_notes.append(
                    f"Property '{name}' (Point): stored as Geoshape (migrator converts from WKT); "
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
        elif (udt := udt_reference(prop.type)) is not None:
            # The UDT's own keys (``<udt>_<field>`` flat, ``<udt>`` json) hold the data — see _translate_udt.
            result.translation_notes.append(
                f"Property '{prop.name}' (UDT '{udt}'): {self.udt_strategy} strategy — written to the UDT's keys."
            )
        else:
            cardinality = prop.cardinality or "SINGLE"
            self._add_property_key(seen, prop.name, prop.type, cardinality, result)
            if is_tuple_type(prop.type):
                result.translation_notes.append(
                    f"Property '{prop.name}' ({prop.type}): stored as a JSON array java.lang.String (SC-006) — "
                    "reconstruct the tuple in the application."
                )

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

    def _edge_index_keys(self, idx: IndexDefinition, result: TranslationResult) -> list[IndexKey]:
        """Return an edge MV's edge-property keys, noting when there are none (so no index is created).

        A JanusGraph edge index can only key on edge properties. DSE edge MVs also partition by the endpoint
        vertex keys, and ``inverse()`` MVs index by the IN vertex only — JanusGraph stores every edge in both
        vertices' adjacency lists, so those lookups need no index (an empty vertex-centric index is rejected:
        "Need to specify sort keys", found live).
        """
        edge_props = {p.name for el in self.analysis.edge_labels if el.label == idx.label for p in el.properties}
        keys = [k for k in idx.keys if k.name in edge_props]
        if not keys:
            result.translation_notes.append(
                f"Edge MV '{idx.name}' on '{idx.label}': no edge-property keys, so no JanusGraph index is "
                "created (endpoint lookups in either direction use adjacency lists)."
            )
        return keys

    def _translate_mv_index(self, idx: IndexDefinition, result: TranslationResult) -> None:
        if idx.inverse:
            # Inverse MV → vertex-centric edge index with Direction.IN, sorted by its edge-property keys
            keys = self._edge_index_keys(idx, result)
            if keys:
                result.vertexCentricEdgeIndexes.append(
                    JGVertexCentricIndex(
                        name=idx.name,
                        indexedEdgeLabel=idx.label,
                        direction="IN",
                        propertyKeys=[k.name for k in keys],
                    )
                )
        else:
            keys = idx.keys
            if idx.element == "edge":
                keys = self._edge_index_keys(idx, result)
                if not keys:
                    return
            type_class = EDGE_TYPE_CLASS if idx.element == "edge" else VERTEX_TYPE_CLASS
            result.compositeIndexes.append(
                JGCompositeIndex(
                    name=idx.name,
                    typeClass=type_class,
                    indexOnly=idx.label,
                    keys=[JGIndexKey(propertyKey=k.name) for k in keys],
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

    def _translate_vertex_centric_index(self, idx: IndexDefinition, result: TranslationResult) -> None:
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
        path = os.path.join(self.output_dir, f"{self.analysis.graph_name}-janusgraph-schema.json")
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
            f"**Decimal strategy:** {self.decimal_strategy}",
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
        """Load an AnalysisResult from a previously-written analysis JSON file (``AnalysisResult.from_dict``)."""
        with open(json_path, encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
        return AnalysisResult.from_dict(data)
