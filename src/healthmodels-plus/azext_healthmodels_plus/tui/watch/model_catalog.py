"""Read-only health-model catalog support."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from azext_healthmodels_plus.tui.client.errors import describe_error
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

if TYPE_CHECKING:
    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient


@dataclass(frozen=True)
class ModelCatalogRow:
    id: str
    name: str
    resource_group: str
    location: str
    provisioning_state: str
    model_key: ModelKey


def _resource_group_from_arm_id(resource_id: str) -> str | None:
    if not resource_id:
        return None
    parts = resource_id.strip("/").split("/")
    for index, segment in enumerate(parts):
        if segment.casefold() == "resourcegroups" and index + 1 < len(parts):
            candidate = parts[index + 1].strip()
            return candidate or None
    return None


def load_model_catalog_rows(
    client: CloudHealthClient,
    resource_group: str | None = None,
) -> list[ModelCatalogRow]:
    rows: list[ModelCatalogRow] = []
    for payload in client.list_health_models(resource_group):
        resource_id = str(payload.get("id", "")).strip()
        model_name = str(payload.get("name", "")).strip()
        if not model_name:
            continue
        row_group = _resource_group_from_arm_id(resource_id) or (resource_group or "")
        model_key = client.model_key(row_group, model_name)
        properties = payload.get("properties")
        provisioning_state = "-"
        if isinstance(properties, dict):
            candidate = properties.get("provisioningState")
            if candidate is not None and str(candidate).strip():
                provisioning_state = str(candidate)
        location = str(payload.get("location", "")).strip() or "-"
        rows.append(ModelCatalogRow(
            id=resource_id,
            name=model_name,
            resource_group=row_group,
            location=location,
            provisioning_state=provisioning_state,
            model_key=model_key,
        ))
    return sorted(rows, key=lambda row: (row.name.casefold(), row.resource_group.casefold(), row.id.casefold()))


def print_plain_model_catalog(
    client: CloudHealthClient,
    resource_group: str | None = None,
    *,
    console: Console | None = None,
) -> int:
    rows = load_model_catalog_rows(client, resource_group)
    output = console or Console()
    scope = f"resource group {resource_group}" if resource_group else "subscription"
    output.print(f"[bold]Health models in {scope}[/]")
    if not rows:
        output.print("[yellow]No health models found.[/]")
        return 0
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name", overflow="fold")
    table.add_column("Resource Group", overflow="fold")
    table.add_column("Region", overflow="fold")
    table.add_column("Provisioning", overflow="fold")
    for row in rows:
        table.add_row(row.name, row.resource_group or "-", row.location, row.provisioning_state)
    output.print(table)
    return 0


@dataclass(frozen=True)
class CatalogSelection:
    model_key: ModelKey


class ModelCatalogApp:  # pragma: no cover - wrapper class for runtime import
    def __new__(
        cls,
        client: CloudHealthClient,
        resource_group: str | None = None,
    ):
        from textual import on
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.widgets import DataTable, Footer, Header, Static

        class _CatalogApp(App[CatalogSelection | None]):
            TITLE = "Health models"
            BINDINGS = [
                Binding("q", "quit", "Quit"),
                Binding("escape", "quit", "Quit"),
                Binding("r", "refresh", "Refresh"),
            ]

            def __init__(self) -> None:
                super().__init__()
                self._client = client
                self._resource_group = resource_group
                self._rows_by_id: dict[str, ModelCatalogRow] = {}
                self._load_generation = 0

            def compose(self) -> ComposeResult:
                yield Header(show_clock=False)
                yield Static("Select a health model and press Enter.", id="catalog-title")
                yield Static("Loading health models...", id="catalog-loading")
                yield Static("", id="catalog-error", markup=False)
                yield Static("No health models found. Press r to refresh or q to quit.", id="catalog-empty")
                table = DataTable(id="catalog-table", cursor_type="row")
                table.add_columns("Name", "Resource Group", "Region", "Provisioning")
                yield table
                yield Footer()

            def on_mount(self) -> None:
                table = self.query_one("#catalog-table", DataTable)
                table.focus()
                self.action_refresh()

            def action_refresh(self) -> None:
                self._load_generation += 1
                generation = self._load_generation
                self._set_loading_state()
                self.run_worker(
                    self._load_rows(generation),
                    thread=True,
                    exclusive=True,
                    group="catalog-load",
                )

            def _set_loading_state(self) -> None:
                self.query_one("#catalog-loading", Static).display = True
                self.query_one("#catalog-error", Static).display = False
                self.query_one("#catalog-empty", Static).display = False

            async def _load_rows(self, generation: int) -> None:
                try:
                    rows = load_model_catalog_rows(self._client, self._resource_group)
                except Exception as error:  # noqa: BLE001
                    self.call_from_thread(self._publish_error, generation, error)
                    return
                self.call_from_thread(self._publish_success, generation, rows)

            def _publish_success(self, generation: int, rows: list[ModelCatalogRow]) -> None:
                if generation != self._load_generation:
                    return
                self.query_one("#catalog-loading", Static).display = False
                self.query_one("#catalog-error", Static).display = False
                self._rows_by_id = {row.id: row for row in rows}
                table = self.query_one("#catalog-table", DataTable)
                table.clear(columns=False)
                for row in rows:
                    table.add_row(
                        row.name,
                        row.resource_group or "-",
                        row.location,
                        row.provisioning_state,
                        key=row.id,
                    )
                empty = self.query_one("#catalog-empty", Static)
                empty.display = not bool(rows)
                if rows:
                    table.move_cursor(row=0, column=0)
                    table.focus()

            def _publish_error(self, generation: int, error: Exception) -> None:
                if generation != self._load_generation:
                    return
                self.query_one("#catalog-loading", Static).display = False
                message = describe_error(error)
                error_widget = self.query_one("#catalog-error", Static)
                error_widget.update(
                    f"Failed to load health models. {message} Press r to retry or q to quit."
                )
                error_widget.display = True
                self.query_one("#catalog-empty", Static).display = False

            @on(DataTable.RowSelected, "#catalog-table")
            def _on_row_selected(self, event: DataTable.RowSelected) -> None:
                row_key = event.row_key
                row_id = row_key.value if hasattr(row_key, "value") else str(row_key)
                self._open_row(row_id)

            def _open_row(self, row_id: str) -> None:
                selected = self._rows_by_id.get(row_id)
                if selected is None:
                    return
                self.exit(CatalogSelection(model_key=selected.model_key))

        return _CatalogApp()


def choose_model_interactively(
    client: CloudHealthClient,
    resource_group: str | None = None,
) -> CatalogSelection | None:
    app = ModelCatalogApp(client, resource_group)
    return app.run()
