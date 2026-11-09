# ruff: noqa: N815  — JanusGraph JSON schema uses camelCase field names (API contract)
"""Pydantic / dataclass models shared across all modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

# ---------------------------------------------------------------------------
# Schema models (populated by analyser.py)
# ---------------------------------------------------------------------------


@dataclass
class PropertyDefinition:
    """A single property key definition extracted from DSE Graph schema."""

    name: str
    type: str  # DSE type string, e.g. "Text", "Uuid", "setOf(Text)"
    cardinality: str = "SINGLE"  # SINGLE | SET | LIST
    collection: str | None = None  # "set" | "list" | "map" | None
    map_key_type: str | None = None  # key type for mapOf(K,V)
    map_value_type: str | None = None  # value type for mapOf(K,V)


@dataclass
class PartitionKey:
    """A partition key column on a DSE vertex or edge label."""

    name: str
    type: str


@dataclass
class ClusteringKey:
    """A clustering key column on a DSE vertex label."""

    name: str
    type: str
    order: str = "ASC"  # ASC | DESC


@dataclass
class UDTField:
    """A single field within a User-Defined Type."""

    name: str
    type: str


@dataclass
class UDTDefinition:
    """A User-Defined Type (UDT) definition from DSE Graph."""

    name: str
    fields: list[UDTField] = field(default_factory=list)


@dataclass
class IndexKey:
    """A key entry within an index definition."""

    name: str
    as_mapping: str | None = None  # "text" | "string" | None (for geo)


@dataclass
class IndexDefinition:
    """An index definition extracted from DSE Graph schema."""

    kind: str  # "materializedView" | "searchIndex" | "secondaryIndex" | "vertexCentric"
    name: str
    element: str  # "vertex" | "edge"
    label: str  # vertex or edge label this index applies to
    keys: list[IndexKey] = field(default_factory=list)
    direction: str | None = None  # for vertexCentric: "BOTH" | "IN" | "OUT"
    order: str | None = None  # "asc" | "desc"
    inverse: bool = False  # for MV with .inverse()
    from_label: str | None = None  # for edge materializedViews
    to_label: str | None = None


@dataclass
class VertexLabelDefinition:
    """A vertex label definition extracted from DSE Graph schema."""

    label: str
    partition_keys: list[PartitionKey] = field(default_factory=list)
    clustering_keys: list[ClusteringKey] = field(default_factory=list)
    properties: list[PropertyDefinition] = field(default_factory=list)
    count: int = 0


@dataclass
class EdgeLabelDefinition:
    """An edge label definition extracted from DSE Graph schema."""

    label: str
    from_label: str = ""
    to_label: str = ""
    properties: list[PropertyDefinition] = field(default_factory=list)
    count: int = 0
    table_name: str = ""  # CQL table name (DSG convention)


@dataclass
class GraphConfig:
    """Graph-level configuration extracted from DSE."""

    schema_mode: str = "Development"
    allow_scan: bool = True
    replication: str = ""
    restrict_lambda: bool = False
    olap_enabled: bool = False
    rlac_enabled: bool = False


@dataclass
class AnalysisResult:
    """Full analysis result produced by analyser.py."""

    graph_name: str
    engine: str  # "classic" | "dsg"
    extracted_at: str
    vertex_labels: list[VertexLabelDefinition] = field(default_factory=list)
    edge_labels: list[EdgeLabelDefinition] = field(default_factory=list)
    udts: list[UDTDefinition] = field(default_factory=list)
    indexes: list[IndexDefinition] = field(default_factory=list)
    config: GraphConfig = field(default_factory=GraphConfig)

    def to_dict(self) -> dict[str, Any]:  # noqa: ANN401
        """Serialise to a plain dict suitable for JSON output."""
        import dataclasses

        def _convert(obj: Any) -> Any:  # noqa: ANN401
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {k: _convert(v) for k, v in dataclasses.asdict(obj).items()}
            if isinstance(obj, list):
                return [_convert(i) for i in obj]
            return obj

        return cast(dict[str, Any], _convert(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisResult:  # noqa: ANN401  # parsed JSON is heterogeneous
        """Rebuild an AnalysisResult from ``to_dict()`` output (the analyser's JSON) — its exact inverse."""
        return cls(
            graph_name=data.get("graph_name", ""),
            engine=data.get("engine", "dsg"),
            extracted_at=data.get("extracted_at", ""),
            vertex_labels=[
                VertexLabelDefinition(
                    label=v["label"],
                    partition_keys=[PartitionKey(**k) for k in v.get("partition_keys", [])],
                    clustering_keys=[ClusteringKey(**k) for k in v.get("clustering_keys", [])],
                    properties=[PropertyDefinition(**p) for p in v.get("properties", [])],
                    count=v.get("count", 0),
                )
                for v in data.get("vertex_labels", [])
            ],
            edge_labels=[
                EdgeLabelDefinition(**{**e, "properties": [PropertyDefinition(**p) for p in e.get("properties", [])]})
                for e in data.get("edge_labels", [])
            ],
            udts=[
                UDTDefinition(name=u["name"], fields=[UDTField(**f) for f in u.get("fields", [])])
                for u in data.get("udts", [])
            ],
            indexes=[
                IndexDefinition(**{"element": "vertex", **i, "keys": [IndexKey(**k) for k in i.get("keys", [])]})
                for i in data.get("indexes", [])
            ],
            config=GraphConfig(**data.get("config", {})),
        )


# ---------------------------------------------------------------------------
# Assessment models (populated by assessor.py)
# ---------------------------------------------------------------------------


@dataclass
class AssessmentFinding:
    """A single compatibility finding produced by the assessor."""

    rule_id: str
    category: str
    severity: str  # GREEN | YELLOW | RED
    title: str
    detail: str
    remediation: str
    docs_link: str


@dataclass
class IndexMapping:
    """One row of the assessment's Index Analysis: what a DSE index becomes on JanusGraph and what it needs."""

    dse_index: str  # DSE index name; "—" for an index the translator adds itself (geo index of a Point property)
    dse_kind: str  # materializedView | searchIndex | secondaryIndex | vertexCentric | Point property
    label: str
    keys: list[str]
    jg_index: str  # JanusGraph index name; "—" when no index is needed
    jg_kind: str  # composite | mixed | vertex-centric | none
    backend: str  # where JanusGraph keeps the index: the search backend (mixed) or the storage backend
    queries: str  # the DSE queries it serves and their JanusGraph predicates

    @property
    def needs_search_backend(self) -> bool:
        """Mixed indexes live in OpenSearch / Elasticsearch; every other kind lives in HCD."""
        return self.jg_kind == "mixed"


@dataclass
class AssessmentResult:
    """Full result of a pre-migration compatibility assessment."""

    graph_name: str
    engine: str  # "classic" | "dsg"
    assessed_at: str
    findings: list[AssessmentFinding] = field(default_factory=list)
    overall_risk: str = "LOW"  # LOW | MEDIUM | HIGH | CRITICAL (computed)
    summary: dict[str, Any] = field(default_factory=dict)
    cluster_overview: dict[str, Any] = field(default_factory=dict)
    index_analysis: list[IndexMapping] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Translation models (populated by translator.py)
# ---------------------------------------------------------------------------


@dataclass
class JGPropertyKey:
    """A JanusGraph property key definition."""

    key: str
    className: str
    cardinality: str = "SINGLE"


@dataclass
class JGVertexLabel:
    """A JanusGraph vertex label definition."""

    label: str
    staticVertex: bool = False
    partition: bool = False


@dataclass
class JGEdgeLabel:
    """A JanusGraph edge label definition."""

    label: str
    multiplicity: str = "MULTI"
    unidirected: bool = False


@dataclass
class JGIndexKey:
    """A key entry in a JanusGraph index."""

    propertyKey: str
    parameters: list[dict[str, str]] = field(default_factory=list)


@dataclass
class JGCompositeIndex:
    """A JanusGraph composite index definition."""

    name: str
    typeClass: str
    indexOnly: str
    unique: bool = False
    keys: list[JGIndexKey] = field(default_factory=list)


@dataclass
class JGMixedIndex:
    """A JanusGraph mixed index definition."""

    name: str
    typeClass: str
    indexOnly: str
    indexBackend: str = "search"
    keys: list[JGIndexKey] = field(default_factory=list)


@dataclass
class JGVertexCentricIndex:
    """A JanusGraph vertex-centric edge index definition."""

    name: str
    indexedEdgeLabel: str
    direction: str = "BOTH"
    propertyKeys: list[str] = field(default_factory=list)
    order: str = "asc"


@dataclass
class TranslationResult:
    """Full JanusGraph schema produced by translator.py."""

    propertyKeys: list[JGPropertyKey] = field(default_factory=list)
    vertexLabels: list[JGVertexLabel] = field(default_factory=list)
    edgeLabels: list[JGEdgeLabel] = field(default_factory=list)
    compositeIndexes: list[JGCompositeIndex] = field(default_factory=list)
    mixedIndexes: list[JGMixedIndex] = field(default_factory=list)
    vertexCentricEdgeIndexes: list[JGVertexCentricIndex] = field(default_factory=list)
    vertexCentricPropertyIndexes: list[dict[str, str]] = field(default_factory=list)
    translation_notes: list[str] = field(default_factory=list)

    def to_schema_dict(self) -> dict[str, Any]:  # noqa: ANN401
        """Return only the JanusGraph-consumable keys (no internal metadata)."""
        import dataclasses

        def _convert(obj: Any) -> Any:  # noqa: ANN401
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {k: _convert(v) for k, v in dataclasses.asdict(obj).items()}
            if isinstance(obj, list):
                return [_convert(i) for i in obj]
            return obj

        return {
            "propertyKeys": [_convert(pk) for pk in self.propertyKeys],
            "vertexLabels": [_convert(vl) for vl in self.vertexLabels],
            "edgeLabels": [_convert(el) for el in self.edgeLabels],
            "compositeIndexes": [_convert(ci) for ci in self.compositeIndexes],
            "mixedIndexes": [_convert(mi) for mi in self.mixedIndexes],
            "vertexCentricEdgeIndexes": [_convert(vc) for vc in self.vertexCentricEdgeIndexes],
            "vertexCentricPropertyIndexes": self.vertexCentricPropertyIndexes,
        }


# ---------------------------------------------------------------------------
# Seeder models (populated by seeder.py)
# ---------------------------------------------------------------------------


class UserFacingError(Exception):
    """An expected failure whose message says what to do; the CLI prints it and exits 1, without a traceback."""


@dataclass
class SeederResult:
    """Result returned by the Seeder after a successful seed operation."""

    graph_name: str
    template: str
    vertex_count: int
    edge_count: int
    next_command: str
    # Counted on the seeded graph. vertex_count/edge_count are the requested sizes *per primary label*, so the
    # totals differ (e.g. food --vertex-count 100 --edge-count 200 seeds 210 vertices and 500 edges).
    total_vertices: int | None = None
    total_edges: int | None = None
