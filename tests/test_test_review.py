from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import patch_verifier.plan as plan_module
import patch_verifier.test_review as review_module
import patch_verifier.verifier as verifier_module
from patch_verifier.materialization import (
    TestMaterializationError as MaterializationError,
    authenticate_test_materialization,
)
from patch_verifier.plan import load_evidence_plan
from patch_verifier.review import (
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
)
from patch_verifier.test_proposals import (
    build_test_proposal_artifact,
    encode_test_proposal_artifact,
)
from patch_verifier.test_review import (
    TestProposalReviewError as ReviewError,
    encode_test_review_artifact,
    finalize_test_proposal_review,
    materialize_reviewed_test_proposal_files,
    materialize_reviewed_test_proposals,
    prepare_test_proposal_review,
    validate_reviewed_test_proposals_artifact,
    validate_reviewed_test_proposals_structure,
    validate_test_materialization_artifact,
    validate_test_proposal_review_artifact,
    write_reviewed_test_proposals,
    write_test_proposal_review,
)
from patch_verifier.verifier import (
    request_from_materialization,
    verify,
)


ISSUE = "# Resolver\n\nExplicit zero must be returned unchanged.\n"


@pytest.mark.parametrize(
    "schema_name",
    [
        "reviewed-requirements.schema.json",
        "test-proposals.schema.json",
        "test-proposal-review.schema.json",
        "reviewed-test-proposals.schema.json",
        "test-materialization.schema.json",
    ],
)
def test_workflow_schemas_reject_sha256_with_trailing_newline(
    schema_name: str,
) -> None:
    resource = files("patch_verifier").joinpath("schemas").joinpath(schema_name)
    schema = json.loads(resource.read_text(encoding="utf-8"))
    digest_validator = Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/sha256"}
    )
    assert digest_validator.is_valid("0" * 64)
    assert not digest_validator.is_valid("0" * 64 + "\n")


@dataclass(frozen=True)
class WorkflowInputs:
    reviewed: bytes
    response: bytes
    proposals: bytes
    issue: bytes


def _candidate(
    *, excerpt: str, source_kind: str, path: str, line: int
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
    identity = {
        "intent": intent,
        "source": source,
        "source_kind": source_kind,
        "statement": statement,
    }
    candidate_id = "cand-" + hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "id": candidate_id,
        "statement": statement,
        "intent": intent,
        "source_kind": source_kind,
        "source": source,
        "review_status": "requires_review",
        "confidence": "high",
        "rationale": "Exact source text supports this candidate.",
    }


def _reviewed_requirements() -> bytes:
    candidates = [
        _candidate(
            excerpt="Explicit zero must be returned unchanged.",
            source_kind="issue",
            path="request.md",
            line=3,
        ),
        _candidate(
            excerpt="assert resolve(None) == 5",
            source_kind="pytest_assertion",
            path="tests/test_resolve.py",
            line=7,
        ),
    ]
    draft = {
        "schema_version": 1,
        "tool_version": "0.5.0",
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
            "issue_sha256": hashlib.sha256(ISSUE.encode()).hexdigest(),
            "issue_source": "request.md",
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": candidates,
            "diagnostics": [],
            "limitations": ["Every candidate requires review."],
            "truncated": False,
        },
    }
    draft_bytes = encode_review_artifact(draft)
    review = prepare_requirement_review(draft_bytes)
    for decision in review["decisions"]:
        decision.update(action="accept", rationale="Accepted source evidence.")
    finalized = finalize_requirement_review(
        draft_bytes, encode_review_artifact(review)
    )
    return encode_review_artifact(finalized)


def _raw(
    requirement_id: str,
    *,
    category: str,
    test_file: str,
    node: str,
    expression: str,
    prefix: str = "",
) -> dict[str, str]:
    return {
        "requirement_id": requirement_id,
        "category": category,
        "test_file": test_file,
        "node": node,
        "rationale": f"Exercise {node} against reviewed behavior.",
        "source": (
            f"{prefix}from value import resolve\n\n"
            f"def {node}():\n"
            f"    assert {expression}\n"
        ),
    }


