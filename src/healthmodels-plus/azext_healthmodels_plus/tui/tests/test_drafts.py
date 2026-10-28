from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest

from azext_healthmodels_plus.tui.domain.drafts import (
    ResourceTarget,
    apply_field_edit,
    build_apply_plan,
    collect_signal_definition_consumers,
    diff_documents,
    normalize_known_readonly_fields,
    parse_field_path,
    validate_target_payload,
)
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.watch.draft_store import DraftStore, DraftTestResult


def _target(kind: str = "signal_definition") -> ResourceTarget:
    return ResourceTarget(
        model=ModelKey("https://management.azure.com", "sub", "rg", "model"),
        kind=kind,  # type: ignore[arg-type]
        name="signal-cpu",
    )


def _baseline() -> dict[str, object]:
    return {
        "name": "signal-cpu",
        "properties": {
            "signalKind": "PrometheusMetricsQuery",
            "queryText": "rate(original[5m])",
            "unknownServerField": {"nested": 1},
        },
        "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.CloudHealth/healthModels/model/signalDefinitions/signal-cpu",
    }


def test_apply_field_edit_preserves_input_aliases_and_unknown_fields() -> None:
    baseline = _baseline()
    edited = apply_field_edit(baseline, parse_field_path("properties.queryText"), "rate(edited[5m])")

    assert baseline["properties"]["queryText"] == "rate(original[5m])"  # type: ignore[index]
    assert edited["properties"]["unknownServerField"] == {"nested": 1}  # type: ignore[index]


def test_apply_field_edit_supports_signal_list_path() -> None:
    baseline = {
        "name": "entity",
        "properties": {
            "signalGroups": {
                "default": {
                    "signals": [
                        {"name": "sig-1", "queryText": "old"},
                    ],
                },
            },
        },
    }
    edited = apply_field_edit(
        baseline,
        parse_field_path("properties.signalGroups.default.signals[0].queryText"),
        "new",
    )
    assert edited["properties"]["signalGroups"]["default"]["signals"][0]["queryText"] == "new"  # type: ignore[index]
    assert baseline["properties"]["signalGroups"]["default"]["signals"][0]["queryText"] == "old"  # type: ignore[index]


@pytest.mark.parametrize(
    "path",
    [
        "id",
        "properties.provisioningState",
        "properties.healthState",
        "properties.discoveredBy",
        "properties.signalGroups.default.signals[0].status",
        "properties.signalGroups.default.signals[0].status.healthState",
        "properties.signalGroups.default.resourceHealth.status",
        "properties.SignalGroups.default.ResourceHealth.status.healthState",
    ],
)
def test_apply_field_edit_rejects_known_readonly_paths(path: str) -> None:
    baseline = {
        "id": "/x",
        "properties": {
            "provisioningState": "Succeeded",
            "healthState": "Healthy",
            "signalGroups": {"default": {"signals": [{"status": {"healthState": "Healthy"}}]}},
        },
    }
    with pytest.raises(ValueError, match="read-only"):
        apply_field_edit(baseline, parse_field_path(path), "x")


@pytest.mark.parametrize(
    "path,match",
    [
        ("properties.HealthState", "read-only"),
        ("properties.DISCOVEREDBY", "read-only"),
        (" properties.healthState", "whitespace"),
        ("properties. healthState", "Invalid field path"),
    ],
)
def test_parse_and_edit_reject_case_variants_and_whitespace_bypass(path: str, match: str) -> None:
    baseline = {"name": "signal-cpu", "properties": {"healthState": "Healthy", "discoveredBy": "agent-a"}}
    if path.startswith(" "):
        with pytest.raises(ValueError, match=match):
            parse_field_path(path)
        return
    with pytest.raises(ValueError, match=match):
        apply_field_edit(baseline, parse_field_path(path), "x")


def test_draft_store_stale_test_result_is_rejected() -> None:
    store = DraftStore()
    target = _target()
    store.get_or_create(target, _baseline())
    updated = store.update_field(target, "properties.queryText", "rate(one[5m])")
    store.record_test_result(
        target,
        DraftTestResult(revision=updated.revision, summary="ok", recorded_at_utc=datetime.now(UTC)),
    )
    newer = store.update_field(target, "properties.queryText", "rate(two[5m])")
    assert store.is_test_stale(target) is True
    with pytest.raises(ValueError, match="Stale test result"):
        store.record_test_result(
            target,
            DraftTestResult(revision=newer.revision - 1, summary="old", recorded_at_utc=datetime.now(UTC)),
        )


