"""Input schema for reviewable, requirement-linked candidate tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .models import ProvenanceTier, Requirement


class PlanError(ValueError):
    """Raised when an evidence plan is incomplete or internally inconsistent."""


_NODE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLAN_KEYS = {"schema_version", "requirements", "cases"}
_REQUIREMENT_KEYS = {"id", "statement", "kind", "provenance", "source", "excerpt"}
_CASE_KEYS = {"id", "requirement_id", "test_file", "node", "rationale"}
_MAX_REQUIREMENTS = 200
_MAX_CASES = 50
_MAX_ID_LENGTH = 64
_MAX_PLAN_DOCUMENT_BYTES = 5 * 1024 * 1024
_MAX_CANDIDATE_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_CANDIDATE_BYTES = 20 * 1024 * 1024
_MAX_BASE_SOURCE_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BASE_SOURCE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class CandidateCase:
    """One pytest node with one unambiguous behavioral requirement."""

    id: str
    requirement_id: str
    source_file: Path
    node: str
    rationale: str
    source_bytes: bytes = field(repr=False)

    @property
    def source_sha256(self) -> str:
        """Hash the exact bytes parsed and later injected by the verifier."""

        return hashlib.sha256(self.source_bytes).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "test_file": str(self.source_file),
            "node": self.node,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CandidateSourceValidation:
    """Structural evidence from a validated in-memory candidate test.

    Line numbers are one-based and columns are UTF-8 byte offsets, matching
    Python's AST coordinates.  ``assertion_excerpt`` is the exact source text
    covered by those coordinates; it excludes indentation before ``assert``
    and any trailing comment or newline outside the assertion node.
    ``python_assertion_count`` covers the whole module, including assertions
    in unselected and nested scopes, so stricter callers can require that the
    selected assertion is the only assertion shipped in the source.
    """

    source_sha256: str
    assertion_start_line: int
    assertion_end_line: int
    assertion_start_column: int
    assertion_end_column: int
    assertion_excerpt: str
    assertion_sha256: str
    python_assertion_count: int


@dataclass(frozen=True)
class EvidencePlan:
    schema_version: int
    requirements: tuple[Requirement, ...]
    cases: tuple[CandidateCase, ...]

    @property
    def requirement_by_id(self) -> dict[str, Requirement]:
        return {item.id: item for item in self.requirements}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "requirements": [item.to_dict() for item in self.requirements],
            "cases": [item.to_dict() for item in self.cases],
        }


def load_evidence_plan(
    path: Path,
    *,
    issue_text: str,
    document_bytes: bytes | None = None,
) -> EvidencePlan:
    """Load and validate a JSON evidence plan.

    Candidate paths are resolved relative to the plan file. Explicit
    requirements must cite ``issue:<line>`` and quote text that occurs at that
    line in the supplied issue description.
    """

    plan_path = path.resolve()
    try:
        if document_bytes is None:
            document_bytes = _read_bounded_bytes(
                plan_path,
                max_bytes=_MAX_PLAN_DOCUMENT_BYTES,
                label=f"evidence plan {path}",
            )
        elif len(document_bytes) > _MAX_PLAN_DOCUMENT_BYTES:
            raise PlanError(
                "evidence plan exceeds the "
                f"{_MAX_PLAN_DOCUMENT_BYTES}-byte limit"
            )
        raw = json.loads(document_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"evidence plan is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PlanError("evidence plan must be a JSON object")
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise PlanError(f"evidence plan has unknown fields {sorted(unknown)}")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise PlanError("evidence plan schema_version must be 1")

    requirement_items = _object_list(
        raw, "requirements", max_items=_MAX_REQUIREMENTS
    )
    case_items = _object_list(raw, "cases", max_items=_MAX_CASES)
    requirements = tuple(_requirement(item) for item in requirement_items)
    source_cache: dict[Path, bytes] = {}
    cases = tuple(
        _case(item, plan_path.parent, source_cache) for item in case_items
    )

    _require_unique((item.id for item in requirements), "requirement")
    _require_unique((item.id for item in cases), "case")
    _require_unique_case_nodes(cases)
    known = {item.id for item in requirements}
    for case in cases:
        if case.requirement_id not in known:
            raise PlanError(
                f"case {case.id!r} references unknown requirement "
                f"{case.requirement_id!r}"
            )

    for requirement in requirements:
        if requirement.provenance.is_strong and not requirement.excerpt:
            raise PlanError(
                f"strong requirement {requirement.id!r} must include a source excerpt"
            )
        if requirement.provenance is ProvenanceTier.EXPLICIT:
            issue_line = _issue_line(requirement.source, requirement.id)
            if not _excerpt_occurs_at_line(
                issue_text, requirement.excerpt or "", issue_line
            ):
                raise PlanError(
                    f"requirement {requirement.id!r} excerpt is not present at "
                    f"{requirement.source}"
                )

    if not requirements:
        raise PlanError("evidence plan must contain at least one requirement")
    if not cases:
        raise PlanError("evidence plan must contain at least one case")
    return EvidencePlan(1, requirements, cases)


def verify_base_sources(plan: EvidencePlan, base_root: Path) -> None:
    """Verify quoted pre-existing evidence against the materialized base tree.

    Sources use ``path/to/file.py:line``.  The line suffix is retained for the
    report; the excerpt may span multiple lines and must occur verbatim in the
    base file.
    """

    resolved_root = base_root.resolve()
    source_cache: dict[Path, str] = {}
    total_source_bytes = 0
    for requirement in plan.requirements:
        if requirement.provenance is not ProvenanceTier.PREEXISTING:
            continue
        source_path, source_line = _file_line(requirement.source, requirement.id)
        relative = Path(source_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PlanError(
                f"requirement {requirement.id!r} has an unsafe base source path"
            )
        full_path = (resolved_root / relative).resolve()
        try:
            full_path.relative_to(resolved_root)
        except ValueError as exc:
            raise PlanError(
                f"requirement {requirement.id!r} base source escapes the base tree"
            ) from exc
        contents = source_cache.get(full_path)
        if contents is None:
            source_bytes = _read_bounded_bytes(
                full_path,
                max_bytes=_MAX_BASE_SOURCE_FILE_BYTES,
                label=f"base source for requirement {requirement.id!r}",
            )
            if (
                total_source_bytes + len(source_bytes)
                > _MAX_TOTAL_BASE_SOURCE_BYTES
            ):
                raise PlanError(
                    "cited base source files exceed the "
                    f"{_MAX_TOTAL_BASE_SOURCE_BYTES}-byte aggregate limit"
                )
            try:
                contents = source_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PlanError(
                    f"base source for requirement {requirement.id!r} "
                    "must be UTF-8 text"
                ) from exc
            source_cache[full_path] = contents
            total_source_bytes += len(source_bytes)
        if not _excerpt_occurs_at_line(
            contents, requirement.excerpt or "", source_line
        ):
            raise PlanError(
                f"requirement {requirement.id!r} excerpt is not present at "
                f"base source {requirement.source}"
            )


def _object_list(
    raw: Mapping[str, Any], key: str, *, max_items: int
) -> list[Mapping[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise PlanError(f"{key} must be a JSON array")
    if len(value) > max_items:
        raise PlanError(f"{key} must contain at most {max_items} entries")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PlanError(f"{key}[{index}] must be a JSON object")
        result.append(item)
    return result


def _requirement(raw: Mapping[str, Any]) -> Requirement:
    unknown = set(raw) - _REQUIREMENT_KEYS
    if unknown:
        raise PlanError(f"invalid requirement: unknown fields {sorted(unknown)}")
    required = ("id", "statement", "kind", "provenance", "source")
    for key in required:
        if not isinstance(raw.get(key), str):
            raise PlanError(f"invalid requirement: {key} must be a string")
    if raw.get("excerpt") is not None and not isinstance(raw.get("excerpt"), str):
        raise PlanError("invalid requirement: excerpt must be a string or null")
    try:
        requirement = Requirement.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"invalid requirement: {exc}") from exc
    if not _ID_RE.fullmatch(requirement.id):
        raise PlanError(f"invalid requirement id: {requirement.id!r}")
    if len(requirement.id) > _MAX_ID_LENGTH:
        raise PlanError(
            f"requirement id must contain at most {_MAX_ID_LENGTH} characters"
        )
    return requirement


def _case(
    raw: Mapping[str, Any],
    plan_root: Path,
    source_cache: dict[Path, bytes],
) -> CandidateCase:
    unknown = set(raw) - _CASE_KEYS
    if unknown:
        raise PlanError(f"invalid case: unknown fields {sorted(unknown)}")
    for key in ("id", "requirement_id", "test_file", "node"):
        if not isinstance(raw.get(key), str):
            raise PlanError(f"invalid case: {key} must be a string")
    if "rationale" in raw and not isinstance(raw.get("rationale"), str):
        raise PlanError("invalid case: rationale must be a string")
    try:
        case_id = str(raw["id"]).strip()
        requirement_id = str(raw["requirement_id"]).strip()
        source_value = str(raw["test_file"])
        node = str(raw["node"]).strip()
        rationale = str(raw.get("rationale", "")).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"invalid case: {exc}") from exc
    if not case_id:
        raise PlanError("case id must not be empty")
    if not _ID_RE.fullmatch(case_id):
        raise PlanError(f"invalid case id: {case_id!r}")
    if len(case_id) > _MAX_ID_LENGTH:
        raise PlanError(
            f"case id must contain at most {_MAX_ID_LENGTH} characters"
        )
    if not requirement_id:
        raise PlanError(f"case {case_id!r} requirement_id must not be empty")
    if not _NODE_RE.fullmatch(node):
        raise PlanError(f"case {case_id!r} has an invalid pytest node {node!r}")
    source_input = Path(source_value)
    if source_input.is_absolute():
        raise PlanError(f"case {case_id!r} test file must be a relative path")
    source_file = (plan_root / source_input).resolve()
    try:
        source_file.relative_to(plan_root.resolve())
    except ValueError as exc:
        raise PlanError(
            f"case {case_id!r} test file must be inside the evidence-plan directory"
        ) from exc
    if source_file.suffix != ".py":
        raise PlanError(f"case {case_id!r} test file must end in .py")
    source_bytes = source_cache.get(source_file)
    if source_bytes is None:
        source_bytes = _read_bounded_bytes(
            source_file,
            max_bytes=_MAX_CANDIDATE_FILE_BYTES,
            label=f"case {case_id!r} test file {source_value!r}",
        )
        total_candidate_bytes = sum(
            len(contents) for contents in source_cache.values()
        ) + len(source_bytes)
        if total_candidate_bytes > _MAX_TOTAL_CANDIDATE_BYTES:
            raise PlanError(
                "candidate test files exceed the "
                f"{_MAX_TOTAL_CANDIDATE_BYTES}-byte aggregate limit"
            )
        source_cache[source_file] = source_bytes
    validate_candidate_test_source(
        source_bytes,
        case_id=case_id,
        node=node,
        source_name=str(source_file),
    )
    return CandidateCase(
        case_id,
        requirement_id,
        source_file,
        node,
        rationale,
        source_bytes,
    )


def _read_bounded_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Read one local artifact without allocating beyond its byte budget."""

    try:
        with path.open("rb") as stream:
            contents = stream.read(max_bytes + 1)
    except OSError as exc:
        raise PlanError(f"cannot read {label}: {exc}") from exc
    if len(contents) > max_bytes:
        raise PlanError(f"{label} exceeds the {max_bytes}-byte limit")
    return contents


