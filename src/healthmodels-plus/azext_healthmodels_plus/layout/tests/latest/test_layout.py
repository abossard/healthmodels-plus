# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import json
import unittest
from itertools import combinations, cycle
from random import Random
from types import SimpleNamespace
from unittest import mock

from azure.cli.core.azclierror import InvalidArgumentValueError, ResourceNotFoundError

from azext_healthmodels_plus.layout import custom
from azext_healthmodels_plus.layout.layout import (CARD_H, CARD_W, ENGINES, GRID_X, ROW, TRACK, arrange, arrange_below,
                                                   cost, describe, descendants, project, tracks)

SERVICES = {'assistant': 3, 'destinations': 2, 'generation': 1, 'interface': 1, 'itinerary': 1, 'preferences': 1}
RESOURCES = [(s, f'{s}-{i}') for s, k in SERVICES.items() for i in range(k)]
GRAPHS = {
    'empty': ([], []),
    'single': (['a'], []),
    'tree': (list('rabcde'), [('r', 'a'), ('r', 'b'), ('a', 'c'), ('a', 'd'), ('b', 'e')]),
    'shared child': (list('rabc'), [('r', 'a'), ('r', 'b'), ('a', 'c'), ('b', 'c')]),
    'long edge': (list('rabc'), [('r', 'a'), ('a', 'b'), ('b', 'c'), ('r', 'c')]),
    'forest and loner': (list('rabxyz'), [('r', 'a'), ('r', 'b'), ('x', 'y')]),
    'cycle': (list('abc'), [('a', 'b'), ('b', 'c'), ('c', 'a')]),
    'unknown and self edges': (list('ab'), [('a', 'b'), ('a', 'a'), ('a', 'ghost')]),
    'wide fan': (['r'] + [f'c{i}' for i in range(12)], [('r', f'c{i}') for i in range(12)]),
    'travel agent': (['root', 'plan-trip', *SERVICES, *(c for _, c in RESOURCES)],
                     [('root', 'plan-trip'), *(('plan-trip', s) for s in SERVICES), *RESOURCES]),
}


SUBTREES = {  # edges around the subtree root 's', which sits below 'r'
    'leaf': [('r', 's')],
    'chain': [('r', 's'), ('s', 'a'), ('a', 'b')],
    'fan': [('r', 's'), *(('s', f'c{i}') for i in range(6))],
    'tree': [('r', 's'), ('s', 'a'), ('s', 'b'), ('a', 'c'), ('a', 'd'), ('b', 'f')],
    'diamond': [('r', 's'), ('s', 'a'), ('s', 'b'), ('a', 'c'), ('b', 'c')],
    'long edge': [('r', 's'), ('s', 'a'), ('a', 'b'), ('s', 'b')],
    'child shared with a card outside': [('r', 's'), ('r', 'o'), ('s', 'a'), ('o', 'a')],
    'cycle through the root': [('r', 's'), ('s', 'a'), ('a', 'b'), ('b', 's')],
    'cycle below the root': [('r', 's'), ('s', 'a'), ('a', 'b'), ('b', 'a')],
}


def subtree_canvases(subtree):
    """Canvases around the subtree root 's': its parent 'r' above it, cards beside and below it, and
    the rest of the subtree tidy, scattered, missing or on top of the cards beside it."""
    around = {'r': (500, 0), 's': (500, ROW), 'west': (250, ROW), 'east': (750, ROW), 'o': (1250, ROW),
              'south-west': (0, 2 * ROW), 'south-east': (1000, 2 * ROW), 'far': (500, 12 * ROW)}
    rest = [n for n in subtree if n != 's']
    scattered = {n: (-i * 90, i * 40) for i, n in enumerate(rest)}
    return {
        'tidy': {**around, **{n: (500 + 250 * i, 3 * ROW) for i, n in enumerate(rest)}},
        'scattered': {**around, **scattered},
        'subtree unplaced': around,
        'on top of the cards beside it': {**around, **{n: around[k] for n, k in zip(rest, cycle(around))}},
        'off the grid': {**{n: (x + 0.3 * i, y + 7.7 * i) for i, (n, (x, y)) in enumerate(around.items())},
                         **scattered},
        'between the rows': {'r': (500, 0), 's': (500, ROW), 'o': (1250, ROW), 'west': (250, ROW + 60),
                             'west2': (0, ROW + 160), 'east': (750, ROW + 100), 'east2': (1000, ROW + 200),
                             'east3': (1250, ROW + 300)},
    }


