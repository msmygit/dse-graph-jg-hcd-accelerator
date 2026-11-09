# READ-ONLY MODULE
# This module contains only static dataclass definitions and constants.
# No I/O of any kind is permitted here.
"""Static compatibility rule catalogue for the DSE Graph → JanusGraph assessor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """Compatibility finding severity."""

    GREEN = "GREEN"  # Fully compatible; no action required
    YELLOW = "YELLOW"  # Needs adaptation; migration proceeds with changes
    RED = "RED"  # Blocker; migration should not proceed until resolved


class Category(str, Enum):
    """Rule category mapping to assessment dimensions."""

    QUERY_PATTERN = "query_pattern"
    SCHEMA = "schema"
    INDEX = "index"
    CONFIGURATION = "configuration"
    OLAP = "olap"
    SECURITY = "security"
    TINKERPOP_DELTA = "tinkerpop_delta"
    DATA_TYPE = "data_type"
    HCD_TARGET = "hcd_target"


@dataclass
class CompatibilityRule:
    """A single compatibility rule in the catalogue."""

    rule_id: str
    category: Category
    severity: Severity
    title: str
    description: str
    detection_hint: str
    jg_equivalent: str
    docs_link: str
    remediation: str


# ---------------------------------------------------------------------------
# Rule catalogue
# ---------------------------------------------------------------------------

RULES: list[CompatibilityRule] = [
    # ── Query Pattern Rules (QP-xxx) ─────────────────────────────────────────
    CompatibilityRule(
        rule_id="QP-001",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="g.with('allow-filtering') usage",
        description="DSE Graph allows 'allow-filtering' to bypass index enforcement. "
        "JanusGraph raises ExceptionForMissingBackingIndex instead.",
        detection_hint="with\\('allow-filtering'\\)",
        jg_equivalent="Remove — ensure a backing index exists for the predicate.",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/",
        remediation=(
            "Remove g.with('allow-filtering') from application queries. "
            "Ensure all filtered properties have a composite or mixed index in JanusGraph."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-002",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="g.with('label-warning', false) usage",
        description="DSE-specific traversal modifier that suppresses label warnings. Not applicable in JanusGraph.",
        detection_hint="with\\('label-warning'",
        jg_equivalent="Remove — not applicable in JanusGraph.",
        docs_link="https://docs.janusgraph.org/",
        remediation="Remove g.with('label-warning', false) from all traversals. JanusGraph does not use this modifier.",
    ),
    CompatibilityRule(
        rule_id="QP-003",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="g.with('ignore-unindexed') usage",
        description="DSE-specific modifier to ignore unindexed property access. JanusGraph enforces indexes or throws.",
        detection_hint="with\\('ignore-unindexed'\\)",
        jg_equivalent="Remove — JanusGraph enforces index or throws ExceptionForMissingBackingIndex.",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/",
        remediation=(
            "Remove g.with('ignore-unindexed'). Create indexes for all filtered properties, "
            "or set query.force-index=false in JanusGraph config to allow full scans."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-004",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="g.with('consistency', ...) usage",
        description="DSE per-traversal consistency level setting. Not supported in JanusGraph traversals.",
        detection_hint="with\\('consistency'",
        jg_equivalent="Set storage.cql.read-consistency-level / write-consistency-level in JanusGraph config.",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/",
        remediation=(
            "Remove g.with('consistency', ...) from traversals. "
            "Configure cluster-wide consistency in janusgraph.properties via "
            "storage.cql.read-consistency-level and storage.cql.write-consistency-level."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-005",
        category=Category.QUERY_PATTERN,
        severity=Severity.RED,
        title="OLAP traversal source 'a' is active",
        description="The 'a' traversal source indicates active DSE Spark/OLAP usage. "
        "JanusGraph requires a separate HadoopGraph + Spark deployment for OLAP.",
        detection_hint="^a\\.",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/hadoop/",
        remediation=(
            "Deploy JanusGraph with HadoopGraph and Apache Spark (separate from the OLTP stack). "
            "Rewrite OLAP jobs using TinkerPop SparkGraphComputer API."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-006",
        category=Category.QUERY_PATTERN,
        severity=Severity.RED,
        title="Search.prefix() / Search.token() usage",
        description="DSE Search predicates not available in JanusGraph without an Elasticsearch backend.",
        detection_hint="Search\\.prefix|Search\\.token",
        jg_equivalent=(
            "Text.textPrefix() (whole value, Mapping.STRING) / Text.textContains() and Text.textContainsPrefix() "
            "(tokenized, Mapping.TEXT) with a mixed index backed by OpenSearch / Elasticsearch."
        ),
        docs_link="https://docs.janusgraph.org/index-backend/text-search/",
        remediation=(
            "Replace Search.prefix() on an asString() key with Text.textPrefix() (Mapping.STRING). "
            "Replace Search.token() with Text.textContains() and Search.tokenPrefix() with Text.textContainsPrefix() "
            "(Mapping.TEXT). The assessment's Index Analysis lists the predicates each indexed key serves."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-007",
        category=Category.QUERY_PATTERN,
        severity=Severity.RED,
        title="Search.fuzzy() / Search.tokenFuzzy() usage",
        description="DSE fuzzy search predicates require Elasticsearch backend in JanusGraph.",
        detection_hint="Search\\.fuzzy|Search\\.tokenFuzzy",
        jg_equivalent="Text.textContainsFuzzy() with an Elasticsearch mixed index.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/",
        remediation=(
            "Replace Search.fuzzy() with Text.textContainsFuzzy(). "
            "Ensure a mixed index with Mapping.TEXT on the property."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-008",
        category=Category.QUERY_PATTERN,
        severity=Severity.RED,
        title="Search.regex() / Search.tokenRegex() usage",
        description="DSE regex search predicates require Elasticsearch backend in JanusGraph.",
        detection_hint="Search\\.regex|Search\\.tokenRegex",
        jg_equivalent="Text.textContainsRegex() with an Elasticsearch mixed index.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/",
        remediation=(
            "Replace Search.regex() with Text.textContainsRegex(). "
            "Ensure a mixed index with Mapping.TEXT on the property."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-009",
        category=Category.QUERY_PATTERN,
        severity=Severity.RED,
        title="Search.phrase() usage",
        description="DSE phrase search predicate requires Elasticsearch backend in JanusGraph.",
        detection_hint="Search\\.phrase",
        jg_equivalent="Text.textContainsPhrase() with an Elasticsearch mixed index.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/",
        remediation=(
            "Replace Search.phrase() with Text.textContainsPhrase(). "
            "Ensure a mixed index with Mapping.TEXT on the property."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-010",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="Geo.inside(Geo.point(...), radius) usage",
        description="DSE geo predicate for point-in-radius queries. API differs in JanusGraph.",
        detection_hint="Geo\\.inside\\(Geo\\.point",
        jg_equivalent="Geo.within(Geoshape.circle(lat, lon, radius)) with a Mapping.PREFIX_TREE mixed index.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#geoshape",
        remediation=(
            "Replace Geo.inside(Geo.point(lat, lon), radius) with "
            "Geo.within(Geoshape.circle(lat, lon, radius)). "
            "Ensure a mixed index with Mapping.PREFIX_TREE exists on the geo property."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-011",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="Geo.inside(Geo.polygon(...)) usage",
        description="DSE geo predicate for point-in-polygon queries. WKT conversion required for JanusGraph.",
        detection_hint="Geo\\.inside\\(Geo\\.polygon",
        jg_equivalent="Geo.within(Geoshape.fromWkt(wkt)) with a Mapping.PREFIX_TREE mixed index.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#geoshape",
        remediation=(
            "Replace Geo.inside(Geo.polygon(...)) with "
            "Geo.within(Geoshape.fromWkt('<polygon WKT string>')). "
            "Convert DSE polygon vertex list to WKT POLYGON format."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-012",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="DseGraph.traversal() usage",
        description="DSE-specific graph traversal factory. Must be replaced with JanusGraph equivalent.",
        detection_hint="DseGraph\\.traversal",
        jg_equivalent="JanusGraphFactory.open(conf).traversal()",
        docs_link="https://docs.janusgraph.org/getting-started/basic-usage/",
        remediation=(
            "Replace DseGraph.traversal(dseSession, graphName) with "
            "JanusGraphFactory.open('/path/to/janusgraph.properties').traversal()."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-013",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="DSG partition key lookup pattern",
        description="DSG-style vertex lookup using partition key map syntax g.V(['~label':'X','key':v]).",
        detection_hint="g\\.V\\(\\[",
        jg_equivalent="g.V().hasLabel('X').has('key', v)",
        docs_link="https://docs.janusgraph.org/basics/gremlin/",
        remediation=(
            "Replace g.V(['~label':'X','key': v]) with g.V().hasLabel('X').has('key', v). "
            "Ensure a composite index on the key property exists for performance."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-014",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="Lambda / Groovy closure in traversal",
        description="Groovy closures (.map{...}, .filter{...}) in traversals are allowed in JanusGraph "
        "but must be within evaluationTimeout.",
        detection_hint="\\.map\\{|\\.filter\\{",
        jg_equivalent="Lambda traversals are allowed; verify evaluationTimeout config.",
        docs_link="https://docs.janusgraph.org/basics/gremlin/",
        remediation=(
            "Groovy lambdas are allowed by default in JanusGraph. "
            "Review evaluationTimeout in gremlin-server.yaml (replaces DSE's restrictLambda). "
            "Long-running lambdas will be terminated at the timeout."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-015",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="graph.tx().open() / .commit() / .rollback() (Classic)",
        description="Classic DSE Graph transaction API. JanusGraph re-introduces graph.tx() in 1.x.",
        detection_hint="graph\\.tx\\(\\)",
        jg_equivalent="graph.tx() is directly compatible in JanusGraph 1.x.",
        docs_link="https://docs.janusgraph.org/basics/transactions/",
        remediation=(
            "graph.tx().open(), .commit(), and .rollback() are compatible with JanusGraph 1.x. "
            "Verify that autocommit behaviour matches your expectations."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-016",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="TinkerPop 3.4 repeat() local semantics",
        description="In TinkerPop 3.4 (DSE), repeat() had local semantics. "
        "TinkerPop 3.7+ (JanusGraph) uses global semantics by default.",
        detection_hint="\\.repeat\\(",
        jg_equivalent="Add explicit .until() guards to all repeat() traversals.",
        docs_link="https://tinkerpop.apache.org/docs/current/upgrade/",
        remediation=(
            "Add explicit .until() or .times() guards to all repeat() traversals. "
            "Without guards, repeat() in TinkerPop 3.7 may loop indefinitely."
        ),
    ),
    CompatibilityRule(
        rule_id="QP-017",
        category=Category.QUERY_PATTERN,
        severity=Severity.YELLOW,
        title="valueMap() without by(unfold())",
        description="TinkerPop 3.5+ valueMap() returns Map<String, List> by default. "
        "Application code expecting flat values must be updated.",
        detection_hint="\\.valueMap\\(",
        jg_equivalent="Add .by(unfold()) after valueMap() for flat single values.",
        docs_link="https://tinkerpop.apache.org/docs/current/reference/#valuemap-step",
        remediation=(
            "Update valueMap() calls to include .by(unfold()) for properties with SINGLE cardinality, "
            "or update consuming code to handle List<Object> values."
        ),
    ),
    # ── Schema Rules (SC-xxx) ─────────────────────────────────────────────────
    CompatibilityRule(
        rule_id="SC-001",
        category=Category.SCHEMA,
        severity=Severity.YELLOW,
        title="partitionBy() / clusterBy() on vertex label",
        description="DSG-specific CQL storage layout directives. JanusGraph manages CQL internally; "
        "these become regular property keys.",
        detection_hint="partitionBy|clusterBy",
        jg_equivalent="Regular property keys in JanusGraph schema.",
        docs_link="https://docs.janusgraph.org/schema/",
        remediation=(
            "partitionBy() and clusterBy() keys become regular JanusGraph property keys. "
            "Composite indexes on these keys preserve query patterns."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-002",
        category=Category.SCHEMA,
        severity=Severity.YELLOW,
        title="fromExistingTable() on vertex label",
        description="DSG-specific directive to map a graph label to an existing CQL table. "
        "JanusGraph cannot map to an existing CQL table.",
        detection_hint="fromExistingTable",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/schema/",
        remediation=(
            "JanusGraph manages its own CQL tables. Remove fromExistingTable() directives. "
            "Migrate data from the existing CQL table into JanusGraph via the migrator."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-003",
        category=Category.SCHEMA,
        severity=Severity.RED,
        title="mapOf(K, V) property type",
        description="JanusGraph has no native map property type. Must use --map-strategy flat or json.",
        detection_hint="mapOf\\(",
        jg_equivalent="flat: parallel _key/_value LIST properties; json: single JSON string.",
        docs_link="https://docs.janusgraph.org/schema/",
        remediation=(
            "Use --map-strategy flat (default) to explode map entries into parallel "
            "<prop>_key LIST and <prop>_value LIST properties. "
            "Or use --map-strategy json to serialise the entire map as a JSON string."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-004",
        category=Category.SCHEMA,
        severity=Severity.YELLOW,
        title="setOf(X) / listOf(X) collection property",
        description="DSE collection properties map to JanusGraph Cardinality.SET / Cardinality.LIST.",
        detection_hint="setOf\\(|listOf\\(",
        jg_equivalent="Cardinality.SET / Cardinality.LIST — fully automated.",
        docs_link="https://docs.janusgraph.org/schema/#property-key-cardinality",
        remediation=(
            "setOf(X) maps to Cardinality.SET; listOf(X) maps to Cardinality.LIST. "
            "This is handled automatically by the translator."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-005",
        category=Category.SCHEMA,
        severity=Severity.RED,
        title="UDT (frozen(typeOf(...))) property type",
        description="JanusGraph has no native UDT support. Must use --udt-strategy flat or json.",
        detection_hint="frozen\\(typeOf|typeOf\\(",
        jg_equivalent="flat: one property key per UDT field; json: single JSON string.",
        docs_link="https://docs.janusgraph.org/schema/",
        remediation=(
            "Use --udt-strategy flat (default) to expand UDT fields into "
            "<udt>_<field> property keys. "
            "Or use --udt-strategy json to serialise the UDT as a JSON string."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-006",
        category=Category.SCHEMA,
        severity=Severity.YELLOW,
        title="tupleOf(...) property type",
        description="JanusGraph has no native tuple type. Values are serialised to a JSON string.",
        detection_hint="tupleOf\\(",
        jg_equivalent="Serialise to JSON string; flag for app-layer reconstruction.",
        docs_link="https://docs.janusgraph.org/schema/",
        remediation=(
            "tupleOf properties are migrated as JSON strings. "
            "Update application code to serialise/deserialise tuple values as JSON."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-007",
        category=Category.SCHEMA,
        severity=Severity.RED,
        title="Meta-properties usage",
        description="Meta-properties (v.property('x').property('meta_key', v)) were removed in DSG Core. "
        "JanusGraph supports them but semantics differ.",
        detection_hint="\\.property\\(.*\\)\\.property\\(",
        jg_equivalent="JanusGraph supports meta-properties but semantics differ — manual review required.",
        docs_link="https://docs.janusgraph.org/schema/#property-key-cardinality",
        remediation=(
            "Review all meta-property usages. JanusGraph meta-properties require "
            "Cardinality.LIST or Cardinality.SET on the outer property key. "
            "DSG Core removed meta-properties; Classic supports them."
        ),
    ),
    CompatibilityRule(
        rule_id="SC-008",
        category=Category.SCHEMA,
        severity=Severity.YELLOW,
        title="Geospatial property type (Point) in schema",
        description="DSE Point type is stored as WKT string in JanusGraph. "
        "A companion geo mixed index is auto-generated for Point properties.",
        detection_hint="Point|Polygon|Linestring",
        jg_equivalent="java.lang.String (WKT) + Mapping.PREFIX_TREE mixed index for Point.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#geoshape",
        remediation=(
            "Point properties are automatically migrated to WKT strings with a "
            "Mapping.PREFIX_TREE mixed index. Polygon and Linestring are stored as WKT "
            "but require manual geo index configuration."
        ),
    ),
    # ── Index Rules (IX-xxx) ──────────────────────────────────────────────────
    CompatibilityRule(
        rule_id="IX-001",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="materializedView() → composite index",
        description="DSE materializedView maps to a JanusGraph composite index. "
        "MV partition key becomes composite index key.",
        detection_hint="materializedView",
        jg_equivalent="JanusGraph composite index.",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/#composite-index",
        remediation="materializedView is automatically translated to a composite index by the translator.",
    ),
    CompatibilityRule(
        rule_id="IX-002",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="materializedView().inverse() → buildEdgeIndex(IN)",
        description="Inverse materializedView on edge label maps to a JanusGraph vertex-centric "
        "edge index with Direction.IN.",
        detection_hint="\\.inverse\\(\\)",
        jg_equivalent="buildEdgeIndex(..., Direction.IN, ...)",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/#vertex-centric-indexes",
        remediation=(
            "Inverse materializedView is automatically translated to a vertex-centric "
            "edge index with Direction.IN by the translator."
        ),
    ),
    CompatibilityRule(
        rule_id="IX-003",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="secondaryIndex() → composite index",
        description="DSE secondary index maps to a JanusGraph composite index (equality predicates only).",
        detection_hint="secondaryIndex",
        jg_equivalent="JanusGraph composite index (equality only).",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/#composite-index",
        remediation="secondaryIndex is automatically translated to a composite index by the translator.",
    ),
    CompatibilityRule(
        rule_id="IX-004",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="searchIndex().by('x').asText() → mixed index with Mapping.TEXT",
        description="DSE text search index maps to a JanusGraph mixed index with Mapping.TEXT "
        "on an Elasticsearch/OpenSearch backend.",
        detection_hint="searchIndex.*asText",
        jg_equivalent="JanusGraph mixed index with Mapping.TEXT on Elasticsearch.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#text-search",
        remediation=(
            "searchIndex with asText() is automatically translated to a mixed index with "
            "Mapping.TEXT. Ensure an Elasticsearch/OpenSearch backend is configured."
        ),
    ),
    CompatibilityRule(
        rule_id="IX-005",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="searchIndex().by('x').asString() → mixed index with Mapping.STRING",
        description="DSE string search index maps to a JanusGraph mixed index with Mapping.STRING.",
        detection_hint="searchIndex.*asString",
        jg_equivalent="JanusGraph mixed index with Mapping.STRING on Elasticsearch.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#string-search",
        remediation=("searchIndex with asString() is automatically translated to a mixed index with Mapping.STRING."),
    ),
    CompatibilityRule(
        rule_id="IX-006",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="searchIndex() on geo property → mixed index with Mapping.PREFIX_TREE",
        description="DSE geo search index maps to a JanusGraph mixed index with Mapping.PREFIX_TREE.",
        detection_hint="searchIndex.*geo|Point.*searchIndex",
        jg_equivalent="JanusGraph mixed index with Mapping.PREFIX_TREE on Elasticsearch.",
        docs_link="https://docs.janusgraph.org/index-management/index-parameters/#geoshape",
        remediation=(
            "Geo searchIndex is automatically translated to a mixed index with "
            "Mapping.PREFIX_TREE. Point properties stored as WKT strings."
        ),
    ),
    CompatibilityRule(
        rule_id="IX-007",
        category=Category.INDEX,
        severity=Severity.RED,
        title="schema.indexFor(...).analyze() — DSE query plan analysis",
        description="DSE query plan analysis API. No equivalent in JanusGraph.",
        detection_hint="indexFor.*analyze",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/index-management/",
        remediation=(
            "schema.indexFor().analyze() is DSE-specific. Remove from application code. "
            "Use JanusGraph's explain() or JanusGraph Server metrics for query analysis."
        ),
    ),
    CompatibilityRule(
        rule_id="IX-008",
        category=Category.INDEX,
        severity=Severity.YELLOW,
        title="Vertex-centric RelationIndex → buildEdgeIndex()",
        description="DSE vertex-centric indexes map to JanusGraph vertex-centric edge indexes.",
        detection_hint="RelationIndex|from.*to.*materializedView.*partitionBy.*OUT|clusterBy.*IN",
        jg_equivalent="JanusGraph buildEdgeIndex() — direction and sort order preserved.",
        docs_link="https://docs.janusgraph.org/index-management/index-performance/#vertex-centric-indexes",
        remediation=(
            "Vertex-centric indexes are automatically translated to JanusGraph "
            "vertex-centric edge indexes by the translator."
        ),
    ),
    # ── Configuration Rules (CF-xxx) ─────────────────────────────────────────
    CompatibilityRule(
        rule_id="CF-001",
        category=Category.CONFIGURATION,
        severity=Severity.YELLOW,
        title="schema_mode = Development",
        description="DSE Development schema mode auto-creates types. JanusGraph equivalent is schema.default=default.",
        detection_hint="schema_mode.*Development",
        jg_equivalent="schema.default=default in janusgraph.properties",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#schema",
        remediation="Set schema.default=default in JanusGraph properties for auto-create behaviour.",
    ),
    CompatibilityRule(
        rule_id="CF-002",
        category=Category.CONFIGURATION,
        severity=Severity.YELLOW,
        title="schema_mode = Production",
        description="DSE Production schema mode enforces strict type checking. "
        "JanusGraph equivalent is schema.default=none.",
        detection_hint="schema_mode.*Production",
        jg_equivalent="schema.default=none in janusgraph.properties",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#schema",
        remediation="Set schema.default=none in JanusGraph properties to enforce strict schema.",
    ),
    CompatibilityRule(
        rule_id="CF-003",
        category=Category.CONFIGURATION,
        severity=Severity.YELLOW,
        title="allow_scan = true",
        description="DSE allows full table scans when allow_scan is true. JanusGraph has no direct equivalent.",
        detection_hint="allow_scan.*true",
        jg_equivalent="query.force-index=false in janusgraph.properties (allows full scans).",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#query",
        remediation=(
            "If full scans are required, set query.force-index=false in JanusGraph config. "
            "Otherwise ensure all filtered properties have indexes."
        ),
    ),
    CompatibilityRule(
        rule_id="CF-004",
        category=Category.CONFIGURATION,
        severity=Severity.YELLOW,
        title="restrict_lambda = true",
        description="DSE restricts Groovy lambda usage in traversals. JanusGraph allows lambdas by default.",
        detection_hint="restrict_lambda.*true",
        jg_equivalent="evaluationTimeout in gremlin-server.yaml controls execution time.",
        docs_link="https://docs.janusgraph.org/basics/server/",
        remediation=(
            "JanusGraph allows lambdas by default. "
            "Set evaluationTimeout in gremlin-server.yaml to bound execution time. "
            "Review all lambda-using traversals for performance impact."
        ),
    ),
    CompatibilityRule(
        rule_id="CF-005",
        category=Category.CONFIGURATION,
        severity=Severity.YELLOW,
        title="graph.system_replication_config",
        description="DSE system replication configuration. Must be translated to JanusGraph CQL properties.",
        detection_hint="system_replication_config|replication_config",
        jg_equivalent=(
            "storage.cql.replication-strategy-class and "
            "storage.cql.replication-strategy-options in janusgraph.properties"
        ),
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#storage-cql",
        remediation=(
            "Translate DSE replication config to JanusGraph: "
            "storage.cql.replication-strategy-class=NetworkTopologyStrategy, "
            "storage.cql.replication-strategy-options=<dc>,<rf>."
        ),
    ),
    CompatibilityRule(
        rule_id="CF-006",
        category=Category.CONFIGURATION,
        severity=Severity.RED,
        title="Source graph unreachable or has no schema",
        description="schema.describe() on the source DSE graph failed or returned nothing, so the graph could not be "
        "assessed. Every other finding in this report is a static advisory, not a result of inspecting the graph.",
        detection_hint="",
        jg_equivalent="",
        docs_link="https://docs.datastax.com/en/dse/6.9/graph/",
        remediation=(
            "Check that the DSE source is healthy (./docker/up.sh waits for it), that --source-gremlin-url is "
            "correct, and that --graph-name exists — seed it first with 'dse-jg-migrator seed' if it does not."
        ),
    ),
    # ── OLAP Rules (OL-xxx) ───────────────────────────────────────────────────
    CompatibilityRule(
        rule_id="OL-001",
        category=Category.OLAP,
        severity=Severity.RED,
        title="OLAP traversal source 'a' is active",
        description="DSE OLAP via Spark. JanusGraph requires a separate HadoopGraph + Spark deployment.",
        detection_hint="^a\\.",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/hadoop/",
        remediation=(
            "Deploy JanusGraph with HadoopGraph and Apache Spark (separate from OLTP). "
            "Rewrite OLAP jobs using TinkerPop SparkGraphComputer API."
        ),
    ),
    CompatibilityRule(
        rule_id="OL-002",
        category=Category.OLAP,
        severity=Severity.RED,
        title="SparkGraphComputer in traversal",
        description="Direct SparkGraphComputer usage in DSE traversals. No automated migration path.",
        detection_hint="SparkGraphComputer",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/hadoop/",
        remediation=(
            "SparkGraphComputer usage requires a full JanusGraph + HadoopGraph + Spark deployment. "
            "Rewrite using TinkerPop's SparkGraphComputer with JanusGraph's HadoopGraph."
        ),
    ),
    CompatibilityRule(
        rule_id="OL-003",
        category=Category.OLAP,
        severity=Severity.RED,
        title="GraphFrames API usage",
        description="DSE GraphFrames API. No direct equivalent in JanusGraph automated migration.",
        detection_hint="GraphFrames",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/hadoop/",
        remediation=(
            "GraphFrames is a DSE-specific OLAP API. "
            "Rewrite using TinkerPop OLAP with SparkGraphComputer or a separate graph analytics platform."
        ),
    ),
    # ── Security Rules (SE-xxx) ───────────────────────────────────────────────
    CompatibilityRule(
        rule_id="SE-001",
        category=Category.SECURITY,
        severity=Severity.RED,
        title="RLAC / Row-Level Access Control",
        description="DSE Enterprise RLAC feature. JanusGraph has no row-level security.",
        detection_hint="RLAC|RowLevelAccess|row_level",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/security/",
        remediation=(
            "RLAC is a DSE Enterprise feature with no JanusGraph equivalent. "
            "Implement application-layer access control using JanusGraph's authorisation hooks "
            "or an external policy engine (e.g., Open Policy Agent)."
        ),
    ),
    CompatibilityRule(
        rule_id="SE-002",
        category=Category.SECURITY,
        severity=Severity.RED,
        title="Row-Level Security policies",
        description="DSE row-level security policies. No direct equivalent in JanusGraph.",
        detection_hint="row_level_security|rls_policy",
        jg_equivalent="",
        docs_link="https://docs.janusgraph.org/advanced-topics/security/",
        remediation=(
            "Row-level security policies must be re-implemented at the application layer. "
            "Consider using JanusGraph's SubgraphStrategy for filtering, "
            "or an external authorisation service."
        ),
    ),
    # ── TinkerPop Delta Rules (TP-xxx) ────────────────────────────────────────
    CompatibilityRule(
        rule_id="TP-001",
        category=Category.TINKERPOP_DELTA,
        severity=Severity.YELLOW,
        title="repeat() step global semantics change (TinkerPop 3.4 → 3.7)",
        description="In TinkerPop 3.4 (DSE), repeat() had local semantics. "
        "TinkerPop 3.7+ (JanusGraph) uses global semantics by default.",
        detection_hint="\\.repeat\\(",
        jg_equivalent="Add explicit .until() guards.",
        docs_link="https://tinkerpop.apache.org/docs/current/upgrade/",
        remediation=(
            "Add explicit .until() or .times() guards to all repeat() traversals. "
            "Without guards, repeat() in TinkerPop 3.7 may loop indefinitely on large graphs."
        ),
    ),
    CompatibilityRule(
        rule_id="TP-002",
        category=Category.TINKERPOP_DELTA,
        severity=Severity.YELLOW,
        title="valueMap() return format change",
        description="TinkerPop 3.5+ valueMap() returns Map<String, List> by default. "
        "Application code expecting flat values must be updated.",
        detection_hint="\\.valueMap\\(",
        jg_equivalent="Add .by(unfold()) for flat single values.",
        docs_link="https://tinkerpop.apache.org/docs/current/reference/#valuemap-step",
        remediation=(
            "Update valueMap() calls to include .by(unfold()) for properties with SINGLE cardinality, "
            "or update consuming code to handle List<Object> values."
        ),
    ),
    CompatibilityRule(
        rule_id="TP-003",
        category=Category.TINKERPOP_DELTA,
        severity=Severity.YELLOW,
        title="scriptEvaluationTimeout renamed to evaluationTimeout",
        description="TinkerPop 3.5+ renamed scriptEvaluationTimeout to evaluationTimeout in gremlin-server.yaml.",
        detection_hint="scriptEvaluationTimeout",
        jg_equivalent="evaluationTimeout in gremlin-server.yaml",
        docs_link="https://tinkerpop.apache.org/docs/current/reference/#_server_configuration",
        remediation=(
            "Rename scriptEvaluationTimeout to evaluationTimeout in gremlin-server.yaml. Value is in milliseconds."
        ),
    ),
    CompatibilityRule(
        rule_id="TP-004",
        category=Category.TINKERPOP_DELTA,
        severity=Severity.GREEN,
        title="GraphBinary serialisation supported",
        description="JanusGraph 1.x supports GraphBinary serialisation (TinkerPop 3.4+). "
        "Update client driver to use it for improved performance.",
        detection_hint="",
        jg_equivalent="GraphBinary serialiser in gremlin-python driver.",
        docs_link="https://tinkerpop.apache.org/docs/current/dev/io/#graphbinary",
        remediation=(
            "Update gremlin-python client to use GraphBinarySerializersV1 for improved performance. "
            "No breaking changes — fully backward compatible."
        ),
    ),
    CompatibilityRule(
        rule_id="TP-005",
        category=Category.TINKERPOP_DELTA,
        severity=Severity.YELLOW,
        title="property(k, v) cardinality resolution",
        description="TinkerPop 3.7 resolves cardinality from schema; ensure schema.default is set.",
        detection_hint="\\.property\\(",
        jg_equivalent="Ensure schema.default is set correctly (none or default).",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#schema",
        remediation=(
            "Set schema.default=none (strict) or schema.default=default (auto-create) "
            "in janusgraph.properties. With schema.default=none, all property keys must be "
            "pre-defined before use."
        ),
    ),
    # ── HCD Target Compatibility Rules (HC-xxx) ───────────────────────────────
    CompatibilityRule(
        rule_id="HC-001",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD AdvancedAuthenticator is enabled",
        description="The credentialed CQL connection to the HCD target succeeded and an anonymous one was rejected, "
        "so password authentication is enforced. JanusGraph must set storage.username and storage.password.",
        detection_hint="AdvancedAuthenticator",
        jg_equivalent="storage.username=<user>, storage.password=<password> in janusgraph.properties",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#storage-cql",
        remediation=(
            "Set storage.username and storage.password in janusgraph.properties, supplied at deploy time "
            "(e.g. from $HCD_CQL_PASSWORD) — never committed."
        ),
    ),
    CompatibilityRule(
        rule_id="HC-002",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD materialized_views_enabled: false",
        description="HCD 2.x has CQL materialized views disabled. JanusGraph does not use CQL MVs — no conflict.",
        detection_hint="materialized_views_enabled.*false",
        jg_equivalent="No action needed — JanusGraph does not create CQL MVs.",
        docs_link="https://docs.janusgraph.org/storage-backend/cassandra/",
        remediation=(
            "No JanusGraph action required. "
            "Remove any pre-existing CQL MV creation scripts from your deployment automation."
        ),
    ),
    CompatibilityRule(
        rule_id="HC-003",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD guardrail defaults enforce batch-size limits",
        description="HCD hcd_guardrail_defaults=true enforces conservative batch-size limits. "
        "Do not enable atomic batch mutations.",
        detection_hint="hcd_guardrail_defaults.*true",
        jg_equivalent="storage.cql.atomic-batch-mutate=false (must not be set to true)",
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#storage-cql",
        remediation=(
            "Ensure storage.cql.atomic-batch-mutate=false (or omit — false is the default). "
            "Do not set storage.cql.atomic-batch-mutate=true against HCD."
        ),
    ),
    CompatibilityRule(
        rule_id="HC-004",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD storage_compatibility_mode: NONE (Cassandra 5+ SSTable formats)",
        description="HCD uses Cassandra 5+ SSTable formats. "
        "JanusGraph 1.x CQL driver (DataStax Java Driver 4.x) fully supports this.",
        detection_hint="storage_compatibility_mode.*NONE",
        jg_equivalent="No action needed — JanusGraph 1.x CQL driver supports Cassandra 5 protocol.",
        docs_link="https://docs.janusgraph.org/storage-backend/cassandra/",
        remediation="No action needed. Informational only.",
    ),
    CompatibilityRule(
        rule_id="HC-005",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD default_secondary_index: sai",
        description="HCD uses Storage-Attached Index (SAI) as the default secondary index. "
        "JanusGraph creates its own index tables; no conflict.",
        detection_hint="default_secondary_index.*sai",
        jg_equivalent="No action needed — JanusGraph manages its own index tables.",
        docs_link="https://docs.janusgraph.org/index-management/",
        remediation="No action needed. Informational only.",
    ),
    CompatibilityRule(
        rule_id="HC-006",
        category=Category.HCD_TARGET,
        severity=Severity.GREEN,
        title="HCD CQL port 9042, datacenter dc1",
        description="HCD CQL port 9042 and datacenter dc1 match JanusGraph defaults.",
        detection_hint="",
        jg_equivalent=("storage.port=9042, storage.cql.local-datacenter=dc1 — matches JanusGraph defaults."),
        docs_link="https://docs.janusgraph.org/basics/configuration-reference/#storage-cql",
        remediation="No action needed. Fully compatible.",
    ),
    CompatibilityRule(
        rule_id="HC-007",
        category=Category.HCD_TARGET,
        severity=Severity.YELLOW,
        title="HCD linux/amd64 image — Apple Silicon emulation",
        description="HCD 2.x image is linux/amd64 only. On Apple Silicon, Docker uses Rosetta emulation.",
        detection_hint="",
        jg_equivalent="No JanusGraph config impact.",
        docs_link="https://docs.docker.com/desktop/features/rosetta/",
        remediation=(
            "Enable Rosetta emulation in Docker Desktop → Settings → Features in Development. "
            "Allocate ≥ 8 GB RAM. Expect ~2× longer startup time vs native amd64."
        ),
    ),
    CompatibilityRule(
        rule_id="HC-008",
        category=Category.HCD_TARGET,
        severity=Severity.RED,
        title="IBM Container Registry authentication required for HCD image",
        description="HCD image requires authentication with IBM Container Registry (cp.icr.io). "
        "docker login must succeed before docker compose up.",
        detection_hint="",
        jg_equivalent="",
        docs_link="https://www.ibm.com/docs/en/hyper-converged-database",
        remediation=(
            "Run: docker login cp.icr.io --username cp\n"
            "When prompted for password, paste your IBM Entitled Registry key "
            "from https://myibm.ibm.com/products-services/containerlibrary"
        ),
    ),
    CompatibilityRule(
        rule_id="HC-009",
        category=Category.HCD_TARGET,
        severity=Severity.RED,
        title="HCD accepts an anonymous connection",
        description="The HCD target accepted a CQL connection without credentials, so authentication is not "
        "enforced. The HCD image ships with authenticator.parameters.enabled commented out (off).",
        detection_hint="",
        jg_equivalent="",
        docs_link="https://www.ibm.com/docs/en/hyper-converged-database",
        remediation=(
            "Enable authentication in cassandra.yaml: under authenticator.parameters set 'enabled: true' and "
            "'default_scheme: internal', and under authorizer.parameters set 'enabled: true'; restart HCD. The local "
            "stack does this with docker/hcd-entrypoint.sh. Then change the default 'cassandra' superuser password."
        ),
    ),
]

# Index for fast lookup by rule_id
RULES_BY_ID: dict[str, CompatibilityRule] = {r.rule_id: r for r in RULES}
