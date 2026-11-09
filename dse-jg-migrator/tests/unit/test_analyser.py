"""Unit tests for analyser.py."""

from __future__ import annotations

from unittest.mock import patch

from dse_jg_migrator.analyser import Analyser

FOOD_SCHEMA_DSL = """
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
  .create()

schema.edgeLabel('created').ifNotExists()
  .from('person').to('recipe').property('created_on', Date).create()

schema.edgeLabel('knows').ifNotExists()
  .from('person').to('person').property('since', Date).create()

schema.vertexLabel('person').materializedView('person_by_name').ifNotExists()
  .partitionBy('name').clusterBy('person_id', Asc).create()

schema.vertexLabel('recipe').searchIndex().ifNotExists()
  .by('instructions').asText().by('cuisine').asString().create()
"""


def _make_analyser(**kwargs) -> Analyser:  # type: ignore[no-untyped-def]
    defaults = {
        "gremlin_url": "ws://localhost:8183/gremlin",
        "cql_host": "localhost",
        "cql_port": 9043,
        "graph_name": "food",
    }
    defaults.update(kwargs)
    return Analyser(**defaults)


# ---------------------------------------------------------------------------
# Engine detection
# ---------------------------------------------------------------------------


def test_detect_engine_dsg() -> None:
    analyser = _make_analyser()
    with patch.object(analyser, "_gremlin_submit", return_value=["engine:Core"]):
        assert analyser._detect_engine() == "dsg"


def test_detect_engine_classic_on_error() -> None:
    analyser = _make_analyser()
    with patch.object(analyser, "_gremlin_submit", side_effect=Exception("err")):
        assert analyser._detect_engine() == "dsg"  # fallback to dsg


# ---------------------------------------------------------------------------
# Vertex label parsing
# ---------------------------------------------------------------------------


def test_parse_vertex_labels_count() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_vertex_labels(FOOD_SCHEMA_DSL)
    assert len(labels) == 2
    label_names = [l.label for l in labels]
    assert "person" in label_names
    assert "recipe" in label_names


def test_parse_vertex_label_partition_key() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_vertex_labels(FOOD_SCHEMA_DSL)
    person = next(l for l in labels if l.label == "person")
    assert len(person.partition_keys) == 1
    assert person.partition_keys[0].name == "person_id"
    assert person.partition_keys[0].type == "Uuid"


def test_parse_vertex_label_properties() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_vertex_labels(FOOD_SCHEMA_DSL)
    person = next(l for l in labels if l.label == "person")
    prop_names = [p.name for p in person.properties]
    assert "name" in prop_names
    assert "nickname" in prop_names


def test_parse_vertex_label_set_cardinality() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_vertex_labels(FOOD_SCHEMA_DSL)
    person = next(l for l in labels if l.label == "person")
    nickname = next(p for p in person.properties if p.name == "nickname")
    assert nickname.cardinality == "SET"
    assert nickname.collection == "set"


def test_parse_vertex_label_map_collection() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_vertex_labels(FOOD_SCHEMA_DSL)
    person = next(l for l in labels if l.label == "person")
    badge = next(p for p in person.properties if p.name == "badge")
    assert badge.collection == "map"


# ---------------------------------------------------------------------------
# Edge label parsing
# ---------------------------------------------------------------------------


def test_parse_edge_labels_count() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_edge_labels(FOOD_SCHEMA_DSL)
    assert len(labels) == 2


def test_parse_edge_label_from_to() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_edge_labels(FOOD_SCHEMA_DSL)
    created = next(l for l in labels if l.label == "created")
    assert created.from_label == "person"
    assert created.to_label == "recipe"


def test_parse_edge_label_properties() -> None:
    analyser = _make_analyser()
    labels = analyser._parse_edge_labels(FOOD_SCHEMA_DSL)
    created = next(l for l in labels if l.label == "created")
    assert any(p.name == "created_on" for p in created.properties)


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


def test_parse_indexes_mv() -> None:
    analyser = _make_analyser()
    indexes = analyser._parse_indexes(FOOD_SCHEMA_DSL)
    mv_indexes = [i for i in indexes if i.kind == "materializedView"]
    assert len(mv_indexes) == 1
    assert mv_indexes[0].name == "person_by_name"


def test_parse_indexes_search() -> None:
    analyser = _make_analyser()
    indexes = analyser._parse_indexes(FOOD_SCHEMA_DSL)
    search_indexes = [i for i in indexes if i.kind == "searchIndex"]
    assert len(search_indexes) == 1
    assert "recipe" in search_indexes[0].label


