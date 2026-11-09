"""Unit tests for translator.py."""

from __future__ import annotations

import json
import os

from dse_jg_migrator.models import (
    AnalysisResult,
    EdgeLabelDefinition,
    IndexDefinition,
    IndexKey,
    PartitionKey,
    PropertyDefinition,
    UDTDefinition,
    UDTField,
    VertexLabelDefinition,
)
from dse_jg_migrator.translator import Translator, map_type

# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------


def test_map_type_text() -> None:
    assert map_type("Text") == "java.lang.String"


def test_map_type_uuid() -> None:
    assert map_type("Uuid") == "java.util.UUID"


def test_map_type_bigint() -> None:
    assert map_type("Bigint") == "java.lang.Long"


def test_map_type_point() -> None:
    assert map_type("Point") == "org.janusgraph.core.attribute.Geoshape"


def test_map_type_never_returns_types_janusgraph_rejects() -> None:
    """JanusGraph rejects LocalDate/LocalTime/BigDecimal/BigInteger/InetAddress (verified on a live server)."""
    from dse_jg_migrator.translator import TYPE_MAP

    rejected = {
        "java.time.LocalDate",
        "java.time.LocalTime",
        "java.math.BigDecimal",
        "java.math.BigInteger",
        "java.net.InetAddress",
    }
    assert not rejected & set(TYPE_MAP.values())
    assert map_type("Date") == "java.util.Date"
    assert map_type("Decimal") == "java.lang.Double"
    assert map_type("Varint") == "java.lang.String"


def test_lossy_type_mapping_is_noted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="x")
    vl.properties = [PropertyDefinition(name="price", type="Decimal"), PropertyDefinition(name="day", type="Date")]
    an.vertex_labels.append(vl)
    result = Translator(analysis=an, output_dir=str(tmp_path)).run()
    notes = "\n".join(result.translation_notes)
    assert "'price' (Decimal)" in notes
    assert "'day' (Date)" in notes


# https://docs.janusgraph.org/schema/#property-key-data-type ("Native JanusGraph Data Types"), plus primitive
# arrays per docs/advanced-topics/serializer.md. java.time.Duration/Instant are registered internally only.
DOCUMENTED_JG_TYPES = {
    "java.lang.String",
    "java.lang.Character",
    "java.lang.Boolean",
    "java.lang.Byte",
    "java.lang.Short",
    "java.lang.Integer",
    "java.lang.Long",
    "java.lang.Float",
    "java.lang.Double",
    "java.util.Date",
    "org.janusgraph.core.attribute.Geoshape",
    "java.util.UUID",
    "byte[]",
}


def test_type_map_targets_are_documented_janusgraph_types() -> None:
    from dse_jg_migrator.translator import DECIMAL_STRATEGIES, ENGINE_TYPE_OVERRIDES, TYPE_MAP

    targets = set(TYPE_MAP.values()) | set(DECIMAL_STRATEGIES.values())
    targets |= {cls for overrides in ENGINE_TYPE_OVERRIDES.values() for cls in overrides.values()}
    assert targets <= DOCUMENTED_JG_TYPES, targets - DOCUMENTED_JG_TYPES


def test_duration_resolves_per_engine() -> None:
    """Classic stores Duration as millis (Long); Core stores CQL duration(months, days, nanos) (ISO String)."""
    from dse_jg_migrator.translator import resolve_target_class

    assert resolve_target_class("Duration", engine="classic") == "java.lang.Long"
    assert resolve_target_class("Duration", engine="dsg") == "java.lang.String"
    assert resolve_target_class("Text", engine="dsg") == "java.lang.String"


def test_decimal_resolves_per_strategy() -> None:
    from dse_jg_migrator.translator import resolve_target_class

    assert resolve_target_class("Decimal", decimal_strategy="double") == "java.lang.Double"
    assert resolve_target_class("Decimal", decimal_strategy="string") == "java.lang.String"


