from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from azext_healthmodels_plus.tui.domain.history import RangeKey
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.domain.occurrences import project
from azext_healthmodels_plus.tui.watch.poller import Poller, Ready


def _entity(model: str, name: str, *, health: str = "Healthy", target: str = "") -> dict[str, Any]:
    props: dict[str, Any] = {"displayName": name, "healthState": health}
    if target:
        props["signalGroups"] = {"azureResource": {"azureResourceId": target, "authenticationSetting": "auth"}}
    return {
        "id": f"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/{model}/entities/{name}",
        "name": name,
        "properties": props,
    }


class _TemporalClient:
    def __init__(self) -> None:
        self.arm_endpoint = "https://management.azure.com"
        self._models: dict[tuple[str, str], list[dict[str, Any]]] = {
            (
                "rg",
                "root",
            ): [
                _entity("root", "root"),
                _entity(
                    "root",
                    "shared",
                    target="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/nested",
                ),
            ],
            ("rg", "nested"): [_entity("nested", "nested"), _entity("nested", "leaf", target="/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/grandchild")],
            ("rg", "grandchild"): [_entity("grandchild", "grandchild"), _entity("grandchild", "node")],
        }
        self._definitions: dict[tuple[str, str], list[dict[str, Any]]] = {
            ("rg", "root"): [{"name": "same-def", "properties": {"signalKind": "External"}}],
            ("rg", "nested"): [{"name": "same-def", "properties": {"signalKind": "External"}}],
            ("rg", "grandchild"): [{"name": "same-def", "properties": {"signalKind": "External"}}],
        }
        self._relationships: dict[tuple[str, str], list[dict[str, Any]]] = {
            ("rg", "root"): [{"name": "edge", "properties": {"parentEntityName": "root", "childEntityName": "shared"}}],
            ("rg", "nested"): [{"name": "edge", "properties": {"parentEntityName": "nested", "childEntityName": "leaf"}}],
            ("rg", "grandchild"): [{"name": "edge", "properties": {"parentEntityName": "grandchild", "childEntityName": "node"}}],
        }
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def model_key(self, group: str, model: str) -> ModelKey:
        return ModelKey(self.arm_endpoint, "00000000-0000-0000-0000-000000000001", group, model)

    def for_model(self, target: ModelKey) -> _TemporalClient:
        return self

    def monitor_reads(self, _stopped: threading.Event):
        class _Context:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

        return _Context()

    def get_entity(self, group: str, model: str, entity_name: str) -> dict[str, Any]:
        self.calls.append(("get_entity", (group, model, entity_name), {}))
        return next(e for e in self._models[(group, model)] if e["name"].casefold() == entity_name.casefold())

    def list_signal_definitions(self, group: str, model: str, *, timestamp: datetime | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_signal_definitions", (group, model), {"timestamp": timestamp}))
        return self._definitions[(group, model)]

    def list_entities(self, group: str, model: str, *, timestamp: datetime | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_entities", (group, model), {"timestamp": timestamp}))
        return self._models[(group, model)]

    def list_relationships(self, group: str, model: str, *, timestamp: datetime | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_relationships", (group, model), {"timestamp": timestamp}))
        return self._relationships[(group, model)]

    def get_entity_history(
        self,
        group: str,
        model: str,
        entity_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("get_entity_history", (group, model, entity_name), body))
        return {
            "entityName": entity_name,
            "signalName": body.get("signalName"),
            "history": [
                {"occurredAt": "2026-01-01T00:00:00Z", "value": None, "healthState": "Unknown", "reason": "no sample"},
                {"occurredAt": "2026-01-01T00:05:00Z", "value": 2.0, "healthState": "Healthy", "reason": None},
            ],
        }

    def get_signal_history(
        self,
        group: str,
        model: str,
        entity_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("get_signal_history", (group, model, entity_name), body))
        return {
            "entityName": entity_name,
            "signalName": body.get("signalName"),
            "history": [
                {"occurredAt": "2026-01-01T00:00:00Z", "value": 1.0, "healthState": "Healthy", "reason": None},
            ],
        }


def test_view_at_has_no_live_fallback() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller.poll_once()
    historical = poller.view_at(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert historical.root == poller.root
    assert not isinstance(historical.entries[historical.root], Ready)
    assert historical.entries[historical.root].previous is None


@pytest.mark.parametrize("target_name", [None, "nested"])
def test_open_at_uses_timestamped_reads_and_publishes_complete_snapshot(target_name: str | None) -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    target = None if target_name is None else client.model_key("rg", target_name)
    at = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    poller.open_at(at, target=target)
    poller.wait()
    view = poller.view_at(at)
    model = poller.root if target is None else target
    assert isinstance(view.entries[model], Ready)
    assert view.entries[model].previous.snapshot.timestamp == "2026-01-01T00:30:00Z"
    assert view.entries[model].previous.captured_at
    calls = [c for c in client.calls if c[0] in {"list_signal_definitions", "list_entities", "list_relationships"}]
    assert calls
    assert all(c[2].get("timestamp") == at for c in calls)


def test_same_model_different_times_do_not_collide_in_running_identity() -> None:
    client = _TemporalClient()
    gate = threading.Event()
    original = client.list_entities

    def blocked(*args, **kwargs):
        gate.wait(2)
        return original(*args, **kwargs)

    client.list_entities = blocked  # type: ignore[assignment]

    poller = Poller(client, "rg", "root")
    t1 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    poller.open_at(t1)
    poller.open_at(t2)
    deadline = time.time() + 2
    while time.time() < deadline and len(poller._running) < 2:  # noqa: SLF001
        time.sleep(0.01)
    assert len(poller._running) == 2  # noqa: SLF001
    gate.set()
    poller.wait()


def test_release_at_withdraws_only_queued_snapshot_demand() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller._running["slot-a"] = object()  # type: ignore[index, assignment]  # noqa: SLF001
    poller._running["slot-b"] = object()  # type: ignore[index, assignment]  # noqa: SLF001
    at = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    poller.open_at(at)
    poller.release_at(at)
    view = poller.view_at(at)
    assert view.entries[view.root].previous is None


def test_request_range_and_range_view_preserve_missing_values_and_reasons() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(key)
    poller.wait()
    view = poller.range_view(key)
    assert view.status == "ready"
    entity_calls = [c for c in client.calls if c[0] == "get_entity_history"]
    assert entity_calls
    assert "signalName" not in entity_calls[0][2]
    first = view.events[0]
    assert first.value is None
    assert first.health.value == "Unknown"
    assert first.reason == "no sample"


@pytest.mark.parametrize(
    ("history", "error"),
    [
        (
            [{"occurredAt": "2026-01-01T00:00:00Z", "healthState": "Healthy"},
             {"occurredAt": "2026-01-01T00:00:00Z", "healthState": "Degraded"}],
            "Duplicate timestamp",
        ),
        ([{"occurredAt": "2026-01-01T00:00:00Z", "healthState": "Bogus"}], "Unsupported health value"),
        ([{"occurredAt": "2026-01-01T00:00:00Z", "healthState": "Healthy", "value": "NaN"}], "Invalid numeric value"),
        (None, "TypeError"),
    ],
)
def test_malformed_range_history_fails_the_range(history: Any, error: str) -> None:
    client = _TemporalClient()
    client.get_entity_history = lambda *_args: {"history": history}  # type: ignore[method-assign]
    poller = Poller(client, "rg", "root")
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(key)
    poller.wait()
    view = poller.range_view(key)
    assert view.status == "failed"
    assert error in view.error


def test_signal_range_routes_to_signal_history_with_signal_name() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        signal_name="same-signal",
    )
    poller.request_range(key)
    poller.wait()
    signal_calls = [c for c in client.calls if c[0] == "get_signal_history"]
    assert signal_calls
    assert signal_calls[0][2]["signalName"] == "same-signal"


def test_open_at_rejects_naive_datetime() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        poller.open_at(datetime(2026, 1, 1, 0, 0))


def test_historical_timestamp_rejection_does_not_fallback_to_untimestamped_calls() -> None:
    class _RejectingTemporalClient(_TemporalClient):
        def __init__(self) -> None:
            super().__init__()
            self.timestamp_probe: list[dict[str, Any]] = []

        def list_signal_definitions(self, group: str, model: str, *args, **kwargs):  # type: ignore[override]
            self.timestamp_probe.append({"method": "defs", "kwargs": dict(kwargs)})
            raise TypeError("timestamp unsupported")

        def list_entities(self, group: str, model: str, *args, **kwargs):  # type: ignore[override]
            self.timestamp_probe.append({"method": "entities", "kwargs": dict(kwargs)})
            raise AssertionError("unexpected fallback call")

        def list_relationships(self, group: str, model: str, *args, **kwargs):  # type: ignore[override]
            self.timestamp_probe.append({"method": "rels", "kwargs": dict(kwargs)})
            raise AssertionError("unexpected fallback call")

    client = _RejectingTemporalClient()
    poller = Poller(client, "rg", "root")
    at = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    poller.open_at(at)
    poller.wait()
    view = poller.view_at(at)
    state = view.entries[poller.root]
    assert state.error
    assert len(client.timestamp_probe) == 1
    assert "timestamp" in client.timestamp_probe[0]["kwargs"]


def test_open_at_and_request_range_are_ignored_after_close() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller.close()
    at = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    poller.open_at(at)
    poller.request_range(
        RangeKey(
            model=poller.root,
            entity_name="shared",
            start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
    )
    assert poller.view_at(at).entries[poller.root].previous is None


def test_duplicate_request_range_loading_joins_existing_job() -> None:
    client = _TemporalClient()
    gate = threading.Event()
    original = client.get_entity_history

    def blocked(group: str, model: str, entity_name: str, body: dict[str, Any]) -> dict[str, Any]:
        gate.wait(2)
        return original(group, model, entity_name, body)

    client.get_entity_history = blocked  # type: ignore[assignment]
    poller = Poller(client, "rg", "root")
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(key)
    poller.request_range(key)
    deadline = time.time() + 2
    while time.time() < deadline and poller.range_view(key).status == "queued":
        time.sleep(0.01)
    assert poller.range_view(key).status == "loading"
    gate.set()
    poller.wait()
    assert len([c for c in client.calls if c[0] == "get_entity_history"]) == 1


def test_release_range_withdraws_queued_only() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller._running["slot-a"] = object()  # type: ignore[index, assignment]  # noqa: SLF001
    poller._running["slot-b"] = object()  # type: ignore[index, assignment]  # noqa: SLF001
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(key)
    poller.release_range(key)
    assert poller.range_view(key).status == "unloaded"


def test_range_view_events_are_immutable() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    key = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(key)
    poller.wait()
    events = poller.range_view(key).events
    assert isinstance(events, tuple)
    with pytest.raises(AttributeError):
        events[0].value = 999  # type: ignore[misc]


def test_view_at_aggregates_all_models_for_same_time_and_preserves_occurrence_projection() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    at = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    nested = client.model_key("rg", "nested")
    grandchild = client.model_key("rg", "grandchild")
    poller.open_at(at)
    poller.open_at(at, target=nested)
    poller.open_at(at, target=grandchild)
    poller.wait()
    view = poller.view_at(at)
    assert view.root == poller.root
    assert set(view.entries) >= {poller.root, nested, grandchild}
    forests = {k: s.previous.forest for k, s in view.entries.items() if s.previous}
    rows = project(view.root, forests, None)
    assert any(row.model == nested and row.entity.name == "nested" for row in rows)
    assert any(row.model == grandchild and row.entity.name == "grandchild" for row in rows)


def test_snapshot_cache_keeps_active_instant_working_set_until_release_and_then_bounds_idle_retention() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller._MAX_HISTORY_CACHE = 8  # noqa: SLF001

    active_at = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    nested = client.model_key("rg", "nested")

    # Active working set: root + nested for the same instant.
    poller.open_at(active_at, purpose="view")
    poller.open_at(active_at, target=nested, purpose="view")
    poller.wait()

    # Repeated ready demand from same purpose is idempotent.
    poller.open_at(active_at, purpose="view")
    # Overlapping purpose keeps same instant pinned independently.
    poller.open_at(active_at, purpose="baseline")

    # Push cache beyond bound with non-active instants and release each.
    for minute in range(1, 18):
        at = active_at + timedelta(minutes=minute)
        poller.open_at(at, purpose="prefetch")
        poller.wait()
        poller.release_at(at, purpose="prefetch")

    active_view = poller.view_at(active_at)
    assert isinstance(active_view.entries[poller.root], Ready)
    assert isinstance(active_view.entries[nested], Ready)

    # Release one overlapping purpose only; instant remains pinned.
    poller.release_at(active_at, purpose="baseline")
    for minute in range(18, 26):
        at = active_at + timedelta(minutes=minute)
        poller.open_at(at, purpose="prefetch")
        poller.wait()
        poller.release_at(at, purpose="prefetch")
    assert isinstance(poller.view_at(active_at).entries[poller.root], Ready)

    # Full release unpins and allows eviction under bounded retention.
    poller.release_at(active_at, purpose="view")
    poller.release_at(active_at, target=nested, purpose="view")
    for minute in range(26, 40):
        at = active_at + timedelta(minutes=minute)
        poller.open_at(at, purpose="prefetch")
        poller.wait()
        poller.release_at(at, purpose="prefetch")
    assert len(poller._snapshots) <= poller._MAX_HISTORY_CACHE  # noqa: SLF001
    assert poller.view_at(active_at).entries[poller.root].previous is None

    poller.close()
    poller.open_at(datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc), purpose="view")
    assert poller.view_at(datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)).entries[poller.root].previous is None


def test_range_cache_keeps_demanded_rows_under_pressure_and_bounds_idle_rows() -> None:
    client = _TemporalClient()
    poller = Poller(client, "rg", "root")
    poller._MAX_HISTORY_CACHE = 8  # noqa: SLF001

    active = RangeKey(
        model=poller.root,
        entity_name="shared",
        start_utc=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        end_utc=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    poller.request_range(active, purpose="view")
    poller.wait()
    assert poller.range_view(active).status == "ready"

    # Pressure with released idle rows should not evict active demanded row.
    for minute in range(1, 18):
        key = RangeKey(
            model=poller.root,
            entity_name="shared",
            start_utc=datetime(2026, 1, 1, minute, 0, tzinfo=timezone.utc),
            end_utc=datetime(2026, 1, 1, minute + 1, 0, tzinfo=timezone.utc),
        )
        poller.request_range(key, purpose="prefetch")
        poller.wait()
        poller.release_range(key, purpose="prefetch")
    assert poller.range_view(active).status == "ready"

    # Release active row and force eviction pressure.
    poller.release_range(active, purpose="view")
    for minute in range(18, 40):
        key = RangeKey(
            model=poller.root,
            entity_name="shared",
            start_utc=datetime(2026, 1, 2, minute - 18, 0, tzinfo=timezone.utc),
            end_utc=datetime(2026, 1, 2, minute - 17, 0, tzinfo=timezone.utc),
        )
        poller.request_range(key, purpose="prefetch")
        poller.wait()
        poller.release_range(key, purpose="prefetch")
    assert len(poller._ranges) <= poller._MAX_HISTORY_CACHE  # noqa: SLF001
