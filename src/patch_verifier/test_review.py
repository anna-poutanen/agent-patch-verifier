"""Human review and inert materialization of generated test proposals.

The workflow deliberately separates three trust transitions:

* :func:`prepare_test_proposal_review` creates a pending ledger bound to the
  exact proposal, reviewed-requirements, and (when applicable) generator
  response bytes.
* :func:`finalize_test_proposal_review` requires an explicit accept, edit, or
  reject decision for every proposal and records the exact effective source
  and assertion evidence for accepted cases.
* :func:`materialize_reviewed_test_proposals` writes a new staging directory
  containing an EvidencePlan v2 and the accepted source bytes.  It never
  imports, collects, or executes candidate code.

Edited source is parsed only through the shared static candidate validator.
The materialized EvidencePlan and completion manifest bind every candidate
file by SHA-256 so a later verifier can reject post-review changes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .plan import load_evidence_plan, validate_candidate_test_source
from .review_validation import validate_reviewed_requirements_artifact
from .test_proposals import (
    TestProposalError,
    _validate_safe_test_file,
    build_test_proposal_artifact,
    validate_test_proposal_artifact,
)


_MAX_PROPOSAL_DOCUMENT_BYTES = 24 * 1024 * 1024
_MAX_REVIEWED_REQUIREMENTS_BYTES = 24 * 1024 * 1024
_MAX_GENERATOR_RESPONSE_BYTES = 24 * 1024 * 1024
_MAX_REVIEW_DOCUMENT_BYTES = 40 * 1024 * 1024
_MAX_FINAL_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_PLAN_DOCUMENT_BYTES = 5 * 1024 * 1024
_MAX_ISSUE_BYTES = 2 * 1024 * 1024
_MAX_RATIONALE_BYTES = 16_000
_MAX_DIRECTORY_PREFIXES = 256
_SECURE_DIR_FD_AVAILABLE = (
    all(
        operation in os.supports_dir_fd
        for operation in (os.open, os.mkdir, os.link, os.stat, os.unlink)
    )
    and os.listdir in os.supports_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_NONBLOCK")
)
_PORTABLE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_COMPONENTS = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_RESERVED_ROOT_COMPONENTS = frozenset(
    {
        "evidence-plan.json",
        "issue.md",
        "materialization.json",
        "reviewed-test-proposals.json",
    }
)
_CATEGORY_ORDER = {
    "requested": 0,
    "preservation": 1,
    "boundary": 2,
    "invalid_input": 3,
    "caller_level": 4,
    "state_sequence": 5,
}


class TestProposalReviewError(RuntimeError):
    """A test-proposal review or materialization is invalid."""


@dataclass(frozen=True)
class TestReviewPreparationResult:
    """Summary of an exclusively created test review ledger."""

    output_file: Path
    proposal_sha256: str
    reviewed_requirements_sha256: str
    issue_sha256: str
    proposal_count: int


@dataclass(frozen=True)
class TestReviewFinalizationResult:
    """Summary of an exclusively created reviewed-test artifact."""

    output_file: Path
    proposal_sha256: str
    review_sha256: str
    issue_sha256: str
    accepted_case_count: int
    rejected_count: int


@dataclass(frozen=True)
class TestMaterializationResult:
    """Summary of a completed, non-executed staging directory."""

    output_directory: Path
    evidence_plan_file: Path
    manifest_file: Path
    reviewed_test_proposals_sha256: str
    evidence_plan_sha256: str
    case_count: int


@dataclass(frozen=True)
class _ProposalContext:
    proposal_bytes: bytes
    proposal: dict[str, Any]
    reviewed_bytes: bytes
    reviewed: dict[str, Any]
    issue_bytes: bytes
    response_bytes: bytes | None


class _DuplicateKeyError(ValueError):
    pass


def prepare_test_proposal_review(
    proposal_document: bytes,
    *,
    reviewed_requirements_document: bytes,
    issue_document: bytes,
    generator_response_document: bytes | None = None,
) -> dict[str, Any]:
    """Create a deterministic pending ledger for exact proposal bytes."""

    context = _load_proposal_context(
        proposal_document,
        reviewed_requirements_document=reviewed_requirements_document,
        issue_document=issue_document,
        generator_response_document=generator_response_document,
    )
    proposals = context.proposal["proposals"]
    assert isinstance(proposals, list)
    ledger: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "test-proposal-review",
        "proposal_artifact": {
            "sha256": hashlib.sha256(context.proposal_bytes).hexdigest(),
            "schema_version": context.proposal["schema_version"],
            "proposal_count": len(proposals),
        },
        "reviewed_requirements": copy.deepcopy(
            context.proposal["reviewed_requirements"]
        ),
        "issue": _issue_binding(context.issue_bytes),
        "generator_response": _review_response_binding(context),
        "decisions": [
            {
                "proposal_id": proposal["id"],
                "proposal": copy.deepcopy(proposal),
                "action": "pending",
                "rationale": None,
                "replacement": None,
            }
            for proposal in proposals
        ],
    }
    validate_test_proposal_review_artifact(ledger)
    _require_document_limit(
        encode_test_review_artifact(ledger),
        label="test proposal review",
        limit=_MAX_REVIEW_DOCUMENT_BYTES,
    )
    return ledger


def finalize_test_proposal_review(
    proposal_document: bytes,
    review_document: bytes,
    *,
    reviewed_requirements_document: bytes,
    issue_document: bytes,
    generator_response_document: bytes | None = None,
) -> dict[str, Any]:
    """Finalize complete decisions into exact effective cases and a v2 plan.

    All-rejected reviews are valid audit artifacts.  They cannot be
    materialized because an EvidencePlan requires at least one case.
    """

    result = _build_finalized_artifact(
        proposal_document,
        review_document,
        reviewed_requirements_document=reviewed_requirements_document,
        issue_document=issue_document,
        generator_response_document=generator_response_document,
    )
    _validate_final_schema(result)
    _require_document_limit(
        encode_test_review_artifact(result),
        label="reviewed test proposals",
        limit=_MAX_FINAL_DOCUMENT_BYTES,
    )
    return result


def validate_test_proposal_review_artifact(payload: object) -> None:
    """Validate the portable structure and internal semantics of a ledger."""

    _validate_review_schema(payload)
    assert isinstance(payload, Mapping)
    proposal_binding = payload["proposal_artifact"]
    requirements_binding = payload["reviewed_requirements"]
    response_binding = payload["generator_response"]
    decisions = payload["decisions"]
    assert isinstance(proposal_binding, Mapping)
    assert isinstance(requirements_binding, Mapping)
    assert isinstance(response_binding, Mapping)
    assert isinstance(decisions, list)

    if proposal_binding["proposal_count"] != len(decisions):
        raise TestProposalReviewError(
            "test_proposal_review.proposal_artifact.proposal_count: does not "
            "match the decision count"
        )
    requirement_ids = requirements_binding["requirement_ids"]
    assert isinstance(requirement_ids, list)
    if requirements_binding["requirement_count"] != len(requirement_ids):
        raise TestProposalReviewError(
            "test_proposal_review.reviewed_requirements.requirement_count: "
            "does not match requirement_ids"
        )
    if response_binding["binding"] == "exact":
        if response_binding["supplied_sha256"] != response_binding["sha256"]:
            raise TestProposalReviewError(
                "test_proposal_review.generator_response.supplied_sha256: "
                "exact bindings require the original response bytes"
            )
    elif response_binding["sha256"] != response_binding["canonical_sha256"]:
        raise TestProposalReviewError(
            "test_proposal_review.generator_response.sha256: canonical "
            "binding must equal canonical_sha256"
        )

    seen_ids: set[str] = set()
    replacement_source_bytes = 0
    for index, decision in enumerate(decisions):
        assert isinstance(decision, Mapping)
        proposal = decision["proposal"]
        assert isinstance(proposal, Mapping)
        proposal_id = decision["proposal_id"]
        assert isinstance(proposal_id, str)
        if proposal_id in seen_ids:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].proposal_id: is duplicated"
            )
        seen_ids.add(proposal_id)
        if proposal["id"] != proposal_id:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].proposal_id: does not "
                "match the embedded proposal"
            )
        _validate_embedded_proposal(proposal, index=index)

        action = decision["action"]
        rationale = decision["rationale"]
        replacement = decision["replacement"]
        if action == "pending":
            continue
        assert isinstance(rationale, str)
        _validate_text(
            rationale,
            label=f"test_proposal_review.decisions[{index}].rationale",
            max_bytes=_MAX_RATIONALE_BYTES,
        )
        if action != "edit":
            if replacement is not None:
                raise TestProposalReviewError(
                    f"test_proposal_review.decisions[{index}].replacement: "
                    "is allowed only for edit decisions"
                )
            continue

        assert isinstance(replacement, Mapping)
        try:
            _validate_safe_test_file(
                replacement["test_file"],
                label=(
                    f"test_proposal_review.decisions[{index}].replacement."
                    "test_file"
                ),
            )
        except TestProposalError as exc:
            raise TestProposalReviewError(str(exc)) from exc
        _validate_text(
            replacement["rationale"],
            label=(
                f"test_proposal_review.decisions[{index}].replacement.rationale"
            ),
            max_bytes=_MAX_RATIONALE_BYTES,
        )
        source_bytes = _encode_utf8(
            replacement["source"],
            label=f"test_proposal_review.decisions[{index}].replacement.source",
        )
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].replacement.source: "
                f"exceeds the {_MAX_SOURCE_BYTES}-byte limit"
            )
        replacement_source_bytes += len(source_bytes)
        if replacement_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise TestProposalReviewError(
                "test_proposal_review.decisions: edited sources exceed the "
                f"{_MAX_TOTAL_SOURCE_BYTES}-byte aggregate limit"
            )
        try:
            validation = validate_candidate_test_source(
                source_bytes,
                case_id=proposal_id,
                node=replacement["node"],
                source_name=replacement["test_file"],
            )
        except ValueError as exc:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].replacement.source: "
                f"inadmissible candidate: {exc}"
            ) from exc
        if validation.python_assertion_count != 1:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].replacement.source: "
                "must contain exactly one Python assert in the entire "
                f"candidate file; found {validation.python_assertion_count}"
            )
        original_raw = _raw_effective_fields(proposal)
        if dict(replacement) == original_raw:
            raise TestProposalReviewError(
                f"test_proposal_review.decisions[{index}].replacement: edit "
                "does not change the proposal"
            )


def validate_reviewed_test_proposals_artifact(
    payload: object,
    *,
    proposal_document: bytes,
    reviewed_requirements_document: bytes,
    review_document: bytes,
    issue_document: bytes,
    generator_response_document: bytes | None = None,
) -> None:
    """Authenticate a finalized artifact against every exact source input."""

    _validate_final_schema(payload)
    expected = _build_finalized_artifact(
        proposal_document,
        review_document,
        reviewed_requirements_document=reviewed_requirements_document,
        issue_document=issue_document,
        generator_response_document=generator_response_document,
    )
    if payload != expected:
        raise TestProposalReviewError(
            "reviewed_test_proposals: content does not exactly match the bound "
            "proposal, review, requirements, and effective sources"
        )


def validate_reviewed_test_proposals_structure(payload: object) -> None:
    """Validate the finalized artifact's closed standalone semantics.

    Exact proposal, review, and requirements bytes are intentionally not
    available at this handoff boundary.  The artifact is nevertheless
    self-contained enough to prove that its audit decisions, effective cases,
    coverage, and EvidencePlan projection agree with one another.  Without
    these checks, a rehashed artifact could claim every proposal was rejected
    while still handing accepted source to the verifier.
    """

    _validate_final_schema(payload)
    assert isinstance(payload, Mapping)
    _validate_finalized_standalone_semantics(payload)


def _validate_finalized_standalone_semantics(
    payload: Mapping[str, Any],
) -> None:
    proposal_binding = payload["proposal_artifact"]
    requirements_binding = payload["reviewed_requirements"]
    response_binding = payload["generator_response"]
    coverage = payload["coverage"]
    requirements = payload["requirements"]
    cases = payload["cases"]
    accepted_cases = payload["accepted_cases"]
    audit = payload["audit"]
    assert isinstance(proposal_binding, Mapping)
    assert isinstance(requirements_binding, Mapping)
    assert isinstance(response_binding, Mapping)
    assert isinstance(coverage, Mapping)
    assert isinstance(requirements, list)
    assert isinstance(cases, list)
    assert isinstance(accepted_cases, list)
    assert isinstance(audit, Mapping)

    # A finalized review can only be produced from the issue-bound v2 chain.
    # Legacy v1 proposal artifacts require v1 reviewed requirements, which do
    # not carry the exact issue binding required by this workflow.
    if proposal_binding["schema_version"] != 2:
        raise TestProposalReviewError(
            "reviewed_test_proposals.proposal_artifact.schema_version: "
            "standalone reviewed tests require issue-bound schema_version 2"
        )
    if requirements_binding["schema_version"] != 2:
        raise TestProposalReviewError(
            "reviewed_test_proposals.reviewed_requirements.schema_version: "
            "standalone reviewed tests require issue-bound schema_version 2"
        )

    requirement_ids = [requirement["id"] for requirement in requirements]
    if len(set(requirement_ids)) != len(requirement_ids):
        raise TestProposalReviewError(
            "reviewed_test_proposals.requirements: requirement ids are duplicated"
        )
    if requirements_binding["requirement_count"] != len(requirements):
        raise TestProposalReviewError(
            "reviewed_test_proposals.reviewed_requirements.requirement_count: "
            "does not match requirements"
        )
    if requirements_binding["requirement_ids"] != requirement_ids:
        raise TestProposalReviewError(
            "reviewed_test_proposals.reviewed_requirements.requirement_ids: "
            "must match requirements exactly in order"
        )
    requirement_by_id = {
        requirement["id"]: requirement for requirement in requirements
    }

    if response_binding["binding"] == "exact":
        if response_binding["supplied_sha256"] != response_binding["sha256"]:
            raise TestProposalReviewError(
                "reviewed_test_proposals.generator_response.supplied_sha256: "
                "exact binding does not match sha256"
            )
    elif response_binding["sha256"] != response_binding["canonical_sha256"]:
        raise TestProposalReviewError(
            "reviewed_test_proposals.generator_response.sha256: canonical "
            "binding does not match canonical_sha256"
        )

    decisions = audit["decisions"]
    assert isinstance(decisions, list)
    if proposal_binding["proposal_count"] != len(decisions):
        raise TestProposalReviewError(
            "reviewed_test_proposals.proposal_artifact.proposal_count: does not "
            "match audit decisions"
        )
    if audit["proposal_count"] != len(decisions):
        raise TestProposalReviewError(
            "reviewed_test_proposals.audit.proposal_count: does not match audit "
            "decisions"
        )

    expected_accepted: list[dict[str, Any]] = []
    action_counts = {"accept": 0, "edit": 0, "reject": 0}
    seen_proposal_ids: set[str] = set()
    proposal_order: list[tuple[object, ...]] = []
    original_proposals: list[dict[str, Any]] = []
    replacement_source_bytes = 0
    for index, decision in enumerate(decisions):
        assert isinstance(decision, Mapping)
        proposal = decision["proposal"]
        assert isinstance(proposal, Mapping)
        proposal_id = decision["proposal_id"]
        assert isinstance(proposal_id, str)
        if proposal_id in seen_proposal_ids:
            raise TestProposalReviewError(
                f"reviewed_test_proposals.audit.decisions[{index}].proposal_id: "
                "is duplicated"
            )
        seen_proposal_ids.add(proposal_id)
        if proposal["id"] != proposal_id:
            raise TestProposalReviewError(
                f"reviewed_test_proposals.audit.decisions[{index}].proposal_id: "
                "does not match the embedded proposal"
            )
        normalized_proposal = _validate_standalone_proposal(
            proposal,
            requirement_by_id=requirement_by_id,
            label=f"reviewed_test_proposals.audit.decisions[{index}].proposal",
        )
        original_proposals.append(normalized_proposal)
        proposal_order.append(_standalone_proposal_order_key(normalized_proposal))

        action = decision["action"]
        assert isinstance(action, str)
        action_counts[action] += 1
        _validate_text(
            decision["rationale"],
            label=(
                f"reviewed_test_proposals.audit.decisions[{index}].rationale"
            ),
            max_bytes=_MAX_RATIONALE_BYTES,
        )
        replacement = decision["replacement"]
        if action in {"accept", "reject"}:
            if replacement is not None:
                raise TestProposalReviewError(
                    f"reviewed_test_proposals.audit.decisions[{index}].replacement: "
                    f"is not allowed for {action} decisions"
                )
            effective = normalized_proposal
        else:
            if not isinstance(replacement, Mapping):
                raise TestProposalReviewError(
                    f"reviewed_test_proposals.audit.decisions[{index}].replacement: "
                    "edit decisions require replacement content"
                )
            effective = _standalone_edited_proposal(
                normalized_proposal,
                replacement,
                requirement_by_id=requirement_by_id,
                index=index,
            )
            replacement_source = replacement["source"]
            assert isinstance(replacement_source, str)
            replacement_source_bytes += len(replacement_source.encode("utf-8"))
            if replacement_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
                raise TestProposalReviewError(
                    "reviewed_test_proposals.audit.decisions: edited sources "
                    f"exceed the {_MAX_TOTAL_SOURCE_BYTES}-byte aggregate limit"
                )
        if action == "reject":
            continue
        expected_accepted.append(
            {
                "origin_proposal_id": proposal_id,
                "review_action": action,
                "id": effective["id"],
                "requirement_id": effective["requirement_id"],
                "category": effective["category"],
                "test_file": effective["test_file"],
                "node": effective["node"],
                "rationale": effective["rationale"],
                "source": copy.deepcopy(effective["source"]),
                "assertion": copy.deepcopy(effective["assertion"]),
            }
        )

    if proposal_order != sorted(proposal_order):
        raise TestProposalReviewError(
            "reviewed_test_proposals.audit.decisions: embedded proposals are not "
            "in deterministic proposal order"
        )
    canonical_response_sha256 = hashlib.sha256(
        _standalone_canonical_response(original_proposals)
    ).hexdigest()
    if response_binding["canonical_sha256"] != canonical_response_sha256:
        raise TestProposalReviewError(
            "reviewed_test_proposals.generator_response.canonical_sha256: does "
            "not match the embedded proposal content"
        )
    _validate_effective_case_set(original_proposals)
    _validate_effective_case_set(expected_accepted)

    for action, field in (
        ("accept", "accepted_count"),
        ("edit", "edited_count"),
        ("reject", "rejected_count"),
    ):
        if audit[field] != action_counts[action]:
            raise TestProposalReviewError(
                f"reviewed_test_proposals.audit.{field}: does not match decisions"
            )
    if accepted_cases != expected_accepted:
        raise TestProposalReviewError(
            "reviewed_test_proposals.accepted_cases: do not exactly match the "
            "effective accepted and edited audit decisions"
        )

    expected_cases = [
        {
            "id": accepted["id"],
            "requirement_id": accepted["requirement_id"],
            "test_file": accepted["test_file"],
            "node": accepted["node"],
            "rationale": accepted["rationale"],
            "test_sha256": accepted["source"]["sha256"],
        }
        for accepted in expected_accepted
    ]
    if cases != expected_cases:
        raise TestProposalReviewError(
            "reviewed_test_proposals.cases: do not exactly project accepted_cases"
        )

    accepted_requirement_ids = {
        accepted["requirement_id"] for accepted in expected_accepted
    }
    expected_covered = [
        requirement_id
        for requirement_id in requirement_ids
        if requirement_id in accepted_requirement_ids
    ]
    expected_uncovered = [
        requirement_id
        for requirement_id in requirement_ids
        if requirement_id not in accepted_requirement_ids
    ]
    if coverage["accepted_requirement_ids"] != expected_covered:
        raise TestProposalReviewError(
            "reviewed_test_proposals.coverage.accepted_requirement_ids: must "
            "list covered requirements in reviewed order"
        )
    if coverage["unaccepted_requirement_ids"] != expected_uncovered:
        raise TestProposalReviewError(
            "reviewed_test_proposals.coverage.unaccepted_requirement_ids: must "
            "list uncovered requirements in reviewed order"
        )


def _validate_standalone_proposal(
    proposal: Mapping[str, Any],
    *,
    requirement_by_id: Mapping[str, Mapping[str, Any]],
    label: str,
) -> dict[str, Any]:
    _validate_materialization_test_file(proposal["test_file"], label=f"{label}.test_file")
    _validate_text(
        proposal["rationale"],
        label=f"{label}.rationale",
        max_bytes=_MAX_RATIONALE_BYTES,
    )
    _validate_embedded_proposal(proposal, index=0)
    requirement_id = proposal["requirement_id"]
    requirement = requirement_by_id.get(requirement_id)
    if requirement is None:
        raise TestProposalReviewError(
            f"{label}.requirement_id: references an unknown reviewed requirement"
        )
    assertion = proposal["assertion"]
    assert isinstance(assertion, Mapping)
    if assertion["requirement"] != requirement:
        raise TestProposalReviewError(
            f"{label}.assertion.requirement: does not exactly match the reviewed "
            "requirement"
        )
    category = proposal["category"]
    if category == "requested" and requirement["kind"] != "requested":
        raise TestProposalReviewError(
            f"{label}.category: requested cases require a requested requirement"
        )
    if category == "preservation" and requirement["kind"] != "preservation":
        raise TestProposalReviewError(
            f"{label}.category: preservation cases require a preservation requirement"
        )
    normalized = copy.deepcopy(dict(proposal))
    if normalized["id"] != _standalone_case_id(normalized):
        raise TestProposalReviewError(
            f"{label}.id: does not match canonical proposal content"
        )
    return normalized


def _standalone_edited_proposal(
    proposal: Mapping[str, Any],
    replacement: Mapping[str, Any],
    *,
    requirement_by_id: Mapping[str, Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    label = f"reviewed_test_proposals.audit.decisions[{index}].replacement"
    _validate_materialization_test_file(
        replacement["test_file"], label=f"{label}.test_file"
    )
    _validate_text(
        replacement["rationale"],
        label=f"{label}.rationale",
        max_bytes=_MAX_RATIONALE_BYTES,
    )
    source_bytes = _encode_utf8(replacement["source"], label=f"{label}.source")
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise TestProposalReviewError(
            f"{label}.source: exceeds the {_MAX_SOURCE_BYTES}-byte limit"
        )
    if dict(replacement) == _raw_effective_fields(proposal):
        raise TestProposalReviewError(f"{label}: edit does not change the proposal")
    try:
        validation = validate_candidate_test_source(
            source_bytes,
            case_id=proposal["id"],
            node=replacement["node"],
            source_name=replacement["test_file"],
        )
    except ValueError as exc:
        raise TestProposalReviewError(
            f"{label}.source: inadmissible candidate: {exc}"
        ) from exc
    if validation.python_assertion_count != 1:
        raise TestProposalReviewError(
            f"{label}.source: must contain exactly one Python assert in the "
            "entire candidate file"
        )
    requirement = requirement_by_id[proposal["requirement_id"]]
    effective: dict[str, Any] = {
        "requirement_id": proposal["requirement_id"],
        "category": proposal["category"],
        "test_file": replacement["test_file"],
        "node": replacement["node"],
        "rationale": replacement["rationale"],
        "review_status": "requires_review",
        "source": {
            "text": replacement["source"],
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "assertion": {
            "kind": "python_assert",
            "requirement": copy.deepcopy(requirement),
            "start_line": validation.assertion_start_line,
            "end_line": validation.assertion_end_line,
            "start_column": validation.assertion_start_column,
            "end_column": validation.assertion_end_column,
            "excerpt": validation.assertion_excerpt,
            "sha256": validation.assertion_sha256,
        },
    }
    effective["id"] = _standalone_case_id(effective)
    return effective


def _standalone_case_id(proposal: Mapping[str, Any]) -> str:
    identity = {key: value for key, value in proposal.items() if key != "id"}
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "case-" + hashlib.sha256(canonical).hexdigest()[:59]


def _standalone_proposal_order_key(
    proposal: Mapping[str, Any],
) -> tuple[object, ...]:
    return (
        proposal["requirement_id"],
        _CATEGORY_ORDER[str(proposal["category"])],
        proposal["test_file"],
        proposal["node"],
        proposal["id"],
    )


def _standalone_canonical_response(
    proposals: Sequence[Mapping[str, Any]],
) -> bytes:
    raw = [
        {
            "category": proposal["category"],
            "node": proposal["node"],
            "rationale": proposal["rationale"],
            "requirement_id": proposal["requirement_id"],
            "source": proposal["source"]["text"],
            "test_file": proposal["test_file"],
        }
        for proposal in proposals
    ]
    raw.sort(
        key=lambda proposal: json.dumps(
            proposal,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return json.dumps(
        {"schema_version": 1, "proposals": raw},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_materialization_test_file(value: object, *, label: str) -> None:
    if not isinstance(value, str):
        raise TestProposalReviewError(f"{label}: must be a string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix != ".py"
        or any(character in value for character in "\\\r\n\0")
        or len(path.parts) > 32
    ):
        raise TestProposalReviewError(
            f"{label}: must be a canonical safe relative .py path"
        )
    for component in path.parts:
        if (
            not _PORTABLE_PATH_COMPONENT.fullmatch(component)
            or component.endswith(".")
            or len(component.encode("ascii")) > 255
            or component.split(".", 1)[0].casefold()
            in _WINDOWS_RESERVED_COMPONENTS
        ):
            raise TestProposalReviewError(
                f"{label}: contains a non-portable component {component!r}"
            )
    if path.parts[0].casefold() in _RESERVED_ROOT_COMPONENTS:
        raise TestProposalReviewError(
            f"{label}: conflicts with a reserved materialization output"
        )


def validate_test_materialization_artifact(payload: object) -> None:
    """Validate a completion manifest without reading its referenced files.

    This checks the closed schema, portable path relationships, uniqueness,
    and aggregate byte bounds.  A handoff loader must additionally hash every
    referenced file before treating the manifest as authenticated.
    """

    _validate_materialization_schema(payload)
    assert isinstance(payload, Mapping)
    finalized_binding = payload["reviewed_test_proposals"]
    assert isinstance(finalized_binding, Mapping)
    if finalized_binding["path"] != "reviewed-test-proposals.json":
        raise TestProposalReviewError(
            "materialization.reviewed_test_proposals.path: expected "
            "'reviewed-test-proposals.json'"
        )
    records = payload["files"]
    assert isinstance(records, list)
    seen_ids: set[str] = set()
    paths: list[tuple[str, ...]] = []
    directory_spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    total_bytes = 0
    for index, record in enumerate(records):
        assert isinstance(record, Mapping)
        case_id = record["case_id"]
        path_value = record["path"]
        assert isinstance(case_id, str)
        assert isinstance(path_value, str)
        if case_id in seen_ids:
            raise TestProposalReviewError(
                f"materialization.files[{index}].case_id: is duplicated"
            )
        seen_ids.add(case_id)
        path = PurePosixPath(path_value)
        if (
            path.is_absolute()
            or path.as_posix() != path_value
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".py"
            or any(character in path_value for character in "\\\r\n\0")
        ):
            raise TestProposalReviewError(
                f"materialization.files[{index}].path: must be a canonical "
                "safe relative .py path"
            )
        if len(path.parts) > 32:
            raise TestProposalReviewError(
                f"materialization.files[{index}].path: exceeds 32 components"
            )
        for component in path.parts:
            if (
                not _PORTABLE_PATH_COMPONENT.fullmatch(component)
                or component.endswith(".")
                or len(component.encode("ascii")) > 255
                or component.split(".", 1)[0].casefold()
                in _WINDOWS_RESERVED_COMPONENTS
            ):
                raise TestProposalReviewError(
                    f"materialization.files[{index}].path: contains a "
                    f"non-portable component {component!r}"
                )
        folded = tuple(part.casefold() for part in path.parts)
        if folded[0] in _RESERVED_ROOT_COMPONENTS:
            raise TestProposalReviewError(
                f"materialization.files[{index}].path: conflicts with a "
                "reserved output"
            )
        for length in range(1, len(folded)):
            prefix = folded[:length]
            spelling = tuple(path.parts[:length])
            prior_spelling = directory_spellings.get(prefix)
            if prior_spelling is not None and prior_spelling != spelling:
                raise TestProposalReviewError(
                    f"materialization.files[{index}].path: portable directory "
                    "spelling collision"
                )
            directory_spellings[prefix] = spelling
            if len(directory_spellings) > _MAX_DIRECTORY_PREFIXES:
                raise TestProposalReviewError(
                    "materialization.files: exceeds the 256-directory-prefix limit"
                )
        for prior in paths:
            shortest = min(len(folded), len(prior))
            if folded == prior or folded[:shortest] == prior[:shortest]:
                raise TestProposalReviewError(
                    f"materialization.files[{index}].path: portable duplicate "
                    "or file/ancestor conflict"
                )
        paths.append(folded)
        total_bytes += record["size"]
        if total_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise TestProposalReviewError(
                "materialization.files: sizes exceed the aggregate source limit"
            )


def encode_test_review_artifact(payload: Mapping[str, object]) -> bytes:
    """Encode a review artifact in deterministic on-disk form."""

    try:
        return (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TestProposalReviewError(
            f"cannot encode test review artifact: {exc}"
        ) from exc


def write_test_proposal_review(
    proposal_file: Path,
    reviewed_requirements_file: Path,
    issue_file: Path,
    output_file: Path,
    *,
    generator_response_file: Path | None = None,
    expected_proposal_sha256: str | None = None,
    expected_reviewed_requirements_sha256: str | None = None,
    expected_issue_sha256: str | None = None,
    expected_generator_response_sha256: str | None = None,
) -> TestReviewPreparationResult:
    """Read exact inputs once and exclusively create a pending review file."""

    proposal_bytes = _read_bounded_file(
        proposal_file,
        label="test proposal artifact",
        max_bytes=_MAX_PROPOSAL_DOCUMENT_BYTES,
    )
    reviewed_bytes = _read_bounded_file(
        reviewed_requirements_file,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    issue_bytes = _read_bounded_file(
        issue_file,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    response_bytes = _read_optional_response(generator_response_file)
    _require_expected_digest(
        proposal_bytes, expected_proposal_sha256, label="test proposal artifact"
    )
    _require_expected_digest(
        reviewed_bytes,
        expected_reviewed_requirements_sha256,
        label="reviewed requirements",
    )
    _require_expected_digest(issue_bytes, expected_issue_sha256, label="issue")
    if response_bytes is not None:
        _require_expected_digest(
            response_bytes,
            expected_generator_response_sha256,
            label="generator response",
        )
    elif expected_generator_response_sha256 is not None:
        raise TestProposalReviewError(
            "expected generator response SHA-256 requires a response file"
        )
    ledger = prepare_test_proposal_review(
        proposal_bytes,
        reviewed_requirements_document=reviewed_bytes,
        issue_document=issue_bytes,
        generator_response_document=response_bytes,
    )
    document = encode_test_review_artifact(ledger)
    _require_document_limit(
        document, label="test proposal review", limit=_MAX_REVIEW_DOCUMENT_BYTES
    )
    output_path = Path(output_file).absolute()
    _write_exclusive(output_path, document)
    return TestReviewPreparationResult(
        output_file=output_path,
        proposal_sha256=hashlib.sha256(proposal_bytes).hexdigest(),
        reviewed_requirements_sha256=hashlib.sha256(reviewed_bytes).hexdigest(),
        issue_sha256=hashlib.sha256(issue_bytes).hexdigest(),
        proposal_count=len(ledger["decisions"]),
    )


def write_reviewed_test_proposals(
    proposal_file: Path,
    reviewed_requirements_file: Path,
    review_file: Path,
    issue_file: Path,
    output_file: Path,
    *,
    generator_response_file: Path | None = None,
    expected_proposal_sha256: str | None = None,
    expected_reviewed_requirements_sha256: str | None = None,
    expected_review_sha256: str | None = None,
    expected_issue_sha256: str | None = None,
    expected_generator_response_sha256: str | None = None,
) -> TestReviewFinalizationResult:
    """Read each exact input once and exclusively write the finalized audit."""

    proposal_bytes = _read_bounded_file(
        proposal_file,
        label="test proposal artifact",
        max_bytes=_MAX_PROPOSAL_DOCUMENT_BYTES,
    )
    reviewed_bytes = _read_bounded_file(
        reviewed_requirements_file,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    review_bytes = _read_bounded_file(
        review_file,
        label="test proposal review",
        max_bytes=_MAX_REVIEW_DOCUMENT_BYTES,
    )
    issue_bytes = _read_bounded_file(
        issue_file,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    response_bytes = _read_optional_response(generator_response_file)
    _require_expected_digest(
        proposal_bytes, expected_proposal_sha256, label="test proposal artifact"
    )
    _require_expected_digest(
        reviewed_bytes,
        expected_reviewed_requirements_sha256,
        label="reviewed requirements",
    )
    _require_expected_digest(
        review_bytes, expected_review_sha256, label="test proposal review"
    )
    _require_expected_digest(issue_bytes, expected_issue_sha256, label="issue")
    if response_bytes is not None:
        _require_expected_digest(
            response_bytes,
            expected_generator_response_sha256,
            label="generator response",
        )
    elif expected_generator_response_sha256 is not None:
        raise TestProposalReviewError(
            "expected generator response SHA-256 requires a response file"
        )
    finalized = finalize_test_proposal_review(
        proposal_bytes,
        review_bytes,
        reviewed_requirements_document=reviewed_bytes,
        issue_document=issue_bytes,
        generator_response_document=response_bytes,
    )
    document = encode_test_review_artifact(finalized)
    _require_document_limit(
        document,
        label="reviewed test proposals",
        limit=_MAX_FINAL_DOCUMENT_BYTES,
    )
    output_path = Path(output_file).absolute()
    _write_exclusive(output_path, document)
    audit = finalized["audit"]
    assert isinstance(audit, Mapping)
    return TestReviewFinalizationResult(
        output_file=output_path,
        proposal_sha256=hashlib.sha256(proposal_bytes).hexdigest(),
        review_sha256=hashlib.sha256(review_bytes).hexdigest(),
        issue_sha256=hashlib.sha256(issue_bytes).hexdigest(),
        accepted_case_count=len(finalized["cases"]),
        rejected_count=audit["rejected_count"],
    )


def materialize_reviewed_test_proposals(
    reviewed_test_proposals_document: bytes,
    *,
    proposal_document: bytes,
    reviewed_requirements_document: bytes,
    review_document: bytes,
    issue_document: bytes,
    output_directory: Path,
    generator_response_document: bytes | None = None,
) -> TestMaterializationResult:
    """Create a complete inert staging directory without running candidates."""

    finalized_bytes = _require_bytes(
        reviewed_test_proposals_document,
        label="reviewed test proposals",
        max_bytes=_MAX_FINAL_DOCUMENT_BYTES,
    )
    finalized = _decode_json(finalized_bytes, label="reviewed test proposals")
    issue_bytes = _require_bytes(
        issue_document,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    try:
        issue_text = issue_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TestProposalReviewError("issue must be UTF-8") from exc
    validate_reviewed_test_proposals_artifact(
        finalized,
        proposal_document=proposal_document,
        reviewed_requirements_document=reviewed_requirements_document,
        review_document=review_document,
        issue_document=issue_bytes,
        generator_response_document=generator_response_document,
    )
    accepted_cases = finalized["accepted_cases"]
    assert isinstance(accepted_cases, list)
    if not accepted_cases:
        raise TestProposalReviewError(
            "reviewed test proposals contain no accepted cases; an "
            "EvidencePlan cannot be materialized"
        )
    plan = {
        "schema_version": 2,
        "requirements": finalized["requirements"],
        "cases": finalized["cases"],
    }
    plan_document = encode_test_review_artifact(plan)
    _require_document_limit(
        plan_document,
        label="evidence plan",
        limit=_MAX_PLAN_DOCUMENT_BYTES,
    )
    plan_sha256 = hashlib.sha256(plan_document).hexdigest()
    finalized_sha256 = hashlib.sha256(finalized_bytes).hexdigest()
    output_path = Path(output_directory).absolute()
    root_descriptor, output_identity = _reserve_output_directory(output_path)
    try:
        file_records: list[dict[str, object]] = []
        candidate_documents: dict[str, bytes] = {}
        total_source_bytes = 0
        for case in accepted_cases:
            assert isinstance(case, Mapping)
            source = case["source"]
            assert isinstance(source, Mapping)
            source_bytes = _encode_utf8(
                source["text"], label=f"accepted case {case['id']} source"
            )
            if hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
                raise TestProposalReviewError(
                    f"accepted case {case['id']} source changed after finalization"
                )
            total_source_bytes += len(source_bytes)
            if total_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
                raise TestProposalReviewError(
                    "accepted sources exceed the aggregate byte limit"
                )
            _write_new_file(
                root_descriptor,
                PurePosixPath(case["test_file"]),
                source_bytes,
                display_root=output_path,
            )
            candidate_documents[case["test_file"]] = source_bytes
            file_records.append(
                {
                    "case_id": case["id"],
                    "path": case["test_file"],
                    "sha256": source["sha256"],
                    "size": len(source_bytes),
                }
            )

        plan_path = output_path / "evidence-plan.json"
        _write_new_file(
            root_descriptor,
            PurePosixPath("evidence-plan.json"),
            plan_document,
            display_root=output_path,
        )
        issue_path = output_path / "issue.md"
        _write_new_file(
            root_descriptor,
            PurePosixPath("issue.md"),
            issue_bytes,
            display_root=output_path,
        )
        finalized_path = output_path / "reviewed-test-proposals.json"
        _write_new_file(
            root_descriptor,
            PurePosixPath("reviewed-test-proposals.json"),
            finalized_bytes,
            display_root=output_path,
        )
        try:
            load_evidence_plan(
                plan_path,
                issue_text=issue_text,
                document_bytes=plan_document,
                candidate_source_documents=candidate_documents,
            )
        except ValueError as exc:
            raise TestProposalReviewError(
                f"materialized EvidencePlan is invalid: {exc}"
            ) from exc
        manifest = {
            "schema_version": 1,
            "artifact_type": "test-materialization",
            "complete": True,
            "reviewed_test_proposals": {
                "path": "reviewed-test-proposals.json",
                "sha256": finalized_sha256,
                "size": len(finalized_bytes),
            },
            "proposal_artifact": copy.deepcopy(finalized["proposal_artifact"]),
            "reviewed_requirements": {
                "sha256": finalized["reviewed_requirements"]["sha256"]
            },
            "review": copy.deepcopy(finalized["review"]),
            "issue": {
                "path": "issue.md",
                "sha256": hashlib.sha256(issue_bytes).hexdigest(),
                "size": len(issue_bytes),
            },
            "evidence_plan": {
                "path": "evidence-plan.json",
                "schema_version": 2,
                "sha256": plan_sha256,
                "size": len(plan_document),
            },
            "files": file_records,
        }
        validate_test_materialization_artifact(manifest)
        _verify_staging_before_commit(
            root_descriptor,
            output_path,
            manifest,
            expected_root_identity=output_identity,
        )
        manifest_document = encode_test_review_artifact(manifest)
        manifest_path = output_path / "materialization.json"
        _write_manifest_exclusive(
            root_descriptor,
            manifest_path,
            manifest_document,
            expected_parent_identity=output_identity,
        )
    finally:
        os.close(root_descriptor)
    return TestMaterializationResult(
        output_directory=output_path,
        evidence_plan_file=plan_path,
        manifest_file=manifest_path,
        reviewed_test_proposals_sha256=finalized_sha256,
        evidence_plan_sha256=plan_sha256,
        case_count=len(accepted_cases),
    )


def materialize_reviewed_test_proposal_files(
    reviewed_test_proposals_file: Path,
    proposal_file: Path,
    reviewed_requirements_file: Path,
    review_file: Path,
    issue_file: Path,
    output_directory: Path,
    *,
    generator_response_file: Path | None = None,
    expected_reviewed_test_proposals_sha256: str | None = None,
    expected_proposal_sha256: str | None = None,
    expected_reviewed_requirements_sha256: str | None = None,
    expected_review_sha256: str | None = None,
    expected_issue_sha256: str | None = None,
    expected_generator_response_sha256: str | None = None,
) -> TestMaterializationResult:
    """Read all bounded inputs once, authenticate them, and stage accepted tests."""

    finalized_bytes = _read_bounded_file(
        reviewed_test_proposals_file,
        label="reviewed test proposals",
        max_bytes=_MAX_FINAL_DOCUMENT_BYTES,
    )
    proposal_bytes = _read_bounded_file(
        proposal_file,
        label="test proposal artifact",
        max_bytes=_MAX_PROPOSAL_DOCUMENT_BYTES,
    )
    reviewed_bytes = _read_bounded_file(
        reviewed_requirements_file,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    review_bytes = _read_bounded_file(
        review_file,
        label="test proposal review",
        max_bytes=_MAX_REVIEW_DOCUMENT_BYTES,
    )
    issue_bytes = _read_bounded_file(
        issue_file,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    response_bytes = _read_optional_response(generator_response_file)
    for document, expected, label in (
        (
            finalized_bytes,
            expected_reviewed_test_proposals_sha256,
            "reviewed test proposals",
        ),
        (proposal_bytes, expected_proposal_sha256, "test proposal artifact"),
        (
            reviewed_bytes,
            expected_reviewed_requirements_sha256,
            "reviewed requirements",
        ),
        (review_bytes, expected_review_sha256, "test proposal review"),
        (issue_bytes, expected_issue_sha256, "issue"),
    ):
        _require_expected_digest(document, expected, label=label)
    if response_bytes is not None:
        _require_expected_digest(
            response_bytes,
            expected_generator_response_sha256,
            label="generator response",
        )
    elif expected_generator_response_sha256 is not None:
        raise TestProposalReviewError(
            "expected generator response SHA-256 requires a response file"
        )
    return materialize_reviewed_test_proposals(
        finalized_bytes,
        proposal_document=proposal_bytes,
        reviewed_requirements_document=reviewed_bytes,
        review_document=review_bytes,
        issue_document=issue_bytes,
        generator_response_document=response_bytes,
        output_directory=output_directory,
    )


def _build_finalized_artifact(
    proposal_document: bytes,
    review_document: bytes,
    *,
    reviewed_requirements_document: bytes,
    issue_document: bytes,
    generator_response_document: bytes | None,
) -> dict[str, Any]:
    context = _load_proposal_context(
        proposal_document,
        reviewed_requirements_document=reviewed_requirements_document,
        issue_document=issue_document,
        generator_response_document=generator_response_document,
    )
    review_bytes = _require_bytes(
        review_document,
        label="test proposal review",
        max_bytes=_MAX_REVIEW_DOCUMENT_BYTES,
    )
    review = _decode_json(review_bytes, label="test proposal review")
    validate_test_proposal_review_artifact(review)
    _authenticate_review_bindings(context, review)

    proposals = context.proposal["proposals"]
    decisions = review["decisions"]
    assert isinstance(proposals, list)
    assert isinstance(decisions, list)
    proposal_ids = [proposal["id"] for proposal in proposals]
    decision_ids = [decision["proposal_id"] for decision in decisions]
    if decision_ids != proposal_ids:
        if set(decision_ids) != set(proposal_ids):
            unknown = sorted(set(decision_ids) - set(proposal_ids))
            missing = sorted(set(proposal_ids) - set(decision_ids))
            detail = (
                f"unknown proposal {unknown[0]}"
                if unknown
                else f"missing decision for proposal {missing[0]}"
            )
            raise TestProposalReviewError(f"test proposal review contains {detail}")
        raise TestProposalReviewError(
            "test proposal review decisions must remain in deterministic "
            "proposal order"
        )
    for index, (proposal, decision) in enumerate(zip(proposals, decisions)):
        if decision["proposal"] != proposal:
            raise TestProposalReviewError(
                f"test proposal review decision {index} proposal preview does "
                "not exactly match the bound proposal artifact"
            )
    pending = [
        decision["proposal_id"]
        for decision in decisions
        if decision["action"] == "pending"
    ]
    if pending:
        raise TestProposalReviewError(
            f"test proposal review has pending decision for proposal {pending[0]}"
        )

    accepted_cases: list[dict[str, Any]] = []
    counts = {"accept": 0, "edit": 0, "reject": 0}
    for index, decision in enumerate(decisions):
        action = decision["action"]
        counts[action] += 1
        if action == "reject":
            continue
        effective = _effective_proposal(
            context,
            decision,
            index=index,
        )
        record = {
            "origin_proposal_id": decision["proposal_id"],
            "review_action": action,
            "id": effective["id"],
            "requirement_id": effective["requirement_id"],
            "category": effective["category"],
            "test_file": effective["test_file"],
            "node": effective["node"],
            "rationale": effective["rationale"],
            "source": copy.deepcopy(effective["source"]),
            "assertion": copy.deepcopy(effective["assertion"]),
        }
        accepted_cases.append(record)
    _validate_effective_case_set(accepted_cases)

    accepted_requirement_ids = {
        case["requirement_id"] for case in accepted_cases
    }
    all_requirements = context.reviewed["requirements"]
    assert isinstance(all_requirements, list)
    requirements = copy.deepcopy(all_requirements)
    accepted_ids_in_order = [
        requirement["id"]
        for requirement in all_requirements
        if requirement["id"] in accepted_requirement_ids
    ]
    unaccepted_ids_in_order = [
        requirement["id"]
        for requirement in all_requirements
        if requirement["id"] not in accepted_requirement_ids
    ]
    cases = [
        {
            "id": case["id"],
            "requirement_id": case["requirement_id"],
            "test_file": case["test_file"],
            "node": case["node"],
            "rationale": case["rationale"],
            "test_sha256": case["source"]["sha256"],
        }
        for case in accepted_cases
    ]
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "reviewed-test-proposals",
        "proposal_artifact": copy.deepcopy(review["proposal_artifact"]),
        "reviewed_requirements": copy.deepcopy(review["reviewed_requirements"]),
        "issue": copy.deepcopy(review["issue"]),
        "generator_response": copy.deepcopy(review["generator_response"]),
        "review": {
            "sha256": hashlib.sha256(review_bytes).hexdigest(),
            "schema_version": review["schema_version"],
        },
        "coverage": {
            "accepted_requirement_ids": accepted_ids_in_order,
            "unaccepted_requirement_ids": unaccepted_ids_in_order,
        },
        "requirements": requirements,
        "cases": cases,
        "accepted_cases": accepted_cases,
        "audit": {
            "proposal_count": len(proposals),
            "accepted_count": counts["accept"],
            "edited_count": counts["edit"],
            "rejected_count": counts["reject"],
            "decisions": copy.deepcopy(decisions),
        },
    }
    if cases:
        plan_document = encode_test_review_artifact(
            {
                "schema_version": 2,
                "requirements": requirements,
                "cases": cases,
            }
        )
        _require_document_limit(
            plan_document,
            label="evidence plan",
            limit=_MAX_PLAN_DOCUMENT_BYTES,
        )
    return result


def _effective_proposal(
    context: _ProposalContext,
    decision: Mapping[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    proposal = decision["proposal"]
    assert isinstance(proposal, Mapping)
    if decision["action"] == "accept":
        return copy.deepcopy(dict(proposal))
    replacement = decision["replacement"]
    assert isinstance(replacement, Mapping)
    raw = {
        "requirement_id": proposal["requirement_id"],
        "category": proposal["category"],
        "test_file": replacement["test_file"],
        "node": replacement["node"],
        "rationale": replacement["rationale"],
        "source": replacement["source"],
    }
    generator = context.proposal["generator"]
    assert isinstance(generator, Mapping)
    try:
        rebuilt = build_test_proposal_artifact(
            context.reviewed_bytes,
            generator_adapter=generator["adapter"],
            generator_model=generator["model"],
            proposals=[raw],
        )
    except TestProposalError as exc:
        raise TestProposalReviewError(
            f"test proposal review decision {index} replacement is invalid: {exc}"
        ) from exc
    rebuilt_proposals = rebuilt["proposals"]
    assert isinstance(rebuilt_proposals, list)
    effective = rebuilt_proposals[0]
    assert isinstance(effective, dict)
    return effective


def _load_proposal_context(
    proposal_document: bytes,
    *,
    reviewed_requirements_document: bytes,
    issue_document: bytes,
    generator_response_document: bytes | None,
) -> _ProposalContext:
    proposal_bytes = _require_bytes(
        proposal_document,
        label="test proposal artifact",
        max_bytes=_MAX_PROPOSAL_DOCUMENT_BYTES,
    )
    reviewed_bytes = _require_bytes(
        reviewed_requirements_document,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    issue_bytes = _require_bytes(
        issue_document,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    response_bytes = None
    if generator_response_document is not None:
        response_bytes = _require_bytes(
            generator_response_document,
            label="generator response",
            max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
        )
    proposal = _decode_json(proposal_bytes, label="test proposal artifact")
    reviewed = _decode_json(reviewed_bytes, label="reviewed requirements")
    try:
        validate_reviewed_requirements_artifact(reviewed)
        validate_test_proposal_artifact(
            proposal,
            reviewed_requirements_document=reviewed_bytes,
            generator_response_document=response_bytes,
        )
    except ValueError as exc:
        raise TestProposalReviewError(
            f"test proposal inputs are invalid: {exc}"
        ) from exc
    _validate_issue_provenance(reviewed, issue_bytes)
    response = proposal["generator_response"]
    assert isinstance(response, Mapping)
    if response["binding"] == "exact" and response_bytes is None:
        raise TestProposalReviewError(
            "exact-bound test proposals require the original generator "
            "response bytes"
        )
    return _ProposalContext(
        proposal_bytes=proposal_bytes,
        proposal=proposal,
        reviewed_bytes=reviewed_bytes,
        reviewed=reviewed,
        issue_bytes=issue_bytes,
        response_bytes=response_bytes,
    )


def _validate_issue_provenance(
    reviewed: Mapping[str, Any],
    issue_bytes: bytes,
) -> None:
    try:
        issue_text = issue_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TestProposalReviewError("issue must be UTF-8") from exc
    binding = reviewed.get("issue")
    if not isinstance(binding, Mapping):
        raise TestProposalReviewError(
            "reviewed requirements do not contain an exact issue binding; "
            "regenerate them before reviewing generated tests"
        )
    observed_sha256 = hashlib.sha256(issue_bytes).hexdigest()
    if binding.get("sha256") != observed_sha256:
        raise TestProposalReviewError(
            "issue SHA-256 does not match the exact issue bound by the "
            "reviewed requirements"
        )
    lines = issue_text.splitlines()
    requirements = reviewed["requirements"]
    assert isinstance(requirements, list)
    for requirement in requirements:
        assert isinstance(requirement, Mapping)
        if requirement["provenance"] != "explicit":
            continue
        source = requirement["source"]
        excerpt = requirement["excerpt"]
        assert isinstance(source, str)
        assert isinstance(excerpt, str)
        match = re.fullmatch(
            r"issue(?:\.md)?:([1-9][0-9]*)",
            source,
            re.IGNORECASE,
        )
        if match is None:
            raise TestProposalReviewError(
                f"explicit requirement {requirement['id']!r} must cite "
                "issue:<line>"
            )
        line = int(match.group(1))
        excerpt_lines = excerpt.splitlines() or [excerpt]
        if line > len(lines):
            matches = False
        else:
            selected = "\n".join(
                lines[line - 1 : line - 1 + len(excerpt_lines)]
            )
            matches = excerpt in selected
        if not matches:
            raise TestProposalReviewError(
                f"explicit requirement {requirement['id']!r} excerpt is not "
                f"present at {source} in the exact issue bytes"
            )


def _review_response_binding(context: _ProposalContext) -> dict[str, object]:
    response = context.proposal["generator_response"]
    assert isinstance(response, Mapping)
    return {
        "binding": response["binding"],
        "sha256": response["sha256"],
        "canonical_sha256": response["canonical_sha256"],
        "supplied_sha256": (
            hashlib.sha256(context.response_bytes).hexdigest()
            if context.response_bytes is not None
            else None
        ),
    }


def _issue_binding(issue_bytes: bytes) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(issue_bytes).hexdigest(),
        "size": len(issue_bytes),
    }


def _authenticate_review_bindings(
    context: _ProposalContext,
    review: Mapping[str, Any],
) -> None:
    expected_proposal = {
        "sha256": hashlib.sha256(context.proposal_bytes).hexdigest(),
        "schema_version": context.proposal["schema_version"],
        "proposal_count": len(context.proposal["proposals"]),
    }
    if review["proposal_artifact"] != expected_proposal:
        raise TestProposalReviewError(
            "test proposal review proposal binding does not match the exact "
            "proposal artifact bytes"
        )
    if review["reviewed_requirements"] != context.proposal["reviewed_requirements"]:
        raise TestProposalReviewError(
            "test proposal review reviewed-requirements binding does not match "
            "the proposal artifact"
        )
    if review["issue"] != _issue_binding(context.issue_bytes):
        raise TestProposalReviewError(
            "test proposal review issue binding does not match the exact "
            "issue bytes"
        )
    if review["generator_response"] != _review_response_binding(context):
        raise TestProposalReviewError(
            "test proposal review generator-response binding does not match "
            "the authenticated response"
        )


def _validate_embedded_proposal(
    proposal: Mapping[str, Any],
    *,
    index: int,
) -> None:
    source = proposal["source"]
    assertion = proposal["assertion"]
    assert isinstance(source, Mapping)
    assert isinstance(assertion, Mapping)
    source_bytes = _encode_utf8(
        source["text"],
        label=f"test_proposal_review.decisions[{index}].proposal.source.text",
    )
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise TestProposalReviewError(
            f"test_proposal_review.decisions[{index}].proposal.source.text: "
            f"exceeds the {_MAX_SOURCE_BYTES}-byte limit"
        )
    if hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
        raise TestProposalReviewError(
            f"test_proposal_review.decisions[{index}].proposal.source.sha256: "
            "does not match exact source text"
        )
    try:
        validation = validate_candidate_test_source(
            source_bytes,
            case_id=proposal["id"],
            node=proposal["node"],
            source_name=proposal["test_file"],
        )
    except ValueError as exc:
        raise TestProposalReviewError(
            f"test_proposal_review.decisions[{index}].proposal.source: "
            f"inadmissible candidate: {exc}"
        ) from exc
    expected_assertion = {
        "kind": "python_assert",
        "requirement": assertion["requirement"],
        "start_line": validation.assertion_start_line,
        "end_line": validation.assertion_end_line,
        "start_column": validation.assertion_start_column,
        "end_column": validation.assertion_end_column,
        "excerpt": validation.assertion_excerpt,
        "sha256": validation.assertion_sha256,
    }
    if validation.python_assertion_count != 1 or assertion != expected_assertion:
        raise TestProposalReviewError(
            f"test_proposal_review.decisions[{index}].proposal.assertion: does "
            "not match the sole Python assert"
        )
    if assertion["requirement"]["id"] != proposal["requirement_id"]:
        raise TestProposalReviewError(
            f"test_proposal_review.decisions[{index}].proposal.assertion: "
            "requirement does not match requirement_id"
        )


def _validate_effective_case_set(cases: list[dict[str, Any]]) -> None:
    seen_ids: dict[str, int] = {}
    paths: list[tuple[tuple[str, ...], tuple[str, ...], int]] = []
    directory_spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    total_source_bytes = 0
    for index, case in enumerate(cases):
        identifier = case["id"]
        if identifier in seen_ids:
            raise TestProposalReviewError(
                f"accepted case {identifier!r} collides with accepted case at "
                f"index {seen_ids[identifier]}"
            )
        seen_ids[identifier] = index
        source = case["source"]
        assert isinstance(source, Mapping)
        source_bytes = _encode_utf8(
            source["text"], label=f"accepted case {identifier} source"
        )
        total_source_bytes += len(source_bytes)
        if total_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise TestProposalReviewError(
                "accepted case sources exceed the "
                f"{_MAX_TOTAL_SOURCE_BYTES}-byte aggregate limit"
            )
        raw_parts = PurePosixPath(case["test_file"]).parts
        if len(raw_parts) > 32:
            raise TestProposalReviewError(
                f"accepted case {identifier!r} test_file exceeds 32 path components"
            )
        folded = tuple(part.casefold() for part in raw_parts)
        if folded[0] in _RESERVED_ROOT_COMPONENTS:
            raise TestProposalReviewError(
                f"accepted case {identifier!r} test_file conflicts with "
                f"reserved output {raw_parts[0]!r}"
            )
        for length in range(1, len(folded)):
            prefix = folded[:length]
            spelling = tuple(raw_parts[:length])
            previous = directory_spellings.get(prefix)
            if previous is not None and previous != spelling:
                raise TestProposalReviewError(
                    f"accepted case {identifier!r} test_file has a portable "
                    "directory spelling collision"
                )
            directory_spellings[prefix] = spelling
            if len(directory_spellings) > _MAX_DIRECTORY_PREFIXES:
                raise TestProposalReviewError(
                    "accepted case paths exceed the 256-directory-prefix limit"
                )
        for previous_folded, previous_raw, previous_index in paths:
            if folded == previous_folded:
                raise TestProposalReviewError(
                    f"accepted case {identifier!r} test_file has a portable "
                    f"collision with accepted case {previous_index}"
                )
            shortest = min(len(folded), len(previous_folded))
            if folded[:shortest] == previous_folded[:shortest]:
                raise TestProposalReviewError(
                    f"accepted case {identifier!r} test_file has a file/ancestor "
                    f"conflict with {'/'.join(previous_raw)!r}"
                )
        paths.append((folded, tuple(raw_parts), index))


def _raw_effective_fields(proposal: Mapping[str, Any]) -> dict[str, object]:
    source = proposal["source"]
    assert isinstance(source, Mapping)
    return {
        "test_file": proposal["test_file"],
        "node": proposal["node"],
        "rationale": proposal["rationale"],
        "source": source["text"],
    }


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _reserve_output_directory(path: Path) -> tuple[int, tuple[int, int]]:
    """Exclusively reserve ``path`` and return its held directory descriptor."""

    if not _SECURE_DIR_FD_AVAILABLE:
        raise TestProposalReviewError(
            "secure materialization is unavailable on this platform"
        )
    parent_descriptor = -1
    root_descriptor = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_descriptor = os.open(path.parent, _directory_open_flags())
        os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
        root_descriptor = os.open(
            path.name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
        root_status = os.fstat(root_descriptor)
        live_status = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        identity = (root_status.st_dev, root_status.st_ino)
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or not stat.S_ISDIR(live_status.st_mode)
            or (live_status.st_dev, live_status.st_ino) != identity
        ):
            raise TestProposalReviewError(
                f"reserved materialization output changed during creation: {path}"
            )
        result = root_descriptor
        root_descriptor = -1
        return result, identity
    except FileExistsError as exc:
        raise TestProposalReviewError(
            f"materialization output already exists: {path}"
        ) from exc
    except TestProposalReviewError:
        raise
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot reserve materialization output {path}: {exc}"
        ) from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _write_manifest_exclusive(
    root_descriptor: int,
    path: Path,
    document: bytes,
    *,
    expected_parent_identity: tuple[int, int],
) -> None:
    """Publish the completion marker inside the exact reserved directory."""

    if not _SECURE_DIR_FD_AVAILABLE:
        raise TestProposalReviewError(
            "secure manifest publication is unavailable on this platform"
        )
    temporary_name: str | None = None
    published = False
    committed = False
    published_identity: tuple[int, int] | None = None
    descriptor = -1
    try:
        parent_status = os.fstat(root_descriptor)
        if (parent_status.st_dev, parent_status.st_ino) != expected_parent_identity:
            raise TestProposalReviewError(
                "reserved materialization output changed before manifest commit"
            )
        _require_live_staging_identity(path.parent, expected_parent_identity)
        for _ in range(128):
            candidate = f".{path.name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=root_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor < 0 or temporary_name is None:
            raise TestProposalReviewError(
                "cannot allocate a unique temporary manifest name"
            )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_status = os.fstat(stream.fileno())
            published_identity = (
                temporary_status.st_dev,
                temporary_status.st_ino,
            )
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        published = True
        live_parent = os.fstat(root_descriptor)
        live_marker = os.stat(
            path.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            (live_parent.st_dev, live_parent.st_ino) != expected_parent_identity
            or (live_marker.st_dev, live_marker.st_ino) != published_identity
        ):
            raise TestProposalReviewError(
                "reserved materialization output changed during manifest commit"
            )
        _require_live_staging_identity(path.parent, expected_parent_identity)
        os.unlink(temporary_name, dir_fd=root_descriptor)
        temporary_name = None
        committed = True
    except FileExistsError as exc:
        raise TestProposalReviewError(
            f"output file already exists: {path}"
        ) from exc
    except TestProposalReviewError:
        raise
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot write output file {path}: {exc}"
        ) from exc
    finally:
        if published and not committed and published_identity is not None:
            try:
                marker_status = os.stat(
                    path.name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
                if (marker_status.st_dev, marker_status.st_ino) == published_identity:
                    os.unlink(path.name, dir_fd=root_descriptor)
            except OSError:
                pass
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except OSError:
                pass


def _require_live_staging_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot inspect reserved materialization output {path}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(status.st_mode)
        or (status.st_dev, status.st_ino) != expected_identity
    ):
        raise TestProposalReviewError(
            "reserved materialization output changed before manifest commit"
        )


def _write_new_file(
    root_descriptor: int,
    relative: PurePosixPath,
    document: bytes,
    *,
    display_root: Path,
) -> None:
    parts = relative.parts
    if not parts:
        raise TestProposalReviewError("materialization path must not be empty")
    parent_descriptor = _ensure_materialization_parent(
        root_descriptor,
        parts[:-1],
        display_root=display_root,
    )
    path = display_root.joinpath(*parts)
    descriptor = -1
    try:
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise TestProposalReviewError(
            f"materialization path already exists: {path}"
        ) from exc
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot write materialization path {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _verify_staging_before_commit(
    root_descriptor: int,
    root: Path,
    manifest: Mapping[str, Any],
    *,
    expected_root_identity: tuple[int, int],
) -> None:
    """Rehash the complete inert tree immediately before publishing its marker."""

    expected: dict[str, tuple[str, int, int]] = {}
    records = manifest["files"]
    assert isinstance(records, list)
    for record in records:
        assert isinstance(record, Mapping)
        expected[record["path"]] = (
            record["sha256"],
            record["size"],
            _MAX_SOURCE_BYTES,
        )
    plan = manifest["evidence_plan"]
    issue = manifest["issue"]
    finalized = manifest["reviewed_test_proposals"]
    assert isinstance(plan, Mapping)
    assert isinstance(issue, Mapping)
    assert isinstance(finalized, Mapping)
    expected[plan["path"]] = (
        plan["sha256"],
        plan["size"],
        _MAX_PLAN_DOCUMENT_BYTES,
    )
    expected[issue["path"]] = (
        issue["sha256"],
        issue["size"],
        _MAX_ISSUE_BYTES,
    )
    expected[finalized["path"]] = (
        finalized["sha256"],
        finalized["size"],
        _MAX_FINAL_DOCUMENT_BYTES,
    )

    root_status = os.fstat(root_descriptor)
    if (root_status.st_dev, root_status.st_ino) != expected_root_identity:
        raise TestProposalReviewError(
            "reserved materialization output changed before manifest commit"
        )
    _require_live_staging_identity(root, expected_root_identity)
    actual_paths: set[str] = set()
    expected_directories: set[str] = set()
    for relative in expected:
        parts = PurePosixPath(relative).parts
        expected_directories.update(
            PurePosixPath(*parts[:length]).as_posix()
            for length in range(1, len(parts))
        )
    actual_directories: set[str] = set()
    _inventory_staging_directory(
        root_descriptor,
        prefix=(),
        actual_paths=actual_paths,
        actual_directories=actual_directories,
    )
    if actual_paths != set(expected):
        unexpected = sorted(actual_paths - set(expected))
        missing = sorted(set(expected) - actual_paths)
        detail = (
            f"unexpected path {unexpected[0]!r}"
            if unexpected
            else f"missing path {missing[0]!r}"
        )
        raise TestProposalReviewError(
            f"staging tree changed before manifest commit: {detail}"
        )
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        detail = (
            f"unexpected directory {unexpected[0]!r}"
            if unexpected
            else f"missing directory {missing[0]!r}"
        )
        raise TestProposalReviewError(
            f"staging tree changed before manifest commit: {detail}"
        )

    for relative, (expected_hash, expected_size, max_bytes) in expected.items():
        document = _read_staged_regular_file(
            root_descriptor,
            relative,
            max_bytes=max_bytes,
        )
        if len(document) != expected_size:
            raise TestProposalReviewError(
                f"staging path {relative!r} size changed before manifest commit"
            )
        if hashlib.sha256(document).hexdigest() != expected_hash:
            raise TestProposalReviewError(
                f"staging path {relative!r} hash changed before manifest commit"
            )
    _require_live_staging_identity(root, expected_root_identity)


def _inventory_staging_directory(
    directory_descriptor: int,
    *,
    prefix: tuple[str, ...],
    actual_paths: set[str],
    actual_directories: set[str],
) -> None:
    """Inventory a staging subtree without reopening its path from the root."""

    try:
        names = os.listdir(directory_descriptor)
    except OSError as exc:
        location = PurePosixPath(*prefix).as_posix() if prefix else "."
        raise TestProposalReviewError(
            f"cannot inventory staging directory {location!r}: {exc}"
        ) from exc
    for name in names:
        relative_parts = (*prefix, name)
        relative = PurePosixPath(*relative_parts).as_posix()
        try:
            entry_status = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise TestProposalReviewError(
                f"cannot inspect staged path {relative!r}: {exc}"
            ) from exc
        if stat.S_ISDIR(entry_status.st_mode):
            child_descriptor = -1
            try:
                child_descriptor = os.open(
                    name,
                    _directory_open_flags(),
                    dir_fd=directory_descriptor,
                )
                child_status = os.fstat(child_descriptor)
                if (
                    not stat.S_ISDIR(child_status.st_mode)
                    or (child_status.st_dev, child_status.st_ino)
                    != (entry_status.st_dev, entry_status.st_ino)
                ):
                    raise TestProposalReviewError(
                        f"staging directory {relative!r} changed during inventory"
                    )
                actual_directories.add(relative)
                _inventory_staging_directory(
                    child_descriptor,
                    prefix=relative_parts,
                    actual_paths=actual_paths,
                    actual_directories=actual_directories,
                )
            except TestProposalReviewError:
                raise
            except OSError as exc:
                raise TestProposalReviewError(
                    f"cannot open staging directory {relative!r} without "
                    f"following links: {exc}"
                ) from exc
            finally:
                if child_descriptor >= 0:
                    os.close(child_descriptor)
            continue
        if not stat.S_ISREG(entry_status.st_mode):
            raise TestProposalReviewError(
                f"staging path {relative!r} is not a regular file"
            )
        actual_paths.add(relative)


def _read_staged_regular_file(
    root_descriptor: int,
    relative: str,
    *,
    max_bytes: int,
) -> bytes:
    """Open one staged file beneath a held root without following any link."""

    if not _SECURE_DIR_FD_AVAILABLE:
        raise TestProposalReviewError(
            "secure staging revalidation is unavailable on this platform"
        )
    parts = PurePosixPath(relative).parts
    if not parts:
        raise TestProposalReviewError("staging path must not be empty")
    common_flags = getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | common_flags
    )
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | common_flags
    directory_descriptors: list[int] = []
    descriptor = -1
    try:
        current = os.dup(root_descriptor)
        directory_descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            directory_descriptors.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot open staged path {relative!r} without following links: {exc}"
        ) from exc
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise TestProposalReviewError(
                f"staging path {relative!r} is not a regular file"
            )
        if status.st_size > max_bytes:
            raise TestProposalReviewError(
                f"staging path {relative!r} exceeds the {max_bytes}-byte limit"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read(max_bytes + 1)
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot read staged path {relative!r}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_materialization_parent(
    root_descriptor: int,
    components: Sequence[str],
    *,
    display_root: Path,
) -> int:
    """Create and open a parent chain anchored to the held staging root."""

    try:
        current_descriptor = os.dup(root_descriptor)
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot duplicate the reserved staging descriptor: {exc}"
        ) from exc
    traversed: list[str] = []
    try:
        for component in components:
            traversed.append(component)
            display_path = display_root.joinpath(*traversed)
            try:
                os.mkdir(
                    component,
                    mode=0o700,
                    dir_fd=current_descriptor,
                )
            except FileExistsError:
                pass
            except OSError as exc:
                raise TestProposalReviewError(
                    f"cannot create materialization directory {display_path}: {exc}"
                ) from exc
            next_descriptor = -1
            try:
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=current_descriptor,
                )
                status = os.fstat(next_descriptor)
                if not stat.S_ISDIR(status.st_mode) or status.st_nlink < 1:
                    raise TestProposalReviewError(
                        "materialization parent is not a safe directory: "
                        f"{display_path}"
                    )
            except TestProposalReviewError:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
                raise
            except OSError as exc:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
                raise TestProposalReviewError(
                    f"cannot open materialization directory {display_path} "
                    f"without following links: {exc}"
                ) from exc
            os.close(current_descriptor)
            current_descriptor = next_descriptor
        result = current_descriptor
        current_descriptor = -1
        return result
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)


def _read_optional_response(path: Path | None) -> bytes | None:
    if path is None:
        return None
    return _read_bounded_file(
        path,
        label="generator response",
        max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
    )


def _require_bytes(document: object, *, label: str, max_bytes: int) -> bytes:
    if not isinstance(document, bytes):
        raise TestProposalReviewError(f"{label} must be supplied as exact bytes")
    if not document.strip():
        raise TestProposalReviewError(f"{label} is empty")
    if len(document) > max_bytes:
        raise TestProposalReviewError(f"{label} exceeds the {max_bytes}-byte limit")
    return document


def _read_bounded_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    resolved = Path(path).resolve()
    try:
        with resolved.open("rb") as stream:
            document = stream.read(max_bytes + 1)
    except OSError as exc:
        raise TestProposalReviewError(f"cannot read {label} {resolved}: {exc}") from exc
    return _require_bytes(document, label=label, max_bytes=max_bytes)


def _require_expected_digest(
    document: bytes,
    expected_sha256: str | None,
    *,
    label: str,
) -> None:
    if expected_sha256 is None:
        return
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise TestProposalReviewError(f"expected {label} SHA-256 is invalid")
    if hashlib.sha256(document).hexdigest() != expected_sha256:
        raise TestProposalReviewError(f"{label} changed after the earlier analysis pass")


def _require_document_limit(document: bytes, *, label: str, limit: int) -> None:
    if len(document) > limit:
        raise TestProposalReviewError(f"{label} exceeds the {limit}-byte limit")


def _write_exclusive(path: Path, document: bytes) -> None:
    if os.path.lexists(path):
        raise TestProposalReviewError(f"output file already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise TestProposalReviewError(
            f"cannot create temporary output beside {path}: {exc}"
        ) from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise TestProposalReviewError(f"output file already exists: {path}") from exc
    except OSError as exc:
        raise TestProposalReviewError(f"cannot write output file {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _decode_json(document: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TestProposalReviewError(f"{label} must be UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise TestProposalReviewError(f"{label} contains {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TestProposalReviewError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except (MemoryError, RecursionError, ValueError) as exc:
        raise TestProposalReviewError(f"{label} is not safely parseable JSON") from exc
    if not isinstance(payload, dict):
        raise TestProposalReviewError(f"{label} must contain a JSON object")
    return payload


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _encode_utf8(value: object, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise TestProposalReviewError(f"{label} must be a string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TestProposalReviewError(
            f"{label} must not contain unpaired Unicode surrogates"
        ) from exc


def _validate_text(value: object, *, label: str, max_bytes: int) -> None:
    if not isinstance(value, str):
        raise TestProposalReviewError(f"{label} must be a string")
    if not value or value != value.strip():
        raise TestProposalReviewError(
            f"{label} must be non-empty without surrounding whitespace"
        )
    if "\0" in value:
        raise TestProposalReviewError(f"{label} must not contain NUL characters")
    if len(_encode_utf8(value, label=label)) > max_bytes:
        raise TestProposalReviewError(f"{label} exceeds the {max_bytes}-byte limit")


def _validate_review_schema(payload: object) -> None:
    _validate_schema(payload, _review_validator(), root="test_proposal_review")
    assert isinstance(payload, Mapping)
    _require_integer_schema_versions(
        payload,
        root="test_proposal_review",
        paths=(
            ("schema_version",),
            ("proposal_artifact", "schema_version"),
            ("reviewed_requirements", "schema_version"),
        ),
    )


def _validate_final_schema(payload: object) -> None:
    _validate_schema(payload, _final_validator(), root="reviewed_test_proposals")
    assert isinstance(payload, Mapping)
    _require_integer_schema_versions(
        payload,
        root="reviewed_test_proposals",
        paths=(
            ("schema_version",),
            ("proposal_artifact", "schema_version"),
            ("reviewed_requirements", "schema_version"),
            ("review", "schema_version"),
        ),
    )


def _validate_materialization_schema(payload: object) -> None:
    _validate_schema(payload, _materialization_validator(), root="materialization")
    assert isinstance(payload, Mapping)
    _require_integer_schema_versions(
        payload,
        root="materialization",
        paths=(
            ("schema_version",),
            ("proposal_artifact", "schema_version"),
            ("review", "schema_version"),
            ("evidence_plan", "schema_version"),
        ),
    )


def _require_integer_schema_versions(
    payload: Mapping[str, Any],
    *,
    root: str,
    paths: Sequence[tuple[str, ...]],
) -> None:
    for path in paths:
        value: object = payload
        for component in path:
            assert isinstance(value, Mapping)
            value = value[component]
        if type(value) is not int:
            raise TestProposalReviewError(
                f"{root}.{'.'.join(path)}: expected integer"
            )


def _validate_schema(
    payload: object,
    validator: Draft202012Validator,
    *,
    root: str,
) -> None:
    try:
        errors = tuple(validator.iter_errors(payload))
    except (MemoryError, RecursionError) as exc:
        raise TestProposalReviewError(
            f"{root}: artifact is too deeply nested or resource intensive"
        ) from exc
    if not errors:
        return
    try:
        leaves = tuple(leaf for error in errors for leaf in _leaf_errors(error))
    except (MemoryError, RecursionError) as exc:
        raise TestProposalReviewError(
            f"{root}: schema errors are too deeply nested or resource intensive"
        ) from exc
    error = min(
        leaves,
        key=lambda item: (
            _path_sort_key(item.absolute_path),
            tuple(str(part) for part in item.absolute_schema_path),
            str(item.validator),
            item.message,
        ),
    )
    path, message = _describe_error(error)
    raise TestProposalReviewError(f"{_format_path(root, path)}: {message}")


@lru_cache(maxsize=1)
def _review_validator() -> Draft202012Validator:
    return _load_validator("test-proposal-review.schema.json")


@lru_cache(maxsize=1)
def _final_validator() -> Draft202012Validator:
    return _load_validator("reviewed-test-proposals.schema.json")


@lru_cache(maxsize=1)
def _materialization_validator() -> Draft202012Validator:
    return _load_validator("test-materialization.schema.json")


def _load_validator(name: str) -> Draft202012Validator:
    resource = files("patch_verifier").joinpath("schemas").joinpath(name)
    try:
        schema = json.loads(resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise RuntimeError(f"packaged {name} schema is invalid") from exc
    return Draft202012Validator(schema)


def _leaf_errors(error: ValidationError) -> Iterator[ValidationError]:
    if not error.context:
        yield error
        return
    for child in error.context:
        yield from _leaf_errors(child)


def _path_sort_key(path: Sequence[object]) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, item) if isinstance(item, int) else (1, str(item)) for item in path
    )


def _describe_error(error: ValidationError) -> tuple[tuple[object, ...], str]:
    path = tuple(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, Mapping):
        missing = sorted(set(error.validator_value) - set(error.instance))
        if missing:
            return (*path, missing[0]), "required property is missing"
    if error.validator in {"additionalProperties", "unevaluatedProperties"}:
        return path, "property is not allowed"
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, str):
            return path, f"expected {expected}"
        return path, "expected " + " or ".join(str(item) for item in expected)
    if error.validator == "const":
        return path, f"expected constant {error.validator_value!r}"
    if error.validator == "enum":
        return path, "expected one of " + ", ".join(
            repr(item) for item in error.validator_value
        )
    if error.validator == "pattern":
        return path, "value does not match the required pattern"
    if error.validator == "maxLength":
        return path, f"must contain at most {error.validator_value} characters"
    if error.validator == "minItems":
        return path, f"must contain at least {error.validator_value} items"
    if error.validator == "maxItems":
        return path, f"must contain at most {error.validator_value} items"
    return path, " ".join(error.message.split())[:240]


def _format_path(root: str, path: Sequence[object]) -> str:
    result = root
    for item in path:
        result += f"[{item}]" if isinstance(item, int) else f".{item}"
    return result


__all__ = [
    "TestMaterializationResult",
    "TestProposalReviewError",
    "TestReviewFinalizationResult",
    "TestReviewPreparationResult",
    "encode_test_review_artifact",
    "finalize_test_proposal_review",
    "materialize_reviewed_test_proposal_files",
    "materialize_reviewed_test_proposals",
    "prepare_test_proposal_review",
    "validate_reviewed_test_proposals_artifact",
    "validate_reviewed_test_proposals_structure",
    "validate_test_materialization_artifact",
    "validate_test_proposal_review_artifact",
    "write_reviewed_test_proposals",
    "write_test_proposal_review",
]
