"""Migration report generator — reads intermediate output files and generates final report."""

from __future__ import annotations

import json
import os
from typing import Any


class Reporter:
    """Reads intermediate pipeline output files and generates <graph>-migration-report.md.

    Reads from: local JSON/JSONL/MD files produced by earlier pipeline phases.
    Writes: <graph_name>-migration-report.md to output_dir.
    """

    def __init__(
        self,
        graph_name: str,
        output_dir: str = ".",
    ) -> None:
        self.graph_name = graph_name
        self.output_dir = output_dir

    def generate(self, phase_stats: dict[str, Any] | None = None) -> str:
        """Generate the final migration report.

        Args:
            phase_stats: Optional dict with per-phase timing/count stats from the pipeline.

        Returns:
            Path to the generated report file.
        """
        lines: list[str] = [
            f"# Migration Report: {self.graph_name} → janusgraph-hcd",
            "",
        ]

        # Phase summary table
        phases = ["Assessment", "Analysis", "Translation", "Init Target", "Migration", "Validation"]
        lines += [
            "## Phase Summary",
            "",
            "| Phase | Status | Duration |",
            "|-------|--------|----------|",
        ]
        for phase in phases:
            stats = (phase_stats or {}).get(phase.lower().replace(" ", "_"), {})
            status = "✅ OK" if stats.get("ok", True) else "❌ FAIL"
            duration = stats.get("duration", "—")
            lines.append(f"| {phase} | {status} | {duration} |")

        # Migration counts from audit log
        audit_path = os.path.join(self.output_dir, f"{self.graph_name}-migration-audit.jsonl")
        if os.path.exists(audit_path):
            lines += ["", "## Migration Counts", "", "| Kind | Label | Migrated | Failed |",
                      "|------|-------|----------|--------|"]
            try:
                with open(audit_path, encoding="utf-8") as fh:
                    for line in fh:
                        entry: dict[str, Any] = json.loads(line.strip())
                        lines.append(
                            f"| {entry.get('kind','')} | {entry.get('label','')} "
                            f"| {entry.get('migrated',0)} | {entry.get('failed',0)} |"
                        )
            except (OSError, json.JSONDecodeError):
                pass

        # Validation summary from report
        validation_path = os.path.join(
            self.output_dir, f"{self.graph_name}-validation-report.md"
        )
        if os.path.exists(validation_path):
            lines += ["", "## Validation Summary", ""]
            try:
                with open(validation_path, encoding="utf-8") as fh:
                    for vline in fh:
                        if vline.strip().startswith("|"):
                            lines.append(vline.rstrip())
            except OSError:
                pass

        # Translation notes from translation report
        trans_path = os.path.join(
            self.output_dir, f"{self.graph_name}-translation-report.md"
        )
        if os.path.exists(trans_path):
            lines += ["", "## Translation Notes", ""]
            try:
                with open(trans_path, encoding="utf-8") as fh:
                    in_notes = False
                    for tline in fh:
                        if tline.strip() == "## Notes":
                            in_notes = True
                            continue
                        if in_notes and tline.startswith("## "):
                            break
                        if in_notes:
                            lines.append(tline.rstrip())
            except OSError:
                pass

        content = "\n".join(lines)
        path = os.path.join(self.output_dir, f"{self.graph_name}-migration-report.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path
