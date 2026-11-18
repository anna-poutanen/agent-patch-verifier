"""Input schema for reviewable, requirement-linked candidate tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CandidateCase:
    """One pytest node with one unambiguous behavioral requirement."""

    id: str
    requirement_id: str
    source_file: Path
    node: str
    rationale: str

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source_file.read_bytes()).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "requirement_id": self.requirement_id,
            "test_file": str(self.source_file),
            "node": self.node,
            "rationale": self.rationale,
        }


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


def load_evidence_plan(path: Path, *, issue_text: str) -> EvidencePlan:
    """Load and validate a JSON evidence plan.

    Candidate paths are resolved relative to the plan file. Explicit
    requirements must cite ``issue:<line>`` and quote text that occurs at that
    line in the supplied issue description.
    """

    plan_path = path.resolve()
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read evidence plan {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"evidence plan is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise PlanError("evidence plan must be a JSON object")
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise PlanError(f"evidence plan has unknown fields {sorted(unknown)}")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise PlanError("evidence plan schema_version must be 1")

    requirement_items = _object_list(raw, "requirements")
    case_items = _object_list(raw, "cases")
    requirements = tuple(_requirement(item) for item in requirement_items)
    cases = tuple(_case(item, plan_path.parent) for item in case_items)

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
        try:
            contents = full_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PlanError(
                f"cannot verify source for requirement {requirement.id!r}: {exc}"
            ) from exc
        if not _excerpt_occurs_at_line(
            contents, requirement.excerpt or "", source_line
        ):
            raise PlanError(
                f"requirement {requirement.id!r} excerpt is not present at "
                f"base source {requirement.source}"
            )


def _object_list(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise PlanError(f"{key} must be a JSON array")
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
    return requirement


def _case(raw: Mapping[str, Any], plan_root: Path) -> CandidateCase:
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
    if not source_file.is_file():
        raise PlanError(f"case {case_id!r} test file does not exist: {source_value}")
    if source_file.suffix != ".py":
        raise PlanError(f"case {case_id!r} test file must end in .py")
    _verify_single_assertion(case_id, source_file, node)
    return CandidateCase(case_id, requirement_id, source_file, node, rationale)


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


def _verify_single_assertion(case_id: str, source_file: Path, node: str) -> None:
    """Keep one candidate node attributable to one explicit assertion."""

    try:
        tree = ast.parse(
            source_file.read_text(encoding="utf-8"),
            filename=str(source_file),
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise PlanError(f"case {case_id!r} test file is not valid Python: {exc}") from exc

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
                f"{source_file.name}"
            )
        scope = matches[0]

    assert isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
    assertions = _assertions_without_nested_scopes(scope)
    if len(assertions) != 1:
        raise PlanError(
            f"case {case_id!r} must contain exactly one Python assert statement; "
            f"found {len(assertions)}"
        )


def _assertions_without_nested_scopes(
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
    return assertions


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

