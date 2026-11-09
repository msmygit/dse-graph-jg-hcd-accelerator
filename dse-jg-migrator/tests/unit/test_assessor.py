"""Unit tests for assessor.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cassandra import AuthenticationFailed
from cassandra.cluster import NoHostAvailable

from dse_jg_migrator.assessor import Assessor
from dse_jg_migrator.models import AssessmentFinding, AssessmentResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FOOD_SCHEMA_DSL = """
schema.vertexLabel('person').ifNotExists()
  .partitionBy('person_id', Uuid)
  .property('name', Text)
  .property('nickname', setOf(Text))
  .property('badge', mapOf(Text, Date))
  .property('location', Point)
  .create()

schema.vertexLabel('recipe').ifNotExists()
  .partitionBy('recipe_id', Uuid)
  .property('name', Text)
  .property('instructions', Text)
  .create()

schema.edgeLabel('knows').ifNotExists()
  .from('person').to('person').property('since', Date).create()

schema.vertexLabel('person').materializedView('person_by_name').ifNotExists()
  .partitionBy('name').clusterBy('person_id', Asc).create()

schema.edgeLabel('knows').from('person').to('person')
  .materializedView('knows_by_since').ifNotExists()
  .partitionBy(OUT, 'person_id').clusterBy('since', Asc).clusterBy(IN, 'person_id', Asc).create()

schema.vertexLabel('recipe').searchIndex().ifNotExists()
  .by('instructions').asText().by('name').asString().create()
