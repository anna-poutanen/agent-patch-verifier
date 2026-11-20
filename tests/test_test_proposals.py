from __future__ import annotations

import copy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import pytest

import patch_verifier.test_proposals as proposal_module
from patch_verifier.review import (
    encode_review_artifact,
    finalize_requirement_review,
    prepare_requirement_review,
)
from patch_verifier.test_proposals import (
    GeneratorIdentity,
    TestProposalError as ProposalError,
    build_test_proposal_artifact,
    encode_test_proposal_artifact,
    generate_test_proposal_artifact,
    load_test_proposal_artifact,
    make_test_proposal_request,
    parse_test_proposal_response,
    validate_test_proposal_artifact,
    write_test_proposal_artifact,
)


def _candidate(
    *,
    excerpt: str,
    source_kind: str,
    path: str,
    line: int,
) -> dict[str, Any]:
    statement = (
        excerpt
        if source_kind == "issue"
        else "Preserve the behavior asserted by the base test: "
        + excerpt.removeprefix("assert ")
    )
    start_column = 0 if source_kind == "issue" else 4
    source = {
        "path": path,
        "start_line": line,
        "end_line": line,
        "start_column": start_column,
        "end_column": start_column + len(excerpt.encode("utf-8")),
        "excerpt": excerpt,
        "sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }
    intent = "requested" if source_kind == "issue" else "preservation"
    identifier_payload = {
        "intent": intent,
        "source": source,
        "source_kind": source_kind,
        "statement": statement,
    }
    canonical = json.dumps(
        identifier_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "id": "cand-" + hashlib.sha256(canonical).hexdigest(),
        "statement": statement,
        "intent": intent,
        "source_kind": source_kind,
        "source": source,
        "review_status": "requires_review",
        "confidence": "high",
        "rationale": "Exact source text provides a reviewable behavior candidate.",
    }


@pytest.fixture
def reviewed_document() -> bytes:
    issue_text = "# Resolver\n\nExplicit zero must be returned unchanged.\n"
    candidates = [
        _candidate(
            excerpt="Explicit zero must be returned unchanged.",
            source_kind="issue",
            path="requests/zero.contract",
            line=3,
        ),
        _candidate(
            excerpt="assert resolve(None) == 5",
            source_kind="pytest_assertion",
            path="tests/test_resolve.py",
            line=7,
        ),
    ]
    draft = {
        "schema_version": 1,
        "tool_version": "0.4.0",
        "environment": {
            "python_executable": "/usr/bin/python3",
            "python_version": "3.13.1",
            "platform": "test",
        },
        "inputs": {
            "repository": "/work/resolver",
            "base_revision": "HEAD",
            "base_sha": "b" * 40,
            "base_context_sha256": "a" * 64,
            "patched_tree_hash": "c" * 40,
            "patch_sha256": "d" * 64,
            "issue_sha256": hashlib.sha256(issue_text.encode()).hexdigest(),
            "issue_source": "requests/zero.contract",
        },
        "changed_symbols": [],
        "draft": {
            "schema_version": 1,
            "candidates": candidates,
            "diagnostics": [],
            "limitations": ["Every candidate requires a human decision."],
            "truncated": False,
        },
    }
    draft_document = encode_review_artifact(draft)
    review = prepare_requirement_review(draft_document)
    for decision in review["decisions"]:
        decision.update(
            action="accept",
            rationale="Accepted as the source-backed test expectation.",
        )
    finalized = finalize_requirement_review(
        draft_document,
        encode_review_artifact(review),
    )
    return encode_review_artifact(finalized)


def _requirement_ids(reviewed_document: bytes) -> list[str]:
    return [
        requirement["id"]
        for requirement in json.loads(reviewed_document)["requirements"]
    ]


def _raw_proposal(
    requirement_id: str,
    *,
    category: str = "boundary",
    test_file: str = "generated_tests/test_zero.py",
    node: str = "test_zero_is_preserved",
    source: str | None = None,
) -> dict[str, str]:
    return {
        "requirement_id": requirement_id,
        "category": category,
        "test_file": test_file,
        "node": node,
        "rationale": "Checks one challenging value against the reviewed requirement.",
        "source": source
        or (
            "from value import resolve\n\n"
            "def test_zero_is_preserved():\n"
            "    label = 'π'\n"
            "    assert resolve(0) == 0\n"
        ),
    }


def _artifact(reviewed_document: bytes) -> dict[str, Any]:
    requirement_id = _requirement_ids(reviewed_document)[0]
    return build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="fixture-adapter",
        generator_model="fixture-model",
        proposals=[_raw_proposal(requirement_id)],
    )


