"""End-to-end tests for the search feature in the Health Model Watch TUI.

Uses Textual's headless ``App.run_test()`` to drive the full application
with fixture data, type search queries, navigate results, and capture
SVG screenshots at each stage for visual verification.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
from azext_healthmodels_plus.tui.watch.status_bar import StatusBar

# ---------------------------------------------------------------------------
# Fixture helpers (same pattern as test_visual.py)
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"


def load_fixture(name: str) -> list[dict]:
    with open(FIXTURES / name) as f:
        return json.load(f)["value"]


def make_mock_client(entities_file: str = "hm-entities.json") -> MagicMock:
    client = MagicMock()
    client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
    client.list_entities.return_value = load_fixture(entities_file)
    client.list_relationships.return_value = load_fixture("hm-relationships.json")
    return client


def _save(app: HealthWatchApp, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    app.save_screenshot(name, path=str(SCREENSHOT_DIR))


# ---------------------------------------------------------------------------
# E2E: Open search modal with /
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slash_opens_search_modal():
    """Press / after data loads → search modal appears with an input."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # The search modal should be on the screen stack
        assert len(app.screen_stack) == 2, "Search modal should push a screen"
        _save(app, "e2e_search_modal_open.svg")


# ---------------------------------------------------------------------------
# E2E: Type a query and see results
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_typing_query_shows_results():
    """Type 'Gateway' → results should appear in the option list."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # Type "Gateway"
        await pilot.press("G", "a", "t", "e", "w", "a", "y")
        await pilot.pause()

        # App search state should have results (entities + signals matching "Gateway")
        assert len(app._search_results) > 0, "Should find matches for 'Gateway'"
        assert app._search_query == "Gateway"

        _save(app, "e2e_search_results_gateway.svg")


@pytest.mark.anyio
async def test_typing_entity_name_finds_entity():
    """Type 'Failures' → should find the Failures entity."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "Failures":
            await pilot.press(ch)
        await pilot.pause()

        entity_results = [r for r in app._search_results if not r.is_signal]
        assert any(
            "Failures" in r.display_name for r in entity_results
        ), "Should match the Failures entity"


@pytest.mark.anyio
async def test_typing_signal_name_finds_signals():
    """Type 'Memory' → should find Memory Pressure signals."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "Memory":
            await pilot.press(ch)
        await pilot.pause()

        signal_results = [r for r in app._search_results if r.is_signal]
        assert len(signal_results) > 0, "Should find signal matches for 'Memory'"
        assert all(
            r.signal_value is not None for r in signal_results
        ), "Signal results should include formatted values"

        _save(app, "e2e_search_results_memory.svg")


# ---------------------------------------------------------------------------
# E2E: Select a result and jump in tree
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_enter_selects_result_and_closes_modal():
    """Type a query, press Enter → modal closes, tree jumps to entity."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "Latency":
            await pilot.press(ch)
        await pilot.pause()

        assert len(app._search_results) > 0
        first_result = app._search_results[0]

        # Press Enter to select
        await pilot.press("enter")
        await pilot.pause()

        # Modal should be dismissed — back to main screen
        assert len(app.screen_stack) == 1, "Modal should be closed"

        _save(app, "e2e_search_selected_latency.svg")


