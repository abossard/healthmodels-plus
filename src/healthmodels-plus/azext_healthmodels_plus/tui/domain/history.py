"""Temporal history keys, parsing, bucketing, and identity snapshot diffing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import json
import math
import statistics
from typing import Any, Mapping

from azext_healthmodels_plus.tui.domain.drafts import normalize_known_readonly_fields
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.models.domain import Forest
from azext_healthmodels_plus.tui.models.enums import HealthState


def _require_aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must use UTC timezone")
    return value.astimezone(UTC)


def parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value}") from exc
    return _require_aware_utc(parsed, "timestamp")


class HistoryBucketHealth(Enum):
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    UNHEALTHY = "Unhealthy"
    UNKNOWN = "Unknown"
    NO_COVERAGE = "NoCoverage"
    MIXED = "Mixed"


@dataclass(frozen=True)
class SnapshotKey:
    model: ModelKey
    at_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_utc", _require_aware_utc(self.at_utc, "SnapshotKey.at_utc"))


@dataclass(frozen=True)
class RangeKey:
    model: ModelKey
    entity_name: str
    start_utc: datetime
    end_utc: datetime
    signal_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_utc", _require_aware_utc(self.start_utc, "RangeKey.start_utc"))
        object.__setattr__(self, "end_utc", _require_aware_utc(self.end_utc, "RangeKey.end_utc"))
        if self.start_utc >= self.end_utc:
            raise ValueError("RangeKey.start_utc must be before RangeKey.end_utc")
        if not self.entity_name:
            raise ValueError("RangeKey.entity_name must be non-empty")


@dataclass(frozen=True)
class HistoryEvent:
    occurred_at: datetime
    health: HistoryBucketHealth
    previous_health: HistoryBucketHealth | None = None
    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class HistoryBucket:
    start_utc: datetime
    end_utc: datetime
    health: HistoryBucketHealth
    worst_health: HistoryBucketHealth
    sample_count: int
    null_value_count: int
    value: float | None = None  # mean of the bucket's non-null signal samples


@dataclass(frozen=True)
class SnapshotNode:
    canonical_name: str
    display_name: str
    health: HistoryBucketHealth
    value: float | None
    parent_canonical_name: str | None = None
    configuration: Mapping[str, Any] | None = None
    value_vector: tuple[tuple[str, float | None], ...] = ()
    parent_canonical_names: tuple[str, ...] = ()
    topology_edges: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SnapshotEdit:
    canonical_name: str
    change_kind: str
    before: Any
    after: Any


@dataclass(frozen=True)
class SnapshotComparison:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    health_edits: tuple[SnapshotEdit, ...]
    value_edits: tuple[SnapshotEdit, ...]
    configuration_edits: tuple[SnapshotEdit, ...]
    topology_edits: tuple[SnapshotEdit, ...]


_HEALTH_ORDER = {
    HistoryBucketHealth.HEALTHY: 0,
    HistoryBucketHealth.UNKNOWN: 1,
    HistoryBucketHealth.DEGRADED: 2,
    HistoryBucketHealth.UNHEALTHY: 3,
    HistoryBucketHealth.NO_COVERAGE: 4,
    HistoryBucketHealth.MIXED: 5,
}


def _to_bucket_health(raw: Any) -> HistoryBucketHealth:
    if isinstance(raw, HistoryBucketHealth):
        return raw
    if isinstance(raw, HealthState):
        return HistoryBucketHealth(raw.value)
    if isinstance(raw, str):
        for candidate in HistoryBucketHealth:
            if candidate.value.casefold() == raw.casefold():
                return candidate
    raise ValueError(f"Unsupported health value: {raw!r}")


def parse_history_events(points: list[Mapping[str, Any]]) -> tuple[HistoryEvent, ...]:
    events: list[HistoryEvent] = []
    seen: set[datetime] = set()
    for index, point in enumerate(points):
        if "occurredAt" not in point:
            raise ValueError(f"Missing occurredAt at index {index}")
        occurred_at = parse_utc_timestamp(str(point["occurredAt"]))
        if occurred_at in seen:
            raise ValueError(f"Duplicate timestamp: {point['occurredAt']}")
        seen.add(occurred_at)
        if point.get("newState") is not None:
            health = _to_bucket_health(point["newState"])
        else:
            health = _to_bucket_health(point.get("healthState", "Unknown"))
        previous_health = (
            _to_bucket_health(point["previousState"])
            if point.get("previousState") is not None
            else None
        )
        value = point.get("value")
        numeric: float | None
        if value is None:
            numeric = None
        elif isinstance(value, bool):
            raise ValueError(f"Invalid numeric value at {point['occurredAt']}: {value!r}")
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid numeric value at {point['occurredAt']}: {value!r}") from exc
            if not math.isfinite(numeric):
                raise ValueError(f"Invalid numeric value at {point['occurredAt']}: {value!r}")
        events.append(
            HistoryEvent(
                occurred_at=occurred_at,
                health=health,
                previous_health=previous_health,
                value=numeric,
                reason=point.get("reason"),
            )
        )
    events.sort(key=lambda event: event.occurred_at)
    return tuple(events)


def bucket_history(
    events: tuple[HistoryEvent, ...],
    range_key: RangeKey,
    bucket_size: timedelta,
    *,
    baseline_health: HistoryBucketHealth | None = None,
) -> tuple[HistoryBucket, ...]:
    if bucket_size <= timedelta(0):
        raise ValueError("bucket_size must be positive")
    if range_key.signal_name is None:
        return _bucket_entity_transitions(events, range_key, bucket_size, baseline_health=baseline_health)
    return _bucket_signal_samples(events, range_key, bucket_size)


def _bucket_signal_samples(
    events: tuple[HistoryEvent, ...],
    range_key: RangeKey,
    bucket_size: timedelta,
) -> tuple[HistoryBucket, ...]:
    times = sorted(event.occurred_at for event in events)
    gaps = [later - earlier for earlier, later in zip(times, times[1:]) if later > earlier]
    # A reported state holds until the next report, so buckets narrower than the sampling cadence are not gaps.
    # shortcut: cadence is inferred from 3+ intervals; pass the signal's refreshInterval if that misleads.
    hold = statistics.median(gaps) * 1.5 if len(gaps) >= 3 else timedelta(0)
    last = max((event for event in events if event.occurred_at < range_key.start_utc),
               key=lambda event: event.occurred_at, default=None)
    buckets: list[HistoryBucket] = []
    cursor = range_key.start_utc
    while cursor < range_key.end_utc:
        bucket_end = min(cursor + bucket_size, range_key.end_utc)
        points = [
            event for event in events
            if cursor <= event.occurred_at < bucket_end
        ]
        if not points:
            if last is not None and bucket_end - last.occurred_at <= hold:
                buckets.append(HistoryBucket(cursor, bucket_end, last.health, last.health, 0, 0, value=last.value))
            else:
                buckets.append(
                    HistoryBucket(cursor, bucket_end, HistoryBucketHealth.NO_COVERAGE, HistoryBucketHealth.NO_COVERAGE, 0, 0)
                )
            cursor = bucket_end
            continue
        last = max(points, key=lambda event: event.occurred_at)

        states = {event.health for event in points}
        if len(states) == 1:
            health = next(iter(states))
        else:
            health = HistoryBucketHealth.MIXED
        worst_health = max(states, key=lambda item: _HEALTH_ORDER[item])
        values = [event.value for event in points if event.value is not None]
        buckets.append(
            HistoryBucket(
                start_utc=cursor,
                end_utc=bucket_end,
                health=health,
                worst_health=worst_health,
                sample_count=len(points),
                null_value_count=len(points) - len(values),
                value=sum(values) / len(values) if values else None,
            )
        )
        cursor = bucket_end
    return tuple(buckets)


def _bucket_entity_transitions(
    events: tuple[HistoryEvent, ...],
    range_key: RangeKey,
    bucket_size: timedelta,
    *,
    baseline_health: HistoryBucketHealth | None,
) -> tuple[HistoryBucket, ...]:
    sorted_events = tuple(sorted(events, key=lambda event: event.occurred_at))
    event_index = 0
    current_state = baseline_health

    # Apply transitions before the requested range.
    while event_index < len(sorted_events) and sorted_events[event_index].occurred_at < range_key.start_utc:
        current_state = sorted_events[event_index].health
        event_index += 1

    # If coverage at range start is unknown, use previousState from first transition.
    if (
        current_state is None
        and event_index < len(sorted_events)
        and sorted_events[event_index].occurred_at >= range_key.start_utc
        and sorted_events[event_index].previous_health is not None
    ):
        current_state = sorted_events[event_index].previous_health

    buckets: list[HistoryBucket] = []
    cursor = range_key.start_utc
    while cursor < range_key.end_utc:
        bucket_end = min(cursor + bucket_size, range_key.end_utc)
        covered_states: list[HistoryBucketHealth] = []
        covered_count = 0
        null_count = 0
        inner_cursor = cursor

        while event_index < len(sorted_events) and sorted_events[event_index].occurred_at < bucket_end:
            transition = sorted_events[event_index]
            if transition.occurred_at > inner_cursor and current_state is not None:
                covered_states.append(current_state)
                covered_count += 1
            current_state = transition.health
            covered_states.append(current_state)
            covered_count += 1
            if transition.value is None:
                null_count += 1
            inner_cursor = transition.occurred_at
            event_index += 1

        if inner_cursor < bucket_end and current_state is not None:
            covered_states.append(current_state)
            covered_count += 1

        if not covered_states:
            bucket_health = HistoryBucketHealth.NO_COVERAGE
            worst_health = HistoryBucketHealth.NO_COVERAGE
        else:
            distinct = set(covered_states)
            bucket_health = next(iter(distinct)) if len(distinct) == 1 else HistoryBucketHealth.MIXED
            worst_health = max(distinct, key=lambda item: _HEALTH_ORDER[item])

        buckets.append(
            HistoryBucket(
                start_utc=cursor,
                end_utc=bucket_end,
                health=bucket_health,
                worst_health=worst_health,
                sample_count=covered_count,
                null_value_count=null_count,
            )
        )
        cursor = bucket_end

    return tuple(buckets)


def compare_snapshots(
    before: Mapping[str, SnapshotNode],
    after: Mapping[str, SnapshotNode],
) -> SnapshotComparison:
    before_keys = set(before)
    after_keys = set(after)
    added = tuple(sorted(after_keys - before_keys))
    removed = tuple(sorted(before_keys - after_keys))

    health_edits: list[SnapshotEdit] = []
    value_edits: list[SnapshotEdit] = []
    configuration_edits: list[SnapshotEdit] = []
    topology_edits: list[SnapshotEdit] = []

    for key in sorted(before_keys & after_keys):
        old = before[key]
        new = after[key]
        if old.health != new.health:
            health_edits.append(SnapshotEdit(key, "health", old.health.value, new.health.value))
        old_values = old.value_vector if old.value_vector else ((old.canonical_name, old.value),)
        new_values = new.value_vector if new.value_vector else ((new.canonical_name, new.value),)
        if old_values != new_values:
            value_edits.append(SnapshotEdit(key, "value", old_values, new_values))
        display_name_changed = old.display_name != new.display_name
        before_configuration = old.configuration or {}
        after_configuration = new.configuration or {}
        configuration_changed = before_configuration != after_configuration
        if display_name_changed or configuration_changed:
            if display_name_changed and configuration_changed:
                before_change: Any = {
                    "display_name": old.display_name,
                    "configuration": before_configuration,
                }
                after_change: Any = {
                    "display_name": new.display_name,
                    "configuration": after_configuration,
                }
            elif display_name_changed:
                before_change = {"display_name": old.display_name}
                after_change = {"display_name": new.display_name}
            else:
                before_change = before_configuration
                after_change = after_configuration
            configuration_edits.append(SnapshotEdit(key, "configuration", before_change, after_change))
        old_topology = (
            old.parent_canonical_names if old.parent_canonical_names else ((old.parent_canonical_name,) if old.parent_canonical_name else ()),
            old.topology_edges,
        )
        new_topology = (
            new.parent_canonical_names if new.parent_canonical_names else ((new.parent_canonical_name,) if new.parent_canonical_name else ()),
            new.topology_edges,
        )
        if old_topology != new_topology:
            topology_edits.append(
                SnapshotEdit(key, "topology", old_topology, new_topology)
            )

    return SnapshotComparison(
        added=added,
        removed=removed,
        health_edits=tuple(health_edits),
        value_edits=tuple(value_edits),
        configuration_edits=tuple(configuration_edits),
        topology_edits=tuple(topology_edits),
    )


def project_snapshot_nodes(
    *,
    model: ModelKey,
    forest: Forest,
    resource_json: Mapping[tuple[str, str], str],
) -> dict[str, SnapshotNode]:
    """Project comparable snapshot nodes from parsed forest and raw resources."""
    entities_raw = _decode_raw_resources(resource_json, "entity")
    definitions_raw = _decode_raw_resources(resource_json, "signaldefinition")
    relationships_raw = _decode_raw_resources(resource_json, "relationship")

    parent_map: dict[str, set[str]] = {}
    for relationship in relationships_raw.values():
        if not isinstance(relationship, Mapping):
            continue
        properties = relationship.get("properties")
        if not isinstance(properties, Mapping):
            continue
        parent_name = str(properties.get("parentEntityName", "")).strip()
        child_name = str(properties.get("childEntityName", "")).strip()
        if not parent_name or not child_name:
            continue
        parent_map.setdefault(child_name.casefold(), set()).add(parent_name.casefold())

    id_by_name = {
        entity.name.casefold(): entity.entity_id
        for entity in forest.entities.values()
    }
    result: dict[str, SnapshotNode] = {}
    for entity in forest.entities.values():
        try:
            health = HistoryBucketHealth(entity.health_state.value)
        except ValueError:
            health = HistoryBucketHealth.UNKNOWN

        signal_values = tuple(sorted((signal.name, signal.value) for signal in entity.signals))
        signal_config = tuple(
            sorted(
                {
                    "name": signal.name,
                    "definition_name": signal.definition_name,
                    "signal_kind": signal.signal_kind.value,
                    "degraded_rule": (
                        {
                            "operator": signal.degraded_rule.operator.value,
                            "threshold": signal.degraded_rule.threshold,
                        }
                        if signal.degraded_rule is not None
                        else None
                    ),
                    "unhealthy_rule": (
                        {
                            "operator": signal.unhealthy_rule.operator.value,
                            "threshold": signal.unhealthy_rule.threshold,
                        }
                        if signal.unhealthy_rule is not None
                        else None
                    ),
                }.items()
            )
            for signal in entity.signals
        )
        definition_names = sorted(
            {
                signal.definition_name.casefold()
                for signal in entity.signals
                if signal.definition_name
            }
        )
        definition_resources = tuple(
            (definition_name, definitions_raw.get(definition_name))
            for definition_name in definition_names
        )
        relationship_resources = _relationship_endpoints_for_entity(
            relationships_raw=relationships_raw,
            entity_name=entity.name,
        )
        entity_raw = entities_raw.get(entity.name.casefold())
        sanitized_entity_raw = (
            _sanitize_entity_resource(entity_raw)
            if isinstance(entity_raw, Mapping)
            else None
        )

        parent_ids = tuple(
            sorted(
                id_by_name.get(parent_name, parent_name)
                for parent_name in parent_map.get(entity.name.casefold(), set())
            )
        )
        topology_edges = tuple(
            sorted(
                (
                    id_by_name.get(parent_name.casefold(), parent_name),
                    id_by_name.get(entity.name.casefold(), entity.name),
                )
                for _, parent_name, child_name in relationship_resources
                if child_name.casefold() == entity.name.casefold()
            )
        )
        result[entity.entity_id] = SnapshotNode(
            canonical_name=entity.entity_id,
            display_name=entity.display_name,
            health=health,
            value=signal_values[0][1] if signal_values else None,
            parent_canonical_name=parent_ids[0] if parent_ids else None,
            parent_canonical_names=parent_ids,
            value_vector=signal_values,
            topology_edges=topology_edges,
            configuration={
                "model": {
                    "subscription": model.subscription,
                    "group": model.group,
                    "name": model.name,
                },
                "impact": entity.impact.value,
                "icon": entity.icon_name,
                "signals": signal_config,
                "entity_resource": sanitized_entity_raw,
                "definitions": definition_resources,
                "children": tuple(sorted(entity.children)),
            },
        )
    return result


def _decode_raw_resources(
    resource_json: Mapping[tuple[str, str], str],
    kind: str,
) -> dict[str, Mapping[str, Any]]:
    decoded: dict[str, Mapping[str, Any]] = {}
    for (resource_kind, resource_name), payload in resource_json.items():
        if resource_kind != kind:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, Mapping):
            decoded[resource_name.casefold()] = data
    return decoded


def _sanitize_entity_resource(resource: Mapping[str, Any]) -> Mapping[str, Any]:
    return normalize_known_readonly_fields(resource)


def _relationship_endpoints_for_entity(
    *,
    relationships_raw: Mapping[str, Mapping[str, Any]],
    entity_name: str,
) -> tuple[tuple[str, str, str], ...]:
    endpoints: list[tuple[str, str, str]] = []
    folded_name = entity_name.casefold()
    for relationship_name, relationship in relationships_raw.items():
        properties = relationship.get("properties")
        if not isinstance(properties, Mapping):
            continue
        parent_name = str(properties.get("parentEntityName", "")).strip()
        child_name = str(properties.get("childEntityName", "")).strip()
        if parent_name.casefold() != folded_name and child_name.casefold() != folded_name:
            continue
        endpoints.append((relationship_name, parent_name, child_name))
    return tuple(sorted(endpoints, key=lambda item: (item[0], item[1], item[2])))
