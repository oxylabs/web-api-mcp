#!/usr/bin/env bash
# Refresh the bundled skill from its canonical home in the web-api-skills repo.
# The MCP server serves this file as a resource and a prompt, so it must not drift.
set -euo pipefail
src="${1:-../web-api-skills/skills/oxylabs-web-api/SKILL.md}"
[ -f "$src" ] || { echo "no skill at $src — pass the path to web-api-skills' SKILL.md"; exit 1; }
cp "$src" "$(cd "$(dirname "$0")/.." && pwd)/src/oxylabs_web_api_mcp/skills/oxylabs-web-api.md"
echo "bundled skill updated from $src"
