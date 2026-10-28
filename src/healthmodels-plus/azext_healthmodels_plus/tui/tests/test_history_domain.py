from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from azext_healthmodels_plus.tui.domain.history import (
    HistoryBucketHealth,
    RangeKey,
    SnapshotComparison,
    SnapshotKey,
    SnapshotNode,
    bucket_history,
    compare_snapshots,
    parse_history_events,
    parse_utc_timestamp,
    project_snapshot_nodes,
)
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.models.domain import EntityNode, Forest, SignalValue
from azext_healthmodels_plus.tui.models.enums import DataUnit, HealthState, Impact, SignalKind


def _model_key() -> ModelKey:
    return ModelKey("https://management.azure.com", "sub", "rg", "model")


@pytest.mark.parametrize(
    "raw,expect_error",
    [
        ("2026-01-01T00:00:00Z", False),
        ("2026-01-01T00:00:00+00:00", False),
        ("2026-01-01T00:00:00", True),
        ("2026-01-01T00:00:00+01:00", True),
    ],
)
def test_timestamp_timezone_validation(raw: str, expect_error: bool) -> None:
    if expect_error:
        with pytest.raises(ValueError, match="UTC"):
            parse_utc_timestamp(raw)
        return
    parsed = parse_utc_timestamp(raw)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_short_outage_bucket_is_preserved_as_mixed_with_unhealthy_worst() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name="s1",
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:10:00Z"),
    )
    events = parse_history_events([
        {"occurredAt": "2026-01-01T00:01:00Z", "value": 1.0, "healthState": "Healthy"},
        {"occurredAt": "2026-01-01T00:02:00Z", "value": 0.0, "healthState": "Unhealthy"},
        {"occurredAt": "2026-01-01T00:03:00Z", "value": 1.0, "healthState": "Healthy"},
    ])

    buckets = bucket_history(events, range_key, timedelta(minutes=5))

    assert buckets[0].health == HistoryBucketHealth.MIXED
    assert buckets[0].worst_health == HistoryBucketHealth.UNHEALTHY
    assert buckets[0].sample_count == 3


def test_bucket_history_gaps_and_null_values() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name="s1",
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:15:00Z"),
    )
    events = parse_history_events([
        {"occurredAt": "2026-01-01T00:01:00Z", "value": None, "healthState": "Unknown"},
        {"occurredAt": "2026-01-01T00:12:00Z", "value": 2.0, "healthState": "Healthy"},
    ])

    buckets = bucket_history(events, range_key, timedelta(minutes=5))

    assert buckets[0].null_value_count == 1
    assert buckets[1].health == HistoryBucketHealth.NO_COVERAGE
    assert buckets[2].health == HistoryBucketHealth.HEALTHY


@pytest.mark.parametrize(
    ("minutes", "end_minute", "bucket_minutes", "expected"),
    [
        # 2-minute cadence in 1-minute buckets: each report holds until the next one.
        ((0, 2, 4, 6, 8), 10, 1, "HHHHHHHHHH"),
        # Same cadence with a hole longer than 1.5x the cadence: the hole stays visible.
        ((0, 2, 4, 10), 12, 1, "HHHHHHHNNNHH"),
        # A hold must cover the whole bucket, so a wide empty bucket after the last report is a gap.
        ((0, 2, 4, 6, 29), 60, 30, "HN"),
    ],
)
def test_bucket_history_holds_reports_within_sampling_cadence(minutes, end_minute, bucket_minutes, expected) -> None:
    start = parse_utc_timestamp("2026-01-01T00:00:00Z")
    range_key = RangeKey(
        model=_model_key(), entity_name="e1", signal_name="s1",
        start_utc=start, end_utc=start + timedelta(minutes=end_minute),
    )
    events = parse_history_events([
        {"occurredAt": f"2026-01-01T00:{m:02d}:00Z", "value": float(m), "healthState": "Healthy"} for m in minutes
    ])

    buckets = bucket_history(events, range_key, timedelta(minutes=bucket_minutes))

    assert "".join("N" if b.health is HistoryBucketHealth.NO_COVERAGE else b.health.value[0] for b in buckets) == expected
    held = [i for i, b in enumerate(buckets) if b.sample_count == 0 and b.health is not HistoryBucketHealth.NO_COVERAGE]
    assert all(buckets[i].value == buckets[i - 1].value for i in held)


@pytest.mark.parametrize(
    "samples,expected",
    [
        ([1.0, 3.0], 2.0),
        ([None, 4.0], 4.0),
        ([None, None], None),
    ],
)
def test_signal_bucket_value_is_mean_of_non_null_samples(samples: list[float | None], expected: float | None) -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name="s1",
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:05:00Z"),
    )
    events = parse_history_events([
        {"occurredAt": f"2026-01-01T00:0{index + 1}:00Z", "value": value, "healthState": "Healthy"}
        for index, value in enumerate(samples)
    ])

    (bucket,) = bucket_history(events, range_key, timedelta(minutes=5))

    assert bucket.value == expected
    assert bucket.null_value_count == samples.count(None)