@pytest.fixture
def workflow() -> WorkflowInputs:
    reviewed = _reviewed_requirements()
    requirement_ids = [
        item["id"] for item in json.loads(reviewed)["requirements"]
    ]
    raw_proposals = [
        _raw(
            requirement_ids[0],
            category="boundary",
            test_file="generated/test_zero.py",
            node="test_zero",
            expression="resolve(0) == 0",
        ),
        _raw(
            requirement_ids[0],
            category="caller_level",
            test_file="generated/test_three.py",
            node="test_three",
            expression="resolve(3) == 3",
        ),
        _raw(
            requirement_ids[1],
            category="preservation",
            test_file="generated/test_missing.py",
            node="test_missing",
            expression="resolve(None) == 5",
        ),
    ]
    response = json.dumps(
        {"schema_version": 1, "proposals": raw_proposals},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    artifact = build_test_proposal_artifact(
        reviewed,
        generator_adapter="fixture",
        generator_model="fixture-model",
        proposals=raw_proposals,
        generator_response_document=response,
    )
    return WorkflowInputs(
        reviewed=reviewed,
        response=response,
        proposals=encode_test_proposal_artifact(artifact),
        issue=ISSUE.encode("utf-8"),
    )


def _terminal_review(workflow: WorkflowInputs) -> tuple[dict[str, Any], bytes]:
    review = prepare_test_proposal_review(
        workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    for decision in review["decisions"]:
        path = decision["proposal"]["test_file"]
        if path.endswith("test_zero.py"):
            decision.update(
                action="edit",
                rationale="Use a clearer accepted test name.",
                replacement={
                    "test_file": "accepted/test_zero_edited.py",
                    "node": "test_explicit_zero",
                    "rationale": "Checks explicit zero after human review.",
                    "source": (
                        "from value import resolve\n\n"
                        "def test_explicit_zero():\n"
                        "    assert resolve(0) == 0\n"
                    ),
                },
            )
        elif path.endswith("test_three.py"):
            decision.update(
                action="reject",
                rationale="This case is redundant for the first review pass.",
            )
        else:
            decision.update(
                action="accept",
                rationale="The proposal directly preserves reviewed behavior.",
            )
    return review, encode_test_review_artifact(review)


def _materialized_terminal_review(
    workflow: WorkflowInputs,
    output: Path,
) -> tuple[dict[str, Any], bytes]:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    materialize_reviewed_test_proposals(
        finalized_bytes,
        proposal_document=workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        review_document=review_bytes,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
        output_directory=output,
    )
    return finalized, finalized_bytes


def test_prepare_is_deterministic_self_contained_and_exactly_bound(
    workflow: WorkflowInputs,
) -> None:
    first = prepare_test_proposal_review(
        workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    second = prepare_test_proposal_review(
        workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )

    assert first == second
    validate_test_proposal_review_artifact(first)
    assert first["proposal_artifact"] == {
        "sha256": hashlib.sha256(workflow.proposals).hexdigest(),
        "schema_version": 2,
        "proposal_count": 3,
    }
    assert first["issue"] == {
        "sha256": hashlib.sha256(workflow.issue).hexdigest(),
        "size": len(workflow.issue),
    }
    assert first["reviewed_requirements"]["sha256"] == hashlib.sha256(
        workflow.reviewed
    ).hexdigest()
    assert first["generator_response"]["supplied_sha256"] == hashlib.sha256(
        workflow.response
    ).hexdigest()
    assert all(item["action"] == "pending" for item in first["decisions"])
    assert [item["proposal"] for item in first["decisions"]] == json.loads(
        workflow.proposals
    )["proposals"]
    floating_version = copy.deepcopy(first)
    floating_version["schema_version"] = 1.0
    with pytest.raises(ReviewError, match="schema_version: expected integer"):
        validate_test_proposal_review_artifact(floating_version)


def test_exact_response_is_required_and_reauthenticated(
    workflow: WorkflowInputs,
) -> None:
    with pytest.raises(ReviewError, match="original generator response"):
        prepare_test_proposal_review(
            workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
        )
    changed = json.dumps(json.loads(workflow.response), indent=2).encode()
    with pytest.raises(ReviewError, match="exact supplied|invalid"):
        prepare_test_proposal_review(
            workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=changed,
        )


def test_issue_bytes_are_exactly_bound_from_reviewed_requirements(
    workflow: WorkflowInputs,
) -> None:
    altered_issue = workflow.issue + b"\nContradictory new instruction.\n"
    with pytest.raises(ReviewError, match="issue SHA-256"):
        prepare_test_proposal_review(
            workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=altered_issue,
            generator_response_document=workflow.response,
        )
    _, review_bytes = _terminal_review(workflow)
    with pytest.raises(ReviewError, match="issue SHA-256"):
        finalize_test_proposal_review(
            workflow.proposals,
            review_bytes,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=altered_issue,
            generator_response_document=workflow.response,
        )


def test_review_rejects_legacy_requirements_without_issue_provenance(
    workflow: WorkflowInputs,
) -> None:
    legacy_reviewed = json.loads(workflow.reviewed)
    legacy_reviewed["schema_version"] = 1
    legacy_reviewed.pop("issue")
    legacy_reviewed_bytes = encode_review_artifact(legacy_reviewed)
    raw_proposals = json.loads(workflow.response)["proposals"]
    proposal = build_test_proposal_artifact(
        legacy_reviewed_bytes,
        generator_adapter="fixture",
        generator_model="fixture-model",
        proposals=raw_proposals,
        generator_response_document=workflow.response,
    )
    with pytest.raises(ReviewError, match="exact issue binding"):
        prepare_test_proposal_review(
            encode_test_proposal_artifact(proposal),
            reviewed_requirements_document=legacy_reviewed_bytes,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )

def test_canonical_proposals_can_be_reviewed_without_response_bytes(
    workflow: WorkflowInputs,
) -> None:
    response = json.loads(workflow.response)
    canonical_artifact = build_test_proposal_artifact(
        workflow.reviewed,
        generator_adapter="fixture",
        generator_model=None,
        proposals=response["proposals"],
    )
    proposal_bytes = encode_test_proposal_artifact(canonical_artifact)

    review = prepare_test_proposal_review(
        proposal_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
    )

    assert review["generator_response"]["binding"] == "canonical"
    assert review["generator_response"]["supplied_sha256"] is None
    for decision in review["decisions"]:
        decision.update(action="reject", rationale="Reviewed and rejected.")
    finalized = finalize_test_proposal_review(
        proposal_bytes,
        encode_test_review_artifact(review),
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
    )
    assert finalized["audit"]["rejected_count"] == 3


def test_finalization_revalidates_edit_and_emits_v2_plan_projection(
    workflow: WorkflowInputs,
) -> None:
    review, review_bytes = _terminal_review(workflow)

    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )

    validate_reviewed_test_proposals_artifact(
        finalized,
        proposal_document=workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        review_document=review_bytes,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    assert finalized["review"]["sha256"] == hashlib.sha256(
        review_bytes
    ).hexdigest()
    assert finalized["requirements"] == json.loads(workflow.reviewed)[
        "requirements"
    ]
    assert finalized["audit"]["accepted_count"] == 1
    assert finalized["audit"]["edited_count"] == 1
    assert finalized["audit"]["rejected_count"] == 1
    assert len(finalized["cases"]) == 2
    edited = next(
        item for item in finalized["accepted_cases"] if item["review_action"] == "edit"
    )
    origin = next(
        item["proposal"]
        for item in review["decisions"]
        if item["proposal_id"] == edited["origin_proposal_id"]
    )
    assert edited["id"] != edited["origin_proposal_id"]
    assert edited["source"]["sha256"] == hashlib.sha256(
        edited["source"]["text"].encode()
    ).hexdigest()
    assert edited["assertion"]["excerpt"] == "assert resolve(0) == 0"
    assert edited["assertion"]["requirement"] == origin["assertion"]["requirement"]
    for plan_case, effective in zip(
        finalized["cases"], finalized["accepted_cases"]
    ):
        assert plan_case["test_sha256"] == effective["source"]["sha256"]
    assert finalized["coverage"]["accepted_requirement_ids"] == [
        item["id"] for item in finalized["requirements"]
    ]
    assert finalized["coverage"]["unaccepted_requirement_ids"] == []
    floating_version = copy.deepcopy(finalized)
    floating_version["schema_version"] = 1.0
    with pytest.raises(ReviewError, match="schema_version: expected integer"):
        validate_reviewed_test_proposals_structure(floating_version)


def test_review_validator_rejects_unsafe_edit_path(
    workflow: WorkflowInputs,
) -> None:
    review, _ = _terminal_review(workflow)
    edited = next(
        decision for decision in review["decisions"] if decision["action"] == "edit"
    )
    edited["replacement"]["test_file"] = "../escape.py"

    with pytest.raises(ReviewError, match="canonical safe relative .py path"):
        validate_test_proposal_review_artifact(review)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            lambda ledger: ledger["proposal_artifact"].update(sha256="0" * 64),
            "exact proposal artifact bytes",
        ),
        (
            lambda ledger: ledger["reviewed_requirements"].update(sha256="0" * 64),
            "reviewed-requirements binding",
        ),
        (
            lambda ledger: ledger["generator_response"].update(
                supplied_sha256="0" * 64
            ),
            "original response bytes|generator-response binding",
        ),
        (
            lambda ledger: ledger["decisions"][0]["proposal"]["source"].update(
                sha256="0" * 64
            ),
            "source.sha256|preview does not exactly match",
        ),
    ],
)
def test_finalization_rejects_binding_and_preview_tampering(
    workflow: WorkflowInputs,
    mutation: Any,
    match: str,
) -> None:
    review, _ = _terminal_review(workflow)
    mutation(review)
    with pytest.raises(ReviewError, match=match):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(review),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )


