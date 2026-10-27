# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import json
from math import isfinite

from azure.cli.core.azclierror import InvalidArgumentValueError, ResourceNotFoundError
from azure.cli.core.commands.client_factory import get_subscription_id
from azure.cli.core.util import send_raw_request
from knack.log import get_logger

from .layout import arrange, arrange_below

logger = get_logger(__name__)

API_VERSION = '2026-11-01'
READ_ONLY = {'healthState', 'provisioningState', 'discoveredBy'}


def _items(cli_ctx, url):
    items = []
    while url:
        page = send_raw_request(cli_ctx, 'GET', url).json()
        items += page.get('value', [])
        url = page.get('nextLink')
    return items


def _model(cmd, resource_group_name, health_model_name):
    """The health model's URL, its entity names, their relationships with the labels the portal
    shows on them, and placed positions."""
    url = (f"{cmd.cli_ctx.cloud.endpoints.resource_manager.rstrip('/')}/subscriptions/"
           f"{get_subscription_id(cmd.cli_ctx)}/resourceGroups/{resource_group_name}"
           f"/providers/Microsoft.CloudHealth/healthmodels/{health_model_name}")
    entities = {e['name']: e['properties'] for e in _items(cmd.cli_ctx, f'{url}/entities?api-version={API_VERSION}')}
    labels = {(r['properties']['parentEntityName'], r['properties']['childEntityName']): r['properties'].get('displayName')
              for r in _items(cmd.cli_ctx, f'{url}/relationships?api-version={API_VERSION}')}
    edges = [(p, c) for p, c in labels if p in entities and c in entities]
    current = {n: (p['canvasPosition']['x'], p['canvasPosition']['y'])
               for n, p in entities.items() if p.get('canvasPosition')}
    logger.info('Loaded %d entities (%d with a canvas position) and %d relationships',
                len(entities), len(current), len(labels))
    if len(edges) < len(labels):
        logger.info('Ignoring %d relationships to entities that are not in the health model',
                    len(labels) - len(edges))
    return url, list(entities), edges, current, labels


def _rows(pos):
    """Positions as export writes and import reads them; ``None`` is no position."""
    return [{'name': n, 'x': xy and xy[0], 'y': xy and xy[1]} for n, xy in pos.items()]


def _save(cmd, url, current, pos, dry_run):
    """PUT every entity whose position changes; ``None`` clears the position. Each entity is read
    right before its PUT, so edits made since the model was loaded are kept."""
    moves = {n: xy for n, xy in pos.items() if current.get(n) != xy}
    logger.info('%d of %d positions change; %s', len(moves), len(pos),
                'dry run, nothing is saved' if dry_run else 'saving them')
    if not dry_run:
        for name, xy in moves.items():
            entity = f'{url}/entities/{name}?api-version={API_VERSION}'
            properties = send_raw_request(cmd.cli_ctx, 'GET', entity).json()['properties']
            properties = {k: v for k, v in properties.items() if k not in READ_ONLY | {'canvasPosition'}}
            if xy:
                properties['canvasPosition'] = {'x': xy[0], 'y': xy[1]}
            send_raw_request(cmd.cli_ctx, 'PUT', entity, body=json.dumps({'properties': properties}))
    return _rows(moves)


def _entry(item):
    """``(name, position)`` from an export row; position ``None`` clears it."""
    name, x, y = item['name'], item['x'], item['y']
    if not isinstance(name, str):
        raise TypeError(f'not an entity name: {name!r}')
    if x is None and y is None:
        return name, None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) for v in (x, y)):
        return name, (x, y)
    raise TypeError(f'not a position: {x!r}, {y!r}')


def arrange_layout(cmd, resource_group_name, health_model_name, engine='auto', entity_name=None,
                   dry_run=False):
    url, names, edges, current, labels = _model(cmd, resource_group_name, health_model_name)
    if entity_name is not None and entity_name not in names:
        raise ResourceNotFoundError(f"Entity '{entity_name}' not found in health model '{health_model_name}'.")
    if entity_name is not None and entity_name not in current:
        raise InvalidArgumentValueError(f"Entity '{entity_name}' has no canvas position to keep; "
                                        "arrange the whole model or place the entity first.")
    if entity_name is not None:
        logger.info("Arranging only '%s' and the entities below it; '%s' keeps its position", entity_name, entity_name)
        pos = arrange_below(entity_name, edges, current, engine=engine, labels=labels)
    else:
        logger.info('Arranging the whole health model')
        pos = arrange(names, edges, engine=engine, current=current, labels=labels)
    return _save(cmd, url, current, pos, dry_run)


def export_layout(cmd, resource_group_name, health_model_name):
    _, names, _, current, _ = _model(cmd, resource_group_name, health_model_name)
    return _rows({n: current.get(n) for n in names})


def import_layout(cmd, resource_group_name, health_model_name, layout, dry_run=False):
    try:
        wanted = dict(map(_entry, layout))
    except (KeyError, TypeError, OverflowError) as ex:
        raise InvalidArgumentValueError(
            '--layout must be a list of {"name", "x", "y"} items, as written by export; '
            'x and y are finite numbers, or both null to clear a position.') from ex
    url, names, _, current, _ = _model(cmd, resource_group_name, health_model_name)
    unknown = sorted(set(wanted) - set(names))
    if unknown:
        logger.warning('Skipping entities that are not in the health model: %s', ', '.join(unknown))
    return _save(cmd, url, current, {n: xy for n, xy in wanted.items() if n in names}, dry_run)
