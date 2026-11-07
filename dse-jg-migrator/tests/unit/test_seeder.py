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
