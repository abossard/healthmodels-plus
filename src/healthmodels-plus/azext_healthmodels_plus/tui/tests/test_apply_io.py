from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from azext_healthmodels_plus.tui.client.errors import AuthenticationError
from azext_healthmodels_plus.tui.domain.drafts import ApplyPlan, diff_documents, normalize_known_readonly_fields
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
import azext_healthmodels_plus.tui.watch.apply as apply_module
from azext_healthmodels_plus.tui.watch.apply import (
    ApplyResult,
    ApplyCoordinator,
    ConnectionScopeDescription,
    ApplyOutcomeKind,
)


@pytest.fixture
def client() -> MagicMock:
    mocked = MagicMock()
    mocked.arm_endpoint = "https://management.azure.com"
    mocked.for_model.return_value = mocked
    mocked.list_entities.return_value = []
    mocked.get_connection_scope.return_value = {
        "arm_endpoint": "https://management.azure.com",
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "subscription_name": "contoso-sub",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "tenant_name": "contoso",
    }
    return mocked


def _model() -> ModelKey:
    return ModelKey(
        endpoint="https://management.azure.com",
        subscription="00000000-0000-0000-0000-000000000000",
        group="rg",
        name="model",
    )


def _coordinator(client: MagicMock, *, revision: int = 1, live: bool = True) -> ApplyCoordinator:
    return ApplyCoordinator(
        client,
        current_revision_supplier=lambda _target: revision,
        live_scope_supplier=lambda _target: live,
    )


def _target(kind: str):
    from azext_healthmodels_plus.tui.domain.drafts import ResourceTarget

    return ResourceTarget(model=_model(), kind=kind, name="res-1")


@pytest.mark.parametrize(
    ("kind", "read_method", "write_method"),
    [
        ("entity", "get_entity", "update_entity"),
        ("signal_definition", "get_signal_definition", "update_signal_definition"),
        ("relationship", "get_relationship", "update_relationship"),
    ],
)
def test_apply_routes_read_and_write_by_named_resource_kind(
    client: MagicMock,
    kind: str,
    read_method: str,
    write_method: str,
) -> None:
    properties_by_kind = {
        "entity": {"displayName": "before", "healthState": "Healthy", "custom": {"k": 1}},
        "signal_definition": {"displayName": "before", "signalKind": "AzureResourceMetric", "custom": {"k": 1}},
        "relationship": {"parentEntityName": "parent", "childEntityName": "child", "custom": {"k": 1}},
    }
    edited_properties_by_kind = {
        "entity": {"displayName": "after", "healthState": "Unknown", "custom": {"k": 1}},
        "signal_definition": {"displayName": "after", "signalKind": "AzureResourceMetric", "custom": {"k": 1}},
        "relationship": {"parentEntityName": "parent", "childEntityName": "child-2", "custom": {"k": 1}},
    }
    baseline = {
        "id": "/ignored",
        "type": "ignored",
        "name": "res-1",
        "properties": properties_by_kind[kind],
    }
    edited = {
        "name": "res-1",
        "properties": edited_properties_by_kind[kind],
    }
    getattr(client, read_method).side_effect = [baseline, baseline, edited]
    getattr(client, write_method).return_value = edited
    coordinator = _coordinator(client, revision=7)
    target = _target(kind)

    plan = coordinator.prepare_plan(
        target=target,
        revision=7,
        edited_resource=edited,
        draft_live_baseline=baseline,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name=target.name,
    )

    assert result.outcome is ApplyOutcomeKind.APPLIED
    assert result.attempted_write is True
    getattr(client, read_method).assert_any_call("rg", "model", "res-1")
    getattr(client, write_method).assert_called_once()
    sent_payload = getattr(client, write_method).call_args.args[3]
    assert "id" not in sent_payload
    assert "type" not in sent_payload
    if kind == "relationship":
        assert sent_payload["properties"]["childEntityName"] == "child-2"
    else:
        assert sent_payload["properties"]["displayName"] == "after"
    assert sent_payload["properties"]["custom"] == {"k": 1}


