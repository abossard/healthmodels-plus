"""Snapshot building and diffing — pure calculations, no I/O.

A ``Snapshot`` captures the health state of every entity at a point in
time.  ``diff_snapshots`` compares two snapshots and returns a sorted
list of ``StateChange`` objects.
"""
from __future__ import annotations

from typing import Mapping

from azext_healthmodels_plus.tui.models.domain import (
    EntityState,
    Forest,
    Snapshot,
    StateChange,
)
from azext_healthmodels_plus.tui.models.enums import ChangeKind, HealthState


# ─── change-map helper ───────────────────────────────────────────────


def build_change_map(changes: list[StateChange]) -> dict[str, StateChange]:
    """Collapse a list of changes into entity_id → first change seen.

    Pure calculation. Since ``diff_snapshots`` returns changes sorted by
    priority (escalations first, then recoveries, etc.), the first entry
    per ``entity_id`` is the highest-priority change for that entity.
    """
    change_map: dict[str, StateChange] = {}
    for ch in changes:
        if ch.entity_id not in change_map:
            change_map[ch.entity_id] = ch
    return change_map


# ─── snapshot construction ───────────────────────────────────────────


def build_snapshot(forest: Forest, timestamp: str) -> Snapshot:
    """Build a flat snapshot from a forest — pure calculation.

    For every entity in the forest, captures the entity-level health
    state and per-signal health states and values.
    """
    states: dict[str, EntityState] = {}

    for name, entity in forest.entities.items():
        states[name] = EntityState(
            entity_id=entity.entity_id,
            display_name=entity.display_name,
            health_state=entity.health_state,
            signal_states={
                sig.name: sig.health_state for sig in entity.signals
            },
            signal_values={
                sig.name: sig.value for sig in entity.signals
            },
        )

    return Snapshot(entity_states=states, timestamp=timestamp)


# ─── snapshot diffing ────────────────────────────────────────────────


def diff_snapshots(
    old: Snapshot | None,
    new: Snapshot,
) -> list[StateChange]:
    """Diff two snapshots, returning a sorted list of state changes.

    Handles:
    - ``old is None`` → every entity in *new* is reported as NEW.
    - Entity present in *new* but absent from *old* → NEW.
    - Entity present in *old* but absent from *new* → REMOVED.
    - Entity health state changed → ESCALATION or RECOVERY.
    - Signal values changed (entity health unchanged) → VALUE_CHANGED.

    The result is sorted: escalations first (highest severity delta),
    then recoveries, then new/removed, then value-only changes.
    """
    if old is None:
        changes = [
            StateChange(
                entity_id=es.entity_id,
                entity_display_name=es.display_name,
                kind=ChangeKind.NEW,
                old_state=None,
                new_state=es.health_state,
            )
            for es in new.entity_states.values()
        ]
        return _sort_changes(changes)

    changes: list[StateChange] = []
    old_states = old.entity_states
    new_states = new.entity_states

    # Entities in new but not old → NEW
    for name, new_es in new_states.items():
        if name not in old_states:
            changes.append(
                StateChange(
                    entity_id=new_es.entity_id,
                    entity_display_name=new_es.display_name,
                    kind=ChangeKind.NEW,
                    old_state=None,
                    new_state=new_es.health_state,
                )
            )
            continue

        old_es = old_states[name]

        # Entity-level health change → record it, then ALSO diff signals.
        if new_es.health_state != old_es.health_state:
            kind = _classify_health_change(old_es.health_state, new_es.health_state)
            changes.append(
                StateChange(
                    entity_id=new_es.entity_id,
                    entity_display_name=new_es.display_name,
                    kind=kind,
                    old_state=old_es.health_state,
                    new_state=new_es.health_state,
                )
            )

        # Signal-level diffs (always run, even when entity health changed)
        changes.extend(_diff_signals(old_es, new_es))

    # Entities in old but not new → REMOVED
    for name, old_es in old_states.items():
        if name not in new_states:
            changes.append(
                StateChange(
                    entity_id=old_es.entity_id,
                    entity_display_name=old_es.display_name,
                    kind=ChangeKind.REMOVED,
                    old_state=old_es.health_state,
                    new_state=None,
                )
            )

    return _sort_changes(changes)


