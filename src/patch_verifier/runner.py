"""Deterministic subprocess primitives for comparing pytest revisions.

The runner deliberately knows very little about Git or report generation.  It
receives two already-materialized workspaces and executes exactly the same
command in each one.  Keeping that boundary small makes the captured results
safe to consume from a higher-level verifier orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import MappingProxyType
from typing import BinaryIO, Mapping, Sequence
import xml.etree.ElementTree as ET


PYTHONHASHSEED = "0"
_TIMEOUT_CLEANUP_SECONDS = 0.25
OUTPUT_LIMIT_BYTES = 1_000_000
_REPOSITORY_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_EXEC_PATH",
)


class Revision(str, Enum):
    """The two revisions participating in a verification run."""

    BASE = "base"
    PATCHED = "patched"


class CommandStatus(str, Enum):
    """Outcome of one command invocation.

    Pytest uses exit code 0 for success and 1 for collected test failures.
    Its other documented exit codes describe interruption, internal errors,
    command-line misuse, or an empty collection, all of which are runner
    errors rather than evidence that an assertion failed.
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"


class RunStability(str, Enum):
    """Whether repeated invocations agree on their command status."""

    STABLE = "stable"
    UNSTABLE = "unstable"


@dataclass(frozen=True, slots=True)
class RevisionWorkspace:
    """A materialized checkout associated with one side of the patch."""

    revision: Revision
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.revision, Revision):
            object.__setattr__(self, "revision", Revision(self.revision))
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class TestCommand:
    """An explicit, shell-free command and optional test path.

    ``test_path`` is appended as the last argument.  A relative path is
    resolved by the child process against its revision workspace, while an
    absolute path can point at one generated test shared by both revisions.
    """

    command: tuple[str, ...]
    test_path: Path | None = None
    junit_xml_path: Path | None = None
    expected_node: str | None = None

    def __post_init__(self) -> None:
        command = _coerce_command(self.command)
        if not command:
            raise ValueError("command must contain at least one argument")
        object.__setattr__(self, "command", command)
        if self.test_path is not None:
            object.__setattr__(self, "test_path", Path(self.test_path))
        if self.junit_xml_path is not None:
            junit_path = Path(self.junit_xml_path)
            if junit_path.is_absolute() or ".." in junit_path.parts:
                raise ValueError("junit_xml_path must be a safe relative path")
            object.__setattr__(self, "junit_xml_path", junit_path)
        if self.expected_node is not None:
            expected_node = self.expected_node.strip()
            if not expected_node:
                raise ValueError("expected_node must not be empty")
            object.__setattr__(self, "expected_node", expected_node)

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the exact argument vector passed to the child process."""

        result = self.command
        if self.junit_xml_path is not None:
            result = (
                *result,
                "--runxfail",
                f"--junitxml={os.fspath(self.junit_xml_path)}",
            )
        if self.test_path is not None:
            result = (*result, os.fspath(self.test_path))
        return result


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Controls common to every invocation in a dual run."""

    timeout_seconds: float = 60.0
    repeat: int = 1
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.repeat, bool) or not isinstance(self.repeat, int):
            raise TypeError("repeat must be an integer")
        if self.repeat < 1:
            raise ValueError("repeat must be at least 1")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a number")
        if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite and greater than 0")

        environment = dict(self.environment)
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("environment keys and values must be strings")
        # A defensive copy prevents callers from changing an in-flight run.
        object.__setattr__(self, "environment", MappingProxyType(environment))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured evidence from one invocation in one revision workspace."""

    revision: Revision
    workspace: Path
    attempt: int
    argv: tuple[str, ...]
    status: CommandStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    failure_signature: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is CommandStatus.PASS


@dataclass(slots=True)
class _OutputCapture:
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        remaining = OUTPUT_LIMIT_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RevisionResult:
    """All repeated command results for a single revision."""

    target: RevisionWorkspace
    attempts: tuple[CommandResult, ...]

    def __post_init__(self) -> None:
        if not self.attempts:
            raise ValueError("a revision result must contain at least one attempt")
        if any(result.revision is not self.target.revision for result in self.attempts):
            raise ValueError("attempt revision does not match its target")

    @property
    def stability(self) -> RunStability:
        first_status = self.attempts[0].status
        if all(result.status is first_status for result in self.attempts[1:]):
            return RunStability.STABLE
        return RunStability.UNSTABLE

    @property
    def status(self) -> CommandStatus | None:
        """Return the agreed status, or ``None`` for an unstable aggregate."""

        if self.stability is RunStability.UNSTABLE:
            return None
        return self.attempts[0].status

    @property
    def is_stable(self) -> bool:
        return self.stability is RunStability.STABLE


@dataclass(frozen=True, slots=True)
class DualRunResult:
    """Paired execution evidence for the base and patched workspaces."""

    base: RevisionResult
    patched: RevisionResult

    def __post_init__(self) -> None:
        if self.base.target.revision is not Revision.BASE:
            raise ValueError("base result must use the base revision")
        if self.patched.target.revision is not Revision.PATCHED:
            raise ValueError("patched result must use the patched revision")

    def for_revision(self, revision: Revision) -> RevisionResult:
        """Select a side without requiring callers to branch on field names."""

        revision = Revision(revision)
        return self.base if revision is Revision.BASE else self.patched


def classify_exit_code(exit_code: int) -> CommandStatus:
    """Map a completed pytest-style process exit code to a command status."""

    if exit_code == 0:
        return CommandStatus.PASS
    if exit_code == 1:
        return CommandStatus.FAIL
    return CommandStatus.ERROR


def run_revision(
    target: RevisionWorkspace,
    command: TestCommand,
    options: RunOptions | None = None,
) -> RevisionResult:
    """Execute ``command`` repeatedly in one materialized workspace."""

    if options is None:
        options = RunOptions()
    attempts = tuple(
        _run_once(target, command, options, attempt)
        for attempt in range(1, options.repeat + 1)
    )
    return RevisionResult(target=target, attempts=attempts)


def run_dual(
    base_workspace: str | os.PathLike[str],
    patched_workspace: str | os.PathLike[str],
    command: TestCommand,
    options: RunOptions | None = None,
    *,
    start_with_patched: bool = False,
) -> DualRunResult:
    """Run the same command in base and patched workspaces.

    The caller is responsible for materializing the workspaces and installing
    their dependencies.  No shell is involved, and both revisions receive the
    same command, timeout, repeat count, and environment overrides.
    """

    base = RevisionWorkspace(Revision.BASE, Path(base_workspace))
    patched = RevisionWorkspace(Revision.PATCHED, Path(patched_workspace))
    if options is None:
        options = RunOptions()
    base_attempts: list[CommandResult] = []
    patched_attempts: list[CommandResult] = []
    for attempt in range(1, options.repeat + 1):
        # Reverse the order on alternating attempts. This makes monotonic
        # external-state contamination become instability instead of a stable
        # but false base/patched differential in the common repeat>=2 case.
        base_first = (attempt % 2 == 1) != start_with_patched
        targets = (base, patched) if base_first else (patched, base)
        for target in targets:
            result = _run_once(target, command, options, attempt)
            if target.revision is Revision.BASE:
                base_attempts.append(result)
            else:
                patched_attempts.append(result)
    return DualRunResult(
        base=RevisionResult(base, tuple(base_attempts)),
        patched=RevisionResult(patched, tuple(patched_attempts)),
    )


def _run_once(
    target: RevisionWorkspace,
    command: TestCommand,
    options: RunOptions,
    attempt: int,
) -> CommandResult:
    started = time.perf_counter()
    argv = command.argv
    try:
        environment = os.environ.copy()
        # Never inherit a caller checkout through PYTHONPATH. Prefer source
        # inside the active revision, including common Python package layouts.
        for inherited_key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            environment.pop(inherited_key, None)
        environment.update(options.environment)
        for inherited_key in (*_REPOSITORY_ENVIRONMENT, "OLDPWD"):
            environment.pop(inherited_key, None)
        for inherited_key in tuple(environment):
            if inherited_key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                environment.pop(inherited_key, None)
        # Revision attribution must not depend on the caller's shell location
        # or repository-selection variables.
        environment["PWD"] = str(target.path.resolve())
        runtime_temp = target.path / ".patch_verifier_runtime"
        runtime_temp.mkdir(exist_ok=True)
        for temp_key in ("TMPDIR", "TMP", "TEMP"):
            environment[temp_key] = str(runtime_temp)
        configured_pythonpath = environment.pop("PYTHONPATH", "")
        local_pythonpath = [str(target.path)]
        for source_directory in ("src", "lib", "python", "source"):
            candidate = target.path / source_directory
            if candidate.is_dir():
                local_pythonpath.append(str(candidate))
        if configured_pythonpath:
            local_pythonpath.append(configured_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(local_pythonpath)
        # Applied after overrides so both revisions use the same hash seed.
        environment["PYTHONHASHSEED"] = PYTHONHASHSEED

        junit_path = (
            None
            if command.junit_xml_path is None
            else target.path / command.junit_xml_path
        )
        if junit_path is not None:
            junit_path.parent.mkdir(parents=True, exist_ok=True)
            junit_path.unlink(missing_ok=True)

        popen_options: dict[str, object] = {}
        if os.name == "posix":
            # Give the test process its own group so a timeout also terminates
            # descendants that inherited its output pipes.
            popen_options["start_new_session"] = True
        process: subprocess.Popen[bytes] = subprocess.Popen(
            argv,
            cwd=target.path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            **popen_options,
        )
        stdout_capture, stdout_thread = _capture_pipe(process.stdout)
        stderr_capture, stderr_thread = _capture_pipe(process.stderr)
        try:
            process.wait(timeout=options.timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            _bounded_process_wait(process)
            _finish_capture(process.stdout, stdout_thread, stdout_capture)
            _finish_capture(process.stderr, stderr_thread, stderr_capture)
            return _result(
                target,
                attempt,
                argv,
                CommandStatus.TIMEOUT,
                None,
                stdout_capture.text(),
                stderr_capture.text(),
                started,
                stdout_truncated=stdout_capture.truncated,
                stderr_truncated=stderr_capture.truncated,
            )
        _finish_capture(process.stdout, stdout_thread, stdout_capture)
        _finish_capture(process.stderr, stderr_thread, stderr_capture)
        exit_code = process.returncode
        status = (
            classify_exit_code(exit_code)
            if junit_path is None
            else classify_junit_result(
                exit_code,
                junit_path,
                expected_node=command.expected_node,
            )
        )
        failure_signature = (
            None
            if junit_path is None
            else junit_failure_signature(junit_path)
        )
        return _result(
            target,
            attempt,
            argv,
            status,
            exit_code,
            stdout_capture.text(),
            stderr_capture.text(),
            started,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            failure_signature=failure_signature,
        )
    except OSError as exc:
        # Missing commands and invalid workspaces are recorded as execution
        # evidence so one bad revision does not discard the other side.
        return _result(
            target,
            attempt,
            argv,
            CommandStatus.ERROR,
            None,
            "",
            f"{type(exc).__name__}: {exc}",
            started,
        )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()


def _bounded_process_wait(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_TIMEOUT_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()


def _capture_pipe(
    pipe: BinaryIO | None,
) -> tuple[_OutputCapture, threading.Thread]:
    capture = _OutputCapture()

    def drain() -> None:
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    return
                capture.append(chunk)
        except (OSError, ValueError):
            capture.truncated = True

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return capture, thread


def _finish_capture(
    pipe: BinaryIO | None,
    thread: threading.Thread,
    capture: _OutputCapture,
) -> None:
    thread.join(timeout=_TIMEOUT_CLEANUP_SECONDS)
    if thread.is_alive():
        capture.truncated = True
        if pipe is not None:
            try:
                os.close(pipe.fileno())
            except OSError:
                pass
        thread.join(timeout=_TIMEOUT_CLEANUP_SECONDS)
    if pipe is not None:
        try:
            pipe.close()
        except OSError:
            pass


def classify_junit_result(
    exit_code: int,
    path: Path,
    *,
    expected_node: str | None = None,
) -> CommandStatus:
    """Classify pytest using its machine-readable JUnit report.

    Setup/teardown errors, collection problems, skipped execution, unrelated
    test items, non-assertion exceptions, and a missing or malformed report are
    harness errors. Only an ``AssertionError`` or explicit ``pytest.fail`` is
    treated as evidence that the candidate assertion failed.
    """

    if exit_code not in {0, 1}:
        return CommandStatus.ERROR
    root = _load_junit_root(path)
    if root is None:
        return CommandStatus.ERROR
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    testcases = list(root.iter("testcase"))
    if not suites or len(testcases) != 1:
        return CommandStatus.ERROR
    try:
        tests = sum(int(item.attrib.get("tests", "0")) for item in suites)
        errors = sum(int(item.attrib.get("errors", "0")) for item in suites)
        failures = sum(int(item.attrib.get("failures", "0")) for item in suites)
        skipped = sum(int(item.attrib.get("skipped", "0")) for item in suites)
    except (TypeError, ValueError):
        return CommandStatus.ERROR
    if tests != 1 or errors > 0 or skipped > 0:
        return CommandStatus.ERROR
    testcase = testcases[0]
    if expected_node is not None and not _testcase_matches_node(
        testcase, expected_node
    ):
        return CommandStatus.ERROR
    if testcase.findall("error") or testcase.findall("skipped"):
        return CommandStatus.ERROR
    failure_elements = testcase.findall("failure")
    if failures != len(failure_elements):
        return CommandStatus.ERROR
    if failure_elements:
        if all(_is_assertion_failure(element) for element in failure_elements):
            return CommandStatus.FAIL
        return CommandStatus.ERROR
    return CommandStatus.PASS if exit_code == 0 else CommandStatus.ERROR


def junit_failure_signature(path: Path) -> str | None:
    """Return a path-independent digest for the observed JUnit failure."""

    root = _load_junit_root(path)
    if root is None:
        return None
    elements = [
        element
        for testcase in root.iter("testcase")
        for tag in ("failure", "error", "skipped")
        for element in testcase.findall(tag)
    ]
    if not elements:
        return None
    parts: list[str] = []
    for element in elements:
        parts.extend(
            [
                element.tag,
                element.attrib.get("type", ""),
                element.attrib.get("message", ""),
            ]
        )
        parts.extend(
            line.strip()
            for line in (element.text or "").splitlines()
            if line.lstrip().startswith("E ")
        )
    payload = "\0".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _load_junit_root(path: Path) -> ET.Element | None:
    try:
        with path.open("rb") as stream:
            payload = stream.read(OUTPUT_LIMIT_BYTES + 1)
    except OSError:
        return None
    if len(payload) > OUTPUT_LIMIT_BYTES:
        return None
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        return None


def _testcase_matches_node(testcase: ET.Element, expected_node: str) -> bool:
    """Conservatively associate one JUnit item with the requested pytest node."""

    parts = expected_node.split("::")
    if testcase.attrib.get("name") != parts[-1]:
        return False
    if len(parts) == 1:
        return True
    classname_parts = testcase.attrib.get("classname", "").split(".")
    return classname_parts[-(len(parts) - 1) :] == parts[:-1]


def _is_assertion_failure(failure: ET.Element) -> bool:
    """Recognize pytest's assertion outcomes without accepting arbitrary errors."""

    failure_type = failure.attrib.get("type", "").rsplit(".", 1)[-1]
    if failure_type in {"AssertionError", "Failed"}:
        return True
    message = failure.attrib.get("message", "").lstrip()
    if message.startswith(("assert ", "AssertionError", "Failed:")):
        return True
    detail = failure.text or ""
    return any(
        line.lstrip().startswith(("E assert ", "E AssertionError", "E Failed:"))
        for line in detail.splitlines()
    )


def _result(
    target: RevisionWorkspace,
    attempt: int,
    argv: tuple[str, ...],
    status: CommandStatus,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    started: float,
    *,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    failure_signature: str | None = None,
) -> CommandResult:
    return CommandResult(
        revision=target.revision,
        workspace=target.path,
        attempt=attempt,
        argv=argv,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.perf_counter() - started,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        failure_signature=failure_signature,
    )


def _coerce_command(command: Sequence[object]) -> tuple[str, ...]:
    result: list[str] = []
    for argument in command:
        if isinstance(argument, str):
            result.append(argument)
        elif isinstance(argument, os.PathLike):
            result.append(os.fspath(argument))
        else:
            raise TypeError("command arguments must be strings or path-like objects")
    return tuple(result)
