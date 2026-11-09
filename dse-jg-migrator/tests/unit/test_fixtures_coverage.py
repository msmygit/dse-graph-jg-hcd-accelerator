"""AGENTS.md §5.4 — the canned schema fixture exercises every rule category."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from dse_jg_migrator.assessor import Assessor
from dse_jg_migrator.compatibility_rules import RULES

FIXTURE = Path(__file__).parent.parent / "fixtures" / "food_schema_all_categories.groovy"

# The 8 categories required by AGENTS.md §5.4 (DATA_TYPE has no rules in the catalogue).
REQUIRED_PREFIXES = {"QP", "SC", "IX", "CF", "OL", "SE", "TP", "HC"}


def _run_assessor_on_fixture() -> set[str]:
    assessor = Assessor("ws://localhost:8183/gremlin", "localhost", 9043, "food", target_cql_host="hcd.local")
    assessor._raw_schema = FIXTURE.read_text(encoding="utf-8")
    with (
        patch.object(assessor, "_gremlin_submit", return_value=[0]),
        patch("cassandra.cluster.Cluster") as cluster_cls,
    ):
        cluster_cls.return_value.connect.return_value.execute.return_value = [MagicMock(value="false")]
        findings = (
            assessor._assess_schema()
            + assessor._assess_indexes()
            + assessor._assess_configuration()
            + assessor._assess_query_patterns()
            + assessor._assess_olap()
            + assessor._assess_security()
            + assessor._assess_tinkerpop_delta()
            + assessor._assess_hcd_target()
        )
    return {f.rule_id for f in findings}


def test_fixture_exists() -> None:
    assert FIXTURE.is_file()


def test_fixture_triggers_every_rule_category() -> None:
    emitted_prefixes = {rid.split("-")[0] for rid in _run_assessor_on_fixture()}
    assert REQUIRED_PREFIXES <= emitted_prefixes


def test_catalogue_covers_all_required_prefixes() -> None:
    assert {r.rule_id.split("-")[0] for r in RULES} >= REQUIRED_PREFIXES
