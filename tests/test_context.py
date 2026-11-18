import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import patch_verifier.context as context_module
from patch_verifier.change_analysis import (
    ChangedSymbol,
    SourceSpan,
    analyze_changed_symbols,
    parse_unified_diff,
)
from patch_verifier.context import (
    ContextKind,
    ContextRevision,
    collect_repository_context,
)


def changed_symbol(
    path: str = "src/pkg/service.py",
    name: str = "resolve",
    *,
    span: SourceSpan | None = None,
) -> ChangedSymbol:
    return ChangedSymbol(
        path=path,
        qualified_name=name,
        kind="function",
        base_span=span,
        patched_span=span,
        removed_lines=(),
        added_lines=(),
    )


def write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_collects_ranked_context_for_src_layout(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/pkg/service.py",
        """\
import math
from .helpers import normalize

def resolve(value: int | None, default: int = 5) -> int:
    \"\"\"Return the default only when value is missing.\"\"\"
    if value is None:
        return default
    return normalize(value) + math.floor(0)
""",
    )
    write(tmp_path, "src/pkg/helpers.py", "def normalize(value):\n    return value\n")
    write(
        tmp_path,
        "src/pkg/api.py",
        """\
from pkg.service import resolve as pick

def endpoint():
    return pick(3)
""",
    )
    write(
        tmp_path,
        "tests/test_service.py",
        """\
from pkg.service import resolve

def test_resolve_zero():
    assert resolve(0) == 0

def test_unrelated():
    assert True
""",
    )

    result = collect_repository_context(tmp_path, [changed_symbol()])

    assert len(result.symbols) == 1
    context = result.symbols[0]
    assert {item.kind for item in context.items} >= {
        ContextKind.SIGNATURE,
        ContextKind.DOCSTRING,
        ContextKind.IMPORT,
        ContextKind.CALLER,
        ContextKind.NEARBY_TEST,
    }
    assert any(
        item.path == "src/pkg/api.py" and item.symbol == "endpoint"
        for item in context.items
    )
    assert any(
        item.path == "tests/test_service.py"
        and item.symbol == "test_resolve_zero"
        for item in context.items
    )
    assert [item.rank for item in context.items] == list(
        range(1, len(context.items) + 1)
    )
    assert all(item.selection_reasons for item in context.items)
    assert all(
        item.score == sum(reason.weight for reason in item.selection_reasons)
        for item in context.items
    )
    signature = next(
        item for item in context.items if item.kind is ContextKind.SIGNATURE
    )
    assert signature.excerpt == (
        "def resolve(value: int | None, default: int=5) -> int"
    )
    assert "Dynamic imports" in " ".join(result.limitations)
    # No Path, Enum, or dataclass instances leak through the serialization API.
    serialized = json.loads(json.dumps(result.to_dict()))
    assert serialized["analysis"] == "static"
    assert serialized["schema_version"] == 1
    assert serialized["revision"] == "patched"


def test_result_models_are_immutable(tmp_path: Path) -> None:
    write(tmp_path, "module.py", "def target():\n    return 1\n")
    result = collect_repository_context(
        tmp_path, [changed_symbol("module.py", "target")]
    )

    with pytest.raises(FrozenInstanceError):
        result.schema_version = "2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.symbols[0].items[0].rank = 99  # type: ignore[misc]


def test_reports_syntax_errors_and_skips_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def leaked():\n    return 'outside'\n", encoding="utf-8")
    write(tmp_path, "bad.py", "def broken(:\n    pass\n")
    (tmp_path / "linked.py").symlink_to(outside)

    result = collect_repository_context(
        tmp_path,
        [
            changed_symbol("bad.py", "broken"),
            changed_symbol("linked.py", "leaked"),
            changed_symbol("../outside.py", "leaked"),
        ],
    )

    assert {(item.code, item.path) for item in result.diagnostics} >= {
        ("syntax_error", "bad.py"),
        ("symlink_skipped", "linked.py"),
    }
    by_path = {item.path: item for item in result.symbols}
    assert by_path["bad.py"].items == ()
    assert "could not be parsed" in " ".join(by_path["bad.py"].limitations)
    assert by_path["linked.py"].items == ()
    assert "could not be parsed safely" in " ".join(
        by_path["linked.py"].limitations
    )
    assert by_path["../outside.py"].items == ()
    assert "safe repository-relative" in " ".join(
        by_path["../outside.py"].limitations
    )


