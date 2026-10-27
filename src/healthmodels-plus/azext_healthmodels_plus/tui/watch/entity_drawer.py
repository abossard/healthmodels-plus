"""Slide-out entity detail drawer for the health model watch TUI.

Renders a read-only summary of the selected :class:`EntityNode` using the
already-fetched :class:`Forest` — no API calls are made from this widget.
"""
from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from azext_healthmodels_plus.tui.models.domain import EntityNode, Forest, SignalValue
from azext_healthmodels_plus.tui.models.enums import Impact


def _impact_label(impact: Impact) -> str:
    """Human-readable label for :class:`Impact`."""
    _map: dict[Impact, str] = {
        Impact.STANDARD: "Standard",
        Impact.LIMITED: "Limited",
        Impact.NONE: "Suppressed",
        Impact.SUPPRESSED: "Suppressed",
    }
    return _map.get(impact, impact.value)


def find_parent_name(forest: Forest, entity_name: str) -> str | None:
    """Return the entity name whose ``children`` contains *entity_name*, or None."""
    for candidate in forest.entities.values():
        if entity_name in candidate.children:
            return candidate.name
    return None


def render_entity_details(forest: Forest, entity: EntityNode) -> Text:
    """Pure function: produce a Rich Text block describing *entity*."""
    hs = entity.health_state
    text = Text()

    # ── Header ────────────────────────────────────────────────────────
    text.append(f"{hs.icon} ", style=hs.color)
    text.append(entity.display_name, style=f"bold {hs.color}")
    text.append("\n")
    text.append(hs.value, style=f"bold {hs.color}")
    text.append("\n\n")

    # ── Identity ──────────────────────────────────────────────────────
    text.append("── Identity ──\n", style="bold cyan")
    text.append("Type:     ", style="dim")
    text.append(f"{entity.icon_name}\n")
    text.append("Impact:   ", style="dim")
    text.append(f"{_impact_label(entity.impact)}\n")
    text.append("Name:     ", style="dim")
    text.append(f"{entity.name}\n")
    text.append("ARM ID:   ", style="dim")
    text.append(f"{entity.entity_id}\n")
    text.append("\n")

    # ── Relationships ─────────────────────────────────────────────────
    text.append("── Relationships ──\n", style="bold cyan")
    parent_name = find_parent_name(forest, entity.name)
    if parent_name is not None:
        parent = forest.entities.get(parent_name)
        if parent is not None:
            text.append("Parent:   ", style="dim")
            text.append(f"{parent.health_state.icon} ")
            text.append(parent.display_name, style=parent.health_state.color)
            text.append("\n")
        else:
            text.append("Parent:   ", style="dim")
            text.append(f"{parent_name}\n")
    else:
        text.append("Parent:   ", style="dim")
        text.append("(root)\n", style="italic dim")

    text.append(f"Children: {len(entity.children)}\n", style="dim")
    for child_name in entity.children:
        child = forest.entities.get(child_name)
        if child is None:
            text.append(f"  • {child_name} ", style="dim")
            text.append("(missing)\n", style="italic dim")
            continue
        chs = child.health_state
        text.append("  • ")
        text.append(f"{chs.icon} ")
        text.append(child.display_name, style=chs.color)
        text.append(" ─ ", style="dim")
        text.append(chs.value, style=chs.color)
        text.append("\n")
    text.append("\n")

    # ── Signals ───────────────────────────────────────────────────────
    text.append(f"── Signals ({len(entity.signals)}) ──\n", style="bold cyan")
    if not entity.signals:
        text.append("(none)\n", style="italic dim")
    else:
        for sig in entity.signals:
            _append_signal_block(text, sig)
    return text


