from __future__ import annotations

import hashlib
import json

import pytest

import patch_verifier.conflicts as conflicts_module
from patch_verifier.conflicts import (
    ConflictAnalysisError,
    ConflictSignal,
    analyze_draft_conflicts,
    analyze_potential_conflicts,
)
from patch_verifier.models import RequirementKind
from patch_verifier.requirements import (
    CandidateConfidence,
    CandidateReviewStatus,
    CandidateSource,
    CandidateSourceKind,
    RequirementCandidate,
)


def candidate(
    statement: str,
    *,
    intent: RequirementKind,
    source_kind: CandidateSourceKind | None = None,
    source_excerpt: str | None = None,
) -> RequirementCandidate:
    source_kind = source_kind or (
        CandidateSourceKind.ISSUE
        if intent is RequirementKind.REQUESTED
        else CandidateSourceKind.PYTEST_ASSERTION
    )
    excerpt = source_excerpt if source_excerpt is not None else statement or "source"
    source = CandidateSource(
        path="issue.md" if source_kind is CandidateSourceKind.ISSUE else "tests/test_x.py",
        start_line=1,
        end_line=1,
        start_column=0,
        end_column=len(excerpt.encode()),
        excerpt=excerpt,
    )
    canonical = json.dumps(
        {
            "intent": intent.value,
            "source": source.to_dict(),
            "source_kind": source_kind.value,
            "statement": statement,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return RequirementCandidate(
        id=f"cand-{digest}",
        statement=statement,
        source_kind=source_kind,
        source=source,
        review_status=CandidateReviewStatus.REQUIRES_REVIEW,
        confidence=CandidateConfidence.HIGH,
        rationale="Review-only test candidate.",
        intent=intent,
    )


def test_numeric_literal_divergence_has_stable_minimal_ledger_shape() -> None:
    requested = candidate(
        "resolve(None) must return 0.", intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: resolve(None) == 5",
        intent=RequirementKind.PRESERVATION,
    )

    forward = analyze_potential_conflicts([requested, preservation])
    reverse = analyze_potential_conflicts([preservation, requested])

    assert forward == reverse
    assert forward.truncated is False
    assert forward.compared_pair_count == 1
    assert len(forward.hints) == 1
    hint = forward.hints[0]
    assert hint.signal is ConflictSignal.NUMERIC_LITERAL_DIVERGENCE
    assert hint.candidate_ids == (requested.id, preservation.id)
    assert hint.requested_evidence == "return 0"
    assert hint.preservation_evidence == "5"
    assert hint.shared_anchor == "resolve"
    assert "Potential tension for review" in hint.message
    assert "does not establish a contradiction" in hint.message
    assert hint.to_dict() == {
        "id": hint.id,
        "candidate_ids": sorted([requested.id, preservation.id]),
        "message": hint.message,
    }
    assert hint.id.startswith("hint-") and len(hint.id) == 69
    assert forward.to_dict()["hint_count"] == 1


def test_string_literal_divergence_preserves_exact_quoted_evidence() -> None:
    requested = candidate(
        'format_mode() must return "safe".', intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: format_mode() == 'legacy'",
        intent=RequirementKind.PRESERVATION,
    )

    hint = analyze_potential_conflicts([requested, preservation]).hints[0]

    assert hint.signal is ConflictSignal.STRING_LITERAL_DIVERGENCE
    assert hint.requested_evidence == 'return "safe"'
    assert hint.preservation_evidence == "'legacy'"
    assert '[string_literal_divergence]' in hint.message


def test_literal_polarity_divergence_requires_same_value_and_subject() -> None:
    requested = candidate(
        "resolve(None) must not return 5.", intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: resolve(None) == 5",
        intent=RequirementKind.PRESERVATION,
    )

    hints = analyze_potential_conflicts([requested, preservation]).hints

    assert [hint.signal for hint in hints] == [ConflictSignal.POLARITY_DIVERGENCE]
    assert hints[0].requested_evidence == "not return 5"


def test_literal_and_polarity_signals_can_both_remain_visible() -> None:
    requested = candidate(
        "resolve(None) must not return 0.", intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: resolve(None) == 5",
        intent=RequirementKind.PRESERVATION,
    )

    signals = [
        hint.signal
        for hint in analyze_potential_conflicts([requested, preservation]).hints
    ]

    assert sorted(signals, key=lambda signal: signal.value) == sorted(
        [
            ConflictSignal.NUMERIC_LITERAL_DIVERGENCE,
            ConflictSignal.POLARITY_DIVERGENCE,
        ],
        key=lambda signal: signal.value,
    )


@pytest.mark.parametrize(
    ("requested_statement", "preservation_statement", "requested_evidence", "preservation_evidence"),
    [
        (
            "The parser must not accept empty input.",
            "The parser must accept empty input.",
            "not accept",
            "accept",
        ),
        (
            "Reject empty input.",
            "Accept empty input.",
            "Reject",
            "Accept",
        ),
        (
            "Do not reject empty input.",
            "Reject empty input.",
            "not reject",
            "Reject",
        ),
    ],
)
def test_natural_language_polarity_is_review_only_and_handles_double_negation(
    requested_statement: str,
    preservation_statement: str,
    requested_evidence: str,
    preservation_evidence: str,
) -> None:
    requested = candidate(requested_statement, intent=RequirementKind.REQUESTED)
    preservation = candidate(
        preservation_statement,
        intent=RequirementKind.PRESERVATION,
        source_kind=CandidateSourceKind.DOCSTRING,
    )

    hint = analyze_potential_conflicts([requested, preservation]).hints[0]

    assert hint.signal is ConflictSignal.POLARITY_DIVERGENCE
    assert hint.requested_evidence == requested_evidence
    assert hint.preservation_evidence == preservation_evidence


@pytest.mark.parametrize(
    ("requested_statement", "preservation_statement"),
    [
        ("resolve(None) must return 5.", "resolve(None) == 5"),
        ("resolve(0) must return 0.", "resolve(3) == 3"),
        ("timeout must be 5.", "retries == 3"),
        ("request timeout must be 5.", "response timeout == 3"),
        ("The parser must accept empty input.", "The validator must reject empty input."),
        ('message() must return "not accepted".', 'message() == "not accepted"'),
    ],
)
def test_no_signal_controls_minimize_false_positives(
    requested_statement: str,
    preservation_statement: str,
) -> None:
    requested = candidate(requested_statement, intent=RequirementKind.REQUESTED)
    preservation = candidate(
        "Preserve the behavior asserted by the base test: " + preservation_statement,
        intent=RequirementKind.PRESERVATION,
    )

    result = analyze_potential_conflicts([requested, preservation])

    assert result.hints == ()
    assert result.compared_pair_count == 1


def test_unrepresentable_escaped_evidence_is_skipped_without_overflow() -> None:
    escaped = "\\\\" * 100
    requested = candidate(
        f'mode() must return "{escaped}".', intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        'Preserve the behavior asserted by the base test: mode() == "legacy"',
        intent=RequirementKind.PRESERVATION,
    )

    result = analyze_potential_conflicts([requested, preservation])

    assert result.hints == ()
    assert result.truncated is False


def test_only_requested_issue_candidates_are_compared() -> None:
    inferred_requested = candidate(
        "resolve() must return 0",
        intent=RequirementKind.REQUESTED,
        source_kind=CandidateSourceKind.DOCSTRING,
    )
    preservation = candidate("resolve() == 5", intent=RequirementKind.PRESERVATION)

    result = analyze_potential_conflicts([inferred_requested, preservation])

    assert result.requested_candidate_count == 0
    assert result.preservation_candidate_count == 1
    assert result.compared_pair_count == 0
    assert result.hints == ()


def test_pair_and_hint_budgets_are_explicit_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = [
        candidate(f"operation_{index}() must return 0", intent=RequirementKind.REQUESTED)
        for index in range(2)
    ]
    preservation = [
        candidate(f"operation_{index}() == 1", intent=RequirementKind.PRESERVATION)
        for index in range(2)
    ]
    monkeypatch.setattr(conflicts_module, "_MAX_PAIRS", 1)

    first = analyze_potential_conflicts([*requested, *preservation])
    second = analyze_potential_conflicts([*reversed(preservation), *reversed(requested)])

    assert first == second
    assert first.truncated is True
    assert first.compared_pair_count == 1
    assert len(first.hints) <= 1


def test_candidate_limit_fails_closed_without_partially_analyzing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conflicts_module, "_MAX_CANDIDATES", 1)
    candidates = [
        candidate("resolve() must return 0", intent=RequirementKind.REQUESTED),
        candidate("resolve() == 5", intent=RequirementKind.PRESERVATION),
    ]

    with pytest.raises(ConflictAnalysisError, match="candidate input exceeds"):
        analyze_potential_conflicts(candidates)


def test_deep_valid_call_subject_does_not_escape_ast_dump_recursion() -> None:
    requested_argument = "~" * 1_000 + "1"
    preservation_argument = "~" * 1_001 + "1"
    requested = candidate(
        f"f({requested_argument}) == 0",
        intent=RequirementKind.REQUESTED,
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: "
        f"f({preservation_argument}) == 5",
        intent=RequirementKind.PRESERVATION,
    )

    result = analyze_potential_conflicts([requested, preservation])

    assert result.compared_pair_count == 1
    assert result.hints == ()


def test_non_iterable_candidate_input_has_domain_error() -> None:
    with pytest.raises(ConflictAnalysisError, match="must be iterable"):
        analyze_potential_conflicts(None)  # type: ignore[arg-type]


def test_reaching_hint_budget_reports_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = [
        candidate(f"operation_{index}() must return 0", intent=RequirementKind.REQUESTED)
        for index in range(2)
    ]
    preservation = [
        candidate(f"operation_{index}() == 1", intent=RequirementKind.PRESERVATION)
        for index in range(2)
    ]
    monkeypatch.setattr(conflicts_module, "_MAX_HINTS", 1)

    result = analyze_potential_conflicts([*requested, *preservation])

    assert len(result.hints) == 1
    assert result.truncated is True


def test_duplicate_id_with_different_content_fails_closed() -> None:
    first = candidate("resolve() must return 0", intent=RequirementKind.REQUESTED)
    second = candidate("resolve() must return 1", intent=RequirementKind.REQUESTED)
    object.__setattr__(second, "id", first.id)

    with pytest.raises(ConflictAnalysisError, match="refers to different candidates"):
        analyze_potential_conflicts([first, second])


@pytest.mark.parametrize(
    ("invalid_statement", "message"),
    [("resolve()\0 == 5", "contains NUL"), ("resolve() == '\ud800'", "valid Unicode")],
)
def test_invalid_statement_text_fails_closed(
    invalid_statement: str, message: str
) -> None:
    item = candidate("safe source", intent=RequirementKind.PRESERVATION)
    object.__setattr__(item, "statement", invalid_statement)

    with pytest.raises(ConflictAnalysisError, match=message):
        analyze_potential_conflicts([item])


def test_validated_draft_artifact_bridge_reconstructs_exact_candidates() -> None:
    requested = candidate(
        "resolve(None) must return 0.", intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: resolve(None) == 5",
        intent=RequirementKind.PRESERVATION,
        source_excerpt="assert resolve(None) == 5",
    )
    artifact = draft_artifact(requested, preservation)

    result = analyze_draft_conflicts(artifact)

    assert result.requested_candidate_count == 1
    assert result.preservation_candidate_count == 1
    assert [hint.signal for hint in result.hints] == [
        ConflictSignal.NUMERIC_LITERAL_DIVERGENCE
    ]
    assert result.hints[0].candidate_ids == (requested.id, preservation.id)


def test_draft_artifact_bridge_rejects_provenance_drift() -> None:
    requested = candidate(
        "resolve(None) must return 0.", intent=RequirementKind.REQUESTED
    )
    preservation = candidate(
        "Preserve the behavior asserted by the base test: resolve(None) == 5",
        intent=RequirementKind.PRESERVATION,
        source_excerpt="assert resolve(None) == 5",
    )
    artifact = draft_artifact(requested, preservation)
    artifact["draft"]["candidates"][0]["source"]["sha256"] = "f" * 64

    with pytest.raises(ConflictAnalysisError, match="artifact is invalid"):
        analyze_draft_conflicts(artifact)


def draft_artifact(
    requested: RequirementCandidate,
    preservation: RequirementCandidate,
) -> dict[str, object]:
    zeros = "0" * 64
    return {
        "schema_version": 1,
        "tool_version": "0.3.0",
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.11.0",
            "platform": "test",
        },
        "inputs": {
            "repository": "/repository",
            "base_revision": "HEAD",
            "base_sha": "0" * 40,
            "base_context_sha256": zeros,
            "patched_tree_hash": "1" * 40,
            "patch_sha256": zeros,
            "issue_sha256": zeros,
            "issue_source": "issue.md",
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": [requested.to_dict(), preservation.to_dict()],
            "diagnostics": [],
            "limitations": [],
            "truncated": False,
        },
    }
