"""Configuration dataclasses for DSE source and JanusGraph/HCD target connections."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class SourceConfig:
    """Connection configuration for the source DSE Graph cluster."""

    gremlin_url: str
    cql_host: str
    cql_port: int = 9042
    graph_name: str = ""
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> SourceConfig:
        """Build from environment variables (DSE_GREMLIN_URL, DSE_CQL_HOST, etc.)."""
        return cls(
            gremlin_url=os.environ["DSE_GREMLIN_URL"],
            cql_host=os.environ["DSE_CQL_HOST"],
            cql_port=int(os.environ.get("DSE_CQL_PORT", "9042")),
            graph_name=os.environ.get("DSE_GRAPH_NAME", ""),
            username=os.environ.get("DSE_USERNAME"),
            password=os.environ.get("DSE_PASSWORD"),
        )


@dataclass
class TargetConfig:
    """Connection configuration for the target JanusGraph / HCD cluster."""

    gremlin_url: str
    cql_host: str = ""
    cql_port: int = 9042
    cql_username: str = "cassandra"
    cql_password: str = "cassandra"  # noqa: S105
    keyspace: str = "jg_migration_test"
    local_datacenter: str = "dc1"
    opensearch_host: str = ""
    opensearch_port: int = 9200

    @classmethod
    def from_env(cls) -> TargetConfig:
        """Build from environment variables (JG_GREMLIN_URL, HCD_CQL_HOST, etc.)."""
        return cls(
            gremlin_url=os.environ["JG_GREMLIN_URL"],
            cql_host=os.environ.get("HCD_CQL_HOST", ""),
            cql_port=int(os.environ.get("HCD_CQL_PORT", "9042")),
            cql_username=os.environ.get("HCD_CQL_USERNAME", "cassandra"),
            cql_password=os.environ.get("HCD_CQL_PASSWORD", "cassandra"),  # noqa: S106
            keyspace=os.environ.get("HCD_KEYSPACE", "jg_migration_test"),
            local_datacenter=os.environ.get("HCD_DATACENTER", "dc1"),
            opensearch_host=os.environ.get("OPENSEARCH_HOST", ""),
            opensearch_port=int(os.environ.get("OPENSEARCH_PORT", "9200")),
        )


@dataclass
class MigrationConfig:
    """Tuning parameters for the data migrator."""

    cql_fetch_size: int = 1000
    gremlin_batch: int = 200
    write_batch: int = 100
    parallel_labels: int = 1
    sqlite_wal: bool = True
    checkpoint_db: str = ""  # defaults to <output_dir>/<graph_name>-migration-checkpoint.db
    audit_log: str = ""  # defaults to <graph_name>-migration-audit.jsonl


@dataclass
class AssessmentConfig:
    """Configuration for the pre-migration assessor."""

    source: SourceConfig = field(default_factory=lambda: SourceConfig(gremlin_url="", cql_host=""))
    target_cql_host: str | None = None
    target_cql_port: int = 9042
    target_cql_username: str = "cassandra"
    target_cql_password: str = "cassandra"  # noqa: S105
    output_dir: str = "."
    output_formats: list[str] = field(default_factory=lambda: ["all"])
    filter_category: str = "all"
    filter_severity: str = "all"