def _append_signal_block(text: Text, sig: SignalValue) -> None:
    """Append a formatted block for a single signal to *text*."""
    hs = sig.health_state
    text.append("◈ ", style="cyan")
    text.append(sig.display_name, style=f"bold {hs.color}")
    text.append("  ")
    text.append(hs.icon)
    text.append("\n")

    text.append("    kind:       ", style="dim")
    text.append(f"{sig.signal_kind.value} ({sig.signal_kind.short_label})\n")

    text.append("    value:      ", style="dim")
    text.append(sig.formatted_value, style=f"bold {hs.color}")
    text.append(f"  [{sig.data_unit.value}]\n")

    text.append("    state:      ", style="dim")
    text.append(f"{hs.value}\n", style=hs.color)

    # Thresholds and diagnostic info are sourced via getattr so this code
    # works before Phase 3 adds those fields to SignalValue.
    _append_thresholds(text, sig)

    text.append("    reported:   ", style="dim")
    text.append(f"{sig.reported_at}\n")

    text.append("    def-name:   ", style="dim")
    text.append(f"{sig.definition_name}\n")
    text.append("    name:       ", style="dim")
    text.append(f"{sig.name}\n")
    _append_diagnostic_block(text, sig)
    text.append("\n")


def _append_thresholds(text: Text, sig: SignalValue) -> None:
    """Append threshold lines for *sig* using getattr for forward-compat."""
    degraded = getattr(sig, "degraded_rule", None)
    unhealthy = getattr(sig, "unhealthy_rule", None)
    if degraded is None and unhealthy is None:
        text.append("    thresholds: ", style="dim")
        text.append("—\n", style="italic dim")
        return
    if degraded is not None:
        text.append("    degraded:  ", style="dim")
        text.append(f"{getattr(degraded, 'operator', '')} {getattr(degraded, 'threshold', '')}\n")
    if unhealthy is not None:
        text.append("    unhealthy: ", style="dim")
        text.append(f"{getattr(unhealthy, 'operator', '')} {getattr(unhealthy, 'threshold', '')}\n")


def _append_diagnostic_block(text: Text, sig: SignalValue) -> None:
    """Append an error line for *sig* if an error is present."""
    error = getattr(sig, "error", None)
    if not error:
        return
    text.append("    error:    ", style="dim")
    text.append(f"{error}\n", style="red")


class EntityDrawer(VerticalScroll):
    """Right-docked sidebar that shows details for a single entity.

    Toggle visibility with :meth:`toggle` or :meth:`hide`; populate it with
    :meth:`show_entity`.
    """

    DEFAULT_CSS = """
    EntityDrawer {
        display: none;
    }
    EntityDrawer.-visible {
        display: block;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._body: Static = Static(Text("Select an entity and press 'd'."), id="entity-drawer-body")
        self._current_entity_name: str | None = None

    def compose(self):  # type: ignore[override]
        yield self._body

    # ── public API ────────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return self.has_class("-visible")

    @property
    def current_entity_name(self) -> str | None:
        return self._current_entity_name

    def show_entity(self, forest: Forest, entity_name: str, scope: str = "", reference_status: str = "") -> bool:
        """Populate the drawer with *entity_name* from *forest* and reveal it.

        Returns ``True`` if the entity was found and displayed.
        """
        entity = forest.entities.get(entity_name)
        if entity is None:
            return False
        details = render_entity_details(forest, entity)
        if scope:
            details.append(f"\nModel: {scope}", style="dim")
        if entity.reference:
            details.append(f"\nReference: {entity.reference.resource_id}", style="dim")
            if entity.reference.diagnostic:
                details.append(f"\n{entity.reference.diagnostic}", style="red")
            if reference_status:
                details.append(f"\nTarget: {reference_status}", style="yellow")
        self._body.update(details)
        self._current_entity_name = entity_name
        self.add_class("-visible")
        return True

    def hide(self) -> None:
        """Hide the drawer (content is retained)."""
        self.remove_class("-visible")

    def toggle_for(self, forest: Forest, entity_name: str) -> bool:
        """Toggle the drawer for *entity_name*.

        If the drawer is already showing *entity_name*, it is hidden.
        Otherwise it is shown with the new entity's details.
        Returns ``True`` when the drawer is visible after the call.
        """
        if self.is_visible and self._current_entity_name == entity_name:
            self.hide()
            return False
        return self.show_entity(forest, entity_name)
