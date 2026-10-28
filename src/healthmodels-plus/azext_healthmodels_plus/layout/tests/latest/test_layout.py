# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import json
import unittest
from itertools import combinations
from types import SimpleNamespace
from unittest import mock

from azure.cli.core.azclierror import InvalidArgumentValueError, ResourceNotFoundError

from azext_healthmodels_plus.layout import custom
from azext_healthmodels_plus.layout.layout import (CARD_H, CARD_W, ENGINES, GRID_X, ROW, TRACK, arrange, arrange_below,
                                                   cost, describe, project, tracks)

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


def overlaps(pos):
    return [(a, b) for a, b in combinations(pos, 2)
            if abs(pos[a][0] - pos[b][0]) < CARD_W and abs(pos[a][1] - pos[b][1]) < CARD_H]


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

    def test_arrange_below_keeps_root_and_avoids_overlaps(self):
        edges = [('r', 'a'), ('r', 'b'), ('a', 'c'), ('a', 'd'), ('a', 'e')]
        for name, current in {
            'cards crowd the subtree': {'r': (250, 0), 'a': (0, 200), 'b': (250, 200),
                                        'c': (0, 400), 'd': (0, 400), 'e': (0, 400)},
            'subtree has no positions': {'r': (250, 0), 'a': (0, 200), 'b': (250, 200)},
        }.items():
            with self.subTest(case=name):
                pos = {**current, **arrange_below('a', edges, current)}
                self.assertEqual(pos['a'], current['a'])
                self.assertEqual(overlaps(pos), [])
                self.assertEqual(pos['r'], current['r'])


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
