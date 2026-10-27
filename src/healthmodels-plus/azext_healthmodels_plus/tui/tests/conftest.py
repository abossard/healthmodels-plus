"""Shared fixtures for azext_healthmodels_plus.tui tests."""

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from azext_healthmodels_plus.tui.client.errors import (
    ArmError,
    AuthenticationError,
    HealthModelNotFoundError,
    ThrottledError,
)
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.domain.graph_builder import build_forest
from azext_healthmodels_plus.tui.domain.parse import (
    parse_entities,
    parse_relationships,
    parse_signal_definitions,
)
from azext_healthmodels_plus.tui.models.domain import (
    EntityNode,
    EvaluationRule,
    Forest,
    SignalDefinition,
    SignalValue,
)
from azext_healthmodels_plus.tui.models.enums import (
    ComparisonOperator,
    DataUnit,
    HealthState,
    Impact,
    SignalKind,
)


# ── 0. anyio backend pin ───────────────────────────────────────────────


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ── 1. Raw JSON loaders ────────────────────────────────────────────────


def _load_json(fixtures_dir: Path, name: str) -> list[dict]:
    """Load a fixture JSON file and unwrap the ``value`` envelope."""
    with open(fixtures_dir / name) as f:
        return json.load(f)["value"]


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def raw_signal_defs(fixtures_dir: Path) -> list[dict]:
    return _load_json(fixtures_dir, "hm-signals.json")


@pytest.fixture(scope="session")
def raw_relationships(fixtures_dir: Path) -> list[dict]:
    return _load_json(fixtures_dir, "hm-relationships.json")


@pytest.fixture(scope="session")
def raw_entities_healthy(fixtures_dir: Path) -> list[dict]:
    return _load_json(fixtures_dir, "hm-entities.json")


@pytest.fixture(scope="session")
def raw_entities_degraded(fixtures_dir: Path) -> list[dict]:
    return _load_json(fixtures_dir, "hm-entities-degraded.json")


# ── 2. Domain forests ──────────────────────────────────────────────────


def _make_forest(
    entities_json: list[dict],
    rels_json: list[dict],
    signals_json: list[dict],
) -> Forest:
    sig_defs = parse_signal_definitions(signals_json)
    entities = parse_entities(entities_json, sig_defs)
    rels = parse_relationships(rels_json)
    return build_forest(entities, rels)


def _entity(
    name: str,
    *,
    health: HealthState = HealthState.HEALTHY,
    children: tuple[str, ...] = (),
    signals: tuple[SignalValue, ...] = (),
    impact: Impact = Impact.STANDARD,
) -> EntityNode:
    return EntityNode(
        entity_id=f"/sub/test/entities/{name}",
        name=name,
        display_name=name,
        health_state=health,
        icon_name="Resource",
        impact=impact,
        signals=signals,
        children=children,
    )


@pytest.fixture
def healthy_forest(
    raw_entities_healthy: list[dict],
    raw_relationships: list[dict],
    raw_signal_defs: list[dict],
) -> Forest:
    return _make_forest(raw_entities_healthy, raw_relationships, raw_signal_defs)


@pytest.fixture
def degraded_forest(
    raw_entities_degraded: list[dict],
    raw_relationships: list[dict],
    raw_signal_defs: list[dict],
) -> Forest:
    return _make_forest(raw_entities_degraded, raw_relationships, raw_signal_defs)


@pytest.fixture
def mixed_forest() -> Forest:
    """root HEALTHY → child DEGRADED → grandchild UNHEALTHY (with UNKNOWN signal)."""
    unknown_sig = SignalValue(
        name="sig-unk",
        definition_name="def-cpu",
        display_name="CPU",
        signal_kind=SignalKind.PROMETHEUS_METRICS_QUERY,
        health_state=HealthState.UNKNOWN,
        value=None,
        data_unit=DataUnit.PERCENT,
        reported_at="2026-01-01T00:00:00Z",
    )
    grandchild = _entity("gc", health=HealthState.UNHEALTHY, signals=(unknown_sig,))
    child = _entity("c", health=HealthState.DEGRADED, children=("gc",))
    root = _entity("r", health=HealthState.HEALTHY, children=("c",))
    return Forest(
        roots=("r",),
        entities={"r": root, "c": child, "gc": grandchild},
        unlinked=(),
    )


@pytest.fixture
def deep_forest() -> Forest:
    """Single chain 4 levels deep: r → a → b → c."""
    c = _entity("c")
    b = _entity("b", children=("c",))
    a = _entity("a", children=("b",))
    r = _entity("r", children=("a",))
    return Forest(
        roots=("r",),
        entities={"r": r, "a": a, "b": b, "c": c},
        unlinked=(),
    )


