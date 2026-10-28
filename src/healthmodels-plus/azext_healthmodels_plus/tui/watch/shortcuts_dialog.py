"""Help and actions dialogs for keyboard-discoverable workspace controls."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class _BaseDialog(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss_dialog", "Close", priority=True)]

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)


class HelpDialog(_BaseDialog):
    """Lists the app's own bindings, so help cannot drift from the keys."""

    def __init__(self, bindings: list[Binding]) -> None:
        super().__init__()
        self._shortcuts = bindings

    def compose(self) -> ComposeResult:
        keys: dict[str, list[str]] = {}
        for binding in self._shortcuts:
            key = self.app.get_key_display(binding)
            keys.setdefault(binding.description, []).append(key if len(key) == 1 else key.replace("^", "ctrl+").title())  # F1, Esc, Ctrl+Q
        with Vertical(id="shortcut-dialog"):
            yield Label("Help", classes="shortcut-title")
            with VerticalScroll(id="help-scroll"):  # focused on open, so arrows and Page keys scroll a short terminal
                yield Static("Arrows move, Enter expands or activates, Tab moves focus. Esc closes this dialog.")
                for description, shown in keys.items():
                    yield Static(f"{' / '.join(shown):<16} {description}")


class ActionsDialog(_BaseDialog):
    """Dismisses with the app action a button names: its id is ``action-<name>`` for ``action_<name>``."""

    BINDINGS = [
        Binding("down", "step(1)", "Next action", priority=True),
        Binding("up", "step(-1)", "Previous action", priority=True),
    ]

    def __init__(self, *, include_back_to_models: bool = False) -> None:
        super().__init__()
        self._dismissed = False
        self._include_back_to_models = include_back_to_models

    def compose(self) -> ComposeResult:
        with Vertical(id="actions-dialog"):
            yield Label("Actions", classes="shortcut-title")
            yield Input(placeholder="Filter actions", id="actions-filter")
            with VerticalScroll(id="actions-scroll"):
                yield Button("Search (/)", id="action-open_search")
                yield Button("Edit selected (e)", id="action-edit_query")
                yield Button("Details (d)", id="action-show_details")
                yield Button("Verify signal (v)", id="action-verify_signal")
                yield Button("Timeline tab (2)", id="action-show_timeline")
                yield Button("Changes tab (3)", id="action-show_changes")
                yield Button("Refresh (r)", id="action-force_refresh")
                if self._include_back_to_models:
                    yield Button("Back to models", id="action-back_to_models")
                yield Button("Quit (q / Ctrl+Q)", id="action-quit")
            yield Static("No matching actions.", id="actions-empty")
        self.call_after_refresh(self._sync_empty_state)

    @on(Input.Changed, "#actions-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        query = event.value.strip().casefold()
        for button in self.query("Button"):
            if not button.id or not button.id.startswith("action-"):
                continue
            label = str(button.label).casefold()
            button.display = (not query) or (query in label)
        self._sync_empty_state()
        focused = self.focused
        if isinstance(focused, Button) and (not focused.display or focused.disabled):
            first_visible = self._first_visible_action()
            self.set_focus(first_visible if first_visible is not None else self.query_one("#actions-filter", Input))

    @on(Input.Submitted, "#actions-filter")
    def _on_filter_submitted(self, _event: Input.Submitted) -> None:
        first_visible = self._first_visible_action()
        if first_visible is not None:
            self._dismiss_action(first_visible.id.removeprefix("action-"))

    @on(Button.Pressed)
    def _on_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("action-"):
            self._dismiss_action(button_id.removeprefix("action-"))

    def action_step(self, delta: int) -> None:
        """Move focus *delta* actions on; from the filter, Down goes to the first and Up to the last."""
        actions = self._visible_actions()
        if not actions:
            return
        focused = self.focused
        if focused in actions:
            self.set_focus(actions[min(max(actions.index(focused) + delta, 0), len(actions) - 1)])
        else:
            self.set_focus(actions[0 if delta > 0 else -1])

    def _visible_actions(self) -> list[Button]:
        return [
            button for button in self.query("Button")
            if button.id and button.id.startswith("action-") and button.display and not button.disabled
        ]

    def _first_visible_action(self) -> Button | None:
        actions = self._visible_actions()
        return actions[0] if actions else None

    def _sync_empty_state(self) -> None:
        empty = self.query_one("#actions-empty", Static)
        empty.display = not bool(self._visible_actions())

    def _dismiss_action(self, action_name: str) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(action_name)
