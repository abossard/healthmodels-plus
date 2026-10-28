# health-models-tui: state and next steps

Written 2026-10-25 for the next agent working on `az health-models-tui`. Moved from always_on_v2 on 2026-10-27: the code is now `azext_healthmodels_plus/tui/` in the `healthmodels-plus` extension, and the always_on_v2 notes below are history.

## State

- Merged into `main` (fast-forward, pushed to `abossard/always-on-v2`); work branch `claude/health-models-tui-0-4-0`. Version 0.4.0 (`pyproject.toml`, `HISTORY.rst`). The README install now builds 0.4.0.
- Tests: `cd src/healthmodels-plus && uv run --locked python -m pytest -q` gives 787 passed (770 of them for the TUI). The run rewrites the screenshot SVGs under `azext_healthmodels_plus/tui/tests/`; restore them before committing: `git checkout -- 'src/healthmodels-plus/*.svg'`.
- Verified on the last commits:
  - Read-only live pass over all 13 models in the user's subscription: every tree loads, every timeline row loads, signal verify works, Prepare is a no-op.
  - Live read-only QA: consumer display names in Prepare, Help at 80x24, future-time refusal, snapshots of expanded nested models.
  - Apply path end to end against a local fake backend (no Azure, network blocked).
  - Packaging: an installed wheel reports `azext.minCliCoreVersion` 2.61.0.
  - Copilot (GPT 6.1) and subagent simplicity reviews after every fix round.

## Rules from the user

- Live Azure is read-only: never Apply, never run a writing `az` command. Prepare is fine (it only reads). Re-confirmed 2026-10-25.
- Gentle changes, simple code, Ousterhout design, parametrized tests, match existing patterns, do only what is asked.
- Commit, push and merge to `main` are fine; it is the user's own repo (user, 2026-10-25). Work on a branch first. No AI attribution, no em dashes.
- The working tree also has 29 modified or untracked files outside `src/health-models-tui` (infra, workflows, other apps). They are not part of this work; never commit them with it.

## Left to do

1. **Open items from the last QA, not fixed:**
   - "Not confirmed: ... wanted 'X', live 'Y'" prints whole values, so a long query fills the result box. The box scrolls.
   - Opening a snapshot waits until the model of every timeline row has loaded, not only the selected path. That is the cost of loading expanded nested models beside the selected row.
   - A snapshot covers the rows that were visible when the timeline was requested. A nested model expanded after that shows `[unloaded]` until the timeline refreshes.
   - The 256-character name limit counts Python code points. How the API counts emoji is unknown.
   - Prepare with two or more shared consumers was only tested with unit tests, not live.
   - Log Analytics verification reads the last row of the first table and sends `timeGrain` as the query timespan. Both match the SDK docs but are not confirmed against how the service evaluates.
   - An Azure metric signal without `timeGrain` is queried without an `interval`, so the API picks the grain.
   - Rare slow quit (seen once in six live quits): the process waited about 5 minutes for a background network read to time out before exiting. Python joins the poller's worker threads at exit; a shorter read timeout or not joining them would fix it.

## Decided, do not reopen without the user

- Timeline state stays in `watch/app.py` (about 500 lines). Two of three reviewers advised against moving it, and the user agreed on 2026-10-25. Its pure formatting functions already live in `watch/timeline.py`.
- No live Apply test (user, 2026-10-25).
- `_fit_cell_width` stays: Rich's `Text.truncate` puts the padding before the ellipsis for wide characters, which changes labels.
- Old plan docs (MCP, SDK integration, search and signal debugging plans) were deleted with the user's approval; git history has them.

## What to test after a change

- **Suite** as above, plus the screenshot restore.
- **Live read-only pass.** A headless script drove the real `HealthWatchApp` with the real `CloudHealthClient` for each model. Per model it recorded:
  - load time and root state;
  - entity and signal counts;
  - one signal verify;
  - timeline rows loaded and failed;
  - the Prepare message on an unchanged entity.

  It never pressed Apply. It lived at `/tmp/qa-allmodels.bIGspm/tui_pass.py` (temporary, may be gone). Recreate it from `tests/test_workspace_routes_e2e.py` patterns if needed.
- **Apply QA on a fake backend.** The last harness is in `/var/folders/l9/k78n64d137x240x8680jjsd00000gn/T/tmp.BafAQEMBaZ/` (temporary):
  - `launch.py` blocks `azure.cli` imports and network connects.
  - `fake_client.py` subclasses the client. A `control.json` sets the write outcome (ok, forbidden 403, timeout, differs), a delay, `readback_fail` and `concurrent_impact`.
  - Writes are logged to `writes.jsonl`.
  - Drive it in tmux and check the write log for every write claim.

  Scenarios that must keep passing:
  - Happy path writes once, only the changed field, and the draft ends clean.
  - Wrong confirmation, no Prepare, and a draft changed since Prepare all write nothing.
  - While a write runs (delay 8 to 12 s):
    - Prepare, Apply, Discard and Restore are refused, and Enter twice gives one write.
    - q and Ctrl+Q do not quit.
    - Typed edits survive, including an edit back to the live value.
  - 403, timeout and differs:
    - the error stays visible;
    - the next Prepare offers the remaining change;
    - another writer's change is not reverted.
  - A timeout where the write landed ends clean.
  - A read-back failure tells the user to discard and edit again.
  - Typed values are written exactly (spaces, emoji, quotes, newlines, 3000 chars).
  - At 80x24, 80x30, 80x31, 80x32 and 120x40, with a long 403 and with 9 drafts, at least one input is visible, and the result box scrolls.
  - A signal definition draft writes `update_signal_definition` with only `queryText`.
  - Unconfirmed fields show "wanted ..., live ..." and only the plan's own fields. A write whose own fields landed is applied even if another writer changed a different field.
  - Display and icon names of 257 characters are refused at Update draft, and at Prepare after a Restore.
  - Future times are refused by Open tree at time and by Compare.
- **Not covered yet:**
  - real Azure write semantics (PUT acceptance, 202, ETags/If-Match, RBAC, real error payloads);
  - terminals other than tmux;
  - signal definition drafts on small screens;
  - relationship drafts (no UI exists for them).

## Code map

Paths are relative to `azext_healthmodels_plus/tui/`.

- `watch/app.py`: the Textual app. Tree, Timeline and Changes tabs; snapshots and compare; the Prepare and Apply workers.
- `watch/poller.py`: thread pool and load state machine, leases for history ranges and snapshots, nested-model queue (explicit navigation first, then visible distance).
- `watch/apply.py`: `ApplyCoordinator`. One prepared plan, used once; plan numbering and publishing under one lock; the scope and endpoint checks are left to the client.
- `watch/draft_store.py`: drafts, Save and Restore, and `rebase`, which replays only the user's own edits onto a new live read-back.
- `watch/changes.py`: the Changes pane. It remembers what it filled, so typed values survive re-renders.
- `watch/timeline.py`: the timeline pane and pure formatting functions.
- `domain/`: parsing, history bucketing, drafts. `client/`: REST client, typed errors, signal query execution.

## Notes on the user's models (read-only findings)

- The demo models are Unhealthy because a PostgreSQL server is stopped.
- Some model identities lack RBAC on their targets.
- `contoso-ahm` has inverted availability thresholds.
- `Connectivity` has no signals.
- The `ahm-pw-*` models look like test leftovers.
