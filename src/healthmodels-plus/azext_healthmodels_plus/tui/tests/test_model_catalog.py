from __future__ import annotations

from dataclasses import dataclass

import pytest

from azext_healthmodels_plus.tui.watch.model_catalog import (
    ModelCatalogRow,
    _resource_group_from_arm_id,
    load_model_catalog_rows,
)


@dataclass
class _FakeClient:
    payload: list[dict[str, object]]
    arm_endpoint: str = "https://management.azure.com"
    _subscription_id: str = "00000000-0000-0000-0000-000000000001"

    def list_health_models(self, resource_group: str | None = None) -> list[dict[str, object]]:
        return list(self.payload)

    def model_key(self, group: str, model: str):
        from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

        return ModelKey(self.arm_endpoint, self._subscription_id, group, model)


def test_resource_group_parsing_is_case_insensitive() -> None:
    resource_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourcegroups/Rg-Case/providers/Microsoft.CloudHealth/healthmodels/demo"
    )

    assert _resource_group_from_arm_id(resource_id) == "Rg-Case"


@pytest.mark.parametrize("resource_id", ["", "/subscriptions/x/providers/Microsoft.CloudHealth/healthmodels/demo"])
def test_resource_group_parsing_returns_none_when_missing(resource_id: str) -> None:
    assert _resource_group_from_arm_id(resource_id) is None


def test_load_model_catalog_rows_returns_full_identity_and_duplicate_names() -> None:
    payload = [
        {
            "id": "/subscriptions/sub/resourceGroups/rg-a/providers/Microsoft.CloudHealth/healthmodels/shared",
            "name": "shared",
            "location": "westeurope",
            "properties": {"provisioningState": "Succeeded"},
        },
        {
            "id": "/subscriptions/sub/resourceGroups/rg-b/providers/Microsoft.CloudHealth/healthmodels/shared",
            "name": "shared",
            "location": "eastus2",
            "properties": {"provisioningState": "Creating"},
        },
    ]

    rows = load_model_catalog_rows(_FakeClient(payload))

    assert [row.name for row in rows] == ["shared", "shared"]
    assert [row.resource_group for row in rows] == ["rg-a", "rg-b"]
    assert [row.location for row in rows] == ["westeurope", "eastus2"]
    assert [row.provisioning_state for row in rows] == ["Succeeded", "Creating"]
    assert rows[0].id != rows[1].id
    assert rows[0].model_key.group == "rg-a"
    assert rows[1].model_key.group == "rg-b"


def test_model_catalog_row_has_no_write_surface() -> None:
    fields = {field.name for field in ModelCatalogRow.__dataclass_fields__.values()}
    assert "create" not in fields
    assert "update" not in fields
    assert "delete" not in fields
