"""Provider-neutral, review-only candidate test proposal artifacts.

This module is deliberately outside the execution path.  It accepts plain
structured proposals from any generator adapter, attaches deterministic
identities and exact assertion spans, and validates the result against the
exact bytes of a finalized reviewed-requirements artifact.

An assertion's requirement preview records the source of the expectation.  It
does *not* establish that generated code implements that expectation
correctly.  Every proposal therefore remains ``requires_review`` until a later
workflow explicitly accepts and executes it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import itertools
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import tempfile
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .models import Requirement
from .plan import CandidateSourceValidation, validate_candidate_test_source
from .review_validation import validate_reviewed_requirements_artifact


_MAX_PROPOSALS = 50
_MAX_PROPOSAL_DOCUMENT_BYTES = 24 * 1024 * 1024
_MAX_GENERATOR_RESPONSE_BYTES = 24 * 1024 * 1024
_MAX_REVIEWED_REQUIREMENTS_BYTES = 24 * 1024 * 1024
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_RATIONALE_BYTES = 16_000
_MAX_PATH_COMPONENTS = 32
_MAX_UNIQUE_DIRECTORIES = 256
_CATEGORY_ORDER = {
    "requested": 0,
    "preservation": 1,
    "boundary": 2,
    "invalid_input": 3,
    "caller_level": 4,
    "state_sequence": 5,
}
_RAW_PROPOSAL_KEYS = {
    "requirement_id",
    "category",
    "test_file",
    "node",
    "rationale",
    "source",
}
_PORTABLE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_COMPONENTS = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_MATERIALIZATION_RESERVED_ROOTS = {
    "evidence-plan.json",
    "issue.md",
    "materialization.json",
    "reviewed-test-proposals.json",
}


class TestProposalError(ValueError):
    """A proposal artifact or one of its exact inputs is invalid."""


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratorIdentity:
    """Opaque provider/model labels; neither affects trust or classification."""

    adapter: str
    model: str | None = None

    def __post_init__(self) -> None:
        _validate_generator_identity(self.adapter, self.model)


@dataclass(frozen=True)
class TestProposalRequest:
    """Provider-neutral input for an adapter that proposes candidate tests."""

    reviewed_requirements_sha256: str
    requirements: tuple[Requirement, ...]
    allowed_categories: tuple[str, ...] = tuple(_CATEGORY_ORDER)
    max_proposals: int = _MAX_PROPOSALS
    review_status: str = "requires_review"


class TestProposalGenerator(Protocol):
    """Minimal adapter boundary; implementations may use any provider or none."""

    identity: GeneratorIdentity

    def propose(
        self,
        request: TestProposalRequest,
    ) -> Iterable[Mapping[str, object]]:
        """Return inert raw proposals without executing repository code."""


@dataclass(frozen=True)
class GeneratorResponseBinding:
    """Audit binding for canonical proposal content and optional exact bytes."""

    binding: str
    sha256: str
    canonical_sha256: str


@dataclass(frozen=True)
class RequirementCoverage:
    """Reviewed requirements with and without at least one proposal."""

    proposed_requirement_ids: tuple[str, ...]
    unproposed_requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class AssertionProvenance:
    """Structural source linkage for the sole Python assertion in a case.

    The requirement is the exact record from the bound reviewed artifact.  The
    linkage is review evidence, not a semantic proof about generated code.
    """

    requirement: Requirement
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    excerpt: str
    sha256: str


@dataclass(frozen=True)
class ProposedCase:
    """One inert, review-only candidate test and its assertion provenance."""

    id: str
    requirement_id: str
    category: str
    test_file: PurePosixPath
    node: str
    rationale: str
    source_bytes: bytes
    assertion: AssertionProvenance

    @property
    def source_text(self) -> str:
        return self.source_bytes.decode("utf-8")

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source_bytes).hexdigest()


@dataclass(frozen=True)
class TestProposalArtifact:
    """A validated proposal set bound to reviewed requirement bytes."""

    schema_version: int
    reviewed_requirements_sha256: str
    generator_adapter: str
    generator_model: str | None
    generator_response: GeneratorResponseBinding
    coverage: RequirementCoverage
    requirements: tuple[Requirement, ...]
    proposals: tuple[ProposedCase, ...]

    @property
    def candidate_files(self) -> dict[PurePosixPath, bytes]:
        """Return inert source bytes for review or accepted-case staging.

        Access does not authorize importing, collecting, or executing the code.
        """

        return {
            proposal.test_file: proposal.source_bytes
            for proposal in self.proposals
        }


@dataclass(frozen=True)
class TestProposalWriteResult:
    """Summary of a no-overwrite proposal artifact write."""

    output_file: Path
    reviewed_requirements_sha256: str
    generator_response_sha256: str
    proposal_count: int
    unproposed_requirement_count: int


def make_test_proposal_request(
    reviewed_requirements_document: bytes,
) -> TestProposalRequest:
    """Create a provider-neutral request from exact reviewed artifact bytes."""

    reviewed_bytes, reviewed = _load_reviewed_requirements(
        reviewed_requirements_document
    )
    requirements = tuple(
        Requirement.from_dict(requirement)
        for requirement in reviewed["requirements"]
    )
    if not requirements:
        raise TestProposalError(
            "reviewed requirements must contain at least one accepted requirement"
        )
    return TestProposalRequest(
        reviewed_requirements_sha256=hashlib.sha256(reviewed_bytes).hexdigest(),
        requirements=requirements,
    )


def generate_test_proposal_artifact(
    reviewed_requirements_document: bytes,
    generator: TestProposalGenerator,
) -> dict[str, Any]:
    """Invoke one provider-neutral adapter and normalize its inert response."""

    identity = generator.identity
    if not isinstance(identity, GeneratorIdentity):
        raise TestProposalError(
            "test proposal generator identity must be a GeneratorIdentity"
        )
    request = make_test_proposal_request(reviewed_requirements_document)
    proposals = generator.propose(request)
    return build_test_proposal_artifact(
        reviewed_requirements_document,
        generator_adapter=identity.adapter,
        generator_model=identity.model,
        proposals=proposals,
    )


def parse_test_proposal_response(
    document: bytes,
) -> tuple[Mapping[str, object], ...]:
    """Parse a bounded provider-neutral response without trusting provider JSON.

    The response envelope is exactly ``{"schema_version": 1,
    "proposals": [...]}``. Duplicate keys, non-finite constants, unknown
    envelope fields, non-object proposals, and oversized arrays are rejected
    before normalization.
    """

    response_bytes = _require_bytes(
        document,
        label="test proposal generator response",
        max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
    )
    payload = _decode_json(
        response_bytes,
        label="test proposal generator response",
    )
    expected = {"schema_version", "proposals"}
    missing = expected - set(payload)
    unknown = set(payload) - expected
    if missing:
        raise TestProposalError(
            "test proposal generator response is missing " + sorted(missing)[0]
        )
    if unknown:
        raise TestProposalError(
            "test proposal generator response contains unsupported field "
            + sorted(unknown)[0]
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise TestProposalError(
            "test proposal generator response schema_version must be 1"
        )
    proposals = payload["proposals"]
    if not isinstance(proposals, list):
        raise TestProposalError(
            "test proposal generator response proposals must be an array"
        )
    if not proposals:
        raise TestProposalError(
            "test proposal generator response proposals must not be empty"
        )
    if len(proposals) > _MAX_PROPOSALS:
        raise TestProposalError(
            "test proposal generator response proposals must contain at most "
            f"{_MAX_PROPOSALS} entries"
        )
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, Mapping):
            raise TestProposalError(
                f"test proposal generator response proposals[{index}] must be "
                "an object"
            )
    return tuple(proposals)


def write_test_proposal_artifact(
    reviewed_requirements_file: Path,
    generator_response_file: Path,
    output_file: Path,
    *,
    generator: GeneratorIdentity,
    expected_reviewed_requirements_sha256: str | None = None,
    expected_generator_response_sha256: str | None = None,
) -> TestProposalWriteResult:
    """Build from exact files and atomically create an output without overwrite.

    Both inputs are read once into bounded immutable byte strings. Optional
    expected digests let a caller bind this write to an earlier analysis pass,
    closing the analysis/read race without re-reading either source file.
    """

    if not isinstance(generator, GeneratorIdentity):
        raise TestProposalError("generator must be a GeneratorIdentity")
    reviewed_path = Path(reviewed_requirements_file).resolve()
    response_path = Path(generator_response_file).resolve()
    output_path = Path(output_file).absolute()
    reviewed_bytes = _read_bounded_file(
        reviewed_path,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    response_bytes = _read_bounded_file(
        response_path,
        label="test proposal generator response",
        max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
    )
    _require_expected_digest(
        reviewed_bytes,
        expected_reviewed_requirements_sha256,
        label="reviewed requirements",
    )
    _require_expected_digest(
        response_bytes,
        expected_generator_response_sha256,
        label="test proposal generator response",
    )
    raw_proposals = parse_test_proposal_response(response_bytes)
    artifact = build_test_proposal_artifact(
        reviewed_bytes,
        generator_adapter=generator.adapter,
        generator_model=generator.model,
        proposals=raw_proposals,
        generator_response_document=response_bytes,
    )
    document = encode_test_proposal_artifact(artifact)
    _write_exclusive(output_path, document)
    coverage = artifact["coverage"]
    assert isinstance(coverage, Mapping)
    unproposed = coverage["unproposed_requirement_ids"]
    assert isinstance(unproposed, list)
    return TestProposalWriteResult(
        output_file=output_path,
        reviewed_requirements_sha256=hashlib.sha256(reviewed_bytes).hexdigest(),
        generator_response_sha256=hashlib.sha256(response_bytes).hexdigest(),
        proposal_count=len(artifact["proposals"]),
        unproposed_requirement_count=len(unproposed),
    )


def build_test_proposal_artifact(
    reviewed_requirements_document: bytes,
    *,
    generator_adapter: str,
    generator_model: str | None,
    proposals: Iterable[Mapping[str, object]],
    generator_response_document: bytes | None = None,
) -> dict[str, Any]:
    """Normalize provider output into a deterministic review-only artifact.

    Generator adapters supply only six provider-neutral fields per proposal:
    ``requirement_id``, ``category``, ``test_file``, ``node``, ``rationale``,
    and complete Python ``source`` text.  This function derives all hashes,
    source spans, requirement previews, review markers, and stable case ids.
    """

    reviewed_bytes, reviewed = _load_reviewed_requirements(
        reviewed_requirements_document
    )
    _validate_generator_identity(generator_adapter, generator_model)
    raw_proposals = _bounded_proposal_iterable(proposals)
    requirements = reviewed["requirements"]
    assert isinstance(requirements, list)
    if not requirements:
        raise TestProposalError(
            "reviewed requirements must contain at least one accepted requirement"
        )
    requirement_by_id = {
        requirement["id"]: requirement for requirement in requirements
    }

    normalized: list[dict[str, Any]] = []
    total_source_bytes = 0
    for index, raw in enumerate(raw_proposals):
        if not isinstance(raw, Mapping):
            raise TestProposalError(f"proposals[{index}] must be an object")
        missing = _RAW_PROPOSAL_KEYS - set(raw)
        unknown = set(raw) - _RAW_PROPOSAL_KEYS
        if missing:
            raise TestProposalError(
                f"proposals[{index}] is missing {sorted(missing)[0]}"
            )
        if unknown:
            raise TestProposalError(
                f"proposals[{index}] contains unsupported field "
                f"{sorted(unknown)[0]}"
            )
        for field in _RAW_PROPOSAL_KEYS:
            if not isinstance(raw[field], str):
                raise TestProposalError(
                    f"proposals[{index}].{field} must be a string"
                )
        requirement_id = raw["requirement_id"]
        category = raw["category"]
        test_file = raw["test_file"]
        node = raw["node"]
        rationale = raw["rationale"]
        source_text = raw["source"]
        assert isinstance(requirement_id, str)
        assert isinstance(category, str)
        assert isinstance(test_file, str)
        assert isinstance(node, str)
        assert isinstance(rationale, str)
        assert isinstance(source_text, str)

        requirement = requirement_by_id.get(requirement_id)
        if requirement is None:
            raise TestProposalError(
                f"proposals[{index}].requirement_id references an unreviewed "
                f"requirement {requirement_id!r}"
            )
        if category not in _CATEGORY_ORDER:
            raise TestProposalError(
                f"proposals[{index}].category is not supported: {category!r}"
            )
        _validate_text_field(
            rationale,
            label=f"proposals[{index}].rationale",
            max_bytes=_MAX_RATIONALE_BYTES,
        )
        _validate_safe_test_file(test_file, label=f"proposals[{index}].test_file")
        source_bytes = _encode_utf8(
            source_text,
            label=f"proposals[{index}].source",
        )
        if not source_bytes:
            raise TestProposalError(f"proposals[{index}].source must not be empty")
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            raise TestProposalError(
                f"proposals[{index}].source exceeds the "
                f"{_MAX_SOURCE_BYTES}-byte limit"
            )
        total_source_bytes += len(source_bytes)
        if total_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise TestProposalError(
                "proposal sources exceed the "
                f"{_MAX_TOTAL_SOURCE_BYTES}-byte aggregate limit"
            )
        try:
            source_validation = validate_candidate_test_source(
                source_bytes,
                case_id=f"proposal {index}",
                node=node,
                source_name=test_file,
            )
        except ValueError as exc:
            raise TestProposalError(
                f"proposals[{index}].source is not an admissible candidate: {exc}"
            ) from exc
        if source_validation.python_assertion_count != 1:
            raise TestProposalError(
                f"proposals[{index}].source must contain exactly one Python "
                "assert in the entire candidate file; found "
                f"{source_validation.python_assertion_count}"
            )
        assertion = _assertion_record(source_validation, requirement=requirement)
        proposal: dict[str, Any] = {
            "requirement_id": requirement_id,
            "category": category,
            "test_file": test_file,
            "node": node,
            "rationale": rationale,
            "review_status": "requires_review",
            "source": {
                "text": source_text,
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
            },
            "assertion": assertion,
        }
        proposal["id"] = _stable_case_id(proposal)
        normalized.append(proposal)

    if not normalized:
        raise TestProposalError("proposals must contain at least one entry")
    canonical_response = _canonical_generator_response(raw_proposals)
    canonical_response_sha256 = hashlib.sha256(canonical_response).hexdigest()
    if generator_response_document is None:
        response_binding = "canonical"
        response_sha256 = canonical_response_sha256
    else:
        exact_response = _require_bytes(
            generator_response_document,
            label="test proposal generator response",
            max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
        )
        parsed_response = parse_test_proposal_response(exact_response)
        if _canonical_generator_response(parsed_response) != canonical_response:
            raise TestProposalError(
                "generator response bytes do not match the supplied raw proposals"
            )
        response_binding = "exact"
        response_sha256 = hashlib.sha256(exact_response).hexdigest()
    normalized.sort(key=_proposal_order_key)
    binding_ids = [requirement["id"] for requirement in requirements]
    proposed_ids = {
        proposal["requirement_id"] for proposal in normalized
    }
    payload: dict[str, Any] = {
        # Proposal artifacts inherit the reviewed-requirements generation.
        # Only a v2 chain carries the exact issue binding required by the
        # review/materialization workflow; legacy v1 remains inspection-only.
        "schema_version": reviewed["schema_version"],
        "artifact_type": "test-proposals",
        "review_status": "requires_review",
        "reviewed_requirements": {
            "sha256": hashlib.sha256(reviewed_bytes).hexdigest(),
            "schema_version": reviewed["schema_version"],
            "requirement_count": len(requirements),
            "requirement_ids": binding_ids,
        },
        "generator": {
            "adapter": generator_adapter,
            "model": generator_model,
        },
        "generator_response": {
            "binding": response_binding,
            "sha256": response_sha256,
            "canonical_sha256": canonical_response_sha256,
        },
        "coverage": {
            "proposed_requirement_ids": [
                requirement_id
                for requirement_id in binding_ids
                if requirement_id in proposed_ids
            ],
            "unproposed_requirement_ids": [
                requirement_id
                for requirement_id in binding_ids
                if requirement_id not in proposed_ids
            ],
        },
        "proposals": normalized,
    }
    validate_test_proposal_artifact(
        payload,
        reviewed_requirements_document=reviewed_bytes,
        generator_response_document=generator_response_document,
    )
    return payload


def encode_test_proposal_artifact(payload: Mapping[str, object]) -> bytes:
    """Encode an artifact deterministically without provider-specific state."""

    try:
        document = (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TestProposalError(f"cannot encode test proposal artifact: {exc}") from exc
    if len(document) > _MAX_PROPOSAL_DOCUMENT_BYTES:
        raise TestProposalError(
            "test proposal artifact exceeds the "
            f"{_MAX_PROPOSAL_DOCUMENT_BYTES}-byte limit"
        )
    return document


def validate_test_proposal_artifact(
    payload: object,
    *,
    reviewed_requirements_document: bytes,
    generator_response_document: bytes | None = None,
) -> None:
    """Validate schema, exact input binding, and assertion semantics."""

    reviewed_bytes, reviewed = _load_reviewed_requirements(
        reviewed_requirements_document
    )
    _validate_schema(payload)
    assert isinstance(payload, Mapping)
    _validate_semantics(
        payload,
        reviewed_bytes=reviewed_bytes,
        reviewed=reviewed,
        generator_response_document=generator_response_document,
    )


def load_test_proposal_artifact(
    document: bytes,
    *,
    reviewed_requirements_document: bytes,
    generator_response_document: bytes | None = None,
) -> TestProposalArtifact:
    """Parse and validate exact proposal bytes into immutable domain objects."""

    proposal_bytes = _require_bytes(
        document,
        label="test proposal artifact",
        max_bytes=_MAX_PROPOSAL_DOCUMENT_BYTES,
    )
    payload = _decode_json(proposal_bytes, label="test proposal artifact")
    validate_test_proposal_artifact(
        payload,
        reviewed_requirements_document=reviewed_requirements_document,
        generator_response_document=generator_response_document,
    )
    _, reviewed = _load_reviewed_requirements(reviewed_requirements_document)
    requirements = tuple(
        Requirement.from_dict(requirement)
        for requirement in reviewed["requirements"]
    )
    parsed: list[ProposedCase] = []
    for proposal in payload["proposals"]:
        assertion = proposal["assertion"]
        source = proposal["source"]
        parsed.append(
            ProposedCase(
                id=proposal["id"],
                requirement_id=proposal["requirement_id"],
                category=proposal["category"],
                test_file=PurePosixPath(proposal["test_file"]),
                node=proposal["node"],
                rationale=proposal["rationale"],
                source_bytes=source["text"].encode("utf-8"),
                assertion=AssertionProvenance(
                    requirement=Requirement.from_dict(assertion["requirement"]),
                    start_line=assertion["start_line"],
                    end_line=assertion["end_line"],
                    start_column=assertion["start_column"],
                    end_column=assertion["end_column"],
                    excerpt=assertion["excerpt"],
                    sha256=assertion["sha256"],
                ),
            )
        )
    generator = payload["generator"]
    generator_response = payload["generator_response"]
    coverage = payload["coverage"]
    binding = payload["reviewed_requirements"]
    assert isinstance(generator, Mapping)
    assert isinstance(generator_response, Mapping)
    assert isinstance(coverage, Mapping)
    assert isinstance(binding, Mapping)
    return TestProposalArtifact(
        schema_version=payload["schema_version"],
        reviewed_requirements_sha256=binding["sha256"],
        generator_adapter=generator["adapter"],
        generator_model=generator["model"],
        generator_response=GeneratorResponseBinding(
            binding=generator_response["binding"],
            sha256=generator_response["sha256"],
            canonical_sha256=generator_response["canonical_sha256"],
        ),
        coverage=RequirementCoverage(
            proposed_requirement_ids=tuple(
                coverage["proposed_requirement_ids"]
            ),
            unproposed_requirement_ids=tuple(
                coverage["unproposed_requirement_ids"]
            ),
        ),
        requirements=requirements,
        proposals=tuple(parsed),
    )


def _validate_semantics(
    payload: Mapping[str, object],
    *,
    reviewed_bytes: bytes,
    reviewed: Mapping[str, Any],
    generator_response_document: bytes | None,
) -> None:
    if type(payload["schema_version"]) is not int:
        raise TestProposalError(
            "test_proposals.schema_version: expected integer"
        )
    binding = payload["reviewed_requirements"]
    generator = payload["generator"]
    generator_response = payload["generator_response"]
    coverage = payload["coverage"]
    proposals = payload["proposals"]
    assert isinstance(binding, Mapping)
    assert isinstance(generator, Mapping)
    assert isinstance(generator_response, Mapping)
    assert isinstance(coverage, Mapping)
    assert isinstance(proposals, list)
    if type(binding["schema_version"]) is not int:
        raise TestProposalError(
            "test_proposals.reviewed_requirements.schema_version: expected integer"
        )
    if payload["schema_version"] != binding["schema_version"]:
        raise TestProposalError(
            "test_proposals.schema_version: must match the bound "
            "reviewed-requirements schema version"
        )
    requirements = reviewed["requirements"]
    assert isinstance(requirements, list)
    expected_ids = [requirement["id"] for requirement in requirements]
    if binding["sha256"] != hashlib.sha256(reviewed_bytes).hexdigest():
        raise TestProposalError(
            "test_proposals.reviewed_requirements.sha256: does not match the "
            "exact reviewed-requirements bytes"
        )
    if binding["schema_version"] != reviewed["schema_version"]:
        raise TestProposalError(
            "test_proposals.reviewed_requirements.schema_version: does not "
            "match the reviewed artifact"
        )
    if binding["requirement_count"] != len(requirements):
        raise TestProposalError(
            "test_proposals.reviewed_requirements.requirement_count: does not "
            "match the reviewed artifact"
        )
    if binding["requirement_ids"] != expected_ids:
        raise TestProposalError(
            "test_proposals.reviewed_requirements.requirement_ids: must exactly "
            "match the reviewed requirements in order"
        )
    _validate_generator_identity(generator["adapter"], generator["model"])
    requirement_by_id = {
        requirement["id"]: requirement for requirement in requirements
    }

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    seen_portable_files: dict[str, str] = {}
    seen_portable_paths: list[tuple[tuple[str, ...], str]] = []
    directory_spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    order_keys: list[tuple[object, ...]] = []
    total_source_bytes = 0
    for index, proposal in enumerate(proposals):
        assert isinstance(proposal, Mapping)
        identifier = proposal["id"]
        requirement_id = proposal["requirement_id"]
        category = proposal["category"]
        test_file = proposal["test_file"]
        node = proposal["node"]
        rationale = proposal["rationale"]
        source = proposal["source"]
        assertion = proposal["assertion"]
        assert isinstance(identifier, str)
        assert isinstance(requirement_id, str)
        assert isinstance(category, str)
        assert isinstance(test_file, str)
        assert isinstance(node, str)
        assert isinstance(rationale, str)
        assert isinstance(source, Mapping)
        assert isinstance(assertion, Mapping)
        if identifier in seen_ids:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].id: proposal id is duplicated"
            )
        seen_ids.add(identifier)
        if test_file in seen_files:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].test_file: each proposal "
                "must use a distinct candidate file"
            )
        seen_files.add(test_file)
        _validate_safe_test_file(
            test_file,
            label=f"test_proposals.proposals[{index}].test_file",
            materialization_safe=payload["schema_version"] == 2,
        )
        portable_key = test_file.casefold()
        previous_spelling = seen_portable_files.get(portable_key)
        if previous_spelling is not None:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].test_file: portable path "
                f"collision with {previous_spelling!r}"
            )
        seen_portable_files[portable_key] = test_file
        if payload["schema_version"] == 2:
            path_parts = tuple(PurePosixPath(test_file).parts)
            portable_parts = tuple(part.casefold() for part in path_parts)
            for previous_parts, previous_path in seen_portable_paths:
                common_length = min(len(previous_parts), len(portable_parts))
                if (
                    previous_parts[:common_length]
                    == portable_parts[:common_length]
                    and len(previous_parts) != len(portable_parts)
                ):
                    raise TestProposalError(
                        f"test_proposals.proposals[{index}].test_file: "
                        f"file/directory path conflict with {previous_path!r}"
                    )
            seen_portable_paths.append((portable_parts, test_file))
            for depth in range(1, len(path_parts)):
                directory_key = portable_parts[:depth]
                spelling = path_parts[:depth]
                previous_spelling_parts = directory_spellings.get(directory_key)
                if (
                    previous_spelling_parts is not None
                    and previous_spelling_parts != spelling
                ):
                    raise TestProposalError(
                        f"test_proposals.proposals[{index}].test_file: portable "
                        "directory spelling collision"
                    )
                directory_spellings[directory_key] = spelling
                if len(directory_spellings) > _MAX_UNIQUE_DIRECTORIES:
                    raise TestProposalError(
                        "test_proposals.proposals: candidate paths exceed the "
                        f"{_MAX_UNIQUE_DIRECTORIES}-directory limit"
                    )
        _validate_text_field(
            rationale,
            label=f"test_proposals.proposals[{index}].rationale",
            max_bytes=_MAX_RATIONALE_BYTES,
        )

        requirement = requirement_by_id.get(requirement_id)
        if requirement is None:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].requirement_id: unknown or "
                "unreviewed requirement"
            )
        if assertion["requirement"] != requirement:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].assertion.requirement: must "
                "exactly match the bound reviewed requirement"
            )
        assertion_requirement = assertion["requirement"]
        assert isinstance(assertion_requirement, Mapping)
        if assertion_requirement["id"] != requirement_id:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].assertion.requirement.id: "
                "does not match requirement_id"
            )
        requirement_kind = requirement["kind"]
        if category == "requested" and requirement_kind != "requested":
            raise TestProposalError(
                f"test_proposals.proposals[{index}].category: requested cases "
                "must link a requested requirement"
            )
        if category == "preservation" and requirement_kind != "preservation":
            raise TestProposalError(
                f"test_proposals.proposals[{index}].category: preservation "
                "cases must link a preservation requirement"
            )

        source_text = source["text"]
        source_sha256 = source["sha256"]
        assert isinstance(source_text, str)
        assert isinstance(source_sha256, str)
        source_bytes = _encode_utf8(
            source_text,
            label=f"test_proposals.proposals[{index}].source.text",
        )
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].source.text: UTF-8 form "
                f"exceeds the {_MAX_SOURCE_BYTES}-byte limit"
            )
        total_source_bytes += len(source_bytes)
        if total_source_bytes > _MAX_TOTAL_SOURCE_BYTES:
            raise TestProposalError(
                "test_proposals.proposals: source UTF-8 forms exceed the "
                f"{_MAX_TOTAL_SOURCE_BYTES}-byte aggregate limit"
            )
        if hashlib.sha256(source_bytes).hexdigest() != source_sha256:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].source.sha256: digest does "
                "not match exact source text"
            )
        try:
            source_validation = validate_candidate_test_source(
                source_bytes,
                case_id=identifier,
                node=node,
                source_name=test_file,
            )
        except ValueError as exc:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].source: inadmissible "
                f"candidate: {exc}"
            ) from exc
        if source_validation.python_assertion_count != 1:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].source: must contain "
                "exactly one Python assert in the entire candidate file; "
                f"found {source_validation.python_assertion_count}"
            )
        expected_assertion = _assertion_record(
            source_validation,
            requirement=requirement,
        )
        if assertion != expected_assertion:
            raise TestProposalError(
                f"test_proposals.proposals[{index}].assertion: does not match "
                "the sole Python assert statement and reviewed requirement"
            )
        if identifier != _stable_case_id(proposal):
            raise TestProposalError(
                f"test_proposals.proposals[{index}].id: does not match "
                "canonical proposal content"
            )
        order_keys.append(_proposal_order_key(proposal))

    if order_keys != sorted(order_keys):
        raise TestProposalError(
            "test_proposals.proposals: proposals are not in deterministic order"
        )
    reconstructed_response = _canonical_generator_response(
        _raw_proposal_from_normalized(proposal) for proposal in proposals
    )
    reconstructed_sha256 = hashlib.sha256(reconstructed_response).hexdigest()
    if generator_response["canonical_sha256"] != reconstructed_sha256:
        raise TestProposalError(
            "test_proposals.generator_response.canonical_sha256: does not "
            "match canonical raw proposal content"
        )
    response_binding = generator_response["binding"]
    if response_binding == "exact" and generator_response_document is None:
        raise TestProposalError(
            "test_proposals.generator_response: exact binding requires the "
            "original generator response bytes"
        )
    if (
        response_binding == "canonical"
        and generator_response["sha256"] != reconstructed_sha256
    ):
        raise TestProposalError(
            "test_proposals.generator_response.sha256: canonical binding does "
            "not match canonical raw proposal content"
        )
    if generator_response_document is not None:
        response_bytes = _require_bytes(
            generator_response_document,
            label="test proposal generator response",
            max_bytes=_MAX_GENERATOR_RESPONSE_BYTES,
        )
        parsed_response = parse_test_proposal_response(response_bytes)
        if _canonical_generator_response(parsed_response) != reconstructed_response:
            raise TestProposalError(
                "test_proposals.generator_response: supplied response bytes do "
                "not match normalized proposal content"
            )
        if response_binding == "exact" and generator_response["sha256"] != (
            hashlib.sha256(response_bytes).hexdigest()
        ):
            raise TestProposalError(
                "test_proposals.generator_response.sha256: does not match the "
                "exact supplied generator response bytes"
            )
    observed_proposed = {
        proposal["requirement_id"] for proposal in proposals
    }
    expected_proposed = [
        requirement_id
        for requirement_id in expected_ids
        if requirement_id in observed_proposed
    ]
    expected_unproposed = [
        requirement_id
        for requirement_id in expected_ids
        if requirement_id not in observed_proposed
    ]
    if coverage["proposed_requirement_ids"] != expected_proposed:
        raise TestProposalError(
            "test_proposals.coverage.proposed_requirement_ids: must list each "
            "covered reviewed requirement exactly once in reviewed order"
        )
    if coverage["unproposed_requirement_ids"] != expected_unproposed:
        raise TestProposalError(
            "test_proposals.coverage.unproposed_requirement_ids: must list "
            "every uncovered reviewed requirement in reviewed order"
        )


def _assertion_record(
    source_validation: CandidateSourceValidation,
    *,
    requirement: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach exact coordinates returned by the shared candidate validator."""

    return {
        "kind": "python_assert",
        "requirement": dict(requirement),
        "start_line": source_validation.assertion_start_line,
        "end_line": source_validation.assertion_end_line,
        "start_column": source_validation.assertion_start_column,
        "end_column": source_validation.assertion_end_column,
        "excerpt": source_validation.assertion_excerpt,
        "sha256": source_validation.assertion_sha256,
    }