@pytest.fixture
def cyclic_forest() -> Forest:
    """Hand-built forest representing the post-cycle-break shape (depth 2)."""
    b = _entity("B")
    a = _entity("A", children=("B",))
    return Forest(roots=("A",), entities={"A": a, "B": b}, unlinked=())


@pytest.fixture
def empty_forest() -> Forest:
    return Forest(roots=(), entities={}, unlinked=())


@pytest.fixture
def single_entity_forest() -> Forest:
    only = _entity("only")
    return Forest(roots=("only",), entities={"only": only}, unlinked=())


@pytest.fixture
def unlinked_forest() -> Forest:
    """2 roots + 2 unlinked entities."""
    r1 = _entity("r1")
    r2 = _entity("r2")
    u1 = _entity("u1")
    u2 = _entity("u2")
    return Forest(
        roots=("r1", "r2"),
        entities={"r1": r1, "r2": r2, "u1": u1, "u2": u2},
        unlinked=("u1", "u2"),
    )


# ── 3. Mock clients ────────────────────────────────────────────────────

def install_wire_forest(client, forest: Forest) -> None:
    """Move legacy prebuilt-forest widget fixtures to the retained read seam."""
    definitions = {}
    entities = []
    relationships = []
    for node in forest.entities.values():
        signals = []
        for sig in node.signals:
            rules = {}
            for key, rule in [("degradedRule", sig.degraded_rule), ("unhealthyRule", sig.unhealthy_rule)]:
                if rule:
                    rules[key] = {"operator": rule.operator.value, "threshold": rule.threshold}
            definitions[sig.definition_name] = {
                "name": sig.definition_name, "properties": {
                    "displayName": sig.display_name, "signalKind": sig.signal_kind.value,
                    "dataUnit": sig.data_unit.value, "evaluationRules": rules,
                },
            }
            signals.append({
                "name": sig.name, "signalDefinitionName": sig.definition_name,
                "signalKind": sig.signal_kind.value, "status": {
                    "healthState": sig.health_state.value, "value": sig.value,
                    "reportedAt": sig.reported_at, "error": sig.error,
                },
            })
        entities.append({"id": node.entity_id, "name": node.name, "properties": {
            "displayName": node.display_name, "healthState": node.health_state.value,
            "impact": node.impact.value, "icon": {"iconName": node.icon_name},
            "signalGroups": {"azureResource": {"signals": signals}},
        }})
        for child in node.children:
            relationships.append({"name": f"{node.name}-{child}", "properties": {
                "parentEntityName": node.name, "childEntityName": child,
            }})
    # Rootless components are how the service expresses these fixture orphans.
    for parent, child in zip(forest.unlinked, forest.unlinked[1:] + forest.unlinked[:1]):
        relationships.append({"name": f"{parent}-{child}", "properties": {
            "parentEntityName": parent, "childEntityName": child,
        }})
    client.list_entities.side_effect = None
    client.list_entities.return_value = entities
    client.list_signal_definitions.side_effect = None
    client.list_signal_definitions.return_value = list(definitions.values())
    client.list_relationships.side_effect = None
    client.list_relationships.return_value = relationships


def _make_client(
    *,
    entities: list[dict] | None = None,
    signal_defs: list[dict] | None = None,
    relationships: list[dict] | None = None,
    list_error: Exception | None = None,
    execute_payload: dict | Exception | None = None,
    history_payload: dict | Exception | None = None,
    list_latency_ms: int = 0,
) -> MagicMock:
    client = MagicMock(spec=CloudHealthClient)

    def _maybe_sleep() -> None:
        if list_latency_ms:
            time.sleep(list_latency_ms / 1000.0)

    def _list_signals(*_a: Any, **_kw: Any) -> list[dict]:
        _maybe_sleep()
        if list_error is not None:
            raise list_error
        return signal_defs or []

    def _list_entities(*_a: Any, **_kw: Any) -> list[dict]:
        _maybe_sleep()
        if list_error is not None:
            raise list_error
        return entities or []

    def _list_rels(*_a: Any, **_kw: Any) -> list[dict]:
        _maybe_sleep()
        if list_error is not None:
            raise list_error
        return relationships or []

    client.list_signal_definitions.side_effect = _list_signals
    client.list_entities.side_effect = _list_entities
    client.list_relationships.side_effect = _list_rels

    if isinstance(execute_payload, Exception):
        client.execute_signal.side_effect = execute_payload
    elif execute_payload is not None:
        client.execute_signal.return_value = execute_payload

    if isinstance(history_payload, Exception):
        client.get_signal_history.side_effect = history_payload
    elif history_payload is not None:
        client.get_signal_history.return_value = history_payload

    return client


