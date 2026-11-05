"""Shaping page content for the agent: token budgets, offloading, empty-page hints."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastmcp.utilities.types import Image

from . import client, config


def _estimate_tokens(text: str) -> int:
    """Approximate the tokens `text` will cost the client.

    ~4 characters per token holds for Latin scripts and is wildly wrong for CJK, where a
    character is close to a whole token. Splitting by script beats a flat ratio, and this
    only has to be good enough to decide whether a page is handed over whole.
    """
    cjk = sum(
        1
        for char in text
        if "぀" <= char <= "ヿ"
        or "㐀" <= char <= "䶿"
        or "一" <= char <= "鿿"
        or "豈" <= char <= "﫿"
        or "가" <= char <= "힯"
    )
    return cjk + (len(text) - cjk + 3) // 4


def _token_budget() -> int | None:
    """How many tokens of content this caller can take. None means no limit.

    Defaults to `OXYLABS_MAX_INLINE_TOKENS`, which sits under the 25k-token cap the major
    clients put on a single tool result. A client that knows its own limit can say so with
    an `X-MCP-Max-Tokens` header; `0` opts out.
    """
    declared = client._http_headers().get("x-mcp-max-tokens")
    if declared is not None:
        try:
            asked = int(str(declared).strip())
        except ValueError:
            asked = -1
        if asked == 0:
            return None  # explicit opt-out
        if asked > 0:
            return asked

    return config.MAX_INLINE_TOKENS


def _spill_dir() -> Path:
    path = Path(
        os.environ.get("OXYLABS_SPILL_DIR", Path(tempfile.gettempdir()) / "oxylabs-web-api-mcp")
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_spills(directory: Path) -> None:
    """Delete spill files older than the TTL. Cheap enough to run on every write."""
    cutoff = time.time() - config.SPILL_TTL_SECONDS
    for stale in [*directory.glob("*.txt"), *directory.glob("*.png")]:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass  # Another process got there first, or the file is not ours to remove.


def _request_id(value: Any) -> str | None:
    """`value` as a request id, or None when it is not one. Ids come from the API and end up
    in paths and URLs, so only all-digit ones are taken."""
    if value is not None and re.fullmatch(r"[0-9]+", str(value)):
        return str(value)
    return None


def _spill_stem(url: str, request_id: Any = None, index: int = 0) -> str:
    """A spill file's name: the request id and the page's place in its results.

    `7504857924934611969-0` names the first page of that request, so `read_result` finds it
    by the id and page, and two requests for one URL — fetched from another `location`,
    say — never write over each other. A request returns several pages at times (a search
    over a few result pages), hence the index. Without an id the name is a hash of the URL.
    """
    if _request_id(request_id):
        return f"{request_id}-{index}"
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _spill_path(url: str, fmt: str, request_id: Any = None, index: int = 0) -> Path:
    """Where `_spill` writes a page: its `_spill_stem`, suffixed by its format."""
    ext = {"markdown": "md", "json": "json"}.get(fmt, "html")
    return _spill_dir() / f"{_spill_stem(url, request_id, index)}.{ext}.txt"


def _spill(text: str, url: str, fmt: str, request_id: Any = None, index: int = 0) -> Path:
    """Write oversized content to the spill dir and return its path."""
    path = _spill_path(url, fmt, request_id, index)
    _prune_spills(path.parent)
    path.write_text(text, encoding="utf-8")
    return path


def _result_url(result: dict[str, Any]) -> str:
    """The URL actually scraped for this result — it sits in the result's `metadata`."""
    metadata = result.get("metadata")
    return str(metadata.get("url") or "") if isinstance(metadata, dict) else ""


def _offload(
    result: dict[str, Any],
    text: str,
    url: str,
    fmt: str,
    budget: int,
    request_id: Any = None,
    index: int = 0,
) -> None:
    """Replace oversized page text under the format's key with a pointer or a notice.

    Each requested `output` value comes back under its own result key (`markdown`, `html`).
    A page goes to disk only when it has a request id, which is what `read_result` finds
    it by.
    """
    tokens = _estimate_tokens(text)
    request_id = _request_id(request_id)
    if config.SPILL_ENABLED and request_id:
        path = _spill(text, url, fmt, request_id, index)
        shown = min(config.PREVIEW_CHARS, len(text))
        result[fmt] = text[:shown]
        result["content_offloaded"] = {
            "path": str(path),
            "format": fmt,
            "total_chars": len(text),
            "estimated_tokens": tokens,
            "preview_chars": shown,
            "note": (
                f"`{fmt}` above is only the first {shown} characters. Read on with "
                f"read_result('{request_id}', page={index}, offset={shown}), then keep "
                "calling it with the `next_offset` it returns until `eof` is true. Read only "
                "as far as you need — do not pull the whole page in by reflex."
            ),
        }
    else:
        # Token density is roughly uniform within a page, so a proportional cut lands
        # close enough to the budget without counting the whole thing twice.
        keep = max(1, int(len(text) * budget / tokens))
        result[fmt] = text[:keep]
        result["content_truncated"] = {
            "total_chars": len(text),
            "returned_chars": min(keep, len(text)),
            "estimated_tokens": tokens,
            "token_budget": budget,
            "note": (
                f"The page is about {tokens} tokens, over the {budget}-token budget for one "
                "tool result, and this server runs over HTTP so it cannot hand back a file "
                "path to read in chunks. To see the rest: scrape a more specific URL, ask "
                "for the section you need with `scrape(json_prompt=...)`, raise the budget with an "
                "`X-MCP-Max-Tokens` header if your client can take more, or run the server "
                "over stdio, where full pages go to disk and `read_result` walks them."
            ),
        }


