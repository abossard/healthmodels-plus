"""Nested navigation through Textual and the real stable SDK HTTP pipeline."""
from __future__ import annotations

import asyncio
import copy
import threading
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from azure.mgmt.cloudhealth import CloudHealthMgmtClient

from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from .test_sdk_integration import (
    _RecordingResponse, _StaticTokenCredential,
)
from azure.core.pipeline.transport import HttpTransport
from azext_healthmodels_plus.tui.watch.app import HealthWatchApp
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
from azext_healthmodels_plus.tui.watch.status_bar import StatusBar
from azext_healthmodels_plus.tui.watch.entity_drawer import EntityDrawer
from azext_healthmodels_plus.tui.watch.query_editor import QueryEditor
from azext_healthmodels_plus.tui.watch.signal_panel import SignalPanel
from textual.app import App
from textual.widgets import Input, Label, OptionList, Static, Tree
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey
from azext_healthmodels_plus.tui.domain.occurrences import project
from azext_healthmodels_plus.tui.domain.graph_builder import build_forest
from azext_healthmodels_plus.tui.domain.parse import parse_entities, parse_relationships

SUB = "00000000-0000-0000-0000-000000000001"
OTHER_SUB = "00000000-0000-0000-0000-000000000002"


@dataclass
class Reply:
    payload: dict
    status: int = 200
    release: threading.Event | None = None


def model_id(name: str, group: str = "rg", sub: str = SUB) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{group}/providers/Microsoft.CloudHealth/healthmodels/{name}"


def entity(model: str, name: str, target: str = "", health: str = "Healthy") -> dict:
    props: dict[str, Any] = {"displayName": name, "healthState": health}
    if target:
        props["signalGroups"] = {"azureResource": {
            "azureResourceId": target, "authenticationSetting": "auth",
        }}
    return {"id": model_id(model) + "/entities/" + name, "name": name, "properties": props}


def add_signal(transport, name: str, group: str = "rg", sub: str = SUB) -> None:
    shared = next(e for e in transport.models[(sub, group, name.lower())] if e["name"] == "shared")
    shared["properties"]["displayName"] = f"{name.upper()} scoped shared"
    shared["properties"]["signalGroups"] = {"external": {"signals": [{
        "name": "same-signal", "signalDefinitionName": "same-def", "signalKind": "External",
        "status": {"healthState": "Healthy", "value": 1},
    }]}}
    transport.definitions[name.lower()] = [{
        "name": "same-def", "properties": {
            "displayName": "same signal", "signalKind": "External", "queryText": f"query-{name.lower()}",
        },
    }]


class ModelTransport(HttpTransport):
    """Service-shaped responses, with a ledger at the HTTP boundary."""

    def __init__(self) -> None:
        self.models: dict[tuple[str, str, str], list[dict]] = {}
        self.requests: list[tuple[str, str, str, str]] = []
        self.blocks: dict[str, threading.Event] = {}
        self.replies: dict[tuple[str, str], list[Reply]] = {}
        self.inflight = 0
        self.maximum = 0
        self.monitor_active: Counter = Counter()
        self.monitor_maximum: Counter = Counter()
        self._lock = threading.Lock()
        self.definitions: dict[str, list[dict]] = {}
        self.relationships: dict[str, list[dict]] = {}

    def put(self, name: str, *children: dict, group: str = "rg", sub: str = SUB) -> None:
        self.models[(sub, group.casefold(), name.casefold())] = [entity(name, name), *children]

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        parts = unquote(urlsplit(request.url).path).strip("/").split("/")
        sub, group, name = parts[1], parts[3].casefold(), parts[7].casefold()
        suffix = "/".join(parts[8:]).casefold()
        key = (sub, group, name)
        monitor = suffix in {"entities", "relationships", "signaldefinitions", f"entities/{name}"} or suffix.endswith("/page2")
        with self._lock:
            self.requests.append((sub, group, name, suffix))
            self.inflight += 1
            self.maximum = max(self.maximum, self.inflight)
            if monitor:
                self.monitor_active[key] += 1
                self.monitor_maximum[key] = max(self.monitor_maximum[key], self.monitor_active[key])
        assert "api-version=2026-11-01" in request.url
        assert request.method == ("POST" if suffix.endswith("getsignalhistory") else "GET")
        entities = copy.deepcopy(self.models[(sub, group, name)])
        if suffix == "entities":
            payload = {"value": entities}
        elif suffix.endswith("/page2"):
            payload = {"value": []}
        elif suffix.endswith("getsignalhistory"):
            payload = {"entityName": "shared", "signalName": "same-signal", "history": []}
        elif suffix.startswith("entities/"):
            payload = next(e for e in entities if e["name"].casefold() == parts[9].casefold())
        elif suffix == "relationships":
            payload = {"value": self.relationships.get(name, [
                {"name": f"edge-{i}", "properties": {
                    "parentEntityName": entities[0]["name"], "childEntityName": e["name"],
                }} for i, e in enumerate(entities[1:])
            ])}
        else:
            if suffix.startswith("signaldefinitions/"):
                payload = next(d for d in self.definitions[name] if d["name"] == parts[9])
            else:
                assert suffix == "signaldefinitions"
                payload = {"value": self.definitions.get(name, [])}
        replies = self.replies.get((name, suffix))
        reply = replies.pop(0) if replies else Reply(payload)
        try:
            release = reply.release or self.blocks.get(name)
            if release is not None:
                assert release.wait(20), f"HTTP barrier timed out: {name}"
            return _RecordingResponse(request, reply.status, reply.payload)
        finally:
            with self._lock:
                self.inflight -= 1
                if monitor:
                    self.monitor_active[key] -= 1

    def counts(self) -> Counter:
        return Counter(name for _, _, name, _ in self.requests)


@pytest.fixture
def service(monkeypatch):
    transport = ModelTransport()

    def factory(cli_ctx, sdk_type, *, subscription_id, api_version):
        return CloudHealthMgmtClient(
            _StaticTokenCredential(), subscription_id,
            api_version=api_version, transport=transport, retry_total=0,
        )

    monkeypatch.setattr(
        "azure.cli.core.commands.client_factory.get_mgmt_service_client", factory,
    )
    yield transport, CloudHealthClient(None, SUB)
    assert all(n <= 1 for n in transport.monitor_maximum.values()), transport.monitor_maximum


async def until(pilot, predicate) -> None:
    async with asyncio.timeout(10):
        while not predicate():
            await pilot.pause()


def labels(app) -> str:
    tree = app.query_one(HealthTree)
    work = [tree.root]
    result = []
    while work:
        node = work.pop()
        result.append(node.label.plain)
        work.extend(reversed(node.children))
    return "\n".join(result)


def nodes(app):
    work = [app.query_one(HealthTree).root]
    while work:
        node = work.pop()
        yield node
        work.extend(reversed(node.children))


