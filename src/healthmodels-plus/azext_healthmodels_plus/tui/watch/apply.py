"""Guarded single-resource Apply orchestration for live health model edits."""
from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from azext_healthmodels_plus.tui.client.errors import HealthModelError
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.domain.drafts import (
    ApplyPlan,
    ResourceTarget,
    build_apply_plan,
    changed_paths,
    collect_signal_definition_consumers,
    get_in,
    normalize_known_readonly_fields,
)

_RACE_WARNING = (
    "Race warning: this apply flow has no proven atomic If-Match protection. "
    "State may change between confirmation, write, and read-back."
)
_RETRY_STEP = "Prepare again to see and retry the remaining changes."


class ApplyOutcomeKind(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    DIFFERS = "differs"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ApplyResult:
    outcome: ApplyOutcomeKind
    target: ResourceTarget
    attempted_write: bool
    message: str
    race_warning: str
    changed_fields: tuple[str, ...]
    confirmation_required: str
    read_back: dict[str, Any] | None = None
    write_error: str | None = None


@dataclass(frozen=True)
class ConnectionScopeDescription:
    arm_endpoint: str
    subscription_id: str
    subscription_name: str
    tenant_id: str
    tenant_name: str


def _written(
    outcome: ApplyOutcomeKind,
    target: ResourceTarget,
    message: str,
    changed_fields: tuple[str, ...],
    read_back: dict[str, Any] | None,
    write_error: str | None = None,
) -> ApplyResult:
    """Result for a sequence that reached the write call."""
    return ApplyResult(
        outcome=outcome,
        target=target,
        attempted_write=True,
        message=message,
        race_warning=_RACE_WARNING,
        changed_fields=changed_fields,
        confirmation_required=target.name,
        read_back=read_back,
        write_error=write_error,
    )


class ApplyCoordinator:
    """Owns one plan preparation and one explicit confirmed apply sequence."""

    def __init__(
        self,
        client: CloudHealthClient,
        *,
        current_revision_supplier: Callable[[ResourceTarget], int],
        live_scope_supplier: Callable[[ResourceTarget], bool],
    ) -> None:
        self._client = client
        self._current_revision = current_revision_supplier
        self._is_live_scope = live_scope_supplier
        self._prepared: ApplyPlan | None = None  # one prepared plan; Apply must present an equal one
        self._preparations = 0  # a cancelled Prepare's thread runs on; only the latest one may publish
        self._publication = threading.Lock()  # guards numbering, publishing and consuming the prepared plan

    def prepare_plan(
        self,
        *,
        target: ResourceTarget,
        revision: int,
        edited_resource: Mapping[str, Any],
        draft_live_baseline: Mapping[str, Any],
        generated_at: datetime | None = None,
    ) -> ApplyPlan | ApplyResult:
        with self._publication:
            self._preparations += 1
            preparation = self._preparations
        if not self._is_live_scope(target):
            return self._reject(
                target,
                "Apply is allowed only for the current live connected scope.",
            )
        try:
            self.describe_connection_scope(target)
        except ValueError as exc:
            return self._reject(
                target,
                f"Unable to verify connected tenant/subscription scope before apply: {exc}",
            )
        if revision != self._current_revision(target):
            return self._reject(
                target,
                "Draft revision is obsolete. Re-open the current draft and prepare again.",
            )

        try:
            live_now_raw = self._read_target(target)
        except ValueError as exc:
            return self._reject(target, str(exc))

        shared_consumers: tuple[str, ...] = ()
        if target.kind == "signal_definition":
            try:
                scoped_client = self._client_for_target(target)
                entities = scoped_client.list_entities(target.model.group, target.model.name)
            except Exception as exc:  # noqa: BLE001
                return self._reject(
                    target,
                    f"Unable to resolve shared definition scope before apply: {type(exc).__name__}: {exc}",
                )
            shared_consumers = collect_signal_definition_consumers(entities, target.name)

        try:
            plan = build_apply_plan(
                target=target,
                revision=revision,
                edited_resource=edited_resource,
                expected_baseline=draft_live_baseline,
                live_baseline=live_now_raw,
                generated_at=generated_at or datetime.now(timezone.utc),
                shared_consumer_entity_names=shared_consumers,
            )
        except ValueError as exc:
            return self._reject(target, str(exc))

        changed = changed_paths(plan.changes)
        if not changed:
            return self._reject(target, "No writable changes to apply.")
        with self._publication:
            if preparation == self._preparations:
                self._prepared = deepcopy(plan)
        return plan

    def apply_confirmed(
        self,
        plan: ApplyPlan,
        *,
        confirmation_name: str,
    ) -> ApplyResult:
        target = plan.target
        with self._publication:
            if self._prepared is None:
                return self._reject(target, "Apply plan is unknown or expired. Prepare again.")
            if plan != self._prepared:
                return self._reject(target, "Apply plan changed after review. Prepare again.")
            self._prepared = None
        if confirmation_name != target.name:
            return self._reject(
                target,
                "Resource-name confirmation mismatch. No write issued.",
            )

        # build_apply_plan validated and normalized the payload; the equality check above pins it.
        normalized_desired = dict(plan.edited_resource)
        recomputed_changes = changed_paths(plan.changes)

        try:
            live_before_raw = self._read_target(target)
        except ValueError as exc:
            return self._reject(target, str(exc))
        if plan.revision != self._current_revision(target):
            return self._reject(
                target,
                "Draft revision changed after plan preparation. No write issued.",
            )
        if not self._is_live_scope(target):
            return self._reject(
                target,
                "Apply is allowed only for the current live connected scope.",
            )
        live_before = normalize_known_readonly_fields(live_before_raw)
        if live_before != dict(plan.normalized_live_baseline):
            return self._reject(
                target,
                "Live baseline changed after plan preparation. No write issued.",
            )
        if target.kind == "signal_definition":
            try:
                scoped_client = self._client_for_target(target)
                entities = scoped_client.list_entities(target.model.group, target.model.name)
                consumers_now = collect_signal_definition_consumers(entities, target.name)
            except Exception as exc:  # noqa: BLE001
                return self._reject(
                    target,
                    f"Unable to verify shared definition scope before write: {type(exc).__name__}: {exc}",
                )
            if consumers_now != tuple(plan.shared_consumer_entity_names):
                return self._reject(
                    target,
                    "Shared definition consumer scope changed. Prepare again.",
                )

        write_error: str | None = None
        write_status: int | None = None
        try:
            self._write_target(target, plan.edited_resource)
        except HealthModelError as exc:
            write_error = exc.diagnostic_text()
            write_status = exc.status_code
        except Exception as exc:  # noqa: BLE001
            write_error = f"{type(exc).__name__}: {exc}"
        rejected = write_error is not None and write_status in (400, 403, 409)

        try:
            read_back_raw = self._read_target(target)
        except Exception as exc:  # noqa: BLE001
            return _written(
                ApplyOutcomeKind.REJECTED if rejected else ApplyOutcomeKind.UNKNOWN, target,
                ("Service rejected write. " if rejected else "Write attempted, outcome unknown. ")
                + f"Read-back failed: {type(exc).__name__}: {exc}",
                recomputed_changes, None, write_error,
            )
        read_back = normalize_known_readonly_fields(read_back_raw)
        # This plan's fields only: a field another writer changed is not this write's to confirm.
        unconfirmed = tuple(
            f"{path}: wanted {change.after!r}, live {live!r}"
            for path, change in zip(changed_paths(plan.changes), plan.changes)
            if (live := get_in(read_back, change.path)) != change.after
        )
        if not unconfirmed:
            return _written(
                ApplyOutcomeKind.APPLIED, target,
                "Read-back matches desired state despite the write error." if write_error
                else "Apply succeeded and read-back matches desired state.",
                recomputed_changes, read_back_raw, write_error,
            )
        if rejected:
            return _written(
                ApplyOutcomeKind.REJECTED, target, "Service rejected write and read-back differs from desired state.",
                unconfirmed, read_back_raw, write_error,
            )
        if write_error:
            return _written(
                ApplyOutcomeKind.UNKNOWN, target,
                "Write outcome is unknown. Read-back differs from desired state. " + _RETRY_STEP,
                unconfirmed, read_back_raw, write_error,
            )
        return _written(
            ApplyOutcomeKind.DIFFERS, target,
            "Write completed but read-back differs from desired state. " + _RETRY_STEP,
            unconfirmed, read_back_raw,
        )

    @staticmethod
    def _reject(target: ResourceTarget, message: str) -> ApplyResult:
        return ApplyResult(
            outcome=ApplyOutcomeKind.REJECTED,
            target=target,
            attempted_write=False,
            message=message,
            race_warning=_RACE_WARNING,
            changed_fields=(),
            confirmation_required=target.name,
        )

    def describe_connection_scope(self, target: ResourceTarget) -> ConnectionScopeDescription:
        scoped = self._client_for_target(target)
        try:
            return ConnectionScopeDescription(**scoped.get_connection_scope())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Unable to resolve connected scope from profile: {type(exc).__name__}: {exc}"
            ) from exc

    def _read_target(self, target: ResourceTarget) -> dict[str, Any]:
        client = self._client_for_target(target)
        group = target.model.group
        model_name = target.model.name
        if target.kind == "entity":
            return client.get_entity(group, model_name, target.name)
        if target.kind == "signal_definition":
            return client.get_signal_definition(group, model_name, target.name)
        return client.get_relationship(group, model_name, target.name)

    def _write_target(self, target: ResourceTarget, payload: Mapping[str, Any]) -> dict[str, Any]:
        client = self._client_for_target(target)
        group = target.model.group
        model_name = target.model.name
        if target.kind == "entity":
            return client.update_entity(group, model_name, target.name, payload)
        if target.kind == "signal_definition":
            return client.update_signal_definition(group, model_name, target.name, payload)
        return client.update_relationship(group, model_name, target.name, payload)

    def _client_for_target(self, target: ResourceTarget) -> CloudHealthClient:
        try:
            return self._client.for_model(target.model)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"Unable to resolve scoped client for apply target: {type(exc).__name__}: {exc}"
            ) from exc
