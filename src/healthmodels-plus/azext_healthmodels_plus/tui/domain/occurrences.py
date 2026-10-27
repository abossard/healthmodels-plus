"""Iterative, path-local projection of shared model snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.models.domain import EntityNode, Forest

Path = tuple[str, ...]


@dataclass(frozen=True)
class Occurrence:
    path: Path
    parent: Path
    model: ModelKey
    entity: EntityNode
    cycle: bool = False


def project(
    root: ModelKey,
    forests: Mapping[ModelKey, Forest],
    expanded: set[Path] | None = None,
) -> list[Occurrence]:
    """None expands cached scope for search; a set expands only those hops."""
    forest = forests.get(root)
    if forest is None:
        return []
    work = [(root, n, (), frozenset(), frozenset({root})) for n in reversed(
        forest.roots + tuple(n for n in forest.unlinked if n not in forest.roots)
    )]
    result = []
    while work:
        key, name, parent, ancestors, models = work.pop()
        forest = forests[key]
        entity = forest.entities.get(name)
        if entity is None:
            continue
        identity = (key, name.casefold())
        path = parent + ("/".join(key.canonical) + "/entities/" + name.casefold(),)
        reference = entity.reference
        cycle = identity in ancestors or bool(reference and reference.target in models)
        row = Occurrence(path, parent, key, entity, cycle)
        result.append(row)
        if cycle:
            continue
        next_ancestors = ancestors | {identity}
        for child in reversed(entity.children):
            work.append((key, child, path, next_ancestors, models))
        if reference and reference.target and (expanded is None or path in expanded):
            target = reference.target
            nested = forests.get(target)
            if nested is not None:
                named_root = next((n for n in nested.entities if n.casefold() == target.name.casefold()), None)
                if named_root is not None:
                    work.append((target, named_root, path, next_ancestors, models | {target}))
    return result
