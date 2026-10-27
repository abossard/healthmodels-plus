"""Parse transport models into domain models — pure functions, no I/O.

This is the boundary between the permissive REST wire format and the
strict frozen domain types. All functions here are calculations.
"""
from __future__ import annotations

from typing import Mapping, Sequence

from azext_healthmodels_plus.tui.models.domain import (
    EntityNode,
    EntityState,
    EvaluationRule,
    Forest,
    Relationship,
    SignalDefinition,
    SignalValue,
    Snapshot,
)
from azext_healthmodels_plus.tui.models.enums import (
    ComparisonOperator,
    DataUnit,
    HealthState,
    Impact,
    SignalKind,
)
from azext_healthmodels_plus.tui.models.transport import (
    TransportEntity,
    TransportEvaluationRule,
    TransportRelationship,
    TransportSignalDefinition,
    TransportSignalRef,
)


# ─── Health state parsing ─────────────────────────────────────────────


def _parse_health_state(raw: str | None) -> HealthState:
    """Parse a health state string into the enum, defaulting to Unknown."""
    if raw is None:
        return HealthState.UNKNOWN
    try:
        return HealthState(raw)
    except ValueError:
        return HealthState.UNKNOWN


def _parse_signal_kind(raw: str | None) -> SignalKind:
    """Parse a signal kind string into the enum."""
    if raw is None:
        return SignalKind.EXTERNAL
    try:
        return SignalKind(raw)
    except ValueError:
        return SignalKind.EXTERNAL


def _parse_data_unit(raw: str | None) -> DataUnit:
    """Parse a data unit string into the enum.

    Unknown / SDK-introduced units are preserved as ``DataUnit.OTHER`` rather
    than silently being coerced to ``COUNT`` (which used to lie about the unit).
    """
    if raw is None:
        return DataUnit.COUNT
    try:
        return DataUnit(raw)
    except ValueError:
        return DataUnit.OTHER


def _parse_operator(raw: str | None) -> ComparisonOperator | None:
    """Parse a comparison operator string into the enum."""
    if raw is None:
        return None
    try:
        return ComparisonOperator(raw)
    except ValueError:
        return None


def _parse_impact(raw: str | None) -> Impact:
    """Parse an impact string into the enum, defaulting to Standard."""
    if raw is None:
        return Impact.STANDARD
    if raw == "None":
        return Impact.SUPPRESSED
    try:
        return Impact(raw)
    except ValueError:
        return Impact.STANDARD


# ─── Signal definition parsing ────────────────────────────────────────


def _parse_rule(raw: TransportEvaluationRule | None) -> EvaluationRule | None:
    operator = _parse_operator(raw.get("operator")) if raw else None
    if operator is None:
        return None
    return EvaluationRule(operator=operator, threshold=float(raw.get("threshold", 0)))


def parse_signal_definition(raw: TransportSignalDefinition) -> SignalDefinition:
    """Parse a single signal definition from the API wire format."""
    props = raw.get("properties") or {}
    rules = props.get("evaluationRules") or {}

    return SignalDefinition(
        name=raw.get("name", ""),
        display_name=props.get("displayName", raw.get("name", "")),
        signal_kind=_parse_signal_kind(props.get("signalKind")),
        data_unit=_parse_data_unit(props.get("dataUnit")),
        degraded_rule=_parse_rule(rules.get("degradedRule")),
        unhealthy_rule=_parse_rule(rules.get("unhealthyRule")),
    )


def parse_signal_definitions(
    raw_list: Sequence[TransportSignalDefinition],
) -> Mapping[str, SignalDefinition]:
    """Parse a list of signal definitions into a name→definition mapping."""
    return {sd.name: sd for raw in raw_list if (sd := parse_signal_definition(raw))}


# ─── Signal value parsing (inline in entities) ────────────────────────


