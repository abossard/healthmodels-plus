"""Bottom status bar for the Textual watch TUI."""
from __future__ import annotations

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


def _truncate_status_error(message: str, limit: int = 80) -> str:
    """Normalize whitespace and truncate for single-line display."""
    normalized = " ".join(message.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit - 1] + "…"


_HINTS = (
    ("F1", "Help"), ("F2", "Actions"), ("Tab", "Focus"), ("Enter", "Activate"), ("/", "Search"),
    ("n", "Next"), ("p", "Prev"), ("v", "Verify"), ("d", "Details"), ("e", "Edit"), ("j", "Jump"),
    ("r", "Refresh"), ("q", "Quit"),
)


class StatusBar(Static):
    """Displays connection state, countdown, change count, and key hints."""

    poll_countdown: reactive[int] = reactive(30)
    change_count: reactive[int] = reactive(0)
    connected: reactive[bool] = reactive(True)
    auto_jump: reactive[bool] = reactive(True)
    has_search_results: reactive[bool] = reactive(False)
    error_text: reactive[str] = reactive("")
    model_work: reactive[str] = reactive("")
    snapshot_at: reactive[str] = reactive("")

    def render(self) -> Text:  # noqa: D102
        parts = Text()
        if self.snapshot_at:
            parts.append(f"◷ Snapshot {self.snapshot_at}", style="bold yellow")
            parts.append(" │ ", style="dim")

        # Connection indicator
        if self.connected:
            parts.append("● Connected", style="bold green")
        else:
            parts.append("○ Disconnected", style="bold red")
            if self.error_text:
                parts.append(" — ", style="dim")
                parts.append(_truncate_status_error(self.error_text), style="dim red")

        parts.append(" │ ", style="dim")
        parts.append(f"Next poll in {self.poll_countdown}s", style="cyan")
        parts.append(" │ ", style="dim")
        style = "bold red" if self.change_count > 0 else "dim"
        parts.append(f"{self.change_count} escalations", style=style)
        parts.append(" │ ", style="dim")
        jump_label = "on" if self.auto_jump else "off"
        parts.append(f"Auto-jump: {jump_label}", style="italic")
        # Model work varies in length, so it goes last where the ellipsis may cut it.
        if self.model_work:
            parts.append(" │ ", style="dim")
            parts.append(self.model_work, style="dim")

        # Only whole hints that fit; F1 Help lists every shortcut.
        parts.append("\n")
        line_start = parts.cell_len
        room = self.content_size.width  # 0 before layout: no limit
        for key, label in _HINTS:
            if key in ("n", "p") and not self.has_search_results:
                continue
            hint = Text.assemble("  " if parts.cell_len > line_start else "", (key, "bold"), (f" {label}", "dim"))
            if room and parts.cell_len - line_start + hint.cell_len > room:
                break
            parts.append_text(hint)
        return parts
