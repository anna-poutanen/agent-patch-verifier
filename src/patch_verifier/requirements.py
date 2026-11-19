"""Deterministic, review-only requirement candidate extraction.

This module produces a *core draft*: source-derived candidates, diagnostics,
and analysis limitations.  A draft is not an evidence plan, no candidate is a
verified oracle, and this module deliberately provides no automatic conversion
to :class:`patch_verifier.models.Requirement`.

The CLI provenance envelope is a separate concern.  Integrators that persist a
draft must add the exact base commit/tree, issue hash, context hash, tool
version, and other run-level provenance outside this module's artifact.

Source locations use one-based line numbers and zero-based, end-exclusive UTF-8
byte columns.  ``CandidateSource.sha256`` hashes exactly
``excerpt.encode("utf-8")``; it is not a whole-document hash.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import inspect
import io
import json
from pathlib import Path, PurePosixPath
import re
import tokenize
from typing import Any, Iterable

from .context import ContextItem, ContextKind, ContextRevision, RepositoryContext
from .models import RequirementKind


_MAX_ISSUE_BYTES = 1_000_000
_MAX_ISSUE_LINES = 20_000
_MAX_RELEVANT_ITEMS = 1_000
_MAX_RELEVANT_FILES = 200
_MAX_FILE_BYTES = 2_000_000
_MAX_TOTAL_SOURCE_BYTES = 20_000_000
_MAX_SOURCE_TOKENS = 100_000
_MAX_AST_NODES = 100_000
_MAX_ASSERTIONS_PER_TEST = 50
_MAX_EXCERPT_BYTES = 16_000
_MAX_STATEMENT_CHARS = 4_000
_MAX_DISCOVERED_CANDIDATES = 5_000
_MAX_CANDIDATES = 500
_MAX_DIAGNOSTICS = 200
_MAX_LIMITATIONS = 50
_MAX_LIMITATION_CHARS = 4_000
_MAX_SOURCE_NAME_CHARS = 512

_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_LIST_ITEM_RE = re.compile(
    r"^[ \t]{0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+"
    r"(?:\[[ xX]\][ \t]+)?(?P<text>.*\S.*)$"
)
_NORMATIVE_RE = re.compile(
    r"\b(?:must|shall|should|required\s+to|needs?\s+to|expected\s+to)\b",
    re.IGNORECASE,
)
_NON_REQUIREMENT_RE = re.compile(
    r"\b(?:not\s+(?:a|an)\s+requirement|non[- ]requirement|"
    r"example\s+only)\b",
    re.IGNORECASE,
)
_UNSAFE_ISSUE_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"not\s+requested|"
    r"not\s+(?:a|an)\s+(?:requested\s+)?(?:behavioral\s+)?"
    r"(?:contract|requirement|acceptance\s+criterion)|"
    r"non[- ]goals?|"
    r"customer\s+quote|"
    r"(?:developer|customer|user|reviewer)\s+"
    r"(?:said|asked|wondered|suggested|proposed|claimed)|"
    r"wondered\s+whether|"
    r"implementation\s+(?:idea|sketch)|"
    r"hypothetical|speculative|maybe|might|could"
    r")\b",
    re.IGNORECASE,
)
_UNDESIRED_BEHAVIOR_HEADINGS = frozenset(
    {
        "actual behavior",
        "actual result",
        "actual results",
        "alternatives considered",
        "broken behavior",
        "bug",
        "bug description",
        "current behavior",
        "current broken behavior",
        "current result",
        "current results",
        "existing behavior",
        "non-goal",
        "non-goals",
        "non goal",
        "non goals",
        "not in scope",
        "observed behavior",
        "observed result",
        "observed results",
        "out of scope",
        "problem",
        "problem statement",
        "rejected alternative",
        "rejected alternatives",
        "rejected approach",
        "rejected approaches",
        "steps to reproduce",
        "what happens",
    }
)
_BEHAVIOR_DOCSTRING_RE = re.compile(
    r"^(?:return|raise|yield|reject|preserve|default|ensure|require|accept)s?\b",
    re.IGNORECASE,
)
_REQUIREMENT_HEADINGS = frozenset(
    {
        "acceptance criteria",
        "behavior",
        "behavioral requirements",
        "desired behavior",
        "expected behavior",
        "expected result",
        "expected results",
        "requirements",
        "specification",
    }
)
_EXCLUDING_TEST_MARKERS = frozenset(
    {"expectedfailure", "skip", "skipif", "skipunless", "xfail"}
)
_SOURCE_KIND_ORDER: dict["CandidateSourceKind", int] = {}


class RequirementExtractionError(ValueError):
    """Raised when safe, faithful candidate extraction cannot begin."""


class CandidateSourceKind(str, Enum):
    """The kind of source from which a candidate was transcribed."""

    ISSUE = "issue"
    PYTEST_ASSERTION = "pytest_assertion"
    DOCSTRING = "docstring"


_SOURCE_KIND_ORDER.update(
    {
        CandidateSourceKind.ISSUE: 0,
        CandidateSourceKind.PYTEST_ASSERTION: 1,
        CandidateSourceKind.DOCSTRING: 2,
    }
)


class CandidateReviewStatus(str, Enum):
    """Candidates are unusable as oracles until an external review step."""

    REQUIRES_REVIEW = "requires_review"


class CandidateConfidence(str, Enum):
    """Confidence that text was extracted as intended, not that it is correct."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class CandidateSource:
    """An exact, bounded source anchor for one candidate.

    Lines are one-based. Columns are zero-based UTF-8 byte offsets and the end
    column is exclusive. ``sha256`` covers the exact UTF-8 bytes of ``excerpt``.
    """

    path: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    excerpt: str
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.path or any(character in self.path for character in "\r\n\0"):
            raise ValueError("candidate source path must be a non-empty single line")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("candidate source line range is invalid")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("candidate source columns must be non-negative")
        if self.start_line == self.end_line and self.end_column < self.start_column:
            raise ValueError("candidate source column range is invalid")
        if not self.excerpt:
            raise ValueError("candidate source excerpt must not be empty")
        try:
            excerpt_bytes = self.excerpt.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no cover - guarded at inputs
            raise ValueError("candidate source excerpt must be valid Unicode") from exc
        object.__setattr__(self, "sha256", hashlib.sha256(excerpt_bytes).hexdigest())

    @property
    def reference(self) -> str:
        """Return the familiar ``path:start`` source citation."""

        return f"{self.path}:{self.start_line}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_column": self.start_column,
            "end_column": self.end_column,
            "excerpt": self.excerpt,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class RequirementCandidate:
    """A traceable hypothesis that always requires human review."""

    id: str
    statement: str
    source_kind: CandidateSourceKind
    source: CandidateSource
    review_status: CandidateReviewStatus
    confidence: CandidateConfidence
    rationale: str
    intent: RequirementKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", CandidateSourceKind(self.source_kind))
        object.__setattr__(
            self, "review_status", CandidateReviewStatus(self.review_status)
        )
        object.__setattr__(self, "confidence", CandidateConfidence(self.confidence))
        object.__setattr__(self, "intent", RequirementKind(self.intent))
        if self.review_status is not CandidateReviewStatus.REQUIRES_REVIEW:
            raise ValueError("requirement candidates must require review")
        if not re.fullmatch(r"cand-[0-9a-f]{64}", self.id):
            raise ValueError("candidate id must be a stable SHA-256 identifier")
        if not self.statement.strip():
            raise ValueError("candidate statement must not be empty")
        if not self.rationale.strip():
            raise ValueError("candidate rationale must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "source_kind": self.source_kind.value,
            "source": self.source.to_dict(),
            "review_status": self.review_status.value,
            "confidence": self.confidence.value,
            "rationale": self.rationale,
            "intent": self.intent.value,
        }


