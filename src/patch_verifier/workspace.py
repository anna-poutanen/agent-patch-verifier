"""Materialize isolated base and patched Git worktrees.

The verifier needs to execute the same test against two exact source trees while
leaving the developer's checkout alone.  :class:`PatchWorkspace` provides that
small, deliberately Git-specific boundary.  It creates two detached temporary
worktrees at a committed revision and applies the supplied patch only to one of
them.

This module isolates source trees; it is not a security sandbox.  Code executed
inside either worktree still has the permissions of the invoking process.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Self


class WorkspaceError(RuntimeError):
    """Base class for errors while preparing or removing source workspaces."""


class RepositoryError(WorkspaceError):
    """The repository cannot be found or inspected by Git."""


class BaseRevisionError(WorkspaceError):
    """The requested base revision does not resolve to a commit."""


class PatchSourceError(WorkspaceError):
    """The patch source is missing, ambiguous, unreadable, or empty."""


class WorkspaceSetupError(WorkspaceError):
    """Git could not create an isolated worktree."""


class PatchApplyError(WorkspaceError):
    """The patch cannot be applied cleanly to the committed base revision."""


class WorkspaceStateError(WorkspaceError):
    """An operation is invalid for the workspace's current lifecycle state."""


class WorkspaceCleanupError(WorkspaceError):
    """One or more temporary worktrees could not be removed."""


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Immutable provenance for a successfully materialized pair of trees.

    ``base_path`` and ``patched_path`` are usable only while their owning
    :class:`PatchWorkspace` is active.  They are retained here after cleanup so
    a report can say exactly where execution occurred.
    """

    repository: Path
    base_revision: str
    base_sha: str
    base_path: Path
    patched_path: Path
    patched_tree_hash: str
    patch_sha256: str


class PatchWorkspace(AbstractContextManager["PatchWorkspace"]):
    """Create temporary detached worktrees for a base commit and a patch.

    Exactly one of ``patch_text`` and ``patch_file`` must be supplied.
    ``patch_text`` accepts either UTF-8 text or already encoded bytes.  The
    latter is useful for Git's textual binary-patch representation.

    Instances are single-use context managers.  Entering returns the instance,
    whose ``base_path`` and ``patched_path`` properties identify the source
    trees.  Cleanup removes both worktrees even when patch application or the
    context body fails.
    """

    _PREFIX = "patch-verifier-"
    _GIT_TIMEOUT_SECONDS = 60.0
    _GIT_REPOSITORY_ENVIRONMENT = (
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

    def __init__(
        self,
        repository: str | os.PathLike[str],
        base_revision: str,
        *,
        patch_text: str | bytes | None = None,
        patch_file: str | os.PathLike[str] | None = None,
        temp_parent: str | os.PathLike[str] | None = None,
    ) -> None:
        if (patch_text is None) == (patch_file is None):
            raise PatchSourceError(
                "provide exactly one of patch_text or patch_file"
            )
        if not isinstance(base_revision, str) or not base_revision.strip():
            raise BaseRevisionError("base_revision must be a non-empty string")
        if patch_text is not None and not isinstance(patch_text, (str, bytes)):
            raise PatchSourceError("patch_text must be text or bytes")

        self._repository_input = Path(repository).expanduser()
        self._base_revision = base_revision
        self._patch_text = patch_text
        self._patch_file = (
            Path(patch_file).expanduser() if patch_file is not None else None
        )
        self._temp_parent = (
            Path(temp_parent).expanduser() if temp_parent is not None else None
        )

        self._state = "new"
        self._repository: Path | None = None
        self._temp_root: Path | None = None
        self._base_path: Path | None = None
        self._patched_path: Path | None = None
        self._registered_worktrees: list[Path] = []
        self._base_sha: str | None = None
        self._patch_sha256: str | None = None
        self._patched_tree_hash: str | None = None
        self._snapshot: WorkspaceSnapshot | None = None

    def __enter__(self) -> Self:
        if self._state != "new":
            raise WorkspaceStateError(
                "a PatchWorkspace instance can be entered only once"
            )

        self._state = "preparing"
        try:
            patch = self._read_patch()
            self._patch_sha256 = hashlib.sha256(patch).hexdigest()
            self._repository = self._resolve_repository()
            self._base_sha = self._resolve_base_sha()
            self._make_worktrees()
            self._patched_tree_hash = self._apply_patch_and_hash_tree(patch)

            assert self._repository is not None
            assert self._base_path is not None
            assert self._patched_path is not None
            self._snapshot = WorkspaceSnapshot(
                repository=self._repository,
                base_revision=self._base_revision,
                base_sha=self._base_sha,
                base_path=self._base_path,
                patched_path=self._patched_path,
                patched_tree_hash=self._patched_tree_hash,
                patch_sha256=self._patch_sha256,
            )
            self._state = "active"
            return self
        except BaseException as error:
            cleanup_errors = self._cleanup_internal()
            self._state = "cleanup_failed" if cleanup_errors else "closed"
            if cleanup_errors:
                error.add_note("cleanup also failed: " + "; ".join(cleanup_errors))
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        cleanup_errors = self._cleanup_internal()
        self._state = "cleanup_failed" if cleanup_errors else "closed"
        if cleanup_errors:
            message = "failed to remove temporary Git workspaces: " + "; ".join(
                cleanup_errors
            )
            if exc_value is not None:
                exc_value.add_note(message)
            else:
                raise WorkspaceCleanupError(message)
        return False

    @property
    def base_path(self) -> Path:
        """Path to the clean base worktree while this context is active."""

        self._require_active()
        assert self._base_path is not None
        return self._base_path

    @property
    def patched_path(self) -> Path:
        """Path to the patched worktree while this context is active."""

        self._require_active()
        assert self._patched_path is not None
        return self._patched_path

    @property
    def base_sha(self) -> str:
        """Full object ID of the resolved base commit."""

        if self._base_sha is None:
            raise WorkspaceStateError("the base revision has not been resolved")
        return self._base_sha

    @property
    def patched_tree_hash(self) -> str:
        """Git tree object ID for the exact patched source tree."""

        if self._patched_tree_hash is None:
            raise WorkspaceStateError("the patched tree has not been materialized")
        return self._patched_tree_hash

    @property
    def patch_sha256(self) -> str:
        """SHA-256 digest of the exact patch bytes supplied to Git."""

        if self._patch_sha256 is None:
            raise WorkspaceStateError("the patch has not been read")
        return self._patch_sha256

    @property
    def snapshot(self) -> WorkspaceSnapshot:
        """Immutable provenance, available after successful context entry."""

        if self._snapshot is None:
            raise WorkspaceStateError("the workspace has not been materialized")
        return self._snapshot

    def cleanup(self) -> None:
        """Remove an active workspace before the context would normally exit."""

        if self._state == "closed":
            return
        cleanup_errors = self._cleanup_internal()
        self._state = "cleanup_failed" if cleanup_errors else "closed"
        if cleanup_errors:
            raise WorkspaceCleanupError(
                "failed to remove temporary Git workspaces: "
                + "; ".join(cleanup_errors)
            )

    def _require_active(self) -> None:
        if self._state != "active":
            raise WorkspaceStateError(
                "workspace paths are available only inside the active context"
            )

    def _read_patch(self) -> bytes:
        if self._patch_text is not None:
            patch = (
                self._patch_text.encode("utf-8")
                if isinstance(self._patch_text, str)
                else self._patch_text
            )
        else:
            assert self._patch_file is not None
            try:
                patch = self._patch_file.read_bytes()
            except OSError as error:
                raise PatchSourceError(
                    f"cannot read patch file {self._patch_file}: {error.strerror or error}"
                ) from error

        if not patch.strip():
            raise PatchSourceError("the supplied patch is empty")
        return patch

    def _resolve_repository(self) -> Path:
        try:
            candidate = self._repository_input.resolve(strict=True)
        except OSError as error:
            raise RepositoryError(
                f"repository path does not exist: {self._repository_input}"
            ) from error
        if not candidate.is_dir():
            raise RepositoryError(f"repository path is not a directory: {candidate}")

        try:
            output = self._git_output(candidate, "rev-parse", "--show-toplevel")
        except _GitCommandError as error:
            raise RepositoryError(
                f"not a usable non-bare Git worktree: {candidate} ({error.detail})"
            ) from error
        return Path(output).resolve()

    def _resolve_base_sha(self) -> str:
        assert self._repository is not None
        revision = f"{self._base_revision}^{{commit}}"
        try:
            return self._git_output(
                self._repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                revision,
            )
        except _GitCommandError as error:
            raise BaseRevisionError(
                f"base revision {self._base_revision!r} does not resolve to a commit "
                f"({error.detail})"
            ) from error

    def _make_worktrees(self) -> None:
        assert self._repository is not None
        assert self._base_sha is not None

        try:
            if self._temp_parent is not None:
                parent = self._temp_parent.resolve(strict=True)
                if not parent.is_dir():
                    raise NotADirectoryError(parent)
            else:
                parent = None
            self._temp_root = Path(
                tempfile.mkdtemp(prefix=self._PREFIX, dir=parent)
            ).resolve()
        except OSError as error:
            raise WorkspaceSetupError(
                f"cannot create temporary workspace: {error}"
            ) from error

        # Keep the terminal directory name identical so ordinary code cannot
        # distinguish revisions merely through ``Path.cwd().name``.
        base_parent = self._temp_root / "base"
        patched_parent = self._temp_root / "patched"
        base_parent.mkdir()
        patched_parent.mkdir()
        self._base_path = base_parent / "worktree"
        self._patched_path = patched_parent / "worktree"
        for label, path in (
            ("base", self._base_path),
            ("patched", self._patched_path),
        ):
            try:
                self._git_output(
                    self._repository,
                    "worktree",
                    "add",
                    "--detach",
                    str(path),
                    self._base_sha,
                )
            except _GitCommandError as error:
                raise WorkspaceSetupError(
                    f"cannot create detached {label} worktree ({error.detail})"
                ) from error
            self._registered_worktrees.append(path)

    def _apply_patch_and_hash_tree(self, patch: bytes) -> str:
        assert self._patched_path is not None
        assert self._temp_root is not None

        try:
            index_output = self._git_output(
                self._patched_path, "rev-parse", "--git-path", "index"
            )
            source_index = Path(index_output)
            if not source_index.is_absolute():
                source_index = (self._patched_path / source_index).resolve()
            temporary_index = self._temp_root / "patched-index"
            shutil.copyfile(source_index, temporary_index)
        except (OSError, _GitCommandError) as error:
            detail = error.detail if isinstance(error, _GitCommandError) else str(error)
            raise WorkspaceSetupError(
                f"cannot prepare an isolated Git index ({detail})"
            ) from error

        environment = {"GIT_INDEX_FILE": str(temporary_index)}
        try:
            self._git_output(
                self._patched_path,
                "apply",
                "--check",
                "--index",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=patch,
                extra_environment=environment,
            )
            self._git_output(
                self._patched_path,
                "apply",
                "--index",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=patch,
                extra_environment=environment,
            )
            return self._git_output(
                self._patched_path,
                "write-tree",
                extra_environment=environment,
            )
        except _GitCommandError as error:
            raise PatchApplyError(
                f"patch does not apply cleanly to {self._base_sha} ({error.detail})"
            ) from error
        finally:
            try:
                temporary_index.unlink(missing_ok=True)
            except OSError:
                # The containing temporary directory is removed during cleanup.
                pass

    def _cleanup_internal(self) -> list[str]:
        errors: list[str] = []
        if self._repository is not None:
            failed_worktrees: set[Path] = set()
            for path in reversed(self._registered_worktrees):
                try:
                    self._git_output(
                        self._repository,
                        "worktree",
                        "remove",
                        "--force",
                        str(path),
                    )
                except (_GitCommandError, WorkspaceError, OSError) as error:
                    detail = (
                        error.detail
                        if isinstance(error, _GitCommandError)
                        else str(error)
                    )
                    errors.append(f"{path}: {detail}")
                    failed_worktrees.add(path)
            self._registered_worktrees = [
                path
                for path in self._registered_worktrees
                if path in failed_worktrees
            ]

        # A failed Git removal leaves administrative data pointing into the
        # temporary root. Keep that root intact so cleanup can be retried
        # without turning the registration into a stale, pathless worktree.
        if self._temp_root is not None and not self._registered_worktrees:
            try:
                shutil.rmtree(self._temp_root)
            except FileNotFoundError:
                self._temp_root = None
            except OSError as error:
                errors.append(f"{self._temp_root}: {error}")
            else:
                self._temp_root = None
        return errors

    @staticmethod
    def _git_output(
        cwd: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
        extra_environment: dict[str, str] | None = None,
    ) -> str:
        environment = os.environ.copy()
        for key in PatchWorkspace._GIT_REPOSITORY_ENVIRONMENT:
            environment.pop(key, None)
        for key in tuple(environment):
            if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                environment.pop(key, None)
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        if extra_environment:
            environment.update(extra_environment)

        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "advice.detachedHead=false",
            "-C",
            str(cwd),
            *arguments,
        ]
        try:
            completed = subprocess.run(
                command,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
                timeout=PatchWorkspace._GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as error:
            raise RepositoryError("Git executable was not found") from error
        except OSError as error:
            raise RepositoryError(f"cannot execute Git: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RepositoryError(
                f"Git timed out after {PatchWorkspace._GIT_TIMEOUT_SECONDS:g} seconds"
            ) from error

        if completed.returncode != 0:
            raise _GitCommandError(
                arguments=arguments,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return completed.stdout.decode("utf-8", errors="replace").strip()


class _GitCommandError(Exception):
    """Internal subprocess failure translated to a public domain error."""

    def __init__(
        self,
        *,
        arguments: tuple[str, ...],
        returncode: int,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        self.arguments = arguments
        self.returncode = returncode
        message = stderr.decode("utf-8", errors="replace").strip()
        if not message:
            message = stdout.decode("utf-8", errors="replace").strip()
        self.detail = message or f"Git exited with status {returncode}"
        super().__init__(self.detail)

