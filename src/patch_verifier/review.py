"""Explicit human review and finalization of requirement drafts.

The workflow is deliberately two-phase. ``prepare_requirement_review`` creates
an inert ledger in which every draft candidate is pending. A human edits that
ledger to accept, edit, or reject each candidate with a rationale. Only
``finalize_requirement_review`` can produce ``Requirement``-shaped records, and
it does so only after binding the review to the exact bytes of its source draft.

Neither phase generates tests, imports target code, or executes repository
code. Conflict hints are advisory metadata and never alter decision semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .draft_validation import validate_requirement_draft_artifact
from .models import ProvenanceTier, Requirement, RequirementKind
from .review_validation import (
    validate_requirement_review_artifact,
    validate_reviewed_requirements_artifact,
)


_MAX_DRAFT_BYTES = 10 * 1024 * 1024
_MAX_REVIEW_BYTES = 16 * 1024 * 1024
_MAX_FINAL_BYTES = 24 * 1024 * 1024
_MAX_REQUIREMENTS = 200
_MAX_CONFLICT_HINTS = 200
_MAX_HINT_CANDIDATES = 20


class RequirementReviewError(RuntimeError):
    """A requirement review artifact is invalid or cannot be finalized."""


@dataclass(frozen=True)
class ReviewPreparationResult:
    """Summary of an exclusively written review ledger."""

    output_file: Path
    draft_sha256: str
    candidate_count: int
    conflict_hint_count: int


@dataclass(frozen=True)
class ReviewFinalizationResult:
    """Summary of an exclusively written finalized artifact."""

    output_file: Path
    draft_sha256: str
    review_sha256: str
    requirement_count: int
    rejected_count: int


class _DuplicateKeyError(ValueError):
    pass


def prepare_requirement_review(
    draft_document: bytes,
    *,
    conflict_hints: Iterable[Mapping[str, object]] = (),
    conflict_analysis: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Create a deterministic pending ledger for exact draft bytes.

    ``conflict_hints`` are bounded, source-linked notices supplied by a caller.
    They are sorted and validated, but remain advisory: finalization requires a
    human decision for every candidate regardless of whether hints are present.
    """

    draft_bytes = _require_bytes(
        draft_document, label="requirement draft", max_bytes=_MAX_DRAFT_BYTES
    )
    draft = _decode_json(draft_bytes, label="requirement draft")
    try:
        validate_requirement_draft_artifact(draft)
    except ValueError as exc:
        raise RequirementReviewError(f"requirement draft is invalid: {exc}") from exc

    candidates = draft["draft"]["candidates"]
    assert isinstance(candidates, list)
    decisions = [
        {
            "candidate_id": candidate["id"],
            "candidate": candidate,
            "action": "pending",
            "requirement_id": stable_requirement_id(candidate["id"]),
            "rationale": None,
            "replacement": None,
        }
        for candidate in candidates
    ]
    if len({decision["requirement_id"] for decision in decisions}) != len(decisions):
        raise RequirementReviewError(
            "stable requirement id collision; the draft cannot be reviewed"
        )
    normalized_hints = _normalize_conflict_hints(conflict_hints)
    normalized_analysis, embedded_hints, hint_count_supplied = _normalize_conflict_analysis(
        conflict_analysis
    )
    if embedded_hints is not None:
        if normalized_hints and normalized_hints != embedded_hints:
            raise RequirementReviewError(
                "conflict_hints do not match conflict_analysis.hints"
            )
        normalized_hints = embedded_hints
    if normalized_analysis is not None:
        if embedded_hints is None and not hint_count_supplied:
            normalized_analysis["hint_count"] = len(normalized_hints)
        _validate_conflict_analysis_counts(
            normalized_analysis,
            hint_count=len(normalized_hints),
            candidate_count=len(candidates),
        )
    ledger: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "requirement-review",
        "draft": {
            "sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "schema_version": draft["schema_version"],
            "candidate_count": len(candidates),
        },
        "conflict_analysis": normalized_analysis,
        "conflict_hints": normalized_hints,
        "decisions": decisions,
    }
    try:
        validate_requirement_review_artifact(ledger)
    except ValueError as exc:
        raise RequirementReviewError(f"cannot prepare review ledger: {exc}") from exc
    return ledger