def test_duplicate_definition_uses_span_but_does_not_claim_callers(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "src/pkg/service.py",
        """\
def resolve(value):
    return value

def resolve(value):
    return 0
""",
    )
    write(
        tmp_path,
        "src/pkg/api.py",
        """\
from pkg.service import resolve

def endpoint():
    return resolve(3)
""",
    )

    result = collect_repository_context(
        tmp_path,
        [changed_symbol(span=SourceSpan(start_line=4, end_line=5))],
    )

    context = result.symbols[0]
    assert any(item.kind is ContextKind.SIGNATURE for item in context.items)
    assert not any(item.kind is ContextKind.CALLER for item in context.items)
    assert "runtime binding and callers are ambiguous" in " ".join(
        context.limitations
    )


def test_ambiguous_src_and_flat_modules_suppress_external_callers(
    tmp_path: Path,
) -> None:
    write(tmp_path, "src/pkg/service.py", "def resolve(value):\n    return value\n")
    write(tmp_path, "pkg/service.py", "def resolve(value):\n    return 0\n")
    write(
        tmp_path,
        "consumer.py",
        "from pkg.service import resolve\n\ndef use():\n    return resolve(1)\n",
    )

    result = collect_repository_context(tmp_path, [changed_symbol()])

    context = result.symbols[0]
    assert not any(
        item.kind is ContextKind.CALLER and item.path == "consumer.py"
        for item in context.items
    )
    assert "multiple repository paths" in " ".join(context.limitations)


def test_unimported_same_name_is_not_a_likely_caller(tmp_path: Path) -> None:
    write(tmp_path, "src/pkg/service.py", "def resolve(value):\n    return value\n")
    write(
        tmp_path,
        "other.py",
        "def resolve():\n    return 1\n\ndef use():\n    return resolve()\n",
    )

    result = collect_repository_context(tmp_path, [changed_symbol()])

    assert not any(
        item.kind is ContextKind.CALLER and item.path == "other.py"
        for item in result.symbols[0].items
    )


@pytest.mark.parametrize(
    "caller_source",
    [
        """\
from pkg.service import resolve

def use(resolve):
    return resolve()
""",
        """\
from pkg.service import resolve

def use():
    result = resolve()
    resolve = lambda: 0
    return result
""",
        """\
from pkg.service import resolve
resolve = lambda: 0

def use():
    return resolve()
""",
        """\
def configure():
    from pkg.service import resolve
    return resolve

def use():
    return resolve()
""",
    ],
)
def test_lexical_shadowing_suppresses_imported_caller(
    tmp_path: Path, caller_source: str
) -> None:
    write(tmp_path, "src/pkg/service.py", "def resolve():\n    return 5\n")
    write(tmp_path, "caller.py", caller_source)

    result = collect_repository_context(tmp_path, [changed_symbol()])

    assert not any(
        item.kind is ContextKind.CALLER and item.path == "caller.py"
        for item in result.symbols[0].items
    )


def test_lexical_shadowing_suppresses_same_module_caller(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/pkg/service.py",
        """\
def resolve():
    return 5

def use(resolve):
    return resolve()
""",
    )

    result = collect_repository_context(tmp_path, [changed_symbol()])

    assert not any(
        item.kind is ContextKind.CALLER and item.symbol == "use"
        for item in result.symbols[0].items
    )


def test_method_call_requires_safe_qualification(tmp_path: Path) -> None:
    write(
        tmp_path,
        "src/pkg/service.py",
        """\
class Widget:
    def target(self):
        return 5

    def ambiguous(self):
        return target()
""",
    )
    write(
        tmp_path,
        "caller.py",
        """\
from pkg.service import Widget

def qualified():
    return Widget.target(None)

def ambiguous():
    return target()
""",
    )

    result = collect_repository_context(
        tmp_path, [changed_symbol(name="Widget.target")]
    )

    callers = {
        (item.path, item.symbol)
        for item in result.symbols[0].items
        if item.kind is ContextKind.CALLER
    }
    assert ("caller.py", "qualified") in callers
    assert ("caller.py", "ambiguous") not in callers
    assert ("src/pkg/service.py", "Widget.ambiguous") not in callers


def test_absent_target_definition_never_produces_callers(tmp_path: Path) -> None:
    write(
        tmp_path,
        "caller.py",
        "from missing import target\n\ndef use():\n    return target()\n",
    )

    result = collect_repository_context(
        tmp_path, [changed_symbol("missing.py", "target")]
    )

    assert not any(
        item.kind is ContextKind.CALLER for item in result.symbols[0].items
    )
    assert "definition context is unavailable" in " ".join(
        result.symbols[0].limitations
    )


