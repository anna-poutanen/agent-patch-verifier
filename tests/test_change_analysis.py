import ast
from pathlib import Path
import sys

import pytest

import patch_verifier.change_analysis as change_analysis
from patch_verifier.change_analysis import (
    DiffParseError,
    analyze_changed_symbols,
    parse_unified_diff,
)


SAMPLE_DIFF = """\
diff --git a/example.py b/example.py
index 1111111..2222222 100644
--- a/example.py
+++ b/example.py
@@ -2,2 +2,2 @@ def resolve(value):
-    return value or 5
+    return 0
 
diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1 +1 @@
-old
+new
"""


def test_parse_unified_diff_tracks_both_revisions() -> None:
    changes = parse_unified_diff(SAMPLE_DIFF)

    assert len(changes) == 2
    assert changes[0].old_path == "example.py"
    assert changes[0].new_path == "example.py"
    assert changes[0].old_lines == frozenset({2})
    assert changes[0].new_lines == frozenset({2})
    assert changes[1].is_python is False


def test_parse_rejects_changed_line_coordinate_amplification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_analysis, "_MAX_CHANGED_LINES", 1)
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-before = 1
+after = 1
"""

    with pytest.raises(DiffParseError, match="changed line coordinates"):
        parse_unified_diff(diff)


def test_parse_rejects_too_many_file_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_analysis, "_MAX_DIFF_FILES", 1)
    diff = """\
diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
diff --git a/two.py b/two.py
--- a/two.py
+++ b/two.py
"""

    with pytest.raises(DiffParseError, match="file entries"):
        parse_unified_diff(diff)


def test_analyze_changed_symbols_uses_innermost_callable(tmp_path: Path) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text(
        "def resolve(value):\n    return value or 5\n\n", encoding="utf-8"
    )
    (patched / "example.py").write_text(
        "def resolve(value):\n    return 0\n\n", encoding="utf-8"
    )

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(SAMPLE_DIFF))

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "resolve"
    assert symbols[0].kind == "function"
    assert symbols[0].removed_lines == (2,)
    assert symbols[0].added_lines == (2,)


def test_python_renamed_to_non_python_still_analyzes_removed_source(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "old.py").write_text(
        "def resolve(value):\n    return value or 5\n", encoding="utf-8"
    )
    (patched / "old.txt").write_text(
        "def resolve(value):\n    return 0\n", encoding="utf-8"
    )
    diff = """\
diff --git a/old.py b/old.txt
similarity index 60%
rename from old.py
rename to old.txt
--- a/old.py
+++ b/old.txt
@@ -1,2 +1,2 @@
 def resolve(value):
-    return value or 5
+    return 0
"""

    change = parse_unified_diff(diff)[0]
    symbols = analyze_changed_symbols(base, patched, (change,))

    assert change.is_python is True
    assert change.old_is_python is True
    assert change.new_is_python is False
    assert len(symbols) == 1
    assert symbols[0].path == "old.py"
    assert symbols[0].qualified_name == "resolve"
    assert symbols[0].base_span is not None
    assert symbols[0].base_span.start_line == 1
    assert symbols[0].patched_span is None
    assert symbols[0].removed_lines == (2,)
    assert symbols[0].added_lines == ()
    assert symbols[0].base_path == "old.py"
    assert symbols[0].patched_path is None
    assert symbols[0].revision_match == "base_only"


def test_decorator_lines_are_part_of_definition_span(tmp_path: Path) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text(
        "def deco(fn):\n"
        "    return fn\n\n"
        "@deco\n"
        "def resolve():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (patched / "example.py").write_text(
        "def deco(fn):\n"
        "    return fn\n\n"
        "@other\n"
        "def resolve():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -4 +4 @@
-@deco
+@other
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "resolve"
    assert symbols[0].base_span is not None
    assert symbols[0].base_span.start_line == 4
    assert symbols[0].patched_span is not None
    assert symbols[0].patched_span.start_line == 4
    assert symbols[0].removed_lines == (4,)
    assert symbols[0].added_lines == (4,)


def test_duplicate_definitions_are_occurrence_aware_and_not_paired(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text(
        "def repeated():\n"
        "    return 1\n\n"
        "def repeated():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    (patched / "example.py").write_text(
        "def repeated():\n"
        "    return 1\n\n"
        "def repeated():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -5 +5 @@
-    return 2
+    return 3
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 2
    removed, added = symbols
    assert removed.qualified_name == added.qualified_name == "repeated"
    assert removed.base_occurrence == 2
    assert removed.patched_occurrence is None
    assert removed.base_span is not None
    assert removed.base_span.start_line == 4
    assert removed.patched_span is None
    assert removed.removed_lines == (5,)
    assert removed.added_lines == ()
    assert added.base_occurrence is None
    assert added.patched_occurrence == 2
    assert added.base_span is None
    assert added.patched_span is not None
    assert added.patched_span.start_line == 4
    assert added.removed_lines == ()
    assert added.added_lines == (5,)
    assert removed.revision_match == "ambiguous_duplicate_unpaired"
    assert added.revision_match == "ambiguous_duplicate_unpaired"
    assert removed.to_dict()["base_occurrence"] == 2


def test_parse_rejects_parent_traversal() -> None:
    unsafe = """\
diff --git a/../outside.py b/../outside.py
--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""

    with pytest.raises(DiffParseError, match="unsafe"):
        parse_unified_diff(unsafe)