@pytest.mark.parametrize(
    "kind,payload,expected_error",
    [
        (
            "relationship",
            {"name": "signal-cpu", "properties": {"parentEntityName": "", "childEntityName": "child"}},
            "parentEntityName",
        ),
        (
            "signal_definition",
            {"name": "signal-cpu", "properties": {"signalKind": ""}},
            "signalKind",
        ),
        (
            "entity",
            {"name": "other", "properties": {}},
            "does not match target",
        ),
    ],
)
def test_validate_target_payload(kind: str, payload: dict[str, object], expected_error: str) -> None:
    result = validate_target_payload(_target(kind), payload)
    assert result.is_valid is False
    assert any(expected_error in message for message in result.errors)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, False])
def test_validate_target_payload_rejects_non_finite_or_bool_health_objective(value: object) -> None:
    payload = {"name": "signal-cpu", "properties": {"healthObjective": value}}
    result = validate_target_payload(_target("entity"), payload)
    assert result.is_valid is False
    assert any("healthObjective" in message for message in result.errors)


@pytest.mark.parametrize("value", [0, 0.0, 100, 100.0, 42.5])
def test_validate_target_payload_accepts_finite_health_objective_boundaries(value: float) -> None:
    payload = {"name": "signal-cpu", "properties": {"healthObjective": value}}
    result = validate_target_payload(_target("entity"), payload)
    assert result.is_valid is True


def test_build_apply_plan_preserves_unknown_fields() -> None:
    baseline = _baseline()
    edited = apply_field_edit(baseline, ("properties", "queryText"), "rate(edited[5m])")
    plan = build_apply_plan(
        target=_target(),
        revision=2,
        edited_resource=edited,
        expected_baseline=baseline,
        live_baseline=baseline,
        generated_at=datetime.now(UTC),
    )
    assert plan.changes
    assert plan.edited_resource["properties"]["unknownServerField"] == {"nested": 1}  # type: ignore[index]


def test_save_restore_overwrite_and_failure_paths(tmp_path: Path) -> None:
    store = DraftStore()
    target = _target()
    store.get_or_create(target, _baseline())
    store.update_field(target, "properties.queryText", "rate(new[5m])")
    save_path = tmp_path / "draft.json"
    store.export_to_json(target, save_path)

    with pytest.raises(FileExistsError):
        store.export_to_json(target, save_path, overwrite=False)

    restored_store = DraftStore()
    restored = restored_store.restore_from_json(
        target=target,
        expected_baseline=_baseline(),
        path=save_path,
    )
    assert restored.edited["properties"]["queryText"] == "rate(new[5m])"  # type: ignore[index]
    assert restored.saved_revision == restored.revision

    mismatched_target = ResourceTarget(target.model, "signal_definition", "different")
    with pytest.raises(ValueError, match="identity mismatch"):
        restored_store.restore_from_json(
            target=mismatched_target,
            expected_baseline=_baseline(),
            path=save_path,
        )


def test_store_get_list_discard_unsaved_and_accept_applied() -> None:
    store = DraftStore()
    target = _target()
    assert store.get(target) is None
    created = store.get_or_create(target, _baseline())
    assert created.saved_revision == 0
    assert store.has_unsaved_changes(target) is False

    store.update_field(target, "properties.queryText", "rate(changed[5m])")
    assert store.has_unsaved_changes(target) is True

    listed = store.list_targets()
    assert listed == (target,)
    snapshots = store.list_snapshots()
    assert len(snapshots) == 1

    readback = _baseline()
    readback["properties"]["queryText"] = "rate(applied[5m])"  # type: ignore[index]
    accepted = store.accept_applied(target, readback)
    assert accepted.baseline["properties"]["queryText"] == "rate(applied[5m])"  # type: ignore[index]
    assert store.has_unsaved_changes(target) is False

    assert store.discard(target) is True
    assert store.get(target) is None