@dataclass(frozen=True)
class RequirementDiagnostic:
    """A bounded explanation of source that could not be considered."""

    code: str
    source: str
    message: str
    line: int | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.code):
            raise ValueError("diagnostic code must be a non-empty machine name")
        if not self.source:
            raise ValueError("diagnostic source must not be empty")
        if not self.message.strip():
            raise ValueError("diagnostic message must not be empty")
        if self.line is not None and self.line < 1:
            raise ValueError("diagnostic line must be one-based")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "source": self.source,
            "message": self.message,
            "line": self.line,
        }


@dataclass(frozen=True)
class RequirementDraft:
    """A deterministic collection of unreviewed requirement candidates."""

    candidates: tuple[RequirementCandidate, ...]
    diagnostics: tuple[RequirementDiagnostic, ...]
    limitations: tuple[str, ...]
    truncated: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "limitations": list(self.limitations),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class _RelevantItem:
    path: str
    start: int
    end: int
    symbol: str | None
    kind: ContextKind


@dataclass(frozen=True)
class _Definition:
    qualified_name: str
    start: int
    end: int
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    pytest_excluded: bool


@dataclass
class _ExtractionState:
    candidates: dict[str, RequirementCandidate] = field(default_factory=dict)
    diagnostics: list[RequirementDiagnostic] = field(default_factory=list)
    truncated: bool = False
    candidate_scan_limit_reported: bool = False
    diagnostic_limit_reached: bool = False

    def diagnostic(
        self,
        code: str,
        source: str,
        message: str,
        line: int | None = None,
        *,
        truncated: bool = False,
    ) -> None:
        self.truncated = self.truncated or truncated
        item = RequirementDiagnostic(code, source, message, line)
        if _MAX_DIAGNOSTICS <= 0:
            self.diagnostic_limit_reached = True
            self.truncated = True
            return
        if len(self.diagnostics) < _MAX_DIAGNOSTICS:
            self.diagnostics.append(item)
            return
        self.diagnostic_limit_reached = True
        self.truncated = True
        marker = RequirementDiagnostic(
            "diagnostic_limit_reached",
            ".",
            "additional requirement-extraction diagnostics were omitted after "
            f"the {_MAX_DIAGNOSTICS}-entry limit was reached",
        )
        if self.diagnostics[-1].code != marker.code:
            self.diagnostics[-1] = marker

    def add_candidate(self, candidate: RequirementCandidate) -> None:
        if candidate.id in self.candidates:
            return
        if len(self.candidates) >= _MAX_DISCOVERED_CANDIDATES:
            self.truncated = True
            if not self.candidate_scan_limit_reported:
                self.candidate_scan_limit_reported = True
                self.diagnostic(
                    "candidate_scan_limit_reached",
                    ".",
                    "candidate discovery stopped after "
                    f"{_MAX_DISCOVERED_CANDIDATES} unique candidates",
                    truncated=True,
                )
            return
        self.candidates[candidate.id] = candidate


_LIMITATIONS = (
    "Every extracted candidate requires explicit review and is not a verified "
    "requirement or test oracle.",
    "Issue language, existing tests, and docstrings may be incomplete, stale, "
    "incorrect, or mutually inconsistent.",
    "Base-side contracts are limited to statically selected pytest assertions "
    "and behavioral docstrings present in the supplied base context.",
    "Static extraction does not establish runtime reachability or behavioral "
    "correctness.",
)