def overlaps(pos, among=None):
    """Pairs of cards on top of each other, only those with a card in ``among`` when given."""
    return [(a, b) for a, b in combinations(pos, 2) if (among is None or a in among or b in among)
            and abs(pos[a][0] - pos[b][0]) < CARD_W and abs(pos[a][1] - pos[b][1]) < CARD_H]


class LayoutTest(unittest.TestCase):

    def test_layout_invariants(self):
        for engine in ENGINES:
            for name, (nodes, edges) in GRAPHS.items():
                with self.subTest(engine=engine, graph=name):
                    current = {n: (-i * 90, i * 40) for i, n in enumerate(nodes[1:])}  # a messy canvas
                    pos = arrange(nodes, edges, engine, current)
                    self.assertEqual(set(pos), set(nodes))
                    self.assertEqual(overlaps(pos), [])
                    self.assertTrue(all(x % GRID_X == 0 and y % (TRACK if engine == 'network' else ROW) == 0
                                        for x, y in pos.values()))
                    downward = [(p, c) for p, c in edges if p in pos and c in pos and p != c and name != 'cycle']
                    self.assertTrue(all(pos[p][1] < pos[c][1] for p, c in downward))

    def test_keep_order_keeps_the_drawn_order_of_each_row(self):
        nodes, edges = list('rabcde'), [('r', 'a'), ('r', 'b'), ('r', 'c'), ('a', 'd'), ('c', 'd'), ('b', 'e')]
        for name, current, row, expected in [
            ('drawn order', {'r': (0, 0), 'a': (500, 300), 'b': (900, 260), 'c': (-40, 310)}, 'abc', 'cab'),
            ('unplaced card goes beside its neighbours', {'a': (500, 300), 'c': (-40, 310), 'e': (200, 600)}, 'abc',
             'cba'),
            ('unplaced card without placed neighbours goes last', {'a': (500, 300), 'c': (-40, 310)}, 'abc', 'cab'),
            ('unplaced card on a tie goes with its own parent, not by name',
             {'a': (500, 300), 'b': (0, 300), 'c': (250, 300), 'd': (0, 600)}, 'de', 'ed'),
        ]:
            with self.subTest(case=name):
                pos = arrange(nodes, edges, 'keep-order', current)
                self.assertEqual(''.join(sorted(row, key=lambda n: pos[n][0])), expected)
        with self.subTest(case='unconnected cards keep their order too'):
            pos = arrange(['a', 'b'], [], 'keep-order', {'a': (500, 0), 'b': (0, 0)})
            self.assertLess(pos['b'][0], pos['a'][0])

    def test_logs_the_steps_and_decisions_of_each_engine(self):
        nodes, edges = GRAPHS['long edge']
        for engine, expected in [('auto', 'Auto: keeping'), ('layered', 'Local search'), ('tree', 'Tree: 1 cards'),
                                 ('network', 'Network:'), ('keep-order', 'Keep-order:')]:
            with self.subTest(engine=engine), self.assertLogs('cli', 'INFO') as logs:
                arrange(nodes, edges, engine)
            self.assertIn(expected, '\n'.join(logs.output))
            self.assertIn('Part 1 of 1 is', '\n'.join(logs.output))

    def test_lone_entities_stay_out_of_the_verbose_log(self):
        with self.assertLogs('cli', 'INFO') as logs:
            arrange(['a', 'b'], [])
        self.assertEqual(len(logs.output), 1)

    def test_parent_with_single_child_is_straight_above_it(self):
        for name, (nodes, edges, parent, child) in {
            'chain': (list('ab'), [('a', 'b')], 'a', 'b'),
            'chain beside a fan': (list('rabcde'), [('r', 'a'), ('r', 'b'), ('a', 'c'), ('b', 'd'), ('b', 'e')],
                                   'a', 'c'),
            **{f'services packed tighter than their resources ({p})': (*GRAPHS['travel agent'], p, c)
               for p, c in RESOURCES if SERVICES[p] == 1},
        }.items():
            with self.subTest(graph=name):
                pos = arrange(nodes, edges)
                self.assertEqual(pos[parent][0], pos[child][0])

    def test_logged_scores_are_those_of_the_saved_layout(self):
        nodes, edges = GRAPHS['travel agent']
        edges = edges + [('destinations', 'assistant-0'), ('preferences', 'itinerary-0')]  # layered 58 -> 438 snapped
        for engine, logged in [('layered', '-> {score} (lower is better)'), ('tree', '-> {score} (lower is better)'),
                               ('network', '-> {score} (lower is better)'), ('auto', 'has {faults}, score {score}')]:
            with self.subTest(engine=engine):
                with self.assertLogs('cli', 'INFO') as logs:
                    pos = arrange(nodes, edges, engine)
                self.assertIn(logged.format(score=f'{cost(nodes, edges, pos):.0f}', faults=describe(nodes, edges, pos)),
                              '\n'.join(logs.output))

    def test_local_search_moves_a_card_past_one_in_the_way(self):
        nodes, edges = GRAPHS['travel agent']
        edges = edges + [('assistant-0', 'generation'), ('destinations-1', 'generation-0')]  # swaps alone: 12 false links
        for engine in ('auto', 'layered', 'tree'):
            with self.subTest(engine=engine):
                self.assertEqual(describe(nodes, edges, arrange(nodes, edges, engine)),
                                 '0 edges through cards, 3 false links, 0 crossings, 0 label clashes')

    def test_project_keeps_order_and_separation(self):
        for want, seps, expected in [
            ([0, 100, 500], [50, 50], [0, 100, 500]),
            ([10, 10], [50], [-15, 35]),
            ([30, 0, 0], [10, 10], [0, 10, 20]),
        ]:
            with self.subTest(want=want):
                self.assertEqual([round(v, 6) for v in project(want, seps)], expected)

    def test_cost_counts_what_the_portal_draws_badly(self):
        row1, row2, row3 = 0, ROW, 2 * ROW
        for name, nodes, edges, pos, expected in [
            ('straight edge', 'ab', [('a', 'b')], {'a': (0, row1), 'b': (0, row2)}, 0),
            ('edge through a card', 'abc', [('a', 'c')], {'a': (0, row1), 'b': (0, row2), 'c': (0, row3)}, 1000),
            ('false links on a merged line', ['p1', 'p2', 'c1', 'c2'], [('p1', 'c1'), ('p2', 'c2')],
             {'p1': (0, row1), 'p2': (250, row1), 'c1': (250, row2), 'c2': (0, row2)}, 20 + 2 * 250 / CARD_W),
            ('a long line crossing a run', 'pqmz', [('p', 'm'), ('q', 'z')],
             {'p': (0, row1), 'q': (250, row1), 'm': (500, row2), 'z': (250, row3)}, 10 + 500 / CARD_W),
        ]:
            with self.subTest(case=name):
                fit_bonus = -cost(list(nodes), [], pos)
                self.assertAlmostEqual(cost(list(nodes), edges, pos) + fit_bonus, expected)

    def test_cost_counts_labels_on_top_of_each_other(self):
        nodes, edges = ['p', 'a', 'b'], [('p', 'a'), ('p', 'b')]
        for name, pos, clashes in [
            ('labels side by side', {'p': (250, 0), 'a': (0, ROW), 'b': (500, ROW)}, 0),
            ('labels on top of each other', {'p': (250, 0), 'a': (125, ROW), 'b': (375, ROW)}, 1),
        ]:
            with self.subTest(case=name):
                labels = {e: 'requires operation evidence' for e in edges}
                self.assertAlmostEqual(cost(nodes, edges, pos, labels) - cost(nodes, edges, pos), 10 * clashes)

    def test_tracks_lower_a_child_whose_line_would_join_an_unrelated_one(self):
        two = [('p1', 'c1'), ('p2', 'c2')]
        for name, edges, pos, lowered in [
            ('crossing lines', two, {'p1': (0, 0), 'p2': (250, 0), 'c1': (250, ROW), 'c2': (0, ROW)}, {'c2'}),
            ('straight lines', two, {'p1': (0, 0), 'p2': (250, 0), 'c1': (0, ROW), 'c2': (250, ROW)}, set()),
            ('lines that share only one parent', [('p1', 'c1'), ('p2', 'c1'), ('p2', 'c2'), ('p3', 'c2')],
             {'p1': (0, 0), 'p2': (250, 0), 'p3': (500, 0), 'c1': (125, ROW), 'c2': (375, ROW)}, {'c2'}),
        ]:
            with self.subTest(case=name):
                nodes = list(pos)
                out = tracks(nodes, edges, pos)
                self.assertEqual({c for c in ('c1', 'c2') if out[c][1] == min(out['c1'][1], out['c2'][1]) + TRACK}, lowered)

    def assert_subtree_arranged(self, root, edges, current, pos, acyclic=True):
        """``root`` keeps its exact position, its subtree is on the grid below it, and the other cards
        only slide sideways, away from it, so that no moved card overlaps another."""
        subtree, (rx, ry) = descendants(root, edges), current[root]
        self.assertEqual(pos[root], current[root])
        self.assertLessEqual(set(subtree), set(pos))
        self.assertEqual(overlaps({**current, **pos}, among=set(pos)), [])
        def on_grid(v, step):  # off-grid roots leave float rounding in the offsets
            return abs(v / step - round(v / step)) < 1e-9

        self.assertTrue(all(on_grid(x - rx, GRID_X) and on_grid(y - ry, TRACK) and y >= ry
                            for x, y in (pos[n] for n in subtree)))
        slid = [n for n in pos if n not in subtree]
        self.assertTrue(all(pos[n][1] == current[n][1] for n in slid))
        self.assertTrue(all((pos[n][0] > current[n][0]) == (current[n][0] >= rx) for n in slid))
        if acyclic:
            self.assertTrue(all(pos[p][1] < pos[c][1] for p, c in edges if p in subtree and c in subtree))

    def test_arrange_below_invariants(self):
        for engine in ENGINES:
            for graph_name, edges in SUBTREES.items():
                nodes = descendants('s', edges)
                for canvas_name, current in subtree_canvases(nodes).items():
                    with self.subTest(engine=engine, graph=graph_name, canvas=canvas_name):
                        pos = arrange_below('s', edges, current, engine)
                        self.assert_subtree_arranged('s', edges, current, pos, acyclic='cycle' not in graph_name)

    def test_arrange_below_invariants_on_random_models(self):
        for seed in range(60):
            rng = Random(seed)
            nodes = [f'n{i}' for i in range(rng.randrange(3, 14))]
            edges = sorted({(nodes[p], c) for i, c in enumerate(nodes[1:], 1)
                            for p in rng.sample(range(i), min(i, rng.choice([1, 1, 2])))})
            if seed % 4 == 0:  # a back edge closes a cycle
                edges.append((nodes[-1], nodes[rng.randrange(len(nodes) - 1)]))
            current = {}
            for n in nodes:
                spots = [(rng.uniform(0, 2500), rng.uniform(0, 1200)) for _ in range(50)]
                current.update(next(([(n, xy)] for xy in spots if not overlaps({**current, n: xy}, among={n})), []))
            roots = [p for p, _ in edges if p in current]
            if not roots:
                continue
            root, engine = rng.choice(roots), list(ENGINES)[seed % len(ENGINES)]
            with self.subTest(seed=seed, engine=engine, root=root):
                pos = arrange_below(root, edges, current, engine)
                self.assert_subtree_arranged(root, edges, current, pos, acyclic=seed % 4 != 0)

    def test_arrange_below_slides_the_rows_beside_the_subtree_as_a_whole(self):
        edges = [('r', 's'), ('s', 'a'), ('s', 'b'), ('s', 'c')]
        current = {'r': (500, 0), 'up': (1500, 0), 's': (500, ROW), 'w': (250, ROW), 'e': (750, ROW),
                   'sw': (0, 2 * ROW), 'se': (1000, 2 * ROW), 'below': (500, 3 * ROW)}
        with self.assertLogs('cli', 'INFO') as logs:
            pos = arrange_below('s', edges, current)
        self.assertEqual(sorted(pos[n] for n in 'abc'), [(250, 2 * ROW), (500, 2 * ROW), (750, 2 * ROW)])
        self.assertEqual({n: xy for n, xy in pos.items() if n not in 'sabc'},
                         {'w': (0, ROW), 'e': (1000, ROW), 'sw': (-250, 2 * ROW), 'se': (1250, 2 * ROW)})
        self.assertIn("'s' stays at (500, 192); 2 cards beside it slid 250 px right, 2 slid 250 px left",
                      '\n'.join(logs.output))

    def test_arrange_below_takes_along_a_card_a_sliding_neighbour_runs_into(self):
        edges = [('s', f'c{i}') for i in range(4)]
        for name, below, expected in [
                ('right', {'e': (1000, 2 * ROW + 100), 'hit': (1250, 2 * ROW + 200), 'clear': (2000, 2 * ROW + 200)},
                 {'e': (1125, 2 * ROW + 100), 'hit': (1375, 2 * ROW + 200)}),
                ('left', {'w': (-100, 2 * ROW + 100), 'hit': (-350, 2 * ROW + 200), 'clear': (-1000, 2 * ROW + 200)},
                 {'w': (-225, 2 * ROW + 100), 'hit': (-475, 2 * ROW + 200)}),
                ('a chain of them', {'e': (1000, 2 * ROW + 100), 'hit': (1250, 2 * ROW + 200),
                                     'next': (1500, 2 * ROW + 300)},
                 {'e': (1125, 2 * ROW + 100), 'hit': (1375, 2 * ROW + 200), 'next': (1625, 2 * ROW + 300)}),
        ]:
            with self.subTest(side=name):
                current = {'s': (500, ROW), **below}
                pos = arrange_below('s', edges, current)
                self.assertEqual({n: xy for n, xy in pos.items() if n in below}, expected)
                self.assert_subtree_arranged('s', edges, current, pos)

    def test_arrange_below_keeps_the_root_on_top_and_shared_children_with_the_subtree(self):
        rx, ry = 164.36806833325412, 454.5796827597004
        for name, edges, current, expected in [
            ('cycle through the root', [('r', 's'), ('s', 'a'), ('a', 'b'), ('b', 's')],
             {'r': (500, 0), 's': (500, ROW), 'a': (0, 0), 'b': (1000, 0)},
             {'s': (500, ROW), 'a': (500, 2 * ROW), 'b': (500, 3 * ROW)}),
            ('child shared with a card outside', [('r', 's'), ('r', 'o'), ('s', 'a'), ('o', 'a')],
             {'r': (875, 0), 's': (500, ROW), 'o': (1250, ROW), 'a': (1250, 2 * ROW)},
             {'s': (500, ROW), 'a': (500, 2 * ROW)}),
            ('position off the grid, as dragged in the portal', [('s', 'a'), ('s', 'b')], {'s': (rx, ry)},
             {'s': (rx, ry), 'a': (rx - 125, ry + ROW), 'b': (rx + 125, ry + ROW)}),
        ]:
            with self.subTest(case=name):
                self.assertEqual(arrange_below('s', edges, current), expected)


