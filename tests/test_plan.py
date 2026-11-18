from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import patch_verifier.plan as plan_module
from patch_verifier.plan import PlanError, load_evidence_plan, verify_base_sources


ISSUE = "Explicit zero must return zero. Existing non-missing values remain unchanged."


def write_plan(tmp_path: Path, requirements: list[dict], cases: list[dict]) -> Path:
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requirements": requirements,
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    return path


def explicit_requirement() -> dict:
    return {
        "id": "REQ-1",
        "statement": "Zero returns zero.",
        "kind": "requested",
        "provenance": "explicit",
        "source": "issue:1",
        "excerpt": "Explicit zero must return zero.",
    }


def padded_candidate(node: str, size: int) -> bytes:
    prefix = f"def {node}():\n    assert True\n#".encode("utf-8")
    assert len(prefix) <= size
    return prefix + (b"x" * (size - len(prefix)))


def preexisting_requirement(
    requirement_id: str, source: str, excerpt: str
) -> dict:
    return {
        "id": requirement_id,
        "statement": f"Preserve {requirement_id}.",
        "kind": "preservation",
        "provenance": "preexisting",
        "source": source,
        "excerpt": excerpt,
    }


def test_loads_requirement_linked_case(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_zero():\n    assert True\n", encoding="utf-8"
    )
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_zero",
                "rationale": "Checks the issue example.",
            }
        ],
    )

    plan = load_evidence_plan(path, issue_text=ISSUE)

    assert plan.requirements[0].id == "REQ-1"
    assert plan.cases[0].source_file == tmp_path / "candidate.py"
    assert len(plan.cases[0].source_sha256) == 64


def test_candidate_bytes_are_read_once_and_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.py"
    original_source = (
        b"def test_zero():\n    assert True\n\n"
        b"def test_one():\n    assert True\n"
    )
    candidate.write_bytes(original_source)
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_zero",
            },
            {
                "id": "CASE-2",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_one",
            },
        ],
    )
    document_bytes = path.read_bytes()
    original_bounded_read = plan_module._read_bounded_bytes
    candidate_reads = 0

    def counted_bounded_read(
        file_path: Path, *, max_bytes: int, label: str
    ) -> bytes:
        nonlocal candidate_reads
        if file_path == candidate:
            candidate_reads += 1
        return original_bounded_read(
            file_path, max_bytes=max_bytes, label=label
        )

    monkeypatch.setattr(plan_module, "_read_bounded_bytes", counted_bounded_read)
    monkeypatch.setattr(
        plan_module, "_MAX_TOTAL_CANDIDATE_BYTES", len(original_source)
    )

    plan = load_evidence_plan(
        path,
        issue_text=ISSUE,
        document_bytes=document_bytes,
    )
    candidate.write_bytes(b"def test_zero():\n    assert False\n")

    assert candidate_reads == 1
    assert plan.cases[0].source_bytes == original_source
    assert plan.cases[1].source_bytes == original_source
    assert plan.cases[0].source_sha256 == hashlib.sha256(original_source).hexdigest()