@pytest.mark.parametrize(
    "caller_source",
    [
        """\
from pkg.service import resolve
use = lambda resolve: resolve()
""",
        """\
from pkg.service import resolve

def use(value):
    match value:
        case {"call": resolve}:
            return resolve()
    return None
""",
    ],
)
def test_lambda_and_pattern_bindings_suppress_callers(
    tmp_path: Path, caller_source: str
) -> None:
    write(tmp_path, "src/pkg/service.py", "def resolve():\n    return 5\n")
    write(tmp_path, "caller.py", caller_source)

    result = collect_repository_context(tmp_path, [changed_symbol()])

    assert not any(
        item.kind is ContextKind.CALLER and item.path == "caller.py"
        for item in result.symbols[0].items
    )


def test_self_call_resolves_only_against_nearest_enclosing_class(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        "src/pkg/service.py",
        """\
class Outer:
    def target(self):
        return 5

    def valid(self):
        return self.target()

    @classmethod
    def valid_cls(cls):
        return cls.target(cls)

    class Inner:
        def invalid(self):
            return self.target()

        @classmethod
        def invalid_cls(cls):
            return cls.target(cls)
""",
    )

    result = collect_repository_context(
        tmp_path, [changed_symbol(name="Outer.target")]
    )

    callers = {
        item.symbol
        for item in result.symbols[0].items
        if item.kind is ContextKind.CALLER
    }
    assert "Outer.valid" in callers
    assert "Outer.valid_cls" in callers
    assert "Outer.Inner.invalid" not in callers
    assert "Outer.Inner.invalid_cls" not in callers


@pytest.mark.parametrize("failure_stage", ["parse", "traversal"])
def test_recursion_errors_become_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    write(tmp_path, "module.py", "def target():\n    return 1\n")

    def exceed_depth(*args: object, **kwargs: object) -> None:
        raise RecursionError

    if failure_stage == "parse":
        monkeypatch.setattr(context_module.ast, "parse", exceed_depth)
    else:
        monkeypatch.setattr(context_module, "_analyze_tree", exceed_depth)

    result = collect_repository_context(
        tmp_path, [changed_symbol("module.py", "target")]
    )

    assert result.symbols[0].items == ()
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.code == "analysis_depth_exceeded"
    )
    assert diagnostic.path == "module.py"


def test_base_revision_nearby_test_uses_base_rename_stem(tmp_path: Path) -> None:
    write(tmp_path, "old_name.py", "def resolve():\n    return 5\n")
    write(tmp_path, "tests/test_old_name.py", "def test_behavior():\n    pass\n")
    write(tmp_path, "tests/test_new_name.py", "def test_behavior():\n    pass\n")
    symbol = ChangedSymbol(
        path="new_name.py",
        qualified_name="resolve",
        kind="function",
        base_span=SourceSpan(1, 2),
        patched_span=SourceSpan(1, 2),
        removed_lines=(2,),
        added_lines=(2,),
        base_path="old_name.py",
        patched_path="new_name.py",
        base_occurrence=1,
        patched_occurrence=1,
        revision_match="unique_identity",
    )

    result = collect_repository_context(
        tmp_path, [symbol], revision=ContextRevision.BASE
    )

    nearby_paths = {
        item.path
        for item in result.symbols[0].items
        if item.kind is ContextKind.NEARBY_TEST
    }
    assert "tests/test_old_name.py" in nearby_paths
    assert "tests/test_new_name.py" not in nearby_paths


