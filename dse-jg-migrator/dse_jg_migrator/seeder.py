"""Sample workload seeder — creates a named DSE Graph with schema + synthetic data.

NOTE: This module WRITES to the source DSE cluster.
It is a demo/dev utility only. Never run against production.
"""

from __future__ import annotations

import re
from importlib import resources

from dse_jg_migrator.models import SeederResult

TEMPLATES: frozenset[str] = frozenset({"food", "social", "iot", "enterprise"})

# `schema = <graph>.schema()` / `g = <graph>.traversal()` — provided by the "<graph>.g" alias.
_BINDING_RE = re.compile(r"(schema|g)\s*=\s*\w+\.(schema|traversal)\(\)")


class Seeder:
    """Creates a named DSE Graph and loads schema + synthetic data from a built-in template.

    Connects to: source DSE Gremlin server (WRITE operations).
    Idempotent: all schema uses ifNotExists(); data insert skips if vertex count already
    meets the --vertex-count target.

    Args:
        gremlin_url: WebSocket URL of the DSE Gremlin server.
        template: Built-in template name ('food', 'social', 'iot', 'enterprise').
        graph_name: Name for the created graph. Defaults to template name.
        vertex_count: Number of synthetic vertices per primary label.
        edge_count: Number of synthetic edges per primary label.
    """

    def __init__(
        self,
        gremlin_url: str,
        template: str,
        graph_name: str | None = None,
        vertex_count: int = 1_000,
        edge_count: int = 5_000,
    ) -> None:
        if template not in TEMPLATES:
            raise ValueError(f"Unknown template '{template}'. Choose from: {sorted(TEMPLATES)}")
        self.gremlin_url = gremlin_url
        self.template = template
        self.graph_name = graph_name or template
        self.vertex_count = vertex_count
        self.edge_count = edge_count

    def run(self) -> SeederResult:
        """Execute the seed. Returns a SeederResult with counts and next-step hints."""
        groovy = self._load_template()
        groovy = self._render_params(groovy)
        from gremlin_python.driver import client as gremlin_client

        # `system.*` statements run on the plain "g" source; everything after graph creation must
        # use the "<graph>.g" alias (DSE does not keep `schema = <graph>.schema()` between requests).
        system_client = gremlin_client.Client(self.gremlin_url, "g")
        graph_client = None
        try:
            for statement in self._split_statements(groovy):
                if _BINDING_RE.fullmatch(statement):
                    continue  # `schema = x.schema()` / `g = x.traversal()` — provided by the alias
                if statement.startswith("system."):
                    system_client.submit(statement).all().result()
                    continue
                if graph_client is None:
                    graph_client = gremlin_client.Client(self.gremlin_url, f"{self.graph_name}.g")
                graph_client.submit(statement).all().result()
        finally:
            system_client.close()
            if graph_client is not None:
                graph_client.close()

        return SeederResult(
            graph_name=self.graph_name,
            template=self.template,
            vertex_count=self.vertex_count,
            edge_count=self.edge_count,
            next_command=self._build_assess_hint(),
        )

    def _load_template(self) -> str:
        """Load the .groovy template from the package's seed_templates/ directory."""
        pkg = resources.files("dse_jg_migrator.seed_templates")
        return (pkg / f"{self.template}.groovy").read_text(encoding="utf-8")

    def _render_params(self, groovy: str) -> str:
        """Replace known tokens in the template.

        Substitution is on a fixed allow-list of tokens — no arbitrary user
        input is interpolated into Groovy to prevent code injection.
        """
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", self.graph_name):
            raise ValueError(f"Graph name '{self.graph_name}' must match [a-zA-Z][a-zA-Z0-9_]*")

        replacements: dict[str, str] = {
            "{{GRAPH_NAME}}": self.graph_name,
            "{{VERTEX_COUNT}}": str(self.vertex_count),
            "{{EDGE_COUNT}}": str(self.edge_count),
        }
        for token, value in replacements.items():
            groovy = groovy.replace(token, value)
        return groovy

    @staticmethod
    def _split_statements(groovy: str) -> list[str]:
        """Split multi-statement Gremlin scripts at blank lines.

        Each statement is submitted as a separate Gremlin request to avoid
        Gremlin server timeout on large scripts.
        """
        statements: list[str] = []
        for block in groovy.split("\n\n"):
            lines = [ln for ln in block.splitlines() if not ln.lstrip().startswith("//")]
            statement = "\n".join(lines).strip()
            if statement:
                statements.append(statement)
        return statements

    def _build_assess_hint(self) -> str:
        return f"dse-jg-migrator assess --source-gremlin-url {self.gremlin_url} --graph-name {self.graph_name}"
