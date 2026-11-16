"""Fitting a company search reply to the caller's token budget."""

from __future__ import annotations

import logging
from typing import Any

from . import config
from .content import (
    _as_text,
    _longest,
    _request_id,
    _spill,
    _tokens,
    _tokens_within,
)
from .errors import ApiError

logger = logging.getLogger(__name__)

# The reply field that holds the companies.
_KEY = "results"
_ADVICE = "Search again with a narrower `query` or a smaller `max_results`."
_FOLLOW_UP = (
    "For one company in full, search again with its website domain as the `query` "
    "and `max_results=1`."
)

# The least room the companies get, in tokens. A smaller budget is exceeded.
_MIN_ROOM_TOKENS = 100

# The fields each tier keeps, as paths into a profile. Missing or null ones are skipped.
_SUMMARY_PATHS = (
    ("name",),
    ("website_url",),
    ("linkedin_url",),
    ("description",),
    ("industry",),
    ("type",),
    ("founded_year",),
    ("employees_count",),
    ("headquarters", "city"),
    ("headquarters", "country"),
    ("funding", "total_usd"),
    ("revenue_annual_usd",),
)
_IDENTITY_PATHS = (("name",), ("website_url",), ("headquarters", "country"))
# Characters of description a summary keeps.
_DESCRIPTION_KEEP = 200
# Characters kept of an item with none of the fields above.
_UNKNOWN_KEEP = 300


def _pick(item: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
    """`item` reduced to `paths`, keeping its shape."""
    out: dict[str, Any] = {}

    for path in paths:
        value: Any = item
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        target = out
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value

    return out


def _project(item: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    """Keep selected fields, truncating unknown structures when necessary."""
    if not isinstance(item, dict):
        return item

    if picked := _pick(item, paths):
        return picked

    text = _as_text(item)
    if len(text) > _UNKNOWN_KEEP:
        return {"truncated_json": text[:_UNKNOWN_KEEP]}

    return item


def _key_facts(item: Any) -> Any:
    """A company's key facts: no investors, web traffic or full description."""
    out = _project(item, _SUMMARY_PATHS)
    text = out.get("description") if isinstance(out, dict) else None

    if isinstance(text, str):
        text = " ".join(text.split())
        if len(text) > _DESCRIPTION_KEEP:
            text = text[:_DESCRIPTION_KEEP].rstrip() + "…"
        out["description"] = text

    return out


def _identity(item: Any) -> Any:
    """Who a company is: its name, website and country."""
    return _project(item, _IDENTITY_PATHS)


_KEY_FACTS_TIER = (
    _key_facts,
    "Over budget: each company is cut to its key facts, without investors, web traffic "
    "or the full description.",
)
_IDENTITY_TIER = (
    _identity,
    "Over budget: each company is cut to its name, website and country.",
)


_FIRST_CUT = "The first item is itself too long: only the start of it is shown."


def _dropped(shown: int, total: int) -> str:
    return f"Showing the {shown} best of {total}: the rest did not fit in one reply."


def _note(reply: dict[str, Any], tier: str, cut: str = "", spill: str = "") -> str:
    """The reply's note: the API's own, what was cut, then how to get the rest."""
    api_note = _as_text(reply["note"]) if reply.get("note") else ""
    parts = [api_note, tier, cut, _FOLLOW_UP, spill, _ADVICE]

    return " ".join(part for part in parts if part)


def _spill_companies(reply: dict[str, Any], text: str) -> str:
    """Write the full list to disk for `read_result`; return the note saying so, or ""."""
    request_id = _request_id(reply.get("request_id"))

    if not (config.SPILL_ENABLED and request_id):
        return ""

    try:
        # No url: it is a list, not a page.
        _spill(text, "", "json", request_id, 0)
    except (ApiError, OSError, ValueError) as exc:
        # Not fatal: the reply just lacks the way to read the rest.
        logger.warning("Could not write the company list for %s to disk: %s", request_id, exc)
        return ""

    return f"The full list is on disk: read_result('{request_id}', page=0) reads it in parts."


def _available_company_tokens(reply: dict[str, Any], budget: int, spill_note: str) -> int:
    """Calculate the token budget for companies, reserving space for the note."""
    envelope = {k: v for k, v in reply.items() if k not in ("note", _KEY)}
    count = len(reply[_KEY])
    # Reserve space for the largest possible truncation note.
    longest_cut = max(_FIRST_CUT, _dropped(count, count), key=_tokens)
    reserved_note = max(
        (
            _note(reply, tier_note, longest_cut, spill_note)
            for tier_note in (_KEY_FACTS_TIER[1], _IDENTITY_TIER[1])
        ),
        key=_tokens,
    )

    return max(
        budget - _tokens({**envelope, _KEY: []}) - _tokens({"note": reserved_note}),
        _MIN_ROOM_TOKENS,
    )


def _fit_company_identities(
    reply: dict[str, Any], identities: list[Any], room: int, spill_note: str
) -> dict[str, Any]:
    """Keep the best-ranked identities that fit; cut a first one that is too long alone."""
    kept = []

    for item in identities:
        size = _tokens_within(_as_text(item), room - 1)
        if size is None:
            break
        room -= size + 1
        kept.append(item)

    cut = _dropped(len(kept), len(identities))

    if not kept:
        first_text = _as_text(identities[0])
        fits = _longest(
            len(first_text), lambda n: _tokens([{"truncated_json": first_text[:n]}]) <= room
        )
        kept = [{"truncated_json": first_text[: max(1, fits)]}]
        cut = _FIRST_CUT

    return {**reply, _KEY: kept, "note": _note(reply, _IDENTITY_TIER[1], cut, spill_note)}


def _fit_companies(reply: Any, budget: int | None) -> Any:
    """Fit company results to the token budget, prioritizing company count over detail."""
    if budget is None or not isinstance(reply, dict):
        return reply

    items = reply.get(_KEY)
    if not isinstance(items, list) or not items:
        return reply

    text = _as_text(items)
    if _tokens_within(text, budget - _tokens({**reply, _KEY: []})) is not None:
        return reply

    spill_note = _spill_companies(reply, text)
    room = _available_company_tokens(reply, budget, spill_note)

    key_facts = [_KEY_FACTS_TIER[0](item) for item in items]
    if _tokens_within(_as_text(key_facts), room) is not None:
        return {
            **reply,
            _KEY: key_facts,
            "note": _note(reply, _KEY_FACTS_TIER[1], spill=spill_note),
        }

    identities = [_IDENTITY_TIER[0](item) for item in items]
    if _tokens_within(_as_text(identities), room) is not None:
        return {
            **reply,
            _KEY: identities,
            "note": _note(reply, _IDENTITY_TIER[1], spill=spill_note),
        }

    return _fit_company_identities(reply, identities, room, spill_note)
