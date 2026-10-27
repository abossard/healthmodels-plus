"""Widget lifecycle E2E tests — drive SignalPanel, EntityDrawer, QueryEditor
through their full state machines via the app.

Each test uses the conftest fixtures (`make_app`, `healthy_client`,
forest fixtures, signal-pair fixtures, execute outcomes). Pure rendering
internals are out of scope; we assert observable widget state only:
``is_visible``, ``is_busy``, ``context``, ``current_entity_name``, plus
the public status / result Static text.
"""
from __future__ import annotations

from typing import Any

import pytest
from textual.widgets import Static, TextArea
from textual.widgets._tree import TreeNode

from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
from azext_healthmodels_plus.tui.watch.entity_drawer import EntityDrawer
from azext_healthmodels_plus.tui.watch.health_tree import EntityData, HealthTree
from azext_healthmodels_plus.tui.watch.query_editor import QueryEditor
from azext_healthmodels_plus.tui.watch.signal_panel import SignalPanel


# ── local helpers ─────────────────────────────────────────────────────


def _walk(node: TreeNode[EntityData]):
    """Depth-first iterator over a Tree starting from *node*."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _first_signal_node(tree: HealthTree) -> TreeNode[EntityData]:
    for n in _walk(tree.root):
        if n.data is not None and n.data.is_signal:
            return n
    raise AssertionError("no signal node in tree")


def _first_entity_node(tree: HealthTree) -> TreeNode[EntityData]:
    for n in _walk(tree.root):
        if (
            n.data is not None
            and not n.data.is_signal
            and not n.data.entity_name.startswith("__")
        ):
            return n
    raise AssertionError("no entity node in tree")


def _all_entity_nodes(tree: HealthTree) -> list[TreeNode[EntityData]]:
    return [
        n for n in _walk(tree.root)
        if n.data is not None
        and not n.data.is_signal
        and not n.data.entity_name.startswith("__")
    ]


def _static_text(widget: Static) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def _focus_node(tree: HealthTree, node: TreeNode[EntityData]) -> None:
    """Move the tree cursor to *node* so ``cursor_node`` reflects it."""
    # Ensure all parent nodes are expanded so the line cache contains *node*.
    parent = node.parent
    while parent is not None:
        if not parent._expanded:
            parent.expand()
        parent = parent.parent
    tree._build()  # type: ignore[attr-defined]
    tree.move_cursor(node)
    tree.select_node(node)


def _install_forest(app: HealthWatchApp, forest, *, dispatch_changes=()) -> None:
    from .conftest import install_wire_forest
    install_wire_forest(app._client, forest)


# ── 1. Signal panel lifecycle ─────────────────────────────────────────


SIGNAL_PANEL_OUTCOMES = [
    ("execute_success", "Verified"),
    ("execute_degraded", "Verified"),
    ("execute_error_payload", "✖"),
    ("execute_exception_throttled", "Throttled"),
    ("execute_exception_auth", "Authentication"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "outcome_fixture, expect_status_substring",
    SIGNAL_PANEL_OUTCOMES,
)
async def test_signal_panel_lifecycle(
    monkeypatch,
    make_app,
    healthy_client,
    promql_signal_pair,
    outcome_fixture,
    expect_status_substring,
    request,
):
    """Walk: hidden → set_signal → busy → result/error → close → reopen."""
    outcome = request.getfixturevalue(outcome_fixture)

    def fake_execute(client, rg, model, entity_name, signal_name):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(
        "azext_healthmodels_plus.tui.watch.app.execute_signal", fake_execute,
    )
    # History is best-effort; stub it so we don't need a real client method.
    monkeypatch.setattr(
        HealthWatchApp,
        "_fetch_signal_history",
        lambda self, e, s, key=None: {"history": []},
    )

    app = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        panel = app.query_one("#signal-panel", SignalPanel)
        tree = app.query_one("#health-tree", HealthTree)

        # 1. hidden initially
        assert panel.display is False
        assert panel.context is None

        # 2. set_signal via 'v' key on a signal node
        sig_node = _first_signal_node(tree)
        _focus_node(tree, sig_node)
        tree.focus()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()

        assert panel.display is True
        assert panel.context is not None
        first_ctx = panel.context

        # 3. wait for worker to complete (busy → result/error)
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert not panel.is_busy
        status_text = _static_text(panel.query_one("#signal-panel-status", Static))
        assert expect_status_substring in status_text

        # 4. close with escape
        await pilot.press("escape")
        await pilot.pause()
        assert panel.display is False

        # 5. reopen on a (potentially) different signal — find another one
        signals = [
            n for n in _walk(tree.root)
            if n.data is not None and n.data.is_signal
        ]
        assert len(signals) >= 1
        next_node = signals[1] if len(signals) > 1 else signals[0]
        _focus_node(tree, next_node)
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()

        assert panel.display is True
        assert panel.context is not None
        # When we picked a different signal, context should reflect it.
        if next_node is not sig_node:
            assert (
                panel.context.signal_name != first_ctx.signal_name
                or panel.context.entity_name != first_ctx.entity_name
            )


# ── 2. Entity drawer lifecycle ────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "forest_fixture",
    ["healthy_forest", "deep_forest", "single_entity_forest"],
)
async def test_entity_drawer_lifecycle(
    make_app, healthy_client, forest_fixture, request,
):
    """Walk: hidden → show e1 → switch to e2 → toggle off → reopen."""
    forest = request.getfixturevalue(forest_fixture)

    app = make_app(healthy_client)
    _install_forest(app, forest)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        drawer = app.query_one("#entity-drawer", EntityDrawer)
        tree = app.query_one("#health-tree", HealthTree)

        # 1. hidden
        assert not drawer.is_visible
        assert drawer.current_entity_name is None

        # 2. show first entity via 'd'
        entities = _all_entity_nodes(tree)
        assert len(entities) >= 1
        first = entities[0]
        _focus_node(tree, first)
        tree.focus()
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert drawer.is_visible
        assert drawer.current_entity_name == first.data.entity_name
        first_name = drawer.current_entity_name

        # 3. if a second entity exists, switch to it (drawer stays visible
        #    but content updates). Otherwise, skip the switch step.
        if len(entities) > 1:
            second = entities[1]
            _focus_node(tree, second)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert drawer.is_visible
            assert drawer.current_entity_name == second.data.entity_name
            assert drawer.current_entity_name != first_name

        # 4. toggle off — pressing d on the same entity hides the drawer
        current = drawer.current_entity_name
        # ensure cursor is on the currently-shown entity
        for n in entities:
            if n.data.entity_name == current:
                _focus_node(tree, n)
                break
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert not drawer.is_visible

        # 5. reopen
        await pilot.press("d")
        await pilot.pause()
        assert drawer.is_visible


# ── 3. Query editor lifecycle ─────────────────────────────────────────


SIGNAL_PAIR_IDS = ["promql", "arm", "kql", "external", "no_value"]


def _make_qe_client(base_client, signal_pair, execute_outcome):
    """Wrap *base_client* with entity/signal-definition reads that return payloads
    payloads matching *signal_pair*'s SignalKind, and an execute_signal that
    returns *execute_outcome* (dict) or raises (Exception).
    """
    sig_def, sig_value, sig_instance = signal_pair

    base_client.get_entity.return_value = {
        "name": "entity",
        "properties": {
            "displayName": "Entity Display",
            "signalGroups": {
                "default": {
                    "azureMonitorWorkspaceResourceId": "/subscriptions/x",
                    "signals": [sig_instance],
                },
            },
        },
    }
    base_client.get_signal_definition.return_value = {
        "name": "signal-definition",
        "properties": {
            "displayName": sig_def.display_name,
            "signalKind": sig_def.signal_kind.value,
            "queryText": getattr(sig_def, "query_text", ""),
        },
    }
    return base_client


QE_OUTCOMES = ["success", "error_payload", "exception_throttled"]


@pytest.mark.anyio
@pytest.mark.parametrize("test_outcome", QE_OUTCOMES)
@pytest.mark.parametrize("signal_pair", SIGNAL_PAIR_IDS, indirect=True)
async def test_query_editor_lifecycle(
    monkeypatch,
    make_app,
    healthy_client,
    signal_pair,
    test_outcome,
    request,
):
    """Walk: open → loading → loaded → edit query → ctrl+r → result → escape."""
    outcome_map = {
        "success": request.getfixturevalue("execute_success"),
        "error_payload": request.getfixturevalue("execute_error_payload"),
        "exception_throttled": request.getfixturevalue("execute_exception_throttled"),
    }
    outcome = outcome_map[test_outcome]

    def fake_execute(client, rg, model, entity_name, signal_name, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(
        "azext_healthmodels_plus.tui.watch.query_editor.execute_signal", fake_execute,
    )

    _make_qe_client(healthy_client, signal_pair, outcome)

    app = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        tree = app.query_one("#health-tree", HealthTree)
        sig_node = _first_signal_node(tree)
        # Align the mocked signal instance name with the node we'll select so
        # ``_fetch_config`` can resolve the signal in the entity payload.
        signal_pair[2]["name"] = sig_node.data.entity_name
        _focus_node(tree, sig_node)
        tree.focus()
        await pilot.pause()

        # 1. open editor
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, QueryEditor)
        editor: QueryEditor = app.screen  # type: ignore[assignment]

        # 2. wait for load
        for _ in range(20):
            if editor._loaded:
                break
            await pilot.pause()
        assert editor._loaded

        ta = editor.query_one("#qe-query", TextArea)
        original = ta.text

        # 3. trigger test (no edit → uses execute_signal directly)
        await pilot.press("ctrl+r")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        results_text = _static_text(editor.query_one("#qe-results", Static))
        if isinstance(outcome, BaseException):
            assert "Error" in results_text or "Throttled" in results_text
        elif outcome.get("error"):
            assert "Error" in results_text or "failed" in results_text
        else:
            assert "succeeded" in results_text or "✓" in results_text

        # 4. escape closes
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, QueryEditor)

        # ensure original used (sanity)
        assert isinstance(original, str)


# ── 4. Query editor: edited query path ────────────────────────────────


@pytest.mark.anyio
async def test_query_editor_edited_query_uses_override_path(
    monkeypatch, make_app, healthy_client, promql_signal_pair, execute_success,
):
    """Edited query text is passed as execute_signal(query_override=...)."""
    captured: dict[str, Any] = {"query_override": None}

    def fake_execute(client, rg, model, entity_name, signal_name, **kwargs):
        captured["query_override"] = kwargs.get("query_override")
        return execute_success

    monkeypatch.setattr(
        "azext_healthmodels_plus.tui.watch.query_editor.execute_signal", fake_execute,
    )

    app = make_app(healthy_client)
    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        tree = app.query_one("#health-tree", HealthTree)
        sig_node = _first_signal_node(tree)
        signal_name = sig_node.data.entity_name

        healthy_client.get_entity.return_value = {
            "name": "entity",
            "properties": {
                "displayName": "Entity Display",
                "signalGroups": {
                    "default": {
                        "azureMonitorWorkspaceResourceId": "/subscriptions/x",
                        "signals": [{
                            "name": signal_name,
                            "displayName": "S",
                            "signalKind": "PrometheusMetricsQuery",
                            "queryText": "rate(original[5m])",
                        }],
                    },
                },
            },
        }
        healthy_client.get_signal_definition.return_value = {
            "name": "def",
            "properties": {"queryText": "rate(original[5m])"},
        }

        _focus_node(tree, sig_node)
        tree.focus()
        await pilot.pause()

        await pilot.press("e")
        await pilot.pause()
        editor: QueryEditor = app.screen  # type: ignore[assignment]
        for _ in range(20):
            if editor._loaded:
                break
            await pilot.pause()
        assert editor._loaded

        ta = editor.query_one("#qe-query", TextArea)
        ta.text = "rate(edited_metric[5m])"
        await pilot.pause()

        await pilot.press("ctrl+r")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert captured.get("query_override") == "rate(edited_metric[5m])"