def draft_requirements(
    issue_text: str | bytes,
    base_root: Path,
    context: RepositoryContext,
    *,
    issue_source: str = "issue.md",
) -> RequirementDraft:
    """Draft review-only requirements from an issue and base-side context.

    ``context`` must have been collected from the base revision.  Existing
    contracts are re-read from safe repository-relative files under
    ``base_root``; context excerpts themselves are never promoted into
    candidates without re-parsing the referenced source.
    """

    normalized_issue = _validate_issue(issue_text, issue_source)
    root = _validate_base_root(base_root)
    if not isinstance(context, RepositoryContext):
        raise RequirementExtractionError("context must be a RepositoryContext")
    if context.revision is not ContextRevision.BASE:
        raise RequirementExtractionError(
            "requirement contracts must use repository context from the base revision"
        )

    state = _ExtractionState()
    limitations = _import_context_metadata(context, state)
    _extract_issue(normalized_issue, issue_source, state)
    relevant = _relevant_context_items(context, state)
    _extract_base_contracts(root, relevant, state)

    ordered = sorted(state.candidates.values(), key=_candidate_order_key)
    if len(ordered) > _MAX_CANDIDATES:
        state.diagnostic(
            "candidate_limit_reached",
            ".",
            f"retained the first {_MAX_CANDIDATES} candidates in deterministic "
            f"order and omitted {len(ordered) - _MAX_CANDIDATES}",
            truncated=True,
        )
        ordered = ordered[:_MAX_CANDIDATES]
    diagnostics = tuple(sorted(state.diagnostics, key=_diagnostic_order_key))
    return RequirementDraft(
        candidates=tuple(ordered),
        diagnostics=diagnostics,
        limitations=limitations,
        truncated=state.truncated,
    )


def _import_context_metadata(
    context: RepositoryContext, state: _ExtractionState
) -> tuple[str, ...]:
    """Carry upstream omissions and static-analysis caveats into the draft."""

    for diagnostic in context.diagnostics:
        raw_code = f"context_{diagnostic.code}"
        code = (
            raw_code
            if len(raw_code) <= 128
            and re.fullmatch(r"[a-z][a-z0-9_]*", raw_code)
            else "context_diagnostic"
        )
        source = diagnostic.path or "."
        if len(source) > 4_096 or any(character in source for character in "\r\n\0"):
            source = "."
        message = f"Base repository context: {diagnostic.message.strip()}"
        if len(message) > 4_000:
            message = message[:3_997] + "..."
        line = diagnostic.line if diagnostic.line and diagnostic.line >= 1 else None
        state.diagnostic(code, source, message, line, truncated=True)

    upstream: set[str] = set()
    for limitation in context.limitations:
        if isinstance(limitation, str) and limitation.strip():
            upstream.add(f"Base context limitation: {limitation.strip()}")
    for symbol in context.symbols:
        for limitation in symbol.limitations:
            if isinstance(limitation, str) and limitation.strip():
                upstream.add(f"Base context limitation: {limitation.strip()}")
                state.truncated = True

    bounded: list[str] = []
    for limitation in sorted(upstream):
        if len(limitation) > _MAX_LIMITATION_CHARS:
            state.diagnostic(
                "context_limitation_too_large",
                ".",
                "an oversized base-context limitation was omitted",
                truncated=True,
            )
            continue
        bounded.append(limitation)

    available = max(0, _MAX_LIMITATIONS - len(_LIMITATIONS))
    if len(bounded) > available:
        state.diagnostic(
            "context_limitation_limit_reached",
            ".",
            f"retained the first {available} base-context limitations and omitted "
            f"{len(bounded) - available}",
            truncated=True,
        )
        bounded = bounded[:available]
    return (*_LIMITATIONS, *bounded)


def _validate_issue(issue_text: str | bytes, issue_source: str) -> str:
    if not isinstance(issue_source, str) or not issue_source.strip():
        raise RequirementExtractionError("issue_source must be a non-empty string")
    if len(issue_source) > _MAX_SOURCE_NAME_CHARS or any(
        character in issue_source for character in "\r\n\0"
    ):
        raise RequirementExtractionError("issue_source must be a bounded single line")
    if isinstance(issue_text, bytes):
        issue_bytes = issue_text
        if len(issue_bytes) > _MAX_ISSUE_BYTES:
            raise RequirementExtractionError(
                f"issue_text exceeds the {_MAX_ISSUE_BYTES}-byte limit"
            )
        try:
            normalized = issue_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RequirementExtractionError(
                "issue_text bytes must be valid UTF-8"
            ) from exc
    elif isinstance(issue_text, str):
        normalized = issue_text
        try:
            issue_bytes = normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RequirementExtractionError(
                "issue_text must contain valid Unicode scalar values"
            ) from exc
    else:
        raise RequirementExtractionError("issue_text must be a string or bytes")
    if len(issue_bytes) > _MAX_ISSUE_BYTES:
        raise RequirementExtractionError(
            f"issue_text exceeds the {_MAX_ISSUE_BYTES}-byte limit"
        )
    if "\0" in normalized:
        raise RequirementExtractionError("issue_text contains a NUL character")
    if len(normalized.splitlines()) > _MAX_ISSUE_LINES:
        raise RequirementExtractionError(
            f"issue_text exceeds the {_MAX_ISSUE_LINES}-line limit"
        )
    return normalized