def _require_unique(values: object, label: str) -> None:
    seen: set[str] = set()
    for value in values:  # type: ignore[union-attr]
        if value in seen:
            raise PlanError(f"duplicate {label} id: {value!r}")
        seen.add(value)


def _require_unique_case_nodes(cases: tuple[CandidateCase, ...]) -> None:
    seen: dict[tuple[Path, str], str] = {}
    for case in cases:
        key = (case.source_file, case.node)
        previous = seen.get(key)
        if previous is not None:
            raise PlanError(
                f"cases {previous!r} and {case.id!r} map the same pytest node; "
                "one node may support only one requirement"
            )
        seen[key] = case.id


def validate_candidate_test_source(
    source_bytes: bytes,
    *,
    case_id: str,
    node: str,
    source_name: str = "<inline candidate>",
) -> CandidateSourceValidation:
    """Validate one candidate test from frozen bytes without executing it.

    The helper performs no filesystem access, imports, pytest collection, or
    target-code execution.  It applies the same structural contract used by
    :func:`load_evidence_plan`: the named function must occur uniquely and
    contain exactly one Python ``assert`` outside nested scopes.

    ``source_bytes`` must be the exact bytes a caller will later persist or
    execute.  The fixed candidate-file limit is enforced here as well as at
    evidence-plan file-read boundaries so an in-memory caller cannot bypass
    the parser's resource budget.
    """

    if not isinstance(source_bytes, bytes):
        raise PlanError("candidate test source must be supplied as exact bytes")
    if len(source_bytes) > _MAX_CANDIDATE_FILE_BYTES:
        raise PlanError(
            "candidate test source exceeds the "
            f"{_MAX_CANDIDATE_FILE_BYTES}-byte limit"
        )
    if not isinstance(case_id, str) or not case_id.strip():
        raise PlanError("candidate test case_id must be a non-empty string")
    if not isinstance(node, str) or not _NODE_RE.fullmatch(node):
        raise PlanError(f"case {case_id!r} has an invalid pytest node {node!r}")
    if not isinstance(source_name, str) or not source_name:
        raise PlanError("candidate test source_name must be a non-empty string")

    try:
        source = source_bytes.decode("utf-8")
        tree = ast.parse(source, filename=source_name)
    except (MemoryError, RecursionError, UnicodeError, SyntaxError, ValueError) as exc:
        detail = str(exc) or type(exc).__name__
        raise PlanError(
            f"case {case_id!r} test source is not safely parseable Python: "
            f"{detail}"
        ) from exc

    scope: ast.AST = tree
    parts = node.split("::")
    for index, part in enumerate(parts):
        allowed = (
            (ast.FunctionDef, ast.AsyncFunctionDef)
            if index == len(parts) - 1
            else (ast.ClassDef,)
        )
        body = getattr(scope, "body", ())
        matches = [
            item
            for item in body
            if isinstance(item, allowed) and getattr(item, "name", None) == part
        ]
        if len(matches) != 1:
            raise PlanError(
                f"case {case_id!r} node {node!r} was not found uniquely in "
                f"{source_name}"
            )
        scope = matches[0]

    assert isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
    assertions = _assertions_without_nested_scopes(scope)
    if len(assertions) != 1:
        raise PlanError(
            f"case {case_id!r} must contain exactly one Python assert statement; "
            f"found {len(assertions)}"
        )
    assertion = assertions[0]
    assertion_bytes = _ast_source_bytes(source_bytes, assertion)
    try:
        assertion_excerpt = assertion_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - AST uses UTF-8 boundaries
        raise PlanError(
            f"case {case_id!r} assertion span is not valid UTF-8"
        ) from exc
    assert assertion.end_lineno is not None
    assert assertion.end_col_offset is not None
    return CandidateSourceValidation(
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        assertion_start_line=assertion.lineno,
        assertion_end_line=assertion.end_lineno,
        assertion_start_column=assertion.col_offset,
        assertion_end_column=assertion.end_col_offset,
        assertion_excerpt=assertion_excerpt,
        assertion_sha256=hashlib.sha256(assertion_bytes).hexdigest(),
        python_assertion_count=_python_assertion_count(tree),
    )