def test_builds_deterministic_review_only_artifact_with_exact_provenance(
    reviewed_document: bytes,
) -> None:
    requirement_ids = _requirement_ids(reviewed_document)
    raw = _raw_proposal(requirement_ids[0])

    first = build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="generic-adapter",
        generator_model="model-name",
        proposals=[raw],
    )
    second = build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="generic-adapter",
        generator_model="model-name",
        proposals=[raw],
    )

    assert first == second
    assert first["schema_version"] == 2
    assert first["review_status"] == "requires_review"
    assert first["reviewed_requirements"] == {
        "sha256": hashlib.sha256(reviewed_document).hexdigest(),
        "schema_version": 2,
        "requirement_count": 2,
        "requirement_ids": requirement_ids,
    }
    assert first["coverage"] == {
        "proposed_requirement_ids": [requirement_ids[0]],
        "unproposed_requirement_ids": [requirement_ids[1]],
    }
    assert first["generator_response"]["binding"] == "canonical"
    assert first["generator_response"]["sha256"] == first[
        "generator_response"
    ]["canonical_sha256"]
    proposal = first["proposals"][0]
    assert len(proposal["id"]) == 64
    assert proposal["id"].startswith("case-")
    assert proposal["review_status"] == "requires_review"
    assert proposal["assertion"]["kind"] == "python_assert"
    assert proposal["assertion"]["excerpt"] == "assert resolve(0) == 0"
    assert proposal["assertion"]["requirement"] == json.loads(
        reviewed_document
    )["requirements"][0]
    assert proposal["source"]["sha256"] == hashlib.sha256(
        raw["source"].encode("utf-8")
    ).hexdigest()

    floating_version = copy.deepcopy(first)
    floating_version["schema_version"] = 2.0
    with pytest.raises(ProposalError, match="schema_version: expected integer"):
        validate_test_proposal_artifact(
            floating_version,
            reviewed_requirements_document=reviewed_document,
        )


def test_load_exposes_only_inert_sources_for_review_staging(
    reviewed_document: bytes,
) -> None:
    artifact = _artifact(reviewed_document)

    loaded = load_test_proposal_artifact(
        encode_test_proposal_artifact(artifact),
        reviewed_requirements_document=reviewed_document,
    )

    proposal = loaded.proposals[0]
    assert proposal.assertion.requirement.id == proposal.requirement_id
    assert proposal.assertion.excerpt == "assert resolve(0) == 0"
    assert loaded.candidate_files == {
        Path("generated_tests/test_zero.py"): proposal.source_bytes
    }
    assert loaded.generator_response.binding == "canonical"
    assert loaded.generator_response.sha256 == artifact[
        "generator_response"
    ]["sha256"]
    assert loaded.coverage.proposed_requirement_ids == (
        proposal.requirement_id,
    )
    assert len(loaded.coverage.unproposed_requirement_ids) == 1
    assert not hasattr(loaded, "to_evidence_plan_dict")
    assert not hasattr(proposal, "to_evidence_case_dict")


