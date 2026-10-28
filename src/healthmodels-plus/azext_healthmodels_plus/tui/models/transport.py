"""Transport models — TypedDicts matching the CloudHealth REST wire format.

These are permissive shapes close to what the API returns. They absorb
preview API churn so the domain models stay stable. Never import these
outside of ``domain.parse`` and ``client.rest_client``.
"""
from __future__ import annotations

from typing import Any, TypedDict


# ─── Signal status (inline in entity signal groups) ──────────────────


class TransportSignalStatus(TypedDict, total=False):
    healthState: str  # "Healthy" | "Degraded" | "Unhealthy" | "Unknown"
    value: float | None
    reportedAt: str  # ISO 8601 timestamp
    error: str  # Optional read-only service-side diagnostic


# ─── Signal reference (within entity signal groups) ──────────────────


class TransportSignalRef(TypedDict, total=False):
    name: str  # GUID — the signal instance name
    signalDefinitionName: str  # GUID → resolved via signal definitions list
    signalKind: str  # "AzureResourceMetric" | "PrometheusMetricsQuery" | ...
    refreshInterval: str  # ISO 8601 duration, e.g. "PT1M"
    status: TransportSignalStatus


# ─── Signal groups (nested in entity properties) ─────────────────────


class TransportAzureResourceGroup(TypedDict, total=False):
    authenticationSetting: str
    azureResourceId: str
    signals: list[TransportSignalRef]


class TransportAzureMonitorWorkspaceGroup(TypedDict, total=False):
    authenticationSetting: str
    azureMonitorWorkspaceResourceId: str
    signals: list[TransportSignalRef]


class TransportDependenciesGroup(TypedDict, total=False):
    aggregationType: str  # "WorstOf" | "Average"
    thresholds: dict[str, Any]


class TransportSignalGroups(TypedDict, total=False):
    azureResource: TransportAzureResourceGroup
    azureMonitorWorkspace: TransportAzureMonitorWorkspaceGroup
    azureLogAnalytics: dict[str, Any]  # similar shape, less common
    dependencies: TransportDependenciesGroup


# ─── Evaluation rules (in signal definitions) ────────────────────────


class TransportEvaluationRule(TypedDict, total=False):
    operator: str  # "GreaterThan" | "LessThan" | ...
    threshold: float
    sensitivity: str


class TransportEvaluationRules(TypedDict, total=False):
    degradedRule: TransportEvaluationRule
    unhealthyRule: TransportEvaluationRule


# ─── Signal definition (top-level resource) ──────────────────────────


class TransportSignalDefinitionProperties(TypedDict, total=False):
    signalKind: str
    displayName: str
    refreshInterval: str
    dataUnit: str
    evaluationRules: TransportEvaluationRules
    metricNamespace: str
    metricName: str
    timeGrain: str
    aggregationType: str
    dimensionFilter: str
    queryText: str


class TransportSignalDefinition(TypedDict, total=False):
    id: str  # Full ARM resource ID
    name: str  # GUID
    type: str
    properties: TransportSignalDefinitionProperties


# ─── Entity (top-level resource) ─────────────────────────────────────


class TransportEntityIcon(TypedDict, total=False):
    iconName: str  # "Resource" | "SystemComponent" | "AzureKubernetesService" | ...


class TransportEntityProperties(TypedDict, total=False):
    displayName: str
    icon: TransportEntityIcon
    impact: str  # "Standard" | "Limited" | "Suppressed"
    healthObjective: str
    healthState: str  # Runtime health state
    signalGroups: TransportSignalGroups
    alerts: dict[str, Any]


class TransportEntity(TypedDict, total=False):
    id: str  # Full ARM resource ID
    name: str  # GUID entity name
    type: str
    properties: TransportEntityProperties


# ─── Relationship (top-level resource) ───────────────────────────────


class TransportRelationshipProperties(TypedDict, total=False):
    parentEntityName: str  # GUID or model name
    childEntityName: str  # GUID


class TransportRelationship(TypedDict, total=False):
    id: str
    name: str  # GUID
    type: str
    properties: TransportRelationshipProperties