def _assertions_without_nested_scopes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Assert]:
    assertions: list[ast.Assert] = []
    stack: list[ast.AST] = list(reversed(function.body))
    nested_scopes = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
    )
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Assert):
            assertions.append(current)
        if isinstance(current, nested_scopes):
            continue
        stack.extend(reversed(tuple(ast.iter_child_nodes(current))))
    return assertions


def _python_assertion_count(tree: ast.AST) -> int:
    """Count every Python assertion in a module without recursive walking."""

    count = 0
    stack = [tree]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Assert):
            count += 1
        stack.extend(ast.iter_child_nodes(current))
    return count


def _ast_source_bytes(source_bytes: bytes, node: ast.AST) -> bytes:
    """Slice exact UTF-8 bytes for a node carrying complete AST positions."""

    start_line = getattr(node, "lineno", None)
    end_line = getattr(node, "end_lineno", None)
    start_column = getattr(node, "col_offset", None)
    end_column = getattr(node, "end_col_offset", None)
    if not all(
        isinstance(value, int)
        for value in (start_line, end_line, start_column, end_column)
    ):
        raise PlanError("candidate assertion is missing complete source coordinates")
    assert isinstance(start_line, int)
    assert isinstance(end_line, int)
    assert isinstance(start_column, int)
    assert isinstance(end_column, int)

    if not 1 <= start_line <= end_line:
        raise PlanError("candidate assertion has invalid source coordinates")
    bounds = _selected_line_bounds(
        source_bytes,
        first_line=start_line,
        last_line=end_line,
    )
    if bounds is None:
        raise PlanError("candidate assertion has invalid source coordinates")
    first_start, first_end, last_start, last_end = bounds
    start = first_start + start_column
    end = last_start + end_column
    if not (first_start <= start <= first_end and last_start <= end <= last_end):
        raise PlanError("candidate assertion has invalid source coordinates")
    if start > end:
        raise PlanError("candidate assertion has invalid source coordinates")
    return source_bytes[start:end]


