"""TDD tests for azure-mgmt-cloudhealth SDK integration.

These tests define the expected behavior for:
- Phase 1: Migrating raw HTTP calls to SDK methods
- Phase 2: Discovery rules (transport, domain, parse)
- Phase 3: Entity history (transport, domain, parse)
- Phase 4: Health model update
- Fix: create_or_update → begin_create_or_update for sub-resources

The test must run after conftest.py mocks azure.cli.core.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport


class _StaticTokenCredential:
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("token", 4_102_444_800)


class _RecordingResponse(HttpResponse):
    def __init__(
        self, request: HttpRequest, status_code: int, payload: dict[str, Any] | list[Any],
    ) -> None:
        super().__init__(request=request, internal_response=None)
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload
        self._body = json.dumps(payload).encode("utf-8")

    def body(self) -> bytes:
        return self._body

    def json(self) -> Any:
        return self._payload

    def stream_download(self, pipeline, **kwargs):  # noqa: ANN001
        yield self._body


@dataclass
class _QueuedHttp:
    status_code: int
    payload: dict[str, Any] | list[Any]


class _RecordingTransport(HttpTransport):
    def __init__(self, queued: list[_QueuedHttp]) -> None:
        self._queued = list(queued)
        self.requests: list[HttpRequest] = []

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        self.requests.append(request)
        if not self._queued:
            raise AssertionError("unexpected extra HTTP request")
        item = self._queued.pop(0)
        return _RecordingResponse(request, item.status_code, item.payload)


# ─── Helpers ───────────────────────────────────────────────────────────


def _load_json(fixtures_dir: Path, name: str) -> Any:
    """Load a fixture JSON file. Unwraps ``value`` envelope when present."""
    with open(fixtures_dir / name) as f:
        data = json.load(f)
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


# ─── A. rest_client.py — SDK method delegation tests ──────────────────


@pytest.mark.parametrize("subscription_id", ["test-sub", "other-sub"])
def test_client_preserves_api_version(monkeypatch, subscription_id) -> None:
    import sys
    from types import SimpleNamespace

    from azext_healthmodels_plus.tui.client.rest_client import API_VERSION, CloudHealthClient

    sdk_type = MagicMock(name="CloudHealthMgmtClient")
    monkeypatch.setitem(
        sys.modules, "azure.mgmt.cloudhealth",
        SimpleNamespace(CloudHealthMgmtClient=sdk_type),
    )
    factory = MagicMock()
    monkeypatch.setitem(
        sys.modules, "azure.cli.core.commands.client_factory",
        SimpleNamespace(get_mgmt_service_client=factory),
    )
    cli_ctx = MagicMock()

    client = CloudHealthClient(cli_ctx, subscription_id)

    factory.assert_called_once_with(
        cli_ctx, sdk_type, subscription_id=subscription_id, api_version=API_VERSION,
    )
    assert client._sdk is factory.return_value


def test_client_uses_stable_cloudhealth_api_version() -> None:
    from azext_healthmodels_plus.tui.client.rest_client import API_VERSION

    assert API_VERSION == "2026-11-01"


def test_get_connection_scope_uses_profile_subscription_signature(monkeypatch) -> None:
    from azure.cli.core._profile import Profile

    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient

    get_subscription = create_autospec(
        Profile.get_subscription,
        return_value={
            "id": "00000000-0000-0000-0000-000000000000",
            "name": "contoso-sub",
            "tenantId": "22222222-2222-2222-2222-222222222222",
            "tenantDisplayName": "contoso",
        },
    )
    monkeypatch.setattr(Profile, "get_subscription", get_subscription)

    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client._subscription_id = "00000000-0000-0000-0000-000000000000"
    client._scope_lock = threading.Lock()
    client._connection_scope_cache = {}
    client.arm_endpoint = "https://management.azure.com"

    scope = client.get_connection_scope()

    assert scope["subscription_id"] == "00000000-0000-0000-0000-000000000000"
    assert scope["subscription_name"] == "contoso-sub"
    assert scope["tenant_id"] == "22222222-2222-2222-2222-222222222222"
    assert scope["tenant_name"] == "contoso"
    assert scope["arm_endpoint"] == "https://management.azure.com"
    assert get_subscription.call_count == 1
    assert get_subscription.call_args.kwargs == {"subscription": "00000000-0000-0000-0000-000000000000"}


@pytest.mark.parametrize(
    ("endpoint", "profile_override", "expectation"),
    [
        ("https://MANAGEMENT.azure.com/", {}, nullcontext()),
        ("https://other.management.azure.com", {}, pytest.raises(ValueError, match="different ARM endpoint")),
        ("https://management.azure.com", {"id": ""}, pytest.raises(ValueError, match="incomplete")),
        ("https://management.azure.com", {"name": " "}, pytest.raises(ValueError, match="incomplete")),
        ("https://management.azure.com", {"tenantId": ""}, pytest.raises(ValueError, match="incomplete")),
        (
            "https://management.azure.com",
            {"id": "00000000-0000-0000-0000-000000000001"},
            pytest.raises(ValueError, match="different subscription"),
        ),
    ],
)
def test_scoped_connection_scope_matches_the_target_model_or_raises(
    monkeypatch, endpoint, profile_override, expectation,
) -> None:
    from azure.cli.core._profile import Profile

    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
    from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

    other = "00000000-0000-0000-0000-000000000002"
    profile = {"id": other, "name": "contoso-sub", "tenantId": "22222222-2222-2222-2222-222222222222", **profile_override}
    monkeypatch.setattr(Profile, "get_subscription", create_autospec(Profile.get_subscription, return_value=profile))
    monkeypatch.setattr("azure.cli.core.commands.client_factory.get_mgmt_service_client", MagicMock())
    cli_ctx = MagicMock()
    cli_ctx.cloud.endpoints.resource_manager = "https://management.azure.com/"
    target = ModelKey(endpoint, other, "rg", "model")

    with expectation:
        scope = CloudHealthClient(cli_ctx, "00000000-0000-0000-0000-000000000001").for_model(target).get_connection_scope()
        assert (scope["arm_endpoint"].casefold(), scope["subscription_id"]) == (target.canonical[0], other)


def test_sdk_request_builder_emits_stable_api_version_and_dimension_filter() -> None:
    script = """
