from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.app import App
from textual.widgets import Button, Label, Static, TextArea

from azext_healthmodels_plus.tui.domain.drafts import build_apply_plan
from azext_healthmodels_plus.tui.domain.drafts import ResourceTarget
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.watch.draft_store import DraftStore
from azext_healthmodels_plus.tui.watch.query_editor import QueryEditor
from azext_healthmodels_plus.tui.watch.shortcuts_dialog import ActionsDialog


class _EditorHost(App[None]):
    def __init__(self, editor: QueryEditor) -> None:
        super().__init__()
        self._editor = editor

    def on_mount(self) -> None:
        self.push_screen(self._editor)


class _QuitGuardDialog(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def compose(self) -> Any:
        with Vertical(id="quit-dialog"):
            yield Label("Unsaved drafts")
            yield Static("Save, discard, or cancel.")
            with Horizontal():
                yield Button("Save", id="quit-save")
                yield Button("Discard", id="quit-discard")
                yield Button("Cancel", id="quit-cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit-save":
            self.dismiss("save")
        elif event.button.id == "quit-discard":
            self.dismiss("discard")
        else:
            self.dismiss("cancel")


class _EditorHostWithQuitActions(_EditorHost):
    def __init__(self, editor: QueryEditor) -> None:
        super().__init__(editor)
        self.quit_choices: list[str] = []

    def action_quit(self) -> None:
        self.push_screen(_QuitGuardDialog(), self._record_quit_choice)

    def action_open_actions(self) -> None:
        self.push_screen(ActionsDialog(), self._run_named_action)

    def _record_quit_choice(self, choice: str | None) -> None:
        if choice:
            self.quit_choices.append(choice)

    def _run_named_action(self, action_name: str | None) -> None:
        if action_name == "quit":
            self.action_quit()


def _make_client(query_text: str = "rate(original[5m])") -> MagicMock:
    client = MagicMock()
    client.get_entity.return_value = {
        "name": "entity",
        "properties": {
            "displayName": "Entity Display",
            "tags": {"healthState": "keep-this-unknown-field"},
            "healthState": "Healthy",
            "signalGroups": {
                "default": {
                    "azureMonitorWorkspaceResourceId": "/subscriptions/x",
                    "signals": [{
                        "name": "sig-A",
                        "displayName": "S",
                        "signalKind": "PrometheusMetricsQuery",
                        "status": {"healthState": "Healthy", "reportedAt": "2026-01-01T00:00:00Z"},
                        "signalDefinitionName": "def-1",
                        "evaluationRules": {},
                    }],
                },
            },
        },
    }
    client.get_signal_definition.return_value = {
        "name": "def-1",
        "properties": {
            "queryText": query_text,
            "signalKind": "PrometheusMetricsQuery",
            "displayName": "Signal A",
        },
    }
    return client


def _target() -> ResourceTarget:
    return ResourceTarget(
        ModelKey("https://management.azure.com", "sub", "rg", "model"),
        "entity",
        "entity",
    )


def _definition_target() -> ResourceTarget:
    base = _target()
    return ResourceTarget(base.model, "signal_definition", "def-1")


async def _wait_loaded(editor: QueryEditor, pilot: Any) -> None:
    for _ in range(8):
        await pilot.pause()
        if editor._loaded:  # type: ignore[attr-defined]
            return
    assert editor._loaded  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_query_editor_default_constructor_path_unchanged() -> None:
    client = _make_client()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A")
    app = _EditorHost(editor)
    captured: dict[str, Any] = {}

    def fake_execute(*_args, **kwargs):
        captured["query_override"] = kwargs.get("query_override")
        return {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}

    with patch("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", side_effect=fake_execute):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
    assert captured["query_override"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(("value_column", "shown"), [("", "first column"), ("count_", "count_")])
async def test_query_editor_shows_the_log_analytics_value_column(value_column, shown) -> None:
    client = _make_client()
    signal = client.get_entity.return_value["properties"]["signalGroups"]["default"]["signals"][0]
    signal.update(signalKind="LogAnalyticsQuery", valueColumnName=value_column)
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A")
    async with _EditorHost(editor).run_test(size=(120, 40)) as pilot:
        await _wait_loaded(editor, pilot)
        meta = str(editor.query_one("#qe-meta", Static).render())
    assert f"{'Value column':<18}{shown}\n" in meta


@pytest.mark.anyio
@pytest.mark.parametrize("keys", [("escape",), ("ctrl+r", "escape")])
@pytest.mark.parametrize(
    ("query_text", "typed", "drafts"),
    [
        ("rate(original[5m])", None, ()),
        ("rate(original[5m])", "rate(changed[5m])", (_definition_target(),)),
        ("", None, ()),  # a metric signal has no query text
        ("", "rate(changed[5m])", (_target(),)),
    ],
)
async def test_query_editor_creates_a_draft_only_when_the_query_changes(keys, query_text, typed, drafts) -> None:
    store = DraftStore()
    editor = QueryEditor(_make_client(query_text), "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=_target())
    app = _EditorHost(editor)
    result = {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}
    with patch("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", return_value=result):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_loaded(editor, pilot)
            if typed is not None:
                editor.query_one("#qe-query", TextArea).text = typed
                await pilot.pause()
            for key in keys:
                await pilot.press(key)
                await app.workers.wait_for_complete()
                await pilot.pause()
    assert store.list_targets() == drafts
    assert store.active_target() == (drafts[0] if drafts else None)


@pytest.mark.anyio
async def test_query_editor_edit_during_a_test_drops_the_stale_result() -> None:
    store = DraftStore()
    editor = QueryEditor(_make_client(), "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=_target())
    app = _EditorHost(editor)

    def fake_execute(*_args, **_kwargs):
        store.update_field(_definition_target(), "properties.queryText", "typed during the test")
        return {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}

    with patch("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", side_effect=fake_execute):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_loaded(editor, pilot)
            editor.query_one("#qe-query", TextArea).text = "typed before the test"
            await pilot.pause()
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.is_running
    assert store.get(_definition_target()).tested_revision is None


@pytest.mark.anyio
async def test_query_editor_retains_query_across_reopen_and_tracks_revision() -> None:
    client = _make_client()
    store = DraftStore()
    target = _target()
    first = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
    app = _EditorHost(first)

    def fake_execute(*_args, **_kwargs):
        return {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}

    with patch("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", side_effect=fake_execute):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            ta = first.query_one("#qe-query", TextArea)
            assert app.focused is ta  # typed letters go to the query, not to app keys
            ta.text = "rate(edited_metric[5m])"
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            reopened = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
            app.push_screen(reopened)
            await pilot.pause()
            await pilot.pause()
            reopened_ta = reopened.query_one("#qe-query", TextArea)
            assert reopened_ta.text == "rate(edited_metric[5m])"

    snapshot = store.get(_definition_target())
    assert snapshot is not None
    retained = snapshot.edited["properties"]["queryText"]  # type: ignore[index]
    assert retained == "rate(edited_metric[5m])"
    assert snapshot.edited["properties"]["displayName"] == "Signal A"  # type: ignore[index]

    plan = build_apply_plan(
        target=_definition_target(),
        revision=snapshot.revision,
        edited_resource=snapshot.edited,
        expected_baseline=snapshot.baseline,
        live_baseline=snapshot.baseline,
        generated_at=datetime.now(UTC),
    )
    reviewed_payload = plan.edited_resource
    assert reviewed_payload["properties"]["queryText"] == "rate(edited_metric[5m])"  # type: ignore[index]
    assert reviewed_payload["properties"]["displayName"] == "Signal A"  # type: ignore[index]
    assert snapshot.revision >= 1
    assert snapshot.tested_revision == snapshot.revision

    entity_snapshot = store.get(target)
    assert entity_snapshot is None


@pytest.mark.anyio
async def test_query_editor_uses_actual_draft_query_not_raw_source_mutation() -> None:
    client = _make_client()
    store = DraftStore()
    target = _target()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
    app = _EditorHost(editor)
    captured: dict[str, Any] = {}

    def fake_execute(*_args, **kwargs):
        captured["query_override"] = kwargs.get("query_override")
        return {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}

    with patch("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", side_effect=fake_execute):
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_loaded(editor, pilot)
            ta = editor.query_one("#qe-query", TextArea)
            ta.text = "rate(draft_query[5m])"
            client.get_signal_definition.return_value["properties"]["queryText"] = "rate(raw_mutated[5m])"
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()

    assert captured["query_override"] == "rate(draft_query[5m])"


@pytest.mark.anyio
async def test_query_editor_ctrl_q_quit_cancel_retains_buffer() -> None:
    client = _make_client()
    store = DraftStore()
    target = _target()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
    app = _EditorHostWithQuitActions(editor)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_loaded(editor, pilot)
        ta = editor.query_one("#qe-query", TextArea)
        ta.text = "rate(ctrl_q_retained[5m])"
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _QuitGuardDialog)
        await pilot.click("#quit-cancel")
        await pilot.pause()
        assert app.screen is editor

    snapshot = store.get(_definition_target())
    assert snapshot is not None
    assert snapshot.edited["properties"]["queryText"] == "rate(ctrl_q_retained[5m])"  # type: ignore[index]


@pytest.mark.anyio
async def test_query_editor_f2_then_quit_uses_same_guard_and_retains_buffer() -> None:
    client = _make_client()
    store = DraftStore()
    target = _target()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
    app = _EditorHostWithQuitActions(editor)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_loaded(editor, pilot)
        ta = editor.query_one("#qe-query", TextArea)
        ta.text = "rate(f2_retained[5m])"
        app.set_focus(ta)
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        assert isinstance(app.screen, ActionsDialog)
        await pilot.click("#actions-filter")
        await pilot.press("q", "u", "i", "t")
        await pilot.pause()
        await pilot.click("#action-quit")
        await pilot.pause()
        assert isinstance(app.screen, _QuitGuardDialog)
        await pilot.click("#quit-cancel")
        await pilot.pause()
        assert app.screen is editor

    snapshot = store.get(_definition_target())
    assert snapshot is not None
    assert snapshot.edited["properties"]["queryText"] == "rate(f2_retained[5m])"  # type: ignore[index]


@pytest.mark.anyio
async def test_query_editor_bare_q_remains_literal() -> None:
    client = _make_client()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A")
    app = _EditorHostWithQuitActions(editor)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        ta = editor.query_one("#qe-query", TextArea)
        ta.text = ""
        app.set_focus(ta)
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert ta.text == "q"


@pytest.mark.anyio
async def test_query_editor_config_load_failure_does_not_invent_draft_on_quit() -> None:
    client = _make_client()
    client.get_entity.side_effect = RuntimeError("load failed")
    store = DraftStore()
    target = _target()
    editor = QueryEditor(client, "rg", "hm", "entity", "sig-A", draft_store=store, draft_target=target)
    app = _EditorHostWithQuitActions(editor)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _QuitGuardDialog)
        await pilot.click("#quit-cancel")
        await pilot.pause()

    assert store.get(target) is None