@pytest.mark.parametrize(
    ("is_live", "revision", "current_revision", "confirmation", "expected_message"),
    [
        (False, 1, 1, "res-1", "current live connected scope"),
        (True, 1, 2, "res-1", "obsolete"),
        (True, 1, 1, "wrong-name", "confirmation mismatch"),
    ],
)
def test_no_writes_on_cancel_history_or_stale_plan(
    client: MagicMock,
    is_live: bool,
    revision: int,
    current_revision: int,
    confirmation: str,
    expected_message: str,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_entity.return_value = baseline
    coordinator = _coordinator(client, revision=current_revision, live=is_live)
    target = _target("entity")

    plan_or_rejected = coordinator.prepare_plan(
        target=target,
        revision=revision,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )

    if isinstance(plan_or_rejected, ApplyResult):
        result = plan_or_rejected
    else:
        result = coordinator.apply_confirmed(
            plan_or_rejected,
            confirmation_name=confirmation,
        )

    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert result.attempted_write is False
    assert expected_message in result.message
    client.update_entity.assert_not_called()


def test_no_write_when_live_baseline_changes_after_plan_prepare(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    changed_live = {"name": "res-1", "properties": {"displayName": "changed elsewhere"}}
    client.get_entity.return_value = changed_live
    coordinator = _coordinator(client, revision=5)

    plan_or_rejected = coordinator.prepare_plan(
        target=_target("entity"),
        revision=5,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert isinstance(plan_or_rejected, ApplyResult)
    assert plan_or_rejected.outcome is ApplyOutcomeKind.REJECTED
    client.update_entity.assert_not_called()


def test_prepare_rejects_noop_plan(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "same"}}
    client.get_entity.return_value = baseline
    coordinator = _coordinator(client, revision=1)

    plan_or_rejected = coordinator.prepare_plan(
        target=_target("entity"),
        revision=1,
        edited_resource=baseline,
        draft_live_baseline=baseline,
    )

    assert isinstance(plan_or_rejected, ApplyResult)
    assert plan_or_rejected.outcome is ApplyOutcomeKind.REJECTED
    assert "No writable changes" in plan_or_rejected.message


@pytest.mark.parametrize(
    "error", [RuntimeError("scope failure"), ValueError("Nested model uses a different ARM endpoint")],
)
def test_prepare_rejects_when_for_model_fails(client: MagicMock, error: Exception) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.for_model.side_effect = error
    coordinator = _coordinator(client, revision=1)

    result = coordinator.prepare_plan(
        target=_target("entity"),
        revision=1,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )

    assert isinstance(result, ApplyResult)
    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "Unable to resolve scoped client for apply target" in result.message
    assert str(error) in result.message
    client.update_entity.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Connected scope is incomplete in Azure CLI profile"),
        ValueError("Azure CLI profile returned a different subscription than the scoped client"),
        RuntimeError("subscription not found in profile"),
    ],
)
def test_prepare_rejects_when_connection_scope_cannot_be_verified(client: MagicMock, error: Exception) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_connection_scope.side_effect = error
    coordinator = _coordinator(client, revision=1)

    result = coordinator.prepare_plan(
        target=_target("entity"),
        revision=1,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )

    assert isinstance(result, ApplyResult)
    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "Unable to verify connected tenant/subscription scope before apply" in result.message
    assert str(error) in result.message
    client.update_entity.assert_not_called()


def test_describe_connection_scope_returns_whitelisted_values(client: MagicMock) -> None:
    coordinator = _coordinator(client)

    scope = coordinator.describe_connection_scope(_target("entity"))

    assert isinstance(scope, ConnectionScopeDescription)
    assert scope.arm_endpoint == "https://management.azure.com"
    assert scope.subscription_id == "00000000-0000-0000-0000-000000000000"
    assert scope.subscription_name == "contoso-sub"
    assert scope.tenant_id == "22222222-2222-2222-2222-222222222222"
    assert scope.tenant_name == "contoso"


def test_apply_surfaces_rejected_for_403_and_reads_back(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_entity.side_effect = [baseline, baseline, baseline]
    client.update_entity.side_effect = AuthenticationError("forbidden", status_code=403)
    coordinator = _coordinator(client, revision=3)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=3,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )

    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert result.attempted_write is True
    assert result.write_error is not None
    assert "HTTP 403" in result.write_error
    assert "Contributor to change it" in result.write_error  # a write needs more than the Reader role
    assert result.read_back == baseline


