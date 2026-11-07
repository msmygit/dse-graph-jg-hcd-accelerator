"""Traversal validator — compares DSE source vs JanusGraph target after migration."""

from __future__ import annotations

import os
from typing import Any

from dse_jg_migrator.models import AnalysisResult


class Validator:
    """Runs count and sample-property validation against source and target clusters.

    Reads from: source DSE Gremlin server, target JanusGraph Gremlin server.
    Writes: <graph_name>-validation-report.md to output_dir.
    """

    def __init__(
        self,
        analysis: AnalysisResult,
        source_gremlin_url: str,
        target_gremlin_url: str,
        source_username: str | None = None,
        source_password: str | None = None,
        target_username: str | None = None,
        target_password: str | None = None,
        validation_queries_path: str | None = None,
        output_dir: str = ".",
    ) -> None:
        self.analysis = analysis
        self.source_gremlin_url = source_gremlin_url
        self.target_gremlin_url = target_gremlin_url
        self.source_username = source_username
        self.source_password = source_password
        self.target_username = target_username
        self.target_password = target_password
        self.validation_queries_path = validation_queries_path
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def run(self) -> dict[str, Any]:
        """Execute all validation checks and write the report."""
        count_results = self._validate_counts()
        smoke_results = self._validate_smoke_tests()

        report = {
            "graph_name": self.analysis.graph_name,
            "count_results": count_results,
            "smoke_results": smoke_results,
            "overall_pass": all(r["match"] for r in count_results),
        }
        self._write_report(report)
        return report

    # ──────────────────────────────────────────────────────────────────────────
    # Count validation
    # ──────────────────────────────────────────────────────────────────────────

    def _validate_counts(self) -> list[dict[str, Any]]:
        """Compare vertex and edge counts between source and target."""
        results: list[dict[str, Any]] = []

        for vl in self.analysis.vertex_labels:
            label = vl.label
            src = self._get_count(self.source_gremlin_url, label, "vertex",
                                  self.source_username, self.source_password)
            tgt = self._get_count(self.target_gremlin_url, label, "vertex",
                                  self.target_username, self.target_password)
            results.append({
                "type": "vertex",
                "label": label,
                "source": src,
                "target": tgt,
                "match": src == tgt,
            })

        for el in self.analysis.edge_labels:
            label = el.label
            src = self._get_count(self.source_gremlin_url, label, "edge",
                                  self.source_username, self.source_password)
            tgt = self._get_count(self.target_gremlin_url, label, "edge",
                                  self.target_username, self.target_password)
            results.append({
                "type": "edge",
                "label": label,
                "source": src,
                "target": tgt,
                "match": src == tgt,
            })

        return results

    def _get_count(
        self,
        url: str,
        label: str,
        element_type: str,
        username: str | None,
        password: str | None,
    ) -> int:
        """Get vertex or edge count for a label from a Gremlin endpoint."""
        if element_type == "vertex":
            script = "g.V().hasLabel(lbl).count()"
        else:
            script = "g.E().hasLabel(lbl).count()"
        try:
            result = self._submit(url, script, {"lbl": label}, username, password)
            return int(result[0]) if result else 0
        except Exception:  # noqa: BLE001, S110
            return -1

    # ──────────────────────────────────────────────────────────────────────────
    # Smoke tests
    # ──────────────────────────────────────────────────────────────────────────

    def _validate_smoke_tests(self) -> list[dict[str, Any]]:
        """Run configured validation queries from a groovy file (if provided)."""
        results: list[dict[str, Any]] = []
        if not self.validation_queries_path:
            return results

        try:
            with open(self.validation_queries_path, encoding="utf-8") as fh:
                lines = [
                    line.strip()
                    for line in fh
                    if line.strip() and not line.strip().startswith("//")
                ]
        except OSError:
            return results

        for query in lines:
            try:
                src_result = self._submit(
                    self.source_gremlin_url, query, {}, self.source_username, self.source_password
                )
                tgt_result = self._submit(
                    self.target_gremlin_url, query, {}, self.target_username, self.target_password
                )
                match = str(src_result) == str(tgt_result)
                results.append({
                    "query": query,
                    "source": str(src_result)[:100],
                    "target": str(tgt_result)[:100],
                    "match": match,
                })
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "query": query,
                    "source": "ERROR",
                    "target": "ERROR",
                    "match": False,
                    "error": str(exc),
                })

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # Report
    # ──────────────────────────────────────────────────────────────────────────

    def _write_report(self, report: dict[str, Any]) -> str:
        lines = [
            f"# Validation Report: {report['graph_name']}",
            "",
            f"**Overall:** {'✅ PASS' if report['overall_pass'] else '❌ FAIL'}",
            "",
            "## Count Validation",
            "",
            "| Type | Label | Source | Target | Match |",
            "|------|-------|--------|--------|-------|",
        ]
        for r in report["count_results"]:
            icon = "✅" if r["match"] else "❌"
            lines.append(
                f"| {r['type']} | {r['label']} | {r['source']} | {r['target']} | {icon} |"
            )

        if report["smoke_results"]:
            lines += [
                "",
                "## Smoke Tests",
                "",
                "| Query | Source | Target | Match |",
                "|-------|--------|--------|-------|",
            ]
            for r in report["smoke_results"]:
                icon = "✅" if r["match"] else "❌"
                lines.append(
                    f"| `{r['query'][:60]}` | {r['source'][:30]} | {r['target'][:30]} | {icon} |"
                )

        path = os.path.join(
            self.output_dir, f"{self.analysis.graph_name}-validation-report.md"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path

    # ──────────────────────────────────────────────────────────────────────────
    # Gremlin I/O
    # ──────────────────────────────────────────────────────────────────────────

    def _submit(
        self,
        url: str,
        script: str,
        bindings: dict[str, Any],
        username: str | None,
        password: str | None,
    ) -> Any:  # noqa: ANN401
        """Submit a Gremlin script to the given URL and return all results."""
        from gremlin_python.driver import client as gremlin_client

        kwargs: dict[str, Any] = {}
        if username:
            kwargs["username"] = username
        if password:
            kwargs["password"] = password

        c = gremlin_client.Client(url, "g", **kwargs)
        try:
            return c.submit(script, bindings).all().result()
        finally:
            c.close()
