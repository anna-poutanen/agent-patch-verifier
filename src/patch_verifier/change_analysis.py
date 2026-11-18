"""Lightweight, deterministic change analysis for Python diffs.

The analyzer deliberately reports what it can prove from line ranges.  It does
not attempt to construct a sound call graph or infer behavior.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
import os
from pathlib import Path
import re


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_MAX_SOURCE_BYTES = 2_000_000
_MAX_DIFF_FILES = 10_000
_MAX_CHANGED_LINES = 200_000


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
        """Whether either side of the change is Python source.

        Looking only at :attr:`path` loses a Python deletion when a file is
        renamed to a non-Python extension because ``path`` prefers the new
        name.  The per-revision properties also let the analyzer avoid
        parsing a non-Python counterpart merely because the other side is
        Python.
        """

        return self.old_is_python or self.new_is_python

    @property
    def old_is_python(self) -> bool:
        return self.old_path is not None and self.old_path.endswith(".py")

    @property
    def new_is_python(self) -> bool:
        return self.new_path is not None and self.new_path.endswith(".py")


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
    base_path: str | None = None
    patched_path: str | None = None
    base_occurrence: int | None = None
    patched_occurrence: int | None = None
    revision_match: str = "unique"

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
            "base_path": self.base_path,
            "patched_path": self.patched_path,
            "base_occurrence": self.base_occurrence,
            "patched_occurrence": self.patched_occurrence,
            "revision_match": self.revision_match,
        }


@dataclass(frozen=True)
class _SymbolRange:
    name: str
    kind: str
    occurrence: int
    start: int
    end: int

    @property
    def width(self) -> int:
        return self.end - self.start

    @property
    def identity(self) -> tuple[str, str, int]:
        return (self.name, self.kind, self.occurrence)


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
    changed_line_count = 0

    def finish_file() -> None:
        nonlocal old_path, new_path, old_lines, new_lines
        if old_path is None and new_path is None:
            return
        if len(changes) >= _MAX_DIFF_FILES:
            raise DiffParseError(
                f"diff contains more than {_MAX_DIFF_FILES} file entries"
            )
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

    for raw_line in _iter_diff_lines(diff_text):
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
                if new_cursor not in new_lines:
                    if changed_line_count >= _MAX_CHANGED_LINES:
                        raise DiffParseError(
                            "diff contains more than "
                            f"{_MAX_CHANGED_LINES} changed line coordinates"
                        )
                    changed_line_count += 1
                new_lines.add(new_cursor)
                new_cursor += 1
            elif raw_line.startswith("-"):
                if old_cursor not in old_lines:
                    if changed_line_count >= _MAX_CHANGED_LINES:
                        raise DiffParseError(
                            "diff contains more than "
                            f"{_MAX_CHANGED_LINES} changed line coordinates"
                        )
                    changed_line_count += 1
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


def _iter_diff_lines(diff_text: str) -> Iterable[str]:
    """Yield patch lines without materializing a second list of the patch."""

    start = 0
    length = len(diff_text)
    while start < length:
        end = diff_text.find("\n", start)
        if end == -1:
            line = diff_text[start:]
            start = length
        else:
            line = diff_text[start:end]
            start = end + 1
        if line.endswith("\r"):
            line = line[:-1]
        yield line


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
        change_finding_start = len(findings)
        base_path = change.old_path if change.old_is_python else None
        patched_path = change.new_path if change.new_is_python else None
        base_ranges = _ranges_for(base_root, base_path)
        patched_ranges = _ranges_for(patched_root, patched_path)
        base_hits = _map_lines(
            change.old_lines if base_path is not None else frozenset(), base_ranges
        )
        patched_hits = _map_lines(
            change.new_lines if patched_path is not None else frozenset(),
            patched_ranges,
        )
        hit_groups = sorted(
            {(name, kind) for name, kind, _ in set(base_hits) | set(patched_hits)}
        )
        if not hit_groups and base_path != patched_path:
            findings.extend(
                _path_change_scope(
                    change,
                    base_path,
                    patched_path,
                    base_ranges,
                    patched_ranges,
                )
            )
            continue

        for name, kind in hit_groups:
            base_group = tuple(
                item
                for item in base_ranges
                if item.name == name and item.kind == kind
            )
            patched_group = tuple(
                item
                for item in patched_ranges
                if item.name == name and item.kind == kind
            )

            if kind == "module":
                identity = (name, kind, 1)
                findings.append(
                    ChangedSymbol(
                        path=patched_path or base_path or change.path,
                        qualified_name=name,
                        kind=kind,
                        base_span=None,
                        patched_span=None,
                        removed_lines=tuple(sorted(base_hits.get(identity, ()))),
                        added_lines=tuple(sorted(patched_hits.get(identity, ()))),
                        base_path=base_path,
                        patched_path=patched_path,
                        revision_match="module",
                    )
                )
                continue

            # A qualified name and kind identify a definition across revisions
            # only when each side contains at most one such definition.  When
            # either side has duplicates, pairing by ordinal would silently
            # misattribute insertions or reorderings.  Report exact,
            # occurrence-aware, side-specific findings instead.
            if len(base_group) > 1 or len(patched_group) > 1:
                for identity in sorted(
                    key
                    for key in base_hits
                    if key[0] == name and key[1] == kind
                ):
                    base_range = _find_range(base_ranges, identity)
                    findings.append(
                        ChangedSymbol(
                            path=patched_path or base_path or change.path,
                            qualified_name=name,
                            kind=kind,
                            base_span=_span(base_range),
                            patched_span=None,
                            removed_lines=tuple(sorted(base_hits[identity])),
                            added_lines=(),
                            base_path=base_path,
                            # This record is deliberately unpaired.  Leaving
                            # the opposite path populated would let context
                            # collection attach a different same-named
                            # definition from that revision.
                            patched_path=None,
                            base_occurrence=identity[2],
                            revision_match="ambiguous_duplicate_unpaired",
                        )
                    )
                for identity in sorted(
                    key
                    for key in patched_hits
                    if key[0] == name and key[1] == kind
                ):
                    patched_range = _find_range(patched_ranges, identity)
                    findings.append(
                        ChangedSymbol(
                            path=patched_path or base_path or change.path,
                            qualified_name=name,
                            kind=kind,
                            base_span=None,
                            patched_span=_span(patched_range),
                            removed_lines=(),
                            added_lines=tuple(sorted(patched_hits[identity])),
                            base_path=None,
                            patched_path=patched_path,
                            patched_occurrence=identity[2],
                            revision_match="ambiguous_duplicate_unpaired",
                        )
                    )
                continue

            base_range = base_group[0] if base_group else None
            patched_range = patched_group[0] if patched_group else None
            base_identity = base_range.identity if base_range is not None else None
            patched_identity = (
                patched_range.identity if patched_range is not None else None
            )
            if base_range is not None and patched_range is not None:
                revision_match = "unique_identity"
            elif base_range is not None:
                revision_match = "base_only"
            else:
                revision_match = "patched_only"
            findings.append(
                ChangedSymbol(
                    path=patched_path or base_path or change.path,
                    qualified_name=name,
                    kind=kind,
                    base_span=_span(base_range),
                    patched_span=_span(patched_range),
                    removed_lines=tuple(
                        sorted(base_hits.get(base_identity, ()))
                        if base_identity is not None
                        else ()
                    ),
                    added_lines=tuple(
                        sorted(patched_hits.get(patched_identity, ()))
                        if patched_identity is not None
                        else ()
                    ),
                    base_path=base_path,
                    patched_path=patched_path,
                    base_occurrence=(
                        base_range.occurrence if base_range is not None else None
                    ),
                    patched_occurrence=(
                        patched_range.occurrence
                        if patched_range is not None
                        else None
                    ),
                    revision_match=revision_match,
                )
            )
        if base_path != patched_path:
            # A rename/copy changes the import path of every definition, not
            # only definitions that also contain edited lines.  Union the
            # path-selected scope with line-selected findings while avoiding
            # duplicate revision/occurrence identities.
            existing_identities = {
                identity
                for item in findings[change_finding_start:]
                for identity in _symbol_revision_identities(item)
            }
            for path_symbol in _path_change_scope(
                change,
                base_path,
                patched_path,
                base_ranges,
                patched_ranges,
            ):
                identities = _symbol_revision_identities(path_symbol)
                if identities and identities <= existing_identities:
                    continue
                findings.append(path_symbol)
                existing_identities.update(identities)
    return tuple(findings)


def _symbol_revision_identities(
    symbol: ChangedSymbol,
) -> set[tuple[str, str, str, str, int | None]]:
    identities: set[tuple[str, str, str, str, int | None]] = set()
    if symbol.base_path is not None:
        identities.add(
            (
                "base",
                symbol.base_path,
                symbol.qualified_name,
                symbol.kind,
                symbol.base_occurrence,
            )
        )
    if symbol.patched_path is not None:
        identities.add(
            (
                "patched",
                symbol.patched_path,
                symbol.qualified_name,
                symbol.kind,
                symbol.patched_occurrence,
            )
        )
    return identities


def _path_change_scope(
    change: FileChange,
    base_path: str | None,
    patched_path: str | None,
    base_ranges: tuple[_SymbolRange, ...],
    patched_ranges: tuple[_SymbolRange, ...],
) -> tuple[ChangedSymbol, ...]:
    """Conservatively expose definitions affected by a path-only change.

    Git emits no hunk coordinates for a 100% rename or copy.  Returning no
    symbols would make downstream context discovery blind to the definitions
    whose import path changed.  All definitions are therefore reported, but
    the revision-match value explicitly says that path metadata, rather than
    changed source lines, selected them.
    """

    path = patched_path or base_path or change.path
    groups = sorted(
        {(item.name, item.kind) for item in (*base_ranges, *patched_ranges)}
    )
    if not groups:
        return (
            ChangedSymbol(
                path=path,
                qualified_name="<module>",
                kind="module",
                base_span=None,
                patched_span=None,
                removed_lines=(),
                added_lines=(),
                base_path=base_path,
                patched_path=patched_path,
                revision_match="path_change_module",
            ),
        )

    findings: list[ChangedSymbol] = []
    for name, kind in groups:
        base_group = tuple(
            item for item in base_ranges if item.name == name and item.kind == kind
        )
        patched_group = tuple(
            item
            for item in patched_ranges
            if item.name == name and item.kind == kind
        )
        if len(base_group) <= 1 and len(patched_group) <= 1:
            base_range = base_group[0] if base_group else None
            patched_range = patched_group[0] if patched_group else None
            if base_range is not None and patched_range is not None:
                revision_match = "path_change_unique_identity"
            elif base_range is not None:
                revision_match = "path_change_base_only"
            else:
                revision_match = "path_change_patched_only"
            findings.append(
                ChangedSymbol(
                    path=path,
                    qualified_name=name,
                    kind=kind,
                    base_span=_span(base_range),
                    patched_span=_span(patched_range),
                    removed_lines=(),
                    added_lines=(),
                    base_path=base_path,
                    patched_path=patched_path,
                    base_occurrence=(
                        base_range.occurrence if base_range is not None else None
                    ),
                    patched_occurrence=(
                        patched_range.occurrence
                        if patched_range is not None
                        else None
                    ),
                    revision_match=revision_match,
                )
            )
            continue

        # Ordinal pairing is unsafe when duplicate qualified names are
        # inserted, removed, or reordered.  Keep each revision explicit.
        for item in base_group:
            findings.append(
                ChangedSymbol(
                    path=path,
                    qualified_name=name,
                    kind=kind,
                    base_span=_span(item),
                    patched_span=None,
                    removed_lines=(),
                    added_lines=(),
                    base_path=base_path,
                    patched_path=None,
                    base_occurrence=item.occurrence,
                    revision_match="path_change_ambiguous_duplicate_unpaired",
                )
            )
        for item in patched_group:
            findings.append(
                ChangedSymbol(
                    path=path,
                    qualified_name=name,
                    kind=kind,
                    base_span=None,
                    patched_span=_span(item),
                    removed_lines=(),
                    added_lines=(),
                    base_path=None,
                    patched_path=patched_path,
                    patched_occurrence=item.occurrence,
                    revision_match="path_change_ambiguous_duplicate_unpaired",
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
        with resolved_path.open("rb") as stream:
            source_bytes = stream.read(_MAX_SOURCE_BYTES + 1)
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            return ()
        tree = ast.parse(
            source_bytes.decode("utf-8"),
            filename=str(resolved_path),
        )
    except (OSError, UnicodeError, SyntaxError, RecursionError):
        return ()

    ranges: list[_SymbolRange] = []
    occurrences: dict[tuple[str, str], int] = {}

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
            identity = (name, kind)
            occurrence = occurrences.get(identity, 0) + 1
            occurrences[identity] = occurrence
            decorator_lines = [item.lineno for item in node.decorator_list]
            start = min([node.lineno, *decorator_lines])
            end = getattr(node, "end_lineno", node.lineno)
            ranges.append(_SymbolRange(name, kind, occurrence, start, end))
            next_parents = (*parents, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, next_parents)

    try:
        visit(tree, ())
    except RecursionError:
        # Deep but valid syntax trees can exceed Python's recursion limit in
        # this lightweight visitor.  Treat the file as unstructured source so
        # changed coordinates map to conservative module scope.
        return ()
    return tuple(ranges)


def _map_lines(
    lines: frozenset[int], ranges: tuple[_SymbolRange, ...]
) -> dict[tuple[str, str, int], set[int]]:
    mapped: dict[tuple[str, str, int], set[int]] = {}
    for line in lines:
        containing = [item for item in ranges if item.start <= line <= item.end]
        if containing:
            target = min(containing, key=lambda item: item.width)
            key = target.identity
        else:
            key = ("<module>", "module", 1)
        mapped.setdefault(key, set()).add(line)
    return mapped


def _find_range(
    ranges: tuple[_SymbolRange, ...], identity: tuple[str, str, int]
) -> _SymbolRange | None:
    return next((item for item in ranges if item.identity == identity), None)


def _span(item: _SymbolRange | None) -> SourceSpan | None:
    if item is None:
        return None
    return SourceSpan(item.start, item.end)

