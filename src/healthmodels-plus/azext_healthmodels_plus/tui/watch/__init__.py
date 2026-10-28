"""Watch mode — live health model monitoring with TUI or plain-text fallback."""
from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = ["run_watch"]

if TYPE_CHECKING:
    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient


def run_watch(
    client: CloudHealthClient,
    resource_group: str | None = None,
    model_name: str | None = None,
    poll_interval: int = 30,
    force_plain: bool = False,
) -> None:
    """Entry point for ``az monitor health-models tui``.

    Dispatches to the interactive Textual TUI when stdout is a TTY and
    the ``textual`` package is available, otherwise falls back to a
    plain-text Rich loop.
    """
    import sys

    launched_from_catalog = model_name is None
    catalog_resource_group = resource_group
    monitor_resource_group = resource_group
    active_model_name = model_name

    if active_model_name is None:
        from azext_healthmodels_plus.tui.watch.model_catalog import (
            choose_model_interactively,
            print_plain_model_catalog,
        )

        if force_plain or not sys.stdout.isatty():
            print_plain_model_catalog(client, catalog_resource_group)
            return
        try:
            selection = choose_model_interactively(client, catalog_resource_group)
        except ImportError:
            print_plain_model_catalog(client, catalog_resource_group)
            return
        if selection is None:
            return
        monitor_resource_group = selection.model_key.group
        active_model_name = selection.model_key.name

    if force_plain or not sys.stdout.isatty():
        from azext_healthmodels_plus.tui.watch.plain import run_plain_watch

        run_plain_watch(client, monitor_resource_group, active_model_name, poll_interval)
    else:
        try:
            from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
        except ImportError:
            from azext_healthmodels_plus.tui.watch.plain import run_plain_watch

            run_plain_watch(client, monitor_resource_group, active_model_name, poll_interval)
            return

        while True:
            app = HealthWatchApp(
                client,
                monitor_resource_group,
                active_model_name,
                poll_interval,
                allow_back_to_models=launched_from_catalog,
            )
            result = app.run()
            if (
                launched_from_catalog
                and result == HealthWatchApp.RESULT_BACK_TO_MODELS
            ):
                from azext_healthmodels_plus.tui.watch.model_catalog import choose_model_interactively

                selection = choose_model_interactively(client, catalog_resource_group)
                if selection is None:
                    return
                monitor_resource_group = selection.model_key.group
                active_model_name = selection.model_key.name
                continue
            return
