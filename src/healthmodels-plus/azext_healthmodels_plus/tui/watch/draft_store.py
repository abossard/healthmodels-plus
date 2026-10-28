"""In-memory retained draft store with explicit local save/restore only."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
import tempfile
from typing import Any, Mapping

from azext_healthmodels_plus.tui.domain.drafts import (
    ResourceTarget,
    apply_field_edit,
    diff_documents,
    find_known_readonly_tampering,
    get_in,
    normalize_known_readonly_fields,
    parse_field_path,
    validate_target_payload,
)


@dataclass(frozen=True)
class DraftSnapshot:
    target: ResourceTarget
    baseline: Mapping[str, Any]
    edited: Mapping[str, Any]
    revision: int
    saved_revision: int | None
    tested_revision: int | None
    tested_at_utc: datetime | None
    saved_at_utc: datetime | None
    last_test_summary: str | None


@dataclass(frozen=True)
class DraftTestResult:
    revision: int
    summary: str
    recorded_at_utc: datetime


class DraftStore:
    def __init__(self) -> None:
        self._entries: dict[ResourceTarget, DraftSnapshot] = {}
        self._active: ResourceTarget | None = None

    def get_or_create(self, target: ResourceTarget, baseline: Mapping[str, Any]) -> DraftSnapshot:
        current = self._entries.get(target)
        if current is not None:
            self._active = target
            return self._clone_snapshot(current)
        validation = validate_target_payload(target, baseline)
        if not validation.is_valid:
            raise ValueError(f"Invalid baseline for {target.kind}:{target.name}: {', '.join(validation.errors)}")
        created = DraftSnapshot(
            target=target,
            baseline=deepcopy(dict(baseline)),
            edited=deepcopy(dict(baseline)),
            revision=0,
            saved_revision=0,
            tested_revision=None,
            tested_at_utc=None,
            saved_at_utc=None,
            last_test_summary=None,
        )
        self._entries[target] = created
        self._active = target
        return self._clone_snapshot(created)

    def get(self, target: ResourceTarget) -> DraftSnapshot | None:
        entry = self._entries.get(target)
        if entry is None:
            return None
        return self._clone_snapshot(entry)

    def list_targets(self) -> tuple[ResourceTarget, ...]:
        return tuple(sorted(self._entries.keys(), key=lambda item: (item.kind, item.name)))

    def list_snapshots(self) -> tuple[DraftSnapshot, ...]:
        return tuple(self._clone_snapshot(self._entries[target]) for target in self.list_targets())

    def active_target(self) -> ResourceTarget | None:
        """The most recently opened or edited draft, else the first listed one."""
        if self._active in self._entries:
            return self._active
        targets = self.list_targets()
        return targets[0] if targets else None

    def update_field(self, target: ResourceTarget, path: str, value: Any) -> DraftSnapshot:
        entry = self._require(target)
        edited = apply_field_edit(entry.edited, parse_field_path(path), value)
        validation = validate_target_payload(target, edited)
        if not validation.is_valid:
            raise ValueError(f"Invalid field update: {', '.join(validation.errors)}")
        updated = replace(
            entry, edited=edited, revision=entry.revision + 1,
            tested_revision=None, tested_at_utc=None, last_test_summary=None,
        )
        self._entries[target] = updated
        self._active = target
        return self._clone_snapshot(updated)

    def revert_field(self, target: ResourceTarget, path: str) -> None:
        """Put one field back as the baseline has it, absent included; a draft left without edits is dropped."""
        entry = self._require(target)
        *parents, leaf = parse_field_path(path)
        edited = deepcopy(dict(entry.edited))
        edited_parent, baseline_parent = get_in(edited, tuple(parents)), get_in(entry.baseline, tuple(parents))
        if isinstance(baseline_parent, Mapping) and leaf in baseline_parent:
            edited_parent[leaf] = deepcopy(baseline_parent[leaf])
        elif isinstance(edited_parent, dict):
            edited_parent.pop(leaf, None)
        if edited == entry.baseline:
            self.discard(target)
        elif edited != entry.edited:
            self._entries[target] = replace(
                entry, edited=edited, revision=entry.revision + 1,
                tested_revision=None, tested_at_utc=None, last_test_summary=None,
            )

    def record_test_result(self, target: ResourceTarget, result: DraftTestResult) -> DraftSnapshot:
        entry = self._require(target)
        if result.revision != entry.revision:
            raise ValueError(
                f"Stale test result revision {result.revision}; current revision is {entry.revision}"
            )
        updated = replace(
            entry, tested_revision=result.revision,
            tested_at_utc=result.recorded_at_utc.astimezone(UTC), last_test_summary=result.summary,
        )
        self._entries[target] = updated
        return self._clone_snapshot(updated)

    def export_to_json(self, target: ResourceTarget, path: Path, overwrite: bool = False) -> Path:
        entry = self._require(target)
        validation = validate_target_payload(target, entry.edited)
        if not validation.is_valid:
            raise ValueError(f"Refusing to export invalid draft: {', '.join(validation.errors)}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {path}")
        saved_at = datetime.now(UTC)
        payload = {
            "target": {
                "endpoint": entry.target.model.endpoint,
                "subscription": entry.target.model.subscription,
                "group": entry.target.model.group,
                "name": entry.target.model.name,
                "kind": entry.target.kind,
                "resourceName": entry.target.name,
            },
            "draft": {
                "baseline": entry.baseline,
                "edited": entry.edited,
                "revision": entry.revision,
                "savedRevision": entry.revision,
                "testedRevision": entry.tested_revision,
                "testedAtUtc": entry.tested_at_utc.isoformat() if entry.tested_at_utc else None,
                "savedAtUtc": saved_at.isoformat(),
                "lastTestSummary": entry.last_test_summary,
            },
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

        self._entries[target] = replace(entry, saved_revision=entry.revision, saved_at_utc=saved_at)
        return path

    def restore_from_json(
        self,
        *,
        target: ResourceTarget,
        expected_baseline: Mapping[str, Any],
        path: Path,
        confirm_overwrite_unsaved: bool = False,
    ) -> DraftSnapshot:
        current = self._entries.get(target)
        if current is not None and current.saved_revision != current.revision and not confirm_overwrite_unsaved:
            raise ValueError("Refusing to replace unsaved draft without explicit overwrite confirmation")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid draft file: {path}") from exc
        if not isinstance(raw, dict):
            raise ValueError("Invalid draft file: expected object root")
        target_raw = raw.get("target") or {}
        draft_raw = raw.get("draft") or {}
        if not isinstance(target_raw, dict) or not isinstance(draft_raw, dict):
            raise ValueError("Invalid draft file: missing target or draft object")
        expected_identity = {
            "endpoint": target.model.endpoint.casefold().rstrip("/"),
            "subscription": target.model.subscription.casefold(),
            "group": target.model.group.casefold(),
            "name": target.model.name.casefold(),
            "kind": target.kind,
            "resourceName": target.name.casefold(),
        }
        actual_identity = {
            "endpoint": str(target_raw.get("endpoint", "")).casefold().rstrip("/"),
            "subscription": str(target_raw.get("subscription", "")).casefold(),
            "group": str(target_raw.get("group", "")).casefold(),
            "name": str(target_raw.get("name", "")).casefold(),
            "kind": target_raw.get("kind"),
            "resourceName": str(target_raw.get("resourceName", "")).casefold(),
        }
        if actual_identity != expected_identity:
            raise ValueError("Draft target identity mismatch")
        current_baseline = deepcopy(dict(expected_baseline))
        restored_baseline = draft_raw.get("baseline")
        restored_edited = draft_raw.get("edited", current_baseline)
        if not isinstance(restored_baseline, Mapping) or not isinstance(restored_edited, Mapping):
            raise ValueError("Invalid draft file: baseline and edited must be objects")
        readonly_diffs = find_known_readonly_tampering(restored_baseline, restored_edited)
        if readonly_diffs:
            raise ValueError(
                "Draft file contains tampered read-only fields: "
                + ", ".join(readonly_diffs)
            )
        if restored_baseline != current_baseline:
            raise ValueError("Draft baseline mismatch")
        try:
            revision = int(draft_raw.get("revision", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid draft file: revision must be an integer") from exc
        tested_revision = draft_raw.get("testedRevision")
        tested_at = draft_raw.get("testedAtUtc")
        saved_at = draft_raw.get("savedAtUtc")
        saved_revision = draft_raw.get("savedRevision")
        if saved_revision is not None:
            try:
                saved_revision = int(saved_revision)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid draft file: savedRevision must be an integer") from exc
            if saved_revision > revision:
                raise ValueError("Invalid draft file: savedRevision cannot exceed revision")
        if tested_revision is not None:
            try:
                tested_revision = int(tested_revision)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid draft file: testedRevision must be an integer") from exc
            if tested_revision > revision:
                raise ValueError("Invalid draft file: testedRevision cannot exceed revision")
        saved_at_utc = datetime.fromisoformat(saved_at) if saved_at else None
        if saved_revision is None and saved_at_utc:
            saved_revision = revision
        restored = DraftSnapshot(
            target=target,
            baseline=current_baseline,
            edited=deepcopy(restored_edited),
            revision=revision,
            saved_revision=saved_revision,
            tested_revision=tested_revision,
            tested_at_utc=datetime.fromisoformat(tested_at) if tested_at else None,
            saved_at_utc=saved_at_utc,
            last_test_summary=draft_raw.get("lastTestSummary"),
        )
        validation = validate_target_payload(target, restored.edited)
        if not validation.is_valid:
            raise ValueError(f"Invalid draft file payload: {', '.join(validation.errors)}")
        self._entries[target] = restored
        self._active = target
        return self._clone_snapshot(restored)

    def is_test_stale(self, target: ResourceTarget) -> bool:
        entry = self._require(target)
        return entry.tested_revision != entry.revision

    def has_unsaved_changes(self, target: ResourceTarget) -> bool:
        entry = self._require(target)
        return entry.saved_revision != entry.revision

    def discard(self, target: ResourceTarget) -> bool:
        removed = self._entries.pop(target, None)
        return removed is not None

    def accept_applied(self, target: ResourceTarget, readback: Mapping[str, Any]) -> DraftSnapshot:
        entry = self._require(target)
        validation = validate_target_payload(target, readback)
        if not validation.is_valid:
            raise ValueError(f"Invalid readback for {target.kind}:{target.name}: {', '.join(validation.errors)}")
        accepted = replace(
            entry, baseline=deepcopy(dict(readback)), edited=deepcopy(dict(readback)),
            saved_revision=entry.revision, saved_at_utc=datetime.now(UTC),
        )
        self._entries[target] = accepted
        return self._clone_snapshot(accepted)

    def rebase(self, target: ResourceTarget, baseline: Mapping[str, Any], written: Mapping[str, Any]) -> DraftSnapshot:
        """Replay the fields edited since the old baseline or since the write on a new live baseline; the rest follow live."""
        entry = self._require(target)
        edited = dict(baseline)
        owned = diff_documents(entry.baseline, entry.edited) + diff_documents(
            written, normalize_known_readonly_fields(entry.edited),
        )
        try:
            for change in owned:
                edited = apply_field_edit(edited, change.path, change.after)
        except ValueError:  # the live shape no longer fits an edit: keep the edits whole, the next Prepare shows the diff
            edited = entry.edited
        updated = replace(entry, baseline=deepcopy(dict(baseline)), edited=deepcopy(dict(edited)))
        self._entries[target] = updated
        return self._clone_snapshot(updated)

    def _require(self, target: ResourceTarget) -> DraftSnapshot:
        if target not in self._entries:
            raise KeyError(f"No draft retained for {target.kind}:{target.name}")
        return self._entries[target]

    def _clone_snapshot(self, entry: DraftSnapshot) -> DraftSnapshot:
        return replace(entry, baseline=deepcopy(dict(entry.baseline)), edited=deepcopy(dict(entry.edited)))