def _stable_case_id(proposal: Mapping[str, object]) -> str:
    identity = {key: value for key, value in proposal.items() if key != "id"}
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    # EvidencePlan ids are capped at 64 characters.  59 hexadecimal digits
    # retain 236 bits after the ``case-`` prefix; duplicate ids are rejected.
    return "case-" + hashlib.sha256(canonical).hexdigest()[:59]


def _proposal_order_key(proposal: Mapping[str, object]) -> tuple[object, ...]:
    return (
        proposal["requirement_id"],
        _CATEGORY_ORDER.get(str(proposal["category"]), len(_CATEGORY_ORDER)),
        proposal["test_file"],
        proposal["node"],
        proposal.get("id", ""),
    )


def _validate_safe_test_file(
    value: str,
    *,
    label: str,
    materialization_safe: bool = True,
) -> None:
    if any(character in value for character in "\\\r\n\0"):
        raise TestProposalError(
            f"{label} must be a single-line POSIX path without backslashes or NUL"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix != ".py"
    ):
        raise TestProposalError(
            f"{label} must be a canonical safe relative .py path"
        )
    if materialization_safe and len(path.parts) > _MAX_PATH_COMPONENTS:
        raise TestProposalError(
            f"{label} must contain at most {_MAX_PATH_COMPONENTS} components"
        )
    if (
        materialization_safe
        and path.parts[0].casefold() in _MATERIALIZATION_RESERVED_ROOTS
    ):
        raise TestProposalError(
            f"{label} uses a reserved materialization root {path.parts[0]!r}"
        )
    for component in path.parts:
        if not _PORTABLE_PATH_COMPONENT.fullmatch(component):
            raise TestProposalError(
                f"{label} contains a non-portable path component {component!r}"
            )
        if component.endswith("."):
            raise TestProposalError(
                f"{label} contains a path component ending in a dot "
                f"{component!r}"
            )
        if len(component.encode("ascii")) > 255:
            raise TestProposalError(
                f"{label} contains a path component exceeding 255 bytes"
            )
        stem = component.split(".", 1)[0].casefold()
        if stem in _WINDOWS_RESERVED_COMPONENTS:
            raise TestProposalError(
                f"{label} contains reserved path component {component!r}"
            )


