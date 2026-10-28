"""Execute signal queries against real data sources.

Resolves the full chain: entity → signal instance → signal group
(data source) → signal definition (query/rules), executes the query
through the :class:`CloudHealthClient` abstraction, extracts the value,
evaluates health using domain-model rules, and returns a structured
result.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from azext_healthmodels_plus.tui.client.errors import (
    AuthenticationError,
    HealthModelError,
    ThrottledError,
)
from azext_healthmodels_plus.tui.client.rest_client import CloudHealthClient
from azext_healthmodels_plus.tui.models.domain import EvaluationRule
from azext_healthmodels_plus.tui.models.enums import ComparisonOperator, HealthState

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalExecutionConfig:
    entity: dict[str, Any]
    signal_instance: dict[str, Any]
    signal_definition: dict[str, Any] | None
    entity_display: str
    signal_definition_name: str
    signal_name: str
    signal_display_name: str
    signal_kind: str
    signal_group: str
    query_text: str
    time_grain: str
    value_column_name: str
    data_unit: str
    evaluation_rules: dict[str, Any]
    metric_namespace: str
    metric_name: str
    aggregation: str
    data_source: str


def resolve_signal_config(
    client: CloudHealthClient,
    resource_group: str,
    model_name: str,
    entity_name: str,
    signal_name: str,
) -> SignalExecutionConfig:
    entity = client.get_entity(resource_group, model_name, entity_name)
    entity_props = entity.get("properties", {})
    entity_display = entity_props.get("displayName", entity_name)
    sig_instance, group_key, group_data = _find_signal_on_entity(entity_props, signal_name)

    sig_def_name = sig_instance.get("signalDefinitionName", "")
    sig_def: dict[str, Any] | None = None
    sig_def_props: dict[str, Any] = {}
    if sig_def_name:
        sig_def = client.get_signal_definition(resource_group, model_name, sig_def_name)
        sig_def_props = sig_def.get("properties", {})

    signal_kind = sig_instance.get("signalKind") or sig_def_props.get("signalKind", "")
    signal_display_name = sig_instance.get("displayName") or sig_def_props.get("displayName", signal_name)
    query_text = sig_instance.get("queryText") or sig_def_props.get("queryText", "")
    data_source = (
        group_data.get("azureMonitorWorkspaceResourceId")
        or group_data.get("azureResourceId")
        or group_data.get("logAnalyticsWorkspaceResourceId", "")
    )

    return SignalExecutionConfig(
        entity=entity,
        signal_instance=dict(sig_instance),
        signal_definition=dict(sig_def) if isinstance(sig_def, dict) else None,
        entity_display=entity_display,
        signal_definition_name=sig_def_name,
        signal_name=signal_name,
        signal_display_name=signal_display_name,
        signal_kind=signal_kind,
        signal_group=group_key,
        query_text=query_text,
        time_grain=sig_instance.get("timeGrain") or sig_def_props.get("timeGrain", ""),
        value_column_name=sig_instance.get("valueColumnName") or sig_def_props.get("valueColumnName", ""),
        data_unit=sig_instance.get("dataUnit") or sig_def_props.get("dataUnit", "Count"),
        evaluation_rules=sig_instance.get("evaluationRules") or sig_def_props.get("evaluationRules", {}),
        metric_namespace=sig_instance.get("metricNamespace") or sig_def_props.get("metricNamespace", ""),
        metric_name=sig_instance.get("metricName") or sig_def_props.get("metricName", ""),
        aggregation=sig_instance.get("aggregationType") or sig_def_props.get("aggregationType", "Average"),
        data_source=data_source,
    )


def execute_signal(
    client: CloudHealthClient,
    resource_group: str,
    model_name: str,
    entity_name: str,
    signal_name: str,
    query_override: str | None = None,
) -> dict[str, Any]:
    """Execute a signal's query and return the result with full metadata.

    Raises ValueError for resolution failures. Query errors are returned
    in the result dict (error field) rather than raised.
    """
    t0 = time.monotonic()

    config = resolve_signal_config(
        client,
        resource_group,
        model_name,
        entity_name,
        signal_name,
    )
    query_text = config.query_text if query_override is None else query_override

    # 3. Execute the query
    raw_output: Any = None
    raw_value: float | None = None
    error: str | None = None
    error_type: str | None = None
    error_status_code: int | None = None
    error_code: str = ""
    evaluation_warning: str | None = None

    try:
        if config.signal_kind == "PrometheusMetricsQuery":
            workspace_id = config.data_source
            if not workspace_id:
                raise ValueError(f"Signal group '{config.signal_group}' has no azureMonitorWorkspaceResourceId")
            raw_output = client.query_prometheus(workspace_id, query_text)
            raw_value, error = _extract_prometheus_value(raw_output)

        elif config.signal_kind == "AzureResourceMetric":
            resource_id = config.data_source
            if not resource_id:
                raise ValueError(f"Signal group '{config.signal_group}' has no azureResourceId")
            raw_output = client.query_azure_metric(
                resource_id, config.metric_name, config.metric_namespace, config.aggregation, config.time_grain,
            )
            raw_value, error = _extract_azure_metric_value(raw_output)

        elif config.signal_kind == "LogAnalyticsQuery":
            workspace_id = config.data_source
            if not workspace_id:
                raise ValueError(f"Signal group '{config.signal_group}' has no logAnalyticsWorkspaceResourceId")
            raw_output = client.query_log_analytics(workspace_id, query_text, config.time_grain)
            raw_value, error = _extract_log_analytics_value(raw_output, config.value_column_name)
            if raw_value is None and error is None:
                evaluation_warning = "Log query returned no rows"  # the service's wording; it reports Unknown

        else:
            error = f"Unsupported signal kind: {config.signal_kind}"

    except (AuthenticationError, ThrottledError):
        # Operational failures (auth/throttling) must surface to the caller
        # rather than be hidden inside a structured "error" field.
        raise
    except (HealthModelError, ValueError, KeyError, TypeError) as exc:
        # Query-side failures (other typed errors, bad PromQL, malformed data) stay on the result, per signal.
        error = f"{type(exc).__name__}: {exc}"
        error_type = type(exc).__name__
        error_status_code = getattr(exc, "status_code", None)
        error_code = getattr(exc, "code", "")
    if raw_value is not None and not math.isfinite(raw_value):
        raw_value, error = None, f"value {raw_value} is not finite"

    # 4. Evaluate health (delegate to domain rule objects)
    health_state = HealthState.UNKNOWN
    if error:
        # Preserve the existing "Error" label for query failures.
        health_state_str = "Error"
    elif raw_value is not None:
        if _has_dynamic_rules(config.evaluation_rules):
            health_state = HealthState.UNKNOWN
            evaluation_warning = (
                "Dynamic evaluation rules are service-evaluated; local verification is unsupported."
            )
        else:
            health_state = _evaluate_health(raw_value, config.evaluation_rules)
        health_state_str = health_state.value
    else:
        health_state_str = health_state.value

    duration_ms = int((time.monotonic() - t0) * 1000)

    return {
        "signalDefinitionId": config.signal_definition_name,
        "signalDefinitionName": config.signal_display_name,
        "signalName": signal_name,
        "entityId": config.entity.get("id", ""),
        "entityName": config.entity_display,
        "signalKind": config.signal_kind,
        "signalGroup": config.signal_group,
        "query": query_text or f"{config.metric_namespace}/{config.metric_name}",
        "dataSource": config.data_source,
        "dataUnit": config.data_unit,
        "rawValue": raw_value,
        "healthState": health_state_str,
        "evaluationRules": config.evaluation_rules,
        "rawOutput": raw_output,
        "durationMs": duration_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "errorType": error_type if error else None,
        "errorStatusCode": error_status_code if error else None,
        "errorCode": error_code if error else "",
        "evaluationWarning": evaluation_warning,
    }


# ── Internal helpers ──────────────────────────────────────────────────


def _find_signal_on_entity(
    entity_props: dict[str, Any],
    signal_name: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Find a signal instance by name across all signal groups.

    Returns (signal_instance, group_key, group_data).
    Raises ValueError if not found.
    """
    signal_groups = entity_props.get("signalGroups", {})
    for group_key, group_data in signal_groups.items():
        if not isinstance(group_data, dict):
            continue
        for sig in group_data.get("signals", []):
            if sig.get("name") == signal_name:
                return sig, group_key, group_data
    raise ValueError(
        f"Signal '{signal_name}' not found on entity. "
        f"Available groups: {list(signal_groups.keys())}"
    )


