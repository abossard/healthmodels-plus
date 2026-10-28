from __future__ import annotations

import json
import threading
from types import MappingProxyType
from datetime import UTC, datetime
from datetime import timedelta
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.containers import ScrollableContainer
from textual.css.query import NoMatches
from textual.widgets import Button

from azext_healthmodels_plus.tui.client.errors import AuthenticationError
from azext_healthmodels_plus.tui.domain.drafts import ApplyPlan, FieldDiff, ResourceTarget
from azext_healthmodels_plus.tui.domain.history import HistoryBucketHealth, SnapshotNode, parse_history_events
from azext_healthmodels_plus.tui.domain.history import SnapshotKey
from azext_healthmodels_plus.tui.domain.history import RangeKey
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.domain.occurrences import project
from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
from azext_healthmodels_plus.tui.watch.app import QuitDraftsDialog
import azext_healthmodels_plus.tui.watch.app as watch_app_module
from azext_healthmodels_plus.tui.watch.apply import ApplyOutcomeKind, ApplyResult
from azext_healthmodels_plus.tui.watch.changes import ChangesPane
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
from azext_healthmodels_plus.tui.watch.poller import Failed, Ready
from azext_healthmodels_plus.tui.watch.query_editor import QueryEditor
from azext_healthmodels_plus.tui.watch.search_modal import SearchModal
from azext_healthmodels_plus.tui.watch.shortcuts_dialog import ActionsDialog
from azext_healthmodels_plus.tui.watch.timeline import BAND_STATES, TimelinePane, node_path_label
from .test_retained_editor import _make_client as _editor_client, _wait_loaded
from .test_temporal_monitor import _TemporalClient


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _fully_visible(widget, width: int, height: int) -> bool:
    region = widget.region
    return region.x >= 0 and region.y >= 0 and (region.x + region.width) <= width and (region.y + region.height) <= height


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_workspace_tab_buttons_toggle_panes(make_app, healthy_client, size) -> None:
    app = make_app(healthy_client)
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        timeline = app.query_one("#timeline-pane", TimelinePane)
        changes = app.query_one("#changes-pane", ChangesPane)
        assert timeline.display is False
        assert changes.display is False

        await pilot.click("#ws-tab-timeline")
        await pilot.pause()
        assert timeline.display is True
        assert changes.display is False

        await pilot.click("#ws-tab-changes")
        await pilot.pause()
        assert timeline.display is False
        assert changes.display is True

        await pilot.click("#ws-tab-tree")
        await pilot.pause()
        assert timeline.display is False
        assert changes.display is False


@pytest.mark.anyio
@pytest.mark.parametrize(("key", "tab"), [("2", "timeline"), ("3", "changes"), ("1", "tree")])
async def test_digit_keys_switch_tabs_and_help_lists_them(make_app, healthy_client, key, tab) -> None:
    app = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("2" if key == "1" else "1", key)
        await pilot.pause()
        assert app.query_one(f"#ws-tab-{tab}").has_class("-selected")
        await pilot.press("f1")
        await pilot.pause()
        help_text = " ".join(str(widget.render()) for widget in app.screen.query("Static"))
        assert f"{key} / Ctrl+{key} " in help_text and f"{tab.title()} tab" in help_text
        assert "F1 " in help_text and "Esc " in help_text and "q / Ctrl+Q " in help_text  # as the status bar and README spell keys


@pytest.mark.anyio
async def test_workspace_tab_timeline_then_tree_click_back_works_at_80x24(make_app, healthy_client) -> None:
    app = make_app(healthy_client)
    async with app.run_test(size=(80, 24)) as pilot:
        await app._do_poll()
        await pilot.pause()
        assert await pilot.click("#ws-tab-timeline")
        await pilot.pause()
        assert app.query_one("#timeline-pane", TimelinePane).display is True
        assert await pilot.click("#ws-tab-tree")
        await pilot.pause()
        assert app.query_one("#timeline-pane", TimelinePane).display is False
        assert app.query_one("#changes-pane", ChangesPane).display is False


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_workspace_tab_labels_render_with_positive_content_height(make_app, healthy_client, size) -> None:
    app = make_app(healthy_client)
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree_button = app.query_one("#ws-tab-tree", Button)
        timeline_button = app.query_one("#ws-tab-timeline", Button)
        changes_button = app.query_one("#ws-tab-changes", Button)
        assert tree_button.content_region.height > 0
        assert timeline_button.content_region.height > 0
        assert changes_button.content_region.height > 0
        svg = app.export_screenshot()
        rendered_text = "".join(ET.fromstring(svg).itertext()).replace("\xa0", " ")
        assert "Tree" in rendered_text
        assert "Timeline" in rendered_text
        assert "Changes" in rendered_text


@pytest.mark.anyio
async def test_timeline_range_button_requests_range_key(make_app, healthy_client) -> None:
    app = make_app(healthy_client)
    captured = []
    app._request_timeline = lambda *, hours: captured.append(hours)  # type: ignore[assignment]

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_timeline()
        app._handle_timeline_button("timeline-range-6h")
        await pilot.pause()

    assert captured
    assert captured[-1] == 6


@pytest.mark.anyio
async def test_changes_apply_blocked_in_historical_mode(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "signal_definition", "sig-A")
    app._draft_store.get_or_create(
        target,
        {"name": "sig-A", "properties": {"signalKind": "PrometheusMetricsQuery", "queryText": "rate(x[5m])"}},
    )
    app._applied_snapshot = SnapshotKey(model, datetime.now(UTC))

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()

        pane = app.query_one("#changes-pane", ChangesPane)
        assert "live-only" in str(pane.query_one("#changes-summary").render()).lower()


@pytest.mark.anyio
async def test_query_editor_target_prefers_entity_when_instance_query_present(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    captured = {}

    def fake_push(screen):
        assert isinstance(screen, QueryEditor)
        captured["target"] = screen._draft_target  # type: ignore[attr-defined]

    app.push_screen = fake_push  # type: ignore[assignment]

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        for node in _walk(tree.root):
            if node.data is not None and not node.data.is_signal:
                node.expand()
        await pilot.pause()
        signal_node = next(n for n in _walk(tree.root) if n.data is not None and n.data.is_signal)
        tree.move_cursor(signal_node)
        await pilot.pause()
        signal_name = signal_node.data.entity_name
        entity_name = signal_node.data.owner_entity_name
        healthy_client.get_entity.return_value = {
            "name": entity_name,
            "properties": {
                "signalGroups": {"default": {"signals": [{"name": signal_name, "queryText": "rate(x[5m])"}]}},
            },
        }
        app.action_edit_query()
        await pilot.pause()

    target = captured["target"]
    assert target is not None
    assert target.kind == "entity"
    assert target.name == entity_name


@pytest.mark.anyio
async def test_query_editor_target_uses_definition_when_instance_query_absent(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    captured = {}

    def fake_push(screen):
        assert isinstance(screen, QueryEditor)
        captured["target"] = screen._draft_target  # type: ignore[attr-defined]

    app.push_screen = fake_push  # type: ignore[assignment]

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        for node in _walk(tree.root):
            if node.data is not None and not node.data.is_signal:
                node.expand()
        await pilot.pause()
        signal_node = next(n for n in _walk(tree.root) if n.data is not None and n.data.is_signal)
        tree.move_cursor(signal_node)
        await pilot.pause()
        signal_name = signal_node.data.entity_name
        entity_name = signal_node.data.owner_entity_name
        key = signal_node.data.model or app._poller.root
        state = app._poller.view.entries[key]
        entity = state.previous.forest.entities[entity_name]  # type: ignore[index,union-attr]
        signal = next(item for item in entity.signals if item.name == signal_name)
        healthy_client.get_entity.return_value = {
            "name": entity_name,
            "properties": {
                "signalGroups": {"default": {"signals": [{"name": signal_name}]}},
            },
        }
        app.action_edit_query()
        await pilot.pause()

    target = captured["target"]
    assert target is not None
    assert target.kind == "entity"
    assert target.name == entity_name


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (80, 40), (160, 50)])
async def test_query_editor_shows_title_query_and_buttons_and_reaches_results(make_app, healthy_client, size) -> None:
    app = make_app(healthy_client)
    editor = QueryEditor(_editor_client(), "rg", "hm", "entity", "sig-A")
    fetch = editor._fetch_config
    editor._fetch_config = lambda: {**fetch(), "data_source": "long data source " * 60}  # wraps into many config rows
    async with app.run_test(size=size) as pilot:
        app.push_screen(editor)
        await _wait_loaded(editor, pilot)
        await pilot.pause()
        await pilot.pause()  # the scroll to the buttons runs after the layout refresh
        root = editor.query_one("#query-editor-root")
        query = editor.query_one("#qe-query")
        assert query.content_region.height > 0 and root.content_region.contains_region(query.region)  # in view on open
        rendered_text = "".join(ET.fromstring(app.export_screenshot()).itertext()).replace("\xa0", " ")
        for label in (str(editor.query_one("#qe-title").render()), "Test Query (Ctrl+R)", "Close (Esc)"):
            assert label in rendered_text
        root.scroll_home(animate=False)
        await pilot.pause()
        meta = editor.query_one("#qe-meta")
        assert root.content_region.contains_point(meta.region.offset)  # the config above stays reachable

        assert list(editor.query(ScrollableContainer).exclude("TextArea")) == [root]  # one scroll, so the wheel reaches everything
        results = editor.query_one("#qe-results")
        root.scroll_to_widget(results, animate=False)
        await pilot.pause()
        assert results.content_region.height > 0 and root.content_region.contains_region(results.region)


