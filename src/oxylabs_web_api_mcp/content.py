"""Shaping page content for the agent: token budgets, offloading, empty-page hints."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastmcp.utilities.types import Image

from . import client, config
from .errors import ApiError

# The keys a result carries readable page content under, one per requested `output`.
_TEXT_FORMATS = ("markdown", "html", "json")
# Room for the note a cut page carries, saying how much was shown and how to get the rest.
_CUT_NOTE_TOKENS = 200
# Everything this server writes to the spill directory, and so everything it may delete.
_SPILL_NAME_RE = re.compile(r"(?:\d+-\d+|[0-9a-f]{16})\.(?:(?:md|json|html)\.txt|png)")


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
        or "豈" <= char <= "﫿"
        or "가" <= char <= "힯"
    )
    return cjk + (len(text) - cjk + 3) // 4


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _tokens(value: Any) -> int:
    """What `value` costs in a reply, as JSON."""
    return _estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))


def _tokens_within(text: str, budget: int) -> int | None:
    """`text`'s estimated tokens, or None when that is over `budget`.

    A token covers at most about four characters, so a text over four times the budget is
    turned down by its length alone, without scanning every character.
    """
    if (len(text) + 3) // 4 > budget:
        return None
    tokens = _estimate_tokens(text)
    return tokens if tokens <= budget else None


def _longest(limit: int, fits: Callable[[int], bool]) -> int:
    """The largest length up to `limit` that `fits`, given that every shorter one fits too."""
    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _prefix_within(text: str, budget: int) -> int:
    """The length of the longest start of `text` that costs at most `budget` in a reply."""
    if _tokens([text]) <= budget:
        return len(text)
    return _longest(
        min(len(text), 4 * budget + 4), lambda length: _tokens([text[:length]]) <= budget
    )


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
    """Where pages and screenshots are written for the agent to open: a directory of this
    user's own.

    The default is a per-user directory in the system temp dir, created private (0700): on
    a shared machine another user must neither read what was scraped nor plant a link in
    it for a write to follow. A directory owned by someone else, or a symlink, is refused.
    """
    configured = os.environ.get("OXYLABS_SPILL_DIR", "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        uid = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
        path = Path(tempfile.gettempdir()) / f"oxylabs-web-api-mcp{uid}"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if hasattr(os, "getuid"):  # Windows keeps a temp dir per user already
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
            ):
                raise ApiError(
                    f"Refusing to write to {path}: it is not a directory of this user's own. "
                    "Set OXYLABS_SPILL_DIR to one that is."
                )
            if not configured and info.st_mode & 0o077:
                os.chmod(path, 0o700)
    except OSError as exc:
        raise ApiError(f"Cannot use the spill directory {path}: {exc}") from exc
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path`, readable by this user only and never through a symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ApiError(f"Could not write {path.name} to the spill directory: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _prune_spills(directory: Path) -> None:
    """Delete this server's own spill files older than the TTL. Cheap enough to run on every
    write.

    Only the names this server writes are considered, so anything else that shares the
    directory is left alone.
    """
    cutoff = time.time() - config.SPILL_TTL_SECONDS
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not _SPILL_NAME_RE.fullmatch(entry.name):
            continue
        try:
            info = entry.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass  # Another process got there first.


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
    _write_private(path, text.encode("utf-8"))
    return path


def _result_url(result: dict[str, Any]) -> str:
    """The URL actually scraped for this result — it sits in the result's `metadata`."""
    metadata = result.get("metadata")
    return str(metadata.get("url") or "") if isinstance(metadata, dict) else ""


def _skeleton(results: list[Any], keys: tuple[str, ...]) -> list[Any]:
    """The results with their page content emptied: what a reply costs besides the pages.

    A screenshot leaves too, since it goes out as an image block rather than as text.
    """
    skeleton = []
    for result in results:
        if isinstance(result, dict):
            result = {
                key: ("" if key in keys or key == "screenshot" else value)
                for key, value in result.items()
            }
        skeleton.append(result)
    return skeleton


