# anbotrai-capacity baseline notes

## Portal Arrange
- Arrange only moves cards in the designer. Nothing is stored until Save. After Save the Save button is disabled and `layout export` returns 17 positions.
- Portal result: canvas 2000 wide (x 0..2000), y max 597, 6 distinct y values: 0, 193, 386, 579, 589, 597.
- Rows 3 and 4 are not aligned: resource cards sit at y 579 (destinations-mcp-resource), 589 (most), 597 (interface-resource). Row pitch is 193, the cards in the last row differ by 8 to 10 px.
- Root at x 625, plan-trip at x 625 (centered over the six services, not at the canvas center).
- Service order left to right: preferences, itinerary, interface, generation, destinations, assistant (reverse of auto).
- Service x gaps are uneven (0, 250, 500, 750, 1125, 1750), resource cards sit under their parent service, with destinations' two children at x 1000 and 1250.

## Engines vs portal
- `auto`, `layered`, `tree`, `network`, `keep-order` gave byte-identical positions (same sorted-JSON hash). `auto` kept `layered`.
- Engine layout: canvas 2000 wide, 4 rows at y 0, 192, 384, 576 (exact pitch 192), root and plan-trip at x 1250.
- Services ordered assistant, destinations, generation, interface, itinerary, preferences. Resource cards in one row at x 0..2000 step 250, all at y 576.
- Reported quality for every engine: 2200 x 706 px, 0 edges through cards, 0 false links, 0 crossings, 0 label clashes. All 17 positions changed each time.
- Portal Arrange vs engines: same canvas width 2000, y max 597 vs 576, 6 vs 4 distinct rows, pitch 193 vs 192, reverse horizontal order, root x 625 vs 1250.

## Process
- Each engine arrange saved directly (no portal Save needed). The portal needed a page reload to show it.
- Restore verified after portal Arrange and after every engine (`jq -S 'sort_by(.name)'` diff empty). Final state matches `anbotrai-capacity.mangled.json`.
- Screenshots 01 to 08 in this folder, designer and graph for each step except 03 (designer only).

## Problems
- First `auto` screenshot attempt failed: my wait looked for card text outside the portal iframe and timed out, and a restore ran in parallel. Redid arrange, screenshots and restore in order.
- The first designer load took over 30 s (navigate timeout), second attempt was fine.
- A stray click on "Collapse all headers" in the left menu happened once. UI-only, no model change.
