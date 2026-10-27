"""The monitor's cache, finite state machine and bounded effect runner."""
from __future__ import annotations

import logging
import threading
import json
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

from azext_healthmodels_plus.tui.client.errors import describe_error
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.domain.formatters import format_utc
from azext_healthmodels_plus.tui.domain.graph_builder import build_forest
from azext_healthmodels_plus.tui.domain.history import HistoryEvent, RangeKey, SnapshotKey, parse_history_events
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.domain.parse import parse_entities, parse_relationships, parse_signal_definitions
from azext_healthmodels_plus.tui.domain.snapshot import build_snapshot, diff_snapshots
from azext_healthmodels_plus.tui.models.domain import Forest, SignalDefinition, Snapshot, StateChange
from azext_healthmodels_plus.tui.models.enums import HealthState

_log = logging.getLogger(__name__)
Reason = Literal["navigation", "manual", "root", "active", "health"]
Path = tuple[str, ...]


@dataclass(frozen=True)
class ModelSnapshot:
    forest: Forest
    snapshot: Snapshot
    root_health: HealthState | None
    changes: tuple[StateChange, ...]
    definitions: Mapping[str, SignalDefinition]
    captured_at: str = field(default="", kw_only=True)
    resource_json: Mapping[tuple[str, str], str] = field(default_factory=dict, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", MappingProxyType(dict(self.definitions)))
        object.__setattr__(self, "resource_json", MappingProxyType(dict(self.resource_json)))


@dataclass(frozen=True)
class Unloaded:
    previous: ModelSnapshot | None = None
    generation: int = 0
    error: str = ""
    last_check: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class Ready(Unloaded):
    previous: ModelSnapshot = field()


@dataclass(frozen=True)
class Stale(Unloaded):
    previous: ModelSnapshot = field()


@dataclass(frozen=True)
class Failed(Unloaded):
    pass


@dataclass(frozen=True)
class Stopped(Unloaded):
    pass


@dataclass(frozen=True)
class Queued(Unloaded):
    reasons: frozenset[Reason] = frozenset()
    sequence: int = 0
    resume: str = field(default="unloaded", kw_only=True)


@dataclass(frozen=True)
class Checking(Queued):
    token: int = 0
    started: int = 0


@dataclass(frozen=True)
class Loading(Checking):
    pass


State = Unloaded | Ready | Stale | Failed | Stopped | Queued | Checking | Loading


def _demand(state: State, reason: Reason, sequence: int) -> State:
    """Same-key demands join an existing job, never create a replacement."""
    if isinstance(state, Stopped):
        return state
    if isinstance(state, Queued):
        return replace(state, reasons=state.reasons | {reason})
    return Queued(state.previous, state.generation, state.error, frozenset({reason}), sequence,
                  resume=type(state).__name__.lower(), last_check=state.last_check)


def _settled(state: Queued) -> State:
    """The resting state a queued entry returns to once its demand is withdrawn."""
    cls = (Stale if state.resume == "stale" else Failed if state.error
           else Ready if state.previous else Unloaded)
    return cls(state.previous, state.generation, state.error, last_check=state.last_check)


def _label(state: State) -> str:
    name = type(state).__name__.lower()
    if isinstance(state, Ready):
        return "cached"
    if state.previous is not None and isinstance(state, (Queued, Checking, Failed)):
        return f"{name}, stale"
    return name


@dataclass(frozen=True)
class MonitorView:
    root: ModelKey
    entries: Mapping[ModelKey, State]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))

    @property
    def summary(self) -> str:
        counts = Counter("cached" if isinstance(s, Ready) else type(s).__name__.lower()
                         for s in self.entries.values())
        stale = sum(s.previous is not None and isinstance(s, (Queued, Failed, Stale))
                    for s in self.entries.values())
        if stale:
            counts["stale"] = stale
        return f"Root {self.root.name}: {_label(self.entries[self.root])}; Models: " + ", ".join(
            f"{n} {state}" for state, n in sorted(counts.items())
        )

    def status(self, key: ModelKey) -> str:
        state = self.entries.get(key, Unloaded())
        captured = f"; captured {format_utc(state.previous.snapshot.timestamp)}" if state.previous else ""
        checked = f"; root checked {format_utc(state.last_check)}" if state.last_check else ""
        error = f"; {state.error}" if state.error else ""
        return _label(state) + captured + checked + error


