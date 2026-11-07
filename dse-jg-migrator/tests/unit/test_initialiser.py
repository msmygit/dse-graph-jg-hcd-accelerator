"""Unit tests for initialiser.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from dse_jg_migrator.initialiser import Initialiser


def _make_schema() -> dict:
    return {
        "propertyKeys": [{"key": "name", "className": "java.lang.String", "cardinality": "SINGLE"}],
        "vertexLabels": [{"label": "person", "staticVertex": False}],
        "edgeLabels": [{"label": "knows", "multiplicity": "MULTI"}],
        "compositeIndexes": [
            {
                "name": "person_by_name",
                "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
                "indexOnly": "person",
                "keys": [{"propertyKey": "name"}],
            }
        ],
        "mixedIndexes": [],
        "vertexCentricEdgeIndexes": [],
        "vertexCentricPropertyIndexes": [],
    }


def test_initialiser_returns_ok_on_success(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    schema = _make_schema()
    schema_path = str(tmp_path / "schema.json")
    with open(schema_path, "w") as fh:
        json.dump(schema, fh)

    init = Initialiser(
        target_gremlin_url="ws://localhost:8182/gremlin",
        schema_json_path=schema_path,
    )

    mock_submit = MagicMock(return_value=["ENABLED"])
    with (
        patch.object(init, "_submit", side_effect=mock_submit),
        patch("dse_jg_migrator.initialiser.time.sleep"),
    ):
        result = init.run()

    assert result["status"] in ("ok", "partial")
    assert "created" in result


def test_initialiser_records_errors_on_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    schema = _make_schema()
    schema_path = str(tmp_path / "schema.json")
    with open(schema_path, "w") as fh:
        json.dump(schema, fh)

    init = Initialiser(
        target_gremlin_url="ws://localhost:8182/gremlin",
        schema_json_path=schema_path,
    )

    def _fail(script: str, bindings: dict = None) -> list:
        raise RuntimeError("schema error")

    with (
        patch.object(init, "_submit", side_effect=_fail),
        patch("dse_jg_migrator.initialiser.time.sleep"),
    ):
        result = init.run()

    assert result["status"] == "partial"
    assert len(result["errors"]) > 0


def test_server_restart_mode_skips_gremlin(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    schema = _make_schema()
    schema_path = str(tmp_path / "schema.json")
    with open(schema_path, "w") as fh:
        json.dump(schema, fh)

    init = Initialiser(
        target_gremlin_url="ws://localhost:8182/gremlin",
        schema_json_path=schema_path,
        mode="server-restart",
    )
    result = init.run()
    assert result["status"] == "skipped"
