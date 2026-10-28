"""Print describe() for prepare.json and for what the auto engine makes of it, offline.

Run from src/healthmodels-plus: uv run --locked python ../../docs/arrange-showcase/features/through-cards/describe.py
"""
import json
from pathlib import Path

from azext_healthmodels_plus.layout.layout import arrange, describe

SERVICES = {
    'assistant': ['assistant-langchain-resource', 'assistant-llamaindex-resource', 'assistant-python-resource'],
    'destinations': ['destinations-ai-resource', 'destinations-mcp-resource'],
    'generation': ['generation-resource'],
    'interface': ['interface-resource'],
    'itinerary': ['itinerary-resource'],
    'preferences': ['preferences-resource'],
}
edges = [('anbotrai-capacity', 'plan-trip')] + [('plan-trip', s) for s in SERVICES] + [
    (s, r) for s, rs in SERVICES.items() for r in rs]
prepared = {e['name']: (e['x'], e['y']) for e in json.loads((Path(__file__).parent / 'prepare.json').read_text())}
nodes = list(prepared)
print('prepared:', describe(nodes, edges, prepared))
print('arranged:', describe(nodes, edges, arrange(nodes, edges, 'auto', prepared)))
