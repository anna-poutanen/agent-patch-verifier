import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from patch_verifier import cli
from patch_verifier import conflicts
from patch_verifier import review


def _analysis(*, truncated: bool = False) -> SimpleNamespace:
    hint = {
        "id": "hint-" + "1" * 64,
        "candidate_ids": ["cand-" + "2" * 64, "cand-" + "3" * 64],
        "message": "Potential tension; inspect both cited candidates.",
    }
    summary = {
        "hints": [hint],
        "requested_candidate_count": 1,
        "preservation_candidate_count": 1,
        "compared_pair_count": 1,
        "hint_count": 1,
        "truncated": truncated,
    }
    return SimpleNamespace(
        hints=(SimpleNamespace(to_dict=lambda: hint),),
        truncated=truncated,
        to_dict=lambda: summary,
    )


def test_prepare_review_calls_review_only_adapter_and_reports_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "review.json"
    observed: dict[str, object] = {}

    def fake_prepare(**arguments: object) -> tuple[SimpleNamespace, bool]:
        observed.update(arguments)
        return (
            SimpleNamespace(
                output_file=output,
                candidate_count=4,
                conflict_hint_count=2,
            ),
            True,
        )

    def unexpected_verify(request: object) -> object:
        raise AssertionError("preparing a review must not run verification")

    monkeypatch.setattr(cli, "_prepare_requirement_review", fake_prepare)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    cli.main(
        [
            "prepare-review",
            "--draft",
            str(tmp_path / "draft.json"),
            "--output",
            str(output),
        ]
    )

    assert observed == {
        "draft_file": tmp_path / "draft.json",
        "output_file": output,
    }
    captured = capsys.readouterr()
    assert f"Review: {output}\n" in captured.out
    assert "Candidates: 4 (all pending; explicit human review required)" in captured.out
    assert "Potential tension hints: 2 (advisory only" in captured.out
    assert "no candidate tests were generated or run" in captured.out
    assert "conflict analysis was truncated" in captured.err


def test_prepare_adapter_analyzes_bounded_draft_and_passes_exact_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "draft.json"
    output = tmp_path / "review.json"
    payload = {"draft": {"marker": "exact parsed value"}}
    draft.write_text(json.dumps(payload), encoding="utf-8")
    analysis = _analysis(truncated=True)
    observed: dict[str, object] = {}
    expected = review.ReviewPreparationResult(
        output_file=output,
        draft_sha256="a" * 64,
        candidate_count=2,
        conflict_hint_count=1,
    )

    def fake_analyze(actual: object) -> SimpleNamespace:
        observed["draft"] = actual
        return analysis

    def fake_write(
        draft_file: Path,
        output_file: Path,
        **arguments: object,
    ) -> review.ReviewPreparationResult:
        observed["write"] = (draft_file, output_file, arguments)
        return expected

    monkeypatch.setattr(conflicts, "analyze_draft_conflicts", fake_analyze)
    monkeypatch.setattr(review, "write_requirement_review", fake_write)

    result, truncated = cli._prepare_requirement_review(
        draft_file=draft,
        output_file=output,
    )

    assert result is expected
    assert truncated is True
    assert observed["draft"] == payload
    written_draft, written_output, arguments = observed["write"]
    assert written_draft == draft
    assert written_output == output
    assert arguments == {
        "conflict_hints": tuple(hint.to_dict() for hint in analysis.hints),
        "conflict_analysis": analysis.to_dict(),
        "expected_draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
    }


def test_finalize_review_recomputes_analysis_and_reports_decision_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "reviewed-requirements.json"
    observed: dict[str, object] = {}

    def fake_finalize(**arguments: object) -> tuple[SimpleNamespace, bool]:
        observed.update(arguments)
        return (
            SimpleNamespace(
                output_file=output,
                requirement_count=3,
                rejected_count=2,
            ),
            False,
        )

    def unexpected_verify(request: object) -> object:
        raise AssertionError("finalizing a review must not run verification")

    monkeypatch.setattr(cli, "_finalize_requirement_review", fake_finalize)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    cli.main(
        [
            "finalize-review",
            "--draft",
            str(tmp_path / "draft.json"),
            "--review",
            str(tmp_path / "review.json"),
            "--output",
            str(output),
        ]
    )

    assert observed == {
        "draft_file": tmp_path / "draft.json",
        "review_file": tmp_path / "review.json",
        "output_file": output,
    }
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Reviewed requirements: {output}\n" in captured.out
    assert "Accepted: 3\n" in captured.out
    assert "Rejected: 2\n" in captured.out
    assert "No candidate tests were generated or run.\n" in captured.out