ENTITIES = [
    {'name': 'root', 'properties': {'displayName': 'Root', 'healthState': 'Healthy', 'provisioningState': 'Succeeded',
                                    'canvasPosition': {'x': 0.0, 'y': 0.0}}},
    {'name': 'api', 'properties': {'displayName': 'API', 'discoveredBy': 'rule', 'healthState': 'Healthy'}},
]
FRESH = [ENTITIES[0], {'name': 'api', 'properties': {  # edited in the portal after the model was loaded
    'displayName': 'API v2', 'icon': {'iconName': 'Custom'}, 'signalGroups': {'dependencies': {'aggregationType': 'WorstOf'}},
    'discoveredBy': 'rule', 'healthState': 'Degraded'}}]
RELATIONSHIPS = [{'name': 'r1', 'properties': {'parentEntityName': 'root', 'childEntityName': 'api'}},
                 {'name': 'r2', 'properties': {'parentEntityName': 'root', 'childEntityName': 'deleted'}}]


class ArrangeCommandTest(unittest.TestCase):

    def run_command(self, command=custom.arrange_layout, **kwargs):
        def respond(_, method, url, body=None):  # pylint: disable=unused-argument
            pages = {'/entities?': {'value': ENTITIES, 'nextLink': url + '&page=2'},
                     '/relationships?': {'value': RELATIONSHIPS},
                     **{f'/entities/{e["name"]}?': e for e in FRESH}}
            page = next((p for k, p in pages.items() if k in url and 'page=2' not in url), {'value': []})
            return SimpleNamespace(json=lambda: page)

        cmd = SimpleNamespace(cli_ctx=SimpleNamespace(cloud=SimpleNamespace(
            endpoints=SimpleNamespace(resource_manager='https://management.azure.com/'))))
        with mock.patch.object(custom, 'send_raw_request', side_effect=respond) as send, \
                mock.patch.object(custom, 'get_subscription_id', return_value='sub'):
            result = command(cmd, 'rg', 'hm', **kwargs)
        return result, [c for c in send.call_args_list if c.args[1] == 'PUT']

    def test_saves_only_moved_entities_without_read_only_fields(self):
        for name, kwargs, puts in [('dry run', {'dry_run': True}, 0), ('save', {}, 1)]:
            with self.subTest(case=name):
                result, calls = self.run_command(**kwargs)
                self.assertEqual(result, [{'name': 'api', 'x': 0, 'y': ROW}])
                self.assertEqual(len(calls), puts)
        url, body = calls[0].args[2], json.loads(calls[0].kwargs['body'])
        self.assertTrue(url.startswith('https://management.azure.com/subscriptions/sub/resourceGroups/rg/'
                                       'providers/Microsoft.CloudHealth/healthmodels/hm/entities/api?'))
        self.assertEqual(body, {'properties': {  # the fresh read, without read-only fields, plus the position
            'displayName': 'API v2', 'icon': {'iconName': 'Custom'},
            'signalGroups': {'dependencies': {'aggregationType': 'WorstOf'}}, 'canvasPosition': {'x': 0, 'y': ROW}}})

    def test_logs_what_it_loaded_and_whether_it_saves(self):
        for name, kwargs, expected in [('dry run', {'dry_run': True}, 'dry run, nothing is saved'),
                                       ('save', {}, 'saving them')]:
            with self.subTest(case=name), self.assertLogs('cli', 'INFO') as logs:
                self.run_command(**kwargs)
            output = '\n'.join(logs.output)
            for line in ('Loaded 2 entities (1 with a canvas position) and 2 relationships',
                         'Ignoring 1 relationships', 'Arranging the whole health model', expected):
                self.assertIn(line, output)

    def test_arranges_below_an_entity(self):
        result, calls = self.run_command(entity_name='root', dry_run=True)
        self.assertEqual((result, calls), ([{'name': 'api', 'x': 0, 'y': ROW}], []))

    def test_export_lists_every_entity(self):
        result, _ = self.run_command(custom.export_layout)
        self.assertEqual(result, [{'name': 'root', 'x': 0.0, 'y': 0.0}, {'name': 'api', 'x': None, 'y': None}])

    def test_import_saves_only_changes_and_clears_null_positions(self):
        layout = [{'name': 'root', 'x': None, 'y': None}, {'name': 'api', 'x': None, 'y': None},
                  {'name': 'gone', 'x': 1, 'y': 2}]
        result, calls = self.run_command(custom.import_layout, layout=layout)
        self.assertEqual(result, [{'name': 'root', 'x': None, 'y': None}])
        self.assertEqual(json.loads(calls[0].kwargs['body']), {'properties': {'displayName': 'Root'}})

    def test_import_rejects_malformed_layouts(self):
        for layout in [[{'name': 'root'}], [{'name': 'root', 'x': 'left', 'y': 0}], {'root': [0, 0]},
                       [{'name': 'root', 'x': True, 'y': 0}], [{'name': 'root', 'x': float('nan'), 'y': 0}],
                       [{'name': 'root', 'x': 1, 'y': None}], [{'name': None, 'x': 0, 'y': 0}],
                       [{'name': 'root', 'x': 10 ** 400, 'y': 0}]]:
            with self.subTest(layout=layout), self.assertRaisesRegex(InvalidArgumentValueError, 'must be a list'):
                self.run_command(custom.import_layout, layout=layout)

    def test_entity_name_must_be_a_placed_entity(self):
        for name, error, message in [('nope', ResourceNotFoundError, "Entity 'nope' not found"),
                                     ('', ResourceNotFoundError, "Entity '' not found"),
                                     ('api', InvalidArgumentValueError, 'has no canvas position')]:
            with self.subTest(entity=name), self.assertRaisesRegex(error, message):
                self.run_command(entity_name=name)


if __name__ == '__main__':
    unittest.main()