def test_entity_transition_buckets_preserve_short_outage_across_intervals() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name=None,
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:15:00Z"),
    )
    events = parse_history_events([
        {"occurredAt": "2026-01-01T00:02:00Z", "previousState": "Healthy", "newState": "Unhealthy"},
        {"occurredAt": "2026-01-01T00:07:00Z", "previousState": "Unhealthy", "newState": "Healthy"},
    ])

    buckets = bucket_history(events, range_key, timedelta(minutes=5))

    assert buckets[0].health == HistoryBucketHealth.MIXED
    assert buckets[0].worst_health == HistoryBucketHealth.UNHEALTHY
    assert buckets[1].health == HistoryBucketHealth.MIXED
    assert buckets[1].worst_health == HistoryBucketHealth.UNHEALTHY
    assert buckets[2].health == HistoryBucketHealth.HEALTHY


def test_entity_transition_no_events_uses_known_baseline() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name=None,
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:15:00Z"),
    )
    buckets = bucket_history((), range_key, timedelta(minutes=5), baseline_health=HistoryBucketHealth.HEALTHY)
    assert [bucket.health for bucket in buckets] == [HistoryBucketHealth.HEALTHY] * 3


def test_entity_transition_no_events_no_baseline_has_no_coverage() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name=None,
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:15:00Z"),
    )
    buckets = bucket_history((), range_key, timedelta(minutes=5))
    assert [bucket.health for bucket in buckets] == [HistoryBucketHealth.NO_COVERAGE] * 3


def test_entity_transition_unknown_baseline_is_carried() -> None:
    range_key = RangeKey(
        model=_model_key(),
        entity_name="e1",
        signal_name=None,
        start_utc=parse_utc_timestamp("2026-01-01T00:00:00Z"),
        end_utc=parse_utc_timestamp("2026-01-01T00:10:00Z"),
    )
    buckets = bucket_history((), range_key, timedelta(minutes=5), baseline_health=HistoryBucketHealth.UNKNOWN)
    assert [bucket.health for bucket in buckets] == [HistoryBucketHealth.UNKNOWN, HistoryBucketHealth.UNKNOWN]


