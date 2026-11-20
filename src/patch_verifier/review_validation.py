"""Strict validation for requirement review and finalization artifacts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
from pathlib import PurePosixPath
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .draft_validation import canonical_candidate_statement
from .requirements import looks_behavioral_docstring


_TERMINAL_ACTIONS = frozenset({"accept", "edit", "reject"})
_SOURCE_KIND_ORDER = {"issue": 0, "pytest_assertion": 1, "docstring": 2}
_MAX_CONFLICT_PAIRS = 20_000
_MAX_CONFLICT_HINTS = 200


def validate_requirement_review_artifact(payload: object) -> None:
    """Validate the standalone shape and internal consistency of a ledger."""

    _validate_schema(payload, _review_validator(), root="requirement_review")
    assert isinstance(payload, Mapping)
    decisions = payload["decisions"]
    hints = payload["conflict_hints"]
    conflict_analysis = payload["conflict_analysis"]
    assert isinstance(decisions, list)
    assert isinstance(hints, list)
    assert conflict_analysis is None or isinstance(conflict_analysis, Mapping)
    draft_binding = payload["draft"]
    assert isinstance(draft_binding, Mapping)
    if draft_binding["candidate_count"] != len(decisions):
        raise ValueError(
            "requirement_review.draft.candidate_count: does not match the "
            "number of candidate decisions"
        )

    decision_ids: list[str] = []
    requirement_ids: set[str] = set()
    candidate_order_keys: list[tuple[object, ...]] = []
    observed_requested = 0
    observed_preservation = 0
    for index, decision in enumerate(decisions):
        assert isinstance(decision, Mapping)
        candidate_id = decision["candidate_id"]
        candidate = decision["candidate"]
        action = decision["action"]
        requirement_id = decision["requirement_id"]
        rationale = decision["rationale"]
        replacement = decision["replacement"]
        assert isinstance(candidate_id, str)
        assert isinstance(candidate, Mapping)
        assert isinstance(action, str)
        assert isinstance(requirement_id, str)
        assert rationale is None or isinstance(rationale, str)
        assert replacement is None or isinstance(replacement, Mapping)
        decision_ids.append(candidate_id)

        if candidate["id"] != candidate_id:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.id: "
                "does not match candidate_id"
            )
        source = candidate["source"]
        assert isinstance(source, Mapping)
        source_kind = candidate["source_kind"]
        intent = candidate["intent"]
        assert isinstance(source_kind, str)
        assert isinstance(intent, str)
        expected_intent = "requested" if source_kind == "issue" else "preservation"
        if intent != expected_intent:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.intent: "
                f"{source_kind} candidates must be {expected_intent}"
            )
        if intent == "requested":
            observed_requested += 1
        else:
            observed_preservation += 1
        path = source["path"]
        statement = candidate["statement"]
        excerpt = source["excerpt"]
        digest = source["sha256"]
        start_line = source["start_line"]
        end_line = source["end_line"]
        start_column = source["start_column"]
        end_column = source["end_column"]
        assert isinstance(path, str)
        assert isinstance(statement, str)
        assert isinstance(excerpt, str)
        assert isinstance(digest, str)
        assert isinstance(start_line, int)
        assert isinstance(end_line, int)
        assert isinstance(start_column, int)
        assert isinstance(end_column, int)
        if any(character in path for character in "\r\n\0"):
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source.path: "
                "must be a single line without NUL characters"
            )
        if source_kind != "issue":
            pure_path = PurePosixPath(path)
            if pure_path.is_absolute() or any(
                part in {"", ".", ".."} for part in pure_path.parts
            ):
                raise ValueError(
                    f"requirement_review.decisions[{index}].candidate.source.path: "
                    "base source must be a safe repository-relative path"
                )
        if end_line < start_line:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source: "
                "end_line precedes start_line"
            )
        line_breaks = tuple(re.finditer(r"\r\n|\r|\n", excerpt))
        if len(line_breaks) != end_line - start_line:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source.excerpt: "
                "line count does not match the recorded span"
            )
        excerpt_bytes = excerpt.encode("utf-8")
        if len(excerpt_bytes) > 16_000:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source.excerpt: "
                "UTF-8 form exceeds the 16000-byte limit"
            )
        if start_line == end_line and end_column - start_column != len(excerpt_bytes):
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source: "
                "single-line columns do not delimit the excerpt UTF-8 bytes"
            )
        if line_breaks:
            final_line = excerpt[line_breaks[-1].end() :]
            if len(final_line.encode("utf-8")) != end_column:
                raise ValueError(
                    f"requirement_review.decisions[{index}].candidate.source: "
                    "end_column does not delimit the final excerpt line"
                )
        if hashlib.sha256(excerpt_bytes).hexdigest() != digest:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.source.sha256: "
                "digest does not match the exact excerpt"
            )
        expected_statement = canonical_candidate_statement(
            source_kind,
            excerpt,
            error_path=(
                f"requirement_review.decisions[{index}].candidate.source.excerpt"
            ),
        )
        if statement != expected_statement:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.statement: "
                "statement is not the deterministic transform of its exact "
                "source excerpt"
            )
        if source_kind == "docstring" and not looks_behavioral_docstring(statement):
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate.statement: "
                "statement does not satisfy behavioral docstring selection"
            )
        identifier_payload = {
            "intent": candidate["intent"],
            "source": dict(source),
            "source_kind": candidate["source_kind"],
            "statement": candidate["statement"],
        }
        canonical = json.dumps(
            identifier_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_candidate_id = "cand-" + hashlib.sha256(canonical).hexdigest()
        if candidate_id != expected_candidate_id:
            raise ValueError(
                f"requirement_review.decisions[{index}].candidate_id: "
                "does not match canonical candidate provenance"
            )
        candidate_order_keys.append(
            (
                _SOURCE_KIND_ORDER[source_kind],
                path,
                start_line,
                start_column,
                end_line,
                end_column,
                statement,
                candidate_id,
            )
        )

        if rationale is not None and "\0" in rationale:
            raise ValueError(
                f"requirement_review.decisions[{index}].rationale: "
                "NUL characters are not allowed"
            )
        expected_requirement_id = "req-" + candidate_id.removeprefix("cand-")[:60]
        if requirement_id != expected_requirement_id:
            raise ValueError(
                f"requirement_review.decisions[{index}].requirement_id: "
                "does not match the stable id derived from candidate_id"
            )
        if action == "pending":
            if any(item is not None for item in (rationale, replacement)):
                raise ValueError(
                    f"requirement_review.decisions[{index}]: pending decisions "
                    "cannot contain review data"
                )
        elif action == "reject":
            if replacement is not None:
                raise ValueError(
                    f"requirement_review.decisions[{index}]: rejected candidates "
                    "cannot produce a requirement"
                )
        elif action == "accept":
            if replacement is not None:
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement: accepted "
                    "candidates cannot contain edits"
                )
        elif action == "edit":
            assert replacement is not None
            statement = replacement["statement"]
            assert isinstance(statement, str)
            expected_kind = "requested" if source_kind == "issue" else "preservation"
            expected_provenance = (
                "explicit" if source_kind == "issue" else "preexisting"
            )
            if replacement["kind"] != expected_kind:
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement.kind: "
                    "cannot change source-backed kind"
                )
            if replacement["provenance"] != expected_provenance:
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement.provenance: "
                    "cannot change source-backed provenance"
                )
            if statement == candidate["statement"]:
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement.statement: "
                    "edit does not change the candidate statement"
                )
            if "\0" in statement:
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement.statement: "
                    "must not contain NUL characters"
                )
            if statement != statement.strip():
                raise ValueError(
                    f"requirement_review.decisions[{index}].replacement.statement: "
                    "must not have leading or trailing whitespace"
                )

        if requirement_id in requirement_ids:
            raise ValueError(
                f"requirement_review.decisions[{index}].requirement_id: "
                "requirement id is duplicated"
            )
        requirement_ids.add(requirement_id)

    if len(set(decision_ids)) != len(decision_ids):
        duplicate = next(
            candidate_id
            for offset, candidate_id in enumerate(decision_ids)
            if candidate_id in decision_ids[:offset]
        )
        raise ValueError(
            "requirement_review.decisions: candidate decision is duplicated: "
            f"{duplicate}"
        )
    if candidate_order_keys != sorted(candidate_order_keys):
        raise ValueError(
            "requirement_review.decisions: candidates are not in deterministic "
            "draft order"
        )

    known_candidates = set(decision_ids)
    seen_hint_ids: set[str] = set()
    hint_order: list[tuple[str, tuple[str, ...], str]] = []
    for index, hint in enumerate(hints):
        assert isinstance(hint, Mapping)
        hint_id = hint["id"]
        candidate_ids = hint["candidate_ids"]
        message = hint["message"]
        assert isinstance(hint_id, str)
        assert isinstance(candidate_ids, list)
        assert isinstance(message, str)
        if hint_id in seen_hint_ids:
            raise ValueError(
                f"requirement_review.conflict_hints[{index}].id: "
                "conflict hint id is duplicated"
            )
        seen_hint_ids.add(hint_id)
        if candidate_ids != sorted(candidate_ids):
            raise ValueError(
                f"requirement_review.conflict_hints[{index}].candidate_ids: "
                "candidate ids must be in deterministic order"
            )
        unknown = sorted(set(candidate_ids) - known_candidates)
        if unknown:
            raise ValueError(
                f"requirement_review.conflict_hints[{index}].candidate_ids: "
                f"unknown candidate id {unknown[0]}"
            )
        if "\0" in message:
            raise ValueError(
                f"requirement_review.conflict_hints[{index}].message: "
                "NUL characters are not allowed"
            )
        hint_order.append((hint_id, tuple(candidate_ids), message))
    if hint_order != sorted(hint_order):
        raise ValueError(
            "requirement_review.conflict_hints: hints are not in deterministic order"
        )
    if conflict_analysis is not None:
        requested = conflict_analysis["requested_candidate_count"]
        preservation = conflict_analysis["preservation_candidate_count"]
        compared = conflict_analysis["compared_pair_count"]
        hint_count = conflict_analysis["hint_count"]
        assert isinstance(requested, int) and not isinstance(requested, bool)
        assert isinstance(preservation, int) and not isinstance(preservation, bool)
        assert isinstance(compared, int) and not isinstance(compared, bool)
        assert isinstance(hint_count, int) and not isinstance(hint_count, bool)
        if hint_count != len(hints):
            raise ValueError(
                "requirement_review.conflict_analysis.hint_count: does not "
                "match conflict_hints"
            )
        if requested != observed_requested:
            raise ValueError(
                "requirement_review.conflict_analysis.requested_candidate_count: "
                f"expected {observed_requested}"
            )
        if preservation != observed_preservation:
            raise ValueError(
                "requirement_review.conflict_analysis.preservation_candidate_count: "
                f"expected {observed_preservation}"
            )
        if compared > requested * preservation:
            raise ValueError(
                "requirement_review.conflict_analysis.compared_pair_count: "
                "exceeds the possible requested/preservation pairs"
            )
        truncated = conflict_analysis["truncated"]
        assert isinstance(truncated, bool)
        possible_pairs = requested * preservation
        if not truncated and compared != possible_pairs:
            raise ValueError(
                "requirement_review.conflict_analysis.compared_pair_count: "
                "must cover every requested/preservation pair when analysis "
                "is not truncated"
            )
        if truncated and not (
            (compared == _MAX_CONFLICT_PAIRS and compared < possible_pairs)
            or hint_count == _MAX_CONFLICT_HINTS
        ):
            raise ValueError(
                "requirement_review.conflict_analysis.truncated: true requires "
                "the pair or hint limit to have been reached"
            )


def validate_reviewed_requirements_artifact(payload: object) -> None:
    """Validate a finalized reviewed-requirements artifact."""

    _validate_schema(payload, _final_validator(), root="reviewed_requirements")
    assert isinstance(payload, Mapping)
    if type(payload["schema_version"]) is not int:
        raise ValueError("reviewed_requirements.schema_version: expected integer")
    requirements = payload["requirements"]
    audit = payload["audit"]
    draft = payload["draft"]
    assert isinstance(requirements, list)
    assert isinstance(audit, Mapping)
    assert isinstance(draft, Mapping)
    review_binding = payload["review"]
    assert isinstance(review_binding, Mapping)
    for label, value in (
        ("draft.schema_version", draft["schema_version"]),
        ("review.schema_version", review_binding["schema_version"]),
    ):
        if type(value) is not int:
            raise ValueError(
                f"reviewed_requirements.{label}: expected integer"
            )
    decisions = audit["decisions"]
    hints = audit["conflict_hints"]
    conflict_analysis = audit["conflict_analysis"]
    assert isinstance(decisions, list)
    assert isinstance(hints, list)

    requirement_ids: list[str] = []
    for index, requirement in enumerate(requirements):
        assert isinstance(requirement, Mapping)
        identifier = requirement["id"]
        statement = requirement["statement"]
        assert isinstance(identifier, str)
        assert isinstance(statement, str)
        if identifier in requirement_ids:
            raise ValueError(
                f"reviewed_requirements.requirements[{index}].id: "
                "requirement id is duplicated"
            )
        requirement_ids.append(identifier)
        if "\0" in statement:
            raise ValueError(
                f"reviewed_requirements.requirements[{index}].statement: "
                "must not contain NUL characters"
            )

    decision_candidate_ids: list[str] = []
    terminal_requirement_ids: list[str] = []
    expected_requirements: list[dict[str, object]] = []
    counts = {"accept": 0, "edit": 0, "reject": 0}
    for index, decision in enumerate(decisions):
        assert isinstance(decision, Mapping)
        action = decision["action"]
        candidate_id = decision["candidate_id"]
        requirement_id = decision["requirement_id"]
        replacement = decision["replacement"]
        candidate = decision["candidate"]
        assert isinstance(action, str)
        assert isinstance(candidate_id, str)
        assert isinstance(requirement_id, str)
        assert replacement is None or isinstance(replacement, Mapping)
        assert isinstance(candidate, Mapping)
        if action not in _TERMINAL_ACTIONS:
            raise ValueError(
                f"reviewed_requirements.audit.decisions[{index}].action: "
                "decision is not terminal"
            )
        counts[action] += 1
        decision_candidate_ids.append(candidate_id)
        if action in {"accept", "edit"}:
            terminal_requirement_ids.append(requirement_id)
        if (action == "edit") != (replacement is not None):
            raise ValueError(
                f"reviewed_requirements.audit.decisions[{index}].replacement: "
                "replacement must be present exactly for edit decisions"
            )
        if action in {"accept", "edit"}:
            source_kind = candidate["source_kind"]
            source = candidate["source"]
            assert isinstance(source_kind, str)
            assert isinstance(source, Mapping)
            expected_kind = "requested" if source_kind == "issue" else "preservation"
            expected_provenance = (
                "explicit" if source_kind == "issue" else "preexisting"
            )
            statement = candidate["statement"]
            assert isinstance(statement, str)
            if replacement is not None:
                if replacement["kind"] != expected_kind:
                    raise ValueError(
                        f"reviewed_requirements.audit.decisions[{index}].replacement.kind: "
                        "cannot change source-backed kind"
                    )
                if replacement["provenance"] != expected_provenance:
                    raise ValueError(
                        f"reviewed_requirements.audit.decisions[{index}].replacement.provenance: "
                        "cannot change source-backed provenance"
                    )
                statement = replacement["statement"]
                if statement == candidate["statement"]:
                    raise ValueError(
                        f"reviewed_requirements.audit.decisions[{index}].replacement.statement: "
                        "edit does not change the candidate statement"
                    )
            source_reference = (
                f"issue:{source['start_line']}"
                if source_kind == "issue"
                else f"{source['path']}:{source['start_line']}"
            )
            expected_requirements.append(
                {
                    "id": requirement_id,
                    "statement": statement,
                    "kind": expected_kind,
                    "provenance": expected_provenance,
                    "source": source_reference,
                    "excerpt": source["excerpt"],
                }
            )

    if len(set(decision_candidate_ids)) != len(decision_candidate_ids):
        raise ValueError(
            "reviewed_requirements.audit.decisions: candidate decisions must be unique"
        )
    if len(set(terminal_requirement_ids)) != len(terminal_requirement_ids):
        raise ValueError(
            "reviewed_requirements.audit.decisions: requirement ids must be unique"
        )
    if requirement_ids != terminal_requirement_ids:
        raise ValueError(
            "reviewed_requirements.requirements: ids do not match accepted audit "
            "decisions in order"
        )
    if requirements != expected_requirements:
        raise ValueError(
            "reviewed_requirements.requirements: records do not exactly match "
            "the audited candidate decisions"
        )
    expected_counts = {
        "candidate_count": len(decisions),
        "accepted_count": counts["accept"],
        "edited_count": counts["edit"],
        "rejected_count": counts["reject"],
    }
    for field, expected in expected_counts.items():
        if audit[field] != expected:
            raise ValueError(
                f"reviewed_requirements.audit.{field}: expected {expected}"
            )
    if draft["candidate_count"] != len(decisions):
        raise ValueError(
            "reviewed_requirements.draft.candidate_count: does not match the "
            "audit trail"
        )

    # Reuse the ledger validator for ordering, hint references, and terminal
    # decision invariants. The binding values are already schema-valid.
    ledger_projection = {
        "schema_version": 1,
        "artifact_type": "requirement-review",
        "draft": dict(draft),
        "conflict_analysis": conflict_analysis,
        "conflict_hints": hints,
        "decisions": decisions,
    }
    validate_requirement_review_artifact(ledger_projection)


def _validate_schema(
    payload: object,
    validator: Draft202012Validator,
    *,
    root: str,
) -> None:
    errors = tuple(validator.iter_errors(payload))
    if not errors:
        return
    leaves = tuple(leaf for error in errors for leaf in _leaf_errors(error))
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
    raise ValueError(f"{_format_path(root, path)}: {message}")


@lru_cache(maxsize=1)
def _review_validator() -> Draft202012Validator:
    return _load_validator("requirement-review.schema.json")


@lru_cache(maxsize=1)
def _final_validator() -> Draft202012Validator:
    return _load_validator("reviewed-requirements.schema.json")


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
    if error.validator in {"additionalProperties", "unevaluatedProperties"} and isinstance(
        error.instance, Mapping
    ):
        return path, "property is not allowed"
    if error.validator == "type":
        expected = error.validator_value
        return path, f"expected {expected if isinstance(expected, str) else ' or '.join(expected)}"
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
    if error.validator == "maxItems":
        return path, f"must contain at most {error.validator_value} items"
    if error.validator == "minItems":
        return path, f"must contain at least {error.validator_value} items"
    if error.validator == "uniqueItems":
        return path, "items must be unique"
    message = " ".join(error.message.split())
    return path, message[:240]


def _format_path(root: str, path: Sequence[object]) -> str:
    result = root
    for item in path:
        result += f"[{item}]" if isinstance(item, int) else f".{item}"
    return result


__all__ = [
    "validate_requirement_review_artifact",
    "validate_reviewed_requirements_artifact",
]
