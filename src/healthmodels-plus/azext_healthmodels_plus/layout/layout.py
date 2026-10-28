# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
"""Canvas layout for health models.

An engine is a pure function ``(nodes, edges, current, labels) -> {node: (x, y)}`` registered in
``ENGINES``. ``edges`` are ``(parent, child)`` pairs between ``nodes`` without self loops,
``current`` holds the positions on the canvas now and may miss entities, and ``labels`` maps an
edge to the text the portal writes on it. Positions are the top-left corners of fixed-size cards.
Parents sit above their children because the portal draws every relationship from the parent's
bottom handle to the child's top handle.
"""

import logging
from bisect import bisect_left, bisect_right
from itertools import accumulate, combinations, count
from math import ceil, inf

from knack.log import get_logger

logger = get_logger(__name__)

CARD_W, CARD_H = 200, 130  # portal cards are 200 wide; 130 fits a name wrapped to three lines
GAP_X, GAP_Y = 50, 62  # about the 250 x 193 pitch of the portal's Arrange
GRID_X, ROW = (CARD_W + GAP_X) / 2, CARD_H + GAP_Y  # half a column lets a parent centre over two
TRACK = ROW // 6  # a child one track lower moves its incoming lines 16 px, clear of a label
LETTER, LABEL_H = 7, 16  # size of the portal's edge label text on the canvas
WORK = 30_000  # local search tries times edges: big models get fewer tries


def note(count, message, *args):
    """Log a step at info level (``--verbose``), or at debug level when it only concerns a lone
    entity: it has nothing to decide, and a model with many would flood the output."""
    logger.log(logging.INFO if count > 1 else logging.DEBUG, message, *args)


def is_card(n):
    """Cards are entity names; the dummy points of long edges are ``(parent, child, row)``."""
    return not isinstance(n, tuple)


def graph(nodes, edges):
    """Parent and child lists, ignoring self loops and edges to unknown nodes."""
    parents, children = {n: [] for n in nodes}, {n: [] for n in nodes}
    for p, c in edges:
        if p != c and p in parents and c in parents:
            parents[c].append(p)
            children[p].append(c)
    return parents, children


def ranks(nodes, parents):
    """Longest path from the roots: a card sits one row below its lowest parent."""
    rank = {}

    def visit(n, path):
        if n not in rank:
            rank[n] = 1 + max((visit(p, path | {n}) for p in parents[n] if p not in path), default=-1)
        return rank[n]

    for n in nodes:
        visit(n, frozenset())
    return rank


def hops(edges, rank):
    """Split edges that skip rows into one-row hops through dummy points ``(parent, child, row)``."""
    out = []
    for p, c in edges:
        if rank[c] > rank[p]:  # anything else is the back edge of a cycle
            chain = [p, *((p, c, r) for r in range(rank[p] + 1, rank[c])), c]
            out += zip(chain, chain[1:])
    return out


def preorder(starts, children):
    """Depth-first order from ``starts``, each node once."""
    seen, found = set(), []

    def visit(n):
        if n not in seen:
            seen.add(n)
            found.append(n)
            for c in children.get(n, ()):
                visit(c)

    for n in starts:
        visit(n)
    return found


def rows_by_dfs(nodes, children, row):
    """Depth-first order from the roots keeps every subtree together in its row."""
    found = preorder(nodes, children)
    rows = [[] for _ in range(1 + max(map(row, found), default=0))]
    for n in found:
        rows[row(n)].append(n)
    return rows


def crossings(rows, down):
    """Edge crossings between neighbouring rows."""
    total = 0
    for upper, lower in zip(rows, rows[1:]):
        at = {n: i for i, n in enumerate(lower)}
        ends = [(i, at[c]) for i, p in enumerate(upper) for c in down.get(p, ())]
        total += sum((a1 - a2) * (b1 - b2) < 0 for (a1, b1), (a2, b2) in combinations(ends, 2))
    return total


def sweep(rows, near):
    """Sort each row by the mean index of its neighbours in the row before (barycenter)."""
    out = [rows[0]]
    for row in rows[1:]:
        at = {n: i for i, n in enumerate(out[-1])}

        def key(item, at=at):
            i, n = item
            xs = [at[m] for m in near.get(n, ()) if m in at]
            return sum(xs) / len(xs) if xs else i

        out.append([n for _, n in sorted(enumerate(row), key=key)])
    return out