def test_legacy_v1_artifacts_remain_readable_without_claiming_v2_issue_binding(
    reviewed_document: bytes,
) -> None:
    legacy_reviewed = json.loads(reviewed_document)
    legacy_reviewed["schema_version"] = 1
    legacy_reviewed.pop("issue")
    legacy_reviewed_bytes = encode_review_artifact(legacy_reviewed)
    requirement_id = _requirement_ids(legacy_reviewed_bytes)[0]
    current = build_test_proposal_artifact(
        legacy_reviewed_bytes,
        generator_adapter="compatibility-test",
        generator_model=None,
        proposals=[_raw_proposal(requirement_id)],
    )
    assert current["schema_version"] == 1
    assert current["reviewed_requirements"]["schema_version"] == 1

    legacy_proposals = copy.deepcopy(current)
    validate_test_proposal_artifact(
        legacy_proposals,
        reviewed_requirements_document=legacy_reviewed_bytes,
    )
    loaded = load_test_proposal_artifact(
        encode_test_proposal_artifact(legacy_proposals),
        reviewed_requirements_document=legacy_reviewed_bytes,
    )
    assert loaded.schema_version == 1

    falsely_promoted = copy.deepcopy(legacy_proposals)
    falsely_promoted["schema_version"] = 2
    with pytest.raises(
        ProposalError,
        match="reviewed_requirements.*schema_version.*expected constant 2",
    ):
        validate_test_proposal_artifact(
            falsely_promoted,
            reviewed_requirements_document=legacy_reviewed_bytes,
        )

    # v1 predates materialization and accepted paths that are now reserved for
    # staging metadata.  Preserve loader compatibility; v2 generation and the
    # review/materialization boundary still reject this path.
    legacy_reserved = copy.deepcopy(legacy_proposals)
    proposal = legacy_reserved["proposals"][0]
    proposal["test_file"] = "issue.md/test_zero.py"
    proposal["id"] = proposal_module._stable_case_id(proposal)
    canonical = proposal_module._canonical_generator_response(
        [proposal_module._raw_proposal_from_normalized(proposal)]
    )
    digest = hashlib.sha256(canonical).hexdigest()
    legacy_reserved["generator_response"].update(
        sha256=digest,
        canonical_sha256=digest,
    )
    validate_test_proposal_artifact(
        legacy_reserved,
        reviewed_requirements_document=legacy_reviewed_bytes,
    )


def test_builder_sorts_proposals_and_coverage_in_reviewed_order(
    reviewed_document: bytes,
) -> None:
    requested, preservation = _requirement_ids(reviewed_document)
    raw_requested = _raw_proposal(requested)
    raw_preservation = _raw_proposal(
        preservation,
        category="preservation",
        test_file="generated_tests/test_missing.py",
        node="test_missing_uses_default",
        source=(
            "from value import resolve\n\n"
            "def test_missing_uses_default():\n"
            "    assert resolve(None) == 5\n"
        ),
    )

    artifact = build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="manual",
        generator_model=None,
        proposals=[raw_preservation, raw_requested],
    )

    assert [item["requirement_id"] for item in artifact["proposals"]] == [
        requested,
        preservation,
    ]
    assert artifact["coverage"]["proposed_requirement_ids"] == [
        requested,
        preservation,
    ]
    assert artifact["coverage"]["unproposed_requirement_ids"] == []


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda value: value["reviewed_requirements"].update(
                sha256="0" * 64
            ),
            "exact reviewed-requirements bytes",
        ),
        (
            lambda value: value["coverage"].update(
                unproposed_requirement_ids=[]
            ),
            "every uncovered reviewed requirement",
        ),
        (
            lambda value: value["proposals"][0]["source"].update(
                sha256="0" * 64
            ),
            "digest does not match",
        ),
        (
            lambda value: value["generator_response"].update(
                canonical_sha256="0" * 64
            ),
            "canonical raw proposal content",
        ),
        (
            lambda value: value["proposals"][0]["assertion"].update(
                start_line=1
            ),
            "sole Python assert statement",
        ),
        (
            lambda value: value["proposals"][0]["assertion"][
                "requirement"
            ].update(statement="Fabricated expectation."),
            "exactly match the bound reviewed requirement",
        ),
        (
            lambda value: value["proposals"][0].update(id="case-" + "0" * 59),
            "canonical proposal content",
        ),
    ],
)
def test_validation_rejects_tampering(
    reviewed_document: bytes,
    mutate: Any,
    match: str,
) -> None:
    artifact = copy.deepcopy(_artifact(reviewed_document))
    mutate(artifact)

    with pytest.raises(ProposalError, match=match):
        validate_test_proposal_artifact(
            artifact,
            reviewed_requirements_document=reviewed_document,
        )


