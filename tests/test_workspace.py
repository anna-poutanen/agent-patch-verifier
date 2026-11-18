from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from patch_verifier.workspace import (
    BaseRevisionError,
    PatchApplyError,
    PatchSourceError,
    PatchWorkspace,
    WorkspaceCleanupError,
    WorkspaceStateError,
    _GitCommandError,
)


PATCH = b"""\
diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1,2 +1,2 @@
 def resolve(value):
-    return value or 5
+    return 0
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _make_repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Patch Verifier Tests")
    _git(path, "config", "user.email", "patch-verifier@example.invalid")
    (path / "value.py").write_text(
        "def resolve(value):\n    return value or 5\n", encoding="utf-8"
    )
    (path / "keep.txt").write_text("committed\n", encoding="utf-8")
    _git(path, "add", "value.py", "keep.txt")
    _git(path, "commit", "--quiet", "-m", "initial")
    return path, _git(path, "rev-parse", "HEAD")


def test_materializes_detached_trees_and_preserves_caller_checkout(
    tmp_path: Path,
) -> None:
    repository, base_sha = _make_repository(tmp_path / "repository")

    # A dirty caller checkout is deliberate: both temporary trees must still
    # start at the committed revision, and cleanup must not touch this change.
    (repository / "value.py").write_text(
        "def resolve(value):\n    return 99\n", encoding="utf-8"
    )
    (repository / "untracked.txt").write_text("developer data\n", encoding="utf-8")
    status_before = _git(repository, "status", "--short")
    caller_bytes_before = (repository / "value.py").read_bytes()
    worktrees_before = _git(repository, "worktree", "list", "--porcelain")

    workspace = PatchWorkspace(
        repository,
        "HEAD",
        patch_text=PATCH,
        temp_parent=tmp_path,
    )
    with workspace as active:
        assert active is workspace
        base_path = workspace.base_path
        patched_path = workspace.patched_path
        snapshot = workspace.snapshot

        assert base_path != patched_path
        assert base_path.name == patched_path.name == "worktree"
        assert _git(base_path, "rev-parse", "HEAD") == base_sha
        assert _git(patched_path, "rev-parse", "HEAD") == base_sha
        assert _git(base_path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert _git(patched_path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert (base_path / "value.py").read_text(encoding="utf-8").endswith(
            "return value or 5\n"
        )
        assert (patched_path / "value.py").read_text(encoding="utf-8").endswith(
            "return 0\n"
        )
        assert _git(base_path, "status", "--short") == ""
        assert _git(patched_path, "status", "--short") == "M value.py"

        base_tree = _git(repository, "rev-parse", f"{base_sha}^{{tree}}")
        assert workspace.base_sha == base_sha
        assert workspace.patched_tree_hash != base_tree
        assert (
            _git(repository, "show", f"{workspace.patched_tree_hash}:value.py")
            == "def resolve(value):\n    return 0"
        )
        assert workspace.patch_sha256 == hashlib.sha256(PATCH).hexdigest()
        assert snapshot.base_sha == base_sha
        assert snapshot.patched_tree_hash == workspace.patched_tree_hash

        # Nothing in the caller's worktree changes even while both temporary
        # worktrees are registered.
        assert (repository / "value.py").read_bytes() == caller_bytes_before
        assert _git(repository, "status", "--short") == status_before

    assert not base_path.exists()
    assert not patched_path.exists()
    assert not base_path.parent.exists()
    assert _git(repository, "worktree", "list", "--porcelain") == worktrees_before
    assert (repository / "value.py").read_bytes() == caller_bytes_before
    assert _git(repository, "status", "--short") == status_before
    assert _git(repository, "rev-parse", "HEAD") == base_sha

    # Provenance remains usable for a report, but stale execution paths do not.
    assert workspace.snapshot == snapshot
    with pytest.raises(WorkspaceStateError, match="active context"):
        _ = workspace.base_path
    with pytest.raises(WorkspaceStateError, match="only once"):
        workspace.__enter__()


def test_reads_patch_file_and_cleans_up_when_context_body_fails(
    tmp_path: Path,
) -> None:
    repository, _ = _make_repository(tmp_path / "repository")
    patch_file = tmp_path / "proposed.diff"
    patch_file.write_bytes(PATCH)

    workspace = PatchWorkspace(
        repository,
        "HEAD",
        patch_file=patch_file,
        temp_parent=tmp_path,
    )
    with pytest.raises(RuntimeError, match="body failed"):
        with workspace:
            root = workspace.base_path.parent
            raise RuntimeError("body failed")

    assert not root.exists()
    assert len(_git(repository, "worktree", "list").splitlines()) == 1


def test_cleanup_retries_a_transient_worktree_remove_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _make_repository(tmp_path / "repository")
    workspace = PatchWorkspace(
        repository,
        "HEAD",
        patch_text=PATCH,
        temp_parent=tmp_path,
    )
    original_git_output = workspace._git_output
    failed_once = False

    def flaky_git_output(
        cwd: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
        extra_environment: dict[str, str] | None = None,
    ) -> str:
        nonlocal failed_once
        if arguments[:2] == ("worktree", "remove") and not failed_once:
            failed_once = True
            raise _GitCommandError(
                arguments=arguments,
                returncode=1,
                stdout=b"",
                stderr=b"transient remove failure",
            )
        return original_git_output(
            cwd,
            *arguments,
            input_bytes=input_bytes,
            extra_environment=extra_environment,
        )

    with pytest.raises(WorkspaceCleanupError, match="transient remove failure"):
        with workspace:
            temporary_root = workspace.base_path.parent
            patched_path = workspace.patched_path
            monkeypatch.setattr(workspace, "_git_output", flaky_git_output)

    assert temporary_root.exists()
    assert patched_path.exists()
    assert len(_git(repository, "worktree", "list").splitlines()) == 2

    monkeypatch.setattr(workspace, "_git_output", original_git_output)
    workspace.cleanup()

    assert not temporary_root.exists()
    assert len(_git(repository, "worktree", "list").splitlines()) == 1


def test_invalid_patch_reports_domain_error_and_removes_partial_workspace(
    tmp_path: Path,
) -> None:
    repository, _ = _make_repository(tmp_path / "repository")
    status_before = _git(repository, "status", "--short")

    with pytest.raises(PatchApplyError, match="does not apply cleanly"):
        with PatchWorkspace(
            repository,
            "HEAD",
            patch_text=PATCH.replace(b"return value or 5", b"return missing"),
            temp_parent=tmp_path,
        ):
            pytest.fail("an invalid patch must not enter its context")

    assert not list(tmp_path.glob("patch-verifier-*"))
    assert len(_git(repository, "worktree", "list").splitlines()) == 1
    assert _git(repository, "status", "--short") == status_before


def test_invalid_base_revision_reports_domain_error_without_temp_files(
    tmp_path: Path,
) -> None:
    repository, _ = _make_repository(tmp_path / "repository")

    with pytest.raises(BaseRevisionError, match="does not resolve to a commit"):
        with PatchWorkspace(
            repository,
            "does-not-exist",
            patch_text=PATCH,
            temp_parent=tmp_path,
        ):
            pytest.fail("an invalid base must not enter its context")

    assert not list(tmp_path.glob("patch-verifier-*"))
    assert len(_git(repository, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize(
    ("patch_text", "patch_file"),
    [
        (None, None),
        (PATCH, "proposed.diff"),
        (b"", None),
    ],
)
def test_rejects_ambiguous_or_empty_patch_sources(
    tmp_path: Path,
    patch_text: bytes | None,
    patch_file: str | None,
) -> None:
    repository, _ = _make_repository(tmp_path / "repository")

    if patch_text == b"":
        workspace = PatchWorkspace(repository, "HEAD", patch_text=patch_text)
        with pytest.raises(PatchSourceError, match="empty"):
            workspace.__enter__()
    else:
        with pytest.raises(PatchSourceError, match="exactly one"):
            PatchWorkspace(
                repository,
                "HEAD",
                patch_text=patch_text,
                patch_file=patch_file,
            )


@pytest.mark.parametrize("source", ["text", "file"])
def test_rejects_oversized_patch_before_repository_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(PatchWorkspace, "_MAX_PATCH_BYTES", len(PATCH) - 1)
    patch_file = tmp_path / "oversized.diff"
    patch_file.write_bytes(PATCH)
    arguments = (
        {"patch_text": PATCH}
        if source == "text"
        else {"patch_file": patch_file}
    )

    workspace = PatchWorkspace(tmp_path / "missing-repository", "HEAD", **arguments)

    with pytest.raises(PatchSourceError, match="exceeds"):
        workspace.__enter__()
