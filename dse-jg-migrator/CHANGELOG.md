# Changelog

All notable changes to `dse-jg-migrator` are documented here.
This file follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format.

---

## [Unreleased]

### Added

- HCD authentication and a clean slate (branch `fix/hcd-auth-clean-slate`, #4):
  - `docker/hcd-entrypoint.sh`: the HCD image ships with authentication off (any client connected without a password). The wrapper enables `authenticator` (`enabled: true`, `default_scheme: internal`) and `authorizer` (`enabled: true`) in the image's own `cassandra.yaml`, refuses to start if those lines are missing, then runs the image entrypoint, which still configures the network. (#4)
  - `HCD_CQL_PASSWORD` is required (`${HCD_CQL_PASSWORD:?…}` in the compose file, an `up.sh` preflight check with the fix, and the `env var` of `assess`/`run-all` `--target-cql-password`) — no password default anywhere (AGENTS.md §6.2). (#4)
  - Rule **HC-009** (RED): HCD accepts an anonymous connection — 58 rules. (#4)
  - `./docker/up.sh --clean [--yes]`: lists, then removes this stack's containers (with anonymous volumes), named volumes, network and `./out` in any compose project; keeps images. README and `docker/README.md` document a clean slate. (#4)

- Frictionless onboarding (branch `fix/frictionless-onboarding`, PR #3), after a fresh-user test of the README failed at its first step:
  - README quick start: prerequisites table, then three commands (`./docker/up.sh`, `seed`, `run-all`) with expected times, where results land, stop/resume/reset, and `docker compose … up -d --wait` as the Docker Compose v2 fallback.
  - `./docker/up.sh` preflight (`--check` runs it alone): compose tool present, runtime running, registry login for the HCD image, ≥ 10 CPUs / 20 GiB for containers (prints the exact fix), and on Apple Silicon + Podman re-enables the QEMU emulation that every VM restart drops. Progress lines while a service boots; a stopped HCD/DSE container is explained from its `system.log`.
  - `HCD_IMAGE_TAG` selects the HCD image tag (default `2.0.8-ubi`) for the compose file and the preflight.
  - Rule **CF-006** (RED): source graph unreachable or without schema — 57 rules.
  - Seeder: scalable template loops (`{{V_RANGE}}` / `{{E_RANGE}}`) are submitted in chunks of 200, so any `--vertex-count` / `--edge-count` stays under DSE's 30 s request cap; `seed` reports the graph's real totals.
  - `./docker/up.sh --reset-target [--yes]`: empties only the local target (`jg_migration_test` keyspace, its OpenSearch index, `./out` checkpoints) so another sample can be migrated without `down -v`; asks first unless `--yes`. `tests/e2e` now uses it for its reset (one implementation; the E2E's default-password literal is gone).
- **ST-1**: Project scaffold — `pyproject.toml`, directory structure, stub modules, `models.py`, `config.py`.
- **ST-0**: Pre-migration compatibility assessor with 57 rules across 8 categories (CF-006 added in #3) (`compatibility_rules.py`, `assessor.py`, `assessment_reporter.py`). MD/JSON/HTML report output. CLI `assess` command with `--target-cql-host` HCD pre-flight option and `--skip-assessment-gate`.
- **ST-2**: DSE Graph schema analyser (`analyser.py`) — engine detection, DSL parsing, CQL count enrichment, JSON output.
- **ST-3**: Schema translator (`translator.py`) — full type mapping, UDT flat/json strategy, mapOf flat/json strategy, composite/mixed/vertex-centric index translation, auto geo mixed index for Point properties.
- **ST-4**: JanusGraph / HCD target schema initialiser (`initialiser.py`) — gremlin-script and server-restart modes, index enablement polling, schema verification.
- **ST-5**: Data migrator (`migrator.py`) — CQL-direct (DSG) and Gremlin-paged (Classic) strategies, SQLite checkpoint/resume, vertex and edge batch migration with Gremlin bindings, JSONL audit log.
- **ST-6**: Traversal validator (`validator.py`) — vertex/edge count comparison, smoke-test query execution, validation report.
- **ST-7**: Full CLI wiring (`cli.py`) — `seed`, `assess`, `analyse`, `translate`, `init-target`, `migrate`, `validate`, `report`, `run-all` commands with `rich` progress output.
- **ST-7**: Migration report generator (`reporter.py`) — reads intermediate output files and produces final migration report.
- **ST-8**: Docker E2E stack (`docker/docker-compose-e2e.yml`) — HCD 2.x, OpenSearch 3, JanusGraph, DSE Graph 6.8 source, optional Visualizer. `janusgraph-hcd.properties`, `seed_dse.groovy`, Docker `README.md`, `tests/e2e/test_e2e.py`.
- **ST-9**: Sample workload seeder (`seeder.py`) — four built-in Gremlin templates (`food`, `social`, `iot`, `enterprise`), token substitution with injection prevention, CLI `seed` command with `--confirm` gate.
- **ST-9**: Seed templates (`seed_templates/food.groovy`, `social.groovy`, `iot.groovy`, `enterprise.groovy`).
- Unit tests for all modules with ≥ 80% coverage gate.

- Fix-up for plan gaps (branch `fix/plan-gaps`, PR #2):
  - `python -m dse_jg_migrator` entry point (`__main__.py`).
  - `examples/` (`food-graph-schema.json`, `janusgraph-target.json`, `food-assessment-report.{md,json}`), regenerated from the live `food` run (analyser/translator/assessor against DSE 6.9.26 and the HCD target).
  - `tests/fixtures/food_schema_all_categories.groovy` plus `test_fixtures_coverage.py` asserting QP/SC/IX/CF/OL/SE/TP/HC are all exercised.
  - `requirements.lock` (pip-compile), `.pre-commit-config.yaml` + `.secrets.baseline` (detect-secrets), CI `secrets` job, CI `pip-audit` now scans `requirements.lock`.
  - Appendix E.2: Classic meta-properties are converted to JSON strings with a warning and `META_PROPERTY_JSON` audit entry.
  - Appendix E.6: migration report lists graph labels and flags RLAC for manual recreation (from `config.rlac_enabled` or SE-001/SE-002 findings).
  - Appendix E.7: `SKIPPED_OLAP` audit entry when the analysis shows an OLAP source (`config.olap_enabled`, now populated by the analyser).
  - Appendix E.9: ID map archived to `<graph>-id-map.sqlite` after migration.
  - New unit tests for `cli.py`, `config.py`, `reporter.py`, entry points; expanded `migrator.py`, `validator.py`, `initialiser.py`, `seeder.py` tests.
  - `--decimal-strategy double|string` on `translate`, `migrate` and `run-all` (one shared option). `double` (default) keeps numeric Decimal semantics and counts values that do not round-trip (`stats["lossy_decimals"]`, `LOSSY_DECIMAL` audit event); `string` stores the exact decimal.
  - Migrator retry and idempotent resume: every batch write is retried 3 times (1 s/2 s/4 s backoff); a batch that still fails is audited (`BATCH_FAILED`), leaves its label incomplete, and makes `migrate` exit non-zero. New `edge_map` checkpoint table; re-runs skip vertices in `id_map` and edges in `edge_map`. Edges start only when every vertex label is complete. Dangling-endpoint edges are counted (`MISSING_ENDPOINTS`).
  - `docker/up.sh`: ordered, health-gated stack startup that works with podman-compose (which does not reliably wait for `service_healthy` and recreates dependencies on `up <svc>`).
  - `resolve_target_class()` in `translator.py`: one type-resolution function shared by the translator and the migrator.
  - Classic-engine analysis: `Analyser._parse_classic()` parses Classic `schema.describe()` (property keys, labels, connections, secondary/materialized/search and vertex-centric indexes); grammar taken from the DSE `describe()` implementation; fixture `tests/fixtures/dse69_classic_describe.groovy.txt` captured live.
  - `AnalysisResult.from_dict()` — exact inverse of `to_dict()`; `Translator.load_analysis()` uses it.
  - `--udt-strategy` / `--map-strategy` on `migrate` (shared with `translate` and `run-all`): the migrator writes UDTs as `<udt>_<field>` or one JSON string, `tupleOf` as a JSON array string (SC-006), and maps as `_key`/`_value` LISTs or one JSON string — exactly the keys `translate` created.
  - `init-target` promotes an index whose key was committed earlier (INSTALLED) via REGISTER_INDEX → REINDEX, watches every schema index (re-runs heal stuck ones), and explains a stall caused by a stale JanusGraph instance or a leaked open server transaction (diagnostic only; nothing is force-closed or rolled back).
  - Every batch request has a 120 s timeout; source page reads are retried like writes and, if they still fail, recorded as `SOURCE_READ_FAILED` (label left incomplete) instead of crashing.
  - Seed templates populate every declared type (sets, maps, Point, UDT, tuple, Duration, Decimal, posts/authored, locations) and honour `--edge-count` with unique edge pairs; a unit test guards against variables shared across separately-submitted template blocks.
  - E2E: waits for the four required services (the visualizer has no healthcheck), resets the target before and after (drops only `jg_migration_test`), asserts `assess` < 60 s, seeds small fixed counts.

### Changed

- `assess`/`run-all`: `--target-cql-password` reads `HCD_CQL_PASSWORD` (was a `cassandra` default) and `--target-cql-username` reads `HCD_CQL_USERNAME`; a `--target-cql-host` without a password stops with a hint. `config.py` and `Assessor` no longer default the password to `cassandra`. (#4)
- `up.sh`: `--reset-target` always authenticates; a container that cannot replay its commit log is reported with the exact volume and `./docker/up.sh --clean` (was `down -v`, which misses volumes of other compose projects). (#4)
- `tests/e2e` requires `HCD_CQL_PASSWORD`. (#4)

### Removed

- `hcd-dev/` (a reference HCD compose setup). Its `cassandra.yaml` was no longer mounted (it bound `rpc_address: localhost`); authentication is now enabled by `docker/hcd-entrypoint.sh` instead. `.secrets.baseline` no longer lists its `keystore_password` false positive. (#4)

- Appendix E.8: `init-target` index-enablement wait now defaults to 30 minutes (was 120 s); new `--index-timeout <minutes>` option.
- Coverage gate restored to 80% (was lowered to 60%) and the `omit` list for `cli.py`, `config.py`, `reporter.py` removed. Per-module coverage is now ≥ 80% for every module (overall 95%).
- Plan statuses: every sub-task `[x] done`, each verified live on 2026-11-09 (evidence in each status line and Appendix E.10).
- Type mapping restricted to JanusGraph's documented property data types (enforced by `test_type_map_targets_are_documented_janusgraph_types`). `Duration`: Classic → `java.lang.Long` milliseconds, Core (DSG) → ISO-8601 `java.lang.String` (was the undocumented `java.time.Duration`). Decimal conversion moved from `_row_to_props` to the per-key `_target_value`.
- `docker/janusgraph-hcd.properties`: the default `cassandra` password is replaced by a placeholder (AGENTS.md §6.1).
- `docker-compose-e2e.yml`: OpenSearch (security disabled) skips the demo security config (`DISABLE_INSTALL_DEMO_CONFIG=true`), so no admin password — default or otherwise — is committed (flagged by the IBM Vault Radar pre-commit control). (#2)
- `.pre-commit-config.yaml`: detect-secrets pinned to v1.5.0, matching the baseline and CI; v1.4.0 could not load the 1.5.0 baseline and failed every commit. (#2)
- `docker-compose-e2e.yml`: the E2E source is DSE 6.9.26 (`-g -s`, `cpus: 8`; needs a Podman VM with ≥ 10 CPUs) — DSE 6.8's Java 8 JVM wedged at a safepoint on every boot under QEMU. 6.8 remains supported on native `amd64`.
- Migrator reads the DSE source over GraphSON v3 (DSE GraphBinary types are undecodable by gremlin-python); the checkpoint DB defaults to `<output_dir>/<graph>-migration-checkpoint.db` (was the CWD).

### Fixed

- (#4) JanusGraph's HCD credentials were set with keys JanusGraph does not have (`storage.cql.username` / `storage.cql.password`, silently ignored) in the compose file, `janusgraph-hcd.properties`, the plan and HC-001's text; the real keys are `storage.username` / `storage.password` (`janusgraph-cfg.md`). Unnoticed because HCD never enforced authentication; with it enforced, JanusGraph failed to start ("requires authentication … but no authenticator configured").
- (#4) `up.sh --reset-target` checks the HCD credentials before stopping JanusGraph, so a wrong password changes nothing.
- (#4) HC-001 claimed "AdvancedAuthenticator confirmed active — credentials accepted" for any successful credentialed connection, but the HCD image accepts every password while authentication is off. The assessor now also tries an anonymous connection (connect only, no query): rejected → HC-001, accepted → HC-009, inconclusive → HC-001 worded as unverified.
- (#3) Quick start step 1 (`docker compose up -d`) returned before the stack was healthy under Podman, so step 2 failed; DSE could also crash on start (exit 100, commit-log replay "incompatible schema") after an unclean stop. `stop_grace_period: 2m` for HCD/DSE (podman-compose applies it only in its own `stop`/`down`, so `up.sh` also sets `--stop-timeout=120` on the containers); a stop now logs "DSE shutdown complete" and the next start replays nothing.
- (#3) `seed` and other commands printed a 60-line aiohttp traceback when a cluster was unreachable; the CLI now prints one line (operation, host, port, hint) and exits 1. Unexpected errors still raise.
- (#3) With the source unreachable, `assess` reported MEDIUM risk from static advisories (passing the gate) and `analyse` wrote an empty analysis with exit 0. Now CF-006 (HIGH risk) and a clear `analyse` failure.
- (#3) `seed` with its documented defaults (1000 / 5000) exceeded DSE's 30 s request cap under emulation and failed with a traceback, leaving a partial graph; loops are now chunked and a rejected statement raises a clear `SeedError`.
- (#3) Docs claimed the seeder is idempotent; only schema creation is — data is appended on each run (plan, `seed --help` and README corrected).
Found by running against live DSE 6.8/6.9 sources and a live JanusGraph + HCD + OpenSearch target (Podman on Apple Silicon):

- `initialiser.py`: every schema script failed on a real JanusGraph server ("enclosing transaction is closed") because it ended on `mgmt.commit();`. Rewritten as ONE idempotent, rollback-on-error management transaction that returns a value. Keys and indexes are created in one transaction so new indexes are ENABLED at once; an index over an already-committed key is promoted (see Added). Index status is read per key (`getIndexStatus()` with no argument fails); `verify_schema` now checks keys, labels and indexes.
- `translator.py`: JanusGraph rejects `LocalDate`, `LocalTime`, `BigDecimal`, `BigInteger` and `InetAddress` ("Not a supported data type"). `TYPE_MAP` now maps Decimal→Double (lossy), Varint→String, Date→`java.util.Date`, Time→String, Inet→String, Point→`Geoshape` (a `PREFIX_TREE` mixed index cannot be built on a String). Lossy mappings are listed in the translation report.
- `analyser.py` / `translator.py`: edge MV keys were taken from the OUT/IN endpoint columns (vertex properties), producing an invalid edge index ("Key must be a user defined key"). Edge MV keys now come from the edge's own property `clusterBy(...)` columns; an edge MV with none creates no index.
- Traversal source is now `<graph>.g` (not `g`) for DSE queries.
- Analyser: real DSE 6.8 Core `schema.describe()` output (one statement per line, `Varchar`, `mapOf(Varchar, Date)`, edge MVs, `partitionBy(OUT, ...)`) is parsed correctly; DSG edge tables are `<from>__<label>__<to>`.
- `migrator.py`: batch scripts returned un-iterated traversals (server error "Serializer ... DefaultGraphTraversal not found") — now `.next().id()`. `datetime.date` cannot be serialised by gremlin-python and tz-aware datetimes are rejected by `java.util.Date`; dates are sent as naive midnight datetimes. Point values are rebuilt with `Geoshape.fromWkt(<binding>)`; Varint ints become Strings; Decimal→float; Inet/Time→String; `solr_query` dropped; sets/maps flattened.
- `migrator.py`: a batch that failed was logged and its label still marked complete, so re-runs never retried it (now retried, then left incomplete — see Added). Edges with a missing endpoint were dropped without being counted.
- `migrator.py` (Classic path): `elementMap()` was read with string keys (`~id`, `~outV`), but gremlin-python returns `T.id`/`T.label`/`Direction.OUT`/`Direction.IN` tokens, so every Classic vertex got a placeholder id and every Classic edge was dropped. Now handled by `_split_element_map()`; Classic `Decimal` values (previously sent unconverted on the Gremlin path) are converted too.
- `migrator.py` (Core path): CQL `Duration` values were sent unconverted (gremlin-python cannot serialise `cassandra.util.Duration`); now ISO-8601 strings.
- `cli.py migrate`: printed "Migration complete" and exited 0 even when batches failed. `init-target` likewise exited 0 when schema creation failed, so `run-all` migrated into a missing schema.
- `migrator.py`: UDT and `tupleOf` values (namedtuple / tuple over CQL) were list-expanded into repeated values of a SINGLE key — silently keeping only the last field. `mapOf` columns (`OrderedMapSerializedKey`, a `Mapping` but not a `dict`) were not flattened; the server could not decode the resulting bytes and never answered, hanging the migrator.
- `migrator.py`: gremlin-python packs a plain `int` as int32, so `Bigint` values near `Long.MAX_VALUE` failed client-side; Long keys (and out-of-range ints) are now sent as `long`.
- `migrator.py` (Classic): `elementMap()` returned only one value of a multi-valued property — vertices are now read with `valueMap(true)`; Classic `Timestamp` values (raw GraphSON `gx:Instant`) are decoded.
- `translator.py`: `load_analysis()` dropped clustering keys (e.g. `reading.ts`), so their property keys were never created; a key-less inverse edge MV produced a vertex-centric index JanusGraph rejects ("Need to specify sort keys"); a UDT property also got an unused String key.
- `validator.py`: "0/0 checks passed" when the analysis had no labels; two failed counts (-1 == -1) counted as a match.
- `tests/unit/test_analyser.py::test_run_produces_json_file` made a real CQL connection to whatever listened on localhost:9043 (AGENTS.md §5.3); now mocked, and the suite was audited for real connections (0).
- Seed templates: `social`, `food` and `enterprise` capped each edge label at the vertex count (`social` yielded 49 edges for `--edge-count 100`) and never populated their sets, maps, Points, UDTs, tuples or `post`/`location` data.
- `docker-compose-e2e.yml`: the DSE healthcheck now runs `cqlsh` — `nodetool status` reported `UN` (and the container "healthy") while CQL still refused connections.
- `hcd-dev/README.md`: HCD image path corrected to `cp.icr.io/cp/ibm-hyper-converged-database/hcd:2.0.8-ubi`.
- `seeder.py` and seed templates: two-client routing (`system` vs `<graph>.g`), `LocalDate`/`Instant` for Date/Timestamp, integer division.
- Docker: HCD needs `CASSANDRA_LIBJEMALLOC=-` and QEMU (not Rosetta) on Apple Silicon; `hcd-dev/cassandra.yaml` binds `rpc_address: localhost` so it is no longer mounted; DSE needs `-g` (plus `-s` for `searchIndex()`) and 6 GB. See `docker/README.md`.

### Known limitations

- Live-verified (2026-11-09, DSE 6.9.26 → JanusGraph + HCD 2.0.8 + OpenSearch 3): every CLI stage on five graphs (`iot`, `social`, `food`, `enterprise` on Core; `classic_demo` on Classic) with field-by-field value comparison, checkpoint resume after a failed batch and after a power loss, and `tests/e2e` 4/4 run twice. DSE 6.8 could not be booted under QEMU (Java 8 safepoint hang) — the Core parser is still covered by the captured 6.8 `describe()` fixture.
- Classic meta-property indexes (`.index(...).property(...)`) are not translated; a Classic edge label with several `.connection()`s keeps the first as its from/to (the Gremlin read path does not use it).
- Migrating several source graphs into one target graph merges same-named labels (e.g. two graphs with `person`); `validate` then correctly reports a count mismatch. Use one target graph per source graph, or distinct labels.
- DSE Core `listOf` is a single list-valued property; on JanusGraph LIST cardinality is separate values, so `values('roles')` returns flat values instead of a nested list — data is identical, application queries may need adjusting.
- Index registration can stall on a stale JanusGraph instance record or a leaked open server transaction; `init-target` reports both with the documented remedies but never forces them. Under QEMU one leaked transaction was observed once (suspected source: the emulated healthcheck console — unconfirmed).
- Retry duplicate risk: if JanusGraph commits a batch but the client times out, the retry writes that batch again. The validator's count check exposes it.
- `--decimal-strategy`, `--udt-strategy` and `--map-strategy` must match between `translate` and `migrate` when they are run separately (`run-all` passes one value of each to both); a mismatch makes the affected batches fail and be reported.
- `requirements.lock` was compiled on Python 3.13; re-resolving on Python 3.12.9 produced an identical lock, and the full unit suite, `mypy --strict` and ruff pass on 3.12.9 too.
- `.secrets.baseline` records one known false positive: `keystore_password` in the reference `hcd-dev/cassandra.yaml` (vendor default). `docker/janusgraph-hcd.properties` no longer carries the default `cassandra` password (AGENTS.md §6.1); it is a placeholder supplied at deploy time.
- (#5) With the source unreachable, commands printed `RuntimeError: Cannot run the event loop while another loop is running` tracebacks and "Unclosed client session" warnings: gremlin-python orphans the transport of a failed connect and GC closed it inside another event loop. All Gremlin clients now open through `gremlin_io.open_gremlin_client()`, whose transport closes itself when its connect fails.
- (#5) `assess` / `analyse` / `run-all` printed DSE's full Java stack trace (`Variable 'a' … is unknown`) for the OLAP capability probe, which fails by design when OLAP is off. `gremlin_io.quiet_gremlin_errors()` mutes gremlin-python's log for those probes only; results are unchanged.
- (#6) `seed` printed a "Next step" `assess` command without the required `--source-cql-host`, so copying it failed ("Missing option"). The hint now adds the Gremlin URL's host and its CQL port (9043 for the local stack's DSE on 8183, otherwise 9042); a unit test parses the hint with the `assess` command itself.

---

### Handoff Notes

- ST-0 through ST-9 implemented in a single pass. The dependency chain was observed.
- `assessor.py` is read-only; the no-write constraint is tested in `test_assessor.py::test_no_write_operations`.
- Gremlin scripts use bindings throughout (no f-string interpolation into Groovy).
- All CQL queries in `migrator.py` and `analyser.py` use parameterised statements.
- `seeder.py` requires `--confirm` or interactive `y` — never runs silently.
- `enterprise` template deliberately triggers RED findings to give operators a concrete example.
- E2E tests require `E2E_ENABLED=1` and a live Docker Compose stack.
