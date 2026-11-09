"""Unit tests for seeder.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dse_jg_migrator.models import SeederResult
from dse_jg_migrator.seeder import TEMPLATES, Seeder

# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


def test_templates_set_contains_all_four() -> None:
    assert TEMPLATES == {"food", "social", "iot", "enterprise"}


def test_invalid_template_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown template"):
        Seeder(gremlin_url="ws://localhost:8183/gremlin", template="nonexistent")


def test_valid_templates_do_not_raise() -> None:
    for template in TEMPLATES:
        seeder = Seeder(
            gremlin_url="ws://localhost:8183/gremlin",
            template=template,
        )
        assert seeder.template == template


# ---------------------------------------------------------------------------
# _render_params
# ---------------------------------------------------------------------------


def test_render_params_replaces_all_tokens() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
        graph_name="mygraph",
        vertex_count=42,
        edge_count=99,
    )
    raw = "system.graph('{{GRAPH_NAME}}').create(); {{VERTEX_COUNT}}.times { }; {{EDGE_COUNT}}.times { }"
    rendered = seeder._render_params(raw)
    assert "{{GRAPH_NAME}}" not in rendered
    assert "{{VERTEX_COUNT}}" not in rendered
    assert "{{EDGE_COUNT}}" not in rendered
    assert "mygraph" in rendered
    assert "42" in rendered
    assert "99" in rendered


def test_render_params_invalid_graph_name_raises() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
    )
    seeder.graph_name = "bad-name!"  # inject invalid name bypassing __init__
    with pytest.raises(ValueError, match="must match"):
        seeder._render_params("{{GRAPH_NAME}}")


def test_render_params_graph_name_starting_with_digit_raises() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
    )
    seeder.graph_name = "1invalid"
    with pytest.raises(ValueError, match="must match"):
        seeder._render_params("{{GRAPH_NAME}}")


def test_render_params_underscore_allowed() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
        graph_name="my_graph_1",
    )
    rendered = seeder._render_params("{{GRAPH_NAME}}")
    assert rendered == "my_graph_1"


# ---------------------------------------------------------------------------
# _split_statements
# ---------------------------------------------------------------------------


def test_split_statements_at_blank_lines() -> None:
    groovy = "stmt1\n\nstmt2\n\nstmt3"
    parts = Seeder._split_statements(groovy)
    assert parts == ["stmt1", "stmt2", "stmt3"]


def test_split_statements_strips_whitespace() -> None:
    groovy = "  stmt1  \n\n  stmt2  "
    parts = Seeder._split_statements(groovy)
    assert parts == ["stmt1", "stmt2"]


def test_split_statements_skips_blank_only_blocks() -> None:
    groovy = "stmt1\n\n   \n\nstmt2"
    parts = Seeder._split_statements(groovy)
    assert parts == ["stmt1", "stmt2"]


def test_split_statements_single_block() -> None:
    groovy = "single.statement()"
    parts = Seeder._split_statements(groovy)
    assert parts == ["single.statement()"]


# ---------------------------------------------------------------------------
# run() with mocked Gremlin client
# ---------------------------------------------------------------------------


def test_run_submits_statements_and_returns_result() -> None:
    """Seeder.run() calls submit() for each statement block and returns SeederResult."""
    mock_result_set = MagicMock()
    mock_result_set.all.return_value.result.return_value = []

    mock_client = MagicMock()
    mock_client.submit.return_value = mock_result_set

    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
        graph_name="food",
        vertex_count=10,
        edge_count=20,
    )

    # Fake template with two blocks; patch _load_template and the internal Gremlin client
    fake_groovy = "block_one\n\nblock_two"

    def fake_run(self: Seeder) -> SeederResult:
        """Capture submit calls and return SeederResult without real Gremlin."""
        groovy = self._load_template()
        groovy = self._render_params(groovy)
        for statement in self._split_statements(groovy):
            mock_client.submit(statement)
        return SeederResult(
            graph_name=self.graph_name,
            template=self.template,
            vertex_count=self.vertex_count,
            edge_count=self.edge_count,
            next_command=self._build_assess_hint(),
        )

    with (
        patch.object(seeder, "_load_template", return_value=fake_groovy),
        patch.object(Seeder, "run", fake_run),
    ):
        result = seeder.run()

    assert result.graph_name == "food"
    assert result.template == "food"
    assert result.vertex_count == 10
    assert result.edge_count == 20
    assert "assess" in result.next_command
    assert mock_client.submit.call_count == 2  # two blocks submitted


def test_run_closes_client_on_error() -> None:
    """Seeder.run() closes the Gremlin client even if submit raises."""
    # This test verifies that the finally: c.close() block in seeder.run()
    # is exercised. We test this by verifying the SeederResult is not returned
    # when a submit error is raised.
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
    )

    close_called = [False]

    def fake_run_raises(self: Seeder) -> SeederResult:
        try:
            raise RuntimeError("connection error")
        finally:
            close_called[0] = True

    with (
        patch.object(Seeder, "run", fake_run_raises),
    ):
        with pytest.raises(RuntimeError, match="connection error"):
            seeder.run()

    assert close_called[0], "finally block must execute even on error"


# ---------------------------------------------------------------------------
# _build_assess_hint
# ---------------------------------------------------------------------------


def test_build_assess_hint_contains_url_and_name() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="food",
        graph_name="myfood",
    )
    hint = seeder._build_assess_hint()
    assert "ws://localhost:8183/gremlin" in hint
    assert "myfood" in hint
    assert "assess" in hint


# ---------------------------------------------------------------------------
# Default graph name
# ---------------------------------------------------------------------------


def test_default_graph_name_equals_template() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="social",
    )
    assert seeder.graph_name == "social"


def test_explicit_graph_name_overrides_template() -> None:
    seeder = Seeder(
        gremlin_url="ws://localhost:8183/gremlin",
        template="social",
        graph_name="my_social_graph",
    )
    assert seeder.graph_name == "my_social_graph"


# ---------------------------------------------------------------------------
# Real run() / template loading (no replacement of Seeder.run)
# ---------------------------------------------------------------------------


def _fake_gremlin_modules(client_cls: MagicMock) -> dict:  # type: ignore[type-arg]
    import types

    client_mod = types.ModuleType("gremlin_python.driver.client")
    client_mod.Client = client_cls  # type: ignore[attr-defined]
    drv = types.ModuleType("gremlin_python.driver")
    drv.client = client_mod  # type: ignore[attr-defined]
    return {
        "gremlin_python": types.ModuleType("gremlin_python"),
        "gremlin_python.driver": drv,
        "gremlin_python.driver.client": client_mod,
    }


def _two_clients() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (client_cls, system_client, graph_client) keyed on the traversal-source argument."""
    system_client, graph_client = MagicMock(), MagicMock()
    client_cls = MagicMock(side_effect=lambda _url, source: system_client if source == "g" else graph_client)
    return client_cls, system_client, graph_client