def test_decimal_string_strategy_and_duration_notes_in_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="x")
    vl.properties = [PropertyDefinition(name="price", type="Decimal"), PropertyDefinition(name="ttl", type="Duration")]
    an.vertex_labels.append(vl)
    result = Translator(analysis=an, output_dir=str(tmp_path), decimal_strategy="string").run()
    classes = {pk.key: pk.className for pk in result.propertyKeys}
    assert classes == {"price": "java.lang.String", "ttl": "java.lang.String"}
    notes = "\n".join(result.translation_notes)
    assert "exact decimal" in notes and "ISO-8601" in notes
    with open(os.path.join(str(tmp_path), "t-translation-report.md"), encoding="utf-8") as fh:
        assert "**Decimal strategy:** string" in fh.read()


def test_map_type_unknown_defaults_to_string() -> None:
    assert map_type("SomeFutureType") == "java.lang.String"


def test_map_type_all_types_return_string() -> None:
    """All entries in TYPE_MAP must return non-empty strings."""
    from dse_jg_migrator.translator import TYPE_MAP

    for dse_type, jg_type in TYPE_MAP.items():
        assert jg_type, f"Empty mapping for {dse_type}"


# ---------------------------------------------------------------------------
# Simple vertex+edge schema translation
# ---------------------------------------------------------------------------


def _make_analysis() -> AnalysisResult:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    vl.partition_keys = [PartitionKey(name="person_id", type="Uuid")]
    vl.properties = [
        PropertyDefinition(name="name", type="Text"),
        PropertyDefinition(name="age", type="Int"),
        PropertyDefinition(name="nickname", type="Text", cardinality="SET", collection="set"),
    ]
    an.vertex_labels.append(vl)

    vl2 = VertexLabelDefinition(label="recipe")
    vl2.partition_keys = [PartitionKey(name="recipe_id", type="Uuid")]
    vl2.properties = [
        PropertyDefinition(name="name", type="Text"),
        PropertyDefinition(name="instructions", type="Text"),
    ]
    an.vertex_labels.append(vl2)

    el = EdgeLabelDefinition(label="knows", from_label="person", to_label="person")
    el.properties = [PropertyDefinition(name="since", type="Date")]
    an.edge_labels.append(el)

    return an


def test_vertex_labels_translated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    label_names = [vl.label for vl in result.vertexLabels]
    assert "person" in label_names
    assert "recipe" in label_names


