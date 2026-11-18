from __future__ import annotations

import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from patch_verifier.models import FindingKind
from patch_verifier.verifier import (
    VerificationError,
    VerificationRequest,
    default_output_directory,
    reproduction_differences,
    request_from_bundle,
    verify,
)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def make_target(
    tmp_path: Path,
    *,
    proposed_return: str = "return 0",
) -> tuple[Path, Path]:
    repository = tmp_path / "target"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.name", "Verifier Test")
    git(repository, "config", "user.email", "verifier@example.invalid")
    (repository / "value.py").write_text(
        "def resolve(value=None):\n    return value or 5\n", encoding="utf-8"
    )
    (repository / "test_existing.py").write_text(
        "from value import resolve\n\n"
        "def test_existing_contracts():\n"
        "    assert resolve() == 5\n"
        "    assert resolve(3) == 3\n",
        encoding="utf-8",
    )
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "base")
    (repository / "value.py").write_text(
        f"def resolve(value=None):\n    {proposed_return}\n", encoding="utf-8"
    )
    patch = tmp_path / "proposed.diff"
    patch.write_text(git(repository, "diff", "--binary") + "\n", encoding="utf-8")
    git(repository, "restore", "value.py")
    return repository, patch


def write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    issue = tmp_path / "issue.md"
    issue.write_bytes(
        b"Explicit zero must return zero. "
        b"Existing behavior for other values must remain unchanged.\r\n"
    )
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "from value import resolve\n\n"
        "def test_zero():\n    assert resolve(0) == 0\n\n"
        "def test_three():\n    assert resolve(3) == 3\n\n"
        "def test_missing():\n    assert resolve() == 5\n",
        encoding="utf-8",
    )
    requirements = [
        {
            "id": "REQ-ZERO",
            "statement": "Explicit zero returns zero.",
            "kind": "requested",
            "provenance": "explicit",
            "source": "issue:1",
            "excerpt": "Explicit zero must return zero.",
        },
        {
            "id": "REQ-THREE",
            "statement": "Three remains three.",
            "kind": "preservation",
            "provenance": "preexisting",
            "source": "test_existing.py:5",
            "excerpt": "assert resolve(3) == 3",
        },
        {
            "id": "REQ-MISSING",
            "statement": "A missing value returns five.",
            "kind": "preservation",
            "provenance": "preexisting",
            "source": "test_existing.py:4",
            "excerpt": "assert resolve() == 5",
        },
    ]
    cases = [
        {
            "id": "CASE-ZERO",
            "requirement_id": "REQ-ZERO",
            "test_file": "candidate.py",
            "node": "test_zero",
        },
        {
            "id": "CASE-THREE",
            "requirement_id": "REQ-THREE",
            "test_file": "candidate.py",
            "node": "test_three",
        },
        {
            "id": "CASE-MISSING",
            "requirement_id": "REQ-MISSING",
            "test_file": "candidate.py",
            "node": "test_missing",
        },
    ]
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {"schema_version": 1, "requirements": requirements, "cases": cases}
        ),
        encoding="utf-8",
    )
    return issue, evidence