@pytest.mark.anyio
@pytest.mark.parametrize("keys", [("escape",), ("ctrl+r", "escape")])
@pytest.mark.parametrize("query_text", ["", "rate(original[5m])"])  # a metric signal has no query text
async def test_query_editor_reverted_query_leaves_no_draft_and_quits_without_asking(
    monkeypatch, make_app, healthy_client, query_text, keys,
) -> None:
    result = {"healthState": "Healthy", "rawValue": 1.0, "durationMs": 1, "timestamp": "now"}
    monkeypatch.setattr("azext_healthmodels_plus.tui.watch.query_editor.execute_signal", lambda *_a, **_kw: result)
    app = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "entity")
    editor = QueryEditor(_editor_client(query_text), "rg", "hm", "entity", "sig-A", draft_store=app._draft_store, draft_target=target)
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(editor)
        await _wait_loaded(editor, pilot)
        query = editor.query_one("#qe-query")
        query.text = "rate(changed[5m])"
        await pilot.pause()
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        query.text = query_text
        await pilot.pause()
        for key in keys:
            await pilot.press(key)
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert app._draft_store.list_targets() == ()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.anyio
async def test_restore_rejects_target_mismatch_and_unsaved_overwrite_without_confirmation(
    make_app,
    healthy_client,
    tmp_path,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "signal_definition", "sig-A")
    baseline = {"name": "sig-A", "properties": {"signalKind": "PrometheusMetricsQuery", "queryText": "rate(x[5m])"}}
    app._draft_store.get_or_create(target, baseline)
    app._draft_store.update_field(target, "properties.queryText", "rate(edited[5m])")

    mismatch_path = tmp_path / "mismatch.json"
    mismatch_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint": model.endpoint,
                    "subscription": model.subscription,
                    "group": model.group,
                    "name": "different-model",
                    "kind": "signal_definition",
                    "resourceName": "sig-A",
                },
                "draft": {"baseline": baseline, "edited": baseline, "revision": 0},
            }
        ),
        encoding="utf-8",
    )

    valid_path = tmp_path / "valid.json"
    valid_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint": model.endpoint,
                    "subscription": model.subscription,
                    "group": model.group,
                    "name": model.name,
                    "kind": "signal_definition",
                    "resourceName": "sig-A",
                },
                "draft": {"baseline": baseline, "edited": baseline, "revision": 0},
            }
        ),
        encoding="utf-8",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-restore-path").value = str(mismatch_path)
        app._handle_changes_button("changes-restore")
        assert "different model/resource" in str(pane.query_one("#changes-summary").render())

        pane.query_one("#changes-restore-path").value = str(valid_path)
        pane.query_one("#changes-restore-confirm").value = ""
        app._handle_changes_button("changes-restore")
        assert "overwrite unsaved draft" in str(pane.query_one("#changes-summary").render())

        # Restore replaces typed text even in a field whose value the file does not change.
        edited = app._draft_store.get(target).edited
        valid_path.write_text(
            json.dumps({**json.loads(valid_path.read_text(encoding="utf-8")), "draft": {"baseline": baseline, "edited": edited, "revision": 0}}),
            encoding="utf-8",
        )
        pane.query_one("#changes-query-text").value = "rate(edited[5m]) typed"
        pane.query_one("#changes-restore-confirm").value = target.name
        app._handle_changes_button("changes-restore")
        assert "Restored draft" in str(pane.query_one("#changes-result").render())
        assert pane.query_one("#changes-query-text").value == "rate(edited[5m])"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_workspace_controls_are_reachable_at_compact_sizes(make_app, healthy_client, size) -> None:
    app = make_app(healthy_client)
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.click("#ws-tab-timeline")
        await pilot.pause()
        width, height = size
        for widget_id in [
            "#timeline-range-1h",
            "#timeline-range-6h",
            "#timeline-go",
            "#timeline-prev",
            "#timeline-next",
            "#timeline-open",
            "#timeline-live",
            "#timeline-compare",
        ]:
            assert _fully_visible(app.query_one(widget_id), width, height)

        await pilot.click("#ws-tab-changes")
        await pilot.pause()
        for widget_id in [
            "#changes-review",
            "#changes-save",
            "#changes-export",
            "#changes-restore",
            "#changes-discard",
            "#changes-prepare-apply",
            "#changes-apply",
        ]:
            assert _fully_visible(app.query_one(widget_id), width, height)


