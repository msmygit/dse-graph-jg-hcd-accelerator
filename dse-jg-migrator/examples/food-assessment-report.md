# Pre-Migration Compatibility Assessment Report
## Graph: food  ·  Engine: dsg  ·  Assessed: 2026-11-09T23:02:21.258488Z

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Overall Risk Level | 🔴 HIGH |
| Total Findings | 23 |
| Blockers (RED) | 2 |
| Warnings (YELLOW) | 19 |
| Compatible (GREEN) | 2 |
| Search backend (OpenSearch / Elasticsearch) | Required — 3 mixed index(es) |

### Key Findings
- 🔴 **2 Blocker(s)** require resolution before migration can proceed.
- 🔎 **OpenSearch / Elasticsearch is required** for 3 mixed index(es): `location_search`, `recipe_search`, `location_geo_geo_point` (see Index Analysis).

---

## Cluster Overview

| Property | Value |
|----------|-------|
| Graph Name | food |
| Engine | DSG (Core) |
| Vertex Labels | 3 |
| Edge Labels | 3 |
| Property Keys | 10 |
| Indexes | 4 (2 materializedView, 2 searchIndex) |
| Schema Mode | unknown |
| OLAP Enabled | no |

---

## Index Analysis

Mixed indexes live in the search backend (OpenSearch / Elasticsearch); composite and vertex-centric indexes live in HCD. `translate` and `init-target` create these indexes automatically; the Queries column lists the DSE predicates each one serves and their JanusGraph replacements.

| DSE Index | DSE Kind | Label | Keys | JanusGraph Index | Backend | Queries |
|-----------|----------|-------|------|------------------|---------|---------|
| location_search | searchIndex | location | location_id, geo_point | location_search (mixed) | 🔎 OpenSearch / Elasticsearch | location_id: `eq`, `neq`; geo_point: `Geo.inside()` → `geoWithin(Geoshape.circle(lat, lon, radius))` |
| person_by_name | materializedView | person | name | person_by_name (composite) | HCD (storage backend) | equality on name (`has(key, value)`); no ranges or text search |
| recipe_search | searchIndex | recipe | recipe_id, cuisine, instructions | recipe_search (mixed) | 🔎 OpenSearch / Elasticsearch | recipe_id: `eq`, `neq`; cuisine: `Search.prefix/regex/fuzzy()` → `textPrefix`, `textRegex`, `textFuzzy`; `eq`, `neq`; instructions: `Search.token*()` → `textContains`, `textContainsPrefix`, `textContainsRegex`, `textContainsFuzzy` |
| knows_by_since | materializedView | knows | since | knows_by_since (composite) | HCD (storage backend) | equality on since (`has(key, value)`); no ranges or text search |
| — | Point property | location | geo_point | location_geo_geo_point (mixed) | 🔎 OpenSearch / Elasticsearch | DSE `Geo.inside()` → `geoWithin(Geoshape.circle(lat, lon, radius))` |

---

## Findings by Category

### Hcd Target

| Rule ID | Severity | Title | Detail |
|---------|----------|-------|--------|
| HC-001 | 🟡 YELLOW | HCD AdvancedAuthenticator is enabled | HCD at localhost:9042 accepted the credentials and rejected an anonymous connect… |
| HC-002 | 🟡 YELLOW | HCD materialized_views_enabled: false | HCD materialized_views_enabled=false confirmed — JanusGraph will not create CQL … |
| HC-003 | 🟡 YELLOW | HCD guardrail defaults enforce batch-size limits | HCD target pre-flight advisory. |
| HC-004 | 🟡 YELLOW | HCD storage_compatibility_mode: NONE (Cassandra 5+ SSTable formats) | HCD target pre-flight advisory. |
| HC-005 | 🟡 YELLOW | HCD default_secondary_index: sai | HCD target pre-flight advisory. |
| HC-006 | ✅ GREEN | HCD CQL port 9042, datacenter dc1 | HCD target pre-flight advisory. |
| HC-007 | 🟡 YELLOW | HCD linux/amd64 image — Apple Silicon emulation | Apple Silicon (arm64) detected — HCD linux/amd64 image runs under Rosetta emulat… |

