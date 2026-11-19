from pathlib import Path
from types import SimpleNamespace

import pytest

from patch_verifier import cli
from patch_verifier import drafting


def test_draft_requirements_calls_review_only_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}
    output = tmp_path / "requirements-draft.json"

    def fake_draft(**arguments: object) -> SimpleNamespace:
        observed.update(arguments)
        return SimpleNamespace(
            output_file=output,
            candidate_count=3,
            truncated=False,
        )

    def unexpected_verify(request: object) -> object:
        raise AssertionError("drafting requirements must not execute candidates")

    monkeypatch.setattr(cli, "_draft_patch_requirements", fake_draft)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    cli.main(
        [
            "draft-requirements",
            "--repo",
            str(tmp_path),
            "--base",
            "main~1",
            "--patch",
            str(tmp_path / "change.diff"),
            "--issue",
            str(tmp_path / "issue.md"),
            "--output",
            str(output),
        ]
    )

    assert observed == {
        "repository": tmp_path.resolve(),
        "base_revision": "main~1",
        "patch_file": tmp_path / "change.diff",
        "issue_file": tmp_path / "issue.md",
        "output_file": output,
    }
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Draft: {output}\n" in captured.out
    assert (
        "Candidates: 3 "
        "(review required; no candidate tests were generated or run)\n"
        in captured.out
    )


def test_draft_adapter_constructs_orchestration_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[drafting.DraftRequirementsRequest] = []
    expected = drafting.DraftRequirementsResult(
        output_file=tmp_path / "draft.json",
        candidate_count=2,
        truncated=False,
    )

    def fake_orchestration(
        request: drafting.DraftRequirementsRequest,
    ) -> drafting.DraftRequirementsResult:
        observed.append(request)
        return expected

    monkeypatch.setattr(drafting, "draft_patch_requirements", fake_orchestration)

    result = cli._draft_patch_requirements(
        repository=tmp_path / "repository",
        base_revision="base-commit",
        patch_file=tmp_path / "change.diff",
        issue_file=tmp_path / "issue.md",
        output_file=tmp_path / "draft.json",
    )

    assert result is expected
    assert observed == [
        drafting.DraftRequirementsRequest(
            repository=tmp_path / "repository",
            base_revision="base-commit",
            patch_file=tmp_path / "change.diff",
            issue_file=tmp_path / "issue.md",
            output_file=tmp_path / "draft.json",
        )
    ]


def test_draft_adapter_normalizes_orchestration_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(request: drafting.DraftRequirementsRequest) -> object:
        raise drafting.DraftingError("cannot write draft")

    monkeypatch.setattr(drafting, "draft_patch_requirements", fail)

    with pytest.raises(ValueError, match="cannot write draft"):
        cli._draft_patch_requirements(
            repository=tmp_path,
            base_revision="HEAD",
            patch_file=tmp_path / "change.diff",
            issue_file=tmp_path / "issue.md",
            output_file=tmp_path / "draft.json",
        )


def test_draft_requirements_reports_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "draft.json"
    monkeypatch.setattr(
        cli,
        "_draft_patch_requirements",
        lambda **arguments: SimpleNamespace(
            output_file=output,
            candidate_count=10,
            truncated=True,
        ),
    )

    cli.main(
        [
            "draft-requirements",
            "--patch",
            str(tmp_path / "change.diff"),
            "--issue",
            str(tmp_path / "issue.md"),
            "--output",
            str(output),
        ]
    )

    assert "Warning: candidate extraction was truncated." in capsys.readouterr().err


def test_draft_requirements_fails_safely_without_running_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "draft.json"

    def fail_draft(**arguments: object) -> object:
        raise ValueError("output file already exists")

    def unexpected_verify(request: object) -> object:
        raise AssertionError("verification must not run after a drafting error")

    monkeypatch.setattr(cli, "_draft_patch_requirements", fail_draft)
    monkeypatch.setattr(cli, "verify", unexpected_verify)

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "draft-requirements",
                "--patch",
                str(tmp_path / "change.diff"),
                "--issue",
                str(tmp_path / "issue.md"),
                "--output",
                str(output),
            ]
        )

    assert raised.value.code == 2
    assert "apv: error: output file already exists" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    "missing_option",
    ["--patch", "--issue", "--output"],
)
def test_draft_requirements_requires_explicit_inputs(
    tmp_path: Path,
    missing_option: str,
) -> None:
    values = {
        "--patch": str(tmp_path / "change.diff"),
        "--issue": str(tmp_path / "issue.md"),
        "--output": str(tmp_path / "draft.json"),
    }
    arguments = ["draft-requirements"]
    for option, value in values.items():
        if option != missing_option:
            arguments.extend([option, value])

    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 2