def _cut(
    result: dict[str, Any],
    key: str,
    text: str,
    url: str,
    share: int,
    budget: int,
    request_id: str | None,
    index: int,
) -> int:
    """Cut the page content under `key` down to `share` tokens; return the tokens it kept.

    With a request id, and locally, the whole page goes to disk and the reply keeps its
    start, so `read_result` can walk the rest. Otherwise the page is cut short with a note
    on how to get more. The note goes under `content_offloaded` or `content_truncated`, or
    `<format>_truncated` for a second format of the same result.
    """
    tokens = _estimate_tokens(text)
    if config.SPILL_ENABLED and request_id:
        path = _spill(text, url, key, request_id, index)
        shown = _prefix_within(text[: config.PREVIEW_CHARS], share)
        result[key] = text[:shown]
        result["content_offloaded"] = {
            "path": str(path),
            "format": key,
            "total_chars": len(text),
            "estimated_tokens": tokens,
            "preview_chars": shown,
            "note": (
                f"`{key}` above is only the first {shown} characters. Read on with "
                f"read_result('{request_id}', page={index}, offset={shown}), then keep "
                "calling it with the `next_offset` it returns until `eof` is true. Read only "
                "as far as you need — do not pull the whole page in by reflex."
            ),
        }
        return _tokens(text[:shown])

    keep = _prefix_within(text, share)
    result[key] = text[:keep]
    if config.SPILL_ENABLED:
        more = (
            "This page cannot be read on in parts, so to see the rest: scrape a more specific "
            "URL, ask for the section you need with `scrape(json_prompt=...)`, request this "
            "format on its own, or raise the budget with an `X-MCP-Max-Tokens` header if your "
            "client can take more."
        )
    else:
        more = (
            "This server runs over HTTP, so it cannot hand back a file path to read in chunks. "
            "To see the rest: scrape a more specific URL, ask for the section you need with "
            "`scrape(json_prompt=...)`, raise the budget with an `X-MCP-Max-Tokens` header if "
            "your client can take more, or run the server over stdio, where full pages go to "
            "disk and `read_result` walks them."
        )
    note_key = "content_truncated" if "content_truncated" not in result else f"{key}_truncated"
    result[note_key] = {
        "format": key,
        "total_chars": len(text),
        "returned_chars": keep,
        "estimated_tokens": tokens,
        "token_budget": budget,
        "note": (
            f"The page is about {tokens} tokens, more than the {budget}-token budget of one "
            f"tool result had room for, so `{key}` above is only its first {keep} characters. "
            + more
        ),
    }
    return _tokens(text[:keep])


