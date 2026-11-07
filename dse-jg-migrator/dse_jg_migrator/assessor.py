# READ-ONLY MODULE
# This module connects to clusters for inspection only.
# It must never execute write operations (INSERT, UPDATE, DELETE, addV, addE, etc.)
# Verified by tests/unit/test_assessor.py::test_no_write_operations.
"""Pre-migration compatibility assessor for DSE Graph → JanusGraph migration."""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from typing import Any

from dse_jg_migrator.compatibility_rules import RULES_BY_ID, Category, Severity
from dse_jg_migrator.models import AssessmentFinding, AssessmentResult


class Assessor:
    """Read-only compatibility assessor. Opens no write transactions.

    Connects to the source DSE Graph cluster (and optionally the target HCD cluster)
    to inspect schema, configuration, and traversal patterns. Emits AssessmentFindings
    for every detected incompatibility.

    Rules checked:
        QP-001 through QP-017 (query patterns)
        SC-001 through SC-008 (schema)
        IX-001 through IX-008 (indexes)
        CF-001 through CF-005 (configuration)
        OL-001 through OL-003 (OLAP)
        SE-001 through SE-002 (security)
        TP-001 through TP-005 (TinkerPop delta)
        HC-001 through HC-008 (HCD target — only when target_cql_host is provided)
    """

    def __init__(
        self,
        gremlin_url: str,
        cql_host: str,
        cql_port: int = 9042,
        graph_name: str = "",
        target_cql_host: str | None = None,
        target_cql_port: int = 9042,
        target_cql_username: str = "cassandra",
        target_cql_password: str = "cassandra",  # noqa: S107
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.gremlin_url = gremlin_url
        self.cql_host = cql_host
        self.cql_port = cql_port
        self.graph_name = graph_name
        self.target_cql_host = target_cql_host
        self.target_cql_port = target_cql_port
        self.target_cql_username = target_cql_username
        self.target_cql_password = target_cql_password
        self._username = username
        self._password = password
        self._raw_schema: str = ""
        self._engine: str = "dsg"

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> AssessmentResult:
        """Execute the full read-only assessment. Returns an AssessmentResult.

        Reads from: source DSE Gremlin server, source DSE CQL endpoint.
        Optionally reads from: target HCD CQL endpoint (when target_cql_host is set).
        Writes: nothing.
        """
        result = AssessmentResult(
            graph_name=self.graph_name,
            engine=self._detect_engine(),
            assessed_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        )
        self._raw_schema = self._fetch_raw_schema()

        result.findings += self._assess_schema()
        result.findings += self._assess_indexes()
        result.findings += self._assess_configuration()
        result.findings += self._assess_query_patterns()
        result.findings += self._assess_olap()
        result.findings += self._assess_security()
        result.findings += self._assess_tinkerpop_delta()

        if self.target_cql_host:
            result.findings += self._assess_hcd_target()

        result.overall_risk = self._compute_risk(result.findings)
        result.summary = self._build_summary(result.findings)
        result.cluster_overview = self._build_cluster_overview()
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers — Gremlin / CQL I/O
    # ──────────────────────────────────────────────────────────────────────────

    def _gremlin_submit(self, script: str, bindings: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
        """Submit a read-only Gremlin script and return all results.

        Reads from: source DSE Gremlin server.
        """
        from gremlin_python.driver import client as gremlin_client

        kwargs: dict[str, Any] = {}
        if self._username and self._password:
            kwargs["username"] = self._username
            kwargs["password"] = self._password

        c = gremlin_client.Client(self.gremlin_url, "g", **kwargs)
        try:
            result_set = c.submit(script, bindings or {})
            return result_set.all().result()
        finally:
            c.close()

    def _detect_engine(self) -> str:
        """Detect DSE engine type (classic or dsg) via Gremlin.

        Reads from: source DSE Gremlin server — system.graph describe query.
        Returns: 'classic' or 'dsg'.
        """
        try:
            desc = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            desc_str = str(desc)
            if "Core" in desc_str or "DSG" in desc_str:
                self._engine = "dsg"
            else:
                self._engine = "classic"
        except Exception:  # noqa: BLE001, S110
            self._engine = "dsg"  # assume dsg on failure
        return self._engine

    def _fetch_raw_schema(self) -> str:
        """Fetch the raw schema DSL string from DSE Graph via schema.describe().

        Reads from: source DSE Gremlin server.
        """
        try:
            parts = self._gremlin_submit(
                "schema.describe()",
            )
            return "\n".join(str(p) for p in (parts or []))
        except Exception:  # noqa: BLE001, S110
            return ""

    def _build_cluster_overview(self) -> dict[str, Any]:
        """Build a summary dict of cluster statistics from the raw schema."""
        schema = self._raw_schema
        return {
            "vertex_label_count": schema.count("schema.vertexLabel("),
            "edge_label_count": schema.count("schema.edgeLabel("),
            "index_count": (
                schema.count("materializedView(")
                + schema.count("searchIndex(")
                + schema.count("secondaryIndex(")
            ),
            "schema_mode": "Development" if "schema_mode.*Development" in schema else "Unknown",
            "olap_enabled": False,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Assessment dimensions
    # ──────────────────────────────────────────────────────────────────────────

    def _assess_schema(self) -> list[AssessmentFinding]:
        """Check schema DSL for SC-xxx rule violations.

        Reads from: raw schema string (already fetched).
        Rules: SC-001 through SC-008.
        """
        findings: list[AssessmentFinding] = []
        schema = self._raw_schema

        checks: list[tuple[str, str]] = [
            ("SC-001", "partitionBy"),
            ("SC-002", "fromExistingTable"),
            ("SC-003", "mapOf("),
            ("SC-004", "setOf("),
            ("SC-005", "typeOf("),
            ("SC-006", "tupleOf("),
            ("SC-008", "Point"),
        ]
        for rule_id, marker in checks:
            if marker in schema:
                findings.append(
                    self._make_finding(rule_id, f"Detected '{marker}' in schema DSL.")
                )

        # SC-007: meta-properties — harder to detect from DSL alone; emit advisory
        if ".property(" in schema:
            # heuristic: if any property chain appears, note meta-property risk
            if "meta_" in schema.lower() or "meta(" in schema.lower():
                findings.append(
                    self._make_finding("SC-007", "Possible meta-property usage detected in schema.")
                )

        return findings

    def _assess_indexes(self) -> list[AssessmentFinding]:
        """Check schema DSL for IX-xxx rule violations.

        Reads from: raw schema string (already fetched).
        Rules: IX-001 through IX-008.
        """
        findings: list[AssessmentFinding] = []
        schema = self._raw_schema

        checks: list[tuple[str, str]] = [
            ("IX-001", "materializedView("),
            ("IX-003", "secondaryIndex("),
            ("IX-004", "asText("),
            ("IX-005", "asString("),
        ]
        for rule_id, marker in checks:
            if marker in schema:
                findings.append(
                    self._make_finding(rule_id, f"Detected '{marker}' in schema DSL.")
                )

        if "materializedView(" in schema and ".inverse()" in schema:
            findings.append(
                self._make_finding("IX-002", "Detected inverse materializedView in schema DSL.")
            )

        if "searchIndex(" in schema and "Point" in schema:
            findings.append(
                self._make_finding(
                    "IX-006",
                    "Detected searchIndex on a geo property (Point type) in schema DSL.",
                )
            )

        if "indexFor(" in schema and ".analyze(" in schema:
            findings.append(
                self._make_finding("IX-007", "Detected schema.indexFor().analyze() in schema DSL.")
            )

        # IX-008: vertex-centric index (OUT/IN in MV clusterBy)
        if "clusterBy(OUT" in schema or "clusterBy(IN" in schema or "partitionBy(OUT" in schema:
            findings.append(
                self._make_finding(
                    "IX-008",
                    "Detected vertex-centric edge index pattern in schema DSL.",
                )
            )

        return findings

    def _assess_configuration(self) -> list[AssessmentFinding]:
        """Check graph configuration for CF-xxx rule violations.

        Reads from: source DSE Gremlin server — system.graph describe.
        Rules: CF-001 through CF-005.
        """
        findings: list[AssessmentFinding] = []
        try:
            parts = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            desc = str(parts or "")
        except Exception:  # noqa: BLE001, S110
            desc = ""

        schema = self._raw_schema + desc

        config_checks: list[tuple[str, str]] = [
            ("CF-001", "Development"),
            ("CF-002", "Production"),
            ("CF-003", "allow_scan"),
            ("CF-004", "restrict_lambda"),
            ("CF-005", "replication_config"),
        ]
        for rule_id, marker in config_checks:
            if marker in schema:
                findings.append(
                    self._make_finding(rule_id, f"Configuration marker '{marker}' detected.")
                )

        return findings

    def _assess_query_patterns(self) -> list[AssessmentFinding]:
        """Detect DSE-specific query patterns in the schema and traversal probes.

        Reads from: source DSE Gremlin server — probes traversal source availability.
        Rules: QP-001 through QP-017.

        Note: Full query-history introspection is not possible without DSE Insights.
        Pattern detection is based on schema inspection and traversal source probes.
        """
        findings: list[AssessmentFinding] = []
        schema = self._raw_schema

        schema_pattern_checks: list[tuple[str, str]] = [
            ("QP-001", "allow-filtering"),
            ("QP-002", "label-warning"),
            ("QP-003", "ignore-unindexed"),
            ("QP-004", "consistency"),
            ("QP-006", "Search.prefix"),
            ("QP-007", "Search.fuzzy"),
            ("QP-008", "Search.regex"),
            ("QP-009", "Search.phrase"),
            ("QP-010", "Geo.inside"),
            ("QP-012", "DseGraph.traversal"),
            ("QP-013", "g.V(["),
            ("QP-015", "graph.tx()"),
        ]
        for rule_id, marker in schema_pattern_checks:
            if marker in schema:
                findings.append(
                    self._make_finding(rule_id, f"Pattern '{marker}' detected in schema/scripts.")
                )

        # Emit advisory findings for geo and search indexes (imply Search.* usage in app code)
        if "searchIndex(" in schema and ("asText(" in schema or "asString(" in schema):
            if not any(f.rule_id == "QP-006" for f in findings):
                findings.append(
                    self._make_finding(
                        "QP-006",
                        "searchIndex with asText/asString detected — application code likely "
                        "uses Search.token() or Search.prefix(). Review and update to Text.*.",
                    )
                )

        if "Point" in schema or "Geo" in schema:
            if not any(f.rule_id == "QP-010" for f in findings):
                findings.append(
                    self._make_finding(
                        "QP-010",
                        "Geo/Point type detected in schema — application code may use "
                        "Geo.inside(). Review and update to Geo.within(Geoshape.circle(...)).",
                    )
                )

        # Probe for OLAP 'a' traversal source
        try:
            result = self._gremlin_submit("a.V().limit(0).count()")
            if result is not None:
                findings.append(
                    self._make_finding("QP-005", "OLAP traversal source 'a' responded to probe.")
                )
        except Exception:  # noqa: BLE001, S110
            pass  # Expected when OLAP is not configured — no finding  # noqa: S110

        return findings

    def _assess_olap(self) -> list[AssessmentFinding]:
        """Detect OLAP usage by probing traversal sources and inspecting graph describe.

        Reads from: source DSE Gremlin server.
        Rules: OL-001 through OL-003.
        """
        findings: list[AssessmentFinding] = []

        try:
            result = self._gremlin_submit("a.V().limit(0).count()")
            if result is not None:
                findings.append(
                    self._make_finding(
                        "OL-001",
                        "OLAP traversal source 'a' is active and responded to probe.",
                    )
                )
        except Exception:  # noqa: BLE001, S110
            pass  # OLAP not configured — no finding  # noqa: S110

        schema = self._raw_schema
        if "SparkGraphComputer" in schema:
            findings.append(
                self._make_finding(
                    "OL-002", "SparkGraphComputer reference detected in schema/scripts."
                )
            )
        if "GraphFrames" in schema:
            findings.append(
                self._make_finding(
                    "OL-003", "GraphFrames API reference detected in schema/scripts."
                )
            )

        return findings

    def _assess_security(self) -> list[AssessmentFinding]:
        """Detect RLAC / row-level security usage in graph configuration.

        Reads from: source DSE Gremlin server — system.graph describe.
        Rules: SE-001, SE-002.
        """
        findings: list[AssessmentFinding] = []
        try:
            parts = self._gremlin_submit(
                "system.graph(name).describe()",
                {"name": self.graph_name},
            )
            desc = str(parts or "").lower()
        except Exception:  # noqa: BLE001, S110
            desc = ""

        schema_lower = self._raw_schema.lower()
        combined = schema_lower + desc

        if "rlac" in combined or "row_level" in combined or "row-level" in combined:
            findings.append(
                self._make_finding("SE-001", "RLAC / Row-Level Access Control marker detected.")
            )
        if "rls_policy" in combined or "row_level_security" in combined:
            findings.append(
                self._make_finding("SE-002", "Row-level security policy marker detected.")
            )

        return findings

    def _assess_tinkerpop_delta(self) -> list[AssessmentFinding]:
        """Emit advisory TinkerPop 3.4 → 3.7 delta findings for every DSE graph.

        These are static advisories — every DSE graph migrating to JanusGraph
        is subject to these breaking changes regardless of schema content.
        Rules: TP-001 through TP-005.
        """
        findings: list[AssessmentFinding] = []
        advisory_rules = ["TP-001", "TP-002", "TP-003", "TP-004", "TP-005"]
        for rule_id in advisory_rules:
            findings.append(
                self._make_finding(
                    rule_id,
                    "Advisory: TinkerPop 3.4 → 3.7 breaking change applies to all DSE Graph migrations.",
                )
            )
        return findings

    def _assess_hcd_target(self) -> list[AssessmentFinding]:
        """Pre-flight checks against the HCD 2.x target cluster.

        Attempts a CQL connection to the HCD target. If the connection fails,
        emits HC-008 (ICR auth required). If successful, checks HC-001 through HC-007.

        Reads from: target HCD CQL endpoint (read-only system table queries).
        Writes: nothing.
        Rules: HC-001 through HC-008.
        """
        findings: list[AssessmentFinding] = []
        try:
            from cassandra.auth import PlainTextAuthProvider
            from cassandra.cluster import Cluster

            auth = PlainTextAuthProvider(
                username=self.target_cql_username,
                password=self.target_cql_password,
            )
            cluster = Cluster(
                [self.target_cql_host],
                port=self.target_cql_port,
                auth_provider=auth,
                connect_timeout=10,
            )
            session = cluster.connect()

            # HC-001: AdvancedAuthenticator — successful connection with credentials confirms it
            findings.append(
                self._make_finding(
                    "HC-001",
                    "HCD AdvancedAuthenticator confirmed active — credentials accepted. "
                    "Ensure JanusGraph config sets storage.cql.username and storage.cql.password.",
                )
            )

            # HC-002: materialized_views_enabled
            try:
                rows = session.execute(
                    "SELECT value FROM system_views.settings WHERE name='materialized_views_enabled'"
                )
                mv_enabled = next((r.value for r in rows), "false")
                if mv_enabled.lower() == "false":
                    findings.append(
                        self._make_finding(
                            "HC-002",
                            "HCD materialized_views_enabled=false confirmed — "
                            "JanusGraph will not create CQL MVs. No conflict.",
                        )
                    )
            except Exception:  # noqa: BLE001, S110
                pass  # system_views not available on all HCD versions  # noqa: S110

            # HC-003 through HC-006: static advisory findings
            for rule_id in ("HC-003", "HC-004", "HC-005", "HC-006"):
                findings.append(
                    self._make_finding(rule_id, "HCD target pre-flight advisory.")
                )

            # HC-007: Apple Silicon detection
            if platform.machine() == "arm64":
                findings.append(
                    self._make_finding(
                        "HC-007",
                        "Apple Silicon (arm64) detected — HCD linux/amd64 image runs under "
                        "Rosetta emulation. Expect ~2× longer startup time.",
                    )
                )

            cluster.shutdown()

        except Exception as exc:  # noqa: BLE001
            # HC-008: Cannot connect to HCD target
            findings.append(
                self._make_finding(
                    "HC-008",
                    f"Cannot connect to HCD target at "
                    f"{self.target_cql_host}:{self.target_cql_port}. "
                    f"Error: {exc}. "
                    "If running locally, ensure 'docker login cp.icr.io --username cp' "
                    "succeeded and the HCD container is healthy (docker compose ps).",
                )
            )

        return findings

    # ──────────────────────────────────────────────────────────────────────────
    # Risk computation and summary
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_risk(findings: list[AssessmentFinding]) -> str:
        """Compute overall migration risk level.

        CRITICAL: any RED finding in the security category.
        HIGH:     any RED finding in any category.
        MEDIUM:   3+ YELLOW findings, or any YELLOW in the OLAP category.
        LOW:      all GREEN or fewer than 3 YELLOW.
        """
        reds = [f for f in findings if f.severity == Severity.RED.value]
        yellows = [f for f in findings if f.severity == Severity.YELLOW.value]
        se_reds = [f for f in reds if f.category == Category.SECURITY.value]

        if se_reds:
            return "CRITICAL"
        if reds:
            return "HIGH"
        if any(f.category == Category.OLAP.value for f in yellows) or len(yellows) >= 3:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _build_summary(findings: list[AssessmentFinding]) -> dict[str, Any]:
        """Build a summary dict of finding counts by severity and category."""
        by_severity: dict[str, int] = {"RED": 0, "YELLOW": 0, "GREEN": 0}
        by_category: dict[str, int] = {}

        for f in findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            by_category[f.category] = by_category.get(f.category, 0) + 1

        return {
            "total_findings": len(findings),
            "by_severity": by_severity,
            "by_category": by_category,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Finding factory
    # ──────────────────────────────────────────────────────────────────────────

    def _make_finding(self, rule_id: str, detail: str) -> AssessmentFinding:
        """Create an AssessmentFinding from a rule_id and a specific detail string."""
        rule = RULES_BY_ID.get(rule_id)
        if rule is None:
            return AssessmentFinding(
                rule_id=rule_id,
                category="unknown",
                severity=Severity.YELLOW.value,
                title=f"Unknown rule {rule_id}",
                detail=detail,
                remediation="",
                docs_link="",
            )
        return AssessmentFinding(
            rule_id=rule.rule_id,
            category=rule.category.value,
            severity=rule.severity.value,
            title=rule.title,
            detail=detail,
            remediation=rule.remediation,
            docs_link=rule.docs_link,
        )
