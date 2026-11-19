"""Adversarial benchmarks for review-only requirement extraction.

The provisional milestone API intentionally returns drafts, not accepted
requirements. These tests treat exact provenance as the safety boundary: every
candidate must point to immutable source text, and plausible but unsupported
assertions must either be omitted or diagnosed.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from patch_verifier.change_analysis import ChangedSymbol, SourceSpan
from patch_verifier.context import (
    ContextItem,
    ContextKind,
    ContextRevision,
    RepositoryContext,
    SelectionReason,
    SymbolContext,
    collect_repository_context,
)
from patch_verifier.requirements import RequirementExtractionError, draft_requirements


FIXTURES = Path(__file__).parent / "fixtures" / "requirements"
BENCHMARK_CASES = (
    "mixed_contracts",
    "misleading_issue",
    "parametrized",
    "shadowing",
    "dynamic_assertions",
)


def _case_root(name: str) -> Path:
    return FIXTURES / name


def _metadata(root: Path) -> dict[str, str]:
    return json.loads((root / "case.json").read_text(encoding="utf-8"))


def _changed_symbol(root: Path) -> ChangedSymbol:
    metadata = _metadata(root)
    relative = metadata["module"]
    qualified_name = metadata["symbol"]
    source = (root / relative).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == qualified_name
    ]
    assert len(matches) == 1, (root, qualified_name)
    node = matches[0]
    kind = (
        "class"
        if isinstance(node, ast.ClassDef)
        else "async_function"
        if isinstance(node, ast.AsyncFunctionDef)
        else "function"
    )
    span = SourceSpan(node.lineno, node.end_lineno or node.lineno)
    return ChangedSymbol(
        path=relative,
        qualified_name=qualified_name,
        kind=kind,
        base_span=span,
        patched_span=span,
        removed_lines=(node.lineno,),
        added_lines=(node.lineno,),
        base_path=relative,
        patched_path=relative,
        base_occurrence=1,
        patched_occurrence=1,
        revision_match="unique_identity",
    )


def _base_context(root: Path) -> RepositoryContext:
    return collect_repository_context(
        root,
        (_changed_symbol(root),),
        revision=ContextRevision.BASE,
    )


def _draft(root: Path, *, issue_as_bytes: bool = False):
    issue_bytes = (root / "issue.md").read_bytes()
    issue: str | bytes = issue_bytes if issue_as_bytes else issue_bytes.decode("utf-8")
    return draft_requirements(
        issue,
        root,
        _base_context(root),
        issue_source="issue.md",
    )


def _candidate_payloads(draft) -> list[dict[str, object]]:
    payload = draft.to_dict()
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    return candidates


def _slice_span(text: str, source: dict[str, object]) -> str:
    lines = text.splitlines(keepends=True)
    start_line = int(source["start_line"])
    end_line = int(source["end_line"])
    start_column = int(source["start_column"])
    end_column = int(source["end_column"])
    assert 1 <= start_line <= end_line <= len(lines)
    if start_line == end_line:
        return lines[start_line - 1][start_column:end_column]
    return (
        lines[start_line - 1][start_column:]
        + "".join(lines[start_line:end_line - 1])
        + lines[end_line - 1][:end_column]
    )


def _source_document(
    root: Path, issue_text: str, source: dict[str, object]
) -> str:
    path = str(source["path"])
    if path == "issue.md":
        return issue_text
    candidate = Path(path)
    assert not candidate.is_absolute()
    assert ".." not in candidate.parts
    return (root / candidate).read_text(encoding="utf-8")


def _diagnostic_text(draft) -> str:
    return json.dumps(draft.to_dict()["diagnostics"], sort_keys=True).casefold()


@pytest.mark.parametrize("case_name", BENCHMARK_CASES)
def test_drafts_are_deterministic_review_only_and_exactly_sourced(
    case_name: str,
) -> None:
    root = _case_root(case_name)
    issue_text = (root / "issue.md").read_text(encoding="utf-8")

    first = _draft(root)
    second = _draft(root)
    assert first.to_dict() == second.to_dict()
    payload = first.to_dict()
    assert payload["schema_version"] == 1
    assert len(payload["candidates"]) <= 500
    assert len(payload["diagnostics"]) <= 200

    ids: set[str] = set()
    for candidate in _candidate_payloads(first):
        assert candidate["review_status"] == "requires_review"
        assert candidate["intent"] in {"requested", "preservation"}
        assert candidate["source_kind"] in {
            "issue",
            "pytest_assertion",
            "docstring",
        }
        assert candidate["confidence"] in {"high", "medium", "low"}
        assert isinstance(candidate["rationale"], str) and candidate["rationale"]
        assert isinstance(candidate["statement"], str) and candidate["statement"]
        assert len(candidate["statement"]) <= 4_000

        candidate_id = str(candidate["id"])
        assert candidate_id and candidate_id not in ids
        ids.add(candidate_id)

        source = candidate["source"]
        assert isinstance(source, dict)
        assert set(source) == {
            "path",
            "start_line",
            "end_line",
            "start_column",
            "end_column",
            "excerpt",
            "sha256",
        }
        excerpt = str(source["excerpt"])
        assert excerpt
        assert len(excerpt.encode("utf-8")) <= 16_000
        document = _source_document(root, issue_text, source)
        assert _slice_span(document, source) == excerpt
        assert hashlib.sha256(excerpt.encode("utf-8")).hexdigest() == source["sha256"]


def test_explicit_request_and_preservation_assertions_keep_distinct_provenance() -> None:
    candidates = _candidate_payloads(_draft(_case_root("mixed_contracts")))

    requested = [
        item
        for item in candidates
        if item["source_kind"] == "issue" and item["intent"] == "requested"
    ]
    assert any(
        "An explicit zero must return zero." in item["source"]["excerpt"]
        for item in requested
    )

    preserved_excerpts = {
        item["source"]["excerpt"].strip()
        for item in candidates
        if item["source_kind"] == "pytest_assertion"
        and item["intent"] == "preservation"
    }
    assert "assert resolve() == 5" in preserved_excerpts
    assert "assert resolve(3) == 3" in preserved_excerpts

    serialized = json.dumps(candidates, sort_keys=True)
    assert "tests/test_unrelated.py" not in serialized
    assert "every falsey object should become five" not in serialized
    assert "return 0" not in serialized


def test_misleading_issue_prose_is_not_promoted_to_a_requested_requirement() -> None:
    root = _case_root("misleading_issue")
    from_text = _draft(root)
    from_bytes = _draft(root, issue_as_bytes=True)
    assert from_text.to_dict() == from_bytes.to_dict()

    issue_candidates = [
        item
        for item in _candidate_payloads(from_text)
        if item["source_kind"] == "issue"
    ]
    assert len(issue_candidates) == 1
    assert issue_candidates[0]["intent"] == "requested"
    assert "Empty input must raise `ValueError`." in issue_candidates[0]["source"][
        "excerpt"
    ]

    serialized = json.dumps(issue_candidates, sort_keys=True)
    assert "parser must be cursed" not in serialized
    assert "silently become zero" not in serialized
    assert "whitespace-only" not in serialized
    assert "catch every exception" not in serialized


def test_parametrized_assertion_stays_one_reviewable_symbolic_contract() -> None:
    candidates = _candidate_payloads(_draft(_case_root("parametrized")))
    assertions = [
        item for item in candidates if item["source_kind"] == "pytest_assertion"
    ]

    assert len(assertions) == 1
    assert assertions[0]["intent"] == "preservation"
    assert assertions[0]["review_status"] == "requires_review"
    assert assertions[0]["source"]["excerpt"].strip() == (
        "assert resolve(value) == expected"
    )
    # Literal parameter rows are context for a reviewer, not three independently
    # sourced assertions at the shared assert location.
    assert "[(None, 5), (0, 0), (3, 3)]" not in json.dumps(assertions)


def test_shadowed_and_overwritten_assertions_are_not_preservation_evidence() -> None:
    candidates = _candidate_payloads(_draft(_case_root("shadowing")))
    assertion_candidates = [
        item for item in candidates if item["source_kind"] == "pytest_assertion"
    ]
    excerpts = {item["source"]["excerpt"].strip() for item in assertion_candidates}

    assert "assert quote(5) == 60" in excerpts
    # Duplicate module-level names are statically ambiguous. Excluding both is
    # safer than guessing which definition pytest will ultimately collect.
    assert "assert quote(2) == 20" not in excerpts
    assert "assert quote(2) == 24" not in excerpts
    assert "assert quote(2) == -1" not in excerpts


def test_dynamic_assertions_are_diagnosed_without_becoming_candidates() -> None:
    draft = _draft(_case_root("dynamic_assertions"))
    assertion_candidates = [
        item
        for item in _candidate_payloads(draft)
        if item["source_kind"] == "pytest_assertion"
    ]
    excerpts = {item["source"]["excerpt"].strip() for item in assertion_candidates}

    assert excerpts == {'assert normalize(None) == "missing"'}
    serialized = json.dumps(assertion_candidates, sort_keys=True)
    assert "operation(\" READY \")" not in serialized
    assert "_assert_case" not in serialized


def _synthetic_context(items: list[ContextItem]) -> RepositoryContext:
    return RepositoryContext(
        symbols=(
            SymbolContext(
                path="bounded.py",
                qualified_name="bounded_value",
                kind="function",
                analyzed_path="bounded.py",
                base_path="bounded.py",
                patched_path="bounded.py",
                base_occurrence=1,
                patched_occurrence=1,
                revision_match="unique_identity",
                items=tuple(items),
            ),
        ),
        diagnostics=(),
        limitations=(),
        revision=ContextRevision.BASE,
    )


def _nearby_test_item(path: str, source: str) -> ContextItem:
    return ContextItem(
        kind=ContextKind.NEARBY_TEST,
        path=path,
        start_line=1,
        end_line=len(source.splitlines()),
        symbol=Path(path).stem,
        excerpt=source,
        excerpt_truncated=False,
        score=90,
        rank=1,
        selection_reasons=(
            SelectionReason(
                code="test_calls_changed_symbol",
                detail="resource-bound requirement benchmark",
                weight=90,
            ),
        ),
    )


def _copy_resource_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "resource"
    shutil.copytree(_case_root("resource_bounds"), target)
    return target


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        (b"x" * 1_000_001, "1000000|1,000,000|issue"),
        (b"x\n" * 20_001, "20000|20,000|line"),
        (b"\xff", "utf-8|UTF-8"),
    ],
    ids=("byte_limit", "line_limit", "invalid_utf8"),
)
def test_issue_resource_limits_fail_before_repository_work(
    tmp_path: Path, issue: bytes, message: str
) -> None:
    root = _copy_resource_fixture(tmp_path)
    with pytest.raises(RequirementExtractionError, match=message):
        draft_requirements(
            issue,
            root,
            _synthetic_context([]),
            issue_source="issue.md",
        )


def test_oversized_relevant_source_is_skipped_with_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    root = _copy_resource_fixture(tmp_path)
    relative = "tests/test_oversized.py"
    source = (
        "from bounded import bounded_value\n\n"
        "def test_oversized():\n"
        "    assert bounded_value(1) == 1\n"
    )
    oversized = source.encode("utf-8") + b"#" * (2_000_001 - len(source))
    (root / relative).write_bytes(oversized)
    context = _synthetic_context([_nearby_test_item(relative, source)])

    draft = draft_requirements("No new behavior is requested.", root, context)

    assert draft.truncated is True
    assert not any(
        item["source"]["path"] == relative for item in _candidate_payloads(draft)
    )
    diagnostic_text = _diagnostic_text(draft)
    assert "2000000" in diagnostic_text or "2,000,000" in diagnostic_text


def test_aggregate_source_budget_truncates_deterministically(tmp_path: Path) -> None:
    root = _copy_resource_fixture(tmp_path)
    items: list[ContextItem] = []
    target_bytes = 1_900_000
    for index in range(11):
        relative = f"tests/test_aggregate_{index:02d}.py"
        source = (
            "from bounded import bounded_value\n\n"
            f"def test_contract_{index:02d}():\n"
            f"    assert bounded_value({index}) == {index}\n"
        )
        padding = "#" + "x" * (target_bytes - len(source.encode("utf-8")) - 2) + "\n"
        (root / relative).write_text(source + padding, encoding="utf-8")
        items.append(_nearby_test_item(relative, source))
    context = _synthetic_context(items)

    first = draft_requirements("No new behavior is requested.", root, context)
    second = draft_requirements("No new behavior is requested.", root, context)

    assert first.to_dict() == second.to_dict()
    assert first.truncated is True
    assertion_paths = {
        item["source"]["path"]
        for item in _candidate_payloads(first)
        if item["source_kind"] == "pytest_assertion"
    }
    assert len(assertion_paths) <= 10
    diagnostic_text = _diagnostic_text(first)
    assert "20000000" in diagnostic_text or "20,000,000" in diagnostic_text


def test_relevant_file_and_candidate_limits_choose_a_stable_prefix(
    tmp_path: Path,
) -> None:
    root = _copy_resource_fixture(tmp_path)
    items: list[ContextItem] = []
    for file_index in range(201):
        relative = f"tests/test_contract_{file_index:03d}.py"
        assertions = "".join(
            f"    assert bounded_value({file_index * 50 + offset}) == "
            f"{file_index * 50 + offset}\n"
            for offset in range(50)
        )
        source = (
            "from bounded import bounded_value\n\n"
            f"def test_contract_{file_index:03d}():\n"
            + assertions
        )
        (root / relative).write_text(source, encoding="utf-8")
        items.append(_nearby_test_item(relative, source))
    context = _synthetic_context(items)

    first = draft_requirements("No new behavior is requested.", root, context)
    second = draft_requirements("No new behavior is requested.", root, context)

    assert first.to_dict() == second.to_dict()
    assert first.truncated is True
    candidates = _candidate_payloads(first)
    assert len(candidates) <= 500
    relevant_paths = {
        item["source"]["path"]
        for item in candidates
        if item["source_kind"] == "pytest_assertion"
    }
    assert len(relevant_paths) <= 200
    diagnostic_text = _diagnostic_text(first)
    assert "500" in diagnostic_text and "candidate" in diagnostic_text
    assert "200" in diagnostic_text and "file" in diagnostic_text


def test_diagnostic_limit_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = _copy_resource_fixture(tmp_path)
    relative = "tests/test_diagnostic_cap.py"
    source = "def test_seed():\n    assert True\n"
    (root / relative).write_text(source, encoding="utf-8")
    items = [
        ContextItem(
            kind=ContextKind.NEARBY_TEST,
            path=relative,
            start_line=index + 1,
            end_line=index + 1,
            symbol=f"missing_scope_{index:03d}",
            excerpt="assert True",
            excerpt_truncated=False,
            score=90,
            rank=1,
            selection_reasons=(
                SelectionReason(
                    code="test_calls_changed_symbol",
                    detail="resource-bound requirement benchmark",
                    weight=90,
                ),
            ),
        )
        for index in range(250)
    ]
    context = _synthetic_context(items)

    first = draft_requirements("No new behavior is requested.", root, context)
    second = draft_requirements("No new behavior is requested.", root, context)

    assert first.to_dict() == second.to_dict()
    assert first.truncated is True
    diagnostics = first.to_dict()["diagnostics"]
    assert len(diagnostics) == 200
    assert any(
        "limit" in json.dumps(item, sort_keys=True).casefold()
        for item in diagnostics
    )