@pytest.mark.parametrize(
    ("live_query", "edit_during_write", "expected_query"),
    [
        ("rate(original[5m])", None, "rate(mine[5m])"),  # the write did not land: the edit stays
        ("rate(mine[5m])", None, "rate(mine[5m])"),  # it landed: nothing left to change
        ("rate(mine[5m])", "rate(original[5m])", "rate(original[5m])"),  # set back to the old live value while writing
    ],
)
def test_rebase_replays_only_the_drafts_own_edits_on_the_new_live_state(
    live_query: str, edit_during_write: str | None, expected_query: str,
) -> None:
    store = DraftStore()
    target = _target()
    store.get_or_create(target, _baseline())
    written = normalize_known_readonly_fields(store.update_field(target, "properties.queryText", "rate(mine[5m])").edited)
    if edit_during_write is not None:
        store.update_field(target, "properties.queryText", edit_during_write)
    live = _baseline()
    live["properties"] = {**live["properties"], "queryText": live_query, "unknownServerField": {"nested": 2}}  # type: ignore[dict-item]

    rebased = store.rebase(target, live, written)

    assert rebased.baseline == live
    assert rebased.edited["properties"]["queryText"] == expected_query  # type: ignore[index]
    assert rebased.edited["properties"]["unknownServerField"] == {"nested": 2}  # someone else's change is not reverted


@pytest.mark.parametrize("baseline_query", [{"queryText": "rate(original[5m])"}, {}])  # a metric signal has no query text
def test_revert_field_restores_the_baseline_field_and_keeps_other_edits(baseline_query: dict[str, str]) -> None:
    store = DraftStore()
    target = _target()
    baseline = {"name": "signal-cpu", "properties": {"signalKind": "PrometheusMetricsQuery", **baseline_query}}
    store.get_or_create(target, baseline)
    store.update_field(target, "properties.displayName", "Mine")
    store.update_field(target, "properties.queryText", "rate(changed[5m])")

    store.revert_field(target, "properties.queryText")

    assert store.get(target).edited == {**baseline, "properties": {**baseline["properties"], "displayName": "Mine"}}


def test_restore_rejects_wrong_model_and_unsaved_overwrite(tmp_path: Path) -> None:
    store = DraftStore()
    target = _target()
    store.get_or_create(target, _baseline())
    store.update_field(target, "properties.queryText", "rate(local-change[5m])")
    save_path = tmp_path / "draft.json"
    store.export_to_json(target, save_path, overwrite=True)
    store.update_field(target, "properties.queryText", "rate(unsaved-after-export[5m])")

    with pytest.raises(ValueError, match="unsaved draft"):
        store.restore_from_json(target=target, expected_baseline=_baseline(), path=save_path)

    other_model_target = ResourceTarget(
        model=ModelKey("https://management.azure.com", "sub", "other-rg", "model"),
        kind="signal_definition",
        name="signal-cpu",
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        store.restore_from_json(
            target=other_model_target,
            expected_baseline=_baseline(),
            path=save_path,
            confirm_overwrite_unsaved=True,
        )


def test_restore_rejects_tampered_readonly_fields(tmp_path: Path) -> None:
    store = DraftStore()
    target = _target()
    baseline = {
        "id": "/entity/1",
        "name": "signal-cpu",
        "properties": {
            "signalKind": "PrometheusMetricsQuery",
            "queryText": "rate(original[5m])",
            "provisioningState": "Succeeded",
            "discoveredBy": "agent-a",
            "signalGroups": {"default": {"signals": [{"name": "sig-1", "status": {"healthState": "Healthy"}}]}},
        },
    }
    store.get_or_create(target, baseline)
    save_path = tmp_path / "tampered.json"
    save_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint": target.model.endpoint,
                    "subscription": target.model.subscription,
                    "group": target.model.group,
                    "name": target.model.name,
                    "kind": target.kind,
                    "resourceName": target.name,
                },
                "draft": {
                    "baseline": baseline,
                    "edited": {
                        **baseline,
                        "id": "/entity/2",
                        "properties": {
                            **baseline["properties"],
                            "provisioningState": "Updating",
                            "discoveredBy": "agent-b",
                            "signalGroups": {
                                "default": {
                                    "signals": [{"name": "sig-1", "status": {"healthState": "Unhealthy"}}]
                                }
                            },
                        },
                    },
                    "revision": 1,
                },
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    before = store.get(target)
    with pytest.raises(ValueError, match="tampered read-only fields"):
        store.restore_from_json(
            target=target,
            expected_baseline=baseline,
            path=save_path,
            confirm_overwrite_unsaved=True,
        )
    after = store.get(target)
    assert before is not None and after is not None
    assert after.edited == before.edited


