# healthmodels-plus Azure CLI extension

Two command surfaces for Azure Monitor health models in one extension:

- `az monitor health-models layout arrange|export|import` arranges the entities of a health model on its canvas, like **Arrange** in the portal designer, and backs up and restores canvas layouts.
- `az monitor health-models tui` browses health models, watches their health live, looks back in time, and makes guarded edits in a terminal UI.

The extension is in preview. It needs Azure CLI 2.75.0 or newer (with Python 3.11+) and `az login` for the subscription you work in.

## The official `health-models` extension is optional

Neither command needs the official `health-models` extension (`az monitor health-models create|show|...`) at runtime. The layout commands call the Microsoft.CloudHealth REST API (version `2026-11-01`) directly with your `az login` credentials and bring their own help for the `az monitor health-models` group. `az monitor health-models tui` uses the `azure-mgmt-cloudhealth` SDK. With both extensions installed, `az monitor health-models --help` lists the official commands next to `layout` and `tui`.

## Layout

```bash
az monitor health-models layout export -g MyResourceGroup -n MyHealthModel -o json > layout.json
az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel --dry-run
az monitor health-models layout arrange -g MyResourceGroup -n MyHealthModel
az monitor health-models layout import -g MyResourceGroup -n MyHealthModel --layout layout.json
```

`--engine` picks the layout:

- `auto` (default): the better of `layered` and `tree` for the model.
- `layered`: rows ordered for few crossings.
- `tree`: every subtree keeps its own columns.
- `network`: for models where many entities share children. Lines that would merge get their own height, so rows are no longer straight.
- `keep-order`: tidies the canvas but keeps the left-to-right order you drew.

`--verbose` logs the steps and decisions: the connected parts, which of `layered` and `tree` `auto` kept and why, the row order and local search, where each part is placed, and what is saved. `--debug` adds the cards that hang under a shared ancestor, and the steps of single unconnected entities.

`--entity-name` arranges one entity and everything below it, keeping that entity in place.

The engines score each drawing the way the portal renders it, as step lines with labels, and prefer fewer edges through cards, fewer merged lines that suggest links that don't exist, fewer crossings and fewer overlapping labels. They are plain Python in `layout/layout.py`, without dependencies.

## Terminal monitor

```bash
az monitor health-models tui                              # pick a model from the active subscription
az monitor health-models tui -g myRg                      # pick a model from one resource group
az monitor health-models tui -g myRg --model myModel      # open a model directly
az monitor health-models tui -g myRg --model myModel --plain --poll-interval 10
```

Without a TTY (or with `--plain`) the command prints plain text instead of the UI.

| Key | Action |
|---|---|
| `↑` `↓` `Enter` | move, expand or collapse |
| `d` / `v` / `e` | details / verify signal / edit (query editor for signals, draft form for entities) |
| `/` then `n` `p` | search, next and previous match |
| `1` `2` `3` | Tree, Timeline, Changes tabs (`Ctrl+1`..`3` where the terminal sends them) |
| `r` / `j` / `+` `-` | refresh / toggle auto-jump to escalations / poll interval ±10 s |
| `F1` / `F2` | all keys / filterable actions (includes Back to models when launched from the list) |
| `Esc` / `q` | close panel / quit (asks before discarding unsaved drafts) |

**Timeline.** Rows fill in as their history arrives; rows still loading show a moving bar and the progress line counts them. Color shows the worst health state in each time slot. For a selected signal, bar height shows its value. `·` marks time without data. Use Prev/Next to step through events, **Open tree at time** to see the model at that time (the status bar then says **Snapshot**), and **Compare** to diff two times.

**Changes.** Edits are local drafts until you apply them. Save (or Overwrite) writes a draft to a JSON file; Restore loads one back. To apply, press **Update draft** to take the form edits into the draft, then **Prepare apply** reads the live resource and shows the tenant, subscription and changed fields, then **Apply prepared** writes after you type the resource name, and reads the result back.

**Nested models.** A reference to another health model starts collapsed. Expanding it loads that one model with your own credentials; a missing permission only affects that row. Repeated references share one cached copy, and cycles are marked.

## Development

From `src/healthmodels-plus`:

```bash
uv sync --locked
uv run --locked python -m pytest
uv build --wheel
```

The test run rewrites ten screenshot SVGs under `azext_healthmodels_plus/tui/tests/` (Textual clip IDs and poll countdowns change); the tests do not compare them. Restore them before committing:

```bash
git checkout -- 'azext_healthmodels_plus/tui/tests/*.svg'
```

Install the wheel into an isolated extension directory so your regular extensions stay untouched, and run the commands in the same shell:

```bash
AZURE_EXTENSION_DIR="$(mktemp -d)"
export AZURE_EXTENSION_DIR
az extension add --source dist/healthmodels_plus-0.4.1-py3-none-any.whl --yes
az monitor health-models layout --help
az monitor health-models tui --help
```

`docs/tui/NEXT_STEPS.md` lists the open items of the terminal monitor and its manual QA steps (live read-only pass, Apply on a fake backend).
