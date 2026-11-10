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
        os.makedirs(output_dir, exist_ok=True)

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

        audit_path = os.path.join(self.output_dir, f"{self.graph_name}-migration-audit.jsonl")
        audit = self._load_audit(audit_path)

        # Phase summary — from the pipeline's stats when given, otherwise from each phase's output files.
        phases = ["Assessment", "Analysis", "Translation", "Init Target", "Migration", "Validation"]
        lines += ["## Phase Summary", ""]
        if phase_stats:
            lines += ["| Phase | Status | Duration |", "|-------|--------|----------|"]
            for phase in phases:
                stats = phase_stats.get(phase.lower().replace(" ", "_"), {})
                status = "✅ OK" if stats.get("ok", True) else "❌ FAIL"
                lines.append(f"| {phase} | {status} | {stats.get('duration', '—')} |")
        else:
            lines += ["| Phase | Status |", "|-------|--------|"]
            for phase, status in zip(phases, self._phase_statuses(audit), strict=True):
                lines.append(f"| {phase} | {status} |")

        # Migration counts — the audit log has one entry per written batch; the table shows one total per label.
        if os.path.exists(audit_path):
            lines += [
                "",
                "## Migration Counts",
                "",
                "| Kind | Label | Migrated | Failed |",
                "|------|-------|----------|--------|",
            ]
            for (kind, label), (migrated, failed) in self._label_totals(audit).items():
                lines.append(f"| {kind} | {label} | {migrated} | {failed} |")

        lines += self._audit_events_section(audit_path)
        lines += self._rlac_section()

        # Validation summary from report
        validation_path = os.path.join(self.output_dir, f"{self.graph_name}-validation-report.md")
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
        trans_path = os.path.join(self.output_dir, f"{self.graph_name}-translation-report.md")
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

    # ──────────────────────────────────────────────────────────────────────────
    # Appendix E sections
    # ──────────────────────────────────────────────────────────────────────────

    def _audit_events_section(self, audit_path: str) -> list[str]:
        """Render non-batch audit events (e.g. SKIPPED_OLAP, META_PROPERTY_JSON) — Appendix E.2 / E.7."""
        events = [e for e in self._load_audit(audit_path) if "migrated" not in e]
        if not events:
            return []
        lines = ["", "## Migration Events", "", "| Event | Label | Detail |", "|-------|-------|--------|"]
        for e in events:
            lines.append(f"| {e.get('kind', '')} | {e.get('label', '')} | {e.get('message', '')} |")
        return lines

    def _rlac_section(self) -> list[str]:
        """List graph labels and flag that RLAC needs manual recreation (Appendix E.6).

        Triggered when the analysis JSON has ``config.rlac_enabled`` or the assessment
        JSON contains an SE-001 / SE-002 finding. Reads local files only.
        """
        analysis: dict[str, Any] = self._load_json(f"{self.graph_name}-analysis.json")
        assessment: dict[str, Any] = self._load_json(f"{self.graph_name}-assessment-report.json")

        rlac = bool(analysis.get("config", {}).get("rlac_enabled", False))
        rlac = rlac or any(f.get("rule_id") in ("SE-001", "SE-002") for f in assessment.get("findings", []))
        if not rlac:
            return []

        vertex_labels = [vl.get("label", "") for vl in analysis.get("vertex_labels", [])]
        edge_labels = [el.get("label", "") for el in analysis.get("edge_labels", [])]
        return [
            "",
            "## Row-Level Access Control (RLAC) — manual action required",
            "",
            "DSE RLAC policies cannot be translated automatically. Recreate equivalent access rules in "
            "the JanusGraph Gremlin Server security configuration for each label below.",
            "",
            f"- **Vertex labels:** {', '.join(vertex_labels) if vertex_labels else '—'}",
            f"- **Edge labels:** {', '.join(edge_labels) if edge_labels else '—'}",
        ]

    # ──────────────────────────────────────────────────────────────────────────
    # Audit log and phase status
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_audit(audit_path: str) -> list[dict[str, Any]]:
        """Read the audit log's entries; ``[]`` if it is absent or unreadable."""
        try:
            with open(audit_path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _label_totals(audit: list[dict[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
        """Sum the batch entries (they carry ``migrated``) per ``(kind, label)``, in the order labels were migrated."""
        totals: dict[tuple[str, str], tuple[int, int]] = {}
        for entry in audit:
            if "migrated" not in entry:
                continue  # event entries (SKIPPED_OLAP, ...) are listed separately
            key = (str(entry.get("kind", "")), str(entry.get("label", "")))
            migrated, failed = totals.get(key, (0, 0))
            totals[key] = (migrated + int(entry.get("migrated", 0)), failed + int(entry.get("failed", 0)))
        return totals

    def _phase_statuses(self, audit: list[dict[str, Any]]) -> list[str]:
        """Status of each phase, read from the files it writes; a phase without output reads "— not run".

        Init Target writes no file: it is reported as done once the target accepted migrated data.
        """
        not_run = "— not run"
        assessment = self._load_json(f"{self.graph_name}-assessment-report.json")
        red = sum(1 for f in assessment.get("findings", []) if f.get("severity") == "RED")
        risk = assessment.get("overall_risk", "")
        if not assessment:
            assessed = not_run
        elif red:
            assessed = f"⚠️ {risk} risk — {red} RED finding(s), see the assessment report"
        else:
            assessed = f"✅ {risk} risk"

        def produced(suffix: str) -> str:
            return "✅ OK" if os.path.exists(os.path.join(self.output_dir, f"{self.graph_name}{suffix}")) else not_run

        failures = sum(1 for e in audit if e.get("kind") in ("BATCH_FAILED", "SOURCE_READ_FAILED"))
        written = sum(int(e.get("migrated", 0)) for e in audit)
        if failures:
            migrated = f"❌ {failures} failed batch(es) or read(s) — re-run migrate"
        elif any("migrated" in e for e in audit):
            migrated = "✅ OK"
        else:
            migrated = not_run

        validated = not_run
        validation_path = os.path.join(self.output_dir, f"{self.graph_name}-validation-report.md")
        try:
            with open(validation_path, encoding="utf-8") as fh:
                overall = next((ln for ln in fh if ln.startswith("**Overall:**")), "")
            validated = "✅ PASS" if "PASS" in overall else "❌ FAIL" if "FAIL" in overall else not_run
        except OSError:
            pass

        return [
            assessed,
            produced("-analysis.json"),
            produced("-janusgraph-schema.json"),
            "✅ OK" if written else not_run,
            migrated,
            validated,
        ]

    def _load_json(self, filename: str) -> dict[str, Any]:
        """Load a JSON file from output_dir; return {} if absent or unreadable."""
        path = os.path.join(self.output_dir, filename)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data
