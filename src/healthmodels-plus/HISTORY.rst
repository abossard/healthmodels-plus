.. :changelog:

Release History
===============

0.3.0
+++++
* ``az monitor health-models tui`` colors the tree lines by health: a vertical line by its parent, a branch by the entity or signal it leads to.

0.2.0
+++++
* ``az health-models-tui`` is now ``az monitor health-models tui``, next to the ``layout`` group. Arguments and help are unchanged.

0.1.0
+++++
* Initial release. Combines the ``health-models-layout`` 1.0.0b1 and ``health-models-tui`` 0.4.0 extensions into one extension, ``healthmodels-plus``. ``az monitor health-models layout arrange|export|import`` and ``az health-models-tui`` keep their names, arguments and help.
* Requires Azure CLI 2.75.0 or newer with Python 3.11 or newer.

Earlier releases of health-models-tui
=====================================

0.4.0
+++++
* Timeline fills in row by row with a loading indicator and no timeout; bands use colored blocks (color = worst state, height = signal value).
* Layout: compact Timeline controls, highlighted active tab, short UTC timestamps, a status bar that never breaks words, full-width side panes below 110 columns.
* Signal panel shows the service-side error; Log Analytics signals are verified by running their query through the workspace query API; non-finite values are errors for every signal kind; the query editor fits small screens.
* Changes tab follows the draft you changed last and keeps typed text until Update draft; Prepare and Apply run in the background and keep edits made meanwhile; Prepare, Apply, Discard, Restore and quit wait for a running write, whose error and outcome stay visible; the quit dialog works from the keyboard.
* ``1`` ``2`` ``3`` switch tabs (Ctrl+digit rarely reaches terminal apps); Help lists every key from the bindings and scrolls on small terminals; the status bar marks an open snapshot.
* Fixes for live models: inline signals show their names, units and thresholds; rules with ``LessThan``, ``GreaterThanOrEqual``, ``LessThanOrEqual`` or ``Equal`` are no longer dropped; Prepare no longer reports a stale baseline because of Resource Health status; malformed or non-finite history fails only its row.
* Data-source query errors and entity 404s are reported as typed errors.
* Snapshots load every nested model on the selected path; Compare diffs only the compared model; nested references expanded in quick succession all load.
* Metric and Prometheus verification use the configured cloud endpoint instead of the public Azure host.
* Apply results show the wanted and the live value of every field that did not land; Prepare lists shared consumers by display name; display and icon names are limited to 256 characters.
* The wheel ships ``azext_metadata.json`` (minimum Azure CLI 2.61.0) and no longer a stale copy of the package from a local ``build/`` directory.

Earlier releases of health-models-layout
========================================

1.0.0b1
+++++++
* Initial release. Adds ``az monitor health-models layout arrange``, ``export`` and ``import`` to lay out, back up and restore the canvas positions of health model entities.
