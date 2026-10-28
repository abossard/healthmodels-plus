# Straight columns

Arrange moves every parent back over its children: a single child under its parent in one straight line (`straighten`), a parent centred over several children on the half-column grid (`place`, `GRID_X`).

## How it is shown

- `prepare.json` keeps every card in its row and leaves the resource row exactly where arrange puts it, but moves all 8 parents off their columns (zig-zag step lines, parents over another parent's children). `describe` counts 8 false links in it.
- Arrange with the default engine (`auto`) moves only those 8 parents, so the before and after differ only in parent columns.
- The table compares each parent's x with the centre of its children's x, before and after.

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
F=docs/arrange-showcase/features/straight-columns
$S lock anbotrai-platform feature-columns
$S import anbotrai-platform $F/prepare.json
$S arrange anbotrai-platform columns-auto
$S restore anbotrai-platform
$S unlock anbotrai-platform feature-columns
```

## Screenshots

- Prepared: [01-prepared-designer.png](01-prepared-designer.png)
- Arranged: [02-arranged-designer.png](02-arranged-designer.png)

## Proof

Canvas x of the parent card and the mean x of its children (cards are all 200 wide, so equal x means centred). Offset = parent minus children.

| parent | children | children centre | prepared parent (offset) | arranged parent (offset) |
| --- | --- | --- | --- | --- |
| anbotrai-platform | 1 | 1000 → 1250 | 1500 (+500) | 1250 (0) |
| plan-trip | 6 | 1250 | 1000 (-250) | 1250 (0) |
| assistant | 3 | 250 | 0 (-250) | 500 (+250) |
| destinations | 2 | 875 | 750 (-125) | 875 (0, half-column) |
| generation | 1 | 1250 | 1000 (-250) | 1125 (-125) |
| interface | 1 | 1500 | 1250 (-250) | 1375 (-125) |
| itinerary | 1 | 1750 | 1875 (+125) | 1625 (-125) |
| preferences | 1 | 2000 | 2250 (+250) | 2000 (0) |

From [logs/anbotrai-platform.columns-auto.log](../../logs/anbotrai-platform.columns-auto.log):

```
INFO: Auto: keeping layered, the first of equal scores
INFO: Part 1 of 1 is 2200 x 706 px with 0 edges through cards, 0 false links, 0 crossings, 0 label clashes
INFO: 8 of 17 positions change; saving them
```

- 4 of 8 parents end up exactly centred: the root straight over `plan-trip`, `plan-trip` over six services, `destinations` over two resources on the half-column grid, `preferences` straight over its resource.
- `generation`, `interface` and `itinerary` stay half a column off their single child, and `assistant` sits over its right child instead of the middle one.
- Why: `place` pulls each service towards `plan-trip`, so the service row ends up packed more tightly than the resources below it. `straighten` then visits that row once, left to right, and only moves a card when the gap to its neighbour already fits, so only the last one, `preferences`, gets in line. Offline, the same pass run right to left straightens all four single-child services (still 0 false links, 0 crossings).

## Verdict

- The before and after show the feature clearly (zig-zag lines and 8 false links become a clean tree, with the root and `plan-trip` straight and centred, and `destinations` on the half grid), but they also show its weak spot: on this model only 1 of 4 single-child services is straightened and `assistant` is not centred, so the after screenshot still has visible half-column jogs.