def test_edge_labels_translated(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    assert any(el.label == "knows" for el in result.edgeLabels)


def test_property_keys_include_partition_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    key_names = [pk.key for pk in result.propertyKeys]
    assert "person_id" in key_names


def test_property_keys_no_duplicates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    keys = [pk.key for pk in result.propertyKeys]
    assert len(keys) == len(set(keys)), "Duplicate property keys in translation result"


def test_set_property_has_set_cardinality(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    nickname = next(pk for pk in result.propertyKeys if pk.key == "nickname")
    assert nickname.cardinality == "SET"


# ---------------------------------------------------------------------------
# mapOf translation
# ---------------------------------------------------------------------------


def test_map_strategy_flat_creates_two_list_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    vl.properties = [PropertyDefinition(name="badge", type="mapOf(Text, Date)", cardinality="LIST", collection="map")]
    an.vertex_labels.append(vl)
    tr = Translator(analysis=an, map_strategy="flat", output_dir=str(tmp_path))
    result = tr.run()
    key_names = [pk.key for pk in result.propertyKeys]
    assert "badge_key" in key_names
    assert "badge_value" in key_names


def test_map_strategy_json_creates_single_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    vl.properties = [PropertyDefinition(name="badge", type="mapOf(Text, Date)", cardinality="LIST", collection="map")]
    an.vertex_labels.append(vl)
    tr = Translator(analysis=an, map_strategy="json", output_dir=str(tmp_path))
    result = tr.run()
    key_names = [pk.key for pk in result.propertyKeys]
    assert "badge" in key_names
    assert "badge_key" not in key_names


# ---------------------------------------------------------------------------
# UDT translation
# ---------------------------------------------------------------------------


def test_udt_flat_strategy_expands_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    udt = UDTDefinition(
        name="address",
        fields=[
            UDTField(name="street", type="Text"),
            UDTField(name="zip", type="Text"),
        ],
    )
    an.udts.append(udt)
    tr = Translator(analysis=an, udt_strategy="flat", output_dir=str(tmp_path))
    result = tr.run()
    key_names = [pk.key for pk in result.propertyKeys]
    assert "address_street" in key_names
    assert "address_zip" in key_names


def test_udt_json_strategy_single_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    udt = UDTDefinition(
        name="address",
        fields=[
            UDTField(name="street", type="Text"),
        ],
    )
    an.udts.append(udt)
    tr = Translator(analysis=an, udt_strategy="json", output_dir=str(tmp_path))
    result = tr.run()
    key_names = [pk.key for pk in result.propertyKeys]
    assert "address" in key_names
    assert "address_street" not in key_names


# ---------------------------------------------------------------------------
# Geo index auto-generation
# ---------------------------------------------------------------------------


def test_point_property_generates_geo_mixed_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="location")
    vl.properties = [PropertyDefinition(name="geo_point", type="Point")]
    an.vertex_labels.append(vl)
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    geo_indexes = [mi for mi in result.mixedIndexes if any(k.propertyKey == "geo_point" for k in mi.keys)]
    assert len(geo_indexes) == 1
    params = geo_indexes[0].keys[0].parameters
    assert any("PREFIX_TREE" in p.get("value", "") for p in params)


def test_polygon_property_does_not_generate_geo_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="route")
    vl.properties = [PropertyDefinition(name="path", type="Polygon")]
    an.vertex_labels.append(vl)
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    geo_indexes = [mi for mi in result.mixedIndexes if any(k.propertyKey == "path" for k in mi.keys)]
    assert len(geo_indexes) == 0


# ---------------------------------------------------------------------------
# Index translation
# ---------------------------------------------------------------------------


def test_mv_translates_to_composite_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    idx = IndexDefinition(kind="materializedView", name="person_by_name", element="vertex", label="person")
    idx.keys = [IndexKey(name="name")]
    an.indexes.append(idx)
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    assert any(ci.name == "person_by_name" for ci in result.compositeIndexes)


def test_search_index_translates_to_mixed_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    idx = IndexDefinition(kind="searchIndex", name="recipe_search", element="vertex", label="recipe")
    idx.keys = [
        IndexKey(name="instructions", as_mapping="asText"),
        IndexKey(name="cuisine", as_mapping="asString"),
    ]
    an.indexes.append(idx)
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    result = tr.run()
    mixed = next(mi for mi in result.mixedIndexes if mi.name == "recipe_search")
    assert len(mixed.keys) == 2
    text_key = next(k for k in mixed.keys if k.propertyKey == "instructions")
    assert any("TEXT" in p["value"] for p in text_key.parameters)


def _edge_mv_analysis(mv_keys: list[str]) -> AnalysisResult:
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    el = EdgeLabelDefinition(label="knows", from_label="person", to_label="person")
    el.properties = [PropertyDefinition(name="since", type="Date")]
    an.edge_labels.append(el)
    idx = IndexDefinition(kind="materializedView", name="knows_by_since", element="edge", label="knows")
    idx.keys = [IndexKey(name=k) for k in mv_keys]
    an.indexes.append(idx)
    return an


def test_edge_mv_keeps_only_edge_property_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Regression: the edge MV's endpoint column (person_id) is a vertex property, not an edge key."""
    result = Translator(analysis=_edge_mv_analysis(["person_id", "since"]), output_dir=str(tmp_path)).run()
    ci = next(c for c in result.compositeIndexes if c.name == "knows_by_since")
    assert ci.typeClass.endswith(".Edge")
    assert [k.propertyKey for k in ci.keys] == ["since"]


def test_edge_mv_without_edge_property_keys_creates_no_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = Translator(analysis=_edge_mv_analysis(["person_id"]), output_dir=str(tmp_path)).run()
    assert not result.compositeIndexes
    assert any("knows_by_since" in n for n in result.translation_notes)


