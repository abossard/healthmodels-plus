"""Pure draft identity, edit, validation, and apply-plan calculations."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Any, Iterable, Literal, Mapping

from azext_healthmodels_plus.tui.domain.model_reference import ModelKey

DraftKind = Literal["entity", "signal_definition", "relationship"]
PathToken = str | int


@dataclass(frozen=True)
class ResourceTarget:
    model: ModelKey
    kind: DraftKind
    name: str

    def __post_init__(self) -> None:
        if self.kind not in {"entity", "signal_definition", "relationship"}:
            raise ValueError(f"Unsupported draft kind: {self.kind}")
        if not self.name:
            raise ValueError("ResourceTarget.name must be non-empty")


@dataclass(frozen=True)
class FieldDiff:
    path: tuple[PathToken, ...]
    before: Any
    after: Any


@dataclass(frozen=True)
class DraftValidationResult:
    is_valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ApplyPlan:
    target: ResourceTarget
    revision: int
    changes: tuple[FieldDiff, ...]
    edited_resource: Mapping[str, Any]
    normalized_live_baseline: Mapping[str, Any]
    generated_at: datetime
    shared_consumer_entity_names: tuple[str, ...] = ()


# The one list of read-only fields. "*" matches any key or index; keys compare after strip().casefold().
_READ_ONLY_PATHS: tuple[tuple[str, ...], ...] = (
    ("id",), ("type",), ("systemdata",), ("etag",), ("provisioningstate",),
    ("properties", "provisioningstate"), ("properties", "healthstate"), ("properties", "discoveredby"),
    ("properties", "signalgroups", "*", "signals", "*", "status"),
    ("properties", "signalgroups", "*", "resourcehealth", "status"),
)


def _is_read_only(path: tuple[PathToken, ...]) -> bool:
    """True when ``path`` is a read-only field or lies inside one."""
    canonical = tuple(token.strip().casefold() if isinstance(token, str) else token for token in path)
    return any(
        len(canonical) >= len(pattern) and all(part in ("*", token) for part, token in zip(pattern, canonical))
        for pattern in _READ_ONLY_PATHS
    )


def parse_field_path(path: str) -> tuple[PathToken, ...]:
    if not path:
        raise ValueError("Field path must be non-empty")
    if path != path.strip():
        raise ValueError("Field path must not have leading or trailing whitespace")
    if ".." in path:
        raise ValueError(f"Invalid field path: {path}")
    if re.search(r"\s", path):
        raise ValueError(f"Invalid field path: {path}")
    raw_parts = path.split(".")
    if not raw_parts:
        raise ValueError("Field path must contain named segments")
    if any(part == "" for part in raw_parts):
        raise ValueError(f"Invalid field path: {path}")
    tokens: list[PathToken] = []
    for part in raw_parts:
        if "[" not in part:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", part) is None:
                raise ValueError(f"Invalid field path segment: {part}")
            tokens.append(part)
            continue
        match = re.fullmatch(r"([^\[]+)((\[[0-9]+\])*)", part)
        if match is None:
            raise ValueError(f"Invalid field path segment: {part}")
        key = match.group(1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) is None:
            raise ValueError(f"Invalid field path segment: {part}")
        tokens.append(key)
        for index_raw in re.findall(r"\[([0-9]+)\]", match.group(2)):
            tokens.append(int(index_raw))
    return tuple(tokens)


def normalize_known_readonly_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy without read-only fields, for comparing and writing resources."""
    def strip(value: Any, path: tuple[PathToken, ...]) -> Any:
        if isinstance(value, Mapping):
            return {k: strip(v, path + (k,)) for k, v in value.items() if not _is_read_only(path + (k,))}
        if isinstance(value, list):
            return [strip(v, path + (i,)) for i, v in enumerate(value) if not _is_read_only(path + (i,))]
        return deepcopy(value)

    return strip(payload, ())


