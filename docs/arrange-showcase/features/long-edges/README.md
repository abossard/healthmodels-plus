# Long edges get their own lane

A relationship that skips a row gets dummy points that follow the portal's step routing, and the point at its middle row keeps the line's whole horizontal run free, so arrange moves cards off the line instead of drawing it through them.

## How it is shown

- One temporary relationship `demo-root-preferences`: the model root -> `preferences` (Customer preferences), skipping the `plan-trip` row.
- Prepared layout ([prepared.json](prepared.json)) is the clean `auto` layout of the original 16 relationships, as a user would have it before adding the new one. The root sits straight above `plan-trip`.
- The portal draws a two-row edge down the parent's column, across at half height, down the child's column. Half height is the middle of the `plan-trip` row, so in the prepared layout the line runs down behind "Plan a trip" and leaves through its right side, reading as `plan-trip -> preferences`.
- After arrange the dummy point in the `plan-trip` row reserves the run's width, and the root moves one half column right: the line now runs down beside "Plan a trip", across above the services and into "Customer preferences".

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
F=docs/arrange-showcase/features/long-edges
$S lock anbotrai-stability feature-long-edges
$F/topology.sh snapshot /tmp/names.before
$F/topology.sh add
$S import anbotrai-stability $F/prepared.json
$S arrange anbotrai-stability long-edges
$F/topology.sh remove
$F/topology.sh snapshot /tmp/names.after   # diff against names.before: identical
$S restore anbotrai-stability               # export diffed against layouts/anbotrai-stability.mangled.json: identical
$S unlock anbotrai-stability feature-long-edges
```

## Screenshots

- Prepared, the root -> preferences line passes behind "Plan a trip": [01-prepared-designer.png](01-prepared-designer.png)
- Prepared, zoomed on the root, "Plan a trip" and "Customer preferences": the line leaves the right side of "Plan a trip": [01b-prepared-zoom.png](01b-prepared-zoom.png)
- Arranged, the line runs in its own lane right of "Plan a trip": [02-arranged-designer.png](02-arranged-designer.png)
- Arranged, same zoom: the line comes straight down from the root, clear of "Plan a trip": [02b-arranged-zoom.png](02b-arranged-zoom.png)

## Numbers

From `describe()` with the 17 relationships, and the arrange log [anbotrai-stability.long-edges.log](../../logs/anbotrai-stability.long-edges.log):

| Layout | Edges through cards | False links | Crossings | Size |
|---|---|---|---|---|
| Prepared | 1 | 0 | 0 | 2200 x 706 |
| Arranged | 0 | 0 | 0 | 2200 x 706 |

- `4 rows by longest path; 1 edges skip rows and get 1 extra points`
- `Auto: layered has 0 edges through cards, 0 false links, 0 crossings, 0 label clashes, score 9`
- `1 of 17 positions change; saving them` (only the root: x 1250 -> 1375)

Candidates tried offline and dropped:

- `plan-trip -> assistant-langchain-resource`: 3 -> 0 edges through cards, but row 2 opens a 2500 px gap and the canvas grows to 4200 px wide, too small to read in fit view.
- `plan-trip -> assistant-python-resource`, `plan-trip -> destinations-ai-resource`: 0 through cards after arrange, but 5 false links and 1 crossing.
- The root -> a resource (skips two rows): no middle row, and the run lands on the `plan-trip` fan-out line (7 to 22 false links).

## Verdict

- Clear before/after: the same line goes from behind a card to a free lane, with zero faults and no size cost.
- Weak: the reserved width is only visible as a 125 px shift of the root, which needs the zoomed crops to read, and the lane is tight (25 px from the card edge); the "whole run reserved" rule shows more dramatically only in layouts that get too wide to read.