import json
from azure.mgmt.cloudhealth.operations import _operations as ops

request = ops.build_signal_definitions_create_or_update_request(
    resource_group_name='rg',
    health_model_name='hm',
    signal_definition_name='sig-1',
    subscription_id='sub',
    api_version='2026-11-01',
    json={
        'properties': {
            'signalKind': 'AzureResourceMetric',
            'metricNamespace': 'Microsoft.Cdn/profiles',
            'metricName': 'OriginHealthPercentage',
            'timeGrain': 'PT1M',
            'aggregationType': 'Average',
            'dimensionFilter': \"Origin eq 'darkux-eastus2-001.eastus2.alwayson.actor:443'\",
        }
    },
)
print(json.dumps({'url': request.url, 'body': request.content}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    emitted = json.loads(result.stdout)
    payload = json.loads(emitted["body"])

    assert emitted["url"] == (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CloudHealth/"
        "healthmodels/hm/signaldefinitions/sig-1?api-version=2026-11-01"
    )
    assert (
        payload["properties"]["dimensionFilter"]
        == "Origin eq 'darkux-eastus2-001.eastus2.alwayson.actor:443'"
    )
    assert "dimension" not in payload["properties"]


def test_sdk_signal_history_request_builder_initial_and_continuation() -> None:
    script = """
import json
from azure.mgmt.cloudhealth.operations import _operations as ops

initial = ops.build_entities_get_signal_history_request(
    resource_group_name='rg',
    health_model_name='hm',
    entity_name='entity-1',
    subscription_id='sub',
    api_version='2026-11-01',
    json={'signalName': 'sig-1', 'startAt': '2025-01-01T00:00:00Z', 'endAt': '2025-01-01T01:00:00Z'},
)
continuation = ops.build_entities_get_signal_history_request(
    resource_group_name='rg',
    health_model_name='hm',
    entity_name='entity-1',
    subscription_id='sub',
    api_version='2026-11-01',
    json={'signalName': 'sig-1', 'nextMarker': 'm1'},
)
print(json.dumps({
    'initial_url': initial.url,
    'initial_body': initial.content,
    'continuation_url': continuation.url,
    'continuation_body': continuation.content,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    emitted = json.loads(result.stdout)
    initial_body = json.loads(emitted["initial_body"])
    continuation_body = json.loads(emitted["continuation_body"])

    assert emitted["initial_url"].endswith(
        "/healthmodels/hm/entities/entity-1/getSignalHistory?api-version=2026-11-01"
    )
    assert emitted["continuation_url"].endswith(
        "/healthmodels/hm/entities/entity-1/getSignalHistory?api-version=2026-11-01"
    )
    assert set(initial_body.keys()) == {"signalName", "startAt", "endAt"}
    assert set(continuation_body.keys()) == {"signalName", "nextMarker"}


@pytest.fixture
def mock_sdk_client():
    """Create a CloudHealthClient with a mocked SDK underneath."""
    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient

    sdk = MagicMock()
    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client._subscription_id = "test-sub"
    client._sdk = sdk
    client.arm_endpoint = "https://management.azure.com"
    return client, sdk


def _real_sdk_client_with_transport(
    transport: _RecordingTransport,
    *,
    api_version: str = "2026-11-01",
    arm_endpoint: str = "https://management.azure.com",
):
    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
    from azure.mgmt.cloudhealth import CloudHealthMgmtClient

    sdk = CloudHealthMgmtClient(
        credential=_StaticTokenCredential(),
        subscription_id="sub",
        transport=transport,
        api_version=api_version,
        retry_total=0,
    )
    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client._subscription_id = "sub"
    client._sdk = sdk
    client.arm_endpoint = arm_endpoint
    return client


def test_get_signal_history_uses_sdk_send_request(mock_sdk_client) -> None:
    client, sdk = mock_sdk_client
    sdk.send_request.return_value = MagicMock(
        raise_for_status=MagicMock(),
        json=MagicMock(return_value={"entityName": "e1", "signalName": "s1", "history": []}),
    )

    result = client.get_signal_history("rg", "m", "e1", {"signalName": "s1"})

    sdk.send_request.assert_called_once()
    assert isinstance(result, dict)


def test_get_entity_and_signal_definition_use_named_sdk_reads(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.entities.get.return_value = MagicMock(as_dict=lambda: {"name": "entity-1"})
    sdk.signal_definitions.get.return_value = MagicMock(as_dict=lambda: {"name": "sig-def-1"})

    entity = client.get_entity("rg", "m", "entity-1")
    sig_def = client.get_signal_definition("rg", "m", "sig-def-1")

    assert entity["name"] == "entity-1"
    assert sig_def["name"] == "sig-def-1"
    sdk.entities.get.assert_called_once_with("rg", "m", "entity-1")
    sdk.signal_definitions.get.assert_called_once_with("rg", "m", "sig-def-1")


def test_list_health_models_subscription_scope_uses_sdk_list_by_subscription(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.health_models.list_by_subscription.return_value = [
        {"id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/a", "name": "a"},
    ]

    rows = client.list_health_models()

    assert rows[0]["name"] == "a"
    sdk.health_models.list_by_subscription.assert_called_once_with()
    sdk.health_models.list_by_resource_group.assert_not_called()


def test_list_health_models_group_scope_uses_sdk_list_by_resource_group(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.health_models.list_by_resource_group.return_value = [
        {"id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/a", "name": "a"},
    ]

    rows = client.list_health_models(resource_group="rg")

    assert rows[0]["name"] == "a"
    sdk.health_models.list_by_resource_group.assert_called_once_with("rg")
    sdk.health_models.list_by_subscription.assert_not_called()


def test_get_signal_history_paginates_next_marker(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    first_page = {
        "entityName": "e1",
        "signalName": "s1",
        "history": [{"value": 1.0}],
        "nextMarker": "m1",
    }
    second_page = {
        "entityName": "e1",
        "signalName": "s1",
        "history": [{"value": 2.0}],
    }
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=first_page)),
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=second_page)),
    ]

    result = client.get_signal_history(
        "rg", "m", "e1",
        {"signalName": "s1", "startAt": "2025-01-01T00:00:00Z", "endAt": "2025-01-01T01:00:00Z"},
    )

    assert sdk.send_request.call_count == 2
    first_request = sdk.send_request.mock_calls[0].args[0]
    second_request = sdk.send_request.mock_calls[1].args[0]
    assert json.loads(first_request.body) == {
        "signalName": "s1",
        "startAt": "2025-01-01T00:00:00Z",
        "endAt": "2025-01-01T01:00:00Z",
    }
    assert json.loads(second_request.body) == {"signalName": "s1", "nextMarker": "m1"}
    assert [point["value"] for point in result["history"]] == [1.0, 2.0]
    assert "nextMarker" not in result


def test_get_signal_history_continuation_without_signal_name_raises(mock_sdk_client) -> None:
    client, sdk = mock_sdk_client
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={"history": [], "nextMarker": "m1"})),
    ]

    with pytest.raises(ValueError, match="requires signalName"):
        client.get_signal_history("rg", "m", "e1", {"startAt": "2025-01-01T00:00:00Z"})

    assert sdk.send_request.call_count == 1


def test_get_signal_history_raises_when_later_page_fails(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={
            "entityName": "e1",
            "signalName": "s1",
            "history": [{"value": 1.0}],
            "nextMarker": "m1",
        })),
        RuntimeError("page 2 down"),
    ]

    with pytest.raises(RuntimeError, match="Signal history is incomplete because continuation failed"):
        client.get_signal_history(
            "rg", "m", "e1",
            {"signalName": "s1", "startAt": "2025-01-01T00:00:00Z", "endAt": "2025-01-01T01:00:00Z"},
        )


def test_sdk_entities_get_signal_history_is_gated_for_stable_api() -> None:
    from azure.mgmt.cloudhealth import CloudHealthMgmtClient

    sdk = CloudHealthMgmtClient(
        credential=_StaticTokenCredential(),
        subscription_id="sub",
        transport=_RecordingTransport([]),
        api_version="2026-11-01",
    )

    with pytest.raises(ValueError, match="not available in API version 2026-11-01"):
        sdk.entities.get_signal_history(
            "rg",
            "model",
            "entity-1",
            body={
                "signalName": "sig-1",
                "startAt": "2026-01-01T00:00:00Z",
                "endAt": "2026-01-01T01:00:00Z",
            },
        )


def test_list_entities_full_sdk_pipeline_handles_two_pages_nextlink() -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {
            "value": [
                {"id": "/sub/x/e1", "name": "e1", "properties": {"healthState": "Healthy", "signalGroups": {}}},
            ],
            "nextLink": "https://management.azure.com/subscriptions/sub/next?page=2",
        }),
        _QueuedHttp(200, {
            "value": [
                {"id": "/sub/x/e2", "name": "e2", "properties": {"healthState": "Degraded", "signalGroups": {}}},
            ],
        }),
    ])
    client = _real_sdk_client_with_transport(transport)

    result = client.list_entities("rg", "model")

    assert [r["name"] for r in result] == ["e1", "e2"]
    assert len(transport.requests) == 2
    assert "api-version=2026-11-01" in transport.requests[0].url
    assert transport.requests[1].url.startswith(
        "https://management.azure.com/subscriptions/sub/next?page=2"
    )


def test_get_signal_history_full_sdk_pipeline_uses_continuation_body() -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {
            "entityName": "e1",
            "signalName": "sig-1",
            "status": {"error": None},
            "history": [{"occurredAt": "2026-01-01T00:00:00Z", "value": None, "healthState": "Unknown", "additionalContext": None}],
            "nextMarker": "m1",
        }),
        _QueuedHttp(200, {
            "entityName": "e1",
            "signalName": "sig-1",
            "history": [{"occurredAt": "2026-01-01T00:05:00Z", "value": 3.0, "healthState": "Degraded", "additionalContext": "warn"}],
        }),
    ])
    client = _real_sdk_client_with_transport(transport)

    result = client.get_signal_history(
        "rg", "model", "entity-1",
        {"signalName": "sig-1", "startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
    )

    assert len(transport.requests) == 2
    first_body = json.loads(transport.requests[0].body)
    second_body = json.loads(transport.requests[1].body)
    assert "api-version=2026-11-01" in transport.requests[0].url
    assert "api-version=2026-11-01" in transport.requests[1].url
    assert set(first_body.keys()) == {"signalName", "startAt", "endAt"}
    assert set(second_body.keys()) == {"signalName", "nextMarker"}
    assert result["status"]["error"] is None
    assert result["history"][0]["value"] is None
    assert result["history"][0]["healthState"] == "Unknown"
    assert result["history"][1]["additionalContext"] == "warn"


def test_get_signal_history_full_sdk_pipeline_raises_on_continuation_failure() -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {
            "entityName": "e1",
            "signalName": "sig-1",
            "history": [{"occurredAt": "2026-01-01T00:00:00Z", "value": 1.0, "healthState": "Healthy"}],
            "nextMarker": "m1",
        }),
        _QueuedHttp(500, {"error": {"code": "ServerError", "message": "page2 failed"}}),
    ])
    client = _real_sdk_client_with_transport(transport)

    with pytest.raises(RuntimeError, match="Signal history is incomplete because continuation failed"):
        client.get_signal_history(
            "rg", "model", "entity-1",
            {"signalName": "sig-1", "startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
        )


# ─── B. Domain model tests ────────────────────────────────────────────


def test_parse_signal_definition_dynamic_rules_are_not_coerced(
    fixtures_dir: Path,
) -> None:
    from azext_healthmodels_plus.tui.domain.parse import parse_signal_definition

    raw = {
        "name": "sig-dynamic",
        "properties": {
            "signalKind": "PrometheusMetricsQuery",
            "evaluationRules": {
                "degradedRule": {"operator": "Dynamic", "threshold": 1},
                "unhealthyRule": {"operator": "Dynamic", "threshold": 2},
            },
        },
    }

    parsed = parse_signal_definition(raw)
    assert parsed.degraded_rule is None
    assert parsed.unhealthy_rule is None


@pytest.mark.parametrize(
    ("wire_operator", "value", "violates"),
    [
        ("GreaterThan", 96.0, True),
        ("LessThan", 94.0, True),
        ("LessThan", 95.0, False),
        ("LessThanOrEqual", 95.0, True),
        ("GreaterThanOrEqual", 95.0, True),
        ("Equal", 95.0, True),
    ],
)
def test_parse_signal_definition_reads_sdk_wire_operators(wire_operator: str, value: float, violates: bool) -> None:
    from azext_healthmodels_plus.tui.domain.parse import parse_signal_definition

    raw = {
        "name": "sig-availability",
        "properties": {
            "signalKind": "AzureResourceMetric",
            "evaluationRules": {"unhealthyRule": {"operator": wire_operator, "threshold": 95}},
        },
    }

    rule = parse_signal_definition(raw).unhealthy_rule
    assert rule is not None
    assert rule.operator.value == wire_operator
    assert rule.operator.evaluate(value, rule.threshold) is violates


@pytest.mark.parametrize(
    "wire_impact,expected",
    [
        ("Suppressed", "Suppressed"),
        ("None", "Suppressed"),
        ("Limited", "Limited"),
    ],
)
def test_parse_entity_impact_stable_wire_semantics(
    wire_impact: str,
    expected: str,
) -> None:
    from azext_healthmodels_plus.tui.domain.parse import parse_entity
    from azext_healthmodels_plus.tui.models.enums import Impact

    entity = parse_entity(
        {
            "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/m/entities/e1",
            "name": "e1",
            "properties": {
                "displayName": "E1",
                "healthState": "Healthy",
                "impact": wire_impact,
                "signalGroups": {},
            },
        },
        sig_defs={},
    )
    assert entity.impact == Impact(expected)


_CPU_RULES = {"unhealthyRule": {"operator": "GreaterThan", "threshold": 95.0}}


@pytest.mark.parametrize(
    ("signal", "definitions"),
    [
        # Live shape: inline signal without signalDefinitionName carries its own fields.
        (
            {"name": "s1", "displayName": "CPU percentage", "signalKind": "AzureResourceMetric",
             "dataUnit": "Percent", "evaluationRules": _CPU_RULES},
            [],
        ),
        (
            {"name": "s1", "signalDefinitionName": "cpu-def", "signalKind": "AzureResourceMetric"},
            [{"name": "cpu-def", "properties": {"displayName": "CPU percentage", "signalKind": "AzureResourceMetric",
                                                "dataUnit": "Percent", "evaluationRules": _CPU_RULES}}],
        ),
    ],
)
def test_parse_entity_signal_metadata_from_inline_or_definition(signal: dict, definitions: list) -> None:
    from azext_healthmodels_plus.tui.domain.parse import parse_entity, parse_signal_definitions
    from azext_healthmodels_plus.tui.models.enums import DataUnit

    entity = parse_entity(
        {
            "id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.CloudHealth/healthmodels/m/entities/e1",
            "name": "e1",
            "properties": {"signalGroups": {"azureResource": {"signals": [signal]}}},
        },
        sig_defs=parse_signal_definitions(definitions),
    )
    parsed = entity.signals[0]
    assert parsed.display_name == "CPU percentage"
    assert parsed.data_unit is DataUnit.PERCENT
    assert parsed.unhealthy_rule is not None and parsed.unhealthy_rule.threshold == 95.0


# ─── Phase 1: Diagnostic UI sinks ────────────────────────────────────

def test_truncate_status_error_short():
    from azext_healthmodels_plus.tui.watch.status_bar import _truncate_status_error
    assert _truncate_status_error("short error") == "short error"

def test_truncate_status_error_long():
    from azext_healthmodels_plus.tui.watch.status_bar import _truncate_status_error
    msg = "A" * 100
    result = _truncate_status_error(msg)
    assert len(result) == 80
    assert result.endswith("…")

def test_truncate_status_error_multiline():
    from azext_healthmodels_plus.tui.watch.status_bar import _truncate_status_error
    msg = "line one\nline two\tand tabs"
    result = _truncate_status_error(msg)
    assert "\n" not in result
    assert "\t" not in result
    assert result == "line one line two and tabs"

@pytest.mark.parametrize("limit", [40, 60, 80, 120])
def test_truncate_status_error_custom_limit(limit):
    from azext_healthmodels_plus.tui.watch.status_bar import _truncate_status_error
    msg = "X" * 200
    result = _truncate_status_error(msg, limit=limit)
    assert len(result) == limit
    assert result.endswith("…")


# ─── Phase 2: Poll error plumbing ────────────────────────────────────

def test_poller_preserves_exception_type_in_error():
    """Poller error message includes exception type and message."""
    from azext_healthmodels_plus.tui.watch.poller import Poller
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_signal_definitions.side_effect = RuntimeError("API down")

    poller = Poller(client, "rg", "model")
    result = poller.poll_once()

    assert result.error is not None
    assert "RuntimeError" in result.error
    assert "API down" in result.error

def test_poller_preserves_auth_error_in_error():
    """Poller error message includes AuthenticationError details."""
    from azext_healthmodels_plus.tui.watch.poller import Poller
    from azext_healthmodels_plus.tui.client.errors import AuthenticationError
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_signal_definitions.side_effect = AuthenticationError("access denied")

    poller = Poller(client, "rg", "model")
    result = poller.poll_once()

    assert result.error is not None
    assert "AuthenticationError" in result.error
    assert "access denied" in result.error
    assert "stale" in result.error.lower()

@pytest.mark.parametrize("exc_class,exc_msg", [
    (RuntimeError, "connection timeout"),
    (ValueError, "bad response"),
    (ConnectionError, "network unreachable"),
])
def test_poller_error_format_parametrized(exc_class, exc_msg):
    from azext_healthmodels_plus.tui.watch.poller import Poller
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_signal_definitions.side_effect = exc_class(exc_msg)

    poller = Poller(client, "rg", "model")
    result = poller.poll_once()

    assert exc_class.__name__ in result.error
    assert exc_msg in result.error


# ─── Phase 3: Signal errors and thresholds ───────────────────────────

def test_signal_value_with_error_field():
    """SignalValue accepts optional error field."""
    from azext_healthmodels_plus.tui.models.domain import SignalValue
    from azext_healthmodels_plus.tui.models.enums import DataUnit, HealthState, SignalKind

    sig = SignalValue(
        name="sig-1",
        definition_name="def-1",
        display_name="CPU",
        signal_kind=SignalKind.PROMETHEUS_METRICS_QUERY,
        health_state=HealthState.UNKNOWN,
        value=None,
        data_unit=DataUnit.PERCENT,
        reported_at="2026-01-01T00:00:00Z",
        error="Metric namespace not found",
    )
    assert sig.error == "Metric namespace not found"


def test_signal_value_with_threshold_rules():
    """SignalValue accepts optional threshold rules from signal definition."""
    from azext_healthmodels_plus.tui.models.domain import EvaluationRule, SignalValue
    from azext_healthmodels_plus.tui.models.enums import (
        ComparisonOperator, DataUnit, HealthState, SignalKind,
    )

    degraded = EvaluationRule(operator=ComparisonOperator.GREATER_THAN, threshold=50.0)
    unhealthy = EvaluationRule(operator=ComparisonOperator.GREATER_THAN, threshold=80.0)

    sig = SignalValue(
        name="sig-1",
        definition_name="def-1",
        display_name="CPU",
        signal_kind=SignalKind.AZURE_RESOURCE_METRIC,
        health_state=HealthState.HEALTHY,
        value=42.0,
        data_unit=DataUnit.PERCENT,
        reported_at="2026-01-01T00:00:00Z",
        degraded_rule=degraded,
        unhealthy_rule=unhealthy,
    )
    assert sig.degraded_rule.threshold == 50.0
    assert sig.unhealthy_rule.threshold == 80.0


def test_signal_value_backward_compat_no_new_fields():
    """Existing SignalValue construction without new fields still works."""
    from azext_healthmodels_plus.tui.models.domain import SignalValue
    from azext_healthmodels_plus.tui.models.enums import DataUnit, HealthState, SignalKind

    sig = SignalValue(
        name="sig-1",
        definition_name="def-1",
        display_name="CPU",
        signal_kind=SignalKind.EXTERNAL,
        health_state=HealthState.HEALTHY,
        value=10.0,
        data_unit=DataUnit.COUNT,
        reported_at="2026-01-01T00:00:00Z",
    )
    assert sig.error is None
    assert sig.degraded_rule is None
    assert sig.unhealthy_rule is None


def test_parse_signal_ref_threads_error_and_thresholds(fixtures_dir):
    """Parse layer threads signal status error and definition thresholds into SignalValue."""
    import json
    from azext_healthmodels_plus.tui.domain.parse import parse_entities, parse_signal_definitions

    with open(fixtures_dir / "hm-signals.json") as f:
        raw_sigs = json.load(f)["value"]
    with open(fixtures_dir / "hm-entities.json") as f:
        raw_entities = json.load(f)["value"]

    sig_defs = parse_signal_definitions(raw_sigs)
    entities = parse_entities(raw_entities, sig_defs)

    found_threshold = False
    for entity in entities.values():
        for sig in entity.signals:
            if sig.unhealthy_rule is not None:
                found_threshold = True
                assert sig.unhealthy_rule.threshold > 0
                break
        if found_threshold:
            break

    assert found_threshold, "Expected at least one signal with thresholds from fixture data"


# ─── Phase 4: ARM error details preservation ─────────────────────────

def test_health_model_error_has_code_and_details():
    from azext_healthmodels_plus.tui.client.errors import HealthModelError
    err = HealthModelError("boom", status_code=500, code="InternalError", details=[{"message": "oops"}])
    assert err.code == "InternalError"
    assert err.details == [{"message": "oops"}]
    assert "InternalError" in err.diagnostic_text()

def test_auth_error_preserves_code():
    from azext_healthmodels_plus.tui.client.errors import AuthenticationError
    err = AuthenticationError("denied", code="AuthorizationFailed")
    assert err.code == "AuthorizationFailed"
    assert "AuthorizationFailed" in err.diagnostic_text()

def test_throttled_error_preserves_code_and_retry():
    from azext_healthmodels_plus.tui.client.errors import ThrottledError
    err = ThrottledError("slow down", retry_after=30, code="TooManyRequests")
    assert err.retry_after == 30
    assert err.code == "TooManyRequests"

def test_not_found_error_preserves_details():
    from azext_healthmodels_plus.tui.client.errors import HealthModelNotFoundError
    err = HealthModelNotFoundError("gone", code="ResourceNotFound", details=[{"message": "model xyz not found"}])
    assert err.code == "ResourceNotFound"
    assert "model xyz not found" in err.diagnostic_text()
    assert "az healthmodel list" not in str(err)
    assert "az health-models-tui -g" in str(err)

def test_arm_error_no_duplicate_fields():
    from azext_healthmodels_plus.tui.client.errors import ArmError
    err = ArmError("fail", status_code=500, code="ServerError", details=[{"code": "X", "message": "Y"}])
    assert err.code == "ServerError"
    assert err.details == [{"code": "X", "message": "Y"}]

@pytest.mark.parametrize("code,details,expected_in_diagnostic", [
    ("", [], "boom"),
    ("E1", [], "code E1"),
    ("E2", [{"message": "detail msg"}], "detail msg"),
    ("", [{"code": "C1"}], "C1"),
])
def test_diagnostic_text_parametrized(code, details, expected_in_diagnostic):
    from azext_healthmodels_plus.tui.client.errors import HealthModelError
    err = HealthModelError("boom", status_code=400, code=code, details=details)
    assert expected_in_diagnostic in err.diagnostic_text()

def test_parse_arm_error_preserves_code_on_subclasses():
    from azext_healthmodels_plus.tui.client.errors import parse_arm_error, AuthenticationError, HealthModelNotFoundError

    body_auth = {"error": {"code": "AuthorizationFailed", "message": "not allowed", "details": [{"message": "forbidden"}]}}
    err = parse_arm_error(body_auth, 403)
    assert isinstance(err, AuthenticationError)
    assert err.code == "AuthorizationFailed"

    body_404 = {"error": {"code": "ResourceNotFound", "message": "missing", "details": []}}
    err2 = parse_arm_error(body_404, 404)
    assert isinstance(err2, HealthModelNotFoundError)
    assert err2.code == "ResourceNotFound"


# ─── Phase 5: Query diagnostics ──────────────────────────────────────

def test_execute_signal_structured_error_fields():
    """execute_signal result includes structured error metadata."""
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal
    from azext_healthmodels_plus.tui.client.errors import ArmError
    from unittest.mock import MagicMock

    client = MagicMock()
    client.get_entity.return_value = {
        "name": "entity-1",
        "properties": {
            "signalGroups": {
                "azureMonitorWorkspace": {
                    "azureMonitorWorkspaceResourceId": "/sub/x/amw",
                    "signals": [{
                        "name": "sig-1",
                        "signalKind": "PrometheusMetricsQuery",
                        "queryText": "rate(x[5m])",
                        "timeGrain": "PT1M",
                    }]
                }
            }
        }
    }
    client.query_prometheus.side_effect = ArmError(
        "workspace not found", status_code=404, code="ResourceNotFound"
    )

    result = execute_signal(client, "rg", "model", "entity-1", "sig-1")

    assert result["error"] is not None
    assert result.get("errorType") == "ArmError"
    assert result.get("errorStatusCode") == 404
    assert result.get("errorCode") == "ResourceNotFound"


@pytest.mark.parametrize(
    "evaluation_rules",
    [
        {"unhealthyRule": {"operator": "Dynamic", "sensitivity": "Medium"}},
        {
            "degradedRule": {"operator": "Dynamic", "sensitivity": "High"},
            "unhealthyRule": {"operator": "GreaterThan", "threshold": 99},
        },
    ],
)
def test_execute_signal_dynamic_rules_are_reported_unknown_without_fake_healthy(
    evaluation_rules: dict[str, Any],
) -> None:
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal

    client = MagicMock()
    client.get_entity.return_value = {
        "id": "/sub/x/entities/e1",
        "name": "entity-1",
        "properties": {
            "signalGroups": {
                "azureMonitorWorkspace": {
                    "azureMonitorWorkspaceResourceId": "/sub/x/amw",
                    "signals": [{
                        "name": "sig-1",
                        "signalKind": "PrometheusMetricsQuery",
                        "queryText": "rate(x[5m])",
                        "timeGrain": "PT1M",
                        "evaluationRules": evaluation_rules,
                    }]
                }
            }
        },
    }
    client.query_prometheus.return_value = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"value": [1234567890, "12.0"]}]},
    }

    result = execute_signal(client, "rg", "model", "entity-1", "sig-1")

    assert result["rawValue"] == 12.0
    assert result["healthState"] == "Unknown"
    assert result["error"] in ("", None)
    assert "Dynamic" in (result.get("evaluationWarning") or "")


def test_execute_signal_azure_metric_error_path():
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal
    from azext_healthmodels_plus.tui.client.errors import ArmError

    client = MagicMock()
    client.get_entity.return_value = {
        "name": "entity-1",
        "properties": {
            "signalGroups": {
                "azureResource": {
                    "azureResourceId": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
                    "signals": [{
                        "name": "sig-1",
                        "signalKind": "AzureResourceMetric",
                        "metricNamespace": "Microsoft.Compute/virtualMachines",
                        "metricName": "Percentage CPU",
                        "aggregationType": "Average",
                        "timeGrain": "PT1M",
                    }],
                }
            }
        },
    }
    client.query_azure_metric.side_effect = ArmError(
        "metric call failed", status_code=403, code="AuthorizationFailed",
    )

    result = execute_signal(client, "rg", "model", "entity-1", "sig-1")

    assert result["error"] is not None
    assert result["errorType"] == "ArmError"
    assert result["errorStatusCode"] == 403
    assert result["errorCode"] == "AuthorizationFailed"


@pytest.mark.parametrize("arm_endpoint", ["https://management.azure.com", "https://management.chinacloudapi.cn"])
def test_query_prometheus_uses_approved_workspace_stable_api_version(monkeypatch, arm_endpoint) -> None:
    import sys
    from types import SimpleNamespace

    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient

    calls: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    def _send_raw_request(cli_ctx, method, url, **kwargs):  # noqa: ANN001
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        if len(calls) == 1:
            return _Resp(
                {
                    "properties": {
                        "metrics": {
                            "prometheusQueryEndpoint": "https://example.prometheus.monitor.azure.com",
                        },
                    },
                }
            )
        return _Resp({"status": "success", "data": {"result": []}})

    monkeypatch.setitem(
        sys.modules,
        "azure.cli.core.util",
        SimpleNamespace(send_raw_request=_send_raw_request),
    )

    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client._subscription_id = "sub"
    client.arm_endpoint = arm_endpoint

    client.query_prometheus(
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Monitor/accounts/ws",
        "sum(rate(x{a='b c'}[5m]))",
    )

    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].startswith(f"{arm_endpoint}/subscriptions/")
    assert "api-version=2025-10-03" in calls[0]["url"]
    assert calls[1]["method"] == "GET"
    assert "query=sum%28rate%28x%7Ba%3D%27b%20c%27%7D%5B5m%5D%29%29" in calls[1]["url"]
    assert calls[1]["kwargs"]["resource"] == "https://prometheus.monitor.azure.com"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, "AuthenticationError"), (404, "HealthModelNotFoundError"), (500, "ArmError")],
)
def test_metric_query_http_failures_become_typed_errors(monkeypatch, status: int, expected: str) -> None:
    import sys
    from types import SimpleNamespace

    from azure.cli.core.azclierror import HTTPError

    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient

    response = SimpleNamespace(status_code=status, text="denied", json=lambda: {"error": {"code": "X", "message": "denied"}})

    def _send_raw_request(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise HTTPError("denied", response)

    monkeypatch.setitem(sys.modules, "azure.cli.core.util", SimpleNamespace(send_raw_request=_send_raw_request))
    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client.arm_endpoint = "https://management.azure.com"

    with pytest.raises(Exception) as raised:
        client.query_azure_metric(
            "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Web/sites/a",
            "CpuTime", "Microsoft.Web/sites", "Average", "PT5M",
        )
    assert type(raised.value).__name__ == expected


@pytest.mark.parametrize(("time_grain", "interval"), [("PT5M", "&interval=PT5M"), ("", None)])
def test_query_azure_metric_sends_the_interval_only_when_set(time_grain: str, interval: str | None) -> None:
    from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient

    client = object.__new__(CloudHealthClient)
    client.arm_endpoint = "https://management.azure.com"
    urls: list[str] = []
    client._raw_get = lambda url, **_kw: urls.append(url) or {}  # type: ignore[method-assign]

    client.query_azure_metric(
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/providers/Microsoft.Web/sites/a",
        "CpuTime", "Microsoft.Web/sites", "Average", time_grain,
    )

    assert (interval in urls[0]) if interval else ("interval=" not in urls[0])


_LA_WORKSPACE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/ws"
)


def _la_table(columns: list[str], rows: list[list[Any]], column_type: str = "real") -> dict[str, Any]:
    """The ARM workspace proxy answers in PascalCase."""
    columns_json = [{"ColumnName": c, "ColumnType": column_type} for c in columns]
    return {"Tables": [{"TableName": "Table_0", "Columns": columns_json, "Rows": rows}]}


@pytest.mark.parametrize(
    ("arm_endpoint", "time_grain", "expected_body"),
    [
        ("https://management.azure.com", "PT30M", {"query": "print value = 20", "timespan": "PT30M"}),
        ("https://management.chinacloudapi.cn", "", {"query": "print value = 20"}),
    ],
)
def test_query_log_analytics_posts_kql_to_the_arm_workspace_proxy(
    arm_endpoint: str, time_grain: str, expected_body: dict[str, Any],
) -> None:
    payload = _la_table(["value"], [[20]])
    transport = _RecordingTransport([_QueuedHttp(200, payload)])
    client = _real_sdk_client_with_transport(transport, arm_endpoint=arm_endpoint)

    assert client.query_log_analytics(_LA_WORKSPACE, "print value = 20", time_grain) == payload

    [request] = transport.requests
    assert request.method == "POST"
    assert request.url == f"{arm_endpoint}{_LA_WORKSPACE}/api/query?api-version=2020-08-01"
    assert json.loads(request.body) == expected_body


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(400, "BadArgumentError", "ArmError"), (403, "InsufficientAccessError", "AuthenticationError")],
)
def test_query_log_analytics_error_payload_becomes_typed_error(status: int, code: str, expected: str) -> None:
    transport = _RecordingTransport([_QueuedHttp(status, {"error": {"code": code, "message": "query failed"}})])
    client = _real_sdk_client_with_transport(transport)

    with pytest.raises(Exception) as raised:
        client.query_log_analytics(_LA_WORKSPACE, "print value = ", "PT5M")

    assert type(raised.value).__name__ == expected
    assert (raised.value.status_code, raised.value.code) == (status, code)


@pytest.mark.parametrize(
    ("payload", "column", "expected_value", "expected_error"),
    [
        pytest.param(_la_table(["TimeGenerated", "Value"], [["t1", 1], ["t2", 3.5]]), "Value", 3.5, None, id="named-column-last-row"),
        pytest.param(_la_table(["value", "s"], [[20, "x"]]), "", 20.0, None, id="first-column-when-unnamed"),
        pytest.param(_la_table(["Value"], []), "Value", None, None, id="empty-table"),
        pytest.param({"Tables": []}, "Value", None, "no tables", id="no-tables"),
        pytest.param(_la_table(["Other"], [[1]]), "Value", None, "column 'Value' not in", id="missing-column"),
        pytest.param(_la_table(["s"], [["x"]]), "", None, "column 's' value 'x' is not numeric", id="non-numeric"),
        pytest.param(_la_table(["Value"], [["20"]]), "Value", None, "column 'Value' value '20' is not numeric", id="numeric-string"),
        pytest.param(_la_table(["Value"], [["20.5"]], "decimal"), "Value", 20.5, None, id="decimal-as-string"),
        pytest.param(_la_table(["Value"], [["x"]], "decimal"), "Value", None, "is not numeric", id="bad-decimal"),
        pytest.param(_la_table(["Value"], [[True]]), "Value", None, "column 'Value' value True is not numeric", id="bool"),
        pytest.param(_la_table(["Value"], [[None]]), "Value", None, "is not numeric", id="null"),
        pytest.param({"error": {"code": "PartialError", "message": "timed out"}}, "Value", None, "PartialError", id="error-payload"),
        pytest.param({"Error": {"code": "PartialError", "message": "timed out"}}, "Value", None, "PartialError", id="pascal-case-error-payload"),
    ],
)
def test_log_analytics_value_extraction(
    payload: dict[str, Any], column: str, expected_value: float | None, expected_error: str | None,
) -> None:
    from azext_healthmodels_plus.tui.client.query_executor import _extract_log_analytics_value

    value, error = _extract_log_analytics_value(payload, column)

    assert value == expected_value
    if expected_error is None:
        assert error is None
    else:
        assert expected_error in error


_GREATER_THAN = {
    "degradedRule": {"operator": "GreaterThan", "threshold": 0},
    "unhealthyRule": {"operator": "GreaterThan", "threshold": 10},
}
_LESS_THAN = {
    "degradedRule": {"operator": "LessThan", "threshold": 100},
    "unhealthyRule": {"operator": "LessThan", "threshold": 67},
}


# kind: (data source field, signal fields, client method, its arguments after the data source, payload of the values)
_SIGNAL_KINDS: dict[str, tuple[str, dict[str, Any], str, tuple[str, ...], Any]] = {
    "PrometheusMetricsQuery": (
        "azureMonitorWorkspaceResourceId", {"queryText": "rate(x[5m])", "timeGrain": "PT1M"},
        "query_prometheus", ("rate(x[5m])",),
        lambda values: {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1234567890, str(v)]} for v in values]}},
    ),
    "AzureResourceMetric": (
        "azureResourceId",
        {"metricNamespace": "Microsoft.Compute/virtualMachines", "metricName": "Percentage CPU", "aggregationType": "Average", "timeGrain": "PT1M"},
        "query_azure_metric", ("Percentage CPU", "Microsoft.Compute/virtualMachines", "Average", "PT1M"),
        lambda values: {"value": [{"timeseries": [{"data": [{"average": v} for v in values]}]}]},
    ),
    "LogAnalyticsQuery": (
        "logAnalyticsWorkspaceResourceId", {"queryText": "T | summarize value = count()", "timeGrain": "PT30M", "valueColumnName": "value"},
        "query_log_analytics", ("T | summarize value = count()", "PT30M"),
        lambda values: _la_table(["Count", "value"], [[999, v] for v in values]),
    ),
}


def _execute_signal_of_kind(kind: str, owner: str, rules: dict[str, Any], values: list[float]) -> dict[str, Any]:
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal

    source_field, fields, method, arguments, payload = _SIGNAL_KINDS[kind]
    fields = {**fields, "evaluationRules": rules}
    signal = {"name": "sig-1", "signalKind": kind}
    client = MagicMock()
    if owner == "instance":
        signal.update(fields)
    else:
        signal["signalDefinitionName"] = "def-1"
        client.get_signal_definition.return_value = {"properties": {"signalKind": kind, **fields}}
    client.get_entity.return_value = {
        "name": "entity-1",
        "properties": {"signalGroups": {"group": {source_field: _LA_WORKSPACE, "signals": [signal]}}},
    }
    getattr(client, method).return_value = payload(values)

    result = execute_signal(client, "rg", "model", "entity-1", "sig-1")

    getattr(client, method).assert_called_once_with(_LA_WORKSPACE, *arguments)
    return result


@pytest.mark.parametrize("kind", list(_SIGNAL_KINDS))
@pytest.mark.parametrize("owner", ["instance", "definition"])
@pytest.mark.parametrize(
    ("rules", "values", "expected_value", "expected_health"),
    [
        (_GREATER_THAN, [20], 20.0, "Unhealthy"),
        (_GREATER_THAN, [5], 5.0, "Degraded"),
        (_GREATER_THAN, [0], 0.0, "Healthy"),
        (_LESS_THAN, [50], 50.0, "Unhealthy"),
        (_LESS_THAN, [80], 80.0, "Degraded"),
        (_LESS_THAN, [100], 100.0, "Healthy"),
        (_LESS_THAN, [math.nan], None, "Error"),
        (_GREATER_THAN, [math.inf], None, "Error"),
    ],
)
def test_execute_signal_evaluates_rules(
    kind: str, owner: str, rules: dict[str, Any], values: list[float], expected_value: float | None, expected_health: str,
) -> None:
    result = _execute_signal_of_kind(kind, owner, rules, values)

    assert (result["rawValue"], result["healthState"]) == (expected_value, expected_health)
    assert (result["error"] is None) == (expected_value is not None)


@pytest.mark.parametrize(
    ("kind", "expected_health", "expected_message"),
    [
        ("PrometheusMetricsQuery", "Error", "no datapoints"),
        ("AzureResourceMetric", "Error", "no data points"),
        ("LogAnalyticsQuery", "Unknown", "Log query returned no rows"),
    ],
)
def test_execute_signal_empty_result(kind: str, expected_health: str, expected_message: str) -> None:
    result = _execute_signal_of_kind(kind, "instance", _LESS_THAN, [])

    assert (result["rawValue"], result["healthState"]) == (None, expected_health)
    assert expected_message in (result["error"] or result["evaluationWarning"])


@pytest.mark.parametrize("time_grain", ["PT1M", ""])
def test_execute_signal_azure_metric_requests_the_time_grain_the_editor_shows(time_grain: str) -> None:
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal

    client = MagicMock()
    client.get_entity.return_value = {
        "name": "entity-1",
        "properties": {"signalGroups": {"group": {"azureResourceId": _LA_WORKSPACE, "signals": [
            {"name": "sig-1", "signalKind": "AzureResourceMetric", "metricName": "Percentage CPU", "timeGrain": time_grain},
        ]}}},
    }
    client.query_azure_metric.return_value = {"value": []}

    execute_signal(client, "rg", "model", "entity-1", "sig-1")

    assert client.query_azure_metric.call_args.args[-1] == time_grain


def test_execute_signal_query_override_is_per_invocation_without_mutation():
    from azext_healthmodels_plus.tui.client.query_executor import execute_signal

    entity_payload = {
        "id": "/sub/x/entities/e1",
        "name": "entity-1",
        "properties": {
            "displayName": "Entity One",
            "signalGroups": {
                "azureMonitorWorkspace": {
                    "azureMonitorWorkspaceResourceId": "/sub/x/amw",
                    "signals": [{
                        "name": "sig-1",
                        "signalKind": "PrometheusMetricsQuery",
                        "queryText": "rate(original[5m])",
                        "signalDefinitionName": "sig-def-1",
                        "evaluationRules": {},
                    }],
                }
            },
        },
    }
    sig_def_payload = {
        "name": "sig-def-1",
        "properties": {
            "queryText": "rate(definition[5m])",
            "signalKind": "PrometheusMetricsQuery",
        },
    }
    client = MagicMock()

    client.get_entity.return_value = entity_payload
    client.get_signal_definition.return_value = sig_def_payload
    client.query_prometheus.return_value = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"value": [1234567890, "11.5"]}]},
    }

    result = execute_signal(
        client, "rg", "model", "entity-1", "sig-1",
        query_override="rate(edited[5m])",
    )

    assert result["query"] == "rate(edited[5m])"
    assert entity_payload["properties"]["signalGroups"]["azureMonitorWorkspace"]["signals"][0]["queryText"] == "rate(original[5m])"
    assert sig_def_payload["properties"]["queryText"] == "rate(definition[5m])"


# ─── Phase 6: Extractor debug logs ───────────────────────────────────

import logging


def test_prometheus_extractor_logs_on_malformed_data(caplog):
    from azext_healthmodels_plus.tui.client.query_executor import _extract_prometheus_value

    malformed = {"data": {"result": [{"value": "not-a-list"}]}}
    with caplog.at_level(logging.DEBUG, logger="azext_healthmodels_plus.tui.client.query_executor"):
        value, err = _extract_prometheus_value(malformed)

    assert value is None
    assert err is not None
    assert any("Failed to extract Prometheus" in r.message for r in caplog.records)


def test_azure_metric_extractor_logs_on_malformed_data(caplog):
    from azext_healthmodels_plus.tui.client.query_executor import _extract_azure_metric_value

    malformed = {"value": [{"timeseries": [{"data": "not-a-list"}]}]}
    with caplog.at_level(logging.DEBUG, logger="azext_healthmodels_plus.tui.client.query_executor"):
        value, err = _extract_azure_metric_value(malformed)

    assert value is None
    assert err is not None
    assert any("Failed to extract Azure metric" in r.message for r in caplog.records)
