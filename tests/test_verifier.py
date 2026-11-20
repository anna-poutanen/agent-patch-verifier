from __future__ import annotations

import copy
import json
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import patch_verifier.verifier as verifier_module
from patch_verifier.models import FindingKind
from patch_verifier.verifier import (
    VerificationError,
    VerificationRequest,
    _validate_context_payload,
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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "A_A_A.A_A_A_A-A.A_A_A.A-A",
            "A_A.A_A_A-A-A-A_A-A_A.A-A",
        ),
        ("AbCDefgHijKlMNopqrst", "AbCdEFGhIJKlMnopQrst"),
    ],
)
def test_generated_test_path_collisions_fail_before_execution(
    first: str,
    second: str,
) -> None:

    first_path = verifier_module._generated_test_relative_path(
        "20261120T120000Z-deadbeef", first
    )
    second_path = verifier_module._generated_test_relative_path(
        "20261120T120000Z-deadbeef", second
    )

    assert first_path.as_posix().casefold() == second_path.as_posix().casefold()
    with pytest.raises(VerificationError, match="generated test name collision"):
        verifier_module._require_unique_generated_test_paths(
            "20261120T120000Z-deadbeef",
            (SimpleNamespace(id=first), SimpleNamespace(id=second)),
        )


def test_bundle_publication_never_replaces_an_existing_empty_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()

    with pytest.raises(FileExistsError):
        verifier_module._write_bundle(
            output,
            manifest_bytes=b"{}\n",
            patch_bytes=b"patch\n",
            issue_bytes=b"issue\n",
            evidence_plan_bytes=b"{}\n",
            results_bytes=b"{}\n",
            frozen_tests={},
            cases=(),
            context_bytes=b"{}\n",
            report_bytes=b"report\n",
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []


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


def write_inputs(
    tmp_path: Path,
    *,
    schema_version: int = 1,
) -> tuple[Path, Path]:
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
    if schema_version == 2:
        candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        for case in cases:
            case["test_sha256"] = candidate_sha256
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "requirements": requirements,
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    return issue, evidence


def valid_context_payload() -> dict[str, object]:
    def snapshot(revision: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "analysis": "static",
            "revision": revision,
            "symbols": [
                {
                    "path": "value.py",
                    "qualified_name": "resolve",
                    "kind": "function",
                    "analyzed_path": "value.py",
                    "base_path": "value.py",
                    "patched_path": "value.py",
                    "base_occurrence": 1,
                    "patched_occurrence": 1,
                    "revision_match": "unique_identity",
                    "items": [
                        {
                            "kind": "signature",
                            "path": "value.py",
                            "start_line": 1,
                            "end_line": 1,
                            "symbol": "resolve",
                            "excerpt": "def resolve(value=None)",
                            "excerpt_truncated": False,
                            "score": 100,
                            "rank": 1,
                            "selection_reasons": [
                                {
                                    "code": "changed_definition",
                                    "detail": "signature of the changed definition",
                                    "weight": 100,
                                }
                            ],
                        }
                    ],
                    "limitations": ["symbol limitation"],
                }
            ],
            "diagnostics": [
                {
                    "code": "example",
                    "path": "value.py",
                    "message": "example diagnostic",
                    "line": None,
                }
            ],
            "limitations": ["snapshot limitation"],
        }

    return {
        "schema_version": 1,
        "base": snapshot("base"),
        "patched": snapshot("patched"),
        "summary": {
            "base": {
                "symbols": 1,
                "items": 1,
                "diagnostics": 1,
                "limitations": 2,
                "selection_reasons": 1,
            },
            "patched": {
                "symbols": 1,
                "items": 1,
                "diagnostics": 1,
                "limitations": 2,
                "selection_reasons": 1,
            },
        },
    }


def test_context_runtime_validator_accepts_schema_shaped_payload() -> None:
    payload = valid_context_payload()

    assert _validate_context_payload(payload) is payload


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("base", "diagnostics", 0, "line"), True, "diagnostic line"),
        (("base", "diagnostics", 0, "line"), 0, "diagnostic line"),
        (("base", "symbols", 0, "base_occurrence"), False, "base_occurrence"),
        (("base", "symbols", 0, "base_occurrence"), 0, "base_occurrence"),
        (("base", "symbols", 0, "items", 0, "start_line"), True, "start_line"),
        (("base", "symbols", 0, "items", 0, "start_line"), 0, "start_line"),
        (("base", "symbols", 0, "items", 0, "rank"), False, "rank"),
        (("base", "symbols", 0, "items", 0, "rank"), 0, "rank"),
        (("base", "symbols", 0, "items", 0, "kind"), "unknown", "kind"),
        (("base", "symbols", 0, "items", 0, "selection_reasons"), [], "non-empty"),
        (("base", "symbols", 0, "items", 0, "selection_reasons", 0, "code"), "", "non-empty"),
        (("summary", "base", "symbols"), True, "non-negative integer"),
    ],
)
def test_context_runtime_validator_rejects_invalid_scalar_semantics(
    path: tuple[str | int, ...], value: object, message: str
) -> None:
    payload = valid_context_payload()
    target: object = payload
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ValueError, match=message):
        _validate_context_payload(payload)