@pytest.mark.parametrize(
    "points,match",
    [
        ([{"occurredAt": "not-a-time", "value": 1.0, "healthState": "Healthy"}], "Invalid timestamp"),
        (
            [
                {"occurredAt": "2026-01-01T00:00:00Z", "value": 1.0, "healthState": "Healthy"},
                {"occurredAt": "2026-01-01T00:00:00Z", "value": 2.0, "healthState": "Degraded"},
            ],
            "Duplicate timestamp",
        ),
    ],
)
def test_parse_history_events_reports_malformed_and_duplicate_timestamps(
    points: list[dict[str, object]],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_history_events(points)


def test_compare_snapshots_handles_rename_reparent_and_remove() -> None:
    before = {
        "entity-a": SnapshotNode("entity-a", "Alpha", HistoryBucketHealth.HEALTHY, 1.0, "root"),
        "entity-b": SnapshotNode("entity-b", "Beta", HistoryBucketHealth.HEALTHY, 1.0, "root"),
    }
    after = {
        "entity-a": SnapshotNode("entity-a", "Alpha Renamed", HistoryBucketHealth.HEALTHY, 1.0, "root-2"),
        "entity-c": SnapshotNode("entity-c", "Gamma", HistoryBucketHealth.HEALTHY, 1.0, "root"),
    }

    comparison: SnapshotComparison = compare_snapshots(before, after)

    assert comparison.added == ("entity-c",)
    assert comparison.removed == ("entity-b",)
    assert any(edit.change_kind == "configuration" for edit in comparison.configuration_edits)
    assert len(comparison.configuration_edits) == 1
    assert any(edit.change_kind == "topology" for edit in comparison.topology_edits)


def test_compare_snapshots_coalesces_display_name_and_payload_name_change_to_one_edit() -> None:
    before = {
        "entity-a": SnapshotNode(
            "entity-a",
            "Alpha",
            HistoryBucketHealth.HEALTHY,
            1.0,
            "root",
            configuration={"properties": {"displayName": "Alpha", "tags": {"owner": "team-a"}}},
        ),
    }
    after = {
        "entity-a": SnapshotNode(
            "entity-a",
            "Alpha (UI verified)",
            HistoryBucketHealth.HEALTHY,
            1.0,
            "root",
            configuration={
                "properties": {"displayName": "Alpha (UI verified)", "tags": {"owner": "team-a"}}
            },
        ),
    }

    comparison = compare_snapshots(before, after)

    assert len(comparison.configuration_edits) == 1
    edit = comparison.configuration_edits[0]
    assert edit.change_kind == "configuration"
    assert edit.before == {
        "display_name": "Alpha",
        "configuration": {"properties": {"displayName": "Alpha", "tags": {"owner": "team-a"}}},
    }
    assert edit.after == {
        "display_name": "Alpha (UI verified)",
        "configuration": {
            "properties": {"displayName": "Alpha (UI verified)", "tags": {"owner": "team-a"}}
        },
    }


def test_compare_snapshots_coalesces_display_name_and_other_config_changes_to_one_edit() -> None:
    before = {
        "entity-a": SnapshotNode(
            "entity-a",
            "Alpha",
            HistoryBucketHealth.HEALTHY,
            1.0,
            "root",
            configuration={"properties": {"displayName": "Alpha", "healthObjective": "Availability"}},
        ),
    }
    after = {
        "entity-a": SnapshotNode(
            "entity-a",
            "Alpha Prime",
            HistoryBucketHealth.HEALTHY,
            1.0,
            "root",
            configuration={"properties": {"displayName": "Alpha Prime", "healthObjective": "Latency"}},
        ),
    }

    comparison = compare_snapshots(before, after)

    assert len(comparison.configuration_edits) == 1
    edit = comparison.configuration_edits[0]
    assert edit.change_kind == "configuration"
    assert edit.before["display_name"] == "Alpha"
    assert edit.after["display_name"] == "Alpha Prime"
    assert edit.before["configuration"]["properties"]["healthObjective"] == "Availability"
    assert edit.after["configuration"]["properties"]["healthObjective"] == "Latency"


def test_snapshot_key_requires_aware_utc_datetime() -> None:
    with pytest.raises(ValueError, match="UTC"):
        SnapshotKey(_model_key(), datetime(2026, 1, 1))

    key = SnapshotKey(_model_key(), datetime(2026, 1, 1, tzinfo=UTC))
    assert key.at_utc.tzinfo is not None


def _sample_forest(
    *,
    first_signal_value: float = 10.0,
    second_signal_value: float = 20.0,
    health: HealthState = HealthState.HEALTHY,
) -> Forest:
    entity = EntityNode(
        entity_id="/entities/e1",
        name="entity-1",
        display_name="Entity 1",
        health_state=health,
        icon_name="Resource",
        impact=Impact.STANDARD,
        signals=(
            SignalValue(
                name="sig-1",
                definition_name="def-1",
                display_name="Signal 1",
                signal_kind=SignalKind.PROMETHEUS_METRICS_QUERY,
                health_state=HealthState.HEALTHY,
                value=first_signal_value,
                data_unit=DataUnit.COUNT,
                reported_at="2026-01-01T00:00:00Z",
            ),
            SignalValue(
                name="sig-2",
                definition_name="def-2",
                display_name="Signal 2",
                signal_kind=SignalKind.PROMETHEUS_METRICS_QUERY,
                health_state=HealthState.HEALTHY,
                value=second_signal_value,
                data_unit=DataUnit.COUNT,
                reported_at="2026-01-01T00:00:00Z",
            ),
        ),
        children=(),
    )
    return Forest(roots=("entity-1",), entities={"entity-1": entity})


def test_project_snapshot_nodes_detects_query_only_definition_change_when_forest_unchanged() -> None:
    forest = _sample_forest()
    before_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={
            ("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {"displayName": "Entity 1"}}),
            ("signaldefinition", "def-1"): json.dumps({"name": "def-1", "properties": {"queryText": "old"}}),
            ("signaldefinition", "def-2"): json.dumps({"name": "def-2", "properties": {"queryText": "fixed"}}),
        },
    )
    after_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={
            ("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {"displayName": "Entity 1"}}),
            ("signaldefinition", "def-1"): json.dumps({"name": "def-1", "properties": {"queryText": "new"}}),
            ("signaldefinition", "def-2"): json.dumps({"name": "def-2", "properties": {"queryText": "fixed"}}),
        },
    )

    comparison = compare_snapshots(before_nodes, after_nodes)
    assert len(comparison.configuration_edits) == 1
    assert comparison.health_edits == ()
    assert comparison.value_edits == ()


def test_project_snapshot_nodes_first_signal_value_change_is_value_only_and_input_unchanged() -> None:
    forest_before = _sample_forest(first_signal_value=10.0)
    forest_after = _sample_forest(first_signal_value=11.0)
    resource_json = {("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {}})}
    resource_json_before = dict(resource_json)
    original_value = forest_before.entities["entity-1"].signals[0].value

    before_nodes = project_snapshot_nodes(model=_model_key(), forest=forest_before, resource_json=resource_json)
    after_nodes = project_snapshot_nodes(model=_model_key(), forest=forest_after, resource_json=resource_json)

    comparison = compare_snapshots(before_nodes, after_nodes)
    assert len(comparison.value_edits) == 1
    assert len(comparison.configuration_edits) == 0
    assert len(comparison.topology_edits) == 0
    assert resource_json == resource_json_before
    assert forest_before.entities["entity-1"].signals[0].value == original_value


def test_project_snapshot_nodes_second_signal_value_change_is_value_only_and_input_unchanged() -> None:
    forest_before = _sample_forest(second_signal_value=20.0)
    forest_after = _sample_forest(second_signal_value=25.0)
    resource_json = {("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {}})}
    resource_json_before = dict(resource_json)
    original_value = forest_before.entities["entity-1"].signals[1].value

    before_nodes = project_snapshot_nodes(model=_model_key(), forest=forest_before, resource_json=resource_json)
    after_nodes = project_snapshot_nodes(model=_model_key(), forest=forest_after, resource_json=resource_json)

    comparison = compare_snapshots(before_nodes, after_nodes)
    assert len(comparison.value_edits) == 1
    assert len(comparison.configuration_edits) == 0
    assert len(comparison.topology_edits) == 0
    assert resource_json == resource_json_before
    assert forest_before.entities["entity-1"].signals[1].value == original_value


def test_project_snapshot_nodes_second_parent_change_is_topology_only_and_input_unchanged() -> None:
    forest = _sample_forest()
    before_resource_json = {
        ("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {}}),
        ("relationship", "r1"): json.dumps(
            {"name": "r1", "properties": {"parentEntityName": "parent-a", "childEntityName": "entity-1"}}
        ),
    }
    after_resource_json = {
        ("entity", "entity-1"): json.dumps({"name": "entity-1", "properties": {}}),
        ("relationship", "r1"): json.dumps(
            {"name": "r1", "properties": {"parentEntityName": "parent-a", "childEntityName": "entity-1"}}
        ),
        ("relationship", "r2"): json.dumps(
            {"name": "r2", "properties": {"parentEntityName": "parent-b", "childEntityName": "entity-1"}}
        ),
    }
    before_copy = dict(before_resource_json)
    after_copy = dict(after_resource_json)
    original_values = tuple(signal.value for signal in forest.entities["entity-1"].signals)

    before_nodes = project_snapshot_nodes(model=_model_key(), forest=forest, resource_json=before_resource_json)
    after_nodes = project_snapshot_nodes(model=_model_key(), forest=forest, resource_json=after_resource_json)

    comparison = compare_snapshots(before_nodes, after_nodes)
    assert len(comparison.value_edits) == 0
    assert len(comparison.configuration_edits) == 0
    assert len(comparison.topology_edits) == 1
    assert before_resource_json == before_copy
    assert after_resource_json == after_copy
    assert tuple(signal.value for signal in forest.entities["entity-1"].signals) == original_values


def test_project_snapshot_nodes_ignores_etag_and_systemdata_metadata_noise() -> None:
    forest = _sample_forest()
    before_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={
            ("entity", "entity-1"): json.dumps(
                {
                    "name": "entity-1",
                    "etag": "v1",
                    "systemData": {"lastModifiedAt": "2026-01-01T00:00:00Z"},
                    "properties": {},
                }
            ),
        },
    )
    after_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={
            ("entity", "entity-1"): json.dumps(
                {
                    "name": "entity-1",
                    "etag": "v2",
                    "systemData": {"lastModifiedAt": "2026-01-01T01:00:00Z"},
                    "properties": {},
                }
            ),
        },
    )

    comparison = compare_snapshots(before_nodes, after_nodes)
    assert len(comparison.configuration_edits) == 0


def test_project_snapshot_nodes_ignores_computed_status_only_config_changes() -> None:
    forest = _sample_forest(health=HealthState.HEALTHY)
    raw_entity_before = {
        "name": "entity-1",
        "properties": {
            "healthState": "Healthy",
            "signalGroups": {"default": {"signals": [{"name": "sig-1", "status": {"healthState": "Healthy"}}]}},
        },
    }
    raw_entity_after = {
        "name": "entity-1",
        "properties": {
            "healthState": "Unhealthy",
            "signalGroups": {"default": {"signals": [{"name": "sig-1", "status": {"healthState": "Unhealthy"}}]}},
        },
    }
    before_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={("entity", "entity-1"): json.dumps(raw_entity_before)},
    )
    after_nodes = project_snapshot_nodes(
        model=_model_key(),
        forest=forest,
        resource_json={("entity", "entity-1"): json.dumps(raw_entity_after)},
    )
    comparison = compare_snapshots(before_nodes, after_nodes)
    assert comparison.configuration_edits == ()