def apply_field_edit(document: Mapping[str, Any], path: tuple[PathToken, ...], value: Any) -> dict[str, Any]:
    if not path:
        raise ValueError("Field path must be non-empty")
    _assert_editable_path(path)
    root = deepcopy(dict(document))
    cursor: Any = root
    for segment, next_segment in zip(path, path[1:]):
        label = f"[{segment}]" if isinstance(segment, int) else f"'{segment}'"
        if isinstance(segment, int):
            if not isinstance(cursor, list):
                raise ValueError(f"Cannot index non-list at {label}")
            if segment < 0 or segment >= len(cursor):
                raise ValueError(f"Index out of range at {label}")
            current = cursor[segment]
        elif isinstance(cursor, dict):
            current = cursor.get(segment)
        else:
            raise ValueError(f"Cannot descend through non-object at {label}")
        if current is None:
            current = cursor[segment] = [] if isinstance(next_segment, int) else {}
        elif not isinstance(current, (dict, list)):
            raise ValueError(f"Cannot descend through scalar at {label}")
        cursor = current

    leaf = path[-1]
    if isinstance(leaf, int):
        if not isinstance(cursor, list):
            raise ValueError(f"Cannot index non-list at [{leaf}]")
        if leaf < 0 or leaf >= len(cursor):
            raise ValueError(f"Index out of range at [{leaf}]")
        cursor[leaf] = deepcopy(value)
    else:
        if not isinstance(cursor, dict):
            raise ValueError(f"Cannot set property on non-object at '{leaf}'")
        cursor[leaf] = deepcopy(value)
    return root


def get_in(document: Mapping[str, Any], path: tuple[PathToken, ...]) -> Any:
    cursor: Any = document
    for segment in path:
        if isinstance(segment, int):
            if not isinstance(cursor, list) or segment < 0 or segment >= len(cursor):
                return None
            cursor = cursor[segment]
        else:
            if not isinstance(cursor, Mapping):
                return None
            cursor = cursor.get(segment)
    return cursor


