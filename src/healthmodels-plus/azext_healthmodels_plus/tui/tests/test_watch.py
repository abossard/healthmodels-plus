"""Tests for the watch subsystem: Poller, run_plain_watch, and run_watch dispatcher."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from azext_healthmodels_plus.tui.models.enums import ChangeKind
from azext_healthmodels_plus.tui.watch.poller import Poller, PollResult

# ─── fixture helpers ─────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list:
    with open(FIXTURES / name) as f:
        return json.load(f)["value"]


def make_mock_client(entities_file: str = "hm-entities.json") -> MagicMock:
    client = MagicMock()
    client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
    client.list_entities.return_value = load_fixture(entities_file)
    client.list_relationships.return_value = load_fixture("hm-relationships.json")
    return client


# ─── Poller tests ────────────────────────────────────────────────────


class TestPollerFirstPoll:
    """First poll_once() call — no previous snapshot exists."""

    def test_forest_has_one_root(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert len(result.forest.roots) == 1

    def test_snapshot_has_30_entity_states(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert len(result.snapshot.entity_states) == 30

    def test_all_changes_are_new(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert len(result.changes) > 0
        assert all(c.kind == ChangeKind.NEW for c in result.changes)

    def test_no_error(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert result.error is None


class TestPollerSecondPollNoChange:
    """Two identical polls — second should report zero changes."""

    def test_second_poll_has_no_changes(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")

        poller.poll_once()  # first
        result2 = poller.poll_once()  # second — same data

        assert result2.changes == []

    def test_second_poll_still_has_forest(self):
        client = make_mock_client()
        poller = Poller(client, "rg-test", "hm-test")

        poller.poll_once()
        result2 = poller.poll_once()

        assert len(result2.forest.roots) == 1
        assert len(result2.snapshot.entity_states) == 30


class TestPollerDegradedData:
    """First poll returns healthy data, second returns degraded — expect escalations."""

    def test_escalations_detected(self):
        normal_entities = load_fixture("hm-entities.json")
        degraded_entities = load_fixture("hm-entities-degraded.json")

        client = MagicMock()
        client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
        client.list_relationships.return_value = load_fixture("hm-relationships.json")
        client.list_entities.side_effect = [normal_entities, degraded_entities]

        poller = Poller(client, "rg-test", "hm-test")
        poller.poll_once()  # first — healthy baseline
        result2 = poller.poll_once()  # second — degraded

        assert result2.error is None
        escalations = [c for c in result2.changes if c.is_escalation]
        assert len(escalations) > 0

    def test_escalation_states(self):
        normal_entities = load_fixture("hm-entities.json")
        degraded_entities = load_fixture("hm-entities-degraded.json")

        client = MagicMock()
        client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
        client.list_relationships.return_value = load_fixture("hm-relationships.json")
        client.list_entities.side_effect = [normal_entities, degraded_entities]

        poller = Poller(client, "rg-test", "hm-test")
        poller.poll_once()
        result2 = poller.poll_once()

        escalations = [c for c in result2.changes if c.is_escalation]
        for esc in escalations:
            # Escalation means new_state severity > old_state severity
            assert esc.new_state is not None
            assert esc.old_state is not None
            assert esc.new_state.severity > esc.old_state.severity


class TestPollerErrorRecovery:
    """Client raises on first call — Poller should not crash."""

    def test_error_on_first_poll(self):
        client = MagicMock()
        client.list_signal_definitions.side_effect = RuntimeError("API down")

        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert result.error is not None
        assert "stale" in result.error.lower() or "fail" in result.error.lower()

    def test_fallback_forest_is_empty(self):
        client = MagicMock()
        client.list_signal_definitions.side_effect = RuntimeError("API down")

        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert result.forest.roots == ()
        assert result.forest.entities == {}

    def test_fallback_snapshot_is_empty(self):
        client = MagicMock()
        client.list_signal_definitions.side_effect = RuntimeError("API down")

        poller = Poller(client, "rg-test", "hm-test")
        result = poller.poll_once()

        assert result.snapshot.entity_states == {}
        assert result.snapshot.timestamp == ""


class TestPollerErrorAfterSuccess:
    """First poll succeeds, second raises — stale data should be preserved."""

    def _make_poller_with_failing_second(self):
        normal_entities = load_fixture("hm-entities.json")
        client = MagicMock()
        client.list_signal_definitions.return_value = load_fixture("hm-signals.json")
        client.list_relationships.return_value = load_fixture("hm-relationships.json")
        client.list_entities.return_value = normal_entities
        poller = Poller(client, "rg-test", "hm-test")

        result1 = poller.poll_once()
        assert result1.error is None  # first poll works

        # Now make client fail
        client.list_signal_definitions.side_effect = RuntimeError("Transient error")
        return poller, result1

    def test_error_set_on_second_poll(self):
        poller, _ = self._make_poller_with_failing_second()
        result2 = poller.poll_once()

        assert result2.error is not None

    def test_stale_forest_preserved(self):
        poller, result1 = self._make_poller_with_failing_second()
        result2 = poller.poll_once()

        # Forest should be the previous good one, not empty
        assert len(result2.forest.roots) == 1
        assert len(result2.forest.entities) == 30

    def test_stale_snapshot_preserved(self):
        poller, result1 = self._make_poller_with_failing_second()
        result2 = poller.poll_once()

        assert len(result2.snapshot.entity_states) == 30
        assert result2.snapshot.timestamp != ""


# ─── run_watch dispatcher tests ─────────────────────────────────────


class TestRunWatchDispatcher:
    """run_watch should dispatch to plain or TUI based on force_plain / isatty."""

    @patch("azext_healthmodels_plus.tui.watch.plain.run_plain_watch")
    def test_force_plain_calls_plain(self, mock_plain):
        from azext_healthmodels_plus.tui.watch import run_watch

        client = MagicMock()
        run_watch(client, "rg", "model", poll_interval=5, force_plain=True)

        mock_plain.assert_called_once_with(client, "rg", "model", 5)

    @patch("azext_healthmodels_plus.tui.watch.plain.run_plain_watch")
    def test_non_tty_calls_plain(self, mock_plain):
        from azext_healthmodels_plus.tui.watch import run_watch

        client = MagicMock()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            run_watch(client, "rg", "model", poll_interval=10, force_plain=False)

        mock_plain.assert_called_once_with(client, "rg", "model", 10)

    @patch("azext_healthmodels_plus.tui.watch.plain.run_plain_watch")
    def test_tty_with_no_textual_falls_back_to_plain(self, mock_plain):
        """When stdout is a TTY but textual is not importable, fall back to plain."""
        import importlib
        from azext_healthmodels_plus.tui.watch import run_watch

        client = MagicMock()

        # Temporarily block the watch.app import so the except ImportError branch runs
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def _fake_import(name, *args, **kwargs):
            if name == "azext_healthmodels_plus.tui.watch.app":
                raise ImportError("no textual")
            return original_import(name, *args, **kwargs)

        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            with patch("builtins.__import__", side_effect=_fake_import):
                run_watch(client, "rg", "model", poll_interval=10, force_plain=False)

        mock_plain.assert_called_once()

    def test_catalog_launch_reopens_monitor_when_back_result_is_returned(self, monkeypatch):
        from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
        from azext_healthmodels_plus.tui.watch import run_watch
        from azext_healthmodels_plus.tui.watch.model_catalog import CatalogSelection

        client = MagicMock()
        selections = [
            CatalogSelection(ModelKey("https://management.azure.com", "sub", "rg-a", "m-a")),
            CatalogSelection(ModelKey("https://management.azure.com", "sub", "rg-b", "m-b")),
            None,
        ]
        app_runs: list[tuple[str, str, bool]] = []
        requested_groups: list[str | None] = []

        class _FakeApp:
            RESULT_BACK_TO_MODELS = "back-to-models"

            def __init__(self, _client, rg, model, _interval, *, allow_back_to_models=False):
                app_runs.append((rg, model, allow_back_to_models))
                self._model = model

            def run(self):
                if self._model == "m-a":
                    return self.RESULT_BACK_TO_MODELS
                return None

        def _choose(_client, group):
            requested_groups.append(group)
            return selections.pop(0)

        monkeypatch.setattr("azext_healthmodels_plus.tui.watch.model_catalog.choose_model_interactively", _choose)
        monkeypatch.setattr("azext_healthmodels_plus.tui.watch.app.HealthWatchApp", _FakeApp)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)

        run_watch(client, resource_group=None, model_name=None, poll_interval=30, force_plain=False)

        assert app_runs == [("rg-a", "m-a", True), ("rg-b", "m-b", True)]
        assert requested_groups == [None, None]


# ─── run_plain_watch tests ───────────────────────────────────────────


class TestRunPlainWatch:
    """run_plain_watch should poll and print until interrupted."""

    @patch("azext_healthmodels_plus.tui.watch.plain.time.sleep", side_effect=KeyboardInterrupt)
    @patch("azext_healthmodels_plus.tui.watch.plain.Console")
    def test_runs_one_poll_then_exits_on_ctrl_c(self, mock_console_cls, mock_sleep):
        from azext_healthmodels_plus.tui.watch.plain import run_plain_watch

        client = make_mock_client()
        run_plain_watch(client, "rg-test", "hm-test", poll_interval=5)

        # Poller should have been called (Console.print called for output)
        assert mock_console_cls.return_value.print.call_count > 0

    @patch("azext_healthmodels_plus.tui.watch.plain.time.sleep", side_effect=KeyboardInterrupt)
    @patch("azext_healthmodels_plus.tui.watch.plain.Console")
    def test_console_clear_called(self, mock_console_cls, mock_sleep):
        from azext_healthmodels_plus.tui.watch.plain import run_plain_watch

        client = make_mock_client()
        run_plain_watch(client, "rg-test", "hm-test", poll_interval=5)

        mock_console_cls.return_value.clear.assert_called()