def test_parse_new_file() -> None:
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+def added():
+    return 1
"""

    change = parse_unified_diff(diff)[0]
    assert change.old_path is None
    assert change.new_path == "new.py"
    assert change.new_lines == frozenset({1, 2})


def test_hunk_content_resembling_file_headers_is_not_parsed_as_a_path() -> None:
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,2 +1,2 @@
--- old marker
+++ new marker
 unchanged
"""

    change = parse_unified_diff(diff)[0]

    assert change.old_path == "example.py"
    assert change.new_path == "example.py"
    assert change.old_lines == frozenset({1})
    assert change.new_lines == frozenset({1})


@pytest.mark.parametrize(
    ("header_path", "expected"),
    [
        ("a/space name.py", "space name.py"),
        ('"a/tab\\tname.py"', "tab\tname.py"),
        ('"a/caf\\303\\251.py"', "café.py"),
    ],
)
def test_parse_git_paths_with_spaces_and_c_quoting(
    header_path: str, expected: str
) -> None:
    diff = f"""\
diff --git ignored ignored
--- {header_path}
+++ {header_path.replace('a/', 'b/', 1)}
@@ -1 +1 @@
-old
+new
"""

    change = parse_unified_diff(diff)[0]

    assert change.old_path == expected
    assert change.new_path == expected


def test_parse_pure_rename_and_copy_metadata() -> None:
    diff = """\
diff --git "a/caf\\303\\251 old.py" "b/caf\\303\\251 new.py"
similarity index 100%
rename from "caf\\303\\251 old.py"
rename to "caf\\303\\251 new.py"
diff --git a/a/source.py b/b/copied.py
similarity index 100%
copy from a/source.py
copy to b/copied.py
"""

    rename, copy = parse_unified_diff(diff)

    assert rename.old_path == "café old.py"
    assert rename.new_path == "café new.py"
    assert rename.old_lines == frozenset()
    assert rename.new_lines == frozenset()
    # Rename/copy metadata does not use Git's synthetic a/ and b/ prefixes.
    assert copy.old_path == "a/source.py"
    assert copy.new_path == "b/copied.py"


@pytest.mark.parametrize("operation", ["rename", "copy"])
def test_path_only_python_change_exposes_definition_scope(
    tmp_path: Path, operation: str
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    source = "def resolve(value):\n    return value or 5\n"
    (base / "old.py").write_text(source, encoding="utf-8")
    (patched / "new.py").write_text(source, encoding="utf-8")
    diff = f"""\
diff --git a/old.py b/new.py
similarity index 100%
{operation} from old.py
{operation} to new.py
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    symbol = symbols[0]
    assert symbol.path == "new.py"
    assert symbol.qualified_name == "resolve"
    assert symbol.kind == "function"
    assert symbol.base_path == "old.py"
    assert symbol.patched_path == "new.py"
    assert symbol.base_span is not None
    assert symbol.patched_span is not None
    assert symbol.removed_lines == ()
    assert symbol.added_lines == ()
    assert symbol.revision_match == "path_change_unique_identity"


def test_rename_with_edited_hunk_also_exposes_untouched_definitions(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "old.py").write_text(
        "def changed():\n    return 1\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    (patched / "new.py").write_text(
        "def changed():\n    return 3\n\ndef untouched():\n    return 2\n",
        encoding="utf-8",
    )
    diff = """\
diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -2 +2 @@ def changed():
-    return 1
+    return 3
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    by_name = {symbol.qualified_name: symbol for symbol in symbols}
    assert set(by_name) == {"changed", "untouched"}
    assert by_name["changed"].revision_match == "unique_identity"
    assert by_name["changed"].removed_lines == (2,)
    assert by_name["changed"].added_lines == (2,)
    assert by_name["untouched"].revision_match == "path_change_unique_identity"
    assert by_name["untouched"].base_path == "old.py"
    assert by_name["untouched"].patched_path == "new.py"


def test_parse_recursion_error_falls_back_to_module_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text("before = 1\n", encoding="utf-8")
    (patched / "example.py").write_text("after = 1\n", encoding="utf-8")

    def fail_parse(*args: object, **kwargs: object) -> ast.AST:
        raise RecursionError("deep AST construction")

    monkeypatch.setattr(change_analysis.ast, "parse", fail_parse)
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-before = 1
+after = 1
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "<module>"
    assert symbols[0].removed_lines == (1,)
    assert symbols[0].added_lines == (1,)
    assert symbols[0].revision_match == "module"


def test_deep_ast_traversal_falls_back_to_module_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text("before = 1\n", encoding="utf-8")
    (patched / "example.py").write_text("after = 1\n", encoding="utf-8")

    node: ast.stmt = ast.Pass()
    for _ in range(sys.getrecursionlimit() + 100):
        node = ast.If(test=ast.Constant(value=True), body=[node], orelse=[])
    deep_tree = ast.Module(body=[node], type_ignores=[])
    monkeypatch.setattr(
        change_analysis.ast, "parse", lambda *args, **kwargs: deep_tree
    )
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-before = 1
+after = 1
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "<module>"
    assert symbols[0].removed_lines == (1,)
    assert symbols[0].added_lines == (1,)
    assert symbols[0].revision_match == "module"


def test_oversized_source_falls_back_to_module_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    patched = tmp_path / "patched"
    base.mkdir()
    patched.mkdir()
    (base / "example.py").write_text("before = 1\n", encoding="utf-8")
    (patched / "example.py").write_text("after = 1\n", encoding="utf-8")
    monkeypatch.setattr(change_analysis, "_MAX_SOURCE_BYTES", 1)
    diff = """\
diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-before = 1
+after = 1
"""

    symbols = analyze_changed_symbols(base, patched, parse_unified_diff(diff))

    assert len(symbols) == 1
    assert symbols[0].qualified_name == "<module>"
    assert symbols[0].revision_match == "module"
