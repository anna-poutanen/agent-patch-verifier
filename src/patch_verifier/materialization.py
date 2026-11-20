"""Authenticate inert reviewed-test staging before explicit execution.

The completion manifest is an integrity ledger, not a signature or proof that
the decisions were made by a particular reviewer.  This module freezes and
cross-checks the exact issue, EvidencePlan, reviewed artifact, and candidate
bytes that a later :func:`patch_verifier.verifier.verify` call may execute.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from .plan import EvidencePlan, PlanError, load_evidence_plan
from .test_review import (
    TestProposalReviewError,
    validate_reviewed_test_proposals_structure,
    validate_test_materialization_artifact,
)


_MAX_MANIFEST_BYTES = 10 * 1024 * 1024
_MAX_FINAL_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_ISSUE_BYTES = 2 * 1024 * 1024
_MAX_PLAN_DOCUMENT_BYTES = 5 * 1024 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024


class TestMaterializationError(RuntimeError):
    """A completed materialization cannot be handed to the verifier safely."""


@dataclass(frozen=True)
class MaterializedTestFile:
    """One exact candidate file bound by the completion manifest."""

    case_id: str
    relative_path: PurePosixPath
    sha256: str
    size: int
    source_bytes: bytes = field(repr=False)


@dataclass(frozen=True)
class AuthenticatedTestMaterialization:
    """Frozen, internally consistent inputs from one completed staging root."""

    root: Path
    manifest_file: Path
    manifest_document: bytes = field(repr=False)
    manifest_sha256: str
    reviewed_test_proposals_file: Path
    reviewed_test_proposals_document: bytes = field(repr=False)
    reviewed_test_proposals_sha256: str
    issue_file: Path
    issue_document: bytes = field(repr=False)
    issue_sha256: str
    evidence_plan_file: Path
    evidence_plan_document: bytes = field(repr=False)
    evidence_plan_sha256: str
    plan: EvidencePlan = field(repr=False)
    files: tuple[MaterializedTestFile, ...]

    @property
    def expected_test_sha256(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.case_id, item.sha256) for item in self.files)


class _DuplicateKeyError(ValueError):
    pass


def authenticate_test_materialization(
    root: Path,
) -> AuthenticatedTestMaterialization:
    """Freeze and authenticate a completed materialization relative to its manifest.

    The returned hashes detect drift between this handoff and verification.
    They do not authenticate reviewer identity, establish semantic correctness,
    or make execution a security sandbox.
    """

    root_path = Path(root).absolute()
    try:
        root_status = root_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TestMaterializationError(
            f"cannot inspect materialization root {root_path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(root_status.st_mode) or root_path.is_symlink():
        raise TestMaterializationError(
            f"materialization root must be a real directory: {root_path}"
        )

    manifest_path = root_path / "materialization.json"
    manifest_document = _read_regular_file(
        root_path,
        "materialization.json",
        label="materialization manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest = _decode_json(
        manifest_document,
        label="materialization manifest",
    )
    try:
        validate_test_materialization_artifact(manifest)
    except (TestProposalReviewError, ValueError) as exc:
        raise TestMaterializationError(
            f"invalid materialization manifest: {exc}"
        ) from exc

    reviewed_binding = _mapping(manifest, "reviewed_test_proposals")
    issue_binding = _mapping(manifest, "issue")
    plan_binding = _mapping(manifest, "evidence_plan")
    raw_files = manifest["files"]
    assert isinstance(raw_files, list)

    expected_files = {
        "materialization.json",
        str(reviewed_binding["path"]),
        str(issue_binding["path"]),
        str(plan_binding["path"]),
        *(str(item["path"]) for item in raw_files),
    }
    _validate_inventory(root_path, expected_files)

    reviewed_path, reviewed_document = _read_bound_file(
        root_path,
        reviewed_binding,
        label="reviewed test proposals",
        max_bytes=_MAX_FINAL_DOCUMENT_BYTES,
    )
    issue_path, issue_document = _read_bound_file(
        root_path,
        issue_binding,
        label="issue",
        max_bytes=_MAX_ISSUE_BYTES,
    )
    plan_path, plan_document = _read_bound_file(
        root_path,
        plan_binding,
        label="evidence plan",
        max_bytes=_MAX_PLAN_DOCUMENT_BYTES,
    )
    try:
        issue_text = issue_document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TestMaterializationError("materialized issue must be UTF-8") from exc

    candidate_documents: dict[str, bytes] = {}
    materialized_files: list[MaterializedTestFile] = []
    for record in raw_files:
        assert isinstance(record, Mapping)
        path, document = _read_bound_file(
            root_path,
            record,
            label=f"candidate for case {record['case_id']}",
            max_bytes=_MAX_SOURCE_BYTES,
        )
        relative = PurePosixPath(str(record["path"]))
        candidate_documents[relative.as_posix()] = document
        materialized_files.append(
            MaterializedTestFile(
                case_id=str(record["case_id"]),
                relative_path=relative,
                sha256=str(record["sha256"]),
                size=int(record["size"]),
                source_bytes=document,
            )
        )
        if path != root_path.joinpath(*relative.parts):
            raise TestMaterializationError(
                f"candidate path spelling changed for case {record['case_id']}"
            )

    try:
        plan = load_evidence_plan(
            plan_path,
            issue_text=issue_text,
            document_bytes=plan_document,
            candidate_source_documents=candidate_documents,
        )
    except PlanError as exc:
        raise TestMaterializationError(
            f"invalid materialized EvidencePlan: {exc}"
        ) from exc
    if plan.schema_version != 2:
        raise TestMaterializationError(
            "materialized EvidencePlan must use schema_version 2"
        )

    reviewed = _decode_json(
        reviewed_document,
        label="reviewed test proposals",
    )
    try:
        validate_reviewed_test_proposals_structure(reviewed)
    except (TestProposalReviewError, ValueError) as exc:
        raise TestMaterializationError(
            f"invalid reviewed test proposals: {exc}"
        ) from exc
    plan_payload = _decode_json(plan_document, label="evidence plan")
    _cross_check_artifacts(
        manifest,
        reviewed,
        plan_payload,
        plan,
        materialized_files,
        candidate_documents,
    )

    return AuthenticatedTestMaterialization(
        root=root_path,
        manifest_file=manifest_path,
        manifest_document=manifest_document,
        manifest_sha256=hashlib.sha256(manifest_document).hexdigest(),
        reviewed_test_proposals_file=reviewed_path,
        reviewed_test_proposals_document=reviewed_document,
        reviewed_test_proposals_sha256=hashlib.sha256(reviewed_document).hexdigest(),
        issue_file=issue_path,
        issue_document=issue_document,
        issue_sha256=hashlib.sha256(issue_document).hexdigest(),
        evidence_plan_file=plan_path,
        evidence_plan_document=plan_document,
        evidence_plan_sha256=hashlib.sha256(plan_document).hexdigest(),
        plan=plan,
        files=tuple(materialized_files),
    )


def _cross_check_artifacts(
    manifest: Mapping[str, Any],
    reviewed: Mapping[str, Any],
    plan_payload: Mapping[str, Any],
    plan: EvidencePlan,
    files: list[MaterializedTestFile],
    candidate_documents: Mapping[str, bytes],
) -> None:
    if reviewed["requirements"] != plan_payload["requirements"]:
        raise TestMaterializationError(
            "reviewed requirements differ from the materialized EvidencePlan"
        )
    if reviewed["cases"] != plan_payload["cases"]:
        raise TestMaterializationError(
            "reviewed cases differ from the materialized EvidencePlan"
        )
    for key in ("proposal_artifact", "review"):
        if manifest[key] != reviewed[key]:
            raise TestMaterializationError(
                f"materialization {key} binding differs from the reviewed artifact"
            )
    manifest_requirements = _mapping(manifest, "reviewed_requirements")
    reviewed_requirements = _mapping(reviewed, "reviewed_requirements")
    if manifest_requirements["sha256"] != reviewed_requirements["sha256"]:
        raise TestMaterializationError(
            "materialization reviewed-requirements hash differs from the "
            "reviewed artifact"
        )
    reviewed_issue = _mapping(reviewed, "issue")
    manifest_issue = _mapping(manifest, "issue")
    if (
        reviewed_issue["sha256"] != manifest_issue["sha256"]
        or reviewed_issue["size"] != manifest_issue["size"]
    ):
        raise TestMaterializationError(
            "reviewed issue binding differs from the materialized issue"
        )

    accepted_cases = reviewed["accepted_cases"]
    raw_cases = plan_payload["cases"]
    if not isinstance(accepted_cases, list) or not isinstance(raw_cases, list):
        raise TestMaterializationError("reviewed and planned cases must be arrays")
    if not (
        len(files)
        == len(accepted_cases)
        == len(raw_cases)
        == len(plan.cases)
    ):
        raise TestMaterializationError(
            "manifest, reviewed artifact, and EvidencePlan case counts differ"
        )

    for record, accepted, planned_raw, planned in zip(
        files,
        accepted_cases,
        raw_cases,
        plan.cases,
    ):
        assert isinstance(accepted, Mapping)
        assert isinstance(planned_raw, Mapping)
        relative = record.relative_path.as_posix()
        expected = {
            "id": record.case_id,
            "path": relative,
            "sha256": record.sha256,
        }
        observed = {
            "id": str(planned_raw["id"]),
            "path": str(planned_raw["test_file"]),
            "sha256": str(planned_raw["test_sha256"]),
        }
        if observed != expected:
            raise TestMaterializationError(
                f"manifest and EvidencePlan differ for case {record.case_id}"
            )
        if (
            accepted["id"] != record.case_id
            or accepted["test_file"] != relative
            or accepted["source"]["sha256"] != record.sha256
        ):
            raise TestMaterializationError(
                f"reviewed artifact differs for case {record.case_id}"
            )
        try:
            accepted_source = accepted["source"]["text"].encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise TestMaterializationError(
                f"reviewed source is not valid UTF-8 for case {record.case_id}"
            ) from exc
        if (
            accepted_source != candidate_documents[relative]
            or planned.source_bytes != candidate_documents[relative]
            or planned.id != record.case_id
        ):
            raise TestMaterializationError(
                f"candidate bytes differ for case {record.case_id}"
            )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value[key]
    if not isinstance(result, Mapping):
        raise TestMaterializationError(f"materialization {key} must be an object")
    return result


def _read_bound_file(
    root: Path,
    binding: Mapping[str, Any],
    *,
    label: str,
    max_bytes: int,
) -> tuple[Path, bytes]:
    relative = PurePosixPath(str(binding["path"]))
    path = root.joinpath(*relative.parts)
    document = _read_regular_file(
        root,
        relative.as_posix(),
        label=label,
        max_bytes=max_bytes,
    )
    expected_size = int(binding["size"])
    if len(document) != expected_size:
        raise TestMaterializationError(
            f"materialized {label} size mismatch: expected {expected_size}, "
            f"observed {len(document)}"
        )
    expected_sha256 = str(binding["sha256"])
    observed_sha256 = hashlib.sha256(document).hexdigest()
    if observed_sha256 != expected_sha256:
        raise TestMaterializationError(
            f"materialized {label} hash mismatch: expected {expected_sha256}, "
            f"observed {observed_sha256}"
        )
    return path, document


def _validate_inventory(root: Path, expected_files: set[str]) -> None:
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in expected_files
        if PurePosixPath(path).parent.as_posix() != "."
    }
    expected_directories |= {
        parent.as_posix()
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    try:
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            for name in directories:
                child = directory_path / name
                status = child.stat(follow_symlinks=False)
                relative = child.relative_to(root).as_posix()
                if not stat.S_ISDIR(status.st_mode):
                    raise TestMaterializationError(
                        f"materialization contains unsafe directory entry {relative!r}"
                    )
                actual_directories.add(relative)
            for name in filenames:
                actual_files.add((directory_path / name).relative_to(root).as_posix())
    except OSError as exc:
        raise TestMaterializationError(
            f"cannot inventory materialization root {root}: {exc}"
        ) from exc
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        detail = (
            f"unexpected path {unexpected[0]!r}"
            if unexpected
            else f"missing path {missing[0]!r}"
        )
        raise TestMaterializationError(f"materialization inventory differs: {detail}")
    if actual_directories != expected_directories:
        unexpected = sorted(actual_directories - expected_directories)
        missing = sorted(expected_directories - actual_directories)
        detail = (
            f"unexpected directory {unexpected[0]!r}"
            if unexpected
            else f"missing directory {missing[0]!r}"
        )
        raise TestMaterializationError(f"materialization inventory differs: {detail}")


def _read_regular_file(
    root: Path,
    relative: str,
    *,
    label: str,
    max_bytes: int,
) -> bytes:
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    if os.open not in os.supports_dir_fd or any(
        not hasattr(os, name) for name in required_flags
    ):
        raise TestMaterializationError(
            "secure materialization authentication is unavailable on this platform"
        )
    parts = PurePosixPath(relative).parts
    if not parts:
        raise TestMaterializationError(f"materialized {label} path is empty")
    common_flags = getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | common_flags
    )
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | common_flags
    directory_descriptors: list[int] = []
    descriptor = -1
    try:
        current = os.open(root, directory_flags)
        directory_descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            directory_descriptors.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
    except OSError as exc:
        raise TestMaterializationError(
            f"cannot open materialized {label} {relative!r} without following "
            f"links: {exc}"
        ) from exc
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise TestMaterializationError(
                f"materialized {label} is not a regular file"
            )
        if status.st_size > max_bytes:
            raise TestMaterializationError(
                f"materialized {label} exceeds the {max_bytes}-byte limit"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            document = stream.read(max_bytes + 1)
    except TestMaterializationError:
        raise
    except OSError as exc:
        raise TestMaterializationError(
            f"cannot read materialized {label} {relative!r}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(document) > max_bytes:
        raise TestMaterializationError(
            f"materialized {label} exceeds the {max_bytes}-byte limit"
        )
    if not document:
        raise TestMaterializationError(f"materialized {label} is empty")
    return document


def _decode_json(document: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            document.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise TestMaterializationError(f"{label} contains {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestMaterializationError(f"{label} is not valid UTF-8 JSON") from exc
    except (MemoryError, RecursionError, ValueError) as exc:
        raise TestMaterializationError(f"{label} is not safely parseable JSON") from exc
    if not isinstance(value, dict):
        raise TestMaterializationError(f"{label} must contain a JSON object")
    return value


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


__all__ = [
    "AuthenticatedTestMaterialization",
    "MaterializedTestFile",
    "TestMaterializationError",
    "authenticate_test_materialization",
]