### Index

| Rule ID | Severity | Title | Detail |
|---------|----------|-------|--------|
| IX-001 | 🟡 YELLOW | materializedView() → composite index | Detected 'materializedView(' in schema DSL. |
| IX-004 | 🟡 YELLOW | searchIndex().by('x').asText() → mixed index with Mapping.TEXT | Detected 'asText(' in schema DSL. |
| IX-005 | 🟡 YELLOW | searchIndex().by('x').asString() → mixed index with Mapping.STRING | Detected 'asString(' in schema DSL. |
| IX-006 | 🟡 YELLOW | searchIndex() on geo property → mixed index with Mapping.PREFIX_TREE | Detected searchIndex on a geo property (Point type) in schema DSL. |
| IX-008 | 🟡 YELLOW | Vertex-centric RelationIndex → buildEdgeIndex() | Detected vertex-centric edge index pattern in schema DSL. |

### Query Pattern

| Rule ID | Severity | Title | Detail |
|---------|----------|-------|--------|
| QP-006 | 🔴 RED | Search.prefix() / Search.token() usage | searchIndex with asText/asString detected — application code likely uses Search.… |
| QP-010 | 🟡 YELLOW | Geo.inside(Geo.point(...), radius) usage | Geo/Point type detected in schema — application code may use Geo.inside(). Revie… |

### Schema

| Rule ID | Severity | Title | Detail |
|---------|----------|-------|--------|
| SC-001 | 🟡 YELLOW | partitionBy() / clusterBy() on vertex label | Detected 'partitionBy' in schema DSL. |
| SC-003 | 🔴 RED | mapOf(K, V) property type | Detected 'mapOf(' in schema DSL. |
| SC-004 | 🟡 YELLOW | setOf(X) / listOf(X) collection property | Detected 'setOf(' in schema DSL. |
| SC-008 | 🟡 YELLOW | Geospatial property type (Point) in schema | Detected 'Point' in schema DSL. |

### Tinkerpop Delta

| Rule ID | Severity | Title | Detail |
|---------|----------|-------|--------|
| TP-001 | 🟡 YELLOW | repeat() step global semantics change (TinkerPop 3.4 → 3.7) | Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migration… |
| TP-002 | 🟡 YELLOW | valueMap() return format change | Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migration… |
| TP-003 | 🟡 YELLOW | scriptEvaluationTimeout renamed to evaluationTimeout | Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migration… |
| TP-004 | ✅ GREEN | GraphBinary serialisation supported | Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migration… |
| TP-005 | 🟡 YELLOW | property(k, v) cardinality resolution | Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migration… |

---

## Blockers (must resolve before migration)

### 1. [SC-003] mapOf(K, V) property type

**Detail:** Detected 'mapOf(' in schema DSL.

**Remediation:** Use --map-strategy flat (default) to explode map entries into parallel <prop>_key LIST and <prop>_value LIST properties. Or use --map-strategy json to serialise the entire map as a JSON string.

**Docs:** <https://docs.janusgraph.org/schema/>

### 2. [QP-006] Search.prefix() / Search.token() usage

**Detail:** searchIndex with asText/asString detected — application code likely uses Search.token() or Search.prefix(). Review and update to Text.*.

**Remediation:** Replace Search.prefix() on an asString() key with Text.textPrefix() (Mapping.STRING). Replace Search.token() with Text.textContains() and Search.tokenPrefix() with Text.textContainsPrefix() (Mapping.TEXT). The assessment's Index Analysis lists the predicates each indexed key serves.

**Docs:** <https://docs.janusgraph.org/index-backend/text-search/>

---
*Report generated by `dse-jg-migrator assess`. Source cluster not modified. Generated: 2026-11-09T23:02:22.518488Z*