@pytest.mark.parametrize(
    ("read_back_name", "outcome", "message", "changed"),
    [
        ("before", ApplyOutcomeKind.UNKNOWN, "Read-back differs", "properties.displayName: wanted 'after', live 'before'"),
        # live matches the edit: same as an applied write
        ("after", ApplyOutcomeKind.APPLIED, "despite the write error", "properties.displayName"),
    ],
)
def test_apply_outcome_for_timeout_follows_the_read_back(
    client: MagicMock, read_back_name: str, outcome: ApplyOutcomeKind, message: str, changed: str,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    read_back = {"name": "res-1", "properties": {"displayName": read_back_name}}
    client.get_entity.side_effect = [baseline, baseline, read_back]
    client.update_entity.side_effect = TimeoutError("timed out")
    coordinator = _coordinator(client, revision=9)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=9,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )

    assert result.outcome is outcome
    assert message in result.message
    assert result.attempted_write is True
    assert result.changed_fields == (changed,)
    assert "timed out" in (result.write_error or "")


@pytest.mark.parametrize(
    ("write_error", "outcome"),
    [
        (None, ApplyOutcomeKind.UNKNOWN),
        (AuthenticationError("forbidden", status_code=403), ApplyOutcomeKind.REJECTED),  # the service said no
    ],
    ids=["write-ok", "write-403"],
)
def test_apply_outcome_when_readback_fails_after_write(
    client: MagicMock, write_error: AuthenticationError | None, outcome: ApplyOutcomeKind,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_entity.side_effect = [baseline, baseline, RuntimeError("read-back unavailable")]
    client.update_entity.return_value = {"ok": True}
    client.update_entity.side_effect = write_error
    coordinator = _coordinator(client, revision=2)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=2,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )

    assert result.outcome is outcome
    assert result.attempted_write is True
    assert result.read_back is None
    assert "Read-back failed: RuntimeError: read-back unavailable" in result.message
    assert result.write_error == (write_error and write_error.diagnostic_text())  # only the write's own error


