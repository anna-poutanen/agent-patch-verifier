"""Domain models for requirement-backed, differential patch evidence.

The models in this module deliberately separate an observed test outcome from the
strength of the requirement behind its assertion.  A pass/fail transition is an
observation; it only becomes a supported regression when the requirement and its
provenance justify that conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Tuple


class _StringEnum(str, Enum):
    """A JSON-friendly enum with a useful string representation."""

    def __str__(self) -> str:
        return self.value


class ProvenanceTier(_StringEnum):
    """How independently a requirement is supported.

    ``EXPLICIT`` and ``PREEXISTING`` are strong enough to support a definitive
    finding.  ``INFERRED`` is useful for exploration, but requires human review.
    """

    EXPLICIT = "explicit"
    PREEXISTING = "preexisting"
    INFERRED = "inferred"

    @property
    def is_strong(self) -> bool:
        return self in {self.EXPLICIT, self.PREEXISTING}


class RequirementKind(_StringEnum):
    """Whether an assertion checks the requested change or preserved behavior."""

    REQUESTED = "requested"
    PRESERVATION = "preservation"


class Outcome(_StringEnum):
    """The outcome of one test execution on one revision."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"


class FindingKind(_StringEnum):
    """Conservative interpretations of a differential test observation."""

    SUPPORTED_REGRESSION = "supported_regression"
    REVIEW_ONLY_CHANGE = "review_only_change"
    INCOMPLETE_FIX = "incomplete_fix"
    CRITERION_PASSES = "criterion_passes"
    PRESERVED = "preserved"
    NO_DIFFERENTIAL = "no_differential"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Requirement:
    """A testable behavioral requirement and the source supporting it.

    ``source`` should identify where the expectation came from, for example
    ``"issue.md:12"`` or ``"tests/test_defaults.py:41"``.  ``excerpt`` keeps a
    short, reviewable piece of that source with the report.
    """

    id: str
    statement: str
    kind: RequirementKind
    provenance: ProvenanceTier
    source: str
    excerpt: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RequirementKind(self.kind))
        object.__setattr__(self, "provenance", ProvenanceTier(self.provenance))
        if not self.id.strip():
            raise ValueError("requirement id must not be empty")
        if not self.statement.strip():
            raise ValueError("requirement statement must not be empty")
        if not self.source.strip():
            raise ValueError("requirement source must not be empty")

    @property
    def has_strong_support(self) -> bool:
        return self.provenance.is_strong

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "kind": self.kind.value,
            "provenance": self.provenance.value,
            "source": self.source,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Requirement":
        return cls(
            id=str(data["id"]),
            statement=str(data["statement"]),
            kind=RequirementKind(data["kind"]),
            provenance=ProvenanceTier(data["provenance"]),
            source=str(data["source"]),
            excerpt=(None if data.get("excerpt") is None else str(data["excerpt"])),
        )


@dataclass(frozen=True)
class RevisionResult:
    """A test result for one revision, including repeat-run stability.

    ``outcome`` is the representative result used for classification.  When
    ``attempts`` are supplied, a result is stable only when every attempt equals
    that outcome.  Callers may also explicitly set ``stable=False`` when a test
    harness detects instability by another mechanism.
    """

    outcome: Outcome
    stable: bool = True
    attempts: Tuple[Outcome, ...] = field(default_factory=tuple)
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", Outcome(self.outcome))
        normalized_attempts = tuple(Outcome(item) for item in self.attempts)
        object.__setattr__(self, "attempts", normalized_attempts)
        if self.stable and any(
            item is not self.outcome for item in normalized_attempts
        ):
            raise ValueError(
                "a stable revision result cannot contain conflicting attempts"
            )

    @classmethod
    def from_attempts(
        cls, attempts: Iterable[Outcome], *, detail: Optional[str] = None
    ) -> "RevisionResult":
        """Build a result from repeated executions and derive stability."""

        normalized = tuple(Outcome(item) for item in attempts)
        if not normalized:
            raise ValueError("at least one attempt is required")
        representative = normalized[0]
        return cls(
            outcome=representative,
            stable=all(item is representative for item in normalized),
            attempts=normalized,
            detail=detail,
        )

    @property
    def is_usable(self) -> bool:
        """Whether this result can participate in pass/fail classification."""

        return self.stable and self.outcome in {Outcome.PASS, Outcome.FAIL}

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "stable": self.stable,
            "attempts": [item.value for item in self.attempts],
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RevisionResult":
        return cls(
            outcome=Outcome(data["outcome"]),
            stable=bool(data.get("stable", True)),
            attempts=tuple(Outcome(item) for item in data.get("attempts", ())),
            detail=(None if data.get("detail") is None else str(data["detail"])),
        )


@dataclass(frozen=True)
class Observation:
    """The same generated test executed on the base and patched revisions."""

    test_id: str
    requirement: Requirement
    base: RevisionResult
    patched: RevisionResult

    def __post_init__(self) -> None:
        if not self.test_id.strip():
            raise ValueError("test id must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "requirement": self.requirement.to_dict(),
            "base": self.base.to_dict(),
            "patched": self.patched.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Observation":
        return cls(
            test_id=str(data["test_id"]),
            requirement=Requirement.from_dict(data["requirement"]),
            base=RevisionResult.from_dict(data["base"]),
            patched=RevisionResult.from_dict(data["patched"]),
        )


@dataclass(frozen=True)
class Finding:
    """A deterministic interpretation of one test observation."""

    kind: FindingKind
    summary: str
    rationale: str
    observation: Observation

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", FindingKind(self.kind))

    @property
    def is_supported_failure(self) -> bool:
        """Whether the finding is strong enough to say the patch is likely wrong."""

        return self.kind in {
            FindingKind.SUPPORTED_REGRESSION,
            FindingKind.INCOMPLETE_FIX,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "rationale": self.rationale,
            "observation": self.observation.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        return cls(
            kind=FindingKind(data["kind"]),
            summary=str(data["summary"]),
            rationale=str(data["rationale"]),
            observation=Observation.from_dict(data["observation"]),
        )
