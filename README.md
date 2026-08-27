# Oxylabs Web API — MCP Server

A self-hostable [Model Context Protocol](https://modelcontextprotocol.io) server that gives
any MCP-capable agent live web access through the Oxylabs Web API.

| Tool | What it does |
|---|---|
| `search` | Search the live web, returns ranked organic results (title, description, URL) |
| `scrape` | Read a single URL as **Markdown** by default, including JS-heavy and bot-protected pages |
| `extract` | Pull named fields off a page as JSON, no selectors. Billed above a scrape, so the user approves each run |
| `check_scrape` | Collect the result of a JavaScript-rendering job |
| `read_scraped` | Read a large page that was offloaded to disk, in chunks |
| `list_scrapers` | List the scrape endpoints the API implements, or describe one's parameters |
| `scrape_target` | Call a target-specific scrape endpoint with its own parameters |

All seven are annotated `readOnlyHint` — nothing here writes anything — so clients can run
them without prompting.

## The skill ships with the server

Connecting the server is the whole install. The agent skill is bundled in the package and
served as an MCP resource and a prompt, so a client that never adds the
[web-api-skills](https://github.com/oxylabs/web-api-skills) repo still gets the judgment
for using these tools well — search to find and scrape to read, when JavaScript rendering
earns its cost, what to do with an empty page, how to cite.

| | |
|---|---|
| `oxylabs://skill/web-api` | The skill itself, as Markdown |
| prompt `web_research` | Takes a `question`, hands the agent the task plus the skill |

`scripts/sync-skill.sh` refreshes the bundled copy from the skills repo — the canonical
copy lives there, and the two must not drift.

## Fitting the client's context

Oversized content is measured in **tokens, not characters**: 40 000 characters of English
is about 10 000 tokens, but 40 000 characters of Chinese is about 40 000, and Claude Code,
Claude Desktop and Cursor all reject a tool result over 25 000. The estimate is script-aware
for that reason. The budget is `OXYLABS_MAX_INLINE_TOKENS` (default 10 000), and a client
that knows its own limit can override it per request with an `X-MCP-Max-Tokens` header —
`0` opts out entirely.

## JavaScript rendering is a job, not a wait

`run_js` pages take 30-150 seconds and routinely outlive a tool call. So `scrape(url,
run_js=True)` (and `extract`, and `scrape_target` with `run_js` in its params) returns a job
id straight away:

```json
{ "job_id": "9f3c1a20b7d4", "status": "running", "url": "https://example.com" }
```

The agent polls `check_scrape(job_id)` — after ~30s, then every ~10s — and does other work
in between. Each reply carries `elapsed_seconds` and says whether the job is still inside
the normal 150s window, so a slow render doesn't read as a stuck one. Results are kept for
`OXYLABS_JOB_TTL_MINUTES` (default 60), long after the job itself has finished.

### It says when a page needed rendering

The agent shouldn't have to guess whether an empty page is empty or just unrendered, and it
shouldn't pay for a render on every page to find out. So a plain `scrape` that comes back
with almost no visible text — or with a "please enable JavaScript" notice — is flagged:

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

HTML is measured on its text, not its markup, so a 3 KB shell of `<meta>` tags still reads
as thin. The flag is a hint, not a retry: rendering is slow and billed, and a genuinely
short page would pay for it on every fetch. The threshold is 500 visible characters.

Jobs live in the server process: they do not survive a restart, and they are not shared
between HTTP replicas. Run one replica, or give it sticky sessions.

## Structured extraction costs extra

`extract(url, prompt)` returns the fields you name as JSON instead of a page to read. The
page is parsed by a model per call, which is billed above a plain `scrape`, so the server
asks the user to approve each run over MCP elicitation. Clients that can't elicit get an
error explaining why rather than a silent charge; `OXYLABS_EXTRACT_APPROVAL=0` waives the
prompt once the user has agreed to the cost.

## Large pages

Web pages routinely exceed what is sensible to hand an agent in one response, so `scrape`
does not return oversized content inline:

- **Running locally (stdio):** the page is written to a temp file. The agent gets the first
  2 000 characters plus a path, and pulls the rest through `read_scraped(path, offset)` —
  reading only as far as it needs instead of paying for the whole page up front.
- **Running remotely (HTTP):** there is no shared filesystem, so a path would be useless.
  The content is truncated with a note stating the full length.

The threshold is `OXYLABS_MAX_INLINE_TOKENS` (default 10 000). `read_scraped` can only read
files in the spill directory — it is deliberately not a general file reader.

Full API documentation: **[Oxylabs Web API docs](https://github.com/oxylabs/gitbook-web-api)**

## Requirements

- Python 3.10+ (built on [FastMCP](https://gofastmcp.com), installed with the package)
- An Oxylabs Web API key — in the [Oxylabs dashboard](https://dashboard.oxylabs.io), create
  a **Web API** instance and generate a key for it

## Install

```bash
uv tool install git+https://github.com/oxylabs/web-api-mcp
```

That puts `oxylabs-web-api-mcp` on your `PATH` in its own environment. `pipx install
git+https://github.com/oxylabs/web-api-mcp` does the same. Installing into a system Python
usually fails — most are marked externally managed and refuse. To work on the server
itself:

```bash
git clone https://github.com/oxylabs/web-api-mcp.git
cd web-api-mcp
pip install -e .
```

`server.json` is the [MCP registry](https://github.com/modelcontextprotocol/registry)
manifest. It has no `packages` block yet — add one once the server is published somewhere
installable, since a registry entry pointing at nothing is worse than no entry.

## Use it locally (stdio)

Point your client at the installed command. Claude Code:

```bash
claude mcp add oxylabs-web-api \
  --env OXYLABS_WEB_API_KEY=your_api_key_here \
  -- oxylabs-web-api-mcp
```

Claude Desktop / Cursor / any client that reads a JSON config:

```json
{
  "mcpServers": {
    "oxylabs-web-api": {
      "command": "oxylabs-web-api-mcp",
      "env": { "OXYLABS_WEB_API_KEY": "your_api_key_here" }
    }
  }
}
```

## Self-host it (HTTP)

The HTTP transport is for running one shared server for a team or for agents that
can't spawn local processes.

```bash
export OXYLABS_WEB_API_KEY=your_api_key_here
export MCP_ALLOWED_HOSTS='mcp.internal.example.com,localhost:*'
oxylabs-web-api-mcp --transport http --host 0.0.0.0 --port 8080
```

The endpoint is then `http://<host>:8080/mcp`.

### Docker

```bash
docker build -t oxylabs-web-api-mcp .
docker run --rm -p 8080:8080 \
  -e OXYLABS_WEB_API_KEY=your_api_key_here \
  -e MCP_ALLOWED_HOSTS='localhost:*,mcp.internal.example.com' \
  oxylabs-web-api-mcp
```

> **`MCP_ALLOWED_HOSTS` is not optional.** The HTTP transport turns on DNS-rebinding
> protection, so a server that doesn't declare its own hostname rejects every request
> with a `Host` header it doesn't recognise (`421 Misdirected Request`). List the
> hostname clients actually connect to. `host:*` matches any port on that host.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OXYLABS_WEB_API_KEY` | *(required on stdio)* | Web API key, sent as `Authorization: Bearer <key>`. Over HTTP a per-request `Authorization: Bearer` header takes precedence |
| `OXYLABS_BASE_URL` | `https://webapi.oxylabs.io` | Override for staging or a proxy |
| `OXYLABS_TIMEOUT` | `120` | Per-request timeout in seconds |
| `OXYLABS_RETRIES` | `2` | Retries on a transient 429/500/502/503/504 |
| `OXYLABS_RATE_LIMIT` | *(off)* | Cap this server's own spend, e.g. `100/1h`, `50/30m` |
| `OXYLABS_JOB_TTL_MINUTES` | `60` | How long a finished job's result stays pollable |
| `OXYLABS_EXTRACT_APPROVAL` | `1` | Set to `0` to skip the user prompt on `extract` |
| `OXYLABS_MAX_INLINE_TOKENS` | `10000` | Above this, content is offloaded or truncated |
| `OXYLABS_SPILL_DIR` | system temp | Where offloaded pages are written (stdio only) |
| `OXYLABS_SPILL` | `1` | Set to `0` to keep everything inline even on stdio |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | HTTP transport bind address |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | Comma-separated `Host` allowlist (HTTP only) |
| `MCP_ALLOWED_ORIGINS` | *(empty)* | Comma-separated `Origin` allowlist (browser clients) |

Copy `.env.example` to `.env` for local use. On stdio the server reads that `.env` from
its working directory: any `OXYLABS_*` name not already set in the environment is filled
from there, so a project that keeps its key in `.env` needs no launcher wrapper. Real environment variables always win, and
only `OXYLABS_*` names are read. Point it elsewhere with `OXYLABS_ENV_FILE=/path/to/.env`.

The key is read at request time and is never written to disk or logged.

## Security notes

- **Callers can bring their own key over HTTP.** Send `Authorization: Bearer <key>` with
  each request and the server uses it for that call, so one deployment serves several
  callers on their own quota. `OXYLABS_WEB_API_KEY` in the server environment is the fallback
  when no header arrives.
- **Cap the spend.** `OXYLABS_RATE_LIMIT=100/1h` refuses tool calls past a sliding window,
  so a runaway agent loop cannot drain the key. Off by default.
- **If you rely on that fallback, treat the endpoint as privileged.** The server does no
  authentication of its own — anyone who can reach it spends the key it holds. Put it
  behind your VPN, an ingress with auth, or a service mesh. Don't expose it to the public
  internet.
- `scrape` fetches whatever URL it is given. If you expose this server to untrusted
  prompts, restrict egress at the network layer rather than trusting the caller.
- `read_scraped` is restricted to the spill directory. Don't point `OXYLABS_SPILL_DIR` at a
  directory holding anything else — it would make those files readable by the agent.

## Development

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest                         # or: python tests/test_server.py
./scripts/sync-skill.sh        # refresh the bundled skill from web-api-skills
```

The tests are offline: error parsing, input validation, envelope trimming, endpoint-name
handling, the job lifecycle and the `extract` approval gate. CI runs the same on 3.10,
3.12 and 3.13.

## License

MIT
