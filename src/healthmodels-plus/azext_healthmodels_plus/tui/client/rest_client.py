"""REST client for the CloudHealth API — the single I/O boundary.

Wraps the ``azure-mgmt-cloudhealth`` SDK for typed CRUD operations and
falls back to ``send_raw_request`` for cross-service queries
(Prometheus and Azure Monitor metrics).

The public ``CloudHealthClient`` API returns plain ``dict`` / ``list[dict]``
values and translates SDK exceptions into the extension's typed errors.
"""
from __future__ import annotations

import logging
import re
import json
import urllib.parse
import threading
from datetime import datetime
from contextlib import contextmanager
from typing import Any, Callable, Final, Mapping

from azure.core.pipeline.transport import HttpRequest
from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

from azext_healthmodels_plus.tui.client.errors import (
    AuthenticationError,
    HealthModelError,
    ThrottledError,
    parse_arm_error,
)

_log = logging.getLogger(__name__)

API_VERSION: Final[str] = "2026-11-01"
PROVIDER: Final[str] = "Microsoft.CloudHealth"

# ARM resource IDs look like:
#   /subscriptions/<guid>/resourceGroups/<name>/providers/<ns>/<type>/<name>[/...]
_ARM_RESOURCE_ID_RE: Final = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}"
    r"(/resourceGroups/[^/?#\s]+"
    r"(/providers/[^/?#\s]+(/[^/?#\s]+/[^/?#\s]+)+)?)?$"
)


def _validate_arm_resource_id(resource_id: str) -> str:
    """Validate that ``resource_id`` is a well-formed ARM resource ID.

    Prevents URL-injection via malformed model data — e.g. a stray ``?`` or
    ``#`` in a resource ID could otherwise alter the request path/query
    when concatenated into a management URL.
    """
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError("resource id must be a non-empty string")
    if any(ch in resource_id for ch in ("?", "#", " ", "\n", "\r", "\t")):
        raise ValueError(
            f"invalid characters in ARM resource id: {resource_id!r}"
        )
    if not _ARM_RESOURCE_ID_RE.match(resource_id):
        raise ValueError(
            f"resource id does not match ARM pattern "
            f"'/subscriptions/<guid>/...': {resource_id!r}"
        )
    return resource_id