async def expand(pilot, app, name: str):
    tree = app.query_one(HealthTree)
    node = next(n for n in nodes(app) if n.data and n.data.entity_name == name)
    tree.move_cursor(node)
    await pilot.press("enter")
    return node


async def idle(pilot, app) -> None:
    await until(pilot, lambda: not any(
        s in app.query_one(StatusBar).model_work for s in ("loading", "checking", "queued")
    ))
    await pilot.pause()


async def refresh(pilot, app, transport) -> None:
    before = transport.counts()["r"]
    await pilot.press("r")
    await until(pilot, lambda: transport.counts()["r"] >= before + 3)
    await idle(pilot, app)


async def enter_root(pilot, app) -> None:
    tree = app.query_one(HealthTree)
    node = next(n for n in nodes(app) if n.data and n.data.entity_name == "R")
    tree.move_cursor(node)
    await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("target, marker", [
    (model_id("B"), "unloaded"),
    (model_id("B", "Other", OTHER_SUB).upper() + "/", "unloaded"),
    (model_id("B") + "/entities/selected", "unsupported"),
    (model_id("B") + "?query=bad", "invalid"),
    (model_id("B%2fchild"), "invalid"),
    (model_id("B%5Cchild"), "invalid"),
    (model_id("B") + "//", "invalid"),
    (model_id("B") + "#fragment", "invalid"),
    (model_id("B", sub="not-a-uuid"), "invalid"),
    (model_id("B").replace("Microsoft.CloudHealth", "Microsoft%2eCloudHealth"), "unloaded"),
    (model_id("B").replace("Microsoft.CloudHealth", "Microsoft.Storage"), ""),
    (model_id("B").replace("healthmodels", "otherResources"), ""),
])
async def test_references_are_visible_and_initially_unfetched(service, target, marker):
    transport, client = service
    transport.put("R", entity("R", "reference", target))
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test() as pilot:
        await until(pilot, lambda: "reference" in labels(app))
        if marker:
            assert marker in labels(app).lower()
        else:
            assert "unloaded" not in labels(app).lower()
        assert transport.counts() == {"r": 3}


@pytest.mark.anyio
async def test_twenty_alias_expansions_share_one_load_and_cycle_is_path_local(service, tmp_path):
    transport, client = service
    transport.put("R", *(entity("R", f"ref-{i}", model_id("B")) for i in range(20)))
    transport.put("B", entity("B", "back", model_id("R")), entity("B", "unopened", model_id("C")))
    transport.put("C")
    transport.blocks["b"] = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(120, 50)) as pilot:
            await until(pilot, lambda: "ref-19" in labels(app))
            for i in range(20):
                await expand(pilot, app, f"ref-{i}")
            await until(pilot, lambda: transport.counts()["b"] == 1)
            assert "loading" in labels(app).lower()
            assert sum("loading" in n.label.plain for n in nodes(app)) == 20
            assert "1 loading" in app.query_one(StatusBar).model_work
            app.save_screenshot("nested-shared-loading.svg", path=str(tmp_path))
            transport.blocks["b"].set()
            await until(pilot, lambda: sum(n.data.entity_name == "B" for n in nodes(app) if n.data) == 20)
            assert transport.counts() == {"r": 3, "b": 3}
            await expand(pilot, app, "back")
            assert "cycle" in labels(app).lower()
            assert transport.counts() == {"r": 3, "b": 3}
    finally:
        transport.blocks["b"].set()


@pytest.mark.anyio
@pytest.mark.parametrize("case, expected_b", [
    ("unchanged-values", 0),
    ("late-matching", 0),
    ("conflicting", 1),
    ("changed-root", 4),
])
async def test_background_refresh_is_gated_by_authoritative_target_root(service, case, expected_b):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", entity("B", "deep", model_id("C")))
    transport.put("C")
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(140, 50)) as pilot:
        await until(pilot, lambda: "ref" in labels(app))
        await expand(pilot, app, "ref")
        await until(pilot, lambda: "deep" in labels(app))
        await idle(pilot, app)
        await enter_root(pilot, app)
        assert transport.counts() == {"r": 3, "b": 3}
        if case == "late-matching":
            transport.models[(SUB, "rg", "r")].append(entity("R", "late", model_id("B")))
        elif case in {"conflicting", "changed-root"}:
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            if case == "changed-root":
                transport.models[(SUB, "rg", "b")][0]["properties"]["healthState"] = "Degraded"
        for i in range(3):
            # Values and timestamps are not health invalidation.
            transport.models[(SUB, "rg", "r")][1]["properties"]["signalGroups"]["azureResource"]["signals"] = [{
                "name": "value-only", "signalKind": "AzureResourceMetric",
                "status": {"healthState": "Healthy", "value": i, "reportedAt": f"2026-10-17T11:00:0{i}Z"},
            }]
            await refresh(pilot, app, transport)
        assert transport.counts() == {"r": 12, "b": 3 + expected_b}


@pytest.mark.anyio
@pytest.mark.parametrize("final", ["Healthy", "Unhealthy"])
@pytest.mark.parametrize("rename", [False, True])
async def test_health_aba_discards_obsolete_check_and_coalesces_latest(service, final, rename):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", entity("B", "original"))
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(120, 50)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            await expand(pilot, app, "ref")
            await until(pilot, lambda: "original" in labels(app))
            await idle(pilot, app)
            await enter_root(pilot, app)
            transport.replies[("b", "entities/b")] = [
                Reply(entity("B", "B", health="Degraded"), release=release),
            ]
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            if rename:
                transport.models[(SUB, "rg", "r")][1]["name"] = "REF"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["b"] == 4)
            assert "checking" in labels(app)
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = final
            if rename:
                transport.models[(SUB, "rg", "r")][1]["name"] = "ref"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["r"] == 9)
            transport.models[(SUB, "rg", "b")][0]["properties"]["healthState"] = final
            release.set()
            await idle(pilot, app)
            assert transport.counts() == {"r": 9, "b": 4 if final == "Healthy" else 8}
            assert transport.maximum <= 2
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [403, 404, 429, 500])
@pytest.mark.parametrize("collection", ["entities", "relationships", "signaldefinitions"])
async def test_failed_child_page_retains_complete_snapshot_and_root_connection(service, status, collection, tmp_path):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", entity("B", "original"))
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(160, 50)) as pilot:
        await until(pilot, lambda: "ref" in labels(app))
        await expand(pilot, app, "ref")
        await until(pilot, lambda: "original" in labels(app))
        await idle(pilot, app)
        before = next(n for n in nodes(app) if n.data and n.data.entity_name == "ref").label.plain
        capture = before.split("; captured ", 1)[1].split("]", 1)[0]
        await enter_root(pilot, app)
        transport.replies[("b", collection)] = [Reply({
            "value": [entity("B", "B"), entity("B", "partial")] if collection == "entities" else [],
            "nextLink": "https://management.azure.com" + model_id("B") + f"/{collection}/page2?api-version=2026-11-01",
        })]
        transport.replies[("b", f"{collection}/page2")] = [Reply({
            "error": {"code": "Denied", "message": f"page two {status}"},
        }, status)]
        # Reopening a cached target is an explicit refresh, not a TTL.
        await expand(pilot, app, "ref")  # collapse
        await expand(pilot, app, "ref")  # reopen
        await until(pilot, lambda: "failed" in labels(app))
        assert "original" in labels(app) and "partial" not in labels(app)
        assert "stale" in labels(app)
        assert str(status) in labels(app)
        assert f"; captured {capture}" in labels(app)
        assert app.query_one(StatusBar).connected
        if status == 403 and collection == "entities":
            await pilot.press("d")
            assert "page two 403" in str(app.query_one("#entity-drawer-body", Static).render())
            app.save_screenshot("nested-failed-cached.svg", path=str(tmp_path))