def test_restore_rejects_case_variant_readonly_tampering(tmp_path: Path) -> None:
    store = DraftStore()
    target = _target()
    baseline = {
        "id": "/entity/1",
        "name": "signal-cpu",
        "properties": {
            "signalKind": "PrometheusMetricsQuery",
            "queryText": "rate(original[5m])",
            "discoveredBy": "agent-a",
        },
    }
    store.get_or_create(target, baseline)
    save_path = tmp_path / "tampered-case-variant.json"
    save_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint": target.model.endpoint,
                    "subscription": target.model.subscription,
                    "group": target.model.group,
                    "name": target.model.name,
                    "kind": target.kind,
                    "resourceName": target.name,
                },
                "draft": {
                    "baseline": baseline,
                    "edited": {
                        **baseline,
                        "properties": {
                            " DiscoveredBy ": "agent-b",
                            "signalKind": "PrometheusMetricsQuery",
                            "queryText": "rate(original[5m])",
                        },
                    },
                    "revision": 1,
                },
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    before = store.get(target)
    with pytest.raises(ValueError, match="tampered read-only fields"):
        store.restore_from_json(
            target=target,
            expected_baseline=baseline,
            path=save_path,
            confirm_overwrite_unsaved=True,
        )
    after = store.get(target)
    assert before is not None and after is not None
    assert after.edited == before.edited


def test_restore_allows_unchanged_readonly_fields(tmp_path: Path) -> None:
    baseline = {
        "id": "/entity/1",
        "name": "signal-cpu",
        "properties": {
            "signalKind": "PrometheusMetricsQuery",
            "queryText": "rate(original[5m])",
            "provisioningState": "Succeeded",
            "discoveredBy": "agent-a",
        },
    }
    target = _target()
    payload = {
        "target": {
            "endpoint": target.model.endpoint,
            "subscription": target.model.subscription,
            "group": target.model.group,
            "name": target.model.name,
            "kind": target.kind,
            "resourceName": target.name,
        },
        "draft": {
            "baseline": baseline,
            "edited": {
                **baseline,
                "properties": {**baseline["properties"], "queryText": "rate(changed[5m])"},
            },
            "revision": 1,
        },
    }
    path = tmp_path / "unchanged-readonly.json"
    path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    store = DraftStore()
    restored = store.restore_from_json(
        target=target,
        expected_baseline=baseline,
        path=path,
        confirm_overwrite_unsaved=True,
    )
    assert restored.edited["properties"]["queryText"] == "rate(changed[5m])"  # type: ignore[index]


def test_export_rejects_non_finite_json_numbers(tmp_path: Path) -> None:
    target = _target("entity")
    baseline = {"name": "signal-cpu", "properties": {"displayName": "Entity A", "healthObjective": 50}}
    store = DraftStore()
    store.get_or_create(target, baseline)
    with pytest.raises(ValueError, match="Invalid field update|finite number"):
        store.update_field(target, "properties.healthObjective", float("nan"))
    export_path = tmp_path / "invalid.json"
    assert export_path.exists() is False


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, False, -1, 101, "abc", None],
)
def test_update_field_rejects_invalid_health_objective_without_mutating_state(value: object) -> None:
    target = _target("entity")
    baseline = {"name": "signal-cpu", "properties": {"displayName": "Entity A", "healthObjective": 50}}
    store = DraftStore()
    created = store.get_or_create(target, baseline)
    with pytest.raises(ValueError, match="Invalid field update|healthObjective"):
        store.update_field(target, "properties.healthObjective", value)
    current = store.get(target)
    assert current is not None
    assert current.revision == created.revision
    assert current.edited["properties"]["healthObjective"] == 50  # type: ignore[index]


@pytest.mark.parametrize("value", [0, 25.5, 100])
def test_update_field_accepts_valid_health_objective(value: float) -> None:
    target = _target("entity")
    baseline = {"name": "signal-cpu", "properties": {"displayName": "Entity A", "healthObjective": 50}}
    store = DraftStore()
    created = store.get_or_create(target, baseline)
    updated = store.update_field(target, "properties.healthObjective", value)
    assert updated.revision == created.revision + 1
    assert updated.edited["properties"]["healthObjective"] == value  # type: ignore[index]


def test_nested_alias_mutation_does_not_escape_store_snapshot() -> None:
    store = DraftStore()
    target = _target()
    first = store.get_or_create(target, _baseline())
    first.baseline["properties"]["unknownServerField"]["nested"] = 999  # type: ignore[index]
    second = store.get(target)
    assert second is not None
    assert second.baseline["properties"]["unknownServerField"]["nested"] == 1  # type: ignore[index]


