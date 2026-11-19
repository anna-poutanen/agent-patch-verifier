import copy
import hashlib
import json
from collections.abc import Callable
from importlib import resources
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError


Artifact = dict[str, Any]


def load_schema() -> dict[str, Any]:
    schema_file = resources.files("patch_verifier").joinpath(
        "schemas/requirements-draft.schema.json"
    )
    return json.loads(schema_file.read_text(encoding="utf-8"))


def valid_artifact() -> Artifact:
    excerpt = "An explicit zero must be returned unchanged."
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "tool_version": "0.2.0",
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.1 (main, Nov 19 2026)",
            "platform": "linux",
        },
        "inputs": {
            "repository": "/work/example",
            "base_revision": "HEAD~1",
            "base_sha": "b" * 40,
            "base_context_sha256": "0" * 64,
            "patched_tree_hash": "c" * 40,
            "patch_sha256": "d" * 64,
            "issue_sha256": "e" * 64,
            "issue_source": "issue.md",
        },
        "changed_symbols": [
            {
                "path": "src/example.py",
                "qualified_name": "resolve",
                "kind": "function",
                "base_span": {"start_line": 3, "end_line": 5},
                "patched_span": {"start_line": 3, "end_line": 6},
                "removed_lines": [4],
                "added_lines": [4, 5],
                "base_path": "src/example.py",
                "patched_path": "src/example.py",
                "base_occurrence": 1,
                "patched_occurrence": 1,
                "revision_match": "unique_identity",
            }
        ],
        "draft": {
            "schema_version": 1,
            "candidates": [
                {
                    "id": "cand-" + "f" * 64,
                    "statement": "An explicit zero is returned unchanged.",
                    "intent": "requested",
                    "source_kind": "issue",
                    "source": {
                        "path": "issue.md",
                        "start_line": 4,
                        "end_line": 4,
                        "start_column": 0,
                        "end_column": len(excerpt.encode("utf-8")),
                        "excerpt": excerpt,
                        "sha256": digest,
                    },
                    "review_status": "requires_review",
                    "confidence": "high",
                    "rationale": "The issue states this behavior directly.",
                }
            ],
            "diagnostics": [
                {
                    "code": "unsupported_assertion",
                    "source": "tests/test_example.py",
                    "message": "A dynamic assertion could not be normalized.",
                    "line": 18,
                }
            ],
            "limitations": [
                "Draft candidates require human review before use as evidence."
            ],
            "truncated": False,
        },
    }


def validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_packaged_requirements_draft_schema_accepts_review_artifact() -> None:
    validator().validate(valid_artifact())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda artifact: artifact.update({"test_command": ["pytest"]}),
            "Additional properties",
        ),
        (
            lambda artifact: artifact["inputs"].pop("patch_sha256"),
            "patch_sha256",
        ),
        (
            lambda artifact: artifact["inputs"].pop("base_context_sha256"),
            "base_context_sha256",
        ),
        (
            lambda artifact: artifact["draft"]["candidates"][0].update(
                {"review_status": "approved"}
            ),
            "requires_review",
        ),
        (
            lambda artifact: artifact["draft"]["candidates"][0].update(
                {"id": "DRAFT-1"}
            ),
            "does not match",
        ),
        (
            lambda artifact: artifact["draft"]["candidates"][0].update(
                {"test_file": "generated_test.py"}
            ),
            "Additional properties",
        ),
        (
            lambda artifact: artifact["draft"]["candidates"][0]["source"].pop(
                "excerpt"
            ),
            "excerpt",
        ),
    ],
)
def test_schema_rejects_unreviewed_execution_or_missing_provenance(
    mutate: Callable[[Artifact], None],
    message: str,
) -> None:
    artifact = copy.deepcopy(valid_artifact())
    mutate(artifact)

    with pytest.raises(ValidationError, match=message):
        validator().validate(artifact)


def test_schema_rejects_unknown_nested_fields() -> None:
    artifact = valid_artifact()
    artifact["draft"]["diagnostics"][0]["detail"] = "not in the contract"

    with pytest.raises(ValidationError, match="Additional properties"):
        validator().validate(artifact)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["draft"].update(
            {"candidates": artifact["draft"]["candidates"] * 501}
        ),
        lambda artifact: artifact["draft"].update(
            {"diagnostics": artifact["draft"]["diagnostics"] * 201}
        ),
        lambda artifact: artifact["draft"].update(
            {"limitations": ["review required"] * 51}
        ),
        lambda artifact: artifact["draft"]["candidates"][0].update(
            {"statement": "x" * 4001}
        ),
        lambda artifact: artifact["draft"]["candidates"][0]["source"].update(
            {"excerpt": "x" * 16001}
        ),
    ],
)
def test_schema_enforces_extractor_resource_bounds(
    mutate: Callable[[Artifact], None],
) -> None:
    artifact = valid_artifact()
    mutate(artifact)

    with pytest.raises(ValidationError):
        validator().validate(artifact)