def test_exact_reviewed_bytes_cannot_be_reserialized_after_binding(
    reviewed_document: bytes,
) -> None:
    artifact = _artifact(reviewed_document)
    reserialized = json.dumps(json.loads(reviewed_document)).encode("utf-8")

    with pytest.raises(ProposalError, match="exact reviewed-requirements bytes"):
        validate_test_proposal_artifact(
            artifact,
            reviewed_requirements_document=reserialized,
        )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"requirement_id": "req-unknown"}, "unreviewed requirement"),
        ({"test_file": "../test_escape.py"}, "safe relative"),
        ({"test_file": "generated\\test_x.py"}, "without backslashes"),
        ({"test_file": "generated//test_x.py"}, "canonical safe relative"),
        ({"test_file": "generated/./test_x.py"}, "canonical safe relative"),
        ({"test_file": "generated./test_x.py"}, "ending in a dot"),
        ({"test_file": f"{'a' * 256}/test_x.py"}, "exceeding 255 bytes"),
        (
            {"test_file": "/".join(["a"] * 32 + ["test_x.py"])},
            "at most 32 components",
        ),
        (
            {"test_file": "evidence-plan.json/test_x.py"},
            "reserved materialization root",
        ),
        ({"rationale": " surrounded "}, "surrounding whitespace"),
        ({"rationale": "\ud800"}, "unpaired Unicode surrogates"),
        ({"source": "\ud800"}, "unpaired Unicode surrogates"),
        ({"category": "unknown"}, "category is not supported"),
        ({"category": "preservation"}, "preservation cases must link"),
        ({"node": "not present"}, "invalid pytest node"),
    ],
)
def test_builder_rejects_untrusted_proposal_fields(
    reviewed_document: bytes,
    change: dict[str, str],
    match: str,
) -> None:
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    raw.update(change)

    with pytest.raises(ProposalError, match=match):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[raw],
        )


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("def test_zero(:\n    assert True\n", "parseable Python"),
        ("def test_zero_is_preserved():\n    pass\n", "exactly one Python assert"),
        (
            "def helper():\n"
            "    assert True\n\n"
            "def test_zero_is_preserved():\n"
            "    assert True\n",
            "entire candidate file; found 2",
        ),
    ],
)
def test_builder_rejects_invalid_or_unlinked_assertions(
    reviewed_document: bytes,
    source: str,
    match: str,
) -> None:
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0], source=source)

    with pytest.raises(ProposalError, match=match):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[raw],
        )


def test_builder_rejects_duplicate_candidate_files(
    reviewed_document: bytes,
) -> None:
    requirement_id = _requirement_ids(reviewed_document)[0]
    first = _raw_proposal(requirement_id)
    second = _raw_proposal(requirement_id, category="caller_level")

    with pytest.raises(ProposalError, match="distinct candidate file"):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[first, second],
        )


def test_builder_rejects_casefolded_portable_path_collisions(
    reviewed_document: bytes,
) -> None:
    requirement_id = _requirement_ids(reviewed_document)[0]
    first = _raw_proposal(
        requirement_id,
        test_file="generated_tests/test_case.py",
    )
    second = _raw_proposal(
        requirement_id,
        category="caller_level",
        test_file="GENERATED_TESTS/test_case.py",
    )

    with pytest.raises(ProposalError, match="portable path collision"):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[first, second],
        )


@pytest.mark.parametrize(
    ("first_path", "second_path", "match"),
    [
        (
            "generated/test_case.py",
            "generated/test_case.py/nested.py",
            "file/directory path conflict",
        ),
        (
            "Generated/test_first.py",
            "generated/test_second.py",
            "directory spelling collision",
        ),
    ],
)
def test_builder_rejects_nonportable_path_tree_conflicts(
    reviewed_document: bytes,
    first_path: str,
    second_path: str,
    match: str,
) -> None:
    requirement_id = _requirement_ids(reviewed_document)[0]
    first = _raw_proposal(requirement_id, test_file=first_path)
    second = _raw_proposal(
        requirement_id,
        category="caller_level",
        test_file=second_path,
    )

    with pytest.raises(ProposalError, match=match):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[first, second],
        )


def test_builder_bounds_unique_candidate_directories(
    reviewed_document: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proposal_module, "_MAX_UNIQUE_DIRECTORIES", 1)
    requirement_id = _requirement_ids(reviewed_document)[0]
    first = _raw_proposal(
        requirement_id,
        test_file="first/test_case.py",
    )
    second = _raw_proposal(
        requirement_id,
        category="caller_level",
        test_file="second/test_case.py",
    )

    with pytest.raises(ProposalError, match="1-directory limit"):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[first, second],
        )


def test_builder_bounds_iterables_before_processing(
    reviewed_document: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proposal_module, "_MAX_PROPOSALS", 2)
    requirement_id = _requirement_ids(reviewed_document)[0]
    proposal = _raw_proposal(requirement_id)

    with pytest.raises(ProposalError, match="at most 2"):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=itertools.repeat(proposal),
        )