@pytest.mark.anyio
async def test_nested_search_details_editor_verification_and_history_keep_target_scope(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B", "other", OTHER_SUB)), entity("R", "shared"))
    transport.put("B", entity("B", "shared"), group="other", sub=OTHER_SUB)
    add_signal(transport, "R")
    add_signal(transport, "B", "other", OTHER_SUB)
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(160, 55)) as pilot:
        await until(pilot, lambda: "ref" in labels(app))
        await expand(pilot, app, "ref")
        await until(pilot, lambda: "B scoped shared" in labels(app))
        await idle(pilot, app)
        tree = app.query_one(HealthTree)
        b_shared = next(n for n in nodes(app) if "B scoped shared" in n.label.plain)
        tree.move_cursor(b_shared)
        await pilot.press("d")
        assert "B scoped shared" in str(app.query_one("#entity-drawer-body", Static).render())
        before_search = len(transport.requests)
        await pilot.press("/")
        app.screen.query_one(Input).value = "scoped shared"
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.option_count == 2
        assert len(transport.requests) == before_search
        await pilot.press("enter")
        await until(pilot, lambda: tree.cursor_node is not None and
                    "B scoped shared" in tree.cursor_node.label.plain)
        await pilot.press("n")
        await until(pilot, lambda: "R scoped shared" in tree.cursor_node.label.plain)
        await pilot.press("p")
        await until(pilot, lambda: "B scoped shared" in tree.cursor_node.label.plain)
        await idle(pilot, app)
        b_signal = next(n for n in nodes(app) if n.data and n.data.is_signal
                        and n.parent and "B scoped shared" in n.parent.label.plain)
        tree.move_cursor(b_signal)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, QueryEditor) and
                    "Loading" not in str(app.screen.query_one("#qe-title", Label).render()))
        assert transport.requests[-2:] == [
            (OTHER_SUB, "other", "b", "entities/shared"),
            (OTHER_SUB, "other", "b", "signaldefinitions/same-def"),
        ]
        await pilot.press("escape", "v")
        await until(pilot, lambda: any(r[3].endswith("getsignalhistory") for r in transport.requests))
        assert transport.requests[-3:] == [
            (OTHER_SUB, "other", "b", "entities/shared"),
            (OTHER_SUB, "other", "b", "signaldefinitions/same-def"),
            (OTHER_SUB, "other", "b", "entities/shared/getsignalhistory"),
        ]
        assert app.query_one(SignalPanel).context.entity_display == "B scoped shared"
        await pilot.press("d")
        assert not app.query_one(EntityDrawer).is_visible


@pytest.mark.parametrize("depth", [1, 32, 1100])
def test_cached_projection_has_no_cross_model_recursion_limit(depth):
    forests = {}
    root = ModelKey("https://management.azure.com", SUB, "rg", "M0")
    for i in range(depth):
        name = f"M{i}"
        records = [entity(name, name)]
        if i + 1 < depth:
            records.append(entity(name, f"next-{i}", model_id(f"M{i+1}")))
        rels = [{"name": "edge", "properties": {
            "parentEntityName": name, "childEntityName": f"next-{i}",
        }}] if i + 1 < depth else []
        forests[ModelKey(root.endpoint, SUB, "rg", name)] = build_forest(
            parse_entities(records, {}), parse_relationships(rels),
        )
    rows = project(root, forests)
    assert rows[-1].model.name == f"M{depth-1}"
    assert len(rows) == 2 * depth - 1
    assert not any(r.cycle for r in rows)


@pytest.mark.anyio
@pytest.mark.parametrize("depth", [1, 32])
async def test_keyboard_opens_only_one_model_per_chain_hop(service, depth):
    transport, client = service
    for i in range(depth):
        name = "R" if i == 0 else f"M{i}"
        children = [entity(name, f"next-{i}", model_id(f"M{i+1}"))] if i + 1 < depth else []
        transport.put(name, *children)
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: "R ─" in labels(app))
        for i in range(depth - 1):
            await expand(pilot, app, f"next-{i}")
            await until(pilot, lambda: f"M{i+1} ─" in labels(app))
        assert transport.counts() == {("r" if i == 0 else f"m{i}"): 3 for i in range(depth)}


@pytest.mark.anyio
async def test_diamond_occurrences_share_target_without_pruning_local_dag_paths(service):
    transport, client = service
    transport.put("R", entity("R", "left", model_id("B")), entity("R", "right", model_id("C")))
    transport.put("B", entity("B", "from-B", model_id("D")))
    transport.put("C", entity("C", "from-C", model_id("D")))
    transport.put("D", entity("D", "back-B", model_id("B")))
    release = threading.Event()
    transport.blocks["d"] = release
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(140, 55)) as pilot:
            await until(pilot, lambda: "right" in labels(app))
            for name in ["left", "right"]:
                next(n for n in nodes(app) if n.data and n.data.entity_name == name).expand()
            await until(pilot, lambda: "from-B" in labels(app) and "from-C" in labels(app))
            for name in ["from-B", "from-C"]:
                next(n for n in nodes(app) if n.data and n.data.entity_name == name).expand()
            await until(pilot, lambda: transport.counts()["d"] == 1)
            release.set()
            await until(pilot, lambda: sum(n.data.entity_name == "D" for n in nodes(app) if n.data) == 2)
            assert transport.counts() == {"r": 3, "b": 3, "c": 3, "d": 3}
            # One D->B is a cycle; the independent R->C->D->B remains navigable.
            backs = [n for n in nodes(app) if n.data and n.data.entity_name == "back-B"]
            assert len(backs) == 2
            assert sum("cycle" in n.label.plain for n in backs) == 1
            next(n for n in backs if "cycle" not in n.label.plain).expand()
            await until(pilot, lambda: sum(n.data.entity_name == "B" for n in nodes(app) if n.data) == 2)
            assert transport.counts() == {"r": 3, "b": 6, "c": 3, "d": 3}
    finally:
        release.set()