class CloudHealthClient:
    """Client for Microsoft.CloudHealth resources.

    Uses the ``azure-mgmt-cloudhealth`` SDK for typed operations and
    ``send_raw_request`` for queries the SDK doesn't expose
    (Prometheus, Azure Monitor metrics).
    """

    def __init__(self, cli_ctx: object, subscription_id: str) -> None:
        self._cli_ctx = cli_ctx
        self._subscription_id = subscription_id

        from azure.cli.core.commands.client_factory import get_mgmt_service_client
        from azure.mgmt.cloudhealth import CloudHealthMgmtClient

        self._sdk = get_mgmt_service_client(
            cli_ctx, CloudHealthMgmtClient, subscription_id=subscription_id,
            api_version=API_VERSION,
        )
        self._scopes = {subscription_id.casefold(): self}
        self._scope_lock = threading.Lock()
        self._connection_scope_cache: dict[str, dict[str, str]] = {}
        self._read_context = threading.local()
        self.arm_endpoint = (
            cli_ctx.cloud.endpoints.resource_manager.rstrip("/")
            if cli_ctx is not None else "https://management.azure.com"
        )

    @property
    def _sdk(self) -> Any:
        local = self._sdk_threads
        if not hasattr(local, "value"):
            from azure.cli.core.commands.client_factory import get_mgmt_service_client
            from azure.mgmt.cloudhealth import CloudHealthMgmtClient

            local.value = get_mgmt_service_client(
                self._cli_ctx, CloudHealthMgmtClient, subscription_id=self._subscription_id,
                api_version=API_VERSION,
            )
        return local.value

    @_sdk.setter
    def _sdk(self, value: Any) -> None:
        # RequestsTransport owns a Session and is explicitly not thread-safe.
        if not hasattr(self, "_sdk_threads"):
            self._sdk_threads = threading.local()
        self._sdk_threads.value = value

    def model_key(self, group: str, model: str) -> ModelKey:
        return ModelKey(self.arm_endpoint, self._subscription_id, group, model)

    def for_model(self, target: ModelKey) -> CloudHealthClient:
        """Reuse the CLI credential factory with an explicit target subscription."""
        if target.canonical[0] != self.arm_endpoint.casefold():
            raise ValueError("Nested model uses a different ARM endpoint")
        with self._scope_lock:
            scope = target.subscription.casefold()
            if scope not in self._scopes:
                scoped = CloudHealthClient(self._cli_ctx, target.subscription)
                scoped._scopes = self._scopes
                scoped._scope_lock = self._scope_lock
                self._scopes[scope] = scoped
            return self._scopes[scope]

    def get_connection_scope(self) -> dict[str, str]:
        """Resolve and cache the active subscription/tenant scope from Azure CLI profile."""
        scope = self._subscription_id.casefold()
        with self._scope_lock:
            cached = self._connection_scope_cache.get(scope)
            if cached is not None:
                return dict(cached)

        from azure.cli.core._profile import Profile

        profile = Profile(cli_ctx=self._cli_ctx)
        subscription = profile.get_subscription(subscription=self._subscription_id)
        if not isinstance(subscription, Mapping):
            raise ValueError("Profile subscription payload must be an object")

        resolved_subscription_id = str(subscription.get("id", "")).strip()
        subscription_name = str(subscription.get("name", "")).strip()
        tenant_id = str(subscription.get("tenantId", "")).strip()
        tenant_name_raw = subscription.get("tenantDisplayName", subscription.get("tenantName"))
        tenant_name = str(tenant_name_raw).strip() if tenant_name_raw is not None else ""
        arm_endpoint = self.arm_endpoint.rstrip("/")

        if not resolved_subscription_id or not subscription_name or not tenant_id or not arm_endpoint:
            raise ValueError("Connected scope is incomplete in Azure CLI profile")
        if resolved_subscription_id.casefold() != self._subscription_id.casefold():
            raise ValueError("Azure CLI profile returned a different subscription than the scoped client")

        value = {
            "arm_endpoint": arm_endpoint,
            "subscription_id": resolved_subscription_id,
            "subscription_name": subscription_name,
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
        }
        with self._scope_lock:
            self._connection_scope_cache[scope] = value
        return dict(value)

    @contextmanager
    def monitor_reads(self, stopped: threading.Event):
        """Stop application paging between responses without cancelling SDK I/O."""
        self._read_context.stopped = stopped
        try:
            yield
        finally:
            del self._read_context.stopped

    def _check_read(self) -> None:
        context = getattr(self, "_read_context", None)
        stopped = getattr(context, "stopped", None)
        if stopped is not None and stopped.is_set():
            raise InterruptedError("Monitor stopped")

    # ─── SDK bridges (DRY exception translation) ──────────────────────

    @staticmethod
    def _to_dict(result: Any) -> dict[str, Any]:
        if result is None:
            return {}
        if hasattr(result, "as_dict"):
            return result.as_dict()
        if isinstance(result, dict):
            return result
        raise TypeError(
            f"Cannot convert {type(result).__name__} to dict"
        )

    def _call(self, fn: Callable[[], Any]) -> dict[str, Any]:
        """Execute a synchronous SDK call and convert the result to a dict."""
        try:
            return self._to_dict(fn())
        except Exception as e:  # noqa: BLE001 — translated below
            self._raise_translated(e)

    def _call_list(self, fn: Callable[[], Any]) -> list[dict[str, Any]]:
        """Execute an SDK paged list call and materialize into ``list[dict]``."""
        try:
            self._check_read()
            from azure.core.paging import ItemPaged

            paged = fn()
            iterator = iter(paged.by_page()) if isinstance(paged, ItemPaged) else iter([paged])
            result = []
            while True:
                self._check_read()
                try:
                    page = next(iterator)
                except StopIteration:
                    return result
                self._check_read()
                result.extend(self._to_dict(item) for item in page)
        except Exception as e:  # noqa: BLE001
            self._raise_translated(e)

    @staticmethod
    def _raise_translated(e: Exception) -> None:
        """Translate an SDK exception into a typed ``HealthModelError`` and raise."""
        from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

        if isinstance(e, (HealthModelError,)):
            raise e

        # Helper to extract ARM code/details from any HttpResponseError
        def _extract_arm_diag(exc: Any) -> tuple[str, list[dict[str, str]]]:
            code = ""
            details: list[dict[str, str]] = []
            if getattr(exc, "error", None) is not None:
                code = str(getattr(exc.error, "code", "") or "")
                raw_details = getattr(exc.error, "details", None)
                if raw_details:
                    try:
                        details = [
                            {"code": str(getattr(d, "code", "")), "message": str(getattr(d, "message", ""))}
                            for d in raw_details
                        ]
                    except (TypeError, AttributeError):
                        pass
            return code, details

        if isinstance(e, ClientAuthenticationError):
            code, details = _extract_arm_diag(e)
            raise AuthenticationError(
                str(e), status_code=getattr(e, "status_code", 403) or 403,
                code=code, details=details,
            ) from e

        if isinstance(e, HttpResponseError):
            status = e.status_code or 0
            if status == 429:
                retry_after = 1.0
                if e.response is not None:
                    try:
                        retry_after = float(
                            e.response.headers.get("Retry-After", 1)
                        )
                    except (TypeError, ValueError):
                        retry_after = 1.0
                code, details = _extract_arm_diag(e)
                raise ThrottledError(
                    str(e), status_code=429, retry_after=retry_after,
                    code=code, details=details,
                ) from e

            body: dict[str, Any]
            if hasattr(e, "model") and e.model is not None and hasattr(e.model, "as_dict"):
                body = e.model.as_dict()
            else:
                code, details_list = _extract_arm_diag(e)
                body = {"error": {"code": code, "message": str(e), "details": details_list}}
            raise parse_arm_error(body, status) from e

        raise

    # ─── Named sub-resource reads ─────────────────────────────────────

    def get_entity(
        self,
        resource_group: str,
        model_name: str,
        entity_name: str,
    ) -> dict[str, Any]:
        return self._call(
            lambda: self._sdk.entities.get(resource_group, model_name, entity_name)
        )

    def get_signal_definition(
        self,
        resource_group: str,
        model_name: str,
        signal_definition_name: str,
    ) -> dict[str, Any]:
        return self._call(
            lambda: self._sdk.signal_definitions.get(
                resource_group, model_name, signal_definition_name
            )
        )

    def get_relationship(
        self,
        resource_group: str,
        model_name: str,
        relationship_name: str,
    ) -> dict[str, Any]:
        return self._call(
            lambda: self._sdk.relationships.get(
                resource_group, model_name, relationship_name
            )
        )

    @staticmethod
    def _at(timestamp: datetime | None) -> dict[str, datetime]:
        # Live reads must not send a timestamp kwarg at all.
        return {} if timestamp is None else {"timestamp": timestamp}

    def list_entities(
        self,
        resource_group: str,
        model_name: str,
        *,
        timestamp: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._call_list(
            lambda: self._sdk.entities.list_by_health_model(resource_group, model_name, **self._at(timestamp))
        )

    def list_relationships(
        self,
        resource_group: str,
        model_name: str,
        *,
        timestamp: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._call_list(
            lambda: self._sdk.relationships.list_by_health_model(resource_group, model_name, **self._at(timestamp))
        )

    def list_signal_definitions(
        self,
        resource_group: str,
        model_name: str,
        *,
        timestamp: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return self._call_list(
            lambda: self._sdk.signal_definitions.list_by_health_model(
                resource_group, model_name, **self._at(timestamp)
            )
        )

    def list_health_models(
        self,
        resource_group: str | None = None,
    ) -> list[dict[str, Any]]:
        if resource_group:
            return self._call_list(
                lambda: self._sdk.health_models.list_by_resource_group(resource_group)
            )
        return self._call_list(lambda: self._sdk.health_models.list_by_subscription())

    def update_entity(
        self,
        resource_group: str,
        model_name: str,
        entity_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._put_named_resource(
            resource_group=resource_group,
            model_name=model_name,
            collection="entities",
            resource_name=entity_name,
            payload=payload,
        )

    def update_signal_definition(
        self,
        resource_group: str,
        model_name: str,
        signal_definition_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._put_named_resource(
            resource_group=resource_group,
            model_name=model_name,
            collection="signaldefinitions",
            resource_name=signal_definition_name,
            payload=payload,
        )

    def update_relationship(
        self,
        resource_group: str,
        model_name: str,
        relationship_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._put_named_resource(
            resource_group=resource_group,
            model_name=model_name,
            collection="relationships",
            resource_name=relationship_name,
            payload=payload,
        )

    # ─── Entity signal operations (history) ───────────────────────────

    def get_signal_history(
        self,
        resource_group: str,
        model_name: str,
        entity_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Get full signal history for an entity, including continuation pages."""
        def next_body(previous: dict[str, Any], page: dict[str, Any], marker: str) -> dict[str, Any]:
            signal_name = previous.get("signalName") or page.get("signalName")
            if not signal_name:
                raise ValueError("signal history continuation requires signalName")
            return {"signalName": str(signal_name), "nextMarker": marker}

        return self._paged_history(
            "Signal", "getSignalHistory", resource_group, model_name, entity_name, body, next_body,
        )

    def get_entity_history(
        self,
        resource_group: str,
        model_name: str,
        entity_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Get full entity history, including continuation pages."""
        def next_body(_previous: dict[str, Any], _page: dict[str, Any], marker: str) -> dict[str, Any]:
            return {"nextMarker": marker, **({"top": body["top"]} if "top" in body else {})}

        return self._paged_history(
            "Entity", "getHistory", resource_group, model_name, entity_name, body, next_body,
        )

    def _paged_history(
        self,
        label: str,
        action: str,
        resource_group: str,
        model_name: str,
        entity_name: str,
        body: dict[str, Any],
        next_body: Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]],
    ) -> dict[str, Any]:
        # The generated SDK operation gates stable history behind a version check, so
        # POST through send_request to stay on the SDK auth/retry pipeline.
        url = self._model_url(resource_group, model_name, "entities", entity_name, action)
        request_body = dict(body)
        merged_history: list[Any] = []
        base_result: dict[str, Any] = {}
        pages = 0
        while True:
            try:
                self._check_read()
                response = self._send("POST", url, request_body)
                self._raise_for_status(response)
                page = self._response_json_dict(response, f"{label} history response")
            except Exception as exc:  # noqa: BLE001
                if not pages:
                    raise
                raise RuntimeError(
                    f"{label} history is incomplete because continuation failed after {len(merged_history)} points: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            pages += 1
            if not base_result:
                base_result = {k: v for k, v in page.items() if k not in {"history", "nextMarker"}}
            merged_history.extend(page.get("history") or [])
            marker = page.get("nextMarker")
            if not marker:
                base_result["history"] = merged_history
                return base_result
            request_body = next_body(request_body, page, marker)

    def _put_named_resource(
        self,
        *,
        resource_group: str,
        model_name: str,
        collection: str,
        resource_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Stable PUT for named CloudHealth resources using SDK send_request pipeline."""
        response = self._send("PUT", self._model_url(resource_group, model_name, collection, resource_name), payload)
        if response.status_code == 202:
            raise RuntimeError(
                "Named update is asynchronous (HTTP 202). "
                "This client requires a completed 200/201 response before success."
            )
        self._raise_for_status(response)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Unexpected status code from named update: {response.status_code}"
            )
        return self._response_json_dict(response, "Named update response")

    def _model_url(
        self, resource_group: str, model_name: str, collection: str, name: str, action: str = "",
    ) -> str:
        def quote(value: str) -> str:
            return urllib.parse.quote(value, safe="")

        suffix = f"/{action}" if action else ""
        return (
            f"{self.arm_endpoint.rstrip('/')}/subscriptions/{quote(self._subscription_id)}"
            f"/resourceGroups/{quote(resource_group)}/providers/{PROVIDER}/healthmodels/{quote(model_name)}"
            f"/{collection}/{quote(name)}{suffix}?api-version={API_VERSION}"
        )

    def _send(self, method: str, url: str, body: Mapping[str, Any]) -> Any:
        request = HttpRequest(method, url, headers={"Content-Type": "application/json"})
        request.set_json_body(dict(body))
        try:
            return self._sdk.send_request(request)
        except Exception as exc:  # noqa: BLE001
            self._raise_translated(exc)
            raise

    def _raise_for_status(self, response: Any) -> None:
        try:
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._raise_translated(exc)
            raise

    @staticmethod
    def _response_json_dict(response: Any, context: str) -> dict[str, Any]:
        payload: Any
        if hasattr(response, "json"):
            payload = response.json()
        else:
            if hasattr(response, "text"):
                raw_text = response.text()
            else:
                raw_body = response.body()
                if isinstance(raw_body, bytes):
                    raw_text = raw_body.decode("utf-8")
                else:
                    raw_text = str(raw_body)
            if not raw_text:
                payload = {}
            else:
                payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise TypeError(f"{context} must be an object")
        return payload

    # ─── Cross-service queries (NOT CloudHealth SDK — different Azure services) ─

    def query_prometheus(
        self,
        workspace_resource_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Resolve a Monitor workspace's Prometheus endpoint and execute an instant PromQL query."""
        _validate_arm_resource_id(workspace_resource_id)

        ws_url = (
            f"{self.arm_endpoint.rstrip('/')}{workspace_resource_id}"
            f"?api-version=2025-10-03"
        )
        ws_data = self._raw_get(ws_url)
        endpoint = (
            ws_data.get("properties", {})
            .get("metrics", {})
            .get("prometheusQueryEndpoint")
        )
        if not endpoint:
            raise ValueError(
                f"No prometheusQueryEndpoint on workspace {workspace_resource_id}"
            )

        encoded_query = urllib.parse.quote(query, safe="")
        prom_url = f"{endpoint}/api/v1/query?query={encoded_query}"
        return self._raw_get(prom_url, resource="https://prometheus.monitor.azure.com")

    def query_azure_metric(
        self,
        resource_id: str,
        metric_name: str,
        metric_namespace: str,
        aggregation: str,
        time_grain: str,
    ) -> dict[str, Any]:
        """Execute an Azure Resource Metrics query via ARM."""
        _validate_arm_resource_id(resource_id)

        url = (
            f"{self.arm_endpoint.rstrip('/')}{resource_id}"
            f"/providers/Microsoft.Insights/metrics"
            f"?api-version=2024-02-01"
            f"&metricnames={urllib.parse.quote(metric_name)}"
            f"&metricNamespace={urllib.parse.quote(metric_namespace)}"
            f"&aggregation={urllib.parse.quote(aggregation)}"
            + (f"&interval={urllib.parse.quote(time_grain)}" if time_grain else "")  # ARM picks one when omitted
        )
        return self._raw_get(url)

    def query_log_analytics(self, workspace_resource_id: str, query: str, timespan: str) -> dict[str, Any]:
        """Execute KQL through the ARM workspace proxy, so the CLI's cloud and management credential apply."""
        _validate_arm_resource_id(workspace_resource_id)

        url = f"{self.arm_endpoint.rstrip('/')}{workspace_resource_id}/api/query?api-version=2020-08-01"
        # Without a timeGrain the service sends no timespan; the query defines its own range.
        body = {"query": query, **({"timespan": timespan} if timespan else {})}
        response = self._send("POST", url, body)
        self._raise_for_status(response)
        return self._response_json_dict(response, "Log Analytics query response")

    def _raw_get(self, url: str, **kwargs: Any) -> Any:
        """GET through the CLI's raw request; HTTP failures become the extension's typed errors."""
        from azure.cli.core.azclierror import HTTPError
        from azure.cli.core.util import send_raw_request

        try:
            return send_raw_request(self._cli_ctx, "GET", url, **kwargs).json()
        except HTTPError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                body = {"error": {"message": exc.response.text}}
            raise parse_arm_error(body, exc.response.status_code) from exc