def test_rejects_unknown_requirement(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "MISSING",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="unknown requirement"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_rejects_two_requirements_mapped_to_the_same_pytest_node(
    tmp_path: Path,
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    second = explicit_requirement()
    second["id"] = "REQ-2"
    path = write_plan(
        tmp_path,
        [explicit_requirement(), second],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            },
            {
                "id": "CASE-2",
                "requirement_id": "REQ-2",
                "test_file": "candidate.py",
                "node": "test_x",
            },
        ],
    )

    with pytest.raises(PlanError, match="same pytest node"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_requires_exactly_one_assertion_per_candidate_node(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "def test_x():\n    assert True\n    assert True\n", encoding="utf-8"
    )
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="exactly one Python assert"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_rejects_issue_excerpt_that_is_not_present(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    requirement["excerpt"] = "A fabricated contract."
    path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="not present at issue:1"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_verifies_preexisting_excerpt_in_base_tree(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    requirement = {
        "id": "REQ-OLD",
        "statement": "Three remains three.",
        "kind": "preservation",
        "provenance": "preexisting",
        "source": "tests/test_defaults.py:2",
        "excerpt": "assert resolve(3) == 3",
    }
    plan_path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-OLD",
                "requirement_id": "REQ-OLD",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    plan = load_evidence_plan(plan_path, issue_text=ISSUE)
    base = tmp_path / "base"
    (base / "tests").mkdir(parents=True)
    (base / "tests/test_defaults.py").write_text(
        "def test_three():\n    assert resolve(3) == 3\n", encoding="utf-8"
    )

    verify_base_sources(plan, base)
    (base / "tests/test_defaults.py").write_text("", encoding="utf-8")
    with pytest.raises(PlanError, match="not present"):
        verify_base_sources(plan, base)


def test_preexisting_source_cannot_escape_base_through_symlink(
    tmp_path: Path,
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    outside = tmp_path / "outside.py"
    outside.write_text("assert resolve(3) == 3\n", encoding="utf-8")
    requirement = {
        "id": "REQ-OLD",
        "statement": "Three remains three.",
        "kind": "preservation",
        "provenance": "preexisting",
        "source": "linked.py:1",
        "excerpt": "assert resolve(3) == 3",
    }
    plan = load_evidence_plan(
        write_plan(
            tmp_path,
            [requirement],
            [
                {
                    "id": "CASE-OLD",
                    "requirement_id": "REQ-OLD",
                    "test_file": "candidate.py",
                    "node": "test_x",
                }
            ],
        ),
        issue_text=ISSUE,
    )
    base = tmp_path / "base"
    base.mkdir()
    (base / "linked.py").symlink_to(outside)

    with pytest.raises(PlanError, match="escapes the base tree"):
        verify_base_sources(plan, base)


@pytest.mark.parametrize("source", ["tests/test_defaults.py", "tests/test_defaults.py:0"])
def test_rejects_invalid_preexisting_source_coordinates(
    tmp_path: Path, source: str
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = {
        "id": "REQ-OLD",
        "statement": "Three remains three.",
        "kind": "preservation",
        "provenance": "preexisting",
        "source": source,
        "excerpt": "assert resolve(3) == 3",
    }
    plan_path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-OLD",
                "requirement_id": "REQ-OLD",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    plan = load_evidence_plan(plan_path, issue_text=ISSUE)

    with pytest.raises(PlanError, match="must cite path:<line>"):
        verify_base_sources(plan, tmp_path / "base")


def test_rejects_explicit_source_other_than_supplied_issue(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    requirement["source"] = "docs/api.md:1"
    path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="must cite issue:<line>"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_rejects_candidate_outside_plan_directory(tmp_path: Path) -> None:
    plan_directory = tmp_path / "plan"
    plan_directory.mkdir()
    (tmp_path / "outside.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    path = write_plan(
        plan_directory,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "../outside.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="inside the evidence-plan directory"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_rejects_unknown_fields_and_coerced_values(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    requirement["confidence"] = 1
    path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    with pytest.raises(PlanError, match="unknown fields"):
        load_evidence_plan(path, issue_text=ISSUE)

    requirement.pop("confidence")
    requirement["id"] = None
    path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    with pytest.raises(PlanError, match="id must be a string"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["confidence"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PlanError, match="unknown fields.*confidence"):
        load_evidence_plan(path, issue_text=ISSUE)


@pytest.mark.parametrize("target", ["requirement", "case"])
def test_rejects_ids_outside_published_pattern(
    tmp_path: Path, target: str
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    candidate = {
        "id": "CASE-1",
        "requirement_id": "REQ-1",
        "test_file": "candidate.py",
        "node": "test_x",
    }
    if target == "requirement":
        requirement["id"] = "REQ WITH SPACE"
        candidate["requirement_id"] = "REQ WITH SPACE"
    else:
        candidate["id"] = "CASE WITH SPACE"
    path = write_plan(tmp_path, [requirement], [candidate])

    with pytest.raises(PlanError, match="invalid (requirement|case) id"):
        load_evidence_plan(path, issue_text=ISSUE)


@pytest.mark.parametrize("target", ["requirement", "case"])
def test_rejects_ids_longer_than_filename_safe_limit(
    tmp_path: Path, target: str
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    candidate = {
        "id": "CASE-1",
        "requirement_id": "REQ-1",
        "test_file": "candidate.py",
        "node": "test_x",
    }
    oversized_id = "A" * 65
    if target == "requirement":
        requirement["id"] = oversized_id
        candidate["requirement_id"] = oversized_id
    else:
        candidate["id"] = oversized_id
    path = write_plan(tmp_path, [requirement], [candidate])

    with pytest.raises(PlanError, match="at most 64 characters"):
        load_evidence_plan(path, issue_text=ISSUE)


@pytest.mark.parametrize("excerpt", [None, ""])
def test_requires_nonempty_excerpt_for_strong_provenance(
    tmp_path: Path, excerpt: str | None
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = explicit_requirement()
    if excerpt is None:
        requirement.pop("excerpt")
    else:
        requirement["excerpt"] = excerpt
    path = write_plan(
        tmp_path,
        [requirement],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    with pytest.raises(PlanError, match="must include a source excerpt"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_published_plan_limits_match_runtime() -> None:
    assert plan_module._MAX_REQUIREMENTS == 200
    assert plan_module._MAX_CASES == 50
    assert plan_module._MAX_ID_LENGTH == 64
    assert plan_module._MAX_PLAN_DOCUMENT_BYTES == 5 * 1024 * 1024
    assert plan_module._MAX_CANDIDATE_FILE_BYTES == 2 * 1024 * 1024
    assert plan_module._MAX_TOTAL_CANDIDATE_BYTES == 20 * 1024 * 1024
    assert plan_module._MAX_BASE_SOURCE_FILE_BYTES == 2 * 1024 * 1024
    assert plan_module._MAX_TOTAL_BASE_SOURCE_BYTES == 20 * 1024 * 1024

    schema_path = (
        Path(__file__).parents[1]
        / "src"
        / "patch_verifier"
        / "schemas"
        / "evidence-plan.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["requirements"]["maxItems"] == 200
    assert schema["properties"]["cases"]["maxItems"] == 50


def test_accepts_maximum_requirement_and_case_counts(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        "\n".join(
            f"def test_{index}():\n    assert True" for index in range(50)
        )
        + "\n",
        encoding="utf-8",
    )
    requirements = []
    for index in range(200):
        requirement = explicit_requirement()
        requirement["id"] = f"REQ-{index}"
        requirements.append(requirement)
    cases = [
        {
            "id": f"CASE-{index}",
            "requirement_id": f"REQ-{index}",
            "test_file": "candidate.py",
            "node": f"test_{index}",
        }
        for index in range(50)
    ]

    plan = load_evidence_plan(
        write_plan(tmp_path, requirements, cases), issue_text=ISSUE
    )

    assert len(plan.requirements) == 200
    assert len(plan.cases) == 50


@pytest.mark.parametrize(
    ("requirements", "cases", "message"),
    [
        (
            [
                {**explicit_requirement(), "id": f"REQ-{index}"}
                for index in range(201)
            ],
            [
                {
                    "id": "CASE-1",
                    "requirement_id": "REQ-0",
                    "test_file": "candidate.py",
                    "node": "test_x",
                }
            ],
            "requirements must contain at most 200 entries",
        ),
        (
            [explicit_requirement()],
            [
                {
                    "id": f"CASE-{index}",
                    "requirement_id": "REQ-1",
                    "test_file": "candidate.py",
                    "node": f"test_{index}",
                }
                for index in range(51)
            ],
            "cases must contain at most 50 entries",
        ),
    ],
)
def test_rejects_plan_counts_before_candidate_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirements: list[dict],
    cases: list[dict],
    message: str,
) -> None:
    path = write_plan(tmp_path, requirements, cases)
    document_bytes = path.read_bytes()

    def unexpected_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("candidate files must not be read past a count limit")

    monkeypatch.setattr(plan_module, "_read_bounded_bytes", unexpected_read)

    with pytest.raises(PlanError, match=message):
        load_evidence_plan(
            path, issue_text=ISSUE, document_bytes=document_bytes
        )


@pytest.mark.parametrize("supplied_bytes", [False, True])
def test_plan_document_byte_limit_includes_supplied_bytes(
    tmp_path: Path, supplied_bytes: bool
) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )
    document = path.read_bytes()
    exact = document + (
        b" " * (plan_module._MAX_PLAN_DOCUMENT_BYTES - len(document))
    )

    if supplied_bytes:
        plan = load_evidence_plan(
            path, issue_text=ISSUE, document_bytes=exact
        )
    else:
        path.write_bytes(exact)
        plan = load_evidence_plan(path, issue_text=ISSUE)
    assert len(plan.cases) == 1

    oversized = exact + b" "
    if supplied_bytes:
        with pytest.raises(PlanError, match="5242880-byte limit"):
            load_evidence_plan(
                path, issue_text=ISSUE, document_bytes=oversized
            )
    else:
        path.write_bytes(oversized)
        with pytest.raises(PlanError, match="5242880-byte limit"):
            load_evidence_plan(path, issue_text=ISSUE)


def test_candidate_file_byte_limit_boundary(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.py"
    exact = padded_candidate(
        "test_x", plan_module._MAX_CANDIDATE_FILE_BYTES
    )
    candidate.write_bytes(exact)
    path = write_plan(
        tmp_path,
        [explicit_requirement()],
        [
            {
                "id": "CASE-1",
                "requirement_id": "REQ-1",
                "test_file": "candidate.py",
                "node": "test_x",
            }
        ],
    )

    plan = load_evidence_plan(path, issue_text=ISSUE)
    assert len(plan.cases[0].source_bytes) == 2 * 1024 * 1024

    candidate.write_bytes(exact + b"x")
    with pytest.raises(PlanError, match="2097152-byte limit"):
        load_evidence_plan(path, issue_text=ISSUE)


def test_candidate_aggregate_byte_limit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_module, "_MAX_CANDIDATE_FILE_BYTES", 128)
    monkeypatch.setattr(plan_module, "_MAX_TOTAL_CANDIDATE_BYTES", 256)
    for index in range(3):
        (tmp_path / f"candidate_{index}.py").write_bytes(
            padded_candidate(f"test_{index}", 128)
        )
    cases = [
        {
            "id": f"CASE-{index}",
            "requirement_id": "REQ-1",
            "test_file": f"candidate_{index}.py",
            "node": f"test_{index}",
        }
        for index in range(3)
    ]

    exact_plan = load_evidence_plan(
        write_plan(tmp_path, [explicit_requirement()], cases[:2]),
        issue_text=ISSUE,
    )
    unique_sources = {
        case.source_file: case.source_bytes for case in exact_plan.cases
    }
    assert sum(len(contents) for contents in unique_sources.values()) == 256

    with pytest.raises(PlanError, match="256-byte aggregate limit"):
        load_evidence_plan(
            write_plan(tmp_path, [explicit_requirement()], cases),
            issue_text=ISSUE,
        )


def test_base_sources_are_read_once_by_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirements = [
        preexisting_requirement("REQ-A", "contracts.txt:1", "first contract"),
        preexisting_requirement("REQ-B", "alias.txt:2", "second contract"),
    ]
    plan = load_evidence_plan(
        write_plan(
            tmp_path,
            requirements,
            [
                {
                    "id": "CASE-1",
                    "requirement_id": "REQ-A",
                    "test_file": "candidate.py",
                    "node": "test_x",
                }
            ],
        ),
        issue_text=ISSUE,
    )
    base = tmp_path / "base"
    base.mkdir()
    source = base / "contracts.txt"
    source_bytes = b"first contract\nsecond contract\n"
    source.write_bytes(source_bytes)
    (base / "alias.txt").symlink_to(source)
    original_bounded_read = plan_module._read_bounded_bytes
    source_reads = 0

    def counted_bounded_read(
        path: Path, *, max_bytes: int, label: str
    ) -> bytes:
        nonlocal source_reads
        if path == source:
            source_reads += 1
        return original_bounded_read(path, max_bytes=max_bytes, label=label)

    monkeypatch.setattr(plan_module, "_read_bounded_bytes", counted_bounded_read)
    monkeypatch.setattr(
        plan_module, "_MAX_TOTAL_BASE_SOURCE_BYTES", len(source_bytes)
    )

    verify_base_sources(plan, base)

    assert source_reads == 1


def test_base_source_file_byte_limit_boundary(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirement = preexisting_requirement(
        "REQ-A", "contract.txt:1", "contract"
    )
    plan = load_evidence_plan(
        write_plan(
            tmp_path,
            [requirement],
            [
                {
                    "id": "CASE-1",
                    "requirement_id": "REQ-A",
                    "test_file": "candidate.py",
                    "node": "test_x",
                }
            ],
        ),
        issue_text=ISSUE,
    )
    base = tmp_path / "base"
    base.mkdir()
    source = base / "contract.txt"
    prefix = b"contract\n#"
    exact = prefix + (
        b"x" * (plan_module._MAX_BASE_SOURCE_FILE_BYTES - len(prefix))
    )
    source.write_bytes(exact)

    verify_base_sources(plan, base)

    source.write_bytes(exact + b"x")
    with pytest.raises(PlanError, match="2097152-byte limit"):
        verify_base_sources(plan, base)


def test_base_source_aggregate_byte_limit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_module, "_MAX_BASE_SOURCE_FILE_BYTES", 128)
    monkeypatch.setattr(plan_module, "_MAX_TOTAL_BASE_SOURCE_BYTES", 256)
    (tmp_path / "candidate.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    requirements = [
        preexisting_requirement(
            f"REQ-{index}", f"contract_{index}.txt:1", f"contract {index}"
        )
        for index in range(3)
    ]
    cases = [
        {
            "id": "CASE-1",
            "requirement_id": "REQ-0",
            "test_file": "candidate.py",
            "node": "test_x",
        }
    ]
    base = tmp_path / "base"
    base.mkdir()
    for index in range(3):
        prefix = f"contract {index}\n#".encode("utf-8")
        (base / f"contract_{index}.txt").write_bytes(
            prefix + (b"x" * (128 - len(prefix)))
        )

    exact_plan = load_evidence_plan(
        write_plan(tmp_path, requirements[:2], cases), issue_text=ISSUE
    )
    verify_base_sources(exact_plan, base)

    over_plan = load_evidence_plan(
        write_plan(tmp_path, requirements, cases), issue_text=ISSUE
    )
    with pytest.raises(PlanError, match="256-byte aggregate limit"):
        verify_base_sources(over_plan, base)
