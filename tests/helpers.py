"""Stand-ins for the API, the HTTP client and the request context, shared by the tests."""

from __future__ import annotations

from oxylabs_web_api_mcp import client, scraping

GROUPS = {
    "scrapers": {
        "json_not_supported": ["/v1/async/scrape/media", "/v1/async/scrape/batch/media"],
        "json_supported": [
            "/v1/scrape/amazon/product",
            "/v1/async/scrape/chatgpt",
            "/v1/async/scrape/batch/amazon/product",
            "/v1/async/scrape/batch/google/search",
            "/v1/async/scrape/batch/google/trends/explore",
        ],
        "json_supported_only_with_prompt_or_schema": [
            "/v1/scrape",
            "/v1/async/scrape",
            "/v1/async/scrape/batch",
        ],
    }
}

SCHEMA = {
    "properties": {
        "output": {"type": "array", "items": {"enum": ["html", "screenshot", "markdown", "json"]}}
    }
}


class _FakeCtx:
    """Just enough of a FastMCP Context for the bits that log."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


class _headers:
    """Stand in for the headers of an in-flight HTTP request."""

    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        self._real = client._http_headers
        client._http_headers = lambda: self.headers
        return self

    def __exit__(self, *exc):
        client._http_headers = self._real
        return False


class _api:
    """Stub `_request`: answer each POST from `answers` in turn, the parser groups, endpoint
    schemas (from `schemas`, keyed by path, else `SCHEMA`), and queued requests (from
    `collected`, keyed by request id, else still pending; a callable is asked each time)."""

    def __init__(self, *answers, schemas=None, collected=None):
        self.answers = list(answers)
        self.schemas = schemas or {}
        self.collected = collected or {}
        self.calls = []

    async def __call__(self, method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            self.calls.append((method, path, None))
            return GROUPS
        if method == "OPTIONS":
            self.calls.append((method, path, None))
            return self.schemas.get(path.split("?")[0], SCHEMA)
        if method == "GET" and path.startswith("/v1/async/scrape/"):
            self.calls.append((method, path, None))
            answer = self.collected.get(path.rsplit("/", 1)[1], {"state": "pending"})
            answer = answer() if callable(answer) else answer
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.calls.append((method, path, payload))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(payload)
        return answer

    def __enter__(self):
        self._real = client._request
        client._request = self
        scraping._JSON_GROUPS.clear()
        scraping._OUTPUT_FORMATS.clear()
        return self

    def __exit__(self, *exc):
        client._request = self._real
        scraping._JSON_GROUPS.clear()
        scraping._OUTPUT_FORMATS.clear()
        return False

    @property
    def posts(self):
        return [(path, payload) for method, path, payload in self.calls if method == "POST"]


def _pending(payload):
    return {
        "state": "pending",
        "params": payload,
        "metadata": {"request_id": "7500000000000000001"},
    }


def _done(payload, **result):
    return {"state": "done", "params": payload, "results": [result]}


def _stub_client(responder):
    """Swap httpx.AsyncClient for one that answers from `responder(method, url)`."""

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, headers=None):
            return responder(method, url)

    return _Client
