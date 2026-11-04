"""Stand-ins for the API, the HTTP client and the request context, shared by the tests."""

from __future__ import annotations

from oxylabs_web_api_mcp import client, scraping

GROUPS = {
    "scrapers": {
        "json_not_supported": ["/v1/async/scrape/media"],
        "json_supported": [
            "/v1/scrape/amazon/product",
            "/v1/async/scrape/chatgpt",
        ],
        "json_supported_only_with_prompt_or_schema": ["/v1/scrape", "/v1/async/scrape"],
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
    """Stub `_request`: answer each POST from `answers` in turn, and the parser groups."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            self.calls.append((method, path, None))
            return GROUPS
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
        return self

    def __exit__(self, *exc):
        client._request = self._real
        scraping._JSON_GROUPS.clear()
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
