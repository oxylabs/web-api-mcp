"""Self-hostable MCP server exposing the Oxylabs Web API to agents.

Built on FastMCP.

Transports:
    stdio   (default) — local clients: Claude Code, Claude Desktop, Cursor
    http              — self-hosting behind your own network and auth

Run:
    OXYLABS_WEB_API_KEY=... oxylabs-web-api-mcp
    OXYLABS_WEB_API_KEY=... oxylabs-web-api-mcp --transport http --port 8080

Over HTTP the key can also arrive per request as `Authorization: Bearer <key>`, so one
deployment can serve several callers on their own keys.
"""

from __future__ import annotations

import argparse
import os

from oxylabs_web_api_mcp import (  # noqa: F401 — skill and tools register on `mcp` at import
    config,
    skill,
    tools,
)
from oxylabs_web_api_mcp.app import mcp


def _allowlist(name: str, default: str = "") -> list[str] | None:
    """Parse a comma-separated allowlist env var. None when unset, so FastMCP's own
    same-origin fallback applies rather than an empty list rejecting everything."""
    raw = os.environ.get(name, default)
    entries = [item.strip() for item in raw.split(",") if item.strip()]
    return entries or None


def main() -> None:
    parser = argparse.ArgumentParser(prog="oxylabs-web-api-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for local clients, http when self-hosting (default: stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    # Offloading to disk is only useful when the agent can open the file, i.e. same host.
    config.SPILL_ENABLED = args.transport == "stdio" and os.environ.get("OXYLABS_SPILL", "1") != "0"

    if args.transport == "stdio":
        mcp.run("stdio", show_banner=False)
    else:
        # FastMCP leaves DNS-rebinding protection off by default; a server reachable over
        # the network turns it on and declares the hostnames it answers for.
        mcp.run(
            "http",
            host=args.host,
            port=args.port,
            host_origin_protection=True,
            allowed_hosts=_allowlist("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*"),
            allowed_origins=_allowlist("MCP_ALLOWED_ORIGINS"),
        )


if __name__ == "__main__":
    main()
