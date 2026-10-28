"""Main Textual application for ``az monitor health-models tui``."""
from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from dataclasses import replace
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Label, Static
from textual.widgets import Input, TextArea

from azext_healthmodels_plus.tui.client.query_executor import execute_signal
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.domain.history import (
    RangeKey,
    HistoryBucketHealth,
    SnapshotKey,
    bucket_history,
    parse_utc_timestamp,
    project_snapshot_nodes,
)
from azext_healthmodels_plus.tui.domain.drafts import (
    MAX_NAME_LENGTH, ApplyPlan, ResourceTarget, changed_paths, get_in, parse_field_path,
)
from azext_healthmodels_plus.tui.domain.formatters import format_utc
from azext_healthmodels_plus.tui.models.domain import Forest, SearchResult
from azext_healthmodels_plus.tui.domain.occurrences import project
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.watch.apply import ApplyCoordinator, ApplyResult
from azext_healthmodels_plus.tui.watch.changes import FIELDS, ChangesPane, field_input_id
from azext_healthmodels_plus.tui.watch.draft_store import DraftSnapshot, DraftStore
from azext_healthmodels_plus.tui.watch.entity_drawer import EntityDrawer
from azext_healthmodels_plus.tui.watch.health_tree import EntityData, HealthTree
from azext_healthmodels_plus.tui.watch.poller import Failed, MonitorView, Poller, Ready, Stopped
from azext_healthmodels_plus.tui.watch.query_editor import QueryEditor
from azext_healthmodels_plus.tui.watch.shortcuts_dialog import ActionsDialog, HelpDialog
from azext_healthmodels_plus.tui.watch.search_modal import SearchModal
from azext_healthmodels_plus.tui.watch.signal_panel import SignalPanel, SignalPanelContext, build_context
from azext_healthmodels_plus.tui.watch.status_bar import StatusBar
from azext_healthmodels_plus.tui.watch.timeline import TimelinePane, bucket_size, compare_text, event_line, health_band

_SETTLED = (Ready, Failed, Stopped)
_ROW_PENDING = frozenset({"queued", "loading"})
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Label plus a 48-bucket band fits one line in the side pane from 120 columns up.
_TIMELINE_LABEL_WIDTH = 20
_APPLY_RUNNING = "An apply is still writing; wait for its result."
_TIMELINE_RANGE_HOURS = {
    "timeline-range-1h": 1,
    "timeline-range-6h": 6,
    "timeline-range-24h": 24,
    "timeline-range-7d": 24 * 7,
}


