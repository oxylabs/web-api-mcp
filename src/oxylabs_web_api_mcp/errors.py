"""Errors whose message is written for the agent that reads it."""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError


class ApiError(ToolError):
    """Raised with a message an agent can act on rather than a raw stack trace.

    A `ToolError` so FastMCP forwards the message to the client verbatim instead of
    masking it as an internal error.
    """


class ApiAuthError(ApiError):
    """The API refused the key (401): nothing but a different key helps."""


class ApiTimeout(ApiError):
    """The API did not answer in time."""


class ApiNotFound(ApiError):
    """The API answered 404: an endpoint it does not have, or a request id it does not know."""


class ApiFaulted(ApiError):
    """The request ran and failed. `tag` names why, e.g. `PARSE_FAILED`; `params` echoes it."""

    def __init__(self, message: str, tag: str = "", params: Any = None) -> None:
        super().__init__(message)
        self.tag = tag
        self.params = params