def test_output_order_is_independent_of_input_order(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    write(tmp_path, "b.py", "def beta():\n    return 2\n")
    alpha = changed_symbol("a.py", "alpha")
    beta = changed_symbol("b.py", "beta")

    first = collect_repository_context(tmp_path, [beta, alpha])
    second = collect_repository_context(tmp_path, [alpha, beta])

    assert first.to_dict() == second.to_dict()
    assert [item.path for item in first.symbols] == ["a.py", "b.py"]


def test_side_specific_identity_selects_duplicate_occurrences(tmp_path: Path) -> None:
    write(
        tmp_path,
        "new.py",
        "def target():\n    return 1\n\ndef target():\n    return 2\n",
    )
    symbols = [
        ChangedSymbol(
            path="new.py",
            qualified_name="target",
            kind="function",
            base_span=None,
            patched_span=SourceSpan(1, 2),
            removed_lines=(),
            added_lines=(2,),
            base_path=None,
            patched_path="new.py",
            base_occurrence=None,
            patched_occurrence=1,
            revision_match="ambiguous_duplicate_unpaired",
        ),
        ChangedSymbol(
            path="new.py",
            qualified_name="target",
            kind="function",
            base_span=None,
            patched_span=SourceSpan(4, 5),
            removed_lines=(),
            added_lines=(5,),
            base_path=None,
            patched_path="new.py",
            base_occurrence=None,
            patched_occurrence=2,
            revision_match="ambiguous_duplicate_unpaired",
        ),
    ]

    result = collect_repository_context(tmp_path, reversed(symbols))

    assert [item.patched_occurrence for item in result.symbols] == [1, 2]
    assert [
        next(
            context_item.start_line
            for context_item in item.items
            if context_item.kind is ContextKind.SIGNATURE
        )
        for item in result.symbols
    ] == [1, 4]
    assert all(
        item.revision_match == "ambiguous_duplicate_unpaired"
        for item in result.symbols
    )
    serialized = result.to_dict()["symbols"]
    assert [item["patched_occurrence"] for item in serialized] == [1, 2]


def test_patched_only_duplicate_cannot_inherit_base_definition_context(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    write(base, "module.py", 'def target():\n    """BASE ONE"""\n    return 1\n')
    write(
        patched,
        "module.py",
        'def target():\n'
        '    """BASE ONE"""\n'
        '    return 1\n'
        'def target():\n'
        '    """PATCHED TWO"""\n'
        '    return 2\n',
    )
    diff = """\
diff --git a/module.py b/module.py
--- a/module.py
+++ b/module.py
@@ -3,0 +4,3 @@ def target():
+def target():
+    \"\"\"PATCHED TWO\"\"\"
+    return 2
"""
    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    assert symbols[0].base_path is None
    assert symbols[0].patched_occurrence == 2

    base_context = collect_repository_context(
        base, symbols, revision=ContextRevision.BASE
    )
    patched_context = collect_repository_context(
        patched, symbols, revision=ContextRevision.PATCHED
    )

    assert base_context.symbols[0].items == ()
    patched_excerpts = [item.excerpt for item in patched_context.symbols[0].items]
    assert "PATCHED TWO" in patched_excerpts
    assert "BASE ONE" not in patched_excerpts


def test_revision_selects_the_corresponding_renamed_path(tmp_path: Path) -> None:
    write(tmp_path, "old.py", "def target():\n    return 'base'\n")
    symbol = ChangedSymbol(
        path="new.py",
        qualified_name="target",
        kind="function",
        base_span=SourceSpan(1, 2),
        patched_span=SourceSpan(1, 2),
        removed_lines=(2,),
        added_lines=(2,),
        base_path="old.py",
        patched_path="new.py",
        base_occurrence=1,
        patched_occurrence=1,
        revision_match="unique_identity",
    )

    result = collect_repository_context(
        tmp_path, [symbol], revision=ContextRevision.BASE
    )

    assert result.revision is ContextRevision.BASE
    assert result.symbols[0].path == "new.py"
    assert result.symbols[0].analyzed_path == "old.py"
    assert result.symbols[0].items[0].path == "old.py"


def test_total_file_limit_is_deterministic_and_diagnosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def first():\n    return 1\n")
    write(tmp_path, "b.py", "def second():\n    return 2\n")
    write(tmp_path, "z.py", "def target():\n    return 3\n")
    monkeypatch.setattr(context_module, "_MAX_PYTHON_FILES", 2)

    result = collect_repository_context(
        tmp_path, [changed_symbol("z.py", "target")]
    )

    diagnostic = next(
        item for item in result.diagnostics if item.code == "file_limit_reached"
    )
    assert diagnostic.path == "."
    assert "skipped 1" in diagnostic.message
    assert result.symbols[0].items == ()


def test_total_source_bytes_are_bounded_and_diagnosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source = "def first():\n    return 1\n"
    write(tmp_path, "a.py", first_source)
    write(tmp_path, "b.py", "def target():\n    return 2\n")
    monkeypatch.setattr(
        context_module,
        "_MAX_TOTAL_SOURCE_BYTES",
        len(first_source.encode("utf-8")),
    )

    result = collect_repository_context(
        tmp_path, [changed_symbol("b.py", "target")]
    )

    assert result.symbols[0].items == ()
    assert any(
        item.code == "source_bytes_limit_reached"
        for item in result.diagnostics
    )


def test_candidate_pool_is_bounded_before_final_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "service.py", "def target():\n    return 1\n")
    tests = "\n\n".join(
        f"def test_behavior_{index}():\n    pass" for index in range(10)
    )
    write(tmp_path, "tests/test_service.py", tests + "\n")
    monkeypatch.setattr(context_module, "_MAX_ITEMS_PER_SYMBOL", 3)

    result = collect_repository_context(
        tmp_path, [changed_symbol("service.py", "target")]
    )

    context = result.symbols[0]
    assert len(context.items) == 3
    assert "truncated to the 3 highest-ranked items" in " ".join(
        context.limitations
    )


def test_changed_symbol_work_is_globally_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    write(tmp_path, "b.py", "def beta():\n    return 2\n")
    monkeypatch.setattr(context_module, "_MAX_CHANGED_SYMBOLS", 1)

    result = collect_repository_context(
        tmp_path,
        [changed_symbol("b.py", "beta"), changed_symbol("a.py", "alpha")],
    )

    assert [item.qualified_name for item in result.symbols] == ["alpha"]
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.code == "changed_symbol_limit_reached"
    )
    assert "skipped 1" in diagnostic.message


