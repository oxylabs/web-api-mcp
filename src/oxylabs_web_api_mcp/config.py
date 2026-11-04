"""Settings, read once at import from the environment and a project `.env`."""

from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path


def _load_env_file() -> None:
    """Fill unset `OXYLABS_*` variables from a `.env` beside the project.

    A project that keeps its key in `.env` — the shape `.env.example` tells people to
    copy — should just work, without wrapping the launcher in a shell that sources it.
    The real environment always wins, and only `OXYLABS_*` names are read, so an
    unrelated `.env` cannot inject anything else into this process.
    """
    try:
        text = Path(os.environ.get("OXYLABS_ENV_FILE", ".env")).read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        name = name.strip()
        # An empty value counts as unset: an MCP config that interpolates ${OXYLABS_WEB_API_KEY}
        # passes an empty string when the variable is missing, and that must not shadow the file.
        if not sep or not name.startswith("OXYLABS_") or os.environ.get(name, "").strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[name] = value


def _parse_rate_limit(spec: str) -> tuple[int, float] | None:
    """Parse `OXYLABS_RATE_LIMIT`, e.g. "100/1h" — a count and a window in seconds."""
    spec = spec.strip()
    if not spec:
        return None
    match = re.fullmatch(r"(\d+)/(\d+)([smh])", spec)
    if not match:
        raise ValueError(f"OXYLABS_RATE_LIMIT must look like '100/1h' or '50/30m', got {spec!r}")
    count, size, unit = int(match[1]), int(match[2]), match[3]
    return count, size * {"s": 1, "m": 60, "h": 3600}[unit]


_load_env_file()

BASE_URL = os.environ.get("OXYLABS_BASE_URL", "https://webapi.oxylabs.io").rstrip("/")
HTTP_TIMEOUT = 200
POLL_SECONDS = 5

# Token budget, not chars: major clients reject a tool result over 25k tokens.
MAX_INLINE_TOKENS = int(os.environ.get("OXYLABS_MAX_INLINE_TOKENS", "10000"))
READ_CHUNK_CHARS = 40000
PREVIEW_CHARS = 2000

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
TIMEOUT_TITLES = frozenset({"REQUEST_FAILED_SCRAPE_TIMEOUT"})
PERMANENT_FAULTS = frozenset(
    {
        "REQUEST_FAILED_BROWSER_INSTRUCTIONS",
        "REQUEST_FAILED_RESPONSE_TOO_BIG",
        "PARSE_NOT_SUPPORTED",
        "PARSE_FAILED_PRODUCT_NOT_FOUND",
        "PARSE_FAILED_INVALID_PARSING_INSTRUCTIONS",
        "PARSE_FAILED_PRESET_NOT_FOUND",
    }
)
PERMANENT_FAULT_PREFIXES = ("DOWNLOAD_FAILED_",)
RETRIES = int(os.environ.get("OXYLABS_RETRIES", "2"))
RETRY_BASE_DELAY = 1.0
RATE_LIMIT = _parse_rate_limit(os.environ.get("OXYLABS_RATE_LIMIT", ""))

THIN_CONTENT_CHARS = 500
JS_REQUIRED_RE = re.compile(
    r"(enable|turn on|requires?|needs?)\s+(?:\w+\s+){0,3}javascript|javascript\s+is\s+"
    r"(?:required|disabled|turned off)|<noscript",
    re.IGNORECASE,
)
SPILL_TTL_SECONDS = 6 * 3600

# Spilling to disk only helps when the agent shares the filesystem (stdio); main() flips it.
SPILL_ENABLED = False

OUTPUT_PARAM = "output"
JSON_PARAM = "json"

# `GET /v1/scrapers?group_by=json_output_support` sorts every endpoint path into one of these.
JSON_NOT_SUPPORTED = "json_not_supported"
JSON_SUPPORTED = "json_supported"  # dedicated parser, and the AI parser with a prompt or schema
JSON_GROUPS_TTL_SECONDS = 3600

# Endpoint names come from `GET /v1/scrapers` as slash-separated source paths
# (`scrape/amazon/search`). Each segment is validated on its own, so a caller
# cannot walk the path with `..`, a scheme, or a query string.
ENDPOINT_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

try:
    VERSION = _package_version("oxylabs-web-api-mcp")
except PackageNotFoundError:  # running from a checkout that was never installed
    VERSION = "0.0.0+local"
