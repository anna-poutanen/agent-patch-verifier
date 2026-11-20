from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from patch_verifier import cli
from patch_verifier import test_proposals
from patch_verifier.review import (
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
)


def _reviewed_requirements_document() -> bytes:
    issue_text = "# Resolver\n\nExplicit zero must be returned unchanged.\n"
    excerpt = "Explicit zero must be returned unchanged."
    source = {
        "path": "requests/zero.contract",
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
    canonical_identity = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate: dict[str, Any] = {
        "id": "cand-" + hashlib.sha256(canonical_identity).hexdigest(),
        **identity,
        "review_status": "requires_review",
        "confidence": "high",
        "rationale": "Exact issue text provides a reviewable behavior candidate.",
    }
    draft = {
        "schema_version": 1,
        "tool_version": "0.4.0",
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
            "issue_sha256": hashlib.sha256(issue_text.encode()).hexdigest(),
            "issue_source": "requests/zero.contract",
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": [candidate],
            "diagnostics": [],
            "limitations": ["Every candidate requires a human decision."],
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


def _raw_proposal(requirement_id: str, *, marker: Path) -> dict[str, str]:
    return {
        "requirement_id": requirement_id,
        "category": "boundary",
        "test_file": "generated_tests/test_zero.py",
        "node": "test_zero_is_preserved",
        "rationale": "Checks the reviewed zero-value boundary.",
        "source": (
            "from pathlib import Path\n\n"
            "def test_zero_is_preserved():\n"
            f"    Path({str(marker)!r}).write_text('executed')\n"
            "    assert 0 == 0\n"
        ),
    }


def test_import_test_proposals_end_to_end_writes_only_exact_review_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reviewed_document = _reviewed_requirements_document()
    reviewed_payload = json.loads(reviewed_document)
    requirement_id = reviewed_payload["requirements"][0]["id"]
    marker = tmp_path / "candidate-executed"
    raw_proposal = _raw_proposal(requirement_id, marker=marker)
    response_document = (
        json.dumps(
            {"schema_version": 1, "proposals": [raw_proposal]},
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    output_file = tmp_path / "test-proposals.json"
    reviewed_file.write_bytes(reviewed_document)
    response_file.write_bytes(response_document)

    cli.main(
        [
            "import-test-proposals",
            "--reviewed",
            str(reviewed_file),
            "--response",
            str(response_file),
            "--adapter",
            "provider-neutral-fixture",
            "--model",
            "fixture-model",
            "--output",
            str(output_file),
        ]
    )

    expected = test_proposals.build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="provider-neutral-fixture",
        generator_model="fixture-model",
        proposals=[raw_proposal],
        generator_response_document=response_document,
    )
    assert output_file.read_bytes() == test_proposals.encode_test_proposal_artifact(
        expected
    )
    loaded = test_proposals.load_test_proposal_artifact(
        output_file.read_bytes(),
        reviewed_requirements_document=reviewed_document,
        generator_response_document=response_document,
    )
    assert loaded.reviewed_requirements_sha256 == hashlib.sha256(
        reviewed_document
    ).hexdigest()
    assert loaded.generator_response.binding == "exact"
    assert loaded.generator_response.sha256 == hashlib.sha256(
        response_document
    ).hexdigest()
    assert loaded.proposals[0].requirement_id == requirement_id
    assert loaded.proposals[0].source_text == raw_proposal["source"]

    assert not marker.exists()
    assert not (tmp_path / "generated_tests").exists()
    assert sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    ) == ["response.json", "reviewed.json", "test-proposals.json"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Test proposals: {output_file}\n" in captured.out
    assert "Proposals: 1 (all require explicit review)" in captured.out
    assert "Reviewed requirements without a proposal: 0" in captured.out
    assert "imported, collected, or executed" in captured.out


def test_import_test_proposals_end_to_end_rejects_unknown_requirement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reviewed_document = _reviewed_requirements_document()
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    output_file = tmp_path / "test-proposals.json"
    marker = tmp_path / "candidate-executed"
    reviewed_file.write_bytes(reviewed_document)
    response_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "proposals": [
                    _raw_proposal("req-" + "f" * 60, marker=marker)
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "import-test-proposals",
                "--reviewed",
                str(reviewed_file),
                "--response",
                str(response_file),
                "--adapter",
                "provider-neutral-fixture",
                "--output",
                str(output_file),
            ]
        )

    assert raised.value.code == 2
    assert not output_file.exists()
    assert not marker.exists()
    assert not (tmp_path / "generated_tests").exists()
    assert sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    ) == ["response.json", "reviewed.json"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "apv: error:" in captured.err
    assert "references an unreviewed requirement" in captured.err
    assert "req-" + "f" * 60 in captured.err


def test_import_test_proposals_is_review_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "test-proposals.json"
    observed: dict[str, object] = {}

    def fake_import(**arguments: object) -> SimpleNamespace:
        observed.update(arguments)
        return SimpleNamespace(
            output_file=output,
            proposal_count=3,
            unproposed_requirement_count=2,
        )

    def unexpected_verify(request: object) -> object:
        raise AssertionError("proposal import must not run verification")

    monkeypatch.setattr(cli, "_import_test_proposals", fake_import)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    cli.main(
        [
            "import-test-proposals",
            "--reviewed",
            str(tmp_path / "reviewed.json"),
            "--response",
            str(tmp_path / "response.json"),
            "--adapter",
            "example-adapter",
            "--model",
            "example-model",
            "--output",
            str(output),
        ]
    )

    assert observed == {
        "reviewed_file": tmp_path / "reviewed.json",
        "response_file": tmp_path / "response.json",
        "output_file": output,
        "generator_adapter": "example-adapter",
        "generator_model": "example-model",
    }
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Test proposals: {output}\n" in captured.out
    assert "Proposals: 3 (all require explicit review)" in captured.out
    assert "Reviewed requirements without a proposal: 2" in captured.out
    assert "imported, collected, or executed" in captured.out


def test_import_adapter_passes_generator_identity_to_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = tmp_path / "reviewed.json"
    response = tmp_path / "response.json"
    output = tmp_path / "proposals.json"
    expected = SimpleNamespace(
        output_file=output,
        proposal_count=1,
        unproposed_requirement_count=0,
    )
    observed: dict[str, object] = {}

    def fake_write(*arguments: object, **keywords: object) -> SimpleNamespace:
        observed["arguments"] = arguments
        observed["keywords"] = keywords
        return expected

    monkeypatch.setattr(test_proposals, "write_test_proposal_artifact", fake_write)

    result = cli._import_test_proposals(
        reviewed_file=reviewed,
        response_file=response,
        output_file=output,
        generator_adapter="adapter-name",
        generator_model=None,
    )

    assert result is expected
    assert observed["arguments"] == (reviewed, response, output)
    identity = observed["keywords"]["generator"]  # type: ignore[index]
    assert identity == test_proposals.GeneratorIdentity("adapter-name", None)


def test_import_adapter_translates_proposal_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*arguments: object, **keywords: object) -> object:
        raise test_proposals.TestProposalError("unsafe generated response")

    monkeypatch.setattr(test_proposals, "write_test_proposal_artifact", fail)

    with pytest.raises(ValueError, match="unsafe generated response"):
        cli._import_test_proposals(
            reviewed_file=tmp_path / "reviewed.json",
            response_file=tmp_path / "response.json",
            output_file=tmp_path / "proposals.json",
            generator_adapter="adapter",
            generator_model=None,
        )
