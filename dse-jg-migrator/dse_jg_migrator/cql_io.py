"""Shared CQL cluster construction for the source DSE and target HCD clusters.

Every module opens its CQL connections through :func:`open_cql_cluster`, so authentication and TLS are applied the same
way everywhere (the source migrator and analyser once connected without the credentials they were given).
"""

from __future__ import annotations

import ssl
from typing import Any


def open_cql_cluster(
    host: str,
    port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
    **kwargs: Any,  # noqa: ANN401 — further cassandra-driver Cluster options (e.g. connect_timeout)
) -> Any:  # noqa: ANN401 — cassandra-driver is untyped
    """Return an unconnected ``cassandra.cluster.Cluster`` for ``host:port``; the caller connects and shuts it down.

    With ``username`` it authenticates with ``PlainTextAuthProvider`` (DSE's DseAuthenticator and HCD's password
    authenticator accept it). With ``ssl_context`` it uses TLS and checks the certificate against ``host``.
    """
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster

    if username:
        kwargs["auth_provider"] = PlainTextAuthProvider(username=username, password=password or "")
    if ssl_context is not None:
        kwargs["ssl_context"] = ssl_context
        kwargs["ssl_options"] = {"server_hostname": host}
    return Cluster([host], port=port, **kwargs)
