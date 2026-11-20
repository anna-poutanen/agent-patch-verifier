from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

import patch_verifier.review as review_module
from patch_verifier.drafting import DraftRequirementsRequest, draft_patch_requirements
from patch_verifier.plan import load_evidence_plan, verify_base_sources
from patch_verifier.review import (
    RequirementReviewError,
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
    stable_requirement_id,
    write_requirement_review,
    write_reviewed_requirements,
)
from patch_verifier.review_validation import (
    validate_requirement_review_artifact,
    validate_reviewed_requirements_artifact,
)


BASE_SOURCE = '''\
def resolve(value=None):
    """Return five when the value is missing; otherwise return it unchanged."""
    return value or 5
'''

BASE_TEST = '''\
from value import resolve


def test_preserves_nonzero_value():
    assert (
        resolve(3)
        == 3
    )
'''

PATCH = '''\
diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1,3 +1,3 @@
 def resolve(value=None):
     """Return five when the value is missing; otherwise return it unchanged."""
-    return value or 5
+    return 0
'''

ISSUE = "# Default handling\n\n- Explicit zero must be returned unchanged.\n"


@pytest.fixture
def draft_inputs(tmp_path: Path) -> tuple[bytes, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "value.py").write_text(BASE_SOURCE, encoding="utf-8")
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_value.py").write_text(BASE_TEST, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=APV Tests",
            "-c",
            "user.email=apv@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repository,
        check=True,
    )
    patch_file = tmp_path / "proposed.diff"
    issue_file = tmp_path / "bug-report.txt"
    draft_file = tmp_path / "draft.json"
    patch_file.write_text(PATCH, encoding="utf-8")
    issue_file.write_text(ISSUE, encoding="utf-8")
    draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=draft_file,
        )
    )
    return draft_file.read_bytes(), repository, ISSUE


def _terminal_review(
    draft_document: bytes,
    *,
    all_rejected: bool = False,
) -> tuple[dict[str, Any], bytes]:
    ledger = prepare_requirement_review(draft_document)
    for decision in ledger["decisions"]:
        candidate = decision["candidate"]
        if all_rejected or candidate["source_kind"] == "docstring":
            decision.update(action="reject", rationale="Not selected for this plan.")
        elif candidate["source_kind"] == "issue":
            decision.update(action="accept", rationale="Matches the supplied issue.")
        else:
            decision.update(
                action="edit",
                rationale="Clarified the existing assertion without changing it.",
                replacement={
                    "statement": "A nonzero value remains unchanged.\nThis is preserved.",
                    "kind": "preservation",
                    "provenance": "preexisting",
                },
            )
    return ledger, encode_review_artifact(ledger)