def transpose(rows, up, down):
    """Swap neighbours in a row while that removes crossings with the rows around it. A swap only
    changes the crossings between the two cards' own edges, so only those are counted."""
    rows, improved = [list(r) for r in rows], True
    while improved:
        improved = False
        for r, row in enumerate(rows):
            above = {n: i for i, n in enumerate(rows[r - 1])} if r else {}
            below = {n: i for i, n in enumerate(rows[r + 1])} if r + 1 < len(rows) else {}

            def ends(n, above=above, below=below):
                return [above[m] for m in up.get(n, ())], [below[m] for m in down.get(n, ())]

            for i in range(len(row) - 1):
                (u_up, u_down), (v_up, v_down) = ends(row[i]), ends(row[i + 1])
                pairs = [(a, b) for us, vs in ((u_up, v_up), (u_down, v_down)) for a in us for b in vs]
                if sum(a < b for a, b in pairs) < sum(a > b for a, b in pairs):
                    row[i], row[i + 1] = row[i + 1], row[i]
                    improved = True
    return rows


def order(rows, up, down, sweeps=12):
    """Alternate down and up barycenter sweeps and keep the order with the fewest crossings."""
    tried = [transpose(rows, up, down)]
    for i in range(sweeps):
        rows = sweep(tried[-1], up) if i % 2 == 0 else sweep(tried[-1][::-1], down)[::-1]
        tried.append(transpose(rows, up, down))
    counts = [crossings(rows, down) for rows in tried]
    best = counts.index(min(counts))
    note(sum(map(len, rows)), 'Row order: %d crossings to start with, %d at best (after %d of %d sweeps)',
         counts[0], counts[best], best, sweeps)
    return tried[best]


