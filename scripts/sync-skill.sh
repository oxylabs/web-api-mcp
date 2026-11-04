#!/usr/bin/env bash
# Refresh the bundled skill from its canonical home in the web-api-skills repo.
# The MCP server serves this file as a resource and a prompt, so it must not drift.
#
#   ./scripts/sync-skill.sh [SRC]           copy SRC over the bundled skill
#   ./scripts/sync-skill.sh --check [SRC]   exit 1 if the bundled skill differs from SRC
#
# SRC is a path or an https URL. Default: the local web-api-skills checkout next to this repo.
# Edit the skill in web-api-skills, never the bundled copy: the check fails CI otherwise.
set -euo pipefail
check=0
[ "${1:-}" = "--check" ] && { check=1; shift; }
src="${1:-../web-api-skills/skills/oxylabs-web-api/SKILL.md}"
dst="$(cd "$(dirname "$0")/.." && pwd)/src/oxylabs_web_api_mcp/skills/oxylabs-web-api.md"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
case "$src" in
  https://*) curl -fsSL "$src" -o "$tmp" ;;
  *) [ -f "$src" ] || { echo "no skill at $src — pass the path to web-api-skills' SKILL.md"; exit 1; }
     cp "$src" "$tmp" ;;
esac

if [ "$check" = 1 ]; then
  if diff -u "$dst" "$tmp"; then
    echo "bundled skill matches $src"
  else
    echo "::error::bundled skill differs from $src. Make the change in web-api-skills, then run scripts/sync-skill.sh"
    exit 1
  fi
else
  cp "$tmp" "$dst"
  echo "bundled skill updated from $src"
fi
