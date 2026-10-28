# Relationship labels

Arrange reads each relationship's `displayName` and, between layouts that are otherwise equal, picks the one where the portal does not draw two labels on top of each other.

## How it is shown

- On `anbotrai-stability` without labels, labels change nothing: none of its 16 relationships has a `displayName`, so arrange is identical with and without them.
- Two temporary crossed links make the difference: `demo-generation-interface` (AI generation to the interface's container app, label `sends drafts to`) and `demo-interface-generation` (Chat interface to the generation AI service, label `renders results of`).
- The prepared layout is what arrange picks when it ignores labels (`offline.py`, labels `{}`): both runs have the same middle, so `sends drafts to` is hidden under `renders results of`.
- Arrange with labels swaps the two service cards, the labels land 250 px apart, and both are readable. Only 2 of 17 cards move.

## Commands

```bash
F=docs/arrange-showcase/features/labels; S=docs/arrange-showcase/scripts/showcase.sh
(cd src/healthmodels-plus && uv run --locked python ../../$F/offline.py)   # writes prepare.json, expected.json
$S lock anbotrai-stability feature-labels
$F/topology.sh add
$S import anbotrai-stability $F/prepare.json        # 01-prepared-designer.png
$S arrange anbotrai-stability labels-arrange        # 02-arranged-designer.png
$F/topology.sh remove && $F/topology.sh verify
$S restore anbotrai-stability && $S unlock anbotrai-stability feature-labels
```

`topology.sh snapshot` wrote `entities.before.json` and `relationships.before.json` before anything was added; `verify` diffs against them.

## Screenshots

- Prepared, one label hidden: [01-prepared-designer.png](01-prepared-designer.png), zoom [01-prepared-designer-zoom.png](01-prepared-designer-zoom.png)
- Arranged, both labels readable: [02-arranged-designer.png](02-arranged-designer.png), zoom [02-arranged-designer-zoom.png](02-arranged-designer-zoom.png)

## Proof

- `offline.py`: `prepare: 0 edges through cards, 0 false links, 0 crossings, 1 label clashes`, `expected: ... 0 label clashes`.
- [logs/anbotrai-stability.labels-arrange.log](../../logs/anbotrai-stability.labels-arrange.log): `Local search kept 1 of 26 swaps (limit 300), score 18 -> 8`. The 10 points are exactly one label clash (`cost` weighs a clash 10); nothing else differs.
- Same log: `2 of 17 positions change`. `generation` moved 1125 to 1375, `interface` 1375 to 1125. The live export after arrange equals `expected.json`.
- After removal: `entities and relationships match the snapshot`; the restored layout equals `layouts/anbotrai-stability.mangled.json`.

## Verdict

- Clear before/after: a label that is invisible becomes readable, and the log ties the swap to the clash count alone.
- Weak: the topology is artificial, and in both drawings the four relationships of the two services still share one horizontal line, so who links to whom is only readable from the labels' positions, not from the lines.
