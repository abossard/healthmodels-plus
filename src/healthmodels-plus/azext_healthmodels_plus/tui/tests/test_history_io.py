from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpRequest, HttpResponse, HttpTransport

from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient


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


class _NoJsonResponse(HttpResponse):
    def __init__(
        self, request: HttpRequest, status_code: int, payload: dict[str, Any] | list[Any],
    ) -> None:
        super().__init__(request=request, internal_response=None)
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(payload).encode("utf-8")

    def body(self) -> bytes:
        return self._body

    def text(self, encoding: str | None = None) -> str:
        return self._body.decode(encoding or "utf-8")

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


class _NoJsonTransport(_RecordingTransport):
    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        self.requests.append(request)
        if not self._queued:
            raise AssertionError("unexpected extra HTTP request")
        item = self._queued.pop(0)
        return _NoJsonResponse(request, item.status_code, item.payload)


class _StopAfterFirstResponseTransport(_RecordingTransport):
    def __init__(self, queued: list[_QueuedHttp], stop: threading.Event) -> None:
        super().__init__(queued)
        self._stop = stop

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        response = super().send(request, **kwargs)
        if len(self.requests) == 1:
            self._stop.set()
        return response


def _real_sdk_client_with_transport(
    transport: _RecordingTransport,
    *,
    api_version: str = "2026-11-01",
    arm_endpoint: str = "https://management.azure.com",
) -> CloudHealthClient:
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
    client._read_context = threading.local()
    client.arm_endpoint = arm_endpoint
    return client


@pytest.fixture
def mock_sdk_client():
    sdk = MagicMock()
    client = object.__new__(CloudHealthClient)
    client._cli_ctx = MagicMock()
    client._subscription_id = "test-sub"
    client._sdk = sdk
    client.arm_endpoint = "https://management.azure.com"
    return client, sdk


@pytest.mark.parametrize(
    ("method_name", "sdk_call"),
    [
        ("list_entities", "entities.list_by_health_model"),
        ("list_relationships", "relationships.list_by_health_model"),
        ("list_signal_definitions", "signal_definitions.list_by_health_model"),
    ],
)
def test_list_wrappers_preserve_no_timestamp_calls(
    mock_sdk_client,
    method_name: str,
    sdk_call: str,
) -> None:
    client, sdk = mock_sdk_client
    endpoint = sdk
    for attr in sdk_call.split("."):
        endpoint = getattr(endpoint, attr)
    endpoint.return_value = [{"name": "x"}]

    result = getattr(client, method_name)("rg", "model")

    assert result == [{"name": "x"}]
    endpoint.assert_called_once_with("rg", "model")


@pytest.mark.parametrize(
    ("method_name", "sdk_call"),
    [
        ("list_entities", "entities.list_by_health_model"),
        ("list_relationships", "relationships.list_by_health_model"),
        ("list_signal_definitions", "signal_definitions.list_by_health_model"),
    ],
)
def test_list_wrappers_pass_timestamp_when_requested(
    mock_sdk_client,
    method_name: str,
    sdk_call: str,
) -> None:
    client, sdk = mock_sdk_client
    endpoint = sdk
    for attr in sdk_call.split("."):
        endpoint = getattr(endpoint, attr)
    endpoint.return_value = [{"name": "x"}]
    at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    result = getattr(client, method_name)("rg", "model", timestamp=at)

    assert result == [{"name": "x"}]
    endpoint.assert_called_once_with("rg", "model", timestamp=at)


@pytest.mark.parametrize(
    ("reader", "path_fragment"),
    [
        ("list_entities", "/entities?"),
        ("list_relationships", "/relationships?"),
        ("list_signal_definitions", "/signaldefinitions?"),
    ],
)
def test_timestamp_is_emitted_on_all_three_history_scope_reads(
    reader: str,
    path_fragment: str,
) -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {"value": [{"name": "x"}]}),
    ])
    client = _real_sdk_client_with_transport(transport)
    at = datetime(2026, 10, 17, 22, 23, 8, tzinfo=timezone.utc)

    getattr(client, reader)("rg", "model", timestamp=at)

    assert len(transport.requests) == 1
    request_url = transport.requests[0].url
    assert path_fragment in request_url
    assert "api-version=2026-11-01" in request_url
    assert "timestamp=" in request_url
    assert re.search(
        r"timestamp=2026-10-17T22%3A23%3A08(?:\.000)?(?:Z|%2B00%3A00)",
        request_url,
    )