def _validate_base_root(base_root: Path) -> Path:
    try:
        root = Path(base_root).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise RequirementExtractionError(
            f"cannot resolve base repository root {base_root!r}: {exc}"
        ) from exc
    if not root.is_dir():
        raise RequirementExtractionError(
            f"base repository root is not a directory: {base_root}"
        )
    return root


def _extract_issue(
    issue_text: str,
    issue_source: str,
    state: _ExtractionState,
) -> None:
    lines = issue_text.splitlines()
    excluded_lines = _excluded_issue_lines(lines)
    active_requirement_level: int | None = None
    active_undesired_level: int | None = None
    fence_character: str | None = None
    fence_length = 0
    fence_line: int | None = None
    inside_comment = False
    comment_line: int | None = None

    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence_character is not None:
            marker = fence.group(1) if fence else ""
            if marker and marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
                fence_line = None
            continue
        if fence:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            fence_line = line_number
            continue

        visible_line = line
        contains_html_comment = inside_comment or "<!--" in visible_line
        if inside_comment:
            closing = visible_line.find("-->")
            if closing < 0:
                continue
            visible_line = " " * (closing + 3) + visible_line[closing + 3 :]
            inside_comment = False
            comment_line = None
        while "<!--" in visible_line:
            opening = visible_line.find("<!--")
            closing = visible_line.find("-->", opening + 4)
            if closing < 0:
                visible_line = visible_line[:opening] + " " * (
                    len(visible_line) - opening
                )
                inside_comment = True
                comment_line = line_number
                break
            visible_line = (
                visible_line[:opening]
                + " " * (closing + 3 - opening)
                + visible_line[closing + 3 :]
            )

        heading = _HEADING_RE.match(visible_line)
        if heading:
            level = len(heading.group(1))
            heading_text = _normalize_heading(heading.group(2))
            if heading_text in _REQUIREMENT_HEADINGS:
                active_requirement_level = level
                active_undesired_level = None
            elif heading_text in _UNDESIRED_BEHAVIOR_HEADINGS:
                active_requirement_level = None
                active_undesired_level = level
            else:
                if (
                    active_requirement_level is not None
                    and level <= active_requirement_level
                ):
                    active_requirement_level = None
                if (
                    active_undesired_level is not None
                    and level <= active_undesired_level
                ):
                    active_undesired_level = None
            continue

        # Removing an inline comment would make the displayed excerpt differ
        # from the contiguous source bytes named by its span. Skip the whole
        # line rather than manufacture provenance that cannot be replayed.
        if contains_html_comment:
            continue

        stripped = visible_line.lstrip()
        if (
            not stripped
            or stripped.startswith((">", "|"))
            or len(visible_line) - len(stripped) >= 4
        ):
            continue

        if line_number in excluded_lines:
            continue
        if active_undesired_level is not None:
            continue

        list_item = _LIST_ITEM_RE.match(visible_line)
        if list_item:
            raw = list_item.group("text")
            start_character = list_item.start("text")
            in_requirement_section = active_requirement_level is not None
        else:
            raw = visible_line
            start_character = 0
            in_requirement_section = False

        leading = len(raw) - len(raw.lstrip())
        excerpt = raw.strip()
        start_character += leading
        if not excerpt or _NON_REQUIREMENT_RE.search(_mask_inline_code(excerpt)):
            continue
        marker_text = _mask_inline_code(excerpt)
        normative = _NORMATIVE_RE.search(marker_text) is not None
        if not normative and not (list_item and in_requirement_section):
            continue
        if excerpt.endswith("?"):
            continue
        if re.match(
            r"^(?:should|can|could|would)\b", marker_text, re.IGNORECASE
        ) and not (list_item and in_requirement_section):
            continue
        if not any(character.isalnum() for character in excerpt):
            continue

        start_column = len(line[:start_character].encode("utf-8"))
        end_character = start_character + len(excerpt)
        end_column = len(line[:end_character].encode("utf-8"))
        source = CandidateSource(
            path=issue_source,
            start_line=line_number,
            end_line=line_number,
            start_column=start_column,
            end_column=end_column,
            excerpt=excerpt,
        )
        rationale = (
            "List item under an explicit requirements or acceptance section in "
            "the supplied issue; review is still required."
            if list_item and in_requirement_section and not normative
            else "Explicit normative language in the supplied issue; review is "
            "still required."
        )
        candidate = _build_candidate(
            statement=excerpt,
            source_kind=CandidateSourceKind.ISSUE,
            source=source,
            confidence=CandidateConfidence.HIGH,
            rationale=rationale,
            intent=RequirementKind.REQUESTED,
            state=state,
        )
        if candidate is not None:
            state.add_candidate(candidate)

    if fence_character is not None:
        state.diagnostic(
            "unterminated_markdown_fence",
            issue_source,
            "content after an unterminated fenced code block was ignored",
            fence_line,
            truncated=True,
        )
    if inside_comment:
        state.diagnostic(
            "unterminated_html_comment",
            issue_source,
            "content after an unterminated HTML comment was ignored",
            comment_line,
            truncated=True,
        )


