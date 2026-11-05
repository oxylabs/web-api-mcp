"""The entry point: what it asks of FastMCP."""

from __future__ import annotations

import inspect
import sys

from oxylabs_web_api_mcp import config, server
from oxylabs_web_api_mcp.app import mcp


def test_the_http_transport_starts_with_the_installed_fastmcp(monkeypatch):
    """Every option main() passes must exist in the installed FastMCP: one it lacks crashes
    the server at startup, which no tool test would notice."""
    calls = []
    monkeypatch.setattr(mcp, "run", lambda transport, **kwargs: calls.append((transport, kwargs)))
    monkeypatch.setattr(sys, "argv", ["oxylabs-web-api-mcp", "--transport", "http"])
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    server.main()
    ((transport, kwargs),) = calls
    assert transport == "http" and kwargs["host_origin_protection"] is True
    inspect.signature(mcp.run_http_async).bind(**kwargs)  # a TypeError names any unknown option