def test_parse_indexes_search_keys() -> None:
    analyser = _make_analyser()
    indexes = analyser._parse_indexes(FOOD_SCHEMA_DSL)
    search = next(i for i in indexes if i.kind == "searchIndex")
    key_names = [k.name for k in search.keys]
    assert "instructions" in key_names
    assert "cuisine" in key_names


# ---------------------------------------------------------------------------
# UDT parsing
# ---------------------------------------------------------------------------


def test_parse_udts_empty_when_no_udts() -> None:
    analyser = _make_analyser()
    udts = analyser._parse_udts(FOOD_SCHEMA_DSL)
    assert udts == []


def test_parse_udts_with_udt() -> None:
    analyser = _make_analyser()
    schema_with_udt = "schema.type('address').ifNotExists().property('street', Text).property('zip', Text).create()"
    udts = analyser._parse_udts(schema_with_udt)
    assert len(udts) == 1
    assert udts[0].name == "address"
    field_names = [f.name for f in udts[0].fields]
    assert "street" in field_names
    assert "zip" in field_names


# ---------------------------------------------------------------------------
# Full run (mocked)
# ---------------------------------------------------------------------------


def test_run_produces_json_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    import os

    analyser = _make_analyser(output_dir=str(tmp_path))

    def mock_submit(script: str, bindings: dict = None) -> list:  # type: ignore[assignment]
        if "schema.describe" in script:
            return [FOOD_SCHEMA_DSL]
        return []

    # CQL count enrichment is mocked too: unmocked, it reached a live DSE on localhost:9043 (AGENTS.md §5.3).
    with (
        patch.object(analyser, "_gremlin_submit", side_effect=mock_submit),
        patch.object(analyser, "_enrich_counts_from_cql") as cql_counts,
    ):
        analyser.run()
    cql_counts.assert_called_once()

    out_file = os.path.join(str(tmp_path), "food-analysis.json")
    assert os.path.exists(out_file)
    with open(out_file, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["graph_name"] == "food"
    assert len(data["vertex_labels"]) == 2
    assert len(data["edge_labels"]) == 2


# ---------------------------------------------------------------------------
# Real DSE 6.8 Core `schema.describe()` output (captured from a live server; see
# tests/fixtures/dse68_core_describe_food.groovy.txt). Regression tests for parser bugs that the
# hand-written DSL above could not expose.
# ---------------------------------------------------------------------------


def _real_describe() -> str:
    from pathlib import Path

    return (Path(__file__).parent.parent / "fixtures" / "dse68_core_describe_food.groovy.txt").read_text(
        encoding="utf-8"
    )


def test_real_describe_edge_label_properties_not_absorbed_by_neighbours() -> None:
    edges = {e.label: e for e in _make_analyser()._parse_edge_labels(_real_describe())}
    assert set(edges) == {"created", "knows", "located_at"}  # edge-MV block must not create a 2nd 'knows'
    assert [p.name for p in edges["created"].properties] == ["created_on"]
    assert edges["located_at"].properties == []
    assert (edges["knows"].from_label, edges["knows"].to_label) == ("person", "person")


def test_real_describe_collection_types_parse_with_varchar() -> None:
    labels = {v.label: v for v in _make_analyser()._parse_vertex_labels(_real_describe())}
    person = {p.name: p for p in labels["person"].properties}
    assert (person["nickname"].type, person["nickname"].cardinality) == ("Varchar", "SET")
    badge = person["badge"]
    assert badge.collection == "map"
    assert (badge.map_key_type, badge.map_value_type) == ("Varchar", "Date")
    assert badge.type == "mapOf(Varchar, Date)"  # not truncated at the inner ')'


def test_real_describe_vertex_block_does_not_include_edge_properties() -> None:
    labels = {v.label: v for v in _make_analyser()._parse_vertex_labels(_real_describe())}
    assert "created_on" not in {p.name for p in labels["recipe"].properties}
    assert "since" not in {p.name for p in labels["person"].properties}


def test_real_describe_indexes_including_edge_materialized_view() -> None:
    idx = {i.name: i for i in _make_analyser()._parse_indexes(_real_describe())}
    assert set(idx) == {"person_by_name", "location_search", "recipe_search", "knows_by_since"}
    assert idx["knows_by_since"].element == "edge" and idx["knows_by_since"].label == "knows"
    # Endpoint columns (OUT/IN, 'person_id') are vertex keys; only the edge property clusterBy('since') is an index key.
    assert [k.name for k in idx["knows_by_since"].keys] == ["since"]
    assert [(k.name, k.as_mapping) for k in idx["recipe_search"].keys] == [
        ("recipe_id", None),
        ("cuisine", "asString"),
        ("instructions", "asText"),
    ]


def test_edge_table_name_matches_dse_dsg_convention() -> None:
    assert Analyser.edge_table_name("person", "knows", "person") == "person__knows__person"


def test_detect_engine_uses_classic_marker_only() -> None:
    analyser = _make_analyser()
    classic = 'system.graph("x").engine(Classic).create()'
    core = "system.graph('x').ifNotExists().withReplication(\"{'class':'NetworkTopologyStrategy'}\").create()"
    with patch.object(analyser, "_gremlin_submit", return_value=[classic]):
        assert analyser._detect_engine() == "classic"
    with patch.object(analyser, "_gremlin_submit", return_value=[core]):
        assert analyser._detect_engine() == "dsg"


def test_gremlin_client_uses_graph_traversal_source() -> None:
    import sys
    import types
    from unittest.mock import MagicMock

    client_cls = MagicMock()
    mod = types.ModuleType("gremlin_python.driver.client")
    mod.Client = client_cls  # type: ignore[attr-defined]
    drv = types.ModuleType("gremlin_python.driver")
    drv.client = mod  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {
            "gremlin_python": types.ModuleType("gremlin_python"),
            "gremlin_python.driver": drv,
            "gremlin_python.driver.client": mod,
        },
    ):
        _make_analyser()._gremlin_submit("schema.describe()")
    assert client_cls.call_args.args[1] == "food.g"


