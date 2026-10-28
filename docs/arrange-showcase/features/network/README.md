# Network engine: own heights for shared children

`--engine network` gives every child that would join an unrelated line its own height, so the portal draws no false links where services share resources.

## How it is shown

- Five temporary `demo-*` relationships on `anbotrai-stability` make services share resources: the assistant calls the itinerary and preferences MCP servers, the itinerary MCP server calls the destinations MCP server, destinations and generation use each other's AI Services account.
- `layered` keeps the resource row straight. The portal draws every line across at the same height, so the lines of assistant, preferences, itinerary and destinations merge into one long line from `api-langchain-js` to `cog-uuj7bntzehqsu (generation)`: it reads as if every one of those services had every one of those resources (27 false links).
- `network` keeps the same columns but drops `mcp-itinerary-planning` one track (32 px) and `mcp-customer-query` and `mcp-destination-recommendation` two tracks (64 px). Each line now runs at its own height and ends at exactly the children of its parents (0 false links).

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
F=docs/arrange-showcase/features/network
$S lock anbotrai-stability feature-network
$F/topology.sh snapshot $F/names.before.json
$F/topology.sh add
$S arrange anbotrai-stability network-layered --engine layered
$S restore anbotrai-stability
$S arrange anbotrai-stability network-network --engine network
$F/topology.sh remove
$F/topology.sh snapshot /tmp/names.after.json && diff $F/names.before.json /tmp/names.after.json
$S restore anbotrai-stability
$S unlock anbotrai-stability feature-network
```

## Screenshots

- `layered`, full canvas: [01-layered-designer.png](01-layered-designer.png)
- `layered`, the merged line: [01b-layered-merged-line.png](01b-layered-merged-line.png)
- `network`, full canvas: [02-network-designer.png](02-network-designer.png)
- `network`, the staggered children: [02b-network-own-heights.png](02b-network-own-heights.png)

## Proof

From `logs/anbotrai-stability.network-layered.log` and `logs/anbotrai-stability.network-network.log` (17 entities, 21 relationships):

| engine | canvas | false links | crossings | edges through cards |
|---|---|---|---|---|
| layered | 2200 x 706 px | 27 | 0 | 0 |
| network | 2200 x 834 px | 0 | 1 | 0 |

- `INFO: Network: own heights for lines that would merge took the canvas height from 706 to 834 px`
- The same numbers came out of `layout.py` offline before going live.
- After `remove`, entity and relationship names matched `names.before.json` (17 entities, 16 relationships), and the exported layout matched `layouts/anbotrai-stability.mangled.json`.

## Verdict

- The before/after reads well in the crops: one long line that links everything becomes four short lines that each end at the right cards, at a cost of 128 px height and one crossing.
- Weak: at full fit-view the 32 px steps are small and the three line heights sit close together, so the difference needs the cropped screenshots; the staggered row also looks untidy, which is the stated trade-off.
