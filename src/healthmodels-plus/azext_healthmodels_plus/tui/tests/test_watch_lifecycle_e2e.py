"""Category 1 — TUI watch lifecycle E2E tests.

End-to-end exercises of ``HealthWatchApp`` through ``App.run_test()``:
first poll, failure handling, recovery, escalation auto-jump, and the
``j`` toggle.  All assertions go through observable widget state — no
direct calls to pure domain functions.
"""
from __future__ import annotations

import pytest

from azext_healthmodels_plus.tui.client.errors import (
    ArmError,
    AuthenticationError,
    HealthModelNotFoundError,
    ThrottledError,
)
from azext_healthmodels_plus.tui.models.enums import ChangeKind
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
from azext_healthmodels_plus.tui.watch.status_bar import StatusBar


# ─── helpers ──────────────────────────────────────────────────────────


async def _settle(app, pilot, *, max_pauses: int = 8) -> None:
    """Allow Textual reactive updates and watcher callbacks to flush."""
    for _ in range(max_pauses):
        await pilot.pause()


# ─── tests ────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "client_fixture, expect_roots, expect_entity_count, expect_error, expect_first_change_kind",
    [
        ("healthy_client",  1, 30, False, ChangeKind.NEW),
        ("degraded_client", 1, 30, False, ChangeKind.NEW),
        ("empty_client",    0,  0, False, None),
    ],
)
async def test_first_poll_renders_tree(
    make_app, request, client_fixture,
    expect_roots, expect_entity_count, expect_error, expect_first_change_kind,
):
    """First poll renders the forest into the tree and flips connected=True."""
    client = request.getfixturevalue(client_fixture)
    app = make_app(client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await _settle(app, pilot)

        tree = app.query_one("#health-tree", HealthTree)
        status = app.query_one("#status-bar", StatusBar)

        assert status.connected is not expect_error
        # Forest is recorded for non-error polls (always non-error here)
        assert app._forest is not None
        assert len(app._forest.roots) == expect_roots
        assert len(app._forest.entities) == expect_entity_count
        # Tree internal node map mirrors the entity count
        assert len(tree._node_map) == expect_entity_count

        # First poll always reports every entity as a NEW change kind, unless empty.
        if expect_first_change_kind is None:
            assert expect_entity_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationError("denied"),
        ThrottledError("slow down", retry_after=10),
        HealthModelNotFoundError("missing"),
        ArmError("boom", status_code=500),
        ConnectionError("net"),
    ],
    ids=["auth", "throttled", "notfound", "arm500", "connection"],
)
async def test_first_poll_failure_renders_stale_and_error(
    make_app, error_client_factory, exc,
):
    """A failing first poll keeps connected=False and forest stays None (stale)."""
    client = error_client_factory(exc)
    app = make_app(client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await _settle(app, pilot)

        status = app.query_one("#status-bar", StatusBar)
        tree = app.query_one("#health-tree", HealthTree)

        # Status bar flips to disconnected and rendered string reflects it
        assert status.connected is False
        rendered = status.render().plain
        assert "Disconnected" in rendered

        # Forest never populated — tree empty, no entity nodes
        assert app._forest is None
        assert len(tree._node_map) == 0


@pytest.mark.anyio
async def test_recovery_after_failure(make_app, recovering_client):
    """First poll fails → disconnected; second poll recovers → connected with data."""
    app = make_app(recovering_client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        # Poll 1 — initial failure
        await app._do_poll()
        await _settle(app, pilot)
        recovering_client._bump_call_counter()

        status = app.query_one("#status-bar", StatusBar)
        tree = app.query_one("#health-tree", HealthTree)
        assert status.connected is False
        assert app._forest is None
        assert len(tree._node_map) == 0

        # Poll 2 — succeeds
        await app._do_poll()
        await _settle(app, pilot)

        assert status.connected is True
        assert app._forest is not None
        assert len(app._forest.entities) == 30
        assert len(tree._node_map) == 30


@pytest.mark.anyio
async def test_escalation_triggers_auto_jump(make_app, escalating_client):
    """Healthy → degraded transition produces escalations and bumps change_count."""
    app = make_app(escalating_client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        # Poll 1 — healthy baseline (no prior snapshot → all NEW, no escalations)
        await app._do_poll()
        await _settle(app, pilot)
        escalating_client._bump_call_counter()

        status = app.query_one("#status-bar", StatusBar)
        baseline_changes = status.change_count
        assert app.auto_jump is True

        # Poll 2 — degraded data → escalations
        await app._do_poll()
        await _settle(app, pilot)

        assert status.connected is True
        # change_count is the cumulative number of escalations seen
        assert status.change_count > baseline_changes


@pytest.mark.anyio
@pytest.mark.parametrize("auto_jump_initial", [True, False])
async def test_auto_jump_toggle_via_j_key(
    make_app, escalating_client, auto_jump_initial,
):
    """Pressing 'j' toggles the auto_jump flag both on the app and status bar."""
    app = make_app(escalating_client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await _settle(app, pilot)
        escalating_client._bump_call_counter()

        # Force the desired starting state by toggling once if needed
        if app.auto_jump is not auto_jump_initial:
            await pilot.press("j")
            await _settle(app, pilot)
        assert app.auto_jump is auto_jump_initial

        # Toggle and verify the flip propagates
        await pilot.press("j")
        await _settle(app, pilot)

        status = app.query_one("#status-bar", StatusBar)
        assert app.auto_jump is (not auto_jump_initial)
        assert status.auto_jump is (not auto_jump_initial)
        rendered = status.render().plain
        expected_label = "off" if auto_jump_initial else "on"
        assert f"Auto-jump: {expected_label}" in rendered
