# Lines through cards

Arrange scores every drawing the way the portal renders it, so a layout whose lines run behind cards (and makes unrelated cards look linked) is replaced by one with none.

## How it is shown

- `prepare.json` stacks the six services in two rows of three: `assistant`, `destinations`, `generation` on top, `interface`, `itinerary`, `preferences` straight below them. The portal routes `plan-trip -> interface` down through `Assistant coordination`, so the canvas reads as a five-level tree in which `Chat interface` is a child of `Assistant coordination` (likewise `Itinerary planning` under `Destination recommendations`, `Customer preferences` under `AI generation`).
- The resources sit in one row in a shuffled order, so the service-to-resource lines also run behind the lower service cards, merge into shared lines and cross.
- No topology change: only positions differ between the two screenshots.
- `describe()` (the same `faults` the engines minimise) counts the prepared and the arranged drawing; the `auto` decision log reports the counts per engine.

## Commands

```bash
S=docs/arrange-showcase/scripts/showcase.sh
$S lock anbotrai-capacity feature-through-cards
$S import anbotrai-capacity docs/arrange-showcase/features/through-cards/prepare.json
$S arrange anbotrai-capacity through-cards-auto
$S restore anbotrai-capacity
$S unlock anbotrai-capacity feature-through-cards
```

Offline counts (from `src/healthmodels-plus`):

```bash
uv run --locked python ../../docs/arrange-showcase/features/through-cards/describe.py
```

## Screenshots

- Prepared: [01-prepared-designer.png](01-prepared-designer.png). Looks like a tidy tree, but three services appear to hang under other services.
- Arranged: [02-arranged-designer.png](02-arranged-designer.png). All six services on one row under `Plan a trip`, each resource under its own service.

## Numbers

`describe.py`:

```text
prepared: 15 edges through cards, 12 false links, 3 crossings, 0 label clashes
arranged: 0 edges through cards, 0 false links, 0 crossings, 0 label clashes
```

`logs/anbotrai-capacity.through-cards-auto.log`:

```text
INFO: Auto: layered has 0 edges through cards, 0 false links, 0 crossings, 0 label clashes, score 6
INFO: Auto: tree has 0 edges through cards, 0 false links, 0 crossings, 0 label clashes, score 6
INFO: Auto: keeping layered, the first of equal scores
INFO: Part 1 of 1 is 2200 x 706 px with 0 edges through cards, 0 false links, 0 crossings, 0 label clashes
```

Prepared cost is 15160 (`cost()`, 1000 per edge through a card), arranged is 6.

## Verdict

- Strong on the misleading part: the prepared screenshot looks clean and plausible, yet three parent links are wrong to the eye, which is exactly what the through-card fault catches.
- Weak on the engine side: on a plain tree both engines reach 0 faults, so the `auto` log shows no contest and the local search keeps 0 swaps. The prepared layout only feeds `current`, which `auto` ignores; the win comes from rebuilding rows, not from the scoring picking between candidates.