def test_get_entity_history_paginates_next_marker(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    first_page = {
        "entityName": "e1",
        "history": [{"newState": "Healthy"}],
        "nextMarker": "m1",
    }
    second_page = {
        "entityName": "e1",
        "history": [{"newState": "Degraded"}],
    }
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=first_page)),
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=second_page)),
    ]

    result = client.get_entity_history(
        "rg", "m", "e1",
        {"startAt": "2025-01-01T00:00:00Z", "endAt": "2025-01-01T01:00:00Z"},
    )

    assert sdk.send_request.call_count == 2
    first_request = sdk.send_request.mock_calls[0].args[0]
    second_request = sdk.send_request.mock_calls[1].args[0]
    assert "/getHistory?api-version=2026-11-01" in first_request.url
    assert "/getHistory?api-version=2026-11-01" in second_request.url
    assert json.loads(first_request.body) == {
        "startAt": "2025-01-01T00:00:00Z",
        "endAt": "2025-01-01T01:00:00Z",
    }
    assert json.loads(second_request.body) == {"nextMarker": "m1"}
    assert [point["newState"] for point in result["history"]] == ["Healthy", "Degraded"]
    assert "nextMarker" not in result


def test_get_entity_history_continuation_preserves_top_when_provided(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={
            "entityName": "e1",
            "history": [{"newState": "Healthy"}],
            "nextMarker": "m1",
        })),
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={
            "entityName": "e1",
            "history": [{"newState": "Degraded"}],
        })),
    ]

    client.get_entity_history(
        "rg", "m", "e1",
        {"startAt": "2025-01-01T00:00:00Z", "endAt": "2025-01-01T01:00:00Z", "top": 100},
    )

    second_request = sdk.send_request.mock_calls[1].args[0]
    assert json.loads(second_request.body) == {"nextMarker": "m1", "top": 100}


def test_get_entity_history_raises_when_later_page_fails(
    mock_sdk_client,
) -> None:
    client, sdk = mock_sdk_client
    sdk.send_request.side_effect = [
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={
            "entityName": "e1",
            "history": [{"newState": "Healthy"}],
            "nextMarker": "m1",
        })),
        RuntimeError("page 2 down"),
    ]

    with pytest.raises(RuntimeError, match="Entity history is incomplete because continuation failed"):
        client.get_entity_history(
            "rg", "m", "e1",
            {"startAt": "2025-01-01T00:00:00Z", "endAt": "2025-01-01T01:00:00Z"},
        )


@pytest.mark.parametrize(
    ("writer", "path_fragment"),
    [
        ("update_entity", "/entities/entity-1?"),
        ("update_signal_definition", "/signaldefinitions/sig-1?"),
        ("update_relationship", "/relationships/rel-1?"),
    ],
)
def test_named_updates_use_stable_put_path_and_payload(
    writer: str,
    path_fragment: str,
) -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {"name": "ok"}),
    ])
    client = _real_sdk_client_with_transport(transport)

    payload = {"name": "n1", "properties": {"displayName": "x"}}
    if writer == "update_entity":
        result = client.update_entity("rg", "model", "entity-1", payload)
    elif writer == "update_signal_definition":
        result = client.update_signal_definition("rg", "model", "sig-1", payload)
    else:
        result = client.update_relationship("rg", "model", "rel-1", payload)

    assert result == {"name": "ok"}
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "PUT"
    assert path_fragment in req.url
    assert "api-version=2026-11-01" in req.url
    assert json.loads(req.body) == payload


