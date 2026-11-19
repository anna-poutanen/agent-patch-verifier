from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import patch_verifier.drafting as drafting_module
from patch_verifier.draft_validation import validate_requirement_draft_artifact
from patch_verifier.drafting import (
    DraftingError,
    DraftRequirementsRequest,
    draft_patch_requirements,
)


BASE_SOURCE = '''\
def resolve(value=None):
    """Return five when the value is missing; otherwise return it unchanged."""
    return value or 5
'''

PATCH = '''\
diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1,3 +1,3 @@
 def resolve(value=None):
     """Return five when the value is missing; otherwise return it unchanged."""
-    return value or 5
+    return 0
'''


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "value.py").write_text(BASE_SOURCE, encoding="utf-8")
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_value.py").write_text(
        "from value import resolve\n\n"
        "def test_preserves_nonzero_value():\n"
        "    assert resolve(3) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=APV Tests",
            "-c",
            "user.email=apv@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repository,
        check=True,
    )
    return repository


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = _repository(tmp_path)
    patch_file = tmp_path / "proposed.diff"
    issue_file = tmp_path / "issue.md"
    patch_file.write_text(PATCH, encoding="utf-8")
    issue_file.write_text(
        "# Default handling\n\n- Explicit zero must be returned unchanged.\n",
        encoding="utf-8",
    )
    return repository, patch_file, issue_file


def _artifact(tmp_path: Path) -> dict[str, Any]:
    repository, patch_file, issue_file = _inputs(tmp_path)
    output = tmp_path / "draft.json"
    draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=output,
        )
    )
    return json.loads(output.read_text(encoding="utf-8"))


def _recompute_candidate_id(candidate: dict[str, Any]) -> None:
    source = candidate["source"]
    source["sha256"] = hashlib.sha256(source["excerpt"].encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {
            "intent": candidate["intent"],
            "source": source,
            "source_kind": candidate["source_kind"],
            "statement": candidate["statement"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate["id"] = "cand-" + hashlib.sha256(canonical).hexdigest()


def test_draft_patch_requirements_writes_deterministic_provenance_envelope(
    tmp_path: Path,
) -> None:
    repository, patch_file, issue_file = _inputs(tmp_path)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first = draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=first_path,
        )
    )
    second = draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=second_path,
        )
    )

    assert first.candidate_count >= 2
    assert second.candidate_count == first.candidate_count
    assert first_path.read_bytes() == second_path.read_bytes()
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    validate_requirement_draft_artifact(payload)
    assert payload["inputs"]["repository"] == str(repository.resolve())
    assert payload["environment"] == {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": sys.platform,
    }
    assert payload["inputs"]["base_revision"] == "HEAD"
    assert payload["inputs"]["patch_sha256"] == hashlib.sha256(
        PATCH.encode("utf-8")
    ).hexdigest()
    assert payload["inputs"]["issue_source"] == "issue.md"
    assert payload["changed_symbols"][0]["qualified_name"] == "resolve"
    assert payload["draft"]["schema_version"] == 1
    assert all(
        candidate["review_status"] == "requires_review"
        for candidate in payload["draft"]["candidates"]
    )
    for candidate in payload["draft"]["candidates"]:
        assert candidate["source"]["sha256"] == hashlib.sha256(
            candidate["source"]["excerpt"].encode("utf-8")
        ).hexdigest()


def test_draft_patch_requirements_refuses_to_overwrite_output(
    tmp_path: Path,
) -> None:
    repository, patch_file, issue_file = _inputs(tmp_path)
    output = tmp_path / "draft.json"
    output.write_text("keep me", encoding="utf-8")

    with pytest.raises(DraftingError, match="already exists"):
        draft_patch_requirements(
            DraftRequirementsRequest(
                repository=repository,
                base_revision="HEAD",
                patch_file=patch_file,
                issue_file=issue_file,
                output_file=output,
            )
        )

    assert output.read_text(encoding="utf-8") == "keep me"