def _recompute_preview_candidate_id(decision: dict[str, Any]) -> None:
    candidate = decision["candidate"]
    canonical = json.dumps(
        {
            "intent": candidate["intent"],
            "source": candidate["source"],
            "source_kind": candidate["source_kind"],
            "statement": candidate["statement"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate_id = "cand-" + hashlib.sha256(canonical).hexdigest()
    candidate["id"] = candidate_id
    decision["candidate_id"] = candidate_id
    decision["requirement_id"] = stable_requirement_id(candidate_id)


def test_prepare_review_is_self_contained_deterministic_and_exactly_bound(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs

    first = prepare_requirement_review(draft_document)
    second = prepare_requirement_review(draft_document)

    assert first == second
    validate_requirement_review_artifact(first)
    assert first["draft"]["sha256"] == hashlib.sha256(draft_document).hexdigest()
    assert first["conflict_analysis"] is None
    draft = json.loads(draft_document)
    assert len(first["decisions"]) == len(draft["draft"]["candidates"])
    for decision, candidate in zip(
        first["decisions"], draft["draft"]["candidates"]
    ):
        assert decision == {
            "candidate_id": candidate["id"],
            "candidate": candidate,
            "action": "pending",
            "requirement_id": stable_requirement_id(candidate["id"]),
            "rationale": None,
            "replacement": None,
        }
        assert len(decision["requirement_id"]) == 64


def test_finalization_emits_plan_shaped_requirements_and_complete_audit(
    draft_inputs: tuple[bytes, Path, str],
    tmp_path: Path,
) -> None:
    draft_document, repository, issue_text = draft_inputs
    ledger, review_document = _terminal_review(draft_document)

    finalized = finalize_requirement_review(draft_document, review_document)

    validate_reviewed_requirements_artifact(finalized)
    assert finalized["schema_version"] == 2
    assert finalized["issue"] == {
        "sha256": hashlib.sha256(issue_text.encode("utf-8")).hexdigest(),
        "source": json.loads(draft_document)["inputs"]["issue_source"],
    }
    assert finalized["draft"] == ledger["draft"]
    assert finalized["review"]["sha256"] == hashlib.sha256(
        review_document
    ).hexdigest()
    assert finalized["audit"]["decisions"] == ledger["decisions"]
    assert finalized["audit"]["candidate_count"] == len(ledger["decisions"])
    assert finalized["audit"]["accepted_count"] == 1
    assert finalized["audit"]["edited_count"] == 1
    assert finalized["audit"]["rejected_count"] >= 1
    assert len(finalized["requirements"]) == 2
    issue_requirement = next(
        item for item in finalized["requirements"] if item["kind"] == "requested"
    )
    preservation = next(
        item for item in finalized["requirements"] if item["kind"] == "preservation"
    )
    assert issue_requirement["source"] == "issue:3"
    assert issue_requirement["provenance"] == "explicit"
    assert preservation["source"].startswith("tests/test_value.py:")
    assert preservation["provenance"] == "preexisting"
    assert "\n" in preservation["statement"]

    legacy = copy.deepcopy(finalized)
    legacy["schema_version"] = 1
    legacy.pop("issue")
    validate_reviewed_requirements_artifact(legacy)
    legacy["issue"] = copy.deepcopy(finalized["issue"])
    with pytest.raises(ValueError):
        validate_reviewed_requirements_artifact(legacy)
    missing_v2_binding = copy.deepcopy(finalized)
    missing_v2_binding.pop("issue")
    with pytest.raises(ValueError):
        validate_reviewed_requirements_artifact(missing_v2_binding)
    floating_version = copy.deepcopy(finalized)
    floating_version["schema_version"] = 2.0
    with pytest.raises(ValueError, match="schema_version: expected integer"):
        validate_reviewed_requirements_artifact(floating_version)

    candidate_tests = tmp_path / "candidate.py"
    candidate_tests.write_text(
        "def test_requested():\n    assert True\n\n"
        "def test_preservation():\n    assert True\n",
        encoding="utf-8",
    )
    plan_file = tmp_path / "evidence.json"
    plan_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requirements": finalized["requirements"],
                "cases": [
                    {
                        "id": "CASE-requested",
                        "requirement_id": issue_requirement["id"],
                        "test_file": "candidate.py",
                        "node": "test_requested",
                    },
                    {
                        "id": "CASE-preservation",
                        "requirement_id": preservation["id"],
                        "test_file": "candidate.py",
                        "node": "test_preservation",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    plan = load_evidence_plan(plan_file, issue_text=issue_text)
    verify_base_sources(plan, repository)


def test_finalization_rejects_pending_missing_unknown_duplicate_and_reordered(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    pending = prepare_requirement_review(draft_document)
    with pytest.raises(RequirementReviewError, match="pending decision"):
        finalize_requirement_review(draft_document, encode_review_artifact(pending))

    complete, _ = _terminal_review(draft_document)
    missing = copy.deepcopy(complete)
    missing["decisions"].pop()
    with pytest.raises(RequirementReviewError, match="candidate_count"):
        finalize_requirement_review(draft_document, encode_review_artifact(missing))

    duplicate = copy.deepcopy(complete)
    duplicate["decisions"][-1] = copy.deepcopy(duplicate["decisions"][0])
    with pytest.raises(RequirementReviewError, match="duplicated"):
        finalize_requirement_review(draft_document, encode_review_artifact(duplicate))

    unknown = copy.deepcopy(complete)
    unknown_decision = copy.deepcopy(unknown["decisions"][0])
    unknown_statement = "Explicit one must be returned unchanged."
    unknown_source = unknown_decision["candidate"]["source"]
    unknown_decision["candidate"]["statement"] = unknown_statement
    unknown_source["excerpt"] = unknown_statement
    unknown_source["end_column"] = (
        unknown_source["start_column"] + len(unknown_statement.encode("utf-8"))
    )
    unknown_source["sha256"] = hashlib.sha256(
        unknown_statement.encode("utf-8")
    ).hexdigest()
    _recompute_preview_candidate_id(unknown_decision)
    unknown["decisions"][0] = unknown_decision
    with pytest.raises(RequirementReviewError, match="unknown candidate"):
        finalize_requirement_review(draft_document, encode_review_artifact(unknown))

    reordered = copy.deepcopy(complete)
    reordered["decisions"][0], reordered["decisions"][1] = (
        reordered["decisions"][1],
        reordered["decisions"][0],
    )
    with pytest.raises(RequirementReviewError, match="deterministic draft order"):
        finalize_requirement_review(draft_document, encode_review_artifact(reordered))


def test_finalization_rejects_hash_preview_id_and_edit_drift(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    complete, _ = _terminal_review(draft_document)

    reserialized = json.dumps(json.loads(draft_document)).encode("utf-8")
    with pytest.raises(RequirementReviewError, match="draft hash does not match"):
        finalize_requirement_review(reserialized, encode_review_artifact(complete))

    preview_drift = copy.deepcopy(complete)
    preview_drift["decisions"][0]["candidate"]["confidence"] = "low"
    with pytest.raises(RequirementReviewError, match="preview does not exactly match"):
        finalize_requirement_review(
            draft_document, encode_review_artifact(preview_drift)
        )

    id_drift = copy.deepcopy(complete)
    id_drift["decisions"][0]["requirement_id"] = "reviewer-selected"
    with pytest.raises(RequirementReviewError, match="stable id derived"):
        finalize_requirement_review(draft_document, encode_review_artifact(id_drift))

    edit = next(
        decision for decision in complete["decisions"] if decision["action"] == "edit"
    )
    wrong_kind = copy.deepcopy(complete)
    wrong = next(
        decision
        for decision in wrong_kind["decisions"]
        if decision["candidate_id"] == edit["candidate_id"]
    )
    wrong["replacement"]["kind"] = "requested"
    with pytest.raises(RequirementReviewError, match="cannot change.*kind"):
        finalize_requirement_review(draft_document, encode_review_artifact(wrong_kind))

    no_op = copy.deepcopy(complete)
    same = next(
        decision
        for decision in no_op["decisions"]
        if decision["candidate_id"] == edit["candidate_id"]
    )
    same["replacement"]["statement"] = same["candidate"]["statement"]
    with pytest.raises(RequirementReviewError, match="does not change"):
        finalize_requirement_review(draft_document, encode_review_artifact(no_op))


def test_portable_validators_reject_semantic_tampering(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    ledger, review_document = _terminal_review(draft_document)
    finalized = finalize_requirement_review(draft_document, review_document)

    changed_requirement = copy.deepcopy(finalized)
    changed_requirement["requirements"][0]["statement"] = "Forged behavior."
    with pytest.raises(ValueError, match="do not exactly match"):
        validate_reviewed_requirements_artifact(changed_requirement)

    changed_source = copy.deepcopy(finalized)
    changed_source["requirements"][0]["source"] = "issue:999"
    with pytest.raises(ValueError, match="do not exactly match"):
        validate_reviewed_requirements_artifact(changed_source)

    wrong_intent = copy.deepcopy(ledger)
    wrong_intent["decisions"][0]["candidate"]["intent"] = "preservation"
    with pytest.raises(ValueError, match="issue candidates must be requested"):
        validate_requirement_review_artifact(wrong_intent)

    bad_span = copy.deepcopy(ledger)
    bad_span["decisions"][0]["candidate"]["source"]["end_column"] += 1
    with pytest.raises(ValueError, match="do not delimit"):
        validate_requirement_review_artifact(bad_span)

    wrong_edit = copy.deepcopy(ledger)
    edited_decision = next(
        decision for decision in wrong_edit["decisions"] if decision["action"] == "edit"
    )
    edited_decision["replacement"]["kind"] = "requested"
    edited_decision["replacement"]["provenance"] = "explicit"
    with pytest.raises(ValueError, match="cannot change source-backed kind"):
        validate_requirement_review_artifact(wrong_edit)

    no_op_edit = copy.deepcopy(ledger)
    no_op_decision = next(
        decision
        for decision in no_op_edit["decisions"]
        if decision["action"] == "edit"
    )
    no_op_decision["replacement"]["statement"] = no_op_decision["candidate"][
        "statement"
    ]
    with pytest.raises(ValueError, match="edit does not change"):
        validate_requirement_review_artifact(no_op_edit)

    forged_statement = copy.deepcopy(ledger)
    forged_statement["decisions"][0]["candidate"]["statement"] = "Unrelated claim."
    _recompute_preview_candidate_id(forged_statement["decisions"][0])
    with pytest.raises(ValueError, match="deterministic transform"):
        validate_requirement_review_artifact(forged_statement)

    reordered = copy.deepcopy(ledger)
    reordered["decisions"][0], reordered["decisions"][1] = (
        reordered["decisions"][1],
        reordered["decisions"][0],
    )
    with pytest.raises(ValueError, match="deterministic draft order"):
        validate_requirement_review_artifact(reordered)

    requested_count = sum(
        decision["candidate"]["intent"] == "requested"
        for decision in ledger["decisions"]
    )
    preservation_count = len(ledger["decisions"]) - requested_count
    incomplete_analysis = copy.deepcopy(ledger)
    incomplete_analysis["conflict_analysis"] = {
        "requested_candidate_count": requested_count,
        "preservation_candidate_count": preservation_count,
        "compared_pair_count": 0,
        "hint_count": 0,
        "truncated": False,
    }
    with pytest.raises(ValueError, match="must cover every"):
        validate_requirement_review_artifact(incomplete_analysis)

    wrong_counts = copy.deepcopy(incomplete_analysis)
    wrong_counts["conflict_analysis"]["requested_candidate_count"] = 0
    with pytest.raises(ValueError, match="requested_candidate_count"):
        validate_requirement_review_artifact(wrong_counts)


def test_duplicate_json_keys_and_non_bytes_are_rejected(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    ledger, review_document = _terminal_review(draft_document)
    duplicate_key_review = review_document.replace(
        b'{\n  "artifact_type"',
        b'{\n  "artifact_type": "requirement-review",\n  "artifact_type"',
        1,
    )
    with pytest.raises(RequirementReviewError, match="duplicate JSON object key"):
        finalize_requirement_review(draft_document, duplicate_key_review)
    with pytest.raises(RequirementReviewError, match="exact bytes"):
        prepare_requirement_review(json.loads(draft_document))  # type: ignore[arg-type]
    validate_requirement_review_artifact(ledger)


def test_conflict_analysis_is_preserved_and_can_be_reauthenticated(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    provisional = prepare_requirement_review(draft_document)
    ids = sorted(decision["candidate_id"] for decision in provisional["decisions"])
    hint = {
        "id": "hint-" + "a" * 64,
        "candidate_ids": ids[:2],
        "message": "Potential tension for explicit human review.",
    }
    analysis = {
        "hints": [hint],
        "requested_candidate_count": 1,
        "preservation_candidate_count": len(ids) - 1,
        "compared_pair_count": len(ids) - 1,
        "hint_count": 1,
        "truncated": False,
    }
    ledger = prepare_requirement_review(
        draft_document, conflict_analysis=analysis
    )
    assert ledger["conflict_analysis"] == {
        "requested_candidate_count": 1,
        "preservation_candidate_count": len(ids) - 1,
        "compared_pair_count": len(ids) - 1,
        "hint_count": 1,
        "truncated": False,
    }
    assert ledger["conflict_hints"] == [hint]
    for decision in ledger["decisions"]:
        decision.update(action="reject", rationale="Reviewed and rejected.")

    finalized = finalize_requirement_review(
        draft_document,
        encode_review_artifact(ledger),
        expected_conflict_analysis=analysis,
    )
    assert finalized["audit"]["conflict_analysis"]["truncated"] is False

    changed = copy.deepcopy(analysis)
    changed["truncated"] = True
    with pytest.raises(RequirementReviewError, match="detector summary"):
        finalize_requirement_review(
            draft_document,
            encode_review_artifact(ledger),
            expected_conflict_analysis=changed,
        )


def test_all_rejected_is_a_valid_auditable_result(
    draft_inputs: tuple[bytes, Path, str],
) -> None:
    draft_document, _, _ = draft_inputs
    ledger, review_document = _terminal_review(draft_document, all_rejected=True)

    finalized = finalize_requirement_review(draft_document, review_document)

    assert finalized["requirements"] == []
    assert finalized["audit"]["rejected_count"] == len(ledger["decisions"])
    validate_reviewed_requirements_artifact(finalized)


def test_file_workflow_is_exclusive_atomic_and_rejects_dangling_symlink(
    draft_inputs: tuple[bytes, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_document, _, _ = draft_inputs
    draft_file = tmp_path / "draft-copy.json"
    draft_file.write_bytes(draft_document)
    review_file = tmp_path / "review.json"
    prepared = write_requirement_review(draft_file, review_file)
    assert prepared.output_file == review_file
    with pytest.raises(RequirementReviewError, match="already exists"):
        write_requirement_review(draft_file, review_file)

    stale_output = tmp_path / "stale-review.json"
    with pytest.raises(RequirementReviewError, match="changed after conflict analysis"):
        write_requirement_review(
            draft_file,
            stale_output,
            expected_draft_sha256="0" * 64,
        )
    assert not stale_output.exists()

    ledger = json.loads(review_file.read_text(encoding="utf-8"))
    for decision in ledger["decisions"]:
        decision.update(action="reject", rationale="Not selected.")
    review_file.write_bytes(encode_review_artifact(ledger))
    stale_final = tmp_path / "stale-final.json"
    with pytest.raises(RequirementReviewError, match="changed after conflict analysis"):
        write_reviewed_requirements(
            draft_file,
            review_file,
            stale_final,
            expected_draft_sha256="0" * 64,
        )
    assert not stale_final.exists()
    final_file = tmp_path / "final.json"
    finalized = write_reviewed_requirements(draft_file, review_file, final_file)
    assert finalized.requirement_count == 0
    assert final_file.exists()

    dangling_target = tmp_path / "must-not-be-created.json"
    dangling_link = tmp_path / "dangling.json"
    os.symlink(dangling_target, dangling_link)
    with pytest.raises(RequirementReviewError, match="already exists|cannot write"):
        write_requirement_review(draft_file, dangling_link)
    assert not dangling_target.exists()

    failed_output = tmp_path / "failed.json"
    original_link = review_module.os.link

    def fail_link(source: object, destination: object) -> None:
        raise OSError("simulated link failure")

    monkeypatch.setattr(review_module.os, "link", fail_link)
    with pytest.raises(RequirementReviewError, match="simulated link failure"):
        write_requirement_review(draft_file, failed_output)
    assert not failed_output.exists()
    assert not list(tmp_path.glob(f".{failed_output.name}.*.tmp"))
    monkeypatch.setattr(review_module.os, "link", original_link)


def test_resource_limits_fail_before_parsing_or_writing(
    draft_inputs: tuple[bytes, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_document, _, _ = draft_inputs
    monkeypatch.setattr(review_module, "_MAX_DRAFT_BYTES", len(draft_document) - 1)
    with pytest.raises(RequirementReviewError, match="byte limit"):
        prepare_requirement_review(draft_document)

    def too_many_hints() -> Any:
        for _ in range(review_module._MAX_CONFLICT_HINTS + 1):
            yield {}

    monkeypatch.setattr(review_module, "_MAX_DRAFT_BYTES", len(draft_document))
    with pytest.raises(RequirementReviewError, match="hint limit"):
        prepare_requirement_review(draft_document, conflict_hints=too_many_hints())