def _extract_prometheus_value(data: dict[str, Any]) -> tuple[float | None, str | None]:
    """Extract the numeric value from a Prometheus query response.

    Returns ``(value, error_message)``. ``value`` is None when extraction
    fails or no datapoint is present; in that case ``error_message`` is a
    short, user-facing description of the problem.
    """
    try:
        results = data.get("data", {}).get("result", [])
        if not results:
            return None, "no datapoints returned by Prometheus query"
        first = results[0]
        value = first.get("value", [])
        if len(value) >= 2 and value[1] is not None:
            return float(value[1]), None
        return None, "Prometheus result missing value field"
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        _log.debug("Failed to extract Prometheus value: %s: %s", type(exc).__name__, exc)
        return None, f"malformed Prometheus response ({type(exc).__name__})"


def _extract_azure_metric_value(data: dict[str, Any]) -> tuple[float | None, str | None]:
    """Extract the latest value from an Azure Metrics response.

    Returns ``(value, error_message)``. See :func:`_extract_prometheus_value`.
    """
    try:
        metrics = data.get("value", [])
        if not metrics:
            return None, "no datapoints returned by Azure metric query"
        timeseries = metrics[0].get("timeseries", [])
        if not timeseries:
            return None, "Azure metric response had no timeseries"
        data_points = timeseries[0].get("data", [])
        if not data_points:
            return None, "Azure metric timeseries had no data points"
        # Walk backwards to find the last non-null data point
        for point in reversed(data_points):
            for key in ("average", "total", "maximum", "minimum", "count"):
                val = point.get(key)
                if val is not None:
                    return float(val), None
        return None, "Azure metric data points contained no numeric values"
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        _log.debug("Failed to extract Azure metric value: %s: %s", type(exc).__name__, exc)
        return None, f"malformed Azure metric response ({type(exc).__name__})"


