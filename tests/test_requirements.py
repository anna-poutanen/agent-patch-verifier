from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

import patch_verifier.requirements as requirements_module
from patch_verifier.context import (
    ContextDiagnostic,
    ContextItem,
    ContextKind,
    ContextRevision,
    RepositoryContext,
    SelectionReason,
    SymbolContext,
)
from patch_verifier.models import RequirementKind
from patch_verifier.requirements import (
    CandidateConfidence,
    CandidateReviewStatus,
    CandidateSource,
    CandidateSourceKind,
    RequirementDiagnostic,
    RequirementExtractionError,
    draft_requirements,
)


def context_item(
    *,
    kind: ContextKind,
    path: str,
    start: int,
    end: int,
    symbol: str,
    reason: str = "test_calls_changed_symbol",
    excerpt: str = "untrusted context excerpt",
) -> ContextItem:
    return ContextItem(
        kind=kind,
        path=path,
        start_line=start,
        end_line=end,
        symbol=symbol,
        excerpt=excerpt,
        excerpt_truncated=False,
        score=90,
        rank=1,
        selection_reasons=(SelectionReason(reason, "selected for a test", 90),),
    )


def base_context(*items: ContextItem) -> RepositoryContext:
    if not items:
        return RepositoryContext(
            symbols=(),
            diagnostics=(),
            limitations=(),
            revision=ContextRevision.BASE,
        )
    symbol = SymbolContext(
        path="src/service.py",
        qualified_name="resolve",
        kind="function",
        analyzed_path="src/service.py",
        base_path="src/service.py",
        patched_path="src/service.py",
        base_occurrence=1,
        patched_occurrence=1,
        revision_match="unique_identity",
        items=items,
    )
    return RepositoryContext(
        symbols=(symbol,),
        diagnostics=(),
        limitations=(),
        revision=ContextRevision.BASE,
    )


def write(root: Path, relative: str, source: str, *, encoding: str = "utf-8") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding=encoding)


def test_extracts_conservative_issue_candidates_with_exact_provenance(
    tmp_path: Path,
) -> None:
    issue = """\
# Fix defaults

Explicit zero must return zero.

## Acceptance criteria
- Missing input returns five.
- [ ] Three remains three.

## Notes
- This might be useful someday.
`must` is an example only.
> A quoted value must not become a requirement.
```python
# The implementation must return six.
```
<!-- The hidden behavior must pass. -->
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [item.statement for item in draft.candidates] == [
        "Explicit zero must return zero.",
        "Missing input returns five.",
        "Three remains three.",
    ]
    assert all(
        item.source_kind is CandidateSourceKind.ISSUE
        and item.intent is RequirementKind.REQUESTED
        and item.review_status is CandidateReviewStatus.REQUIRES_REVIEW
        and item.confidence is CandidateConfidence.HIGH
        for item in draft.candidates
    )
    first = draft.candidates[0]
    assert first.source.reference == "issue.md:3"
    assert first.source.start_column == 0
    assert first.source.end_column == len(first.source.excerpt.encode("utf-8"))
    assert first.source.sha256 == hashlib.sha256(
        first.source.excerpt.encode("utf-8")
    ).hexdigest()
    assert all(isinstance(item, str) for item in draft.limitations)
    assert "verified requirement" in " ".join(draft.limitations)
    assert not hasattr(first, "to_requirement")

    serialized = json.loads(json.dumps(draft.to_dict()))
    assert serialized["schema_version"] == 1
    assert serialized["truncated"] is False
    assert serialized["candidates"][0]["source"]["sha256"] == first.source.sha256


def test_issue_columns_are_zero_based_utf8_byte_offsets(tmp_path: Path) -> None:
    issue = "- café must stay warm."

    candidate = draft_requirements(issue, tmp_path, base_context()).candidates[0]

    assert candidate.source.excerpt == "café must stay warm."
    assert candidate.source.start_column == len("- ".encode("utf-8"))
    assert candidate.source.end_column == len(issue.encode("utf-8"))


def test_text_and_utf8_bytes_produce_identical_drafts(tmp_path: Path) -> None:
    issue = "The café value must remain stable."

    from_text = draft_requirements(issue, tmp_path, base_context())
    from_bytes = draft_requirements(issue.encode("utf-8"), tmp_path, base_context())

    assert from_text.to_dict() == from_bytes.to_dict()


def test_invalid_utf8_issue_bytes_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RequirementExtractionError, match="valid UTF-8"):
        draft_requirements(b"value must work\xff", tmp_path, base_context())


def test_issue_questions_inline_code_and_non_requirements_are_skipped(
    tmp_path: Path,
) -> None:
    issue = """\
