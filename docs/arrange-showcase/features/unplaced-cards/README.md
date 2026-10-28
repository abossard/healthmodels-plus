# Unplaced cards with keep-order

`arrange --engine keep-order` puts cards without a canvas position under their own parents and moves no card that already has a position.

## How it is shown

- Model `anbotrai-experience`. `prepare.json` is a tidy keep-order layout with the services in user-journey order (Chat interface, Customer preferences, Destinations, Itinerary, AI generation, Assistant), not the alphabetical order `tree` would pick. That layout has 0 false links.
- Three resource positions are cleared with `x`/`y` null: `interface-resource` (only child), `destinations-mcp-resource` (second of two), `assistant-llamaindex-resource` (middle of three). The portal piles them in the left column, one on top of Chat interface.
- `arrange --engine keep-order` then writes only those three cards. Each lands under its own parent, the llamaindex card between its langchain and python siblings.
- The result equals the layout before the positions were cleared, card for card (checked offline and live).

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
$S lock anbotrai-experience feature-unplaced
$S import anbotrai-experience docs/arrange-showcase/features/unplaced-cards/prepare.json
$S arrange anbotrai-experience unplaced-keep-order --engine keep-order
$S restore anbotrai-experience
$S unlock anbotrai-experience feature-unplaced
```

## Screenshots

- [01-prepared-designer.png](01-prepared-designer.png): 14 placed cards, the 3 unplaced ones stacked in the left column with lines running back across the canvas.
- [02-keep-order-designer.png](02-keep-order-designer.png): after keep-order, the 3 cards sit under their parents, every other card is unchanged and no lines merge.

## Proof

From `logs/anbotrai-experience.unplaced-keep-order.log`:

- `Loaded 17 entities (14 with a canvas position) and 16 relationships`
- `Keep-order: 14 cards keep their drawn left-to-right order, 3 without a position go beside their placed neighbours or last in their row`
- `Part 1 of 1 is 2200 x 706 px with 0 edges through cards, 0 false links, 0 crossings, 0 label clashes`
- `3 of 17 positions change; saving them`: only the unplaced cards moved. Saved: `interface-resource` (0, 576), `destinations-mcp-resource` (750, 576), `assistant-llamaindex-resource` (1750, 576).
- Export after arrange equals the uncleared layout for all 17 entities.

## Verdict

- The cleared-position case shows the feature well: three cards go back exactly where they belong, nothing else moves ("3 of 17 positions change"), and the result has 0 false links.
- Weak points:
  - Parents with several children are not exactly centred: Destinations sits over its second resource, Assistant over its first. No service order fixes this (all 720 checked offline); it is how keep-order places parents.
  - Offline, the "new entity" case is weaker. If the placed cards are packed without gaps, an unplaced card gets its parent's x, and a tie with a placed card of another group is broken by entity name, so it can land in the wrong group. This showcase avoids that case.