def test_builder_enforces_source_byte_budgets(
    reviewed_document: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proposal_module, "_MAX_SOURCE_BYTES", 40)
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])

    with pytest.raises(ProposalError, match="40-byte limit"):
        build_test_proposal_artifact(
            reviewed_document,
            generator_adapter="manual",
            generator_model=None,
            proposals=[raw],
        )


@pytest.mark.parametrize(
    ("document", "match"),
    [
        (
            b'{"schema_version":1,"schema_version":1,"proposals":[]}',
            "duplicate JSON object key",
        ),
        (
            b'{"schema_version":1,"proposals":[],"extra":true}',
            "unsupported field extra",
        ),
        (
            b'{"schema_version":NaN,"proposals":[]}',
            "safely parseable JSON",
        ),
        (
            b'{"schema_version":true,"proposals":[]}',
            "schema_version must be 1",
        ),
        (
            b'{"schema_version":1,"proposals":[1]}',
            r"proposals\[0\] must be an object",
        ),
    ],
)
def test_raw_response_parser_rejects_ambiguous_or_unknown_json(
    document: bytes,
    match: str,
) -> None:
    with pytest.raises(ProposalError, match=match):
        parse_test_proposal_response(document)


def test_raw_response_parser_returns_bounded_mapping_tuple(
    reviewed_document: bytes,
) -> None:
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    document = json.dumps({"schema_version": 1, "proposals": [raw]}).encode()

    parsed = parse_test_proposal_response(document)

    assert parsed == (raw,)


def test_provider_neutral_protocol_has_no_provider_dependency(
    reviewed_document: bytes,
) -> None:
    requirement_id = _requirement_ids(reviewed_document)[0]

    class StaticGenerator:
        identity = GeneratorIdentity("static", None)

        def propose(
            self,
            request: proposal_module.TestProposalRequest,
        ) -> Iterable[Mapping[str, object]]:
            assert request.review_status == "requires_review"
            assert request.reviewed_requirements_sha256 == hashlib.sha256(
                reviewed_document
            ).hexdigest()
            return [_raw_proposal(requirement_id)]

    request = make_test_proposal_request(reviewed_document)
    artifact = generate_test_proposal_artifact(reviewed_document, StaticGenerator())

    assert request.requirements[0].id == requirement_id
    assert artifact["generator"] == {"adapter": "static", "model": None}


def test_generator_identity_is_read_once(
    reviewed_document: bytes,
) -> None:
    requirement_id = _requirement_ids(reviewed_document)[0]

    class DynamicIdentityGenerator:
        reads = 0

        @property
        def identity(self) -> GeneratorIdentity:
            self.reads += 1
            return GeneratorIdentity(f"adapter-{self.reads}")

        def propose(
            self,
            request: proposal_module.TestProposalRequest,
        ) -> Iterable[Mapping[str, object]]:
            return [_raw_proposal(requirement_id)]

    generator = DynamicIdentityGenerator()

    artifact = generate_test_proposal_artifact(reviewed_document, generator)

    assert generator.reads == 1
    assert artifact["generator"]["adapter"] == "adapter-1"


def test_generator_identity_translates_unpaired_surrogate() -> None:
    with pytest.raises(ProposalError, match="unpaired Unicode surrogates"):
        GeneratorIdentity("\ud800")


def test_file_wrapper_reads_exact_inputs_and_never_overwrites(
    reviewed_document: bytes,
    tmp_path: Path,
) -> None:
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    output_file = tmp_path / "proposals.json"
    reviewed_file.write_bytes(reviewed_document)
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    response_document = json.dumps(
        {"schema_version": 1, "proposals": [raw]},
        sort_keys=True,
    ).encode("utf-8")
    response_file.write_bytes(response_document)

    result = write_test_proposal_artifact(
        reviewed_file,
        response_file,
        output_file,
        generator=GeneratorIdentity("file-adapter", "model"),
        expected_reviewed_requirements_sha256=hashlib.sha256(
            reviewed_document
        ).hexdigest(),
        expected_generator_response_sha256=hashlib.sha256(
            response_document
        ).hexdigest(),
    )

    assert result.output_file == output_file
    assert result.proposal_count == 1
    assert result.unproposed_requirement_count == 1
    encoded = json.loads(output_file.read_bytes())
    assert encoded["generator_response"] == {
        "binding": "exact",
        "sha256": hashlib.sha256(response_document).hexdigest(),
        "canonical_sha256": encoded["generator_response"]["canonical_sha256"],
    }
    loaded = load_test_proposal_artifact(
        output_file.read_bytes(),
        reviewed_requirements_document=reviewed_document,
        generator_response_document=response_document,
    )
    assert loaded.generator_adapter == "file-adapter"
    with pytest.raises(ProposalError, match="already exists"):
        write_test_proposal_artifact(
            reviewed_file,
            response_file,
            output_file,
            generator=GeneratorIdentity("file-adapter", "model"),
        )