class QuitDraftsDialog(ModalScreen[str]):
    # Enter stays with the focused button so Save/Discard work from the keyboard.
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(id="shortcut-dialog"):
            yield Label("Unsaved drafts", classes="shortcut-title")
            yield Static("Some drafts are not saved to a file. Review them in Changes, discard them and quit, or cancel.")
            with Horizontal(id="quit-dialog-actions"):
                yield Button("Review drafts", id="quit-save")
                yield Button("Discard and quit", id="quit-discard")
                yield Button("Cancel", id="quit-cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit-save":
            self.dismiss("save")
            return
        if event.button.id == "quit-discard":
            self.dismiss("discard")
            return
        self.dismiss("cancel")


class HealthWatchApp(App[str | None]):
    """Interactive TUI that polls the health model and renders a live tree."""

    CSS_PATH = "styles.tcss"
    TITLE = "Health Model Watch"
    # Below 110 columns side panes take the full width; below 30 rows the Changes result is compact (see styles.tcss).
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (30, "-tall")]
    RESULT_BACK_TO_MODELS = "back-to-models"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit"),
        # Plain digits too: most terminals never send Ctrl+digit.
        Binding("1", "show_tree", "Tree tab"),
        Binding("2", "show_timeline", "Timeline tab"),
        Binding("3", "show_changes", "Changes tab"),
        Binding("ctrl+1", "show_tree", "Tree tab"),
        Binding("ctrl+2", "show_timeline", "Timeline tab"),
        Binding("ctrl+3", "show_changes", "Changes tab"),
        Binding("f1", "open_help", "Help"),
        Binding("f2", "open_actions", "Actions"),
        Binding("slash", "open_search", "Search"),
        Binding("e", "edit_query", "Edit (query or draft)"),
        Binding("j", "toggle_autojump", "Toggle auto-jump"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("n", "next_match", "Next match"),
        Binding("p", "prev_match", "Prev match"),
        Binding("v", "verify_signal", "Verify signal"),
        Binding("escape", "close_panel", "Close panel", show=False),
        Binding("plus", "increase_interval", "Poll interval +10 s"),
        Binding("minus", "decrease_interval", "Poll interval -10 s"),
        Binding("d", "show_details", "Details"),
    ]

    auto_jump: reactive[bool] = reactive(True)

    def __init__(
        self,
        client: CloudHealthClient,
        rg: str,
        model: str,
        poll_interval: int = 30,
        *,
        allow_back_to_models: bool = False,
    ) -> None:
        super().__init__()
        self._poller = Poller(client, rg, model)
        self._client = client
        self._poll_interval = poll_interval
        self._model = model
        self._forest: Forest | None = None
        self._search_results: list[SearchResult] = []
        self._search_query: str = ""
        self._search_cursor: int = 0
        self._poll_timer: Any = None
        self._drawer_data: EntityData | None = None
        self._signal_gate = asyncio.Lock()
        self._draft_store = DraftStore()
        self._apply = ApplyCoordinator(
            client,
            current_revision_supplier=self._current_revision_for_target,
            live_scope_supplier=self._is_live_scope_for_target,
        )
        self._timeline_key: RangeKey | None = None
        self._timeline_keys_by_path: dict[tuple[str, ...], RangeKey] = {}
        self._timeline_events: tuple[Any, ...] = ()
        self._timeline_buckets_by_path: dict[tuple[str, ...], tuple[Any, ...]] = {}
        self._timeline_row_status: dict[tuple[str, ...], str] = {}
        self._timeline_index: int = -1
        self._timeline_path: tuple[str, ...] = ()
        self._requested_snapshot: SnapshotKey | None = None
        self._applied_snapshot: SnapshotKey | None = None
        self._snapshot_scopes: dict[SnapshotKey, frozenset[ModelKey]] = {}
        self._prepared_apply_plan: ApplyPlan | None = None
        self._compare_lease: tuple[ModelKey, datetime, datetime] | None = None
        self._applies_in_flight = 0
        self._timeline_baseline_settled = False
        self._timeline_hours = 1
        self._render_pending = False
        self._timeline_frame = 0
        self._timeline_pulse: Any = None
        self._changes_result_by_target: dict[ResourceTarget, str] = {}
        self._form_target: ResourceTarget | None = None  # the draft the form shows
        self._allow_back_to_models = allow_back_to_models

    # ── layout ────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:  # noqa: D102
        with Horizontal(id="workspace-tabs"):
            yield Button("Tree", id="ws-tab-tree")
            yield Button("Timeline", id="ws-tab-timeline")
            yield Button("Changes", id="ws-tab-changes")
        yield HealthTree(id="health-tree")
        yield TimelinePane(id="timeline-pane")
        yield ChangesPane(id="changes-pane")
        yield EntityDrawer(id="entity-drawer")
        yield SignalPanel(id="signal-panel")
        yield StatusBar(id="status-bar")

    # ── lifecycle ─────────────────────────────────────────────────────

    def on_mount(self) -> None:  # noqa: D102
        self.title = f"Health Model: {self._model}"
        self.set_focus(self.query_one("#health-tree", HealthTree))
        panel = self.query_one("#signal-panel", SignalPanel)
        panel.display = False
        panel.clear_signal()
        self._set_workspace_tab("tree")
        loop = asyncio.get_running_loop()

        def request_render() -> None:
            # Poller threads call this on every state change; one render per loop turn is enough.
            if not self._render_pending:
                self._render_pending = True
                loop.call_soon_threadsafe(self._render_coalesced)

        self._poller.listen(request_render)
        self._poll_timer = self.set_interval(self._poll_interval, self._poller.refresh)
        self.set_interval(1.0, self._tick_countdown)
        self._timeline_pulse = self.set_interval(0.2, self._pulse_timeline, pause=True)
        self.call_after_refresh(self._poller.refresh)

    # ── polling ───────────────────────────────────────────────────────

    async def _do_poll(self) -> None:
        self._poller.refresh()
        await asyncio.to_thread(self._poller.wait)
        self._render_monitor()

    def _render_coalesced(self) -> None:
        self._render_pending = False  # before rendering, so changes during the render schedule another
        self._render_monitor()

    def _shown_view(self) -> MonitorView:
        """What the tree shows: the applied snapshot, otherwise live data."""
        return self._poller.view_at(self._applied_snapshot.at_utc) if self._applied_snapshot else self._poller.view

    def _render_monitor(self) -> None:
        if not self.is_running:
            return
        view = self._poller.view
        root = view.entries[view.root]
        status = self.query_one("#status-bar", StatusBar)
        status.connected = not isinstance(root, Failed)
        status.error_text = root.error
        status.model_work = view.summary
        previous = self._forest
        self._forest = root.previous.forest if root.previous else None
        status.snapshot_at = format_utc(self._applied_snapshot.at_utc) if self._applied_snapshot else ""
        self.query_one("#timeline-pane", TimelinePane).set_applied_at(f"Tree shows: {status.snapshot_at or 'live'}")
        tree = self.query_one("#health-tree", HealthTree)
        shown = self._shown_view()
        tree.apply_view(shown)
        if root.previous and self._forest is not previous:
            escalations = [c for c in root.previous.changes if c.is_escalation]
            status.change_count += len(escalations)
            status.poll_countdown = self._poll_interval
            if self.auto_jump and escalations and self._applied_snapshot is None:
                tree.scroll_to_entity(escalations[0].entity_id)
        drawer = self.query_one("#entity-drawer", EntityDrawer)
        if drawer.is_visible and self._drawer_data:
            if not tree.contains(self._drawer_data.path) or not self._show_drawer(self._drawer_data):
                drawer.hide()
        panel = self.query_one(SignalPanel)
        if panel.context and panel.context.occurrence_path:
            if not tree.contains(panel.context.occurrence_path):
                panel.clear_signal()
                panel.display = False
        self._refresh_timeline()

    def on_health_tree_navigation(self, event: HealthTree.Navigation) -> None:
        try:
            tree = self.query_one(HealthTree)
        except NoMatches:
            return
        if event.expand and event.data.target:
            self._poller.expand(event.data.target, tree.distances())
        else:
            self._poller.navigate(event.data.model, tree.distances())

    def on_unmount(self) -> None:
        self._poller.close()

    def _tick_countdown(self) -> None:
        results = self.query("#status-bar")
        if results:
            results.first().poll_countdown = max(0, results.first().poll_countdown - 1)

    # ── actions ───────────────────────────────────────────────────────

    def action_toggle_autojump(self) -> None:
        """Toggle automatic scrolling to escalation events."""
        self.auto_jump = not self.auto_jump
        self.query_one("#status-bar", StatusBar).auto_jump = self.auto_jump

    def action_force_refresh(self) -> None:
        """Trigger an immediate poll outside the regular interval."""
        self.query_one("#status-bar", StatusBar).poll_countdown = 0
        self._poller.refresh(manual=True)

    def action_show_tree(self) -> None:
        self._set_workspace_tab("tree")

    def action_show_timeline(self) -> None:
        self._set_workspace_tab("timeline")
        self._ensure_tree_selection()
        model, entity, signal_name, _ = self._selected_context()
        key = self._timeline_key
        if key is None or (key.model, key.entity_name, key.signal_name) != (model, entity, signal_name):
            self._request_timeline(hours=self._timeline_hours)

    def action_show_changes(self) -> None:
        self._set_workspace_tab("changes")
        self._ensure_tree_selection()
        self._render_changes()

    def action_increase_interval(self) -> None:
        """Increase the poll interval by 10 s (max 300 s)."""
        self._poll_interval = min(300, self._poll_interval + 10)
        self._reschedule_poll_timer()

    def action_decrease_interval(self) -> None:
        """Decrease the poll interval by 10 s (min 10 s)."""
        self._poll_interval = max(10, self._poll_interval - 10)
        self._reschedule_poll_timer()

    def _reschedule_poll_timer(self) -> None:
        """Restart the poll timer using the current ``_poll_interval``."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._poll_timer = self.set_interval(self._poll_interval, self._poller.refresh)
        status = self.query_one("#status-bar", StatusBar)
        status.poll_countdown = self._poll_interval

    # ── search ────────────────────────────────────────────────────────

    def action_open_search(self) -> None:
        """Open the search modal with the previous query pre-filled."""
        if self._forest is None:
            return
        if self._is_text_focused():
            return

        def _handle_result(result: SearchResult | None) -> None:
            if result is not None:
                self._search_cursor = next(
                    (i for i, r in enumerate(self._search_results)
                     if (r.occurrence_path == result.occurrence_path if result.occurrence_path
                         else r.entity_id == result.entity_id)),
                    0,
                )
                self._show_match(result)
            status = self.query_one("#status-bar", StatusBar)
            status.has_search_results = len(self._search_results) > 0

        view = self._shown_view()
        forests = {k: s.previous.forest for k, s in view.entries.items() if s.previous}
        modal = SearchModal(
            forests.get(view.root, self._forest),
            self._search_query,
            on_state=self._update_search_state,
            occurrences=project(view.root, forests),
            unloaded=sum(s.previous is None for s in view.entries.values()),
        )
        self.push_screen(modal, _handle_result)

    def action_open_help(self) -> None:
        self.push_screen(HelpDialog(self.BINDINGS))

    def action_open_actions(self) -> None:
        self.push_screen(
            ActionsDialog(include_back_to_models=self._allow_back_to_models),
            self._run_named_action,
        )

    def _run_named_action(self, action_name: str | None) -> None:
        if action_name is not None:
            getattr(self, f"action_{action_name}")()

    def _update_search_state(self, query: str, results: list[SearchResult]) -> None:
        """Called by SearchModal on each keystroke to keep app state in sync."""
        self._search_query = query
        self._search_results = results

    def action_next_match(self) -> None:
        """Jump to the next search result in the tree."""
        if not self._search_results:
            return
        self._search_cursor = (self._search_cursor + 1) % len(self._search_results)
        self._show_match(self._search_results[self._search_cursor])

    def action_prev_match(self) -> None:
        """Jump to the previous search result in the tree."""
        if not self._search_results:
            return
        self._search_cursor = (self._search_cursor - 1) % len(self._search_results)
        self._show_match(self._search_results[self._search_cursor])

    def _show_match(self, result: SearchResult) -> None:
        tree = self.query_one(HealthTree)
        if result.occurrence_path:
            tree.reveal(result.occurrence_path, self._shown_view())
        else:
            tree.scroll_to_entity(result.entity_id)

    def _is_text_focused(self) -> bool:
        focused: Widget | None = self.focused
        return isinstance(focused, (Input, TextArea))

    def _ensure_tree_selection(self) -> None:
        tree = self.query_one("#health-tree", HealthTree)
        node = tree.cursor_node
        if node is not None and node.data is not None:
            return

        def _walk(candidate):
            yield candidate
            for child in candidate.children:
                yield from _walk(child)

        for candidate in _walk(tree.root):
            data = candidate.data
            if data is None or data.is_signal or data.entity_name == "__unlinked__":
                continue
            if not data.path:
                continue
            tree.move_cursor(candidate)
            return

    # ── details drawer ────────────────────────────────────────────────

    def action_show_details(self) -> None:
        """Toggle the entity detail drawer for the currently selected tree node."""
        drawer = self.query_one("#entity-drawer", EntityDrawer)

        if self._forest is None:
            drawer.hide()
            return

        tree = self.query_one("#health-tree", HealthTree)
        node = tree.cursor_node
        data: EntityData | None = node.data if node is not None else None

        if data is None or data.entity_name == "__unlinked__":
            # Nothing meaningful selected → just toggle visibility off.
            drawer.hide()
            return

        # For signal leaves, the drawer shows the parent entity.
        if data.is_signal and node is not None and node.parent is not None and node.parent.data is not None:
            data = node.parent.data
        if drawer.is_visible and self._drawer_data == data:
            drawer.hide()
        elif self._show_drawer(data):
            self._drawer_data = data

    def _show_drawer(self, data: EntityData) -> bool:
        """Show *data*'s entity as the shown view has it; False while its model is not loaded there."""
        shown = self._shown_view()
        key = data.model or shown.root
        state = shown.entries.get(key)
        if not state or not state.previous:
            return False
        entity = state.previous.forest.entities.get(data.entity_name)
        target = entity.reference.target if entity and entity.reference else None
        self.query_one("#entity-drawer", EntityDrawer).show_entity(
            state.previous.forest, data.entity_name,
            f"{key.label}; {shown.status(key)}",
            f"{target.label}: {shown.status(target)}" if target else "",
        )
        return True

    # ── query editor ──────────────────────────────────────────────────

    def action_edit_query(self) -> None:
        """Open query editor for signals, guided draft edit for entities."""
        if self._applied_snapshot is not None:
            self.notify("Return to live before editing.", severity="warning")
            return
        self._ensure_tree_selection()
        tree = self.query_one("#health-tree", HealthTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            self.notify("Select an entity or signal first.", severity="warning")
            return
        if not node.data.is_signal:
            self._open_entity_draft_editor(node.data)
            return
        entity_name = node.data.owner_entity_name
        if not entity_name or entity_name.startswith("__"):
            return
        key = node.data.model or self._poller.root
        self.push_screen(
            QueryEditor(
                self._scoped_client(key),
                key.group,
                key.name,
                entity_name,
                node.data.entity_name,
                draft_store=self._draft_store,
                draft_target=ResourceTarget(key, "entity", entity_name),
            )
        )

    def _open_entity_draft_editor(self, data: EntityData) -> None:
        key = data.model or self._poller.root
        if data.entity_name.startswith("__"):
            self.notify("Select an entity occurrence to edit.", severity="warning")
            return
        state = self._poller.view.entries.get(key)
        previous = getattr(state, "previous", None)
        if previous is None:
            self.notify("Entity baseline is not loaded yet.", severity="warning")
            return
        payload = previous.resource_json.get(("entity", data.entity_name.casefold()))
        if not payload:
            self.notify("Entity baseline payload is unavailable.", severity="warning")
            return
        baseline = json.loads(payload)
        target = ResourceTarget(key, "entity", data.entity_name)
        self._draft_store.get_or_create(target, baseline)
        self._prepared_apply_plan = None
        self._set_workspace_tab("changes")
        self._render_changes()

    def _scoped_client(self, key: ModelKey) -> CloudHealthClient:
        return self._client if key == self._poller.root else self._client.for_model(key)

    # ── signal verification ───────────────────────────────────────────

    def action_verify_signal(self) -> None:
        """Open the verification panel for the selected signal and run its query."""
        if self._applied_snapshot is not None:
            self.notify("Return to live to test current configuration.", severity="warning")
            return
        panel = self.query_one("#signal-panel", SignalPanel)
        tree = self.query_one("#health-tree", HealthTree)

        node = tree.cursor_node
        data: EntityData | None = node.data if node is not None else None

        if data is None or not data.is_signal or data.owner_entity_name is None:
            if panel.display:
                panel.display = False
            return

        owner = data.owner_entity_name
        signal_name = data.entity_name

        key = data.model or self._poller.root
        state = self._poller.view.entries.get(key)
        entity_node = state.previous.forest.entities.get(owner) if state and state.previous else None
        ctx = replace(build_context(entity_node, signal_name, owner), model=key, occurrence_path=data.path)

        already_showing = (
            panel.display
            and panel.context is not None
            and panel.context.entity_name == owner
            and panel.context.signal_name == signal_name
            and panel.context.model == key
            and panel.context.occurrence_path == data.path
        )
        if not panel.display or not already_showing:
            panel.set_signal(ctx)
            panel.display = True

        if panel.context is not None:
            self._run_verification(panel.context)

    def action_close_panel(self) -> None:
        """Close the signal verification panel and entity drawer (Escape)."""
        panel = self.query_one("#signal-panel", SignalPanel)
        if panel.display:
            panel.display = False
            panel.clear_signal()
        drawer = self.query_one("#entity-drawer", EntityDrawer)
        if drawer.is_visible:
            drawer.hide()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Wire the panel's Verify button to the background worker."""
        button_id = event.button.id or ""
        if button_id == "ws-tab-tree":
            self.action_show_tree()
            return
        if button_id == "ws-tab-timeline":
            self.action_show_timeline()
            return
        if button_id == "ws-tab-changes":
            self.action_show_changes()
            return
        if button_id.startswith("timeline-"):
            self._handle_timeline_button(button_id)
            return
        if button_id.startswith("changes-"):
            self._handle_changes_button(button_id)
            return
        if event.button.id != "signal-panel-verify":
            return
        panel = self.query_one("#signal-panel", SignalPanel)
        if panel.context is None or panel.is_busy:
            return
        self._run_verification(panel.context)

    def on_input_changed(self, event: Input.Changed) -> None:
        # Changed arrives after the value is set, so only focus tells typing from set_form_values.
        if not event.input.has_focus:
            return
        input_id = event.input.id or ""
        if self._prepared_apply_plan is None:
            return
        if input_id in {field_input_id(key) for key in FIELDS}:
            self._prepared_apply_plan = None
            pane = self.query_one("#changes-pane", ChangesPane)
            pane.set_plan("")
            pane.set_summary("Prepared apply plan invalidated by pending field edits.")

    def _run_verification(self, ctx: SignalPanelContext) -> None:
        panel = self.query_one("#signal-panel", SignalPanel)
        if panel.is_busy:
            return
        panel.mark_verifying(queued=self._signal_gate.locked())
        self.run_worker(
            self._verify_worker(ctx),
            exclusive=False,
            group="signal-verify",
        )

    async def _verify_worker(self, ctx: SignalPanelContext) -> None:
        async with self._signal_gate:
            await self._verify_selected(ctx)

    async def _verify_selected(self, ctx: SignalPanelContext) -> None:
        panel = self.query_one("#signal-panel", SignalPanel)
        if panel.context is not ctx or not panel.display:
            return
        panel.mark_verifying()
        key = ctx.model or self._poller.root
        client = self._scoped_client(key)
        try:
            result = await asyncio.to_thread(
                execute_signal,
                client,
                key.group,
                key.name,
                ctx.entity_name,
                ctx.signal_name,
            )
        except Exception as exc:  # noqa: BLE001 — surface executor failure in UI
            if panel.context is ctx and panel.display:
                panel.show_exception(exc)
            return
        if panel.context is not ctx or not panel.display:
            return
        panel.show_result(result)
        panel.mark_history_loading()

        # History is a visual enhancement — fetch it after the main result
        # is on-screen, and never let a failure bubble up.
        try:
            history = await asyncio.to_thread(self._fetch_signal_history, ctx.entity_name, ctx.signal_name, key)
        except Exception as exc:  # noqa: BLE001 — history is best-effort
            if panel.context is ctx and panel.display:
                panel.show_history(exc)
            return
        if panel.context is ctx and panel.display:
            panel.show_history(history)

    def _fetch_signal_history(self, entity_name: str, signal_name: str, key: ModelKey) -> dict[str, Any]:
        """Request the last hour of history for *signal_name* on *entity_name*."""
        now = datetime.now(UTC)
        body = {
            "signalName": signal_name,
            "startAt": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return self._scoped_client(key).get_signal_history(key.group, key.name, entity_name, body)

    def action_back_to_models(self) -> None:
        if not self._allow_back_to_models:
            self.notify("Back to models is unavailable for this launch.", severity="warning")
            return
        self._request_exit(self.RESULT_BACK_TO_MODELS)

    def action_quit(self) -> None:
        self._request_exit(None)

    def _request_exit(self, result: str | None) -> None:
        if self._applies_in_flight:
            self.notify("An apply is still writing. Quit after its result is shown.", severity="warning")
            return
        has_draft_changes = any(self._draft_store.has_unsaved_changes(target) for target in self._draft_store.list_targets())
        has_pending_form_edits = self._changes_form_has_pending_edits()
        if has_draft_changes or has_pending_form_edits:
            self.push_screen(QuitDraftsDialog(), lambda choice: self._handle_quit_choice(choice, result))
            return
        self.exit(result)

    def _handle_quit_choice(self, choice: str | None, result: str | None) -> None:
        if choice == "discard":
            for target in self._draft_store.list_targets():
                self._draft_store.discard(target)
            self.exit(result)
            return
        if choice == "save":
            self.action_close_panel()
            self.action_show_changes()
            pane = self.query_one("#changes-pane", ChangesPane)
            if self._changes_form_has_pending_edits():
                pane.set_summary("Review the drafts: apply pending form edits with Update draft, then Save each to a file.")
            else:
                pane.set_summary("Review the drafts: Save or Overwrite writes each one to a file before you quit.")

    def _changes_form_has_pending_edits(self) -> bool:
        try:
            pane = self.query_one("#changes-pane", ChangesPane)
        except NoMatches:
            return False
        target = self._draft_store.active_target()
        draft = self._draft_store.get(target) if target is not None else None
        # Empty means no edit, as in Update draft, so an emptied field never blocks Prepare.
        return bool(pane.edits(self._draft_form_values(draft.edited if draft else {})))

    @staticmethod
    def _draft_form_values(edited: Mapping[str, Any]) -> dict[str, str]:
        values = {key: get_in(edited, parse_field_path(path)) for key, (path, _) in FIELDS.items()}
        return {key: "" if value is None or isinstance(value, (dict, list)) else str(value) for key, value in values.items()}

    def _set_workspace_tab(self, tab: str) -> None:
        timeline = self.query_one("#timeline-pane", TimelinePane)
        changes = self.query_one("#changes-pane", ChangesPane)
        timeline.display = tab == "timeline"
        changes.display = tab == "changes"
        for name in ("tree", "timeline", "changes"):
            self.query_one(f"#ws-tab-{name}", Button).set_class(name == tab, "-selected")
        # Focus follows the tab, so keys never drive a tree hidden behind a full-width pane.
        self.set_focus({"tree": self.query_one("#health-tree"), "timeline": timeline, "changes": changes}[tab])

    def _selected_context(self) -> tuple[ModelKey, str, str | None, tuple[str, ...]]:
        tree = self.query_one("#health-tree", HealthTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return self._poller.root, "", None, ()
        data = node.data
        model = data.model or self._poller.root
        if data.is_signal:
            # The owner's timeline row carries the signal; signal leaves have no row of their own.
            return model, data.owner_entity_name or "", data.entity_name, data.path[:-1]
        return model, data.entity_name, None, data.path

    def _request_timeline(self, *, hours: int) -> None:
        model, entity, signal_name, path = self._selected_context()
        if not entity:
            self.query_one("#timeline-pane", TimelinePane).set_summary("Select an entity or signal first.")
            return
        if self._timeline_key is not None:
            self._release_timeline_leases(self._timeline_key, self._timeline_keys_by_path)
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)
        key = RangeKey(model=model, entity_name=entity, start_utc=start, end_utc=end, signal_name=signal_name)
        self._timeline_key = key
        self._timeline_path = path
        self._timeline_keys_by_path = {}
        self._timeline_buckets_by_path = {}
        self._timeline_row_status = {}
        self._timeline_events = ()
        self._timeline_index = -1
        self._timeline_baseline_settled = False
        for row in self._visible_occurrence_rows():
            row_path = row["path"]
            row_name = row["name"]
            row_model = row.get("model", model)
            row_key = RangeKey(
                model=row_model,
                entity_name=row_name,
                start_utc=start,
                end_utc=end,
                signal_name=signal_name if row_path == path else None,
            )
            self._timeline_keys_by_path[row_path] = row_key
            self._timeline_row_status[row_path] = "queued"
            self._poller.request_range(row_key, purpose="timeline-row")
        self._timeline_hours = hours
        # Each row model needs its own state at the range start, or its band starts uncovered.
        for row_model in self._timeline_models(key, self._timeline_keys_by_path):
            self._poller.open_at(start, target=row_model, purpose="timeline-baseline")
        pane = self.query_one("#timeline-pane", TimelinePane)
        pane.set_summary(f"Loading {entity} ({format_utc(start)} .. {format_utc(end)})")
        pane.set_events("")
        self._refresh_timeline()

    @staticmethod
    def _timeline_models(key: RangeKey, keys_by_path: dict[tuple[str, ...], RangeKey]) -> set[ModelKey]:
        return {row_key.model for row_key in keys_by_path.values()} | {key.model}

    def _release_timeline_leases(self, key: RangeKey, keys_by_path: dict[tuple[str, ...], RangeKey]) -> None:
        for row_key in keys_by_path.values():
            self._poller.release_range(row_key, purpose="timeline-row")
        for row_model in self._timeline_models(key, keys_by_path):
            self._poller.release_at(key.start_utc, target=row_model, purpose="timeline-baseline")

    def _refresh_timeline(self) -> None:
        """Pull settled rows, the opened snapshot and the compare in; runs on every poller change."""
        key = self._timeline_key
        if key is not None:
            self._collect_timeline_rows(key)
        requested = self._requested_snapshot
        if requested is not None and requested != self._applied_snapshot and all(
            isinstance(self._poller.view_at(requested.at_utc).entries.get(model), _SETTLED)
            for model in self._snapshot_scopes[requested]
        ):
            self._apply_snapshot(requested)
        lease = self._compare_lease
        if lease is not None and all(
            isinstance(self._poller.view_at(at).entries.get(lease[0]), _SETTLED) for at in lease[1:]
        ):
            self._complete_compare_snapshots(*lease)
        if self._render_timeline_progress() and self._timeline_pulse is not None:
            self._timeline_pulse.resume()

    def _collect_timeline_rows(self, key: RangeKey) -> None:
        at_start = self._poller.view_at(key.start_utc).entries
        # Rows bucketed before their models' start states arrived are bucketed again once with them.
        baseline_arrived = not self._timeline_baseline_settled and all(
            at_start.get(model) is None or isinstance(at_start.get(model), _SETTLED)
            for model in self._timeline_models(key, self._timeline_keys_by_path)
        )
        self._timeline_baseline_settled |= baseline_arrived
        views = {}
        for path, row_key in self._timeline_keys_by_path.items():
            status = self._timeline_row_status.get(path)
            if status not in _ROW_PENDING and not (baseline_arrived and status == "ready"):
                continue
            view = self._poller.range_view(row_key)
            if view.status != status or baseline_arrived:
                views[path] = view
        if views:
            self._apply_timeline_ranges(key, views, self._timeline_keys_by_path)

    def _apply_timeline_ranges(
        self,
        selected_key: RangeKey,
        views_by_path: dict[tuple[str, ...], Any],
        keys_by_path: dict[tuple[str, ...], RangeKey],
    ) -> None:
        """Merge newly settled row views; rows still loading keep animating."""
        pane = self.query_one("#timeline-pane", TimelinePane)
        size = bucket_size(selected_key)
        for path, view in views_by_path.items():
            was_ready = self._timeline_row_status.get(path) == "ready"
            self._timeline_row_status[path] = view.status
            selected = path == self._timeline_path
            if view.status != "ready":
                if selected and view.status not in _ROW_PENDING:
                    pane.set_summary(f"Range load: {view.status} {view.error}")
                    pane.set_events("")
                continue
            key = keys_by_path[path]
            events = view.events
            buckets = bucket_history(events, key, size, baseline_health=self._timeline_baseline_for(key))
            self._timeline_buckets_by_path[path] = buckets
            if not selected:
                continue
            # A transition before the window shapes the band but is not an event in the range.
            events = tuple(event for event in events if key.start_utc <= event.occurred_at <= key.end_utc)
            self._timeline_events = events
            if was_ready:
                continue
            self._timeline_index = len(events) - 1
            if events:
                pane.set_go_to_value(events[-1].occurred_at, keep_typed=True)
                noun, preposition = ("sample", "of") if selected_key.signal_name else ("event", "in")
                pane.set_summary(
                    f"{len(events)} {noun}{'' if len(events) == 1 else 's'} {preposition} {self._timeline_subject(selected_key)}"
                )
                pane.set_events("\n".join(event_line(event) for event in events[-10:]))
            else:
                pane.set_summary("No events in selected range.")
                pane.set_events("")
        pane.set_bands(self._render_timeline_bands(selected_key))

    def _timeline_subject(self, key: RangeKey) -> str:
        previous = getattr(self._poller.view.entries.get(key.model), "previous", None)
        entity = previous.forest.entities.get(key.entity_name) if previous is not None else None
        if entity is None:
            return key.signal_name or key.entity_name
        if key.signal_name is None:
            return entity.display_name
        return next((s.display_name for s in entity.signals if s.name == key.signal_name), key.signal_name)

    def _render_timeline_progress(self) -> bool:
        """Show what is still coming in; True while anything is pending."""
        spin = _SPINNER[self._timeline_frame % len(_SPINNER)]
        lines = []
        pending_rows = sum(status in _ROW_PENDING for status in self._timeline_row_status.values())
        if self._timeline_key is not None and pending_rows:
            done = len(self._timeline_row_status) - pending_rows
            lines.append(f"{spin} Loading timeline rows: {done}/{len(self._timeline_row_status)} ready")
        if self._requested_snapshot is not None and self._requested_snapshot != self._applied_snapshot:
            lines.append(f"{spin} Loading snapshot at {format_utc(self._requested_snapshot.at_utc)}")
        if self._compare_lease is not None:
            lines.append(f"{spin} Loading compare snapshots")
        pane = self.query_one("#timeline-pane", TimelinePane)
        pane.set_progress("\n".join(lines))
        if pending_rows and self._timeline_key is not None:
            pane.set_bands(self._render_timeline_bands(self._timeline_key))
        return bool(lines)

    def _pulse_timeline(self) -> None:
        self._timeline_frame += 1
        # The timer can fire while the app tears its widgets down; then there is nothing to animate.
        if not self.query("#timeline-progress") or not self._render_timeline_progress():
            self._timeline_pulse.pause()

    def _timeline_baseline_for(self, key: RangeKey) -> HistoryBucketHealth | None:
        start_state = self._poller.view_at(key.start_utc).entries.get(key.model)
        previous = getattr(start_state, "previous", None)
        if previous is None:
            return None
        entity = previous.forest.entities.get(key.entity_name)
        if entity is None:
            return None
        try:
            return HistoryBucketHealth(entity.health_state.value)
        except ValueError:
            return HistoryBucketHealth.UNKNOWN

    def _timeline_label(self, row: dict[str, Any], width: int) -> str:
        name = str(row.get("name", "")).strip() or "<unnamed>"
        row_model = row.get("model")
        # An applied snapshot labels rows by its own metadata, never by later live renames.
        view = self._shown_view()
        previous = getattr(view.entries.get(row_model), "previous", None) if isinstance(row_model, ModelKey) else None
        entity = previous.forest.entities.get(name) if previous is not None else None
        display_name = getattr(entity, "display_name", None)
        label_value = " ".join(display_name.split()) if isinstance(display_name, str) else ""
        prefix = "  " * max(0, int(row.get("depth", 0)))
        return self._fit_cell_width(prefix + (label_value or name), width)

    @staticmethod
    def _fit_cell_width(value: str, width: int) -> str:
        if width <= 0:
            return ""
        if cell_len(value) <= width:
            return value + (" " * (width - cell_len(value)))
        if width == 1:
            return "…"
        target_width = width - 1
        trimmed = ""
        for char in value:
            if cell_len(trimmed + char) > target_width:
                break
            trimmed += char
        clipped = trimmed + "…"
        if cell_len(clipped) < width:
            clipped += " " * (width - cell_len(clipped))
        return clipped

    def _pending_band(self, width: int, offset: int) -> Text:
        """A bright segment sweeps across rows whose data is still on the way."""
        head = (self._timeline_frame + offset * 3) % width
        lit = {(head + step) % width for step in range(min(3, width))}
        return Text.assemble(*(("━", "bold cyan") if index in lit else ("─", "dim") for index in range(width)))

    def _render_timeline_bands(self, key: RangeKey) -> Text:
        reference = self._timeline_buckets_by_path.get(self._timeline_path, ())
        width = len(reference) or max(1, math.ceil((key.end_utc - key.start_utc) / bucket_size(key)))
        preview_value = self.query_one("#timeline-pane", TimelinePane).go_to_value()
        preview = preview_value or "unset"
        crosshair = [" "] * width
        if preview_value:
            try:
                preview_at = parse_utc_timestamp(preview_value)
                if key.start_utc <= preview_at <= key.end_utc and width > 0:
                    ratio = (preview_at - key.start_utc).total_seconds() / (key.end_utc - key.start_utc).total_seconds()
                    idx = min(width - 1, max(0, int(ratio * width)))
                    crosshair[idx] = "^"
            except ValueError:
                pass

        text = Text()
        text.append(f"UTC axis: {format_utc(key.start_utc)} .. {format_utc(key.end_utc)}\n")
        text.append(f"Marked time: {preview}\n")
        text.append(f"{'':<{_TIMELINE_LABEL_WIDTH}} {''.join(crosshair)}\n")
        for offset, row in enumerate(self._visible_occurrence_rows()):
            text.append(self._timeline_label(row, _TIMELINE_LABEL_WIDTH) + " ")
            text.append_text(self._row_band(row["path"], width, offset))
            text.append("\n")
        return text

    def _row_band(self, path: tuple[str, ...], width: int, offset: int) -> Text:
        buckets = self._timeline_buckets_by_path.get(path)
        status = self._timeline_row_status.get(path, "unloaded")
        if buckets is None and status in _ROW_PENDING:
            return self._pending_band(width, offset)
        if buckets is None:
            marker = "!" if status == "failed" else "~" if status == "stopped" else "."
            return Text(f"{marker * width} [{status}]", style="dim")
        if not buckets and path == self._timeline_path:
            return Text.assemble(("!" * width, "red"), (" [no-buckets]", "dim"))
        return health_band(buckets).append("." * (width - len(buckets)), style="dim")

    def _visible_occurrence_rows(self) -> list[dict[str, Any]]:
        tree = self.query_one("#health-tree", HealthTree)
        rows: list[tuple[int, tuple[str, ...], str, int, ModelKey]] = []
        occurrence_nodes = getattr(tree, "_occurrence_nodes", {})
        for path, node in occurrence_nodes.items():
            data = node.data
            if data is None or data.is_signal:
                continue
            parent = node.parent
            visible = True
            while parent is not None:
                if not parent.is_expanded and parent is not tree.root:
                    visible = False
                    break
                parent = parent.parent
            if not visible:
                continue
            depth = max(0, len(path) - 1)
            rows.append((node.line, path, data.entity_name, depth, data.model or self._poller.root))
        rows.sort(key=lambda item: item[0])
        return [
            {
                "path": path,
                "name": name,
                "depth": depth,
                "model": model,
            }
            for _, path, name, depth, model in rows
        ]

    def _handle_timeline_button(self, button_id: str) -> None:
        if button_id in _TIMELINE_RANGE_HOURS:
            self._request_timeline(hours=_TIMELINE_RANGE_HOURS[button_id])
            return
        if button_id in {"timeline-prev", "timeline-next"}:
            if self._timeline_events:
                step = -1 if button_id == "timeline-prev" else 1
                self._timeline_index = min(len(self._timeline_events) - 1, max(0, self._timeline_index + step))
                pane = self.query_one("#timeline-pane", TimelinePane)
                pane.set_go_to_value(self._timeline_events[self._timeline_index].occurred_at)
                if self._timeline_key is not None:
                    pane.set_bands(self._render_timeline_bands(self._timeline_key))
            return
        handler = {
            "timeline-go": self._preview_snapshot_at_input,
            "timeline-open": self._open_snapshot_at_input,
            "timeline-live": self._return_to_live,
            "timeline-compare": self._compare_snapshots_by_inputs,
        }.get(button_id)
        if handler is not None:
            handler()

    def _preview_snapshot_at_input(self) -> None:
        pane = self.query_one("#timeline-pane", TimelinePane)
        value = pane.go_to_value()
        if not value:
            pane.set_summary("Enter a UTC timestamp first.")
            return
        try:
            _ = parse_utc_timestamp(value)
        except ValueError as exc:
            pane.set_summary(str(exc))
            return
        pane.set_summary(f"Marked {value}")
        if self._timeline_key is not None:
            pane.set_bands(self._render_timeline_bands(self._timeline_key))

    def _open_snapshot_at_input(self) -> None:
        pane = self.query_one("#timeline-pane", TimelinePane)
        value = pane.go_to_value()
        if not value:
            pane.set_summary("Enter a UTC timestamp first.")
            return
        try:
            at = parse_utc_timestamp(value)
        except ValueError as exc:
            pane.set_summary(str(exc))
            return
        if at > datetime.now(UTC):
            pane.set_summary(f"{format_utc(at)} is in the future. Enter a past time.")
            return
        model = self._timeline_key.model if self._timeline_key is not None else self._selected_context()[0]
        key = SnapshotKey(model, at)
        for previous in {self._requested_snapshot, self._applied_snapshot} - {key, None}:
            self._release_snapshot(previous)
        # The tree is projected from the launch model, and needs every model with a visible row at that time.
        self._snapshot_scopes[key] = self._snapshot_scopes.get(key, frozenset()) | {key.model, self._poller.root} | {
            row_key.model for row_key in self._timeline_keys_by_path.values()
        }
        self._requested_snapshot = key
        for snapshot_model in self._snapshot_scopes[key]:
            self._poller.open_at(at, target=snapshot_model, purpose="timeline-open")
        self._refresh_timeline()

    def _release_snapshot(self, key: SnapshotKey) -> None:
        for model in self._snapshot_scopes.pop(key, ()):
            self._poller.release_at(key.at_utc, target=model, purpose="timeline-open")

    def _apply_snapshot(self, key: SnapshotKey) -> None:
        if self._requested_snapshot != key:
            return
        view = self._poller.view_at(key.at_utc)
        pane = self.query_one("#timeline-pane", TimelinePane)
        # A nested model without data at that time shows as [unloaded]; the tree needs only the launch model.
        if not isinstance(view.entries.get(self._poller.root), Ready):
            pane.set_summary(f"Snapshot unavailable at {format_utc(key.at_utc)}.")
            self._release_snapshot(key)
            self._requested_snapshot = None
            return
        self._applied_snapshot = key
        self._render_monitor()
        if self._timeline_path:
            self.query_one("#health-tree", HealthTree).reveal(self._timeline_path, view)
        pane.set_summary(f"Showing snapshot at {format_utc(key.at_utc)}.")

    def _return_to_live(self) -> None:
        for key in {self._applied_snapshot, self._requested_snapshot} - {None}:
            self._release_snapshot(key)
        self._requested_snapshot = None
        self._applied_snapshot = None
        self._render_monitor()

    def _compare_snapshots_by_inputs(self) -> None:
        pane = self.query_one("#timeline-pane", TimelinePane)
        left = pane.compare_a() or (self._applied_snapshot.at_utc.isoformat().replace("+00:00", "Z") if self._applied_snapshot else "")
        right = pane.compare_b() or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if not left:
            pane.set_summary("Set compare A time first.")
            return
        try:
            left_at = parse_utc_timestamp(left)
            right_at = parse_utc_timestamp(right)
        except ValueError as exc:
            pane.set_summary(str(exc))
            return
        if (latest := max(left_at, right_at)) > datetime.now(UTC):
            pane.set_summary(f"{format_utc(latest)} is in the future. Enter a past time.")
            return
        pane.set_summary(f"Loading compare snapshots at {format_utc(left_at)} and {format_utc(right_at)}...")
        model = self._timeline_key.model if self._timeline_key is not None else self._poller.root
        # Only the latest compare is pending; a rerun drops the previous one's leases.
        if self._compare_lease not in (None, (model, left_at, right_at)):
            self._release_compare(*self._compare_lease)
        self._compare_lease = (model, left_at, right_at)
        self._poller.open_at(left_at, target=model, purpose="timeline-compare-left")
        self._poller.open_at(right_at, target=model, purpose="timeline-compare-right")
        self._refresh_timeline()

    def _release_compare(self, model: ModelKey, left_at: datetime, right_at: datetime) -> None:
        self._poller.release_at(left_at, target=model, purpose="timeline-compare-left")
        self._poller.release_at(right_at, target=model, purpose="timeline-compare-right")
        if self._compare_lease == (model, left_at, right_at):
            self._compare_lease = None

    def _complete_compare_snapshots(self, model: ModelKey, left_at: datetime, right_at: datetime) -> None:
        pane = self.query_one("#timeline-pane", TimelinePane)
        left_view = self._poller.view_at(left_at)
        right_view = self._poller.view_at(right_at)
        left_state = left_view.entries.get(model)
        right_state = right_view.entries.get(model)
        left_status = type(left_state).__name__.lower() if left_state is not None else "missing"
        right_status = type(right_state).__name__.lower() if right_state is not None else "missing"
        if not isinstance(left_state, Ready) or not isinstance(right_state, Ready):
            pane.set_summary(
                f"Compare unavailable (A={left_status}, B={right_status}). Previous comparison preserved."
            )
            self._release_compare(model, left_at, right_at)
            return
        # Only the compared model: other models cached at one of the times would show as added or removed.
        left_nodes, right_nodes = (
            project_snapshot_nodes(model=model, forest=state.previous.forest, resource_json=state.previous.resource_json)
            for state in (left_state, right_state)
        )
        pane.set_events(compare_text(left_at, right_at, left_nodes, right_nodes))
        pane.set_summary("Compare complete.")
        self._release_compare(model, left_at, right_at)

    def _render_changes(self) -> None:
        pane = self.query_one("#changes-pane", ChangesPane)
        snapshots = self._draft_store.list_snapshots()
        if not snapshots:
            pane.set_kind("")
            pane.set_drafts("Drafts: none")
            pane.set_summary("No retained drafts.")
            pane.set_result("")
            pane.set_plan("")
            pane.set_details("")
            # A discarded draft must not leave values the quit guard reads as pending edits.
            self._form_target = None
            pane.set_form_values(dict.fromkeys(FIELDS, ""))
            return
        def label(snapshot: DraftSnapshot) -> str:
            name = self._draft_form_values(snapshot.edited)["display_name"] or snapshot.target.name
            return f"{name} ({snapshot.target.kind.replace('_', ' ')})"

        pane.set_drafts(f"Drafts ({len(snapshots)}): " + "; ".join(
            label(snapshot) + (" unsaved" if self._draft_store.has_unsaved_changes(snapshot.target) else "")
            for snapshot in snapshots
        ))
        active = self._draft_store.active_target()
        draft = next(snapshot for snapshot in snapshots if snapshot.target == active)
        target = draft.target
        pane.set_kind(target.kind)
        summary = f"Active: {label(draft)}"
        if target.kind == "signal_definition":
            consumers = 0
            for state in self._poller.view.entries.values():
                previous = getattr(state, "previous", None)
                if previous is None:
                    continue
                for entity in previous.forest.entities.values():
                    consumers += sum(1 for signal in entity.signals if signal.definition_name == target.name)
            summary += f" (consumers: {consumers})"
        plan = self._prepared_apply_plan
        if plan is not None and (plan.target != target or plan.revision != draft.revision):
            self._prepared_apply_plan = None
            if plan.target == target:
                summary += "\nPrepared apply plan invalidated: the draft changed since Prepare."
        if self._prepared_apply_plan is None:
            pane.set_plan("")
        if self._applies_in_flight:
            summary += "\n" + _APPLY_RUNNING
        pane.set_summary(summary)
        pane.set_result(self._changes_result_by_target.get(target, ""))
        # The same draft again: a field typed into since the last fill stays, unless the draft changed that field.
        pane.set_form_values(self._draft_form_values(draft.edited), keep_typed=self._form_target == target)
        self._form_target = target
        pane.set_details(
            ("Unsaved changes" if self._draft_store.has_unsaved_changes(target) else "No unsaved changes")
            + ("" if draft.tested_revision is None
               else "; query tested" if draft.tested_revision == draft.revision else "; query test is outdated")
        )

    def _handle_changes_button(self, button_id: str) -> None:
        pane = self.query_one("#changes-pane", ChangesPane)
        target = self._draft_store.active_target()
        if target is None:
            pane.set_summary("No draft available.")
            return
        draft = self._draft_store.get(target)
        if draft is None:
            pane.set_summary("No draft available.")
            return
        if self._applies_in_flight and button_id in {
            "changes-prepare-apply", "changes-apply", "changes-restore", "changes-discard",
        }:
            pane.set_summary(_APPLY_RUNNING)
            return

        try:
            if button_id == "changes-review":
                updates: list[tuple[str, Any]] = []
                allowed_impacts = {"Standard", "Limited", "Suppressed"}
                for key, value in pane.edits(self._draft_form_values(draft.edited)).items():
                    path, kind = FIELDS[key]
                    if kind != target.kind:
                        continue
                    if key in ("display_name", "icon") and len(value) > MAX_NAME_LENGTH:
                        pane.set_summary(f"{path} must be at most {MAX_NAME_LENGTH} characters (got {len(value)}).")
                        return
                    if key == "impact" and value not in allowed_impacts:
                        pane.set_summary(f"Impact must be one of: Standard, Limited, Suppressed (got {value!r}).")
                        return
                    if key == "objective":
                        try:
                            parsed = float(value)
                        except ValueError:
                            pane.set_summary("Objective must be a numeric percentage (0-100).")
                            return
                        if parsed < 0 or parsed > 100:
                            pane.set_summary("Objective must be between 0 and 100.")
                            return
                        updates.append((path, parsed))
                        continue
                    updates.append((path, value))
                if not updates:
                    pane.set_summary("No changed fields to update.")
                    return
                for path, value in updates:
                    self._draft_store.update_field(target, path, value)
                self._changes_result_by_target[target] = f"Draft updated ({len(updates)} field(s))."
                self._form_target = None  # refill from the draft, so "99.00" shows as the stored "99.0", not as an edit
                self._render_changes()
                return
            if button_id in {"changes-save", "changes-export"}:
                raw_path = pane.save_path()
                if not raw_path:
                    pane.set_summary("Enter a save/export path.")
                    return
                path = Path(raw_path).expanduser()
                self._draft_store.export_to_json(target, path, overwrite=(button_id == "changes-export"))
                self._changes_result_by_target[target] = f"Saved draft to {path}"
                self._render_changes()
                return
            if button_id == "changes-restore":
                raw_path = pane.restore_path()
                if not raw_path:
                    pane.set_summary("Enter a restore path.")
                    return
                path = Path(raw_path).expanduser()
                raw = json.loads(path.read_text(encoding="utf-8"))
                target_raw = raw.get("target") if isinstance(raw, dict) else None
                expected = {
                    "endpoint": target.model.endpoint,
                    "subscription": target.model.subscription,
                    "group": target.model.group,
                    "name": target.model.name,
                    "kind": target.kind,
                    "resourceName": target.name,
                }
                if not isinstance(target_raw, dict) or any(target_raw.get(k) != v for k, v in expected.items()):
                    pane.set_summary("Restore rejected: draft file targets a different model/resource.")
                    return
                self._draft_store.restore_from_json(
                    target=target,
                    expected_baseline=draft.baseline,
                    path=path,
                    confirm_overwrite_unsaved=(pane.take_confirmation("restore") == target.name),
                )
                self._prepared_apply_plan = None
                self._form_target = None  # the file replaces the whole form, typed text included
                self._changes_result_by_target[target] = f"Restored draft from {path}"
                self._render_changes()
                return
            if button_id == "changes-discard":
                self._draft_store.discard(target)
                self._prepared_apply_plan = None
                self._changes_result_by_target.pop(target, None)
                self._render_changes()
                self.notify(f"Discarded the draft for {target.name}.")
                return
            if button_id == "changes-prepare-apply":
                if self._applied_snapshot is not None:
                    pane.set_summary("Apply is live-only. Return to live first.")
                    return
                if self._changes_form_has_pending_edits():
                    pane.set_summary("Update draft first: the plan would not include the edits in the form.")
                    return
                pane.set_summary("Preparing apply plan...")
                pane.set_result("")
                self._changes_result_by_target.pop(target, None)
                self.run_worker(self._prepare_apply(target, draft), group="changes-prepare", exclusive=True)
                return
            if button_id == "changes-apply":
                plan = self._prepared_apply_plan
                if plan is None or plan.target != target:
                    pane.set_summary("Prepare apply first.")
                    return
                # Consume the plan now so a second click cannot issue a second write.
                self._prepared_apply_plan = None
                pane.set_summary("Applying prepared plan...")
                self._applies_in_flight += 1  # before the worker starts, so no click in between starts a second write
                self.run_worker(
                    self._apply_prepared(plan.target, draft.revision, plan, pane.take_confirmation("apply")),
                    group="changes-apply",
                )
                return
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if isinstance(exc, ValueError) and "explicit overwrite confirmation" in message:
                pane.set_summary("Restore blocked: type resource name in confirmation field to overwrite unsaved draft.")
                return
            pane.set_summary(f"{type(exc).__name__}: {exc}")

    async def _prepare_apply(self, target: ResourceTarget, draft: DraftSnapshot) -> None:
        """Scope lookup and live reads run in a thread so the UI keeps rendering."""
        pane = self.query_one("#changes-pane", ChangesPane)
        try:
            scope = await asyncio.to_thread(self._apply.describe_connection_scope, target)
        except ValueError as exc:
            pane.set_summary(f"Prepare blocked: unable to verify the connected tenant and subscription: {exc}")
            pane.set_plan("")
            self._prepared_apply_plan = None
            return
        try:
            plan_or_result = await asyncio.to_thread(
                self._apply.prepare_plan,
                target=target,
                revision=draft.revision,
                edited_resource=draft.edited,
                draft_live_baseline=draft.baseline,
            )
        except Exception as exc:  # noqa: BLE001 (surface read failures in the pane)
            pane.set_summary(f"{type(exc).__name__}: {exc}")
            return
        if isinstance(plan_or_result, ApplyResult):
            pane.set_summary(plan_or_result.message)
            pane.set_details(plan_or_result.race_warning)
            self._changes_result_by_target[target] = plan_or_result.message
            return
        if self._draft_store.active_target() != target:
            return  # the user switched drafts while this plan was being prepared
        if self._changes_form_has_pending_edits() or self._current_revision_for_target(target) != draft.revision:
            pane.set_summary("Plan dropped: the draft or form changed while preparing. Update draft, then prepare again.")
            return
        self._prepared_apply_plan = plan_or_result
        changed = tuple(changed_paths(plan_or_result.changes))
        state = self._poller.view.entries.get(target.model)
        entities = state.previous.forest.entities if state is not None and state.previous else {}
        consumers = ((entities[name].display_name if name in entities else "") or name
                     for name in plan_or_result.shared_consumer_entity_names)
        pane.set_plan(
            "\n".join(
                [
                    f"Tenant: {scope.tenant_name or '(unnamed tenant)'} ({scope.tenant_id})",
                    f"Subscription: {scope.subscription_name} ({scope.subscription_id})",
                    f"ARM endpoint: {scope.arm_endpoint}",
                    f"Model: {plan_or_result.target.model.group}/{plan_or_result.target.model.name}",
                    f"Prepared target: {plan_or_result.target.kind}:{plan_or_result.target.name}",
                    f"Revision: {plan_or_result.revision}",
                    f"Shared consumers: {', '.join(consumers) or 'none'}",
                ]
            )
        )
        pane.set_details(
            "\n".join(
                [
                    "Changed fields:",
                    *(f" - {field}" for field in changed),
                    "",
                    (
                        f"Race warning: {format_utc(plan_or_result.generated_at)} "
                        "plan is non-atomic and state may change before apply."
                    ),
                ]
            )
        )
        pane.set_summary("Plan prepared. Review details, then type apply confirmation and run Apply prepared.")
        self._changes_result_by_target[target] = (
            f"Prepared apply for {plan_or_result.target.kind}:{plan_or_result.target.name}"
        )

    async def _apply_prepared(
        self, target: ResourceTarget, revision: int, plan: ApplyPlan, confirmation: str,
    ) -> None:
        pane = self.query_one("#changes-pane", ChangesPane)
        pane.set_plan("")  # the coordinator uses a plan once, even when the apply raises
        try:
            result = await asyncio.to_thread(
                self._apply.apply_confirmed, plan, confirmation_name=confirmation,
            )
        except Exception as exc:  # noqa: BLE001 (surface apply failures in the pane)
            pane.set_summary(f"{type(exc).__name__}: {exc}")
            return
        finally:
            self._applies_in_flight -= 1
        self._changes_result_by_target[target] = "\n".join(filter(None, (  # the error before the field list: short screens show 3 lines
            f"Apply {result.outcome.value}: {result.message}",
            result.write_error and f"Write error: {result.write_error}",
            result.changed_fields and (
                f"{'Changed' if result.outcome.value == 'applied' else 'Not confirmed'}: {'; '.join(result.changed_fields)}"
            ),
        )))
        current = self._draft_store.get(target)
        if result.read_back is not None and current is not None:
            if result.outcome.value == "applied" and current.revision == revision:
                self._draft_store.accept_applied(target, result.read_back)
            else:
                self._draft_store.rebase(target, result.read_back, plan.edited_resource)  # the next Prepare diffs against live
        self._render_changes()

    def _current_revision_for_target(self, target: ResourceTarget) -> int:
        draft = self._draft_store.get(target)
        if draft is None:
            return -1
        return draft.revision

    def _is_live_scope_for_target(self, target: ResourceTarget) -> bool:
        return self._applied_snapshot is None
