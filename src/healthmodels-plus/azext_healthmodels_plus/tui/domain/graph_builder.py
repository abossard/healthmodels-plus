"""Build a validated Forest from entities + relationships — pure calculation.

No I/O, no side effects. Given a flat map of entities and a list of
relationship edges, produces a ``Forest`` with children populated,
cycles broken, and dangling references reported via ``Forest.warnings``.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Mapping, Sequence

from azext_healthmodels_plus.tui.models.domain import EntityNode, Forest, Relationship

_LOG = logging.getLogger(__name__)


# ─── public API ──────────────────────────────────────────────────────


def build_forest(
    entities: Mapping[str, EntityNode],
    relationships: Sequence[Relationship],
) -> Forest:
    """Build a validated forest from a flat entity map and relationship edges.

    Algorithm
    ---------
    1. Build an adjacency map (parent → [child, …]) from *relationships*.
    2. Identify root entities — those that never appear as a child.
    3. Walk the graph from each root via DFS; break any cycle by moving
       the back-edge target into the *unlinked* set.
    4. Rebuild each ``EntityNode`` with its ``children`` tuple populated.
    5. Return a ``Forest`` containing roots, enriched entities, and unlinked.
    """
    warnings: list[str] = []
    adjacency = _build_adjacency(entities, relationships, warnings)
    all_children = _all_child_names(adjacency)
    roots = sorted(name for name in entities if name not in all_children)

    visited: set[str] = set()
    unlinked: list[str] = []
    safe_adjacency: dict[str, list[str]] = {k: list(v) for k, v in adjacency.items()}

    for root in roots:
        _break_cycles(root, safe_adjacency, set(), visited, unlinked)

    # Rootless cycles: every node in the cycle is unreachable from any root,
    # so the cycle would survive untouched. Run cycle-breaking from each
    # unvisited node so safe_adjacency is acyclic regardless of root layout.
    for name in entities:
        if name not in visited:
            _break_cycles(name, safe_adjacency, set(), visited, unlinked)

    # An entity is "unlinked" if it isn't a root and isn't reachable from one.
    reachable_from_root: set[str] = set()
    for root in roots:
        _collect_reachable(root, safe_adjacency, reachable_from_root)
    for name in entities:
        if name not in reachable_from_root and name not in roots:
            unlinked.append(name)

    enriched = _populate_children(entities, safe_adjacency, warnings)

    return Forest(
        roots=tuple(roots),
        entities=enriched,
        unlinked=tuple(dict.fromkeys(unlinked)),  # dedupe, preserve order
        warnings=tuple(warnings),
    )


def _collect_reachable(
    node: str,
    adjacency: dict[str, list[str]],
    out: set[str],
) -> None:
    """Collect every node reachable from *node* via *adjacency* into *out*."""
    if node in out:
        return
    out.add(node)
    for child in adjacency.get(node, []):
        _collect_reachable(child, adjacency, out)


# ─── internal helpers ────────────────────────────────────────────────


def _build_adjacency(
    entities: Mapping[str, EntityNode],
    relationships: Sequence[Relationship],
    warnings: list[str],
) -> dict[str, list[str]]:
    """Build parent→children adjacency from relationships.

    Dangling references (parent or child not in *entities*) are recorded as
    diagnostic strings in *warnings* rather than being silently dropped.
    """
    adj: dict[str, list[str]] = {}
    entity_names = set(entities)

    for rel in relationships:
        parent = rel.parent_entity_name
        child = rel.child_entity_name

        if child not in entity_names:
            msg = (
                f"Dangling relationship {rel.name!r}: "
                f"child entity {child!r} not in entity set (parent={parent!r})."
            )
            _LOG.warning(msg)
            warnings.append(msg)
            continue

        if parent not in entity_names:
            msg = (
                f"Dangling relationship {rel.name!r}: "
                f"parent entity {parent!r} not in entity set (child={child!r})."
            )
            _LOG.warning(msg)
            warnings.append(msg)
            continue

        adj.setdefault(parent, [])
        if child not in adj[parent]:
            adj[parent].append(child)

    return adj


def _all_child_names(adjacency: dict[str, list[str]]) -> set[str]:
    """Return the set of all names that appear as children in *adjacency*."""
    children: set[str] = set()
    for child_list in adjacency.values():
        children.update(child_list)
    return children


def _break_cycles(
    node: str,
    adjacency: dict[str, list[str]],
    path: set[str],
    visited: set[str],
    unlinked: list[str],
) -> None:
    """DFS walk from *node*; break back-edges to avoid cycles.

    Any child that is already on the current DFS *path* is a cycle target —
    remove it from the adjacency list and add it to *unlinked*.
    """
    if node in visited:
        return
    visited.add(node)
    path.add(node)

    children = adjacency.get(node, [])
    safe_children: list[str] = []

    for child in children:
        if child in path:
            # Cycle detected — break the edge
            unlinked.append(child)
        else:
            safe_children.append(child)
            _break_cycles(child, adjacency, path, visited, unlinked)

    adjacency[node] = safe_children
    path.discard(node)


def _populate_children(
    entities: Mapping[str, EntityNode],
    adjacency: dict[str, list[str]],
    warnings: list[str],
) -> Mapping[str, EntityNode]:
    """Keep DAG edges. Occurrence projection owns path-local presentation."""
    claimed: dict[str, str] = {}  # child_name → first parent that claimed it
    result: dict[str, EntityNode] = {}

    for name, entity in entities.items():
        my_children: list[str] = []
        for child in adjacency.get(name, []):
            owner = claimed.get(child)
            if owner is None:
                claimed[child] = name
                my_children.append(child)
            elif owner != name:
                msg = (
                    f"Entity {child!r} has multiple parents "
                    f"(first={owner!r}, also={name!r}); retaining both paths."
                )
                _LOG.warning(msg)
                warnings.append(msg)
                my_children.append(child)
        result[name] = replace(entity, children=tuple(my_children))

    return result
