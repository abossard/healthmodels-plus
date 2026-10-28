# Arrange showcase runbook

How to reproduce every screenshot of the showcase. Paths are relative to the repository root.

## One-time setup

1. Create `docs/arrange-showcase/.env` (git ignores it) with `SUBSCRIPTION`, `RESOURCE_GROUP` (where the health models are) and `TENANT` (the tenant domain the portal links use, such as `contoso.onmicrosoft.com`). Every script reads the Azure ids from there.
2. `docs/arrange-showcase/scripts/showcase.sh setup` builds the extension and installs it into `.azext/` (your regular Azure CLI extensions stay untouched).
3. `uv run --no-project --with playwright==1.55.0 python docs/arrange-showcase/scripts/edge-login.py` opens Edge; sign in to the Azure portal once. The profile is kept in `.playwright/edge-profile`.
4. Register the Playwright MCP server for Claude Code in this folder:

   ```bash
   claude mcp add --scope local playwright-edge -- "$PWD/docs/arrange-showcase/scripts/playwright-edge-mcp.sh"
   ```

   Every server gets its own copy of the signed-in profile and runs headless Edge at 1920x1080, so several sessions can take screenshots at once.

## Models

All four health models in `rg-anbotrai` (`anbotrai-platform`, `anbotrai-experience`, `anbotrai-capacity`, `anbotrai-stability`) have the same tree: the model root, `plan-trip`, six services and their resources (17 entities, 16 relationships). Only the root has a canvas position, so the portal stacks every other card in one column. `docs/arrange-showcase/layouts/<model>.mangled.json` is that layout, saved before anything changed.

## Rules

- **One writer per model.** Take the model with `showcase.sh lock <model> <owner>` before you change positions, entities or relationships, and give it back with `showcase.sh unlock <model> <owner>` when the model is restored. `lock` waits up to 9 minutes; call it with a Bash timeout of 600000 ms and call it again if it gives up.
- **Restore before unlock.** `showcase.sh restore <model>` imports the saved layout (nulls clear positions). Then check it: `showcase.sh export <model> /tmp/x.json` must match `layouts/<model>.mangled.json` (`jq -S 'sort_by(.name)'` on both, then `diff`).
- **Topology changes only on `anbotrai-stability`**, only while you hold its lock, named `demo-*`, and removed again before unlock. Keep the exact add and remove commands in a script in your folder.
- Use `jq` for JSON, never `grep` or `sed`.
- If `az` says a `monitor health-models` command is "misspelled or not recognized", run it again.

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
$S save <model>                     # once: export the current layout to layouts/<model>.mangled.json
$S arrange <model> <tag> [az args]  # e.g. $S arrange anbotrai-platform tree --engine tree; log in logs/<model>.<tag>.log
$S arrange <model> <tag> --dry-run  # same, nothing saved
$S export <model> <file> / $S import <model> <file> / $S restore <model>
$S url <model> designer|graph       # portal URL
```

The layout engine is plain Python without Azure calls: `src/healthmodels-plus/azext_healthmodels_plus/layout/layout.py`. `arrange(nodes, edges, engine, current, labels)` and `arrange_below(root, edges, current, engine, labels)` return positions; `describe(nodes, edges, pos, labels)` counts edges through cards, false links, crossings and label clashes of any layout, including a prepared one. Try ideas offline first (from `src/healthmodels-plus`: `uv run --locked python -c '...'`), then go live.

## Screenshots

Use the `playwright-edge` MCP tools (`mcp__playwright-edge__*`), not the built-in browser or Chrome.

1. Navigate to `$S url <model> designer` (or `graph`) and wait until the cards are drawn (about 15 s; `browser_wait_for` a card's text such as `Plan a trip`).
2. Collapse the resource menu on the left (the `«` button right of the menu's Search box) so the canvas is wider.
3. Click the canvas's fit-view control (bottom left of the canvas, the corners icon below `+` and `−`).
4. `browser_take_screenshot` with `type: png` and `filename: docs/arrange-showcase/<your folder>/<name>.png` (relative to the repository root).
5. After any import or arrange, navigate to the page again so the portal reads the new positions.

The designer toolbar has Arrange, Save and Discard. Never Save in the designer unless your task says so; leave it with Discard. If the portal shows a sign-in page, stop and report it. Never type credentials.
