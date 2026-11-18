from pathlib import Path

import pytest

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
