"""End-to-end orchestration for deterministic patch verification."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import html
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
from .context import ContextRevision, RepositoryContext, collect_repository_context
from .manifest_validation import validate_manifest_v2
from .models import (
    Finding,
    FindingKind,
    Outcome,
    RevisionResult as EvidenceRevisionResult,
)
from .patches import is_binary_patch
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

# The collector can emit at most 500 bounded excerpts per snapshot, so 10 MiB
# leaves substantial headroom while preventing an untrusted replay bundle from
# forcing an unbounded read before JSON validation.
_MAX_CONTEXT_ARTIFACT_BYTES = 10 * 1024 * 1024
_MAX_PATCH_BYTES = 20 * 1024 * 1024
_MAX_ISSUE_BYTES = 2 * 1024 * 1024
_MAX_EVIDENCE_PLAN_BYTES = 5 * 1024 * 1024
_MAX_MANIFEST_BYTES = 10 * 1024 * 1024
_MAX_RESULTS_BYTES = 128 * 1024 * 1024
_MAX_REPORT_BYTES = 10 * 1024 * 1024
_MAX_CANDIDATE_TEST_BYTES = 2 * 1024 * 1024
_MAX_HARNESS_BYTES = 1024 * 1024
_MAX_REPEAT = 5
_MAX_CASE_REPETITIONS = 50
_MAX_TIMEOUT_SECONDS = 3600.0


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
    expected_patch_sha256: str | None = None
    expected_issue_sha256: str | None = None
    expected_evidence_sha256: str | None = None
    expected_scope_sha256: str | None = None
    expected_test_sha256: tuple[tuple[str, str], ...] = ()

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
        raw_test_hashes = self.expected_test_sha256
        if isinstance(raw_test_hashes, Mapping):
            test_hash_items = tuple(raw_test_hashes.items())
        else:
            test_hash_items = tuple(raw_test_hashes)
        normalized_test_hashes: list[tuple[str, str]] = []
        seen_test_ids: set[str] = set()
        for item in test_hash_items:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
            ):
                raise TypeError(
                    "expected_test_sha256 must contain (case id, SHA-256) pairs"
                )
            case_id, digest = item
            if not case_id:
                raise ValueError("expected test case id must not be empty")
            if case_id in seen_test_ids:
                raise ValueError(f"duplicate expected test case id: {case_id!r}")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(
                    f"expected test SHA-256 for {case_id!r} is invalid"
                )
            seen_test_ids.add(case_id)
            normalized_test_hashes.append((case_id, digest))
        object.__setattr__(
            self,
            "expected_test_sha256",
            tuple(sorted(normalized_test_hashes)),
        )
        if not self.base_revision.strip():
            raise ValueError("base_revision must not be empty")
        if not self.command_prefix:
            raise ValueError("command_prefix must not be empty")
        if (
            self.timeout_seconds <= 0
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds > _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "timeout_seconds must be finite, greater than zero, and no more "
                f"than {_MAX_TIMEOUT_SECONDS:g}"
            )
        if self.repeat < 1 or self.repeat > _MAX_REPEAT:
            raise ValueError(f"repeat must be between 1 and {_MAX_REPEAT}")
        for label, digest in (
            ("reproduction manifest", self.reproduction_manifest_sha256),
            ("reproduction results", self.reproduction_results_sha256),
            ("expected patch", self.expected_patch_sha256),
            ("expected issue", self.expected_issue_sha256),
            ("expected evidence plan", self.expected_evidence_sha256),
            ("expected change scope", self.expected_scope_sha256),
        ):
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{label} SHA-256 is invalid")

    @property
    def expected_test_hashes(self) -> dict[str, str]:
        return dict(self.expected_test_sha256)


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
    context_sha256: str
    results_sha256: str
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

    patch_bytes = _read_bytes(
        request.patch_file,
        "patch",
        max_bytes=_MAX_PATCH_BYTES,
        expected_sha256=request.expected_patch_sha256,
    )
    if is_binary_patch(patch_bytes):
        raise VerificationError("binary patches are outside the current MVP scope")
    try:
        patch_text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("patch must be UTF-8 text") from exc
    issue_bytes = _read_bytes(
        request.issue_file,
        "issue",
        max_bytes=_MAX_ISSUE_BYTES,
        expected_sha256=request.expected_issue_sha256,
    )
    try:
        issue_text = issue_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("issue file must be UTF-8") from exc
    evidence_bytes = _read_bytes(
        request.evidence_file,
        "evidence plan",
        max_bytes=_MAX_EVIDENCE_PLAN_BYTES,
        expected_sha256=request.expected_evidence_sha256,
    )
    plan = load_evidence_plan(
        request.evidence_file,
        issue_text=issue_text,
        document_bytes=evidence_bytes,
    )
    _verify_expected_test_hashes(plan, request.expected_test_hashes)
    case_repetitions = len(plan.cases) * request.repeat
    if case_repetitions > _MAX_CASE_REPETITIONS:
        raise VerificationError(
            "verification plan requests "
            f"{case_repetitions} case repetitions; the limit is "
            f"{_MAX_CASE_REPETITIONS}"
        )
    if request.output_directory.exists():
        raise VerificationError(
            f"output directory already exists: {request.output_directory}"
        )

    run_id = _run_id(patch_bytes)
    changes = parse_unified_diff(patch_text)
    case_results: list[CaseVerification] = []
    frozen_tests = {case.id: case.source_bytes for case in plan.cases}

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
        scope_payload = _scope_payload(changes, symbols)
        if request.expected_scope_sha256 is not None:
            observed_scope_sha256 = hashlib.sha256(
                _json_document(scope_payload)
            ).hexdigest()
            if observed_scope_sha256 != request.expected_scope_sha256:
                raise VerificationError(
                    "recomputed change scope differs from the recorded bundle"
                )
        # Keep the two revisions as separate evidence domains. In particular,
        # patch-added documentation and tests must never be presented as
        # pre-existing support for a requirement.
        base_context = collect_repository_context(
            workspace.base_path,
            symbols,
            revision=ContextRevision.BASE,
        )
        patched_context = collect_repository_context(
            workspace.patched_path,
            symbols,
            revision=ContextRevision.PATCHED,
        )
        context_payload = _context_payload(base_context, patched_context)
        context_bytes = _context_document_bytes(context_payload)
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

    evidence_plan_bytes = _bounded_json_document(
        _bundled_plan(plan, case_results),
        label="normalized evidence plan",
        max_bytes=_MAX_EVIDENCE_PLAN_BYTES,
    )
    results_bytes = _bounded_json_document(
        _results_payload(case_results),
        label="results",
        max_bytes=_MAX_RESULTS_BYTES,
    )
    manifest = _manifest(
        request=request,
        run_id=run_id,
        snapshot=snapshot,
        issue_bytes=issue_bytes,
        plan=plan,
        scope_payload=scope_payload,
        cases=case_results,
        context_bytes=context_bytes,
        evidence_plan_bytes=evidence_plan_bytes,
        results_bytes=results_bytes,
    )
    # The report intentionally excludes its own hash. Render it exactly once,
    # then bind those immutable bytes into the manifest before writing either
    # artifact so the bundle cannot describe bytes other than those emitted.
    report_bytes = _bounded_bytes(
        _render_report(manifest, case_results, context_payload).encode("utf-8"),
        label="report",
        max_bytes=_MAX_REPORT_BYTES,
    )
    manifest["report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    try:
        validate_manifest_v2(manifest)
    except ValueError as exc:  # pragma: no cover - protects the bundle contract
        raise VerificationError(f"generated manifest is invalid: {exc}") from exc
    manifest_bytes = _bounded_json_document(
        manifest,
        label="manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        _write_bundle(
            request.output_directory,
            manifest_bytes=manifest_bytes,
            patch_bytes=patch_bytes,
            issue_bytes=issue_bytes,
            evidence_plan_bytes=evidence_plan_bytes,
            results_bytes=results_bytes,
            frozen_tests=frozen_tests,
            cases=case_results,
            context_bytes=context_bytes,
            report_bytes=report_bytes,
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
        context_sha256=str(manifest["context_sha256"]),
        results_sha256=str(manifest["results_sha256"]),
        reproduction_manifest_sha256=request.reproduction_manifest_sha256,
        reproduction_results_sha256=request.reproduction_results_sha256,
    )


def _verify_expected_test_hashes(
    plan: EvidencePlan, expected_hashes: Mapping[str, str]
) -> None:
    """Check the exact candidate bytes frozen while loading the plan."""

    if not expected_hashes:
        return
    cases_by_id = {case.id: case for case in plan.cases}
    if set(cases_by_id) != set(expected_hashes):
        missing = sorted(set(cases_by_id) - set(expected_hashes))
        extra = sorted(set(expected_hashes) - set(cases_by_id))
        raise VerificationError(
            "bundled candidate-test hash mapping differs from the evidence plan: "
            f"missing hashes for {missing}, unexpected hashes for {extra}"
        )
    for case_id, case in cases_by_id.items():
        expected = expected_hashes[case_id]
        observed = case.source_sha256
        if observed != expected:
            raise VerificationError(
                f"bundled test for case {case_id} hash mismatch: "
                f"expected {expected}, observed {observed}"
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
    try:
        manifest_bytes = _read_bundle_bytes(
            manifest_path,
            run_directory,
            "manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        manifest = _validate_manifest_payload(manifest)
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
    expected_test_sha256 = tuple(
        (str(item["id"]), str(item["test_sha256"]))
        for item in manifest["cases"]
    )
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
        expected_patch_sha256=str(manifest["patch_sha256"]),
        expected_issue_sha256=str(manifest["issue_sha256"]),
        expected_evidence_sha256=str(manifest["evidence_plan_sha256"]),
        expected_scope_sha256=(
            hashlib.sha256(_json_document(manifest["scope"])).hexdigest()
            if _bundle_schema_version(manifest) == 2
            else None
        ),
        expected_test_sha256=expected_test_sha256,
    )


def reproduction_differences(
    run_directory: Path, result: VerificationResult
) -> tuple[str, ...]:
    """Compare a replay's classifications with the bundle's recorded results."""

    run_root = Path(run_directory).resolve()
    results_path = run_root / "results.json"
    manifest_path = run_root / "manifest.json"
    replay_root = result.output_directory.resolve()
    replay_results_path = replay_root / "results.json"
    try:
        manifest_bytes = _read_bundle_bytes(
            manifest_path,
            run_root,
            "manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        results_bytes = _read_bundle_bytes(
            results_path,
            run_root,
            "recorded results",
            max_bytes=_MAX_RESULTS_BYTES,
        )
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
        manifest = _validate_manifest_payload(manifest)
        _validate_bundle_inputs(run_root, manifest)
        payload = json.loads(results_bytes.decode("utf-8"))
        replay_results_bytes = _read_bundle_bytes(
            replay_results_path,
            replay_root,
            "reproduced results",
            max_bytes=_MAX_RESULTS_BYTES,
        )
        observed_replay_results_sha256 = hashlib.sha256(
            replay_results_bytes
        ).hexdigest()
        if observed_replay_results_sha256 != result.results_sha256:
            raise VerificationError(
                "reproduced results changed after verification: "
                f"expected {result.results_sha256}, "
                f"observed {observed_replay_results_sha256}"
            )
        replay_payload = json.loads(replay_results_bytes.decode("utf-8"))
        replay_manifest_bytes = _read_bundle_bytes(
            replay_root / "manifest.json",
            replay_root,
            "reproduced manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        replay_manifest = _validate_manifest_payload(
            json.loads(replay_manifest_bytes.decode("utf-8"))
        )
        _validate_bundle_inputs(replay_root, replay_manifest)
        recorded = _case_execution_signatures(payload)
        replayed = _case_execution_signatures(replay_payload)
        schema_version = _bundle_schema_version(manifest)
        if schema_version == 2:
            _, recorded_context = _load_context_artifact(
                run_root / "context.json", run_root, "repository context"
            )
            replay_context_bytes, replay_context = _load_context_artifact(
                replay_root / "context.json",
                replay_root,
                "reproduced repository context",
            )
            if (
                hashlib.sha256(replay_context_bytes).hexdigest()
                != result.context_sha256
            ):
                raise VerificationError(
                    "reproduced repository context changed after verification"
                )
        else:
            recorded_context = None
            replay_context = None
    except VerificationError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
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
    if recorded_context is not None and recorded_context != replay_context:
        differences.append("repository context changed during reproduction")
    if (
        _bundle_schema_version(manifest) == 2
        and manifest["scope"] != replay_manifest["scope"]
    ):
        differences.append(
            "changed-file or changed-symbol scope changed during reproduction"
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
    manifest = _validate_manifest_payload(manifest)
    schema_version = _bundle_schema_version(manifest)
    artifacts = [
        ("patch.diff", "patch_sha256", "patch", _MAX_PATCH_BYTES),
        ("issue.md", "issue_sha256", "issue", _MAX_ISSUE_BYTES),
        (
            "evidence-plan.json",
            "evidence_plan_sha256",
            "evidence plan",
            _MAX_EVIDENCE_PLAN_BYTES,
        ),
        ("results.json", "results_sha256", "recorded results", _MAX_RESULTS_BYTES),
        (
            "harness/conftest.py",
            "harness_sha256",
            "pytest harness",
            _MAX_HARNESS_BYTES,
        ),
    ]
    if schema_version == 2:
        artifacts.append(
            ("report.md", "report_sha256", "report", _MAX_REPORT_BYTES)
        )
    artifact_bytes: dict[str, bytes] = {}
    for filename, hash_key, label, max_bytes in artifacts:
        expected = manifest.get(hash_key)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise VerificationError(f"bundle manifest has no valid {hash_key}")
        artifact_bytes[filename] = _require_hash(
            run_directory / filename,
            expected,
            label,
            run_directory,
            max_bytes=max_bytes,
        )
    current_harness_hash = hashlib.sha256(_IMPORT_GUARD_SOURCE).hexdigest()
    if manifest.get("harness_sha256") != current_harness_hash:
        raise VerificationError(
            "the installed verifier harness differs from the recorded bundle"
        )
    if schema_version == 2:
        expected_context = manifest.get("context_sha256")
        if not isinstance(expected_context, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_context
        ):
            raise VerificationError("bundle manifest has no valid context_sha256")
        _, context_payload = _load_context_artifact(
            run_directory / "context.json",
            run_directory,
            "repository context",
            expected_sha256=expected_context,
        )
        _validate_context_scope(context_payload, manifest)

    try:
        patch_text = artifact_bytes["patch.diff"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("bundled patch must be UTF-8 text") from exc
    try:
        expected_changed_files = _changed_files_payload(
            parse_unified_diff(patch_text)
        )
    except ValueError as exc:
        raise VerificationError(f"invalid bundled patch: {exc}") from exc
    scope = manifest.get("scope")
    if not isinstance(scope, Mapping):
        raise VerificationError("bundle manifest scope must be an object")
    if scope.get("changed_files") != expected_changed_files:
        raise VerificationError(
            "bundle manifest changed-file scope differs from the bundled patch"
        )

    try:
        plan_payload = json.loads(
            artifact_bytes["evidence-plan.json"].decode("utf-8")
        )
        plan_requirements = plan_payload["requirements"]
        manifest_requirements = manifest["requirements"]
        if plan_requirements != manifest_requirements:
            raise ValueError("manifest and evidence-plan requirements differ")
        plan_cases = plan_payload["cases"]
        manifest_cases = manifest["cases"]
        if not isinstance(plan_cases, list) or not isinstance(manifest_cases, list):
            raise TypeError("cases must be arrays")
        plan_by_id = {str(item["id"]): item for item in plan_cases}
        if len(plan_by_id) != len(plan_cases):
            raise ValueError("duplicate case ids in evidence plan")
        manifest_case_ids = [str(item["id"]) for item in manifest_cases]
        if len(set(manifest_case_ids)) != len(manifest_case_ids):
            raise ValueError("duplicate case ids in bundle manifest")
        if set(manifest_case_ids) != set(plan_by_id):
            raise ValueError("manifest and evidence-plan case ids differ")
        for recorded in manifest_cases:
            case_id = str(recorded["id"])
            planned = plan_by_id[case_id]
            if str(recorded["requirement_id"]) != str(planned["requirement_id"]):
                raise ValueError(f"requirement id differs for case {case_id}")
            expected_injected = _generated_test_relative_path(
                str(manifest["run_id"]), case_id
            )
            if recorded.get("injected_path") != expected_injected.as_posix():
                raise ValueError(f"injected path differs for case {case_id}")
            relative = Path(str(planned["test_file"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe test path for case {case_id}")
            expected_bundled_test = Path("tests") / expected_injected.name
            if relative != expected_bundled_test:
                raise ValueError(f"bundled test path differs for case {case_id}")
            node = planned.get("node")
            if not isinstance(node, str):
                raise ValueError(f"missing pytest node for case {case_id}")
            command_prefix = manifest["execution"]["command_prefix"]
            junit_path = expected_injected.parent / f"junit-{case_id}.xml"
            expected_command = [
                *command_prefix,
                f"{expected_injected.as_posix()}::{node}",
                "--runxfail",
                f"--junitxml={junit_path.as_posix()}",
            ]
            if recorded.get("command") != expected_command:
                raise ValueError(f"command differs for case {case_id}")
            test_path = run_directory / relative
            test_hash = recorded.get("test_sha256")
            if not isinstance(test_hash, str):
                raise ValueError(f"missing test hash for case {case_id}")
            _require_hash(
                test_path,
                test_hash,
                f"test for case {case_id}",
                run_directory,
                max_bytes=_MAX_CANDIDATE_TEST_BYTES,
            )
        results_payload = json.loads(
            artifact_bytes["results.json"].decode("utf-8")
        )
        _validate_results_payload(
            results_payload,
            manifest=manifest,
            plan_by_id=plan_by_id,
        )
    except OSError as exc:
        raise VerificationError(f"cannot inspect bundle evidence plan: {exc}") from exc
    except (
        AttributeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise VerificationError(f"invalid bundle artifact mapping: {exc}") from exc


def _validate_results_payload(
    payload: object,
    *,
    manifest: Mapping[str, Any],
    plan_by_id: Mapping[str, Any],
) -> None:
    """Validate recorded execution evidence before any replay code runs."""

    if not isinstance(payload, Mapping):
        raise ValueError("results must be an object")
    if set(payload) != {"schema_version", "cases"}:
        raise ValueError("results have unexpected or missing properties")
    version = payload["schema_version"]
    if not _is_strict_integer(version) or version != 1:
        raise ValueError("results schema_version must be 1")
    cases = payload["cases"]
    if not isinstance(cases, list):
        raise ValueError("results cases must be an array")
    if len(cases) > 50:
        raise ValueError("results cases must contain at most 50 entries")

    manifest_cases = manifest["cases"]
    if not isinstance(manifest_cases, list):
        raise ValueError("manifest cases must be an array")
    manifest_by_id = {
        str(item["id"]): item
        for item in manifest_cases
        if isinstance(item, Mapping)
    }
    if len(manifest_by_id) != len(manifest_cases):
        raise ValueError("manifest cases must contain unique object entries")

    result_ids: list[str] = []
    expected_repeat = manifest["execution"]["repeat"]
    for index, item in enumerate(cases):
        if not isinstance(item, Mapping):
            raise ValueError(f"results cases[{index}] must be an object")
        required = {
            "id",
            "requirement_id",
            "rationale",
            "test_path",
            "test_sha256",
            "command",
            "base",
            "patched",
            "finding",
        }
        if set(item) != required:
            raise ValueError(
                f"results cases[{index}] has unexpected or missing properties"
            )
        case_id = item["id"]
        requirement_id = item["requirement_id"]
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"results cases[{index}].id must be a non-empty string")
        if not isinstance(requirement_id, str) or not requirement_id:
            raise ValueError(
                f"results cases[{index}].requirement_id must be a non-empty string"
            )
        if case_id not in manifest_by_id or case_id not in plan_by_id:
            raise ValueError(f"results contain unexpected case id {case_id!r}")
        manifest_case = manifest_by_id[case_id]
        planned_case = plan_by_id[case_id]
        if not isinstance(planned_case, Mapping):
            raise ValueError(f"plan case {case_id!r} must be an object")
        if requirement_id != str(manifest_case["requirement_id"]):
            raise ValueError(f"results requirement id differs for case {case_id}")
        if requirement_id != str(planned_case["requirement_id"]):
            raise ValueError(f"plan requirement id differs for case {case_id}")
        if item["test_path"] != planned_case["test_file"]:
            raise ValueError(f"results test path differs for case {case_id}")
        if item["test_sha256"] != manifest_case["test_sha256"]:
            raise ValueError(f"results test hash differs for case {case_id}")
        if item["command"] != manifest_case["command"]:
            raise ValueError(f"results command differs for case {case_id}")
        if not isinstance(item["rationale"], str):
            raise ValueError(f"results rationale must be a string for case {case_id}")
        if item["rationale"] != planned_case.get("rationale", ""):
            raise ValueError(f"results rationale differs for case {case_id}")

        base_statuses = _validate_recorded_revision(
            item["base"],
            case_id=case_id,
            revision="base",
            repeat=expected_repeat,
            expected_argv=item["command"],
        )
        patched_statuses = _validate_recorded_revision(
            item["patched"],
            case_id=case_id,
            revision="patched",
            repeat=expected_repeat,
            expected_argv=item["command"],
        )
        finding_payload = item["finding"]
        if not isinstance(finding_payload, Mapping):
            raise ValueError(f"results finding must be an object for case {case_id}")
        _validate_recorded_finding_shape(finding_payload, case_id=case_id)
        try:
            finding = Finding.from_dict(finding_payload)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid finding for case {case_id}: {exc}") from exc
        if finding.kind.value != manifest_case["finding"]:
            raise ValueError(f"results finding differs for case {case_id}")
        observation = finding.observation
        if observation.test_id != case_id:
            raise ValueError(f"finding test id differs for case {case_id}")
        if observation.requirement.id != requirement_id:
            raise ValueError(f"finding requirement id differs for case {case_id}")
        requirements = manifest["requirements"]
        requirement_by_id = {
            str(requirement["id"]): requirement
            for requirement in requirements
            if isinstance(requirement, Mapping)
        }
        expected_requirement = requirement_by_id.get(requirement_id)
        if expected_requirement is None:
            raise ValueError(f"finding references unknown requirement {requirement_id}")
        if observation.requirement.to_dict() != expected_requirement:
            raise ValueError(f"finding requirement differs for case {case_id}")
        if tuple(value.value for value in observation.base.attempts) != base_statuses:
            raise ValueError(f"finding base attempts differ for case {case_id}")
        if (
            tuple(value.value for value in observation.patched.attempts)
            != patched_statuses
        ):
            raise ValueError(f"finding patched attempts differ for case {case_id}")
        if observation.base.outcome.value != base_statuses[0] or (
            observation.base.stable != (len(set(base_statuses)) == 1)
        ):
            raise ValueError(f"finding base aggregate differs for case {case_id}")
        if observation.base.detail != _recorded_revision_detail(item["base"]):
            raise ValueError(f"finding base detail differs for case {case_id}")
        if observation.patched.outcome.value != patched_statuses[0] or (
            observation.patched.stable != (len(set(patched_statuses)) == 1)
        ):
            raise ValueError(f"finding patched aggregate differs for case {case_id}")
        if observation.patched.detail != _recorded_revision_detail(
            item["patched"]
        ):
            raise ValueError(f"finding patched detail differs for case {case_id}")
        expected_finding = classify(
            observation.requirement,
            observation.base,
            observation.patched,
            test_id=case_id,
        )
        if finding.to_dict() != expected_finding.to_dict():
            raise ValueError(f"finding classification differs for case {case_id}")
        result_ids.append(case_id)

    manifest_ids = [str(item["id"]) for item in manifest_cases]
    if result_ids != manifest_ids:
        raise ValueError("results case order differs from the manifest")


def _validate_context_scope(
    context: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    scope = manifest.get("scope")
    if not isinstance(scope, Mapping):
        raise VerificationError("bundle manifest scope must be an object")
    changed_symbols = scope.get("changed_symbols")
    if not isinstance(changed_symbols, list):
        raise VerificationError("bundle manifest changed_symbols must be an array")

    identity_keys = (
        "path",
        "qualified_name",
        "kind",
        "base_path",
        "patched_path",
        "base_occurrence",
        "patched_occurrence",
        "revision_match",
    )

    def identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(item.get(key) for key in identity_keys)

    manifest_counts = Counter(
        identity(item) for item in changed_symbols if isinstance(item, Mapping)
    )
    if sum(manifest_counts.values()) != len(changed_symbols):
        raise VerificationError("bundle manifest changed_symbols entries are invalid")
    for revision in ("base", "patched"):
        snapshot = context[revision]
        for symbol in snapshot["symbols"]:
            key = identity(symbol)
            if manifest_counts[key] <= 0:
                raise VerificationError(
                    f"bundled {revision} context symbol is absent from manifest scope"
                )
            manifest_counts[key] -= 1
        # Each revision is an independently truncated view, so consume against
        # a fresh multiset rather than making the two snapshots compete.
        manifest_counts = Counter(
            identity(item)
            for item in changed_symbols
            if isinstance(item, Mapping)
        )


def _validate_recorded_finding_shape(
    payload: Mapping[str, Any], *, case_id: str
) -> None:
    if set(payload) != {"kind", "summary", "rationale", "observation"}:
        raise ValueError(f"finding has unexpected or missing properties for {case_id}")
    observation = payload["observation"]
    if not isinstance(observation, Mapping) or set(observation) != {
        "test_id",
        "requirement",
        "base",
        "patched",
    }:
        raise ValueError(
            f"finding observation has unexpected or missing properties for {case_id}"
        )
    requirement = observation["requirement"]
    if not isinstance(requirement, Mapping) or set(requirement) != {
        "id",
        "statement",
        "kind",
        "provenance",
        "source",
        "excerpt",
    }:
        raise ValueError(
            f"finding requirement has unexpected or missing properties for {case_id}"
        )
    for revision in ("base", "patched"):
        result = observation[revision]
        if not isinstance(result, Mapping) or set(result) != {
            "outcome",
            "stable",
            "attempts",
            "detail",
        }:
            raise ValueError(
                f"finding {revision} has unexpected or missing properties for "
                f"{case_id}"
            )


def _validate_recorded_revision(
    payload: object,
    *,
    case_id: str,
    revision: str,
    repeat: object,
    expected_argv: object,
) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"results {revision} must be an object for case {case_id}")
    if set(payload) != {"stable", "status", "attempts"}:
        raise ValueError(
            f"results {revision} has unexpected or missing properties for case "
            f"{case_id}"
        )
    stable = payload["stable"]
    status = payload["status"]
    attempts = payload["attempts"]
    if not isinstance(stable, bool):
        raise ValueError(f"results {revision}.stable must be boolean")
    allowed_statuses = {item.value for item in Outcome}
    if status is not None and status not in allowed_statuses:
        raise ValueError(f"results {revision}.status is invalid")
    if not isinstance(attempts, list):
        raise ValueError(f"results {revision}.attempts must be an array")
    if not _is_strict_integer(repeat) or len(attempts) != repeat:
        raise ValueError(
            f"results {revision}.attempts count differs for case {case_id}"
        )

    statuses: list[str] = []
    required = {
        "attempt",
        "status",
        "exit_code",
        "argv",
        "workspace",
        "duration_seconds",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
        "failure_signature",
    }
    for expected_attempt, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, Mapping) or set(attempt) != required:
            raise ValueError(
                f"results {revision}.attempts[{expected_attempt - 1}] has "
                "unexpected or missing properties"
            )
        if (
            not _is_strict_integer(attempt["attempt"])
            or attempt["attempt"] != expected_attempt
        ):
            raise ValueError(f"results {revision} attempt numbering is invalid")
        attempt_status = attempt["status"]
        if attempt_status not in allowed_statuses:
            raise ValueError(f"results {revision} attempt status is invalid")
        exit_code = attempt["exit_code"]
        if exit_code is not None and not _is_strict_integer(exit_code):
            raise ValueError(f"results {revision} exit_code is invalid")
        if (
            (attempt_status == Outcome.PASS.value and exit_code != 0)
            or (
                attempt_status == Outcome.FAIL.value
                and exit_code not in {0, 1}
            )
            or (attempt_status == Outcome.TIMEOUT.value and exit_code is not None)
        ):
            raise ValueError(
                f"results {revision} exit code is inconsistent with status"
            )
        argv = attempt["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) for value in argv)
        ):
            raise ValueError(f"results {revision} argv is invalid")
        if argv != expected_argv:
            raise ValueError(
                f"results {revision} argv differs for case {case_id}"
            )
        if not isinstance(attempt["workspace"], str):
            raise ValueError(f"results {revision} workspace is invalid")
        duration = attempt["duration_seconds"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ValueError(f"results {revision} duration is invalid")
        if not isinstance(attempt["stdout"], str) or not isinstance(
            attempt["stderr"], str
        ):
            raise ValueError(f"results {revision} output is invalid")
        if not isinstance(attempt["stdout_truncated"], bool) or not isinstance(
            attempt["stderr_truncated"], bool
        ):
            raise ValueError(f"results {revision} truncation flags are invalid")
        failure_signature = attempt["failure_signature"]
        if failure_signature is not None and not isinstance(
            failure_signature, str
        ):
            raise ValueError(
                f"results {revision} failure signature is invalid"
            )
        if failure_signature is not None and not re.fullmatch(
            r"[0-9a-f]{64}", failure_signature
        ):
            raise ValueError(
                f"results {revision} failure signature is invalid"
            )
        statuses.append(attempt_status)

    derived_stable = len(set(statuses)) == 1
    derived_status = statuses[0] if derived_stable else None
    if stable != derived_stable or status != derived_status:
        raise ValueError(f"results {revision} aggregate is inconsistent")
    return tuple(statuses)


def _recorded_revision_detail(payload: object) -> str:
    assert isinstance(payload, Mapping)
    attempts = payload["attempts"]
    assert isinstance(attempts, list)
    return ", ".join(
        f"attempt {attempt['attempt']}: {attempt['status']}"
        + (
            f" (exit {attempt['exit_code']})"
            if attempt["exit_code"] is not None
            else ""
        )
        for attempt in attempts
    )


def _bundle_schema_version(manifest: Mapping[str, Any]) -> int:
    version = manifest.get("schema_version")
    if isinstance(version, bool) or version not in (1, 2):
        raise VerificationError("bundle manifest has an unsupported schema_version")
    return version


def _validate_manifest_payload(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise VerificationError("bundle manifest must be a JSON object")
    version = _bundle_schema_version(payload)
    if version == 2:
        try:
            validate_manifest_v2(payload)
        except ValueError as exc:
            raise VerificationError(f"invalid bundle manifest: {exc}") from exc
    else:
        _validate_legacy_manifest_v1(payload)
    return payload


def _validate_legacy_manifest_v1(payload: Mapping[str, Any]) -> None:
    """Validate replay-critical fields absent from the old schema-less format."""

    try:
        for key in ("repository", "requested_base_revision"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id
        ):
            raise ValueError("run_id is invalid")
        for key in ("base_commit", "patched_tree"):
            value = payload.get(key)
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value
            ):
                raise ValueError(f"{key} must be a Git object id")
        for key in (
            "patch_sha256",
            "issue_sha256",
            "evidence_plan_sha256",
            "results_sha256",
            "harness_sha256",
        ):
            value = payload.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{key} must be a SHA-256 digest")
        execution = payload.get("execution")
        if not isinstance(execution, Mapping):
            raise ValueError("execution must be an object")
        command = execution.get("command_prefix")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            raise ValueError("execution.command_prefix must be an array of strings")
        timeout = execution.get("timeout_seconds")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "execution.timeout_seconds must be positive, finite, and no more "
                f"than {_MAX_TIMEOUT_SECONDS:g}"
            )
        repeat = execution.get("repeat")
        if not _is_strict_integer(repeat) or not 1 <= repeat <= _MAX_REPEAT:
            raise ValueError(f"execution.repeat must be between 1 and {_MAX_REPEAT}")
        output_limit = execution.get("output_limit_bytes_per_stream")
        if output_limit != OUTPUT_LIMIT_BYTES:
            raise ValueError(
                "execution.output_limit_bytes_per_stream is incompatible with "
                f"this release (expected {OUTPUT_LIMIT_BYTES})"
            )
        cases = payload.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
            raise ValueError("cases must contain between 1 and 50 entries")
        for index, case in enumerate(cases):
            if not isinstance(case, Mapping):
                raise ValueError(f"cases[{index}] must be an object")
            for key in ("id", "requirement_id", "test_sha256"):
                if not isinstance(case.get(key), str) or not case[key]:
                    raise ValueError(f"cases[{index}].{key} must be a string")
            if len(case["id"]) > 64 or len(case["requirement_id"]) > 64:
                raise ValueError(f"cases[{index}] identifiers are too long")
            if not re.fullmatch(r"[0-9a-f]{64}", case["test_sha256"]):
                raise ValueError(f"cases[{index}].test_sha256 is invalid")
    except (KeyError, TypeError) as exc:
        raise VerificationError(f"invalid legacy bundle manifest: {exc}") from exc
    except ValueError as exc:
        raise VerificationError(f"invalid legacy bundle manifest: {exc}") from exc


def _load_context_artifact(
    path: Path,
    bundle_root: Path,
    label: str,
    *,
    expected_sha256: str | None = None,
) -> tuple[bytes, Mapping[str, Any]]:
    raw = _read_bundle_bytes(
        path,
        bundle_root,
        label,
        max_bytes=_MAX_CONTEXT_ARTIFACT_BYTES,
    )
    if expected_sha256 is not None:
        observed = hashlib.sha256(raw).hexdigest()
        if observed != expected_sha256:
            raise VerificationError(
                f"bundled {label} hash mismatch: expected {expected_sha256}, "
                f"observed {observed}"
            )
    try:
        payload = json.loads(raw.decode("utf-8"))
        validated = _validate_context_payload(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VerificationError(f"invalid bundled {label}: {exc}") from exc
    return raw, validated


def _require_hash(
    path: Path,
    expected: str,
    label: str,
    bundle_root: Path,
    *,
    max_bytes: int,
) -> bytes:
    raw = _read_bundle_bytes(
        path,
        bundle_root,
        label,
        max_bytes=max_bytes,
    )
    observed = hashlib.sha256(raw).hexdigest()
    if observed != expected:
        raise VerificationError(
            f"bundled {label} hash mismatch: expected {expected}, observed {observed}"
        )
    return raw


def _read_bundle_bytes(
    path: Path,
    bundle_root: Path,
    label: str,
    *,
    max_bytes: int,
) -> bytes:
    safe_path = _require_bundle_file(path, bundle_root, label)
    try:
        with safe_path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise VerificationError(
            f"cannot read bundled {label} {safe_path}: {exc}"
        ) from exc
    if len(raw) > max_bytes:
        raise VerificationError(
            f"bundled {label} exceeds the {max_bytes}-byte limit"
        )
    return raw


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


def _read_bytes(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as exc:
        raise VerificationError(f"cannot read {label} file {path}: {exc}") from exc
    if len(data) > max_bytes:
        raise VerificationError(
            f"{label} file exceeds the {max_bytes}-byte limit: {path}"
        )
    if expected_sha256 is not None:
        observed = hashlib.sha256(data).hexdigest()
        if observed != expected_sha256:
            raise VerificationError(
                f"{label} file hash mismatch: "
                f"expected {expected_sha256}, observed {observed}"
            )
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
    result: dict[str, str] = {}
    for root in (base_root, patched_root):
        target_root = root / f".patch_verifier_generated_{run_id}"
        if target_root.exists():
            raise VerificationError(f"generated-test path already exists: {target_root}")
        target_root.mkdir()
        (target_root / "conftest.py").write_bytes(_IMPORT_GUARD_SOURCE)

    used_names: set[str] = set()
    for case in cases:
        relative = _generated_test_relative_path(run_id, case.id)
        name = relative.name
        if name in used_names:  # pragma: no cover - digest makes this implausible
            raise VerificationError(f"generated test name collision for {case.id!r}")
        used_names.add(name)
        contents = frozen_tests[case.id]
        for root in (base_root, patched_root):
            (root / relative).write_bytes(contents)
        result[case.id] = relative.as_posix()
    return result


def _generated_test_relative_path(run_id: str, case_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", case_id).strip("_") or "case"
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8]
    return Path(f".patch_verifier_generated_{run_id}") / (
        f"test_{safe_id}_{digest}.py"
    )


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
    scope_payload: Mapping[str, Any],
    cases: Sequence[CaseVerification],
    context_bytes: bytes,
    evidence_plan_bytes: bytes,
    results_bytes: bytes,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "tool": {"name": "agent-patch-verifier", "version": __version__},
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(snapshot.repository),
        "requested_base_revision": request.base_revision,
        "base_commit": snapshot.base_sha,
        "patched_tree": snapshot.patched_tree_hash,
        "patch_sha256": snapshot.patch_sha256,
        "issue_sha256": hashlib.sha256(issue_bytes).hexdigest(),
        "evidence_plan_sha256": hashlib.sha256(evidence_plan_bytes).hexdigest(),
        "results_sha256": hashlib.sha256(results_bytes).hexdigest(),
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
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
        "scope": dict(scope_payload),
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


def _changed_files_payload(
    changes: Sequence[FileChange],
) -> list[dict[str, Any]]:
    return [
        {
            "old_path": item.old_path,
            "new_path": item.new_path,
            "old_lines": sorted(item.old_lines),
            "new_lines": sorted(item.new_lines),
        }
        for item in changes
    ]


def _scope_payload(
    changes: Sequence[FileChange], symbols: Sequence[ChangedSymbol]
) -> dict[str, Any]:
    return {
        "changed_files": _changed_files_payload(changes),
        "changed_symbols": [item.to_dict() for item in symbols],
    }


def _write_bundle(
    output: Path,
    *,
    manifest_bytes: bytes,
    patch_bytes: bytes,
    issue_bytes: bytes,
    evidence_plan_bytes: bytes,
    results_bytes: bytes,
    frozen_tests: Mapping[str, bytes],
    cases: Sequence[CaseVerification],
    context_bytes: bytes,
    report_bytes: bytes,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent)
    )
    try:
        (temporary / "tests").mkdir()
        (temporary / "harness").mkdir()
        (temporary / "manifest.json").write_bytes(manifest_bytes)
        (temporary / "results.json").write_bytes(results_bytes)
        (temporary / "context.json").write_bytes(context_bytes)
        (temporary / "evidence-plan.json").write_bytes(evidence_plan_bytes)
        (temporary / "issue.md").write_bytes(issue_bytes)
        (temporary / "patch.diff").write_bytes(patch_bytes)
        (temporary / "harness" / "conftest.py").write_bytes(
            _IMPORT_GUARD_SOURCE
        )
        for case in cases:
            destination = temporary / "tests" / Path(case.injected_path).name
            destination.write_bytes(frozen_tests[case.case.id])
        (temporary / "report.md").write_bytes(report_bytes)
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


def _context_payload(
    base: RepositoryContext, patched: RepositoryContext
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "base": base.to_dict(),
        "patched": patched.to_dict(),
    }
    payload["summary"] = {
        "base": _context_summary(payload["base"]),
        "patched": _context_summary(payload["patched"]),
    }
    _validate_context_payload(payload)
    return payload


def _context_document_bytes(context_payload: Mapping[str, Any]) -> bytes:
    return _bounded_json_document(
        context_payload,
        label="repository context",
        max_bytes=_MAX_CONTEXT_ARTIFACT_BYTES,
    )


def _context_summary(snapshot: Mapping[str, Any]) -> dict[str, int]:
    symbols = snapshot["symbols"]
    diagnostics = snapshot["diagnostics"]
    limitations = snapshot["limitations"]
    return {
        "symbols": len(symbols),
        "items": sum(len(symbol["items"]) for symbol in symbols),
        "diagnostics": len(diagnostics),
        "limitations": len(limitations)
        + sum(len(symbol["limitations"]) for symbol in symbols),
        "selection_reasons": sum(
            len(item["selection_reasons"])
            for symbol in symbols
            for item in symbol["items"]
        ),
    }


_CONTEXT_ITEM_KINDS = frozenset(
    {"signature", "docstring", "import", "caller", "nearby_test"}
)
_CONTEXT_SUMMARY_KEYS = frozenset(
    {"symbols", "items", "diagnostics", "limitations", "selection_reasons"}
)
_MAX_CONTEXT_SYMBOLS = 200
_MAX_CONTEXT_DIAGNOSTICS = 200
_MAX_CONTEXT_ITEMS_PER_SYMBOL = 100
_MAX_CONTEXT_ITEMS_PER_SNAPSHOT = 500


def _is_strict_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_context_payload(payload: object) -> Mapping[str, Any]:
    """Validate the portable context artifact without trusting its producer."""

    if not isinstance(payload, Mapping):
        raise ValueError("context must be an object")
    if set(payload) != {"schema_version", "base", "patched", "summary"}:
        raise ValueError("context has unexpected or missing properties")
    version = payload["schema_version"]
    if not _is_strict_integer(version) or version != 1:
        raise ValueError("context schema_version must be 1")

    for revision in ("base", "patched"):
        snapshot = payload[revision]
        if not isinstance(snapshot, Mapping):
            raise ValueError(f"context.{revision} must be an object")
        required = {
            "schema_version",
            "analysis",
            "revision",
            "symbols",
            "diagnostics",
            "limitations",
        }
        if set(snapshot) != required:
            raise ValueError(
                f"context.{revision} has unexpected or missing properties"
            )
        snapshot_version = snapshot["schema_version"]
        if not _is_strict_integer(snapshot_version) or snapshot_version != 1:
            raise ValueError(f"context.{revision}.schema_version must be 1")
        if snapshot["analysis"] != "static":
            raise ValueError(f"context.{revision}.analysis must be static")
        if snapshot["revision"] != revision:
            raise ValueError(f"context.{revision}.revision must be {revision}")
        symbols = snapshot["symbols"]
        diagnostics = snapshot["diagnostics"]
        limitations = snapshot["limitations"]
        if not isinstance(symbols, list):
            raise ValueError(f"context.{revision}.symbols must be an array")
        if len(symbols) > _MAX_CONTEXT_SYMBOLS:
            raise ValueError(
                f"context.{revision}.symbols must contain at most "
                f"{_MAX_CONTEXT_SYMBOLS} entries"
            )
        if not isinstance(diagnostics, list):
            raise ValueError(f"context.{revision}.diagnostics must be an array")
        if len(diagnostics) > _MAX_CONTEXT_DIAGNOSTICS:
            raise ValueError(
                f"context.{revision}.diagnostics must contain at most "
                f"{_MAX_CONTEXT_DIAGNOSTICS} entries"
            )
        if not isinstance(limitations, list) or not all(
            isinstance(item, str) for item in limitations
        ):
            raise ValueError(
                f"context.{revision}.limitations must be an array of strings"
            )
        for symbol in symbols:
            _validate_context_symbol(symbol, revision)
        total_items = sum(len(symbol["items"]) for symbol in symbols)
        if total_items > _MAX_CONTEXT_ITEMS_PER_SNAPSHOT:
            raise ValueError(
                f"context.{revision} must contain at most "
                f"{_MAX_CONTEXT_ITEMS_PER_SNAPSHOT} total items"
            )
        for diagnostic in diagnostics:
            _validate_context_diagnostic(diagnostic, revision)

    summary = payload["summary"]
    if not isinstance(summary, Mapping):
        raise ValueError("context.summary must be an object")
    if set(summary) != {"base", "patched"}:
        raise ValueError("context.summary has unexpected or missing properties")
    for revision in ("base", "patched"):
        counts = summary[revision]
        if not isinstance(counts, Mapping):
            raise ValueError(f"context.summary.{revision} must be an object")
        if set(counts) != _CONTEXT_SUMMARY_KEYS:
            raise ValueError(
                f"context.summary.{revision} has unexpected or missing properties"
            )
        for key in _CONTEXT_SUMMARY_KEYS:
            value = counts[key]
            if not _is_strict_integer(value) or value < 0:
                raise ValueError(
                    f"context.summary.{revision}.{key} must be a non-negative integer"
                )

    expected_summary = {
        "base": _context_summary(payload["base"]),
        "patched": _context_summary(payload["patched"]),
    }
    if summary != expected_summary:
        raise ValueError("context summary counts do not match the snapshots")
    return payload


def _validate_context_diagnostic(diagnostic: object, revision: str) -> None:
    if not isinstance(diagnostic, Mapping):
        raise ValueError(f"context.{revision}.diagnostics entries must be objects")
    if set(diagnostic) != {"code", "path", "message", "line"}:
        raise ValueError(
            f"context.{revision} diagnostic has unexpected or missing properties"
        )
    for key in ("code", "path", "message"):
        if not isinstance(diagnostic[key], str):
            raise ValueError(
                f"context.{revision} diagnostic {key} must be a string"
            )
    line = diagnostic["line"]
    if line is not None and (not _is_strict_integer(line) or line < 1):
        raise ValueError(
            f"context.{revision} diagnostic line must be a positive integer or null"
        )


def _validate_context_symbol(symbol: object, revision: str) -> None:
    if not isinstance(symbol, Mapping):
        raise ValueError(f"context.{revision}.symbols entries must be objects")
    required = {
        "path",
        "qualified_name",
        "kind",
        "analyzed_path",
        "base_path",
        "patched_path",
        "base_occurrence",
        "patched_occurrence",
        "revision_match",
        "items",
        "limitations",
    }
    if set(symbol) != required:
        raise ValueError(
            f"context.{revision} symbol has unexpected or missing properties"
        )
    for key in ("path", "qualified_name", "kind", "revision_match"):
        if not isinstance(symbol[key], str):
            raise ValueError(f"context.{revision} symbol {key} must be a string")
    for key in ("analyzed_path", "base_path", "patched_path"):
        if symbol[key] is not None and not isinstance(symbol[key], str):
            raise ValueError(
                f"context.{revision} symbol {key} must be a string or null"
            )
    for key in ("base_occurrence", "patched_occurrence"):
        value = symbol[key]
        if value is not None and (not _is_strict_integer(value) or value < 1):
            raise ValueError(
                f"context.{revision} symbol {key} must be a positive integer or null"
            )
    limitations = symbol["limitations"]
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise ValueError(
            f"context.{revision} symbol limitations must be an array of strings"
        )
    items = symbol["items"]
    if not isinstance(items, list):
        raise ValueError(f"context.{revision} symbol items must be an array")
    if len(items) > _MAX_CONTEXT_ITEMS_PER_SYMBOL:
        raise ValueError(
            f"context.{revision} symbol items must contain at most "
            f"{_MAX_CONTEXT_ITEMS_PER_SYMBOL} entries"
        )
    for item in items:
        _validate_context_item(item, revision)


def _validate_context_item(item: object, revision: str) -> None:
    if not isinstance(item, Mapping):
        raise ValueError(f"context.{revision} items must be objects")
    required = {
        "kind",
        "path",
        "start_line",
        "end_line",
        "symbol",
        "excerpt",
        "excerpt_truncated",
        "score",
        "rank",
        "selection_reasons",
    }
    if set(item) != required:
        raise ValueError(
            f"context.{revision} item has unexpected or missing properties"
        )
    kind = item["kind"]
    if not isinstance(kind, str) or kind not in _CONTEXT_ITEM_KINDS:
        raise ValueError(f"context.{revision} item kind is not recognized")
    for key in ("path", "excerpt"):
        if not isinstance(item[key], str):
            raise ValueError(f"context.{revision} item {key} must be a string")
    if item["symbol"] is not None and not isinstance(item["symbol"], str):
        raise ValueError(f"context.{revision} item symbol must be a string or null")
    for key in ("start_line", "end_line", "score", "rank"):
        if not _is_strict_integer(item[key]):
            raise ValueError(f"context.{revision} item {key} must be an integer")
    for key in ("start_line", "end_line", "rank"):
        if item[key] < 1:
            raise ValueError(
                f"context.{revision} item {key} must be a positive integer"
            )
    if item["end_line"] < item["start_line"]:
        raise ValueError(
            f"context.{revision} item end_line must not precede start_line"
        )
    if not isinstance(item["excerpt_truncated"], bool):
        raise ValueError(
            f"context.{revision} item excerpt_truncated must be a boolean"
        )
    reasons = item["selection_reasons"]
    if not isinstance(reasons, list) or not reasons:
        raise ValueError(
            f"context.{revision} item selection_reasons must be a non-empty array"
        )
    for reason in reasons:
        if not isinstance(reason, Mapping):
            raise ValueError(
                f"context.{revision} selection reasons must be objects"
            )
        if set(reason) != {"code", "detail", "weight"}:
            raise ValueError(
                f"context.{revision} selection reason has unexpected or missing properties"
            )
        if (
            not isinstance(reason["code"], str)
            or not reason["code"]
            or not isinstance(reason["detail"], str)
            or not reason["detail"]
        ):
            raise ValueError(
                f"context.{revision} selection reason text must be non-empty strings"
            )
        if not _is_strict_integer(reason["weight"]):
            raise ValueError(
                f"context.{revision} selection reason weight must be an integer"
            )


def _json_document(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _bounded_json_document(
    value: object, *, label: str, max_bytes: int
) -> bytes:
    return _bounded_bytes(
        _json_document(value),
        label=label,
        max_bytes=max_bytes,
    )


def _bounded_bytes(data: bytes, *, label: str, max_bytes: int) -> bytes:
    if len(data) > max_bytes:
        raise VerificationError(
            f"generated {label} exceeds the {max_bytes}-byte limit"
        )
    return data


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
    manifest: Mapping[str, Any],
    cases: Sequence[CaseVerification],
    context_payload: Mapping[str, Any],
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
        finding_title = _safe_markdown_inline(
            f"{case.case.id}: {case.finding.summary}", limit=500
        )
        lines.extend(
            [
                f"### {finding_title}",
                "",
                f"- Classification: `{case.finding.kind.value}`",
                "- Requirement: **"
                f"{_safe_markdown_inline(requirement.statement, limit=300)}**",
                f"- Requirement kind: `{requirement.kind.value}`",
                f"- Provenance: `{requirement.provenance.value}` from "
                f"{_safe_code_span(requirement.source, limit=500)}",
                f"- Base: `{base.outcome.value}` ({'stable' if base.stable else 'unstable'})",
                f"- Patched: `{patched.outcome.value}` ({'stable' if patched.stable else 'unstable'})",
                "- Test artifact: "
                f"{_safe_code_span(f'tests/{Path(case.injected_path).name}', limit=500)}",
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
                    *_text_block(
                        requirement.excerpt,
                        limit=2000,
                        truncation_label="excerpt",
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                _safe_markdown_inline(case.finding.rationale, limit=1000),
                "",
                "Execution evidence:",
                "",
            ]
        )
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
                f"- {_safe_code_span(str(symbol['path']), limit=500)}::"
                f"{_safe_code_span(str(symbol['qualified_name']), limit=300)} "
                f"({_safe_code_span(str(symbol['kind']), limit=80)})"
            )
    else:
        lines.append("- No changed Python symbol could be mapped from the diff.")
    lines.extend(["", *_render_context_section(context_payload)])
    lines.extend(
        [
            "## Reproduction material",
            "",
            "This bundle contains `patch.diff`, `issue.md`, the normalized `evidence-plan.json`, separated base/patched static context in `context.json`, exact test files, `manifest.json`, and raw per-attempt output in `results.json`.",
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


def _render_context_section(context_payload: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Repository context",
        "",
        "Static context is reported for review and candidate generation; it does not affect finding classification.",
        "",
        "> Base and patched snapshots are separate evidence domains. Patched context may explain the proposed implementation, but it is not pre-existing support for a requirement.",
        "",
    ]
    max_items = 8
    max_limitations = 8
    max_diagnostics = 5
    for revision, title in (("base", "Base"), ("patched", "Patched")):
        snapshot = context_payload[revision]
        summary = context_payload["summary"][revision]
        lines.extend(
            [
                f"### {title} snapshot",
                "",
                f"- Changed symbols considered: `{summary['symbols']}`",
                f"- Selected context items: `{summary['items']}`",
                f"- Diagnostics: `{summary['diagnostics']}`",
                f"- Recorded limitations: `{summary['limitations']}`",
                "",
            ]
        )
        selected: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for symbol in snapshot["symbols"]:
            for item in symbol["items"]:
                selected.append((symbol, item))
        if selected:
            lines.extend(["Selected items (bounded):", ""])
            for symbol, item in selected[:max_items]:
                reasons = "; ".join(
                    f"{reason['code']}: {reason['detail']}"
                    for reason in item["selection_reasons"]
                )
                location = (
                    f"{item['path']}:{item['start_line']}-{item['end_line']}"
                )
                lines.extend(
                    [
                        f"- {_safe_code_span(location, limit=500)} "
                        f"— {_safe_code_span(str(item['kind']), limit=80)} for "
                        f"{_safe_code_span(str(symbol['qualified_name']), limit=300)} "
                        f"(score `{item['score']}`)",
                        "  - Selection: "
                        f"{_safe_markdown_inline(reasons or 'no reason recorded', limit=300)}",
                    ]
                )
                if item["excerpt"]:
                    lines.extend(
                        [
                            "",
                            *_text_block(
                                item["excerpt"],
                                limit=500,
                                truncation_label="excerpt",
                            ),
                            "",
                        ]
                    )
            omitted = len(selected) - max_items
            if omitted > 0:
                lines.extend(
                    [f"- {omitted} additional item(s) are retained in `context.json`.", ""]
                )
        else:
            lines.extend(["- No static context item was selected.", ""])

        limitations = list(snapshot["limitations"])
        for symbol in snapshot["symbols"]:
            limitations.extend(symbol["limitations"])
        limitations = list(dict.fromkeys(limitations))
        if limitations:
            lines.extend(["Limitations (bounded):", ""])
            for limitation in limitations[:max_limitations]:
                lines.append(
                    f"- {_safe_markdown_inline(str(limitation), limit=300)}"
                )
            if len(limitations) > max_limitations:
                lines.append(
                    f"- {len(limitations) - max_limitations} additional limitation(s) are retained in `context.json`."
                )
            lines.append("")

        diagnostics = snapshot["diagnostics"]
        if diagnostics:
            lines.extend(["Diagnostics (bounded):", ""])
            for diagnostic in diagnostics[:max_diagnostics]:
                line = (
                    ""
                    if diagnostic["line"] is None
                    else f":{diagnostic['line']}"
                )
                diagnostic_location = f"{diagnostic['path']}{line}"
                lines.append(
                    f"- {_safe_code_span(str(diagnostic['code']), limit=80)} at "
                    f"{_safe_code_span(diagnostic_location, limit=500)}: "
                    f"{_safe_markdown_inline(str(diagnostic['message']), limit=300)}"
                )
            if len(diagnostics) > max_diagnostics:
                lines.append(
                    f"- {len(diagnostics) - max_diagnostics} additional diagnostic(s) are retained in `context.json`."
                )
            lines.append("")
    return lines


def _bounded_inline(value: str, *, limit: int = 300) -> str:
    value = " ".join(value.split())
    if len(value) > limit:
        return value[:limit] + f"... (truncated after {limit} characters)"
    return value


_MARKDOWN_INLINE_ESCAPE = re.compile(r"([\\`*{}\[\]()_#+!|>~])")


def _safe_markdown_inline(value: str, *, limit: int = 300) -> str:
    bounded = _bounded_inline(value, limit=limit)
    escaped_html = html.escape(bounded, quote=False)
    return _MARKDOWN_INLINE_ESCAPE.sub(r"\\\1", escaped_html)


def _safe_code_span(value: str, *, limit: int = 500) -> str:
    bounded = _bounded_inline(value, limit=limit)
    fence = "`"
    while fence in bounded:
        fence += "`"
    if fence == "`":
        return f"`{bounded}`"
    return f"{fence} {bounded} {fence}"


def _text_block(
    value: str,
    *,
    limit: int = 4000,
    truncation_label: str = "output",
) -> list[str]:
    if len(value) > limit:
        value = (
            value[:limit]
            + f"\n... {truncation_label} truncated after {limit} characters ..."
        )
    fence = "```"
    while fence in value:
        fence += "`"
    return [f"{fence}text", value.rstrip("\n"), fence]

