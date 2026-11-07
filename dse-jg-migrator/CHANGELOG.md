# Changelog

All notable changes to `dse-jg-migrator` are documented here.
This file follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) format.

---

## [Unreleased]

### Added

- **ST-1**: Project scaffold — `pyproject.toml`, directory structure, stub modules, `models.py`, `config.py`.
- **ST-0**: Pre-migration compatibility assessor with 51 rules across 8 categories (`compatibility_rules.py`, `assessor.py`, `assessment_reporter.py`). MD/JSON/HTML report output. CLI `assess` command with `--target-cql-host` HCD pre-flight option and `--skip-assessment-gate`.
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

### Changed
*(none)*

### Fixed
*(none)*

---

### Handoff Notes

- ST-0 through ST-9 implemented in a single pass. The dependency chain was observed.
- `assessor.py` is read-only; the no-write constraint is tested in `test_assessor.py::test_no_write_operations`.
- Gremlin scripts use bindings throughout (no f-string interpolation into Groovy).
- All CQL queries in `migrator.py` and `analyser.py` use parameterised statements.
- `seeder.py` requires `--confirm` or interactive `y` — never runs silently.
- `enterprise` template deliberately triggers RED findings to give operators a concrete example.
- E2E tests require `E2E_ENABLED=1` and a live Docker Compose stack.