@pytest.mark.anyio
async def test_navigation_upgrades_inflight_root_check_without_overlap(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", entity("B", "cached"))
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            await expand(pilot, app, "ref")
            await until(pilot, lambda: "cached" in labels(app))
            await idle(pilot, app)
            await enter_root(pilot, app)
            transport.replies[("b", "entities/b")] = [Reply(entity("B", "B"), release=release)]
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["b"] == 4)
            await expand(pilot, app, "ref")  # collapse
            await expand(pilot, app, "ref")  # navigation joins checking
            release.set()
            await idle(pilot, app)
            assert transport.counts() == {"r": 6, "b": 7}
            assert transport.maximum <= 2
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["root", "nested", "page"])
async def test_quit_keeps_real_sdk_slot_until_completion_and_starts_no_more_reads(service, phase):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B")
    release = threading.Event()
    transport.blocks["r" if phase in {"root", "page"} else "b"] = release
    if phase == "page":
        transport.replies[("r", "signaldefinitions")] = [Reply({
            "value": [], "nextLink": "https://management.azure.com" + model_id("R")
            + "/signaldefinitions/page2?api-version=2026-11-01",
        }, release=release)]
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test() as pilot:
            if phase == "nested":
                await until(pilot, lambda: "ref" in labels(app))
                await expand(pilot, app, "ref")
            await until(pilot, lambda: transport.inflight == 1)
            await pilot.press("q")
        assert transport.inflight == 1
        count = len(transport.requests)
        release.set()
        async with asyncio.timeout(10):
            while transport.inflight:
                await asyncio.sleep(0)
        assert len(transport.requests) == count
        assert transport.counts() == ({"r": 3, "b": 1} if phase == "nested" else {"r": 1})
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("previous", [False, True])
async def test_observation_during_full_load_discards_old_snapshot_before_one_replacement(service, previous):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", entity("B", "original"))
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(140, 55)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            if previous:
                await expand(pilot, app, "ref")
                await until(pilot, lambda: "original" in labels(app))
                await idle(pilot, app)
                await expand(pilot, app, "ref")  # collapse before new activation
            transport.replies[("b", "entities")] = [Reply({
                "value": [entity("B", "B"), entity("B", "obsolete")],
            }, release=release)]
            await expand(pilot, app, "ref")
            start_b = 3 if previous else 0
            await until(pilot, lambda: transport.counts()["b"] == start_b + 2)
            await enter_root(pilot, app)
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            transport.models[(SUB, "rg", "b")][0]["properties"]["healthState"] = "Degraded"
            transport.models[(SUB, "rg", "b")][-1]["properties"]["displayName"] = "latest"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["r"] == 6)
            release.set()
            await until(pilot, lambda: "latest" in labels(app))
            await idle(pilot, app)
            assert "obsolete" not in labels(app)
            # One obsolete full load, then one latest check/full (cached) or full (initial).
            assert transport.counts()["b"] == start_b + (7 if previous else 6)
            assert transport.maximum <= 2
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("due_root", [False, True])
async def test_queue_orders_explicit_navigation_then_visible_distance_and_withdraws_demand(service, due_root):
    transport, client = service
    refs = [entity("R", f"ref-{name}", model_id(name)) for name in "BCDEH"]
    transport.put("R", *refs, entity("R", "branch"), entity("R", "nav", model_id("F")),
                  entity("R", "withdraw", model_id("G")))
    transport.relationships["r"] = [
        {"name": f"edge-{i}", "properties": {"parentEntityName": "R", "childEntityName": f"ref-{n}"}}
        for i, n in enumerate("BCEH")
    ] + [
        {"name": "far", "properties": {"parentEntityName": "branch", "childEntityName": "ref-D"}},
        {"name": "branch", "properties": {"parentEntityName": "R", "childEntityName": "branch"}},
        {"name": "nav", "properties": {"parentEntityName": "R", "childEntityName": "nav"}},
        {"name": "withdraw", "properties": {"parentEntityName": "R", "childEntityName": "withdraw"}},
    ]
    for name in "BCDEFGH":
        transport.put(name)
    b_release, c_release = threading.Event(), threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=1 if due_root else 9999)
    try:
        async with app.run_test(size=(160, 55)) as pilot:
            await until(pilot, lambda: "ref-E" in labels(app))
            # Expanding arrow occurrences without changing the cursor.
            # One at a time: each activation withdraws the previous one if it is still queued.
            for name in "bcdeh":
                next(n for n in nodes(app) if n.data and n.data.entity_name == f"ref-{name.upper()}").expand()
                await until(pilot, lambda: transport.counts()[name] == 3)
                await idle(pilot, app)
            await enter_root(pilot, app)
            for name, release in [("b", b_release), ("c", c_release)]:
                transport.replies[(name, f"entities/{name}")] = [
                    Reply(entity(name.upper(), name.upper()), release=release),
                ]
            for record in transport.models[(SUB, "rg", "r")][1:6]:
                record["properties"]["healthState"] = "Degraded"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["b"] == 4 and transport.counts()["c"] == 4)
            if due_root:
                await until(pilot, lambda: "Root R: queued" in app.query_one(StatusBar).model_work)
            await expand(pilot, app, "withdraw")
            assert "queued" in labels(app)
            await expand(pilot, app, "withdraw")  # withdraw G while both real slots are busy
            await expand(pilot, app, "nav")  # explicit F outranks queued E/D checks
            ledger = len(transport.requests)
            b_release.set()
            await until(pilot, lambda: transport.counts()["f"] == 3 and transport.counts()["d"] == 4)
            starts = [(n, suffix) for _, _, n, suffix in transport.requests[ledger:]]
            assert starts == [
                ("f", "signaldefinitions"), ("f", "entities"), ("f", "relationships"),
            ] + ([("r", "signaldefinitions"), ("r", "entities"), ("r", "relationships")] if due_root else []) + [
                ("e", "entities/e"), ("h", "entities/h"), ("d", "entities/d"),
            ]
            assert transport.counts()["g"] == 0
            assert transport.maximum == 2
            c_release.set()
            await idle(pilot, app)
    finally:
        b_release.set()
        c_release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("collapsed", ["", "D"])
