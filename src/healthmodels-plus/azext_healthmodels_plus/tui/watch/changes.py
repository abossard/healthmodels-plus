"""Changes side pane for retained drafts, guided edits, and guarded apply."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalGroup, VerticalScroll
from textual.widgets import Button, Input, Static


# The guided fields: key -> (path in the resource, draft kind). Input ids are field_input_id(key).
FIELDS: dict[str, tuple[str, str]] = {
    "display_name": ("properties.displayName", "entity"),
    "impact": ("properties.impact", "entity"),
    "objective": ("properties.healthObjective", "entity"),
    "icon": ("properties.icon.iconName", "entity"),
    "query_text": ("properties.queryText", "signal_definition"),
}


def field_input_id(key: str) -> str:
    return "changes-" + key.replace("_", "-")


class ChangesPane(Vertical):
    _filled = dict.fromkeys(FIELDS, "")  # the values last filled in, so typed values can be told apart

    def compose(self) -> ComposeResult:
        yield Static("Changes", id="changes-title")
        yield Static("Drafts: none", id="changes-drafts")
        with VerticalScroll(id="changes-result-scroll"):  # focusable, so a cut-off result can be scrolled
            yield Static("", id="changes-result")
        with VerticalScroll(id="changes-form-scroll"):
            # One section per draft kind; set_kind shows the active draft's.
            with VerticalGroup(id="changes-entity-fields", classes="changes-kind"):
                yield Static("Entity fields", classes="changes-section-label")
                yield Static("Display name", classes="changes-field-label")
                with Horizontal():
                    yield Input(placeholder="Display name", id="changes-display-name")
                yield Static("Impact (Standard|Limited|Suppressed)", classes="changes-field-label", id="changes-label-impact")
                with Horizontal():
                    yield Input(placeholder="Impact", id="changes-impact")
                yield Static("Health objective (0-100)", classes="changes-field-label")
                with Horizontal():
                    yield Input(placeholder="Objective", id="changes-objective")
                yield Static("Icon name", classes="changes-field-label")
                with Horizontal():
                    yield Input(placeholder="Icon", id="changes-icon")
            with VerticalGroup(id="changes-signal_definition-fields", classes="changes-kind"):
                yield Static("Signal definition fields", classes="changes-section-label")
                yield Static("Query text", classes="changes-field-label")
                with Horizontal():
                    yield Input(placeholder="Query text", id="changes-query-text")
            with Horizontal():
                yield Button("Update draft", id="changes-review")
            with Horizontal():
                yield Input(placeholder="Save/export path", id="changes-save-path")
                yield Button("Save", id="changes-save")
                yield Button("Overwrite", id="changes-export")
            with Horizontal():
                yield Input(placeholder="Restore path", id="changes-restore-path")
            with Horizontal():
                yield Button("Restore", id="changes-restore")
                yield Button("Discard", id="changes-discard")
            with Horizontal():
                yield Input(placeholder="Restore confirm resource name", id="changes-restore-confirm")
        with Vertical(id="changes-apply-controls"):
            with VerticalScroll(id="changes-review-scroll"):
                yield Static("", id="changes-summary")
                yield Static("", id="changes-plan")
                yield Static("", id="changes-details")
            with Horizontal():
                yield Button("Prepare apply", id="changes-prepare-apply")
                yield Button("Apply prepared", id="changes-apply")
            with Horizontal():
                yield Input(placeholder="Apply confirm resource name", id="changes-apply-confirm")

    def save_path(self) -> str:
        return self.query_one("#changes-save-path", Input).value.strip()

    def restore_path(self) -> str:
        return self.query_one("#changes-restore-path", Input).value.strip()

    def take_confirmation(self, action: str) -> str:
        """The resource name typed for `action` ("apply" or "restore"), cleared so every use needs it typed again."""
        confirm = self.query_one(f"#changes-{action}-confirm", Input)
        value, confirm.value = confirm.value.strip(), ""
        return value

    def set_drafts(self, text: str) -> None:
        self.query_one("#changes-drafts", Static).update(Text(text))

    def set_summary(self, text: str) -> None:
        self.query_one("#changes-summary", Static).update(Text(text))

    def set_result(self, text: str) -> None:
        self.query_one("#changes-result", Static).update(Text(text))
        self.query_one("#changes-result-scroll").display = bool(text)  # an empty bordered box is noise

    def set_plan(self, text: str) -> None:
        self.query_one("#changes-plan", Static).update(Text(text))

    def set_details(self, text: str) -> None:
        self.query_one("#changes-details", Static).update(Text(text))

    def form_values(self) -> dict[str, str]:
        return {key: self.query_one(f"#{field_input_id(key)}", Input).value for key in FIELDS}

    def set_form_values(self, values: dict[str, str], *, keep_typed: bool = False) -> None:
        typed = self.form_values()
        for key, value in values.items():
            if keep_typed and typed[key] not in ("", self._filled[key]) and value == self._filled[key]:
                continue
            self.query_one(f"#{field_input_id(key)}", Input).value = value
        self._filled = values

    def edits(self, base: dict[str, str]) -> dict[str, str]:
        """The typed fields that differ from *base*; empty means no edit."""
        return {key: value for key, value in self.form_values().items() if value and value != base[key]}

    def set_kind(self, kind: str) -> None:
        for section in self.query(".changes-kind"):
            section.display = section.id == f"changes-{kind}-fields"
