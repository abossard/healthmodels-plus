# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------


def load_arguments(self, _):
    from azure.cli.core.commands.parameters import get_enum_type
    from azure.cli.core.commands.validators import validate_file_or_dict
    from .layout import ENGINES

    with self.argument_context('monitor health-models layout') as c:
        c.argument('health_model_name', options_list=['--name', '-n', '--health-model-name'],
                   help='Name of the health model.')
        c.argument('dry_run', action='store_true', help='Return the new positions without saving them.')
    with self.argument_context('monitor health-models layout arrange') as c:
        c.argument('engine', arg_type=get_enum_type(list(ENGINES)),
                   help='auto: the better of layered and tree for this model. layered: rows ordered for few '
                        'crossings. tree: every subtree keeps its own columns. network: for models where many '
                        'entities share children; children that would join unrelated lines move down a little, '
                        'so rows are no longer straight. keep-order: tidy the canvas as drawn, keeping the '
                        'left-to-right order of every row.')
        c.argument('entity_name', help='Arrange only this entity and the entities below it. The entity keeps '
                                       'its position; cards beside them in those rows slide aside.')
    with self.argument_context('monitor health-models layout import') as c:
        c.argument('layout', type=validate_file_or_dict,
                   help='Positions as written by export: a JSON file path or string with a list of '
                        '{"name", "x", "y"} items. x and y null clears a position.')
