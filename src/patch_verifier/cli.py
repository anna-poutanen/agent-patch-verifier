"""Command-line interface for Agent Patch Verifier."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys
from typing import Protocol, Sequence

from . import __version__
from .plan import PlanError
from .verifier import (
    VerificationError,
    VerificationRequest,
    default_output_directory,
    reproduction_differences,
    request_from_bundle,
    verify,
)
from .workspace import WorkspaceError


class _DraftResult(Protocol):
    output_file: Path
    candidate_count: int
    truncated: bool


class _ReviewPreparationResult(Protocol):
    output_file: Path
    candidate_count: int
    conflict_hint_count: int


class _ReviewFinalizationResult(Protocol):
    output_file: Path
    requirement_count: int
    rejected_count: int


def _draft_patch_requirements(
    *,
    repository: Path,
    base_revision: str,
    patch_file: Path,
    issue_file: Path,
    output_file: Path,
) -> _DraftResult:
    """Call drafting orchestration without widening top-level CLI imports."""

    from .drafting import (
        DraftingError,
        DraftRequirementsRequest,
        draft_patch_requirements,
    )
    from .requirements import RequirementExtractionError

    request = DraftRequirementsRequest(
        repository=repository,
        base_revision=base_revision,
        patch_file=patch_file,
        issue_file=issue_file,
        output_file=output_file,
    )
    try:
        return draft_patch_requirements(request)
    except (DraftingError, RequirementExtractionError) as exc:
        # Keep argparse's established, user-facing exit-code-two behavior
        # without making every CLI invocation import the drafting subsystem.
        raise ValueError(str(exc)) from exc


def _load_requirement_draft(draft_file: Path) -> dict[str, object]:
    """Load only the parsed value of a bounded requirement draft."""

    payload, _ = _load_requirement_draft_with_digest(draft_file)
    return payload


def _load_requirement_draft_with_digest(
    draft_file: Path,
) -> tuple[dict[str, object], str]:
    """Load a bounded JSON object for conflict analysis.

    The review subsystem separately reads and validates the exact draft bytes
    that it binds into an artifact. The returned digest lets that second read
    fail if the file changed after this bounded, duplicate-free parse.
    """

    import hashlib
    import json

    maximum_bytes = 10 * 1024 * 1024
    path = Path(draft_file).resolve()
    try:
        with path.open("rb") as stream:
            document = stream.read(maximum_bytes + 1)
    except OSError as exc:
        raise ValueError(f"cannot read requirement draft {path}: {exc}") from exc
    if len(document) > maximum_bytes:
        raise ValueError(
            f"requirement draft exceeds the {maximum_bytes}-byte limit"
        )
    if not document.strip():
        raise ValueError("requirement draft is empty")
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("requirement draft must be UTF-8") from exc

    class _DuplicateKeyError(ValueError):
        pass

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKeyError(
                    f"duplicate JSON object key {key!r}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        raise ValueError(f"requirement draft contains {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            "requirement draft is not valid JSON at "
            f"line {exc.lineno}, column {exc.colno}"
        ) from exc
    except (RecursionError, ValueError) as exc:
        raise ValueError("requirement draft is not safely parseable JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("requirement draft must contain a JSON object")
    return payload, hashlib.sha256(document).hexdigest()


def _prepare_requirement_review(
    *,
    draft_file: Path,
    output_file: Path,
) -> tuple[_ReviewPreparationResult, bool]:
    """Analyze a draft and write an inert, all-pending review ledger."""

    from .conflicts import ConflictAnalysisError, analyze_draft_conflicts
    from .review import RequirementReviewError, write_requirement_review

    draft, draft_sha256 = _load_requirement_draft_with_digest(draft_file)
    try:
        analysis = analyze_draft_conflicts(draft)
        hints = tuple(hint.to_dict() for hint in analysis.hints)
        result = write_requirement_review(
            draft_file,
            output_file,
            conflict_hints=hints,
            conflict_analysis=analysis.to_dict(),
            expected_draft_sha256=draft_sha256,
        )
    except (ConflictAnalysisError, RequirementReviewError) as exc:
        raise ValueError(str(exc)) from exc
    return result, analysis.truncated


def _finalize_requirement_review(
    *,
    draft_file: Path,
    review_file: Path,
    output_file: Path,
) -> tuple[_ReviewFinalizationResult, bool]:
    """Recompute advisory hints and finalize explicit human decisions."""

    from .conflicts import ConflictAnalysisError, analyze_draft_conflicts
    from .review import RequirementReviewError, write_reviewed_requirements

    draft, draft_sha256 = _load_requirement_draft_with_digest(draft_file)
    try:
        analysis = analyze_draft_conflicts(draft)
        hints = tuple(hint.to_dict() for hint in analysis.hints)
        result = write_reviewed_requirements(
            draft_file,
            review_file,
            output_file,
            expected_conflict_hints=hints,
            expected_conflict_analysis=analysis.to_dict(),
            expected_draft_sha256=draft_sha256,
        )
    except (ConflictAnalysisError, RequirementReviewError) as exc:
        raise ValueError(str(exc)) from exc
    return result, analysis.truncated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apv",
        description=(
            "Draft and explicitly review requirements, or run requirement-linked "
            "pytest evidence against base and patched revisions."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    draft_parser = subparsers.add_parser(
        "draft-requirements",
        help="extract review-only requirement candidates for a Git patch",
    )
    draft_parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="source Git repository (default: current directory)",
    )
    draft_parser.add_argument(
        "--base",
        default="HEAD",
        help="committed base revision (default: HEAD)",
    )
    draft_parser.add_argument(
        "--patch",
        type=Path,
        required=True,
        help="textual Git patch to inspect",
    )
    draft_parser.add_argument(
        "--issue",
        type=Path,
        required=True,
        help="UTF-8 issue description",
    )
    draft_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON artifact path; existing files are never overwritten",
    )
    prepare_review_parser = subparsers.add_parser(
        "prepare-review",
        help="create an all-pending human review ledger from a draft",
    )
    prepare_review_parser.add_argument(
        "--draft",
        type=Path,
        required=True,
        help="requirement-draft JSON artifact",
    )
    prepare_review_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new review-ledger path; existing files are never overwritten",
    )
    finalize_review_parser = subparsers.add_parser(
        "finalize-review",
        help="finalize explicit human decisions into reviewed requirements",
    )
    finalize_review_parser.add_argument(
        "--draft",
        type=Path,
        required=True,
        help="exact requirement draft used to prepare the review ledger",
    )
    finalize_review_parser.add_argument(
        "--review",
        type=Path,
        required=True,
        help="completed review ledger with no pending decisions",
    )
    finalize_review_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new reviewed-requirements path; existing files are never overwritten",
    )
    verify_parser = subparsers.add_parser(
        "verify", help="verify a proposed textual Git patch"
    )
    verify_parser.add_argument("--repo", type=Path, default=Path.cwd())
    verify_parser.add_argument("--base", default="HEAD")
    verify_parser.add_argument("--patch", type=Path, required=True)
    verify_parser.add_argument("--issue", type=Path, required=True)
    verify_parser.add_argument("--evidence", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path)
    verify_parser.add_argument(
        "--test-command",
        default=f"{shlex.quote(sys.executable)} -m pytest -q --tb=short",
        help="shell-like command prefix parsed into arguments; no shell is executed",
    )
    verify_parser.add_argument("--timeout", type=float, default=60.0)
    verify_parser.add_argument("--repeat", type=int, default=2)
    reproduce_parser = subparsers.add_parser(
        "reproduce", help="rerun a saved evidence bundle"
    )
    reproduce_parser.add_argument("run_directory", type=Path)
    reproduce_parser.add_argument("--output", type=Path)
    reproduce_parser.add_argument(
        "--repo",
        type=Path,
        help="source repository override when the recorded repository moved",
    )
    reproduce_parser.add_argument(
        "--test-command",
        help=(
            "override the recorded pytest command prefix when its interpreter "
            "is unavailable; this makes the replay environment non-exact"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    replay_differences: tuple[str, ...] = ()
    try:
        if arguments.command == "draft-requirements":
            draft_result = _draft_patch_requirements(
                repository=arguments.repo.resolve(),
                base_revision=arguments.base,
                patch_file=arguments.patch,
                issue_file=arguments.issue,
                output_file=arguments.output,
            )
            print(f"Draft: {draft_result.output_file}")
            print(
                f"Candidates: {draft_result.candidate_count} "
                "(review required; no candidate tests were generated or run)"
            )
            if draft_result.truncated:
                print(
                    "Warning: candidate extraction was truncated.",
                    file=sys.stderr,
                )
            return
        if arguments.command == "prepare-review":
            review_result, analysis_truncated = _prepare_requirement_review(
                draft_file=arguments.draft,
                output_file=arguments.output,
            )
            print(f"Review: {review_result.output_file}")
            print(
                f"Candidates: {review_result.candidate_count} "
                "(all pending; explicit human review required)"
            )
            print(
                f"Potential tension hints: {review_result.conflict_hint_count} "
                "(advisory only; no candidate tests were generated or run)"
            )
            if analysis_truncated:
                print(
                    "Warning: conflict analysis was truncated; the review may "
                    "be missing potential tension hints.",
                    file=sys.stderr,
                )
            return
        if arguments.command == "finalize-review":
            review_result, analysis_truncated = _finalize_requirement_review(
                draft_file=arguments.draft,
                review_file=arguments.review,
                output_file=arguments.output,
            )
            print(f"Reviewed requirements: {review_result.output_file}")
            print(f"Accepted: {review_result.requirement_count}")
            print(f"Rejected: {review_result.rejected_count}")
            print("No candidate tests were generated or run.")
            if analysis_truncated:
                print(
                    "Warning: conflict analysis was truncated; the review may "
                    "be missing potential tension hints.",
                    file=sys.stderr,
                )
            return
        if arguments.command == "verify":
            command_prefix = tuple(shlex.split(arguments.test_command))
            repository = arguments.repo.resolve()
            output = arguments.output or default_output_directory(repository)
            request = VerificationRequest(
                repository=repository,
                base_revision=arguments.base,
                patch_file=arguments.patch,
                issue_file=arguments.issue,
                evidence_file=arguments.evidence,
                output_directory=output,
                command_prefix=command_prefix,
                timeout_seconds=arguments.timeout,
                repeat=arguments.repeat,
            )
        else:
            command_override = (
                None
                if arguments.test_command is None
                else tuple(shlex.split(arguments.test_command))
            )
            request = request_from_bundle(
                arguments.run_directory,
                output_directory=arguments.output,
                repository=arguments.repo,
                command_prefix_override=command_override,
            )
        result = verify(request)
        if arguments.command == "reproduce":
            replay_differences = reproduction_differences(
                arguments.run_directory, result
            )
    except (PlanError, VerificationError, WorkspaceError, ValueError) as exc:
        parser.exit(2, f"apv: error: {exc}\n")

    print(f"Report: {result.output_directory / 'report.md'}")
    if arguments.command == "reproduce" and arguments.test_command is not None:
        print("Note: replay used a non-exact test-command override.")
    if replay_differences:
        print("Reproduction drifted from the recorded findings:", file=sys.stderr)
        for difference in replay_differences:
            print(f"- {difference}", file=sys.stderr)
        raise SystemExit(2)
    if result.has_supported_failure:
        print(
            f"Found {len(result.supported_failures)} supported patch failure(s)."
        )
        if result.is_inconclusive:
            print(
                f"Warning: {len(result.inconclusive_findings)} additional "
                "case(s) were inconclusive."
            )
        raise SystemExit(1)
    if result.is_inconclusive:
        print(
            f"Run inconclusive: {len(result.inconclusive_findings)} case(s) "
            "did not produce usable pass/fail evidence.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print("No supported counterexample found; this is not proof of correctness.")


if __name__ == "__main__":
    main()