def test_finalization_rejects_pending_reordered_and_missing_decisions(
    workflow: WorkflowInputs,
) -> None:
    pending = prepare_test_proposal_review(
        workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    with pytest.raises(ReviewError, match="pending decision"):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(pending),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )

    complete, _ = _terminal_review(workflow)
    reordered = copy.deepcopy(complete)
    reordered["decisions"][0], reordered["decisions"][1] = (
        reordered["decisions"][1],
        reordered["decisions"][0],
    )
    with pytest.raises(ReviewError, match="deterministic proposal order"):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(reordered),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )

    missing = copy.deepcopy(complete)
    missing["decisions"].pop()
    with pytest.raises(ReviewError, match="proposal_count"):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(missing),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )


def test_review_rules_require_rationale_replacement_and_real_edit(
    workflow: WorkflowInputs,
) -> None:
    complete, _ = _terminal_review(workflow)

    no_rationale = copy.deepcopy(complete)
    no_rationale["decisions"][0]["rationale"] = ""
    with pytest.raises(ReviewError, match="rationale|expected constant"):
        validate_test_proposal_review_artifact(no_rationale)

    accept_replacement = copy.deepcopy(complete)
    accepted = next(
        item for item in accept_replacement["decisions"] if item["action"] == "accept"
    )
    accepted["replacement"] = {
        "test_file": "x.py",
        "node": "test_x",
        "rationale": "x",
        "source": "def test_x():\n    assert True\n",
    }
    with pytest.raises(ReviewError, match="replacement|property|expected constant"):
        validate_test_proposal_review_artifact(accept_replacement)

    no_op = copy.deepcopy(complete)
    edited = next(item for item in no_op["decisions"] if item["action"] == "edit")
    proposal = edited["proposal"]
    edited["replacement"] = {
        "test_file": proposal["test_file"],
        "node": proposal["node"],
        "rationale": proposal["rationale"],
        "source": proposal["source"]["text"],
    }
    with pytest.raises(ReviewError, match="does not change"):
        validate_test_proposal_review_artifact(no_op)

    retarget = copy.deepcopy(complete)
    retarget_edit = next(
        item for item in retarget["decisions"] if item["action"] == "edit"
    )
    retarget_edit["replacement"]["category"] = "state_sequence"
    with pytest.raises(ReviewError, match="property is not allowed"):
        validate_test_proposal_review_artifact(retarget)


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("def test_bad(:\n    assert True\n", "parseable Python|invalid"),
        ("def test_bad():\n    pass\n", "exactly one Python assert"),
        (
            "# coding: latin-1\n"
            "def test_bad():\n"
            "    assert 'é' == 'é'\n",
            "UTF-8 encoding|invalid",
        ),
        (
            "def helper():\n    assert True\n\n"
            "def test_bad():\n    assert True\n",
            "exactly one Python assert",
        ),
    ],
)
def test_edit_source_is_statically_revalidated(
    workflow: WorkflowInputs,
    source: str,
    match: str,
) -> None:
    complete, _ = _terminal_review(workflow)
    edited = next(item for item in complete["decisions"] if item["action"] == "edit")
    edited["replacement"].update(node="test_bad", source=source)
    with pytest.raises(ReviewError, match=match):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(complete),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )


@pytest.mark.parametrize(
    ("paths", "match"),
    [
        (
            ["accepted/test_same.py", "ACCEPTED/test_same.py"],
            "portable collision|directory spelling collision",
        ),
        (["accepted.py", "accepted.py/test_nested.py"], "file/ancestor"),
        (["A/test_one.py", "a/test_two.py"], "directory spelling collision"),
        (["evidence-plan.json/test_one.py", "ok/test_two.py"], "reserved"),
        (
            ["/".join(["d"] * 33 + ["test_one.py"]), "ok/test_two.py"],
            "at most 32 components|32 path components|invalid",
        ),
    ],
)
def test_effective_paths_reject_portable_and_reserved_conflicts(
    workflow: WorkflowInputs,
    paths: list[str],
    match: str,
) -> None:
    complete, _ = _terminal_review(workflow)
    editable = [
        item
        for item in complete["decisions"]
        if item["action"] in {"accept", "edit"}
    ]
    for index, (decision, path) in enumerate(zip(editable, paths)):
        proposal = decision["proposal"]
        decision.update(
            action="edit",
            rationale="Change the materialized path for this review.",
            replacement={
                "test_file": path,
                "node": f"test_path_{index}",
                "rationale": "Path collision regression case.",
                "source": (
                    "from value import resolve\n\n"
                    f"def test_path_{index}():\n"
                    f"    assert resolve({index}) == {index}\n"
                ),
            },
        )
        assert proposal["requirement_id"] == decision["proposal"]["requirement_id"]
    with pytest.raises(ReviewError, match=match):
        finalize_test_proposal_review(
            workflow.proposals,
            encode_test_review_artifact(complete),
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )


