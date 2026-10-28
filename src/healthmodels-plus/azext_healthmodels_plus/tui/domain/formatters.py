"""Rich Text formatting for health-model tree labels — pure calculations.

Every function here is a pure mapping from domain objects to ``rich.text.Text``
or plain strings.  No I/O, no mutable state, no side effects.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from rich.text import Text

from azext_healthmodels_plus.tui.domain.history import parse_utc_timestamp
from azext_healthmodels_plus.tui.domain.snapshot import build_change_map
from azext_healthmodels_plus.tui.models.domain import (
    EntityNode,
    Forest,
    SignalValue,
    StateChange,
)


# ─── timestamps ──────────────────────────────────────────────────────


def format_utc(value: datetime | str) -> str:
    """Short display form such as ``2026-10-24 13:11:37Z``; unparsable text is returned as is."""
    if isinstance(value, str):
        try:
            value = parse_utc_timestamp(value)
        except ValueError:
            return value
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


# ─── entity label ────────────────────────────────────────────────────


def format_entity_label(
    entity: EntityNode,
    change: StateChange | None = None,
) -> Text:
    """Pure function: EntityNode → Rich Text label for a tree node.

    Format::

        {icon} {display_name} ─── {health_state}

    If *change* is an escalation, appends ``⚡ was {old_icon}``.
    """
    hs = entity.health_state
    label = Text()

    label.append(f"{hs.icon} ")
    label.append(entity.display_name, style=f"bold {hs.color}")
    label.append(" ─── ", style="dim")
    label.append(hs.value, style=hs.color)

    if change is not None and change.is_escalation and change.old_state is not None:
        label.append(f" ⚡ was {change.old_state.icon}", style="reverse yellow")

    return label


# ─── signal label ────────────────────────────────────────────────────

_SIGNAL_NAME_WIDTH: int = 30


def format_signal_label(
    signal: SignalValue,
    prev_signal: SignalValue | None = None,
) -> Text:
    """Pure function: SignalValue → Rich Text label for a signal leaf node.

    Format::

        ◈ {display_name} ·····  {formatted_value}  {icon}

    If *prev_signal* existed and the value changed, appends
    ``↑ was {old_value}``.
    """
    hs = signal.health_state
    label = Text()

    label.append("◈ ", style="cyan")

    # Pad display_name with dots to fixed width
    name = signal.display_name
    if len(name) < _SIGNAL_NAME_WIDTH:
        padded = name + " " + "·" * (_SIGNAL_NAME_WIDTH - len(name) - 1)
    else:
        padded = name if len(name) == _SIGNAL_NAME_WIDTH else name[:_SIGNAL_NAME_WIDTH - 1] + "…"
    label.append(padded, style="dim")

    label.append("  ")
    label.append(signal.formatted_value, style=f"bold {hs.color}")
    label.append(f"  {hs.icon}")

    if prev_signal is not None and prev_signal.value != signal.value:
        old_formatted = prev_signal.formatted_value
        label.append(f" ↑ was {old_formatted}", style="italic yellow")

    return label


# ─── plain-text tree ─────────────────────────────────────────────────


def format_plain_tree(
    forest: Forest,
    changes: list[StateChange] | None = None,
) -> str:
    """Format the full forest as a plain-text string with tree guide chars.

    Uses ``├──``, ``└──``, ``│`` to draw the hierarchy, with emojis and
    signal values.  Suitable for terminal output without Rich rendering.
    """
    change_map = build_change_map(changes or [])

    lines: list[str] = []

    for i, root_name in enumerate(forest.roots):
        is_last_root = i == len(forest.roots) - 1
        entity = forest.entities.get(root_name)
        if entity is None:
            continue
        _render_node(
            entity, forest, change_map, lines,
            prefix="", is_last=is_last_root, visited=set(),
        )

    # Append unlinked entities at the end
    if forest.unlinked:
        lines.append("")
        lines.append("⚠ Unlinked entities:")
        for name in forest.unlinked:
            entity = forest.entities.get(name)
            if entity is not None:
                lines.append(f"  {entity.health_state.icon} {entity.display_name}")

    return "\n".join(lines)


# ─── internal helpers ────────────────────────────────────────────────


def _render_node(
    entity: EntityNode,
    forest: Forest,
    change_map: dict[str, StateChange],
    lines: list[str],
    prefix: str,
    is_last: bool,
    visited: set[str] | None = None,
) -> None:
    """Recursively render a single entity node and its children.

    *visited* tracks entity names already rendered on the current path; if a
    child is already in this set, we render a ``[cycle: …]`` marker instead
    of recursing into it. This makes the formatter robust against cyclic
    forests (which graph_builder normally breaks, but defence in depth
    protects against bugs and externally-constructed forests).
    """
    if visited is None:
        visited = set()
    connector = "└── " if is_last else "├── "
    change = change_map.get(entity.entity_id)

    # Entity line
    hs = entity.health_state
    line = f"{prefix}{connector}{hs.icon} {entity.display_name} ─── {hs.value}"
    if change is not None and change.is_escalation and change.old_state is not None:
        line += f" ⚡ was {change.old_state.icon}"
    lines.append(line)

    visited = visited | {entity.name}

    # Prepare prefix for children / signals
    child_prefix = prefix + ("    " if is_last else "│   ")

    # Signals
    child_items: list[str] = list(entity.children)
    total_items = len(entity.signals) + len(child_items)
    for idx, signal in enumerate(entity.signals):
        is_last_item = (idx == total_items - 1)
        sig_connector = "└── " if is_last_item else "├── "
        sig_line = (
            f"{child_prefix}{sig_connector}"
            f"◈ {signal.display_name} ── {signal.formatted_value} {signal.health_state.icon}"
        )
        lines.append(sig_line)

    # Child entities
    for idx, child_name in enumerate(child_items):
        is_last_child = (
            len(entity.signals) + idx == total_items - 1
        )
        if child_name in visited:
            cycle_connector = "└── " if is_last_child else "├── "
            lines.append(f"{child_prefix}{cycle_connector}↺ [cycle: {child_name}]")
            continue
        child_entity = forest.entities.get(child_name)
        if child_entity is None:
            continue
        _render_node(
            child_entity, forest, change_map, lines,
            prefix=child_prefix, is_last=is_last_child, visited=visited,
        )
