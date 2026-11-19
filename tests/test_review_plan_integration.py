from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from patch_verifier.draft_validation import validate_requirement_draft_artifact
from patch_verifier.plan import load_evidence_plan
from patch_verifier.review import (
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
)


def _candidate(
    *,
    excerpt: str,
    source_kind: str,
    path: str,
    line: int,
) -> dict[str, Any]:
    statement = (
        excerpt
        if source_kind == "issue"
        else "Preserve the behavior asserted by the base test: "
        + excerpt.removeprefix("assert ")
    )
    start_column = 0 if source_kind == "issue" else 4
    source = {
        "path": path,
        "start_line": line,
        "end_line": line,
        "start_column": start_column,
        "end_column": start_column + len(excerpt.encode("utf-8")),
        "excerpt": excerpt,
        "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    intent = "requested" if source_kind == "issue" else "preservation"
    identifier_payload = {
        "intent": intent,
        "source": source,
        "source_kind": source_kind,
        "statement": statement,
    }
    canonical = json.dumps(
        identifier_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "id": "cand-" + hashlib.sha256(canonical).hexdigest(),
        "statement": statement,
        "intent": intent,
        "source_kind": source_kind,
        "source": source,
        "review_status": "requires_review",
        "confidence": "high",
        "rationale": "Exact source text provides a reviewable behavior candidate.",
    }


def test_reviewed_requirements_load_directly_into_evidence_plan(
    tmp_path: Path,
) -> None:
    issue_text = (
        "# Resolver behavior\n"
        "\n"
        "Explicit zero must be returned unchanged.\n"
    )
    issue_source = "customer-requests/zero-default.contract"
    candidates = [
        _candidate(
            excerpt="Explicit zero must be returned unchanged.",
            source_kind="issue",
            path=issue_source,
            line=3,
        ),
        _candidate(
            excerpt="assert resolve(None) == 5",
            source_kind="pytest_assertion",
            path="tests/test_resolve.py",
            line=7,
        ),
        _candidate(
            excerpt="assert resolve(3) == 3",
            source_kind="pytest_assertion",
            path="tests/test_resolve.py",
            line=11,
        ),
    ]
    draft = {
        "schema_version": 1,
        "tool_version": "0.4.0",
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.1",
            "platform": "test",
        },
        "inputs": {
            "repository": "/work/resolver",
            "base_revision": "HEAD",
            "base_sha": "b" * 40,
            "base_context_sha256": "a" * 64,
            "patched_tree_hash": "c" * 40,
            "patch_sha256": "d" * 64,
            "issue_sha256": hashlib.sha256(issue_text.encode("utf-8")).hexdigest(),
            "issue_source": issue_source,
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": candidates,
            "diagnostics": [],
            "limitations": ["Every candidate requires an explicit human decision."],
            "truncated": False,
        },
    }
    validate_requirement_draft_artifact(draft)
    draft_document = (json.dumps(draft, sort_keys=True) + "\n").encode("utf-8")

    review = prepare_requirement_review(draft_document)
    review["decisions"][0].update(
        action="accept",
        rationale="The issue explicitly requires this behavior.",
    )
    review["decisions"][1].update(
        action="edit",
        rationale="Clarify the preserved behavior without changing its source basis.",
        replacement={
            "statement": "A missing value preserves the base default of five.",
            "kind": "preservation",
            "provenance": "preexisting",
        },
    )
    review["decisions"][2].update(
        action="reject",
        rationale="This neighboring behavior is outside the reviewed scope.",
    )

    finalized = finalize_requirement_review(
        draft_document,
        encode_review_artifact(review),
    )

    assert finalized["audit"]["accepted_count"] == 1
    assert finalized["audit"]["edited_count"] == 1
    assert finalized["audit"]["rejected_count"] == 1
    assert len(finalized["requirements"]) == 2
    assert finalized["requirements"][0]["source"] == "issue:3"
    assert all(len(requirement["id"]) <= 64 for requirement in finalized["requirements"])

    (tmp_path / "candidate_zero.py").write_text(
        "def test_zero_is_preserved():\n"
        "    assert resolve(0) == 0\n",
        encoding="utf-8",
    )
    (tmp_path / "candidate_default.py").write_text(
        "def test_missing_uses_five():\n"
        "    assert resolve(None) == 5\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "evidence-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requirements": finalized["requirements"],
                "cases": [
                    {
                        "id": "case-zero",
                        "requirement_id": finalized["requirements"][0]["id"],
                        "test_file": "candidate_zero.py",
                        "node": "test_zero_is_preserved",
                        "rationale": "Challenges the requested zero-value behavior.",
                    },
                    {
                        "id": "case-default",
                        "requirement_id": finalized["requirements"][1]["id"],
                        "test_file": "candidate_default.py",
                        "node": "test_missing_uses_five",
                        "rationale": "Preserves the reviewed base default behavior.",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    plan = load_evidence_plan(plan_path, issue_text=issue_text)

    assert [requirement.id for requirement in plan.requirements] == [
        requirement["id"] for requirement in finalized["requirements"]
    ]
    assert plan.requirements[0].source == "issue:3"
    assert all(len(requirement.id) <= 64 for requirement in plan.requirements)
    assert {case.requirement_id for case in plan.cases} == {
        requirement.id for requirement in plan.requirements
    }
