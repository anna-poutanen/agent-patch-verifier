"""Validation for portable manifest-v2 evidence bundle metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
import json
import math
import re

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


_SIMPLE_PATH_COMPONENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ERROR_MESSAGE_LENGTH = 240


def validate_manifest_v2(payload: object) -> None:
    """Validate a manifest-v2 payload or raise a concise ``ValueError``.

    This function deliberately handles schema version 2 only. Compatibility
    policy and validation for legacy manifest-v1 bundles belong to the bundle
    loader, which can dispatch by version before calling this helper.
    """

    validator = _manifest_v2_validator()
    errors = tuple(validator.iter_errors(payload))
    if not errors:
        assert isinstance(payload, Mapping)
        execution = payload["execution"]
        assert isinstance(execution, Mapping)
        timeout = execution["timeout_seconds"]
        if isinstance(timeout, (int, float)) and not isinstance(
            timeout, bool
        ) and not math.isfinite(timeout):
            raise ValueError(
                "manifest.execution.timeout_seconds: must be finite"
            )
        return

    actionable = tuple(_actionable_error(error) for error in errors)
    error = min(actionable, key=_error_sort_key)
    path, message = _describe_error(error)
    raise ValueError(f"{_format_path(path)}: {message}")


@lru_cache(maxsize=1)
def _manifest_v2_validator() -> Draft202012Validator:
    schema_resource = (
        files("patch_verifier")
        .joinpath("schemas")
        .joinpath("manifest.schema.json")
    )
    try:
        schema = json.loads(schema_resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise RuntimeError("packaged manifest-v2 schema is invalid") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _actionable_error(error: ValidationError) -> ValidationError:
    """Prefer the deepest concrete cause of a combinator failure."""

    leaves = tuple(_leaf_errors(error))
    return min(
        leaves,
        key=lambda item: (
            -len(item.absolute_path),
            _path_sort_key(item.absolute_path),
            str(item.validator),
            item.message,
        ),
    )


def _leaf_errors(error: ValidationError) -> Iterator[ValidationError]:
    if not error.context:
        yield error
        return
    for child in error.context:
        yield from _leaf_errors(child)


def _error_sort_key(error: ValidationError) -> tuple[object, ...]:
    return (
        _path_sort_key(error.absolute_path),
        tuple(str(item) for item in error.absolute_schema_path),
        str(error.validator),
        error.message,
    )


def _path_sort_key(path: Sequence[object]) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, item) if isinstance(item, int) else (1, str(item)) for item in path
    )


def _describe_error(error: ValidationError) -> tuple[tuple[object, ...], str]:
    path = tuple(error.absolute_path)
    validator = error.validator

    if validator == "required" and isinstance(error.instance, Mapping):
        required = set(error.validator_value)
        missing = sorted(required - set(error.instance))
        if missing:
            return (*path, missing[0]), "required property is missing"

    if validator == "additionalProperties" and isinstance(
        error.instance, Mapping
    ):
        allowed = set(error.schema.get("properties", ()))
        unexpected = sorted(set(error.instance) - allowed)
        if unexpected:
            return (*path, unexpected[0]), "property is not allowed"

    if validator == "type":
        expected = error.validator_value
        if isinstance(expected, str):
            description = expected
        else:
            description = " or ".join(str(item) for item in expected)
        return path, f"expected {description}"

    if validator == "const":
        return path, f"expected constant {error.validator_value!r}"

    if validator == "enum":
        choices = ", ".join(repr(item) for item in error.validator_value)
        return path, f"expected one of {choices}"

    if validator == "pattern":
        return path, "value does not match the required pattern"

    if validator == "format":
        return path, f"expected {error.validator_value} format"

    if validator == "minimum":
        return path, f"must be at least {error.validator_value}"

    if validator == "maximum":
        return path, f"must be at most {error.validator_value}"

    if validator == "exclusiveMinimum":
        return path, f"must be greater than {error.validator_value}"

    if validator == "minLength":
        return path, f"must contain at least {error.validator_value} characters"

    if validator == "minItems":
        return path, f"must contain at least {error.validator_value} items"

    if validator == "maxItems":
        return path, f"must contain at most {error.validator_value} items"

    if validator == "uniqueItems":
        return path, "items must be unique"

    message = " ".join(error.message.split())
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[: _MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
    return path, message


def _format_path(path: Sequence[object]) -> str:
    result = "manifest"
    for item in path:
        if isinstance(item, int):
            result += f"[{item}]"
        else:
            component = str(item)
            if _SIMPLE_PATH_COMPONENT.fullmatch(component):
                result += f".{component}"
            else:
                result += f"[{json.dumps(component)}]"
    return result


__all__ = ["validate_manifest_v2"]
