"""Category 4 — data-flow integration E2E tests.

Exercises the full ``client → poller → forest → tree`` path for a range
of forest shapes, plus error surfacing through the status bar.
Pure domain functions are *not* tested directly — only the user-visible
TUI state after a poll cycle.
"""
from __future__ import annotations

import pytest

from azext_healthmodels_plus.tui.client.errors import (
    ArmError,
    AuthenticationError,
    HealthModelNotFoundError,
    ThrottledError,
)
from azext_healthmodels_plus.tui.models.domain import Forest, Snapshot
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
from azext_healthmodels_plus.tui.watch.poller import PollResult
from azext_healthmodels_plus.tui.watch.status_bar import StatusBar


# ─── helpers ──────────────────────────────────────────────────────────


async def _settle(app, pilot, *, max_pauses: int = 8) -> None:
    for _ in range(max_pauses):
        await pilot.pause()


def _max_depth(forest: Forest) -> int:
    """Compute the deepest path in the forest, treating each entity as a level.

    Returns 0 for an empty forest, 1 for a single entity with no children.
    """
    if not forest.roots:
        return 0

    def _walk(name: str, seen: set[str]) -> int:
        if name in seen:  # safety against cycles in malformed inputs
            return 0
        seen = seen | {name}
        node = forest.entities.get(name)
        if node is None or not node.children:
            return 1
        return 1 + max(_walk(c, seen) for c in node.children)

    return max(_walk(r, set()) for r in forest.roots)


def _stub_poll_with_forest(app, forest: Forest) -> None:
    from .conftest import install_wire_forest
    install_wire_forest(app._client, forest)


# ─── tests ────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "forest_fixture, expect_root_count, expect_unlinked_count, expect_max_depth",
    [
        ("healthy_forest",       1, 0, 4),
        ("deep_forest",          1, 0, 4),
        ("cyclic_forest",        1, 0, 2),
        ("unlinked_forest",      2, 2, 1),
        ("empty_forest",         0, 0, 0),
        ("single_entity_forest", 1, 0, 1),
    ],
)
async def test_poll_renders_forest_shape(
    make_app, healthy_client, request, forest_fixture,
    expect_root_count, expect_unlinked_count, expect_max_depth,
):
    """Each forest shape lands intact in the tree after one poll cycle."""
    forest = request.getfixturevalue(forest_fixture)
    app = make_app(healthy_client, interval=9999)
    _stub_poll_with_forest(app, forest)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await _settle(app, pilot)

        tree = app.query_one("#health-tree", HealthTree)
        status = app.query_one("#status-bar", StatusBar)

        assert status.connected is True
        assert set(app._forest.entities) == set(forest.entities)
        assert len(app._forest.roots) == expect_root_count
        assert len(app._forest.unlinked) == expect_unlinked_count
        assert _max_depth(app._forest) == expect_max_depth
        # Every named entity in the forest got rendered as a tree node
        assert len(tree._node_map) == len(forest.entities)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exc, expect_status_substring",
    [
        (AuthenticationError("x"),                "Disconnected"),
        (ThrottledError("x", retry_after=5),      "Disconnected"),
        (HealthModelNotFoundError("x"),           "Disconnected"),
        (ArmError("x", status_code=500),          "Disconnected"),
    ],
    ids=["auth", "throttled", "notfound", "arm500"],
)
async def test_sdk_error_surfaces_in_status_bar(
    make_app, error_client_factory, exc, expect_status_substring,
):
    """Any SDK error during polling flips the status bar to disconnected."""
    client = error_client_factory(exc)
    app = make_app(client, interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await _settle(app, pilot)

        status = app.query_one("#status-bar", StatusBar)
        tree = app.query_one("#health-tree", HealthTree)

        assert status.connected is False
        assert expect_status_substring in status.render().plain
        # Forest stays unset — UI shows nothing on first-poll failure
        assert app._forest is None
        assert len(tree._node_map) == 0
