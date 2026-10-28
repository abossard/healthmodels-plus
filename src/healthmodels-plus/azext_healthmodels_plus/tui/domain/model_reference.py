"""Canonical scoped model identity and the stable model-root reference contract."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit
from uuid import UUID


@dataclass(frozen=True)
class ModelKey:
    endpoint: str = field(compare=False)
    subscription: str = field(compare=False)
    group: str = field(compare=False)
    name: str = field(compare=False)
    canonical: tuple[str, str, str, str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical", (
            str(self.endpoint).rstrip("/").casefold(), str(self.subscription).casefold(),
            self.group.casefold(), self.name.casefold(),
        ))

    @property
    def label(self) -> str:
        return f"{self.subscription}/{self.group}/{self.name}"


@dataclass(frozen=True)
class ModelReference:
    resource_id: str
    target: ModelKey | None = None
    diagnostic: str = ""


def parse_reference(
    resource_id: str, endpoint: str = "https://management.azure.com",
) -> ModelReference | None:
    parts = resource_id.rstrip("/").split("/")
    # Ordinary Azure resources are not nested models.
    if not any(unquote(p).casefold() == "microsoft.cloudhealth" for p in parts):
        return None
    try:
        if resource_id.endswith("//"):
            raise ValueError("empty trailing segment")
        if urlsplit(resource_id).query or urlsplit(resource_id).fragment:
            raise ValueError("query or fragment")
        if re.search(r"%(?![0-9a-fA-F]{2})", resource_id):
            raise ValueError("invalid escape")
        decoded = [unquote(p) for p in parts]
        if any(re.search(r"[/\\?#\s\x00-\x1f]", p) or p in {".", ".."} for p in decoded[1:]):
            raise ValueError("invalid segment or encoded separator")
        if len(decoded) < 9 or decoded[0] != "" or any(not p for p in decoded[1:]):
            raise ValueError("incomplete model ARM ID")
        if decoded[6].casefold() == "microsoft.cloudhealth" and decoded[7].casefold() != "healthmodels":
            return None
        if [decoded[i].casefold() for i in (1, 3, 5, 6, 7)] != [
            "subscriptions", "resourcegroups", "providers", "microsoft.cloudhealth", "healthmodels",
        ]:
            raise ValueError("expected model ARM ID")
        subscription = str(UUID(decoded[2]))
        if subscription != decoded[2].casefold():
            raise ValueError("expected subscription UUID")
        if len(decoded) != 9:
            return ModelReference(resource_id, diagnostic="unsupported entity/subresource reference")
        return ModelReference(resource_id, ModelKey(endpoint, subscription, decoded[4], decoded[8]))
    except ValueError as exc:
        return ModelReference(resource_id, diagnostic=f"invalid nested reference: {exc}")
