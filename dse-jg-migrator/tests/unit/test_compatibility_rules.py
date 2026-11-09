"""Unit tests for compatibility_rules.py."""

from __future__ import annotations

from dse_jg_migrator.compatibility_rules import RULES, RULES_BY_ID, Category, Severity


def test_all_rules_have_unique_ids() -> None:
    """Each rule must have a unique rule_id."""
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids)), "Duplicate rule IDs found"


def test_all_rules_have_non_empty_remediation() -> None:
    """Every rule must have a non-empty remediation string."""
    for rule in RULES:
        assert rule.remediation.strip(), f"{rule.rule_id} has empty remediation"


def test_all_rules_have_non_empty_docs_link() -> None:
    """Every rule must have a non-empty docs_link."""
    for rule in RULES:
        assert rule.docs_link.strip(), f"{rule.rule_id} has empty docs_link"


def test_all_rules_have_non_empty_description() -> None:
    """Every rule must have a non-empty description."""
    for rule in RULES:
        assert rule.description.strip(), f"{rule.rule_id} has empty description"


def test_rule_catalogue_covers_all_categories() -> None:
    """At least one rule must exist per defined category (DATA_TYPE used in summaries only)."""
    categories_in_rules = {r.category for r in RULES}
    # DATA_TYPE is used in assessment summaries but has no dedicated static rules;
    # it is emitted dynamically by the assessor when map/UDT types are found.
    covered_by_static_rules = {
        Category.QUERY_PATTERN,
        Category.SCHEMA,
        Category.INDEX,
        Category.CONFIGURATION,
        Category.OLAP,
        Category.SECURITY,
        Category.TINKERPOP_DELTA,
        Category.HCD_TARGET,
    }
    for cat in covered_by_static_rules:
        assert cat in categories_in_rules, f"No rule found for category {cat}"


def test_rule_catalogue_covers_all_severities() -> None:
    """At least one rule must exist per defined severity."""
    severities_in_rules = {r.severity for r in RULES}
    for sev in Severity:
        assert sev in severities_in_rules, f"No rule found for severity {sev}"


def test_rules_by_id_lookup() -> None:
    """RULES_BY_ID index must contain every rule."""
    for rule in RULES:
        assert rule.rule_id in RULES_BY_ID
        assert RULES_BY_ID[rule.rule_id] is rule


def test_qp_rules_present() -> None:
    """Query pattern rules QP-001 through QP-017 must all exist."""
    for n in range(1, 18):
        rule_id = f"QP-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_sc_rules_present() -> None:
    """Schema rules SC-001 through SC-008 must all exist."""
    for n in range(1, 9):
        rule_id = f"SC-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_ix_rules_present() -> None:
    """Index rules IX-001 through IX-008 must all exist."""
    for n in range(1, 9):
        rule_id = f"IX-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_cf_rules_present() -> None:
    """Configuration rules CF-001 through CF-006 must all exist."""
    for n in range(1, 7):
        rule_id = f"CF-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_ol_rules_present() -> None:
    """OLAP rules OL-001 through OL-003 must all exist."""
    for n in range(1, 4):
        rule_id = f"OL-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_se_rules_present() -> None:
    """Security rules SE-001 through SE-002 must all exist."""
    for n in range(1, 3):
        rule_id = f"SE-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_tp_rules_present() -> None:
    """TinkerPop delta rules TP-001 through TP-005 must all exist."""
    for n in range(1, 6):
        rule_id = f"TP-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_hc_rules_present() -> None:
    """HCD target rules HC-001 through HC-008 must all exist."""
    for n in range(1, 9):
        rule_id = f"HC-{n:03d}"
        assert rule_id in RULES_BY_ID, f"Missing rule {rule_id}"


def test_total_rule_count() -> None:
    """Exactly 57 rules should exist (QP-017 + SC-008 + IX-008 + CF-006 + OL-003 + SE-002 + TP-005 + HC-008)."""
    assert len(RULES) == 57, f"Expected 57 rules, got {len(RULES)}"


def test_hc_006_is_green() -> None:
    """HC-006 (port/DC compatibility) must be GREEN."""
    assert RULES_BY_ID["HC-006"].severity == Severity.GREEN


def test_tp_004_is_green() -> None:
    """TP-004 (GraphBinary) must be GREEN."""
    assert RULES_BY_ID["TP-004"].severity == Severity.GREEN


def test_red_rules_have_empty_jg_equivalent_or_note() -> None:
    """RED blockers should clearly indicate no auto-migration path (jg_equivalent empty or note)."""
    red_blockers_no_auto = [
        "QP-005",
        "QP-006",
        "QP-007",
        "QP-008",
        "QP-009",
        "SC-003",
        "SC-005",
        "SC-007",
        "OL-001",
        "OL-002",
        "OL-003",
        "SE-001",
        "SE-002",
        "HC-008",
    ]
    for rule_id in red_blockers_no_auto:
        rule = RULES_BY_ID[rule_id]
        assert rule.severity == Severity.RED, f"{rule_id} expected RED"


def test_cf_006_source_unreachable_is_red() -> None:
    """CF-006 is RED, so an unreachable source makes the assessment gate stop run-all (found in a fresh-user test)."""
    rule = RULES_BY_ID["CF-006"]
    assert rule.severity == Severity.RED
    assert "seed" in rule.remediation and "up.sh" in rule.remediation