def _relevant_context_items(
    context: RepositoryContext, state: _ExtractionState
) -> tuple[_RelevantItem, ...]:
    discovered: set[_RelevantItem] = set()
    for symbol in context.symbols:
        for item in symbol.items:
            if item.kind is ContextKind.DOCSTRING:
                discovered.add(_relevant_item(item))
            elif item.kind is ContextKind.NEARBY_TEST and any(
                reason.code == "test_calls_changed_symbol"
                for reason in item.selection_reasons
            ):
                discovered.add(_relevant_item(item))

    ordered = sorted(
        discovered,
        key=lambda item: (
            item.path,
            item.start,
            item.end,
            item.kind.value,
            item.symbol or "",
        ),
    )
    if len(ordered) > _MAX_RELEVANT_ITEMS:
        state.diagnostic(
            "relevant_item_limit_reached",
            ".",
            f"retained the first {_MAX_RELEVANT_ITEMS} relevant context items "
            f"and omitted {len(ordered) - _MAX_RELEVANT_ITEMS}",
            truncated=True,
        )
        ordered = ordered[:_MAX_RELEVANT_ITEMS]

    paths = sorted({item.path for item in ordered})
    if len(paths) > _MAX_RELEVANT_FILES:
        allowed = set(paths[:_MAX_RELEVANT_FILES])
        omitted = len(paths) - _MAX_RELEVANT_FILES
        state.diagnostic(
            "relevant_file_limit_reached",
            ".",
            f"retained the first {_MAX_RELEVANT_FILES} relevant source files "
            f"and omitted {omitted}",
            truncated=True,
        )
        ordered = [item for item in ordered if item.path in allowed]
    return tuple(ordered)


def _relevant_item(item: ContextItem) -> _RelevantItem:
    return _RelevantItem(
        path=item.path,
        start=item.start_line,
        end=item.end_line,
        symbol=item.symbol,
        kind=item.kind,
    )


def _extract_base_contracts(
    root: Path,
    relevant: tuple[_RelevantItem, ...],
    state: _ExtractionState,
) -> None:
    by_path: dict[str, list[_RelevantItem]] = {}
    for item in relevant:
        by_path.setdefault(item.path, []).append(item)

    total_bytes = 0
    aggregate_limit_reported = False
    for relative_path in sorted(by_path):
        safe_path = _safe_source_path(root, relative_path, state)
        if safe_path is None:
            continue
        try:
            with safe_path.open("rb") as source_file:
                source_bytes = source_file.read(_MAX_FILE_BYTES + 1)
        except OSError as exc:
            state.diagnostic(
                "source_read_error",
                relative_path,
                f"could not read relevant base source: {type(exc).__name__}",
            )
            continue
        if len(source_bytes) > _MAX_FILE_BYTES:
            state.diagnostic(
                "source_file_too_large",
                relative_path,
                f"relevant source exceeds the {_MAX_FILE_BYTES}-byte limit",
                truncated=True,
            )
            continue
        if total_bytes + len(source_bytes) > _MAX_TOTAL_SOURCE_BYTES:
            if not aggregate_limit_reported:
                aggregate_limit_reported = True
                state.diagnostic(
                    "aggregate_source_limit_reached",
                    ".",
                    "relevant base source exceeds the aggregate "
                    f"{_MAX_TOTAL_SOURCE_BYTES}-byte limit",
                    truncated=True,
                )
            continue
        total_bytes += len(source_bytes)

        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(source_bytes).readline)
            source_text = source_bytes.decode(encoding)
        except (LookupError, SyntaxError, UnicodeError) as exc:
            line = exc.lineno if isinstance(exc, SyntaxError) else None
            state.diagnostic(
                "syntax_error" if isinstance(exc, SyntaxError) else "source_decode_error",
                relative_path,
                (
                    exc.msg
                    if isinstance(exc, SyntaxError)
                    else f"could not decode relevant source: {type(exc).__name__}"
                ),
                line,
            )
            continue

        if not _within_token_limit(source_text):
            state.diagnostic(
                "source_token_limit_reached",
                relative_path,
                f"relevant source exceeds the {_MAX_SOURCE_TOKENS}-token "
                "pre-parse limit",
                truncated=True,
            )
            continue
        try:
            tree = ast.parse(source_text, filename=relative_path)
        except SyntaxError as exc:
            state.diagnostic(
                "syntax_error",
                relative_path,
                exc.msg,
                exc.lineno,
            )
            continue
        except RecursionError:
            state.diagnostic(
                "analysis_depth_exceeded",
                relative_path,
                "AST parsing exceeds the analysis depth limit",
            )
            continue
        if not _within_ast_limit(tree):
            state.diagnostic(
                "ast_node_limit_reached",
                relative_path,
                f"relevant source exceeds the {_MAX_AST_NODES}-node AST limit",
                truncated=True,
            )
            continue
        try:
            definitions = _definitions(tree)
        except RecursionError:
            state.diagnostic(
                "analysis_depth_exceeded",
                relative_path,
                "AST traversal exceeds the analysis depth limit",
            )
            continue

        for item in sorted(
            by_path[relative_path],
            key=lambda value: (
                value.start,
                value.end,
                value.kind.value,
                value.symbol or "",
            ),
        ):
            matches = _matching_definitions(definitions, item)
            if len(matches) != 1:
                state.diagnostic(
                    "ambiguous_context_source",
                    relative_path,
                    "context item could not be matched to exactly one base-side "
                    "definition",
                    item.start,
                )
                continue
            definition = matches[0]
            if item.kind is ContextKind.NEARBY_TEST:
                _extract_assertions(
                    relative_path, source_text, definition, state
                )
            elif item.kind is ContextKind.DOCSTRING:
                _extract_docstring(
                    relative_path, source_text, definition, state
                )