@pytest.fixture
def healthy_client(
    raw_entities_healthy: list[dict],
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
) -> MagicMock:
    return _make_client(
        entities=raw_entities_healthy,
        signal_defs=raw_signal_defs,
        relationships=raw_relationships,
    )


@pytest.fixture
def degraded_client(
    raw_entities_degraded: list[dict],
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
) -> MagicMock:
    return _make_client(
        entities=raw_entities_degraded,
        signal_defs=raw_signal_defs,
        relationships=raw_relationships,
    )


@pytest.fixture
def empty_client() -> MagicMock:
    return _make_client(entities=[], signal_defs=[], relationships=[])


@pytest.fixture
def error_client_factory(
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
):
    def _build(exc: Exception) -> MagicMock:
        return _make_client(
            entities=[],
            signal_defs=raw_signal_defs,
            relationships=raw_relationships,
            list_error=exc,
        )
    return _build


@pytest.fixture
def slow_client(
    raw_entities_healthy: list[dict],
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
) -> MagicMock:
    return _make_client(
        entities=raw_entities_healthy,
        signal_defs=raw_signal_defs,
        relationships=raw_relationships,
        list_latency_ms=200,
    )


@pytest.fixture
def recovering_client(
    raw_entities_healthy: list[dict],
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
) -> MagicMock:
    """First poll raises ArmError; subsequent polls succeed with healthy data."""
    client = MagicMock(spec=CloudHealthClient)
    state = {"calls": 0}

    def _maybe_fail(payload: list[dict]) -> list[dict]:
        if state["calls"] == 0:
            # All three list_* calls in the first poll see the error;
            # the poller catches on the first call so only one increment matters.
            raise ArmError("transient", status_code=500)
        return payload

    def _list_signals(*_a: Any, **_kw: Any) -> list[dict]:
        return _maybe_fail(raw_signal_defs)

    def _list_entities(*_a: Any, **_kw: Any) -> list[dict]:
        return _maybe_fail(raw_entities_healthy)

    def _list_rels(*_a: Any, **_kw: Any) -> list[dict]:
        return _maybe_fail(raw_relationships)

    client.list_signal_definitions.side_effect = _list_signals
    client.list_entities.side_effect = _list_entities
    client.list_relationships.side_effect = _list_rels

    def _bump(*_a: Any, **_kw: Any) -> None:
        # Bump after each full poll (caller invokes manually)
        state["calls"] += 1

    client._bump_call_counter = _bump  # test helper
    return client


@pytest.fixture
def escalating_client(
    raw_entities_healthy: list[dict],
    raw_entities_degraded: list[dict],
    raw_signal_defs: list[dict],
    raw_relationships: list[dict],
) -> MagicMock:
    """First poll → healthy entities; second poll → degraded entities."""
    client = MagicMock(spec=CloudHealthClient)
    state = {"poll": 0}

    def _list_signals(*_a: Any, **_kw: Any) -> list[dict]:
        return raw_signal_defs

    def _list_entities(*_a: Any, **_kw: Any) -> list[dict]:
        return raw_entities_degraded if state["poll"] >= 1 else raw_entities_healthy

    def _list_rels(*_a: Any, **_kw: Any) -> list[dict]:
        return raw_relationships

    client.list_signal_definitions.side_effect = _list_signals
    client.list_entities.side_effect = _list_entities
    client.list_relationships.side_effect = _list_rels

    def _bump(*_a: Any, **_kw: Any) -> None:
        state["poll"] += 1

    client._bump_call_counter = _bump
    return client


# ── 4. Signal pairs ────────────────────────────────────────────────────


def _eval_rule(op: ComparisonOperator = ComparisonOperator.GREATER_THAN, threshold: float = 80.0) -> EvaluationRule:
    return EvaluationRule(operator=op, threshold=threshold)


def _signal_pair(
    *,
    kind: SignalKind,
    unit: DataUnit = DataUnit.PERCENT,
    value: float | None = 12.5,
    health: HealthState = HealthState.HEALTHY,
    extra_props: dict[str, Any] | None = None,
) -> tuple[SignalDefinition, SignalValue, dict]:
    sig_def = SignalDefinition(
        name="def-1",
        display_name=f"{kind.value} signal",
        signal_kind=kind,
        data_unit=unit,
        degraded_rule=_eval_rule(threshold=50.0),
        unhealthy_rule=_eval_rule(threshold=80.0),
    )
    sig_val = SignalValue(
        name="sig-1",
        definition_name="def-1",
        display_name=sig_def.display_name,
        signal_kind=kind,
        health_state=health if value is not None else HealthState.UNKNOWN,
        value=value,
        data_unit=unit,
        reported_at="2026-01-01T00:00:00Z",
    )
    payload: dict[str, Any] = {
        "name": "sig-1",
        "signalDefinitionName": "def-1",
        "signalKind": kind.value,
    }
    if extra_props:
        payload.update(extra_props)
    return sig_def, sig_val, payload