def test_file_wrapper_rejects_stale_input_digest_before_write(
    reviewed_document: bytes,
    tmp_path: Path,
) -> None:
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    output_file = tmp_path / "proposals.json"
    reviewed_file.write_bytes(reviewed_document)
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    response_file.write_text(
        json.dumps({"schema_version": 1, "proposals": [raw]}),
        encoding="utf-8",
    )

    with pytest.raises(ProposalError, match="earlier analysis pass"):
        write_test_proposal_artifact(
            reviewed_file,
            response_file,
            output_file,
            generator=GeneratorIdentity("file-adapter"),
            expected_generator_response_sha256="0" * 64,
        )

    assert not output_file.exists()


def test_exact_response_binding_rejects_different_response_bytes(
    reviewed_document: bytes,
) -> None:
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    response = json.dumps(
        {"schema_version": 1, "proposals": [raw]},
        sort_keys=True,
    ).encode("utf-8")
    artifact = build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="file-adapter",
        generator_model=None,
        proposals=parse_test_proposal_response(response),
        generator_response_document=response,
    )
    reserialized = json.dumps(json.loads(response), indent=2).encode("utf-8")

    with pytest.raises(ProposalError, match="exact supplied generator response"):
        validate_test_proposal_artifact(
            artifact,
            reviewed_requirements_document=reviewed_document,
            generator_response_document=reserialized,
        )


def test_exact_response_binding_requires_original_bytes_for_validation(
    reviewed_document: bytes,
) -> None:
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    response = json.dumps(
        {"schema_version": 1, "proposals": [raw]},
        sort_keys=True,
    ).encode("utf-8")
    artifact = build_test_proposal_artifact(
        reviewed_document,
        generator_adapter="file-adapter",
        generator_model=None,
        proposals=parse_test_proposal_response(response),
        generator_response_document=response,
    )
    artifact["generator_response"]["sha256"] = "0" * 64

    with pytest.raises(ProposalError, match="requires the original"):
        validate_test_proposal_artifact(
            artifact,
            reviewed_requirements_document=reviewed_document,
        )
    with pytest.raises(ProposalError, match="exact supplied generator response"):
        validate_test_proposal_artifact(
            artifact,
            reviewed_requirements_document=reviewed_document,
            generator_response_document=response,
        )


def test_file_wrapper_translates_escaped_surrogate_json(
    reviewed_document: bytes,
    tmp_path: Path,
) -> None:
    reviewed_file = tmp_path / "reviewed.json"
    response_file = tmp_path / "response.json"
    output_file = tmp_path / "proposals.json"
    reviewed_file.write_bytes(reviewed_document)
    raw = _raw_proposal(_requirement_ids(reviewed_document)[0])
    raw["source"] = "\ud800"
    response_file.write_text(
        json.dumps({"schema_version": 1, "proposals": [raw]}),
        encoding="utf-8",
    )

    with pytest.raises(ProposalError, match="unpaired Unicode surrogates"):
        write_test_proposal_artifact(
            reviewed_file,
            response_file,
            output_file,
            generator=GeneratorIdentity("file-adapter"),
        )

    assert not output_file.exists()


def test_load_rejects_duplicate_keys_before_schema_validation(
    reviewed_document: bytes,
) -> None:
    artifact = _artifact(reviewed_document)
    document = encode_test_proposal_artifact(artifact)
    ambiguous = document.replace(
        b'"artifact_type": "test-proposals",',
        b'"artifact_type": "test-proposals",\n  "artifact_type": "test-proposals",',
        1,
    )

    with pytest.raises(ProposalError, match="duplicate JSON object key"):
        load_test_proposal_artifact(
            ambiguous,
            reviewed_requirements_document=reviewed_document,
        )
