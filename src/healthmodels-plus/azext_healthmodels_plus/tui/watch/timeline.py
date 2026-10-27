"""Timeline side pane for range/history navigation and compare."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Static

from azext_healthmodels_plus.tui.domain.formatters import format_utc
from azext_healthmodels_plus.tui.domain.history import (
    HistoryBucketHealth,
    HistoryEvent,
    RangeKey,
    SnapshotNode,
    compare_snapshots,
)
from azext_healthmodels_plus.tui.models.enums import HealthState
from azext_healthmodels_plus.tui.watch.sparkline import scale_blocks

# Color is the state; the glyph is what a bucket without a value shows.
BAND_STATES: dict[HistoryBucketHealth, tuple[str, str]] = {
    HistoryBucketHealth.HEALTHY: ("█", HealthState.HEALTHY.color),
    HistoryBucketHealth.DEGRADED: ("█", HealthState.DEGRADED.color),
    HistoryBucketHealth.UNHEALTHY: ("█", HealthState.UNHEALTHY.color),
    HistoryBucketHealth.UNKNOWN: ("█", HealthState.UNKNOWN.color),
    HistoryBucketHealth.NO_COVERAGE: ("·", "bright_black"),
}


def health_band(buckets: Sequence[Any]) -> Text:
    """One glyph per bucket: height is the value within this row's range, color is the worst state."""
    band = Text()
    for bucket, level in zip(buckets, scale_blocks(getattr(bucket, "value", None) for bucket in buckets)):
        health = getattr(bucket, "health", None)
        if health is HistoryBucketHealth.MIXED:
            health = getattr(bucket, "worst_health", None)
        glyph, style = BAND_STATES.get(health, BAND_STATES[HistoryBucketHealth.UNKNOWN])
        band.append(level or glyph, style=style)
    return band


def bucket_size(key: RangeKey) -> timedelta:
    return max((key.end_utc - key.start_utc) / 48, timedelta(minutes=1))


def event_line(event: HistoryEvent) -> str:
    state = f"{event.previous_health.value} -> {event.health.value}" if event.previous_health else event.health.value
    parts = [format_utc(event.occurred_at), state]
    if event.value is not None:
        parts.append(f"{event.value:g}")
    if event.reason:
        parts.append(event.reason)
    return "  ".join(parts)


def node_path_label(canonical: str, nodes: dict[str, SnapshotNode]) -> str:
    node = nodes.get(canonical)
    if node is None:
        return canonical
    names = [node.display_name]
    seen = {canonical}
    parent = node.parent_canonical_name
    while parent and parent not in seen:  # relationships can form cycles
        seen.add(parent)
        parent_node = nodes.get(parent)
        if parent_node is None:
            names.insert(0, parent)
            break
        names.insert(0, parent_node.display_name)
        parent = parent_node.parent_canonical_name
    return " / ".join(names)


def compare_text(
    left_at: datetime, right_at: datetime, left_nodes: dict[str, SnapshotNode], right_nodes: dict[str, SnapshotNode],
) -> str:
    comparison = compare_snapshots(left_nodes, right_nodes)
    lines = [f"Compare {format_utc(left_at)} -> {format_utc(right_at)}"]
    lines.extend(f"added {node_path_label(item, right_nodes)}" for item in comparison.added)
    lines.extend(f"removed {node_path_label(item, left_nodes)}" for item in comparison.removed)
    both = {**left_nodes, **right_nodes}  # an edited node is labeled by its path at B, else at A
    for label, edits in (
        ("health", comparison.health_edits),
        ("value", comparison.value_edits),
        ("config", comparison.configuration_edits),
        ("topology", comparison.topology_edits),
    ):
        lines.extend(
            f"{label} {node_path_label(edit.canonical_name, both)}: {edit.before} -> {edit.after}"
            for edit in edits
        )
    output = "\n".join(lines)
    if len(lines) == 1:
        output += "\nNo differences."
    return output


class TimelinePane(Vertical):
    _filled_go_to = ""  # the time this pane last wrote, so a typed time can be told apart

    @staticmethod
    def _legend_text() -> Text:
        entries = (Text.assemble((glyph, style), f" {health.value}") for health, (glyph, style) in BAND_STATES.items())
        return Text("Legend (height = value, color = worst state): ").append_text(Text(", ").join(entries))

    def compose(self) -> ComposeResult:
        self.can_focus = True
        yield Static("Timeline", id="timeline-title")
        with Horizontal():
            yield Button("1h", id="timeline-range-1h")
            yield Button("6h", id="timeline-range-6h")
            yield Button("24h", id="timeline-range-24h")
            yield Button("7d", id="timeline-range-7d")
        with Horizontal():
            yield Input(placeholder="Time (UTC)", id="timeline-go-to")
            yield Button("Mark", id="timeline-go")
            yield Button("Prev", id="timeline-prev")
            yield Button("Next", id="timeline-next")
        with Horizontal():
            yield Button("Open tree at time", id="timeline-open")
            yield Button("Return to live", id="timeline-live")
        with Horizontal():
            yield Input(placeholder="Compare A UTC", id="timeline-compare-a")
            yield Input(placeholder="Compare B UTC", id="timeline-compare-b")
            yield Button("Compare", id="timeline-compare")
        with VerticalScroll(id="timeline-scroll"):
            yield Static("", id="timeline-applied-at")
            yield Static("No range loaded.", id="timeline-summary")
            yield Static("", id="timeline-progress")
            yield Static(self._legend_text(), id="timeline-legend")
            yield Static("", id="timeline-bands")
            yield Static("", id="timeline-events")

    def set_summary(self, text: str) -> None:
        self.query_one("#timeline-summary", Static).update(Text(text))  # names may contain [ ]

    def set_progress(self, text: str) -> None:
        self.query_one("#timeline-progress", Static).update(Text(text, style="bold cyan"))

    def set_applied_at(self, text: str) -> None:
        self.query_one("#timeline-applied-at", Static).update(Text(text))

    def set_events(self, text: str) -> None:
        self.query_one("#timeline-events", Static).update(Text(text))

    def set_bands(self, text: Text | str) -> None:
        self.query_one("#timeline-bands", Static).update(text if isinstance(text, Text) else Text(text))

    def go_to_value(self) -> str:
        return self.query_one("#timeline-go-to", Input).value.strip()

    def set_go_to_value(self, value: datetime, *, keep_typed: bool = False) -> None:
        if keep_typed and self.go_to_value() not in ("", self._filled_go_to):
            return
        utc = value.astimezone(UTC)
        if utc.microsecond:
            # Whole seconds, rounded up, so opening the tree here shows the state after the event.
            utc = utc.replace(microsecond=0) + timedelta(seconds=1)
        self._filled_go_to = utc.isoformat().replace("+00:00", "Z")
        self.query_one("#timeline-go-to", Input).value = self._filled_go_to

    def compare_a(self) -> str:
        return self.query_one("#timeline-compare-a", Input).value.strip()

    def compare_b(self) -> str:
        return self.query_one("#timeline-compare-b", Input).value.strip()