def diff_documents(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[FieldDiff, ...]:
    def _walk(path: tuple[PathToken, ...], left: Any, right: Any, out: list[FieldDiff]) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            keys = sorted(set(left) | set(right))
            for key in keys:
                _walk(path + (key,), left.get(key), right.get(key), out)
            return
        if isinstance(left, list) and isinstance(right, list):
            max_len = max(len(left), len(right))
            for index in range(max_len):
                left_item = left[index] if index < len(left) else None
                right_item = right[index] if index < len(right) else None
                _walk(path + (index,), left_item, right_item, out)
            return
        if left != right:
            out.append(FieldDiff(path=path, before=left, after=right))

    diffs: list[FieldDiff] = []
    _walk((), before, after, diffs)
    return tuple(diffs)


MAX_NAME_LENGTH = 256  # the CloudHealth API documents no limit for names; query text gets none
_NAME_PATHS = (("properties", "displayName"), ("properties", "icon", "iconName"))


def validate_target_payload(target: ResourceTarget, payload: Mapping[str, Any]) -> DraftValidationResult:
    errors: list[str] = []
    if str(payload.get("name", "")).strip() not in {"", target.name}:
        errors.append("Payload name does not match target name")
    properties = payload.get("properties")
    if not isinstance(properties, Mapping):
        errors.append("Payload properties must be an object")
    elif target.kind == "relationship":
        if not str(properties.get("parentEntityName", "")).strip():
            errors.append("Relationship requires properties.parentEntityName")
        if not str(properties.get("childEntityName", "")).strip():
            errors.append("Relationship requires properties.childEntityName")
    elif target.kind == "signal_definition":
        if not str(properties.get("signalKind", "")).strip():
            errors.append("Signal definition requires properties.signalKind")
    elif target.kind == "entity":
        _validate_finite_percentage_field(
            errors=errors,
            container=properties,
            key="healthObjective",
            label="Entity properties.healthObjective",
        )
    return DraftValidationResult(is_valid=not errors, errors=tuple(errors))


def build_apply_plan(
    *,
    target: ResourceTarget,
    revision: int,
    edited_resource: Mapping[str, Any],
    expected_baseline: Mapping[str, Any],
    live_baseline: Mapping[str, Any],
    generated_at: datetime,
    shared_consumer_entity_names: tuple[str, ...] = (),
) -> ApplyPlan:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    validation = validate_target_payload(target, edited_resource)
    if not validation.is_valid:
        raise ValueError(f"Draft is invalid: {', '.join(validation.errors)}")
    normalized_expected = normalize_known_readonly_fields(expected_baseline)
    normalized_live = normalize_known_readonly_fields(live_baseline)
    if normalized_expected != normalized_live:
        raise ValueError("Live baseline changed; draft baseline no longer matches current live resource. Discard the draft and edit again from the live state.")
    normalized_edited = normalize_known_readonly_fields(edited_resource)
    changes = diff_documents(normalized_live, normalized_edited)
    too_long = [  # changed names only: a long name already live is not this edit's
        f"{_format_path(path)} must be at most {MAX_NAME_LENGTH} characters (got {len(name)})."
        for path in _NAME_PATHS
        if isinstance(name := get_in(normalized_edited, path), str)
        and len(name) > MAX_NAME_LENGTH and name != get_in(normalized_live, path)
    ]
    if too_long:
        raise ValueError(" ".join(too_long))
    return ApplyPlan(
        target=target,
        revision=revision,
        changes=changes,
        edited_resource=deepcopy(dict(normalized_edited)),
        normalized_live_baseline=deepcopy(dict(normalized_live)),
        shared_consumer_entity_names=tuple(shared_consumer_entity_names),
        generated_at=generated_at,
    )


def collect_signal_definition_consumers(
    entities: Iterable[Mapping[str, Any]],
    signal_definition_name: str,
) -> tuple[str, ...]:
    def uses_definition(entity: Mapping[str, Any]) -> bool:
        properties = entity.get("properties")
        groups = properties.get("signalGroups") if isinstance(properties, Mapping) else None
        return isinstance(groups, Mapping) and any(
            isinstance(signal, Mapping)
            and str(signal.get("signalDefinitionName", "")).strip() == signal_definition_name
            for group in groups.values() if isinstance(group, Mapping)
            for signal in (group.get("signals") if isinstance(group.get("signals"), list) else ())
        )

    return tuple(sorted({
        name for entity in entities
        if (name := str(entity.get("name", "")).strip()) and uses_definition(entity)
    }))


def changed_paths(changes: Iterable[FieldDiff]) -> tuple[str, ...]:
    return tuple(_format_path(change.path) for change in changes)


def find_known_readonly_tampering(
    baseline: Mapping[str, Any],
    edited: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(_format_path(diff.path) for diff in diff_documents(baseline, edited) if _is_read_only(diff.path))


def _format_path(tokens: tuple[PathToken, ...]) -> str:
    output: list[str] = []
    for token in tokens:
        if isinstance(token, int):
            if not output:
                output.append(f"[{token}]")
            else:
                output[-1] = f"{output[-1]}[{token}]"
        else:
            output.append(token)
    return ".".join(output)


def _assert_editable_path(path: tuple[PathToken, ...]) -> None:
    if not path:
        raise ValueError("Field path must be non-empty")
    if not isinstance(path[0], str):
        raise ValueError("Field path must start with an object key")
    if _is_read_only(path):
        raise ValueError(f"Field path is read-only: {_format_path(path)}")


def _validate_finite_percentage_field(
    *,
    errors: list[str],
    container: Mapping[str, Any],
    key: str,
    label: str,
) -> None:
    if key not in container:
        return
    raw = container.get(key)
    if isinstance(raw, bool):
        errors.append(f"{label} must be a finite number between 0 and 100")
        return
    if not isinstance(raw, (int, float)):
        errors.append(f"{label} must be a finite number between 0 and 100")
        return
    numeric = float(raw)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
        errors.append(f"{label} must be a finite number between 0 and 100")