def _safe_source_path(
    root: Path, relative_path: str, state: _ExtractionState
) -> Path | None:
    pure = PurePosixPath(relative_path)
    if (
        not relative_path
        or pure.is_absolute()
        or ".." in pure.parts
        or any(part in {"", "."} for part in pure.parts)
    ):
        state.diagnostic(
            "unsafe_source_path",
            relative_path or ".",
            "context source is not a safe repository-relative path",
        )
        return None
    candidate = root.joinpath(*pure.parts)
    current = root
    try:
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                state.diagnostic(
                    "symlink_source_skipped",
                    relative_path,
                    "symlinked context source or parent was not used",
                )
                return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        state.diagnostic(
            "source_path_error",
            relative_path,
            f"context source could not be resolved safely: {type(exc).__name__}",
        )
        return None
    if not resolved.is_file():
        state.diagnostic(
            "source_not_regular_file",
            relative_path,
            "context source is not a regular file",
        )
        return None
    return resolved


def _within_ast_limit(tree: ast.AST) -> bool:
    try:
        for index, _ in enumerate(ast.walk(tree), start=1):
            if index > _MAX_AST_NODES:
                return False
    except RecursionError:
        return False
    return True


def _within_token_limit(source_text: str) -> bool:
    try:
        for index, _ in enumerate(
            tokenize.generate_tokens(io.StringIO(source_text).readline), start=1
        ):
            if index > _MAX_SOURCE_TOKENS:
                return False
    except (IndentationError, tokenize.TokenError):
        # Preserve the more specific SyntaxError diagnostic from ast.parse.
        return True
    return True


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _pytest_exclusion_aliases(
    body: Iterable[ast.stmt],
    inherited: tuple[frozenset[str], frozenset[str]] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    namespaces = set(inherited[0]) if inherited else {"pytest", "unittest"}
    direct_markers = set(inherited[1]) if inherited else set()
    for statement in body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name in {"pytest", "unittest"}:
                    namespaces.add(alias.asname or alias.name)
        elif isinstance(statement, ast.ImportFrom) and statement.module in {
            "pytest",
            "unittest",
        }:
            for alias in statement.names:
                local_name = alias.asname or alias.name
                canonical = alias.name.casefold()
                if canonical == "mark":
                    namespaces.add(local_name)
                elif canonical in _EXCLUDING_TEST_MARKERS:
                    direct_markers.add(local_name)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            if value is None:
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else (statement.target,)
            )
            target_names = tuple(
                target.id for target in targets if isinstance(target, ast.Name)
            )
            if not target_names:
                continue
            value_name = _dotted_name(value)
            if value_name is None:
                continue
            current = (frozenset(namespaces), frozenset(direct_markers))
            if _dotted_name_is_excluding_marker(value_name, current):
                direct_markers.update(target_names)
            elif _dotted_name_is_marker_namespace(value_name, current):
                namespaces.update(target_names)
    return frozenset(namespaces), frozenset(direct_markers)


def _dotted_name_is_marker_namespace(
    name: str,
    aliases: tuple[frozenset[str], frozenset[str]],
) -> bool:
    namespaces, _ = aliases
    parts = name.casefold().split(".")
    folded_namespaces = frozenset(item.casefold() for item in namespaces)
    return parts[0] in folded_namespaces and (
        len(parts) == 1 or parts[-1] == "mark"
    )


def _dotted_name_is_excluding_marker(
    name: str,
    aliases: tuple[frozenset[str], frozenset[str]],
) -> bool:
    namespaces, direct_markers = aliases
    parts = name.casefold().split(".")
    folded_namespaces = frozenset(item.casefold() for item in namespaces)
    folded_direct_markers = frozenset(item.casefold() for item in direct_markers)
    if parts[0] in folded_direct_markers:
        return True
    marker = parts[-1]
    if marker not in _EXCLUDING_TEST_MARKERS:
        return False
    return (
        len(parts) == 1
        or "mark" in parts[:-1]
        or (len(parts) > 1 and parts[-2] in {"pytest", "unittest"})
        or parts[0] in folded_namespaces
    )


def _contains_excluding_pytest_marker(
    node: ast.AST,
    aliases: tuple[frozenset[str], frozenset[str]],
) -> bool:
    for child in ast.walk(node):
        name = _dotted_name(child)
        if name is not None and _dotted_name_is_excluding_marker(name, aliases):
            return True
    return False


def _scope_has_excluding_pytestmark(
    body: Iterable[ast.stmt],
    aliases: tuple[frozenset[str], frozenset[str]],
) -> bool:
    for statement in body:
        value: ast.AST | None = None
        targets: Iterable[ast.expr] = ()
        if isinstance(statement, ast.Assign):
            value = statement.value
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            value = statement.value
            targets = (statement.target,)
        if value is None:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ) and _contains_excluding_pytest_marker(value, aliases):
            return True
    return False


def _function_has_runtime_pytest_exclusion(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: tuple[frozenset[str], frozenset[str]],
) -> bool:
    aliases = _pytest_exclusion_aliases(function.body, aliases)
    found = False

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            nonlocal found
            if _contains_excluding_pytest_marker(node, aliases):
                found = True
                return
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            return

        def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef
        ) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
            return

    visitor = Visitor()
    for statement in function.body:
        visitor.visit(statement)
        if found:
            break
    return found