def test_context_runtime_validator_rejects_reversed_line_span() -> None:
    payload = valid_context_payload()
    item = payload["base"]["symbols"][0]["items"][0]  # type: ignore[index]
    item["start_line"] = 3
    item["end_line"] = 2

    with pytest.raises(ValueError, match="end_line must not precede start_line"):
        _validate_context_payload(payload)


@pytest.mark.parametrize(
    "target",
    ["snapshot", "symbol", "item", "reason", "diagnostic", "summary_counts"],
)
def test_context_runtime_validator_requires_exact_object_properties(
    target: str,
) -> None:
    payload = valid_context_payload()
    base = payload["base"]  # type: ignore[assignment]
    objects = {
        "snapshot": (base, "analysis"),
        "symbol": (base["symbols"][0], "path"),
        "item": (base["symbols"][0]["items"][0], "excerpt"),
        "reason": (
            base["symbols"][0]["items"][0]["selection_reasons"][0],
            "detail",
        ),
        "diagnostic": (base["diagnostics"][0], "line"),
        "summary_counts": (payload["summary"]["base"], "items"),  # type: ignore[index]
    }
    value, key = objects[target]
    del value[key]

    with pytest.raises(ValueError, match="unexpected or missing"):
        _validate_context_payload(payload)


@pytest.mark.parametrize(
    ("collection", "limit", "message"),
    [
        ("symbols", 200, "symbols must contain at most 200"),
        ("diagnostics", 200, "diagnostics must contain at most 200"),
        ("items", 100, "items must contain at most 100"),
    ],
)
def test_context_runtime_validator_enforces_collection_bounds(
    collection: str, limit: int, message: str
) -> None:
    payload = valid_context_payload()
    base = payload["base"]  # type: ignore[assignment]
    if collection == "items":
        values = base["symbols"][0]["items"]
    else:
        values = base[collection]
    values.extend([values[0]] * limit)

    with pytest.raises(ValueError, match=message):
        _validate_context_payload(payload)


def test_context_runtime_validator_caps_total_items_per_snapshot() -> None:
    payload = valid_context_payload()
    base = payload["base"]  # type: ignore[assignment]
    template = base["symbols"][0]
    symbols = []
    for index in range(6):
        symbol = copy.deepcopy(template)
        symbol["qualified_name"] = f"resolve_{index}"
        symbol["items"] = [copy.deepcopy(template["items"][0]) for _ in range(84)]
        symbols.append(symbol)
    base["symbols"] = symbols

    with pytest.raises(ValueError, match="at most 500 total items"):
        _validate_context_payload(payload)


def test_generated_context_uses_the_replay_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_CONTEXT_ARTIFACT_BYTES", 1)

    with pytest.raises(
        VerificationError,
        match="generated repository context exceeds the 1-byte limit",
    ):
        verifier_module._context_document_bytes(valid_context_payload())


