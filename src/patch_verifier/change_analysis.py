"""Lightweight, deterministic change analysis for Python diffs.

The analyzer deliberately reports what it can prove from line ranges.  It does
not attempt to construct a sound call graph or infer behavior.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import os
from pathlib import Path
import re


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


class DiffParseError(ValueError):
    """Raised when a unified diff cannot be parsed safely."""


@dataclass(frozen=True)
class FileChange:
    """Changed line coordinates for one file."""

    old_path: str | None
    new_path: str | None
    old_lines: frozenset[int] = field(default_factory=frozenset)
    new_lines: frozenset[int] = field(default_factory=frozenset)

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        if path is None:  # pragma: no cover - prevented by the parser
            raise DiffParseError("diff entry has neither an old nor a new path")
        return path

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")


@dataclass(frozen=True)
class SourceSpan:
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ChangedSymbol:
    """A Python symbol touched in either revision."""

    path: str
    qualified_name: str
    kind: str
    base_span: SourceSpan | None
    patched_span: SourceSpan | None
    removed_lines: tuple[int, ...]
    added_lines: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        def span_dict(span: SourceSpan | None) -> dict[str, int] | None:
            if span is None:
                return None
            return {"start_line": span.start_line, "end_line": span.end_line}

        return {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "base_span": span_dict(self.base_span),
            "patched_span": span_dict(self.patched_span),
            "removed_lines": list(self.removed_lines),
            "added_lines": list(self.added_lines),
        }


@dataclass(frozen=True)
class _SymbolRange:
    name: str
    kind: str
    start: int
    end: int

    @property
    def width(self) -> int:
        return self.end - self.start


def parse_unified_diff(diff_text: str) -> tuple[FileChange, ...]:
    """Parse changed line numbers from a Git-style unified diff.

    Only source coordinates are extracted.  Patch application remains Git's
    responsibility in :mod:`patch_verifier.workspace`.
    """

    changes: list[FileChange] = []
    old_path: str | None = None
    new_path: str | None = None
    old_lines: set[int] = set()
    new_lines: set[int] = set()
    old_cursor: int | None = None
    new_cursor: int | None = None

    def finish_file() -> None:
        nonlocal old_path, new_path, old_lines, new_lines
        if old_path is None and new_path is None:
            return
        changes.append(
            FileChange(
                old_path=old_path,
                new_path=new_path,
                old_lines=frozenset(old_lines),
                new_lines=frozenset(new_lines),
            )
        )
        old_path = None
        new_path = None
        old_lines = set()
        new_lines = set()

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            finish_file()
            old_cursor = None
            new_cursor = None
            continue
        match = _HUNK_RE.match(raw_line)
        if match:
            old_cursor = int(match.group("old_start"))
            new_cursor = int(match.group("new_start"))
            continue
        if old_cursor is not None and new_cursor is not None:
            if raw_line.startswith("\\ No newline at end of file"):
                continue
            if raw_line.startswith("+"):
                new_lines.add(new_cursor)
                new_cursor += 1
            elif raw_line.startswith("-"):
                old_lines.add(old_cursor)
                old_cursor += 1
            elif raw_line.startswith(" "):
                old_cursor += 1
                new_cursor += 1
            else:
                # Metadata starts a new section, or the input is not a
                # conventional Git diff. Stop interpreting body lines until
                # another hunk.
                old_cursor = None
                new_cursor = None
            continue
        if raw_line.startswith("--- "):
            old_path = _parse_diff_path(raw_line[4:])
            continue
        if raw_line.startswith("+++ "):
            new_path = _parse_diff_path(raw_line[4:])
            continue
        if raw_line.startswith(("rename from ", "copy from ")):
            old_path = _parse_diff_path(
                raw_line.split(" ", 2)[2], strip_git_prefix=False
            )
            continue
        if raw_line.startswith(("rename to ", "copy to ")):
            new_path = _parse_diff_path(
                raw_line.split(" ", 2)[2], strip_git_prefix=False
            )
            continue

    finish_file()
    return tuple(changes)


def analyze_changed_symbols(
    base_root: Path,
    patched_root: Path,
    changes: tuple[FileChange, ...],
) -> tuple[ChangedSymbol, ...]:
    """Map changed Python lines to their innermost enclosing symbols."""

    findings: list[ChangedSymbol] = []
    for change in changes:
        if not change.is_python:
            continue
        base_ranges = _ranges_for(base_root, change.old_path)
        patched_ranges = _ranges_for(patched_root, change.new_path)
        base_hits = _map_lines(change.old_lines, base_ranges)
        patched_hits = _map_lines(change.new_lines, patched_ranges)
        keys = sorted(set(base_hits) | set(patched_hits))
        if not keys and (change.old_lines or change.new_lines):
            keys = [("<module>", "module")]

        for name, kind in keys:
            base_range = _find_range(base_ranges, name, kind)
            patched_range = _find_range(patched_ranges, name, kind)
            removed = tuple(sorted(base_hits.get((name, kind), ())))
            added = tuple(sorted(patched_hits.get((name, kind), ())))
            findings.append(
                ChangedSymbol(
                    path=change.path,
                    qualified_name=name,
                    kind=kind,
                    base_span=_span(base_range),
                    patched_span=_span(patched_range),
                    removed_lines=removed,
                    added_lines=added,
                )
            )
    return tuple(findings)


def _parse_diff_path(value: str, *, strip_git_prefix: bool = True) -> str | None:
    value = value.rstrip("\r")
    if value.startswith('"'):
        value = _decode_git_quoted_path(value)
    else:
        # Traditional unified diffs may append a timestamp after a tab. Git's
        # own diff format does not, and Git C-quotes paths containing tabs.
        value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if not value:
        raise DiffParseError("empty diff path")
    path = value
    if strip_git_prefix and path.startswith(("a/", "b/")):
        path = path[2:]
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise DiffParseError(f"unsafe diff path: {path!r}")
    return candidate.as_posix()


def _decode_git_quoted_path(value: str) -> str:
    """Decode Git's byte-oriented C quoting for a path.

    Git represents non-ASCII path bytes as three-digit octal escapes by
    default.  Python string-literal decoding is not suitable here: decoding
    ``\\303\\251`` independently produces ``\u00c3\u00a9`` instead of first
    reconstructing the UTF-8 byte sequence for ``\u00e9``.
    """

    if len(value) < 2 or not value.endswith('"'):
        raise DiffParseError(f"invalid Git-quoted diff path: {value!r}")

    escapes = {
        "a": 0x07,
        "b": 0x08,
        "t": 0x09,
        "n": 0x0A,
        "v": 0x0B,
        "f": 0x0C,
        "r": 0x0D,
        '"': ord('"'),
        "\\": ord("\\"),
    }
    body = value[1:-1]
    decoded = bytearray()
    cursor = 0
    while cursor < len(body):
        character = body[cursor]
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            cursor += 1
            continue

        cursor += 1
        if cursor >= len(body):
            raise DiffParseError(f"invalid Git-quoted diff path: {value!r}")
        escape = body[cursor]
        if escape in escapes:
            decoded.append(escapes[escape])
            cursor += 1
            continue
        octal = body[cursor : cursor + 3]
        if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
            raise DiffParseError(f"invalid Git-quoted diff path: {value!r}")
        byte = int(octal, 8)
        if byte > 0xFF:
            raise DiffParseError(f"invalid Git-quoted diff path: {value!r}")
        decoded.append(byte)
        cursor += 3

    return os.fsdecode(bytes(decoded))


def _ranges_for(root: Path, relative_path: str | None) -> tuple[_SymbolRange, ...]:
    if relative_path is None:
        return ()
    resolved_root = root.resolve()
    path = resolved_root / relative_path
    if path.is_symlink():
        return ()
    try:
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return ()
    if not resolved_path.is_file():
        return ()
    try:
        tree = ast.parse(
            resolved_path.read_text(encoding="utf-8"),
            filename=str(resolved_path),
        )
    except (OSError, UnicodeError, SyntaxError):
        return ()

    ranges: list[_SymbolRange] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        next_parents = parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = ".".join((*parents, node.name))
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            end = getattr(node, "end_lineno", node.lineno)
            ranges.append(_SymbolRange(name, kind, node.lineno, end))
            next_parents = (*parents, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, next_parents)

    visit(tree, ())
    return tuple(ranges)


def _map_lines(
    lines: frozenset[int], ranges: tuple[_SymbolRange, ...]
) -> dict[tuple[str, str], set[int]]:
    mapped: dict[tuple[str, str], set[int]] = {}
    for line in lines:
        containing = [item for item in ranges if item.start <= line <= item.end]
        if containing:
            target = min(containing, key=lambda item: item.width)
            key = (target.name, target.kind)
        else:
            key = ("<module>", "module")
        mapped.setdefault(key, set()).add(line)
    return mapped


def _find_range(
    ranges: tuple[_SymbolRange, ...], name: str, kind: str
) -> _SymbolRange | None:
    return next((item for item in ranges if item.name == name and item.kind == kind), None)


def _span(item: _SymbolRange | None) -> SourceSpan | None:
    if item is None:
        return None
    return SourceSpan(item.start, item.end)