"""


def _make_assessor(**kwargs) -> Assessor:  # type: ignore[no-untyped-def]
    defaults = {
        "gremlin_url": "ws://localhost:8183/gremlin",
        "cql_host": "localhost",
        "cql_port": 9043,
        "graph_name": "food",
    }
    defaults.update(kwargs)
    return Assessor(**defaults)


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------


def test_detect_engine_dsg() -> None:
    """_detect_engine returns 'dsg' when Gremlin response contains 'Core'."""
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", return_value=["engine:Core"]):
        assert assessor._detect_engine() == "dsg"


def test_detect_engine_classic() -> None:
    """_detect_engine returns 'classic' when response does not mention Core/DSG."""
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", return_value=["engine:Classic"]):
        assert assessor._detect_engine() == "classic"


def test_detect_engine_fallback_on_error() -> None:
    """_detect_engine falls back to 'dsg' if Gremlin call raises."""
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", side_effect=Exception("conn refused")):
        assert assessor._detect_engine() == "dsg"


# ---------------------------------------------------------------------------
# Schema assessment
# ---------------------------------------------------------------------------


def test_assess_schema_detects_mapOf() -> None:
    """_assess_schema emits SC-003 when mapOf( is present in schema DSL."""
    assessor = _make_assessor()
    assessor._raw_schema = "schema.vertexLabel('x').property('badge', mapOf(Text, Date)).create()"
    findings = assessor._assess_schema()
    rule_ids = [f.rule_id for f in findings]
    assert "SC-003" in rule_ids


def test_assess_schema_detects_setOf() -> None:
    """_assess_schema emits SC-004 when setOf( is present."""
    assessor = _make_assessor()
    assessor._raw_schema = ".property('tags', setOf(Text))"
    findings = assessor._assess_schema()
    assert any(f.rule_id == "SC-004" for f in findings)


def test_assess_schema_detects_udt() -> None:
    """_assess_schema emits SC-005 when typeOf( is present."""
    assessor = _make_assessor()
    assessor._raw_schema = ".property('address', frozen(typeOf('address_type')))"
    findings = assessor._assess_schema()
    assert any(f.rule_id == "SC-005" for f in findings)


def test_assess_schema_detects_geo_point() -> None:
    """_assess_schema emits SC-008 when Point type is in schema DSL."""
    assessor = _make_assessor()
    assessor._raw_schema = ".property('loc', Point)"
    findings = assessor._assess_schema()
    assert any(f.rule_id == "SC-008" for f in findings)


def test_assess_schema_clean_schema() -> None:
    """_assess_schema emits no findings for a clean schema."""
    assessor = _make_assessor()
    assessor._raw_schema = (
        "schema.vertexLabel('person').property('name', Text).create()\nschema.edgeLabel('knows').create()"
    )
    findings = assessor._assess_schema()
    assert len(findings) == 0


# ---------------------------------------------------------------------------
# Index assessment
# ---------------------------------------------------------------------------


def test_assess_indexes_detects_mv() -> None:
    """_assess_indexes emits IX-001 for materializedView."""
    assessor = _make_assessor()
    assessor._raw_schema = FOOD_SCHEMA_DSL
    findings = assessor._assess_indexes()
    assert any(f.rule_id == "IX-001" for f in findings)


def test_assess_indexes_detects_search_text() -> None:
    """_assess_indexes emits IX-004 for asText()."""
    assessor = _make_assessor()
    assessor._raw_schema = FOOD_SCHEMA_DSL
    findings = assessor._assess_indexes()
    assert any(f.rule_id == "IX-004" for f in findings)


def test_assess_indexes_detects_inverse_mv() -> None:
    """_assess_indexes emits IX-002 for .inverse()."""
    assessor = _make_assessor()
    assessor._raw_schema = ".materializedView('x').ifNotExists().inverse().create()"
    findings = assessor._assess_indexes()
    assert any(f.rule_id == "IX-002" for f in findings)


# ---------------------------------------------------------------------------
# Query pattern assessment
# ---------------------------------------------------------------------------


def test_assess_query_patterns_olap_source_active() -> None:
    """_assess_query_patterns emits QP-005 when 'a' traversal source responds."""
    assessor = _make_assessor()
    assessor._raw_schema = ""
    with patch.object(assessor, "_gremlin_submit", return_value=[0]):
        findings = assessor._assess_query_patterns()
    assert any(f.rule_id == "QP-005" for f in findings)


def test_assess_query_patterns_olap_source_inactive() -> None:
    """_assess_query_patterns does NOT emit QP-005 when OLAP probe raises."""
    assessor = _make_assessor()
    assessor._raw_schema = ""

    def _side_effect(script: str, bindings: dict = None) -> list:  # type: ignore[assignment]
        if "a.V()" in script:
            raise Exception("OLAP not configured")
        return []

    with patch.object(assessor, "_gremlin_submit", side_effect=_side_effect):
        findings = assessor._assess_query_patterns()
    assert not any(f.rule_id == "QP-005" for f in findings)


# ---------------------------------------------------------------------------
# TinkerPop delta (static advisories)
# ---------------------------------------------------------------------------


def test_assess_tinkerpop_delta_always_emits_five() -> None:
    """_assess_tinkerpop_delta always emits exactly 5 advisory findings."""
    assessor = _make_assessor()
    findings = assessor._assess_tinkerpop_delta()
    assert len(findings) == 5
    rule_ids = {f.rule_id for f in findings}
    assert rule_ids == {"TP-001", "TP-002", "TP-003", "TP-004", "TP-005"}


# ---------------------------------------------------------------------------
# Risk computation
# ---------------------------------------------------------------------------


def test_compute_risk_critical_on_se_red() -> None:
    """Risk is CRITICAL when a RED finding is in the security category."""
    findings = [
        AssessmentFinding(
            rule_id="SE-001",
            category="security",
            severity="RED",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
    ]
    assert Assessor._compute_risk(findings) == "CRITICAL"


def test_compute_risk_high_on_non_security_red() -> None:
    """Risk is HIGH when a RED finding exists outside security."""
    findings = [
        AssessmentFinding(
            rule_id="OL-001",
            category="olap",
            severity="RED",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
    ]
    assert Assessor._compute_risk(findings) == "HIGH"


def test_compute_risk_medium_on_olap_yellow() -> None:
    """Risk is MEDIUM when a YELLOW finding is in the OLAP category."""
    findings = [
        AssessmentFinding(
            rule_id="OL-X",
            category="olap",
            severity="YELLOW",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
    ]
    assert Assessor._compute_risk(findings) == "MEDIUM"


def test_compute_risk_medium_on_three_yellows() -> None:
    """Risk is MEDIUM when 3+ YELLOW findings exist."""
    findings = [
        AssessmentFinding(
            rule_id=f"QP-00{i}",
            category="query_pattern",
            severity="YELLOW",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
        for i in range(1, 4)
    ]
    assert Assessor._compute_risk(findings) == "MEDIUM"


def test_compute_risk_low_on_few_yellows() -> None:
    """Risk is LOW when fewer than 3 YELLOW findings and no RED."""
    findings = [
        AssessmentFinding(
            rule_id="QP-001",
            category="query_pattern",
            severity="YELLOW",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
    ]
    assert Assessor._compute_risk(findings) == "LOW"


def test_compute_risk_low_on_empty() -> None:
    """Risk is LOW with no findings."""
    assert Assessor._compute_risk([]) == "LOW"


def test_compute_risk_low_on_all_green() -> None:
    """Risk is LOW when all findings are GREEN."""
    findings = [
        AssessmentFinding(
            rule_id="HC-006",
            category="hcd_target",
            severity="GREEN",
            title="T",
            detail="D",
            remediation="R",
            docs_link="L",
        )
    ]
    assert Assessor._compute_risk(findings) == "LOW"


# ---------------------------------------------------------------------------
# Read-only assertion
# ---------------------------------------------------------------------------


def test_no_write_operations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Assessor must never issue write Gremlin queries to the mock connection."""
    submitted_scripts: list[str] = []

    def mock_submit(script: str, bindings: dict = None) -> list:  # type: ignore[assignment]
        submitted_scripts.append(script)
        return []

    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", side_effect=mock_submit):
        assessor.run()

    write_keywords = ("addV", "addE", "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE")
    for script in submitted_scripts:
        script_upper = script.upper()
        for kw in write_keywords:
            assert kw.upper() not in script_upper, f"assessor.py issued a write operation: {script}"


# ---------------------------------------------------------------------------
# HCD target pre-flight
# ---------------------------------------------------------------------------


def _hcd_clusters(anonymous_connect: object) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Patchable Cluster factory: the credentialed connection succeeds; the anonymous one does ``anonymous_connect``.

    Returns (factory, credentialed session, anonymous cluster).
    """
    session = MagicMock()
    session.execute.return_value = [MagicMock(value="false")]
    credentialed = MagicMock()
    credentialed.connect.return_value = session
    anonymous = MagicMock()
    if isinstance(anonymous_connect, BaseException):
        anonymous.connect.side_effect = anonymous_connect
    else:
        anonymous.connect.return_value = MagicMock()
    factory = MagicMock(side_effect=lambda *a, **kw: credentialed if "auth_provider" in kw else anonymous)
    return factory, session, anonymous


def _hcd_findings(anonymous_connect: object) -> tuple[list[AssessmentFinding], MagicMock, MagicMock]:
    factory, session, anonymous = _hcd_clusters(anonymous_connect)
    assessor = _make_assessor(target_cql_host="hcd", target_cql_port=9042)
    with patch("cassandra.cluster.Cluster", factory):
        findings = assessor._assess_hcd_target()
    return findings, session, anonymous


def test_hcd_target_rejecting_anonymous_emits_hc001_not_hc009() -> None:
    """Credentials accepted and anonymous rejected → HC-001 (authentication enforced)."""
    rejected = NoHostAvailable("no host", {"hcd:9042": AuthenticationFailed("Remote end requires authentication")})
    findings, _, anonymous = _hcd_findings(rejected)
    ids = [f.rule_id for f in findings]
    assert "HC-001" in ids and "HC-009" not in ids and "HC-008" not in ids
    assert "rejected an anonymous connection" in next(f.detail for f in findings if f.rule_id == "HC-001")
    anonymous.shutdown.assert_called_once()


def test_hcd_target_accepting_anonymous_emits_red_hc009() -> None:
    """An HCD that accepts a connection without credentials → HC-009 (RED), not HC-001."""
    findings, _, anonymous = _hcd_findings(None)
    ids = [f.rule_id for f in findings]
    assert "HC-009" in ids and "HC-001" not in ids
    assert next(f for f in findings if f.rule_id == "HC-009").severity == "RED"
    anonymous.shutdown.assert_called_once()


def test_hcd_target_inconclusive_anonymous_probe_emits_unverified_hc001() -> None:
    """An anonymous probe that fails for another reason does not claim enforcement either way."""
    findings, _, _ = _hcd_findings(NoHostAvailable("no host", {"hcd:9042": OSError("connection reset")}))
    detail = next(f.detail for f in findings if f.rule_id == "HC-001")
    assert "could not be verified" in detail
    assert "HC-009" not in [f.rule_id for f in findings]


def test_hcd_target_connection_failure_emits_hc008() -> None:
    """A failed credentialed connection → HC-008 only; no auth finding is claimed."""
    factory = MagicMock()
    factory.return_value.connect.side_effect = NoHostAvailable("no host", {"hcd:9042": OSError("refused")})
    assessor = _make_assessor(target_cql_host="hcd", target_cql_port=9042)
    with patch("cassandra.cluster.Cluster", factory):
        findings = assessor._assess_hcd_target()
    assert [f.rule_id for f in findings] == ["HC-008"]
    assert "hcd:9042" in findings[0].detail


def test_hcd_target_probe_is_read_only() -> None:
    """The HCD pre-flight issues reads only, and the anonymous probe issues no query at all (AGENTS.md §5.5)."""
    findings, session, anonymous = _hcd_findings(None)
    session.execute.assert_called()
    for call in session.execute.call_args_list:
        query = str(call.args[0]).upper()
        assert not any(kw in query for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE")), call
    anonymous.connect.return_value.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Full run (mocked)
# ---------------------------------------------------------------------------


def test_run_returns_assessment_result() -> None:
    """Assessor.run() returns an AssessmentResult with the expected structure."""
    assessor = _make_assessor()

    def mock_submit(script: str, bindings: dict = None) -> list:  # type: ignore[assignment]
        return []

    with patch.object(assessor, "_gremlin_submit", side_effect=mock_submit):
        result = assessor.run()

    assert isinstance(result, AssessmentResult)
    assert result.graph_name == "food"
    assert result.overall_risk in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert isinstance(result.findings, list)
    assert isinstance(result.summary, dict)
    assert "total_findings" in result.summary


# ---------------------------------------------------------------------------
# CF-006: an unreachable source (or a graph with no schema) must never look like a passable assessment
# ---------------------------------------------------------------------------


def test_unreachable_source_emits_cf_006_and_high_risk() -> None:
    """Found in a fresh-user test: with DSE down, assess reported MEDIUM risk from static advisories and passed."""
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", side_effect=ConnectionRefusedError("Connect call failed")):
        result = assessor.run()
    cf006 = [f for f in result.findings if f.rule_id == "CF-006"]
    assert (
        len(cf006) == 1
        and "ws://localhost:8183/gremlin" in cf006[0].detail
        and "Connect call failed" in cf006[0].detail
    )
    assert result.overall_risk in ("HIGH", "CRITICAL")


def test_empty_schema_emits_cf_006() -> None:
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", return_value=[]):
        result = assessor.run()
    assert any(f.rule_id == "CF-006" and "no schema" in f.detail for f in result.findings)


def test_readable_schema_emits_no_cf_006() -> None:
    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", return_value=["schema.vertexLabel('person').create()"]):
        result = assessor.run()
    assert not any(f.rule_id == "CF-006" for f in result.findings)


def test_olap_probe_does_not_log_the_expected_server_error() -> None:
    """The 'a' OLAP probe fails by design without OLAP; gremlin-python must not print that error (found live)."""
    import logging

    states: list[bool] = []

    def submit(script: str, bindings: object = None) -> list[int]:
        if script.startswith("a."):
            states.append(logging.getLogger("gremlinpython").disabled)
            raise RuntimeError("Variable 'a' is unknown")
        return []

    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", side_effect=submit):
        assessor._assess_olap()
    assert states == [True]
    assert logging.getLogger("gremlinpython").disabled is False


# ---------------------------------------------------------------------------
# Index Analysis and Cluster Overview (plan Step 0.3 §1 and §4)
# ---------------------------------------------------------------------------

_FIXTURES = __import__("pathlib").Path(__file__).parent.parent / "fixtures"


def _index_rows(fixture: str, engine: str = "dsg") -> dict[str, object]:
    assessor = _make_assessor()
    assessor._raw_schema = (_FIXTURES / fixture).read_text(encoding="utf-8")
    assessor._engine = engine
    return {(r.dse_index, r.label): r for r in assessor._index_analysis(assessor._parse_source_schema())}


def test_index_analysis_shows_which_indexes_need_the_search_backend() -> None:
    """Live finding: the report never said which DSE indexes need OpenSearch / Elasticsearch on JanusGraph."""
    rows = _index_rows("dse68_core_describe_food.groovy.txt")
    recipe = rows[("recipe_search", "recipe")]
    assert (recipe.jg_kind, recipe.backend, recipe.needs_search_backend) == (
        "mixed",
        "OpenSearch / Elasticsearch",
        True,
    )
    assert "instructions: `Search.token*()` → `textContains`" in recipe.queries  # asText: tokenized
    assert "cuisine: `Search.prefix/regex/fuzzy()` → `textPrefix`" in recipe.queries  # asString: whole value
    assert "recipe_id: `eq`, `neq`" in recipe.queries  # Uuid: no ranges
    assert "geo_point: `Geo.inside()` → `geoWithin" in rows[("location_search", "location")].queries
    person = rows[("person_by_name", "person")]
    assert (person.jg_kind, person.backend, person.needs_search_backend) == (
        "composite",
        "HCD (storage backend)",
        False,
    )
    assert rows[("knows_by_since", "knows")].jg_kind == "composite"
    geo = rows[("—", "location")]  # added by the translator for the Point property, no DSE counterpart
    assert (geo.dse_kind, geo.jg_index, geo.jg_kind) == ("Point property", "location_geo_geo_point", "mixed")


def test_index_analysis_classic_vertex_centric_and_secondary() -> None:
    rows = _index_rows("dse69_classic_describe.groovy.txt", engine="classic")
    assert rows[("knowsBySince", "knows")].jg_kind == "vertex-centric"
    assert rows[("byName", "person")].jg_kind == "composite"
    assert rows[("search", "person")].needs_search_backend


def test_index_analysis_flags_a_text_key_indexed_both_ways() -> None:
    """DSE indexes a Text key with neither asText() nor asString() both ways; JanusGraph defaults to TEXT only."""
    assessor = _make_assessor()
    assessor._raw_schema = (
        "schema.vertexLabel('recipe').ifNotExists().partitionBy('recipe_id', Uuid).property('name', Text).create()\n"
        "schema.vertexLabel('recipe').searchIndex().ifNotExists().by('name').create()\n"
    )
    (row,) = assessor._index_analysis(assessor._parse_source_schema())
    assert "Mapping.TEXTSTRING" in row.queries


def test_unparsable_schema_gives_no_index_rows_instead_of_raising() -> None:
    assessor = _make_assessor()
    assessor._raw_schema = "schema.vertexLabel('x')"
    with patch("dse_jg_migrator.assessor.parse_schema", side_effect=ValueError("bad DSL")):
        analysis = assessor._parse_source_schema()
    assert assessor._index_analysis(analysis) == []


def test_run_fills_overview_from_the_parsed_schema_and_probes() -> None:
    """Live findings: schema_mode was always "Unknown" (a regex used as plain text) and olap_enabled always False."""
    schema = (_FIXTURES / "dse68_core_describe_food.groovy.txt").read_text(encoding="utf-8")

    def submit(script: str, bindings: dict | None = None) -> list:  # type: ignore[type-arg]
        if script == "schema.describe()":
            return [schema]
        if script == "system.graph(name).describe()":
            return ["system.graph('food').ifNotExists().withReplication(...).engine(Core).create() Production"]
        if script.startswith("a.V()"):
            return [0]  # OLAP answers
        return []

    assessor = _make_assessor()
    with patch.object(assessor, "_gremlin_submit", side_effect=submit):
        result = assessor.run()
    o = result.cluster_overview
    assert (o["vertex_label_count"], o["edge_label_count"]) == (3, 3)
    assert o["indexes_by_kind"] == {"searchIndex": 2, "materializedView": 2}
    assert o["mixed_index_count"] == 3
    assert (o["schema_mode"], o["olap_enabled"]) == ("Production", True)
    assert {r.dse_index for r in result.index_analysis} >= {"recipe_search", "person_by_name"}