def test_verification_request_rejects_excessive_repeats(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repeat must be between 1 and 5"):
        VerificationRequest(
            repository=tmp_path,
            base_revision="HEAD",
            patch_file=tmp_path / "patch.diff",
            issue_file=tmp_path / "issue.md",
            evidence_file=tmp_path / "evidence.json",
            output_directory=tmp_path / "run",
            repeat=6,
        )


def test_verification_request_rejects_excessive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no more than 3600"):
        VerificationRequest(
            repository=tmp_path,
            base_revision="HEAD",
            patch_file=tmp_path / "patch.diff",
            issue_file=tmp_path / "issue.md",
            evidence_file=tmp_path / "evidence.json",
            output_directory=tmp_path / "run",
            timeout_seconds=3601,
        )


def test_verify_rejects_excessive_case_repetitions_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = tmp_path / "patch.diff"
    issue = tmp_path / "issue.md"
    evidence = tmp_path / "evidence.json"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    issue.write_text("requirement\n", encoding="utf-8")
    evidence.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        verifier_module,
        "load_evidence_plan",
        lambda *args, **kwargs: SimpleNamespace(cases=(object(),) * 11),
    )

    with pytest.raises(
        VerificationError,
        match="55 case repetitions; the limit is 50",
    ):
        verify(
            VerificationRequest(
                repository=tmp_path,
                base_revision="HEAD",
                patch_file=patch,
                issue_file=issue,
                evidence_file=evidence,
                output_directory=tmp_path / "run",
                repeat=5,
            )
        )


