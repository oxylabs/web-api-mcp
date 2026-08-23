# Oxylabs Web API — MCP Server

A self-hostable [Model Context Protocol](https://modelcontextprotocol.io) server that gives
any MCP-capable agent live web access through the Oxylabs Web API.

| Tool | What it does |
|---|---|
| `search` | Search the live web, returns ranked organic results (title, description, URL) |
| `scrape` | Read a single URL as **Markdown** by default, including JS-heavy and bot-protected pages |
| `read_scraped` | Read a large page that was offloaded to disk, in chunks |
| `list_scrapers` | List the scrape endpoints the API implements |

## Large pages

Web pages routinely exceed what is sensible to hand an agent in one response, so `scrape`
does not return oversized content inline:

- **Running locally (stdio):** the page is written to a temp file. The agent gets the first
  2 000 characters plus a path, and pulls the rest through `read_scraped(path, offset)` —
  reading only as far as it needs instead of paying for the whole page up front.
- **Running remotely (HTTP):** there is no shared filesystem, so a path would be useless.
  The content is truncated with a note stating the full length.

The threshold is `OXYLABS_MAX_INLINE_CHARS` (default 40 000). `read_scraped` can only read
files in the spill directory — it is deliberately not a general file reader.

Full API documentation: **[Oxylabs Web API docs](https://github.com/oxylabs/gitbook-web-api)**

## Requirements

- Python 3.10+
- An Oxylabs Web API key — in the [Oxylabs dashboard](https://dashboard.oxylabs.io), create
  a **Web API** instance and generate a key for it

## Install

```bash
git clone https://github.com/oxylabs/web-api-mcp.git
cd web-api-mcp
pip install .
```

## Use it locally (stdio)

Point your client at the installed command. Claude Code:

```bash
claude mcp add oxylabs-web-api \
  --env OXYLABS_API_KEY=your_api_key_here \
  -- oxylabs-web-api-mcp
```

Claude Desktop / Cursor / any client that reads a JSON config:

```json
{
  "mcpServers": {
    "oxylabs-web-api": {
      "command": "oxylabs-web-api-mcp",
      "env": { "OXYLABS_API_KEY": "your_api_key_here" }
    }
  }
}
```

## Self-host it (HTTP)

The HTTP transport is for running one shared server for a team or for agents that
can't spawn local processes.

```bash
export OXYLABS_API_KEY=your_api_key_here
export MCP_ALLOWED_HOSTS='mcp.internal.example.com,localhost:*'
oxylabs-web-api-mcp --transport streamable-http --host 0.0.0.0 --port 8080
```

The endpoint is then `http://<host>:8080/mcp`.

### Docker

```bash
docker build -t oxylabs-web-api-mcp .
docker run --rm -p 8080:8080 \
  -e OXYLABS_API_KEY=your_api_key_here \
  -e MCP_ALLOWED_HOSTS='localhost:*,mcp.internal.example.com' \
  oxylabs-web-api-mcp
```

> **`MCP_ALLOWED_HOSTS` is not optional.** The MCP SDK enables DNS-rebinding protection
> with an empty allowlist, so a server that doesn't declare its own hostname rejects
> every request with a `Host` header it doesn't recognise. List the hostname clients
> actually connect to. `host:*` matches any port on that host.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OXYLABS_API_KEY` | *(required)* | Web API key, sent as `Authorization: Bearer <key>` |
| `OXYLABS_BASE_URL` | `https://webapi.oxylabs.io` | Override for staging or a proxy |
| `OXYLABS_TIMEOUT` | `120` | Per-request timeout in seconds |
| `OXYLABS_MAX_INLINE_CHARS` | `40000` | Above this, content is offloaded or truncated |
| `OXYLABS_SPILL_DIR` | system temp | Where offloaded pages are written (stdio only) |
| `OXYLABS_SPILL_TTL_HOURS` | `6` | Offloaded pages older than this are pruned on write |
| `OXYLABS_SPILL` | `1` | Set to `0` to keep everything inline even on stdio |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | HTTP transport bind address |
| `MCP_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | Comma-separated `Host` allowlist (HTTP only) |
| `MCP_ALLOWED_ORIGINS` | *(empty)* | Comma-separated `Origin` allowlist (browser clients) |

Copy `.env.example` to `.env` for local use. The key is read from the environment at
request time and is never written to disk or logged.

## Security notes

- **The server holds your API key, so treat its endpoint as privileged.** It performs no
  authentication of its own — anyone who can reach the HTTP endpoint can spend your quota.
  Put it behind your VPN, an ingress with auth, or a service mesh. Don't expose it to the
  public internet.
- `scrape` fetches whatever URL it is given. If you expose this server to untrusted
  prompts, restrict egress at the network layer rather than trusting the caller.
- `read_scraped` is restricted to the spill directory. Don't point `OXYLABS_SPILL_DIR` at a
  directory holding anything else — it would make those files readable by the agent.

## Development

```bash
pip install -e '.[dev]'
python tests/test_server.py    # offline checks: error parsing + input validation
```

## License

MIT
