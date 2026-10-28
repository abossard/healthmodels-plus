"""Azure CLI extension healthmodels-plus: health model canvas layout and terminal monitor."""
from __future__ import annotations

try:
    from azure.cli.core import AzCommandsLoader
except ModuleNotFoundError:  # pragma: no cover
    class AzCommandsLoader:  # type: ignore[override]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ModuleNotFoundError(
                "azure.cli.core is required to load this Azure CLI extension."
            )


class HealthModelsPlusCommandsLoader(AzCommandsLoader):
    """Loader for az monitor health-models layout and tui."""

    def load_command_table(self, args: list[str]):  # type: ignore[override]
        from azext_healthmodels_plus.layout import _help as _layout_help  # noqa: F401
        from azext_healthmodels_plus.layout.commands import load_command_table as load_layout_commands
        from azext_healthmodels_plus.tui import _help as _tui_help  # noqa: F401
        from azext_healthmodels_plus.tui.commands import load_command_table as load_tui_commands

        load_layout_commands(self, args)
        load_tui_commands(self, args)
        return super().load_command_table(args)

    def load_arguments(self, command: str) -> None:
        from azext_healthmodels_plus.layout._params import load_arguments as load_layout_arguments
        from azext_healthmodels_plus.tui._params import load_arguments as load_tui_arguments

        load_layout_arguments(self, command)
        load_tui_arguments(self, command)


COMMAND_LOADER_CLS = HealthModelsPlusCommandsLoader