def test_all_rejected_is_auditable_but_not_materializable(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    review = prepare_test_proposal_review(
        workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    for decision in review["decisions"]:
        decision.update(
            action="reject",
            rationale="Rejected after human inspection.",
        )
    review_bytes = encode_test_review_artifact(review)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    assert finalized["cases"] == []
    assert finalized["accepted_cases"] == []
    assert finalized["requirements"] == json.loads(workflow.reviewed)["requirements"]
    output = tmp_path / "stage"
    with pytest.raises(ReviewError, match="no accepted cases"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert not output.exists()


def test_materialization_freezes_exact_bytes_and_never_executes_candidate(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    review, review_bytes = _terminal_review(workflow)
    accepted = next(item for item in review["decisions"] if item["action"] == "accept")
    marker = tmp_path / "must-not-exist"
    proposal = accepted["proposal"]
    accepted.update(
        action="edit",
        rationale="Add an inert import-time marker for the no-execution test.",
        replacement={
            "test_file": proposal["test_file"],
            "node": proposal["node"],
            "rationale": proposal["rationale"],
            "source": (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "from value import resolve\n\n"
                f"def {proposal['node']}():\n"
                "    assert resolve(None) == 5\n"
            ),
        },
    )
    review_bytes = encode_test_review_artifact(review)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    output = tmp_path / "stage"

    result = materialize_reviewed_test_proposals(
        finalized_bytes,
        proposal_document=workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        review_document=review_bytes,
        generator_response_document=workflow.response,
        issue_document=workflow.issue,
        output_directory=output,
    )

    assert not marker.exists()
    assert result.case_count == 2
    assert (output / "issue.md").read_bytes() == workflow.issue
    assert (output / "reviewed-test-proposals.json").read_bytes() == finalized_bytes
    plan_bytes = (output / "evidence-plan.json").read_bytes()
    plan = json.loads(plan_bytes)
    assert plan["schema_version"] == 2
    assert plan["requirements"] == json.loads(workflow.reviewed)["requirements"]
    loaded = load_evidence_plan(
        output / "evidence-plan.json",
        issue_text=workflow.issue.decode(),
    )
    assert loaded.schema_version == 2
    manifest = json.loads((output / "materialization.json").read_bytes())
    validate_test_materialization_artifact(manifest)
    assert manifest["complete"] is True
    assert manifest["evidence_plan"]["sha256"] == hashlib.sha256(
        plan_bytes
    ).hexdigest()
    assert manifest["evidence_plan"]["size"] == len(plan_bytes)
    assert manifest["issue"]["sha256"] == hashlib.sha256(
        workflow.issue
    ).hexdigest()
    assert manifest["reviewed_test_proposals"]["sha256"] == hashlib.sha256(
        finalized_bytes
    ).hexdigest()
    for case in finalized["accepted_cases"]:
        assert (output / case["test_file"]).read_bytes() == case["source"][
            "text"
        ].encode("utf-8")

    invalid_manifest = copy.deepcopy(manifest)
    invalid_manifest["files"][0]["path"] = "../escape.py"
    with pytest.raises(ReviewError, match="canonical safe"):
        validate_test_materialization_artifact(invalid_manifest)
    floating_manifest = copy.deepcopy(manifest)
    floating_manifest["schema_version"] = 1.0
    with pytest.raises(ReviewError, match="schema_version: expected integer"):
        validate_test_materialization_artifact(floating_manifest)


def test_materialization_is_exclusive_and_manifest_is_last_commit_marker(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    output = tmp_path / "incomplete"
    original = review_module._write_new_file

    def fail_on_plan(
        root_descriptor: int,
        relative: Any,
        document: bytes,
        *,
        display_root: Path,
    ) -> None:
        if relative.name == "evidence-plan.json":
            raise ReviewError("simulated plan write failure")
        original(
            root_descriptor,
            relative,
            document,
            display_root=display_root,
        )

    monkeypatch.setattr(review_module, "_write_new_file", fail_on_plan)
    with pytest.raises(ReviewError, match="simulated"):
        materialize_reviewed_test_proposals(
            finalized_bytes,
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert output.is_dir()
    assert not (output / "materialization.json").exists()

    monkeypatch.setattr(review_module, "_write_new_file", original)
    with pytest.raises(ReviewError, match="already exists"):
        materialize_reviewed_test_proposals(
            finalized_bytes,
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )


def test_candidate_write_is_not_redirected_by_nested_parent_replacement(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    output = tmp_path / "parent-swap-stage"
    installed_parent = output / "accepted"
    displaced_parent = tmp_path / "held-original-parent"
    alternate_parent = tmp_path / "attacker-parent"
    alternate_parent.mkdir()
    original_open = review_module.os.open
    swapped = False

    def swap_parent_before_candidate_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == "test_zero_edited.py"
            and flags & os.O_CREAT
            and dir_fd is not None
        ):
            swapped = True
            installed_parent.rename(displaced_parent)
            alternate_parent.rename(installed_parent)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        review_module.os,
        "open",
        swap_parent_before_candidate_open,
    )
    with pytest.raises(ReviewError, match="missing path"):
        materialize_reviewed_test_proposals(
            finalized_bytes,
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )

    assert swapped is True
    assert not (installed_parent / "test_zero_edited.py").exists()
    assert not (output / "materialization.json").exists()


def test_materialization_fails_closed_without_secure_dir_fd_support(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "unsupported-stage"
    monkeypatch.setattr(review_module, "_SECURE_DIR_FD_AVAILABLE", False)

    with pytest.raises(ReviewError, match="secure materialization is unavailable"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )

    assert not output.exists()


def test_materialization_rejects_issue_drift_before_reserving_output(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "stage"
    with pytest.raises(ReviewError, match="issue SHA-256"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=b"# Different issue\n",
            output_directory=output,
        )
    assert not output.exists()


def test_precommit_inventory_rejects_staged_source_tampering(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    output = tmp_path / "tampered-stage"
    original_verify = review_module._verify_staging_before_commit

    def tamper_then_verify(
        root_descriptor: int,
        root: Path,
        manifest: dict[str, Any],
        *,
        expected_root_identity: tuple[int, int],
    ) -> None:
        first_path = manifest["files"][0]["path"]
        root.joinpath(*first_path.split("/")).write_bytes(b"tampered\n")
        original_verify(
            root_descriptor,
            root,
            manifest,
            expected_root_identity=expected_root_identity,
        )

    monkeypatch.setattr(
        review_module, "_verify_staging_before_commit", tamper_then_verify
    )
    with pytest.raises(ReviewError, match="changed before manifest commit"):
        materialize_reviewed_test_proposals(
            finalized_bytes,
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert output.is_dir()
    assert not (output / "materialization.json").exists()


def test_precommit_inventory_rejects_unexpected_empty_directory(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "extra-directory-stage"
    original_verify = review_module._verify_staging_before_commit

    def add_directory_then_verify(
        root_descriptor: int,
        root: Path,
        manifest: dict[str, Any],
        *,
        expected_root_identity: tuple[int, int],
    ) -> None:
        (root / "unexpected-empty-directory").mkdir()
        original_verify(
            root_descriptor,
            root,
            manifest,
            expected_root_identity=expected_root_identity,
        )

    monkeypatch.setattr(
        review_module, "_verify_staging_before_commit", add_directory_then_verify
    )
    with pytest.raises(ReviewError, match="unexpected directory"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert output.is_dir()
    assert not (output / "materialization.json").exists()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"),
    reason="FIFO probe requires POSIX nonblocking file support",
)
def test_precommit_rehash_rejects_fifo_without_blocking(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "fifo-stage"
    original_read = review_module._read_staged_regular_file
    replaced = False

    def replace_with_fifo(
        root_descriptor: int, relative: str, *, max_bytes: int
    ) -> bytes:
        nonlocal replaced
        if not replaced and "/" in relative:
            replaced = True
            target = output.joinpath(*relative.split("/"))
            target.unlink()
            os.mkfifo(target)
        return original_read(root_descriptor, relative, max_bytes=max_bytes)

    monkeypatch.setattr(
        review_module, "_read_staged_regular_file", replace_with_fifo
    )
    with pytest.raises(ReviewError, match="not a regular file"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert not (output / "materialization.json").exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="parent-component probe requires secure dir-fd traversal",
)
def test_precommit_rehash_rejects_parent_symlink_swap(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "parent-symlink-stage"
    external = tmp_path / "external"
    original_read = review_module._read_staged_regular_file
    swapped = False
    swapped_parent: Path | None = None

    def swap_parent_to_symlink(
        root_descriptor: int, relative: str, *, max_bytes: int
    ) -> bytes:
        nonlocal swapped, swapped_parent
        if not swapped and "/" in relative:
            swapped = True
            parts = relative.split("/")
            parent = output.joinpath(*parts[:-1])
            swapped_parent = parent
            source = parent / parts[-1]
            source_bytes = source.read_bytes()
            source.unlink()
            parent.rmdir()
            external.mkdir()
            (external / parts[-1]).write_bytes(source_bytes)
            os.symlink(external, parent)
        return original_read(root_descriptor, relative, max_bytes=max_bytes)

    monkeypatch.setattr(
        review_module, "_read_staged_regular_file", swap_parent_to_symlink
    )
    with pytest.raises(ReviewError, match="without following links"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert swapped_parent is not None and swapped_parent.is_symlink()
    assert not (output / "materialization.json").exists()


def test_manifest_fsync_failure_leaves_no_completion_marker(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)
    output = tmp_path / "fsync-failure"
    original_fsync = review_module.os.fsync
    calls = 0

    def fail_manifest_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == len(finalized["accepted_cases"]) + 4:
            raise OSError("simulated manifest fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(review_module.os, "fsync", fail_manifest_fsync)
    with pytest.raises(ReviewError, match="simulated manifest fsync failure"):
        materialize_reviewed_test_proposals(
            finalized_bytes,
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert output.is_dir()
    assert not (output / "materialization.json").exists()
    assert list(output.glob(".materialization.json.*.tmp")) == []


def test_manifest_temp_cleanup_failure_removes_completion_marker(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "temp-cleanup-failure"
    original_unlink = review_module.os.unlink

    def fail_temp_unlink(
        path: str | bytes | int,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if str(path).startswith(".materialization.json."):
            raise OSError("simulated manifest temp cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(review_module.os, "unlink", fail_temp_unlink)
    with pytest.raises(ReviewError, match="temp cleanup failure"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert output.is_dir()
    assert not (output / "materialization.json").exists()


def test_manifest_publication_never_recreates_deleted_staging_root(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "removed-before-commit"
    original_publish = review_module._write_manifest_exclusive

    def remove_root_then_publish(
        root_descriptor: int,
        path: Path,
        document: bytes,
        *,
        expected_parent_identity: tuple[int, int],
    ) -> None:
        shutil.rmtree(path.parent)
        original_publish(
            root_descriptor,
            path,
            document,
            expected_parent_identity=expected_parent_identity,
        )

    monkeypatch.setattr(
        review_module, "_write_manifest_exclusive", remove_root_then_publish
    )
    with pytest.raises(ReviewError, match="cannot inspect reserved"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert not output.exists()


def test_manifest_publication_rejects_staging_root_substitution(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "substituted-before-commit"
    displaced = tmp_path / "original-stage"
    original_publish = review_module._write_manifest_exclusive

    def substitute_root_then_publish(
        root_descriptor: int,
        path: Path,
        document: bytes,
        *,
        expected_parent_identity: tuple[int, int],
    ) -> None:
        path.parent.rename(displaced)
        path.parent.mkdir()
        original_publish(
            root_descriptor,
            path,
            document,
            expected_parent_identity=expected_parent_identity,
        )

    monkeypatch.setattr(
        review_module,
        "_write_manifest_exclusive",
        substitute_root_then_publish,
    )
    with pytest.raises(ReviewError, match="changed before manifest commit"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    assert not (output / "materialization.json").exists()
    assert not (displaced / "materialization.json").exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_materialization_never_overwrites_existing_output(
    workflow: WorkflowInputs,
    tmp_path: Path,
    kind: str,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    output = tmp_path / "occupied"
    if kind == "file":
        output.write_text("keep", encoding="utf-8")
    elif kind == "directory":
        output.mkdir()
        (output / "keep").write_text("keep", encoding="utf-8")
    else:
        target = tmp_path / "link-target"
        target.mkdir()
        os.symlink(target, output)

    with pytest.raises(ReviewError, match="already exists"):
        materialize_reviewed_test_proposals(
            encode_test_review_artifact(finalized),
            proposal_document=workflow.proposals,
            reviewed_requirements_document=workflow.reviewed,
            review_document=review_bytes,
            generator_response_document=workflow.response,
            issue_document=workflow.issue,
            output_directory=output,
        )
    if kind == "file":
        assert output.read_text(encoding="utf-8") == "keep"
    elif kind == "directory":
        assert (output / "keep").read_text(encoding="utf-8") == "keep"


def test_file_wrappers_are_exact_digest_bound_and_no_overwrite(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    proposal_file = tmp_path / "proposals.json"
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    review_file = tmp_path / "test-review.json"
    final_file = tmp_path / "reviewed-tests.json"
    issue_file = tmp_path / "issue.txt"
    proposal_file.write_bytes(workflow.proposals)
    reviewed_file.write_bytes(workflow.reviewed)
    response_file.write_bytes(workflow.response)
    issue_file.write_bytes(workflow.issue)

    prepared = write_test_proposal_review(
        proposal_file,
        reviewed_file,
        issue_file,
        review_file,
        generator_response_file=response_file,
        expected_proposal_sha256=hashlib.sha256(workflow.proposals).hexdigest(),
    )
    assert prepared.proposal_count == 3
    ledger = json.loads(review_file.read_bytes())
    for decision in ledger["decisions"]:
        decision.update(action="accept", rationale="Accepted after review.")
    review_file.write_bytes(encode_test_review_artifact(ledger))
    finalized = write_reviewed_test_proposals(
        proposal_file,
        reviewed_file,
        review_file,
        issue_file,
        final_file,
        generator_response_file=response_file,
        expected_review_sha256=hashlib.sha256(review_file.read_bytes()).hexdigest(),
    )
    assert finalized.accepted_case_count == 3
    stage = tmp_path / "stage"
    materialized = materialize_reviewed_test_proposal_files(
        final_file,
        proposal_file,
        reviewed_file,
        review_file,
        issue_file,
        stage,
        generator_response_file=response_file,
        expected_issue_sha256=hashlib.sha256(workflow.issue).hexdigest(),
    )
    assert materialized.case_count == 3
    with pytest.raises(ReviewError, match="already exists"):
        write_test_proposal_review(
            proposal_file,
            reviewed_file,
            issue_file,
            review_file,
            generator_response_file=response_file,
        )
    stale = tmp_path / "stale.json"
    with pytest.raises(ReviewError, match="earlier analysis pass"):
        write_test_proposal_review(
            proposal_file,
            reviewed_file,
            issue_file,
            stale,
            generator_response_file=response_file,
            expected_proposal_sha256="0" * 64,
        )
    assert not stale.exists()


def test_final_validator_rejects_plan_source_or_audit_tampering(
    workflow: WorkflowInputs,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    for changed in (
        _mutated(finalized, ("cases", 0, "test_sha256"), "0" * 64),
        _mutated(finalized, ("accepted_cases", 0, "source", "sha256"), "0" * 64),
        _mutated(finalized, ("audit", "accepted_count"), 99),
    ):
        with pytest.raises(ReviewError, match="does not exactly match|maximum"):
            validate_reviewed_test_proposals_artifact(
                changed,
                proposal_document=workflow.proposals,
                reviewed_requirements_document=workflow.reviewed,
                review_document=review_bytes,
                issue_document=workflow.issue,
                generator_response_document=workflow.response,
            )


def _mutated(
    value: dict[str, Any], path: tuple[object, ...], replacement: object
) -> dict[str, Any]:
    changed = copy.deepcopy(value)
    target: Any = changed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    return changed


def test_duplicate_keys_nonfinite_json_and_resource_limits_fail_closed(
    workflow: WorkflowInputs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review, review_bytes = _terminal_review(workflow)
    duplicate = review_bytes.replace(
        b'{\n  "artifact_type"',
        b'{\n  "artifact_type": "test-proposal-review",\n  "artifact_type"',
        1,
    )
    with pytest.raises(ReviewError, match="duplicate JSON object key"):
        finalize_test_proposal_review(
            workflow.proposals,
            duplicate,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )

    nonfinite = encode_test_review_artifact(review).replace(
        b'"schema_version": 1', b'"schema_version": NaN', 1
    )
    with pytest.raises(ReviewError, match="safely parseable JSON"):
        finalize_test_proposal_review(
            workflow.proposals,
            nonfinite,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )

    monkeypatch.setattr(review_module, "_MAX_REVIEW_DOCUMENT_BYTES", len(review_bytes) - 1)
    with pytest.raises(ReviewError, match="byte limit"):
        finalize_test_proposal_review(
            workflow.proposals,
            review_bytes,
            reviewed_requirements_document=workflow.reviewed,
            issue_document=workflow.issue,
            generator_response_document=workflow.response,
        )


def test_dangling_output_symlink_is_never_followed(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    proposal_file = tmp_path / "proposals.json"
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    issue_file = tmp_path / "issue.md"
    proposal_file.write_bytes(workflow.proposals)
    reviewed_file.write_bytes(workflow.reviewed)
    response_file.write_bytes(workflow.response)
    issue_file.write_bytes(workflow.issue)
    target = tmp_path / "must-not-be-created.json"
    link = tmp_path / "dangling.json"
    os.symlink(target, link)

    with pytest.raises(ReviewError, match="already exists"):
        write_test_proposal_review(
            proposal_file,
            reviewed_file,
            issue_file,
            link,
            generator_response_file=response_file,
        )
    assert not target.exists()


def test_completed_materialization_authenticates_into_exact_verifier_request(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    finalized, finalized_bytes = _materialized_terminal_review(workflow, stage)

    authenticated = authenticate_test_materialization(stage)

    assert authenticated.root == stage.absolute()
    assert authenticated.reviewed_test_proposals_document == finalized_bytes
    assert authenticated.issue_document == workflow.issue
    assert authenticated.plan.schema_version == 2
    assert [item.case_id for item in authenticated.files] == [
        item["id"] for item in finalized["accepted_cases"]
    ]
    assert authenticated.expected_test_sha256 == tuple(
        (item["id"], item["source"]["sha256"])
        for item in finalized["accepted_cases"]
    )

    patch = tmp_path / "patch.diff"
    patch.write_bytes(b"diff --git a/value.py b/value.py\n")
    request = request_from_materialization(
        stage,
        repository=tmp_path / "repository",
        base_revision="HEAD",
        patch_file=patch,
        output_directory=tmp_path / "run",
        repeat=1,
    )

    assert request.issue_file == (stage / "issue.md").resolve()
    assert request.evidence_file == (stage / "evidence-plan.json").resolve()
    assert request.expected_issue_sha256 == authenticated.issue_sha256
    assert request.expected_evidence_sha256 == authenticated.evidence_plan_sha256
    assert request.expected_test_sha256 == tuple(
        sorted(authenticated.expected_test_sha256)
    )
    assert request.frozen_issue_document == workflow.issue
    assert request.frozen_evidence_document == authenticated.evidence_plan_document
    assert request.frozen_candidate_sources == {
        item.relative_path.as_posix(): item.source_bytes
        for item in authenticated.files
    }


def test_materialization_and_handoff_never_reopen_frozen_candidate_paths(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, review_bytes = _terminal_review(workflow)
    finalized = finalize_test_proposal_review(
        workflow.proposals,
        review_bytes,
        reviewed_requirements_document=workflow.reviewed,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
    )
    finalized_bytes = encode_test_review_artifact(finalized)

    def forbid_plain_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("reviewed candidate paths must not be reopened")

    monkeypatch.setattr(plan_module, "_read_bounded_bytes", forbid_plain_read)
    stage = tmp_path / "stage"
    materialize_reviewed_test_proposals(
        finalized_bytes,
        proposal_document=workflow.proposals,
        reviewed_requirements_document=workflow.reviewed,
        review_document=review_bytes,
        issue_document=workflow.issue,
        generator_response_document=workflow.response,
        output_directory=stage,
    )

    authenticated = authenticate_test_materialization(stage)

    assert authenticated.expected_test_sha256 == tuple(
        (item["id"], item["source"]["sha256"])
        for item in finalized["accepted_cases"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "audit_count",
        "coverage",
        "case_projection",
        "accepted_assertion",
        "requirements_binding",
        "response_binding",
    ],
)
def test_standalone_final_validator_rejects_internally_inconsistent_claims(
    workflow: WorkflowInputs,
    tmp_path: Path,
    mutation: str,
) -> None:
    finalized, _ = _materialized_terminal_review(workflow, tmp_path / "stage")
    changed = copy.deepcopy(finalized)
    if mutation == "audit_count":
        changed["audit"]["accepted_count"] = 0
    elif mutation == "coverage":
        changed["coverage"]["accepted_requirement_ids"] = []
        changed["coverage"]["unaccepted_requirement_ids"] = [
            item["id"] for item in changed["requirements"]
        ]
    elif mutation == "case_projection":
        changed["cases"][0]["node"] = changed["cases"][1]["node"]
    elif mutation == "accepted_assertion":
        changed["accepted_cases"][0]["assertion"]["excerpt"] = "assert False"
        changed["accepted_cases"][0]["assertion"]["sha256"] = hashlib.sha256(
            b"assert False"
        ).hexdigest()
    elif mutation == "requirements_binding":
        changed["reviewed_requirements"]["requirement_ids"].reverse()
    else:
        changed["generator_response"]["supplied_sha256"] = "0" * 64

    with pytest.raises(ReviewError):
        validate_reviewed_test_proposals_structure(changed)


def test_materialization_handoff_rejects_all_rejected_audit_with_live_cases(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    _materialized_terminal_review(workflow, stage)
    reviewed_path = stage / "reviewed-test-proposals.json"
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    for decision in reviewed["audit"]["decisions"]:
        decision.update(
            action="reject",
            rationale="Reviewer rejected this proposal.",
            replacement=None,
        )
    reviewed["audit"].update(
        accepted_count=0,
        edited_count=0,
        rejected_count=reviewed["audit"]["proposal_count"],
    )
    reviewed_document = encode_test_review_artifact(reviewed)
    reviewed_path.write_bytes(reviewed_document)
    manifest_path = stage / "materialization.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reviewed_test_proposals"].update(
        sha256=hashlib.sha256(reviewed_document).hexdigest(),
        size=len(reviewed_document),
    )
    manifest_path.write_bytes(encode_test_review_artifact(manifest))

    with pytest.raises(
        MaterializationError,
        match="effective accepted and edited audit decisions",
    ):
        authenticate_test_materialization(stage)


@pytest.mark.parametrize("target", ["candidate", "issue", "plan", "reviewed"])
def test_materialization_handoff_rejects_bound_file_drift(
    workflow: WorkflowInputs,
    tmp_path: Path,
    target: str,
) -> None:
    stage = tmp_path / "stage"
    finalized, _ = _materialized_terminal_review(workflow, stage)
    targets = {
        "candidate": stage / finalized["accepted_cases"][0]["test_file"],
        "issue": stage / "issue.md",
        "plan": stage / "evidence-plan.json",
        "reviewed": stage / "reviewed-test-proposals.json",
    }
    targets[target].write_bytes(targets[target].read_bytes() + b"\n")

    with pytest.raises(MaterializationError, match="(?:size|hash) mismatch"):
        authenticate_test_materialization(stage)


def test_materialization_handoff_rejects_symlinks_and_unexpected_files(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    _materialized_terminal_review(workflow, stage)
    external = tmp_path / "external-issue.md"
    external.write_bytes((stage / "issue.md").read_bytes())
    (stage / "issue.md").unlink()
    (stage / "issue.md").symlink_to(external)

    with pytest.raises(
        MaterializationError,
        match="unsafe directory entry|without following links",
    ):
        authenticate_test_materialization(stage)

    (stage / "issue.md").unlink()
    (stage / "issue.md").write_bytes(external.read_bytes())
    (stage / "unexpected.txt").write_text("drift", encoding="utf-8")
    with pytest.raises(MaterializationError, match="unexpected path"):
        authenticate_test_materialization(stage)


def test_materialization_handoff_rejects_ambiguous_or_missing_manifest(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    _materialized_terminal_review(workflow, stage)
    manifest = stage / "materialization.json"
    original = manifest.read_bytes()
    manifest.write_bytes(
        original.replace(
            b'{\n  "artifact_type"',
            b'{\n  "artifact_type": "test-materialization",\n  "artifact_type"',
            1,
        )
    )

    with pytest.raises(MaterializationError, match="duplicate JSON object key"):
        authenticate_test_materialization(stage)

    manifest.unlink()
    with pytest.raises(MaterializationError, match="cannot open materialized"):
        authenticate_test_materialization(stage)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _review_workflow_target(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Review Handoff Test")
    _git(repository, "config", "user.email", "review@example.invalid")
    (repository / "value.py").write_text(
        "def resolve(value=None):\n"
        "    return value if value is not None else 5\n",
        encoding="utf-8",
    )
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_resolve.py").write_text(
        "from value import resolve\n\n"
        "def test_zero():\n"
        "    assert resolve(0) == 0\n\n"
        "def test_missing():\n"
        "    assert resolve(None) == 5\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "base")
    (repository / "value.py").write_text(
        "def resolve(value=None):\n    return 0\n",
        encoding="utf-8",
    )
    patch = tmp_path / "proposal.diff"
    patch.write_text(
        _git(repository, "diff", "--binary") + "\n",
        encoding="utf-8",
    )
    _git(repository, "restore", "value.py")
    return repository, patch


def test_materialized_review_handoff_executes_only_at_explicit_verify(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    _materialized_terminal_review(workflow, stage)
    repository, patch = _review_workflow_target(tmp_path)
    request = request_from_materialization(
        stage,
        repository=repository,
        base_revision="HEAD",
        patch_file=patch,
        output_directory=tmp_path / "run",
        command_prefix=(sys.executable, "-m", "pytest", "-q", "--tb=short"),
        timeout_seconds=30,
        repeat=1,
    )

    result = verify(request)

    assert result.has_supported_failure
    assert any(
        finding.observation.requirement.id
        == json.loads(workflow.reviewed)["requirements"][1]["id"]
        for finding in result.supported_failures
    )
    assert (result.output_directory / "manifest.json").is_file()


def test_materialized_request_executes_authenticated_snapshot_after_path_drift(
    workflow: WorkflowInputs,
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    finalized, _ = _materialized_terminal_review(workflow, stage)
    repository, patch = _review_workflow_target(tmp_path)
    request = request_from_materialization(
        stage,
        repository=repository,
        base_revision="HEAD",
        patch_file=patch,
        output_directory=tmp_path / "run",
        repeat=1,
    )
    candidate = stage / finalized["accepted_cases"][0]["test_file"]
    candidate.write_bytes(candidate.read_bytes() + b"\n")
    (stage / "issue.md").write_text("changed issue\n", encoding="utf-8")
    (stage / "evidence-plan.json").write_text("{}\n", encoding="utf-8")

    result = verify(request)

    assert result.has_supported_failure
    assert (request.output_directory / "manifest.json").is_file()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "symlink"),
    reason="special-file handoff probe requires POSIX FIFO and symlink support",
)
def test_materialized_request_never_reopens_replaced_staging_tree(
    workflow: WorkflowInputs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    finalized, _ = _materialized_terminal_review(workflow, stage)
    repository, patch = _review_workflow_target(tmp_path)
    request = request_from_materialization(
        stage,
        repository=repository,
        base_revision="HEAD",
        patch_file=patch,
        output_directory=tmp_path / "run",
        repeat=1,
    )

    displaced = tmp_path / "authenticated-stage"
    stage.rename(displaced)
    hostile = tmp_path / "hostile-stage"
    hostile.mkdir()
    os.mkfifo(hostile / "issue.md")
    os.symlink(tmp_path / "missing-plan.json", hostile / "evidence-plan.json")
    hostile_candidate = hostile / finalized["accepted_cases"][0]["test_file"]
    hostile_candidate.parent.mkdir(parents=True)
    os.mkfifo(hostile_candidate)
    os.symlink(hostile, stage, target_is_directory=True)

    original_input_read = verifier_module._read_bytes

    def reject_staging_input_read(
        path: Path,
        label: str,
        *,
        max_bytes: int,
        expected_sha256: str | None = None,
    ) -> bytes:
        if label in {"issue", "evidence plan"}:
            raise AssertionError(f"reopened replaced {label} path")
        return original_input_read(
            path,
            label,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
        )

    original_plan_read = plan_module._read_bounded_bytes

    def reject_candidate_read(
        path: Path,
        *,
        max_bytes: int,
        label: str,
    ) -> bytes:
        if label.startswith("case "):
            raise AssertionError("reopened replaced candidate path")
        return original_plan_read(path, max_bytes=max_bytes, label=label)

    monkeypatch.setattr(verifier_module, "_read_bytes", reject_staging_input_read)
    monkeypatch.setattr(plan_module, "_read_bounded_bytes", reject_candidate_read)

    result = verify(request)

    assert result.has_supported_failure
    assert (request.output_directory / "manifest.json").is_file()