Should this return zero?
The word `must` is documentation.
This is not a requirement and should stay out.
- The value could change.
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert draft.candidates == ()


def test_reported_speculation_and_later_paragraph_disclaimer_are_skipped(
    tmp_path: Path,
) -> None:
    issue = """\
Customer quote: "The parser must be cursed; maybe invalid input should
silently become zero." This is diagnostic color, not a requested contract.

A developer wondered whether every falsey object should become five; that is
not requested. One implementation sketch would return zero.

- A reviewer said every object must become five;
  this speculative idea is not requested.

The actual parser must reject empty input.
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [candidate.statement for candidate in draft.candidates] == [
        "The actual parser must reject empty input."
    ]


def test_current_broken_behavior_section_is_not_promoted_as_requested(
    tmp_path: Path,
) -> None:
    issue = """\
## Current broken behavior
Users must call flush twice because of the bug.

## Expected behavior
A single call must persist the data.
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [candidate.statement for candidate in draft.candidates] == [
        "A single call must persist the data."
    ]


def test_excluded_issue_sections_do_not_override_explicit_requirement_lists(
    tmp_path: Path,
) -> None:
    issue = """\
## Actual results
Users must retry the operation today.

## Non–goals
The verifier must support JavaScript.

## Out-of-scope
The verifier must provision databases.

## Rejected alternatives
Every request must be serialized globally.

## Acceptance criteria
- Should preserve explicit zero.
- Can accept a missing value.
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [candidate.statement for candidate in draft.candidates] == [
        "Should preserve explicit zero.",
        "Can accept a missing value.",
    ]


def test_inline_html_comments_are_not_given_inexact_source_spans(
    tmp_path: Path,
) -> None:
    issue = """\
The parser <!-- internal note --> must return five.
The parser must preserve explicit zero.
"""

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [candidate.statement for candidate in draft.candidates] == [
        "The parser must preserve explicit zero."
    ]
    candidate = draft.candidates[0]
    source_line = issue.splitlines()[candidate.source.start_line - 1]
    assert source_line.encode("utf-8")[
        candidate.source.start_column : candidate.source.end_column
    ].decode("utf-8") == candidate.source.excerpt


def test_unterminated_markdown_regions_are_diagnosed_and_bounded(
    tmp_path: Path,
) -> None:
    issue = "Before must work.\n```python\nHidden must not escape.\n"

    draft = draft_requirements(issue, tmp_path, base_context())

    assert [item.statement for item in draft.candidates] == ["Before must work."]
    assert draft.truncated
    assert [(item.code, item.line) for item in draft.diagnostics] == [
        ("unterminated_markdown_fence", 2)
    ]


def test_extracts_exact_base_pytest_assertions_but_not_nested_scopes(
    tmp_path: Path,
) -> None:
    source = """\
from service import resolve

def test_contract():
    assert resolve() == 5
    if True:
        assert resolve(3) == 3, "three"

    def nested():
        assert resolve(0) == 99