def _definitions(tree: ast.Module) -> tuple[_Definition, ...]:
    found: list[_Definition] = []
    excluded_class_names: set[str] = set()
    aliases = _pytest_exclusion_aliases(tree.body)
    module_excluded = _scope_has_excluding_pytestmark(tree.body, aliases)

    def visit(
        node: ast.AST,
        parents: tuple[str, ...],
        inherited_exclusion: bool,
        scope_aliases: tuple[frozenset[str], frozenset[str]],
    ) -> None:
        next_parents = parents
        next_exclusion = inherited_exclusion
        next_aliases = scope_aliases
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified_name = ".".join((*parents, node.name))
            decorator_lines = [decorator.lineno for decorator in node.decorator_list]
            own_exclusion = any(
                _contains_excluding_pytest_marker(decorator, scope_aliases)
                for decorator in node.decorator_list
            )
            if isinstance(node, ast.ClassDef):
                next_aliases = _pytest_exclusion_aliases(
                    node.body, scope_aliases
                )
                own_exclusion = own_exclusion or any(
                    (base_name := _dotted_name(base)) is not None
                    and (
                        base_name in excluded_class_names
                        or base_name.rsplit(".", 1)[-1] in excluded_class_names
                    )
                    for base in node.bases
                )
                own_exclusion = own_exclusion or _scope_has_excluding_pytestmark(
                    node.body, next_aliases
                )
            else:
                next_aliases = _pytest_exclusion_aliases(
                    node.body, scope_aliases
                )
                own_exclusion = (
                    own_exclusion
                    or _function_has_runtime_pytest_exclusion(
                        node, scope_aliases
                    )
                )
            next_exclusion = inherited_exclusion or own_exclusion
            found.append(
                _Definition(
                    qualified_name=qualified_name,
                    start=min([node.lineno, *decorator_lines]),
                    end=getattr(node, "end_lineno", node.lineno),
                    node=node,
                    pytest_excluded=next_exclusion,
                )
            )
            next_parents = (*parents, node.name)
            if isinstance(node, ast.ClassDef) and next_exclusion:
                excluded_class_names.update({qualified_name, node.name})
        for child in ast.iter_child_nodes(node):
            visit(child, next_parents, next_exclusion, next_aliases)

    visit(tree, (), module_excluded, aliases)
    return tuple(
        sorted(found, key=lambda item: (item.start, item.end, item.qualified_name))
    )


def _matching_definitions(
    definitions: tuple[_Definition, ...], item: _RelevantItem
) -> tuple[_Definition, ...]:
    return tuple(
        definition
        for definition in definitions
        if (item.symbol is None or definition.qualified_name == item.symbol)
        and definition.start <= item.end
        and definition.end >= item.start
    )


def _extract_assertions(
    path: str,
    source_text: str,
    definition: _Definition,
    state: _ExtractionState,
) -> None:
    if not isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        state.diagnostic(
            "pytest_scope_not_function",
            path,
            "selected pytest context is not a function definition",
            definition.start,
        )
        return
    if definition.pytest_excluded:
        state.diagnostic(
            "pytest_non_contract_marker",
            path,
            "selected pytest function or an enclosing scope is marked skipped "
            "or expected-failure; its assertions were not drafted",
            definition.start,
        )
        return
    assertions = _direct_assertions(definition.node)
    if len(assertions) > _MAX_ASSERTIONS_PER_TEST:
        state.diagnostic(
            "assertion_limit_reached",
            path,
            f"retained the first {_MAX_ASSERTIONS_PER_TEST} assertions in "
            f"{definition.qualified_name} and omitted "
            f"{len(assertions) - _MAX_ASSERTIONS_PER_TEST}",
            definition.start,
            truncated=True,
        )
        assertions = assertions[:_MAX_ASSERTIONS_PER_TEST]
    for assertion in assertions:
        excerpt = ast.get_source_segment(source_text, assertion)
        expression = ast.get_source_segment(source_text, assertion.test)
        if not excerpt or not expression:
            state.diagnostic(
                "source_segment_unavailable",
                path,
                "could not recover the exact pytest assertion source",
                assertion.lineno,
            )
            continue
        statement = "Preserve the behavior asserted by the base test: " + expression.strip()
        source = _python_source(path, assertion, excerpt)
        candidate = _build_candidate(
            statement=statement,
            source_kind=CandidateSourceKind.PYTEST_ASSERTION,
            source=source,
            confidence=CandidateConfidence.MEDIUM,
            rationale=(
                "A statically selected base-side pytest test contains this exact "
                "assertion. Review is required to confirm it is an intended, "
                "current contract and relates to the patch."
            ),
            intent=RequirementKind.PRESERVATION,
            state=state,
        )
        if candidate is not None:
            state.add_candidate(candidate)


def _direct_assertions(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Assert]:
    assertions: list[ast.Assert] = []

    class Visitor(ast.NodeVisitor):
        def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
            assertions.append(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            return

        def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef
        ) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
            return

    visitor = Visitor()
    for statement in function.body:
        visitor.visit(statement)
    return sorted(
        assertions,
        key=lambda node: (
            node.lineno,
            node.col_offset,
            getattr(node, "end_lineno", node.lineno),
            getattr(node, "end_col_offset", node.col_offset),
        ),
    )


def _extract_docstring(
    path: str,
    source_text: str,
    definition: _Definition,
    state: _ExtractionState,
) -> None:
    docstring = ast.get_docstring(definition.node, clean=True)
    body = getattr(definition.node, "body", ())
    expression = body[0] if body else None
    if (
        docstring is None
        or not isinstance(expression, ast.Expr)
        or not isinstance(expression.value, ast.Constant)
        or not isinstance(expression.value.value, str)
    ):
        state.diagnostic(
            "docstring_source_unavailable",
            path,
            "selected context definition has no traceable literal docstring",
            definition.start,
        )
        return
    statement = inspect.cleandoc(docstring).strip()
    if not statement or not looks_behavioral_docstring(statement):
        return
    excerpt = ast.get_source_segment(source_text, expression)
    if not excerpt:
        state.diagnostic(
            "source_segment_unavailable",
            path,
            "could not recover the exact docstring literal source",
            expression.lineno,
        )
        return
    source = _python_source(path, expression, excerpt)
    candidate = _build_candidate(
        statement=statement,
        source_kind=CandidateSourceKind.DOCSTRING,
        source=source,
        confidence=CandidateConfidence.LOW,
        rationale=(
            "A behavioral docstring on a statically selected base-side definition "
            "states this behavior. Review is required because documentation can "
            "be stale or descriptive rather than normative."
        ),
        intent=RequirementKind.PRESERVATION,
        state=state,
    )
    if candidate is not None:
        state.add_candidate(candidate)