def _parse_signal_ref(
    raw: TransportSignalRef,
    sig_defs: Mapping[str, SignalDefinition],
) -> SignalValue:
    """Parse a signal reference from an entity's signal group.

    Raises ValueError if the signal has no name.
    """
    sig_name = raw.get("name", "")
    if not sig_name:
        raise ValueError(f"Signal reference missing 'name': {raw!r}")

    def_name = raw.get("signalDefinitionName", "")
    sig_def = sig_defs.get(def_name)
    if sig_def is None and not def_name:
        # Inline signals carry displayName, dataUnit and evaluationRules themselves.
        try:
            sig_def = parse_signal_definition({"name": sig_name, "properties": raw})
        except (TypeError, ValueError):
            sig_def = None  # one odd inline signal must not fail the whole model load

    status = raw.get("status") or {}
    raw_value = status.get("value")

    return SignalValue(
        name=sig_name,
        definition_name=def_name,
        display_name=sig_def.display_name if sig_def else (raw.get("displayName") or def_name[:12] or sig_name),
        signal_kind=_parse_signal_kind(raw.get("signalKind")),
        health_state=_parse_health_state(status.get("healthState")),
        value=float(raw_value) if raw_value is not None else None,
        data_unit=sig_def.data_unit if sig_def else DataUnit.COUNT,
        reported_at=status.get("reportedAt", ""),
        error=str(status.get("error")) if status.get("error") else None,
        degraded_rule=sig_def.degraded_rule if sig_def else None,
        unhealthy_rule=sig_def.unhealthy_rule if sig_def else None,
    )


# ─── Entity parsing ──────────────────────────────────────────────────


def parse_entity(
    raw: TransportEntity,
    sig_defs: Mapping[str, SignalDefinition],
    endpoint: str = "https://management.azure.com",
) -> EntityNode:
    """Parse a single entity from the API wire format.

    Raises ValueError if the entity has no id or name.
    """
    entity_id = raw.get("id", "")
    entity_name = raw.get("name", "")
    if not entity_id:
        raise ValueError(f"Entity missing 'id': {raw!r}")
    if not entity_name:
        raise ValueError(f"Entity missing 'name': {raw!r}")

    props = raw.get("properties") or {}
    icon_raw = props.get("icon") or {}
    icon_name = icon_raw.get("iconName", "") if isinstance(icon_raw, dict) else str(icon_raw)

    # Collect all signals from all signal groups
    signals: list[SignalValue] = []
    signal_groups = props.get("signalGroups", {})
    if isinstance(signal_groups, dict):
        for _group_key, group_val in signal_groups.items():
            if not isinstance(group_val, dict):
                continue
            signals_list = group_val.get("signals")
            if not isinstance(signals_list, list):
                continue
            for sig_ref in signals_list:
                signals.append(_parse_signal_ref(sig_ref, sig_defs))

    from azext_healthmodels_plus.tui.domain.model_reference import parse_reference

    azure_group = signal_groups.get("azureResource", {}) if isinstance(signal_groups, dict) else {}
    resource_id = azure_group.get("azureResourceId", "") if isinstance(azure_group, dict) else ""
    return EntityNode(
        entity_id=entity_id,
        name=entity_name,
        display_name=props.get("displayName", raw.get("name", "")),
        health_state=_parse_health_state(props.get("healthState")),
        icon_name=icon_name,
        impact=_parse_impact(props.get("impact")),
        signals=tuple(signals),
        reference=parse_reference(resource_id, endpoint) if resource_id else None,
    )


def parse_entities(
    raw_list: Sequence[TransportEntity],
    sig_defs: Mapping[str, SignalDefinition],
    endpoint: str = "https://management.azure.com",
) -> Mapping[str, EntityNode]:
    """Parse a list of entities into a name→EntityNode mapping.

    Raises ValueError if any entity is malformed.
    """
    return {
        ent.name: ent
        for raw in raw_list
        for ent in [parse_entity(raw, sig_defs, endpoint)]
    }


# ─── Relationship parsing ────────────────────────────────────────────


def parse_relationship(raw: TransportRelationship) -> Relationship:
    """Parse a single relationship from the API wire format."""
    props = raw.get("properties", {})
    return Relationship(
        relationship_id=raw.get("id", ""),
        name=raw.get("name", ""),
        parent_entity_name=props.get("parentEntityName", ""),
        child_entity_name=props.get("childEntityName", ""),
    )


def parse_relationships(
    raw_list: Sequence[TransportRelationship],
) -> Sequence[Relationship]:
    """Parse a list of relationships."""
    return [parse_relationship(raw) for raw in raw_list]
