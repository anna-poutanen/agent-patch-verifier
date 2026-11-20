from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from patch_verifier import cli, test_proposals
from patch_verifier.review import (
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
)
from patch_verifier.test_review import encode_test_review_artifact


ISSUE = "# Resolver\n\nExplicit zero must be returned unchanged.\n"


def _reviewed_requirements_document() -> bytes:
    excerpt = "Explicit zero must be returned unchanged."
    source = {
        "path": "request.md",
        "start_line": 3,
        "end_line": 3,
        "start_column": 0,
        "end_column": len(excerpt.encode("utf-8")),
        "excerpt": excerpt,
        "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    identity = {
        "intent": "requested",
        "source": source,
        "source_kind": "issue",
        "statement": excerpt,
    }
    candidate: dict[str, Any] = {
        "id": "cand-"
        + hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        **identity,
        "review_status": "requires_review",
        "confidence": "high",
        "rationale": "Exact issue text supports this behavior candidate.",
    }
    draft = {
        "schema_version": 1,
        "tool_version": "0.5.0",
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.1",
            "platform": "test",
        },
        "inputs": {
            "repository": "/work/resolver",
            "base_revision": "HEAD",
            "base_sha": "b" * 40,
            "base_context_sha256": "a" * 64,
            "patched_tree_hash": "c" * 40,
            "patch_sha256": "d" * 64,
            "issue_sha256": hashlib.sha256(ISSUE.encode("utf-8")).hexdigest(),
            "issue_source": "request.md",
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": [candidate],
            "diagnostics": [],
            "limitations": ["Every candidate requires explicit review."],
            "truncated": False,
        },
    }
    draft_document = encode_review_artifact(draft)
    review = prepare_requirement_review(draft_document)
    review["decisions"][0].update(
        action="accept",
        rationale="Accepted as the source-backed test expectation.",
    )
    finalized = finalize_requirement_review(
        draft_document,
        encode_review_artifact(review),
    )
    return encode_review_artifact(finalized)


def test_review_cli_workflow_is_inert_through_materialization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reviewed_document = _reviewed_requirements_document()
    requirement_id = json.loads(reviewed_document)["requirements"][0]["id"]
    marker = tmp_path / "candidate-code-executed"
    candidate_source = (
        "from pathlib import Path\n\n"
        f"Path({str(marker)!r}).write_text('executed')\n\n"
        "def test_zero_is_preserved():\n"
        "    assert 0 == 0\n"
    )
    raw_proposal = {
        "requirement_id": requirement_id,
        "category": "boundary",
        "test_file": "generated_tests/test_zero.py",
        "node": "test_zero_is_preserved",
        "rationale": "Checks the reviewed zero-value boundary.",
        "source": candidate_source,
    }
    response_document = (
        json.dumps(
            {"schema_version": 1, "proposals": [raw_proposal]},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    proposals = test_proposals.build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="provider-neutral-fixture",
        generator_model="fixture-model",
        proposals=[raw_proposal],
        generator_response_document=response_document,
    )
    proposal_document = test_proposals.encode_test_proposal_artifact(proposals)

    proposals_file = tmp_path / "test-proposals.json"
    reviewed_file = tmp_path / "reviewed-requirements.json"
    issue_file = tmp_path / "issue.md"
    response_file = tmp_path / "response.json"
    review_file = tmp_path / "test-proposal-review.json"
    reviewed_tests_file = tmp_path / "reviewed-test-proposals.json"
    staging_directory = tmp_path / "staged-tests"
    proposals_file.write_bytes(proposal_document)
    reviewed_file.write_bytes(reviewed_document)
    issue_file.write_text(ISSUE, encoding="utf-8")
    response_file.write_bytes(response_document)

    cli.main(
        [
            "prepare-test-review",
            "--proposals",
            str(proposals_file),
            "--reviewed",
            str(reviewed_file),
            "--issue",
            str(issue_file),
            "--response",
            str(response_file),
            "--output",
            str(review_file),
        ]
    )

    assert not marker.exists()
    pending_review = json.loads(review_file.read_bytes())
    assert pending_review["decisions"][0]["action"] == "pending"
    prepared_output = capsys.readouterr()
    assert prepared_output.err == ""
    assert f"Test proposal review: {review_file}" in prepared_output.out
    assert "all pending; explicit human review required" in prepared_output.out
    assert "not imported, collected, or executed" in prepared_output.out

    pending_review["decisions"][0].update(
        action="accept",
        rationale="Accepted after reviewing the exact candidate source.",
    )
    review_file.write_bytes(encode_test_review_artifact(pending_review))
    cli.main(
        [
            "finalize-test-review",
            "--proposals",
            str(proposals_file),
            "--reviewed",
            str(reviewed_file),
            "--issue",
            str(issue_file),
            "--review",
            str(review_file),
            "--response",
            str(response_file),
            "--output",
            str(reviewed_tests_file),
        ]
    )

    assert not marker.exists()
    reviewed_tests = json.loads(reviewed_tests_file.read_bytes())
    assert len(reviewed_tests["accepted_cases"]) == 1
    finalized_output = capsys.readouterr()
    assert finalized_output.err == ""
    assert f"Reviewed test proposals: {reviewed_tests_file}" in finalized_output.out
    assert "Accepted: 1" in finalized_output.out
    assert "Rejected: 0" in finalized_output.out
    assert "not imported, collected, or executed" in finalized_output.out

    cli.main(
        [
            "materialize-reviewed-tests",
            "--reviewed-tests",
            str(reviewed_tests_file),
            "--proposals",
            str(proposals_file),
            "--reviewed",
            str(reviewed_file),
            "--issue",
            str(issue_file),
            "--review",
            str(review_file),
            "--response",
            str(response_file),
            "--output-dir",
            str(staging_directory),
        ]
    )

    assert not marker.exists()
    assert (
        staging_directory / "generated_tests" / "test_zero.py"
    ).read_text(encoding="utf-8") == candidate_source
    assert (staging_directory / "evidence-plan.json").is_file()
    assert (staging_directory / "reviewed-test-proposals.json").is_file()
    assert (staging_directory / "materialization.json").is_file()
    materialized_output = capsys.readouterr()
    assert materialized_output.err == ""
    assert f"Staging directory: {staging_directory}" in materialized_output.out
    assert "Accepted cases staged: 1" in materialized_output.out
    assert "were not imported, collected, or executed" in materialized_output.out


def test_prepare_test_review_cli_translates_review_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from patch_verifier import test_review

    def fail(*args: object, **kwargs: object) -> object:
        raise test_review.TestProposalReviewError("exact proposal binding changed")

    monkeypatch.setattr(test_review, "write_test_proposal_review", fail)
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "prepare-test-review",
                "--proposals",
                str(tmp_path / "proposals.json"),
                "--reviewed",
                str(tmp_path / "reviewed.json"),
                "--issue",
                str(tmp_path / "issue.md"),
                "--output",
                str(tmp_path / "review.json"),
            ]
        )

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "apv: error: exact proposal binding changed\n"


