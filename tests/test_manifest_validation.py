from __future__ import annotations

from copy import deepcopy
import json

import pytest

from patch_verifier.manifest_validation import validate_manifest_v2


def _valid_manifest() -> dict[str, object]:
    run_id = "20261118T120000Z-1234abcd"
    return {
        "schema_version": 2,
        "tool": {"name": "agent-patch-verifier", "version": "0.2.0"},
        "run_id": run_id,
        "created_at": "2026-11-18T12:00:00+00:00",
        "repository": "/workspace/project",
        "requested_base_revision": "HEAD",
        "base_commit": "a" * 40,
        "patched_tree": "b" * 40,
        "patch_sha256": "1" * 64,
        "issue_sha256": "2" * 64,
        "evidence_plan_sha256": "3" * 64,
        "results_sha256": "4" * 64,
        "context_sha256": "5" * 64,
        "harness_sha256": "6" * 64,
        "report_sha256": "7" * 64,
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.1",
            "platform": "linux",
        },
        "execution": {
            "command_prefix": ["/usr/bin/python3", "-m", "pytest", "-q"],
            "timeout_seconds": 60.0,
            "repeat": 2,
            "output_limit_bytes_per_stream": 250_000,
            "warning": "Local subprocess execution is not a security sandbox.",
        },
        "scope": {
            "changed_files": [
                {
                    "old_path": "example.py",
                    "new_path": "example.py",
                    "old_lines": [2],
                    "new_lines": [2],
                }
            ],
            "changed_symbols": [
                {
                    "path": "example.py",
                    "qualified_name": "resolve",
                    "kind": "function",
                    "base_span": {"start_line": 1, "end_line": 2},
                    "patched_span": {"start_line": 1, "end_line": 2},
                    "removed_lines": [2],
                    "added_lines": [2],
                    "base_path": "example.py",
                    "patched_path": "example.py",
                    "base_occurrence": 1,
                    "patched_occurrence": 1,
                    "revision_match": "unique_identity",
                }
            ],
        },
        "requirements": [
            {
                "id": "REQ-ONE",
                "statement": "A supplied value remains unchanged.",
                "kind": "preservation",
                "provenance": "preexisting",
                "source": "tests/test_example.py:4",
                "excerpt": "assert resolve(3) == 3",
            }
        ],
        "cases": [
            {
                "id": "CASE-ONE",
                "requirement_id": "REQ-ONE",
                "injected_path": (
                    f".patch_verifier_generated_{run_id}/"
                    "test_CASE_ONE_deadbeef.py"
                ),
                "test_sha256": "8" * 64,
                "command": [
                    "/usr/bin/python3",
                    "-m",
                    "pytest",
                    "-q",
                    "generated.py::test_value",
                    "--runxfail",
                    "--junitxml=result.xml",
                ],
                "finding": "preserved",
            }
        ],
    }


def test_accepts_representative_manifest_v2() -> None:
    assert validate_manifest_v2(_valid_manifest()) is None


def test_reports_missing_required_property_at_its_path() -> None:
    payload = _valid_manifest()
    del payload["report_sha256"]

    with pytest.raises(
        ValueError,
        match=r"^manifest\.report_sha256: required property is missing$",
    ):
        validate_manifest_v2(payload)


def test_reports_unknown_property_at_its_path() -> None:
    payload = _valid_manifest()
    payload["unexpected"] = True

    with pytest.raises(
        ValueError,
        match=r"^manifest\.unexpected: property is not allowed$",
    ):
        validate_manifest_v2(payload)


def test_rejects_boolean_where_integer_is_required() -> None:
    payload = _valid_manifest()
    execution = payload["execution"]
    assert isinstance(execution, dict)
    execution["repeat"] = True

    with pytest.raises(
        ValueError,
        match=r"^manifest\.execution\.repeat: expected integer$",
    ):
        validate_manifest_v2(payload)


def test_reports_malformed_nested_value_at_the_deepest_path() -> None:
    payload = deepcopy(_valid_manifest())
    scope = payload["scope"]
    assert isinstance(scope, dict)
    symbols = scope["changed_symbols"]
    assert isinstance(symbols, list)
    symbol = symbols[0]
    assert isinstance(symbol, dict)
    base_span = symbol["base_span"]
    assert isinstance(base_span, dict)
    base_span["start_line"] = "1"

    with pytest.raises(
        ValueError,
        match=(
            r"^manifest\.scope\.changed_symbols\[0\]\.base_span\.start_line: "
            r"expected integer$"
        ),
    ):
        validate_manifest_v2(payload)


def test_rejects_more_cases_than_the_execution_budget() -> None:
    payload = deepcopy(_valid_manifest())
    cases = payload["cases"]
    assert isinstance(cases, list)
    cases[:] = [deepcopy(cases[0]) for _ in range(51)]

    with pytest.raises(
        ValueError,
        match=r"^manifest\.cases: must contain at most 50 items$",
    ):
        validate_manifest_v2(payload)


def test_rejects_json_number_that_overflows_to_infinity() -> None:
    payload = _valid_manifest()
    serialized = json.dumps(payload).replace("60.0", "1e999", 1)
    overflowed = json.loads(serialized)

    with pytest.raises(
        ValueError,
        match=(
            r"^manifest\.execution\.timeout_seconds: must be at most 3600$"
        ),
    ):
        validate_manifest_v2(overflowed)