def looks_behavioral_docstring(statement: str) -> bool:
    """Return whether docstring text expresses a behavioral expectation."""

    first_line = next(
        (line.strip() for line in statement.splitlines() if line.strip()), ""
    )
    return bool(
        _NORMATIVE_RE.search(_mask_inline_code(statement))
        or _BEHAVIOR_DOCSTRING_RE.match(first_line)
    )


def _python_source(path: str, node: ast.AST, excerpt: str) -> CandidateSource:
    return CandidateSource(
        path=path,
        start_line=node.lineno,
        end_line=getattr(node, "end_lineno", node.lineno),
        start_column=node.col_offset,
        end_column=getattr(node, "end_col_offset", node.col_offset),
        excerpt=excerpt,
    )


def _build_candidate(
    *,
    statement: str,
    source_kind: CandidateSourceKind,
    source: CandidateSource,
    confidence: CandidateConfidence,
    rationale: str,
    intent: RequirementKind,
    state: _ExtractionState,
) -> RequirementCandidate | None:
    if len(statement) > _MAX_STATEMENT_CHARS:
        state.diagnostic(
            "candidate_statement_too_large",
            source.path,
            f"candidate statement exceeds the {_MAX_STATEMENT_CHARS}-character limit",
            source.start_line,
            truncated=True,
        )
        return None
    if len(source.excerpt.encode("utf-8")) > _MAX_EXCERPT_BYTES:
        state.diagnostic(
            "candidate_excerpt_too_large",
            source.path,
            f"candidate excerpt exceeds the {_MAX_EXCERPT_BYTES}-byte limit",
            source.start_line,
            truncated=True,
        )
        return None
    identifier_payload = {
        "intent": intent.value,
        "source": source.to_dict(),
        "source_kind": source_kind.value,
        "statement": statement,
    }
    canonical = json.dumps(
        identifier_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identifier = "cand-" + hashlib.sha256(canonical).hexdigest()
    return RequirementCandidate(
        id=identifier,
        statement=statement,
        source_kind=source_kind,
        source=source,
        review_status=CandidateReviewStatus.REQUIRES_REVIEW,
        confidence=confidence,
        rationale=rationale,
        intent=intent,
    )


def _candidate_order_key(candidate: RequirementCandidate) -> tuple[Any, ...]:
    return (
        _SOURCE_KIND_ORDER[candidate.source_kind],
        candidate.source.path,
        candidate.source.start_line,
        candidate.source.start_column,
        candidate.source.end_line,
        candidate.source.end_column,
        candidate.statement,
        candidate.id,
    )


def _diagnostic_order_key(diagnostic: RequirementDiagnostic) -> tuple[Any, ...]:
    return (
        diagnostic.source,
        diagnostic.line or 0,
        diagnostic.code,
        diagnostic.message,
    )


def _normalize_heading(value: str) -> str:
    value = re.sub(r"[`*]", "", value).strip().casefold()
    value = re.sub(r"[_\-‐‑‒–—−]+", " ", value)
    return re.sub(r"\s+", " ", value).rstrip(":")


def _excluded_issue_lines(lines: list[str]) -> frozenset[int]:
    """Identify whole prose paragraphs that explicitly disclaim requirements.

    Looking at a complete paragraph prevents the first wrapped line of reported
    speech from being promoted before a later continuation says that the idea is
    not requested.  List items remain separate units so a neighboring note does
    not suppress a real acceptance criterion.
    """

    excluded: set[int] = set()
    paragraph: list[tuple[int, str]] = []

    def flush() -> None:
        if not paragraph:
            return
        combined = " ".join(text.strip() for _, text in paragraph)
        if _UNSAFE_ISSUE_CONTEXT_RE.search(_mask_inline_code(combined)):
            excluded.update(line for line, _ in paragraph)
        paragraph.clear()

    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        fence = _FENCE_RE.match(line)
        if fence_character is not None:
            marker = fence.group(1) if fence else ""
            if marker and marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
            continue
        if fence:
            flush()
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        if not line.strip() or _HEADING_RE.match(line):
            flush()
            continue
        if _LIST_ITEM_RE.match(line):
            flush()
            paragraph.append((line_number, line))
            continue
        paragraph.append((line_number, line))
    flush()
    return frozenset(excluded)


def _mask_inline_code(value: str) -> str:
    """Mask simple inline-code spans before looking for normative language."""

    characters = list(value)
    cursor = 0
    while cursor < len(value):
        if value[cursor] != "`":
            cursor += 1
            continue
        end = cursor
        while end < len(value) and value[end] == "`":
            end += 1
        width = end - cursor
        closing = value.find("`" * width, end)
        if closing < 0:
            cursor = end
            continue
        for index in range(cursor, closing + width):
            characters[index] = " "
        cursor = closing + width
    return "".join(characters)


__all__ = [
    "CandidateConfidence",
    "CandidateReviewStatus",
    "CandidateSource",
    "CandidateSourceKind",
    "RequirementCandidate",
    "RequirementDiagnostic",
    "RequirementDraft",
    "RequirementExtractionError",
    "draft_requirements",
]