def test_verify_materialized_cli_is_an_explicit_execution_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = object()
    captured_request: dict[str, object] = {}

    def build_request(**kwargs: object) -> object:
        captured_request.update(kwargs)
        return request

    result = SimpleNamespace(
        output_directory=tmp_path / "run",
        has_supported_failure=False,
        supported_failures=(),
        is_inconclusive=False,
        inconclusive_findings=(),
    )
    monkeypatch.setattr(cli, "_request_from_test_materialization", build_request)
    monkeypatch.setattr(cli, "verify", lambda actual: result)

    cli.main(
        [
            "verify-materialized",
            "--materialization",
            str(tmp_path / "staged"),
            "--repo",
            str(tmp_path / "repo"),
            "--base",
            "base-revision",
            "--patch",
            str(tmp_path / "change.diff"),
            "--output",
            str(tmp_path / "run"),
            "--test-command",
            "python3 -m pytest -q",
            "--timeout",
            "17",
            "--repeat",
            "3",
        ]
    )

    assert captured_request == {
        "materialization_directory": tmp_path / "staged",
        "repository": (tmp_path / "repo").resolve(),
        "base_revision": "base-revision",
        "patch_file": tmp_path / "change.diff",
        "output_directory": tmp_path / "run",
        "command_prefix": ("python3", "-m", "pytest", "-q"),
        "timeout_seconds": 17.0,
        "repeat": 3,
    }
    captured = capsys.readouterr()
    assert "Report:" in captured.out
    assert "not proof of correctness" in captured.out
    assert "untrusted Python" in captured.err
    assert "without an OS sandbox" in captured.err


def test_verify_materialized_adapter_translates_authentication_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from patch_verifier import verifier
    from patch_verifier.materialization import TestMaterializationError

    def fail(*args: object, **kwargs: object) -> object:
        raise TestMaterializationError("candidate hash changed")

    monkeypatch.setattr(verifier, "request_from_materialization", fail)

    with pytest.raises(ValueError, match="candidate hash changed"):
        cli._request_from_test_materialization(
            materialization_directory=tmp_path / "staged",
            repository=tmp_path / "repo",
            base_revision="HEAD",
            patch_file=tmp_path / "change.diff",
            output_directory=tmp_path / "run",
            command_prefix=("python3", "-m", "pytest"),
            timeout_seconds=60.0,
            repeat=2,
        )
