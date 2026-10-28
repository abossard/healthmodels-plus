"""CLI parameters for az monitor health-models tui."""
from __future__ import annotations


def load_arguments(self, _):  # noqa: ANN001
    from azure.cli.core.commands.parameters import get_resource_group_completion_list

    with self.argument_context("monitor health-models tui") as context:
        context.argument(
            "resource_group",
            options_list=["--resource-group", "-g"],
            default=None,
            help="Name of the resource group.",
            completer=get_resource_group_completion_list,
        )
        context.argument(
            "model_name",
            options_list=["--model-name", "--model"],
            default=None,
            help="Name of the health model to monitor.",
        )
        context.argument(
            "poll_interval",
            options_list=["--poll-interval"],
            type=int,
            default=30,
            help="Polling interval in seconds (default: 30).",
        )
        context.argument(
            "plain",
            options_list=["--plain"],
            action="store_true",
            help="Use plain-text output mode.",
        )
        context.argument(
            "debug_poll",
            options_list=["--debug-poll"],
            action="store_true",
            help="Show API calls, timing, and diff details on stderr.",
        )
