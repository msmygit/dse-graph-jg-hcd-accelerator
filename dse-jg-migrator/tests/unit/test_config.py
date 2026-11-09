"""Unit tests for config.py."""

from __future__ import annotations

import pytest

from dse_jg_migrator.config import AssessmentConfig, MigrationConfig, SourceConfig, TargetConfig


def test_source_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSE_GREMLIN_URL", "ws://dse:8182/gremlin")
    monkeypatch.setenv("DSE_CQL_HOST", "dse")
    monkeypatch.setenv("DSE_CQL_PORT", "9043")
    monkeypatch.setenv("DSE_GRAPH_NAME", "food")
    monkeypatch.setenv("DSE_USERNAME", "u")
    monkeypatch.setenv("DSE_PASSWORD", "p")
    cfg = SourceConfig.from_env()
    assert (cfg.gremlin_url, cfg.cql_host, cfg.cql_port, cfg.graph_name) == (
        "ws://dse:8182/gremlin",
        "dse",
        9043,
        "food",
    )
    assert (cfg.username, cfg.password) == ("u", "p")


def test_source_config_from_env_missing_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DSE_GREMLIN_URL", raising=False)
    with pytest.raises(KeyError):
        SourceConfig.from_env()


def test_target_config_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HCD_CQL_HOST", "HCD_CQL_PORT", "HCD_CQL_USERNAME", "HCD_CQL_PASSWORD", "HCD_KEYSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JG_GREMLIN_URL", "ws://jg:8182/gremlin")
    cfg = TargetConfig.from_env()
    assert cfg.gremlin_url == "ws://jg:8182/gremlin"
    assert cfg.cql_port == 9042
    assert cfg.keyspace == "jg_migration_test"


def test_target_config_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JG_GREMLIN_URL", "ws://jg:8182/gremlin")
    monkeypatch.setenv("HCD_CQL_HOST", "hcd")
    monkeypatch.setenv("HCD_CQL_PORT", "9999")
    monkeypatch.setenv("OPENSEARCH_PORT", "9201")
    cfg = TargetConfig.from_env()
    assert (cfg.cql_host, cfg.cql_port, cfg.opensearch_port) == ("hcd", 9999, 9201)


def test_dataclass_defaults() -> None:
    assert MigrationConfig().write_batch == 100
    assert AssessmentConfig().output_formats == ["all"]
