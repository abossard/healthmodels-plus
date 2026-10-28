# anbotrai-experience baseline arrange sweep

- Lock owner `baseline-experience`, released after the last restore.
- Screenshots: `01` mangled, `02` portal arrange, `03` restored (designer only), `04`-`08` auto, layered, tree, network, keep-order. Designer and graph for each except `03`.
- Positions: `docs/arrange-showcase/layouts/anbotrai-experience.{portal-arrange,auto,layered,tree,network,keep-order}.json`.
- Restore verified (`jq -S 'sort_by(.name)'` diff against `mangled.json` empty) after portal arrange and after every engine.

## Portal Arrange

- Arrange alone only moves cards in the designer. Save went from disabled to enabled, nothing is stored until Save.
- After Save the positions were in the model: `layout export` returned 17 entries, and the graph view showed the tree.
- Save button was disabled again right after the click, no progress indicator seen.
- Layout is a plain top-down tree: root, `plan-trip`, six services in one row, resources below their service.

## Portal Arrange vs `auto`

- Canvas x range: both 0 to 2000. Max y: portal 597, auto 576.
- Row pitch: portal 193 (y 0, 193, 386, 579), auto 192 (y 0, 192, 384, 576).
- Portal's bottom row is not aligned: y 579, 589 and 597, depending on card height. `auto` puts all resources on y 576.
- Portal x values are on a 125 grid (0, 250, 500, 625, 750, 1000, 1125, 1250, 1500, 1750, 2000). `auto` uses 0, 250, ..., 2000 plus 875, 1375, 1625.
- Order of the bottom row is reversed: portal `preferences, itinerary, interface, generation, destinations-mcp, destinations-ai, assistant-python, assistant-llamaindex, assistant-langchain`. `auto` starts with `assistant-langchain` and ends with `preferences`.
- Both give a tree with no crossings in the screenshots. `auto` printed 0 edges through cards, 0 false links, 0 crossings, 0 label clashes.

## Engines

- `auto`, `layered`, `tree`, `network`, `keep-order` produced identical positions (same hash of `jq -S 'sort_by(.name)'`).
- `auto` log: it scored `tree` and `layered` equal, kept `layered`.
- Each arrange reported part 1 of 1, 2200 x 706 px, 17 of 17 positions changed.
- On this model (a strict tree) the engines cannot be told apart. Only the portal result differs.

## Problems

- First navigation to the portal timed out and landed on `/auth/login/`. A second attempt worked, no sign-in was needed.
- `browser_wait_for` times out at 5 s here, so a fixed 18 s wait after navigation was used instead.
- The resource menu stays expanded after navigation, so it was collapsed on each load before fit view.