def test_local_and_bundle_reads_stop_at_the_byte_limit(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"ab")

    with pytest.raises(VerificationError, match="exceeds the 1-byte limit"):
        verifier_module._read_bytes(
            oversized,
            "test artifact",
            max_bytes=1,
        )
    with pytest.raises(VerificationError, match="exceeds the 1-byte limit"):
        verifier_module._read_bundle_bytes(
            oversized,
            tmp_path,
            "test artifact",
            max_bytes=1,
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_local_and_bundle_reads_reject_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "artifact"
    os.mkfifo(fifo)

    started = time.perf_counter()
    with pytest.raises(VerificationError, match="not a regular file"):
        verifier_module._read_bytes(fifo, "test artifact", max_bytes=100)
    with pytest.raises(VerificationError, match="not a regular file"):
        verifier_module._read_bundle_bytes(
            fifo,
            tmp_path,
            "test artifact",
            max_bytes=100,
        )
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("label", ["results", "report", "manifest"])
def test_generated_documents_have_a_hard_byte_limit(label: str) -> None:
    with pytest.raises(
        VerificationError,
        match=f"generated {label} exceeds the 1-byte limit",
    ):
        verifier_module._bounded_json_document(
            {"value": "too large"},
            label=label,
            max_bytes=1,
        )


def test_context_report_escapes_and_bounds_inline_repository_text() -> None:
    payload = valid_context_payload()
    base = payload["base"]  # type: ignore[assignment]
    symbol = base["symbols"][0]
    item = symbol["items"][0]
    item["path"] = "repo`name.md\n# forged-path " + "P" * 700
    symbol["qualified_name"] = "resolve`\n## forged-symbol " + "Q" * 500
    item["selection_reasons"][0]["detail"] = (
        "<script id=reason>bad()</script> [jump](javascript:bad()) "
        + "R" * 500
    )
    base["limitations"][0] = (
        "<img id=limitation src=x onerror=bad()> **forged** " + "L" * 500
    )
    base["diagnostics"][0]["message"] = (
        "<script id=diagnostic>bad()</script> [jump](javascript:bad()) "
        + "D" * 500
    )

    report = "\n".join(verifier_module._render_context_section(payload))

    assert "&lt;script id=reason&gt;" in report
    assert "&lt;img id=limitation" in report
    assert "&lt;script id=diagnostic&gt;" in report
    assert "\\[jump\\]\\(javascript:bad\\(\\)\\)" in report
    assert "\n# forged-path" not in report
    assert "\n## forged-symbol" not in report
    assert "`` repo`name.md" in report
    assert "P" * 600 not in report
    assert "Q" * 400 not in report
    assert "R" * 400 not in report
    assert "D" * 400 not in report
    assert "truncated after 500 characters" in report
    assert "truncated after 300 characters" in report


def test_end_to_end_finds_overfitted_zero_patch(tmp_path: Path) -> None:
    repository, patch = make_target(
        tmp_path,
        proposed_return='"""Patched-only context."""\n    return 0',
    )
    issue, evidence = write_inputs(tmp_path, schema_version=2)
    issue_excerpt = (
        "Explicit zero must return zero. ``` <img id=excerpt src=x onerror=bad()>\n"
        "# forged excerpt heading\n"
        + "E" * 2500
        + "END-OF-OVERSIZED-EXCERPT"
    )
    issue.write_text(
        issue_excerpt
        + "\nExisting behavior for other values must remain unchanged.\n",
        encoding="utf-8",
    )
    evidence_payload = json.loads(evidence.read_text(encoding="utf-8"))
    evidence_payload["requirements"][0]["statement"] = (
        "<script id=statement>bad()</script> **forged** "
        "[click](javascript:bad()) "
        + "S" * 600
        + "END-OF-OVERSIZED-STATEMENT"
    )
    evidence_payload["requirements"][0]["excerpt"] = issue_excerpt
    evidence.write_text(json.dumps(evidence_payload), encoding="utf-8")
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
    context = json.loads((output / "context.json").read_text(encoding="utf-8"))
    report_bytes = (output / "report.md").read_bytes()
    report = report_bytes.decode("utf-8")
    assert manifest["schema_version"] == 2
    assert manifest["base_commit"] == git(repository, "rev-parse", "HEAD")
    assert len(manifest["evidence_plan_sha256"]) == 64
    assert len(manifest["results_sha256"]) == 64
    assert len(manifest["context_sha256"]) == 64
    assert len(manifest["report_sha256"]) == 64
    assert hashlib.sha256(report_bytes).hexdigest() == manifest["report_sha256"]
    assert result.context_sha256 == manifest["context_sha256"]
    assert result.results_sha256 == manifest["results_sha256"]
    assert all("--runxfail" in case["command"] for case in manifest["cases"])
    assert all(
        any(argument.startswith("--junitxml=") for argument in case["command"])
        for case in manifest["cases"]
    )
    assert len(results["cases"]) == 3
    assert context["schema_version"] == 1
    assert context["base"]["revision"] == "base"
    assert context["patched"]["revision"] == "patched"
    assert context["summary"]["base"]["symbols"] == 1
    assert context["summary"]["patched"]["symbols"] == 1
    base_excerpts = [
        item["excerpt"]
        for symbol in context["base"]["symbols"]
        for item in symbol["items"]
    ]
    patched_excerpts = [
        item["excerpt"]
        for symbol in context["patched"]["symbols"]
        for item in symbol["items"]
    ]
    assert all("Patched-only context" not in item for item in base_excerpts)
    assert any("Patched-only context" in item for item in patched_excerpts)
    assert "Found 2 supported patch failure(s)." in report
    assert "REQ-THREE" in report
    assert "Exact command:" in report
    assert "Execution evidence:" in report
    assert "## Repository context" in report
    assert "Base and patched snapshots are separate evidence domains" in report
    assert "Selection:" in report
    assert "<script id=statement>" not in report
    assert "&lt;script id=statement&gt;" in report
    assert "\\[click\\]\\(javascript:bad\\(\\)\\)" in report
    assert "END-OF-OVERSIZED-STATEMENT" not in report
    assert "truncated after 300 characters" in report
    assert "````text" in report
    assert "# forged excerpt heading" in report
    assert "END-OF-OVERSIZED-EXCERPT" not in report
    assert "excerpt truncated after 2000 characters" in report
    assert (output / "patch.diff").read_bytes() == patch.read_bytes()
    assert (output / "issue.md").read_bytes() == issue.read_bytes()
    assert len(list((output / "tests").glob("test_*.py"))) == 3

    bundled_plan = json.loads(
        (output / "evidence-plan.json").read_text(encoding="utf-8")
    )
    assert bundled_plan["schema_version"] == 2
    assert all(
        case["test_sha256"] == manifest_case["test_sha256"]
        for case, manifest_case in zip(
            bundled_plan["cases"], manifest["cases"]
        )
    )
    assert bundled_plan["cases"][0]["test_file"].startswith("tests/test_")

    replay_artifacts = (
        ("patch.diff", "patch"),
        ("issue.md", "issue"),
        ("evidence-plan.json", "evidence plan"),
        (bundled_plan["cases"][0]["test_file"], "bundled test"),
    )
    swap_request = request_from_bundle(
        output,
        output_directory=tmp_path / "replay-swap",
    )
    saved_artifacts = {
        relative: (output / relative).read_bytes()
        for relative, _label in replay_artifacts
    }
    try:
        for relative, _label in replay_artifacts:
            artifact = output / relative
            artifact.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(artifact)
            else:
                artifact.symlink_to(tmp_path / "missing-after-authentication")
        frozen_replay = verify(swap_request)
    finally:
        for relative, _label in replay_artifacts:
            artifact = output / relative
            artifact.unlink(missing_ok=True)
            artifact.write_bytes(saved_artifacts[relative])
    assert [item.kind for item in frozen_replay.findings] == [
        FindingKind.CRITERION_PASSES,
        FindingKind.SUPPORTED_REGRESSION,
        FindingKind.SUPPORTED_REGRESSION,
    ]

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

    replay_context_path = reproduced_output / "context.json"
    replay_context_bytes = replay_context_path.read_bytes()
    changed_context = json.loads(replay_context_bytes.decode("utf-8"))
    changed_context["base"]["limitations"][0] += " Reproduction drift."
    changed_context_bytes = (
        json.dumps(changed_context, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    replay_context_path.write_bytes(changed_context_bytes)
    changed_context_result = replace(
        reproduced,
        context_sha256=hashlib.sha256(changed_context_bytes).hexdigest(),
    )
    replay_manifest_path = reproduced_output / "manifest.json"
    replay_manifest_bytes = replay_manifest_path.read_bytes()
    changed_replay_manifest = json.loads(replay_manifest_bytes.decode("utf-8"))
    changed_replay_manifest["context_sha256"] = changed_context_result.context_sha256
    replay_manifest_path.write_text(
        json.dumps(changed_replay_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        assert (
            "repository context changed during reproduction"
            in reproduction_differences(output, changed_context_result)
        )
    finally:
        replay_context_path.write_bytes(replay_context_bytes)
        replay_manifest_path.write_bytes(replay_manifest_bytes)

    replay_results_path = reproduced_output / "results.json"
    replay_results_bytes = replay_results_path.read_bytes()
    replay_results = json.loads(replay_results_path.read_text(encoding="utf-8"))
    replay_results["cases"][0]["finding"]["kind"] = "inconclusive"
    replay_results_path.write_text(json.dumps(replay_results), encoding="utf-8")
    with pytest.raises(
        VerificationError,
        match="reproduced results changed after verification",
    ):
        reproduction_differences(output, reproduced)
    replay_results_path.write_bytes(replay_results_bytes)
    changed_snapshot = replace(result.snapshot, patched_tree_hash="0" * 40)
    assert reproduction_differences(
        output, replace(result, snapshot=changed_snapshot)
    )

    for relative in (
        "patch.diff",
        "issue.md",
        "evidence-plan.json",
        "results.json",
        "context.json",
        "report.md",
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

    bundled_context = output / "context.json"
    context_bytes = bundled_context.read_bytes()
    external_context = tmp_path / "external-context.json"
    external_context.write_bytes(context_bytes)
    bundled_context.unlink()
    bundled_context.symlink_to(external_context)
    try:
        with pytest.raises(VerificationError, match="symbolic link"):
            request_from_bundle(output)
    finally:
        bundled_context.unlink()
        bundled_context.write_bytes(context_bytes)

    bundled_report = output / "report.md"
    saved_report_bytes = bundled_report.read_bytes()
    external_report = tmp_path / "external-report.md"
    external_report.write_bytes(saved_report_bytes)
    bundled_report.unlink()
    bundled_report.symlink_to(external_report)
    try:
        with pytest.raises(VerificationError, match="symbolic link"):
            request_from_bundle(output)
    finally:
        bundled_report.unlink()
        bundled_report.write_bytes(saved_report_bytes)

    manifest_path = output / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    missing_tree_manifest = json.loads(manifest_bytes.decode("utf-8"))
    missing_tree_manifest.pop("patched_tree")
    manifest_path.write_text(
        json.dumps(missing_tree_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="invalid bundle manifest.*patched_tree",
        ):
            request_from_bundle(output)
    finally:
        manifest_path.write_bytes(manifest_bytes)

    mismatched_requirements = json.loads(manifest_bytes.decode("utf-8"))
    mismatched_requirements["requirements"][0]["statement"] += " Forged."
    manifest_path.write_text(
        json.dumps(mismatched_requirements, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="manifest and evidence-plan requirements differ",
        ):
            request_from_bundle(output)
    finally:
        manifest_path.write_bytes(manifest_bytes)

    mismatched_finding = json.loads(manifest_bytes.decode("utf-8"))
    mismatched_finding["cases"][0]["finding"] = "supported_regression"
    manifest_path.write_text(
        json.dumps(mismatched_finding, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="results finding differs",
        ):
            request_from_bundle(output)
    finally:
        manifest_path.write_bytes(manifest_bytes)

    mismatched_scope = json.loads(manifest_bytes.decode("utf-8"))
    mismatched_scope["scope"]["changed_symbols"][0][
        "qualified_name"
    ] = "forged"
    manifest_path.write_text(
        json.dumps(mismatched_scope, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="context symbol is absent from manifest scope",
        ):
            request_from_bundle(output)
    finally:
        manifest_path.write_bytes(manifest_bytes)

    extra_scope_symbol = json.loads(manifest_bytes.decode("utf-8"))
    forged_symbol = copy.deepcopy(
        extra_scope_symbol["scope"]["changed_symbols"][0]
    )
    forged_symbol["qualified_name"] = "forged"
    extra_scope_symbol["scope"]["changed_symbols"].append(forged_symbol)
    manifest_path.write_text(
        json.dumps(extra_scope_symbol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        scope_request = request_from_bundle(
            output,
            output_directory=tmp_path / "replay-forged-scope",
        )
        with pytest.raises(
            VerificationError,
            match="recomputed change scope differs",
        ):
            verify(scope_request)
        assert not scope_request.output_directory.exists()
    finally:
        manifest_path.write_bytes(manifest_bytes)

    mismatched_files = json.loads(manifest_bytes.decode("utf-8"))
    mismatched_files["scope"]["changed_files"][0]["old_path"] = "forged.py"
    manifest_path.write_text(
        json.dumps(mismatched_files, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="changed-file scope differs",
        ):
            request_from_bundle(output)
    finally:
        manifest_path.write_bytes(manifest_bytes)

    results_path = output / "results.json"
    saved_results_bytes = results_path.read_bytes()
    malformed_results = json.loads(saved_results_bytes.decode("utf-8"))
    del malformed_results["cases"][0]["patched"]["attempts"]
    malformed_results_bytes = (
        json.dumps(malformed_results, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    results_path.write_bytes(malformed_results_bytes)
    malformed_results_manifest = json.loads(manifest_bytes.decode("utf-8"))
    malformed_results_manifest["results_sha256"] = hashlib.sha256(
        malformed_results_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(malformed_results_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="invalid bundle artifact mapping.*patched.*missing properties",
        ):
            request_from_bundle(output)
    finally:
        results_path.write_bytes(saved_results_bytes)
        manifest_path.write_bytes(manifest_bytes)

    invalid_attempt_results = json.loads(saved_results_bytes.decode("utf-8"))
    invalid_attempt_results["cases"][0]["base"]["attempts"][0][
        "attempt"
    ] = True
    invalid_attempt_bytes = (
        json.dumps(invalid_attempt_results, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    results_path.write_bytes(invalid_attempt_bytes)
    invalid_attempt_manifest = json.loads(manifest_bytes.decode("utf-8"))
    invalid_attempt_manifest["results_sha256"] = hashlib.sha256(
        invalid_attempt_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(invalid_attempt_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(VerificationError, match="attempt numbering is invalid"):
            request_from_bundle(output)
    finally:
        results_path.write_bytes(saved_results_bytes)
        manifest_path.write_bytes(manifest_bytes)

    mismatched_argv_results = json.loads(saved_results_bytes.decode("utf-8"))
    mismatched_argv_results["cases"][0]["base"]["attempts"][0]["argv"] = [
        "forged-command"
    ]
    mismatched_argv_bytes = (
        json.dumps(mismatched_argv_results, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    results_path.write_bytes(mismatched_argv_bytes)
    mismatched_argv_manifest = json.loads(manifest_bytes.decode("utf-8"))
    mismatched_argv_manifest["results_sha256"] = hashlib.sha256(
        mismatched_argv_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(mismatched_argv_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(VerificationError, match="argv differs"):
            request_from_bundle(output)
    finally:
        results_path.write_bytes(saved_results_bytes)
        manifest_path.write_bytes(manifest_bytes)

    def assert_rehashed_results_rejected(
        payload: dict[str, object], pattern: str
    ) -> None:
        changed_bytes = (
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        results_path.write_bytes(changed_bytes)
        changed_manifest = json.loads(manifest_bytes.decode("utf-8"))
        changed_manifest["results_sha256"] = hashlib.sha256(
            changed_bytes
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(changed_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            with pytest.raises(VerificationError, match=pattern):
                request_from_bundle(output)
        finally:
            results_path.write_bytes(saved_results_bytes)
            manifest_path.write_bytes(manifest_bytes)

    inconsistent_exit = json.loads(saved_results_bytes.decode("utf-8"))
    inconsistent_exit["cases"][0]["patched"]["attempts"][0]["exit_code"] = 1
    assert_rehashed_results_rejected(inconsistent_exit, "exit code is inconsistent")

    invalid_signature = json.loads(saved_results_bytes.decode("utf-8"))
    invalid_signature["cases"][0]["base"]["attempts"][0][
        "failure_signature"
    ] = "not-a-digest"
    assert_rehashed_results_rejected(invalid_signature, "failure signature is invalid")

    forged_detail = json.loads(saved_results_bytes.decode("utf-8"))
    forged_detail["cases"][0]["finding"]["observation"]["patched"][
        "detail"
    ] = "forged detail"
    assert_rehashed_results_rejected(forged_detail, "patched detail differs")

    oversized_context_bytes = context_bytes + b" " * (
        verifier_module._MAX_CONTEXT_ARTIFACT_BYTES - len(context_bytes) + 1
    )
    bundled_context.write_bytes(oversized_context_bytes)
    oversized_manifest = json.loads(manifest_bytes.decode("utf-8"))
    oversized_manifest["context_sha256"] = hashlib.sha256(
        oversized_context_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(oversized_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="repository context exceeds the .*byte limit",
        ):
            request_from_bundle(output)
    finally:
        bundled_context.write_bytes(context_bytes)
        manifest_path.write_bytes(manifest_bytes)

    invalid_context = json.loads(context_bytes.decode("utf-8"))
    invalid_context["base"]["revision"] = "patched"
    invalid_context_bytes = (
        json.dumps(invalid_context, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    bundled_context.write_bytes(invalid_context_bytes)
    invalid_manifest = json.loads(manifest_bytes.decode("utf-8"))
    invalid_manifest["context_sha256"] = hashlib.sha256(
        invalid_context_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(invalid_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(VerificationError, match="invalid bundled repository context"):
            request_from_bundle(output)
    finally:
        bundled_context.write_bytes(context_bytes)
        manifest_path.write_bytes(manifest_bytes)

    missing_diagnostic_field = json.loads(context_bytes.decode("utf-8"))
    missing_diagnostic_field["base"]["diagnostics"].append(
        {
            "code": "tampered",
            "path": ".",
            "message": "the required line field was removed",
        }
    )
    missing_diagnostic_field["summary"]["base"]["diagnostics"] += 1
    missing_diagnostic_bytes = (
        json.dumps(missing_diagnostic_field, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    bundled_context.write_bytes(missing_diagnostic_bytes)
    invalid_manifest = json.loads(manifest_bytes.decode("utf-8"))
    invalid_manifest["context_sha256"] = hashlib.sha256(
        missing_diagnostic_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(invalid_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(
            VerificationError,
            match="diagnostic has unexpected or missing properties",
        ):
            request_from_bundle(output)
    finally:
        bundled_context.write_bytes(context_bytes)
        manifest_path.write_bytes(manifest_bytes)

    legacy_run = tmp_path / "legacy-v1-run"
    shutil.copytree(output, legacy_run)
    legacy_manifest_path = legacy_run / "manifest.json"
    legacy_manifest = json.loads(
        legacy_manifest_path.read_text(encoding="utf-8")
    )
    legacy_manifest["schema_version"] = 1
    legacy_manifest.pop("context_sha256")
    legacy_manifest.pop("report_sha256")
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (legacy_run / "context.json").unlink()
    legacy_replayed = verify(
        request_from_bundle(
            legacy_run,
            output_directory=tmp_path / "legacy-v1-reproduction",
        )
    )
    assert reproduction_differences(legacy_run, legacy_replayed) == ()

    incompatible_legacy = dict(legacy_manifest)
    incompatible_legacy["execution"] = dict(legacy_manifest["execution"])
    incompatible_legacy["execution"]["output_limit_bytes_per_stream"] = 1_000_000
    legacy_manifest_path.write_text(
        json.dumps(incompatible_legacy, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        VerificationError,
        match="output_limit_bytes_per_stream is incompatible",
    ):
        request_from_bundle(legacy_run)

    legacy_manifest.pop("patched_tree")
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        VerificationError,
        match="invalid legacy bundle manifest.*patched_tree",
    ):
        request_from_bundle(legacy_run)


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