@pytest.mark.parametrize(
    ("writer", "path_fragment"),
    [
        ("update_entity", "/entities/entity-1?"),
        ("update_signal_definition", "/signaldefinitions/sig-1?"),
        ("update_relationship", "/relationships/rel-1?"),
    ],
)
def test_named_updates_accept_created_201(
    writer: str,
    path_fragment: str,
) -> None:
    transport = _RecordingTransport([
        _QueuedHttp(201, {"name": "created"}),
    ])
    client = _real_sdk_client_with_transport(transport)

    payload = {"name": "n1", "properties": {"displayName": "x"}}
    if writer == "update_entity":
        result = client.update_entity("rg", "model", "entity-1", payload)
    elif writer == "update_signal_definition":
        result = client.update_signal_definition("rg", "model", "sig-1", payload)
    else:
        result = client.update_relationship("rg", "model", "rel-1", payload)

    assert result == {"name": "created"}
    assert path_fragment in transport.requests[0].url


@pytest.mark.parametrize(
    ("writer", "resource_name"),
    [
        ("update_entity", "entity-1"),
        ("update_signal_definition", "sig-1"),
        ("update_relationship", "rel-1"),
    ],
)
def test_named_updates_refuse_202_async_success(
    writer: str,
    resource_name: str,
) -> None:
    transport = _RecordingTransport([
        _QueuedHttp(202, {"status": "Accepted"}),
    ])
    client = _real_sdk_client_with_transport(transport)

    payload = {"name": "n1", "properties": {"displayName": "x"}}
    with pytest.raises(RuntimeError, match="asynchronous"):
        if writer == "update_entity":
            client.update_entity("rg", "model", resource_name, payload)
        elif writer == "update_signal_definition":
            client.update_signal_definition("rg", "model", resource_name, payload)
        else:
            client.update_relationship("rg", "model", resource_name, payload)


def test_named_update_and_history_parse_http_response_without_json_method() -> None:
    transport = _NoJsonTransport([
        _QueuedHttp(200, {"name": "updated"}),
        _QueuedHttp(200, {
            "entityName": "e1",
            "signalName": "sig-1",
            "history": [],
        }),
    ])
    client = _real_sdk_client_with_transport(transport)

    update_result = client.update_entity(
        "rg", "model", "entity-1", {"name": "entity-1", "properties": {"displayName": "x"}},
    )
    history_result = client.get_signal_history(
        "rg", "model", "entity-1",
        {"signalName": "sig-1", "startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
    )

    assert update_result == {"name": "updated"}
    assert history_result["entityName"] == "e1"
    assert history_result["history"] == []


@pytest.mark.parametrize(
    ("reader", "body"),
    [
        (
            "get_entity_history",
            {"startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
        ),
        (
            "get_signal_history",
            {"signalName": "sig-1", "startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
        ),
    ],
)
def test_history_continuation_stops_before_page2_when_monitor_stops(
    reader: str,
    body: dict[str, Any],
) -> None:
    stop = threading.Event()
    first_page = {"history": [{"x": 1}], "nextMarker": "next"}
    if reader == "get_signal_history":
        first_page["signalName"] = "sig-1"
    transport = _StopAfterFirstResponseTransport(
        [
            _QueuedHttp(200, first_page),
            _QueuedHttp(200, {"history": [{"x": 2}]}),
        ],
        stop,
    )
    client = _real_sdk_client_with_transport(transport)

    with pytest.raises(RuntimeError, match="incomplete"):
        with client.monitor_reads(stop):
            getattr(client, reader)("rg", "model", "entity-1", body)

    assert len(transport.requests) == 1


def test_named_put_and_entity_history_use_scoped_arm_endpoint() -> None:
    transport = _RecordingTransport([
        _QueuedHttp(200, {"name": "updated"}),
        _QueuedHttp(200, {"entityName": "e1", "history": []}),
    ])
    scoped_endpoint = "https://management.usgovcloudapi.net"
    client = _real_sdk_client_with_transport(transport, arm_endpoint=scoped_endpoint)

    client.update_entity(
        "rg", "model", "entity-1", {"name": "entity-1", "properties": {"displayName": "x"}},
    )
    client.get_entity_history(
        "rg", "model", "entity-1",
        {"startAt": "2026-01-01T00:00:00Z", "endAt": "2026-01-01T01:00:00Z"},
    )

    assert len(transport.requests) == 2
    for request in transport.requests:
        assert request.url.startswith(f"{scoped_endpoint}/subscriptions/")
        assert not request.url.startswith("https://management.azure.com/subscriptions/")