@pytest.mark.parametrize(
    ("write_error", "outcome"),
    [
        (AuthenticationError("forbidden", status_code=403), ApplyOutcomeKind.REJECTED),
        (TimeoutError("timed out"), ApplyOutcomeKind.UNKNOWN),
        (None, ApplyOutcomeKind.DIFFERS),
    ],
    ids=["403", "timeout", "differs"],
)
def test_apply_lists_the_plan_fields_that_did_not_land_not_another_writers(
    client: MagicMock, write_error: Exception | None, outcome: ApplyOutcomeKind,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before", "impact": "Standard"}}
    edited = {"name": "res-1", "properties": {"displayName": "after", "impact": "Standard"}}
    read_back = {"name": "res-1", "properties": {"displayName": "before", "impact": "Limited"}}  # another writer
    client.get_entity.side_effect = [baseline, baseline, read_back]
    client.update_entity.side_effect = write_error
    coordinator = _coordinator(client, revision=5)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=5,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(plan, confirmation_name="res-1")

    assert result.outcome is outcome
    assert result.changed_fields == ("properties.displayName: wanted 'after', live 'before'",)


@pytest.mark.parametrize(
    "write_error",
    [AuthenticationError("forbidden", status_code=403), TimeoutError("timed out"), None],
    ids=["403", "timeout", "no-error"],
)
def test_apply_is_applied_when_own_fields_landed_and_another_writer_changed_others(
    client: MagicMock, write_error: Exception | None,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before", "impact": "Standard"}}
    edited = {"name": "res-1", "properties": {"displayName": "after", "impact": "Standard"}}
    read_back = {"name": "res-1", "properties": {"displayName": "after", "impact": "Limited"}}  # another writer
    client.get_entity.side_effect = [baseline, baseline, read_back]
    client.update_entity.side_effect = write_error
    coordinator = _coordinator(client, revision=5)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=5,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(plan, confirmation_name="res-1")

    assert result.outcome is ApplyOutcomeKind.APPLIED
    assert result.read_back == read_back


def test_apply_rejects_tampered_plan_payload(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_entity.return_value = baseline
    coordinator = _coordinator(client, revision=2)
    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=2,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    # mutate after review
    plan.edited_resource["properties"]["displayName"] = "EVIL"  # type: ignore[index]
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )
    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "changed after review" in result.message
    client.update_entity.assert_not_called()


@pytest.mark.parametrize(
    "write_error",
    [AuthenticationError("forbidden", status_code=403), TimeoutError("timed out")],
)
def test_apply_rejects_a_second_apply_of_the_same_plan(client: MagicMock, write_error: Exception) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    client.get_entity.return_value = baseline
    client.update_entity.side_effect = write_error
    coordinator = _coordinator(client, revision=2)
    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=2,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    coordinator.apply_confirmed(plan, confirmation_name="res-1")

    result = coordinator.apply_confirmed(plan, confirmation_name="res-1")

    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert result.attempted_write is False
    assert "unknown or expired" in result.message
    client.update_entity.assert_called_once()


def test_a_superseded_prepare_finishing_late_keeps_the_newer_plan(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    coordinator = _coordinator(client)
    newer: dict[str, Any] = {}

    def read_side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if not newer:  # the first prepare's live read: a second prepare starts and finishes meanwhile
            newer["plan"] = None
            newer["plan"] = coordinator.prepare_plan(
                target=_target("entity"),
                revision=1,
                edited_resource={"name": "res-1", "properties": {"displayName": "newer"}},
                draft_live_baseline=baseline,
            )
        return baseline

    client.get_entity.side_effect = read_side_effect
    coordinator.prepare_plan(
        target=_target("entity"),
        revision=1,
        edited_resource={"name": "res-1", "properties": {"displayName": "older"}},
        draft_live_baseline=baseline,
    )

    result = coordinator.apply_confirmed(newer["plan"], confirmation_name="res-1")

    assert "changed after review" not in result.message
    client.update_entity.assert_called_once()


@pytest.mark.parametrize(
    ("paused", "older", "writes", "rejection"),
    [
        # An older prepare stops after its generation check, before it publishes.
        ((apply_module, "deepcopy"), lambda coordinator, prepare: prepare("older"), 1, "changed after review"),
        # An apply stops between comparing its plan and consuming it.
        (
            (ApplyPlan, "__eq__"),
            lambda coordinator, prepare: coordinator.apply_confirmed(prepare("older"), confirmation_name="res-1"),
            2,
            "unknown or expired",
        ),
    ],
    ids=["prepare-paused-before-publishing", "apply-paused-before-consuming"],
)
def test_a_paused_older_operation_keeps_the_newer_prepared_plan(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch, paused: tuple[Any, str], older: Any, writes: int, rejection: str,
) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    client.get_entity.return_value = baseline
    coordinator = _coordinator(client)
    pausing, resume = threading.Event(), threading.Event()
    real = getattr(*paused)

    def pause_once(*args: Any) -> Any:
        if not pausing.is_set():
            pausing.set()
            assert resume.wait(5)
        return real(*args)

    def prepare(name: str) -> Any:
        return coordinator.prepare_plan(
            target=_target("entity"),
            revision=1,
            edited_resource={"name": "res-1", "properties": {"displayName": name}},
            draft_live_baseline=baseline,
        )

    monkeypatch.setattr(*paused, pause_once)
    older_thread = threading.Thread(target=older, args=(coordinator, prepare))
    older_thread.start()
    assert pausing.wait(5)
    newer: dict[str, Any] = {}
    newer_thread = threading.Thread(target=lambda: newer.update(plan=prepare("newer")))
    newer_thread.start()
    newer_thread.join(0.2)  # time for the newer prepare to start; it waits for the lock the paused operation holds
    resume.set()
    older_thread.join(5)
    newer_thread.join(5)

    result = coordinator.apply_confirmed(newer["plan"], confirmation_name="res-1")

    assert rejection not in result.message
    assert client.update_entity.call_count == writes


def test_apply_rechecks_revision_after_network_read_before_write(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before"}}
    edited = {"name": "res-1", "properties": {"displayName": "after"}}
    revisions = {"value": 1}

    def revision_supplier(_target: Any) -> int:
        return revisions["value"]

    calls = {"count": 0}

    def read_side_effect(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 2:
            revisions["value"] = 2
        return baseline

    client.get_entity.side_effect = read_side_effect
    coordinator = ApplyCoordinator(
        client,
        current_revision_supplier=revision_supplier,
        live_scope_supplier=lambda _target: True,
    )
    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=1,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )
    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "changed after plan preparation" in result.message
    client.update_entity.assert_not_called()


def test_prepare_signal_definition_refuses_unknown_consumer_scope(client: MagicMock) -> None:
    baseline = {
        "name": "res-1",
        "properties": {"displayName": "before", "signalKind": "AzureResourceMetric"},
    }
    edited = {
        "name": "res-1",
        "properties": {"displayName": "after", "signalKind": "AzureResourceMetric"},
    }
    client.get_signal_definition.return_value = baseline
    client.list_entities.side_effect = RuntimeError("entity list failed")
    coordinator = _coordinator(client, revision=1)

    result = coordinator.prepare_plan(
        target=_target("signal_definition"),
        revision=1,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )

    assert isinstance(result, ApplyResult)
    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "Unable to resolve shared definition scope" in result.message


def test_apply_signal_definition_refuses_consumer_scope_drift(client: MagicMock) -> None:
    baseline = {
        "name": "res-1",
        "properties": {"displayName": "before", "signalKind": "AzureResourceMetric"},
    }
    edited = {
        "name": "res-1",
        "properties": {"displayName": "after", "signalKind": "AzureResourceMetric"},
    }
    entity_set_a = [{
        "name": "a",
        "properties": {
            "signalGroups": {
                "azureResource": {
                    "signals": [{"signalDefinitionName": "res-1"}],
                }
            }
        },
    }]
    entity_set_b = entity_set_a + [{
        "name": "b",
        "properties": {
            "signalGroups": {
                "azureResource": {
                    "signals": [{"signalDefinitionName": "res-1"}],
                }
            }
        },
    }]
    client.get_signal_definition.side_effect = [baseline, baseline]
    client.list_entities.side_effect = [entity_set_a, entity_set_b]
    coordinator = _coordinator(client, revision=1)

    plan = coordinator.prepare_plan(
        target=_target("signal_definition"),
        revision=1,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )

    assert result.outcome is ApplyOutcomeKind.REJECTED
    assert "consumer scope changed" in result.message
    client.update_signal_definition.assert_not_called()


def test_apply_surfaces_differs_on_readback_mismatch_without_error(client: MagicMock) -> None:
    baseline = {"name": "res-1", "properties": {"displayName": "before", "x": {"y": 1}}}
    edited = {"name": "res-1", "properties": {"displayName": "after", "x": {"y": 2}}}
    read_back = {"name": "res-1", "properties": {"displayName": "after", "x": {"y": 1}}}
    client.get_entity.side_effect = [baseline, baseline, read_back]
    client.update_entity.return_value = {"ok": True}
    coordinator = _coordinator(client, revision=4)

    plan = coordinator.prepare_plan(
        target=_target("entity"),
        revision=4,
        edited_resource=edited,
        draft_live_baseline=baseline,
    )
    assert not isinstance(plan, ApplyResult)
    result = coordinator.apply_confirmed(
        plan,
        confirmation_name="res-1",
    )

    assert result.outcome is ApplyOutcomeKind.DIFFERS
    assert result.changed_fields == ("properties.x.y: wanted 2, live 1",)


def test_normalize_known_readonly_fields_preserves_unknown_data() -> None:
    raw = {
        "id": "/sub/x",
        "type": "Microsoft.CloudHealth/healthModels/entities",
        "etag": "abc",
        "systemData": {"createdBy": "service"},
        "name": "res-1",
        "properties": {
            "displayName": "n",
            "healthState": "Healthy",
            "provisioningState": "Succeeded",
            "customUnknown": {"nested": 1},
        },
        "customRootUnknown": {"keep": True},
    }

    sanitized = normalize_known_readonly_fields(raw)

    assert "id" not in sanitized
    assert "type" not in sanitized
    assert "etag" not in sanitized
    assert "systemData" not in sanitized
    assert "provisioningState" not in sanitized["properties"]
    assert sanitized["properties"]["customUnknown"] == {"nested": 1}
    assert sanitized["customRootUnknown"] == {"keep": True}


def test_diff_documents_changed_paths_reports_precise_nested_paths() -> None:
    before = {"a": {"b": 1, "c": 2}}
    after = {"a": {"b": 3, "d": 4}}
    paths = tuple(".".join(change.path) for change in diff_documents(before, after))
    assert paths == ("a.b", "a.c", "a.d")