def _validate_text_field(value: str, *, label: str, max_bytes: int) -> None:
    if not value or not value.strip():
        raise TestProposalError(f"{label} must not be empty")
    if value != value.strip():
        raise TestProposalError(f"{label} must not have surrounding whitespace")
    if "\0" in value:
        raise TestProposalError(f"{label} must not contain NUL characters")
    if len(_encode_utf8(value, label=label)) > max_bytes:
        raise TestProposalError(f"{label} exceeds the {max_bytes}-byte limit")


def _encode_utf8(value: str, *, label: str) -> bytes:
    """Encode untrusted text while keeping input errors in the public API."""

    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TestProposalError(
            f"{label} must not contain unpaired Unicode surrogates"
        ) from exc


def _validate_generator_identity(adapter: object, model: object) -> None:
    if not isinstance(adapter, str):
        raise TestProposalError("test_proposals.generator.adapter must be a string")
    _validate_text_field(
        adapter,
        label="test_proposals.generator.adapter",
        max_bytes=1024,
    )
    if any(character in adapter for character in "\r\n"):
        raise TestProposalError(
            "test_proposals.generator.adapter must be a single line"
        )
    if model is not None:
        if not isinstance(model, str):
            raise TestProposalError(
                "test_proposals.generator.model must be a string or null"
            )
        _validate_text_field(
            model,
            label="test_proposals.generator.model",
            max_bytes=1024,
        )
        if any(character in model for character in "\r\n"):
            raise TestProposalError(
                "test_proposals.generator.model must be a single line"
            )