async def test_references_expanded_while_both_slots_are_busy_load_unless_collapsed(service, collapsed):
    transport, client = service
    transport.put("R", *(entity("R", f"ref-{name}", model_id(name)) for name in "BCDEH"))
    for name in "BCDEH":
        transport.put(name)
    release = threading.Event()
    transport.blocks.update(b=release, c=release)
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(160, 55)) as pilot:
            await until(pilot, lambda: "ref-H" in labels(app))
            # Each activation withdraws the previous one's explicit demand while D, E and H wait.
            for name in "BCDEH":
                next(n for n in nodes(app) if n.data and n.data.entity_name == f"ref-{name}").expand()
            await until(pilot, lambda: app._poller._active is not None and app._poller._active.name == "H")
            for name in collapsed:
                next(n for n in nodes(app) if n.data and n.data.entity_name == f"ref-{name}").collapse()
            await until(pilot, lambda: not any(key.name in collapsed for key in app._poller._visible))
            release.set()
            await until(pilot, lambda: all(transport.counts()[name] == 3 for name in "bceh"))
            await idle(pilot, app)
            assert transport.counts()["d"] == (0 if collapsed else 3)
            assert ("unloaded" in labels(app)) == bool(collapsed)
    finally:
        release.set()


@pytest.mark.anyio
async def test_same_subscription_sdk_instances_are_thread_owned(service, monkeypatch):
    transport, _ = service
    owners = {}
    pipelines = []

    class ThreadTransport(HttpTransport):
        def __init__(self):
            pipelines.append(self)

        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, request, **kwargs):
            ident = threading.get_ident()
            assert owners.setdefault(self, ident) == ident, "SDK transport crossed worker threads"
            return transport.send(request, **kwargs)

    def factory(ctx, typ, *, subscription_id, api_version):
        return CloudHealthMgmtClient(_StaticTokenCredential(), subscription_id,
                                     api_version=api_version, transport=ThreadTransport(), retry_total=0)

    monkeypatch.setattr("azure.cli.core.commands.client_factory.get_mgmt_service_client", factory)
    transport.put("R", entity("R", "left", model_id("B")), entity("R", "right", model_id("C")))
    transport.put("B")
    transport.put("C")
    release = threading.Event()
    transport.blocks.update(b=release, c=release)
    app = HealthWatchApp(CloudHealthClient(None, SUB), "rg", "R", poll_interval=9999)
    try:
        async with app.run_test() as pilot:
            await until(pilot, lambda: "right" in labels(app))
            for name in ["left", "right"]:
                next(n for n in nodes(app) if n.data and n.data.entity_name == name).expand()
            await until(pilot, lambda: transport.inflight == 2)
            release.set()
            await idle(pilot, app)
            assert transport.counts() == {"r": 3, "b": 3, "c": 3}
            assert len(set(owners.values())) == 2
            assert len(owners) >= 2
    finally:
        release.set()


@pytest.mark.anyio
async def test_withdrawn_navigation_after_acknowledged_health_change_is_cached_not_stale(service):
    transport, client = service
    transport.put("R", *(entity("R", f"ref-{n}", model_id(n)) for n in "BCD"))
    for name in "BCD":
        transport.put(name)
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: "ref-B" in labels(app))
            await expand(pilot, app, "ref-B")
            await until(pilot, lambda: transport.counts()["b"] == 3)
            await idle(pilot, app)
            await enter_root(pilot, app)
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            transport.models[(SUB, "rg", "b")][0]["properties"]["healthState"] = "Degraded"
            await refresh(pilot, app, transport)
            assert transport.counts()["b"] == 7
            transport.blocks.update(c=release, d=release)
            for name in ["ref-C", "ref-D"]:
                next(n for n in nodes(app) if n.data and n.data.entity_name == name).expand()
            await until(pilot, lambda: transport.inflight == 2)
            await expand(pilot, app, "ref-B")  # collapse
            await expand(pilot, app, "ref-B")  # queued navigation
            assert "queued" in labels(app)
            await expand(pilot, app, "ref-B")  # withdraw
            reference = next(n for n in nodes(app) if n.data and n.data.entity_name == "ref-B")
            assert "[cached;" in reference.label.plain and "stale" not in reference.label.plain
            release.set()
            await idle(pilot, app)
            assert transport.counts()["b"] == 7
    finally:
        release.set()


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [("other", SUB), ("rg", OTHER_SUB)])
async def test_same_named_models_in_different_scopes_are_distinct(service, scope):
    transport, client = service
    group, sub = scope
    transport.put("R", entity("R", "local", model_id("B")),
                  entity("R", "scoped", model_id("B", group, sub)),
                  entity("R", "alias", model_id("B").upper() + "/"))
    transport.put("B", entity("B", "local-data"))
    transport.put("B", entity("B", "other-data"), group=group, sub=sub)
    release = threading.Event()
    transport.blocks["b"] = release
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await until(pilot, lambda: "alias" in labels(app))
            for name in ["local", "scoped", "alias"]:
                next(n for n in nodes(app) if n.data and n.data.entity_name == name).expand()
            await until(pilot, lambda: transport.inflight == 2)
            await pilot.pause()
            release.set()
            await until(pilot, lambda: "local-data" in labels(app) and "other-data" in labels(app))
            await idle(pilot, app)
            assert transport.counts() == {"r": 3, "b": 6}
            assert sum(n.data.entity_name == "local-data" for n in nodes(app) if n.data) == 2
            assert sum(r[0] == sub and r[1] == group and r[2] == "b" for r in transport.requests) == 3
    finally:
        release.set()


@pytest.mark.anyio
async def test_local_dag_keeps_two_reference_paths(service):
    transport, client = service
    transport.put("R", entity("R", "left"), entity("R", "right"), entity("R", "ref", model_id("B")))
    transport.put("B")
    transport.relationships["r"] = [
        {"name": f"edge-{i}", "properties": {"parentEntityName": p, "childEntityName": c}}
        for i, (p, c) in enumerate([("R", "left"), ("R", "right"), ("left", "ref"), ("right", "ref")])
    ]
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test() as pilot:
        await until(pilot, lambda: labels(app).count("ref ─") == 2)
        next(n for n in nodes(app) if n.data and n.data.entity_name == "ref").expand()
        await until(pilot, lambda: labels(app).count("B ─") == 1)
        await idle(pilot, app)
        next(n for n in nodes(app) if n.data and n.data.entity_name == "ref" and not n.is_expanded).expand()
        await until(pilot, lambda: labels(app).count("B ─") == 2)
        assert transport.counts() == {"r": 3, "b": 3}


@pytest.mark.anyio
async def test_twenty_active_cursor_moves_coalesce(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B", *(entity("B", f"row-{i}") for i in range(25)))
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(150, 60)) as pilot:
        await until(pilot, lambda: "ref" in labels(app))
        await expand(pilot, app, "ref")
        await until(pilot, lambda: "row-24" in labels(app))
        await idle(pilot, app)
        tree = app.query_one(HealthTree)
        node = next(n for n in nodes(app) if n.data and n.data.entity_name == "B")
        tree.move_cursor(node)
        await pilot.press(*(["down"] * 20))
        await idle(pilot, app)
        assert transport.counts() == {"r": 3, "b": 3}


