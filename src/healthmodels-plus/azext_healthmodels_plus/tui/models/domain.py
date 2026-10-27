"""Domain models — immutable dataclasses representing health model state.

These are the stable internal types used by all domain logic. They are
created from transport models via ``domain.parse`` and never change when
the preview API wire format changes.

All types are frozen dataclasses with strict typing.
"""
from __future__ import annotations

import logging
import types
from dataclasses import dataclass
from typing import Mapping
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey, ModelReference

from azext_healthmodels_plus.tui.models.enums import (
    ChangeKind,
    ComparisonOperator,
    DataUnit,
    HealthState,
    Impact,
    SignalKind,
)

_LOG = logging.getLogger(__name__)


# ─── Evaluation rule ─────────────────────────────────────────────────


@dataclass(frozen=True)
class EvaluationRule:
    """A threshold rule for evaluating signal health.

    Invariants:
    - threshold is always a finite float (no NaN/inf)
    """

    operator: ComparisonOperator
    threshold: float

    def __post_init__(self) -> None:
        if not isinstance(self.operator, ComparisonOperator):
            raise TypeError(f"operator must be ComparisonOperator, got {type(self.operator).__name__}")
        t = self.threshold
        if not isinstance(t, (int, float)) or t != t or t in (float("inf"), float("-inf")):
            raise ValueError(f"threshold must be a finite number, got {t!r}")

    def triggers(self, value: float) -> bool:
        """Return True if *value* violates this rule (crosses the threshold)."""
        return self.operator.evaluate(value, self.threshold)


# ─── Signal definition (reusable across entities) ────────────────────


@dataclass(frozen=True)
class SignalDefinition:
    """A reusable signal definition with evaluation thresholds.

    Invariants:
    - name and display_name are non-empty
    - unhealthy_rule is always present (degraded_rule is optional)
    - if both rules exist, they use the same operator direction
    """

    name: str  # GUID
    display_name: str
    signal_kind: SignalKind
    data_unit: DataUnit
    degraded_rule: EvaluationRule | None
    unhealthy_rule: EvaluationRule | None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SignalDefinition.name must be non-empty")
        if not self.display_name:
            raise ValueError("SignalDefinition.display_name must be non-empty")
        if self.signal_kind is None:
            raise ValueError("SignalDefinition.signal_kind must not be None")
        if not isinstance(self.signal_kind, SignalKind):
            raise TypeError(
                f"SignalDefinition.signal_kind must be SignalKind, got {type(self.signal_kind).__name__}"
            )
        if self.unhealthy_rule is None:
            # API data may be incomplete — warn but don't crash.
            _LOG.warning(
                "SignalDefinition %r has no unhealthy_rule; signal will never be Unhealthy.",
                self.name,
            )
        if self.unhealthy_rule is not None and self.degraded_rule is not None:
            if self.unhealthy_rule.operator.direction != self.degraded_rule.operator.direction:
                raise ValueError(
                    f"SignalDefinition {self.name!r}: degraded_rule and unhealthy_rule "
                    f"must use the same operator direction "
                    f"(got {self.degraded_rule.operator.value!r} vs "
                    f"{self.unhealthy_rule.operator.value!r})"
                )

    def evaluate(self, value: float) -> HealthState:
        """Evaluate a numeric value against this definition's rules.

        Returns the worst triggered state: Unhealthy > Degraded > Healthy.
        """
        if self.unhealthy_rule is not None and self.unhealthy_rule.triggers(value):
            return HealthState.UNHEALTHY
        if self.degraded_rule is not None and self.degraded_rule.triggers(value):
            return HealthState.DEGRADED
        return HealthState.HEALTHY


# ─── Signal value (runtime state of a signal within an entity) ───────


@dataclass(frozen=True)
class SignalValue:
    """Runtime state of a single signal within an entity.

    Invariants:
    - name is non-empty
    - health_state is UNKNOWN iff value is None (no data ↔ unknown health)
    """

    name: str  # GUID — signal instance name
    definition_name: str  # GUID — references a SignalDefinition
    display_name: str  # Human-readable (resolved from SignalDefinition)
    signal_kind: SignalKind
    health_state: HealthState
    value: float | None  # None when no data available
    data_unit: DataUnit
    reported_at: str  # ISO 8601 timestamp
    error: str | None = None  # Service-side diagnostic (from SignalStatus.error)
    degraded_rule: EvaluationRule | None = None  # Copied from signal definition
    unhealthy_rule: EvaluationRule | None = None  # Copied from signal definition

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SignalValue.name must be non-empty")

    @property
    def formatted_value(self) -> str:
        """Format the value with appropriate unit suffix."""
        if self.value is None:
            return "—"
        if self.data_unit == DataUnit.PERCENT:
            return f"{self.value:.2f}%"
        elif self.data_unit == DataUnit.MILLISECONDS:
            return f"{self.value:.2f}ms"
        elif self.data_unit == DataUnit.BYTES:
            if self.value >= 1_073_741_824:
                return f"{self.value / 1_073_741_824:.1f}GB"
            elif self.value >= 1_048_576:
                return f"{self.value / 1_048_576:.1f}MB"
            elif self.value >= 1024:
                return f"{self.value / 1024:.1f}KB"
            return f"{self.value:.0f}B"
        else:
            # Count or unknown — show as number
            if self.value == int(self.value):
                return str(int(self.value))
            return f"{self.value:.2f}"


# ─── Entity node (a monitored component) ─────────────────────────────


