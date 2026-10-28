"""Signal Verification Panel — live-executes a signal's query and renders results.

Presentation-only widget.  Query execution is delegated to
:func:`azext_healthmodels_plus.tui.client.query_executor.execute_signal`; the panel
never performs I/O directly — callers run the executor in a thread and
push the resulting dict back via :meth:`SignalPanel.show_result`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from azext_healthmodels_plus.tui.domain.formatters import format_utc
from azext_healthmodels_plus.tui.models.domain import EntityNode, SignalValue
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.models.enums import HealthState, SignalKind
from azext_healthmodels_plus.tui.watch.sparkline import (
    extract_history_values,
    render_sparkline,
    render_summary,
)


# ─── Immutable view-model ─────────────────────────────────────────────


@dataclass(frozen=True)
class SignalPanelContext:
    """Identity + cached domain data for the signal currently pinned."""

    entity_name: str  # GUID of owning entity (for execute_signal)
    signal_name: str  # GUID of signal instance
    entity_display: str
    signal: SignalValue | None  # May be None if not found in forest
    model: ModelKey | None = None
    occurrence_path: tuple[str, ...] = ()


# ─── Widget ───────────────────────────────────────────────────────────


class SignalPanel(VerticalScroll):
    """Right-docked drawer that displays a signal and verifies its query.

    The panel has two phases:

    1. *Static*  — shows metadata derived from the domain ``SignalValue``
       (name, kind, current health state, last reported value).
    2. *Verified* — after :meth:`show_result` is called with the output
       of :func:`execute_signal`, shows query text, data source, rules,
       raw value, computed health, duration, and raw output.
    """


    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx: SignalPanelContext | None = None
        self._busy: bool = False

    # ── layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:  # noqa: D102
        yield Static("", id="signal-panel-header", classes="sp-header")
        yield Static("", id="signal-panel-meta", classes="sp-section")
        yield Button("Verify query (v)", id="signal-panel-verify", variant="primary")
        yield Static("", id="signal-panel-status", classes="sp-status")
        yield Static("", id="signal-panel-result", classes="sp-section")
        yield Static("", id="signal-panel-sparkline", classes="sp-sparkline")
        yield Static("", id="signal-panel-raw", classes="sp-raw")

    # ── public API ────────────────────────────────────────────────────

    def set_signal(self, ctx: SignalPanelContext) -> None:
        """Pin *ctx* as the active signal and render static metadata."""
        self._ctx = ctx
        self._busy = False
        self._render_header()
        self._render_meta()
        self._clear_result()

    def clear_signal(self) -> None:
        """Reset the panel to an empty state."""
        self._ctx = None
        self._busy = False
        self.query_one("#signal-panel-header", Static).update("")
        self.query_one("#signal-panel-meta", Static).update(
            Text("Select a signal and press 'v' to verify.", style="dim italic")
        )
        self._clear_result()

    @property
    def context(self) -> SignalPanelContext | None:
        return self._ctx

    @property
    def is_busy(self) -> bool:
        return self._busy

    def mark_verifying(self, *, queued: bool = False) -> None:
        """Indicate that a verification request is in flight."""
        self._busy = True
        self.query_one("#signal-panel-status", Static).update(
            Text("Queued query verification…" if queued else "⏳ Executing query…", style="bold yellow")
        )
        self.query_one("#signal-panel-result", Static).update("")
        self.query_one("#signal-panel-sparkline", Static).update(
            Text("history pending query…", style="dim italic")
        )
        self.query_one("#signal-panel-raw", Static).update("")

    def mark_history_loading(self) -> None:
        self.query_one("#signal-panel-sparkline", Static).update(
            Text("loading history…", style="dim italic")
        )

    def show_result(self, result: dict[str, Any]) -> None:
        """Render the output of :func:`execute_signal`."""
        self._busy = False

        err = result.get("error")
        status = self.query_one("#signal-panel-status", Static)
        if err:
            status.update(Text(f"✖ Error: {err}", style="bold red"))
        else:
            duration = result.get("durationMs", 0)
            ts = result.get("timestamp", "")
            status.update(
                Text.assemble(
                    ("✔ Verified", "bold green"),
                    (f"  ({duration} ms at {format_utc(ts)})", "dim"),
                )
            )

        self.query_one("#signal-panel-result", Static).update(
            _format_result_block(result)
        )
        self.query_one("#signal-panel-raw", Static).update(
            _format_raw_output(result.get("rawOutput"))
        )

    def show_exception(self, exc: BaseException) -> None:
        """Render an unexpected failure from the executor."""
        self._busy = False
        self.query_one("#signal-panel-status", Static).update(
            Text(f"✖ {type(exc).__name__}: {exc}", style="bold red")
        )
        self.query_one("#signal-panel-result", Static).update("")
        self.query_one("#signal-panel-sparkline", Static).update("")
        self.query_one("#signal-panel-raw", Static).update("")

    def show_history(self, history: Any) -> None:
        """Render the sparkline + summary for a ``getSignalHistory`` response.

        Accepts the raw response dict, a pre-extracted list of floats,
        an exception (rendered as a placeholder), or ``None``.  Never
        raises — the sparkline is a visual enhancement.
        """
        target = self.query_one("#signal-panel-sparkline", Static)

        if isinstance(history, BaseException):
            target.update(
                Text(f"history unavailable: {history}", style="dim italic")
            )
            return

        if isinstance(history, list) and (
            not history or all(isinstance(v, (int, float)) for v in history)
        ):
            values = [float(v) for v in history]
        else:
            try:
                values = extract_history_values(history)
            except Exception:  # noqa: BLE001 — defensive: never crash on history
                values = []

        state = self._ctx.signal.health_state if self._ctx and self._ctx.signal else None
        target.update(_format_history_block(values, state))

    # ── internal rendering ────────────────────────────────────────────

    def _render_header(self) -> None:
        assert self._ctx is not None
        sig = self._ctx.signal
        display_name = sig.display_name if sig is not None else self._ctx.signal_name
        kind_label = _kind_label(sig.signal_kind if sig is not None else None)

        t = Text()
        t.append("◈ ", style="cyan bold")
        t.append(display_name, style="bold")
        t.append("  ")
        t.append(f"[{kind_label}]", style="dim")
        t.append("\n")
        t.append("on ", style="dim")
        t.append(self._ctx.entity_display, style="italic")
        self.query_one("#signal-panel-header", Static).update(t)

    def _render_meta(self) -> None:
        assert self._ctx is not None
        sig = self._ctx.signal

        t = Text()
        if sig is None:
            t.append(
                "No cached data for this signal — press the Verify button to execute.",
                style="dim italic",
            )
            self.query_one("#signal-panel-meta", Static).update(t)
            return

        hs = sig.health_state
        t.append("State:   ", style="dim")
        t.append(f"{hs.icon} {hs.value}", style=f"bold {hs.color}")
        t.append("\n")
        t.append("Value:   ", style="dim")
        t.append(sig.formatted_value, style="bold")
        t.append(f"  ({sig.data_unit.value})", style="dim")
        t.append("\n")
        t.append("Updated: ", style="dim")
        t.append(format_utc(sig.reported_at or "—"), style="italic")
        t.append("\n")
        t.append("Instance: ", style="dim")
        t.append(sig.name, style="cyan")
        if sig.error:
            t.append("\n")
            t.append("Error:   ", style="dim")
            t.append(sig.error, style="red")

        self.query_one("#signal-panel-meta", Static).update(t)

    def _clear_result(self) -> None:
        self.query_one("#signal-panel-status", Static).update(
            Text("Press 'v' or click Verify to run the query.", style="dim")
        )
        self.query_one("#signal-panel-result", Static).update("")
        self.query_one("#signal-panel-sparkline", Static).update("")
        self.query_one("#signal-panel-raw", Static).update("")


# ─── Pure formatters ──────────────────────────────────────────────────


def _format_history_block(
    values: list[float], state: HealthState | None
) -> Text:
    """Render the sparkline row: label · chart · summary."""
    t = Text()
    t.append("Trend (1h): ", style="dim underline")
    t.append("\n")
    t.append(render_sparkline(values, width=30, state=state))
    t.append("  ")
    t.append(render_summary(values))
    return t


def _kind_label(kind: SignalKind | None) -> str:
    if kind is None:
        return "Signal"
    return kind.short_label


def _format_rule(label: str, rule: dict[str, Any] | None) -> Text:
    t = Text()
    t.append(f"{label + ':':<11}", style="dim")
    if not rule:
        t.append("—", style="dim")
        return t
    op = rule.get("operator", "?")
    threshold = rule.get("threshold", "?")
    t.append(f"{op} {threshold}", style="bold")
    return t


def _format_result_block(result: dict[str, Any]) -> Text:
    t = Text()

    # Computed health state
    health_str = result.get("healthState", "Unknown")
    try:
        hs = HealthState(health_str)
        t.append("Evaluated: ", style="dim")
        t.append(f"{hs.icon} {hs.value}", style=f"bold {hs.color}")
    except ValueError:
        t.append("Evaluated: ", style="dim")
        t.append(health_str, style="bold red")
    t.append("\n")

    warning = result.get("evaluationWarning")
    if warning:
        t.append("Evaluation: ", style="dim")
        t.append(str(warning), style="yellow")
        t.append("\n")

    # Raw value
    raw_value = result.get("rawValue")
    data_unit = result.get("dataUnit", "")
    t.append("Raw value: ", style="dim")
    if raw_value is None:
        t.append("—", style="dim italic")
    else:
        t.append(f"{raw_value}", style="bold")
        if data_unit:
            t.append(f" {data_unit}", style="dim")
    t.append("\n\n")

    # Thresholds
    rules = result.get("evaluationRules") or {}
    t.append(_format_rule("Degraded", rules.get("degradedRule")))
    t.append("\n")
    t.append(_format_rule("Unhealthy", rules.get("unhealthyRule")))
    t.append("\n\n")

    # Query
    query_text = result.get("query", "")
    t.append("Query:\n", style="dim underline")
    t.append(query_text or "—", style="cyan")
    t.append("\n\n")

    # Data source
    t.append("Data source:\n", style="dim underline")
    t.append(result.get("dataSource", "") or "—", style="italic")
    t.append("\n")
    group = result.get("signalGroup", "")
    if group:
        t.append(f"(group: {group})", style="dim")

    # Structured error diagnostics
    if result.get("error"):
        err_type = result.get("errorType")
        err_status = result.get("errorStatusCode")
        err_code = result.get("errorCode")
        if err_type or err_status or err_code:
            t.append("\n\n")
            if err_type:
                t.append("Error type:  ", style="dim")
                t.append(f"{err_type}", style="bold red")
                t.append("\n")
            if err_status:
                t.append("HTTP status: ", style="dim")
                t.append(f"{err_status}", style="bold red")
                t.append("\n")
            if err_code:
                t.append("Error code:  ", style="dim")
                t.append(f"{err_code}", style="bold red")

    return t


def _format_raw_output(raw: Any) -> Text:
    t = Text()
    if raw is None:
        return t
    import json

    t.append("Raw output:\n", style="dim underline")
    try:
        pretty = json.dumps(raw, indent=2, default=str)
    except (TypeError, ValueError):
        pretty = str(raw)
    # Truncate very long blobs so the drawer stays usable.
    if len(pretty) > 4000:
        pretty = pretty[:4000] + "\n… (truncated)"
    t.append(pretty, style="dim")
    return t


# ─── Helpers for callers ──────────────────────────────────────────────


def build_context(
    entity: EntityNode | None,
    signal_name: str,
    owner_entity_name: str,
) -> SignalPanelContext:
    """Build a :class:`SignalPanelContext` from domain objects.

    Pure function — performs no I/O.  *entity* may be ``None`` when the
    owning entity is missing from the forest (unusual but possible).
    """
    signal: SignalValue | None = None
    entity_display = owner_entity_name
    if entity is not None:
        entity_display = entity.display_name or entity.name
        for sig in entity.signals:
            if sig.name == signal_name:
                signal = sig
                break
    return SignalPanelContext(
        entity_name=owner_entity_name,
        signal_name=signal_name,
        entity_display=entity_display,
        signal=signal,
    )