@pytest.mark.anyio
async def test_normal_due_timer_refreshes_active_model(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B")
    app = HealthWatchApp(client, "rg", "R", poll_interval=1)
    async with app.run_test() as pilot:
        await until(pilot, lambda: "ref" in labels(app))
        await expand(pilot, app, "ref")
        await until(pilot, lambda: "B ─" in labels(app))
        await until(pilot, lambda: transport.counts()["r"] == 6 and transport.counts()["b"] == 6)
        await idle(pilot, app)
        assert transport.counts() == {"r": 6, "b": 6}


@pytest.mark.anyio
async def test_removed_reference_rejects_late_check_and_retains_new_target_unloaded(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))
    transport.put("B")
    transport.put("C")
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(150, 50)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            await expand(pilot, app, "ref")
            await until(pilot, lambda: "B ─" in labels(app))
            await idle(pilot, app)
            await enter_root(pilot, app)
            transport.replies[("b", "entities/b")] = [
                Reply(entity("B", "B", health="Degraded"), release=release),
            ]
            transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["b"] == 4)
            transport.models[(SUB, "rg", "r")][1]["properties"]["signalGroups"]["azureResource"]["azureResourceId"] = model_id("C")
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["r"] == 9)
            release.set()
            await idle(pilot, app)
            assert transport.counts() == {"r": 9, "b": 4}
            assert "B ─" not in labels(app)
            assert "[unloaded]" in labels(app)
    finally:
        release.set()


def test_plain_reports_unloaded_scope_without_crawling(service, monkeypatch, capsys):
    from azext_healthmodels_plus.tui.watch.plain import run_plain_watch

    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")))

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("azext_healthmodels_plus.tui.watch.plain.time.sleep", interrupt)
    run_plain_watch(client, "rg", "R")
    output = capsys.readouterr().out
    assert "loading" in output and "cached" in output and "unloaded" in output
    assert "Watch stopped" in output
    assert transport.counts() == {"r": 3}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["denied", "missing-root"])
async def test_initial_child_failure_is_unavailable_and_siblings_remain_usable(service, failure):
    transport, client = service
    transport.put("R", entity("R", "bad", model_id("B")), entity("R", "good", model_id("C")))
    transport.put("B")
    transport.put("C", entity("C", "usable-sibling"))
    if failure == "denied":
        transport.replies[("b", "signaldefinitions")] = [Reply({
            "error": {"code": "Denied", "message": "no target permission"},
        }, 403)]
    else:
        transport.models[(SUB, "rg", "b")] = [entity("B", "not-the-named-root")]
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(160, 55)) as pilot:
        await until(pilot, lambda: "bad" in labels(app))
        await expand(pilot, app, "bad")
        await until(pilot, lambda: "failed" in labels(app))
        assert "B ─" not in labels(app)
        assert "no target permission" in labels(app) if failure == "denied" else "Missing expected nested root" in labels(app)
        await expand(pilot, app, "good")
        await until(pilot, lambda: "usable-sibling" in labels(app))
        assert app.query_one(StatusBar).connected


@pytest.mark.anyio
async def test_loaded_grandchild_does_not_refresh_when_its_binding_is_unchanged(service):
    transport, client = service
    transport.put("R", entity("R", "ref-B", model_id("B")))
    transport.put("B", entity("B", "ref-C", model_id("C")))
    transport.put("C")
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(160, 55)) as pilot:
        await until(pilot, lambda: "ref-B" in labels(app))
        await expand(pilot, app, "ref-B")
        await until(pilot, lambda: "ref-C" in labels(app))
        await expand(pilot, app, "ref-C")
        await until(pilot, lambda: "C ─" in labels(app))
        await idle(pilot, app)
        await enter_root(pilot, app)
        transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
        transport.models[(SUB, "rg", "b")][0]["properties"]["healthState"] = "Degraded"
        await refresh(pilot, app, transport)
        assert transport.counts() == {"r": 6, "b": 7, "c": 3}


def selected_signal(app, model: str):
    return next(n for n in nodes(app) if n.data and n.data.is_signal
                and n.parent and f"{model} scoped shared" in n.parent.label.plain)


@pytest.mark.anyio
async def test_delayed_history_cannot_replace_new_context_and_removed_drawer_is_local(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")), entity("R", "shared"))
    transport.put("B", entity("B", "shared"))
    add_signal(transport, "R")
    add_signal(transport, "B")
    release = threading.Event()
    transport.replies[("b", "entities/shared/getsignalhistory")] = [Reply({
        "entityName": "shared", "signalName": "same-signal", "history": [{
            "occurredAt": "2026-10-17T11:00:00Z", "value": 999, "healthState": "Healthy",
        }],
    }, release=release)]
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(180, 60)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            await expand(pilot, app, "ref")
            await until(pilot, lambda: "B scoped shared" in labels(app))
            tree = app.query_one(HealthTree)
            tree.move_cursor(selected_signal(app, "B"))
            await pilot.press("v")
            await until(pilot, lambda: transport.inflight == 1 and
                        transport.requests[-1][3].endswith("getsignalhistory"))
            tree.move_cursor(selected_signal(app, "R"))
            await pilot.press("v")
            assert "Queued" in str(app.query_one("#signal-panel-status", Static).render())
            release.set()
            await until(pilot, lambda: transport.requests[-1] == (
                SUB, "rg", "r", "entities/shared/getsignalhistory",
            ))
            await pilot.pause()
            assert "query-r" in str(app.query_one("#signal-panel-result", Static).render())
            assert "999" not in str(app.query_one("#signal-panel-sparkline", Static).render())
            b_shared = next(n for n in nodes(app) if "B scoped shared" in n.label.plain and n.data and not n.data.is_signal)
            tree.move_cursor(b_shared)
            tree.focus()
            await pilot.press("d")
            await idle(pilot, app)
            assert app.query_one(EntityDrawer).is_visible
            transport.models[(SUB, "rg", "r")] = [
                e for e in transport.models[(SUB, "rg", "r")] if e["name"] != "ref"
            ]
            await refresh(pilot, app, transport)
            assert not app.query_one(EntityDrawer).is_visible
            assert app.query_one(SignalPanel).display
            assert app.query_one(SignalPanel).context.model.name == "R"
    finally:
        release.set()


@pytest.mark.anyio
async def test_dismissed_editor_ignores_late_nested_configuration(service):
    transport, client = service
    transport.put("R", entity("R", "ref", model_id("B")), entity("R", "shared"))
    transport.put("B", entity("B", "shared"))
    add_signal(transport, "R")
    add_signal(transport, "B")
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(160, 55)) as pilot:
            await until(pilot, lambda: "ref" in labels(app))
            await expand(pilot, app, "ref")
            await until(pilot, lambda: "B scoped shared" in labels(app))
            tree = app.query_one(HealthTree)
            transport.replies[("b", "entities/shared")] = [
                Reply(copy.deepcopy(transport.models[(SUB, "rg", "b")][-1]), release=release),
            ]
            tree.move_cursor(selected_signal(app, "B"))
            await pilot.press("e")
            await until(pilot, lambda: transport.inflight == 1)
            await pilot.press("escape")
            assert not isinstance(app.screen, QueryEditor)
            tree.move_cursor(selected_signal(app, "R"))
            await pilot.press("e")
            await until(pilot, lambda: isinstance(app.screen, QueryEditor) and
                        "R scoped shared" in str(app.screen.query_one("#qe-title", Label).render()))
            release.set()
            await pilot.pause()
            assert "R scoped shared" in str(app.screen.query_one("#qe-title", Label).render())
    finally:
        release.set()