def _selected_line_bounds(
    source_bytes: bytes,
    *,
    first_line: int,
    last_line: int,
) -> tuple[int, int, int, int] | None:
    """Find two logical line bounds without allocating per-line objects.

    Python accepts LF, CRLF, and CR newlines.  A candidate may contain millions
    of blank lines within the byte limit, so ``splitlines`` and a complete
    offset table would turn a bounded source into unbounded object overhead.
    """

    first_bounds: tuple[int, int] | None = None
    last_bounds: tuple[int, int] | None = None
    line_number = 1
    line_start = 0
    index = 0
    source_length = len(source_bytes)
    while index < source_length:
        byte = source_bytes[index]
        if byte not in (10, 13):
            index += 1
            continue
        if line_number == first_line:
            first_bounds = (line_start, index)
        if line_number == last_line:
            last_bounds = (line_start, index)
        if first_bounds is not None and last_bounds is not None:
            break
        if byte == 13 and index + 1 < source_length and source_bytes[index + 1] == 10:
            index += 1
        index += 1
        line_number += 1
        line_start = index

    if first_bounds is None and line_number == first_line:
        first_bounds = (line_start, source_length)
    if last_bounds is None and line_number == last_line:
        last_bounds = (line_start, source_length)
    if first_bounds is None or last_bounds is None:
        return None
    return (*first_bounds, *last_bounds)


def _issue_line(source: str, requirement_id: str) -> int:
    match = re.fullmatch(r"issue(?:\.md)?:([1-9][0-9]*)", source, re.IGNORECASE)
    if match is None:
        raise PlanError(
            f"explicit requirement {requirement_id!r} must cite issue:<line>"
        )
    return int(match.group(1))


def _file_line(source: str, requirement_id: str) -> tuple[str, int]:
    path, separator, line_text = source.rpartition(":")
    if not separator or not path or not line_text.isdigit() or int(line_text) < 1:
        raise PlanError(
            f"preexisting requirement {requirement_id!r} must cite path:<line>"
        )
    return path, int(line_text)


def _excerpt_occurs_at_line(contents: str, excerpt: str, line: int) -> bool:
    lines = contents.splitlines()
    excerpt_lines = excerpt.splitlines() or [excerpt]
    if line > len(lines):
        return False
    selected = "\n".join(lines[line - 1 : line - 1 + len(excerpt_lines)])
    return excerpt in selected

