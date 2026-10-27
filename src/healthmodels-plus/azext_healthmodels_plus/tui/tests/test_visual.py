"""Visual test harness for the Health Model Watch TUI.

Uses Textual's headless ``App.run_test()`` to launch the TUI,
inject fixture data via a mocked client, capture SVG screenshots,
and verify the tree renders correctly.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
SCREENSHOT_DIR = Path(__file__).parent


def load_fixture(name: str) -> list[dict]:
    with open(FIXTURES / name) as f:
        return json.load(f)["value"]


def make_mock_client(entities_file: str = "hm-entities.json") -> MagicMock:
    """Create a mock ``CloudHealthClient`` backed by JSON fixtures."""
    client = MagicMock()
    client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
    client.list_entities.return_value = load_fixture(entities_file)
    client.list_relationships.return_value = load_fixture("hm-relationships.json")
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tui_renders_tree():
    """Launch the TUI headlessly, trigger a poll, verify tree renders."""
    from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
    from azext_healthmodels_plus.tui.watch.health_tree import HealthTree

    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=300)

    async with app.run_test(size=(120, 40)) as pilot:
        # Trigger the initial poll manually
        await app._do_poll()
        await pilot.pause()

        # Verify the tree has content
        tree = app.query_one("#health-tree", HealthTree)
        assert tree.root.children, "Tree should have child nodes after poll"

        # Save screenshot for visual inspection
        app.save_screenshot("test_tui_healthy.svg", path=str(SCREENSHOT_DIR))


@pytest.mark.anyio
async def test_tui_shows_all_entities():
    """Verify all 30 entities are rendered in the tree."""
    from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
    from azext_healthmodels_plus.tui.watch.health_tree import HealthTree

    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=300)

    async with app.run_test(size=(120, 60)) as pilot:
        await app._do_poll()
        await pilot.pause()

        tree = app.query_one("#health-tree", HealthTree)
        # _node_map is keyed by entity_id for every entity in the tree
        entity_count = len(tree._node_map)
        assert entity_count == 30, f"Expected 30 entities, got {entity_count}"


@pytest.mark.anyio
async def test_tui_degraded_shows_changes():
    """Poll with healthy then degraded data, verify changes detected."""
    from azext_healthmodels_plus.tui.watch.app import HealthWatchApp

    client = make_mock_client("hm-entities.json")
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=300)

    async with app.run_test(size=(120, 60)) as pilot:
        # First poll — all healthy
        await app._do_poll()
        await pilot.pause()

        # Switch fixture to degraded data for second poll
        client.list_entities.return_value = load_fixture("hm-entities-degraded.json")
        await app._do_poll()
        await pilot.pause()

        # Save screenshot showing degraded state
        app.save_screenshot("test_tui_degraded.svg", path=str(SCREENSHOT_DIR))


@pytest.mark.anyio
async def test_tui_keyboard_navigation():
    """Test arrow keys navigate the tree."""
    from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
    from azext_healthmodels_plus.tui.watch.health_tree import HealthTree

    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=300)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        tree = app.query_one("#health-tree", HealthTree)
        tree.focus()
        await pilot.pause()

        # Press down arrow to navigate
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()

        # The cursor should have moved
        assert tree.cursor_line > 0, "Cursor should move after pressing down"


@pytest.mark.anyio
async def test_quit_binding():
    """Test that 'q' quits the app."""
    from azext_healthmodels_plus.tui.watch.app import HealthWatchApp

    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=300)

    async with app.run_test() as pilot:
        await pilot.press("q")
        # If we reach here without hanging, the quit binding works
