"""Help text for az health-models-tui."""
from __future__ import annotations

from knack.help_files import helps

helps["health-models-tui"] = """
type: command
short-summary: Launch the Health Models terminal monitor.
long-summary: |
  Starts the model browser by default. Select a model to open the interactive
  monitor, or use plain output mode in non-TTY/plain mode.
examples:
  - name: Browse health models in the current subscription
    text: az health-models-tui
  - name: Browse models in one resource group
    text: az health-models-tui -g myRg
  - name: Launch the interactive monitor
    text: az health-models-tui -g myRg --model myModel
  - name: Use plain-text output mode
    text: az health-models-tui --plain
  - name: Poll every 10 seconds
    text: az health-models-tui -g myRg --model myModel --poll-interval 10
"""