def test_enrich_counts_from_cql_sets_edge_table_names() -> None:
    from unittest.mock import MagicMock

    from dse_jg_migrator.models import AnalysisResult, EdgeLabelDefinition

    result = AnalysisResult(graph_name="food", engine="dsg", extracted_at="t")
    result.edge_labels = [EdgeLabelDefinition(label="knows", from_label="person", to_label="person")]
    cluster = MagicMock()
    cluster.connect.return_value.execute.return_value = iter([(7,)])
    with patch("cassandra.cluster.Cluster", return_value=cluster):
        _make_analyser()._enrich_counts_from_cql(result)
    assert result.edge_labels[0].table_name == "person__knows__person"
    assert result.edge_labels[0].count == 7


# ---------------------------------------------------------------------------
# Classic engine: real DSE 6.9 Classic `schema.describe()` output (tests/fixtures/dse69_classic_describe.groovy.txt)
# ---------------------------------------------------------------------------


def _classic_describe() -> str:
    from pathlib import Path

    return (Path(__file__).parent.parent / "fixtures" / "dse69_classic_describe.groovy.txt").read_text(encoding="utf-8")


def test_classic_describe_is_parsed() -> None:
    """Live finding: the Core-only parser returned 0 labels for a Classic graph, so nothing was migrated."""
    vertex_labels, edge_labels, indexes = Analyser._parse_classic(_classic_describe())
    (person,) = vertex_labels
    assert [(p.name, p.type, p.cardinality) for p in person.properties] == [
        ("name", "Text", "SINGLE"),
        ("nick", "Text", "LIST"),  # .multiple()
        ("joined", "Timestamp", "SINGLE"),
    ]
    (knows,) = edge_labels
    assert (knows.from_label, knows.to_label, [p.name for p in knows.properties]) == ("person", "person", ["since"])
    by_name = {i.name: i for i in indexes}
    assert by_name["search"].kind == "searchIndex" and by_name["search"].keys[0].as_mapping == "asString"
    assert by_name["byJoined"].kind == "materializedView" and by_name["byName"].kind == "secondaryIndex"
    vc = by_name["knowsBySince"]
    assert (vc.kind, vc.label, vc.direction, [k.name for k in vc.keys]) == ("vertexCentric", "knows", "OUT", ["since"])


def test_run_uses_the_classic_parser_for_classic_graphs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    analyser = _make_analyser(output_dir=str(tmp_path))

    def submit(script: str, bindings: dict = None) -> list:  # type: ignore[assignment]
        if "system.graph" in script:
            return ['system.graph("classic_demo").engine(Classic).create()']
        if "schema.describe" in script:
            return [_classic_describe()]
        return [10]  # counts

    with patch.object(analyser, "_gremlin_submit", side_effect=submit):
        result = analyser.run()
    assert result.engine == "classic"
    assert [vl.label for vl in result.vertex_labels] == ["person"] and result.vertex_labels[0].count == 10