def _process_content(payload: dict[str, Any], fmt: str, budget: int | None) -> dict[str, Any]:
    """Offload or truncate page content that would not fit the caller's token budget.

    No reformatting happens here — the API renders the requested format server-side.
    """
    results = payload.get("results")
    if not isinstance(results, list) or budget is None or fmt == "screenshot":
        return payload

    requested_url = str((payload.get("params") or {}).get("url") or "")
    metadata = payload.get("metadata")
    request_id = metadata.get("request_id") if isinstance(metadata, dict) else None
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        content = result.get(fmt)
        if not isinstance(content, str):
            continue
        # A token is never fewer than one byte, so anything this short cannot overflow;
        # skip the scan for the ordinary page.
        if len(content) <= budget:
            continue
        if _estimate_tokens(content) > budget:
            url = _result_url(result) or requested_url
            _offload(result, content, url, fmt, budget, request_id, index)
    return payload


def _pop_screenshots(
    envelope: dict[str, Any], body: dict[str, Any] | None = None
) -> dict[str, Any] | list[Any]:
    """Turn base64 screenshots into image content blocks the agent can actually look at.

    The API returns a PNG as a base64 string under `screenshot`. Left as text it is
    thousands of useless tokens; as an MCP image the model sees the page.
    """
    body = envelope if body is None else body
    images = [image for _, image in _decode_screenshots(body, envelope.get("request_id"))]
    return [envelope, *images] if images else envelope


def _decode_screenshots(
    body: dict[str, Any], request_id: Any = None
) -> list[tuple[dict[str, Any], Image]]:
    """Swap each base64 screenshot in `body` for a short info dict; return (info, image) pairs.

    Locally the PNG is also written to the spill dir so the user can open it.
    """
    shots: list[tuple[dict[str, Any], Image]] = []
    for index, result in enumerate(body.get("results") or []):
        if not isinstance(result, dict) or not isinstance(result.get("screenshot"), str):
            continue
        try:
            png = base64.b64decode(result["screenshot"], validate=True)
        except (ValueError, TypeError):
            continue  # Not what we expected; leave it for the agent to read as text.
        info: dict[str, Any] = {"bytes": len(png), "note": "Returned as an image block."}
        if config.SPILL_ENABLED:
            stem = _spill_stem(_result_url(result) or uuid.uuid4().hex, request_id, index)
            path = _spill_dir() / f"{stem}.png"
            path.write_bytes(png)
            info["path"] = str(path)
        result["screenshot"] = info
        shots.append((info, Image(data=png, format="png")))
    return shots


def _visible_text(content: str, fmt: str) -> str:
    """Roughly what a reader would see. A heuristic for one hint, not a parser."""
    if fmt == "html":
        content = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", content)
        content = re.sub(r"<[^>]+>", " ", content)
    return " ".join(content.split())


def _flag_thin_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Point out a page that came back empty, so the agent knows `run_js` is worth a try.

    A client-side-rendered page fetched without JavaScript returns a shell: a few hundred
    characters of chrome and nothing to read. That is indistinguishable from a genuinely
    short page to anything but the agent, so this flags it rather than retrying — a
    silent retry would double the cost and the wait on every short page.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return payload

    params = payload.get("params") or {}
    fmt = _output_format(params)
    if params.get("run_js"):
        note = (
            "Even rendered with JavaScript, this page returned no readable content: it is "
            "likely behind a login, a paywall or a block. Do not retry with run_js again. "
            "If the site is on a country TLD, one more try with `location` set to that "
            "country may help; otherwise report the page as unreadable, and do not fall "
            "back to your own recollection of what it says."
        )
    else:
        note = (
            "This page returned no readable content, which usually means it renders "
            "client-side. Retry the same call with run_js=True — rendering takes "
            "30-150s, and a render that outlives the call comes back as a request id to "
            "collect with `check_requests`. If the page is "
            "genuinely this short, take it at face value: do not retry twice, and do "
            "not fall back to your own recollection of what the page says."
        )

    for result in results:
        if not isinstance(result, dict):
            continue
        content = result.get(fmt)
        # Offloaded content is, by definition, not thin.
        if not isinstance(content, str) or "content_offloaded" in result:
            continue

        text = _visible_text(content, fmt)
        # The JS notice is checked against the raw content so a <noscript> block counts,
        # and only on a shortish page — a long article *about* JavaScript is not a shell.
        asks_for_js = (
            len(text) < config.THIN_CONTENT_CHARS * 10
            and config.JS_REQUIRED_RE.search(content) is not None
        )
        if len(text) >= config.THIN_CONTENT_CHARS and not asks_for_js:
            continue

        result["content_thin"] = {
            "visible_chars": len(text),
            "reason": "the page says it needs JavaScript" if asks_for_js else "almost no text",
            "note": note,
        }
    return payload


def _trim(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop envelope fields the agent gains nothing from re-reading.

    `params` echoes the request that was just sent, and `metadata` is mostly timestamps.
    The request id stays — it is what support needs to trace a call.
    """
    if not isinstance(payload, dict):
        return payload
    trimmed = {k: v for k, v in payload.items() if k not in ("params", "metadata")}
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("request_id"):
        trimmed["request_id"] = metadata["request_id"]
    return trimmed


def _output_format(params: Any) -> str:
    """The first requested output format, which is the key the content lives under."""
    output = params.get(config.OUTPUT_PARAM) if isinstance(params, dict) else None
    if isinstance(output, list) and output:
        return str(output[0])
    return "markdown"
