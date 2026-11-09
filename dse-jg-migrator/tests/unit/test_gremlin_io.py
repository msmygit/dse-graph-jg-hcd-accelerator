"""Unit tests for gremlin_io.py — no network: the aiohttp transport and the driver Client are mocked."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from dse_jg_migrator.gremlin_io import _closing_transport, open_gremlin_client, quiet_gremlin_errors

TRANSPORT = "gremlin_python.driver.aiohttp.transport.AiohttpTransport"


def test_failed_connect_closes_transport_and_reraises() -> None:
    """Live finding: an orphaned transport was closed later by GC inside another loop ('Cannot run the event loop')."""
    with patch(TRANSPORT) as transport_cls:
        transport_cls.return_value.connect.side_effect = OSError("refused")
        transport = _closing_transport()
        with pytest.raises(OSError, match="refused"):
            transport.connect("ws://localhost:1/gremlin")
    transport_cls.return_value.close.assert_called_once()


def test_successful_connect_keeps_transport_open() -> None:
    with patch(TRANSPORT) as transport_cls:
        driver_connect = transport_cls.return_value.connect  # replaced on the instance by the wrapper
        transport = _closing_transport()
        transport.connect("ws://h:8182/gremlin", {"h": "v"})
    driver_connect.assert_called_once_with("ws://h:8182/gremlin", {"h": "v"})
    transport_cls.return_value.close.assert_not_called()


def test_open_gremlin_client_uses_closing_transport_and_passes_kwargs() -> None:
    with patch("gremlin_python.driver.client.Client") as client_cls:
        open_gremlin_client("ws://h/gremlin", "food.g", username="u")
    client_cls.assert_called_once_with("ws://h/gremlin", "food.g", transport_factory=_closing_transport, username="u")


def test_quiet_gremlin_errors_mutes_only_inside_and_restores(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("gremlinpython")
    with caplog.at_level(logging.ERROR, logger="gremlinpython"):
        with pytest.raises(RuntimeError), quiet_gremlin_errors():
            log.error("Received error message 'expected'")
            raise RuntimeError("probe failed")
        log.error("Received error message 'real'")
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["Received error message 'real'"]
    assert log.disabled is False