def project(want, seps):
    """Closest positions to ``want`` that keep the order and the minimum separations (isotonic
    regression by pool-adjacent-violators on ``want - offset``)."""
    offs = list(accumulate([0, *seps]))
    blocks = []  # [sum, count]
    for v in (w - o for w, o in zip(want, offs)):
        blocks.append([v, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1] = [blocks[-1][0] + s, blocks[-1][1] + c]
    return [s / c + o for (s, c), o in zip((b for b in blocks for _ in range(b[1])), offs)]


def place(rows, want, seps, iterations=40):
    """Move every point to where ``want`` pulls it, then project each row back to legal."""
    x = {n: v for row in rows for n, v in zip(row, project([0] * len(row), seps(row, {})))}
    for i in range(iterations):
        for row in rows if i % 2 == 0 else rows[::-1]:
            x.update(zip(row, project([want(n, x) for n in row], seps(row, x))))
    return x


def straighten(rows, x, seps, up, down):
    """Columns: line up a card with its parent when it is that parent's only child and either row
    has room. Each row is visited both ways, so a card can take the room its neighbour just made."""
    def fits(row, n, v):
        i, gaps = row.index(n), seps(row)
        return (i == 0 or v - x[row[i - 1]] >= gaps[i - 1] - 1e-6) and (
            i == len(row) - 1 or x[row[i + 1]] - v >= gaps[i] - 1e-6)

    for above, row in zip(rows, rows[1:]):
        for n in row + row[::-1]:
            if is_card(n) and len(up.get(n, ())) == 1 and len(down[up[n][0]]) == 1:
                p = up[n][0]
                if fits(row, n, x[p]):
                    x[n] = x[p]
                elif fits(above, p, x[n]):
                    x[p] = x[n]
    return x


def route(pos, p, c):
    """The portal's step edge: down from the parent, across at half height, down into the child."""
    sx, sy = pos[p][0] + CARD_W / 2, pos[p][1] + CARD_H
    tx, ty = pos[c][0] + CARD_W / 2, pos[c][1]
    return [(sx, sy), (sx, (sy + ty) / 2), (tx, (sy + ty) / 2), (tx, ty)]


def faults(nodes, edges, pos, labels=None):
    """What the portal draws misleadingly in ``pos``: ``(through, false, crossed, clashes, length)``,
    the edges through a card, false links (a parent and a child that only look linked because
    their lines merge into one), crossings, labels on top of each other, and the length of the
    horizontal lines in cards."""
    routes = {e: route(pos, *e) for e in edges}
    runs = sorted((r[1][1], min(r[1][0], r[2][0]), max(r[1][0], r[2][0]), e) for e, r in routes.items())
    heights = [y for y, *_ in runs]
    cards = sorted(nodes, key=lambda n: pos[n][1])
    tops = [pos[n][1] for n in cards]

    def cuts(a, b, xy):
        return (xy[0] < max(a[0], b[0]) and min(a[0], b[0]) < xy[0] + CARD_W
                and xy[1] < max(a[1], b[1]) and min(a[1], b[1]) < xy[1] + CARD_H)

    def cut_by(e):  # cards, other than its own ends, that the route of e passes through
        r = routes[e]
        lo, hi = min(r[0][1], r[3][1]), max(r[0][1], r[3][1])
        for n in cards[bisect_right(tops, lo - CARD_H):bisect_left(tops, hi)]:
            if n not in e and any(cuts(a, b, pos[n]) for a, b in zip(r, r[1:])):
                yield n

    def crossed_by(e):  # unrelated edges whose run a vertical part of e passes through
        for (x, y0), (_, y1) in (routes[e][:2], routes[e][2:]):
            for _, lo, hi, f in runs[bisect_right(heights, min(y0, y1)):bisect_left(heights, max(y0, y1))]:
                if lo < x < hi and not set(e) & set(f):
                    yield frozenset((e, f))

    def misread(line):  # a merged line reads as every parent on it linked to every child on it
        return len({p for p, _ in line}) * len({c for _, c in line}) - len(line)

    through = sum(1 for e in edges for _ in cut_by(e))
    crossed = len({pair for e in edges for pair in crossed_by(e)})
    false, line, last_y, end = 0, [], None, -inf
    for y, lo, hi, e in runs:
        if y != last_y or lo > end + 10:  # a visible gap ends a line
            false, line, end = false + misread(line), [], -inf
        line, last_y, end = line + [e], y, max(end, hi)
    false += misread(line)
    boxes = [((r[1][0] + r[2][0]) / 2, r[1][1], len(labels[e]) * LETTER / 2)
             for e, r in routes.items() if (labels or {}).get(e)]
    clashes = sum(abs(x1 - x2) < w1 + w2 and abs(y1 - y2) < LABEL_H
                  for (x1, y1, w1), (x2, y2, w2) in combinations(boxes, 2))
    length = sum(abs(r[1][0] - r[2][0]) for r in routes.values()) / CARD_W
    return through, false, crossed, clashes, length


def cost(nodes, edges, pos, labels=None):
    """How misleading the portal draws ``pos``. Worst is an edge through a card, then false links,
    crossings and labels on top of each other; long horizontal lines and a canvas that fits the
    screen worse count a little."""
    through, false, crossed, clashes, length = faults(nodes, edges, pos, labels)
    return 1000 * through + 10 * (false + crossed + clashes) + length - 2000 * fit(*size(pos))


def describe(nodes, edges, pos, labels=None):
    through, false, crossed, clashes, _ = faults(nodes, edges, pos, labels)
    return f'{through} edges through cards, {false} false links, {crossed} crossings, {clashes} label clashes'


def improve(rows, judge, tries):
    """Local search: swap neighbours in a row while that lowers ``judge`` of the drawing, at most
    ``tries`` times so big models stay fast."""
    rows, improved = [list(r) for r in rows], True
    start = best = judge(rows)
    left, kept = tries, 0
    while improved:
        improved = False
        for row, i in [(row, i) for row in rows for i in range(len(row) - 1)]:
            if left == 0:
                break
            left -= 1
            row[i], row[i + 1] = row[i + 1], row[i]
            score = judge(rows)
            if score < best - 1:  # a real gain, not a few pixels of fit
                best, improved, kept = score, True, kept + 1
            else:
                row[i], row[i + 1] = row[i + 1], row[i]
    note(sum(map(len, rows)), 'Local search kept %d of %d swaps (limit %d), score %.0f -> %.0f (lower is better)',
         kept, tries - left, tries, start, best)
    return rows


def columns(nodes, parents, rank, card_x=None, judge=None):
    """Dummy points for long edges; rows sorted by ``card_x`` when given, else ordered for few
    crossings and refined by local search when ``judge`` scores drawings; then balanced, straight
    columns, snapped to the grid like the saved layout, so ``judge`` and ``auto`` score what is saved."""
    links = [(p, c) for c in nodes for p in parents[c]]
    up, down = {}, {}
    for p, c in hops(links, rank):
        down.setdefault(p, []).append(c)
        up.setdefault(c, []).append(p)

    def width(n, x):  # at its middle row a long edge's run needs its whole length kept free
        if is_card(n):
            return CARD_W
        p, c, r = n
        return abs(x[p] - x[c]) + 20 if x and 2 * r == rank[p] + rank[c] else 0

    def seps(row, x):
        return [(width(a, x) + width(b, x)) / 2 + GAP_X for a, b in zip(row, row[1:])]

    def want(n, x):
        if is_card(n):
            near = up.get(n, []) + down.get(n, [])
            return sum(x[m] for m in near) / len(near) if near else x[n]
        p, c, r = n  # the portal draws a long edge down the parent's column, across, down the child's
        mid = rank[p] + rank[c]
        return x[p] if 2 * r < mid else x[c] if 2 * r > mid else (x[p] + x[c]) / 2

    def sort_key(n):
        if is_card(n):
            return card_x[n]
        p, c, r = n  # a dummy point sits between its edge's ends
        a, b = card_x[p], card_x[c]
        return min(a, b) if inf in (a, b) else a + (b - a) * (r - rank[p]) / (rank[c] - rank[p])

    def draw(rows):
        x = place(rows, want, seps)
        x = straighten(rows, x, lambda row: seps(row, x), up, down)
        return normalize({n: (x[n] - CARD_W / 2, rank[n] * ROW) for n in nodes})

    rows = rows_by_dfs(nodes, down, lambda n: rank[n] if is_card(n) else n[2])
    note(len(nodes), '%d rows by longest path; %d edges skip rows and get %d extra points', len(rows),
         sum(rank[c] - rank[p] > 1 for p, c in links), sum(not is_card(n) for row in rows for n in row))
    rows = order(rows, up, down) if card_x is None else [sorted(row, key=sort_key) for row in rows]
    if judge:
        rows = improve(rows, lambda rows: judge(draw(rows)), WORK // max(len(links), 100))
    return draw(rows)


def layered(nodes, edges, current, labels):  # pylint: disable=unused-argument
    """A fresh layout: rows by longest path, the row order with the fewest crossings found, then
    refined on how the portal draws it."""
    parents, _ = graph(nodes, edges)
    return columns(nodes, parents, ranks(nodes, parents), judge=lambda pos: cost(nodes, edges, pos, labels))


def tree(nodes, edges, current, labels):  # pylint: disable=unused-argument
    """Subtrees keep their own columns: a card with several parents hangs under the deepest
    ancestor they share, each row follows the depth-first order of that tree, then the drawing is
    refined like ``layered``."""
    parents, _ = graph(nodes, edges)
    rank, ancestors, owner, shared_cards = ranks(nodes, parents), {}, {}, 0

    def above(n):
        if n not in ancestors:
            ancestors[n] = {n}.union(*(above(p) for p in parents[n] if rank[p] < rank[n]))
        return ancestors[n]

    for n in nodes:
        ps = [p for p in parents[n] if rank[p] < rank[n]]
        if ps:
            shared = set.intersection(*map(above, ps))
            owner[n] = max(shared, key=lambda a: (rank[a], -nodes.index(a))) if shared else ps[0]
            if len(ps) > 1:
                shared_cards += 1
                logger.debug("Tree: '%s' has parents %s and hangs under '%s'", n, ps, owner[n])
    note(len(nodes), 'Tree: %d cards with several parents hang under the deepest ancestor their parents share',
         shared_cards)
    kids = {n: [m for m in nodes if owner.get(m) == n] for n in nodes}
    at = {n: i for i, n in enumerate(preorder([n for n in nodes if n not in owner], kids))}
    return columns(nodes, parents, rank, at, lambda pos: cost(nodes, edges, pos, labels))


def auto(nodes, edges, current, labels):
    """Whichever of ``layered`` and ``tree`` the portal draws less misleadingly."""
    tried = {}
    for engine in (layered, tree):
        note(len(nodes), 'Auto: trying %s', engine.__name__)
        tried[engine.__name__] = engine(nodes, edges, current, labels)
    scores = {name: cost(nodes, edges, pos, labels) for name, pos in tried.items()}
    best = min(scores, key=scores.get)
    for name, pos in tried.items():
        note(len(nodes), 'Auto: %s has %s, score %.0f', name, describe(nodes, edges, pos, labels), scores[name])
    note(len(nodes), 'Auto: keeping %s, %s', best,
         'the lower score' if len(set(scores.values())) > 1 else 'the first of equal scores')
    return tried[best]


def tracks(nodes, edges, pos):
    """Give each child that would join an unrelated line its own height: between two rows, colour
    the spans of the children's incoming lines greedily (longest first, only children with the
    same parents may share a height), push each child down a ``TRACK`` per colour, and grow the
    gaps around a row by its deepest track."""
    parents, _ = graph(nodes, edges)
    row = {n: round(pos[n][1] / ROW) for n in nodes}
    track = dict.fromkeys(nodes, 0)
    for r in sorted(set(row.values())):
        spans = []
        for c in (n for n in nodes if row[n] == r):
            ps = {p for p in parents[c] if row[p] == r - 1}
            xs = [pos[p][0] for p in ps] + [pos[c][0]]
            if max(xs) > min(xs):
                spans.append((min(xs), max(xs), c, ps))
        taken = []
        for lo, hi, c, ps in sorted(spans, key=lambda s: s[0] - s[1]):
            busy = {t for l2, h2, t, p2 in taken if l2 < hi + GAP_X and lo < h2 + GAP_X and ps != p2}
            track[c] = next(t for t in count() if t not in busy)
            taken.append((lo, hi, track[c], ps))
    deepest = {r: max(track[n] for n in nodes if row[n] == r) for r in set(row.values())}
    out, top = {}, 0
    for r in sorted(deepest):  # the gap above a row also holds the lines down to its deepest track
        top += deepest[r] * TRACK
        out.update((n, (pos[n][0], top + track[n] * TRACK)) for n in nodes if row[n] == r)
        top += ROW + deepest[r] * TRACK
    return out


def network(nodes, edges, current, labels):  # pylint: disable=unused-argument
    """For models where many entities share children: the layered layout, but children that would
    join unrelated lines get their own height (see ``tracks``), and the row order is refined on
    that drawing. Rows are no longer straight."""
    parents, _ = graph(nodes, edges)
    flat = columns(nodes, parents, ranks(nodes, parents),
                   judge=lambda pos: cost(nodes, edges, tracks(nodes, edges, pos), labels))
    out = tracks(nodes, edges, flat)
    note(len(nodes), 'Network: own heights for lines that would merge took the canvas height from %d to %d px',
         size(flat)[1], size(out)[1])
    return out


def keep_order(nodes, edges, current, labels):  # pylint: disable=unused-argument
    """Tidy the canvas as drawn: rows, spacing, centring and columns come from the layered layout,
    but every row keeps its left-to-right order from the canvas. A card without a position goes
    where its placed neighbours are, or to the end of its row."""
    parents, children = graph(nodes, edges)
    rank = ranks(nodes, parents)
    placed = {n: current[n][0] for n in nodes if n in current}
    near = {n: [placed[m] for m in parents[n] + children[n] if m in placed] for n in nodes}
    drawn = {n: placed.get(n, sum(near[n]) / len(near[n]) if near[n] else inf) for n in nodes}
    note(len(nodes), 'Keep-order: %d cards keep their drawn left-to-right order, %d without a position go beside '
         'their placed neighbours or last in their row', len(placed), len(nodes) - len(placed))
    # cards at the same x keep their depth-first order; walking the rows in drawn order makes that
    # the order of their parents, so an unplaced card stays with its siblings instead of going by name
    pos = columns(sorted(nodes, key=lambda n: (rank[n], drawn[n])), parents, rank, drawn)
    return {n: pos[n] for n in nodes}


ENGINES = {'auto': auto, 'layered': layered, 'tree': tree, 'network': network, 'keep-order': keep_order}


def components(nodes, edges):
    """Connected parts of the graph, largest first, each in input order."""
    parents, children = graph(nodes, edges)
    near = {n: {*parents[n], *children[n]} for n in nodes}
    seen, parts = set(), []
    for n in nodes:
        if n not in seen:
            part, todo = set(), [n]
            while todo:
                m = todo.pop()
                if m not in part:
                    part.add(m)
                    todo += near[m]
            seen |= part
            parts.append([m for m in nodes if m in part])
    return sorted(parts, key=len, reverse=True)


def size(pos):
    xs, ys = [x for x, _ in pos.values()], [y for _, y in pos.values()]
    return max(xs) - min(xs) + CARD_W, max(ys) - min(ys) + CARD_H


def fit(w, h):
    """Zoom that shows a ``w`` x ``h`` canvas on a 16:9 screen; bigger reads better."""
    return min(16 / w, 9 / h)


def snap(v, step):
    return round(round(v / step) * step)


def normalize(pos):
    """Move the top-left card to (0, 0) and snap every card to the grid."""
    x0, y0 = min(x for x, _ in pos.values()), min(y for _, y in pos.values())
    return {n: (snap(x - x0, GRID_X), snap(y - y0, TRACK)) for n, (x, y) in pos.items()}


def arrange(nodes, edges, engine='auto', current=None, labels=None):
    """Lay out each connected part, then shelve the parts left to right with an empty column
    between them, opening a new shelf below whenever that fits a 16:9 screen better."""
    nodes, out, left, top = list(dict.fromkeys(nodes)), {}, 0, 0
    known = set(nodes)
    edges = [(p, c) for p, c in edges if p != c and p in known and c in known]
    # keep-order lays out unconnected parts together, so their drawn order survives too
    parts = list(filter(None, [nodes] if engine == 'keep-order' else components(nodes, edges)))
    logger.info("Arranging %d entities and %d relationships with engine '%s' as %d part(s)",
                len(nodes), len(edges), engine, len(parts))
    for number, part in enumerate(parts, 1):
        inside = set(part)
        links = [(p, c) for p, c in edges if p in inside and c in inside]
        note(len(part), 'Part %d of %d: %d entities, %d relationships', number, len(parts), len(part), len(links))
        pos = normalize(ENGINES[engine](part, links, current or {}, labels or {}))
        w, h = size(pos)
        note(len(part), 'Part %d of %d is %d x %d px with %s', number, len(parts), w, h,
             describe(part, links, pos, labels))
        if out:
            total_w, total_h = size(out)
            shelf = total_h - CARD_H + 2 * ROW
            if fit(max(total_w, w), shelf + h) > fit(max(total_w, left + w), max(total_h, top + h)):
                left, top = 0, shelf
                note(len(part), 'Part %d opens a new shelf below the others: that fits a 16:9 screen better', number)
            else:
                note(len(part), 'Part %d goes beside the previous one on the current shelf', number)
        out.update({n: (x + left, y + top) for n, (x, y) in pos.items()})
        left += w + CARD_W + 2 * GAP_X
    return out


def descendants(root, edges):
    """``root`` and every entity below it."""
    found, todo = [], [root]
    while todo:
        n = todo.pop(0)
        if n not in found:
            found.append(n)
            todo += [c for p, c in edges if p == n]
    return found


def arrange_below(root, edges, current, engine='auto', labels=None):
    """Arrange ``root`` and its descendants with ``root`` kept in place, and slide the other
    cards in the rows the subtree covers aside so that nothing overlaps it. Returns the positions
    of the subtree and of the cards it pushed; ``current`` maps entities to their positions."""
    pos = arrange(descendants(root, edges), edges, engine, current, labels)
    rx, ry = current.get(root, pos[root])
    pos = {n: (x + rx - pos[root][0], y + ry - pos[root][1]) for n, (x, y) in pos.items()}
    left = min(x for x, _ in pos.values())
    right = max(x for x, _ in pos.values()) + CARD_W
    bottom = max(y for _, y in pos.values()) + CARD_H
    # shortcut: only cards that overlap the subtree's rows move, so a card pushed into a taller
    # neighbour outside those rows can still overlap; iterate the band if that shows up
    band = {n: xy for n, xy in current.items() if n not in pos and ry - CARD_H < xy[1] < bottom}
    rights = {n: xy for n, xy in band.items() if xy[0] >= rx}
    lefts = {n: xy for n, xy in band.items() if xy[0] < rx}
    push_right = ceil(max([0] + [right + GAP_X - x for x, _ in rights.values()]) / GRID_X) * GRID_X
    push_left = ceil(max([0] + [x + CARD_W + GAP_X - left for x, _ in lefts.values()]) / GRID_X) * GRID_X
    logger.info("'%s' stays at (%g, %g); %d cards beside it slid %g px right, %d slid %g px left to clear the subtree",
                root, rx, ry, len(rights) if push_right else 0, push_right, len(lefts) if push_left else 0, push_left)
    pos.update({n: (x + push_right, y) for n, (x, y) in rights.items()})
    pos.update({n: (x - push_left, y) for n, (x, y) in lefts.items()})
    return pos
