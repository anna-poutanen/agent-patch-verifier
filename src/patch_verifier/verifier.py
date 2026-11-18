"""End-to-end orchestration for deterministic patch verification."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

from . import __version__
from .change_analysis import ChangedSymbol, FileChange, analyze_changed_symbols, parse_unified_diff
from .classifier import classify
from .models import (
    Finding,
    FindingKind,
    Outcome,
    RevisionResult as EvidenceRevisionResult,
)
from .plan import CandidateCase, EvidencePlan, load_evidence_plan, verify_base_sources
from .runner import (
    CommandResult,
    DualRunResult,
    OUTPUT_LIMIT_BYTES,
    RevisionResult as RunnerRevisionResult,
    RunOptions,
    TestCommand,
    run_dual,
)
from .workspace import PatchWorkspace, WorkspaceSnapshot


class VerificationError(RuntimeError):
    """Raised when a verification request cannot be executed faithfully."""


_IMPORT_GUARD_SOURCE = b'''\
"""APV pytest harness: reject imports shadowed by installed packages."""

import ast
from pathlib import Path
import sys

import pytest


def _candidate_imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def _is_local_source(root, name):
    for source_root in (
        root,
        root / "src",
        root / "lib",
        root / "python",
        root / "source",
    ):
        if (source_root / f"{name}.py").is_file():
            return True
        if (source_root / name / "__init__.py").is_file():
            return True
    return False


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


@pytest.fixture(autouse=True)
def _apv_import_origin_guard(request):
    yield
    root = Path.cwd().resolve()
    shadowed = []
    for name in sorted(_candidate_imports(request.node.path)):
        if not _is_local_source(root, name):
            continue
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, "__file__", None)
        search = getattr(getattr(module, "__spec__", None), "submodule_search_locations", None)
        origins = ([origin] if origin else []) + list(search or [])
        if not origins or not all(_inside(item, root) for item in origins):
            shadowed.append(f"{name}={origins or ['unknown origin']}")
    if shadowed:
        pytest.fail("APV import-origin mismatch: " + ", ".join(shadowed))
'''


@dataclass(frozen=True)
class VerificationRequest:
    repository: Path
    base_revision: str
    patch_file: Path
    issue_file: Path
    evidence_file: Path
    output_directory: Path
    command_prefix: tuple[str, ...] = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
    )
    timeout_seconds: float = 60.0
    repeat: int = 2
    reproduction_manifest_sha256: str | None = None
    reproduction_results_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_revision, str):
            raise TypeError("base_revision must be a string")
        if isinstance(self.command_prefix, (str, bytes)) or not all(
            isinstance(item, str) for item in self.command_prefix
        ):
            raise TypeError("command_prefix must be a sequence of strings")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a number")
        if isinstance(self.repeat, bool) or not isinstance(self.repeat, int):
            raise TypeError("repeat must be an integer")
        object.__setattr__(self, "repository", Path(self.repository).resolve())
        object.__setattr__(self, "patch_file", Path(self.patch_file).resolve())
        object.__setattr__(self, "issue_file", Path(self.issue_file).resolve())
        object.__setattr__(self, "evidence_file", Path(self.evidence_file).resolve())
        object.__setattr__(
            self, "output_directory", Path(self.output_directory).resolve()
        )
        object.__setattr__(
            self, "command_prefix", tuple(self.command_prefix)
        )
        if not self.base_revision.strip():
            raise ValueError("base_revision must not be empty")
        if not self.command_prefix:
            raise ValueError("command_prefix must not be empty")
        if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if self.repeat < 1:
            raise ValueError("repeat must be at least one")
        for label, digest in (
            ("reproduction manifest", self.reproduction_manifest_sha256),
            ("reproduction results", self.reproduction_results_sha256),
        ):
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{label} SHA-256 is invalid")


@dataclass(frozen=True)
class CaseVerification:
    case: CandidateCase
    injected_path: str
    test_sha256: str
    command: tuple[str, ...]
    dual_result: DualRunResult
    finding: Finding


@dataclass(frozen=True)
class VerificationResult:
    output_directory: Path
    run_id: str
    snapshot: WorkspaceSnapshot
    findings: tuple[Finding, ...]
    changed_symbols: tuple[ChangedSymbol, ...]
    reproduction_manifest_sha256: str | None = None
    reproduction_results_sha256: str | None = None

    @property
    def supported_failures(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.is_supported_failure)

    @property
    def has_supported_failure(self) -> bool:
        return bool(self.supported_failures)

    @property
    def inconclusive_findings(self) -> tuple[Finding, ...]:
        return tuple(
            item for item in self.findings if item.kind is FindingKind.INCONCLUSIVE
        )

    @property
    def is_inconclusive(self) -> bool:
        return bool(self.inconclusive_findings)


def verify(request: VerificationRequest) -> VerificationResult:
    """Execute a complete verification run and write its evidence bundle."""

    patch_bytes = _read_bytes(request.patch_file, "patch")
    if b"\0" in patch_bytes or b"GIT binary patch" in patch_bytes:
        raise VerificationError("binary patches are outside the current MVP scope")
    try:
        patch_text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("patch must be UTF-8 text") from exc
    issue_bytes = _read_bytes(request.issue_file, "issue")
    try:
        issue_text = issue_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("issue file must be UTF-8") from exc
    plan = load_evidence_plan(request.evidence_file, issue_text=issue_text)
    if request.output_directory.exists():
        raise VerificationError(
            f"output directory already exists: {request.output_directory}"
        )

    run_id = _run_id(patch_bytes)
    changes = parse_unified_diff(patch_text)
    case_results: list[CaseVerification] = []
    frozen_tests = {case.id: case.source_file.read_bytes() for case in plan.cases}

    with PatchWorkspace(
        request.repository,
        request.base_revision,
        patch_text=patch_bytes,
    ) as workspace:
        verify_base_sources(plan, workspace.base_path)
        symbols = analyze_changed_symbols(
            workspace.base_path,
            workspace.patched_path,
            changes,
        )
        snapshot = workspace.snapshot

    options = RunOptions(
        timeout_seconds=request.timeout_seconds,
        repeat=1,
    )
    requirements = plan.requirement_by_id
    for case in plan.cases:
        base_attempts: list[CommandResult] = []
        patched_attempts: list[CommandResult] = []
        base_target = None
        patched_target = None
        relative_path = ""
        test_command: TestCommand | None = None
        for attempt in range(1, request.repeat + 1):
            # Every attempt gets newly materialized trees. Candidate tests may
            # mutate their cwd, so reusing a checkout would let one case or
            # repeat manufacture a later differential result.
            with PatchWorkspace(
                request.repository,
                snapshot.base_sha,
                patch_text=patch_bytes,
            ) as execution_workspace:
                _verify_snapshot_identity(snapshot, execution_workspace.snapshot)
                injected = _inject_tests(
                    run_id,
                    (case,),
                    frozen_tests,
                    execution_workspace.base_path,
                    execution_workspace.patched_path,
                )
                relative_path = injected[case.id]
                node_id = f"{relative_path}::{case.node}"
                command = (*request.command_prefix, node_id)
                junit_path = Path(relative_path).parent / f"junit-{case.id}.xml"
                test_command = TestCommand(
                    command,
                    junit_xml_path=junit_path,
                    expected_node=case.node,
                )
                current = run_dual(
                    execution_workspace.base_path,
                    execution_workspace.patched_path,
                    test_command,
                    options,
                    start_with_patched=attempt % 2 == 0,
                )
                if base_target is None:
                    base_target = current.base.target
                    patched_target = current.patched.target
                base_attempts.append(
                    replace(current.base.attempts[0], attempt=attempt)
                )
                patched_attempts.append(
                    replace(current.patched.attempts[0], attempt=attempt)
                )

        assert base_target is not None
        assert patched_target is not None
        assert test_command is not None
        dual = DualRunResult(
            base=RunnerRevisionResult(base_target, tuple(base_attempts)),
            patched=RunnerRevisionResult(patched_target, tuple(patched_attempts)),
        )
        finding = classify(
            requirements[case.requirement_id],
            _to_evidence_result(dual.base),
            _to_evidence_result(dual.patched),
            test_id=case.id,
        )
        case_results.append(
            CaseVerification(
                case=case,
                injected_path=relative_path,
                test_sha256=hashlib.sha256(frozen_tests[case.id]).hexdigest(),
                command=test_command.argv,
                dual_result=dual,
                finding=finding,
            )
        )

    manifest = _manifest(
        request=request,
        run_id=run_id,
        snapshot=snapshot,
        issue_bytes=issue_bytes,
        plan=plan,
        changes=changes,
        symbols=symbols,
        cases=case_results,
    )
    try:
        _write_bundle(
            request.output_directory,
            manifest=manifest,
            patch_bytes=patch_bytes,
            issue_bytes=issue_bytes,
            plan=plan,
            frozen_tests=frozen_tests,
            cases=case_results,
        )
    except OSError as exc:
        raise VerificationError(
            f"cannot write evidence bundle {request.output_directory}: {exc}"
        ) from exc
    findings = tuple(item.finding for item in case_results)
    return VerificationResult(
        output_directory=request.output_directory,
        run_id=run_id,
        snapshot=snapshot,
        findings=findings,
        changed_symbols=symbols,
        reproduction_manifest_sha256=request.reproduction_manifest_sha256,
        reproduction_results_sha256=request.reproduction_results_sha256,
    )


def _verify_snapshot_identity(
    expected: WorkspaceSnapshot, observed: WorkspaceSnapshot
) -> None:
    if observed.base_sha != expected.base_sha:
        raise VerificationError(
            f"base commit changed during verification: {observed.base_sha}"
        )
    if observed.patch_sha256 != expected.patch_sha256:
        raise VerificationError("patch bytes changed during verification")
    if observed.patched_tree_hash != expected.patched_tree_hash:
        raise VerificationError(
            "patch materialized to a different Git tree during verification: "
            f"expected {expected.patched_tree_hash}, "
            f"observed {observed.patched_tree_hash}"
        )


def default_output_directory(repository: Path) -> Path:
    repository = Path(repository).resolve()
    environment = os.environ.copy()
    for key in PatchWorkspace._GIT_REPOSITORY_ENVIRONMENT:
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(repository),
                "rev-parse",
                "--git-path",
                "patch-verifier/runs",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=PatchWorkspace._GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise VerificationError("Git timed out while locating repository metadata") from exc
    except OSError as exc:
        raise VerificationError(f"cannot locate repository metadata: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git rev-parse failed"
        raise VerificationError(f"cannot locate repository metadata: {detail}")
    storage = Path(completed.stdout.strip())
    if not storage.is_absolute():
        storage = repository / storage
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return storage.resolve() / timestamp


def default_reproduction_output(run_directory: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_directory = Path(run_directory).resolve()
    return run_directory.parent / f"{run_directory.name}-reproduction-{timestamp}"


def request_from_bundle(
    run_directory: Path,
    *,
    output_directory: Path | None = None,
    repository: Path | None = None,
    command_prefix_override: tuple[str, ...] | None = None,
) -> VerificationRequest:
    """Reconstruct a request after validating all recorded input artifacts.

    Hashes detect accidental or post-run mutation relative to the manifest.
    They do not authenticate the manifest itself, so a bundle must still come
    from a trusted source before its tests are executed.
    """

    run_directory = Path(run_directory).resolve()
    manifest_path = run_directory / "manifest.json"
    _require_bundle_file(manifest_path, run_directory, "manifest")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        schema_version = manifest["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError("unsupported schema_version")
        execution = manifest["execution"]
        recorded_repository = Path(manifest["repository"])
        base_commit = str(manifest["base_commit"])
        raw_command = execution["command_prefix"]
        if not isinstance(raw_command, list) or not all(
            isinstance(item, str) for item in raw_command
        ):
            raise ValueError("execution.command_prefix must be an array of strings")
        command_prefix = tuple(raw_command)
        timeout_seconds = float(execution["timeout_seconds"])
        repeat_value = execution["repeat"]
        if isinstance(repeat_value, bool) or not isinstance(repeat_value, int):
            raise ValueError("execution.repeat must be an integer")
        repeat = repeat_value
    except OSError as exc:
        raise VerificationError(f"cannot read bundle manifest {manifest_path}: {exc}") from exc
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise VerificationError(f"invalid bundle manifest {manifest_path}: {exc}") from exc
    _validate_bundle_inputs(run_directory, manifest)
    output = output_directory or default_reproduction_output(run_directory)
    return VerificationRequest(
        repository=repository or recorded_repository,
        base_revision=base_commit,
        patch_file=run_directory / "patch.diff",
        issue_file=run_directory / "issue.md",
        evidence_file=run_directory / "evidence-plan.json",
        output_directory=output,
        command_prefix=(
            command_prefix
            if command_prefix_override is None
            else command_prefix_override
        ),
        timeout_seconds=timeout_seconds,
        repeat=repeat,
        reproduction_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        reproduction_results_sha256=str(manifest["results_sha256"]),
    )


def reproduction_differences(
    run_directory: Path, result: VerificationResult
) -> tuple[str, ...]:
    """Compare a replay's classifications with the bundle's recorded results."""

    run_root = Path(run_directory).resolve()
    results_path = run_root / "results.json"
    manifest_path = run_root / "manifest.json"
    replay_results_path = result.output_directory / "results.json"
    try:
        manifest_bytes = _require_bundle_file(
            manifest_path, run_root, "manifest"
        ).read_bytes()
        results_bytes = _require_bundle_file(
            results_path, run_root, "recorded results"
        ).read_bytes()
        if (
            result.reproduction_manifest_sha256 is not None
            and hashlib.sha256(manifest_bytes).hexdigest()
            != result.reproduction_manifest_sha256
        ):
            raise VerificationError("recorded manifest changed during reproduction")
        if (
            result.reproduction_results_sha256 is not None
            and hashlib.sha256(results_bytes).hexdigest()
            != result.reproduction_results_sha256
        ):
            raise VerificationError("recorded results changed during reproduction")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        _validate_bundle_inputs(run_root, manifest)
        payload = json.loads(results_bytes.decode("utf-8"))
        replay_payload = json.loads(
            replay_results_path.read_text(encoding="utf-8")
        )
        recorded = _case_execution_signatures(payload)
        replayed = _case_execution_signatures(replay_payload)
    except VerificationError:
        raise
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise VerificationError(
            f"cannot compare reproduction with {results_path}: {exc}"
        ) from exc
    differences: list[str] = []
    if result.snapshot.base_sha != str(manifest["base_commit"]):
        differences.append(
            f"base commit changed from {manifest['base_commit']} "
            f"to {result.snapshot.base_sha}"
        )
    if result.snapshot.patched_tree_hash != str(manifest["patched_tree"]):
        differences.append(
            f"patched tree changed from {manifest['patched_tree']} "
            f"to {result.snapshot.patched_tree_hash}"
        )
    if len(recorded) != len(replayed):
        differences.append(
            f"case count changed from {len(recorded)} to {len(replayed)}"
        )
    for index, (before, after) in enumerate(zip(recorded, replayed), start=1):
        if before != after:
            differences.append(
                f"case {index} changed from {before[0]}={before[1]} "
                f"base{before[2]} patched{before[3]} to "
                f"{after[0]}={after[1]} base{after[2]} patched{after[3]}"
            )
    return tuple(differences)


def _case_execution_signatures(payload: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    cases = payload["cases"]
    if not isinstance(cases, list):
        raise TypeError("cases must be an array")
    return [
        (
            str(item["id"]),
            str(item["finding"]["kind"]),
            _raw_revision_signature(item["base"]),
            _raw_revision_signature(item["patched"]),
        )
        for item in cases
    ]


def _raw_revision_signature(value: Mapping[str, Any]) -> tuple[Any, ...]:
    attempts = value["attempts"]
    if not isinstance(attempts, list):
        raise TypeError("recorded attempts must be an array")
    return (
        bool(value["stable"]),
        None if value["status"] is None else str(value["status"]),
        tuple(
            (
                int(item["attempt"]),
                str(item["status"]),
                item["exit_code"],
                item.get("failure_signature"),
                bool(item.get("stdout_truncated", False)),
                bool(item.get("stderr_truncated", False)),
            )
            for item in attempts
        ),
    )


def _validate_bundle_inputs(
    run_directory: Path, manifest: Mapping[str, Any]
) -> None:
    artifacts = (
        ("patch.diff", "patch_sha256", "patch"),
        ("issue.md", "issue_sha256", "issue"),
        ("evidence-plan.json", "evidence_plan_sha256", "evidence plan"),
        ("results.json", "results_sha256", "recorded results"),
        ("harness/conftest.py", "harness_sha256", "pytest harness"),
    )
    for filename, hash_key, label in artifacts:
        expected = manifest.get(hash_key)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise VerificationError(f"bundle manifest has no valid {hash_key}")
        _require_hash(run_directory / filename, expected, label, run_directory)
    current_harness_hash = hashlib.sha256(_IMPORT_GUARD_SOURCE).hexdigest()
    if manifest.get("harness_sha256") != current_harness_hash:
        raise VerificationError(
            "the installed verifier harness differs from the recorded bundle"
        )

    plan_path = run_directory / "evidence-plan.json"
    try:
        plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_cases = plan_payload["cases"]
        manifest_cases = manifest["cases"]
        if not isinstance(plan_cases, list) or not isinstance(manifest_cases, list):
            raise TypeError("cases must be arrays")
        plan_by_id = {str(item["id"]): item for item in plan_cases}
        if len(plan_by_id) != len(plan_cases):
            raise ValueError("duplicate case ids in evidence plan")
        if {str(item["id"]) for item in manifest_cases} != set(plan_by_id):
            raise ValueError("manifest and evidence-plan case ids differ")
        for recorded in manifest_cases:
            case_id = str(recorded["id"])
            planned = plan_by_id[case_id]
            if str(recorded["requirement_id"]) != str(planned["requirement_id"]):
                raise ValueError(f"requirement id differs for case {case_id}")
            relative = Path(str(planned["test_file"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe test path for case {case_id}")
            test_path = run_directory / relative
            test_hash = recorded.get("test_sha256")
            if not isinstance(test_hash, str):
                raise ValueError(f"missing test hash for case {case_id}")
            _require_hash(
                test_path,
                test_hash,
                f"test for case {case_id}",
                run_directory,
            )
    except OSError as exc:
        raise VerificationError(f"cannot inspect bundle evidence plan: {exc}") from exc
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise VerificationError(f"invalid bundle artifact mapping: {exc}") from exc


def _require_hash(
    path: Path,
    expected: str,
    label: str,
    bundle_root: Path,
) -> None:
    safe_path = _require_bundle_file(path, bundle_root, label)
    try:
        observed = hashlib.sha256(safe_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise VerificationError(
            f"cannot read bundled {label} {safe_path}: {exc}"
        ) from exc
    if observed != expected:
        raise VerificationError(
            f"bundled {label} hash mismatch: expected {expected}, observed {observed}"
        )


def _require_bundle_file(path: Path, bundle_root: Path, label: str) -> Path:
    if path.is_symlink():
        raise VerificationError(f"bundled {label} must not be a symbolic link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"cannot resolve bundled {label} {path}: {exc}") from exc
    try:
        resolved.relative_to(bundle_root)
    except ValueError as exc:
        raise VerificationError(f"bundled {label} escapes the bundle directory") from exc
    if not resolved.is_file():
        raise VerificationError(f"bundled {label} is not a regular file: {path}")
    return resolved


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"cannot read {label} file {path}: {exc}") from exc
    if not data.strip():
        raise VerificationError(f"{label} file is empty: {path}")
    return data


def _run_id(patch_bytes: bytes) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{hashlib.sha256(patch_bytes).hexdigest()[:8]}"


def _inject_tests(
    run_id: str,
    cases: Sequence[CandidateCase],
    frozen_tests: Mapping[str, bytes],
    base_root: Path,
    patched_root: Path,
) -> dict[str, str]:
    relative_root = Path(f".patch_verifier_generated_{run_id}")
    result: dict[str, str] = {}
    for root in (base_root, patched_root):
        target_root = root / relative_root
        if target_root.exists():
            raise VerificationError(f"generated-test path already exists: {target_root}")
        target_root.mkdir()
        (target_root / "conftest.py").write_bytes(_IMPORT_GUARD_SOURCE)

    used_names: set[str] = set()
    for case in cases:
        safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", case.id).strip("_") or "case"
        digest = hashlib.sha256(case.id.encode("utf-8")).hexdigest()[:8]
        name = f"test_{safe_id}_{digest}.py"
        if name in used_names:  # pragma: no cover - digest makes this implausible
            raise VerificationError(f"generated test name collision for {case.id!r}")
        used_names.add(name)
        relative = relative_root / name
        contents = frozen_tests[case.id]
        for root in (base_root, patched_root):
            (root / relative).write_bytes(contents)
        result[case.id] = relative.as_posix()
    return result


def _to_evidence_result(result: RunnerRevisionResult) -> EvidenceRevisionResult:
    attempts = tuple(Outcome(item.status.value) for item in result.attempts)
    representative = attempts[0]
    detail = ", ".join(
        f"attempt {item.attempt}: {item.status.value}"
        + (f" (exit {item.exit_code})" if item.exit_code is not None else "")
        for item in result.attempts
    )
    return EvidenceRevisionResult(
        outcome=representative,
        stable=result.is_stable,
        attempts=attempts,
        detail=detail,
    )


def _manifest(
    *,
    request: VerificationRequest,
    run_id: str,
    snapshot: WorkspaceSnapshot,
    issue_bytes: bytes,
    plan: EvidencePlan,
    changes: Sequence[FileChange],
    symbols: Sequence[ChangedSymbol],
    cases: Sequence[CaseVerification],
) -> dict[str, Any]:
    bundled_plan = _bundled_plan(plan, cases)
    results_payload = _results_payload(cases)
    return {
        "schema_version": 1,
        "tool": {"name": "agent-patch-verifier", "version": __version__},
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(snapshot.repository),
        "requested_base_revision": request.base_revision,
        "base_commit": snapshot.base_sha,
        "patched_tree": snapshot.patched_tree_hash,
        "patch_sha256": snapshot.patch_sha256,
        "issue_sha256": hashlib.sha256(issue_bytes).hexdigest(),
        "evidence_plan_sha256": hashlib.sha256(
            _json_document(bundled_plan)
        ).hexdigest(),
        "results_sha256": hashlib.sha256(
            _json_document(results_payload)
        ).hexdigest(),
        "harness_sha256": hashlib.sha256(_IMPORT_GUARD_SOURCE).hexdigest(),
        "environment": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": sys.platform,
        },
        "execution": {
            "command_prefix": list(request.command_prefix),
            "timeout_seconds": request.timeout_seconds,
            "repeat": request.repeat,
            "output_limit_bytes_per_stream": OUTPUT_LIMIT_BYTES,
            "warning": "Local subprocess execution is not a security sandbox.",
        },
        "scope": {
            "changed_files": [
                {
                    "old_path": item.old_path,
                    "new_path": item.new_path,
                    "old_lines": sorted(item.old_lines),
                    "new_lines": sorted(item.new_lines),
                }
                for item in changes
            ],
            "changed_symbols": [item.to_dict() for item in symbols],
        },
        "requirements": [item.to_dict() for item in plan.requirements],
        "cases": [
            {
                "id": item.case.id,
                "requirement_id": item.case.requirement_id,
                "injected_path": item.injected_path,
                "test_sha256": item.test_sha256,
                "command": list(item.command),
                "finding": item.finding.kind.value,
            }
            for item in cases
        ],
    }


def _write_bundle(
    output: Path,
    *,
    manifest: Mapping[str, Any],
    patch_bytes: bytes,
    issue_bytes: bytes,
    plan: EvidencePlan,
    frozen_tests: Mapping[str, bytes],
    cases: Sequence[CaseVerification],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent)
    )
    try:
        bundled_plan = _bundled_plan(plan, cases)
        results_payload = _results_payload(cases)
        (temporary / "tests").mkdir()
        (temporary / "harness").mkdir()
        (temporary / "manifest.json").write_bytes(_json_document(manifest))
        (temporary / "results.json").write_bytes(
            _json_document(results_payload)
        )
        (temporary / "evidence-plan.json").write_bytes(
            _json_document(bundled_plan)
        )
        (temporary / "issue.md").write_bytes(issue_bytes)
        (temporary / "patch.diff").write_bytes(patch_bytes)
        (temporary / "harness" / "conftest.py").write_bytes(
            _IMPORT_GUARD_SOURCE
        )
        for case in cases:
            destination = temporary / "tests" / Path(case.injected_path).name
            destination.write_bytes(frozen_tests[case.case.id])
        (temporary / "report.md").write_text(
            _render_report(manifest, cases), encoding="utf-8"
        )
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _bundled_plan(
    plan: EvidencePlan, cases: Sequence[CaseVerification]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "requirements": [item.to_dict() for item in plan.requirements],
        "cases": [
            {
                "id": item.case.id,
                "requirement_id": item.case.requirement_id,
                "test_file": f"tests/{Path(item.injected_path).name}",
                "node": item.case.node,
                "rationale": item.case.rationale,
            }
            for item in cases
        ],
    }


def _results_payload(cases: Sequence[CaseVerification]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cases": [_case_to_dict(item) for item in cases],
    }


def _json_document(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _case_to_dict(case: CaseVerification) -> dict[str, Any]:
    return {
        "id": case.case.id,
        "requirement_id": case.case.requirement_id,
        "rationale": case.case.rationale,
        "test_path": f"tests/{Path(case.injected_path).name}",
        "test_sha256": case.test_sha256,
        "command": list(case.command),
        "base": _runner_result_to_dict(case.dual_result.base),
        "patched": _runner_result_to_dict(case.dual_result.patched),
        "finding": case.finding.to_dict(),
    }


def _runner_result_to_dict(result: RunnerRevisionResult) -> dict[str, Any]:
    return {
        "stable": result.is_stable,
        "status": None if result.status is None else result.status.value,
        "attempts": [_command_result_to_dict(item) for item in result.attempts],
    }


def _command_result_to_dict(result: CommandResult) -> dict[str, Any]:
    return {
        "attempt": result.attempt,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "argv": list(result.argv),
        "workspace": str(result.workspace),
        "duration_seconds": result.duration_seconds,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "failure_signature": result.failure_signature,
    }


def _render_report(
    manifest: Mapping[str, Any], cases: Sequence[CaseVerification]
) -> str:
    supported = [item for item in cases if item.finding.is_supported_failure]
    inconclusive = [
        item for item in cases if item.finding.kind is FindingKind.INCONCLUSIVE
    ]
    if supported:
        headline = f"Found {len(supported)} supported patch failure(s)."
        if inconclusive:
            headline += f" {len(inconclusive)} additional case(s) were inconclusive."
    elif inconclusive:
        headline = (
            f"Run inconclusive: {len(inconclusive)} case(s) had unusable execution "
            "results."
        )
    else:
        headline = "No supported counterexample was found in this bounded run."
    lines = [
        "# Agent Patch Verifier report",
        "",
        f"**Outcome:** {headline}",
        "",
        "> A clean report is not proof that the patch is correct. It only means the supplied candidates did not produce a supported failure.",
        "",
        "## Revisions",
        "",
        f"- Base commit: `{manifest['base_commit']}`",
        f"- Patched tree: `{manifest['patched_tree']}`",
        f"- Patch SHA-256: `{manifest['patch_sha256']}`",
        f"- Repeats per revision: `{manifest['execution']['repeat']}`",
        "",
        "## Findings",
        "",
    ]
    for case in cases:
        requirement = case.finding.observation.requirement
        base = case.finding.observation.base
        patched = case.finding.observation.patched
        lines.extend(
            [
                f"### {case.case.id}: {case.finding.summary}",
                "",
                f"- Classification: `{case.finding.kind.value}`",
                f"- Requirement: **{requirement.statement}**",
                f"- Requirement kind: `{requirement.kind.value}`",
                f"- Provenance: `{requirement.provenance.value}` from `{requirement.source}`",
                f"- Base: `{base.outcome.value}` ({'stable' if base.stable else 'unstable'})",
                f"- Patched: `{patched.outcome.value}` ({'stable' if patched.stable else 'unstable'})",
                f"- Test artifact: `tests/{Path(case.injected_path).name}`",
                f"- Test SHA-256: `{case.test_sha256}`",
                "",
                "Exact command:",
                "",
                *_text_block(shlex.join(case.command)),
                "",
            ]
        )
        if requirement.excerpt:
            lines.extend(
                [
                    "Source excerpt:",
                    "",
                    "> " + requirement.excerpt.replace("\n", "\n> "),
                    "",
                ]
            )
        lines.extend([case.finding.rationale, "", "Execution evidence:", ""])
        for label, revision in (
            ("Base", case.dual_result.base),
            ("Patched", case.dual_result.patched),
        ):
            lines.extend([f"#### {label}", ""])
            for attempt in revision.attempts:
                exit_text = (
                    "none" if attempt.exit_code is None else str(attempt.exit_code)
                )
                lines.append(
                    f"- Attempt {attempt.attempt}: `{attempt.status.value}`; "
                    f"exit `{exit_text}`; {attempt.duration_seconds:.3f}s"
                )
                if attempt.stdout_truncated or attempt.stderr_truncated:
                    truncated_streams = ", ".join(
                        name
                        for name, truncated in (
                            ("stdout", attempt.stdout_truncated),
                            ("stderr", attempt.stderr_truncated),
                        )
                        if truncated
                    )
                    lines.append(
                        f"  - Captured {truncated_streams} exceeded the "
                        f"{OUTPUT_LIMIT_BYTES}-byte limit and was truncated."
                    )
                if attempt.stdout:
                    lines.extend(["", "  stdout:", "", *_text_block(attempt.stdout)])
                if attempt.stderr:
                    lines.extend(["", "  stderr:", "", *_text_block(attempt.stderr)])
            lines.append("")

    lines.extend(
        [
            "## Changed Python symbols",
            "",
        ]
    )
    changed_symbols = manifest["scope"]["changed_symbols"]
    if changed_symbols:
        for symbol in changed_symbols:
            lines.append(
                f"- `{symbol['path']}::{symbol['qualified_name']}` ({symbol['kind']})"
            )
    else:
        lines.append("- No changed Python symbol could be mapped from the diff.")
    lines.extend(
        [
            "",
            "## Reproduction material",
            "",
            "This bundle contains `patch.diff`, `issue.md`, the normalized `evidence-plan.json`, exact test files, `manifest.json`, and raw per-attempt output in `results.json`.",
            "",
            "Run `apv reproduce <this-run-directory>` to execute the bundled tests again against the recorded base commit and patch. Use `--repo /path/to/source` if the repository moved. Reproduction verifies all recorded artifact hashes before execution.",
            "",
            "The bundle is not an authenticated archive and does not contain the source repository. Only reproduce trusted bundles: their Python tests and target code execute with your user permissions.",
            "",
            "Temporary worktree paths in raw results are historical and are removed after the run.",
            "",
        ]
    )
    return "\n".join(lines)


def _text_block(value: str, *, limit: int = 4000) -> list[str]:
    if len(value) > limit:
        value = value[:limit] + f"\n... output truncated after {limit} characters ..."
    fence = "```"
    while fence in value:
        fence += "`"
    return [f"{fence}text", value.rstrip("\n"), fence]

