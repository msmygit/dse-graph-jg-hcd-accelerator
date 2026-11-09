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


# ---------------------------------------------------------------------------
# Appendix E.8 — index enablement wait
# ---------------------------------------------------------------------------


def _init(tmp_path, **kwargs) -> Initialiser:  # type: ignore[no-untyped-def]
    return Initialiser(
        target_gremlin_url="ws://localhost:8182/gremlin",
        schema_json_path=str(tmp_path / "schema.json"),
        **kwargs,
    )


def test_default_index_timeout_is_30_minutes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from dse_jg_migrator.initialiser import DEFAULT_INDEX_TIMEOUT_S

    assert DEFAULT_INDEX_TIMEOUT_S == 30 * 60
    assert _init(tmp_path).index_timeout_s == 1800


def test_run_passes_configured_timeout_to_wait(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    (tmp_path / "schema.json").write_text(json.dumps(_make_schema()), encoding="utf-8")
    init = _init(tmp_path, index_timeout_s=42)
    with (
        patch.object(init, "_submit", return_value=["ENABLED"]),
        patch.object(init, "_await_index_enablement") as wait,
    ):
        init.run()
    wait.assert_called_once()
    assert wait.call_args.args[1] == 42


def test_await_returns_immediately_when_no_indexes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    with patch.object(init, "_submit") as submit:
        init._await_index_enablement(["PropertyKey:name"])
    submit.assert_not_called()


def test_submit_passes_credentials_and_closes_client(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sys
    import types

    init = _init(tmp_path, username="u", password="p")  # noqa: S106
    fake = types.ModuleType("gremlin_python.driver.client")
    fake.Client = MagicMock()  # type: ignore[attr-defined]
    fake.Client.return_value.submit.return_value.all.return_value.result.return_value = ["ok"]
    drv = types.ModuleType("gremlin_python.driver")
    drv.client = fake  # type: ignore[attr-defined]
    with patch.dict(
        sys.modules,
        {
            "gremlin_python": types.ModuleType("gremlin_python"),
            "gremlin_python.driver": drv,
            "gremlin_python.driver.client": fake,
        },
    ):
        assert init._submit("x", {"a": 1}) == ["ok"]
    fake.Client.assert_called_once_with(init.target_gremlin_url, "g", username="u", password="p")  # noqa: S106
    fake.Client.return_value.close.assert_called_once()


# ---------------------------------------------------------------------------
# Live-found regressions (JanusGraph 1.x): single transaction, returned value, per-key status
# ---------------------------------------------------------------------------


def _rich_schema() -> dict:
    return {
        "propertyKeys": [
            {"key": "name", "className": "java.lang.String", "cardinality": "SINGLE"},
            {"key": "blob", "className": "byte[]", "cardinality": "SINGLE"},
            {"key": "since", "className": "java.util.Date", "cardinality": "SINGLE"},
        ],
        "vertexLabels": [{"label": "person"}],
        "edgeLabels": [{"label": "knows", "multiplicity": "MULTI"}],
        "compositeIndexes": [
            {
                "name": "knows_by_since",
                "typeClass": "org.apache.tinkerpop.gremlin.structure.Edge",
                "indexOnly": "knows",
                "unique": False,
                "keys": [{"propertyKey": "since", "parameters": []}],
            }
        ],
        "mixedIndexes": [
            {
                "name": "person_search",
                "typeClass": "org.apache.tinkerpop.gremlin.structure.Vertex",
                "indexOnly": "person",
                "indexBackend": "search",
                "keys": [
                    {
                        "propertyKey": "name",
                        "parameters": [
                            {"key": "mapping", "value": "org.janusgraph.core.schema.Mapping.TEXT", "parser": "enum"}
                        ],
                    }
                ],
            }
        ],
        "vertexCentricEdgeIndexes": [
            {
                "name": "knows_vc",
                "indexedEdgeLabel": "knows",
                "direction": "IN",
                "propertyKeys": ["since"],
                "order": "asc",
            }
        ],
    }


def test_build_bindings_shapes_the_schema_for_the_script() -> None:
    b = Initialiser.build_bindings(_rich_schema())
    assert {"key": "blob", "cls": "[B", "card": "SINGLE"} in b["pks"]  # byte[] -> Class.forName("[B")
    assert b["vls"] == ["person"]
    assert b["els"] == [{"label": "knows", "mult": "MULTI"}]
    by_name = {i["name"]: i for i in b["idxs"]}
    assert by_name["knows_by_since"]["edge"] is True
    assert by_name["knows_by_since"]["backend"] == ""
    assert by_name["person_search"]["edge"] is False
    assert by_name["person_search"]["backend"] == "search"
    assert by_name["person_search"]["keys"] == [{"name": "name", "mapping": "TEXT"}]
    assert b["vcis"][0]["dir"] == "IN"


def test_all_schema_is_created_in_one_transaction(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Keys and indexes created in different transactions leave the index INSTALLED forever (live finding)."""
    import json

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path)
    created = ["PropertyKey:name", "Index:person_search"]
    n_indexes = len(Initialiser.build_bindings(_rich_schema())["idxs"])
    with (
        patch.object(init, "_submit", side_effect=[created] + [["ENABLED"]] * n_indexes) as submit,
        patch("dse_jg_migrator.initialiser.time.sleep"),
    ):
        result = init.run()
    assert submit.call_count == 1 + n_indexes  # one schema script + one status poll per schema index
    script, bindings = submit.call_args_list[0].args
    assert script.count("openManagement()") == 1
    assert "m.commit()" in script and "m.rollback()" in script
    assert script.rstrip().endswith("}")  # never ends on a bare commit — must return a value
    assert "pks" in bindings and "idxs" in bindings
    assert result == {"status": "ok", "created": created, "errors": []}


def test_schema_failure_is_reported_not_raised(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path)
    with patch.object(init, "_submit", side_effect=RuntimeError("boom")):
        result = init.run()
    assert result["status"] == "partial"
    assert "ws://localhost:8182/gremlin" in result["errors"][0]
    assert "boom" in result["errors"][0]


def test_await_uses_per_key_status_script(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    with patch.object(init, "_submit", return_value=["ENABLED", "ENABLED"]) as submit:
        assert init._await_index_enablement(["Index:a"], timeout_s=60) == []
    script = submit.call_args.args[0]
    assert "getFieldKeys()" in script and "getIndexStatus(it)" in script and "rollback()" in script


def test_await_promotes_an_installed_key_register_then_reindex(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: an index over a key committed in an earlier transaction stays INSTALLED. It is promoted along
    the documented lifecycle: REGISTER_INDEX, then REINDEX (which enables it); every key must end ENABLED."""
    from dse_jg_migrator.initialiser import _INDEX_STATUS_SCRIPT, _REGISTER_SCRIPT, _REINDEX_SCRIPT

    init = _init(tmp_path)
    replies = {
        _INDEX_STATUS_SCRIPT: iter(
            [["ENABLED", "INSTALLED"], ["ENABLED", "INSTALLED"], ["ENABLED", "REGISTERED"], ["ENABLED", "ENABLED"]]
        ),
        _REGISTER_SCRIPT: iter([["registered"]]),
        _REINDEX_SCRIPT: iter([["reindexing"]]),
    }
    calls: list[str] = []

    def submit(script, bindings=None):  # type: ignore[no-untyped-def]
        calls.append(script)
        return next(replies[script])

    with (
        patch.object(init, "_submit", side_effect=submit),
        patch("dse_jg_migrator.initialiser.time.sleep"),
    ):
        assert init._await_index_enablement(["Index:a"], timeout_s=60) == []
    assert calls.count(_REGISTER_SCRIPT) == 1 and calls.count(_REINDEX_SCRIPT) == 1
    assert calls.index(_REGISTER_SCRIPT) < calls.index(_REINDEX_SCRIPT)
    for script in (_REGISTER_SCRIPT, _REINDEX_SCRIPT):
        assert "m.commit()" in script and not script.rstrip().endswith("m.commit();")  # must return a value


def test_rerun_waits_on_existing_indexes_not_only_created_ones(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A re-run creates nothing, but must still heal an index left INSTALLED by an earlier run (live finding)."""
    import json

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path)
    with (
        patch.object(init, "_submit", return_value=[]),
        patch.object(init, "_await_index_enablement", return_value=[]) as wait,
    ):
        init.run()
    names = {ix["name"] for ix in Initialiser.build_bindings(_rich_schema())["idxs"]}
    assert {c.split(":", 1)[1] for c in wait.call_args.args[0]} == names


def test_await_returns_pending_names_on_timeout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    clock = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0])
    with (
        patch.object(init, "_submit", return_value=["INSTALLED"]),
        patch("dse_jg_migrator.initialiser.time.sleep"),
        patch("dse_jg_migrator.initialiser.time.time", side_effect=lambda: next(clock)),
    ):
        assert init._await_index_enablement(["Index:a"], timeout_s=15) == ["a"]


def test_unenabled_index_is_reported_as_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path, index_timeout_s=1)
    with (
        patch.object(init, "_submit", return_value=["Index:person_search"]),
        patch.object(init, "_await_index_enablement", return_value=["person_search"]),
    ):
        result = init.run()
    assert result["status"] == "partial"
    assert "person_search" in result["errors"][0]


def test_verify_schema_checks_keys_labels_and_indexes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    found = ["PropertyKey:name", "VertexLabel:person", "Index:knows_by_since"]
    with patch.object(init, "_submit", return_value=found):
        res = init.verify_schema(_rich_schema())
    assert res["PropertyKey:name"] is True
    assert res["PropertyKey:blob"] is False
    assert res["VertexLabel:person"] is True
    assert res["EdgeLabel:knows"] is False
    assert res["Index:knows_by_since"] is True
    assert res["Index:person_search"] is False


def test_verify_schema_all_false_when_server_unreachable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    with patch.object(init, "_submit", side_effect=RuntimeError("down")):
        res = init.verify_schema(_rich_schema())
    assert res and not any(res.values())


def test_stuck_index_error_names_other_registered_instances(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: a JanusGraph instance that was not shut down cleanly blocks index registration forever."""
    import json

    from dse_jg_migrator.initialiser import _OPEN_INSTANCES_SCRIPT

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path, index_timeout_s=1)

    def submit(script, bindings=None):  # type: ignore[no-untyped-def]
        return ["0a59-new(current)", "0a59-old"] if script == _OPEN_INSTANCES_SCRIPT else []

    with (
        patch.object(init, "_submit", side_effect=submit) as sub,
        patch.object(init, "_await_index_enablement", return_value=["person_search"]),
    ):
        result = init.run()
    assert "0a59-old" in result["errors"][0] and "forceCloseInstance" in result["errors"][0]
    assert "0a59-new" not in result["errors"][0]
    assert not any("forceCloseInstance(" in str(c.args[0]) for c in sub.call_args_list)  # diagnose, never force-close


def test_no_instance_hint_when_only_current_instance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    init = _init(tmp_path)
    with patch.object(init, "_submit", return_value=["0a59-new(current)"]):
        assert init._stale_instance_hint() == ""
    with patch.object(init, "_submit", side_effect=RuntimeError("down")):
        assert init._stale_instance_hint() == ""


def test_stuck_index_error_reports_open_transactions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Live finding: one leaked server transaction kept a key INSTALLED ("Stale transaction alert")."""
    import json

    from dse_jg_migrator.initialiser import _OPEN_INSTANCES_SCRIPT, _OPEN_TX_SCRIPT

    (tmp_path / "schema.json").write_text(json.dumps(_rich_schema()), encoding="utf-8")
    init = _init(tmp_path, index_timeout_s=1)
    replies = {_OPEN_INSTANCES_SCRIPT: ["0a59-new(current)"], _OPEN_TX_SCRIPT: [1]}

    def submit(script, bindings=None):  # type: ignore[no-untyped-def]
        return replies.get(script, [])

    with (
        patch.object(init, "_submit", side_effect=submit) as sub,
        patch.object(init, "_await_index_enablement", return_value=["person_search"]),
    ):
        result = init.run()
    assert "1 open transaction(s)" in result["errors"][0] and "forceCloseInstance" not in result["errors"][0]
    assert not any(
        "rollback()" in str(c.args[0]) and "getOpenTransactions" in str(c.args[0]) for c in sub.call_args_list
    )
    with patch.object(init, "_submit", return_value=[0]):
        assert init._open_transaction_hint() == ""