"""
    write(tmp_path, "tests/test_service.py", source)
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=3,
        end=9,
        symbol="test_contract",
    )

    draft = draft_requirements("", tmp_path, base_context(item))

    assert len(draft.candidates) == 2
    assert [candidate.source.excerpt for candidate in draft.candidates] == [
        "assert resolve() == 5",
        'assert resolve(3) == 3, "three"',
    ]
    assert [candidate.source.start_line for candidate in draft.candidates] == [4, 6]
    assert all(
        candidate.source_kind is CandidateSourceKind.PYTEST_ASSERTION
        and candidate.intent is RequirementKind.PRESERVATION
        and candidate.confidence is CandidateConfidence.MEDIUM
        and candidate.review_status is CandidateReviewStatus.REQUIRES_REVIEW
        for candidate in draft.candidates
    )
    assert all(
        candidate.source.sha256
        == hashlib.sha256(candidate.source.excerpt.encode("utf-8")).hexdigest()
        for candidate in draft.candidates
    )


@pytest.mark.parametrize(
    ("source", "symbol"),
    [
        (
            "import pytest\n\n"
            "@pytest.mark.xfail(reason='known bug')\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "import pytest\n\n"
            "pytestmark = pytest.mark.skip(reason='unsupported')\n\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "import pytest\n\n"
            "@pytest.mark.skipif(False, reason='conditional')\n"
            "class TestContract:\n"
            "    def test_contract(self):\n"
            "        assert resolve(3) == 99\n",
            "TestContract.test_contract",
        ),
        (
            "import pytest\n\n"
            "def test_contract():\n"
            "    pytest.xfail('known bug')\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "from pytest import mark as marker\n\n"
            "@marker.xfail(reason='known bug')\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "from pytest import mark as marker\n\n"
            "pytestmark = marker.xfail(reason='known bug')\n\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "import pytest\n\n"
            "@pytest.mark.xfail(reason='known bug')\n"
            "class BaseContract:\n"
            "    pass\n\n"
            "class TestContract(BaseContract):\n"
            "    def test_contract(self):\n"
            "        assert resolve(3) == 99\n",
            "TestContract.test_contract",
        ),
        (
            "import pytest\n\n"
            "marker = pytest.mark\n\n"
            "@marker.xfail(reason='known bug')\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "import pytest\n\n"
            "known_bug = pytest.mark.xfail(reason='known bug')\n\n"
            "@known_bug\n"
            "def test_contract():\n"
            "    assert resolve(3) == 99\n",
            "test_contract",
        ),
        (
            "class TestContract:\n"
            "    from pytest import mark as marker\n\n"
            "    @marker.xfail(reason='known bug')\n"
            "    def test_contract(self):\n"
            "        assert resolve(3) == 99\n",
            "TestContract.test_contract",
        ),
    ],
)
def test_skipped_or_expected_failure_tests_are_not_preservation_contracts(
    tmp_path: Path,
    source: str,
    symbol: str,
) -> None:
    write(tmp_path, "tests/test_service.py", source)
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=len(source.splitlines()),
        symbol=symbol,
    )

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()
    assert [diagnostic.code for diagnostic in draft.diagnostics] == [
        "pytest_non_contract_marker"
    ]


def test_proximity_only_tests_are_not_promoted_to_contracts(tmp_path: Path) -> None:
    write(
        tmp_path,
        "tests/test_service.py",
        "def test_resolve():\n    assert 1 == 1\n",
    )
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=2,
        symbol="test_resolve",
        reason="test_name_mentions_symbol",
    )

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()


def test_context_excerpt_is_not_trusted_over_reparsed_base_source(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "tests/test_service.py",
        "def test_contract():\n    assert actual() == 5\n",
    )
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=2,
        symbol="test_contract",
        excerpt="assert forged() == 999",
    )

    candidate = draft_requirements(
        "", tmp_path, base_context(item)
    ).candidates[0]

    assert candidate.source.excerpt == "assert actual() == 5"
    assert "forged" not in candidate.statement


def test_extracts_behavioral_docstring_as_low_confidence_preservation(
    tmp_path: Path,
) -> None:
    source = '''\
def resolve(value=None):
    """Return five only when the value is missing."""
    return value
'''
    write(tmp_path, "src/service.py", source)
    item = context_item(
        kind=ContextKind.DOCSTRING,
        path="src/service.py",
        start=2,
        end=2,
        symbol="resolve",
        reason="changed_definition_docstring",
    )

    candidate = draft_requirements(
        "", tmp_path, base_context(item)
    ).candidates[0]

    assert candidate.statement == "Return five only when the value is missing."
    assert candidate.source.excerpt == (
        '"""Return five only when the value is missing."""'
    )
    assert candidate.source.start_line == 2
    assert candidate.source_kind is CandidateSourceKind.DOCSTRING
    assert candidate.intent is RequirementKind.PRESERVATION
    assert candidate.confidence is CandidateConfidence.LOW


def test_descriptive_docstring_is_not_treated_as_a_behavioral_contract(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "src/service.py",
        'def resolve():\n    """A resolution helper."""\n    return 5\n',
    )
    item = context_item(
        kind=ContextKind.DOCSTRING,
        path="src/service.py",
        start=2,
        end=2,
        symbol="resolve",
    )

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()


def test_duplicate_context_items_deduplicate_stably(tmp_path: Path) -> None:
    write(
        tmp_path,
        "tests/test_service.py",
        "def test_contract():\n    assert resolve() == 5\n",
    )
    first = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=2,
        symbol="test_contract",
    )
    second = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=2,
        symbol="test_contract",
        excerpt="different untrusted excerpt",
    )

    forward = draft_requirements("Value must work.", tmp_path, base_context(first, second))
    reverse = draft_requirements("Value must work.", tmp_path, base_context(second, first))

    assert forward.to_dict() == reverse.to_dict()
    assert len(forward.candidates) == 2
    assert len({candidate.id for candidate in forward.candidates}) == 2


def test_candidate_ids_change_with_exact_source_provenance(tmp_path: Path) -> None:
    first = draft_requirements("Value must work.", tmp_path, base_context())
    second = draft_requirements(
        "\nValue must work.", tmp_path, base_context()
    )

    assert first.candidates[0].statement == second.candidates[0].statement
    assert first.candidates[0].id != second.candidates[0].id


def test_rejects_patched_context_for_preservation_sources(tmp_path: Path) -> None:
    context = RepositoryContext(
        symbols=(),
        diagnostics=(),
        limitations=(),
        revision=ContextRevision.PATCHED,
    )

    with pytest.raises(RequirementExtractionError, match="base revision"):
        draft_requirements("Value must work.", tmp_path, context)


def test_base_context_diagnostics_and_limitations_are_exposed(
    tmp_path: Path,
) -> None:
    context = RepositoryContext(
        symbols=(),
        diagnostics=(
            ContextDiagnostic(
                code="source_bytes_limit_reached",
                path=".",
                message="remaining Python files were not indexed",
            ),
        ),
        limitations=("Static calls may be incomplete.",),
        revision=ContextRevision.BASE,
    )

    draft = draft_requirements("Value must work.", tmp_path, context)

    assert draft.truncated
    assert [diagnostic.code for diagnostic in draft.diagnostics] == [
        "context_source_bytes_limit_reached"
    ]
    assert (
        "Base context limitation: Static calls may be incomplete."
        in draft.limitations
    )


@pytest.mark.parametrize(
    ("issue", "message"),
    [
        ("bad\0text", "NUL"),
        ("\ud800", "Unicode scalar"),
    ],
)
def test_rejects_malformed_issue_text(
    tmp_path: Path, issue: str, message: str
) -> None:
    with pytest.raises(RequirementExtractionError, match=message):
        draft_requirements(issue, tmp_path, base_context())


def test_rejects_oversized_issue_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(requirements_module, "_MAX_ISSUE_BYTES", 4)

    with pytest.raises(RequirementExtractionError, match="4-byte limit"):
        draft_requirements("must work", tmp_path, base_context())


def test_unsafe_and_symlinked_context_paths_are_never_read(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def test_contract():\n    assert leaked()\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)
    unsafe = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="../outside.py",
        start=1,
        end=2,
        symbol="test_contract",
    )
    linked = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="linked.py",
        start=1,
        end=2,
        symbol="test_contract",
    )

    draft = draft_requirements("", tmp_path, base_context(unsafe, linked))

    assert draft.candidates == ()
    assert {diagnostic.code for diagnostic in draft.diagnostics} == {
        "unsafe_source_path",
        "symlink_source_skipped",
    }


def test_source_syntax_error_becomes_diagnostic(tmp_path: Path) -> None:
    write(tmp_path, "tests/test_bad.py", "def test_contract(:\n    pass\n")
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_bad.py",
        start=1,
        end=2,
        symbol="test_contract",
    )

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()
    diagnostic = draft.diagnostics[0]
    assert diagnostic.code == "syntax_error"
    assert diagnostic.source == "tests/test_bad.py"
    assert diagnostic.line == 1


def test_pep263_encoded_source_is_parsed_and_excerpt_hashes_utf8(
    tmp_path: Path,
) -> None:
    source = (
        "# -*- coding: latin-1 -*-\n"
        "def test_contract():\n"
        "    assert label() == 'café'\n"
    )
    path = tmp_path / "tests/test_encoded.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(source.encode("latin-1"))
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_encoded.py",
        start=2,
        end=3,
        symbol="test_contract",
    )

    candidate = draft_requirements(
        "", tmp_path, base_context(item)
    ).candidates[0]

    assert "café" in candidate.source.excerpt
    assert candidate.source.sha256 == hashlib.sha256(
        candidate.source.excerpt.encode("utf-8")
    ).hexdigest()


def test_oversized_relevant_source_is_diagnosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        "tests/test_service.py",
        "def test_contract():\n    assert resolve() == 5\n",
    )
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=2,
        symbol="test_contract",
    )
    monkeypatch.setattr(requirements_module, "_MAX_FILE_BYTES", 10)

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()
    assert draft.truncated
    assert draft.diagnostics[0].code == "source_file_too_large"


def test_relevant_source_token_cap_applies_before_ast_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (
        "def test_contract():\n"
        "    value = 1\n"
        "    value = 2\n"
        "    value = 3\n"
        "    assert resolve() == 5\n"
    )
    write(tmp_path, "tests/test_service.py", source)
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=5,
        symbol="test_contract",
    )
    monkeypatch.setattr(requirements_module, "_MAX_SOURCE_TOKENS", 10)

    draft = draft_requirements("", tmp_path, base_context(item))

    assert draft.candidates == ()
    assert draft.truncated
    assert [diagnostic.code for diagnostic in draft.diagnostics] == [
        "source_token_limit_reached"
    ]


def test_assertion_and_candidate_caps_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        "tests/test_service.py",
        "def test_contract():\n"
        "    assert resolve(1) == 1\n"
        "    assert resolve(2) == 2\n"
        "    assert resolve(3) == 3\n",
    )
    item = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="tests/test_service.py",
        start=1,
        end=4,
        symbol="test_contract",
    )
    monkeypatch.setattr(requirements_module, "_MAX_ASSERTIONS_PER_TEST", 2)
    monkeypatch.setattr(requirements_module, "_MAX_CANDIDATES", 2)

    draft = draft_requirements(
        "Requested value must work.", tmp_path, base_context(item)
    )

    assert [candidate.source_kind for candidate in draft.candidates] == [
        CandidateSourceKind.ISSUE,
        CandidateSourceKind.PYTEST_ASSERTION,
    ]
    assert draft.candidates[1].source.start_line == 2
    assert draft.truncated
    assert {diagnostic.code for diagnostic in draft.diagnostics} == {
        "assertion_limit_reached",
        "candidate_limit_reached",
    }


def test_diagnostics_are_bounded_with_a_stable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="missing-a.py",
        start=1,
        end=2,
        symbol="test_a",
    )
    second = context_item(
        kind=ContextKind.NEARBY_TEST,
        path="missing-b.py",
        start=1,
        end=2,
        symbol="test_b",
    )
    monkeypatch.setattr(requirements_module, "_MAX_DIAGNOSTICS", 1)

    draft = draft_requirements("", tmp_path, base_context(first, second))

    assert draft.truncated
    assert len(draft.diagnostics) == 1
    assert draft.diagnostics[0].code == "diagnostic_limit_reached"


def test_result_records_are_immutable_and_validate_text_fields(tmp_path: Path) -> None:
    draft = draft_requirements("Value must work.", tmp_path, base_context())

    with pytest.raises(FrozenInstanceError):
        draft.candidates[0].statement = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="message"):
        RequirementDiagnostic("bad", "issue.md", "")
    with pytest.raises(ValueError, match="excerpt"):
        CandidateSource("issue.md", 1, 1, 0, 0, "")


def test_missing_base_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RequirementExtractionError, match="cannot resolve"):
        draft_requirements(
            "Value must work.", tmp_path / "missing", base_context()
        )
