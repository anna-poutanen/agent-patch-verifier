from pathlib import Path
from types import SimpleNamespace

import pytest

from patch_verifier import cli


def test_cli_reports_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == "apv 0.5.0\n"


def test_cli_exits_two_when_execution_is_inconclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = SimpleNamespace(
        output_directory=tmp_path / "run",
        has_supported_failure=False,
        supported_failures=(),
        is_inconclusive=True,
        inconclusive_findings=(object(),),
    )
    monkeypatch.setattr(cli, "verify", lambda request: result)

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "verify",
                "--patch",
                str(tmp_path / "patch.diff"),
                "--issue",
                str(tmp_path / "issue.md"),
                "--evidence",
                str(tmp_path / "evidence.json"),
                "--output",
                str(tmp_path / "run"),
            ]
        )

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "Run inconclusive" in captured.err


def test_cli_exits_two_when_reproduction_findings_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = object()
    result = SimpleNamespace(
        output_directory=tmp_path / "replay",
        has_supported_failure=False,
        supported_failures=(),
        is_inconclusive=False,
        inconclusive_findings=(),
    )
    monkeypatch.setattr(cli, "request_from_bundle", lambda *args, **kwargs: request)
    monkeypatch.setattr(cli, "verify", lambda actual: result)
    monkeypatch.setattr(
        cli,
        "reproduction_differences",
        lambda run_directory, actual: ("CASE-1 changed",),
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(["reproduce", str(tmp_path / "recorded")])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "Reproduction drifted" in captured.err
    assert "CASE-1 changed" in captured.err