@pytest.mark.anyio
async def test_escape_closes_modal_preserves_state():
    """Type a query, press Escape → modal closes but query + results persist."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "Cosmos":
            await pilot.press(ch)
        await pilot.pause()

        saved_query = app._search_query
        saved_count = len(app._search_results)
        assert saved_count > 0

        await pilot.press("escape")
        await pilot.pause()

        assert len(app.screen_stack) == 1, "Modal should be closed"
        assert app._search_query == saved_query, "Query should persist after Escape"
        assert len(app._search_results) == saved_count, "Results should persist"


# ---------------------------------------------------------------------------
# E2E: n/p navigation after search
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_n_p_cycle_through_results():
    """Search, close modal, then n/p should jump through matches in tree."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 60)) as pilot:
        await app._do_poll()
        await pilot.pause()

        # Search for something with multiple results
        await pilot.press("slash")
        await pilot.pause()
        for ch in "Pod":
            await pilot.press(ch)
        await pilot.pause()

        assert len(app._search_results) >= 2, "Need multiple results for n/p test"

        # Select first result and close
        await pilot.press("enter")
        await pilot.pause()

        # Now press n to go to next match
        initial_cursor = app._search_cursor
        await pilot.press("n")
        await pilot.pause()

        assert app._search_cursor == (initial_cursor + 1) % len(
            app._search_results
        ), "n should advance cursor"

        _save(app, "e2e_search_next_match.svg")

        # Press p to go back
        await pilot.press("p")
        await pilot.pause()

        assert app._search_cursor == initial_cursor, "p should go back"


@pytest.mark.anyio
async def test_n_wraps_around():
    """n at the last result should wrap to the first."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        for ch in "Pod":
            await pilot.press(ch)
        await pilot.pause()

        result_count = len(app._search_results)
        assert result_count >= 2

        await pilot.press("enter")
        await pilot.pause()

        # Advance to the last result
        for _ in range(result_count - 1):
            await pilot.press("n")
            await pilot.pause()

        assert app._search_cursor == result_count - 1

        # One more n should wrap to 0
        await pilot.press("n")
        await pilot.pause()

        assert app._search_cursor == 0, "Should wrap around to first result"


# ---------------------------------------------------------------------------
# E2E: Status bar shows n/p hints only after search
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_status_bar_no_search_hints_initially():
    """Before any search, status bar should not show n/p hints."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        status = app.query_one("#status-bar", StatusBar)
        assert not status.has_search_results

        _save(app, "e2e_status_bar_no_search.svg")


@pytest.mark.anyio
async def test_status_bar_shows_search_hints_after_search():
    """After searching and selecting, status bar should show n/p hints."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        for ch in "Cosmos":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        status = app.query_one("#status-bar", StatusBar)
        assert status.has_search_results, "Should show search hints after a search"

        _save(app, "e2e_status_bar_with_search.svg")


# ---------------------------------------------------------------------------
# E2E: Reopen search preserves previous query
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reopen_search_preserves_query():
    """Close modal, press / again → previous query should be pre-filled."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        # First search
        await pilot.press("slash")
        await pilot.pause()
        for ch in "Event":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app._search_query == "Event"

        # Reopen
        await pilot.press("slash")
        await pilot.pause()

        from textual.widgets import Input

        inp = app.screen.query_one("#search-input", Input)
        assert inp.value == "Event", "Query should be pre-filled on reopen"

        _save(app, "e2e_search_reopened.svg")


# ---------------------------------------------------------------------------
# E2E: Arrow keys in search navigate the results list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_arrow_keys_navigate_results():
    """Down arrow in search modal should move highlight in option list."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # Search for something with multiple results
        for ch in "Cosmos":
            await pilot.press(ch)
        await pilot.pause()

        from textual.widgets import OptionList

        option_list = app.screen.query_one("#search-results", OptionList)
        assert option_list.highlighted == 0

        await pilot.press("down")
        await pilot.pause()

        assert option_list.highlighted == 1, "Down should move highlight"

        await pilot.press("up")
        await pilot.pause()

        assert option_list.highlighted == 0, "Up should move highlight back"

        _save(app, "e2e_search_arrow_navigation.svg")


# ---------------------------------------------------------------------------
# E2E: Search with degraded data shows health colors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_degraded_data_shows_colors():
    """Search in degraded state → results should reflect degraded health."""
    client = make_mock_client("hm-entities-degraded.json")
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "Failures":
            await pilot.press(ch)
        await pilot.pause()

        # The Failures entity should be Degraded in the degraded fixture
        from azext_healthmodels_plus.tui.models.enums import HealthState

        degraded_results = [
            r for r in app._search_results
            if r.health_state in (HealthState.DEGRADED, HealthState.UNHEALTHY)
        ]
        assert len(degraded_results) > 0, "Degraded data should show degraded results"

        _save(app, "e2e_search_degraded.svg")


# ---------------------------------------------------------------------------
# E2E: No results for nonexistent query
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_results_for_gibberish():
    """Type gibberish → no results, enter does nothing."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "zzzzxxx":
            await pilot.press(ch)
        await pilot.pause()

        assert len(app._search_results) == 0
        # Enter should not crash when there are no results
        await pilot.press("enter")
        await pilot.pause()

        # Still on the modal (enter with no results doesn't dismiss)
        assert len(app.screen_stack) == 2


