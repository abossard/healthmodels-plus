# Keep the drawn order

`--engine keep-order` tidies a hand-drawn canvas (rows, spacing, centring, columns) and keeps every row's left-to-right order; `layered` lays the same canvas out from scratch and drops that order.

## How it is shown

- `prepare.json` draws the six services in user-journey order (Chat interface, Customer preferences, Destination recommendations, Itinerary planning, AI generation, Assistant coordination), with uneven gaps, five different heights and crooked resources. Two resource rows also carry a deliberate order: destinations `mcp` before `ai`, assistant `python`, `langchain`, `llamaindex`.
- `keep-order` from that layout: same order in every row, now on one grid.
- `layered` from the same layout: the tidy result, but services and resources come out in entity-name order (assistant first, preferences last).
- Model `anbotrai-platform`, positions only.

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
$S import anbotrai-platform docs/arrange-showcase/features/keep-order/prepare.json
$S arrange anbotrai-platform keep-order-arrange --engine keep-order
$S import anbotrai-platform docs/arrange-showcase/features/keep-order/prepare.json
$S arrange anbotrai-platform keep-order-layered --engine layered
$S restore anbotrai-platform
```

## Screenshots

1. Prepared, journey order, messy: [01-prepared-designer.png](01-prepared-designer.png)
2. `keep-order`, journey order kept, tidy: [02-keep-order-designer.png](02-keep-order-designer.png)
3. `layered` from the same prepared layout, order replaced: [03-layered-designer.png](03-layered-designer.png)

## Proof

Service row, left to right (from `export` after each arrange):

| Layout | Order |
| --- | --- |
| prepared | interface, preferences, destinations, itinerary, generation, assistant |
| keep-order | interface, preferences, destinations, itinerary, generation, assistant |
| layered | assistant, destinations, generation, interface, itinerary, preferences |

Resource rows: keep-order keeps `destinations-mcp, destinations-ai` and `assistant-python, assistant-langchain, assistant-llamaindex`; layered gives `ai, mcp` and `langchain, llamaindex, python`.

`describe()` offline, confirmed by the live logs:

- prepared: 2190 x 990 px, 1 edge through a card (`plan-trip -> preferences` runs through Destination recommendations), services at 5 heights
- keep-order and layered: both 2200 x 706 px, 0 edges through cards, 0 false links, 0 crossings, 0 label clashes

From [the keep-order log](../../logs/anbotrai-platform.keep-order-arrange.log):

```text
INFO: Keep-order: 17 cards keep their drawn left-to-right order, 0 without a position go beside their placed neighbours or last in their row
INFO: Part 1 of 1 is 2200 x 706 px with 0 edges through cards, 0 false links, 0 crossings, 0 label clashes
```

From [the layered log](../../logs/anbotrai-platform.keep-order-layered.log):

```text
INFO: Row order: 0 crossings to start with, 0 at best (after 0 of 12 sweeps)
INFO: Local search kept 0 of 13 swaps (limit 300), score 6 -> 6 (lower is better)
```

## Verdict

- Shows the feature clearly: 02 and 03 are equally tidy with identical scores, and only keep-order still reads as the user journey left to right.
- Weak: the model is a tree, so every order has 0 crossings; layered does not reorder to remove crossings here, it ignores the canvas and falls back to entity-name order. Keep-order also leaves a one-column gap between Customer preferences and Destination recommendations, because the destinations card is straightened over its AI resource instead of centred over both children.
