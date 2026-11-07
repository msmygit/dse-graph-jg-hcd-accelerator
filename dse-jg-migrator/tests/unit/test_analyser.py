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
    schema_with_udt = (
        "schema.type('address').ifNotExists()"
        ".property('street', Text).property('zip', Text).create()"
    )
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

    with patch.object(analyser, "_gremlin_submit", side_effect=mock_submit):
        analyser.run()

    out_file = os.path.join(str(tmp_path), "food-analysis.json")
    assert os.path.exists(out_file)
    with open(out_file, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["graph_name"] == "food"
    assert len(data["vertex_labels"]) == 2
    assert len(data["edge_labels"]) == 2
