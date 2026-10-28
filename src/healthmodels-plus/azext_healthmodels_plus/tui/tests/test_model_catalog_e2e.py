from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from threading import Event
from typing import Any

import pytest
from rich.console import Console
from textual.widgets import DataTable, Static

from azext_healthmodels_plus.tui.client.errors import (
    AuthenticationError,
    HealthModelError,
    ThrottledError,
)
from azext_healthmodels_plus.tui.watch import run_watch
from azext_healthmodels_plus.tui.watch.model_catalog import (
    CatalogSelection,
    ModelCatalogApp,
    print_plain_model_catalog,
)


def _row(resource_group: str, name: str, state: str = "Succeeded") -> dict[str, Any]:
    return {
        "id": f"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/{resource_group}/providers/Microsoft.CloudHealth/healthmodels/{name}",
        "name": name,
        "location": "westeurope",
        "properties": {"provisioningState": state},
    }


@dataclass
class _ScriptedClient:
    responses: list[list[dict[str, Any]] | Exception]
    arm_endpoint: str = "https://management.azure.com"
    _subscription_id: str = "00000000-0000-0000-0000-000000000001"
    calls: list[tuple[str | None, str]] = field(default_factory=list)

    def model_key(self, group: str, model: str):
        from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

        return ModelKey(self.arm_endpoint, self._subscription_id, group, model)

    def list_health_models(self, resource_group: str | None = None):
        self.calls.append((resource_group, threading.current_thread().name))
        if not self.responses:
            raise AssertionError("unexpected extra list call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def _wait_for(predicate, pilot, attempts: int = 60) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause()
    assert predicate()


def _static_plain(app, selector: str) -> str:
    rendered = app.query_one(selector, Static).render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_catalog_physical_down_enter_selects_exact_arm_identity(size) -> None:
    client = _ScriptedClient([
        [_row("rg-a", "shared"), _row("rg-b", "shared"), _row("rg-c", "other")],
    ])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=size) as pilot:
        await _wait_for(
            lambda: app.query_one("#catalog-loading", Static).display is False
            and app.query_one("#catalog-table", DataTable).row_count == 3,
            pilot,
        )
        await pilot.press("down", "down", "enter")
        await pilot.pause()

    assert app.return_value == CatalogSelection(client.model_key("rg-b", "shared"))
    assert len(client.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_catalog_empty_state_is_visible_and_refreshable(size) -> None:
    client = _ScriptedClient([[], []])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=size) as pilot:
        await _wait_for(
            lambda: app.query_one("#catalog-loading", Static).display is False,
            pilot,
        )
        empty = app.query_one("#catalog-empty", Static)
        assert empty.display is True
        await pilot.press("r")
        await _wait_for(lambda: len(client.calls) == 2, pilot)
        await pilot.press("q")
        await pilot.pause()

    assert app.return_value is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("unauthorized", status_code=401),
        AuthenticationError("forbidden", status_code=403),
        ThrottledError("throttled", retry_after=2),
        ConnectionError("transport down"),
    ],
)
async def test_catalog_retry_retains_last_success_rows_on_failure(error: Exception) -> None:
    client = _ScriptedClient([
        [_row("rg-a", "alpha")],
        error,
        [_row("rg-a", "alpha"), _row("rg-b", "beta")],
    ])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(
            lambda: app.query_one("#catalog-table", DataTable).row_count == 1
            and app.query_one("#catalog-loading", Static).display is False,
            pilot,
        )
        await pilot.press("r")
        await _wait_for(lambda: app.query_one("#catalog-error", Static).display is True, pilot)
        assert app.query_one("#catalog-table", DataTable).row_count == 1

        await pilot.press("r")
        await _wait_for(
            lambda: app.query_one("#catalog-error", Static).display is False
            and app.query_one("#catalog-table", DataTable).row_count == 2,
            pilot,
        )

    assert len(client.calls) == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error,expected_fragment",
    [
        (HealthModelError("Forbidden for [scope]", code="AuthorizationFailed"), "[scope]"),
        (RuntimeError("bad [/] tag"), "[/]"),
    ],
)
async def test_catalog_initial_error_renders_literal_brackets_and_recovers(
    error: Exception, expected_fragment: str,
) -> None:
    client = _ScriptedClient([error, [_row("rg-a", "alpha")]])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(lambda: app.query_one("#catalog-error", Static).display is True, pilot)
        error_text = _static_plain(app, "#catalog-error")
        assert expected_fragment in error_text
        assert app.is_running is True

        await pilot.press("r")
        await _wait_for(
            lambda: app.query_one("#catalog-error", Static).display is False
            and app.query_one("#catalog-table", DataTable).row_count == 1,
            pilot,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error,expected_fragment",
    [
        (HealthModelError("Denied [AuthorizationFailed]", code="AuthorizationFailed"), "[AuthorizationFailed]"),
        (RuntimeError("refresh broke [/root]"), "[/root]"),
    ],
)
async def test_catalog_refresh_error_renders_literal_brackets_and_retains_rows(
    error: Exception, expected_fragment: str,
) -> None:
    client = _ScriptedClient([
        [_row("rg-a", "alpha")],
        error,
        [_row("rg-a", "alpha"), _row("rg-b", "beta")],
    ])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(lambda: app.query_one("#catalog-table", DataTable).row_count == 1, pilot)
        await pilot.press("r")
        await _wait_for(lambda: app.query_one("#catalog-error", Static).display is True, pilot)
        error_text = _static_plain(app, "#catalog-error")
        assert expected_fragment in error_text
        assert app.query_one("#catalog-table", DataTable).row_count == 1
        assert app.is_running is True

        await pilot.press("r")
        await _wait_for(
            lambda: app.query_one("#catalog-error", Static).display is False
            and app.query_one("#catalog-table", DataTable).row_count == 2,
            pilot,
        )


