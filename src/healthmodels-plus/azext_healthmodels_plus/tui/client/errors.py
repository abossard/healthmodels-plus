"""Error handling for the Health Model Azure CLI extension (Microsoft.CloudHealth API)."""

from __future__ import annotations


class HealthModelError(Exception):
    """Base exception for all health model errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str = "",
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details: list[dict[str, str]] = details or []

    def diagnostic_text(self) -> str:
        """Format a diagnostic string including class name, code and first detail."""
        parts = [f"{type(self).__name__}: {self}"]
        if self.status_code:
            suffix = f"(HTTP {self.status_code}"
            if self.code:
                suffix += f", code {self.code}"
            suffix += ")"
            parts.append(suffix)
        elif self.code:
            parts.append(f"[{self.code}]")
        if self.details:
            first = self.details[0]
            detail_msg = first.get("message", first.get("code", ""))
            if detail_msg:
                parts.append(f"detail: {detail_msg}")
        return " ".join(parts)


class HealthModelNotFoundError(HealthModelError):
    """The specified health model does not exist."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str = "",
        details: list[dict[str, str]] | None = None,
    ) -> None:
        hint = (
            " Verify the resource group exists and the health model name is"
            " spelled correctly in the current subscription context"
            " (`az account show`). Then retry"
            " `az monitor health-models tui -g <resource-group> --model <model-name>`."
        )
        super().__init__(message + hint, status_code, code=code, details=details)


class EntityNotFoundError(HealthModelError):
    """Entity not found within a health model."""


class AuthenticationError(HealthModelError):
    """Authentication or authorization failure (HTTP 401/403)."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str = "",
        details: list[dict[str, str]] | None = None,
    ) -> None:
        hint = (
            " Run `az login` to refresh credentials. If the issue persists,"
            " your account needs Reader on the target resource to view it and Contributor to change it."
        )
        super().__init__(message + hint, status_code, code=code, details=details)


class ThrottledError(HealthModelError):
    """Request was throttled (HTTP 429)."""

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        *,
        retry_after: float | None = None,
        code: str = "",
        details: list[dict[str, str]] | None = None,
    ) -> None:
        if retry_after is not None:
            message += f" Retry after {retry_after} seconds."
        super().__init__(message, status_code, code=code, details=details)
        self.retry_after = retry_after


class ArmError(HealthModelError):
    """Generic ARM error response."""


def describe_error(exc: BaseException) -> str:
    """One-line text for any failure; typed errors add HTTP status, code and first detail."""
    return exc.diagnostic_text() if isinstance(exc, HealthModelError) else f"{type(exc).__name__}: {exc}"


_NOT_FOUND_CODES = frozenset({"ResourceNotFound", "ResourceGroupNotFound"})

_ENTITY_NOT_FOUND_CODES = frozenset({
    "EntityNotFound",
    "SignalDefinitionNotFound",
    "SignalInstanceNotFound",
    "RelationshipNotFound",
    "AuthenticationSettingNotFound",
    "AuthenticationSettingsNotFound",
    "DiscoveryRuleNotFound",
})


def parse_arm_error(
    response_body: dict[str, object], status_code: int
) -> HealthModelError:
    """Parse an ARM error response body into a typed exception.

    ARM errors arrive as::

        {"error": {"code": "...", "message": "...", "details": [...]}}
    """
    error_obj = response_body.get("error") if isinstance(response_body, dict) else None
    if not isinstance(error_obj, dict):
        body_repr = repr(response_body)
        if len(body_repr) > 200:
            body_repr = body_repr[:200] + "..."
        return ArmError(
            f"Unexpected error response (HTTP {status_code}): {body_repr}",
            status_code=status_code,
            code="",
            details=[{"code": "raw_body", "message": body_repr}],
        )

    code = str(error_obj.get("code", ""))
    message = str(error_obj.get("message", "Unknown error"))
    raw_details = error_obj.get("details")
    details: list[dict[str, str]] = (
        [{str(k): str(v) for k, v in d.items()} for d in raw_details if isinstance(d, dict)]
        if isinstance(raw_details, list)
        else []
    )

    if code in _ENTITY_NOT_FOUND_CODES:
        return EntityNotFoundError(
            message, status_code=status_code, code=code, details=details
        )

    if code in _NOT_FOUND_CODES or status_code == 404:
        return HealthModelNotFoundError(
            message, status_code=status_code, code=code, details=details
        )

    if status_code in (401, 403):
        return AuthenticationError(
            message, status_code=status_code, code=code, details=details
        )

    if status_code == 429:
        return ThrottledError(
            message, status_code=status_code, code=code, details=details
        )

    return ArmError(message, status_code=status_code, code=code, details=details)