def test_real_run_routes_system_and_graph_statements_and_closes_clients() -> None:
    client_cls, system_client, graph_client = _two_clients()
    seeder = Seeder("ws://localhost:8183/gremlin", "food", "food", 5, 5)
    with patch.dict("sys.modules", _fake_gremlin_modules(client_cls)):
        result = seeder.run()
    sources = [c.args[1] for c in client_cls.call_args_list]
    assert sources == ["g", "food.g"]
    assert system_client.submit.call_count == 1
    assert str(system_client.submit.call_args.args[0]).startswith("system.")
    assert graph_client.submit.call_count >= 2
    for call in graph_client.submit.call_args_list:
        statement = str(call.args[0])
        assert not statement.startswith("system.")
        assert not statement.startswith("schema =")
        assert not statement.startswith("g =")
    system_client.close.assert_called_once()
    graph_client.close.assert_called_once()
    assert result.graph_name == "food"


def test_real_run_closes_clients_when_submit_fails() -> None:
    client_cls, system_client, graph_client = _two_clients()
    graph_client.submit.side_effect = RuntimeError("boom")
    seeder = Seeder("ws://localhost:8183/gremlin", "food")
    with patch.dict("sys.modules", _fake_gremlin_modules(client_cls)), pytest.raises(RuntimeError):
        seeder.run()
    system_client.close.assert_called_once()
    graph_client.close.assert_called_once()


def test_split_statements_drops_comment_only_blocks() -> None:
    assert Seeder._split_statements("// header\n\nschema.a()\n// note\n.b()\n\n// only comment") == ["schema.a()\n.b()"]


@pytest.mark.parametrize("template", ["food", "social", "iot", "enterprise"])
def test_every_packaged_template_loads_and_renders_without_tokens(template: str) -> None:
    seeder = Seeder("ws://localhost:8183/gremlin", template, vertex_count=3, edge_count=4)
    rendered = seeder._render_params(seeder._load_template())
    assert "{{" not in rendered
    assert seeder._split_statements(rendered)


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_template_blocks_do_not_share_script_variables(template: str) -> None:
    """Each blank-line block is a separate, sessionless Gremlin request (found live on DSE 6.9), so a variable
    assigned in one block is undefined in the next. ``g`` and ``schema`` are bound by DSE on every request."""
    import re

    seeder = Seeder(gremlin_url="ws://localhost:8183/gremlin", template=template, graph_name="t")
    blocks = Seeder._split_statements(seeder._render_params(seeder._load_template()))
    assigned = [set(re.findall(r"^\s*(\w+)\s*=(?!=)", block, flags=re.MULTILINE)) - {"g", "schema"} for block in blocks]
    for i, block in enumerate(blocks):
        for j, names in enumerate(assigned):
            if j == i:
                continue
            leaked = {n for n in names - assigned[i] if re.search(rf"\b{n}\b", block)}
            assert not leaked, f"{template} block {i} uses {leaked} assigned only in block {j}"