@pytest.fixture
def promql_signal_pair():
    return _signal_pair(
        kind=SignalKind.PROMETHEUS_METRICS_QUERY,
        extra_props={
            "workspaceResourceId": "/sub/x/amw",
            "query": "rate(http_requests_total[5m])",
        },
    )


@pytest.fixture
def arm_signal_pair():
    return _signal_pair(
        kind=SignalKind.AZURE_RESOURCE_METRIC,
        extra_props={
            "metricNamespace": "Microsoft.Compute/virtualMachines",
            "metricName": "Percentage CPU",
            "aggregation": "Average",
        },
    )


@pytest.fixture
def kql_signal_pair():
    return _signal_pair(
        kind=SignalKind.LOG_ANALYTICS_QUERY,
        extra_props={
            "workspaceResourceId": "/sub/x/log",
            "query": "AzureDiagnostics | take 10",
        },
    )


@pytest.fixture
def external_signal_pair():
    return _signal_pair(
        kind=SignalKind.EXTERNAL,
        extra_props={},
    )


@pytest.fixture
def no_value_signal_pair():
    return _signal_pair(
        kind=SignalKind.PROMETHEUS_METRICS_QUERY,
        value=None,
        health=HealthState.UNKNOWN,
        extra_props={"query": "rate(x[5m])"},
    )


@pytest.fixture
def signal_pair(
    request,
    promql_signal_pair,
    arm_signal_pair,
    kql_signal_pair,
    external_signal_pair,
    no_value_signal_pair,
):
    """Indirect resolver: parametrize with one of the SIGNAL_PAIR_IDS strings."""
    return {
        "promql": promql_signal_pair,
        "arm": arm_signal_pair,
        "kql": kql_signal_pair,
        "external": external_signal_pair,
        "no_value": no_value_signal_pair,
    }[request.param]


# ── 5. Execution / history responses ───────────────────────────────────


@pytest.fixture
def execute_success() -> dict[str, Any]:
    return {
        "healthState": "Healthy",
        "value": 42.0,
        "unit": "Percent",
        "query": "rate(x[5m])",
        "timestamp": "2026-01-01T00:00:00Z",
        "raw": {"data": "ok"},
    }


@pytest.fixture
def execute_degraded() -> dict[str, Any]:
    return {
        "healthState": "Degraded",
        "value": 65.0,
        "unit": "Percent",
        "query": "rate(x[5m])",
        "timestamp": "2026-01-01T00:00:00Z",
        "raw": {"data": "ok"},
    }


@pytest.fixture
def execute_error_payload() -> dict[str, Any]:
    return {"error": "Workspace not found", "query": "rate(x[5m])"}


@pytest.fixture
def execute_exception_throttled() -> Exception:
    return ThrottledError("rate limit hit", retry_after=30)


@pytest.fixture
def execute_exception_auth() -> Exception:
    return AuthenticationError("forbidden")


@pytest.fixture
def history_response() -> dict[str, Any]:
    return {"history": [{"value": float(i)} for i in range(1, 6)]}


@pytest.fixture
def history_empty() -> dict[str, Any]:
    return {"history": []}


# ── 6. App harness ─────────────────────────────────────────────────────


@pytest.fixture
def make_app():
    """Factory fixture: build a HealthWatchApp wired to *client*."""
    def _make(client: Any, *, model: str = "m", rg: str = "rg", interval: int = 30):
        from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
        return HealthWatchApp(client, rg, model, poll_interval=interval)
    return _make


@pytest.fixture
def app_size() -> tuple[int, int]:
    return (120, 40)


# ── Helper functions (not fixtures — imported by tests) ────────────────


async def trigger_poll(app, pilot) -> None:
    """Run one poll cycle and let the UI settle."""
    await app._do_poll()
    await pilot.pause()


def select_first_signal_node(tree):
    """Walk the tree and return the first signal leaf TreeNode, or None."""
    stack = list(tree.root.children)
    while stack:
        node = stack.pop(0)
        data = node.data
        if data is not None and getattr(data, "is_signal", False):
            return node
        stack.extend(list(node.children))
    return None


def select_first_entity_node(tree):
    """Return the first non-signal entity TreeNode (skip __unlinked__), or None."""
    stack = list(tree.root.children)
    while stack:
        node = stack.pop(0)
        data = node.data
        if (
            data is not None
            and not getattr(data, "is_signal", False)
            and getattr(data, "entity_name", "") != "__unlinked__"
        ):
            return node
        stack.extend(list(node.children))
    return None
