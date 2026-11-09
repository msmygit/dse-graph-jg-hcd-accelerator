"""Unit tests for models.py."""

from __future__ import annotations

from dse_jg_migrator.models import (
    AnalysisResult,
    AssessmentResult,
    GraphConfig,
    JGCompositeIndex,
    JGEdgeLabel,
    JGIndexKey,
    JGPropertyKey,
    JGVertexLabel,
    PartitionKey,
    PropertyDefinition,
    SeederResult,
    TranslationResult,
    UDTDefinition,
    UDTField,
    VertexLabelDefinition,
)

# ---------------------------------------------------------------------------
# AnalysisResult
# ---------------------------------------------------------------------------


def test_analysis_result_to_dict() -> None:
    an = AnalysisResult(graph_name="food", engine="dsg", extracted_at="2026-01-01T00:00:00Z")
    vl = VertexLabelDefinition(label="person")
    vl.partition_keys = [PartitionKey(name="person_id", type="Uuid")]
    an.vertex_labels.append(vl)

    d = an.to_dict()
    assert d["graph_name"] == "food"
    assert d["engine"] == "dsg"
    assert len(d["vertex_labels"]) == 1
    assert d["vertex_labels"][0]["label"] == "person"
    assert d["vertex_labels"][0]["partition_keys"][0]["name"] == "person_id"


def test_analysis_result_defaults() -> None:
    an = AnalysisResult(graph_name="x", engine="classic", extracted_at="t")
    assert an.vertex_labels == []
    assert an.edge_labels == []
    assert an.udts == []
    assert an.indexes == []
    assert isinstance(an.config, GraphConfig)


# ---------------------------------------------------------------------------
# AssessmentResult
# ---------------------------------------------------------------------------


def test_assessment_result_defaults() -> None:
    ar = AssessmentResult(graph_name="g", engine="dsg", assessed_at="t")
    assert ar.findings == []
    assert ar.overall_risk == "LOW"
    assert ar.summary == {}
    assert ar.cluster_overview == {}


# ---------------------------------------------------------------------------
# TranslationResult
# ---------------------------------------------------------------------------


def test_translation_result_to_schema_dict() -> None:
    tr = TranslationResult()
    tr.propertyKeys = [JGPropertyKey(key="name", className="java.lang.String")]
    tr.vertexLabels = [JGVertexLabel(label="person")]
    tr.edgeLabels = [JGEdgeLabel(label="knows")]
    tr.compositeIndexes = [
        JGCompositeIndex(
            name="person_by_name",
            typeClass="org.apache.tinkerpop.gremlin.structure.Vertex",
            indexOnly="person",
            keys=[JGIndexKey(propertyKey="name")],
        )
    ]

    d = tr.to_schema_dict()
    assert "propertyKeys" in d
    assert "vertexLabels" in d
    assert "edgeLabels" in d
    assert "compositeIndexes" in d
    assert "mixedIndexes" in d
    assert "vertexCentricEdgeIndexes" in d
    assert "vertexCentricPropertyIndexes" in d
    assert d["propertyKeys"][0]["key"] == "name"
    assert d["vertexLabels"][0]["label"] == "person"
    assert d["compositeIndexes"][0]["name"] == "person_by_name"


def test_translation_result_no_translation_notes_in_schema_dict() -> None:
    """translation_notes must NOT appear in the JanusGraph-consumable schema dict."""
    tr = TranslationResult()
    tr.translation_notes = ["Some note"]
    d = tr.to_schema_dict()
    assert "translation_notes" not in d


# ---------------------------------------------------------------------------
# SeederResult
# ---------------------------------------------------------------------------


def test_seeder_result_fields() -> None:
    sr = SeederResult(
        graph_name="food",
        template="food",
        vertex_count=1000,
        edge_count=5000,
        next_command="dse-jg-migrator assess --graph-name food",
    )
    assert sr.graph_name == "food"
    assert sr.vertex_count == 1000
    assert sr.edge_count == 5000
    assert "assess" in sr.next_command


# ---------------------------------------------------------------------------
# Property cardinality defaults
# ---------------------------------------------------------------------------


def test_property_definition_default_cardinality() -> None:
    p = PropertyDefinition(name="x", type="Text")
    assert p.cardinality == "SINGLE"
    assert p.collection is None


def test_udt_definition_fields() -> None:
    udt = UDTDefinition(
        name="address",
        fields=[
            UDTField(name="street", type="Text"),
            UDTField(name="zip", type="Text"),
        ],
    )
    assert len(udt.fields) == 2
    assert udt.fields[0].name == "street"


def test_analysis_result_round_trips_through_json() -> None:
    """from_dict must invert to_dict — a partial reload dropped clustering keys (found live: iot `reading.ts`)."""
    import json

    from dse_jg_migrator.models import (
        AnalysisResult,
        ClusteringKey,
        EdgeLabelDefinition,
        GraphConfig,
        IndexDefinition,
        IndexKey,
        PartitionKey,
        PropertyDefinition,
        UDTDefinition,
        UDTField,
        VertexLabelDefinition,
    )

    an = AnalysisResult(
        graph_name="iot",
        engine="dsg",
        extracted_at="2026-11-09T00:00:00Z",
        vertex_labels=[
            VertexLabelDefinition(
                label="reading",
                partition_keys=[PartitionKey(name="device_id", type="Uuid")],
                clustering_keys=[ClusteringKey(name="ts", type="Timestamp", order="Desc")],
                properties=[PropertyDefinition(name="tags", type="Text", cardinality="SET", collection="set")],
                count=100,
            )
        ],
        edge_labels=[
            EdgeLabelDefinition(
                label="produced",
                from_label="device",
                to_label="reading",
                properties=[PropertyDefinition(name="at", type="Timestamp")],
                count=100,
                table_name="device__produced__reading",
            )
        ],
        udts=[UDTDefinition(name="addr", fields=[UDTField(name="zip", type="Int")])],
        indexes=[
            IndexDefinition(
                kind="vertexCentric",
                name="by_at",
                element="edge",
                label="produced",
                keys=[IndexKey(name="at")],
                direction="OUT",
                order="desc",
                from_label="device",
                to_label="reading",
            )
        ],
        config=GraphConfig(schema_mode="Production", olap_enabled=True),
    )
    assert AnalysisResult.from_dict(json.loads(json.dumps(an.to_dict()))) == an