@pytest.mark.anyio
async def test_timeline_controls_reachable_via_tab_and_enter(make_app, healthy_client) -> None:
    app = make_app(healthy_client)
    app._request_timeline = lambda *, hours: None  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.click("#ws-tab-timeline")
        await pilot.pause()
        app.query_one("#timeline-go-to").value = "2026-01-01T00:00:00Z"
        app.set_focus(app.query_one("#timeline-go", Button))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        summary = str(app.query_one("#timeline-summary").render())
        assert "Marked" in summary


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fill", "expected"),
    [
        (lambda pane: setattr(pane.query_one("#timeline-go-to"), "value", ""), "2026-01-01T00:20:01Z"),
        (lambda pane: setattr(pane.query_one("#timeline-go-to"), "value", "2026-01-01T00:05:00Z"), "2026-01-01T00:05:00Z"),
        # filled by an earlier load, not typed
        (lambda pane: pane.set_go_to_value(datetime(2026, 1, 1, 0, 5, tzinfo=UTC)), "2026-01-01T00:20:01Z"),
    ],
    ids=["empty", "typed", "filled"],
)
async def test_timeline_counts_only_in_window_events_and_fills_untyped_time_rounded_up(
    make_app, healthy_client, fill, expected,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    key = RangeKey(model=app._poller.root, entity_name="EntityA", start_utc=start, end_utc=start + timedelta(hours=1))
    path = ("EntityA",)
    app._timeline_key, app._timeline_path = key, path
    app._timeline_baseline_for = lambda key: None  # type: ignore[assignment]
    app._visible_occurrence_rows = lambda: [{"path": path, "name": "EntityA", "depth": 0}]  # type: ignore[assignment]
    view = SimpleNamespace(status="ready", error="", events=parse_history_events([
        {"occurredAt": "2025-12-31T23:00:00Z", "newState": "Degraded", "reason": "context"},
        {"occurredAt": "2026-01-01T00:20:00.250Z", "newState": "Healthy", "reason": "inside"},
    ]))
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        pane = app.query_one("#timeline-pane", TimelinePane)
        fill(pane)
        app._apply_timeline_ranges(key, {path: view}, {path: key})
        await pilot.pause()
        assert "1 event in" in str(pane.query_one("#timeline-summary").render())
        assert "context" not in str(pane.query_one("#timeline-events").render())
        assert pane.go_to_value() == expected


@pytest.mark.anyio
async def test_ready_mapping_rangeview_renders_events_and_prev_next_selects_exact_time(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    selected_key = RangeKey(model=model, entity_name="EntityA", start_utc=start, end_utc=end)
    path = ("EntityA",)
    app._timeline_key = selected_key
    app._timeline_path = path
    app._timeline_baseline_for = lambda key: None  # type: ignore[assignment]
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {"path": path, "name": "EntityA", "depth": 0, "label": "EntityA"}
    ]
    history = MappingProxyType(
        {
            "history": [
                {
                    "occurredAt": "2026-01-01T00:10:00Z",
                    "newState": "Healthy",
                    "value": 1,
                    "reason": "first",
                },
                {
                    "occurredAt": "2026-01-01T00:30:00Z",
                    "newState": "Degraded",
                    "value": 2,
                    "reason": "second",
                },
            ]
        }
    )
    view = SimpleNamespace(status="ready", events=parse_history_events(history["history"]), error="")

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._apply_timeline_ranges(selected_key, {path: view}, {path: selected_key})
        await pilot.pause()
        pane = app.query_one("#timeline-pane", TimelinePane)
        bands = str(pane.query_one("#timeline-bands").render())
        events = str(pane.query_one("#timeline-events").render())
        assert "EntityA" in bands
        assert "first" in events and "second" in events
        assert pane.go_to_value() == "2026-01-01T00:30:00Z"

        app._handle_timeline_button("timeline-prev")
        await pilot.pause()
        assert pane.go_to_value() == "2026-01-01T00:10:00Z"

        app._handle_timeline_button("timeline-next")
        await pilot.pause()
        assert pane.go_to_value() == "2026-01-01T00:30:00Z"


@pytest.mark.anyio
async def test_entity_edit_opens_changes_draft(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        entity_node = next(n for n in _walk(tree.root) if n.data is not None and not n.data.is_signal and n.data.entity_name != "__unlinked__")
        tree.move_cursor(entity_node)
        tree.select_node(entity_node)
        app.action_edit_query()
        await pilot.pause()
        changes = app.query_one("#changes-pane", ChangesPane)
        assert changes.display is True
        assert "Active:" in str(changes.query_one("#changes-summary").render())


@pytest.mark.anyio
async def test_changes_pane_follows_most_recently_edited_entity(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        by_name = {
            n.data.entity_name: n for n in _walk(tree.root)
            if n.data is not None and not n.data.is_signal and not n.data.entity_name.startswith("__")
        }
        first, last = sorted(by_name)[0], sorted(by_name)[-1]
        for name in (last, first, last):
            tree.move_cursor(by_name[name])
            app.action_edit_query()
            await pilot.pause()
        summary = str(app.query_one("#changes-pane", ChangesPane).query_one("#changes-summary").render())
        assert f"Active: {app._forest.entities[last].display_name} (entity)" in summary


@pytest.mark.anyio
async def test_actions_dialog_can_run_search_then_guided_edit(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        entity_node = next(n for n in _walk(tree.root) if n.data is not None and not n.data.is_signal and n.data.entity_name != "__unlinked__")
        tree.move_cursor(entity_node)
        tree.select_node(entity_node)

        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#action-open_search")
        await pilot.pause()
        assert isinstance(app.screen, SearchModal)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#action-edit_query")
        await pilot.pause()
        changes = app.query_one("#changes-pane", ChangesPane)
        assert changes.display is True
        assert "Active:" in str(changes.query_one("#changes-summary").render())


@pytest.mark.anyio
async def test_actions_dialog_filter_can_trigger_quit_with_unsaved_guard(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    app._draft_store.update_field(target, "properties.displayName", "Edited")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#actions-filter")
        await pilot.press("q", "u", "i", "t")
        await pilot.pause()
        await pilot.click("#action-quit")
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)


@pytest.mark.anyio
async def test_back_action_hidden_for_direct_launch(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.screen.query_one("#action-back_to_models")


@pytest.mark.anyio
async def test_back_action_uses_quit_guard_and_cancel_keeps_focus_and_buffer(healthy_client) -> None:
    app = HealthWatchApp(healthy_client, "rg", "m", allow_back_to_models=True)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(
        target,
        {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}},
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pending_value = "Keep me"
        pane.query_one("#changes-display-name").value = pending_value
        await pilot.pause()

        tree = app.query_one("#health-tree", HealthTree)
        app.set_focus(tree)
        await pilot.pause()

        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#actions-filter")
        await pilot.press("b", "a", "c", "k")
        await pilot.pause()
        await pilot.click("#action-back_to_models")
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)

        await pilot.click("#quit-cancel")
        await pilot.pause()
        assert app.is_running
        assert pane.query_one("#changes-display-name").value == pending_value
        assert app.focused is tree


@pytest.mark.anyio
async def test_back_action_discard_exits_browser_launch_monitor(healthy_client) -> None:
    app = HealthWatchApp(healthy_client, "rg", "m", allow_back_to_models=True)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(
        target,
        {"name": "EntityA", "properties": {"displayName": "EntityA"}},
    )
    app._draft_store.update_field(target, "properties.displayName", "Edited")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        await pilot.click("#actions-filter")
        await pilot.press("b", "a", "c", "k")
        await pilot.pause()
        await pilot.click("#action-back_to_models")
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.click("#quit-discard")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.anyio
@pytest.mark.parametrize("quit_key", ["q", "ctrl+q"])
async def test_quit_keys_open_save_discard_cancel_modal_with_pending_form_edits(make_app, healthy_client, quit_key) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-display-name").value = "Pending"
        await pilot.pause()
        app.set_focus(app.query_one("#health-tree", HealthTree))
        await pilot.pause()

        await pilot.press(quit_key)
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        assert app.screen.query_one("#quit-save").display is True
        assert app.screen.query_one("#quit-discard").display is True
        assert app.screen.query_one("#quit-cancel").display is True

        await pilot.click("#quit-cancel")
        await pilot.pause()
        assert app.is_running

        await pilot.press(quit_key)
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.click("#quit-save")
        await pilot.pause()
        assert "Review the drafts" in str(pane.query_one("#changes-summary").render())

        app.set_focus(app.query_one("#health-tree", HealthTree))
        await pilot.pause()
        await pilot.press(quit_key)
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.click("#quit-discard")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.anyio
@pytest.mark.parametrize(("keys", "running"), [(("escape",), True), (("tab", "enter"), False)])
async def test_quit_dialog_choices_work_from_keyboard(make_app, healthy_client, keys, running) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    app._draft_store.update_field(target, "properties.displayName", "Unsaved")
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.press(*keys)  # focus starts on Save; Tab moves to Discard
        await pilot.pause()
        assert app.is_running is running


@pytest.mark.anyio
async def test_quit_after_discarding_last_draft_exits_without_prompt(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        await pilot.pause()
        app._handle_changes_button("changes-discard")
        await pilot.pause()
        app.set_focus(app.query_one("#health-tree", HealthTree))
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.anyio
@pytest.mark.parametrize("quit_key", ["q", "ctrl+q"])
async def test_quit_guard_keeps_pending_changes_when_switching_to_tree_tab(make_app, healthy_client, quit_key) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pending_value = "Pending cross-tab"
        pane.query_one("#changes-display-name").value = pending_value
        await pilot.pause()

        await pilot.click("#ws-tab-tree")
        await pilot.pause()
        assert app.query_one("#changes-pane", ChangesPane).display is False

        await pilot.press(quit_key)
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.click("#quit-cancel")
        await pilot.pause()
        assert app.is_running
        assert app.query_one("#changes-pane", ChangesPane).display is False

        await pilot.click("#ws-tab-tree")
        await pilot.pause()
        await pilot.press(quit_key)
        await pilot.pause()
        assert isinstance(app.screen, QuitDraftsDialog)
        await pilot.click("#quit-save")
        await pilot.pause()
        pane = app.query_one("#changes-pane", ChangesPane)
        assert "apply pending form edits with Update draft" in str(pane.query_one("#changes-summary").render())


@pytest.mark.anyio
async def test_prepared_plan_invalidated_after_field_edit(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard", "healthObjective": 99}}
    app._draft_store.get_or_create(target, baseline)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._prepared_apply_plan = object()  # type: ignore[assignment]
        pane = app.query_one("#changes-pane", ChangesPane)
        app.set_focus(pane.query_one("#changes-display-name"))
        await pilot.pause()
        pane.query_one("#changes-display-name").value = "Edited Later"
        await pilot.pause()
        assert "invalidated" in str(pane.query_one("#changes-summary").render()).lower()
        assert str(pane.query_one("#changes-plan").render()) == ""
        app._prepared_apply_plan = SimpleNamespace(target=target, revision=app._draft_store.get(target).revision)  # type: ignore[assignment]
        app._draft_store.get_or_create(ResourceTarget(model, "entity", "EntityB"), {"name": "EntityB", "properties": {}})
        app.action_show_changes()
        assert app._prepared_apply_plan is None
        assert str(pane.query_one("#changes-plan").render()) == ""
        assert "draft changed since prepare" not in str(pane.query_one("#changes-summary").render()).lower()  # B never had a plan


@pytest.mark.anyio
async def test_entity_changes_form_shows_labeled_entity_fields_only(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        assert pane.query_one("#changes-entity-fields").display is True
        impact_label = str(pane.query_one("#changes-label-impact").render())
        assert "Standard|Limited|Suppressed" in impact_label
        assert pane.query_one("#changes-signal_definition-fields").display is False


@pytest.mark.anyio
@pytest.mark.parametrize("reopen", ["action_show_changes", "action_edit_query"])
@pytest.mark.parametrize(
    "edit_meanwhile",
    [None, ("properties.impact", "Suppressed"), ("properties.displayName", "edited in the editor")],
    ids=["no-edit", "other-field", "typed-field"],
)
async def test_reopening_the_same_draft_keeps_typed_input_unless_the_draft_changed_that_field(
    make_app, healthy_client, reopen, edit_meanwhile,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        tree.move_cursor(next(
            n for n in _walk(tree.root) if n.data is not None and not n.data.is_signal and not n.data.entity_name.startswith("__")
        ))
        app.action_edit_query()
        target = app._draft_store.active_target()
        app.query_one("#changes-display-name").value = "typed"
        app._prepared_apply_plan = SimpleNamespace(target=target, revision=app._draft_store.get(target).revision)  # type: ignore[assignment]
        app.query_one("#changes-pane", ChangesPane).set_plan("Prepared plan")
        app.action_show_tree()
        if edit_meanwhile:  # another edit of the same draft: a query saved in the query editor, a Restore
            app._draft_store.update_field(target, *edit_meanwhile)
        getattr(app, reopen)()
        edited = app._draft_store.get(target).edited["properties"]
        typed_field_changed = edit_meanwhile is not None and edit_meanwhile[0] == "properties.displayName"
        assert app.query_one("#changes-display-name").value == (edited["displayName"] if typed_field_changed else "typed")
        assert app.query_one("#changes-impact").value == edited["impact"]
        # A plan is held only while it matches the shown draft revision, and its text shows exactly while one is held.
        assert (app._prepared_apply_plan is None) == (edit_meanwhile is not None or reopen == "action_edit_query")
        assert (str(app.query_one("#changes-plan").render()) == "") == (app._prepared_apply_plan is None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stored", "typed", "message", "revision"),
    [
        (99, {"#changes-display-name": "Renamed"}, "Draft updated (1 field(s)).", 1),
        (99, {"#changes-objective": "99.0"}, "Draft updated (1 field(s)).", 1),
        (99.0, {"#changes-objective": "99.00"}, "Draft updated (1 field(s)).", 1),
        (99, {"#changes-display-name": ""}, "No changed fields to update.", 0),
        (99, {}, "No changed fields to update.", 0),
    ],
)
async def test_update_draft_writes_and_counts_only_changed_fields(
    make_app, healthy_client, stored, typed, message, revision,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard", "healthObjective": stored}}
    app._draft_store.get_or_create(target, baseline)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        for widget_id, value in typed.items():
            pane.query_one(widget_id).value = value
        app._handle_changes_button("changes-review")
        await pilot.pause()
        shown = str(pane.query_one("#changes-result").render()) + str(pane.query_one("#changes-summary").render())
        assert message in shown
        assert app._draft_store.get(target).revision == revision
        assert not app._changes_form_has_pending_edits()  # Prepare is not blocked
        assert str(pane.query_one("#changes-plan").render()) == ""  # no plan was prepared, so none was invalidated


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "live", "typed", "written"),
    [
        ("entity", {"displayName": "EntityA"}, {"#changes-display-name": "  x ; "}, {"displayName": "  x ; "}),
        ("entity", {"displayName": "EntityA"}, {"#changes-display-name": "  "}, {"displayName": "  "}),
        ("entity", {"healthObjective": 99}, {"#changes-objective": " 50 "}, {"healthObjective": 50.0}),
        ("entity", {"displayName": "Event Hubs ", "impact": "Standard"}, {"#changes-impact": "Limited"}, {"impact": "Limited"}),
        ("signal_definition", {"signalKind": "PrometheusMetricsQuery", "queryText": "rate(x[5m])\n"}, {}, {}),
    ],
    ids=["padded", "spaces-only", "padded-number", "live-trailing-space", "live-query-newline"],
)
async def test_update_draft_writes_typed_text_exactly_and_filled_live_text_is_no_edit(
    make_app, healthy_client, kind, live, typed, written,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, kind, "res-1")
    app._draft_store.get_or_create(target, {"name": "res-1", "properties": live})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        assert not app._changes_form_has_pending_edits()
        pane = app.query_one("#changes-pane", ChangesPane)
        for widget_id, value in typed.items():
            pane.query_one(widget_id).value = value
        app._handle_changes_button("changes-review")
        await pilot.pause()
        draft = app._draft_store.get(target)
        assert draft.edited["properties"] == {**live, **written}
        assert draft.revision == len(typed)
        assert not app._changes_form_has_pending_edits()


@pytest.mark.anyio
@pytest.mark.parametrize("typed", [" Limited ", "Bogus"])
async def test_update_draft_rejects_an_unknown_impact_showing_the_typed_value(make_app, healthy_client, typed) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-impact").value = typed
        app._handle_changes_button("changes-review")
        await pilot.pause()
        assert repr(typed) in str(pane.query_one("#changes-summary").render())  # quoted, so padding spaces show
        assert app._draft_store.get(target).revision == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "widget_id", "length", "rejected"),
    [
        ("entity", "#changes-display-name", 256, ""),
        ("entity", "#changes-display-name", 257, "properties.displayName must be at most 256 characters (got 257)."),
        ("entity", "#changes-icon", 257, "properties.icon.iconName must be at most 256 characters (got 257)."),
        ("signal_definition", "#changes-query-text", 3000, ""),
    ],
    ids=["name-at-limit", "name-above-limit", "icon-above-limit", "query-has-no-limit"],
)
async def test_update_draft_rejects_a_name_above_the_length_limit(
    make_app, healthy_client, kind, widget_id, length, rejected,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, kind, "res-1")
    app._draft_store.get_or_create(target, {"name": "res-1", "properties": {"signalKind": "PrometheusMetricsQuery"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one(widget_id).value = "x" * length
        app._handle_changes_button("changes-review")
        await pilot.pause()
        assert (rejected or "Active:") in str(pane.query_one("#changes-summary").render())
        assert app._draft_store.get(target).revision == (0 if rejected else 1)


@pytest.mark.anyio
@pytest.mark.parametrize("edited_during_write", ["edited during write", "EntityA"], ids=["new-value", "old-live-value"])
async def test_apply_keeps_edits_made_while_the_write_runs(make_app, healthy_client, edited_during_write) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA"}}
    app._draft_store.get_or_create(target, baseline)

    class _ApplyStub:
        def apply_confirmed(self, plan, confirmation_name):  # noqa: ANN001
            assert app._applies_in_flight == 1
            app._draft_store.update_field(target, "properties.displayName", edited_during_write)
            app.query_one("#changes-impact").value = "Limited"  # typed, not yet Update draft
            return ApplyResult(
                outcome=ApplyOutcomeKind.APPLIED, target=target, attempted_write=True, message="Applied.",
                race_warning="", changed_fields=(), confirmation_required=target.name, read_back=read_back,
            )

    read_back = {"name": "EntityA", "properties": {"displayName": "applied"}}

    app._apply = _ApplyStub()  # type: ignore[assignment]
    app._prepared_apply_plan = SimpleNamespace(target=target, revision=0, edited_resource=read_back)  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        draft = app._draft_store.get(target)
        assert draft.edited["properties"]["displayName"] == edited_during_write
        assert draft.baseline == read_back  # the next Prepare compares against the applied state
        assert app._draft_store.has_unsaved_changes(target)
        assert app.query_one("#changes-impact").value == "Limited"
        assert app._applies_in_flight == 0


@pytest.mark.anyio
async def test_prepare_asks_for_update_draft_when_the_form_has_edits(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-display-name").value = "typed"
        app._handle_changes_button("changes-prepare-apply")
        await pilot.pause()
        assert "Update draft first" in str(pane.query_one("#changes-summary").render())
        assert not app.workers


@pytest.mark.anyio
async def test_prepare_drops_the_plan_when_the_draft_was_updated_during_the_read(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA"}}
    app._draft_store.get_or_create(target, baseline)

    class _PrepareStub:
        def describe_connection_scope(self, target):  # noqa: ANN001
            return SimpleNamespace(
                arm_endpoint="https://management.azure.com", tenant_id="t", tenant_name="contoso",
                subscription_id="s", subscription_name="contoso-sub",
            )

        def prepare_plan(self, **kwargs):  # noqa: ANN003
            # The user types and presses Update draft while the live read runs.
            app._draft_store.update_field(target, "properties.displayName", "newer")
            app.query_one("#changes-display-name").value = "newer"
            return ApplyPlan(
                target=target,
                revision=kwargs["revision"],
                changes=(FieldDiff(path=("properties", "displayName"), before="EntityA", after="EntityA"),),
                edited_resource=baseline,
                normalized_live_baseline=baseline,
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                shared_consumer_entity_names=(),
            )

    app._apply = _PrepareStub()  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._prepared_apply_plan is None
        assert "Plan dropped" in str(app.query_one("#changes-summary").render())


@pytest.mark.anyio
@pytest.mark.parametrize("setter", ["set_drafts", "set_summary", "set_result", "set_plan", "set_details"])
async def test_changes_text_with_brackets_renders_literally(make_app, healthy_client, setter) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        pane = app.query_one("#changes-pane", ChangesPane)
        getattr(pane, setter)("API [/] [bold]")
        app.query_one("#timeline-pane", TimelinePane).set_summary("1 event in API [/]")
        await pilot.pause()
        assert app.is_running


@pytest.mark.anyio
async def test_nested_model_snapshot_keeps_the_launch_model_tree() -> None:
    client = _TemporalClient()
    app = HealthWatchApp(client, "rg", "root", poll_interval=9999)
    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        nested = client.model_key("rg", "nested")
        start = datetime(2026, 1, 1, tzinfo=UTC)
        app._timeline_key = RangeKey(model=nested, entity_name="nested", start_utc=start, end_utc=start + timedelta(hours=1))
        app.query_one("#timeline-pane", TimelinePane).query_one("#timeline-go-to").value = "2026-01-01T00:30:00Z"
        app._handle_timeline_button("timeline-open")
        for _ in range(100):
            if app._applied_snapshot is not None:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        assert app._applied_snapshot is not None
        assert app.query_one("#health-tree", HealthTree)._occurrence_nodes  # not an empty tree


@pytest.mark.anyio
@pytest.mark.parametrize("selected", ["node", "root"], ids=["row-in-deepest-model", "nested-models-beside-the-row"])
@pytest.mark.parametrize("missing", ["", "nested"], ids=["all-present", "intermediate-missing-at-that-time"])
async def test_nested_snapshot_loads_every_model_with_a_timeline_row(missing, selected) -> None:
    class _CreatedLaterClient(_TemporalClient):
        def list_entities(self, group, model, *, timestamp=None):  # noqa: ANN001
            if model == missing and timestamp is not None:
                raise LookupError(f"{model} did not exist yet")
            return super().list_entities(group, model, timestamp=timestamp)

    client = _CreatedLaterClient()
    app = HealthWatchApp(client, "rg", "root", poll_interval=9999)
    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        nested, grandchild = client.model_key("rg", "nested"), client.model_key("rg", "grandchild")
        for model in (nested, grandchild):
            app._poller.expand(model, {model: 0})
            await app._do_poll()
        view = app._poller.view
        rows = project(view.root, {k: s.previous.forest for k, s in view.entries.items() if s.previous})
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(hours=1)
        app._timeline_keys_by_path = {
            row.path: RangeKey(model=row.model, entity_name=row.entity.name, start_utc=start, end_utc=end) for row in rows
        }
        node_path = next(row.path for row in rows if row.entity.name == "node")
        app.query_one("#health-tree", HealthTree).reveal(node_path, view)  # the nested models are expanded in the tree
        app._timeline_path = next(row.path for row in rows if row.entity.name == selected)
        app._timeline_key = app._timeline_keys_by_path[app._timeline_path]
        app.query_one("#timeline-pane", TimelinePane).query_one("#timeline-go-to").value = "2026-01-01T00:30:00Z"
        app._handle_timeline_button("timeline-open")
        for _ in range(100):
            if app._applied_snapshot is not None:
                break
            await pilot.pause(0.02)
        await pilot.pause()
        # A model created later degrades to an unloaded subtree; only the launch model failing is fatal.
        assert app._applied_snapshot is not None
        assert "Showing snapshot at" in str(app.query_one("#timeline-summary").render())
        assert isinstance(app._poller.view_at(app._applied_snapshot.at_utc).entries.get(nested), Ready) is (not missing)
        assert app.query_one("#health-tree", HealthTree).contains(node_path) is (not missing)


@pytest.mark.anyio
async def test_compare_ignores_models_cached_at_only_one_of_the_times() -> None:
    client = _TemporalClient()
    app = HealthWatchApp(client, "rg", "root", poll_interval=9999)
    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        left = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
        app._poller.open_at(left, target=client.model_key("rg", "nested"), purpose="test")
        await app._do_poll()
        pane = app.query_one("#timeline-pane", TimelinePane)
        pane.query_one("#timeline-compare-a").value = "2026-01-01T00:10:00Z"
        pane.query_one("#timeline-compare-b").value = "2026-01-01T00:40:00Z"
        app._handle_timeline_button("timeline-compare")
        for _ in range(100):
            if "Compare complete" in str(pane.query_one("#timeline-summary").render()):
                break
            await pilot.pause(0.02)
        assert "No differences." in str(pane.query_one("#timeline-events").render())


@pytest.mark.parametrize(
    ("parents", "expected"),
    [({"a": "a"}, "A"), ({"a": "b", "b": "a"}, "B / A"), ({"a": "b", "b": None}, "B / A")],
)
def test_compare_path_label_stops_at_a_parent_cycle(parents, expected) -> None:
    nodes = {
        name: SnapshotNode(name, name.upper(), HistoryBucketHealth.HEALTHY, None, parent_canonical_name=parent)
        for name, parent in parents.items()
    }
    assert node_path_label("a", nodes) == expected


@pytest.mark.anyio
async def test_quit_waits_for_a_running_apply(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._applies_in_flight = 1
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running
        app._applies_in_flight = 0
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.anyio
@pytest.mark.parametrize("button", ["changes-prepare-apply", "changes-apply", "changes-discard", "changes-restore"])
async def test_changes_actions_that_replace_the_draft_wait_for_a_running_apply(make_app, healthy_client, button) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    release = threading.Event()
    writes = []

    class _SlowApply:
        def apply_confirmed(self, plan, confirmation_name):  # noqa: ANN001
            writes.append(plan)
            release.wait(5)
            return ApplyResult(
                outcome=ApplyOutcomeKind.APPLIED, target=target, attempted_write=True, message="Applied.",
                race_warning="", changed_fields=(), confirmation_required=target.name,
            )

    app._apply = _SlowApply()  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._prepared_apply_plan = SimpleNamespace(target=target, revision=0)  # type: ignore[assignment]
        app.query_one("#changes-apply", Button).press()
        await pilot.pause()
        app._prepared_apply_plan = SimpleNamespace(target=target, revision=0)  # type: ignore[assignment]
        app.query_one(f"#{button}", Button).press()
        await pilot.pause()
        assert "An apply is still writing; wait for its result." in str(app.query_one("#changes-summary").render())
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(writes) == 1
        assert app._draft_store.get(target) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("typed", ["Renamed", ""], ids=["update-draft", "re-render"])
async def test_changes_summary_shows_a_running_apply_until_its_result(make_app, healthy_client, typed) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    release = threading.Event()

    class _SlowApply:
        def apply_confirmed(self, plan, confirmation_name):  # noqa: ANN001
            release.wait(5)
            return ApplyResult(
                outcome=ApplyOutcomeKind.APPLIED, target=target, attempted_write=True, message="Applied.",
                race_warning="", changed_fields=(), confirmation_required=target.name,
            )

    app._apply = _SlowApply()  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._prepared_apply_plan = SimpleNamespace(target=target, revision=0)  # type: ignore[assignment]
        app._handle_changes_button("changes-apply")
        await pilot.pause()
        if typed:
            app.query_one("#changes-display-name").value = typed
            app._handle_changes_button("changes-review")
        else:
            app._render_changes()
        await pilot.pause()
        assert "An apply is still writing; wait for its result." in str(app.query_one("#changes-summary").render())
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "still writing" not in str(app.query_one("#changes-summary").render())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("write_error", "shown"),
    [
        (AuthenticationError("forbidden", status_code=403), ("Apply rejected:", "HTTP 403")),
        (TimeoutError("timed out"), ("Apply unknown:", "TimeoutError: timed out", "Prepare again")),
        (None, ("Apply differs:", "Prepare again")),
    ],
    ids=["rejected", "unknown", "differs"],
)
async def test_apply_outcome_with_read_back_stays_shown_and_the_next_prepare_diffs_against_it(
    make_app, healthy_client, write_error, shown,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "before"}})
    app._draft_store.update_field(target, "properties.displayName", "after")
    live = {"name": "EntityA", "properties": {"displayName": "before"}}

    def _write(*_args):  # noqa: ANN002
        live["properties"] = {"displayName": "elsewhere"}  # the live state moved on, whether or not this write landed
        if write_error is not None:
            raise write_error

    healthy_client.for_model.return_value = healthy_client
    healthy_client.get_connection_scope.return_value = {
        "arm_endpoint": "https://management.azure.com", "subscription_id": "s", "subscription_name": "contoso-sub",
        "tenant_id": "t", "tenant_name": "contoso",
    }
    healthy_client.get_entity.side_effect = lambda *_args: json.loads(json.dumps(live))
    healthy_client.update_entity.side_effect = _write
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        pane.query_one("#changes-apply-confirm").value = target.name
        app._handle_changes_button("changes-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        result = str(pane.query_one("#changes-result").render())
        assert all(text in result for text in shown)
        assert "Not confirmed: properties.displayName: wanted 'after', live 'elsewhere'" in result
        assert pane.query_one("#changes-apply-confirm").value == ""  # another Apply needs the name typed again
        assert app._draft_store.get(target).edited["properties"] == {"displayName": "after"}

        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Plan prepared." in str(pane.query_one("#changes-summary").render())
        assert "Apply" not in str(pane.query_one("#changes-result").render())
        assert healthy_client.update_entity.call_count == 1


@pytest.mark.anyio
async def test_apply_result_persists_after_render_refresh(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}}
    app._draft_store.get_or_create(target, baseline)

    class _ApplyStub:
        def apply_confirmed(self, plan, confirmation_name):  # noqa: ANN001
            return ApplyResult(
                outcome=ApplyOutcomeKind.APPLIED,
                target=target,
                attempted_write=True,
                message="Applied and verified.",
                race_warning="Race warning text.",
                changed_fields=("properties.displayName",),
                confirmation_required=target.name,
                read_back=baseline,
            )

    app._apply = _ApplyStub()  # type: ignore[assignment]
    app._prepared_apply_plan = SimpleNamespace(target=target, revision=0)  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-apply-confirm").value = target.name
        app._handle_changes_button("changes-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Applied and verified." in str(pane.query_one("#changes-result").render())
        app._render_changes()
        await pilot.pause()
        assert "Applied and verified." in str(pane.query_one("#changes-result").render())


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_filled_changes_route_keeps_prepare_apply_clickable(make_app, healthy_client, size) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    app._draft_store.get_or_create(
        target,
        {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard", "healthObjective": 99}},
    )
    long_path = "/tmp/" + ("very-long-segment/" * 12) + "draft.json"
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        pane = app.query_one("#changes-pane", ChangesPane)
        pane.query_one("#changes-save-path").value = long_path
        pane.query_one("#changes-restore-path").value = long_path
        pane.query_one("#changes-restore-confirm").value = target.name
        await pilot.pause()
        app.query_one("#changes-prepare-apply").scroll_visible()
        await pilot.pause()
        await pilot.click("#changes-prepare-apply")
        await pilot.pause()
        app.query_one("#changes-apply").scroll_visible()
        await pilot.pause()
        await pilot.click("#changes-apply")
        await pilot.pause()
        assert app.query_one("#changes-prepare-apply").display is True


@pytest.mark.anyio
async def test_compare_unavailable_side_preserves_previous_output(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_timeline()
        await pilot.pause()
        pane = app.query_one("#timeline-pane", TimelinePane)
        pane.set_events("previous compare output")
        model = app._poller.root
        failed_view = SimpleNamespace(entries={model: Failed()})
        app._poller.view_at = lambda at: failed_view  # type: ignore[assignment]
        left_at = datetime.now(UTC)
        right_at = datetime.now(UTC)
        app._complete_compare_snapshots(model, left_at, right_at)
        await pilot.pause()
        assert "Compare unavailable" in str(pane.query_one("#timeline-summary").render())
        assert "previous compare output" in str(pane.query_one("#timeline-events").render())


@pytest.mark.anyio
async def test_prepare_apply_blocked_when_scope_description_unavailable(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}}
    app._draft_store.get_or_create(target, baseline)
    app._apply.describe_connection_scope = lambda target: (_ for _ in ()).throw(ValueError("scope unavailable"))  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        pane = app.query_one("#changes-pane", ChangesPane)
        assert "Prepare blocked" in str(pane.query_one("#changes-summary").render())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("consumer", "shown"),
    [("ConsumerA", "ConsumerA"), ("21eb9ab7-3f38-55a4-bf16-38367018071f", "Failures")],
    ids=["unknown-entity-shows-its-name", "loaded-entity-shows-its-display-name"],
)
async def test_prepare_plan_includes_scope_and_target_review_details(make_app, healthy_client, consumer, shown) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}}
    app._draft_store.get_or_create(target, baseline)
    class _PrepareStub:
        def describe_connection_scope(self, target):  # noqa: ANN001
            return SimpleNamespace(
                arm_endpoint="https://management.azure.com",
                tenant_id="tenant-123",
                tenant_name="contoso",
                subscription_id="sub-456",
                subscription_name="contoso-sub",
            )

        def prepare_plan(self, **kwargs):  # noqa: ANN003
            return ApplyPlan(
                target=target,
                revision=3,
                changes=(FieldDiff(path=("properties", "displayName"), before="EntityA", after="EntityA-edited"),),
                edited_resource=baseline,
                normalized_live_baseline=baseline,
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                shared_consumer_entity_names=(consumer,),
            )

    app._apply = _PrepareStub()  # type: ignore[assignment]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        plan = str(app.query_one("#changes-plan").render())
        details = str(app.query_one("#changes-details").render())
        assert "Tenant: contoso (tenant-123)" in plan
        assert "Subscription: contoso-sub (sub-456)" in plan
        assert "ARM endpoint: https://management.azure.com" in plan
        assert f"Model: {model.group}/{model.name}" in plan
        assert "Prepared target: entity:EntityA" in plan
        assert f"Shared consumers: {shown}" in plan
        assert "Changed fields:" in details
        assert " - properties.displayName" in details
        assert "Race warning:" in details


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_prepare_review_scroll_and_actions_are_reachable_in_compact_layouts(make_app, healthy_client, size) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    target = ResourceTarget(model, "entity", "EntityA")
    baseline = {"name": "EntityA", "properties": {"displayName": "EntityA", "impact": "Standard"}}
    app._draft_store.get_or_create(target, baseline)

    class _PrepareStub:
        def describe_connection_scope(self, target):  # noqa: ANN001
            return SimpleNamespace(
                arm_endpoint="https://management.azure.com",
                tenant_id="tenant-123",
                tenant_name="contoso",
                subscription_id="sub-456",
                subscription_name="contoso-sub",
            )

        def prepare_plan(self, **kwargs):  # noqa: ANN003
            return ApplyPlan(
                target=target,
                revision=3,
                changes=tuple(
                    FieldDiff(path=("properties", f"displayName{i}"), before=f"v{i}", after=f"w{i}")
                    for i in range(16)
                ),
                edited_resource=baseline,
                normalized_live_baseline=baseline,
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                shared_consumer_entity_names=("ConsumerA",),
            )

    app._apply = _PrepareStub()  # type: ignore[assignment]
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        app._handle_changes_button("changes-prepare-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        width, height = size
        assert _fully_visible(app.query_one("#changes-review-scroll"), width, height)
        assert _fully_visible(app.query_one("#changes-apply-confirm"), width, height)
        assert _fully_visible(app.query_one("#changes-apply"), width, height)
        review = app.query_one("#changes-review-scroll")
        assert review.size.height == (4 if height < 30 else height * 3 // 10)  # grows with the screen above the short layout
        review.scroll_end(animate=False)
        await pilot.pause()
        assert review.scroll_y > 0
        assert "Tenant: contoso (tenant-123)" in str(app.query_one("#changes-plan").render())
        assert await pilot.click("#changes-apply-confirm")
        await pilot.pause()
        assert await pilot.click("#changes-apply")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert str(app.query_one("#changes-plan").render()) == ""  # a started apply uses the plan up
        assert "Race warning:" in str(app.query_one("#changes-details").render())


@pytest.mark.anyio
@pytest.mark.parametrize("height", [24, 31, 32, 50])
@pytest.mark.parametrize("other_drafts", [0, 8])
async def test_a_long_result_message_leaves_a_form_field_visible_at_80_columns(make_app, healthy_client, height, other_drafts) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    for index in range(other_drafts):
        app._draft_store.get_or_create(ResourceTarget(app._poller.root, "entity", f"Other{index}"), {"name": f"Other{index}", "properties": {}})
    target = ResourceTarget(app._poller.root, "entity", "EntityA")
    app._draft_store.get_or_create(target, {"name": "EntityA", "properties": {"displayName": "EntityA"}})
    app._changes_result_by_target[target] = (
        "Apply rejected: Service rejected write and read-back differs from desired state.\n"
        "Write error: " + AuthenticationError(
            "The client 'qa@fake' does not have authorization to perform action "
            "'Microsoft.CloudHealth/healthmodels/entities/write'.",
            status_code=403, code="AuthorizationFailed",
        ).diagnostic_text() + "\nNot confirmed: properties.displayName"
    )
    async with app.run_test(size=(80, height)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_changes()
        await pilot.pause()
        assert "Write error:" in str(app.query_one("#changes-result").render())
        field = app.query_one("#changes-display-name")
        assert app.screen.get_widget_at(field.region.x, field.region.y)[0] is field
        result_scroll = app.query_one("#changes-result-scroll")
        assert result_scroll.can_focus
        if height == 24:
            assert result_scroll.max_scroll_y > 0  # the cut-off rest of the error stays reachable


@pytest.mark.anyio
async def test_timeline_row_leases_released_when_request_superseded(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    released_rows: list[RangeKey] = []
    released_at: list[tuple[datetime, str]] = []
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_timeline()
        await pilot.pause()
        first_key, first_rows = app._timeline_key, dict(app._timeline_keys_by_path)
        assert first_key is not None and first_rows
        app._poller.release_range = lambda key, purpose="default": released_rows.append(key)  # type: ignore[assignment]
        app._poller.release_at = lambda at, target=None, purpose="default": released_at.append((at, purpose))  # type: ignore[assignment]
        app._request_timeline(hours=6)
        await pilot.pause()

    assert set(released_rows) >= set(first_rows.values())
    assert (first_key.start_utc, "timeline-baseline") in released_at


@pytest.mark.anyio
async def test_timeline_fills_rows_progressively_and_never_times_out(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    start = datetime(2026, 1, 1, tzinfo=UTC)
    key = RangeKey(model=model, entity_name="EntityA", start_utc=start, end_utc=start + timedelta(hours=1))
    child = RangeKey(model=model, entity_name="Child", start_utc=start, end_utc=start + timedelta(hours=1))
    rows = {("EntityA",): key, ("EntityA", "Child"): child}
    ready = SimpleNamespace(status="ready", error="", events=parse_history_events([
        {"occurredAt": "2026-01-01T00:10:00Z", "newState": "Healthy", "reason": "first"},
    ]))
    views = {key: ready, child: SimpleNamespace(status="loading", error="", events=None)}
    app._poller.range_view = lambda range_key: views[range_key]  # type: ignore[assignment]
    app._poller.view_at = lambda at: SimpleNamespace(entries={model: Failed()})  # type: ignore[assignment]
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {"path": ("EntityA",), "name": "EntityA", "depth": 0, "model": model},
        {"path": ("EntityA", "Child"), "name": "Child", "depth": 1, "model": model},
    ]

    def screen(widget_id: str) -> str:
        return str(app.query_one("#timeline-pane", TimelinePane).query_one(widget_id).render())

    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._set_workspace_tab("timeline")
        app._timeline_key, app._timeline_path, app._timeline_keys_by_path = key, ("EntityA",), rows
        app._timeline_row_status = {path: "queued" for path in rows}
        app._refresh_timeline()
        await pilot.pause()
        assert "1 event in EntityA" in screen("#timeline-summary")
        assert "1/2 ready" in screen("#timeline-progress")
        child_band = next(line for line in screen("#timeline-bands").splitlines() if "Child" in line)
        assert "━" in child_band

        app._pulse_timeline()
        await pilot.pause()
        moved = next(line for line in screen("#timeline-bands").splitlines() if "Child" in line)
        assert moved != child_band  # the shimmer advances while data is on the way

        views[child] = ready
        app._refresh_timeline()
        await pilot.pause()
        assert screen("#timeline-progress").strip() == ""
        assert "[loading]" not in screen("#timeline-bands")
        assert "Timed out" not in screen("#timeline-summary")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("button", "inputs", "expected"),
    [
        ("timeline-open", {"#timeline-go-to": "2026-01-01T00:30:00Z"}, "Showing snapshot at"),
        ("timeline-open", {"#timeline-go-to": "2999-01-01T00:00:00Z"}, "is in the future"),
        ("timeline-compare", {"#timeline-compare-a": "2026-01-01T00:10:00Z", "#timeline-compare-b": "2026-01-01T00:40:00Z"},
         "Compare complete"),
        ("timeline-compare", {"#timeline-compare-a": "2026-01-01T00:10:00Z", "#timeline-compare-b": "2999-01-01T00:00:00Z"},
         "is in the future"),
    ],
)
async def test_snapshot_open_and_compare_complete_from_poller_updates(button, inputs, expected) -> None:
    app = HealthWatchApp(_TemporalClient(), "rg", "root", poll_interval=9999)
    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        pane = app.query_one("#timeline-pane", TimelinePane)
        for widget_id, value in inputs.items():
            pane.query_one(widget_id).value = value
        app._handle_timeline_button(button)
        for _ in range(100):
            if expected in str(pane.query_one("#timeline-summary").render()):
                break
            await pilot.pause(0.02)
        assert expected in str(pane.query_one("#timeline-summary").render())
        assert str(pane.query_one("#timeline-progress").render()).strip() == ""


@pytest.mark.anyio
async def test_open_snapshot_drives_tree_and_status_until_return_to_live() -> None:
    app = HealthWatchApp(_TemporalClient(), "rg", "root", poll_interval=9999)
    async with app.run_test(size=(160, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        tree = app.query_one("#health-tree", HealthTree)
        shown: list[str] = []
        apply_view = tree.apply_view
        tree.apply_view = lambda view: (shown.append(view.entries[view.root].previous.snapshot.timestamp), apply_view(view))  # type: ignore[method-assign]
        app.query_one("#timeline-pane", TimelinePane).query_one("#timeline-go-to").value = "2026-01-01T00:30:00Z"
        app._handle_timeline_button("timeline-open")
        for _ in range(100):
            if app._applied_snapshot is not None:
                break
            await pilot.pause(0.02)
        app._render_monitor()  # a later poll must not swap the tree back to live data
        assert shown[-1] == "2026-01-01T00:30:00Z"
        assert "Snapshot 2026-01-01 00:30:00Z" in str(app.query_one("#status-bar").render())
        app._handle_timeline_button("timeline-live")
        await pilot.pause()
        assert shown[-1] != "2026-01-01T00:30:00Z"
        assert "Snapshot" not in str(app.query_one("#status-bar").render())


@pytest.mark.anyio
async def test_revisiting_timeline_tab_keeps_the_loaded_range(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.action_show_timeline()
        first = app._timeline_key
        app.action_show_changes()
        app.action_show_timeline()
        assert first is not None and app._timeline_key is first


@pytest.mark.anyio
async def test_timeline_selected_zero_bucket_row_shows_error_marker(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    key = RangeKey(
        model=model,
        entity_name="EntityA",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=1),
    )
    app._timeline_path = ("EntityA",)
    app._timeline_buckets_by_path = {("EntityA",): ()}
    app._timeline_row_status = {("EntityA",): "ready"}
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {"path": ("EntityA",), "name": "EntityA", "depth": 0, "label": "EntityA"}
    ]
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        output = app._render_timeline_bands(key)
        pane = app.query_one("#timeline-pane", TimelinePane)
        pane.set_bands(output)
        await pilot.pause()
        rendered = str(pane.query_one("#timeline-bands").render())
    assert "EntityA" in output
    assert "[no-buckets]" in output
    assert "[no-buckets]" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("width", [115, 130])
async def test_failed_timeline_rows_stay_on_one_line_in_the_docked_pane(make_app, healthy_client, width) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    key = RangeKey(model=app._poller.root, entity_name="EntityA", start_utc=start, end_utc=start + timedelta(hours=1))
    app._timeline_row_status = {("EntityA",): "failed"}
    app._visible_occurrence_rows = lambda: [{"path": ("EntityA",), "name": "EntityA", "depth": 0}]  # type: ignore[assignment]
    async with app.run_test(size=(width, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._set_workspace_tab("timeline")
        bands = app._render_timeline_bands(key)
        assert "[failed]" in bands.plain
        pane = app.query_one("#timeline-pane", TimelinePane)
        pane.set_bands(bands)
        await pilot.pause()
        assert pane.query_one("#timeline-bands").region.height == len(bands.plain.split("\n"))


@pytest.mark.anyio
async def test_actions_modal_is_compact_centered_overlay_at_small_and_large_sizes(make_app, healthy_client) -> None:
    for size in ((80, 24), (120, 40)):
        app: HealthWatchApp = make_app(healthy_client)
        async with app.run_test(size=size) as pilot:
            await app._do_poll()
            await pilot.pause()
            await pilot.press("f2")
            await pilot.pause()
            actions_root = app.screen.query_one("#actions-dialog")
            width, height = size
            assert 0 < actions_root.region.x < width - 1
            assert 0 < actions_root.region.y < height - 1
            assert actions_root.region.width < width
            assert actions_root.region.height < height
            svg = app.export_screenshot()
            rendered = "".join(ET.fromstring(svg).itertext()).replace("\xa0", " ")
            assert "Auto-jump: on" in rendered
            assert "Actions" in rendered
            await pilot.press("escape")
            await pilot.pause()


@pytest.mark.anyio
async def test_timeline_bands_use_colored_text_and_display_name_from_snapshot(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    key = RangeKey(
        model=model,
        entity_name="entity-guid-aaaaaaaa",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=6),
    )
    row_path = ("entity-guid-aaaaaaaa",)
    app._timeline_path = row_path
    app._timeline_buckets_by_path = {
        row_path: (
            SimpleNamespace(health=HistoryBucketHealth.HEALTHY),
            SimpleNamespace(health=HistoryBucketHealth.DEGRADED),
            SimpleNamespace(health=HistoryBucketHealth.UNHEALTHY),
            SimpleNamespace(health=HistoryBucketHealth.UNKNOWN),
            SimpleNamespace(health=HistoryBucketHealth.NO_COVERAGE),
            SimpleNamespace(health=HistoryBucketHealth.MIXED),
        )
    }
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {
            "path": row_path,
            "name": "entity-guid-aaaaaaaa",
            "depth": 0,
            "model": model,
            "label": "entity-guid-aaaaaaaa",
        }
    ]
    app._poller.view_at = lambda at: SimpleNamespace(  # type: ignore[assignment]
        entries={
            model: SimpleNamespace(
                previous=SimpleNamespace(
                    forest=SimpleNamespace(
                        entities={
                            "entity-guid-aaaaaaaa": SimpleNamespace(display_name="Entity Display ✅")
                        }
                    )
                )
            )
        }
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._applied_snapshot = SnapshotKey(model, key.start_utc)  # after the poll: the fake view only serves labels
        rendered = app._render_timeline_bands(key)
        assert isinstance(rendered, Text)
        assert "Entity Display ✅" in rendered.plain
        assert "entity-guid-aaaaaaaa" not in rendered.plain
        assert len(rendered.spans) > 0


@pytest.mark.anyio
async def test_request_timeline_uses_per_row_model_keys(make_app, healthy_client) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    root_model = app._poller.root
    nested_model = ModelKey(
        endpoint=root_model.endpoint,
        subscription=root_model.subscription,
        group=root_model.group,
        name="nested",
    )
    path_root = ("root-entity",)
    path_nested = ("nested-entity",)
    app._selected_context = lambda: (root_model, "root-entity", None, path_root)  # type: ignore[assignment]
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {"path": path_root, "name": "root-entity", "depth": 0, "label": "Root", "model": root_model},
        {"path": path_nested, "name": "nested-entity", "depth": 1, "label": "Nested", "model": nested_model},
    ]
    requested: dict[tuple[str, ...], ModelKey] = {}
    app._poller.request_range = lambda key, purpose="default": requested.__setitem__(  # type: ignore[assignment]
        path_root if key.entity_name == "root-entity" else path_nested,
        key.model,
    )
    app._poller.open_at = lambda *args, **kwargs: None  # type: ignore[assignment]
    app._poller.release_at = lambda *args, **kwargs: None  # type: ignore[assignment]
    app.run_worker = lambda coro, *args, **kwargs: coro.close()  # type: ignore[assignment]

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._request_timeline(hours=1)

    assert requested[path_root] == root_model
    assert requested[path_nested] == nested_model


@pytest.mark.parametrize("label", ["界" * 30, "🙂" * 30])
def test_fit_cell_width_keeps_exact_terminal_width_for_wide_labels(make_app, healthy_client, label: str) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    fitted = app._fit_cell_width(label, 32)
    assert cell_len(fitted) == 32


@pytest.mark.parametrize(
    "display_names",
    [
        ("界" * 30, "short"),
        ("🙂" * 30, "name"),
    ],
)
@pytest.mark.anyio
async def test_timeline_band_column_alignment_uses_cell_width(make_app, healthy_client, display_names) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    model = app._poller.root
    key = RangeKey(
        model=model,
        entity_name="entity-one",
        start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        end_utc=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=6),
    )
    row_one = ("entity-one",)
    row_two = ("entity-two",)
    app._timeline_path = row_one
    app._timeline_buckets_by_path = {
        row_one: (SimpleNamespace(health=HistoryBucketHealth.UNKNOWN),),
        row_two: (SimpleNamespace(health=HistoryBucketHealth.UNKNOWN),),
    }
    app._visible_occurrence_rows = lambda: [  # type: ignore[assignment]
        {"path": row_one, "name": "entity-one", "depth": 0, "model": model, "label": "entity-one"},
        {"path": row_two, "name": "entity-two", "depth": 0, "model": model, "label": "entity-two"},
    ]
    app._poller.view_at = lambda at: SimpleNamespace(  # type: ignore[assignment]
        entries={
            model: SimpleNamespace(
                previous=SimpleNamespace(
                    forest=SimpleNamespace(
                        entities={
                            "entity-one": SimpleNamespace(display_name=display_names[0]),
                            "entity-two": SimpleNamespace(display_name=display_names[1]),
                        }
                    )
                )
            )
        }
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()
        app._applied_snapshot = SnapshotKey(model, key.start_utc)  # after the poll: the fake view only serves labels
        rendered = app._render_timeline_bands(key).plain.splitlines()
        row_lines = rendered[-2:]
        assert len(row_lines) == 2
        for line in row_lines:
            band_start = line.index(BAND_STATES[HistoryBucketHealth.UNKNOWN][0])
            assert cell_len(line[:band_start]) == watch_app_module._TIMELINE_LABEL_WIDTH + 1


def test_timeline_label_uses_applied_nested_snapshot_and_normalizes_control_whitespace() -> None:
    client = _TemporalClient()
    client._models[("rg", "nested")][0]["properties"]["displayName"] = "Nested\tDisplay\nName"
    app = HealthWatchApp(client, "rg", "root", poll_interval=30)
    try:
        nested_model = client.model_key("rg", "nested")
        applied_at = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
        app._poller.open_at(applied_at, target=nested_model, purpose="test")
        app._poller.wait()
        app._applied_snapshot = SnapshotKey(nested_model, applied_at)
        row = {"name": "nested", "depth": 0, "model": nested_model}
        label = app._timeline_label(row, 32)
        assert "Nested Display Name" in label
        assert "\n" not in label and "\t" not in label
        assert cell_len(label) == 32
    finally:
        app._poller.close()


def test_timeline_label_uses_live_nested_metadata_without_viewat_patch() -> None:
    client = _TemporalClient()
    client._models[("rg", "nested")][0]["properties"]["displayName"] = "Nested Live Name"
    app = HealthWatchApp(client, "rg", "root", poll_interval=30)
    try:
        nested_model = client.model_key("rg", "nested")
        app._poller.poll_once()
        app._poller.expand(nested_model, {nested_model: 0})
        app._poller.wait()
        label = app._timeline_label({"name": "nested", "depth": 0, "model": nested_model}, 32)
        assert "Nested Live Name" in label
    finally:
        app._poller.close()


def test_timeline_label_prefers_applied_historical_name_over_later_live_rename() -> None:
    client = _TemporalClient()
    client._models[("rg", "nested")][0]["properties"]["displayName"] = "Historical Name"
    app = HealthWatchApp(client, "rg", "root", poll_interval=30)
    try:
        nested_model = client.model_key("rg", "nested")
        historical_at = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
        app._poller.open_at(historical_at, target=nested_model, purpose="test")
        app._poller.wait()
        app._applied_snapshot = SnapshotKey(nested_model, historical_at)

        client._models[("rg", "nested")][0]["properties"]["displayName"] = "Live Renamed"
        app._poller.poll_once()
        app._poller.navigate(app._poller.root, {nested_model: 0})
        app._poller.wait()

        label = app._timeline_label({"name": "nested", "depth": 0, "model": nested_model}, 32)
        assert "Historical Name" in label
        assert "Live Renamed" not in label
    finally:
        app._poller.close()


def test_timeline_label_historical_missing_metadata_uses_canonical_not_live_name() -> None:
    client = _TemporalClient()
    client._models[("rg", "nested")][0]["properties"]["displayName"] = ""
    app = HealthWatchApp(client, "rg", "root", poll_interval=30)
    try:
        nested_model = client.model_key("rg", "nested")
        historical_at = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
        app._poller.open_at(historical_at, target=nested_model, purpose="test")
        app._poller.wait()
        app._applied_snapshot = SnapshotKey(nested_model, historical_at)

        client._models[("rg", "nested")][0]["properties"]["displayName"] = "Live Name"
        app._poller.poll_once()
        app._poller.navigate(app._poller.root, {nested_model: 0})
        app._poller.wait()

        label = app._timeline_label({"name": "nested", "depth": 0, "model": nested_model}, 32)
        assert label.lstrip().startswith("nested")
        assert "Live Name" not in label
    finally:
        app._poller.close()


def test_timeline_label_ignores_requested_snapshot_until_applied() -> None:
    client = _TemporalClient()
    client._models[("rg", "nested")][0]["properties"]["displayName"] = "Live Name"
    app = HealthWatchApp(client, "rg", "root", poll_interval=30)
    try:
        nested_model = client.model_key("rg", "nested")
        app._poller.poll_once()
        app._poller.expand(nested_model, {nested_model: 0})
        app._poller.wait()

        client._models[("rg", "nested")][0]["properties"]["displayName"] = "Requested Name"
        requested_at = datetime(2026, 1, 1, 0, 50, tzinfo=UTC)
        app._poller.open_at(requested_at, target=nested_model, purpose="test")
        app._poller.wait()
        app._requested_snapshot = SnapshotKey(nested_model, requested_at)

        label = app._timeline_label({"name": "nested", "depth": 0, "model": nested_model}, 32)
        assert "Live Name" in label
        assert "Requested Name" not in label
    finally:
        app._poller.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("size", "minimum_visible_actions"), [((80, 24), 4), ((120, 40), 6)])
async def test_actions_overlay_shows_compact_content_not_two_visible_rows(
    make_app, healthy_client, size, minimum_visible_actions,
) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        dialog = app.screen.query_one("#actions-dialog")
        visible_action_rows = 0
        for button in app.screen.query("Button"):
            if not button.id or not button.id.startswith("action-"):
                continue
            if button.region.height <= 0:
                continue
            if button.region.y < dialog.region.y or (button.region.y + button.region.height) > (dialog.region.y + dialog.region.height):
                continue
            visible_action_rows += 1
        assert visible_action_rows >= minimum_visible_actions


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_actions_overlay_paints_labels_and_back_row_with_real_content_regions(make_app, healthy_client, size) -> None:
    app: HealthWatchApp = make_app(healthy_client)
    fired: list[str | None] = []
    async with app.run_test(size=size) as pilot:
        await app._do_poll()
        await pilot.pause()
        app.push_screen(ActionsDialog(include_back_to_models=True), lambda value: fired.append(value))
        await pilot.pause()

        for button in app.screen.query("Button"):
            if button.id and button.id.startswith("action-") and button.display:
                assert button.content_region.height >= 1

        initial_painted = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()  # type: ignore[attr-defined]
        )
        assert "Search (/)" in initial_painted

        await pilot.press(*(["down"] * 8))
        await pilot.pause()
        scrolled_painted = "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()  # type: ignore[attr-defined]
        )
        assert "Quit (q / Ctrl+Q)" in scrolled_painted
        assert "Back to models" in scrolled_painted

        await pilot.click("#actions-filter")
        await pilot.press("b", "a", "c", "k")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fired[-1] == "back_to_models"
