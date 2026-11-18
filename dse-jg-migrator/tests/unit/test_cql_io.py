"""Unit tests for cql_io.py — no network: cassandra-driver's Cluster is mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cassandra.auth import PlainTextAuthProvider

from dse_jg_migrator.cql_io import open_cql_cluster

DUMMY_PW = "pw"  # a test value, not a credential  # pragma: allowlist secret


def test_anonymous_cluster_has_no_auth_and_no_tls() -> None:
    with patch("cassandra.cluster.Cluster") as cluster_cls:
        open_cql_cluster("dse1", 9042, connect_timeout=10)
    cluster_cls.assert_called_once_with(["dse1"], port=9042, connect_timeout=10)


def test_credentials_become_a_plain_text_auth_provider() -> None:
    with patch("cassandra.cluster.Cluster") as cluster_cls:
        open_cql_cluster("dse1", 9042, username="reader", password=DUMMY_PW)
    auth = cluster_cls.call_args.kwargs["auth_provider"]
    assert isinstance(auth, PlainTextAuthProvider)
    assert (auth.username, auth.password) == ("reader", DUMMY_PW)


def test_tls_checks_the_certificate_against_the_host() -> None:
    ctx = MagicMock()
    with patch("cassandra.cluster.Cluster") as cluster_cls:
        open_cql_cluster("dse1.example", 9142, ssl_context=ctx)
    kwargs = cluster_cls.call_args.kwargs
    assert kwargs["ssl_context"] is ctx
    assert kwargs["ssl_options"] == {"server_hostname": "dse1.example"}