def finalize_requirement_review(
    draft_document: bytes,
    review_document: bytes,
    *,
    expected_conflict_hints: Iterable[Mapping[str, object]] | None = None,
    expected_conflict_analysis: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Finalize a complete ledger into requirements and an audit trail.

    The SHA-256 values bind exact input bytes, not a canonicalized JSON value.
    A semantically identical reserialization of the draft is therefore a
    different source artifact and is rejected by an existing ledger.
    """

    draft_bytes = _require_bytes(
        draft_document, label="requirement draft", max_bytes=_MAX_DRAFT_BYTES
    )
    review_bytes = _require_bytes(
        review_document, label="requirement review", max_bytes=_MAX_REVIEW_BYTES
    )
    draft = _decode_json(draft_bytes, label="requirement draft")
    review = _decode_json(review_bytes, label="requirement review")
    try:
        validate_requirement_draft_artifact(draft)
    except ValueError as exc:
        raise RequirementReviewError(f"requirement draft is invalid: {exc}") from exc
    try:
        validate_requirement_review_artifact(review)
    except ValueError as exc:
        raise RequirementReviewError(f"requirement review is invalid: {exc}") from exc
    if expected_conflict_hints is not None:
        expected_hints = _normalize_conflict_hints(expected_conflict_hints)
        if review["conflict_hints"] != expected_hints:
            raise RequirementReviewError(
                "requirement review conflict hints do not match the expected "
                "detector output"
            )
    if expected_conflict_analysis is not None:
        expected_analysis, embedded_hints, hint_count_supplied = _normalize_conflict_analysis(
            expected_conflict_analysis
        )
        assert expected_analysis is not None
        if embedded_hints is None and not hint_count_supplied:
            expected_analysis["hint_count"] = len(review["conflict_hints"])
        if review["conflict_analysis"] != expected_analysis:
            raise RequirementReviewError(
                "requirement review conflict analysis does not match the expected "
                "detector summary"
            )
        if embedded_hints is not None and review["conflict_hints"] != embedded_hints:
            raise RequirementReviewError(
                "requirement review conflict hints do not match the expected "
                "detector output"
            )

    observed_draft_sha256 = hashlib.sha256(draft_bytes).hexdigest()
    binding = review["draft"]
    assert isinstance(binding, Mapping)
    if binding["sha256"] != observed_draft_sha256:
        raise RequirementReviewError(
            "requirement review draft hash does not match the exact draft bytes"
        )
    if binding["schema_version"] != draft["schema_version"]:
        raise RequirementReviewError(
            "requirement review draft schema version does not match the draft"
        )

    candidates = draft["draft"]["candidates"]
    decisions = review["decisions"]
    assert isinstance(candidates, list)
    assert isinstance(decisions, list)
    if binding["candidate_count"] != len(candidates):
        raise RequirementReviewError(
            "requirement review candidate count does not match the draft"
        )

    candidate_ids = [candidate["id"] for candidate in candidates]
    decision_ids = [decision["candidate_id"] for decision in decisions]
    unknown = sorted(set(decision_ids) - set(candidate_ids))
    if unknown:
        raise RequirementReviewError(
            f"requirement review contains unknown candidate {unknown[0]}"
        )
    missing = sorted(set(candidate_ids) - set(decision_ids))
    if missing:
        raise RequirementReviewError(
            f"requirement review is missing a decision for candidate {missing[0]}"
        )
    if decision_ids != candidate_ids:
        raise RequirementReviewError(
            "requirement review decisions must remain in deterministic draft order"
        )
    stable_ids = [stable_requirement_id(candidate_id) for candidate_id in candidate_ids]
    if len(set(stable_ids)) != len(stable_ids):
        raise RequirementReviewError(
            "stable requirement id collision; the draft cannot be finalized"
        )
    for index, (candidate, decision) in enumerate(zip(candidates, decisions)):
        if decision["candidate"] != candidate:
            raise RequirementReviewError(
                f"requirement review decision {index} candidate preview does not "
                "exactly match the bound draft"
            )

    pending = [
        decision["candidate_id"]
        for decision in decisions
        if decision["action"] == "pending"
    ]
    if pending:
        raise RequirementReviewError(
            f"requirement review has pending decision for candidate {pending[0]}"
        )
    accepted_count = sum(
        decision["action"] in {"accept", "edit"} for decision in decisions
    )
    if accepted_count > _MAX_REQUIREMENTS:
        raise RequirementReviewError(
            f"requirement review accepts {accepted_count} requirements; "
            f"the EvidencePlan-compatible limit is {_MAX_REQUIREMENTS}"
        )

    requirements: list[dict[str, Any]] = []
    action_counts = {"accept": 0, "edit": 0, "reject": 0}
    for index, (candidate, decision) in enumerate(zip(candidates, decisions)):
        assert isinstance(candidate, Mapping)
        assert isinstance(decision, Mapping)
        action = decision["action"]
        assert isinstance(action, str)
        action_counts[action] += 1
        if action == "reject":
            continue

        expected_kind, expected_provenance = _source_semantics(candidate)
        statement = candidate["statement"]
        if action == "edit":
            replacement = decision["replacement"]
            assert isinstance(replacement, Mapping)
            if replacement["kind"] != expected_kind.value:
                raise RequirementReviewError(
                    f"requirement review decision {index} cannot change the "
                    f"source-backed kind from {expected_kind.value}"
                )
            if replacement["provenance"] != expected_provenance.value:
                raise RequirementReviewError(
                    f"requirement review decision {index} cannot change the "
                    f"source-backed provenance from {expected_provenance.value}"
                )
            statement = replacement["statement"]
            if statement == candidate["statement"]:
                raise RequirementReviewError(
                    f"requirement review decision {index} is an edit but does "
                    "not change the candidate statement"
                )

        source = candidate["source"]
        assert isinstance(source, Mapping)
        source_reference = (
            f"issue:{source['start_line']}"
            if candidate["source_kind"] == "issue"
            else f"{source['path']}:{source['start_line']}"
        )
        try:
            requirement = Requirement(
                id=decision["requirement_id"],
                statement=statement,
                kind=expected_kind,
                provenance=expected_provenance,
                source=source_reference,
                excerpt=source["excerpt"],
            )
        except (TypeError, ValueError) as exc:  # defensive domain-model check
            raise RequirementReviewError(
                f"requirement review decision {index} is invalid: {exc}"
            ) from exc
        requirements.append(requirement.to_dict())

    result: dict[str, Any] = {
        # v2 adds an exact issue binding.  v1 remains readable, but emitting
        # the new field under the closed v1 contract would make otherwise
        # valid artifacts unreadable by existing consumers.
        "schema_version": 2,
        "artifact_type": "reviewed-requirements",
        "draft": dict(binding),
        "issue": {
            "sha256": draft["inputs"]["issue_sha256"],
            "source": draft["inputs"]["issue_source"],
        },
        "review": {
            "sha256": hashlib.sha256(review_bytes).hexdigest(),
            "schema_version": review["schema_version"],
        },
        "requirements": requirements,
        "audit": {
            "candidate_count": len(candidates),
            "accepted_count": action_counts["accept"],
            "edited_count": action_counts["edit"],
            "rejected_count": action_counts["reject"],
            "conflict_analysis": review["conflict_analysis"],
            "conflict_hints": review["conflict_hints"],
            "decisions": decisions,
        },
    }
    try:
        validate_reviewed_requirements_artifact(result)
    except ValueError as exc:  # generated artifacts must pass their public validator
        raise RequirementReviewError(
            f"generated reviewed requirements are invalid: {exc}"
        ) from exc
    return result


def encode_review_artifact(payload: Mapping[str, object]) -> bytes:
    """Encode a review workflow artifact in the canonical on-disk form."""

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
        raise RequirementReviewError(f"cannot encode review artifact: {exc}") from exc


def stable_requirement_id(candidate_id: str) -> str:
    """Derive a stable EvidencePlan-compatible id from a candidate id.

    EvidencePlan ids are limited to 64 characters. ``req-`` plus the first 60
    hexadecimal characters retains 240 bits of the candidate's SHA-256 digest;
    preparation and finalization both reject the theoretical collision case.
    """

    if not isinstance(candidate_id, str) or not candidate_id.startswith("cand-"):
        raise RequirementReviewError("candidate id is not a canonical candidate hash")
    digest = candidate_id[5:]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RequirementReviewError("candidate id is not a canonical candidate hash")
    return "req-" + digest[:60]


def write_requirement_review(
    draft_file: Path,
    output_file: Path,
    *,
    conflict_hints: Iterable[Mapping[str, object]] = (),
    conflict_analysis: Mapping[str, object] | None = None,
    expected_draft_sha256: str | None = None,
) -> ReviewPreparationResult:
    """Prepare and exclusively write a ledger without overwriting a file."""

    draft_path = Path(draft_file).resolve()
    output_path = Path(output_file).absolute()
    draft_bytes = _read_bounded_file(
        draft_path, label="requirement draft", max_bytes=_MAX_DRAFT_BYTES
    )
    _require_expected_digest(
        draft_bytes,
        expected_draft_sha256,
        label="requirement draft",
    )
    ledger = prepare_requirement_review(
        draft_bytes,
        conflict_hints=conflict_hints,
        conflict_analysis=conflict_analysis,
    )
    document = encode_review_artifact(ledger)
    if len(document) > _MAX_REVIEW_BYTES:
        raise RequirementReviewError(
            f"requirement review exceeds the {_MAX_REVIEW_BYTES}-byte limit"
        )
    _write_exclusive(output_path, document)
    return ReviewPreparationResult(
        output_file=output_path,
        draft_sha256=ledger["draft"]["sha256"],
        candidate_count=ledger["draft"]["candidate_count"],
        conflict_hint_count=len(ledger["conflict_hints"]),
    )


def write_reviewed_requirements(
    draft_file: Path,
    review_file: Path,
    output_file: Path,
    *,
    expected_conflict_hints: Iterable[Mapping[str, object]] | None = None,
    expected_conflict_analysis: Mapping[str, object] | None = None,
    expected_draft_sha256: str | None = None,
) -> ReviewFinalizationResult:
    """Finalize exact input files and exclusively write the reviewed result."""

    draft_path = Path(draft_file).resolve()
    review_path = Path(review_file).resolve()
    output_path = Path(output_file).absolute()
    draft_bytes = _read_bounded_file(
        draft_path, label="requirement draft", max_bytes=_MAX_DRAFT_BYTES
    )
    _require_expected_digest(
        draft_bytes,
        expected_draft_sha256,
        label="requirement draft",
    )
    review_bytes = _read_bounded_file(
        review_path, label="requirement review", max_bytes=_MAX_REVIEW_BYTES
    )
    finalized = finalize_requirement_review(
        draft_bytes,
        review_bytes,
        expected_conflict_hints=expected_conflict_hints,
        expected_conflict_analysis=expected_conflict_analysis,
    )
    document = encode_review_artifact(finalized)
    if len(document) > _MAX_FINAL_BYTES:
        raise RequirementReviewError(
            f"reviewed requirements exceed the {_MAX_FINAL_BYTES}-byte limit"
        )
    _write_exclusive(output_path, document)
    audit = finalized["audit"]
    return ReviewFinalizationResult(
        output_file=output_path,
        draft_sha256=finalized["draft"]["sha256"],
        review_sha256=finalized["review"]["sha256"],
        requirement_count=len(finalized["requirements"]),
        rejected_count=audit["rejected_count"],
    )


def _source_semantics(
    candidate: Mapping[str, object],
) -> tuple[RequirementKind, ProvenanceTier]:
    source_kind = candidate["source_kind"]
    if source_kind == "issue":
        return RequirementKind.REQUESTED, ProvenanceTier.EXPLICIT
    if source_kind in {"pytest_assertion", "docstring"}:
        return RequirementKind.PRESERVATION, ProvenanceTier.PREEXISTING
    raise RequirementReviewError(f"unknown candidate source kind: {source_kind!r}")


def _normalize_conflict_hints(
    conflict_hints: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    try:
        raw_hints = list(
            itertools.islice(iter(conflict_hints), _MAX_CONFLICT_HINTS + 1)
        )
    except TypeError as exc:
        raise RequirementReviewError("conflict_hints must be iterable") from exc
    if len(raw_hints) > _MAX_CONFLICT_HINTS:
        raise RequirementReviewError(
            f"conflict_hints exceeds the {_MAX_CONFLICT_HINTS}-hint limit"
        )
    normalized: list[dict[str, object]] = []
    for index, hint in enumerate(raw_hints):
        if not isinstance(hint, Mapping):
            raise RequirementReviewError(
                f"conflict_hints[{index}] must be an object"
            )
        unexpected = set(hint) - {"id", "candidate_ids", "message"}
        missing = {"id", "candidate_ids", "message"} - set(hint)
        if missing:
            raise RequirementReviewError(
                f"conflict_hints[{index}] is missing {sorted(missing)[0]}"
            )
        if unexpected:
            raise RequirementReviewError(
                f"conflict_hints[{index}] contains unsupported field "
                f"{sorted(unexpected)[0]}"
            )
        candidate_ids = hint["candidate_ids"]
        if not isinstance(candidate_ids, (list, tuple)):
            raise RequirementReviewError(
                f"conflict_hints[{index}].candidate_ids must be an array"
            )
        if not 2 <= len(candidate_ids) <= _MAX_HINT_CANDIDATES:
            raise RequirementReviewError(
                f"conflict_hints[{index}].candidate_ids must contain between "
                f"2 and {_MAX_HINT_CANDIDATES} items"
            )
        if any(not isinstance(candidate_id, str) for candidate_id in candidate_ids):
            raise RequirementReviewError(
                f"conflict_hints[{index}].candidate_ids must contain strings"
            )
        if not isinstance(hint["id"], str):
            raise RequirementReviewError(
                f"conflict_hints[{index}].id must be a string"
            )
        if not isinstance(hint["message"], str):
            raise RequirementReviewError(
                f"conflict_hints[{index}].message must be a string"
            )
        normalized.append(
            {
                "id": hint["id"],
                "candidate_ids": sorted(candidate_ids),
                "message": hint["message"],
            }
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["id"],
            tuple(item["candidate_ids"]),
            item["message"],
        ),
    )


def _normalize_conflict_analysis(
    conflict_analysis: Mapping[str, object] | None,
) -> tuple[
    dict[str, object] | None,
    list[dict[str, object]] | None,
    bool,
]:
    """Normalize a ConflictAnalysis mapping without importing its producer."""

    if conflict_analysis is None:
        return None, None, False
    if not isinstance(conflict_analysis, Mapping):
        raise RequirementReviewError("conflict_analysis must be an object or null")
    raw = dict(conflict_analysis)
    embedded = raw.pop("hints", None)
    supplied_hint_count = "hint_count" in raw
    raw_hint_count = raw.pop("hint_count", None)
    expected = {
        "requested_candidate_count",
        "preservation_candidate_count",
        "compared_pair_count",
        "truncated",
    }
    missing = expected - set(raw)
    unexpected = set(raw) - expected
    if missing:
        raise RequirementReviewError(
            f"conflict_analysis is missing {sorted(missing)[0]}"
        )
    if unexpected:
        raise RequirementReviewError(
            f"conflict_analysis contains unsupported field {sorted(unexpected)[0]}"
        )
    for field in (
        "requested_candidate_count",
        "preservation_candidate_count",
        "compared_pair_count",
    ):
        if type(raw[field]) is not int or raw[field] < 0:
            raise RequirementReviewError(
                f"conflict_analysis.{field} must be a non-negative integer"
            )
    if type(raw["truncated"]) is not bool:
        raise RequirementReviewError(
            "conflict_analysis.truncated must be a boolean"
        )
    if supplied_hint_count and (
        type(raw_hint_count) is not int or raw_hint_count < 0
    ):
        raise RequirementReviewError(
            "conflict_analysis.hint_count must be a non-negative integer"
        )
    embedded_hints = (
        None if embedded is None else _normalize_conflict_hints(embedded)
    )
    hint_count = (
        raw_hint_count
        if supplied_hint_count
        else (len(embedded_hints) if embedded_hints is not None else 0)
    )
    if embedded_hints is not None and hint_count != len(embedded_hints):
        raise RequirementReviewError(
            "conflict_analysis.hint_count does not match conflict_analysis.hints"
        )
    summary = {
        "requested_candidate_count": raw["requested_candidate_count"],
        "preservation_candidate_count": raw["preservation_candidate_count"],
        "compared_pair_count": raw["compared_pair_count"],
        "hint_count": hint_count,
        "truncated": raw["truncated"],
    }
    return summary, embedded_hints, supplied_hint_count


def _validate_conflict_analysis_counts(
    summary: Mapping[str, object],
    *,
    hint_count: int,
    candidate_count: int,
) -> None:
    requested = summary["requested_candidate_count"]
    preservation = summary["preservation_candidate_count"]
    compared = summary["compared_pair_count"]
    assert isinstance(requested, int)
    assert isinstance(preservation, int)
    assert isinstance(compared, int)
    if summary["hint_count"] != hint_count:
        raise RequirementReviewError(
            "conflict_analysis.hint_count does not match conflict_hints"
        )
    if requested + preservation > candidate_count:
        raise RequirementReviewError(
            "conflict analysis candidate counts exceed the draft candidate count"
        )
    if compared > requested * preservation:
        raise RequirementReviewError(
            "conflict analysis pair count exceeds the possible candidate pairs"
        )


def _require_bytes(document: bytes, *, label: str, max_bytes: int) -> bytes:
    if not isinstance(document, bytes):
        raise RequirementReviewError(f"{label} must be supplied as exact bytes")
    if len(document) > max_bytes:
        raise RequirementReviewError(f"{label} exceeds the {max_bytes}-byte limit")
    if not document.strip():
        raise RequirementReviewError(f"{label} is empty")
    return document


def _decode_json(document: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RequirementReviewError(f"{label} must be UTF-8") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except _DuplicateKeyError as exc:
        raise RequirementReviewError(f"{label} contains {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RequirementReviewError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except (RecursionError, ValueError) as exc:
        raise RequirementReviewError(f"{label} is not safely parseable JSON") from exc
    if not isinstance(payload, dict):
        raise RequirementReviewError(f"{label} must contain a JSON object")
    return payload


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _read_bounded_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            document = stream.read(max_bytes + 1)
    except OSError as exc:
        raise RequirementReviewError(f"cannot read {label} {path}: {exc}") from exc
    return _require_bytes(document, label=label, max_bytes=max_bytes)


def _require_expected_digest(
    document: bytes,
    expected_sha256: str | None,
    *,
    label: str,
) -> None:
    """Fail if a file changed after an earlier exact-byte analysis pass."""

    if expected_sha256 is None:
        return
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise RequirementReviewError(f"expected {label} SHA-256 is invalid")
    if hashlib.sha256(document).hexdigest() != expected_sha256:
        raise RequirementReviewError(
            f"{label} changed after conflict analysis; rerun the review command"
        )


def _write_exclusive(path: Path, document: bytes) -> None:
    if path.exists():
        raise RequirementReviewError(f"output file already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise RequirementReviewError(
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
        raise RequirementReviewError(f"output file already exists: {path}") from exc
    except OSError as exc:
        raise RequirementReviewError(f"cannot write output file {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "RequirementReviewError",
    "ReviewFinalizationResult",
    "ReviewPreparationResult",
    "encode_review_artifact",
    "finalize_requirement_review",
    "prepare_requirement_review",
    "stable_requirement_id",
    "write_requirement_review",
    "write_reviewed_requirements",
]
