# DSE Graph → JanusGraph / HCD Migration Accelerator — Implementation Plan

> **Status:** Pending implementation  
> **Goal:** Build a lightweight CLI tool that (1) analyses a live DSE Graph cluster, (2) generates a migration plan, and (3) executes the schema + data migration to JanusGraph with HCD as its CQL backend.  
> **Sources:** `bdp/dse-graph/`, `dse-docs-new/modules/graph/`, `janusgraph/`, project structure exploration (November 2026).

---

## Table of Contents
1. [Background and Motivation](#1-background-and-motivation)
2. [Scope](#2-scope)
3. [Architecture Overview](#3-architecture-overview)
4. [Key Concepts: DSE Graph vs JanusGraph](#4-key-concepts-dse-graph-vs-janusgraph)
5. [Sub-Task 1 — Project Scaffold](#sub-task-1--project-scaffold)
6. [Sub-Task 2 — DSE Graph Analyser](#sub-task-2--dse-graph-analyser)
7. [Sub-Task 3 — Schema Translator](#sub-task-3--schema-translator)
8. [Sub-Task 4 — JanusGraph / HCD Target Initialiser](#sub-task-4--janusgraph--hcd-target-initialiser)
9. [Sub-Task 5 — Data Migrator](#sub-task-5--data-migrator)
10. [Sub-Task 6 — Traversal Validator](#sub-task-6--traversal-validator)
11. [Sub-Task 7 — CLI Wrapper and Report Generator](#sub-task-7--cli-wrapper-and-report-generator)
12. [Sub-Task 8 — Docker / Compose Integration and E2E Test](#sub-task-8--docker--compose-integration-and-e2e-test)
13. [Appendix A — Data Type Mapping Table](#appendix-a--data-type-mapping-table)
14. [Appendix B — Index Type Mapping Table](#appendix-b--index-type-mapping-table)
15. [Appendix C — DSE Graph Schema Syntax Reference](#appendix-c--dse-graph-schema-syntax-reference)
16. [Appendix D — JanusGraph JSON Schema Format](#appendix-d--janusgraph-json-schema-format)
17. [Appendix E — Known Gaps and Caveats](#appendix-e--known-gaps-and-caveats)

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
   │   ├── cli.py               # Click entry point
   │   ├── analyser.py          # DSE Graph introspection
   │   ├── translator.py        # Schema mapping logic
   │   ├── initialiser.py       # JanusGraph target setup
   │   ├── migrator.py          # Data movement
   │   ├── validator.py         # Post-migration checks
   │   ├── reporter.py          # Report generation
   │   ├── models.py            # Dataclasses for schema elements
   │   └── config.py            # Configuration dataclasses
   ├── tests/
   │   ├── test_analyser.py
   │   ├── test_translator.py
   │   └── test_models.py
   └── examples/
       ├── food-graph-schema.json     # Example extracted schema
       └── janusgraph-target.json     # Example translated schema
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

1. **Configure the JanusGraph server to accept JSON schema init.** The target JanusGraph server must be started with:
   ```properties
   # janusgraph-cql-hcd.properties
   gremlin.graph=org.janusgraph.core.JanusGraphFactory
   storage.backend=cql
   storage.hostname=<hcd-host>
   storage.port=9042
   storage.cql.keyspace=<target-keyspace>
   storage.cql.replication-strategy-class=NetworkTopologyStrategy
   storage.cql.replication-strategy-options=dc1,3

   # Index backend (Elasticsearch for search indexes)
   index.search.backend=elasticsearch
   index.search.hostname=<es-host>
   index.search.port=9200
   index.search.index-name=<target-keyspace>

   # Schema
   schema.default=none
   schema.init.strategy=json
   schema.init.json.file=/conf/<graph-name>-janusgraph-schema.json
   schema.init.json.indices-activation-type=REINDEX_AND_ENABLE_NON_ENABLED
   ```
   The `schema.init.strategy=json` approach is the cleanest for declarative schema init (see `janusgraph/janusgraph-core/src/main/java/org/janusgraph/core/schema/JsonSchemaInitStrategy.java`).

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
Provide a Docker Compose file that spins up the full target stack (JanusGraph + HCD/Cassandra + Elasticsearch) plus a small DSE Graph "test source" so the tool can be validated end-to-end.

### Expected Outcomes
- `docker compose -f docker/docker-compose-e2e.yml up -d` starts the full environment.
- `dse-jg-migrator run-all --graph-name food ...` completes successfully.
- `dse-jg-migrator validate ...` reports 100% vertex/edge count match.
- A `tests/e2e/test_e2e.py` script verifies the above programmatically.

### Todo List

1. **Extend the existing Docker Compose reference** from `janusgraph/janusgraph-dist/docker/examples/docker-compose-cql-es.yml`:
   ```yaml
   services:
     janusgraph:
       image: janusgraph/janusgraph:latest
       ports: ["8182:8182"]
       environment:
         JAVA_OPTIONS: >-
           -Dgremlin.graph=org.janusgraph.core.JanusGraphFactory
           -Dstorage.backend=cql
           -Dstorage.hostname=cassandra
           -Dstorage.cql.keyspace=test_graph
           -Dindex.search.backend=elasticsearch
           -Dindex.search.hostname=elasticsearch
       depends_on: [cassandra, elasticsearch]

     cassandra:
       image: cassandra:4.1   # Compatible with HCD
       environment:
         HEAP_NEWSIZE: "128M"
         MAX_HEAP_SIZE: "512M"

     elasticsearch:
       image: elasticsearch:8.19.15
       environment:
         discovery.type: single-node
         xpack.security.enabled: "false"

     # Test DSE Graph source (using DSE 6.8 image)
     dse-graph-source:
       image: datastax/dse-server:6.8.48
       environment:
         DS_LICENSE: accept
         DSE_GRAPH: 1
       ports: ["9042:9042", "8183:8182"]
   ```

2. **Write `tests/e2e/seed_dse.groovy`** that creates the `food` test graph with all the schema patterns (vertex labels with partition keys, edge labels, search indexes, MV indexes).

3. **Write `tests/e2e/test_e2e.py`** that:
   - Seeds DSE Graph via Gremlin.
   - Runs `dse-jg-migrator run-all`.
   - Asserts all validation checks pass.

### Relevant Context
- `janusgraph/janusgraph-dist/docker/examples/docker-compose-cql-es.yml` is the canonical reference compose file.
- DSE 6.8 Docker image is `datastax/dse-server:6.8.48` (or latest 6.8.x from Docker Hub).

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

*End of plan document. Created: November 2026.*