def test_end_to_end_finds_overfitted_zero_patch(tmp_path: Path) -> None:
    repository, patch = make_target(tmp_path)
    issue, evidence = write_inputs(tmp_path)
    status_before = git(repository, "status", "--short")
    output = tmp_path / "run"

    result = verify(
        VerificationRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch,
            issue_file=issue,
            evidence_file=evidence,
            output_directory=output,
            command_prefix=(sys.executable, "-m", "pytest", "-q", "--tb=short"),
            repeat=2,
        )
    )

    assert [item.kind for item in result.findings] == [
        FindingKind.CRITERION_PASSES,
        FindingKind.SUPPORTED_REGRESSION,
        FindingKind.SUPPORTED_REGRESSION,
    ]
    assert result.has_supported_failure
    assert len(result.supported_failures) == 2
    assert result.changed_symbols[0].qualified_name == "resolve"
    assert git(repository, "status", "--short") == status_before
    assert len(git(repository, "worktree", "list").splitlines()) == 1

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((output / "results.json").read_text(encoding="utf-8"))
    report = (output / "report.md").read_text(encoding="utf-8")
    assert manifest["base_commit"] == git(repository, "rev-parse", "HEAD")
    assert len(manifest["evidence_plan_sha256"]) == 64
    assert len(manifest["results_sha256"]) == 64
    assert all("--runxfail" in case["command"] for case in manifest["cases"])
    assert all(
        any(argument.startswith("--junitxml=") for argument in case["command"])
        for case in manifest["cases"]
    )
    assert len(results["cases"]) == 3
    assert "Found 2 supported patch failure(s)." in report
    assert "REQ-THREE" in report
    assert "Exact command:" in report
    assert "Execution evidence:" in report
    assert (output / "patch.diff").read_bytes() == patch.read_bytes()
    assert (output / "issue.md").read_bytes() == issue.read_bytes()
    assert len(list((output / "tests").glob("test_*.py"))) == 3

    bundled_plan = json.loads(
        (output / "evidence-plan.json").read_text(encoding="utf-8")
    )
    assert bundled_plan["cases"][0]["test_file"].startswith("tests/test_")

    reproduced_output = tmp_path / "reproduced"
    reproduced = verify(
        request_from_bundle(output, output_directory=reproduced_output)
    )
    assert [item.kind for item in reproduced.findings] == [
        FindingKind.CRITERION_PASSES,
        FindingKind.SUPPORTED_REGRESSION,
        FindingKind.SUPPORTED_REGRESSION,
    ]
    assert reproduced.snapshot.base_sha == result.snapshot.base_sha
    assert reproduction_differences(output, reproduced) == ()

    alternate_repository = tmp_path / "alternate-source-location"
    replay_request = request_from_bundle(output, repository=alternate_repository)
    assert replay_request.repository == alternate_repository.resolve()
    command_override = (sys.executable, "-m", "pytest", "-q")
    replay_request = request_from_bundle(
        output, command_prefix_override=command_override
    )
    assert replay_request.command_prefix == command_override

    replay_results_path = reproduced_output / "results.json"
    replay_results = json.loads(replay_results_path.read_text(encoding="utf-8"))
    replay_results["cases"][0]["finding"]["kind"] = "inconclusive"
    replay_results_path.write_text(json.dumps(replay_results), encoding="utf-8")
    assert reproduction_differences(output, reproduced)
    changed_snapshot = replace(result.snapshot, patched_tree_hash="0" * 40)
    assert reproduction_differences(
        output, replace(result, snapshot=changed_snapshot)
    )

    for relative in (
        "patch.diff",
        "issue.md",
        "evidence-plan.json",
        "results.json",
        "harness/conftest.py",
        bundled_plan["cases"][0]["test_file"],
    ):
        artifact = output / relative
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"\n# changed")
        try:
            with pytest.raises(VerificationError, match="hash mismatch"):
                request_from_bundle(output)
        finally:
            artifact.write_bytes(original)

    bundled_evidence = output / "evidence-plan.json"
    evidence_bytes = bundled_evidence.read_bytes()
    external_evidence = tmp_path / "external-evidence.json"
    external_evidence.write_bytes(evidence_bytes)
    bundled_evidence.unlink()
    bundled_evidence.symlink_to(external_evidence)
    try:
        with pytest.raises(VerificationError, match="symbolic link"):
            request_from_bundle(output)
    finally:
        bundled_evidence.unlink()
        bundled_evidence.write_bytes(evidence_bytes)


def test_cli_verify_and_reproduce_workflow(tmp_path: Path) -> None:
    repository, patch = make_target(tmp_path)
    issue, evidence = write_inputs(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    output = tmp_path / "cli-run"

    verified = subprocess.run(
        [
            sys.executable,
            "-m",
            "patch_verifier",
            "verify",
            "--repo",
            str(repository),
            "--base",
            "HEAD",
            "--patch",
            str(patch),
            "--issue",
            str(issue),
            "--evidence",
            str(evidence),
            "--output",
            str(output),
            "--timeout",
            "10",
            "--repeat",
            "1",
        ],
        cwd=project_root,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )

    assert verified.returncode == 1, verified.stderr
    assert f"Report: {output / 'report.md'}" in verified.stdout
    assert "Found 2 supported patch failure(s)." in verified.stdout
    assert verified.stderr == ""
    assert (output / "report.md").is_file()

    replay = tmp_path / "cli-replay"
    reproduced = subprocess.run(
        [
            sys.executable,
            "-m",
            "patch_verifier",
            "reproduce",
            str(output),
            "--output",
            str(replay),
        ],
        cwd=project_root,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )

    assert reproduced.returncode == 1, reproduced.stderr
    assert f"Report: {replay / 'report.md'}" in reproduced.stdout
    assert "Found 2 supported patch failure(s)." in reproduced.stdout
    assert "Reproduction drifted" not in reproduced.stderr
    assert (replay / "report.md").is_file()


def test_default_output_does_not_dirty_target_worktree(tmp_path: Path) -> None:
    repository, _ = make_target(tmp_path)

    output = default_output_directory(repository)

    assert output.is_relative_to(repository / ".git" / "patch-verifier" / "runs")
    assert git(repository, "status", "--short") == ""


def test_each_case_and_repeat_uses_fresh_worktrees(tmp_path: Path) -> None:
    repository, patch = make_target(
        tmp_path,
        proposed_return="return value if value is not None else 5",
    )
    issue, evidence = write_inputs(tmp_path)
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "from pathlib import Path\n"
        "from value import resolve\n\n"
        "def test_zero():\n"
        "    if resolve(0) == 0:\n"
        "        Path('candidate-leak').write_text('created')\n"
        "    assert resolve(0) == 0\n\n"
        "def test_three():\n"
        "    assert not Path('candidate-leak').exists() and resolve(3) == 3\n\n"
        "def test_missing():\n"
        "    assert resolve() == 5\n",
        encoding="utf-8",
    )

    result = verify(
        VerificationRequest(
            repository=repository,
            base_revision="HEAD",
            patch_file=patch,
            issue_file=issue,
            evidence_file=evidence,
            output_directory=tmp_path / "fresh-run",
            repeat=2,
        )
    )

    assert [item.kind for item in result.findings] == [
        FindingKind.CRITERION_PASSES,
        FindingKind.PRESERVED,
        FindingKind.PRESERVED,
    ]