def _extract_log_analytics_value(data: dict[str, Any], column: str) -> tuple[float | None, str | None]:
    """Read *column* from the last row of the first table; unnamed means the first column, as the service does.

    Returns ``(value, error_message)``, or ``(None, None)`` when the query returned no rows.
    """
    try:
        if error := data.get("error") or data.get("Error"):
            return None, f"Log Analytics query error: {error}"
        tables = data.get("Tables") or []
        if not tables:
            return None, "no tables returned by Log Analytics query"
        columns = tables[0].get("Columns") or []
        names = [c.get("ColumnName") for c in columns]
        rows = tables[0].get("Rows") or []
        if not rows:
            return None, None
        name = column or (names[0] if names else "")
        if name not in names:
            return None, f"column '{name}' not in Log Analytics result columns {names}"
        index = names.index(name)
        raw = rows[-1][index]
        if isinstance(raw, str) and columns[index].get("ColumnType") == "decimal":
            raw = float(raw)  # the query API sends decimal values as strings
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"column '{name}' value {raw!r} is not numeric"
        return float(raw), None
    except ValueError:
        return None, f"column '{name}' value {raw!r} is not numeric"
    except (TypeError, IndexError, AttributeError) as exc:
        _log.debug("Failed to extract Log Analytics value: %s: %s", type(exc).__name__, exc)
        return None, f"malformed Log Analytics response ({type(exc).__name__})"


def _parse_rule(rule: dict[str, Any]) -> EvaluationRule | None:
    """Build a domain ``EvaluationRule`` from an API rule dict, or return None.

    Returns None only when the rule dict is genuinely empty or missing the
    operator/threshold keys. Malformed rules (bad operator, non-numeric
    threshold) raise ``ValueError`` so the caller can surface UNKNOWN
    health rather than silently treating them as HEALTHY.
    """
    if not rule:
        return None
    op_raw = rule.get("operator", "")
    threshold_raw = rule.get("threshold")
    if not op_raw or threshold_raw is None:
        return None
    try:
        op = ComparisonOperator(op_raw)
        threshold = float(threshold_raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"invalid evaluation rule {rule!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return EvaluationRule(operator=op, threshold=threshold)


def _evaluate_health(value: float, rules: Any) -> HealthState:
    """Evaluate a value against evaluation rules using domain rule objects.

    Mirrors :meth:`SignalDefinition.evaluate` but tolerates missing rules.
    Malformed rules result in :data:`HealthState.UNKNOWN` (with a logged
    warning) so that invalid configuration is visible rather than silently
    reported as HEALTHY.
    """
    if not isinstance(rules, dict):
        if rules is not None:
            _log.warning("Skipping evaluation: evaluationRules is not a dict: %r", type(rules).__name__)
        return HealthState.UNKNOWN
    try:
        unhealthy = _parse_rule(rules.get("unhealthyRule") or {})
        if unhealthy is not None and unhealthy.triggers(value):
            return HealthState.UNHEALTHY
        degraded = _parse_rule(rules.get("degradedRule") or {})
        if degraded is not None and degraded.triggers(value):
            return HealthState.DEGRADED
    except ValueError as exc:
        _log.warning("Skipping evaluation due to malformed rule: %s", exc)
        return HealthState.UNKNOWN
    return HealthState.HEALTHY


def _has_dynamic_rules(rules: Any) -> bool:
    if not isinstance(rules, dict):
        return False
    for key in ("unhealthyRule", "degradedRule"):
        rule = rules.get(key)
        if isinstance(rule, dict) and str(rule.get("operator")) == "Dynamic":
            return True
    return False
