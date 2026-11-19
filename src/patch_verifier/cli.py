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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apv",
        description=(
            "Draft review-only requirements or run requirement-linked pytest "
            "evidence against base and patched revisions."
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

