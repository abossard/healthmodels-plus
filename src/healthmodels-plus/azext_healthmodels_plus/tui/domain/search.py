"""In-memory search over a health model Forest — pure calculation."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Sequence
from azext_healthmodels_plus.tui.domain.occurrences import Occurrence
from azext_healthmodels_plus.tui.models.domain import Forest, SearchResult


def search_forest(forest: Forest, query: str) -> list[SearchResult]:
    """Return entities and signals whose display_name matches *query*.

    Matching is case-insensitive.  Results are sorted: prefix matches
    first, then substring matches; entities before signals within each
    group.
    """
    if not query:
        return []

    q = query.lower()
    results: list[SearchResult] = []
    for entity in forest.entities.values():
        if q in entity.display_name.lower():
            results.append(SearchResult(
                entity_id=entity.entity_id,
                display_name=entity.display_name,
                is_signal=False,
                health_state=entity.health_state,
            ))
        results.extend(
            SearchResult(
                entity_id=entity.entity_id,
                display_name=sig.display_name,
                is_signal=True,
                health_state=sig.health_state,
                signal_value=sig.formatted_value,
                parent_display_name=entity.display_name,
                signal_name=sig.name,
            )
            for sig in entity.signals
            if q in sig.display_name.lower()
        )
    return sorted(results, key=lambda r: (not r.display_name.lower().startswith(q), r.is_signal))


def search_occurrences(rows: Sequence[Occurrence], query: str) -> list[SearchResult]:
    results = []
    for row in rows:
        for match in search_forest(Forest((row.entity.name,), {row.entity.name: row.entity}), query):
            path = row.path + (("signals/" + match.signal_name.casefold(),) if match.signal_name else ())
            results.append(replace(match, model=row.model, occurrence_path=path))
    return sorted(results, key=lambda r: (not r.display_name.casefold().startswith(query.casefold()), r.is_signal))
