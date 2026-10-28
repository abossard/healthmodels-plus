# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from knack.help_files import helps  # pylint: disable=unused-import

helps['monitor health-models'] = """
type: group
short-summary: Manage Azure Monitor health models.
"""

helps['monitor health-models layout'] = """
type: group
short-summary: Arrange, export and import the positions of health model entities on the canvas.
"""

helps['monitor health-models layout arrange'] = """
type: command
short-summary: Arrange the entities on the canvas, like Arrange in the portal designer.
long-summary: >
    Parents go above their children; a parent with a single child sits straight above it;
    unconnected parts are shelved to fit a widescreen view. Only entities whose position changes
    are saved.
examples:
  - name: Preview a fresh layout without saving it.
    text: az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --dry-run
  - name: Preview a fresh layout and show the steps and decisions behind it.
    text: az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --dry-run --verbose
  - name: Tidy the canvas but keep the left-to-right order you drew.
    text: az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --engine keep-order
  - name: Give lines of entities with many shared children their own heights.
    text: az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --engine network
  - name: Arrange only the entities below "checkout", keeping "checkout" where it is.
    text: az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --entity-name checkout
"""

helps['monitor health-models layout export'] = """
type: command
short-summary: Export the canvas position of every entity; entities without a position get null.
examples:
  - name: Print the layout as JSON; redirect it to a file to keep a backup.
    text: az monitor health-models layout export -g MyResourceGroup -n MyHealthModel -o json
"""

helps['monitor health-models layout import'] = """
type: command
short-summary: Set canvas positions from an export, saving only the entities that move.
examples:
  - name: Restore a saved layout.
    text: az monitor health-models layout import -g MyResourceGroup -n MyHealthModel --layout layout.json
"""