@pytest.mark.anyio
async def test_unrelated_commit_preserves_nested_selection_expansion_and_scroll(service):
    transport, client = service
    transport.put("R", entity("R", "ref-B", model_id("B")), entity("R", "ref-C", model_id("C")))
    transport.put("B", *(entity("B", f"row-{i}") for i in range(80)))
    transport.put("C")
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(120, 35)) as pilot:
        await until(pilot, lambda: "ref-B" in labels(app))
        await expand(pilot, app, "ref-B")
        await until(pilot, lambda: "row-79" in labels(app))
        tree = app.query_one(HealthTree)
        row = next(n for n in nodes(app) if n.data and n.data.entity_name == "row-60")
        tree.move_cursor(row)
        tree.scroll_to_node(row, animate=False)
        await pilot.pause()
        scroll = tree.scroll_y
        assert scroll > 0
        next(n for n in nodes(app) if n.data and n.data.entity_name == "ref-C").expand()
        await until(pilot, lambda: "C ─" in labels(app))
        await pilot.pause()
        assert tree.cursor_node.data.entity_name == "row-60"
        assert tree.scroll_y == scroll
        assert transport.counts()["b"] == 3


@pytest.mark.anyio
async def test_acknowledged_conflicting_alias_does_not_reprobe_after_another_alias_aba(service):
    transport, client = service
    transport.put("R", entity("R", "stable", model_id("B"), "Degraded"),
                  entity("R", "changing", model_id("B")))
    transport.put("B")
    release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(170, 50)) as pilot:
            await until(pilot, lambda: "changing" in labels(app))
            await expand(pilot, app, "changing")
            await until(pilot, lambda: "B ─" in labels(app))
            await idle(pilot, app)
            await enter_root(pilot, app)
            transport.replies[("b", "entities/b")] = [
                Reply(entity("B", "B", health="Unhealthy"), release=release),
            ]
            changing = transport.models[(SUB, "rg", "r")][2]["properties"]
            changing["healthState"] = "Unhealthy"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["b"] == 4)
            changing["healthState"] = "Healthy"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["r"] == 9)
            release.set()
            await idle(pilot, app)
            await refresh(pilot, app, transport)
            assert transport.counts() == {"r": 12, "b": 4}
    finally:
        release.set()


@pytest.mark.anyio
async def test_search_and_escalation_jump_do_not_open_unloaded_models(service):
    transport, client = service
    transport.put("R", entity("R", "reference", model_id("B")))
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test() as pilot:
        await until(pilot, lambda: "reference" in labels(app))
        await pilot.press("/")
        assert "1 unloaded" in str(app.screen.query_one("#search-scope", Static).render())
        app.screen.query_one(Input).value = "reference"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert transport.counts() == {"r": 3}
        transport.models[(SUB, "rg", "r")][1]["properties"]["healthState"] = "Degraded"
        await refresh(pilot, app, transport)
        assert transport.counts() == {"r": 6}
        assert "[unloaded]" in labels(app)