def _process_content(payload: dict[str, Any], fmt: str, budget: int | None) -> dict[str, Any]:
    """Fit the page content in `payload` to the caller's token budget, as one reply.

    Every page of the result counts, and every format it came in, parsed JSON included,
    beside the rest of the reply. Pages go out whole, in order, while they fit; the rest
    share what is left, each cut to its share. `fmt` is the format the request asked for
    first, the one a cut page can be read on in. No reformatting happens here — the API
    renders the requested format server-side.
    """
    results = payload.get("results")
    if not isinstance(results, list) or budget is None:
        return payload
    keys = tuple(dict.fromkeys(key for key in (fmt, *_TEXT_FORMATS) if key in _TEXT_FORMATS))
    parts = [
        (index, result, key, result[key])
        for index, result in enumerate(results)
        if isinstance(result, dict)
        for key in keys
        if result.get(key) not in (None, "")
    ]
    if not parts:
        return payload

    # What the reply costs besides its pages, as it will go out: trimmed, the id lifted.
    envelope = {k: v for k, v in payload.items() if k not in ("params", "metadata", "results")}
    skeleton = {**envelope, "results": _skeleton(results, keys), "request_id": "0" * 19}
    left = budget - _tokens(skeleton) - config.REPLY_RESERVE_TOKENS
    texts: dict[int, str] = {}
    cut = []
    for n, (_, _, _, value) in enumerate(parts):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        texts[n] = value if isinstance(value, str) else encoded
        tokens = _tokens_within(encoded, left)
        if tokens is not None:
            left -= tokens
        else:
            cut.append(n)
    if not cut:
        return payload

    metadata = payload.get("metadata")
    request_id = _request_id(metadata.get("request_id") if isinstance(metadata, dict) else None)
    requested_url = str((payload.get("params") or {}).get("url") or "")
    left -= len(cut) * _CUT_NOTE_TOKENS
    for k, n in enumerate(cut):
        index, result, key, _ = parts[n]
        share = max(0, left // (len(cut) - k))
        url = _result_url(result) or requested_url
        own = request_id if key == fmt else None  # only the request's own format reads on
        left -= _cut(result, key, texts[n], url, share, budget, own, index)
    return payload


def _pop_screenshots(envelope: dict[str, Any]) -> dict[str, Any] | list[Any]:
    """Turn base64 screenshots into image content blocks the agent can actually look at.

    The API returns a PNG as a base64 string under `screenshot`. Left as text it is
    thousands of useless tokens; as an MCP image the model sees the page.
    """
    images = [image for _, image in _decode_screenshots(envelope, envelope.get("request_id"))]
    return [envelope, *images] if images else envelope


def _png_size(png: bytes) -> tuple[int, int] | None:
    """A PNG's width and height, read from its header; None when it has none."""
    if len(png) >= 24 and png.startswith(b"\x89PNG\r\n\x1a\n") and png[12:16] == b"IHDR":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return None


def _too_big(size: int, dimensions: tuple[int, int] | None) -> str:
    """Why a screenshot is too big to send as an image, or "" when it is not."""
    if size > config.MAX_SCREENSHOT_BYTES:
        return f"at {size / 1_000_000:.1f} MB it is larger"
    if dimensions and max(dimensions) > config.MAX_SCREENSHOT_SIDE_PX:
        return f"at {dimensions[0]}x{dimensions[1]} px it is larger"
    return ""


def _decode_screenshots(
    body: dict[str, Any], request_id: Any = None
) -> list[tuple[dict[str, Any], Image]]:
    """Swap each base64 screenshot in `body` for a short info dict; return (info, image) pairs.

    Locally the PNG is also written to the spill dir so the user can open it. A screenshot
    too big for a model to take as one image is not returned as an image at all: model
    APIs refuse the whole request carrying it, and it would stay in the conversation.
    """
    shots: list[tuple[dict[str, Any], Image]] = []
    pruned = False
    for index, result in enumerate(body.get("results") or []):
        if not isinstance(result, dict) or not isinstance(result.get("screenshot"), str):
            continue
        try:
            png = base64.b64decode(result["screenshot"], validate=True)
        except (ValueError, TypeError):
            continue  # Not what we expected; leave it for the agent to read as text.
        info: dict[str, Any] = {"bytes": len(png), "note": "Returned as an image block."}
        dimensions = _png_size(png)
        if dimensions:
            info["width"], info["height"] = dimensions
        if config.SPILL_ENABLED:
            directory = _spill_dir()
            if not pruned:
                _prune_spills(directory)
                pruned = True
            stem = _spill_stem(_result_url(result) or uuid.uuid4().hex, request_id, index)
            path = directory / f"{stem}.png"
            _write_private(path, png)
            info["path"] = str(path)
        result["screenshot"] = info
        too_big = _too_big(len(png), dimensions)
        if too_big:
            info["note"] = f"Not returned as an image: {too_big} than a model takes in one. " + (
                "Open the file at `path`."
                if "path" in info
                else "Read the page with format='markdown' instead."
            )
            continue
        shots.append((info, Image(data=png, format="png")))
    return shots


def _visible_text(content: str, fmt: str) -> str:
    """Roughly what a reader would see. A heuristic for one hint, not a parser."""
    if fmt == "html":
        content = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", content)
        content = re.sub(r"<[^>]+>", " ", content)
    return " ".join(content.split())


def _target_error(result: dict[str, Any]) -> tuple[int, str] | None:
    """The HTTP error the target answered this page with, as the API recorded it, if any."""
    statuses = (result.get("metadata") or {}).get("statuses")
    status = statuses.get("http_request") if isinstance(statuses, dict) else None
    code = status.get("code") if isinstance(status, dict) else None
    if isinstance(code, int) and code >= 400:
        return code, str(status.get("tag") or f"HTTP {code}")
    return None


def _flag_thin_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Point out a page that came back empty, so the agent knows `run_js` is worth a try.

    A client-side-rendered page fetched without JavaScript returns a shell: a few hundred
    characters of chrome and nothing to read. That is indistinguishable from a genuinely
    short page to anything but the agent, so this flags it rather than retrying — a
    silent retry would double the cost and the wait on every short page. A page the
    target answered with an HTTP error is its error page, and says so instead.
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
        # A page cut down to fit is, by definition, not thin.
        if (
            not isinstance(content, str)
            or "content_offloaded" in result
            or "content_truncated" in result
        ):
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

        error = _target_error(result)
        if error:
            code, tag = error
            result["content_thin"] = {
                "visible_chars": len(text),
                "reason": f"the site answered {tag}",
                "note": (
                    f"The site answered HTTP {code} ({tag}), so this is its error page, not a "
                    "page that needs JavaScript: run_js will not help. Check the URL; if the "
                    "site may block some countries, one more try with `location` can help. "
                    "Otherwise report the page as unavailable, and do not fall back to your "
                    "own recollection of what it says."
                ),
            }
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
