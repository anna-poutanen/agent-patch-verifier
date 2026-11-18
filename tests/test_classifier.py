import json
import unittest

from patch_verifier.classifier import classify, classify_observation
from patch_verifier.models import (
    Finding,
    FindingKind,
    Observation,
    Outcome,
    ProvenanceTier,
    Requirement,
    RequirementKind,
    RevisionResult,
)


def requirement(
    *,
    kind=RequirementKind.PRESERVATION,
    provenance=ProvenanceTier.EXPLICIT,
):
    return Requirement(
        id="REQ-001",
        statement="A supplied nonzero value is returned unchanged.",
        kind=kind,
        provenance=provenance,
        source="issue.md:7",
        excerpt="Preserve supplied values.",
    )


def result(outcome, *, stable=True):
    return RevisionResult(outcome=outcome, stable=stable)


class ClassificationTests(unittest.TestCase):
    def test_base_pass_patch_fail_with_explicit_preservation_is_regression(self):
        finding = classify(
            requirement(), result(Outcome.PASS), result(Outcome.FAIL)
        )

        self.assertEqual(finding.kind, FindingKind.SUPPORTED_REGRESSION)
        self.assertTrue(finding.is_supported_failure)

    def test_preexisting_preservation_is_also_strong_support(self):
        finding = classify(
            requirement(provenance=ProvenanceTier.PREEXISTING),
            result(Outcome.PASS),
            result(Outcome.FAIL),
        )

        self.assertEqual(finding.kind, FindingKind.SUPPORTED_REGRESSION)

    def test_inferred_preservation_failure_is_review_only(self):
        finding = classify(
            requirement(provenance=ProvenanceTier.INFERRED),
            result(Outcome.PASS),
            result(Outcome.FAIL),
        )

        self.assertEqual(finding.kind, FindingKind.REVIEW_ONLY_CHANGE)
        self.assertFalse(finding.is_supported_failure)

    def test_requested_requirement_failing_both_is_incomplete_fix(self):
        finding = classify(
            requirement(kind=RequirementKind.REQUESTED),
            result(Outcome.FAIL),
            result(Outcome.FAIL),
        )

        self.assertEqual(finding.kind, FindingKind.INCOMPLETE_FIX)
        self.assertTrue(finding.is_supported_failure)

    def test_inferred_requested_failure_is_not_called_incomplete_fix(self):
        finding = classify(
            requirement(
                kind=RequirementKind.REQUESTED,
                provenance=ProvenanceTier.INFERRED,
            ),
            result(Outcome.FAIL),
            result(Outcome.FAIL),
        )

        self.assertEqual(finding.kind, FindingKind.NO_DIFFERENTIAL)

    def test_requested_fail_to_pass_means_criterion_passes(self):
        finding = classify(
            requirement(kind=RequirementKind.REQUESTED),
            result(Outcome.FAIL),
            result(Outcome.PASS),
        )

        self.assertEqual(finding.kind, FindingKind.CRITERION_PASSES)
        self.assertFalse(finding.is_supported_failure)

    def test_preservation_pass_to_pass_is_preserved(self):
        finding = classify(
            requirement(), result(Outcome.PASS), result(Outcome.PASS)
        )

        self.assertEqual(finding.kind, FindingKind.PRESERVED)

    def test_requested_pass_to_pass_has_no_differential(self):
        finding = classify(
            requirement(kind=RequirementKind.REQUESTED),
            result(Outcome.PASS),
            result(Outcome.PASS),
        )

        self.assertEqual(finding.kind, FindingKind.NO_DIFFERENTIAL)

    def test_errors_and_timeouts_are_inconclusive(self):
        pairs = [
            (Outcome.ERROR, Outcome.PASS),
            (Outcome.PASS, Outcome.ERROR),
            (Outcome.TIMEOUT, Outcome.FAIL),
            (Outcome.FAIL, Outcome.TIMEOUT),
        ]
        for base_outcome, patched_outcome in pairs:
            with self.subTest(base=base_outcome, patched=patched_outcome):
                finding = classify(
                    requirement(), result(base_outcome), result(patched_outcome)
                )
                self.assertEqual(finding.kind, FindingKind.INCONCLUSIVE)

    def test_unstable_result_is_inconclusive(self):
        unstable = RevisionResult.from_attempts([Outcome.PASS, Outcome.FAIL])

        finding = classify(requirement(), unstable, result(Outcome.FAIL))

        self.assertFalse(unstable.stable)
        self.assertEqual(finding.kind, FindingKind.INCONCLUSIVE)

    def test_preservation_fail_to_pass_is_review_only(self):
        finding = classify(
            requirement(), result(Outcome.FAIL), result(Outcome.PASS)
        )

        self.assertEqual(finding.kind, FindingKind.REVIEW_ONLY_CHANGE)


class SerializationTests(unittest.TestCase):
    def test_finding_round_trips_through_json(self):
        observation = Observation(
            test_id="test_preserves_three",
            requirement=requirement(provenance=ProvenanceTier.PREEXISTING),
            base=RevisionResult.from_attempts([Outcome.PASS, Outcome.PASS]),
            patched=RevisionResult.from_attempts([Outcome.FAIL, Outcome.FAIL]),
        )
        finding = classify_observation(observation)

        payload = json.loads(json.dumps(finding.to_dict()))
        restored = Finding.from_dict(payload)

        self.assertEqual(restored, finding)
        self.assertEqual(payload["kind"], "supported_regression")
        self.assertEqual(payload["observation"]["base"]["outcome"], "pass")
        self.assertEqual(
            payload["observation"]["requirement"]["provenance"],
            "preexisting",
        )

    def test_models_accept_string_enum_values(self):
        model = Requirement(
            id="REQ-002",
            statement="Missing values use the default.",
            kind="requested",
            provenance="explicit",
            source="issue.md:8",
        )
        execution = RevisionResult(outcome="pass", attempts=("pass", "pass"))

        self.assertIs(model.kind, RequirementKind.REQUESTED)
        self.assertIs(model.provenance, ProvenanceTier.EXPLICIT)
        self.assertEqual(execution.attempts, (Outcome.PASS, Outcome.PASS))


if __name__ == "__main__":
    unittest.main()
