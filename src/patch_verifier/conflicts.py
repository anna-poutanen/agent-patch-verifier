"""Conservative, review-only tension hints for requirement candidates.

The functions in this module compare requested issue candidates with
preservation candidates.  They deliberately emit *potential* tensions, not
contradictions: matching text is only a prompt for a human reviewer and says
nothing about which candidate (if either) describes correct behavior.

Only narrow, reproducible signals are considered.  Literal comparisons must
describe the same statically recognizable subject, including identical call
arguments when both subjects are calls.  Polarity comparisons require the
same normalized proposition.  Inputs, comparisons, and output are bounded.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import itertools
import json
import math
import re
from typing import Any, Iterable

from .draft_validation import validate_requirement_draft_artifact
from .models import RequirementKind
from .requirements import (
    CandidateConfidence,
    CandidateReviewStatus,
    CandidateSource,
    CandidateSourceKind,
    RequirementCandidate,
)


_MAX_CANDIDATES = 500
_MAX_STATEMENT_BYTES = 16_000
_MAX_PAIRS = 20_000
_MAX_HINTS = 200
_MAX_EVIDENCE_BYTES = 256
_MAX_ANCHOR_BYTES = 128
_MAX_MESSAGE_BYTES = 1_024

_PRESERVATION_PREFIX = "Preserve the behavior asserted by the base test:"
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:\.[0-9]+)?")
_QUOTED_RE = re.compile(
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",
    re.DOTALL,
)
_NATURAL_LITERAL_RE = re.compile(
    r"^(?P<subject>.+?)\s+"
    r"(?:(?:must|shall|should|needs?\s+to|is\s+expected\s+to)\s+)?"
    r"(?P<negation>(?:not|never)\s+)?"
    r"(?P<predicate>returns?|equals?|is|be|produces?|yields?|"
    r"defaults?\s+to|resolves?\s+to)\s+"
    r"(?P<value>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_SUBJECT_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "behavior",
        "function",
        "method",
        "result",
        "the",
        "value",
    }
)
_PROPOSITION_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "are",
        "asserted",
        "base",
        "be",
        "been",
        "behavior",
        "being",
        "by",
        "did",
        "do",
        "does",
        "expected",
        "is",
        "must",
        "need",
        "needs",
        "preserve",
        "shall",
        "should",
        "test",
        "that",
        "the",
        "this",
        "to",
    }
)
_ACTION_FORMS: dict[str, tuple[str, bool]] = {
    "accept": ("accept", False),
    "accepted": ("accept", False),
    "accepting": ("accept", False),
    "accepts": ("accept", False),
    "reject": ("accept", True),
    "rejected": ("accept", True),
    "rejecting": ("accept", True),
    "rejects": ("accept", True),
    "allow": ("allow", False),
    "allowed": ("allow", False),
    "allowing": ("allow", False),
    "allows": ("allow", False),
    "deny": ("allow", True),
    "denied": ("allow", True),
    "denies": ("allow", True),
    "disallow": ("allow", True),
    "disallowed": ("allow", True),
    "disallows": ("allow", True),
    "forbid": ("allow", True),
    "forbidden": ("allow", True),
    "forbids": ("allow", True),
    "enable": ("enable", False),
    "enabled": ("enable", False),
    "enables": ("enable", False),
    "disable": ("enable", True),
    "disabled": ("enable", True),
    "disables": ("enable", True),
    "include": ("include", False),
    "included": ("include", False),
    "includes": ("include", False),
    "exclude": ("include", True),
    "excluded": ("include", True),
    "excludes": ("include", True),
}


class ConflictAnalysisError(ValueError):
    """Raised when an input cannot be analyzed within the fixed limits."""


class ConflictSignal(str, Enum):
    """A narrow textual signal that may deserve human review."""

    NUMERIC_LITERAL_DIVERGENCE = "numeric_literal_divergence"
    STRING_LITERAL_DIVERGENCE = "string_literal_divergence"
    POLARITY_DIVERGENCE = "polarity_divergence"


_SIGNAL_ORDER = {
    ConflictSignal.NUMERIC_LITERAL_DIVERGENCE: 0,
    ConflictSignal.STRING_LITERAL_DIVERGENCE: 1,
    ConflictSignal.POLARITY_DIVERGENCE: 2,
}


@dataclass(frozen=True)
class ConflictHint:
    """One stable, review-only hint connecting exactly two candidates.

    ``candidate_ids`` always contains the requested candidate first and the
    preservation candidate second.  ``to_dict`` intentionally emits the
    review ledger's minimal hint shape; typed signal and evidence fields remain
    available to library callers.
    """

    id: str
    candidate_ids: tuple[str, str]
    signal: ConflictSignal
    requested_evidence: str
    preservation_evidence: str
    shared_anchor: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", ConflictSignal(self.signal))
        if not re.fullmatch(r"hint-[0-9a-f]{64}", self.id):
            raise ValueError("conflict hint id must be a stable SHA-256 identifier")
        if len(self.candidate_ids) != 2 or self.candidate_ids[0] == self.candidate_ids[1]:
            raise ValueError("conflict hints require two distinct candidate ids")
        if any(
            re.fullmatch(r"cand-[0-9a-f]{64}", candidate_id) is None
            for candidate_id in self.candidate_ids
        ):
            raise ValueError("conflict hints require valid candidate ids")
        for label, value, limit in (
            ("requested evidence", self.requested_evidence, _MAX_EVIDENCE_BYTES),
            (
                "preservation evidence",
                self.preservation_evidence,
                _MAX_EVIDENCE_BYTES,
            ),
            ("shared anchor", self.shared_anchor, _MAX_ANCHOR_BYTES),
            ("message", self.message, _MAX_MESSAGE_BYTES),
        ):
            if not value or len(value.encode("utf-8")) > limit:
                raise ValueError(f"conflict hint {label} must be non-empty and bounded")
            if "\0" in value:
                raise ValueError(f"conflict hint {label} must not contain NUL")

    def to_dict(self) -> dict[str, Any]:
        """Return the strict, minimal representation used by review ledgers."""

        return {
            "id": self.id,
            "candidate_ids": sorted(self.candidate_ids),
            "message": self.message,
        }


@dataclass(frozen=True)
class ConflictAnalysis:
    """Deterministic output and explicit resource accounting."""

    hints: tuple[ConflictHint, ...]
    requested_candidate_count: int
    preservation_candidate_count: int
    compared_pair_count: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "hints": [hint.to_dict() for hint in self.hints],
            "requested_candidate_count": self.requested_candidate_count,
            "preservation_candidate_count": self.preservation_candidate_count,
            "compared_pair_count": self.compared_pair_count,
            "hint_count": len(self.hints),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class _CallIdentity:
    name: str
    arguments: str


@dataclass(frozen=True)
class _Subject:
    tokens: frozenset[str]
    call: _CallIdentity | None


@dataclass(frozen=True)
class _LiteralValue:
    kind: str
    value: str | Decimal


@dataclass(frozen=True)
class _LiteralFrame:
    subject: _Subject
    literal: _LiteralValue
    evidence: str
    positive: bool


@dataclass(frozen=True)
class _PolarityFrame:
    proposition: frozenset[str]
    positive: bool
    evidence: str


@dataclass(frozen=True)
class _CandidateFacts:
    candidate: RequirementCandidate
    literal: _LiteralFrame | None
    polarity: _PolarityFrame | None


@dataclass(frozen=True)
class _DetectedSignal:
    signal: ConflictSignal
    requested_evidence: str
    preservation_evidence: str
    shared_anchor: str


def analyze_potential_conflicts(
    candidates: Iterable[RequirementCandidate],
) -> ConflictAnalysis:
    """Find conservative tension hints among unreviewed candidates.

    The iterable may contain at most 500 candidates.  This function never
    selects a winner and never promotes a candidate into a requirement.  A
    ``truncated`` result means the deterministic pair or hint budget was
    reached; callers should surface that fact during review.
    """

    try:
        candidate_iterator = iter(candidates)
    except TypeError as exc:
        raise ConflictAnalysisError("candidates must be iterable") from exc
    bounded = list(itertools.islice(candidate_iterator, _MAX_CANDIDATES + 1))
    if len(bounded) > _MAX_CANDIDATES:
        raise ConflictAnalysisError(
            f"candidate input exceeds the {_MAX_CANDIDATES}-candidate limit"
        )
    if any(not isinstance(candidate, RequirementCandidate) for candidate in bounded):
        raise ConflictAnalysisError(
            "conflict analysis requires RequirementCandidate inputs"
        )

    unique: dict[str, RequirementCandidate] = {}
    for candidate in bounded:
        previous = unique.get(candidate.id)
        if previous is not None and previous != candidate:
            raise ConflictAnalysisError(
                f"candidate id {candidate.id} refers to different candidates"
            )
        unique[candidate.id] = candidate

    requested: list[_CandidateFacts] = []
    preservation: list[_CandidateFacts] = []
    for candidate in sorted(unique.values(), key=lambda item: item.id):
        if "\0" in candidate.statement:
            raise ConflictAnalysisError(
                f"candidate {candidate.id} statement contains NUL"
            )
        try:
            statement_size = len(candidate.statement.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ConflictAnalysisError(
                f"candidate {candidate.id} statement is not valid Unicode"
            ) from exc
        if statement_size > _MAX_STATEMENT_BYTES:
            raise ConflictAnalysisError(
                f"candidate {candidate.id} statement exceeds the "
                f"{_MAX_STATEMENT_BYTES}-byte analysis limit"
            )
        facts = _facts(candidate)
        if (
            candidate.intent is RequirementKind.REQUESTED
            and candidate.source_kind is CandidateSourceKind.ISSUE
        ):
            requested.append(facts)
        elif candidate.intent is RequirementKind.PRESERVATION:
            preservation.append(facts)

    hints: list[ConflictHint] = []
    compared = 0
    truncated = False
    for requested_facts in requested:
        for preservation_facts in preservation:
            if compared >= _MAX_PAIRS:
                truncated = True
                break
            compared += 1
            for detected in _detect_signals(requested_facts, preservation_facts):
                if len(hints) >= _MAX_HINTS:
                    truncated = True
                    break
                hints.append(
                    _make_hint(
                        requested_facts.candidate,
                        preservation_facts.candidate,
                        detected,
                    )
                )
                if len(hints) >= _MAX_HINTS:
                    # Reaching the hard output budget ends analysis.  Marking
                    # the boundary conservatively avoids implying that later
                    # pairs and signals were inspected.
                    truncated = True
                    break
            if len(hints) >= _MAX_HINTS:
                break
        if truncated or len(hints) >= _MAX_HINTS:
            break

    # The review ledger's canonical hint ordering is the stable hint ID.
    hints.sort(key=lambda hint: hint.id)
    return ConflictAnalysis(
        hints=tuple(hints),
        requested_candidate_count=len(requested),
        preservation_candidate_count=len(preservation),
        compared_pair_count=compared,
        truncated=truncated,
    )


def analyze_draft_conflicts(
    draft_artifact: Mapping[str, object],
) -> ConflictAnalysis:
    """Validate a requirement-draft artifact and analyze its candidates.

    This is the safe JSON-to-domain bridge for CLI and review-ledger callers.
    Validation includes the artifact's cross-field provenance, exact source
    hashes and spans, deterministic candidate IDs, and source-kind semantics.
    The reconstruction does not accept, edit, or otherwise promote candidates.
    """

    if not isinstance(draft_artifact, Mapping):
        raise ConflictAnalysisError("requirement draft artifact must be an object")
    draft = draft_artifact.get("draft")
    if not isinstance(draft, Mapping):
        raise ConflictAnalysisError("requirement draft artifact is missing its draft")
    raw_candidates = draft.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ConflictAnalysisError("requirement draft candidates must be an array")
    if len(raw_candidates) > _MAX_CANDIDATES:
        raise ConflictAnalysisError(
            f"requirement draft exceeds the {_MAX_CANDIDATES}-candidate limit"
        )
    try:
        validate_requirement_draft_artifact(draft_artifact)
    except ValueError as exc:
        raise ConflictAnalysisError(
            f"requirement draft artifact is invalid: {exc}"
        ) from exc

    draft = draft_artifact.get("draft")
    if not isinstance(draft, Mapping):  # defensive against mutable custom mappings
        raise ConflictAnalysisError("validated requirement draft is missing its draft")
    raw_candidates = draft.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ConflictAnalysisError(
            "validated requirement draft candidates must be an array"
        )
    candidates = tuple(
        _candidate_from_artifact(candidate, index)
        for index, candidate in enumerate(raw_candidates)
    )
    return analyze_potential_conflicts(candidates)


def _candidate_from_artifact(
    raw: object,
    index: int,
) -> RequirementCandidate:
    if not isinstance(raw, Mapping):
        raise ConflictAnalysisError(
            f"validated requirement draft candidate {index} must be an object"
        )
    source_raw = raw.get("source")
    if not isinstance(source_raw, Mapping):
        raise ConflictAnalysisError(
            f"validated requirement draft candidate {index} source must be an object"
        )
    try:
        source = CandidateSource(
            path=source_raw["path"],
            start_line=source_raw["start_line"],
            end_line=source_raw["end_line"],
            start_column=source_raw["start_column"],
            end_column=source_raw["end_column"],
            excerpt=source_raw["excerpt"],
        )
        if source.sha256 != source_raw["sha256"]:
            raise ValueError("source digest changed after artifact validation")
        return RequirementCandidate(
            id=raw["id"],
            statement=raw["statement"],
            source_kind=CandidateSourceKind(raw["source_kind"]),
            source=source,
            review_status=CandidateReviewStatus(raw["review_status"]),
            confidence=CandidateConfidence(raw["confidence"]),
            rationale=raw["rationale"],
            intent=RequirementKind(raw["intent"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConflictAnalysisError(
            f"validated requirement draft candidate {index} could not be reconstructed: "
            f"{exc}"
        ) from exc


def _facts(candidate: RequirementCandidate) -> _CandidateFacts:
    text = candidate.statement.strip()
    if text.startswith(_PRESERVATION_PREFIX):
        text = text[len(_PRESERVATION_PREFIX) :].strip()
    literal = _literal_frame(text)
    return _CandidateFacts(
        candidate=candidate,
        literal=literal,
        polarity=None if literal is not None else _polarity_frame(text),
    )


def _literal_frame(text: str) -> _LiteralFrame | None:
    python_frame = _python_literal_frame(text)
    if python_frame is not None:
        return python_frame
    match = _NATURAL_LITERAL_RE.fullmatch(text)
    if match is None:
        return None
    literal_match = _literal_from_suffix(match.group("value"))
    if literal_match is None:
        return None
    literal, literal_text = literal_match
    subject = _subject(match.group("subject"))
    if not subject.tokens:
        return None
    negation = match.group("negation") or ""
    evidence = f"{negation}{match.group('predicate')} {literal_text}".strip()
    if not _bounded_evidence(evidence):
        return None
    return _LiteralFrame(
        subject=subject,
        literal=literal,
        evidence=evidence,
        positive=not bool(negation),
    )


def _python_literal_frame(text: str) -> _LiteralFrame | None:
    try:
        expression = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    if not isinstance(expression, ast.Compare) or len(expression.ops) != 1:
        return None
    if len(expression.comparators) != 1:
        return None
    operator = expression.ops[0]
    if not isinstance(operator, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
        return None

    left = expression.left
    right = expression.comparators[0]
    left_literal = _ast_literal(left)
    right_literal = _ast_literal(right)
    if (left_literal is None) == (right_literal is None):
        return None
    literal_node, subject_node, literal = (
        (left, right, left_literal)
        if left_literal is not None
        else (right, left, right_literal)
    )
    literal_text = ast.get_source_segment(text, literal_node)
    subject_text = ast.get_source_segment(text, subject_node)
    if not literal_text or not subject_text:
        return None
    subject = _subject(subject_text)
    if not subject.tokens or not _bounded_evidence(literal_text):
        return None
    return _LiteralFrame(
        subject=subject,
        literal=literal,
        evidence=literal_text,
        positive=isinstance(operator, (ast.Eq, ast.Is)),
    )


def _literal_from_suffix(text: str) -> tuple[_LiteralValue, str] | None:
    stripped = text.strip()
    attempts: list[str] = []
    if stripped.endswith((".", ";")):
        attempts.append(stripped[:-1].rstrip())
    attempts.append(stripped)
    for attempt in attempts:
        if not attempt:
            continue
        try:
            node = ast.parse(attempt, mode="eval").body
        except (SyntaxError, ValueError, MemoryError, RecursionError):
            continue
        value = _ast_literal(node)
        if value is not None:
            return value, attempt
    return None


def _ast_literal(node: ast.AST) -> _LiteralValue | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return _LiteralValue("string", value)
    if isinstance(value, int):
        return _LiteralValue("numeric", Decimal(value))
    if isinstance(value, float) and math.isfinite(value):
        try:
            return _LiteralValue("numeric", Decimal(str(value)).normalize())
        except InvalidOperation:  # pragma: no cover - finite floats are valid
            return None
    return None


def _subject(text: str) -> _Subject:
    stripped = text.strip()
    call: _CallIdentity | None = None
    try:
        expression = ast.parse(stripped, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        expression = None
    if isinstance(expression, ast.Call):
        try:
            name = _dotted_name(expression.func)
            arguments = (
                None
                if name is None
                else ast.dump(
                    ast.Tuple(
                        elts=[*expression.args, *expression.keywords],
                        ctx=ast.Load(),
                    ),
                    annotate_fields=True,
                    include_attributes=False,
                )
            )
        except (MemoryError, RecursionError):
            # Deep but syntactically valid expressions can exceed ast.dump's
            # recursion budget. Make the subject unmatchable: falling back to
            # word tokens could conflate two calls whose deep arguments differ.
            return _Subject(tokens=frozenset(), call=None)
        if name is not None and arguments is not None:
            call = _CallIdentity(name.casefold(), arguments)
    tokens = frozenset(
        token
        for token in _word_tokens(stripped)
        if token not in _SUBJECT_STOP_WORDS
    )
    return _Subject(tokens=tokens, call=call)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _polarity_frame(text: str) -> _PolarityFrame | None:
    masked = _QUOTED_RE.sub(lambda match: " " * len(match.group(0)), text)
    matches = list(_WORD_RE.finditer(masked))
    if not matches:
        return None

    explicit_negations = [
        match for match in matches if match.group(0).casefold() in {"not", "never"}
    ]
    actions: list[tuple[re.Match[str], str, bool]] = []
    for match in matches:
        normalized = match.group(0).casefold()
        action = _ACTION_FORMS.get(normalized)
        if action is not None:
            actions.append((match, action[0], action[1]))

    # Multiple action or negation cues make scope and double-negation too
    # ambiguous for this deliberately narrow detector.
    if len(actions) != 1 or len(explicit_negations) > 1:
        return None
    action_match, canonical_action, inherently_negative = actions[0]
    has_explicit_negation = bool(explicit_negations)
    positive = not (inherently_negative ^ has_explicit_negation)

    proposition: set[str] = set()
    for match in matches:
        token = match.group(0).casefold()
        if token in {"not", "never"} or token in _PROPOSITION_STOP_WORDS:
            continue
        if match is action_match:
            proposition.add(canonical_action)
        else:
            proposition.add(token)
    if len(proposition) < 2:
        return None

    if has_explicit_negation:
        negation = explicit_negations[0]
        start = min(negation.start(), action_match.start())
        end = max(negation.end(), action_match.end())
        evidence = text[start:end]
    else:
        evidence = text[action_match.start() : action_match.end()]
    if not _bounded_evidence(evidence):
        return None
    return _PolarityFrame(
        proposition=frozenset(proposition),
        positive=positive,
        evidence=evidence,
    )


def _detect_signals(
    requested: _CandidateFacts,
    preservation: _CandidateFacts,
) -> tuple[_DetectedSignal, ...]:
    detected: list[_DetectedSignal] = []
    literal_polarity_detected = False
    left_literal = requested.literal
    right_literal = preservation.literal
    if left_literal is not None and right_literal is not None:
        anchor = _shared_subject_anchor(left_literal.subject, right_literal.subject)
        if anchor is not None and left_literal.literal.kind == right_literal.literal.kind:
            if left_literal.literal.value != right_literal.literal.value:
                signal = (
                    ConflictSignal.NUMERIC_LITERAL_DIVERGENCE
                    if left_literal.literal.kind == "numeric"
                    else ConflictSignal.STRING_LITERAL_DIVERGENCE
                )
                detected.append(
                    _DetectedSignal(
                        signal,
                        left_literal.evidence,
                        right_literal.evidence,
                        anchor,
                    )
                )
            if left_literal.positive != right_literal.positive:
                literal_polarity_detected = True
                detected.append(
                    _DetectedSignal(
                        ConflictSignal.POLARITY_DIVERGENCE,
                        left_literal.evidence,
                        right_literal.evidence,
                        anchor,
                    )
                )

    left_polarity = requested.polarity
    right_polarity = preservation.polarity
    if (
        not literal_polarity_detected
        and left_polarity is not None
        and right_polarity is not None
        and left_polarity.positive != right_polarity.positive
        and left_polarity.proposition == right_polarity.proposition
    ):
        anchor = " ".join(sorted(left_polarity.proposition))
        if _bounded(anchor, _MAX_ANCHOR_BYTES):
            detected.append(
                _DetectedSignal(
                    ConflictSignal.POLARITY_DIVERGENCE,
                    left_polarity.evidence,
                    right_polarity.evidence,
                    anchor,
                )
            )
    return tuple(sorted(detected, key=lambda item: _SIGNAL_ORDER[item.signal]))


def _shared_subject_anchor(left: _Subject, right: _Subject) -> str | None:
    if left.call is not None and right.call is not None:
        if left.call != right.call:
            return None
        return left.call.name if _bounded(left.call.name, _MAX_ANCHOR_BYTES) else None

    # Without two exact call shapes, require the complete meaningful subject
    # vocabulary to match.  This intentionally misses paraphrases in exchange
    # for avoiding cross-input and cross-feature false positives.
    if not left.tokens or left.tokens != right.tokens:
        return None
    anchor = " ".join(sorted(left.tokens))
    return anchor if _bounded(anchor, _MAX_ANCHOR_BYTES) else None


def _word_tokens(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD_RE.finditer(text))


def _make_hint(
    requested: RequirementCandidate,
    preservation: RequirementCandidate,
    detected: _DetectedSignal,
) -> ConflictHint:
    requested_evidence = _json_quote(detected.requested_evidence)
    preservation_evidence = _json_quote(detected.preservation_evidence)
    anchor = _json_quote(detected.shared_anchor)
    message = (
        f"Potential tension for review [{detected.signal.value}]: requested "
        f"evidence {requested_evidence}; preservation evidence "
        f"{preservation_evidence}; shared behavior anchor {anchor}. This "
        "heuristic does not establish a contradiction or which behavior is correct."
    )
    if not _bounded(message, _MAX_MESSAGE_BYTES):  # pragma: no cover - guarded inputs
        raise ConflictAnalysisError("generated conflict hint message exceeds its limit")
    identity = json.dumps(
        {
            "candidate_ids": [requested.id, preservation.id],
            "preservation_evidence": detected.preservation_evidence,
            "requested_evidence": detected.requested_evidence,
            "shared_anchor": detected.shared_anchor,
            "signal": detected.signal.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    hint_id = "hint-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return ConflictHint(
        id=hint_id,
        candidate_ids=(requested.id, preservation.id),
        signal=detected.signal,
        requested_evidence=detected.requested_evidence,
        preservation_evidence=detected.preservation_evidence,
        shared_anchor=detected.shared_anchor,
        message=message,
    )


def _json_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _bounded(value: str, maximum: int) -> bool:
    return bool(value) and "\0" not in value and len(value.encode("utf-8")) <= maximum


def _bounded_evidence(value: str) -> bool:
    """Bound raw evidence and its escaped representation in hint messages."""

    return _bounded(value, _MAX_EVIDENCE_BYTES) and len(
        _json_quote(value).encode("utf-8")
    ) <= _MAX_EVIDENCE_BYTES
