"""End-to-end orchestration for review-only requirement drafting."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from . import __version__
from .change_analysis import analyze_changed_symbols, parse_unified_diff
from .context import ContextRevision, collect_repository_context
from .draft_validation import validate_requirement_draft_artifact
from .patches import is_binary_patch
from .requirements import draft_requirements
from .workspace import PatchWorkspace


_MAX_PATCH_BYTES = 20 * 1024 * 1024
_MAX_ISSUE_BYTES = 1_000_000
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


class DraftingError(RuntimeError):
    """A requirement draft could not be created or written faithfully."""


@dataclass(frozen=True)
class DraftRequirementsRequest:
    """Inputs for deriving review-only requirements from an exact patch."""

    repository: Path
    base_revision: str
    patch_file: Path
    issue_file: Path
    output_file: Path

    def __post_init__(self) -> None:
        if not isinstance(self.base_revision, str) or not self.base_revision.strip():
            raise ValueError("base_revision must be a non-empty string")
        object.__setattr__(self, "repository", Path(self.repository).resolve())
        object.__setattr__(self, "patch_file", Path(self.patch_file).resolve())
        object.__setattr__(self, "issue_file", Path(self.issue_file).resolve())
        object.__setattr__(self, "output_file", Path(self.output_file).resolve())


@dataclass(frozen=True)
class DraftRequirementsResult:
    """A successfully written requirement-draft artifact."""

    output_file: Path
    candidate_count: int
    truncated: bool


def draft_patch_requirements(
    request: DraftRequirementsRequest,
) -> DraftRequirementsResult:
    """Analyze a patch and write a provenance-bound requirement draft.

    This function never accepts candidates as requirements and never executes
    generated tests.  Its output is a review queue whose every source excerpt
    is tied to the exact base commit and input hashes recorded in the envelope.
    """

    if request.output_file.exists():
        raise DraftingError(f"output file already exists: {request.output_file}")

    patch_bytes = _read_input(
        request.patch_file, "patch", max_bytes=_MAX_PATCH_BYTES
    )
    if is_binary_patch(patch_bytes):
        raise DraftingError("binary patches are outside the current MVP scope")
    issue_bytes = _read_input(
        request.issue_file, "issue", max_bytes=_MAX_ISSUE_BYTES
    )
    try:
        patch_text = patch_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DraftingError("patch file must be UTF-8") from exc

    changes = parse_unified_diff(patch_text)
    with PatchWorkspace(
        request.repository,
        request.base_revision,
        patch_text=patch_bytes,
    ) as workspace:
        symbols = analyze_changed_symbols(
            workspace.base_path,
            workspace.patched_path,
            changes,
        )
        base_context = collect_repository_context(
            workspace.base_path,
            symbols,
            revision=ContextRevision.BASE,
        )
        draft = draft_requirements(
            issue_bytes,
            workspace.base_path,
            base_context,
            issue_source=request.issue_file.name,
        )
        base_context_bytes = json.dumps(
            base_context.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        snapshot = workspace.snapshot

    payload: dict[str, Any] = {
        "schema_version": 1,
        "tool_version": __version__,
        "environment": {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": sys.platform,
        },
        "inputs": {
            "repository": str(snapshot.repository),
            "base_revision": request.base_revision,
            "base_sha": snapshot.base_sha,
            "patched_tree_hash": snapshot.patched_tree_hash,
            "patch_sha256": snapshot.patch_sha256,
            "issue_sha256": hashlib.sha256(issue_bytes).hexdigest(),
            "issue_source": request.issue_file.name,
            "base_context_sha256": hashlib.sha256(base_context_bytes).hexdigest(),
        },
        "changed_symbols": [symbol.to_dict() for symbol in symbols],
        "draft": draft.to_dict(),
    }
    try:
        validate_requirement_draft_artifact(payload)
    except ValueError as exc:
        raise DraftingError(f"generated requirement draft is invalid: {exc}") from exc

    document = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(document) > _MAX_ARTIFACT_BYTES:
        raise DraftingError(
            "generated requirement draft exceeds the "
            f"{_MAX_ARTIFACT_BYTES}-byte artifact limit"
        )
    _write_exclusive(request.output_file, document)
    return DraftRequirementsResult(
        output_file=request.output_file,
        candidate_count=len(draft.candidates),
        truncated=draft.truncated,
    )


def _read_input(path: Path, label: str, *, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as exc:
        raise DraftingError(f"cannot read {label} file {path}: {exc}") from exc
    if len(data) > max_bytes:
        raise DraftingError(
            f"{label} file exceeds the {max_bytes}-byte limit: {path}"
        )
    if not data.strip():
        raise DraftingError(f"{label} file is empty: {path}")
    return data


def _write_exclusive(path: Path, document: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
    except OSError as exc:
        raise DraftingError(
            f"cannot create temporary requirement draft beside {path}: {exc}"
        ) from exc

    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        # Linking a fully flushed inode creates the final name atomically and,
        # unlike os.replace(), fails if another process created the requested
        # output while extraction was running.
        os.link(temporary, path)
    except FileExistsError as exc:
        raise DraftingError(f"output file already exists: {path}") from exc
    except OSError as exc:
        raise DraftingError(f"cannot write requirement draft {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