# ─── internal helpers ────────────────────────────────────────────────


def _classify_health_change(
    old_state: HealthState,
    new_state: HealthState,
) -> ChangeKind:
    """Classify a health-state transition as escalation or recovery."""
    if new_state.severity > old_state.severity:
        return ChangeKind.ESCALATION
    return ChangeKind.RECOVERY


def _diff_signals(
    old_es: EntityState,
    new_es: EntityState,
) -> list[StateChange]:
    """Detect added/removed signals, value changes, and health-state changes."""
    changes: list[StateChange] = []

    old_keys = set(old_es.signal_states) | set(old_es.signal_values)
    new_keys = set(new_es.signal_states) | set(new_es.signal_values)

    # Added signals → NEW
    for sig_name in sorted(new_keys - old_keys):
        new_state = new_es.signal_states.get(sig_name, HealthState.UNKNOWN)
        changes.append(
            StateChange(
                entity_id=new_es.entity_id,
                entity_display_name=new_es.display_name,
                kind=ChangeKind.NEW,
                old_state=None,
                new_state=new_state,
                signal_name=sig_name,
            )
        )

    # Removed signals → REMOVED
    for sig_name in sorted(old_keys - new_keys):
        old_state = old_es.signal_states.get(sig_name, HealthState.UNKNOWN)
        changes.append(
            StateChange(
                entity_id=new_es.entity_id,
                entity_display_name=new_es.display_name,
                kind=ChangeKind.REMOVED,
                old_state=old_state,
                new_state=None,
                signal_name=sig_name,
            )
        )

    # Signals present on both sides — diff health state and value.
    for sig_name in sorted(old_keys & new_keys):
        old_state = old_es.signal_states.get(sig_name, HealthState.UNKNOWN)
        new_state = new_es.signal_states.get(sig_name, HealthState.UNKNOWN)
        old_val = old_es.signal_values.get(sig_name)
        new_val = new_es.signal_values.get(sig_name)

        if old_state != new_state:
            # Pick escalation/recovery by severity; same-severity-different-state
            # falls back to VALUE_CHANGED so StateChange invariants hold.
            if new_state.severity > old_state.severity:
                kind = ChangeKind.ESCALATION
            elif new_state.severity < old_state.severity:
                kind = ChangeKind.RECOVERY
            else:
                kind = ChangeKind.VALUE_CHANGED
            changes.append(
                StateChange(
                    entity_id=new_es.entity_id,
                    entity_display_name=new_es.display_name,
                    kind=kind,
                    old_state=old_state,
                    new_state=new_state,
                    signal_name=sig_name,
                )
            )
        elif old_val != new_val:
            changes.append(
                StateChange(
                    entity_id=new_es.entity_id,
                    entity_display_name=new_es.display_name,
                    kind=ChangeKind.VALUE_CHANGED,
                    old_state=old_state,
                    new_state=new_state,
                    signal_name=sig_name,
                )
            )

    return changes


def _sort_changes(changes: list[StateChange]) -> list[StateChange]:
    """Sort changes: escalations first, then recoveries, then others.

    Within escalations, higher severity delta sorts first.
    """
    return sorted(changes, key=_change_sort_key)


def _change_sort_key(change: StateChange) -> tuple[int, int, str]:
    """Return a sort key for a ``StateChange``.

    Priority order (lower = first):
      0 — ESCALATION  (sub-sorted by severity delta descending)
      1 — RECOVERY
      2 — NEW
      3 — REMOVED
      4 — VALUE_CHANGED
    """
    priority_map = {
        ChangeKind.ESCALATION: 0,
        ChangeKind.RECOVERY: 1,
        ChangeKind.NEW: 2,
        ChangeKind.REMOVED: 3,
        ChangeKind.VALUE_CHANGED: 4,
    }
    priority = priority_map.get(change.kind, 5)

    # For escalations, sort by severity delta descending (negate for ascending sort)
    severity_delta = 0
    if change.old_state is not None and change.new_state is not None:
        severity_delta = -(change.new_state.severity - change.old_state.severity)

    return (priority, severity_delta, change.entity_display_name)
