"""Shared Gremlin client construction for the source DSE and target JanusGraph servers.

Every module opens its Gremlin clients through :func:`open_gremlin_client`, so connection behaviour is defined once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

_GREMLIN_LOGGER = "gremlinpython"


def _closing_transport() -> Any:  # noqa: ANN401 — gremlin-python's transport classes are untyped
    """Return an ``AiohttpTransport`` that releases its event loop and session when its connect fails.

    gremlin-python orphans the transport of a failed connect; garbage collection later runs its ``close()`` while
    another client's event loop is running, which prints ``RuntimeError: Cannot run the event loop while another
    loop is running`` and "Unclosed client session". Closing it right away, while no loop runs, avoids both.
    """
    from gremlin_python.driver.aiohttp.transport import AiohttpTransport

    transport = AiohttpTransport()
    connect: Callable[..., None] = transport.connect

    def connect_or_close(url: str, headers: dict[str, str] | None = None) -> None:
        try:
            connect(url, headers)
        except BaseException:
            transport.close()
            raise

    transport.connect = connect_or_close
    return transport


def open_gremlin_client(url: str, traversal_source: str, **kwargs: Any) -> Any:  # noqa: ANN401 — untyped driver
    """Open a gremlin-python ``Client``; ``kwargs`` go to ``Client`` (username, password, message_serializer).

    Raises whatever the driver raises when the server cannot be reached; nothing is left to clean up afterwards.
    """
    from gremlin_python.driver import client as gremlin_client

    return gremlin_client.Client(url, traversal_source, transport_factory=_closing_transport, **kwargs)


@contextmanager
def quiet_gremlin_errors() -> Iterator[None]:
    """Silence gremlin-python's own ERROR log for a request that is expected to fail (e.g. a capability probe).

    The driver logs every server error with its full Java stack trace before raising it; callers that handle the
    exception do not need that output. Other requests keep logging normally.
    """
    log = logging.getLogger(_GREMLIN_LOGGER)
    previous = log.disabled
    log.disabled = True
    try:
        yield
    finally:
        log.disabled = previous
