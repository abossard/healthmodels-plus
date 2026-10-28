#!/usr/bin/env bash
# Playwright MCP on headless Edge with a private copy of the profile that edge-login.py signed in,
# so several sessions can drive the portal at once without sharing a browser.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PROFILE="$(mktemp -d "${TMPDIR:-/tmp}/edge-mcp.XXXXXX")"
trap 'rm -rf "$PROFILE"' EXIT
cp -R "$ROOT/.playwright/edge-profile/." "$PROFILE/"
rm -f "$PROFILE"/Singleton*
npx -y @playwright/mcp@0.0.83 --browser msedge --headless --user-data-dir "$PROFILE" \
  --viewport-size 1920x1080 --output-dir "$ROOT/.playwright/output" "$@"
