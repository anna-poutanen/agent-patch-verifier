"""Conservative, deterministic classification of differential test results."""

from __future__ import annotations

from typing import Iterable, List

from .models import (
    Finding,
    FindingKind,
    Observation,
    Outcome,
    Requirement,
    RequirementKind,
    RevisionResult,
)


def classify_observation(observation: Observation) -> Finding:
    """Classify one base/patched observation without model judgment.

    Error, timeout, and unstable executions take precedence over every other
    rule.  The classifier only emits a supported failure when both the outcome
    transition and the requirement provenance justify it.
    """

    requirement = observation.requirement
    base = observation.base
    patched = observation.patched

    if not base.is_usable or not patched.is_usable:
        reasons = []
        if not base.stable:
            reasons.append("the base result is unstable")
        elif base.outcome in {Outcome.ERROR, Outcome.TIMEOUT}:
            reasons.append(f"the base result is {base.outcome.value}")
        if not patched.stable:
            reasons.append("the patched result is unstable")
        elif patched.outcome in {Outcome.ERROR, Outcome.TIMEOUT}:
            reasons.append(f"the patched result is {patched.outcome.value}")
        rationale = "; ".join(reasons) or "an execution result is not usable"
        return _finding(
            FindingKind.INCONCLUSIVE,
            "Execution is inconclusive",
            f"No behavioral conclusion is supported because {rationale}.",
            observation,
        )

    if base.outcome is Outcome.PASS and patched.outcome is Outcome.FAIL:
        if (
            requirement.kind is RequirementKind.PRESERVATION
            and requirement.has_strong_support
        ):
            return _finding(
                FindingKind.SUPPORTED_REGRESSION,
                f"Patch violates preserved requirement {requirement.id}",
                "The test passes on the base revision and fails on the patched "
                "revision. The preservation requirement has "
                f"{requirement.provenance.value} support, so this is reproducible "
                "evidence of a regression.",
                observation,
            )
        return _finding(
            FindingKind.REVIEW_ONLY_CHANGE,
            f"Patch changes behavior covered by {requirement.id}",
            "The test passes on the base revision and fails on the patched "
            "revision, but the requirement is not a strongly supported preservation "
            "requirement. Report the change for review rather than as a proven "
            "regression.",
            observation,
        )

    if base.outcome is Outcome.FAIL and patched.outcome is Outcome.FAIL:
        if (
            requirement.kind is RequirementKind.REQUESTED
            and requirement.has_strong_support
        ):
            return _finding(
                FindingKind.INCOMPLETE_FIX,
                f"Patch does not satisfy requested requirement {requirement.id}",
                "The requested criterion fails on both revisions and has "
                f"{requirement.provenance.value} support, so the proposed patch does "
                "not implement this part of the requested behavior.",
                observation,
            )
        return _finding(
            FindingKind.NO_DIFFERENTIAL,
            f"No differential result for {requirement.id}",
            "The criterion fails on both revisions. Its kind or provenance is not "
            "sufficient to classify the patch as an incomplete fix.",
            observation,
        )

    if base.outcome is Outcome.FAIL and patched.outcome is Outcome.PASS:
        if requirement.kind is RequirementKind.REQUESTED:
            return _finding(
                FindingKind.CRITERION_PASSES,
                f"Patch satisfies tested criterion {requirement.id}",
                "The requested criterion fails on the base revision and passes on "
                "the patched revision. This supports the criterion, but does not prove "
                "the patch correct beyond the tested behavior.",
                observation,
            )
        return _finding(
            FindingKind.REVIEW_ONLY_CHANGE,
            f"Patch changes behavior covered by {requirement.id}",
            "The preservation criterion fails on the base revision and passes on the "
            "patched revision. This behavioral change may be beneficial, but it is not "
            "evidence of a requested fix or regression.",
            observation,
        )

    # The only remaining usable pair is pass/pass.
    if requirement.kind is RequirementKind.PRESERVATION:
        return _finding(
            FindingKind.PRESERVED,
            f"Patch preserves tested requirement {requirement.id}",
            "The preservation criterion passes on both revisions.",
            observation,
        )
    return _finding(
        FindingKind.NO_DIFFERENTIAL,
        f"No differential result for {requirement.id}",
        "The requested criterion passes on both revisions, so this test does not "
        "show that the patch caused the behavior.",
        observation,
    )


def classify(
    requirement: Requirement,
    base: RevisionResult,
    patched: RevisionResult,
    *,
    test_id: str = "generated_test",
) -> Finding:
    """Convenience wrapper for callers that do not already have an Observation."""

    return classify_observation(
        Observation(
            test_id=test_id,
            requirement=requirement,
            base=base,
            patched=patched,
        )
    )


def classify_many(observations: Iterable[Observation]) -> List[Finding]:
    """Classify observations in input order."""

    return [classify_observation(observation) for observation in observations]


def _finding(
    kind: FindingKind,
    summary: str,
    rationale: str,
    observation: Observation,
) -> Finding:
    return Finding(
        kind=kind,
        summary=summary,
        rationale=rationale,
        observation=observation,
    )