def _bounded_proposal_iterable(
    proposals: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    try:
        values = list(itertools.islice(iter(proposals), _MAX_PROPOSALS + 1))
    except TypeError as exc:
        raise TestProposalError("proposals must be iterable") from exc
    if len(values) > _MAX_PROPOSALS:
        raise TestProposalError(
            f"proposals must contain at most {_MAX_PROPOSALS} entries"
        )
    return values


def _raw_proposal_from_normalized(
    proposal: Mapping[str, object],
) -> dict[str, object]:
    source = proposal["source"]
    assert isinstance(source, Mapping)
    return {
        "requirement_id": proposal["requirement_id"],
        "category": proposal["category"],
        "test_file": proposal["test_file"],
        "node": proposal["node"],
        "rationale": proposal["rationale"],
        "source": source["text"],
    }


def _canonical_generator_response(
    proposals: Iterable[Mapping[str, object]],
) -> bytes:
    """Encode raw proposal content independent of provider JSON/order."""

    normalized = [
        {key: proposal[key] for key in sorted(_RAW_PROPOSAL_KEYS)}
        for proposal in proposals
    ]
    normalized.sort(
        key=lambda proposal: json.dumps(
            proposal,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return json.dumps(
        {"schema_version": 1, "proposals": normalized},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _load_reviewed_requirements(
    document: bytes,
) -> tuple[bytes, dict[str, Any]]:
    reviewed_bytes = _require_bytes(
        document,
        label="reviewed requirements",
        max_bytes=_MAX_REVIEWED_REQUIREMENTS_BYTES,
    )
    reviewed = _decode_json(reviewed_bytes, label="reviewed requirements")
    try:
        validate_reviewed_requirements_artifact(reviewed)
    except ValueError as exc:
        raise TestProposalError(f"reviewed requirements are invalid: {exc}") from exc
    return reviewed_bytes, reviewed


def _require_bytes(document: object, *, label: str, max_bytes: int) -> bytes:
    if not isinstance(document, bytes):
        raise TestProposalError(f"{label} must be supplied as exact bytes")
    if not document.strip():
        raise TestProposalError(f"{label} is empty")
    if len(document) > max_bytes:
        raise TestProposalError(f"{label} exceeds the {max_bytes}-byte limit")
    return document


def _read_bounded_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as stream:
            document = stream.read(max_bytes + 1)
    except OSError as exc:
        raise TestProposalError(f"cannot read {label} {path}: {exc}") from exc
    return _require_bytes(document, label=label, max_bytes=max_bytes)


def _require_expected_digest(
    document: bytes,
    expected_sha256: str | None,
    *,
    label: str,
) -> None:
    if expected_sha256 is None:
        return
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise TestProposalError(f"expected {label} SHA-256 is invalid")
    if hashlib.sha256(document).hexdigest() != expected_sha256:
        raise TestProposalError(
            f"{label} changed after the earlier analysis pass"
        )


def _write_exclusive(path: Path, document: bytes) -> None:
    if path.exists():
        raise TestProposalError(f"output file already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
    except OSError as exc:
        raise TestProposalError(
            f"cannot create temporary output beside {path}: {exc}"
        ) from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise TestProposalError(f"output file already exists: {path}") from exc
    except OSError as exc:
        raise TestProposalError(f"cannot write output file {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _decode_json(document: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TestProposalError(f"{label} must be UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise TestProposalError(f"{label} contains {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TestProposalError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except (MemoryError, RecursionError, ValueError) as exc:
        raise TestProposalError(f"{label} is not safely parseable JSON") from exc
    if not isinstance(payload, dict):
        raise TestProposalError(f"{label} must contain a JSON object")
    return payload


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def _validate_schema(payload: object) -> None:
    errors = tuple(_validator().iter_errors(payload))
    if not errors:
        return
    leaves = tuple(leaf for error in errors for leaf in _leaf_errors(error))
    error = min(
        leaves,
        key=lambda item: (
            _path_sort_key(item.absolute_path),
            tuple(str(part) for part in item.absolute_schema_path),
            str(item.validator),
            item.message,
        ),
    )
    path, message = _describe_error(error)
    raise TestProposalError(f"{_format_path(path)}: {message}")


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    resource = (
        files("patch_verifier")
        .joinpath("schemas")
        .joinpath("test-proposals.schema.json")
    )
    try:
        schema = json.loads(resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise RuntimeError("packaged test-proposals-v1 schema is invalid") from exc
    return Draft202012Validator(schema)


def _leaf_errors(error: ValidationError) -> Iterator[ValidationError]:
    if not error.context:
        yield error
        return
    for child in error.context:
        yield from _leaf_errors(child)


def _path_sort_key(path: Sequence[object]) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, item) if isinstance(item, int) else (1, str(item)) for item in path
    )


def _describe_error(error: ValidationError) -> tuple[tuple[object, ...], str]:
    path = tuple(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, Mapping):
        missing = sorted(set(error.validator_value) - set(error.instance))
        if missing:
            return (*path, missing[0]), "required property is missing"
    if error.validator == "additionalProperties" and isinstance(
        error.instance, Mapping
    ):
        allowed = set(error.schema.get("properties", ()))
        unexpected = sorted(set(error.instance) - allowed)
        if unexpected:
            return (*path, unexpected[0]), "property is not allowed"
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, str):
            return path, f"expected {expected}"
        return path, "expected " + " or ".join(str(item) for item in expected)
    if error.validator == "const":
        return path, f"expected constant {error.validator_value!r}"
    if error.validator == "enum":
        return path, "expected one of " + ", ".join(
            repr(item) for item in error.validator_value
        )
    if error.validator == "pattern":
        return path, "value does not match the required pattern"
    if error.validator == "minItems":
        return path, f"must contain at least {error.validator_value} items"
    if error.validator == "maxItems":
        return path, f"must contain at most {error.validator_value} items"
    if error.validator == "maxLength":
        return path, f"must contain at most {error.validator_value} characters"
    return path, " ".join(error.message.split())[:240]


def _format_path(path: Sequence[object]) -> str:
    result = "test_proposals"
    for item in path:
        result += f"[{item}]" if isinstance(item, int) else f".{item}"
    return result


__all__ = [
    "AssertionProvenance",
    "GeneratorIdentity",
    "GeneratorResponseBinding",
    "ProposedCase",
    "RequirementCoverage",
    "TestProposalArtifact",
    "TestProposalError",
    "TestProposalGenerator",
    "TestProposalRequest",
    "TestProposalWriteResult",
    "build_test_proposal_artifact",
    "encode_test_proposal_artifact",
    "generate_test_proposal_artifact",
    "load_test_proposal_artifact",
    "make_test_proposal_request",
    "parse_test_proposal_response",
    "validate_test_proposal_artifact",
    "write_test_proposal_artifact",
]