def test_artifact_validator_rejects_cross_field_provenance_drift(
    tmp_path: Path,
) -> None:
    repository, patch_file, issue_file = _inputs(tmp_path)
    output = tmp_path / "draft.json"
    draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=output,
        )
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    issue_index = next(
        index
        for index, candidate in enumerate(payload["draft"]["candidates"])
        if candidate["source_kind"] == "issue"
    )

    wrong_digest = copy.deepcopy(payload)
    wrong_digest["draft"]["candidates"][issue_index]["source"]["sha256"] = (
        "0" * 64
    )
    with pytest.raises(ValueError, match="digest does not match"):
        validate_requirement_draft_artifact(wrong_digest)

    wrong_id = copy.deepcopy(payload)
    wrong_id["draft"]["candidates"][issue_index]["id"] = "cand-" + "0" * 64
    with pytest.raises(ValueError, match="canonical candidate provenance"):
        validate_requirement_draft_artifact(wrong_id)

    wrong_intent = copy.deepcopy(payload)
    wrong_intent["draft"]["candidates"][issue_index]["intent"] = "preservation"
    with pytest.raises(ValueError, match="expected constant 'requested'"):
        validate_requirement_draft_artifact(wrong_intent)

    wrong_span = copy.deepcopy(payload)
    wrong_span["draft"]["candidates"][issue_index]["source"]["end_column"] += 1
    with pytest.raises(ValueError, match="do not delimit"):
        validate_requirement_draft_artifact(wrong_span)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda symbol: symbol.update({"removed_lines": [3, 2]}),
            "strictly increasing",
        ),
        (
            lambda symbol: symbol.update({"removed_lines": [999_999]}),
            "outside base_span",
        ),
        (
            lambda symbol: symbol.update({"base_span": None}),
            "base_span and base_occurrence",
        ),
        (
            lambda symbol: symbol.update({"base_path": None}),
            "base_path is required",
        ),
        (
            lambda symbol: symbol.update({"revision_match": "base_only"}),
            "base_only requires only a base symbol span",
        ),
        (
            lambda symbol: symbol.update(
                {"revision_match": "path_change_unique_identity"}
            ),
            "path-change-only symbols cannot contain changed line",
        ),
        (
            lambda symbol: symbol.update(
                {"removed_lines": [], "added_lines": []}
            ),
            "non-path-change symbols require at least one changed line",
        ),
        (
            lambda symbol: symbol.update({"path": "not-the-patched-path.py"}),
            "path must equal patched_path",
        ),
    ],
    ids=(
        "unsorted-lines",
        "line-outside-span",
        "occurrence-without-span",
        "span-without-path",
        "revision-side-mismatch",
        "path-change-with-lines",
        "non-path-change-without-lines",
        "canonical-path-mismatch",
    ),
)
def test_artifact_validator_rejects_impossible_changed_symbol_provenance(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    payload = _artifact(tmp_path)
    mutation(payload["changed_symbols"][0])

    with pytest.raises(ValueError, match=message):
        validate_requirement_draft_artifact(payload)


@pytest.mark.parametrize(
    ("edited", "expected_revision_match"),
    [
        (False, "path_change_ambiguous_duplicate_unpaired"),
        (True, "ambiguous_duplicate_unpaired"),
    ],
    ids=("pure-rename", "edited-rename"),
)
def test_validator_accepts_generated_ambiguous_symbol_from_path_change(
    tmp_path: Path,
    edited: bool,
    expected_revision_match: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "old.py").write_text(
        "if enabled:\n"
        "    def duplicate():\n"
        "        return 1\n"
        "else:\n"
        "    def duplicate():\n"
        "        return 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=APV Tests",
            "-c",
            "user.email=apv@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "mv", "old.py", "new.py"],
        cwd=repository,
        check=True,
    )
    if edited:
        renamed = repository / "new.py"
        renamed.write_text(
            renamed.read_text(encoding="utf-8").replace("return 1", "return 10"),
            encoding="utf-8",
        )
    patch_file = tmp_path / "rename.diff"
    patch_file.write_bytes(
        subprocess.run(
            ["git", "diff", "--binary", "--find-renames", "HEAD"],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    )
    issue_file = tmp_path / "issue.md"
    issue_file.write_text("The module must remain importable.\n", encoding="utf-8")
    output = tmp_path / "draft.json"

    draft_patch_requirements(
        DraftRequirementsRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch_file,
            issue_file=issue_file,
            output_file=output,
        )
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    base_records = [
        symbol
        for symbol in payload["changed_symbols"]
        if symbol["revision_match"] == expected_revision_match
        and symbol["base_span"] is not None
    ]
    assert base_records
    assert all(symbol["path"] == "new.py" for symbol in base_records)
    assert all(symbol["base_path"] == "old.py" for symbol in base_records)
    assert all(symbol["patched_path"] is None for symbol in base_records)
    validate_requirement_draft_artifact(payload)


@pytest.mark.parametrize(
    "source_kind",
    ["issue", "pytest_assertion", "docstring"],
)
def test_artifact_validator_recomputes_statement_from_exact_excerpt(
    tmp_path: Path,
    source_kind: str,
) -> None:
    payload = _artifact(tmp_path)
    candidate = next(
        item
        for item in payload["draft"]["candidates"]
        if item["source_kind"] == source_kind
    )
    candidate["statement"] = "Forged statement with a self-consistent ID."
    _recompute_candidate_id(candidate)

    with pytest.raises(ValueError, match="deterministic transform"):
        validate_requirement_draft_artifact(payload)


def test_docstring_candidate_must_belong_to_base_changed_symbol(
    tmp_path: Path,
) -> None:
    payload = _artifact(tmp_path)
    candidate = next(
        item
        for item in payload["draft"]["candidates"]
        if item["source_kind"] == "docstring"
    )
    candidate["source"]["path"] = "unrelated.py"
    _recompute_candidate_id(candidate)

    with pytest.raises(ValueError, match="base-side changed-symbol span"):
        validate_requirement_draft_artifact(payload)


def test_docstring_candidate_must_satisfy_behavioral_selection(
    tmp_path: Path,
) -> None:
    payload = _artifact(tmp_path)
    candidate = next(
        item
        for item in payload["draft"]["candidates"]
        if item["source_kind"] == "docstring"
    )
    source = candidate["source"]
    source["excerpt"] = '"""Implementation detail only."""'
    source["end_line"] = source["start_line"]
    source["end_column"] = source["start_column"] + len(
        source["excerpt"].encode("utf-8")
    )
    candidate["statement"] = "Implementation detail only."
    _recompute_candidate_id(candidate)

    with pytest.raises(ValueError, match="behavioral docstring selection"):
        validate_requirement_draft_artifact(payload)


def test_validator_handles_cr_only_multiline_docstring_spans(
    tmp_path: Path,
) -> None:
    payload = _artifact(tmp_path)
    candidate = next(
        item
        for item in payload["draft"]["candidates"]
        if item["source_kind"] == "docstring"
    )
    candidate["source"].update(
        {
            "start_line": 1,
            "end_line": 3,
            "start_column": 4,
            "end_column": 3,
            "excerpt": '"""Return five.\rPreserve values.\r"""',
        }
    )
    candidate["statement"] = "Return five.\nPreserve values."
    _recompute_candidate_id(candidate)

    validate_requirement_draft_artifact(payload)

    candidate["source"]["end_column"] += 1
    _recompute_candidate_id(candidate)
    with pytest.raises(ValueError, match="final excerpt line"):
        validate_requirement_draft_artifact(payload)


@pytest.mark.parametrize(
    "code",
    [
        "context_parser_limit",
        "candidate_limit_reached",
        "unterminated_markdown_fence",
        "source_file_too_large",
        "candidate_excerpt_too_large",
    ],
)
def test_truncating_diagnostic_requires_truncated_draft(
    tmp_path: Path,
    code: str,
) -> None:
    payload = _artifact(tmp_path)
    payload["draft"]["diagnostics"] = [
        {
            "code": code,
            "source": ".",
            "message": "some extracted information was omitted",
            "line": None,
        }
    ]

    with pytest.raises(ValueError, match="truncated: must be true"):
        validate_requirement_draft_artifact(payload)

    payload["draft"]["truncated"] = True
    validate_requirement_draft_artifact(payload)


def test_nontruncating_diagnostic_does_not_require_truncated_draft(
    tmp_path: Path,
) -> None:
    payload = _artifact(tmp_path)
    payload["draft"]["diagnostics"] = [
        {
            "code": "syntax_error",
            "source": "value.py",
            "message": "base source could not be parsed",
            "line": 1,
        }
    ]

    validate_requirement_draft_artifact(payload)


def test_unknown_limit_named_diagnostic_does_not_imply_truncation(
    tmp_path: Path,
) -> None:
    payload = _artifact(tmp_path)
    payload["draft"]["diagnostics"] = [
        {
            "code": "future_limit_reached",
            "source": ".",
            "message": "an extension-specific advisory diagnostic",
            "line": None,
        }
    ]

    validate_requirement_draft_artifact(payload)


def test_draft_patch_requirements_bounds_issue_before_workspace_creation(
    tmp_path: Path,
) -> None:
    repository, patch_file, issue_file = _inputs(tmp_path)
    issue_file.write_bytes(b"x" * 1_000_001)

    with pytest.raises(DraftingError, match="1000000-byte limit"):
        draft_patch_requirements(
            DraftRequirementsRequest(
                repository=repository,
                base_revision="HEAD",
                patch_file=patch_file,
                issue_file=issue_file,
                output_file=tmp_path / "draft.json",
            )
        )


@pytest.mark.parametrize(
    "patch_bytes",
    [
        b"GIT binary patch\nliteral 1\nAcmZQz\n",
        b"diff --git a/value.py b/value.py\n\0",
    ],
)
def test_draft_patch_requirements_rejects_binary_patches_before_workspace(
    tmp_path: Path,
    patch_bytes: bytes,
) -> None:
    repository, patch_file, issue_file = _inputs(tmp_path)
    patch_file.write_bytes(patch_bytes)
    output = tmp_path / "draft.json"

    with pytest.raises(DraftingError, match="binary patches"):
        draft_patch_requirements(
            DraftRequirementsRequest(
                repository=repository,
                base_revision="HEAD",
                patch_file=patch_file,
                issue_file=issue_file,
                output_file=output,
            )
        )

    assert not output.exists()


def test_atomic_output_failure_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "draft.json"

    def fail_link(source: Path, destination: Path) -> None:
        raise OSError("link unavailable")

    monkeypatch.setattr(drafting_module.os, "link", fail_link)

    with pytest.raises(DraftingError, match="cannot write requirement draft"):
        drafting_module._write_exclusive(output, b'{"complete":true}\n')

    assert not output.exists()
    assert list(tmp_path.glob(".draft.json.*.tmp")) == []