def test_finalize_adapter_requires_current_conflict_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "draft.json"
    review_file = tmp_path / "review.json"
    output = tmp_path / "reviewed.json"
    payload = {"draft": {"marker": "recompute me"}}
    draft.write_text(json.dumps(payload), encoding="utf-8")
    analysis = _analysis()
    observed: dict[str, object] = {}
    expected = review.ReviewFinalizationResult(
        output_file=output,
        draft_sha256="a" * 64,
        review_sha256="b" * 64,
        requirement_count=1,
        rejected_count=1,
    )

    def fake_analyze(actual: object) -> SimpleNamespace:
        observed["draft"] = actual
        return analysis

    def fake_write(
        draft_file: Path,
        actual_review_file: Path,
        output_file: Path,
        **arguments: object,
    ) -> review.ReviewFinalizationResult:
        observed["write"] = (
            draft_file,
            actual_review_file,
            output_file,
            arguments,
        )
        return expected

    monkeypatch.setattr(conflicts, "analyze_draft_conflicts", fake_analyze)
    monkeypatch.setattr(review, "write_reviewed_requirements", fake_write)

    result, truncated = cli._finalize_requirement_review(
        draft_file=draft,
        review_file=review_file,
        output_file=output,
    )

    assert result is expected
    assert truncated is False
    assert observed["draft"] == payload
    written_draft, written_review, written_output, arguments = observed["write"]
    assert (written_draft, written_review, written_output) == (
        draft,
        review_file,
        output,
    )
    assert arguments == {
        "expected_conflict_hints": tuple(
            hint.to_dict() for hint in analysis.hints
        ),
        "expected_conflict_analysis": analysis.to_dict(),
        "expected_draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
    }


def test_finalize_review_reports_truncated_conflict_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "requirements.json"
    monkeypatch.setattr(
        cli,
        "_finalize_requirement_review",
        lambda **arguments: (
            SimpleNamespace(
                output_file=output,
                requirement_count=1,
                rejected_count=0,
            ),
            True,
        ),
    )

    cli.main(
        [
            "finalize-review",
            "--draft",
            str(tmp_path / "draft.json"),
            "--review",
            str(tmp_path / "review.json"),
            "--output",
            str(output),
        ]
    )

    assert "conflict analysis was truncated" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "adapter_name"),
    [
        (
            ["prepare-review", "--draft", "draft.json", "--output", "review.json"],
            "_prepare_requirement_review",
        ),
        (
            [
                "finalize-review",
                "--draft",
                "draft.json",
                "--review",
                "review.json",
                "--output",
                "requirements.json",
            ],
            "_finalize_requirement_review",
        ),
    ],
)
def test_review_commands_normalize_expected_errors_to_exit_two(
    arguments: list[str],
    adapter_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**keyword_arguments: object) -> object:
        raise ValueError("review artifact does not match its draft")

    def unexpected_verify(request: object) -> object:
        raise AssertionError("review errors must not fall through to verification")

    monkeypatch.setattr(cli, adapter_name, fail)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 2
    assert (
        "apv: error: review artifact does not match its draft"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["prepare-review", "--draft", "draft.json"],
        ["prepare-review", "--output", "review.json"],
        [
            "finalize-review",
            "--review",
            "review.json",
            "--output",
            "requirements.json",
        ],
        [
            "finalize-review",
            "--draft",
            "draft.json",
            "--output",
            "requirements.json",
        ],
        [
            "finalize-review",
            "--draft",
            "draft.json",
            "--review",
            "review.json",
        ],
    ],
)
def test_review_commands_require_explicit_artifact_paths(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 2


def test_requirement_draft_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    draft = tmp_path / "draft.json"
    draft.write_text('{"draft": {}, "draft": {}}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object key 'draft'"):
        cli._load_requirement_draft(draft)
