# DSE Graph → JanusGraph / HCD Migration Accelerator — Implementation Plan

> **Status:** Pending implementation
> **Goal:** Build a lightweight CLI tool that (0) runs a non-destructive pre-migration compatibility assessment, (1) analyses a live DSE Graph cluster, (2) generates a migration plan, and (3) executes the schema + data migration to JanusGraph with HCD as its CQL backend.
> **Sources:** `bdp/dse-graph/`, `dse-docs-new/modules/graph/`, `janusgraph/`, project structure exploration (November 2026).

---

## Table of Contents
1. [Background and Motivation](#1-background-and-motivation)
2. [Scope](#2-scope)
3. [Architecture Overview](#3-architecture-overview)
4. [Key Concepts: DSE Graph vs JanusGraph](#4-key-concepts-dse-graph-vs-janusgraph)
5. [Sub-Task 0 — Pre-Migration Compatibility Assessment](#sub-task-0--pre-migration-compatibility-assessment)
6. [Sub-Task 1 — Project Scaffold](#sub-task-1--project-scaffold)
7. [Sub-Task 2 — DSE Graph Analyser](#sub-task-2--dse-graph-analyser)
8. [Sub-Task 3 — Schema Translator](#sub-task-3--schema-translator)
9. [Sub-Task 4 — JanusGraph / HCD Target Initialiser](#sub-task-4--janusgraph--hcd-target-initialiser)
10. [Sub-Task 5 — Data Migrator](#sub-task-5--data-migrator)
11. [Sub-Task 6 — Traversal Validator](#sub-task-6--traversal-validator)
12. [Sub-Task 7 — CLI Wrapper and Report Generator](#sub-task-7--cli-wrapper-and-report-generator)
13. [Sub-Task 8 — Docker / Compose Integration and E2E Test](#sub-task-8--docker--compose-integration-and-e2e-test)
14. [Appendix A — Data Type Mapping Table](#appendix-a--data-type-mapping-table)
15. [Appendix B — Index Type Mapping Table](#appendix-b--index-type-mapping-table)
16. [Appendix C — DSE Graph Schema Syntax Reference](#appendix-c--dse-graph-schema-syntax-reference)
17. [Appendix D — JanusGraph JSON Schema Format](#appendix-d--janusgraph-json-schema-format)
18. [Appendix E — Known Gaps and Caveats](#appendix-e--known-gaps-and-caveats)
19. [Appendix F — Compatibility Assessment Rule Catalogue](#appendix-f--compatibility-assessment-rule-catalogue)

---

## 1. Background and Motivation

DataStax Enterprise (DSE) Graph provides two distinct graph engines on top of Cassandra:

| Engine | DSE Version | Key Characteristic |
|--------|-------------|-------------------|
| **Classic** | ≤ 6.7 | Custom graph storage layer, bidirectional edges, meta-properties, `graph.tx()` API |
| **DSG (Core / NGDG)** | 6.8+ (default) | 1:1 graph-to-CQL-keyspace mapping; Gremlin + CQL dual access; unidirectional edges |

Both engines use [Apache TinkerPop 3.x](https://tinkerpop.apache.org/) as the Gremlin API layer, which is the same layer JanusGraph exposes. This means Gremlin traversals remain largely portable; the migration challenge sits in:

1. **Schema re-definition** — DSE Graph uses its own schema DSL; JanusGraph uses a Java/JSON management API.
2. **Index re-creation** — Search indexes (Solr) map to JanusGraph mixed indexes (Elasticsearch); composite/MV indexes map to JanusGraph composite indexes; vertex-centric indexes have a near 1:1 equivalent.
3. **Data movement** — DSG stores vertices/edges as regular CQL tables; Classic stores them in internal DSE tables. Both require extraction and re-insertion.
4. **Configuration re-mapping** — DSE replication, consistency, timeout, and sandbox settings need equivalents in JanusGraph properties.

**JanusGraph with HCD (Hyper-Converged Database) as the CQL backend** is the target. HCD is a Cassandra-compatible storage engine. The JanusGraph CQL backend (`storage.backend=cql`) connects to HCD exactly as it does to Apache Cassandra (confirmed by `janusgraph/janusgraph-cql/src/main/java/org/janusgraph/diskstorage/cql/CQLStoreManager.java` and examples in `janusgraph-examples/example-cql/conf/jgex-cql.properties`).

---

## 2. Scope

### In scope
- **Assess** (read-only, non-destructive) a running DSE Graph cluster — query patterns, schema, configuration, OLAP usage, security features — and produce a structured compatibility report before any migration action.
- **Analyse** a running DSE Graph cluster (Classic or DSG) via Gremlin + CQL connections.
- **Extract** all schema elements: vertex labels, edge labels, property keys, UDTs, all index types, graph-level configuration.
- **Translate** the extracted schema into a JanusGraph JSON schema definition.
- **Initialise** the target JanusGraph + HCD cluster with the translated schema.
- **Migrate data** (vertices + edges) using paginated CQL reads (DSG) or Gremlin reads (Classic), and paginated Gremlin writes to the target.
- **Validate** key traversals on the target and produce a diff report.
- **Produce** a human-readable migration report (`migration-report.md`) and a machine-readable audit log (`migration-audit.jsonl`).

### Out of scope
- DSE Graph OLAP / Spark analytics (GraphFrames, GraphComputer programs). These are cluster-specific and cannot be automatically translated.
- Row-Level Access Control (RLAC) and DSE authorisation policies — these need manual recreation.
- DSE Authentication / TLS certificate re-issuance.
- Schema migration between multiple graphs in one run (one graph per invocation, can be scripted externally).

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   dse-jg-migrator CLI                   │
│                     (Python 3.11+)                      │
└───────────┬─────────────────────────────────────────────┘
            │
   ┌────────▼────────────────────────────────────────┐
   │  STEP 0 — Compatibility Assessor  (assess cmd)  │◀── --analyze-only flag
   │  read-only: schema + query + config + risk score│
   └────────┬────────────────────────────────────────┘
            │ assessment-report.{md,json,html}
            │ (GATE — user reviews & approves before migration)
            │
   ┌────────▼────────┐      ┌──────────────────────┐
   │  DSE Analyser   │      │  Schema Translator   │
   │  (Gremlin +     │─────▶│  (DSE → JG mapping)  │
   │   CQL read)     │      └──────────┬───────────┘
   └─────────────────┘                 │
                                       │  JanusGraph JSON schema
                              ┌────────▼────────────┐
                              │  JG Initialiser     │
                              │  (POST schema via   │
                              │   Gremlin REST)     │
                              └────────┬────────────┘
                                       │
                              ┌────────▼────────────┐
                              │  Data Migrator      │
                              │  (batch Gremlin     │
                              │   reads + writes)   │
                              └────────┬────────────┘
                                       │
                              ┌────────▼────────────┐
                              │  Traversal Validator│
                              │  (spot-check        │
                              │   counts + samples) │
                              └────────┬────────────┘
                                       │
                              ┌────────▼────────────┐
                              │  Report Generator   │
                              │  (MD + JSONL)       │
                              └─────────────────────┘
```

**Technology choices (minimal dependencies):**
- **Python 3.11+** — widely available, no JVM required for the tool itself.
- **`gremlinpython`** (`apache-tinkerpop-gremlin-python`) — official TinkerPop Python driver; connects to both DSE Gremlin server and JanusGraph Gremlin server over WebSocket.
- **`cassandra-driver`** (`cassandra-driver`) — for CQL introspection of DSG keyspaces and paginated bulk reads.
- **`click`** — lightweight CLI framework.
- **`rich`** — terminal progress and formatted output (optional but strongly recommended for UX).

**Search/index backend assumption:** Elasticsearch or OpenSearch is **always** co-deployed alongside HCD. The tool unconditionally configures the JanusGraph `index.search.*` namespace to target this backend. OpenSearch is wire-compatible with the Elasticsearch REST API; the same JanusGraph Elasticsearch backend driver works against both (verified against OpenSearch 2.x and Elasticsearch 8.x).

No Spring, no JVM tooling, no Spark required in the migration tool itself.

---

## 4. Key Concepts: DSE Graph vs JanusGraph

### 4.1 Engine differences that drive implementation decisions

| Aspect | DSE Classic | DSG (Core) | JanusGraph |
|--------|------------|------------|------------|
| Schema source of truth | Internal DSE tables | CQL system tables | JanusGraph management graph |
| Schema introspection | `schema.describe()` Gremlin | CQL `system_schema` + `graph_schema` | `mgmt.printSchema()` or JSON |
| Vertex storage | Internal sharded tables | One CQL table per vertex label | Internal JanusGraph encoding in CQL |
| Edge storage | Internal sharded tables | One CQL table per edge label (dir) | Internal JanusGraph encoding in CQL |
| Property scoping | Global namespace | Per-label namespace | Global namespace |
| Edge direction | Bidirectional | Unidirectional (+ inverse MV) | Directed (BOTH by default) |
| Composite indexes | Secondary + MV | Materialized Views | `buildCompositeIndex()` |
| Full-text indexes | DSE Search (Solr) | DSE Search (Solr) | Mixed index (ES/Solr/Lucene) |
| Vertex-centric index | `RelationIndex` | Implicit in edge table | `buildEdgeIndex()` |
| Gremlin version | TinkerPop 3.4.x | TinkerPop 3.4.x | TinkerPop 3.7.x |
| Transaction API | `graph.tx()` | Removed | `graph.tx()` |
| Multi-properties | Native | `setOf()` / `listOf()` | `Cardinality.LIST` / `SET` |
| Meta-properties | Native | Replaced by UDTs/maps | Via vertex properties with cardinality |

### 4.2 Gremlin portability notes

Most standard Gremlin (TinkerPop) traversals work identically. The following DSE-specific patterns need adaptation:

| DSE Pattern | JanusGraph Equivalent |
|-------------|----------------------|
| `g.with('allow-filtering').V()` | `g.V()` (no restriction needed, but index required) |
| `g.with('consistency', 'ALL').addV(...)` | `graph.tx().open()` with backend consistency config |
| `schema.indexFor(...).analyze()` | `mgmt.printSchema()` review |
| `g.with('restrict_lambda', false)` | Not applicable (JanusGraph allows lambdas) |
| `DseGraph.traversal()` | `JanusGraphFactory.open(conf).traversal()` |
| `g.V(['~label':'person','person_id': uuid])` | `g.V().hasLabel('person').has('person_id', uuid)` |

### 4.3 ID format differences

| System | Vertex ID format | Edge ID format |
|--------|-----------------|----------------|
| DSG | `dseg:/person/4ce9caf1-...` (URI string) | `dseg:/person-created-recipe/4ce9caf1-.../2006` |
| JanusGraph | Internal `long` or `RelationIdentifier` | `RelationIdentifier` |

IDs **cannot be preserved**. The migrator must maintain a mapping table (`old_id → new_id`) for cross-reference during edge migration.

---

## Sub-Task 0 — Pre-Migration Compatibility Assessment

**Status:** `[ ] pending`

> **This sub-task is a mandatory gateway.** The `assess` command must succeed and the user must review the report before `run-all` or any individual migration sub-command will proceed. It is intentionally **read-only** and **non-destructive** — it can be run any number of times without affecting the source cluster.

### Intent

Connect to a live DSE Graph cluster (Classic or DSG), interrogate its schema, configuration, traversal patterns, and operational metadata, then score every discovered feature against JanusGraph's capability matrix. Emit a structured, human-readable assessment report in Markdown, JSON, and HTML formats that the operator can review, share with stakeholders, and use to plan remediation before triggering the full migration.

The assessment covers seven dimensions:
1. **Query pattern catalogue** — which DSE-specific Gremlin steps, predicates, and traversal modifiers are in use.
2. **Schema compatibility** — vertex/edge label definitions, property types, cardinalities, UDTs, collection types.
3. **Index compatibility** — search indexes (Solr), materialized views, secondary indexes, vertex-centric indexes.
4. **Configuration compatibility** — `schema_mode`, `allow_scan`, replication, consistency, lambda restrictions.
5. **OLAP / Spark usage** — detection of `a` traversal source, `SparkGraphComputer`, `GraphFrames` patterns.
6. **Security features** — RLAC / Row-Level Security usage (flags for manual recreation).
7. **TinkerPop version delta** — patterns that break between TinkerPop 3.4.x (DSE) and 3.7.x (JanusGraph).

### Expected Outcomes

- `dse-jg-migrator assess --source-gremlin-url ws://<host>:8182/gremlin --source-cql-host <h> --graph-name <g>` exits 0 and writes:
  - `<graph-name>-assessment-report.md` — human-readable full report
  - `<graph-name>-assessment-report.json` — machine-parseable findings (see schema below)
  - `<graph-name>-assessment-report.html` — standalone HTML with collapsible sections
- The report contains an **overall risk level**: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`.
- The `run-all` command reads `<graph-name>-assessment-report.json`, checks `overall_risk`, and aborts if the operator has not passed `--skip-assessment-gate` when risk is `HIGH` or `CRITICAL`.
- `--analyze-only` flag on `run-all` runs only Step 0 and exits — no migration is triggered.
- All operations are read-only. No writes to either cluster.

### Todo List

#### Step 0.1 — Define the compatibility rule catalogue (`compatibility_rules.py`)

Create a static Python module containing every known incompatibility as a dataclass-driven rule. Rules are the single source of truth for what the assessor checks and how it scores findings.

```python
# compatibility_rules.py
from dataclasses import dataclass, field
from enum import Enum

class Severity(str, Enum):
    GREEN  = "GREEN"   # Fully compatible; no action required
    YELLOW = "YELLOW"  # Needs adaptation; migration proceeds with changes
    RED    = "RED"     # Blocker; migration should not proceed until resolved

class Category(str, Enum):
    QUERY_PATTERN    = "query_pattern"
    SCHEMA           = "schema"
    INDEX            = "index"
    CONFIGURATION    = "configuration"
    OLAP             = "olap"
    SECURITY         = "security"
    TINKERPOP_DELTA  = "tinkerpop_delta"
    DATA_TYPE        = "data_type"
    HCD_TARGET       = "hcd_target"   # Pre-flight checks against the HCD target cluster

@dataclass
class CompatibilityRule:
    rule_id:        str           # e.g. "QP-001"
    category:       Category
    severity:       Severity
    title:          str
    description:    str           # What the rule detects
    detection_hint: str           # Regex or Gremlin query to find this pattern
    jg_equivalent:  str           # What to do in JanusGraph (empty if RED/unsupported)
    docs_link:      str           # Link to JanusGraph or TinkerPop docs
    remediation:    str           # Step-by-step fix guidance
```

The catalogue must contain (at minimum) the following rules, derived from the compatibility research in this codebase:

**Query Pattern Rules (QP-xxx):**

| Rule ID | Severity | Pattern Detected | JanusGraph Equivalent |
|---------|----------|------------------|-----------------------|
| `QP-001` | YELLOW | `g.with('allow-filtering')` | Remove — JanusGraph raises `ExceptionForMissingBackingIndex` instead; ensure index exists |
| `QP-002` | YELLOW | `g.with('label-warning', false)` | Remove — not applicable in JanusGraph |
| `QP-003` | YELLOW | `g.with('ignore-unindexed')` | Remove — JanusGraph enforces index or throws |
| `QP-004` | YELLOW | `g.with('consistency', ...)` | Set `storage.cql.read-consistency-level` / `write-consistency-level` in JG config |
| `QP-005` | RED | `a.` (OLAP traversal source) | Requires separate JanusGraph OLAP/HadoopGraph setup; out of automated migration scope |
| `QP-006` | RED | `Search.prefix(...)` / `Search.token(...)` | Use `Text.textContainsPrefix()` with a mixed index backed by Elasticsearch |
| `QP-007` | RED | `Search.fuzzy(...)` / `Search.tokenFuzzy(...)` | Use `Text.textContainsFuzzy()` with Elasticsearch backend |
| `QP-008` | RED | `Search.regex(...)` / `Search.tokenRegex(...)` | Use `Text.textContainsRegex()` with Elasticsearch backend |
| `QP-009` | RED | `Search.phrase(...)` | Use `Text.textContainsPhrase()` with Elasticsearch backend |
| `QP-010` | YELLOW | `Geo.inside(Geo.point(...), radius)` | Use `Geo.within(Geoshape.circle(lat, lon, radius))` with `Mapping.PREFIX_TREE` mixed index |
| `QP-011` | YELLOW | `Geo.inside(Geo.polygon(...))` | Use `Geo.within(Geoshape.fromWkt(wkt))` — polygon WKT conversion required |
| `QP-012` | YELLOW | `DseGraph.traversal()` | Replace with `JanusGraphFactory.open(conf).traversal()` |
| `QP-013` | YELLOW | `g.V(['~label':'X','key': v])` (DSG partition key lookup) | Replace with `g.V().hasLabel('X').has('key', v)` |
| `QP-014` | YELLOW | Lambda / Groovy closure in traversal (`.map{...}`, `.filter{...}`) | JanusGraph allows by default; verify `evaluationTimeout` config |
| `QP-015` | YELLOW | `graph.tx().open()` / `.commit()` / `.rollback()` (Classic) | Directly compatible — JanusGraph re-introduces `graph.tx()` |
| `QP-016` | YELLOW | TinkerPop 3.4 `repeat()` local semantics | JanusGraph uses TinkerPop 3.7 global-semantics default; add `.repeat(...).until(...)` guards |
| `QP-017` | YELLOW | `valueMap()` without `by(unfold())` | TinkerPop 3.5+ `valueMap()` returns `Map<String, List>`; add `.by(unfold())` for flat values |

**Schema Rules (SC-xxx):**

| Rule ID | Severity | Pattern Detected | Notes |
|---------|----------|-----------------|-------|
| `SC-001` | YELLOW | `partitionBy(...)` / `clusterBy(...)` on vertex label | JanusGraph manages CQL storage internally; these become regular property keys |
| `SC-002` | YELLOW | `fromExistingTable(...)` | DSG-specific; JanusGraph cannot map to an existing CQL table |
| `SC-003` | RED | `mapOf(K, V)` property type | JanusGraph has no native map; use `--map-strategy flat\|json` |
| `SC-004` | YELLOW | `setOf(X)` / `listOf(X)` | Maps to `Cardinality.SET` / `Cardinality.LIST` — fully automated |
| `SC-005` | RED | UDT (`frozen(typeOf(...))`) | Use `--udt-strategy flat\|json` |
| `SC-006` | YELLOW | `tupleOf(...)` property type | Serialise to JSON string; flag for app-layer reconstruction |
| `SC-007` | RED | Meta-properties (`v.property('x').property('meta_key', v)`) | Removed in DSG Core; JanusGraph supports but semantics differ — manual review required |
| `SC-008` | YELLOW | `property('x', Point)` / geo type in schema | Handled automatically: WKT string + `Mapping.PREFIX_TREE` mixed index generated for `Point`; `Polygon`/`Linestring` stored as WKT, no auto index |

**Index Rules (IX-xxx):**

| Rule ID | Severity | Pattern Detected | Notes |
|---------|----------|-----------------|-------|
| `IX-001` | YELLOW | `materializedView(...)` | Maps to JanusGraph composite index; MV partition key → composite index key |
| `IX-002` | YELLOW | `materializedView(...).inverse()` | Maps to `buildEdgeIndex(..., Direction.IN, ...)` |
| `IX-003` | YELLOW | `secondaryIndex(...)` | Maps to composite index (equality predicate only) |
| `IX-004` | YELLOW | `searchIndex().by('x').asText()` | Maps to mixed index with `Mapping.TEXT` on Elasticsearch |
| `IX-005` | YELLOW | `searchIndex().by('x').asString()` | Maps to mixed index with `Mapping.STRING` |
| `IX-006` | YELLOW | `searchIndex()` on geo property | Maps to mixed index with `Mapping.PREFIX_TREE`; auto-generated for `Point` |
| `IX-007` | RED | `schema.indexFor(...).analyze()` | DSE query-plan analysis; no equivalent in JanusGraph |
| `IX-008` | YELLOW | Vertex-centric `RelationIndex` | Maps to `buildEdgeIndex()` — direction and sort order preserved |

**Configuration Rules (CF-xxx):**

| Rule ID | Severity | Config Key | Notes |
|---------|----------|------------|-------|
| `CF-001` | YELLOW | `graph.schema_mode = Development` | JanusGraph equivalent: `schema.default=default` (auto-creates types) |
| `CF-002` | YELLOW | `graph.schema_mode = Production` | JanusGraph equivalent: `schema.default=none` (strict) |
| `CF-003` | YELLOW | `graph.allow_scan = true` | JanusGraph: no direct equivalent; ensure indexes exist or set `query.force-index=false` |
| `CF-004` | YELLOW | `traversal_sources.*.restrict_lambda = true` | JanusGraph: lambdas allowed by default; `evaluationTimeout` controls execution time |
| `CF-005` | YELLOW | `graph.system_replication_config` | JanusGraph: configure via `storage.cql.replication-strategy-*` |

**OLAP Rules (OL-xxx):**

| Rule ID | Severity | Pattern Detected | Notes |
|---------|----------|-----------------|-------|
| `OL-001` | RED | `a` traversal source usage | OLAP via DSE Spark; JanusGraph requires separate HadoopGraph + Spark deployment |
| `OL-002` | RED | `SparkGraphComputer` in traversal | Same as OL-001 |
| `OL-003` | RED | `GraphFrames` API usage | No direct equivalent in JanusGraph automated migration |

**Security Rules (SE-xxx):**

| Rule ID | Severity | Feature | Notes |
|---------|----------|---------|-------|
| `SE-001` | RED | RLAC / Row-Level Access Control | DSE Enterprise feature; JanusGraph has no row-level security — requires application-layer implementation |
| `SE-002` | RED | Row-Level Security policies | Same as SE-001 |

**TinkerPop Delta Rules (TP-xxx):**

| Rule ID | Severity | Breaking Change | Notes |
|---------|----------|----------------|-------|
| `TP-001` | YELLOW | `repeat()` step global semantics (3.4→3.7) | Add explicit `.until()` guards to prevent infinite loops |
| `TP-002` | YELLOW | `valueMap()` return format | Now returns `Map<String, List>` by default; add `.by(unfold())` |
| `TP-003` | YELLOW | `scriptEvaluationTimeout` → `evaluationTimeout` | Rename in gremlin-server.yaml |
| `TP-004` | GREEN | GraphBinary serialization | JanusGraph 1.x supports GraphBinary; update client driver to use it |
| `TP-005` | YELLOW | `property(k, v)` with no schema | TinkerPop 3.7 resolves cardinality from schema; ensure `schema.default` is set correctly |

**HCD Target Compatibility Rules (HC-xxx):**

These rules are evaluated by the assessor against the **target** HCD environment (not the source DSE cluster). They run as a pre-flight connectivity and compatibility check before migration starts. Source: `hcd-dev/cassandra.yaml` and `hcd-dev/compose.yaml`.

| Rule ID | Severity | What is Checked | Notes |
|---------|----------|----------------|-------|
| `HC-001` | YELLOW | HCD `AdvancedAuthenticator` is enabled | `storage.cql.username` and `storage.cql.password` **must** be set in JanusGraph properties; anonymous connections rejected |
| `HC-002` | YELLOW | HCD `materialized_views_enabled: false` | JanusGraph does not use CQL MVs — no conflict, but any pre-existing CQL MV creation scripts must be removed |
| `HC-003` | YELLOW | HCD `hcd_guardrail_defaults: true` enforces batch-size limits | `storage.cql.atomic-batch-mutate=false` must be set; do not enable atomic batch mutations |
| `HC-004` | YELLOW | HCD `storage_compatibility_mode: NONE` (Cassandra 5+ SSTable formats) | JanusGraph 1.x CQL driver (DataStax Java Driver 4.x) fully supports Cassandra 5 protocol — no action needed; flag as informational |
| `HC-005` | YELLOW | HCD `default_secondary_index: sai` (Storage-Attached Index) | JanusGraph creates its own internal index tables; the SAI default does not interfere. Informational — no action needed |
| `HC-006` | GREEN | HCD CQL port `9042`, datacenter `dc1` | Matches JanusGraph defaults (`storage.port=9042`, `storage.cql.local-datacenter=dc1`) — fully compatible |
| `HC-007` | YELLOW | HCD `platform: linux/amd64` — Apple Silicon emulation | If the migration tool runs on Apple Silicon (M-series), Docker emulation is used for HCD. Flag for operators; no JanusGraph config impact |
| `HC-008` | RED | IBM Container Registry authentication required | `docker login cp.icr.io --username cp` must succeed before `docker compose up`. Assessor detects if HCD is reachable on target host; if not, emits this RED finding with login instructions |

The assessor probes `HC-008` by attempting a `cqlsh` connection to the configured HCD host/port with the provided credentials. If the connection fails and the target host is a local Docker network address (e.g., `127.0.0.1` or a `docker-compose` service name), it emits `HC-008` with the ICR login instructions.

---

#### Step 0.2 — Build the `Assessor` class (`assessor.py`)

The `Assessor` orchestrates all data collection in a read-only pass against the DSE cluster.

```python
# assessor.py
from dataclasses import dataclass, field
from datetime import datetime
from gremlin_python.driver import client as gremlin_client
from cassandra.cluster import Cluster

@dataclass
class AssessmentFinding:
    rule_id:     str
    category:    str
    severity:    str          # GREEN / YELLOW / RED
    title:       str
    detail:      str          # What was found, with context (label name, line, etc.)
    remediation: str
    docs_link:   str

@dataclass
class AssessmentResult:
    graph_name:   str
    engine:       str          # classic | dsg
    assessed_at:  str
    findings:     list[AssessmentFinding] = field(default_factory=list)
    overall_risk: str = "LOW"  # LOW | MEDIUM | HIGH | CRITICAL (computed)
    summary:      dict = field(default_factory=dict)  # counts by severity/category

class Assessor:
    """Read-only compatibility assessor. Opens no write transactions."""

    def __init__(self, gremlin_url: str, cql_host: str, cql_port: int,
                 graph_name: str, username: str = None, password: str = None):
        self.gremlin_url = gremlin_url
        self.cql_host    = cql_host
        self.cql_port    = cql_port
        self.graph_name  = graph_name
        self._username   = username
        self._password   = password

    def __init__(self, gremlin_url: str, cql_host: str, cql_port: int,
                 graph_name: str,
                 target_cql_host: str = None, target_cql_port: int = 9042,
                 target_cql_username: str = "cassandra",
                 target_cql_password: str = "cassandra",
                 username: str = None, password: str = None):
        self.gremlin_url         = gremlin_url
        self.cql_host            = cql_host
        self.cql_port            = cql_port
        self.graph_name          = graph_name
        self.target_cql_host     = target_cql_host      # optional — HCD target pre-flight
        self.target_cql_port     = target_cql_port
        self.target_cql_username = target_cql_username
        self.target_cql_password = target_cql_password
        self._username           = username
        self._password           = password

    def run(self) -> AssessmentResult:
        """Execute the full assessment. Returns an AssessmentResult."""
        result = AssessmentResult(
            graph_name=self.graph_name,
            engine=self._detect_engine(),
            assessed_at=datetime.utcnow().isoformat() + "Z",
        )
        result.findings += self._assess_schema()
        result.findings += self._assess_indexes()
        result.findings += self._assess_configuration()
        result.findings += self._assess_query_patterns()
        result.findings += self._assess_olap()
        result.findings += self._assess_security()
        result.findings += self._assess_tinkerpop_delta()
        if self.target_cql_host:
            result.findings += self._assess_hcd_target()  # pre-flight against HCD
        result.overall_risk = self._compute_risk(result.findings)
        result.summary      = self._build_summary(result.findings)
        return result
```

**Collection methods — each returns `list[AssessmentFinding]`:**

1. **`_assess_schema()`** — Run `schema.describe()` via Gremlin; parse the DSL string (reuse the DSL parser from Sub-Task 2's `analyser.py`). Check every vertex label, edge label, property key, and UDT against the SC-xxx rules. Emit a finding for each match.

2. **`_assess_indexes()`** — Parse the indexes section of the `schema.describe()` output. Match each index type against the IX-xxx rules.

3. **`_assess_configuration()`** — For DSG: query `system.graph(name).describe()` to get `schema_mode`, `allow_scan`, `replication_config`, `restrict_lambda`. For Classic: read from `bdp/dse-graph/src/doc/graph_opts.yaml_template`-compatible keys. Match against CF-xxx rules.

4. **`_assess_query_patterns()`** — This is the most complex method. It must detect DSE-specific patterns without executing them:
   ```python
   def _assess_query_patterns(self) -> list[AssessmentFinding]:
       """
       Attempt to identify DSE-specific patterns by querying the Gremlin
       server's stored script cache (if accessible) or by inspecting the
       schema describe output for known markers.

       For DSG: also scan CQL `system_schema.views` for MV-based patterns.
       For both: probe the traversal source names (g, a, dev) to detect OLAP.
       """
       findings = []
       # Probe for 'a' traversal source (OLAP indicator)
       try:
           probe = self._gremlin_submit("a.V().limit(0).count()")
           if probe is not None:
               findings.append(self._make_finding("QP-005", "OLAP traversal source 'a' is active"))
       except Exception:
           pass  # Expected if OLAP not configured — no finding
       # ... etc. for each QP rule
       return findings
   ```
   > **Note:** Full query-history introspection is not possible without DSE Insights / OpsCenter. For an in-scope, lightweight approach: (a) probe traversal source availability, (b) detect search/geo usage by inspecting index types (a `searchIndex` implies Search.* predicates in application code), (c) emit YELLOW findings for _all_ discovered search and geo indexes noting that `Search.*` predicates require adaptation regardless of whether they appear in the query history.

5. **`_assess_olap()`** — Probe the `a` traversal source (see above) and check `system.graph(name).describe()` for `GraphOLAPEnabled: true`.

6. **`_assess_security()`** — For DSG: query `system.graph(name).describe()` for RLAC configuration. For Classic: query `system.graphs()` and inspect security metadata.

7. **`_assess_tinkerpop_delta()`** — Emit static TP-001 through TP-005 findings for every graph, since TinkerPop version delta affects all DSE graphs migrating to JanusGraph. Severity is YELLOW; these are advisory.

8. **`_assess_hcd_target()`** — Called only when `--target-cql-host` is provided. Probes the HCD target cluster and emits HC-xxx findings. This is a **target-side pre-flight**, not a source DSE analysis:

   ```python
   def _assess_hcd_target(self) -> list[AssessmentFinding]:
       """
       Pre-flight checks against the HCD 2.x target cluster.
       Attempts a CQL connection; if it fails, emits HC-008 (ICR auth required).
       If it succeeds, introspects cassandra.yaml-equivalent system tables to
       detect AdvancedAuthenticator, guardrail defaults, and platform metadata.
       """
       findings = []
       try:
           from cassandra.cluster import Cluster
           from cassandra.auth import PlainTextAuthProvider
           auth = PlainTextAuthProvider(self.target_cql_username, self.target_cql_password)
           cluster = Cluster(
               [self.target_cql_host],
               port=self.target_cql_port,
               auth_provider=auth,
               connect_timeout=10
           )
           session = cluster.connect()

           # HC-001: AdvancedAuthenticator — connection itself confirms auth is required;
           # if we reached here with credentials, auth is working → emit YELLOW (informational)
           findings.append(self._make_finding(
               "HC-001",
               "HCD AdvancedAuthenticator confirmed active — credentials accepted. "
               "Ensure JanusGraph config sets storage.cql.username and storage.cql.password."
           ))

           # HC-002: materialized_views_enabled
           rows = session.execute(
               "SELECT value FROM system_views.settings WHERE name='materialized_views_enabled'"
           )
           mv_enabled = next((r.value for r in rows), "false")
           if mv_enabled.lower() == "false":
               findings.append(self._make_finding(
                   "HC-002",
                   "HCD materialized_views_enabled=false confirmed — JanusGraph will not create CQL MVs."
               ))

           # HC-003, HC-004, HC-005, HC-006 emitted as static advisory findings
           for rule_id in ("HC-003", "HC-004", "HC-005", "HC-006"):
               findings.append(self._make_finding(rule_id, "HCD target pre-flight advisory."))

           # HC-007: Apple Silicon detection
           import platform as plat
           if plat.machine() == "arm64":
               findings.append(self._make_finding(
                   "HC-007",
                   "Apple Silicon (arm64) detected — HCD linux/amd64 image runs under Rosetta emulation."
               ))

           cluster.shutdown()

       except Exception as e:
           # HC-008: Cannot connect to HCD target — likely ICR auth / image not pulled
           findings.append(self._make_finding(
               "HC-008",
               f"Cannot connect to HCD target at {self.target_cql_host}:{self.target_cql_port}. "
               f"Error: {e}. "
               "If running locally, ensure docker login cp.icr.io --username cp succeeded "
               "and the HCD container is healthy (docker compose ps)."
           ))

       return findings
   ```

**Risk computation:**
```python
def _compute_risk(self, findings: list[AssessmentFinding]) -> str:
    """
    CRITICAL: any RED finding in SE (security) category
    HIGH:     any RED finding in any category
    MEDIUM:   3+ YELLOW findings, or any YELLOW in OL (OLAP) category
    LOW:      all GREEN or fewer than 3 YELLOW
    """
    reds   = [f for f in findings if f.severity == "RED"]
    yellows = [f for f in findings if f.severity == "YELLOW"]
    se_reds = [f for f in reds if f.category == "security"]

    if se_reds:
        return "CRITICAL"
    if reds:
        return "HIGH"
    if any(f.category == "olap" for f in yellows) or len(yellows) >= 3:
        return "MEDIUM"
    return "LOW"
```

---

#### Step 0.3 — Build the `AssessmentReporter` class (`assessment_reporter.py`)

Renders an `AssessmentResult` into the three output formats.

```python
# assessment_reporter.py

class AssessmentReporter:
    def __init__(self, result: AssessmentResult, output_dir: str = "."):
        self.result     = result
        self.output_dir = output_dir

    def write_all(self) -> dict[str, str]:
        """Write MD, JSON, HTML and return dict of {format: filepath}."""
        return {
            "markdown": self._write_markdown(),
            "json":     self._write_json(),
            "html":     self._write_html(),
        }

    def _write_markdown(self) -> str: ...
    def _write_json(self)     -> str: ...
    def _write_html(self)     -> str: ...
```

**Markdown report structure** (`<graph-name>-assessment-report.md`):

```markdown
# Pre-Migration Compatibility Assessment Report
## Graph: <graph-name>  ·  Engine: dsg  ·  Assessed: 2026-11-07T16:31:30Z

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Overall Risk Level | 🔴 HIGH |
| Total Findings | 14 |
| Blockers (RED) | 3 |
| Warnings (YELLOW) | 9 |
| Compatible (GREEN) | 2 |
| Effort Estimate | 2–4 weeks (see Roadmap) |

### Key Findings
- 🔴 **3 Blockers** require resolution before migration can proceed.
- ⚠️  OLAP (`a` traversal source) detected — requires separate HadoopGraph/Spark setup.
- ⚠️  2 `Search.*` predicates in use — application code must be updated to use `Text.*` equivalents.
- ✅  Schema (vertex/edge labels, properties) is 92% compatible.

---

## 1. Cluster Overview

| Property | Value |
|----------|-------|
| Graph Name | food |
| Engine | DSG (Core) |
| DSE Version | 6.8.48 |
| Vertex Labels | 5 |
| Edge Labels | 8 |
| Property Keys | 23 |
| Indexes | 12 (3 searchIndex, 4 materializedView, 2 secondaryIndex, 3 vertexCentric) |
| Vertices (approx.) | 12,500 |
| Edges (approx.) | 35,000 |
| Schema Mode | Production |
| Allow Scan | false |
| OLAP Enabled | true |

---

## 2. Query Pattern Analysis

### 2.1 DSE-Specific Patterns Detected

| Pattern | Occurrences | Severity | JanusGraph Action |
|---------|------------|----------|--------------------|
| OLAP traversal source `a` | Detected (active) | 🔴 RED | Requires HadoopGraph + Spark setup (OL-001) |
| `searchIndex().asText()` (implies Search.token) | 2 labels | 🟡 YELLOW | Update to `Text.textContains()` + mixed index (IX-004) |
| `g.with('allow-filtering')` | Schema indicates possible use | 🟡 YELLOW | Remove; ensure indexes exist (QP-001) |
| `Geo.inside()` | 1 label (location) | 🟡 YELLOW | Use `Geo.within(Geoshape.circle(...))` (QP-010) |

### 2.2 TinkerPop 3.4 → 3.7 Delta

| Change | Risk | Action |
|--------|------|--------|
| `repeat()` global semantics | 🟡 YELLOW | Add `.until()` guards |
| `valueMap()` format | 🟡 YELLOW | Add `.by(unfold())` |
| `scriptEvaluationTimeout` renamed | 🟡 YELLOW | Update gremlin-server.yaml |

---

## 3. Schema Analysis

### 3.1 Vertex Labels

| Label | Properties | Partition Keys | Collections | UDTs | Compatibility |
|-------|-----------|----------------|------------|------|---------------|
| person | name, age, location (Point) | person_id | nickname (setOf) | address | ✅ with geo index auto-gen |
| recipe | title, instructions, cuisine | recipe_id | tags (setOf) | — | ✅ |
| meal | name, date | meal_id | courses (listOf) | — | ✅ |

### 3.2 Edge Labels

| Label | From → To | Properties | Compatibility |
|-------|-----------|-----------|---------------|
| knows | person → person | since | ✅ |
| created | person → recipe | date | ✅ |
| ate | person → meal | portion | ✅ |

### 3.3 Type Conversion Summary

| DSE Type | Count | JanusGraph Mapping | Action |
|----------|------|--------------------|--------|
| Text | 12 | java.lang.String | ✅ Auto |
| Uuid | 4 | java.util.UUID | ✅ Auto |
| Point | 1 | java.lang.String + geo index | ✅ Auto |
| setOf(Text) | 3 | Cardinality.SET | ✅ Auto |
| mapOf(Text,Date) | 1 | flat: _key/_value LIST | ⚠️ Review --map-strategy |
| UDT address | 1 | flat: address_street, address_zip | ⚠️ Review --udt-strategy |

---

## 4. Index Analysis

| Name | Type | Label | Compatibility | Action |
|------|------|-------|---------------|--------|
| person_by_name | materializedView | person | ✅ → Composite Index | Auto |
| recipe_search | searchIndex (text) | recipe | ⚠️ → Mixed Index (ES) | Auto |
| location_geo | searchIndex (geo) | location | ✅ → Mixed Index (PREFIX_TREE) | Auto |
| knows_by_since | vertex-centric | knows | ✅ → buildEdgeIndex() | Auto |

---

## 5. Configuration Compatibility

| DSE Config | Value | JanusGraph Equivalent | Action |
|-----------|-------|----------------------|--------|
| schema_mode | Production | schema.default=none | ⚠️ Set in JG properties |
| allow_scan | false | query.force-index=true | ✅ Compatible |
| replication_config | NTS, dc1:3 | storage.cql.replication-strategy-options=dc1,3 | ✅ Compatible |
| restrict_lambda | true | evaluationTimeout (ms) | ⚠️ Review timeout value |

---

## 6. Migration Readiness Assessment

### 6.1 Blockers (must resolve before migration)

| # | Rule | Finding | Remediation |
|---|------|---------|-------------|
| 1 | OL-001 | OLAP `a` traversal source active | Deploy JanusGraph + HadoopGraph separately; rewrite OLAP jobs using TinkerPop SparkGraphComputer |
| 2 | SE-001 | RLAC configured on graph | Implement application-layer access control; no automatic migration path |

### 6.2 Warnings (migration proceeds; application changes needed)

| # | Rule | Finding | Remediation |
|---|------|---------|-------------|
| 1 | QP-001 | allow-filtering usage likely | Remove `g.with('allow-filtering')` from application queries; validate indexes |
| 2 | IX-004 | searchIndex on recipe label | Mixed index auto-created; update app code from `Search.token()` to `Text.textContains()` |
| ... | | | |

### 6.3 Recommendations

- Run the migration in a non-production environment first.
- Use `--parallel-labels 4` only after verifying JanusGraph heap headroom.
- After migration, run `dse-jg-migrator validate` to confirm all counts match.

---

## 7. Detailed Migration Roadmap

### Phase 0 — Pre-migration (owner: DBA / Graph Engineer)
1. Resolve all RED blockers above.
2. Confirm Elasticsearch/OpenSearch co-deployment with HCD.
3. Provision JanusGraph keyspace with correct replication.

### Phase 1 — Schema migration
1. Run `dse-jg-migrator analyse` to extract full schema.
2. Run `dse-jg-migrator translate --udt-strategy flat --map-strategy flat`.
3. Review `<graph>-translation-report.md`.
4. Run `dse-jg-migrator init-target`.
5. Verify `mgmt.printSchema()` on JanusGraph.

### Phase 2 — Data migration
1. Run `dse-jg-migrator migrate` with `--cql-fetch-size 2000 --write-batch 200`.
2. Monitor checkpoint file progress.
3. Run `dse-jg-migrator validate`.

### Phase 3 — Application cutover
1. Update Gremlin connection string to JanusGraph server.
2. Replace DSE-specific patterns (see Query Pattern Analysis above).
3. Test `repeat()` traversals with added `.until()` guards.
4. Update `valueMap()` calls to include `.by(unfold())`.
5. Decommission DSE Graph cluster after 2-week parallel run.

### Rollback Strategy
- Schema init is idempotent; re-run `init-target` is safe.
- Data migration checkpoint: resume from last committed offset.
- Full rollback: drop JanusGraph keyspace in HCD (`DROP KEYSPACE <name>`); re-run from Phase 1.

---
*Report generated by `dse-jg-migrator assess`. Source cluster not modified.*
```

---

#### Step 0.4 — Define the assessment JSON schema

The `<graph-name>-assessment-report.json` must be machine-parseable for downstream tooling and for the `run-all` gate check:

```json
{
  "schema_version": "1.0",
  "graph_name": "food",
  "engine": "dsg",
  "assessed_at": "2026-11-07T16:31:30Z",
  "overall_risk": "HIGH",
  "summary": {
    "total_findings": 14,
    "by_severity": {"RED": 3, "YELLOW": 9, "GREEN": 2},
    "by_category": {
      "query_pattern": 5,
      "schema": 3,
      "index": 3,
      "configuration": 2,
      "olap": 1,
      "security": 1,
      "tinkerpop_delta": 5,
      "data_type": 2
    }
  },
  "cluster_overview": {
    "dse_version": "6.8.48",
    "vertex_label_count": 5,
    "edge_label_count": 8,
    "property_key_count": 23,
    "index_count": 12,
    "approx_vertex_count": 12500,
    "approx_edge_count": 35000,
    "schema_mode": "Production",
    "allow_scan": false,
    "olap_enabled": true
  },
  "findings": [
    {
      "rule_id": "OL-001",
      "category": "olap",
      "severity": "RED",
      "title": "OLAP traversal source 'a' is active",
      "detail": "The 'a' traversal source responds to queries. This indicates active OLAP/Spark usage.",
      "remediation": "Deploy JanusGraph with HadoopGraph and Apache Spark. Rewrite OLAP jobs.",
      "docs_link": "https://docs.janusgraph.org/advanced-topics/hadoop/"
    }
  ]
}
```

---

#### Step 0.5 — Wire the `assess` CLI command and `--analyze-only` flag

**In `cli.py`:**

```python
@main.command()
@click.option('--source-gremlin-url', required=True,
              help='WebSocket URL of the source DSE Gremlin server (ws://<host>:8182/gremlin)')
@click.option('--source-cql-host',    required=True,
              help='CQL host of the source DSE cluster.')
@click.option('--source-cql-port',    default=9042, show_default=True)
@click.option('--graph-name',         required=True)
@click.option('--output-dir',         default='.', show_default=True)
# ── Target HCD pre-flight (optional but recommended) ────────────────────────
@click.option('--target-cql-host', default=None,
              help='CQL host of the target HCD cluster. When provided, runs HCD pre-flight '
                   'checks (HC-001 through HC-008) including ICR auth probe and guardrail detection.')
@click.option('--target-cql-port',     default=9042,       show_default=True)
@click.option('--target-cql-username', default='cassandra', show_default=True,
              help='HCD CQL username (AdvancedAuthenticator). Default: cassandra.')
@click.option('--target-cql-password', default='cassandra', show_default=True,
              hide_input=True, help='HCD CQL password.')
# ── Output and filtering ─────────────────────────────────────────────────────
@click.option('--format', 'output_formats',
              type=click.Choice(['md', 'json', 'html', 'all']),
              multiple=True, default=['all'], show_default=True,
              help='Output format(s). Pass multiple times or use "all".')
@click.option('--filter-category',
              type=click.Choice(['query_pattern','schema','index',
                                 'configuration','olap','security',
                                 'tinkerpop_delta','data_type',
                                 'hcd_target','all']),   # hcd_target added
              default='all', show_default=True,
              help='Restrict findings to a specific category.')
@click.option('--filter-severity',
              type=click.Choice(['RED','YELLOW','GREEN','all']),
              default='all', show_default=True,
              help='Restrict findings to a minimum severity.')
@click.option('--username', default=None, help='DSE source authentication username.')
@click.option('--password', default=None, help='DSE source authentication password.',
              hide_input=True)
def assess(source_gremlin_url, source_cql_host, source_cql_port,
           graph_name, output_dir,
           target_cql_host, target_cql_port, target_cql_username, target_cql_password,
           output_formats, filter_category, filter_severity, username, password):
    """
    Run a read-only pre-migration compatibility assessment.

    Connects to the source DSE Graph cluster and (optionally) the target HCD 2.x
    cluster to analyse schema, query patterns, indexes, configuration, and HCD
    pre-flight requirements against JanusGraph's capability matrix.

    Produces a structured report in Markdown, JSON, and HTML formats.

    This command is NON-DESTRUCTIVE and can be run repeatedly.

    Example (source-only):
      dse-jg-migrator assess \\
        --source-gremlin-url ws://localhost:8183/gremlin \\
        --source-cql-host localhost --source-cql-port 9043 \\
        --graph-name food

    Example (source + HCD target pre-flight):
      dse-jg-migrator assess \\
        --source-gremlin-url ws://localhost:8183/gremlin \\
        --source-cql-host localhost --source-cql-port 9043 \\
        --target-cql-host localhost --target-cql-port 9042 \\
        --graph-name food
    """
    from .assessor import Assessor
    from .assessment_reporter import AssessmentReporter

    assessor = Assessor(
        gremlin_url=source_gremlin_url,
        cql_host=source_cql_host,
        cql_port=source_cql_port,
        graph_name=graph_name,
        target_cql_host=target_cql_host,
        target_cql_port=target_cql_port,
        target_cql_username=target_cql_username,
        target_cql_password=target_cql_password,
        username=username,
        password=password,
    )
    console.print(f"[bold]Assessing graph:[/bold] {graph_name} at {source_gremlin_url}")
    with console.status("Running compatibility assessment (read-only)..."):
        result = assessor.run()

    # Apply filters
    if filter_category != 'all':
        result.findings = [f for f in result.findings if f.category == filter_category]
    if filter_severity != 'all':
        order = {"RED": 3, "YELLOW": 2, "GREEN": 1}
        min_level = order[filter_severity]
        result.findings = [f for f in result.findings if order[f.severity] >= min_level]

    reporter = AssessmentReporter(result, output_dir=output_dir)
    paths = reporter.write_all()

    _print_risk_banner(result.overall_risk, result.summary)
    for fmt, path in paths.items():
        console.print(f"  [green]✓[/green] {fmt:8s} → {path}")

    # Gate: exit non-zero if HIGH/CRITICAL so CI pipelines can fail
    if result.overall_risk in ("HIGH", "CRITICAL"):
        console.print(
            "[red]Assessment gate FAILED.[/red] Resolve RED findings before running migration. "
            "Pass --skip-assessment-gate to override."
        )
        raise SystemExit(1)
```

**In the `run-all` command** — add gate logic:

```python
@main.command('run-all')
@click.option('--analyze-only', is_flag=True, default=False,
              help='Run only the assessment phase (Step 0) and exit. No data is migrated.')
@click.option('--skip-assessment-gate', is_flag=True, default=False,
              help='Proceed with migration even if assessment risk is HIGH or CRITICAL.')
@click.pass_context
def run_all(ctx, analyze_only, skip_assessment_gate, **kwargs):
    """Run the full migration pipeline. Assessment (Step 0) runs first as a gate."""
    ctx.invoke(assess, ...)

    if analyze_only:
        console.print("[bold]--analyze-only:[/bold] Stopping after assessment.")
        return

    # Load assessment result to check gate
    assessment_path = f"{graph_name}-assessment-report.json"
    risk = _load_risk(assessment_path)
    if risk in ("HIGH", "CRITICAL") and not skip_assessment_gate:
        console.print(
            f"[red]Migration blocked:[/red] assessment risk is {risk}. "
            "Resolve RED findings or pass --skip-assessment-gate."
        )
        raise SystemExit(1)

    ctx.invoke(analyse, ...)
    ctx.invoke(translate, ...)
    ctx.invoke(init_target, ...)
    ctx.invoke(migrate, ...)
    ctx.invoke(validate, ...)
    ctx.invoke(report, ...)
```

---

#### Step 0.6 — Unit tests (`tests/test_assessor.py`, `tests/test_compatibility_rules.py`)

1. **`test_compatibility_rules.py`** — Verify the complete rule catalogue loads without errors; assert that every rule has a non-empty `remediation` and `docs_link`; assert rule IDs are unique.

2. **`tests/test_assessor.py`** — Mock the Gremlin client and CQL session. Provide a canned `schema.describe()` response containing every DSE pattern (one per rule category). Assert that `Assessor.run()` returns exactly the expected `AssessmentFinding` instances. Test `_compute_risk()` for each threshold case.

3. **`tests/test_assessment_reporter.py`** — Given a synthetic `AssessmentResult` with known findings, assert Markdown output contains the expected table headers; assert JSON output is valid and matches the schema in Step 0.4; assert HTML output contains `<table>` tags.

---

### Relevant Context

| Source | Location | Key information used |
|--------|----------|--------------------|
| DSE search predicates | `bdp/dse-graph/src/test/java/com/datastax/bdp/graph/server/GremlinServerIntegrationTest.java:471–483` | `Search.prefix`, `Search.fuzzy`, `Search.token` usage |
| DSE geo predicates | `dse-docs-new/modules/graph/examples/food/TRAVERSALS/geo_specific_point.gremlin` | `Geo.inside(Geo.point(...), radius)` pattern |
| OLAP traversal source | `bdp/dse-graph/src/main/java/com/datastax/bdp/graph/server/TraversalSourceManager.java:170–173` | `a` traversal source activation |
| Configuration template | `bdp/dse-graph/src/doc/graph_opts.yaml_template` | `schema_mode`, `allow_scan`, `restrict_lambda` keys |
| Schema mode test | `bdp/dse-graph/src/test/java/com/datastax/bdp/graph/server/GremlinServerIntegrationTest.java:250,264` | Development vs Production mode |
| TinkerPop 3.4→3.8 changes | `janusgraph/janusgraph-core` — TinkerPop 3.8.2 dependency | `repeat()` semantics, `valueMap()` format |
| JanusGraph Text predicates | `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/attribute/Text.java` | `textContains`, `textContainsPrefix`, `textContainsFuzzy` |
| JanusGraph Geo predicates | `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/attribute/Geo.java` | `within`, `geoIntersect`, `Geoshape` |
| JanusGraph Mapping enum | `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/Mapping.java` | `TEXT`, `STRING`, `PREFIX_TREE` |
| DSE schema syntax | `dse-docs-new/modules/graph/examples/food/` | All `.gremlin` example files |

---

### Output file summary for Sub-Task 0

| File | Format | Consumer |
|------|--------|---------|
| `<graph>-assessment-report.md` | Markdown | Human review, sharing |
| `<graph>-assessment-report.json` | JSON (schema v1.0) | `run-all` gate, CI/CD, programmatic integration |
| `<graph>-assessment-report.html` | Standalone HTML | Browser viewing, email sharing |

---


## Sub-Task 1 — Project Scaffold

**Status:** `[ ] pending`

### Intent
Create the Python project structure, dependency manifest, and development environment for the `dse-jg-migrator` tool.

### Expected Outcomes
- `migrator/` Python package with runnable entry point.
- `pyproject.toml` (or `requirements.txt`) with pinned dependencies.
- `README.md` in the project root explaining usage.
- Passing `python -m dse_jg_migrator --help` invocation.

### Todo List
1. Create directory structure:
   ```
   dse-jg-migrator/
   ├── pyproject.toml
   ├── README.md
   ├── dse_jg_migrator/
   │   ├── __init__.py
   │   ├── cli.py                    # Click entry point
   │   ├── assessor.py               # NEW: Pre-migration compatibility engine
   │   ├── compatibility_rules.py    # NEW: Static rule catalogue (GREEN/YELLOW/RED)
   │   ├── assessment_reporter.py    # NEW: MD / JSON / HTML report renderer
   │   ├── analyser.py               # DSE Graph introspection
   │   ├── translator.py             # Schema mapping logic
   │   ├── initialiser.py            # JanusGraph target setup
   │   ├── migrator.py               # Data movement
   │   ├── validator.py              # Post-migration checks
   │   ├── reporter.py               # Migration report generation
   │   ├── models.py                 # Dataclasses for schema elements
   │   └── config.py                 # Configuration dataclasses
   ├── tests/
   │   ├── test_assessor.py          # NEW
   │   ├── test_compatibility_rules.py  # NEW
   │   ├── test_analyser.py
   │   ├── test_translator.py
   │   └── test_models.py
   └── examples/
       ├── food-graph-schema.json         # Example extracted schema
       ├── food-assessment-report.md      # NEW: Example assessment output
       ├── food-assessment-report.json    # NEW: Machine-parseable assessment
       └── janusgraph-target.json         # Example translated schema
   ```
2. Write `pyproject.toml`:
   ```toml
   [build-system]
   requires = ["setuptools>=68"]
   build-backend = "setuptools.backends.legacy:build"

   [project]
   name = "dse-jg-migrator"
   version = "0.1.0"
   requires-python = ">=3.11"
   dependencies = [
       "click>=8.1",
       "gremlinpython>=3.7.0",
       "cassandra-driver>=3.29.0",
       "rich>=13.0",
       "pydantic>=2.0",
   ]

   [project.scripts]
   dse-jg-migrator = "dse_jg_migrator.cli:main"
   ```
3. Define `models.py` dataclasses (see [Sub-Task 2](#sub-task-2--dse-graph-analyser) for field inventory).
4. Define `config.py` connection configuration dataclasses.
5. Implement stub `cli.py` with top-level command groups: `analyse`, `translate`, `migrate`, `validate`, `report`.

### Relevant Context
- No existing Python code in the repo; this is greenfield.
- `janusgraph/janusgraph-examples/example-cql/conf/jgex-cql.properties` shows the JanusGraph CQL connection properties that `config.py` must capture.
- The tool must work against DSE 6.7 (Classic) and DSE 6.8+ (DSG/Core) Gremlin servers.

---

## Sub-Task 2 — DSE Graph Analyser

**Status:** `[ ] pending`

### Intent
Connect to the source DSE Graph cluster and extract a complete, structured description of the graph schema, configuration, and basic statistics (vertex/edge counts per label). This is the *read-only discovery phase* — no writes to either cluster.

### Expected Outcomes
- `dse_jg_migrator analyse --source-host <h> --source-port 8182 --graph-name <g>` produces a `<graph-name>-analysis.json` file.
- The JSON file contains: graph engine type, all vertex labels (with property keys, cardinality, data types, and partition/clustering key info), all edge labels (with property keys and directionality), all index definitions, all UDTs, graph configuration, and per-label vertex/edge counts.
- The tool distinguishes Classic vs DSG by probing via Gremlin.

### Todo List

1. **Detect engine type.** Send `schema.describe()` via Gremlin. DSG returns a Groovy DSL string; Classic returns a different format. Alternatively query `system.graph('<name>').describe()`.

   ```python
   # In analyser.py
   from gremlin_python.driver import client, serializer

   def detect_engine(g_client) -> str:
       """Returns 'classic' or 'dsg'."""
       result = g_client.submit("system.graph('%s').describe()" % graph_name)
       desc = result.all().result()
       # DSG graphs show engine=Core in the describe output
       if "Core" in str(desc) or "DSG" in str(desc):
           return "dsg"
       return "classic"
   ```

2. **Extract schema via Gremlin DSL.** Both Classic and DSG expose `schema.describe()` which returns the full schema as a Groovy string. This is the primary extraction mechanism.

   ```python
   def extract_raw_schema(g_client, graph_name: str) -> str:
       """Returns the raw schema DSL string from DSE Graph."""
       result = g_client.submit(
           "schema.describe()",
           request_options={"bindings": {}, "aliases": {"g": f"{graph_name}.g"}}
       )
       return "\n".join(str(r) for r in result.all().result())
   ```

3. **Parse the DSL string.** Use regex + string parsing (NOT a full Groovy parser — keep it minimal) to extract:
   - `schema.vertexLabel('X')` blocks → vertex labels
   - `schema.edgeLabel('X')` blocks → edge labels
   - `.partitionBy(...)`, `.clusterBy(...)`, `.property(...)` clauses
   - Index creation blocks (`.materializedView(...)`, `.searchIndex()`, `.secondaryIndex()`)
   - UDT definitions (`schema.type('X')`)

   **Reference schema syntax** (from `dse-docs-new/modules/graph/`):
   ```groovy
   // Vertex label example
   schema.vertexLabel('person').
     ifNotExists().
     partitionBy('person_id', Uuid).
     property('name', Text).
     property('nickname', setOf(Text)).
     property('macro_goal', listOf(Int)).
     property('badge', mapOf(Text, Date)).
     create()

   // Edge label example
   schema.edgeLabel('knows').
     ifNotExists().
     from('person').to('person').
     property('since', Date).
     create()

   // Materialized view index example
   schema.vertexLabel('person').
     materializedView('person_by_name').
     ifNotExists().
     partitionBy('name').
     clusterBy('person_id', Asc).
     create()

   // Search index example
   schema.vertexLabel('recipe').
     searchIndex().
     ifNotExists().
     by('instructions').asText().
     by('cuisine').asString().
     create()
   ```

4. **For DSG: supplement with CQL introspection.** Connect via `cassandra-driver` to read the raw CQL table definitions — these provide ground-truth column types.
   ```python
   from cassandra.cluster import Cluster
   from cassandra.metadata import KeyspaceMetadata

   def extract_cql_schema(host: str, port: int, keyspace: str) -> dict:
       cluster = Cluster([host], port=port)
       session = cluster.connect()
       ks_meta: KeyspaceMetadata = cluster.metadata.keyspaces[keyspace]
       tables = {}
       for table_name, table_meta in ks_meta.tables.items():
           tables[table_name] = {
               "columns": {col: str(meta.cql_type) for col, meta in table_meta.columns.items()},
               "partition_key": [c.name for c in table_meta.partition_key],
               "clustering_key": [c.name for c in table_meta.clustering_key],
           }
       return tables
   ```

5. **Collect statistics.** For each vertex label and edge label, run a count query:
   ```groovy
   g.V().hasLabel('person').count()  // OLTP count (use with caution on large graphs)
   ```
   Or for DSG, prefer CQL: `SELECT count(*) FROM <keyspace>.<table>` — this is faster and does not tax the graph engine.

6. **Output `<graph-name>-analysis.json`** with the following structure:
   ```json
   {
     "graph_name": "food",
     "engine": "dsg",
     "extracted_at": "2026-11-07T00:51:01Z",
     "vertex_labels": [
       {
         "label": "person",
         "partition_keys": [{"name": "person_id", "type": "Uuid"}],
         "clustering_keys": [],
         "properties": [
           {"name": "name", "type": "Text", "cardinality": "SINGLE"},
           {"name": "nickname", "type": "Text", "collection": "set"},
           {"name": "badge", "type": "mapOf(Text, Date)", "collection": "map"}
         ],
         "count": 12500
       }
     ],
     "edge_labels": [
       {
         "label": "knows",
         "from_label": "person",
         "to_label": "person",
         "properties": [{"name": "since", "type": "Date", "cardinality": "SINGLE"}],
         "count": 35000
       }
     ],
     "udts": [
       {"name": "address", "fields": [{"name": "street", "type": "Text"}]}
     ],
     "indexes": [
       {
         "kind": "materializedView",
         "name": "person_by_name",
         "element": "vertex",
         "label": "person",
         "keys": [{"name": "name", "as": null}]
       },
       {
         "kind": "searchIndex",
         "name": "recipe_search",
         "element": "vertex",
         "label": "recipe",
         "keys": [
           {"name": "instructions", "as": "text"},
           {"name": "cuisine", "as": "string"}
         ]
       }
     ],
     "config": {
       "schema_mode": "Production",
       "replication": "{'class': 'SimpleStrategy', 'replication_factor': 1}"
     }
   }
   ```

### Relevant Context
- DSE Gremlin server runs on port `8182` by default (config in `bdp/dse-graph/src/test/resources/gremlin_server_settings.yaml`).
- Gremlin Python client: `gremlin_python.driver.client.Client(url, traversal_source)`.
- For Classic graphs, `schema.describe()` is the canonical way to get full schema — it returns a multi-line Groovy string.
- For DSG, `schema.describe()` also works; the CQL introspection via `cassandra-driver` supplements it with column-level type accuracy.
- `dse-docs-new/modules/graph/pages/` documents `schema.describe()` return format.

---

## Sub-Task 3 — Schema Translator

**Status:** `[ ] pending`

### Intent
Transform the `<graph-name>-analysis.json` (DSE schema representation) into a JanusGraph-compatible JSON schema definition consumable by `JsonSchemaInitStrategy`. This is a pure data transformation — no network calls required.

### Expected Outcomes
- `dse_jg_migrator translate --analysis <graph-name>-analysis.json` produces `<graph-name>-janusgraph-schema.json`.
- All vertex labels, edge labels, property keys, composite indexes, mixed indexes, and vertex-centric indexes are represented.
- A `<graph-name>-translation-report.md` is produced listing every mapping decision, every gap, and every type that required manual review.

### Todo List

1. **Map property key data types.** See [Appendix A](#appendix-a--data-type-mapping-table) for the complete mapping. Implement `map_type(dse_type: str) -> str` in `translator.py`:

   ```python
   TYPE_MAP = {
       "Text": "java.lang.String",
       "Varchar": "java.lang.String",
       "Ascii": "java.lang.String",
       "Int": "java.lang.Integer",
       "Bigint": "java.lang.Long",
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
       # Geospatial — mapped to String (WKT) for the property key;
       # a companion mixed index with geo_point mapping is created automatically.
       "Point":      "java.lang.String",   # stored as WKT; geo_point mixed index added
       "Polygon":    "java.lang.String",   # stored as WKT; no geo index (non-point types)
       "Linestring": "java.lang.String",   # stored as WKT; no geo index
   }

   # Property keys whose DSE type was geospatial — the translator creates an
   # additional mixed index entry with Geo mapping for Point properties.
   GEO_TYPES = {"Point", "Polygon", "Linestring"}
   ```

2. **Map cardinality.** DSE `SINGLE` → JG `SINGLE`. DSE `setOf(X)` → JG `SET`. DSE `listOf(X)` → JG `LIST`. DSE `mapOf(K, V)` → `LIST` with a note that maps are not natively supported; store as serialised JSON string property or skip.

3. **Map vertex labels.** Every DSE vertex label → JanusGraph vertex label entry:
   ```json
   {
     "label": "person",
     "staticVertex": false,
     "partition": false
   }
   ```
   Note: JanusGraph vertex labels have no partition-key concept at the schema level — the CQL storage layout is JanusGraph-internal. The DSE partition/clustering keys become regular property keys in JanusGraph (the graph engine manages storage).

4. **Map edge labels.** DSE edge label → JanusGraph edge label:
   ```json
   {
     "label": "knows",
     "multiplicity": "MULTI",
     "unidirected": false
   }
   ```
   Default multiplicity: `MULTI` (allows multiple edges between same pair). If a DSG edge label has an `inverse()` materialised view, set `unidirected: false` (bidirectional traversal supported via both the main and inverse tables).

5. **Map composite indexes.** DSE `materializedView` → JanusGraph composite index:
   ```json
   {
     "name": "person_by_name",
     "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
     "indexOnly": "person",
     "unique": false,
     "keys": [{"propertyKey": "name"}]
   }
   ```

6. **Map search indexes.** DSE `searchIndex().by('X').asText()` → JanusGraph mixed index keyed to Elasticsearch/OpenSearch:
   ```json
   {
     "name": "recipe_search",
     "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
     "indexOnly": "recipe",
     "indexBackend": "search",
     "keys": [
       {
         "propertyKey": "instructions",
         "parameters": [
           {"key": "mapping", "value": "org.janusgraph.core.schema.Mapping.TEXT", "parser": "enum"}
         ]
       },
       {
         "propertyKey": "cuisine",
         "parameters": [
           {"key": "mapping", "value": "org.janusgraph.core.schema.Mapping.STRING", "parser": "enum"}
         ]
       }
     ]
   }
   ```

   **Geospatial `Point` properties** must be accompanied by an **auto-generated** geo mixed index. When the translator encounters a property key whose DSE type was `Point`, it automatically adds:
   ```json
   {
     "name": "<vertex_label>_geo_<property_name>",
     "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
     "indexOnly": "<vertex_label>",
     "indexBackend": "search",
     "keys": [
       {
         "propertyKey": "<property_name>",
         "parameters": [
           {
             "key": "mapping",
             "value": "org.janusgraph.core.schema.Mapping.PREFIX_TREE",
             "parser": "enum"
           }
         ]
       }
     ]
   }
   ```
   The property key itself is stored as `java.lang.String` (WKT format). JanusGraph's `Geo.within()` predicate then works against this mixed index for point-in-polygon and distance queries. Data migration converts DSE `Point` values (e.g., `POINT(30 10)`) to WKT strings unchanged, since DSE already uses WKT notation.

   `Polygon` and `Linestring` types are stored as WKT strings but do **not** receive a geo index automatically — geo predicates on those types require manual Elasticsearch mapping configuration. The translation report lists them under "Requires Manual Geo Index Setup".

   See [Appendix B](#appendix-b--index-type-mapping-table) for the complete mapping.

7. **Map vertex-centric indexes.** DSE `RelationIndex` / built-in edge table ordering → JanusGraph vertex-centric edge index:
   ```json
   {
     "name": "knows_by_since",
     "indexedEdgeLabel": "knows",
     "direction": "BOTH",
     "propertyKeys": ["since"],
     "order": "asc"
   }
   ```

8. **Handle UDTs.** JanusGraph has no native UDT support. The default strategy is `flat`: each UDT field becomes a separate JanusGraph property key named `<udt_name>_<field_name>` (e.g., a UDT `address{street, zip}` on vertex label `location` produces property keys `address_street` and `address_zip`). The `--udt-strategy json` flag switches to serialising the entire UDT value as a JSON string on a single property key named `<udt_name>` (type `java.lang.String`). A WARNING is always emitted and the UDT is listed in the translation report.

   ```python
   # translator.py — UDT expansion (flat strategy, the default)
   def expand_udt(udt_name: str, udt_fields: list[dict], strategy: str = "flat") -> list[dict]:
       """Return a list of JanusGraph property key definitions for a UDT."""
       if strategy == "json":
           return [{"key": udt_name, "className": "java.lang.String", "cardinality": "SINGLE"}]
       # flat: one property key per UDT field
       result = []
       for field in udt_fields:
           jg_type = TYPE_MAP.get(field["type"], "java.lang.String")
           result.append({
               "key": f"{udt_name}_{field['name']}",
               "className": jg_type,
               "cardinality": "SINGLE",
               "_source_udt": udt_name,       # metadata only, stripped before output
               "_source_field": field["name"],
           })
       return result
   ```

9. **Output `<graph-name>-janusgraph-schema.json`** in the format expected by `JsonSchemaInitStrategy`:
   ```json
   {
     "propertyKeys": [...],
     "vertexLabels": [...],
     "edgeLabels": [...],
     "compositeIndexes": [...],
     "mixedIndexes": [...],
     "vertexCentricEdgeIndexes": [...],
     "vertexCentricPropertyIndexes": []
   }
   ```
   Source: `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/json/definition/JsonSchemaDefinition.java`.

10. **Emit a translation report** `<graph-name>-translation-report.md`:
    - Table of every vertex label mapped
    - Table of every property key mapped (including type conversion)
    - Table of every index mapped
    - Section: "Requires Manual Review" — geospatial types, UDTs, lambda-dependent traversals, RLAC

### Relevant Context
- JanusGraph JSON schema structure: `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/json/definition/`
  - `JsonSchemaDefinition.java`
  - `JsonVertexLabelDefinition.java`
  - `JsonEdgeLabelDefinition.java`
  - `JsonPropertyKeyDefinition.java`
  - `JsonCompositeIndexDefinition.java`
  - `JsonMixedIndexDefinition.java`
  - `JsonVertexCentricEdgeIndexDefinition.java`
- Example JSON schema: `janusgraph/janusgraph-es/src/test/resources/test_schema_example.json`
- `JsonSchemaInitStrategy.initializeSchemaFromFile(graph, path)` is the JanusGraph entry point for schema initialisation from JSON.

---

## Sub-Task 4 — JanusGraph / HCD Target Initialiser

**Status:** `[ ] pending`

### Intent
Apply the translated JanusGraph JSON schema to the target JanusGraph + HCD cluster, then verify that all schema elements were created successfully.

### Expected Outcomes
- `dse_jg_migrator init-target --target-gremlin-url ws://<host>:8182/gremlin --schema <graph-name>-janusgraph-schema.json` runs without error.
- `mgmt.printSchema()` on the target cluster shows all expected labels, property keys, and indexes in `ENABLED` state.
- Idempotent: running the command twice does not produce errors or duplicate elements.

### Todo List

1. **Configure the JanusGraph server to connect to HCD 2.x.** HCD 2.x uses image `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi` (IBM Container Registry — requires `docker login cp.icr.io --username cp`). Key HCD-specific CQL properties derived from [`hcd-dev/cassandra.yaml`](../hcd-dev/cassandra.yaml) and [`hcd-dev/compose.yaml`](../hcd-dev/compose.yaml):

   | HCD Setting | Value | JanusGraph property |
   |-------------|-------|---------------------|
   | Cluster name | `hcd_cluster` | n/a (informational) |
   | CQL port | `9042` | `storage.port=9042` |
   | Local datacenter | `dc1` | `storage.cql.local-datacenter=dc1` |
   | Authenticator | `AdvancedAuthenticator` (password enabled) | `storage.cql.username=cassandra` / `storage.cql.password=cassandra` |
   | Default secondary index | `sai` (Storage-Attached Index) | no conflict — JanusGraph manages its own index tables |
   | Materialized views | **disabled** (`materialized_views_enabled: false`) | no conflict — JanusGraph does not use CQL MVs |
   | `storage_compatibility_mode` | `NONE` (Cassandra 5+ formats) | requires JanusGraph CQL driver ≥ 4.x (satisfied by JanusGraph 1.x) |
   | `hcd_guardrail_defaults` | `true` | sets conservative batch-size limits; ensure JanusGraph batch mutations stay < 5 MB |

   The target JanusGraph server must be started with:
   ```properties
   # janusgraph-cql-hcd.properties
   gremlin.graph=org.janusgraph.core.JanusGraphFactory
   storage.backend=cql
   storage.hostname=<hcd-host>          # service name in compose: "hcd"
   storage.port=9042
   storage.cql.keyspace=<target-keyspace>
   storage.cql.local-datacenter=dc1     # HCD default DC name from cassandra.yaml
   storage.cql.username=cassandra       # HCD AdvancedAuthenticator default role
   storage.cql.password=cassandra       # Change in production
   storage.cql.replication-strategy-class=NetworkTopologyStrategy
   storage.cql.replication-strategy-options=dc1,1   # single-node dev; use dc1,3 in production

   # Index backend — OpenSearch (wire-compatible with Elasticsearch REST API)
   # Use opensearchproject/opensearch:3 locally; same config keys as elasticsearch backend
   index.search.backend=elasticsearch   # JanusGraph Elasticsearch driver is OpenSearch-compatible
   index.search.hostname=<opensearch-host>
   index.search.port=9200
   index.search.index-name=<target-keyspace>

   # Schema
   schema.default=none
   schema.init.strategy=json
   schema.init.json.file=/conf/<graph-name>-janusgraph-schema.json
   schema.init.json.indices-activation-type=REINDEX_AND_ENABLE_NON_ENABLED
   ```
   The `schema.init.strategy=json` approach is the cleanest for declarative schema init (see `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/JsonSchemaInitStrategy.java`).

   > **HCD guardrail note:** `hcd_guardrail_defaults: true` enforces a batch-size fail threshold. JanusGraph's `storage.cql.atomic-batch-mutate=false` (default) avoids atomic batch mutations, which sidesteps this guardrail. Do not set `storage.cql.atomic-batch-mutate=true` against HCD.

2. **Alternative: submit schema via Gremlin.** If the operator cannot restart the server, send schema creation Gremlin scripts via the Gremlin WebSocket API. The initialiser module should support both modes:
   - `--mode server-restart` (preferred — use JSON init strategy)
   - `--mode gremlin-script` (fallback — send Groovy via WebSocket)

   Gremlin-script mode example:
   ```groovy
   // Sent to JanusGraph Gremlin server
   mgmt = graph.openManagement()
   if (!mgmt.getPropertyKey('name')) {
     mgmt.makePropertyKey('name').dataType(String.class).cardinality(Cardinality.SINGLE).make()
   }
   if (!mgmt.getVertexLabel('person')) {
     mgmt.makeVertexLabel('person').make()
   }
   mgmt.buildIndex('person_by_name', Vertex.class).
     addKey(mgmt.getPropertyKey('name')).
     indexOnly(mgmt.getVertexLabel('person')).
     buildCompositeIndex()
   mgmt.commit()
   ```

3. **Wait for index enablement.** JanusGraph indexes go through a lifecycle: `INSTALLED → REGISTERED → ENABLED`. After schema creation, the initialiser must poll until all indexes reach `ENABLED`:
   ```groovy
   // Check index status
   mgmt = graph.openManagement()
   idx = mgmt.getGraphIndex('person_by_name')
   idx.getIndexStatus(mgmt.getPropertyKey('name'))  // Should be ENABLED
   mgmt.rollback()
   ```
   The `ManagementSystem.awaitGraphIndexStatus(graph, indexName).call()` Java API does this — expose via a Gremlin script that loops until status is ENABLED.

4. **Verify schema.** After init, run `mgmt.printSchema()` and compare vertex labels, edge labels, property keys, and index names against expected values from the translated schema JSON. Emit `PASS` or `FAIL` per element.

### Relevant Context
- `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/JsonSchemaInitStrategy.java` — primary schema init entry point.
- `janusgraph/janusgraph-dist/src/assembly/static/conf/gremlin-server/gremlin-server-cql-es.yaml` — Gremlin server config for CQL + Elasticsearch target.
- `janusgraph/janusgraph-dist/docker/examples/docker-compose-cql-es.yml` — full Docker Compose reference stack.

---

## Sub-Task 5 — Data Migrator

**Status:** `[ ] pending`

### Intent
Move all vertex and edge data from the DSE Graph cluster to the JanusGraph + HCD cluster in a resumable, batched, fault-tolerant manner.

### Expected Outcomes
- `dse_jg_migrator migrate --analysis <graph-name>-analysis.json --source-host <h> --target-gremlin-url <url>` migrates all vertices and edges.
- Maintains a local SQLite checkpoint file so the migration can be resumed after interruption.
- Produces per-label progress output and a final `<graph-name>-migration-audit.jsonl` log.

### Todo List

1. **Strategy selection.** Two strategies, auto-selected by engine type:
   - **CQL-direct** (DSG only, recommended): Read from the underlying CQL tables using `cassandra-driver` with driver-managed token-range paging. Each vertex label maps to a CQL table `<keyspace>.<vertex_label>`; each edge label to `<from_label>_<edge_label>_<to_label>` (or the custom `tableName()` recorded in the analysis JSON). This strategy supports 10s of millions of rows because paging is handled by the driver's token-ring iterator without holding a cursor open.
   - **Gremlin-paged** (Classic and DSG fallback): Use `g.V().hasLabel('X').range(offset, offset+batch_size)` with `g.with('allow-filtering')`. This is the only option for Classic graphs. It is ~3–5× slower than CQL-direct and requires smaller batch sizes to avoid Gremlin server timeouts.

2. **Batch size defaults and tuning (for 1 → 10M+ scale):**

   | Scale | CQL read `fetch_size` | Gremlin read batch | JG write batch | SQLite WAL |
   |-------|----------------------|-------------------|----------------|------------|
   | < 100K | 500 | 500 | 50 | off |
   | 100K – 1M | 1000 | 200 | 100 | on |
   | 1M – 10M | 2000 | 100 | 200 | on |
   | > 10M | 5000 | 50 | 500 | on + page_size pragma |

   Expose as CLI flags with 1000/200/100/off defaults: `--cql-fetch-size`, `--gremlin-batch`, `--write-batch`, `--sqlite-wal`.

   For graphs > 5M vertices the SQLite `id_map` table grows large. Use WAL mode and an `8192` page size to keep INSERT throughput acceptable:
   ```python
   conn = sqlite3.connect(db_path)
   conn.execute("PRAGMA journal_mode=WAL")
   conn.execute("PRAGMA page_size=8192")
   conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL
   conn.execute("PRAGMA cache_size=-65536")    # 64 MB page cache
   ```

3. **Vertex migration (CQL-direct for DSG):**
   ```python
   from cassandra.query import SimpleStatement

   def migrate_vertices_cql(
       session, keyspace: str, label: str,
       cql_fetch_size: int, write_batch: int,
       target_client, id_map_conn
   ):
       stmt = SimpleStatement(
           f"SELECT * FROM {keyspace}.{label}",
           fetch_size=cql_fetch_size          # driver auto-pages via paging token
       )
       write_buf = []
       for row in session.execute(stmt):      # driver transparently follows pages
           props = {k: v for k, v in row._asdict().items() if v is not None}
           write_buf.append((label, props))
           if len(write_buf) >= write_batch:
               new_ids = flush_vertex_batch(target_client, write_buf)
               persist_id_map(id_map_conn, write_buf, new_ids, keyspace, label)
               write_buf.clear()
       if write_buf:
           new_ids = flush_vertex_batch(target_client, write_buf)
           persist_id_map(id_map_conn, write_buf, new_ids, keyspace, label)

   def flush_vertex_batch(client, buf: list) -> list:
       """Submit up to write_batch addV() calls in one Gremlin request using bindings."""
       script_parts, bindings = [], {}
       for i, (label, props) in enumerate(buf):
           prop_chain = "".join(
               f".property(k_{i}_{j}, v_{i}_{j})"
               for j, k in enumerate(props)
           )
           for j, (k, v) in enumerate(props.items()):
               bindings[f"k_{i}_{j}"] = k
               bindings[f"v_{i}_{j}"] = v
           script_parts.append(f"g.addV(lbl_{i}){prop_chain}.id()")
           bindings[f"lbl_{i}"] = label
       script = "[" + ",".join(script_parts) + "]"
       return client.submit(script, bindings).all().result()
   ```

4. **Vertex migration (Gremlin-paged for Classic):**
   ```python
   from gremlin_python.statics import T

   def migrate_vertices_gremlin(
       source_client, label: str, gremlin_batch: int,
       write_batch: int, target_client, id_map_conn
   ):
       offset = 0
       while True:
           results = source_client.submit(
               "g.with('allow-filtering').V().hasLabel(lbl)"
               ".range(off, end).elementMap()",
               {"lbl": label, "off": offset, "end": offset + gremlin_batch}
           ).all().result()
           if not results:
               break
           write_buf = []
           for vm in results:
               old_id = str(vm.pop(T.id))
               vl = vm.pop(T.label)
               write_buf.append((old_id, vl, dict(vm)))
           # flush in write_batch chunks
           for chunk_start in range(0, len(write_buf), write_batch):
               chunk = write_buf[chunk_start: chunk_start + write_batch]
               new_ids = flush_vertex_batch_classic(target_client, chunk)
               persist_id_map_classic(id_map_conn, chunk, new_ids)
           offset += gremlin_batch
   ```

5. **Edge migration (ALL vertex labels must be fully committed to id_map first):**
   ```python
   def migrate_edges_cql(
       session, keyspace: str, edge_label: str,
       from_label: str, to_label: str, table_name: str,
       cql_fetch_size: int, write_batch: int,
       target_client, id_map_conn
   ):
       # table_name from analysis JSON (custom tableName() or default convention)
       stmt = SimpleStatement(
           f"SELECT * FROM {keyspace}.{table_name}",
           fetch_size=cql_fetch_size
       )
       write_buf, missing = [], 0
       for row in session.execute(stmt):
           row_dict = row._asdict()
           out_old = build_dse_vertex_id(from_label, row_dict)
           in_old  = build_dse_vertex_id(to_label,  row_dict)
           out_new = lookup_id(id_map_conn, out_old)
           in_new  = lookup_id(id_map_conn, in_old)
           if out_new is None or in_new is None:
               missing += 1
               log.warning("missing vertex for edge %s: out=%s in=%s", edge_label, out_old, in_old)
               continue
           edge_props = {
               k: v for k, v in row_dict.items()
               if v is not None and k not in pk_columns(from_label, to_label)
           }
           write_buf.append((edge_label, out_new, in_new, edge_props))
           if len(write_buf) >= write_batch:
               flush_edge_batch(target_client, write_buf)
               write_buf.clear()
       if write_buf:
           flush_edge_batch(target_client, write_buf)
       if missing:
           log.error("%d edges skipped for label %s (missing vertices)", missing, edge_label)
   ```

6. **Checkpoint/resume.** Use SQLite (`sqlite3` from stdlib) to store progress:
   ```python
   import sqlite3

   def init_checkpoint(db_path: str, use_wal: bool = True) -> sqlite3.Connection:
       conn = sqlite3.connect(db_path)
       if use_wal:
           conn.execute("PRAGMA journal_mode=WAL")
           conn.execute("PRAGMA page_size=8192")
           conn.execute("PRAGMA synchronous=NORMAL")
           conn.execute("PRAGMA cache_size=-65536")
       conn.execute("""CREATE TABLE IF NOT EXISTS progress (
           label TEXT, kind TEXT, last_offset INTEGER,
           completed INTEGER DEFAULT 0,
           PRIMARY KEY (label, kind))""")
       conn.execute("""CREATE TABLE IF NOT EXISTS id_map (
           old_id TEXT PRIMARY KEY,
           new_id TEXT NOT NULL)""")
       conn.execute("CREATE INDEX IF NOT EXISTS idx_id_map ON id_map(old_id)")
       conn.commit()
       return conn
   ```

   The checkpoint is updated after every write batch flush. Restart resumes from the last committed offset in `progress`.

7. **Ordering constraint.** Migrate ALL vertices first (all labels). Optionally parallelise vertex labels with `--parallel-labels N` (default 1); each label gets its own Gremlin connection and thread; SQLite WAL allows concurrent writes. Start edge migration only after ALL vertex labels report `completed=1` in the checkpoint.

8. **Batch writes to JanusGraph — transaction grouping.** Group `write_batch` addV/addE calls in a single Gremlin request using bindings to avoid Groovy string injection. The recommended `write_batch` is 100–500 for tens-of-millions scale (tune down if JG server heap pressure is observed). Each batch is one Gremlin WebSocket round-trip.

9. **Audit log.** Append one JSON line per processed batch (not per element — keeps I/O manageable at scale) to `<graph-name>-migration-audit.jsonl`:
   ```json
   {"ts":"2026-11-07T01:00:00Z","kind":"vertex","label":"person","batch_start":0,"batch_size":100,"migrated":100,"failed":0}
   {"ts":"2026-11-07T01:00:05Z","kind":"edge","label":"knows","batch_start":0,"batch_size":100,"migrated":99,"failed":1}
   ```
   Individual failures (missing vertex IDs, type serialisation errors) are logged at `ERROR` level with their source row identifiers for post-run triage.

### Relevant Context
- DSG CQL table naming: `<keyspace>.<vertex_label>` for vertices; `<from_label>_<edge_label>_<to_label>` (or the custom `tableName()`) for edges. The `table_name` field in the analysis JSON captures the actual name resolved at analyse time. Source: `dse-docs-new/modules/graph/` CQL hybrid section.
- JanusGraph Gremlin endpoint: `ws://<host>:8182/gremlin` (confirmed in `janusgraph-dist/src/assembly/static/conf/gremlin-server/gremlin-server-cql-es.yaml`).
- `cassandra-driver` pagination: `SimpleStatement(query, fetch_size=N)` — the driver transparently fetches next pages using paging state tokens; the `for row in session.execute(stmt)` loop spans all pages with no explicit cursor management.
- For > 10M rows per label consider enabling `storage.cql.parallel-scan-token-ranges` on the JanusGraph CQL backend (integer, default 1) for parallel token-range scans during re-indexing, separate from the migrator itself.

---

## Sub-Task 6 — Traversal Validator

**Status:** `[ ] pending`

### Intent
Run a configurable set of spot-check traversals against both the source DSE Graph and the target JanusGraph, compare results, and report discrepancies.

### Expected Outcomes
- `dse_jg_migrator validate --source-host <h> --target-gremlin-url <url> --graph-name <g>` runs validation and produces `<graph-name>-validation-report.md`.
- Vertex/edge counts per label are compared (100% match expected).
- A sample of 10 vertices per label is retrieved from both systems and property values are compared (with type normalisation).

### Todo List

1. **Count validation.** For every vertex label and edge label, compare counts between source and target:
   ```python
   def validate_counts(source_client, target_client, analysis):
       results = []
       for vl in analysis["vertex_labels"]:
           label = vl["label"]
           src_count = get_count(source_client, label, "vertex")
           tgt_count = get_count(target_client, label, "vertex")
           results.append({
               "type": "vertex", "label": label,
               "source": src_count, "target": tgt_count,
               "match": src_count == tgt_count
           })
       return results
   ```

2. **Sample property validation.** Retrieve 10 random vertices per label from source. Look them up by `person_id` (or equivalent partition key) in JanusGraph. Compare property values with type normalisation (e.g., `UUID` string → UUID object).

3. **Traversal smoke test.** Execute a configured list of traversals from a `validation-queries.groovy` file (user-provided) against both endpoints and compare result counts:
   ```python
   # validation-queries.groovy (example)
   # g.V().hasLabel('person').out('knows').count()
   # g.V().has('person', 'name', 'Julia Child').out('created').values('name')
   ```

4. **Report.** Emit `<graph-name>-validation-report.md` with a table per validation check and `PASS`/`FAIL` status.

### Relevant Context
- JanusGraph and DSE both support standard Gremlin `count()`, `values()`, `elementMap()` — these can be run identically on both endpoints.
- Type differences (e.g., DSE `Uuid` vs JanusGraph `UUID`) must be normalised to string for comparison.

---

## Sub-Task 7 — CLI Wrapper and Report Generator

**Status:** `[ ] pending`

### Intent
Wire all modules into a cohesive `click`-based CLI with clear command names, help text, and progress output. Generate the final human-readable migration report.

### Expected Outcomes
- Single entry point `dse-jg-migrator` with sub-commands: `analyse`, `translate`, `init-target`, `migrate`, `validate`, `report`, and `run-all` (pipeline).
- `run-all` executes all phases in order with a single command.
- Final `<graph-name>-migration-report.md` summarises all phases.

### Todo List

1. **Implement `cli.py` with Click:**
   ```python
   import click
   from rich.console import Console

   console = Console()

   @click.group()
   def main():
       """DSE Graph → JanusGraph / HCD Migration Accelerator"""

   @main.command()
   @click.option('--source-gremlin-url', required=True)
   @click.option('--source-cql-host', required=True)
   @click.option('--graph-name', required=True)
   @click.option('--output-dir', default='.')
   def analyse(source_gremlin_url, source_cql_host, graph_name, output_dir):
       """Analyse DSE Graph cluster and extract schema."""
       ...

   @main.command()
   @click.option('--analysis', required=True, type=click.Path(exists=True))
   @click.option('--udt-strategy', type=click.Choice(['flat', 'json']), default='flat',
                 help='UDT strategy: flat (default) explodes UDT fields to <udt>_<field> properties; '
                      'json serialises the whole UDT as a JSON string.')
   @click.option('--map-strategy', type=click.Choice(['flat', 'json']), default='flat',
                 help='mapOf() strategy: flat (default) produces parallel _key/_value LIST properties; '
                      'json serialises the whole map as a JSON string.')
   def translate(analysis, udt_strategy, map_strategy):
       """Translate DSE Graph schema to JanusGraph JSON schema."""
       ...

   @main.command('run-all')
   @click.pass_context
   def run_all(ctx):
       """Run full migration pipeline (analyse → translate → init → migrate → validate → report)."""
       ctx.invoke(analyse, ...)
       ctx.invoke(translate, ...)
       ...
   ```

2. **Progress display with `rich`:**
   ```python
   from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn

   with Progress(SpinnerColumn(), "[progress.description]{task.description}",
                 BarColumn(), TaskProgressColumn()) as progress:
       task = progress.add_task("Migrating vertices...", total=vertex_count)
       for batch in batches:
           progress.update(task, advance=len(batch))
   ```

3. **Final report generation.** `reporter.py` reads all intermediate output files and generates `<graph-name>-migration-report.md`:
   ```markdown
   # Migration Report: food → janusgraph-hcd

   ## Summary
   | Phase     | Status | Duration |
   |-----------|--------|----------|
   | Analysis  | ✅ OK  | 12s      |
   | Translation | ✅ OK | 1s      |
   | Init Target | ✅ OK | 45s    |
   | Migration | ✅ OK  | 8m 22s  |
   | Validation | ✅ OK | 35s    |

   ## Vertex Counts
   | Label  | Source | Target | Match |
   |--------|--------|--------|-------|
   | person | 12,500 | 12,500 | ✅    |
   | recipe | 8,200  | 8,200  | ✅    |

   ## Translation Gaps
   - ℹ️ `location.geo_point` (Point type): Auto geo mixed index created with `Mapping.PREFIX_TREE`. Verify with `g.V().has('location','geo_point',Geo.within(...))`.
   - ⚠️ `route.path` (Linestring type): Stored as WKT string. Geo predicates require manual mixed index addition — see Appendix E.1.
   - ℹ️ UDT `address` (flat strategy): Exploded to `address_street`, `address_zip` property keys. Switch to `--udt-strategy json` to store as a single JSON string instead.
   - ℹ️ `person.badge` (mapOf): Flat strategy applied; migrated as `badge_key` LIST + `badge_value` LIST.
   ```

### Relevant Context
- `click` and `rich` are both well-established Python CLI libraries with no heavy transitive dependencies.

---

## Sub-Task 8 — Docker / Compose Integration and E2E Test

**Status:** `[ ] pending`

### Intent
Provide a Docker Compose file that spins up the **exact production-equivalent local stack** — HCD 2.x (IBM Container Registry), OpenSearch 3, JanusGraph, and a DSE Graph source — so the migration tool can be validated end-to-end without any cloud dependency.

### Expected Outcomes
- `docker compose -f docker/docker-compose-e2e.yml up -d` starts all five services and all healthchecks pass.
- `dse-jg-migrator assess --graph-name food ...` completes in < 60 seconds.
- `dse-jg-migrator run-all --graph-name food ...` completes successfully.
- `dse-jg-migrator validate ...` reports 100% vertex/edge count match.
- A `tests/e2e/test_e2e.py` script verifies the above programmatically.

### Prerequisites

Before running `docker compose up`, the operator must authenticate with IBM Container Registry to pull the HCD image:

```bash
# Replace <your-entitlement-key> with your IBM Entitled Registry key
# from https://myibm.ibm.com/products-services/containerlibrary
docker login cp.icr.io --username cp
# When prompted for password, paste your entitlement key
```

Verify authentication:
```bash
docker login cp.icr.io --username cp
# Expected: "Login Succeeded"
```

**System requirements** (from `hcd-dev/README.md`):
- Docker memory allocation: **≥ 8 GB** total (HCD 3 GB + DSE 2 GB + JanusGraph 1.5 GB + OpenSearch 1.5 GB)
- Disk space: **≥ 15 GB** free
- Platform: HCD image is `linux/amd64` — on Apple Silicon, Docker uses Rosetta/emulation (slower, but functional)
- Ports required: `9042` (HCD CQL), `8181` (HCD Data API), `8182` (JanusGraph Gremlin), `9200` (OpenSearch), `9043` (DSE CQL), `8183` (DSE Gremlin)

### Todo List

1. **Create `docker/docker-compose-e2e.yml`** — the canonical E2E compose file built from `hcd-dev/compose.yaml` and `janusgraph/janusgraph-dist/docker/examples/docker-compose-cql-es.yml`:

   ```yaml
   # docker/docker-compose-e2e.yml
   # Full local simulation: DSE Graph (source) → JanusGraph + HCD 2.x + OpenSearch (target)
   #
   # Prerequisites:
   #   docker login cp.icr.io --username cp   (IBM entitlement key required for HCD image)
   #
   # Usage:
   #   docker compose -f docker/docker-compose-e2e.yml up -d
   #   docker compose -f docker/docker-compose-e2e.yml ps       # verify all healthy
   #   docker compose -f docker/docker-compose-e2e.yml down -v  # full teardown

   networks:
     migrator-net:

   volumes:
     hcd-data:
     opensearch-data:
     dse-data:

   services:

     # ── TARGET: HCD 2.x (IBM Hyper-Converged Database) ─────────────────────
     hcd:
       image: cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi
       platform: linux/amd64           # HCD ships amd64 only; Rosetta emulation on Apple Silicon
       container_name: migrator-hcd
       networks: [migrator-net]
       mem_limit: 3G
       environment:
         - MAX_HEAP_SIZE=1536M
         - CLUSTER_NAME=hcd_cluster
         - DS_LICENSE=accept
         - HCD_AUTO_CONF_OFF=cassandra.yaml
         - JVM_EXTRA_OPTS=-Dcassandra.sai.latest.version=ec
       volumes:
         - ${PWD}/hcd-dev/cassandra.yaml:/opt/hcd/resources/cassandra/conf/cassandra.yaml:ro
         - hcd-data:/var/lib/cassandra
       ports:
         - "9042:9042"                 # CQL — JanusGraph connects here
       healthcheck:
         test: ["CMD-SHELL", "cqlsh -u cassandra -p cassandra -e 'describe keyspaces'"]
         interval: 15s
         timeout: 10s
         retries: 20
         start_period: 60s             # HCD initialises slowly on first start

     # ── TARGET: OpenSearch 3 (index backend for JanusGraph) ─────────────────
     opensearch:
       image: opensearchproject/opensearch:3
       container_name: migrator-opensearch
       networks: [migrator-net]
       mem_limit: 1536M
       environment:
         - discovery.type=single-node
         - OPENSEARCH_INITIAL_ADMIN_PASSWORD=Admin_1234!   # OpenSearch 2.12+ requires strong password
         - plugins.security.disabled=true                  # Disable security for local dev
         - OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m
       volumes:
         - opensearch-data:/usr/share/opensearch/data
       ports:
         - "9200:9200"                 # REST API — JanusGraph connects here
         - "9600:9600"                 # Performance analyser
       healthcheck:
         test: ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health | grep -qv '\"status\":\"red\"'"]
         interval: 10s
         timeout: 10s
         retries: 12
         start_period: 30s

     # ── TARGET: JanusGraph (graph engine over HCD + OpenSearch) ─────────────
     janusgraph:
       image: janusgraph/janusgraph:latest
       container_name: migrator-janusgraph
       networks: [migrator-net]
       ports:
         - "8182:8182"                 # Gremlin WebSocket
       environment:
         JANUS_PROPS_TEMPLATE: cql-es
         # Storage — HCD 2.x
         janusgraph.storage.backend: cql
         janusgraph.storage.hostname: hcd
         janusgraph.storage.port: "9042"
         janusgraph.storage.cql.local-datacenter: dc1
         janusgraph.storage.cql.username: cassandra
         janusgraph.storage.cql.password: cassandra
         janusgraph.storage.cql.keyspace: jg_migration_test
         janusgraph.storage.cql.replication-strategy-class: NetworkTopologyStrategy
         janusgraph.storage.cql.replication-strategy-options: dc1,1
         # Do NOT use atomic-batch-mutate — HCD guardrail defaults limit batch size
         janusgraph.storage.cql.atomic-batch-mutate: "false"
         # Index — OpenSearch (elasticsearch driver is wire-compatible)
         janusgraph.index.search.backend: elasticsearch
         janusgraph.index.search.hostname: opensearch
         janusgraph.index.search.port: "9200"
         janusgraph.index.search.index-name: jg_migration_test
         # Schema
         janusgraph.schema.default: none
       depends_on:
         hcd:
           condition: service_healthy
         opensearch:
           condition: service_healthy
       healthcheck:
         test: ["CMD", "bin/gremlin.sh", "-e", "scripts/remote-connect.groovy"]
         interval: 10s
         timeout: 30s
         retries: 6
         start_period: 30s

     # ── SOURCE: DSE Graph 6.8 (Classic + DSG source for migration testing) ──
     dse-graph-source:
       image: datastax/dse-server:6.8.65-ubi7
       container_name: migrator-dse-source
       networks: [migrator-net]
       mem_limit: 2G
       environment:
         DS_LICENSE: accept
         DSE_GRAPH: "1"               # Enables DSE Graph engine
       volumes:
         - dse-data:/var/lib/cassandra
       ports:
         - "9043:9042"               # DSE CQL (offset to avoid clash with HCD on 9042)
         - "8183:8182"               # DSE Gremlin WebSocket
       healthcheck:
         test: ["CMD-SHELL", "nodetool status | grep -q UN"]
         interval: 15s
         timeout: 10s
         retries: 20
         start_period: 90s           # DSE takes longer than HCD to initialise

     # ── OPTIONAL: JanusGraph Visualizer (graph exploration UI) ──────────────
     janusgraph-visualizer:
       image: janusgraph/janusgraph-visualizer:latest
       container_name: migrator-visualizer
       networks: [migrator-net]
       ports:
         - "3000:3000"               # UI
         - "3001:3001"               # Gremlin proxy
       environment:
         - GREMLIN_HOST=migrator-janusgraph
       depends_on:
         janusgraph:
           condition: service_healthy
   ```

2. **Add a `docker/janusgraph-hcd.properties`** config file that the JanusGraph container mounts — this is the production-equivalent properties file for the E2E test and can be copied as the starting point for real deployments:

   ```properties
   # docker/janusgraph-hcd.properties
   # JanusGraph configuration for HCD 2.x backend + OpenSearch index
   # Copy this file and adjust hostnames / keyspace for production use.

   gremlin.graph=org.janusgraph.core.JanusGraphFactory

   # ── Storage: HCD 2.x ──────────────────────────────────────────────────────
   storage.backend=cql
   storage.hostname=hcd
   storage.port=9042
   storage.cql.local-datacenter=dc1
   storage.cql.username=cassandra
   storage.cql.password=cassandra
   storage.cql.keyspace=jg_migration_test
   storage.cql.replication-strategy-class=NetworkTopologyStrategy
   storage.cql.replication-strategy-options=dc1,1
   # HCD guardrail: do not use atomic batch mutations
   storage.cql.atomic-batch-mutate=false

   # ── Index: OpenSearch / Elasticsearch (wire-compatible) ───────────────────
   index.search.backend=elasticsearch
   index.search.hostname=opensearch
   index.search.port=9200
   index.search.index-name=jg_migration_test
   # For OpenSearch with security disabled locally; add auth headers for production
   # index.search.elasticsearch.http.auth.type=basic
   # index.search.elasticsearch.http.auth.basic.username=admin
   # index.search.elasticsearch.http.auth.basic.password=Admin_1234!

   # ── Schema ────────────────────────────────────────────────────────────────
   schema.default=none
   ```

3. **Create `docker/seed_dse.groovy`** that seeds the `food` test graph with all schema patterns the assessor and migrator must handle:

   ```groovy
   // docker/seed_dse.groovy
   // Run with: docker exec -i migrator-dse-source dse gremlin-console -e /dev/stdin < docker/seed_dse.groovy

   :remote connect tinkerpop.server conf/remote.yaml session
   :remote console

   // Create the food graph (DSG / Core engine)
   system.graph('food').ifNotExists()
     .withReplication("{'class':'SimpleStrategy','replication_factor':1}")
     .andDurableWrites(true)
     .create()

   :remote config alias g food.g
   schema = food.schema()

   // Vertex labels — covering partition keys, clustering, collections, UDTs
   schema.vertexLabel('person').ifNotExists()
     .partitionBy('person_id', Uuid)
     .property('name', Text)
     .property('nickname', setOf(Text))
     .property('badge', mapOf(Text, Date))
     .create()

   schema.vertexLabel('recipe').ifNotExists()
     .partitionBy('recipe_id', Uuid)
     .property('name', Text)
     .property('instructions', Text)
     .property('cuisine', Text)
     .property('tags', setOf(Text))
     .create()

   // Edge labels
   schema.edgeLabel('created').ifNotExists()
     .from('person').to('recipe')
     .property('created_on', Date)
     .create()

   schema.edgeLabel('knows').ifNotExists()
     .from('person').to('person')
     .property('since', Date)
     .create()

   // Indexes — MV, search, vertex-centric
   schema.vertexLabel('person').materializedView('person_by_name')
     .ifNotExists().partitionBy('name').clusterBy('person_id', Asc).create()

   schema.vertexLabel('recipe').searchIndex().ifNotExists()
     .by('instructions').asText()
     .by('cuisine').asString()
     .create()

   schema.edgeLabel('knows').from('person').to('person')
     .materializedView('knows_by_since').ifNotExists()
     .partitionBy(OUT, 'person_id').clusterBy('since', Asc).clusterBy(IN, 'person_id', Asc)
     .create()

   // Seed data
   p1 = g.addV('person').property('person_id', UUID.randomUUID()).property('name', 'Alice').next()
   p2 = g.addV('person').property('person_id', UUID.randomUUID()).property('name', 'Bob').next()
   r1 = g.addV('recipe').property('recipe_id', UUID.randomUUID())
           .property('name', 'Pasta').property('instructions', 'Boil water, add pasta')
           .property('cuisine', 'Italian').next()
   g.addE('created').from(p1).to(r1).property('created_on', new Date()).iterate()
   g.addE('knows').from(p1).to(p2).property('since', new Date()).iterate()
   ```

4. **Write `tests/e2e/test_e2e.py`** that:
   - Waits for all five healthchecks to pass (poll with `docker compose ps`).
   - Seeds DSE Graph by executing `seed_dse.groovy`.
   - Runs `dse-jg-migrator assess` and asserts exit 0 and `overall_risk != "CRITICAL"`.
   - Runs `dse-jg-migrator run-all --skip-assessment-gate`.
   - Asserts `dse-jg-migrator validate` reports 100% count match.

5. **Document Apple Silicon caveat** in a `docker/README.md`:
   > HCD 2.x (`linux/amd64`) runs under Rosetta emulation on Apple M-series chips. Start Docker with at least 8 GB RAM and `rosetta` emulation enabled in Docker Desktop → Settings → Features in Development. Expect ~2× longer startup time vs native `amd64`.

### Relevant Context
- HCD image: `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi` (from `hcd-dev/compose.yaml:3`)
- HCD Data API image: `cp.icr.io/cp/cpd/ibm-datastax-hcd/data-api:v1.0.53` (optional for this stack — not needed by JanusGraph)
- HCD cassandra.yaml key settings: `cluster_name=hcd_cluster`, `authenticator=AdvancedAuthenticator` (password auth enabled), `dc1`, `materialized_views_enabled=false`, `default_secondary_index=sai`, `storage_compatibility_mode=NONE`, `hcd_guardrail_defaults=true`
- OpenSearch image: `opensearchproject/opensearch:3` — requires `plugins.security.disabled=true` for local dev (OpenSearch 2.12+ enforces security by default)
- JanusGraph Elasticsearch backend is fully OpenSearch-compatible (same REST API); no driver change needed
- JanusGraph reference compose: `janusgraph/janusgraph-dist/docker/examples/docker-compose-cql-es.yml`
- DSE 6.8 image: `datastax/dse-server:6.8.65-ubi7` — DSE Graph enabled via `DSE_GRAPH=1` environment variable
- DSE 6.9 image: `datastax/dse-server:6.9.26-ubi` (if testing DSG against a 6.9 source)

---

## Appendix A — Data Type Mapping Table

| DSE Graph Type | JanusGraph `className` | Notes |
|----------------|----------------------|-------|
| `Text`, `Varchar` | `java.lang.String` | |
| `Ascii` | `java.lang.String` | Non-ASCII characters dropped if present |
| `Int` | `java.lang.Integer` | |
| `Bigint` | `java.lang.Long` | |
| `Smallint` | `java.lang.Short` | |
| `Tinyint` | `java.lang.Byte` | |
| `Boolean` | `java.lang.Boolean` | |
| `Float` | `java.lang.Float` | |
| `Double` | `java.lang.Double` | |
| `Decimal` | `java.math.BigDecimal` | |
| `Varint` | `java.math.BigInteger` | |
| `Uuid`, `Timeuuid` | `java.util.UUID` | |
| `Timestamp`, `Instant` | `java.util.Date` | Millisecond precision |
| `Date` | `java.time.LocalDate` | JanusGraph 1.0+ required |
| `Time` | `java.time.LocalTime` | JanusGraph 1.0+ required |
| `Duration` | `java.time.Duration` | |
| `Blob` | `byte[]` (use `Byte.class` in JG) | |
| `Inet` | `java.net.InetAddress` | |
| `Point` | `java.lang.String` (WKT) + auto `Mapping.PREFIX_TREE` mixed index | Stored as WKT; translator auto-generates geo mixed index; `Geo.within()` works out of the box |
| `Polygon` | `java.lang.String` (WKT) | Stored as WKT; no auto geo index; add `Mapping.PREFIX_TREE` mixed index manually for geo predicates |
| `Linestring` | `java.lang.String` (WKT) | Stored as WKT; no auto geo index; add `Mapping.PREFIX_TREE` mixed index manually for geo predicates |
| `setOf(X)` | element `className` with `Cardinality.SET` | Element type resolved individually |
| `listOf(X)` | element `className` with `Cardinality.LIST` | Element type resolved individually |
| `mapOf(K,V)` | `flat` default: `<prop>_key` LIST + `<prop>_value` LIST; `json`: single `java.lang.String` | `--map-strategy flat\|json` (default `flat`) |
| `frozen(typeOf('X'))` (UDT) | `flat` default: `<udt>_<field>` per field; `json`: single `java.lang.String` | `--udt-strategy flat\|json` (default `flat`); see Sub-Task 3, step 8 |

---

## Appendix B — Index Type Mapping Table

| DSE Index Type | JanusGraph Index Type | Notes |
|----------------|----------------------|-------|
| `materializedView()` | Composite Index (`buildCompositeIndex()`) | Partition key of MV → composite index key |
| `secondaryIndex().indexValues()` | Composite Index (on collection element) | Limited predicate support (equality only) |
| `searchIndex().asText()` | Mixed Index, `Mapping.TEXT` | Full-text analysis via Elasticsearch |
| `searchIndex().asString()` | Mixed Index, `Mapping.STRING` | Exact string match |
| `searchIndex()` (numeric) | Mixed Index (default) | Range queries supported |
| `searchIndex()` (geospatial `Point`) | Mixed Index, `Mapping.PREFIX_TREE` or Geo | Requires Elasticsearch geo_point field type |
| `RelationIndex` (vertex-centric) | `buildEdgeIndex()` | Direction and sort order preserved |
| `inverse()` MV on edge | Not directly mapped | Use `buildEdgeIndex(label, name, Direction.IN, ...)` |

**Index predicate portability:**

| DSE Predicate | JanusGraph Predicate | Notes |
|---------------|---------------------|-------|
| `Cmp.eq` | `P.eq()` | Universal |
| `Cmp.gt/gte/lt/lte` | `P.gt/gte/lt/lte()` | Composite for orderable types; mixed for search |
| `TextPredicate.token` | `Text.tokenize()` | Requires mixed index with TEXT mapping |
| `TextPredicate.regex` | `Text.regex()` | Requires mixed index |
| `TextPredicate.prefix` | `Text.prefix()` | Requires mixed index |
| `TextPredicate.fuzzy` | `Text.fuzzy()` | Requires mixed index (ES only) |
| `SpatialPredicate.inside` | `Geo.within()` | Requires `Mapping.PREFIX_TREE` |
| `Contain.within` | `P.within()` | Works with SET/LIST cardinality |

---

## Appendix C — DSE Graph Schema Syntax Reference

This section captures the key schema DSL patterns from `dse-docs-new/modules/graph/` and `bdp/dse-graph/` that the parser in Sub-Task 2 must handle.

### Vertex Label Patterns
```groovy
// Pattern 1: Single partition key
schema.vertexLabel('LABEL').ifNotExists().
  partitionBy('PK_COL', TYPE).
  property('COL', TYPE).
  create()

// Pattern 2: Composite partition key
schema.vertexLabel('LABEL').ifNotExists().
  partitionBy('PK1', TYPE).partitionBy('PK2', TYPE).
  property('COL', TYPE).
  create()

// Pattern 3: Partition + Clustering
schema.vertexLabel('LABEL').ifNotExists().
  partitionBy('PK', TYPE).
  clusterBy('CK', TYPE).  // Optional: Asc|Desc
  property('COL', TYPE).
  create()

// Pattern 4: From existing CQL table
schema.vertexLabel('LABEL').fromExistingTable('TABLE_NAME').ifNotExists().create()

// Pattern 5: With static column
schema.vertexLabel('LABEL').ifNotExists().
  partitionBy('PK', TYPE).clusterBy('CK', TYPE).
  property('COL', TYPE, Static).
  create()
```

### Edge Label Patterns
```groovy
// Pattern 1: Simple (no properties)
schema.edgeLabel('LABEL').ifNotExists().from('VL1').to('VL2').create()

// Pattern 2: With properties
schema.edgeLabel('LABEL').ifNotExists().from('VL1').to('VL2').
  property('COL', TYPE).create()

// Pattern 3: Custom table name
schema.edgeLabel('LABEL').tableName('TABLE').ifNotExists().from('VL1').to('VL2').create()

// Pattern 4: Custom partition/clustering
schema.edgeLabel('LABEL').fromExistingTable('TABLE').ifNotExists().
  partitionBy(OUT, 'OUT_PK').partitionBy('EDGE_COL').
  clusterBy(IN, 'IN_PK', Asc).create()
```

### Index Patterns
```groovy
// Materialized view (vertex)
schema.vertexLabel('VL').materializedView('MV_NAME').ifNotExists().
  partitionBy('INDEXED_COL').clusterBy('PK_COL', Asc).create()

// Materialized view (edge)
schema.edgeLabel('EL').from('VL1').to('VL2').
  materializedView('MV_NAME').ifNotExists().
  partitionBy(OUT, 'OUT_PK').clusterBy(IN, 'IN_PK', Asc).create()

// Inverse edge MV
schema.edgeLabel('EL').from('VL1').to('VL2').
  materializedView('MV_NAME').ifNotExists().inverse().create()

// Secondary index (collection)
schema.vertexLabel('VL').secondaryIndex('IDX_NAME').ifNotExists().
  by('COL').indexValues().create()

// Search index
schema.vertexLabel('VL').searchIndex().ifNotExists().
  by('COL1').asText().by('COL2').asString().create()

// Edge search index
schema.edgeLabel('EL').from('VL1').to('VL2').
  searchIndex().ifNotExists().by('COL').asText().create()
```

---

## Appendix D — JanusGraph JSON Schema Format

Full format reference based on `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/json/definition/`:

```json
{
  "propertyKeys": [
    {
      "key": "<property-key-name>",
      "className": "<java-class-fqn>",
      "cardinality": "SINGLE|LIST|SET",
      "ttl": 7200000
    }
  ],
  "vertexLabels": [
    {
      "label": "<vertex-label-name>",
      "staticVertex": false,
      "partition": false,
      "ttl": 7200000
    }
  ],
  "edgeLabels": [
    {
      "label": "<edge-label-name>",
      "multiplicity": "MULTI|SIMPLE|ONE2MANY|MANY2ONE|ONE2ONE",
      "unidirected": false,
      "ttl": 7200000
    }
  ],
  "compositeIndexes": [
    {
      "name": "<index-name>",
      "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
      "indexOnly": "<label-name>",
      "unique": false,
      "consistency": "DEFAULT|LOCK|FORK",
      "keys": [
        {"propertyKey": "<property-key-name>"}
      ],
      "inlinePropertyKeys": ["<property-key-name>"]
    }
  ],
  "mixedIndexes": [
    {
      "name": "<index-name>",
      "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
      "indexOnly": "<label-name>",
      "indexBackend": "search",
      "keys": [
        {
          "propertyKey": "<property-key-name>",
          "parameters": [
            {
              "key": "mapping",
              "value": "org.janusgraph.core.schema.Mapping.TEXT",
              "parser": "enum"
            },
            {
              "key": "string-analyzer",
              "value": "standard",
              "parser": "string"
            }
          ]
        }
      ]
    }
  ],
  "vertexCentricEdgeIndexes": [
    {
      "name": "<index-name>",
      "indexedEdgeLabel": "<edge-label-name>",
      "direction": "BOTH|IN|OUT",
      "propertyKeys": ["<property-key-name>"],
      "order": "asc|desc"
    }
  ],
  "vertexCentricPropertyIndexes": [
    {
      "name": "<index-name>",
      "indexedPropertyKey": "<property-key-name>",
      "propertyKeys": ["<other-property-key>"],
      "order": "asc"
    }
  ]
}
```

---

## Appendix E — Known Gaps and Caveats

### E.1 Geospatial Data
DSE Graph supports `Point`, `Polygon`, `Linestring` natively via its Solr search backend (`SpatialPredicate` enum: `inside`, `insideCartesian`, `geospatialWithinDistance`, `geospatialWithinPolygon` — source: `bdp/dse-graph/src/main/java/com/datastax/bdp/graph/api/property/SpatialPredicate.java`).

Since Elasticsearch/OpenSearch is **always** co-deployed alongside HCD in this model, full geo support is achievable without manual backend reconfiguration.

**`Point` properties — fully automated:**
1. Translator creates property key as `java.lang.String` (WKT).
2. Translator auto-generates a `Mapping.PREFIX_TREE` mixed index entry for the property, targeting the `search` backend.
3. Data migrator passes DSE `Point` values (already WKT `POINT(lon lat)` notation) through unchanged.
4. `Geo.within(Geoshape.circle(lat, lon, radius))` and `Geo.geoIntersect(shape)` predicates work against the auto-created mixed index.

**`Polygon` and `Linestring` properties — stored but not auto-indexed:**
- Stored as WKT strings. Geo predicates on these types require a `geo_shape` Elasticsearch field (supported by JanusGraph `Mapping.PREFIX_TREE`) — the translator flags these in the "Requires Manual Geo Index Verification" section of the translation report. The mixed index JSON parameters needed are:
  ```json
  {"key": "mapping", "value": "org.janusgraph.core.schema.Mapping.PREFIX_TREE", "parser": "enum"}
  ```

**DSE → JanusGraph geo predicate mapping:**
| DSE Predicate | JanusGraph Equivalent |
|---------------|-----------------------|
| `SpatialPredicate.inside(Geo.point(lon,lat), radius, unit)` | `Geo.within(Geoshape.circle(lat, lon, radius))` |
| `SpatialPredicate.insideCartesian(polygon)` | `Geo.within(Geoshape.box(...))` |
| `SpatialPredicate.geospatialWithinPolygon(wkt)` | `Geo.within(Geoshape.fromWkt(wkt))` |
| `SpatialPredicate.geospatialWithinDistance(point, dist)` | `Geo.within(Geoshape.circle(lat, lon, dist))` |

### E.2 Meta-Properties (Classic only)
Classic DSE Graph supports meta-properties (properties of properties). JanusGraph supports meta-properties via `VertexProperty` but these are semantically different from DSG collections. The migrator converts meta-properties to a serialised JSON string on the parent property and logs a warning.

### E.3 DSE `mapOf(K, V)` Collections
JanusGraph has no native map property type. The `--map-strategy` CLI flag selects the approach (default `flat`):

- **`flat` (default):** A map property `badge: {silver: Date, gold: Date}` becomes two parallel `LIST` property keys — `badge_key` (`java.lang.String`, `Cardinality.LIST`) and `badge_value` (`<value_type>`, `Cardinality.LIST`). Values are inserted as parallel positional lists (`badge_key=["silver","gold"]`, `badge_value=[date1, date2]`). The key list can be given a composite index for equality lookup. Map semantics are reconstructed at the application layer by zipping the two lists.
- **`json`:** The entire map is serialised to a single `java.lang.String` property (`json.dumps()`). Simpler schema, but individual map keys are not Gremlin-queryable without `Text.regex()` on the serialised string.

### E.4 Transaction API Differences
DSE Classic used `graph.tx().open()/commit()/rollback()`. DSG removed this. JanusGraph has `graph.tx()` again. For migrated application code, the JanusGraph `tx()` API works like Classic. No change needed for DSG-based applications since they were already using single-traversal patterns.

### E.5 Gremlin Version Delta (3.4.x → 3.7.x)
DSE Graph targets TinkerPop 3.4.x; JanusGraph 1.x targets TinkerPop 3.7.x. The Gremlin 3.5–3.7 releases introduced breaking changes in: `valueMap()` return format (now includes `id` and `label` by default when using `elementMap()`), and `by()` step syntax in some edge cases. Application traversals should be reviewed against the [TinkerPop migration guide](https://tinkerpop.apache.org/docs/current/upgrade/#_upgrading_for_users).

### E.6 DSE Row-Level Access Control
DSE RLAC policies are stored in DSE's internal security tables and cannot be automatically translated. The report generator will list all graph labels and flag that RLAC needs manual recreation in JanusGraph's Gremlin Server security configuration.

### E.7 DSE OLAP (Spark / GraphFrames)
Spark-based OLAP traversals (`a` traversal source, `GraphComputer`, `GraphFrames`) have no direct equivalent automated migration path. JanusGraph supports `SparkGraphComputer` via TinkerPop's built-in Spark integration, but the Spark job configuration is environment-specific. The tool logs a `SKIPPED_OLAP` entry in the audit log for every OLAP-configured traversal source it detects.

### E.8 Index Lifecycle in JanusGraph
After `JsonSchemaInitStrategy` creates indexes, they go through states `INSTALLED → REGISTERED → ENABLED`. Depending on data volume in HCD, enabling indexes may take significant time. The `init-target` command waits up to a configurable timeout (default: 30 minutes) polling index status before proceeding.

### E.9 DSG Vertex/Edge ID Preservation
DSG vertex IDs are URI strings (`dseg:/person/uuid`). JanusGraph assigns internal `long` IDs. The migration tool maintains an `id_map` in SQLite during the run. This map is essential for edge migration. After migration, the map is archived to `<graph-name>-id-map.sqlite` for post-migration ID lookups.

---


---

## Appendix F — Compatibility Assessment Rule Catalogue

Complete reference for all rules implemented in `compatibility_rules.py`. Each rule is uniquely identified by its `rule_id` and maps directly to a detection method in `assessor.py`.

### Rule ID Naming Convention

```
<PREFIX>-<NNN>
│          └── Zero-padded 3-digit sequence within the category
└── QP = Query Pattern  |  SC = Schema  |  IX = Index
    CF = Configuration  |  OL = OLAP    |  SE = Security
    TP = TinkerPop Delta
```

### Complete Rule Catalogue

#### Query Pattern Rules

| Rule ID | Severity | Detection Hint | JanusGraph Equivalent | Docs |
|---------|----------|----------------|-----------------------|------|
| `QP-001` | YELLOW | Regex `g\.with\(['"]allow-filtering['"]\)` in schema markers or CQL `system_schema.views` ALLOW FILTERING clauses | Remove; create required indexes first | [JanusGraph Index Performance](https://docs.janusgraph.org/index-management/index-performance/) |
| `QP-002` | YELLOW | Regex `g\.with\(['"]label-warning['"]\s*,\s*false\)` | Remove — not applicable | N/A |
| `QP-003` | YELLOW | Regex `g\.with\(['"]ignore-unindexed['"]\)` | Remove — JanusGraph throws on unindexed | [JanusGraph Exception Handling](https://docs.janusgraph.org/advanced-topics/common-questions/) |
| `QP-004` | YELLOW | Regex `g\.with\(['"]consistency['"]` | Configure `storage.cql.*-consistency-level` | [JanusGraph CQL Config](https://docs.janusgraph.org/storage-backend/cassandra/) |
| `QP-005` | RED | Gremlin probe: `a.V().limit(0).count()` succeeds | Separate HadoopGraph + Spark deployment | [JanusGraph Hadoop](https://docs.janusgraph.org/advanced-topics/hadoop/) |
| `QP-006` | RED | `searchIndex` with `asText()` in schema → implies `Search.token*` in app | `Text.textContains()` / `Text.textContainsPrefix()` | [JanusGraph Text Search](https://docs.janusgraph.org/index-backend/text-search/) |
| `QP-007` | RED | `searchIndex` with `asText()` + fuzzy threshold | `Text.textContainsFuzzy(term, distance)` | [JanusGraph Text Search](https://docs.janusgraph.org/index-backend/text-search/) |
| `QP-008` | RED | `searchIndex` + regex patterns inferred | `Text.textContainsRegex(pattern)` | [JanusGraph Text Search](https://docs.janusgraph.org/index-backend/text-search/) |
| `QP-009` | RED | `searchIndex` with phrase proximity (DSE Solr `phrase` query) | `Text.textContainsPhrase(phrase)` | [JanusGraph Text Search](https://docs.janusgraph.org/index-backend/text-search/) |
| `QP-010` | YELLOW | `searchIndex()` on a `Point`-typed property | `Geo.within(Geoshape.circle(lat, lon, radius))` with `Mapping.PREFIX_TREE` | [JanusGraph Geo Search](https://docs.janusgraph.org/index-backend/elasticsearch/#geoshape-mappings) |
| `QP-011` | YELLOW | `searchIndex()` on a `Polygon`-typed property | `Geo.within(Geoshape.fromWkt(wkt))` — requires manual mixed index | [JanusGraph Geo Search](https://docs.janusgraph.org/index-backend/elasticsearch/#geoshape-mappings) |
| `QP-012` | YELLOW | `DseGraph.traversal()` or `DseGraph.` class references | Replace with `JanusGraphFactory.open(conf).traversal()` | [JanusGraph Getting Started](https://docs.janusgraph.org/getting-started/basic-usage/) |
| `QP-013` | YELLOW | `g.V(['~label':` partition key ID syntax in schema examples | Replace with `g.V().hasLabel('X').has('key', v)` | [JanusGraph Gremlin](https://docs.janusgraph.org/getting-started/gremlin/) |
| `QP-014` | YELLOW | Groovy closure syntax `{...}` detected in stored procedures / user scripts | JanusGraph permits by default; set `evaluationTimeout` appropriately | [Gremlin Server Security](https://tinkerpop.apache.org/docs/current/reference/#gremlin-server-sandboxing) |
| `QP-015` | YELLOW | `graph.tx().open()` / Classic transaction pattern | Directly compatible — JanusGraph `graph.tx()` works identically | [JanusGraph Transactions](https://docs.janusgraph.org/transactions/) |
| `QP-016` | YELLOW | Schema uses `repeat()` (inferred from traversal examples) | Add `.until()` to all `repeat()` steps | [TinkerPop 3.5 Migration](https://tinkerpop.apache.org/docs/current/upgrade/#_upgrading_for_users) |
| `QP-017` | YELLOW | Schema uses `valueMap()` (inferred from traversal examples) | Add `.by(unfold())` for flat values | [TinkerPop 3.5 Migration](https://tinkerpop.apache.org/docs/current/upgrade/#_upgrading_for_users) |

#### Schema Rules

| Rule ID | Severity | Detection Hint | JanusGraph Equivalent | Docs |
|---------|----------|----------------|-----------------------|------|
| `SC-001` | YELLOW | `\.partitionBy\(` or `\.clusterBy\(` in schema DSL | Become regular property keys; JanusGraph manages CQL layout | [JanusGraph Schema](https://docs.janusgraph.org/schema/) |
| `SC-002` | YELLOW | `\.fromExistingTable\(` in schema DSL | No equivalent; data must be re-ingested via migration | N/A |
| `SC-003` | RED | `mapOf\(` in schema DSL | `--map-strategy flat` or `json` | [JanusGraph Properties](https://docs.janusgraph.org/schema/#property-key-cardinality) |
| `SC-004` | YELLOW | `setOf\(` or `listOf\(` in schema DSL | `Cardinality.SET` or `Cardinality.LIST` — automated | [JanusGraph Cardinality](https://docs.janusgraph.org/schema/#property-key-cardinality) |
| `SC-005` | RED | `frozen\(typeOf\(` or UDT field references | `--udt-strategy flat` or `json` | N/A |
| `SC-006` | YELLOW | `tupleOf\(` in schema DSL | Serialise to JSON string | N/A |
| `SC-007` | RED | Meta-property usage in Classic schema | Manual redesign required | [JanusGraph Meta-Properties](https://docs.janusgraph.org/schema/#vertex-labels) |
| `SC-008` | YELLOW | Property of type `Point`, `Polygon`, or `Linestring` in schema | `Point` → WKT + auto `Mapping.PREFIX_TREE` index; others → WKT only | [JanusGraph Geo](https://docs.janusgraph.org/index-backend/elasticsearch/#geoshape-mappings) |

#### Index Rules

| Rule ID | Severity | Detection Hint | JanusGraph Equivalent | Docs |
|---------|----------|----------------|-----------------------|------|
| `IX-001` | YELLOW | `\.materializedView\(` in schema DSL | `buildCompositeIndex()` | [JanusGraph Composite Indexes](https://docs.janusgraph.org/index-management/index-performance/#composite-index) |
| `IX-002` | YELLOW | `\.materializedView\(.*\)\..*\.inverse\(\)` | `buildEdgeIndex(..., Direction.IN, ...)` | [JanusGraph Vertex-Centric](https://docs.janusgraph.org/index-management/index-performance/#vertex-centric-indexes) |
| `IX-003` | YELLOW | `\.secondaryIndex\(` in schema DSL | `buildCompositeIndex()` | [JanusGraph Composite Indexes](https://docs.janusgraph.org/index-management/index-performance/#composite-index) |
| `IX-004` | YELLOW | `\.searchIndex\(\).*\.asText\(\)` | Mixed index, `Mapping.TEXT`, Elasticsearch backend | [JanusGraph Mixed Indexes](https://docs.janusgraph.org/index-management/index-performance/#mixed-index) |
| `IX-005` | YELLOW | `\.searchIndex\(\).*\.asString\(\)` | Mixed index, `Mapping.STRING` | [JanusGraph Mixed Indexes](https://docs.janusgraph.org/index-management/index-performance/#mixed-index) |
| `IX-006` | YELLOW | `\.searchIndex\(\)` on `Point`/`Polygon`/`Linestring` property | Mixed index, `Mapping.PREFIX_TREE`; auto for `Point` | [JanusGraph Geo Search](https://docs.janusgraph.org/index-backend/elasticsearch/#geoshape-mappings) |
| `IX-007` | RED | `schema\.indexFor\(.*\)\.analyze\(\)` | No equivalent; remove and use `mgmt.printSchema()` instead | N/A |
| `IX-008` | YELLOW | Vertex-centric index patterns in schema describe output | `buildEdgeIndex()` | [JanusGraph Vertex-Centric](https://docs.janusgraph.org/index-management/index-performance/#vertex-centric-indexes) |

#### Configuration Rules

| Rule ID | Severity | Detection Hint | JanusGraph Equivalent | Docs |
|---------|----------|----------------|-----------------------|------|
| `CF-001` | YELLOW | `schema_mode.*Development` in `system.graph().describe()` | `schema.default=default` in janusgraph properties | [JanusGraph Schema Constraints](https://docs.janusgraph.org/schema/schema-constraints/) |
| `CF-002` | YELLOW | `schema_mode.*Production` | `schema.default=none` | [JanusGraph Schema Constraints](https://docs.janusgraph.org/schema/schema-constraints/) |
| `CF-003` | YELLOW | `allow_scan.*true` | Ensure indexes cover all traversals; `query.force-index=false` disables enforcement | [JanusGraph Index Performance](https://docs.janusgraph.org/index-management/index-performance/) |
| `CF-004` | YELLOW | `restrict_lambda.*true` | `evaluationTimeout` (ms) in gremlin-server.yaml | [Gremlin Server Config](https://tinkerpop.apache.org/docs/current/reference/#gremlin-server) |
| `CF-005` | YELLOW | `system_replication_config` in graph describe | `storage.cql.replication-strategy-class` + `storage.cql.replication-strategy-options` | [JanusGraph CQL](https://docs.janusgraph.org/storage-backend/cassandra/) |

#### OLAP Rules

| Rule ID | Severity | Detection Hint | Notes |
|---------|----------|----------------|-------|
| `OL-001` | RED | Gremlin probe `a.V().limit(0).count()` returns result (no exception) | Manual HadoopGraph + Spark setup required |
| `OL-002` | RED | `GraphOLAPEnabled.*true` in `system.graph().describe()` | Same as OL-001 |
| `OL-003` | RED | `SparkGraphComputer` or `GraphFrames` class reference in stored scripts | No automated migration path |

#### Security Rules

| Rule ID | Severity | Detection Hint | Notes |
|---------|----------|----------------|-------|
| `SE-001` | RED | RLAC entries in `system.graph().describe()` or `system_auth.resource_role_permisions` DSE tables | Application-layer access control must be implemented manually |
| `SE-002` | RED | Row-level security policy references in DSE configuration | Same as SE-001 |

#### TinkerPop Delta Rules

| Rule ID | Severity | Detection Hint | JanusGraph Action | Docs |
|---------|----------|----------------|-------------------|------|
| `TP-001` | YELLOW | Static advisory — applies to all graphs | Add `.until()` guards to all `repeat()` steps | [TinkerPop 3.5 Upgrade](https://tinkerpop.apache.org/docs/current/upgrade/#_upgrading_for_users) |
| `TP-002` | YELLOW | Static advisory — applies to all graphs | Add `.by(unfold())` to `valueMap()` calls | [TinkerPop 3.5 Upgrade](https://tinkerpop.apache.org/docs/current/upgrade/#_upgrading_for_users) |
| `TP-003` | YELLOW | Static advisory — gremlin-server.yaml | Rename `scriptEvaluationTimeout` → `evaluationTimeout` | [Gremlin Server 3.5 Config](https://tinkerpop.apache.org/docs/current/reference/#gremlin-server) |
| `TP-004` | GREEN | Static advisory — JanusGraph 1.x supports GraphBinary | Update client driver to use GraphBinary serializer | [TinkerPop GraphBinary](https://tinkerpop.apache.org/docs/current/dev/io/#graphbinary) |
| `TP-005` | YELLOW | Static advisory — applies to all graphs with `schema.default=none` | Ensure every `property(k, v)` call has a pre-defined property key in JanusGraph schema | [JanusGraph Schema](https://docs.janusgraph.org/schema/) |

#### HCD Target Compatibility Rules

These rules are evaluated against the **target** HCD 2.x cluster and are only run when `--target-cql-host` is supplied to the `assess` command. Source: `hcd-dev/compose.yaml`, `hcd-dev/cassandra.yaml`, `hcd-dev/README.md`.

| Rule ID | Severity | Detection Hint | JanusGraph/HCD Action | Docs |
|---------|----------|----------------|-----------------------|------|
| `HC-001` | YELLOW | CQL connection with credentials succeeds → `AdvancedAuthenticator` active | Set `storage.cql.username=cassandra` and `storage.cql.password=cassandra` in JG properties (never leave blank) | [JanusGraph CQL Auth](https://docs.janusgraph.org/storage-backend/cassandra/#authentication) |
| `HC-002` | YELLOW | `SELECT value FROM system_views.settings WHERE name='materialized_views_enabled'` returns `false` | JanusGraph does not create CQL MVs — no action needed; confirm no application scripts attempt `CREATE MATERIALIZED VIEW` | [HCD cassandra.yaml](../hcd-dev/cassandra.yaml) |
| `HC-003` | YELLOW | Static advisory: `hcd_guardrail_defaults=true` in HCD cassandra.yaml | Set `storage.cql.atomic-batch-mutate=false` in JanusGraph config; keep write batches < 5 MB | [HCD cassandra.yaml](../hcd-dev/cassandra.yaml) |
| `HC-004` | YELLOW | Static advisory: `storage_compatibility_mode=NONE` (Cassandra 5+ SSTable formats) | JanusGraph 1.x uses DataStax Java Driver 4.x which supports Cassandra 5 natively — no action needed; informational | [HCD cassandra.yaml](../hcd-dev/cassandra.yaml) |
| `HC-005` | YELLOW | Static advisory: `default_secondary_index=sai` | JanusGraph creates its own index tables; SAI default does not interfere — informational | [HCD cassandra.yaml](../hcd-dev/cassandra.yaml) |
| `HC-006` | GREEN | CQL port `9042` + datacenter `dc1` confirmed | Matches JanusGraph defaults; set `storage.port=9042` and `storage.cql.local-datacenter=dc1` | [JanusGraph CQL](https://docs.janusgraph.org/storage-backend/cassandra/) |
| `HC-007` | YELLOW | `platform.machine() == "arm64"` at runtime | HCD `linux/amd64` image runs under Rosetta emulation on Apple Silicon — expect slower startup; increase `start_period` in Docker healthcheck | [hcd-dev/README.md](../hcd-dev/README.md) |
| `HC-008` | RED | CQL connection to target host fails (any exception) | `docker login cp.icr.io --username cp` must succeed; pull HCD image `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi`; verify `docker compose ps` shows `healthy` | [hcd-dev/README.md](../hcd-dev/README.md) |

---

*End of plan document. Updated: November 2026.*
