from __future__ import annotations

import json
from pathlib import Path

import pytest

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