@dataclass
class PollResult:
    forest: Forest
    snapshot: Snapshot
    changes: list[StateChange] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class RangeView:
    status: Literal["unloaded", "queued", "loading", "ready", "failed", "stopped"] = "unloaded"
    sequence: int = 0
    events: tuple[HistoryEvent, ...] | None = None  # parsed once in the worker; immutable, so shared freely
    error: str = ""


class Poller:
    """One session owner. UI/plain adapters submit demand, never perform loads.

    State changes and queue decisions are serialized under the condition.
    Worker futures occupy slots until synchronous SDK work really completes.
    """

    def __init__(self, client: CloudHealthClient, rg: str, model: str) -> None:
        self._client = client
        key = client.model_key(rg, model) if isinstance(client, CloudHealthClient) else None
        self.root = key if isinstance(key, ModelKey) else ModelKey("https://management.azure.com", "", rg, model)
        self._entries: dict[ModelKey, State] = {self.root: Unloaded()}
        self._bindings: dict[tuple[ModelKey, str], tuple[ModelKey, HealthState]] = {}
        self._candidates: dict[tuple[ModelKey, str], tuple[ModelKey, HealthState]] = {}
        self._condition = threading.Condition(threading.RLock())
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="health-model")
        self._running: dict[tuple[str, Any], Future[Any]] = {}
        self._stop = threading.Event()
        self._sequence = 0
        self._token = 0
        self._advancing = False
        self._active: ModelKey | None = None
        self._visible: dict[ModelKey, int] = {}
        self._listener: Callable[[], None] | None = None
        self._snapshots: dict[SnapshotKey, State] = {}
        self._ranges: dict[RangeKey, RangeView] = {}
        self._advance_requested = False
        self._snapshot_demands: dict[SnapshotKey, set[str]] = {}
        self._range_demands: dict[RangeKey, set[str]] = {}

    @property
    def view(self) -> MonitorView:
        with self._condition:
            return MonitorView(self.root, self._entries)

    def listen(self, listener: Callable[[], None]) -> None:
        self._listener = listener

    def refresh(self, *, manual: bool = False) -> None:
        with self._condition:
            self._request(self.root, "manual" if manual else "root")
            if self._active is not None and self._active != self.root:
                self._request(self._active, "manual" if manual else "active")
            self._advance()

    def navigate(self, active: ModelKey | None, distances: Mapping[ModelKey, int]) -> None:
        with self._condition:
            self._visible = dict(distances)
            for key in self._entries.keys() - self._visible.keys():
                self._withdraw(key, set())  # a reference kept only for being expanded goes once it is not
            if active != self._active:
                self._withdraw(self._active, {"navigation", "active"})
                self._active = active
                if active is not None and active != self.root:
                    self._request(active, "navigation")
            self._advance()

    def expand(self, target: ModelKey, distances: Mapping[ModelKey, int]) -> None:
        with self._condition:
            self._visible = dict(distances)
            for key in self._entries.keys() - self._visible.keys():
                self._withdraw(key, set())
            activation = target != self._active
            if activation:
                self._withdraw(self._active, {"navigation", "active"})
                self._active = target
            state = self._entries.get(target, Unloaded())
            if activation or isinstance(state, (Queued, Failed)) or type(state) is Unloaded:
                self._request(target, "navigation")
            self._advance()

    def wait(self) -> None:
        with self._condition:
            self._condition.wait_for(lambda: self._stop.is_set() or (
                not self._running
                and not any(type(s) is Queued for s in self._entries.values())
                and not any(type(s) is Queued for s in self._snapshots.values())
                and not any(v.status == "queued" for v in self._ranges.values())
            ))

    def poll_once(self) -> PollResult:
        self.refresh()
        self.wait()
        state = self.view.entries[self.root]
        previous = state.previous
        return PollResult(
            previous.forest if previous else Forest((), {}),
            previous.snapshot if previous else Snapshot({}, ""),
            list(previous.changes) if previous else [],
            (state.error + " - showing stale data") if state.error else None,
        )

    def close(self) -> None:
        with self._condition:
            self._stop.set()
            self._listener = None
            self._entries = {
                k: Stopped(s.previous, s.generation, s.error, last_check=s.last_check)
                for k, s in self._entries.items()
            }
            self._snapshots = {
                k: Stopped(s.previous, s.generation, s.error, last_check=s.last_check)
                for k, s in self._snapshots.items()
            }
            self._ranges = {
                k: replace(v, status="stopped") for k, v in self._ranges.items()
            }
            self._snapshot_demands.clear()
            self._range_demands.clear()
            self._condition.notify_all()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _snapshot_key(self, at: datetime, target: ModelKey | None) -> SnapshotKey:
        return SnapshotKey(target or self.root, at)

    @staticmethod
    def _resource_json(
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        definitions: list[dict[str, Any]],
    ) -> Mapping[tuple[str, str], str]:
        return {
            (kind, name): json.dumps(resource, sort_keys=True, separators=(",", ":"))
            for kind, resources in (
                ("entity", entities), ("relationship", relationships), ("signaldefinition", definitions),
            )
            for resource in resources
            if (name := str(resource.get("name", "")).casefold())
        }

    def open_at(self, at: datetime, target: ModelKey | None = None, *, purpose: str = "default") -> None:
        with self._condition:
            if self._stop.is_set():
                return
            key = self._snapshot_key(at, target)
            if not purpose:
                raise ValueError("open_at purpose must be non-empty")
            self._snapshot_demands.setdefault(key, set()).add(purpose)
            self._sequence += 1
            self._snapshots[key] = _demand(self._snapshots.get(key, Unloaded()), "navigation", self._sequence)
            self._prune_snapshots()
            self._advance()

    def release_at(self, at: datetime, target: ModelKey | None = None, *, purpose: str = "default") -> None:
        with self._condition:
            key = self._snapshot_key(at, target)
            owners = self._snapshot_demands.get(key)
            if owners is not None:
                owners.discard(purpose)
                if not owners:
                    self._snapshot_demands.pop(key, None)
            state = self._snapshots.get(key)
            if type(state) is Queued:
                self._snapshots[key] = _settled(state)
                self._prune_snapshots()
            self._advance()

    def view_at(self, at: datetime) -> MonitorView:
        with self._condition:
            stamp = SnapshotKey(self.root, at).at_utc
            entries = {
                key.model: state
                for key, state in self._snapshots.items()
                if key.at_utc == stamp
            }
            if self.root not in entries:
                entries[self.root] = Unloaded()
            return MonitorView(self.root, entries)

    def request_range(self, key: RangeKey, *, purpose: str = "default") -> None:
        with self._condition:
            if self._stop.is_set():
                return
            if not purpose:
                raise ValueError("request_range purpose must be non-empty")
            self._range_demands.setdefault(key, set()).add(purpose)
            self._sequence += 1
            entry = self._ranges.get(key, RangeView())
            if entry.status in {"queued", "loading", "ready"}:
                return
            self._ranges[key] = replace(entry, status="queued", sequence=self._sequence)
            self._prune_ranges()
            self._advance()

    def release_range(self, key: RangeKey, *, purpose: str = "default") -> None:
        with self._condition:
            owners = self._range_demands.get(key)
            if owners is not None:
                owners.discard(purpose)
                if not owners:
                    self._range_demands.pop(key, None)
            entry = self._ranges.get(key)
            if entry and entry.status == "queued":
                self._ranges[key] = replace(entry, status="unloaded")
                self._prune_ranges()
            self._advance()

    def range_view(self, key: RangeKey) -> RangeView:
        with self._condition:
            return self._ranges.get(key, RangeView())

    def _request(self, key: ModelKey, reason: Reason) -> None:
        if self._stop.is_set():
            return
        self._sequence += 1
        self._entries[key] = _demand(self._entries.get(key, Unloaded()), reason, self._sequence)

    def _withdraw(self, key: ModelKey | None, reasons: set[Reason]) -> None:
        if key is None:
            return
        state = self._entries.get(key)
        if isinstance(state, Queued):
            remaining = state.reasons - reasons
            # An expanded reference with nothing loaded yet keeps its load, at visible-distance priority.
            if remaining or isinstance(state, Checking) or (state.previous is None and key in self._visible):
                self._entries[key] = replace(state, reasons=remaining)
            else:
                self._entries[key] = _settled(state)

    def _priority(self, item: tuple[ModelKey, State]) -> tuple:
        key, state = item
        assert isinstance(state, Queued)
        rank = 0 if state.reasons & {"navigation", "manual"} else 1 if "root" in state.reasons else 2
        return rank, self._visible.get(key, float("inf")), state.sequence, key.canonical

    def _advance(self) -> None:
        if self._stop.is_set():
            return
        if self._advancing:
            self._advance_requested = True
            return
        self._advancing = True
        try:
            while True:
                self._advance_requested = False
                while len(self._running) < 2:
                    live_queue = [
                        (k, s) for k, s in self._entries.items()
                        if type(s) is Queued and ("live", k) not in self._running
                    ]
                    snapshot_queue = [
                        (k, s) for k, s in self._snapshots.items()
                        if type(s) is Queued and ("snapshot", k) not in self._running
                    ]
                    range_queue = [
                        (k, v) for k, v in self._ranges.items()
                        if v.status == "queued" and ("range", k) not in self._running
                    ]
                    if not live_queue and not snapshot_queue and not range_queue:
                        break
                    candidate: tuple[Any, Any, str] | None = None
                    if live_queue:
                        k, s = min(live_queue, key=self._priority)
                        candidate = (k, s, "live")
                    if snapshot_queue:
                        k, s = min(snapshot_queue, key=lambda item: (0, item[1].sequence, item[0].at_utc, item[0].model.canonical))
                        probe = (k, s, "snapshot")
                        if candidate is None or (0, s.sequence) < (
                            1 if candidate[2] == "live" and not candidate[1].reasons & {"navigation", "manual"} else 0,
                            candidate[1].sequence,
                        ):
                            candidate = probe
                    if range_queue:
                        k, v = min(range_queue, key=lambda item: (item[1].sequence, repr(item[0])))
                        probe = (k, v, "range")
                        if candidate is None:
                            candidate = probe
                    assert candidate is not None
                    if candidate[2] == "range":
                        self._start_range(candidate[0], candidate[1])
                        continue
                    key, state = candidate[0], candidate[1]
                    self._token += 1
                    cls = Checking if candidate[2] == "live" and state.reasons == {"health"} and state.previous else Loading
                    running = cls(
                        state.previous, state.generation, state.error, state.reasons,
                        state.sequence, self._token, state.generation,
                        last_check=state.last_check,
                    )
                    if candidate[2] == "live":
                        self._start(key, running)
                    else:
                        self._start_snapshot(key, running)
                if self._listener:
                    self._listener()
                self._condition.notify_all()
                if not self._advance_requested:
                    break
        finally:
            self._advancing = False

    def _start(self, key: ModelKey, command: Checking) -> None:
        self._entries[key] = command
        if self._listener:
            self._listener()
        future = self._executor.submit(self._run, key, command)
        run_key = ("live", key)
        self._running[run_key] = future
        future.add_done_callback(lambda f: self._complete(key, command.token, f, run_key))

    def _start_snapshot(self, key: SnapshotKey, command: Checking) -> None:
        self._snapshots[key] = command
        if self._listener:
            self._listener()
        future = self._executor.submit(self._run_snapshot, key, command)
        run_key = ("snapshot", key)
        self._running[run_key] = future
        future.add_done_callback(lambda f: self._complete_snapshot(key, command.token, f, run_key))

    def _start_range(self, key: RangeKey, command: RangeView) -> None:
        self._ranges[key] = replace(command, status="loading")
        if self._listener:
            self._listener()
        future = self._executor.submit(self._run_range, key)
        run_key = ("range", key)
        self._running[run_key] = future
        future.add_done_callback(lambda f: self._complete_range(key, f, run_key))

    def _client_for(self, model: ModelKey) -> Any:
        return self._client if model == self.root else self._client.for_model(model)

    def _load(self, client: Any, model: ModelKey, at: datetime | None, previous: ModelSnapshot | None) -> ModelSnapshot:
        """Read definitions, entities and relationships live (``at=None``) or at a UTC instant."""
        timestamp = {} if at is None else {"timestamp": at}
        self._check_live()
        raw_definitions = client.list_signal_definitions(model.group, model.name, **timestamp)
        definitions = parse_signal_definitions(raw_definitions)
        self._check_live()
        raw_entities = client.list_entities(model.group, model.name, **timestamp)
        entities = parse_entities(raw_entities, definitions, model.endpoint)
        self._check_live()
        raw_relationships = client.list_relationships(model.group, model.name, **timestamp)
        relationships = parse_relationships(raw_relationships)
        self._check_live()
        forest = build_forest(entities, relationships)
        root = next((e for e in entities.values() if e.name.casefold() == model.name.casefold()), None)
        if at is None and model != self.root and root is None:
            raise ValueError(f"Missing expected nested root entity {model.name!r} in {model.label}")
        captured_at = datetime.now(timezone.utc).isoformat()
        snapshot = build_snapshot(forest, captured_at if at is None else at.isoformat().replace("+00:00", "Z"))
        return ModelSnapshot(
            forest, snapshot, root.health_state if root else None,
            tuple(diff_snapshots(previous.snapshot if previous else None, snapshot)),
            definitions,
            captured_at=captured_at,
            resource_json=self._resource_json(raw_entities, raw_relationships, raw_definitions),
        )

    def _run(self, key: ModelKey, command: Checking) -> ModelSnapshot | None:
        client = self._client_for(key)
        with client.monitor_reads(self._stop):
            self._check_live()
            if type(command) is Checking:
                raw = client.get_entity(key.group, key.name, key.name)
                root = parse_entities([raw], {}, key.endpoint)
                actual = next(iter(root.values())).health_state
                with self._condition:
                    current = self._entries[key]
                    if not isinstance(current, Checking) or current.token != command.token:
                        return None
                    if current.generation != command.started:
                        return None
                    current = replace(current, last_check=datetime.now(timezone.utc).isoformat())
                    self._entries[key] = current
                    if actual == command.previous.root_health and current.reasons == {"health"}:
                        return None
                    self._entries[key] = Loading(
                        current.previous, current.generation, current.error, current.reasons,
                        current.sequence, current.token, current.started,
                        last_check=current.last_check,
                    )
                    if self._listener:
                        self._listener()
            return self._load(client, key, None, command.previous)

    def _run_snapshot(self, key: SnapshotKey, command: Checking) -> ModelSnapshot | None:
        client = self._client_for(key.model)
        with client.monitor_reads(self._stop):
            return self._load(client, key.model, key.at_utc, command.previous)

    def _run_range(self, key: RangeKey) -> tuple[HistoryEvent, ...]:
        model = key.model
        client = self._client_for(model)
        body = {
            "startAt": key.start_utc.isoformat().replace("+00:00", "Z"),
            "endAt": key.end_utc.isoformat().replace("+00:00", "Z"),
        }
        if key.signal_name is not None:
            body["signalName"] = key.signal_name
        with client.monitor_reads(self._stop):
            self._check_live()
            if key.signal_name is None:
                result = client.get_entity_history(model.group, model.name, key.entity_name, body)
            else:
                result = client.get_signal_history(model.group, model.name, key.entity_name, body)
            self._check_live()
            if not isinstance(result, Mapping):
                raise TypeError("Range response must be a mapping")
            # Parsing here means malformed history fails this range instead of raising in the UI.
            return parse_history_events(result.get("history", []))

    def _check_live(self) -> None:
        if self._stop.is_set():
            raise InterruptedError("Monitor stopped")

    def _complete(self, key: ModelKey, token: int, future: Future[ModelSnapshot | None], run_key: object) -> None:
        with self._condition:
            self._running.pop(run_key, None)
            if self._stop.is_set():
                self._condition.notify_all()
                return
            state = self._entries[key]
            if not isinstance(state, Checking) or state.token != token:
                self._advance()
                return
            if state.generation != state.started:
                # Only one latest replacement. Matching final health cancels obsolete work.
                self._entries[key] = (
                    Stale(state.previous, state.generation, state.error, last_check=state.last_check)
                    if state.previous else Unloaded(generation=state.generation, error=state.error)
                )
                if self._needed(key) and state.previous is None:
                    self._request(key, "navigation")
                elif self._needed(key) and self._conflicts(key):
                    self._request(key, "health")
                if state.reasons & {"navigation", "manual", "active"} and self._needed(key):
                    self._request(key, "navigation")
                self._advance()
                return
            try:
                snapshot = future.result()
            except Exception as exc:
                _log.exception("Model read failed: %s", key.label)
                self._entries[key] = Failed(state.previous, state.generation, describe_error(exc), last_check=state.last_check)
            else:
                if snapshot is None and state.reasons - {"health"}:
                    # Demand can join after the worker's check decision but before publication.
                    self._start(key, Loading(
                        state.previous, state.generation, state.error, state.reasons,
                        state.sequence, state.token, state.started, last_check=state.last_check,
                    ))
                    self._advance()
                    return
                snapshot = snapshot or state.previous
                assert snapshot is not None
                self._entries[key] = Ready(snapshot, state.generation, last_check=state.last_check)
                self._candidates = {k: v for k, v in self._candidates.items() if v[0] != key}
                if snapshot is not state.previous:
                    self._observe(key, snapshot)
            self._advance()

    def _complete_snapshot(
        self, key: SnapshotKey, token: int, future: Future[ModelSnapshot | None], run_key: object,
    ) -> None:
        with self._condition:
            self._running.pop(run_key, None)
            if self._stop.is_set():
                self._condition.notify_all()
                return
            state = self._snapshots.get(key, Unloaded())
            if not isinstance(state, Checking) or state.token != token:
                self._advance()
                return
            try:
                snapshot = future.result()
            except Exception as exc:
                _log.exception("Historical model read failed: %s at %s", key.model.label, key.at_utc.isoformat())
                self._snapshots[key] = Failed(state.previous, state.generation, describe_error(exc), last_check=state.last_check)
            else:
                self._snapshots[key] = Ready(snapshot, state.generation, last_check=state.last_check)
            self._prune_snapshots()
            self._advance()

    def _complete_range(self, key: RangeKey, future: Future[tuple[HistoryEvent, ...]], run_key: object) -> None:
        with self._condition:
            self._running.pop(run_key, None)
            if self._stop.is_set():
                self._condition.notify_all()
                return
            current = self._ranges.get(key, RangeView())
            if current.status != "loading":
                self._advance()
                return
            try:
                result = future.result()
            except Exception as exc:
                self._ranges[key] = replace(current, status="failed", error=describe_error(exc))
            else:
                self._ranges[key] = replace(current, status="ready", events=result, error="")
            self._prune_ranges()
            self._advance()

    def _prune_snapshots(self) -> None:
        if len(self._snapshots) <= self._MAX_HISTORY_CACHE:
            return
        running = {
            job_key[1] for job_key in self._running
            if job_key[0] == "snapshot"
        }
        pinned_instants = {
            demand_key.at_utc for demand_key, owners in self._snapshot_demands.items() if owners
        }
        for key, state in list(self._snapshots.items()):
            if len(self._snapshots) <= self._MAX_HISTORY_CACHE:
                break
            if key in running or type(state) is Queued:
                continue
            if key.at_utc in pinned_instants:
                continue
            self._snapshots.pop(key, None)

    def _prune_ranges(self) -> None:
        if len(self._ranges) <= self._MAX_HISTORY_CACHE:
            return
        running = {
            job_key[1] for job_key in self._running
            if job_key[0] == "range"
        }
        ordered = sorted(self._ranges.items(), key=lambda item: item[1].sequence)
        for key, state in ordered:
            if len(self._ranges) <= self._MAX_HISTORY_CACHE:
                break
            if key in running or state.status == "queued":
                continue
            if key in self._range_demands and self._range_demands[key]:
                continue
            self._ranges.pop(key, None)

    def _needed(self, target: ModelKey) -> bool:
        return target == self.root or target == self._active or any(
            key == target for key, _ in self._bindings.values()
        )

    def _conflicts(self, target: ModelKey) -> bool:
        previous = self._entries[target].previous
        return previous is not None and any(
            key == target and health != previous.root_health for key, health in self._candidates.values()
        )

    def _observe(self, source: ModelKey, snapshot: ModelSnapshot) -> None:
        old = {k: v for k, v in self._bindings.items() if k[0] == source}
        current = {
            (source, e.name.casefold()): (e.reference.target, e.health_state)
            for e in snapshot.forest.entities.values()
            if e.reference and e.reference.target
        }
        for binding in old:
            self._bindings.pop(binding, None)
            if old.get(binding) != current.get(binding):
                self._candidates.pop(binding, None)
        self._bindings.update(current)
        live = {self.root}
        pending = [self.root]
        while pending:
            source_key = pending.pop()
            for (owner, _), (target_key, _) in self._bindings.items():
                if owner == source_key and target_key not in live:
                    live.add(target_key)
                    pending.append(target_key)
        self._bindings = {k: v for k, v in self._bindings.items() if k[0] in live}
        self._candidates = {k: v for k, v in self._candidates.items() if k in self._bindings}
        if self._active is not None and self._active not in live:
            self._withdraw(self._active, {"navigation", "active"})
            self._active = None
        for binding, (target, health) in current.items():
            state = self._entries.setdefault(target, Unloaded())
            before = old.get(binding)
            if before == (target, health) or target == source:
                continue
            if state.previous is None and not isinstance(state, Checking):
                continue
            # Existing changed observations always supersede older in-flight candidates.
            changed = before is not None
            cached_health = state.previous.root_health if state.previous else None
            if changed or health != cached_health:
                state = replace(state, generation=state.generation + 1)
                self._entries[target] = state
            if health != cached_health:
                self._candidates[binding] = (target, health)
                self._request(target, "health")
        for target, state in list(self._entries.items()):
            if type(state) is Queued and "health" in state.reasons and not self._conflicts(target):
                self._withdraw(target, {"health"})
                state = self._entries[target]
            if target != self.root and not self._needed(target):
                if isinstance(state, Checking):
                    self._entries[target] = replace(state, generation=state.generation + 1)
                elif type(state) is Queued:
                    self._entries[target] = Stale(state.previous, state.generation) if state.previous else Unloaded()
    _MAX_HISTORY_CACHE = 128
