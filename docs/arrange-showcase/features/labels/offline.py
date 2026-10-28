"""Arrange anbotrai-stability with the demo relationships, once blind to labels and once with them.
Writes prepare.json (the blind result) and expected.json. Run from src/healthmodels-plus:
uv run --locked python ../../docs/arrange-showcase/features/labels/offline.py"""
import json
import logging
from pathlib import Path

from azext_healthmodels_plus.layout.layout import arrange, describe

logging.disable(logging.CRITICAL)
HERE = Path(__file__).parent
nodes = json.loads((HERE / 'entities.before.json').read_text())
edges = [(r['p'], r['c']) for r in json.loads((HERE / 'relationships.before.json').read_text())]
demo = {('generation', 'interface-resource'): 'sends drafts to',
        ('interface', 'generation-resource'): 'renders results of'}
edges += demo
for name, labels in (('prepare', {}), ('expected', demo)):
    pos = arrange(nodes, edges, labels=labels)
    print(f'{name}: {describe(nodes, edges, pos, demo)}')
    (HERE / f'{name}.json').write_text(json.dumps([{'name': n, 'x': x, 'y': y} for n, (x, y) in pos.items()], indent=1))