@dataclass(frozen=True)
class EntityNode:
    """A monitored entity in the health model tree.

    Invariants:
    - entity_id and name are non-empty
    - impact is a typed Impact enum, not a raw string
    - signals is an immutable tuple (no accidental mutation)
    """

    entity_id: str  # ARM resource ID (globally unique identity)
    name: str  # GUID entity name
    display_name: str
    health_state: HealthState
    icon_name: str  # e.g., "Resource", "SystemComponent", "AzureKubernetesService"
    impact: Impact
    signals: tuple[SignalValue, ...]  # All signals from all signal groups
    children: tuple[str, ...] = ()  # Child entity_ids (populated by graph builder)
    reference: ModelReference | None = None

    def __post_init__(self) -> None:
        if not self.entity_id:
            raise ValueError("EntityNode.entity_id must be non-empty")
        if not self.name:
            raise ValueError("EntityNode.name must be non-empty")
        if not isinstance(self.impact, Impact):
            raise TypeError(f"EntityNode.impact must be Impact enum, got {type(self.impact).__name__}")


# ─── Relationship ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Relationship:
    """A parent→child edge in the health model graph.

    Invariants:
    - all fields are non-empty
    - parent and child are different
    """

    relationship_id: str  # ARM resource ID
    name: str  # GUID
    parent_entity_name: str  # GUID or model name
    child_entity_name: str  # GUID

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Relationship.name must be non-empty")
        if not self.parent_entity_name:
            raise ValueError("Relationship.parent_entity_name must be non-empty")
        if not self.child_entity_name:
            raise ValueError("Relationship.child_entity_name must be non-empty")
        if self.parent_entity_name == self.child_entity_name:
            raise ValueError(
                f"Relationship cannot be self-referential: {self.parent_entity_name}"
            )


# ─── Entity state (for snapshot diffing) ─────────────────────────────


@dataclass(frozen=True)
class EntityState:
    """The health state of an entity at a point in time, for diffing."""

    entity_id: str
    display_name: str
    health_state: HealthState
    signal_states: Mapping[str, HealthState]  # signal_name → health state
    signal_values: Mapping[str, float | None]  # signal_name → value

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_states", types.MappingProxyType(dict(self.signal_states)))
        object.__setattr__(self, "signal_values", types.MappingProxyType(dict(self.signal_values)))


# ─── Snapshot ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time snapshot of all entity states."""

    entity_states: Mapping[str, EntityState]  # entity_name → EntityState
    timestamp: str  # When this snapshot was taken (ISO 8601)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "entity_states", types.MappingProxyType(dict(self.entity_states))
        )


# ─── State change (diff result) ──────────────────────────────────────


@dataclass(frozen=True)
class StateChange:
    """A detected change between two snapshots.

    Invariants enforced by __post_init__:
    - NEW  → old_state is None, new_state is not None
    - REMOVED → old_state is not None, new_state is None
    - ESCALATION → both states present, new is worse than old
    - RECOVERY → both states present, new is better than old
    - VALUE_CHANGED → both states present
    """

    entity_id: str
    entity_display_name: str
    kind: ChangeKind
    old_state: HealthState | None  # None for NEW entities
    new_state: HealthState | None  # None for REMOVED entities
    signal_name: str | None = None  # If change is at signal level

    def __post_init__(self) -> None:
        if self.kind == ChangeKind.NEW:
            if self.old_state is not None:
                raise ValueError("NEW change must have old_state=None")
            if self.new_state is None:
                raise ValueError("NEW change must have new_state")
        elif self.kind == ChangeKind.REMOVED:
            if self.old_state is None:
                raise ValueError("REMOVED change must have old_state")
            if self.new_state is not None:
                raise ValueError("REMOVED change must have new_state=None")
        elif self.kind in (ChangeKind.ESCALATION, ChangeKind.RECOVERY, ChangeKind.VALUE_CHANGED):
            if self.old_state is None or self.new_state is None:
                raise ValueError(f"{self.kind.value} change requires both old_state and new_state")
            if self.kind == ChangeKind.ESCALATION and not self.new_state.is_worse_than(self.old_state):
                raise ValueError(
                    f"ESCALATION requires new_state worse than old_state, "
                    f"got {self.old_state.value}→{self.new_state.value}"
                )
            if self.kind == ChangeKind.RECOVERY and not self.new_state.is_better_than(self.old_state):
                raise ValueError(
                    f"RECOVERY requires new_state better than old_state, "
                    f"got {self.old_state.value}→{self.new_state.value}"
                )

    @property
    def is_escalation(self) -> bool:
        """True if this is a severity escalation (worse health)."""
        return self.kind == ChangeKind.ESCALATION


# ─── Forest (validated graph output) ─────────────────────────────────


@dataclass(frozen=True)
class Forest:
    """A validated forest of entity trees, produced by graph_builder."""

    roots: tuple[str, ...]  # Root entity names (may be multiple)
    entities: Mapping[str, EntityNode]  # entity_name → EntityNode (with children populated)
    unlinked: tuple[str, ...] = ()  # Entity names with no parent or invalid refs
    warnings: tuple[str, ...] = ()  # Diagnostic messages (dangling relationships, multi-parent, …)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", types.MappingProxyType(dict(self.entities)))


# ─── Search result ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchResult:
    """A single match from searching the forest."""

    entity_id: str  # ARM resource ID of the entity (or parent entity for signals)
    display_name: str
    is_signal: bool
    health_state: HealthState
    signal_value: str | None = None  # Formatted value (signals only)
    parent_display_name: str | None = None  # Parent entity name (signals only)
    model: ModelKey | None = None
    occurrence_path: tuple[str, ...] = ()
    signal_name: str | None = None
