# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------


def load_command_table(self, _):
    from azure.cli.core.commands import CliCommandType
    custom = CliCommandType(operations_tmpl='azext_healthmodels_plus.layout.custom#{}')
    with self.command_group('monitor health-models layout', custom_command_type=custom, is_preview=True) as g:
        g.custom_command('arrange', 'arrange_layout')
        g.custom_command('export', 'export_layout')
        g.custom_command('import', 'import_layout')