def test_total_ranked_context_items_are_globally_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    write(tmp_path, "b.py", "def beta():\n    return 2\n")
    monkeypatch.setattr(context_module, "_MAX_TOTAL_CONTEXT_ITEMS", 1)

    result = collect_repository_context(
        tmp_path,
        [changed_symbol("b.py", "beta"), changed_symbol("a.py", "alpha")],
    )

    assert [item.qualified_name for item in result.symbols] == ["alpha"]
    assert sum(len(item.items) for item in result.symbols) == 1
    diagnostic = next(
        item
        for item in result.diagnostics
        if item.code == "context_item_limit_reached"
    )
    assert "skipped 1" in diagnostic.message


def test_candidate_cap_ranks_after_all_reasons_are_aggregated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "service.py", "def target():\n    return 1\n")
    write(
        tmp_path,
        "tests/test_service.py",
        "from service import target\n\n"
        "def test_target():\n"
        "    assert target() == 1\n",
    )
    monkeypatch.setattr(context_module, "_MAX_ITEMS_PER_SYMBOL", 1)

    result = collect_repository_context(
        tmp_path, [changed_symbol("service.py", "target")]
    )

    assert len(result.symbols[0].items) == 1
    selected = result.symbols[0].items[0]
    assert selected.kind is ContextKind.NEARBY_TEST
    assert selected.score == 135
    assert {reason.code for reason in selected.selection_reasons} == {
        "test_calls_changed_symbol",
        "test_name_mentions_symbol",
    }


def test_symbol_cap_tie_breaking_is_independent_of_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        "module.py",
        "def target(a):\n    return a\n\n"
        "def target(b, c):\n    return b + c\n",
    )
    first = changed_symbol(
        "module.py", "target", span=SourceSpan(start_line=1, end_line=2)
    )
    second = changed_symbol(
        "module.py", "target", span=SourceSpan(start_line=4, end_line=5)
    )
    monkeypatch.setattr(context_module, "_MAX_CHANGED_SYMBOLS", 1)

    forward = collect_repository_context(tmp_path, [first, second])
    reverse = collect_repository_context(tmp_path, [second, first])

    assert forward.to_dict() == reverse.to_dict()
    signature = next(
        item
        for item in forward.symbols[0].items
        if item.kind is ContextKind.SIGNATURE
    )
    assert signature.excerpt == "def target(a)"


def test_changed_symbol_input_has_a_hard_safety_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    monkeypatch.setattr(context_module, "_MAX_CHANGED_SYMBOL_INPUTS", 1)

    with pytest.raises(ValueError, match="1-symbol safety limit"):
        collect_repository_context(
            tmp_path,
            [changed_symbol("a.py", "alpha"), changed_symbol("a.py", "alpha")],
        )


def test_repository_discovery_has_a_hard_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    write(tmp_path, "b.py", "def beta():\n    return 2\n")
    monkeypatch.setattr(context_module, "_MAX_DISCOVERED_ENTRIES", 1)

    result = collect_repository_context(
        tmp_path, [changed_symbol("b.py", "beta")]
    )

    assert result.symbols[0].items == ()
    assert any(
        item.code == "directory_entry_limit_reached"
        for item in result.diagnostics
    )


def test_repository_diagnostics_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "a.py", "def broken(:\n    pass\n")
    write(tmp_path, "b.py", "def also_broken(:\n    pass\n")
    monkeypatch.setattr(context_module, "_MAX_DIAGNOSTICS", 1)

    result = collect_repository_context(
        tmp_path, [changed_symbol("a.py", "broken")]
    )

    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].code == "diagnostic_limit_reached"