@pytest.mark.anyio
async def test_catalog_mounts_before_io_and_lists_on_worker_thread() -> None:
    started = Event()
    release = Event()

    class _BlockingClient(_ScriptedClient):
        def list_health_models(self, resource_group: str | None = None):
            self.calls.append((resource_group, threading.current_thread().name))
            started.set()
            release.wait(timeout=5)
            return [_row("rg-a", "alpha")]

    client = _BlockingClient([[_row("rg-a", "alpha")]])
    app = ModelCatalogApp(client, None)

    async with app.run_test(size=(120, 40)) as pilot:
        await _wait_for(lambda: started.is_set(), pilot)
        assert app.query_one("#catalog-loading", Static).display is True
        release.set()
        await _wait_for(lambda: app.query_one("#catalog-table", DataTable).row_count == 1, pilot)

    assert client.calls[0][1] != "MainThread"


def test_plain_catalog_prints_finite_read_only_list() -> None:
    client = _ScriptedClient([[_row("rg-a", "alpha"), _row("rg-b", "beta")]])
    console = Console(record=True, width=120)

    exit_code = print_plain_model_catalog(client, console=console)

    rendered = console.export_text()
    assert exit_code == 0
    assert "Health models in subscription" in rendered
    assert "alpha" in rendered
    assert "beta" in rendered
    assert "Create" not in rendered


def test_run_watch_without_model_uses_catalog_plain_path(monkeypatch) -> None:
    client = _ScriptedClient([[_row("rg-a", "alpha")]])
    calls: list[tuple[str, object]] = []

    def _capture_catalog(_client, _group):
        calls.append(("catalog", (_client, _group)))
        return 0

    def _capture_plain(_client, _group, _model, _poll):
        calls.append(("plain", (_client, _group, _model, _poll)))

    monkeypatch.setattr("azext_healthmodels_plus.tui.watch.model_catalog.print_plain_model_catalog", _capture_catalog)
    monkeypatch.setattr("azext_healthmodels_plus.tui.watch.plain.run_plain_watch", _capture_plain)

    run_watch(client, resource_group=None, model_name=None, force_plain=True, poll_interval=13)

    assert calls == [("catalog", (client, None))]
