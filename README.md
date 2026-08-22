# Oxylabs Web API — MCP Server

A self-hostable [Model Context Protocol](https://modelcontextprotocol.io) server that gives
any MCP-capable agent live web access through the Oxylabs Web API.

| Tool | What it does |
|---|---|
| `search` | Search the live web, returns ranked organic results (title, description, URL) |
| `scrape` | Fetch and parse a single URL, including JS-heavy and bot-protected pages |
| `list_scrapers` | List the scrape endpoints the API implements |

Full API documentation: **[Oxylabs Web API docs](https://github.com/nedasvi/project-search-docs)**

## Requirements

- Python 3.10+
- An Oxylabs Web API key

## Install

```bash
git clone https://github.com/nedasvi/project-search-mcp.git
cd project-search-mcp
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

## Development

```bash
pip install -e '.[dev]'
python tests/test_server.py    # offline checks: error parsing + input validation
```

## License

MIT