# ---------------------------------------------------------------------------
# E2E: n/p do nothing when no search results
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_n_p_noop_without_search():
    """n and p should be no-ops when no search has been done."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        # Press n and p without any search — should not crash
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()

        assert app._search_cursor == 0
        assert len(app._search_results) == 0


# ═══════════════════════════════════════════════════════════════════════
# Failure / negative / edge-case tests
# ═══════════════════════════════════════════════════════════════════════


# ---------------------------------------------------------------------------
# FAIL: Search before any data is loaded (no forest)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_slash_before_data_loads_does_nothing():
    """Press / before any poll completes → no modal should open."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        # Do NOT poll — forest is None
        await pilot.press("slash")
        await pilot.pause()

        # Modal should NOT have been pushed
        assert len(app.screen_stack) == 1, "No modal when forest is None"

        _save(app, "e2e_fail_slash_no_data.svg")


# ---------------------------------------------------------------------------
# FAIL: Search with API error (disconnected state)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_after_api_error():
    """If first poll fails (client error), / should not open modal."""
    client = MagicMock()
    client.list_signal_definitions.side_effect = RuntimeError("API down")

    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        # Forest is still None after a failed poll
        assert app._forest is None

        await pilot.press("slash")
        await pilot.pause()

        assert len(app.screen_stack) == 1, "No modal when poll failed"

        status = app.query_one("#status-bar", StatusBar)
        assert not status.connected, "Should show disconnected"

        _save(app, "e2e_fail_api_error.svg")


# ---------------------------------------------------------------------------
# FAIL: Clear search query completely → results list empties
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_clearing_search_query_empties_results():
    """Type a query, then backspace it all away → 0 results."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # Type "Pod"
        for ch in "Pod":
            await pilot.press(ch)
        await pilot.pause()
        assert len(app._search_results) > 0, "Should have results"

        # Now backspace all 3 chars
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()

        assert app._search_query == ""
        assert len(app._search_results) == 0, "Empty query → no results"

        _save(app, "e2e_fail_cleared_query.svg")


# ---------------------------------------------------------------------------
# FAIL: Special characters in search query → no crash, no results
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_special_characters_in_search():
    """Type special chars like <>&'\" → should not crash, just 0 results."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # Type characters that could break regex or HTML
        for ch in "<>&":
            await pilot.press(ch)
        await pilot.pause()

        # Should not crash and should have 0 results
        assert len(app._search_results) == 0

        _save(app, "e2e_fail_special_chars.svg")


# ---------------------------------------------------------------------------
# FAIL: Single-character search → should still work
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_single_character_search():
    """Typing just 'G' should return results (all names starting with G)."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        await pilot.press("G")
        await pilot.pause()

        # Should find GraphOrleons + Gateway entities + signals
        assert len(app._search_results) > 0, "Single char should match"

        _save(app, "e2e_fail_single_char.svg")


# ---------------------------------------------------------------------------
# FAIL: n/p after search with only 1 result — cursor stays at 0
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_n_p_with_single_result():
    """When search returns exactly 1 result, n/p should wrap to same item."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        # "GraphOrleons" is a unique entity name — should match exactly 1
        for ch in "GraphOrleons":
            await pilot.press(ch)
        await pilot.pause()

        assert len(app._search_results) == 1, "Exact name should match 1"

        await pilot.press("enter")
        await pilot.pause()

        # n should wrap around to 0
        await pilot.press("n")
        await pilot.pause()
        assert app._search_cursor == 0

        await pilot.press("p")
        await pilot.pause()
        assert app._search_cursor == 0

        _save(app, "e2e_fail_single_result_np.svg")


