# anbotrai-platform baseline arrange sweep

## Portal Arrange
- Arrange only moves cards in the open designer. Save was needed: the toolbar showed Save/Discard enabled after Arrange, and the positions were exported from the model only after clicking Save.
- After Save: 17 of 17 entities have positions in the exported JSON (`layouts/anbotrai-platform.portal-arrange.json`).
- Graph view shows the same arranged tree after a reload.

## Portal Arrange vs `auto`
- Both are a three-row tree (root, `plan-trip`, six services, nine resources), column pitch 250 px, x from 0 to 2000.
- Row pitch: portal 193 px (y 0, 193, 386, 579 to 597), `auto` 192 px (y 0, 192, 384, 576).
- Portal rows are not aligned: resource row has y 579, 589 and 597 (`destinations-mcp-resource` 579, `interface-resource` 597). `auto` has one y per row.
- Canvas: portal 2000 x 597 (origin of cards), `auto` 2000 x 576. Same width, `auto` 21 px shorter.
- Root x: portal 625, `auto` 1250 (centered over the children).
- Order left to right (services): portal preferences, itinerary, interface, generation, destinations, assistant. `auto` is the reverse: assistant, destinations, generation, interface, itinerary, preferences.
- Portal service spacing is uneven (destinations at 1125, assistant at 1750). `auto` spacing is uneven too, to fit children below.
- Resources: portal puts them leftmost-first under preferences, `auto` starts with the assistant resources.

## Engines
- `auto`, `layered`, `tree`, `network` and `keep-order` gave identical positions (jq `-S` sorted by name, `diff` clean for every pair).
- `auto` log: layered and tree both score 6 with 0 edges through cards, 0 false links, 0 crossings, 0 label clashes; it keeps layered (first of equal scores).
- Every engine: 17 of 17 positions change, part is 2200 x 706 px.

## Restore
- Restored and verified after the portal Arrange and after each engine (`jq -S 'sort_by(.name)'` diff against `layouts/anbotrai-platform.mangled.json` clean).
- `03-restored-designer.png` shows the single stacked column again.

## What went wrong
- Navigating from one portal URL to the other (hash change only) shows the previous layout. First `04-auto-designer.png` was a stale mangled stack and was retaken. Fix: navigate to `about:blank` first, then to the portal URL.
- `browser_wait_for` on card text (`Plan a trip`) timed out because card text is not matched by the wait; used a fixed 12 to 14 s wait instead.
- Role lookup of the Arrange button failed; clicked it by coordinates (after collapsing the menu it sits at about 906,162).
