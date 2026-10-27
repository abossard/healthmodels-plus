"""Enums for the Azure Health Model extension (Microsoft.CloudHealth API)."""

from __future__ import annotations

from enum import Enum


class HealthState(Enum):
    """The runtime health state of an entity or signal."""

    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    UNHEALTHY = "Unhealthy"
    UNKNOWN = "Unknown"
    ERROR = "Error"
    DELETED = "Deleted"

    @property
    def severity(self) -> int:
        """Numeric severity for comparison (higher is worse)."""
        _map: dict[str, int] = {
            "Healthy": 0,
            "Unknown": 1,
            "Degraded": 2,
            "Unhealthy": 3,
            "Error": 4,
            "Deleted": 4,
        }
        return _map[self.value]

    def is_worse_than(self, other: HealthState) -> bool:
        """True if this state is strictly worse (higher severity) than *other*."""
        return self.severity > other.severity

    def is_better_than(self, other: HealthState) -> bool:
        """True if this state is strictly better (lower severity) than *other*."""
        return self.severity < other.severity

    @property
    def icon(self) -> str:
        """Emoji icon representing this health state."""
        _map: dict[str, str] = {
            "Healthy": "🟢",
            "Degraded": "🟡",
            "Unhealthy": "🔴",
            "Unknown": "⚪",
            "Error": "❌",
            "Deleted": "🗑",
        }
        return _map[self.value]

    @property
    def color(self) -> str:
        """Rich markup color string."""
        _map: dict[str, str] = {
            "Healthy": "green",
            "Degraded": "yellow",
            "Unhealthy": "red",
            "Unknown": "dim",
            "Error": "red",
            "Deleted": "dim",
        }
        return _map[self.value]


class SignalKind(Enum):
    """How the signal data is fetched."""

    AZURE_RESOURCE_METRIC = "AzureResourceMetric"
    PROMETHEUS_METRICS_QUERY = "PrometheusMetricsQuery"
    LOG_ANALYTICS_QUERY = "LogAnalyticsQuery"
    EXTERNAL = "External"

    @property
    def short_label(self) -> str:
        """Short display label for CLI output."""
        _map: dict[str, str] = {
            "AzureResourceMetric": "ARM",
            "PrometheusMetricsQuery": "PromQL",
            "LogAnalyticsQuery": "KQL",
            "External": "Ext",
        }
        return _map[self.value]


class DataUnit(Enum):
    """Unit of measurement for signals."""

    COUNT = "Count"
    PERCENT = "Percent"
    MILLISECONDS = "MilliSeconds"
    BYTES = "Bytes"
    OTHER = "Other"  # Fallback for unknown / SDK-introduced units


class ComparisonOperator(Enum):
    """Comparison operators for evaluation rules, in the Microsoft.CloudHealth wire format."""

    GREATER_THAN = "GreaterThan"
    LESS_THAN = "LessThan"
    GREATER_THAN_OR_EQUAL = "GreaterThanOrEqual"
    LESS_THAN_OR_EQUAL = "LessThanOrEqual"
    EQUAL = "Equal"

    @property
    def direction(self) -> str:
        """Conceptual direction: 'up', 'down', or 'eq'."""
        if self in (ComparisonOperator.GREATER_THAN, ComparisonOperator.GREATER_THAN_OR_EQUAL):
            return "up"
        if self in (ComparisonOperator.LESS_THAN, ComparisonOperator.LESS_THAN_OR_EQUAL):
            return "down"
        return "eq"

    def evaluate(self, value: float, threshold: float) -> bool:
        """Return True if *value* violates the threshold (triggers the rule)."""
        if self is ComparisonOperator.GREATER_THAN:
            return value > threshold
        if self is ComparisonOperator.GREATER_THAN_OR_EQUAL:
            return value >= threshold
        if self is ComparisonOperator.LESS_THAN:
            return value < threshold
        if self is ComparisonOperator.LESS_THAN_OR_EQUAL:
            return value <= threshold
        if self is ComparisonOperator.EQUAL:
            return value == threshold
        raise ValueError(f"Unknown ComparisonOperator: {self!r}")


class Impact(Enum):
    """Propagation weight of an entity's health to its parent."""

    STANDARD = "Standard"
    LIMITED = "Limited"
    SUPPRESSED = "Suppressed"

    # Backward-compatible alias for legacy internal references.
    NONE = "Suppressed"


class ChangeKind(Enum):
    """Type of state change detected between polls."""

    ESCALATION = "escalation"
    RECOVERY = "recovery"
    NEW = "new"
    REMOVED = "removed"
    VALUE_CHANGED = "value_changed"
