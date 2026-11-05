# Oxylabs Web API — MCP Server

mcp-name: io.oxylabs/web-api-mcp

This repository is a developer-focused, technical reference covering the official [Model Context Protocol](https://modelcontextprotocol.io) server for the Oxylabs Web API. It gives any MCP-capable agent live web access – search, scrape pages as Markdown or JSON, and call dedicated scrapers – through one MCP connection.

| | Remote MCP | Self-hosted MCP |
|---|---|---|
| **Runs on** | Oxylabs infrastructure | Your machine or infrastructure |
| **Setup** | Add the server URL and your API key to your client | Install the server (Python 3.10+ and `uv`), then configure your client |
| **Updates and maintenance** | Managed by Oxylabs | Managed by you |
| **Best for** | The quickest way to give a compatible agent access to Web API | Full control over the environment, or clients that can't connect to remote servers with a custom header |

The tools and their behavior are the same on both options, and the sections on self-hosting cover everything you need to run the server yourself.

| Tool | What it does |
|---|---|
| `search` | Search the live web, returns ranked organic results (title, description, URL) |
| `scrape` | Read a URL, or a list of URLs, as **Markdown** by default, including JS-heavy and bot-protected pages. Ask for a screenshot, or pass `json_prompt` / `json_schema` to pull named fields off the page as JSON |
| `list_scrapers` | List the scrape endpoints the API implements, grouped by parser support, or describe one's parameters |
| `run_scraper` | Run a dedicated scrape endpoint with its own parameters, as content or as structured JSON; a list of `url`s or `query`s runs them all |
| `check_requests` | Collect requests the API is running in the background, waiting for them to finish |
| `read_result` | Read a page too long for one reply in parts, by its request id |

All six are annotated `readOnlyHint` — nothing here writes anything, so clients can run
them without prompting.

## Requirements

- **An Oxylabs Web API key** — in the [Oxylabs dashboard](https://dashboard.oxylabs.io), create a **Web API** instance, open it, and copy the API key shown at the top of the instance page.
- **For Remote MCP** – an MCP client that supports remote (HTTP) MCP servers with custom headers, such as Claude Code, Codex, or Cursor.
- **For self-hosting** – Python 3.10+ (built on [FastMCP](https://gofastmcp.com), installed with the package) and [`uv`](https://docs.astral.sh/uv/getting-started/installation/).

## Connect to the Remote MCP server

The server is hosted and maintained by Oxylabs. There is nothing to install or update.

```
https://mcp.oxylabs.io/web-api-mcp
```

The server authenticates each request with your Web API key, sent as an `Authorization: Bearer <key>` header. Set the key as an environment variable in the terminal where you run your client:

```bash
export OXYLABS_WEB_API_KEY="your_api_key_here"
```

Claude Code:

```bash
claude mcp add --transport http --scope user oxylabs-web-api \
  https://mcp.oxylabs.io/web-api-mcp \
  --header "Authorization: Bearer ${OXYLABS_WEB_API_KEY}"
```

Codex:

```bash
codex mcp add oxylabs-web-api \
  --url https://mcp.oxylabs.io/web-api-mcp \
  --bearer-token-env-var OXYLABS_WEB_API_KEY
```

Cursor — one click, then make the key visible to Cursor (below):

[![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=oxylabs-web-api&config=eyJ1cmwiOiJodHRwczovL21jcC5veHlsYWJzLmlvL3dlYi1hcGktbWNwIiwiaGVhZGVycyI6eyJBdXRob3JpemF0aW9uIjoiQmVhcmVyICR7ZW52Ok9YWUxBQlNfV0VCX0FQSV9LRVl9In19)

Cursor reads `${env:OXYLABS_WEB_API_KEY}` from its own environment. On macOS, Cursor opened from the Dock or Spotlight does not see variables exported in your shell profile, so either start it from a terminal where the key is exported, or replace `${env:OXYLABS_WEB_API_KEY}` with the key itself in **Cursor Settings → Tools & MCP** (or in `~/.cursor/mcp.json`). If Cursor can't see the variable, it sends an empty key: the server still connects and lists its tools, but every call fails with an error saying the Web API key is missing.

Cursor (manual, `~/.cursor/mcp.json`) and other clients supporting custom headers:

```json
{
  "mcpServers": {
    "oxylabs-web-api": {
      "url": "https://mcp.oxylabs.io/web-api-mcp",
      "headers": {
        "Authorization": "Bearer ${env:OXYLABS_WEB_API_KEY}"
      }
    }
  }
}
```

Clients that only support OAuth sign-in for remote servers, such as custom connectors in the claude.ai web interface, can't send custom headers and can't connect to the Remote MCP server — use the self-hosted server with those clients instead.

## Self-hosting

Run the server yourself when you want full control over where it runs and how it is configured. You install, host, and update it. The Remote MCP server and a self-hosted server use the same server name (`oxylabs-web-api`) in the examples, so if you switch from one to the other, remove the existing server from your client first.

### Install

```bash
uv tool install git+https://github.com/oxylabs/web-api-mcp
```

That puts `oxylabs-web-api-mcp` on your `PATH` in its own environment. `pipx install git+https://github.com/oxylabs/web-api-mcp` does the same. Installing into a system Python usually fails — most are marked externally managed and refuse. To work on the server itself:

```bash
git clone https://github.com/oxylabs/web-api-mcp.git
cd web-api-mcp
pip install -e .
```

`server.json` is the [MCP registry](https://github.com/modelcontextprotocol/registry) manifest. It has no `packages` block yet — add one once the server is published somewhere installable, since a registry entry pointing at nothing is worse than no entry.

### Run it locally (stdio)

Point your client at the installed `oxylabs-web-api-mcp` command. Keep your API key in an environment variable or a `.env` file rather than writing it into a client config.

Claude Code:

```bash
claude mcp add oxylabs-web-api \
  --env OXYLABS_WEB_API_KEY='${OXYLABS_WEB_API_KEY}' \
  -- oxylabs-web-api-mcp
```

Claude Desktop — create a `.env` file containing `OXYLABS_WEB_API_KEY=your_api_key_here`, keep it outside any shared or version-controlled folder, and point the server at it:

```json
{
  "mcpServers": {
    "oxylabs-web-api": {
      "command": "oxylabs-web-api-mcp",
      "env": { "OXYLABS_ENV_FILE": "/absolute/path/to/.env" }
    }
  }
}
```

Cursor:

```json
{
  "mcpServers": {
    "oxylabs-web-api": {
      "command": "oxylabs-web-api-mcp",
      "env": { "OXYLABS_WEB_API_KEY": "${env:OXYLABS_WEB_API_KEY}" }
    }
  }
}
```

For other clients that read a JSON config, use the same `command` entry and your client's
own way of passing environment variables.

### Run it as a shared server (HTTP)

The HTTP transport is for running one shared server for a team or for agents that
can't spawn local processes.

```bash
export OXYLABS_WEB_API_KEY=your_api_key_here
export MCP_ALLOWED_HOSTS='mcp.internal.example.com,localhost:*'
oxylabs-web-api-mcp --transport http --host 0.0.0.0 --port 8080
```

The endpoint is then `http://<host>:8080/mcp`.

#### Docker

```bash
export OXYLABS_WEB_API_KEY=your_api_key_here
docker build -t oxylabs-web-api-mcp .
docker run --rm -p 8080:8080 \
  -e OXYLABS_WEB_API_KEY \
  -e MCP_ALLOWED_HOSTS='localhost:*,mcp.internal.example.com' \
  oxylabs-web-api-mcp
```

Passing `-e OXYLABS_WEB_API_KEY` with no value makes Docker take the variable from your shell.

> **`MCP_ALLOWED_HOSTS` is not optional.** The HTTP transport turns on DNS-rebinding
> protection, so a server that doesn't declare its own hostname rejects every request
> with a `Host` header it doesn't recognise (`421 Misdirected Request`). List the
> hostname clients actually connect to. `host:*` matches any port on that host.

### Configuration

These variables apply to a server you run yourself. The Remote MCP server's settings are managed by Oxylabs.

| Variable | Default | Purpose |
|---|---|---|
| `OXYLABS_WEB_API_KEY` | *(required on stdio)* | Web API key, sent as `Authorization: Bearer <key>`. Over HTTP a per-request `Authorization: Bearer` header takes precedence |
| `OXYLABS_BASE_URL` | `https://webapi.oxylabs.io` | Override the API base URL, e.g. to go through a proxy |
| `OXYLABS_RETRIES` | `2` | Retries on a transient errors |
| `OXYLABS_RATE_LIMIT` | *(off)* | Cap this server's own spend, e.g. `100/1h`, `50/30m`. Each item of a batch counts as one scrape |
| `OXYLABS_MAX_INLINE_TOKENS` | `10000` | Above this, content is offloaded or truncated |
| `OXYLABS_SPILL_DIR` | system temp | Where offloaded pages are written (stdio only) |
| `OXYLABS_SPILL` | `1` | Set to `0` to keep everything inline even on stdio |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | HTTP transport bind address |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | Comma-separated `Host` allowlist (HTTP only) |
| `MCP_ALLOWED_ORIGINS` | *(empty)* | Comma-separated `Origin` allowlist (browser clients) |

Copy `.env.example` to `.env` for local use. On stdio the server reads that `.env` from its working directory: any `OXYLABS_*` name not already set in the environment is filled from there, so a project that keeps its key in `.env` needs no launcher wrapper. Real environment variables always win, and only `OXYLABS_*` names are read. Point it elsewhere with `OXYLABS_ENV_FILE=/path/to/.env`.

The key is read at request time and is never written to disk or logged.

## The skill ships with the server

Connecting the server is the whole install. The agent skill is bundled in the package and served as an MCP resource and a prompt, so a client that never adds the [web-api-skills](https://github.com/oxylabs/web-api-skills) repo still gets the judgment for using these tools well — search to find and scrape to read, when JavaScript rendering earns its cost, what to do with an empty page, how to cite.

| | |
|---|---|
| `oxylabs://skill/web-api` | The skill itself, as Markdown |
| prompt `web_research` | Takes a `question`, hands the agent the task plus the skill |

`scripts/sync-skill.sh` refreshes the bundled copy from the skills repo — the canonical copy lives there, and the two must not drift.

## Fitting the client's context

Oversized content is measured in **tokens, not characters**: 40 000 characters of English is about 10 000 tokens, but 40 000 characters of Chinese is about 40 000, and Claude Code, Claude Desktop and Cursor all reject a tool result over 25 000. The estimate is script-aware for that reason. The budget is `OXYLABS_MAX_INLINE_TOKENS` (default 10 000), and a client that knows its own limit can override it per request with an `X-MCP-Max-Tokens` header — `0` opts out entirely.

## Sync first, queued only on a timeout

Every scrape goes to the synchronous endpoint first. If it times out, the same request is sent to the corresponding asynchronous endpoint (`/v1/async/...`) and the agent gets a request id to collect. `async/...` endpoints passed to `run_scraper` are always queued.

```json
{ "state": "pending", "request_id": "7504857924934611969", "note": "The synchronous call timed out (…), so the same request was queued instead. Collect it with check_requests(['7504857924934611969']) …" }
```

`check_requests` waits for the request to finish (up to 30 s), reporting progress meanwhile, so the agent makes one call instead of polling in a loop it has no way to pace. The API holds the result, so nothing about a request lives in this process: restarts and extra replicas do not lose it.

### Many pages in one call

`scrape` takes a list of URLs, and `run_scraper` a list of `url`s or `query`s, when every other parameter is the same. Up to five run side by side through the synchronous endpoint; more go to the API's batch endpoint in one request, each item counting against `OXYLABS_RATE_LIMIT`. Either way the call waits for what was queued and answers with an item per URL or query: its state and, for as many as fit the token budget, its result. The rest come as summaries showing as much of each page as there was room for, and anything still running comes as a request id for `check_requests`.

### It says when a page needed rendering

The agent shouldn't have to guess whether an empty page is empty or just unrendered, and it shouldn't pay for a render on every page to find out. So a plain `scrape` that comes back with almost no visible text — or with a "please enable JavaScript" notice — is flagged:

```jsonc
{
  "content": "# Loading…",
  "content_thin": {
    "visible_chars": 9,
    "reason": "almost no text",
    "note": "…renders client-side. Retry the same call with run_js=True…"
  }
}
```

HTML is measured on its text, not its markup, so a 3 KB shell of `<meta>` tags still reads as thin. The flag is a hint, not a retry: rendering is slow and billed, and a genuinely short page would pay for it on every fetch. The threshold is 500 visible characters.

## Structured data

Request structured data and the server selects the parser for you:

- **Dedicated parser** — included at no extra cost. Available on selected target endpoints, it returns that endpoint's predefined structure.
- **AI parser** — billed on top of the scrape. Available on most endpoints, it returns exactly the fields your prompt or schema describes.

To keep costs down, the server tries the dedicated parser first and falls back to the AI parser only when the endpoint has no dedicated parser or the dedicated parse fails. Each result states which parser produced it, and the agent can request the AI parser directly when a dedicated result lacks the fields it needs.

## Large pages

Web pages routinely exceed what is sensible to hand an agent in one response, so `scrape` does not return oversized content inline:

- **Running locally (stdio):** the page is written to a temp file, named by its request id. The agent gets the first 2 000 characters and pulls the rest through `read_result(request_id, page, offset)` — reading only as far as it needs instead of paying for the whole page up front.
- **Running over HTTP (Remote MCP or a self-hosted HTTP server):** there is no shared filesystem. A queued result is read on in parts with the same `read_result`, straight from the API, which keeps it. A synchronous result is not kept by the API, so a page too long for one reply is cut short with a note stating its full length.

The threshold is `OXYLABS_MAX_INLINE_TOKENS` (default 10 000). `read_result` reads only the files this server wrote to the spill directory, found by request id — it is deliberately not a general file reader.

Full API documentation: **[Oxylabs Web API docs](https://developers.oxylabs.io/products/web-api)**

### Security notes

- **Keep keys out of config files.** Use environment variables or a `.env` file outside version control, and don't commit client configs that contain a key.
- **Callers can bring their own key over HTTP.** Send `Authorization: Bearer <key>` with each request and the server uses it for that call, so one deployment serves several callers on their own quota. `OXYLABS_WEB_API_KEY` in the server environment is the fallback when no header arrives.
- **Cap the spend.** `OXYLABS_RATE_LIMIT=100/1h` refuses scrapes past a sliding window, so a runaway agent loop cannot drain the key; each URL or query of a list counts. Off by default. Separately, one call takes at most 50 URLs or queries.
- **If you rely on that fallback, treat the endpoint as privileged.** The server does no authentication of its own — anyone who can reach it spends the key it holds. Put it behind your VPN, an ingress with auth, or a service mesh. Don't expose it to the public internet.
- `scrape` fetches whatever URL it is given. If you expose this server to untrusted prompts, restrict egress at the network layer rather than trusting the caller.
- `read_result` is restricted to the spill directory. Don't point `OXYLABS_SPILL_DIR` at a directory holding anything else — it would make those files readable by the agent.

## Development

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
./scripts/sync-skill.sh        # refresh the bundled skill from web-api-skills
```

The tests are offline: error parsing, input validation, envelope trimming, endpoint-name handling, sync-first scraping with the queued fallback and polling, and the dedicated/AI parser selection. CI runs the same on 3.10, 3.12 and 3.13.

## Learn More

For detailed configuration, advanced usage, and code examples in multiple languages, check the official documentation:

- [Oxylabs MCP documentation](<https://developers.oxylabs.io/ai-workflows/mcp>)
- [Get started with Web API](https://oxylabs.io/products/web-api)
- [Web API documentation](https://developers.oxylabs.io/products/web-api)

## License

MIT

## Contact Us

If you have questions or need support, reach out to us at [support@oxylabs.io](mailto:support@oxylabs.io), or through live chat in the [Oxylabs Dashboard](https://dashboard.oxylabs.io/). For enterprise inquiries, contact your dedicated account manager.