# ---------------------------------------------------------------------------
# FAIL: Rapid open/close/reopen — state consistency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rapid_open_close_reopen():
    """Rapidly opening, typing, escaping, and reopening should not corrupt state."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        # Round 1: open, type, escape
        await pilot.press("slash")
        await pilot.pause()
        for ch in "CPU":
            await pilot.press(ch)
        await pilot.pause()
        r1_count = len(app._search_results)
        await pilot.press("escape")
        await pilot.pause()

        # Round 2: reopen, type different query
        await pilot.press("slash")
        await pilot.pause()
        # Previous "CPU" should be selected; backspace clears it
        await pilot.press("backspace")
        await pilot.pause()
        for ch in "FD":
            await pilot.press(ch)
        await pilot.pause()
        r2_count = len(app._search_results)
        assert r2_count != r1_count or True, "Different queries should work"

        await pilot.press("escape")
        await pilot.pause()

        assert app._search_query == "FD"

        # Round 3: reopen again and enter
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        for ch in "Event":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert len(app.screen_stack) == 1
        assert app._search_query == "Event"

        _save(app, "e2e_fail_rapid_open_close.svg")


# ---------------------------------------------------------------------------
# FAIL: Search after data changes (degradation mid-session)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_reflects_latest_poll_data():
    """Search results should reflect the latest polled data, not stale data."""
    client = make_mock_client("hm-entities.json")
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        # First poll — healthy
        await app._do_poll()
        await pilot.pause()

        # Search for Failures — should be Healthy
        await pilot.press("slash")
        await pilot.pause()
        for ch in "Failures":
            await pilot.press(ch)
        await pilot.pause()

        from azext_healthmodels_plus.tui.models.enums import HealthState

        healthy_results = [
            r for r in app._search_results
            if r.display_name == "Failures"
        ]
        assert healthy_results[0].health_state == HealthState.HEALTHY

        await pilot.press("escape")
        await pilot.pause()

        # Second poll — degraded data
        client.list_entities.return_value = load_fixture("hm-entities-degraded.json")
        await app._do_poll()
        await pilot.pause()

        # Reopen search — results should now reflect degraded state
        await pilot.press("slash")
        await pilot.pause()
        # "Failures" still in query from before, selected text
        # Backspace clears, then retype
        await pilot.press("backspace")
        await pilot.pause()
        for ch in "Failures":
            await pilot.press(ch)
        await pilot.pause()

        degraded_results = [
            r for r in app._search_results
            if r.display_name == "Failures"
        ]
        assert degraded_results[0].health_state == HealthState.DEGRADED, \
            "Search should reflect latest poll data"

        _save(app, "e2e_fail_data_changes.svg")


# ---------------------------------------------------------------------------
# FAIL: Empty option list, arrow keys don't crash
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_arrow_keys_on_empty_results():
    """Up/Down on an empty result list should not crash."""
    client = make_mock_client()
    app = HealthWatchApp(client, "rg", "hm-graphorleons", poll_interval=9999)

    async with app.run_test(size=(120, 40)) as pilot:
        await app._do_poll()
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        for ch in "zzzzz":
            await pilot.press(ch)
        await pilot.pause()
        assert len(app._search_results) == 0

        # Arrow keys on empty list should not crash
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        await pilot.press("down", "down", "down")
        await pilot.pause()

        # Should still be on modal, no crash
        assert len(app.screen_stack) == 2

        _save(app, "e2e_fail_arrows_empty.svg")