def test_build_apply_plan_rejects_changed_live_baseline() -> None:
    baseline = _baseline()
    edited = apply_field_edit(baseline, ("properties", "queryText"), "rate(edited[5m])")
    live_changed = _baseline()
    live_changed["properties"]["unknownServerField"]["nested"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="baseline changed"):
        build_apply_plan(
            target=_target(),
            revision=2,
            edited_resource=edited,
            expected_baseline=baseline,
            live_baseline=live_changed,
            generated_at=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    ("live_name", "path", "value", "rejected"),
    [
        ("x", ("properties", "displayName"), "n" * 257, True),  # a restored file skips Update draft's check
        ("x", ("properties", "icon", "iconName"), "n" * 257, True),
        ("x", ("properties", "displayName"), "n" * 256, False),
        ("n" * 300, ("properties", "queryText"), "rate(edited[5m])", False),  # an unchanged long live name is not this edit's
    ],
)
def test_build_apply_plan_limits_changed_names(live_name: str, path: tuple[str, ...], value: str, rejected: bool) -> None:
    baseline = _baseline()
    baseline["properties"]["displayName"] = live_name  # type: ignore[index]
    edited = apply_field_edit(baseline, path, value)
    with pytest.raises(ValueError, match="at most 256 characters") if rejected else nullcontext():
        build_apply_plan(
            target=_target(),
            revision=1,
            edited_resource=edited,
            expected_baseline=baseline,
            live_baseline=baseline,
            generated_at=datetime.now(UTC),
        )


def test_diff_documents_tracks_list_leaf_paths() -> None:
    before = {"properties": {"signalGroups": {"default": {"signals": [{"queryText": "old"}]}}}}
    after = {"properties": {"signalGroups": {"default": {"signals": [{"queryText": "new"}]}}}}
    diffs = diff_documents(before, after)
    assert len(diffs) == 1
    assert diffs[0].path == ("properties", "signalGroups", "default", "signals", 0, "queryText")


def test_normalize_known_readonly_fields_is_path_scoped() -> None:
    payload = {
        "id": "/id",
        "type": "kind",
        "properties": {
            "healthState": "Healthy",
            "provisioningState": "Succeeded",
            "discoveredBy": "agent-x",
            "tags": {"healthState": "preserve"},
            "signalGroups": {
                "default": {
                    "signals": [{
                        "name": "sig",
                        "queryText": "rate(x[5m])",
                        "status": {"healthState": "Unknown", "reportedAt": "2026-01-01T00:00:00Z"},
                    }],
                },
            },
        },
    }
    normalized = normalize_known_readonly_fields(payload)
    assert "id" not in normalized
    assert "type" not in normalized
    assert "healthState" not in normalized["properties"]  # type: ignore[index]
    assert "discoveredBy" not in normalized["properties"]  # type: ignore[index]
    assert normalized["properties"]["tags"]["healthState"] == "preserve"  # type: ignore[index]
    assert "status" not in normalized["properties"]["signalGroups"]["default"]["signals"][0]  # type: ignore[index]


def test_normalize_ignores_resource_health_status_differences_between_list_and_get() -> None:
    # Live list omits null keys that GET returns, and the status itself is runtime state.
    def entity(status: dict) -> dict:
        return {"name": "e", "properties": {"signalGroups": {"azureResource": {
            "azureResourceId": "/subscriptions/s/rg/x",
            "resourceHealth": {"enabled": "Enabled", "signalName": "rh", "status": status},
        }}}}

    listed = entity({"healthState": "Unknown", "reportedAt": "2026-10-23T15:50:43Z"})
    fetched = entity({"healthState": "Healthy", "reportedAt": "2026-10-24T09:00:00Z", "category": None})
    assert normalize_known_readonly_fields(listed) == normalize_known_readonly_fields(fetched)
    resource_health = normalize_known_readonly_fields(listed)["properties"]["signalGroups"]["azureResource"]["resourceHealth"]
    assert resource_health == {"enabled": "Enabled", "signalName": "rh"}


def test_collect_signal_definition_consumers_from_real_shape() -> None:
    entities = [
        {"name": "a", "properties": {"signalGroups": {"g": {"signals": [{"signalDefinitionName": "def-1"}]}}}},
        {"name": "b", "properties": {"signalGroups": {"g": {"signals": [{"signalDefinitionName": "def-2"}]}}}},
        {"name": "c", "properties": {"signalGroups": {"g": {"signals": [{"signalDefinitionName": "def-1"}]}}}},
    ]
    assert collect_signal_definition_consumers(entities, "def-1") == ("a", "c")