def _inverse_mv_analysis(keys: list[str]) -> AnalysisResult:
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    el = EdgeLabelDefinition(label="follows", from_label="user", to_label="user")
    el.properties = [PropertyDefinition(name="followed_at", type="Timestamp")]
    an.edge_labels.append(el)
    an.indexes.append(
        IndexDefinition(
            kind="materializedView",
            name="follows_inverse",
            element="edge",
            label="follows",
            inverse=True,
            keys=[IndexKey(name=k) for k in keys],
        )
    )
    return an


def test_keyless_inverse_mv_creates_no_index(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: JanusGraph rejects a vertex-centric index without sort keys ("Need to specify sort keys");
    IN-direction lookups are native via adjacency lists."""
    result = Translator(analysis=_inverse_mv_analysis([]), output_dir=str(tmp_path)).run()
    assert not result.vertexCentricEdgeIndexes
    assert any("follows_inverse" in n and "adjacency lists" in n for n in result.translation_notes)


def test_inverse_mv_with_edge_property_key_translates_to_vertex_centric_in(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = Translator(analysis=_inverse_mv_analysis(["user_id", "followed_at"]), output_dir=str(tmp_path)).run()
    vc = next(vc for vc in result.vertexCentricEdgeIndexes if vc.name == "follows_inverse")
    assert vc.direction == "IN"
    assert vc.propertyKeys == ["followed_at"]  # the endpoint vertex key is dropped


# ---------------------------------------------------------------------------
# JSON schema output
# ---------------------------------------------------------------------------


def test_schema_json_written(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    tr.run()
    schema_file = os.path.join(str(tmp_path), "food-janusgraph-schema.json")
    assert os.path.exists(schema_file)
    with open(schema_file, encoding="utf-8") as fh:
        data = json.load(fh)
    assert "propertyKeys" in data
    assert "vertexLabels" in data
    assert "edgeLabels" in data
    assert "compositeIndexes" in data
    assert "mixedIndexes" in data
    assert "vertexCentricEdgeIndexes" in data


def test_translation_report_written(tmp_path) -> None:  # type: ignore[no-untyped-def]
    an = _make_analysis()
    tr = Translator(analysis=an, output_dir=str(tmp_path))
    tr.run()
    report_file = os.path.join(str(tmp_path), "food-translation-report.md")
    assert os.path.exists(report_file)
    content = open(report_file, encoding="utf-8").read()
    assert "## Vertex Labels" in content
    assert "person" in content


def _udt_tuple_analysis() -> AnalysisResult:
    an = AnalysisResult(graph_name="t", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="employee")
    vl.properties = [
        PropertyDefinition(name="home_address", type="frozen(typeOf('address'))"),
        PropertyDefinition(name="salary_range", type="tupleOf(Int, Int)"),
    ]
    an.vertex_labels.append(vl)
    an.udts.append(UDTDefinition(name="address", fields=[UDTField(name="street", type="Text")]))
    return an


def test_udt_property_writes_to_udt_keys_not_a_fallback_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: an unused String key `home_address` was created next to `address_*`; its data was lost."""
    keys = {pk.key for pk in Translator(analysis=_udt_tuple_analysis(), output_dir=str(tmp_path)).run().propertyKeys}
    assert "home_address" not in keys and "address_street" in keys
    json_keys = {
        pk.key
        for pk in Translator(analysis=_udt_tuple_analysis(), output_dir=str(tmp_path), udt_strategy="json")
        .run()
        .propertyKeys
    }
    assert "address" in json_keys and "home_address" not in json_keys


def test_tuple_property_is_a_json_string_with_note(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = Translator(analysis=_udt_tuple_analysis(), output_dir=str(tmp_path)).run()
    assert {pk.key: pk.className for pk in result.propertyKeys}["salary_range"] == "java.lang.String"
    assert any("salary_range" in n and "JSON array" in n for n in result.translation_notes)