@pytest.mark.parametrize("depth", [1, 32, 1100])
def test_installed_tree_widget_renders_and_selects_deep_projection(depth):
    script = """
import asyncio, sys
from dataclasses import replace
from textual.app import App
from azext_healthmodels_plus.tui.domain.parse import parse_entities
from azext_healthmodels_plus.tui.models.domain import Forest
from azext_healthmodels_plus.tui.watch.health_tree import HealthTree
depth = int(sys.argv[1])
entities = parse_entities([
    {"id": f"/entities/node-{i}", "name": f"node-{i}", "properties": {"healthState": "Healthy"}}
    for i in range(depth)
], {})
forest = Forest(("node-0",), {
    name: replace(node, children=(f"node-{i+1}",) if i + 1 < depth else ())
    for i, (name, node) in enumerate(entities.items())
})
class WidgetApp(App):
    def compose(self):
        yield HealthTree()
    def on_mount(self):
        self.query_one(HealthTree).apply_forest(forest, [])
async def main():
    app = WidgetApp()
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        tree = app.query_one(HealthTree)
        tree.scroll_to_entity(f"/entities/node-{depth-1}")
        await pilot.pause()
        assert tree.cursor_node.data.entity_name == f"node-{depth-1}"
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", script, str(depth)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


async def click_toggle(pilot, tree, node) -> None:
    line = node.line - int(tree.scroll_y)
    offset = 0
    for segment in tree.render_line(line):
        if segment.style and segment.style.meta.get("toggle"):
            content = tree.content_region
            assert await pilot.click(tree, offset=(
                content.x - tree.region.x + offset,
                content.y - tree.region.y + line,
            ))
            await pilot.pause()
            return
        offset += segment.cell_length
    raise AssertionError("Tree toggle not rendered")


@pytest.mark.anyio
@pytest.mark.parametrize("initially_expanded", [True, False], ids=["collapse", "expand"])
@pytest.mark.parametrize("widget_type", [Tree, HealthTree], ids=["pinned-vendor", "monitor"])
async def test_earlier_branch_toggle_rebases_selected_node_before_next_key(widget_type, initially_expanded):
    class WidgetApp(App):
        def compose(self):
            tree = widget_type("Root", id="tree") if widget_type is Tree else widget_type(id="tree")
            branch = tree.root.add("A", expand=initially_expanded)
            branch.add_leaf("A1")
            branch.add_leaf("A2")
            tree.root.add_leaf("B")
            tree.root.add_leaf("C")
            tree.root.expand()
            yield tree

    app = WidgetApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tree = app.query_one("#tree", Tree)
        branch, selected, following = tree.root.children
        tree.move_cursor(selected)
        tree.focus()
        await pilot.pause()
        await click_toggle(pilot, tree, branch)
        assert branch.is_expanded is not initially_expanded
        assert tree.cursor_node is selected
        assert tree.cursor_line == selected.line
        await pilot.press("down")
        assert tree.cursor_node is following


@pytest.mark.anyio
@pytest.mark.parametrize("initially_expanded", [True, False], ids=["collapse", "expand"])
async def test_app_earlier_toggle_preserves_selected_entity_actions(service, initially_expanded):
    transport, client = service
    transport.put("R", *(entity("R", n) for n in ("A", "A1", "A2", "B", "C")))
    transport.relationships["r"] = [
        {"name": str(i), "properties": {"parentEntityName": parent, "childEntityName": child}}
        for i, (parent, child) in enumerate((("R", "A"), ("A", "A1"), ("A", "A2"), ("R", "B"), ("R", "C")))
    ]
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: "A2" in labels(app))
        await idle(pilot, app)
        tree = app.query_one(HealthTree)
        branch = next(n for n in nodes(app) if n.data and n.data.entity_name == "A")
        selected = next(n for n in nodes(app) if n.data and n.data.entity_name == "B")
        if not initially_expanded:
            branch.collapse()
            await pilot.pause()
        tree.move_cursor(selected)
        tree.focus()
        await pilot.pause()
        await click_toggle(pilot, tree, branch)
        assert branch.is_expanded is not initially_expanded
        assert tree.cursor_node is selected
        assert tree.cursor_line == selected.line
        await pilot.press("d")
        assert app.query_one(EntityDrawer).current_entity_name == "B"
        await pilot.press("down")
        assert tree.cursor_node.data.entity_name == "C"
        assert transport.counts() == {"r": 3}


@pytest.mark.anyio
@pytest.mark.parametrize("origin,demand,extra_reads", [
    ("cached", "none", 0),
    ("failed", "none", 0),
    ("stale", "none", 0),
    ("cached", "alias", 1),
    ("cached", "navigation", 3),
    ("cached", "manual", 3),
])
async def test_matching_observation_withdraws_only_obsolete_queued_health(service, origin, demand, extra_reads):
    transport, client = service
    refs = [entity("R", f"ref-{n}", model_id(n)) for n in "CDB"]
    if demand == "alias":
        refs.append(entity("R", "alias", model_id("B")))
    transport.put("R", *refs)
    for name in "BCD":
        transport.put(name)
    c_release, d_release, old_check_release = threading.Event(), threading.Event(), threading.Event()
    parent_release = threading.Event()
    app = HealthWatchApp(client, "rg", "R", poll_interval=9999)
    try:
        async with app.run_test(size=(180, 55)) as pilot:
            await until(pilot, lambda: "ref-B" in labels(app))
            for name in "CDB":
                next(n for n in nodes(app) if n.data and n.data.entity_name == f"ref-{name}").expand()
            await until(pilot, lambda: all(transport.counts()[n] == 3 for n in "bcd"))
            await idle(pilot, app)
            await enter_root(pilot, app)
            if origin == "failed":
                transport.replies[("b", "signaldefinitions")] = [Reply({
                    "error": {"code": "Denied", "message": "before queued invalidation"},
                }, 403)]
                await expand(pilot, app, "ref-B")
                await expand(pilot, app, "ref-B")
                await until(pilot, lambda: "before queued invalidation" in labels(app))
                await idle(pilot, app)
                await enter_root(pilot, app)
            elif origin == "stale":
                transport.replies[("b", "entities/b")] = [
                    Reply(entity("B", "B", health="Degraded"), release=old_check_release),
                ]
                transport.models[(SUB, "rg", "r")][3]["properties"]["healthState"] = "Degraded"
                await pilot.press("r")
                await until(pilot, lambda: transport.counts()["b"] == 4)
                transport.models[(SUB, "rg", "r")][3]["properties"]["healthState"] = "Healthy"
                await pilot.press("r")
                await until(pilot, lambda: transport.counts()["r"] == 9)
                old_check_release.set()
                await idle(pilot, app)

            def b_label():
                return next(n.label.plain for n in nodes(app) if n.data and n.data.entity_name == "ref-B")

            capture = b_label().split("; captured ", 1)[1].split(";", 1)[0].split("]", 1)[0]
            before_b, before_r = transport.counts()["b"], transport.counts()["r"]
            assert f"[{origin}" in b_label()
            for name, release in (("c", c_release), ("d", d_release)):
                transport.replies[(name, f"entities/{name}")] = [
                    Reply(entity(name.upper(), name.upper()), release=release),
                ]
            for record in transport.models[(SUB, "rg", "r")][1:]:
                record["properties"]["healthState"] = "Degraded"
            await pilot.press("r")
            await until(pilot, lambda: transport.counts()["c"] == 4 and transport.counts()["d"] == 4)
            assert "[queued" in b_label()
            assert transport.counts()["b"] == before_b
            transport.models[(SUB, "rg", "r")][3]["properties"]["healthState"] = "Healthy"
            await pilot.press("r")
            await until(pilot, lambda: "Root R: queued" in app.query_one(StatusBar).model_work)
            ledger_start = len(transport.requests)
            if demand in {"navigation", "manual"}:
                transport.replies[("r", "entities")] = [Reply({
                    "value": copy.deepcopy(transport.models[(SUB, "rg", "r")]),
                }, release=parent_release)]
                c_release.set()
                await until(pilot, lambda: transport.counts()["r"] == before_r + 5)
            if demand == "navigation":
                reference = next(n for n in nodes(app) if n.data and n.data.entity_name == "ref-B")
                reference.collapse()
                await pilot.pause()
                reference = next(n for n in nodes(app) if n.data and n.data.entity_name == "ref-B")
                reference.expand()
                await pilot.pause()
            elif demand == "manual":
                app.query_one(HealthTree).move_cursor(
                    next(n for n in nodes(app) if n.data and n.data.entity_name == "B")
                )
                await pilot.pause()
                await pilot.press("r")
                await enter_root(pilot, app)
            c_release.set()
            parent_release.set()
            await until(pilot, lambda: transport.counts()["r"] == before_r + 6
                        and "Root R: cached" in app.query_one(StatusBar).model_work)
            d_release.set()
            await idle(pilot, app)
            assert transport.counts()["b"] == before_b + extra_reads
            assert transport.maximum == 2
            if demand in {"none", "alias"}:
                assert f"; captured {capture}" in b_label()
            if demand == "none":
                assert f"[{origin}" in b_label()
                if origin == "failed":
                    assert "before queued invalidation" in b_label()
            if demand in {"navigation", "manual"}:
                assert all(suffix != "entities/b" for _, _, name, suffix in transport.requests[ledger_start:] if name == "b")
                assert [name for _, _, name, _ in transport.requests[ledger_start:]] == ["r"] * 3 + ["b"] * 3
    finally:
        c_release.set()
        d_release.set()
        old_check_release.set()
        parent_release.set()
