"""Deterministic, reviewable repository context for changed Python symbols.

This module intentionally performs static analysis only.  It finds context that
is useful for proposing tests, but does not claim to construct a complete call
graph.  Every selected item records the reason it was selected and the result
always carries the important limitations of the analysis.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
import io
from itertools import islice
import os
from pathlib import Path, PurePosixPath
import tokenize
from typing import Any, Iterable

from patch_verifier.change_analysis import ChangedSymbol, SourceSpan


_MAX_FILE_BYTES = 2_000_000
_MAX_PYTHON_FILES = 5_000
_MAX_TOTAL_SOURCE_BYTES = 20_000_000
_MAX_SOURCE_TOKENS = 100_000
_MAX_AST_NODES = 100_000
_MAX_ANALYSIS_RECORDS = 100_000
_MAX_EXCERPT_CHARS = 4_000
_MAX_ITEMS_PER_SYMBOL = 100
_MAX_CHANGED_SYMBOLS = 200
_MAX_CHANGED_SYMBOL_INPUTS = 10_000
_MAX_TOTAL_CONTEXT_ITEMS = 500
_MAX_DISCOVERED_ENTRIES = 100_000
_MAX_DIAGNOSTICS = 200
_SOURCE_ROOT_NAMES = frozenset({"src", "lib", "python", "source"})
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


class ContextKind(str, Enum):
    """The kind of source evidence in a context item."""

    SIGNATURE = "signature"
    DOCSTRING = "docstring"
    IMPORT = "import"
    CALLER = "caller"
    NEARBY_TEST = "nearby_test"


class ContextRevision(str, Enum):
    """The source revision represented by a context snapshot."""

    BASE = "base"
    PATCHED = "patched"


@dataclass(frozen=True)
class SelectionReason:
    """A machine-readable explanation of why an item was selected."""

    code: str
    detail: str
    weight: int

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "weight": self.weight}


@dataclass(frozen=True)
class ContextItem:
    """One ranked source item associated with a changed symbol."""

    kind: ContextKind
    path: str
    start_line: int
    end_line: int
    symbol: str | None
    excerpt: str
    excerpt_truncated: bool
    score: int
    rank: int
    selection_reasons: tuple[SelectionReason, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol": self.symbol,
            "excerpt": self.excerpt,
            "excerpt_truncated": self.excerpt_truncated,
            "score": self.score,
            "rank": self.rank,
            "selection_reasons": [item.to_dict() for item in self.selection_reasons],
        }


@dataclass(frozen=True)
class ContextDiagnostic:
    """A file that could not be analyzed completely and the reason why."""

    code: str
    path: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "line": self.line,
        }


@dataclass(frozen=True)
class SymbolContext:
    """Ranked context and limitations for one changed symbol."""

    path: str
    qualified_name: str
    kind: str
    analyzed_path: str | None
    base_path: str | None
    patched_path: str | None
    base_occurrence: int | None
    patched_occurrence: int | None
    revision_match: str
    items: tuple[ContextItem, ...] = field(default_factory=tuple)
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "analyzed_path": self.analyzed_path,
            "base_path": self.base_path,
            "patched_path": self.patched_path,
            "base_occurrence": self.base_occurrence,
            "patched_occurrence": self.patched_occurrence,
            "revision_match": self.revision_match,
            "items": [item.to_dict() for item in self.items],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class RepositoryContext:
    """Portable result of a static repository-context collection pass."""

    symbols: tuple[SymbolContext, ...]
    diagnostics: tuple[ContextDiagnostic, ...]
    limitations: tuple[str, ...]
    revision: ContextRevision = ContextRevision.PATCHED
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "analysis": "static",
            "revision": self.revision.value,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class _Definition:
    qualified_name: str
    kind: str
    occurrence: int
    start: int
    end: int
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


@dataclass(frozen=True)
class _ImportBinding:
    local_name: str
    module: str
    member: str | None
    import_style: str


@dataclass(frozen=True)
class _ImportRecord:
    start: int
    end: int
    owner: str | None
    excerpt: str
    bindings: tuple[_ImportBinding, ...]
    is_wildcard: bool


@dataclass(frozen=True)
class _CallRecord:
    start: int
    end: int
    owner: str | None
    target: tuple[str, ...]
    lambda_parameters: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _NameBinding:
    owner: str | None
    name: str
    kind: str
    line: int
    definition: str | None = None


@dataclass(frozen=True)
class _PythonFile:
    path: str
    module: str
    lines: tuple[str, ...]
    definitions: tuple[_Definition, ...]
    imports: tuple[_ImportRecord, ...]
    calls: tuple[_CallRecord, ...]
    bindings: tuple[_NameBinding, ...]
    is_test: bool


@dataclass
class _Candidate:
    kind: ContextKind
    path: str
    start: int
    end: int
    symbol: str | None
    excerpt: str
    excerpt_truncated: bool
    reasons: dict[str, SelectionReason] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(reason.weight for reason in self.reasons.values())


@dataclass
class _CandidatePool:
    limit: int
    items: dict[tuple[object, ...], _Candidate] = field(default_factory=dict)
    truncated: bool = False


class _AnalysisRecordLimitExceeded(Exception):
    """The repository-wide retained static-analysis record budget is exhausted."""


_STATIC_LIMITATIONS = (
    "Static analysis does not execute descriptors, decorators, monkeypatching, "
    "dependency injection, or runtime registration.",
    "Dynamic imports and calls through values whose concrete type is unknown may "
    "not be discovered.",
    "Reported callers are likely static matches, not a complete or sound call graph.",
)


def _append_context_diagnostic(
    diagnostics: list[ContextDiagnostic], diagnostic: ContextDiagnostic
) -> None:
    """Append a diagnostic without allowing adversarial artifact growth.

    The first ``_MAX_DIAGNOSTICS - 1`` details are preserved.  Once another
    diagnostic arrives, the final slot becomes a stable truncation marker and
    subsequent details are ignored.
    """

    if _MAX_DIAGNOSTICS <= 0:
        return
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(diagnostic)
        return
    if diagnostics[-1].code != "diagnostic_limit_reached":
        diagnostics[-1] = ContextDiagnostic(
            code="diagnostic_limit_reached",
            path=".",
            message=(
                "additional repository-context diagnostics were omitted after "
                f"the {_MAX_DIAGNOSTICS}-entry limit was reached"
            ),
        )


def collect_repository_context(
    root: Path,
    changed_symbols: Iterable[ChangedSymbol],
    *,
    revision: ContextRevision | str = ContextRevision.PATCHED,
) -> RepositoryContext:
    """Collect deterministic static context for ``changed_symbols`` under ``root``.

    The returned dataclasses are frozen and contain tuples only, so callers can
    safely cache them.  :meth:`RepositoryContext.to_dict` produces only JSON
    primitives.  Files outside ``root``, symlinks, oversized files, and files
    that fail to parse are never used as evidence and are instead diagnosed.
    """

    revision = ContextRevision(revision)
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(str(root))

    bounded_symbols = tuple(
        islice(changed_symbols, _MAX_CHANGED_SYMBOL_INPUTS + 1)
    )
    if len(bounded_symbols) > _MAX_CHANGED_SYMBOL_INPUTS:
        raise ValueError(
            "changed symbol input exceeds the "
            f"{_MAX_CHANGED_SYMBOL_INPUTS}-symbol safety limit"
        )

    files, diagnostics = _index_repository(resolved_root)
    files_by_path = {item.path: item for item in files}
    module_paths: dict[str, set[str]] = {}
    for item in files:
        module_paths.setdefault(item.module, set()).add(item.path)

    ordered_symbols = sorted(bounded_symbols, key=_changed_symbol_order_key)
    if len(ordered_symbols) > _MAX_CHANGED_SYMBOLS:
        skipped_symbols = len(ordered_symbols) - _MAX_CHANGED_SYMBOLS
        _append_context_diagnostic(
            diagnostics,
            ContextDiagnostic(
                code="changed_symbol_limit_reached",
                path=".",
                message=(
                    f"retained the first {_MAX_CHANGED_SYMBOLS} changed symbols "
                    "in deterministic order and skipped "
                    f"{skipped_symbols} additional symbols"
                ),
            )
        )
        ordered_symbols = ordered_symbols[:_MAX_CHANGED_SYMBOLS]

    contexts: list[SymbolContext] = []
    remaining_items = _MAX_TOTAL_CONTEXT_ITEMS
    for index, symbol in enumerate(ordered_symbols):
        if remaining_items == 0:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="context_item_limit_reached",
                    path=".",
                    message=(
                        "repository context is limited to "
                        f"{_MAX_TOTAL_CONTEXT_ITEMS} ranked items; skipped "
                        f"{len(ordered_symbols) - index} changed symbols"
                    ),
                )
            )
            break
        symbol_context = _context_for_symbol(
            symbol,
            revision,
            files,
            files_by_path,
            module_paths,
            diagnostics,
            max_items=min(_MAX_ITEMS_PER_SYMBOL, remaining_items),
        )
        contexts.append(symbol_context)
        remaining_items -= len(symbol_context.items)
    return RepositoryContext(
        symbols=tuple(contexts),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.path, item.line or 0, item.code, item.message),
            )
        ),
        limitations=_STATIC_LIMITATIONS,
        revision=revision,
    )


def _index_repository(
    root: Path,
) -> tuple[tuple[_PythonFile, ...], list[ContextDiagnostic]]:
    paths: list[tuple[str, Path]] = []
    diagnostics: list[ContextDiagnostic] = []
    skipped_python_paths = 0
    discovered_entries = 0
    discovery_limit_reached = False

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            if discovered_entries >= _MAX_DISCOVERED_ENTRIES:
                discovery_limit_reached = True
                break
            discovered_entries += 1
            child = directory_path / name
            relative = child.relative_to(root).as_posix()
            if name in _SKIPPED_DIRECTORIES:
                continue
            if child.is_symlink():
                _append_context_diagnostic(
                    diagnostics,
                    ContextDiagnostic(
                        code="symlink_skipped",
                        path=relative,
                        message="symlinked directory was not traversed",
                    )
                )
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

        if discovery_limit_reached:
            directory_names[:] = []
            break

        for name in sorted(file_names):
            if discovered_entries >= _MAX_DISCOVERED_ENTRIES:
                discovery_limit_reached = True
                break
            discovered_entries += 1
            if not name.endswith(".py"):
                continue
            child = directory_path / name
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                _append_context_diagnostic(
                    diagnostics,
                    ContextDiagnostic(
                        code="symlink_skipped",
                        path=relative,
                        message="symlinked file was not analyzed",
                    )
                )
                continue
            if len(paths) >= _MAX_PYTHON_FILES:
                skipped_python_paths += 1
                continue
            try:
                resolved = child.resolve(strict=True)
                resolved.relative_to(root)
                if not resolved.is_file():
                    continue
            except (OSError, ValueError):
                _append_context_diagnostic(
                    diagnostics,
                    ContextDiagnostic(
                        code="unsafe_path",
                        path=relative,
                        message=(
                            "file did not resolve to a regular file inside the root"
                        ),
                    )
                )
                continue
            paths.append((relative, resolved))

        if discovery_limit_reached:
            directory_names[:] = []
            break

    if discovery_limit_reached:
        _append_context_diagnostic(
            diagnostics,
            ContextDiagnostic(
                code="directory_entry_limit_reached",
                path=".",
                message=(
                    "repository discovery stopped after "
                    f"{_MAX_DISCOVERED_ENTRIES} directory entries"
                ),
            ),
        )

    paths.sort(key=lambda item: item[0])
    if skipped_python_paths:
        _append_context_diagnostic(
            diagnostics,
            ContextDiagnostic(
                code="file_limit_reached",
                path=".",
                message=(
                    f"retained the first {_MAX_PYTHON_FILES} safe Python files in "
                    "deterministic traversal order and skipped "
                    f"{skipped_python_paths} additional Python paths"
                ),
            )
        )

    indexed: list[_PythonFile] = []
    total_source_bytes = 0
    source_limit_reported = False
    remaining_ast_nodes = _MAX_AST_NODES
    remaining_analysis_records = _MAX_ANALYSIS_RECORDS
    for relative, path in paths:
        if remaining_ast_nodes == 0:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="ast_node_limit_reached",
                    path=".",
                    message=(
                        "repository static analysis is limited to "
                        f"{_MAX_AST_NODES} retained AST nodes; remaining files "
                        "were omitted in deterministic path order"
                    ),
                ),
            )
            break
        if remaining_analysis_records == 0:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="analysis_record_limit_reached",
                    path=".",
                    message=(
                        "repository static analysis is limited to "
                        f"{_MAX_ANALYSIS_RECORDS} retained analysis records; "
                        "remaining files were omitted in deterministic path order"
                    ),
                ),
            )
            break
        try:
            with path.open("rb") as source_file:
                source_bytes = source_file.read(_MAX_FILE_BYTES + 1)
            if len(source_bytes) > _MAX_FILE_BYTES:
                _append_context_diagnostic(
                    diagnostics,
                    ContextDiagnostic(
                        code="file_too_large",
                        path=relative,
                        message=(
                            f"file exceeds the {_MAX_FILE_BYTES}-byte analysis limit"
                        ),
                    )
                )
                continue
            if total_source_bytes + len(source_bytes) > _MAX_TOTAL_SOURCE_BYTES:
                if not source_limit_reported:
                    _append_context_diagnostic(
                        diagnostics,
                        ContextDiagnostic(
                            code="source_bytes_limit_reached",
                            path=".",
                            message=(
                                "repository source analysis is limited to "
                                f"{_MAX_TOTAL_SOURCE_BYTES} bytes"
                            ),
                        )
                    )
                    source_limit_reported = True
                continue
            total_source_bytes += len(source_bytes)
            source = source_bytes.decode("utf-8")
            if _source_token_limit_exceeded(source):
                _append_context_diagnostic(
                    diagnostics,
                    ContextDiagnostic(
                        code="source_token_limit_reached",
                        path=relative,
                        message=(
                            "file exceeds the "
                            f"{_MAX_SOURCE_TOKENS}-token pre-parse analysis limit"
                        ),
                    ),
                )
                continue
            tree = ast.parse(source, filename=relative)
        except RecursionError:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="analysis_depth_exceeded",
                    path=relative,
                    message="AST parsing exceeds the static analysis depth limit",
                )
            )
            continue
        except SyntaxError as error:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="syntax_error",
                    path=relative,
                    message=error.msg,
                    line=error.lineno,
                )
            )
            continue
        except (OSError, UnicodeError) as error:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="read_error",
                    path=relative,
                    message=type(error).__name__,
                )
            )
            continue

        ast_node_count = _bounded_ast_node_count(tree, remaining_ast_nodes)
        if ast_node_count is None:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="ast_node_limit_reached",
                    path=relative,
                    message=(
                        "repository static analysis is limited to "
                        f"{_MAX_AST_NODES} retained AST nodes; this file and "
                        "remaining files were omitted in deterministic path order"
                    ),
                ),
            )
            break

        module = _module_name(relative)
        try:
            (
                definitions,
                imports,
                calls,
                bindings,
                analysis_record_count,
            ) = _analyze_tree(
                tree,
                source,
                module,
                relative,
                record_limit=remaining_analysis_records,
            )
        except _AnalysisRecordLimitExceeded:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="analysis_record_limit_reached",
                    path=relative,
                    message=(
                        "repository static analysis is limited to "
                        f"{_MAX_ANALYSIS_RECORDS} retained analysis records; "
                        "this file and remaining files were omitted in "
                        "deterministic path order"
                    ),
                ),
            )
            break
        except RecursionError:
            _append_context_diagnostic(
                diagnostics,
                ContextDiagnostic(
                    code="analysis_depth_exceeded",
                    path=relative,
                    message="AST nesting exceeds the static analysis depth limit",
                )
            )
            continue
        remaining_ast_nodes -= ast_node_count
        remaining_analysis_records -= analysis_record_count
        indexed.append(
            _PythonFile(
                path=relative,
                module=module,
                # Excerpts are selected many times while ranking callers and
                # tests.  Splitting once avoids quadratic work on a large file
                # with many matching call sites.
                lines=tuple(source.splitlines()),
                definitions=definitions,
                imports=imports,
                calls=calls,
                bindings=bindings,
                is_test=_is_test_path(relative),
            )
        )
    return tuple(indexed), diagnostics


def _source_token_limit_exceeded(source: str) -> bool:
    """Return whether ``source`` exceeds the allocation-safe parse gate.

    Invalid token streams are passed through so ``ast.parse`` can preserve the
    existing, more useful syntax-error diagnostic.
    """

    source_stream = io.StringIO(source)
    try:
        for token_count, _ in enumerate(
            tokenize.generate_tokens(source_stream.readline), start=1
        ):
            if token_count > _MAX_SOURCE_TOKENS:
                return True
    except (IndentationError, tokenize.TokenError):
        return False
    finally:
        source_stream.close()
    return False


def _bounded_ast_node_count(tree: ast.AST, limit: int) -> int | None:
    """Count AST nodes without traversing beyond the remaining global budget."""

    count = 0
    for _ in ast.walk(tree):
        count += 1
        if count > limit:
            return None
    return count


def _analyze_tree(
    tree: ast.Module,
    source: str,
    module: str,
    path: str,
    *,
    record_limit: int,
) -> tuple[
    tuple[_Definition, ...],
    tuple[_ImportRecord, ...],
    tuple[_CallRecord, ...],
    tuple[_NameBinding, ...],
    int,
]:
    definitions: list[_Definition] = []
    imports: list[_ImportRecord] = []
    calls: list[_CallRecord] = []
    bindings: list[_NameBinding] = []
    occurrences: dict[tuple[str, str], int] = {}
    record_count = 0

    def reserve_records(count: int = 1) -> None:
        nonlocal record_count
        if count > record_limit - record_count:
            raise _AnalysisRecordLimitExceeded
        record_count += count

    def visit(
        node: ast.AST,
        parents: tuple[str, ...],
        owner: str | None,
        lambda_parameters: frozenset[str],
    ) -> None:
        next_parents = parents
        next_owner = owner
        next_lambda_parameters = lambda_parameters
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified_name = ".".join((*parents, node.name))
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else "async_function"
                if isinstance(node, ast.AsyncFunctionDef)
                else "function"
            )
            identity = (qualified_name, kind)
            occurrence = occurrences.get(identity, 0) + 1
            occurrences[identity] = occurrence
            decorator_lines = [item.lineno for item in node.decorator_list]
            arguments = (
                _function_arguments(node.args)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else ()
            )
            reserve_records(2 + len(arguments))
            bindings.append(
                _NameBinding(
                    owner=owner,
                    name=node.name,
                    kind="definition",
                    line=node.lineno,
                    definition=qualified_name,
                )
            )
            definitions.append(
                _Definition(
                    qualified_name=qualified_name,
                    kind=kind,
                    occurrence=occurrence,
                    start=min([node.lineno, *decorator_lines]),
                    end=getattr(node, "end_lineno", node.lineno),
                    node=node,
                )
            )
            next_parents = (*parents, node.name)
            next_owner = qualified_name
            for argument in arguments:
                bindings.append(
                    _NameBinding(
                        owner=qualified_name,
                        name=argument.arg,
                        kind="parameter",
                        line=argument.lineno,
                    )
                )
        elif isinstance(node, ast.Lambda):
            next_lambda_parameters = lambda_parameters | frozenset(
                argument.arg for argument in _function_arguments(node.args)
            )

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_bindings, wildcard = _import_bindings(node, module, path)
            reserve_records(1 + len(import_bindings))
            imports.append(
                _ImportRecord(
                    start=node.lineno,
                    end=getattr(node, "end_lineno", node.lineno),
                    owner=owner,
                    excerpt=ast.get_source_segment(source, node) or "",
                    bindings=import_bindings,
                    is_wildcard=wildcard,
                )
            )
        elif isinstance(node, ast.Name) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            reserve_records()
            bindings.append(
                _NameBinding(
                    owner=owner,
                    name=node.id,
                    kind="assignment" if isinstance(node.ctx, ast.Store) else "delete",
                    line=node.lineno,
                )
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            reserve_records(len(node.names))
            for name in node.names:
                bindings.append(
                    _NameBinding(
                        owner=owner,
                        name=name,
                        kind=(
                            "global_directive"
                            if isinstance(node, ast.Global)
                            else "nonlocal_directive"
                        ),
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.ExceptHandler) and node.name:
            reserve_records()
            bindings.append(
                _NameBinding(
                    owner=owner,
                    name=node.name,
                    kind="exception_target",
                    line=node.lineno,
                )
            )
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            reserve_records()
            bindings.append(
                _NameBinding(
                    owner=owner,
                    name=node.name,
                    kind="pattern_capture",
                    line=node.lineno,
                )
            )
        elif isinstance(node, ast.MatchMapping) and node.rest:
            reserve_records()
            bindings.append(
                _NameBinding(
                    owner=owner,
                    name=node.rest,
                    kind="pattern_capture",
                    line=node.lineno,
                )
            )
        elif isinstance(node, ast.Call):
            target = _dotted_expression(node.func)
            if target:
                reserve_records()
                calls.append(
                    _CallRecord(
                        start=node.lineno,
                        end=getattr(node, "end_lineno", node.lineno),
                        owner=owner,
                        target=target,
                        lambda_parameters=lambda_parameters,
                    )
                )

        for child in ast.iter_child_nodes(node):
            visit(child, next_parents, next_owner, next_lambda_parameters)

    visit(tree, (), None, frozenset())
    return (
        tuple(sorted(definitions, key=lambda item: (item.start, item.end))),
        tuple(sorted(imports, key=lambda item: (item.start, item.end))),
        tuple(sorted(calls, key=lambda item: (item.start, item.end, item.target))),
        tuple(
            sorted(
                bindings,
                key=lambda item: (
                    item.owner or "",
                    item.name,
                    item.line,
                    item.kind,
                    item.definition or "",
                ),
            )
        ),
        record_count,
    )


def _context_for_symbol(
    symbol: ChangedSymbol,
    revision: ContextRevision,
    files: tuple[_PythonFile, ...],
    files_by_path: dict[str, _PythonFile],
    module_paths: dict[str, set[str]],
    diagnostics: list[ContextDiagnostic],
    *,
    max_items: int,
) -> SymbolContext:
    limitations: set[str] = set()
    candidates = _CandidatePool(max_items)

    target_path, target_span, target_occurrence = _revision_identity(symbol, revision)

    if target_path is not None and not _is_safe_relative_path(target_path):
        return SymbolContext(
            path=symbol.path,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            analyzed_path=target_path,
            base_path=symbol.base_path,
            patched_path=symbol.patched_path,
            base_occurrence=symbol.base_occurrence,
            patched_occurrence=symbol.patched_occurrence,
            revision_match=symbol.revision_match,
            limitations=(
                "Changed symbol path is not a safe repository-relative path.",
            ),
        )

    target_file = files_by_path.get(target_path) if target_path is not None else None
    identity_path = target_path or symbol.path
    target_module = _module_name(identity_path)
    if target_file is None:
        matching_diagnostics = [
            item for item in diagnostics if item.path == target_path
        ]
        if matching_diagnostics:
            limitations.add(
                "The changed file could not be parsed safely; definition context "
                "is unavailable."
            )
        elif target_path is None or target_span is None:
            limitations.add(
                "The changed symbol is absent from this revision; definition "
                "context is unavailable."
            )
        else:
            limitations.add(
                "The changed file was not found among safe Python files in this "
                "revision."
            )

    matching_definitions = (
        []
        if target_file is None
        else [
            item
            for item in target_file.definitions
            if item.qualified_name == symbol.qualified_name and item.kind == symbol.kind
        ]
    )
    selected_definition = _select_definition(
        matching_definitions, target_span, target_occurrence
    )
    duplicate_definition = len(matching_definitions) > 1
    if duplicate_definition:
        limitations.add(
            "Multiple definitions share this qualified name; runtime binding and "
            "callers are ambiguous."
        )
    if symbol.revision_match == "ambiguous_duplicate_unpaired":
        limitations.add(
            "The change analyzer could not pair this duplicate definition across "
            "revisions."
        )
    if matching_definitions and selected_definition is None:
        limitations.add(
            "A unique changed definition could not be selected from duplicate names."
        )
    elif (
        target_file is not None
        and not matching_definitions
        and symbol.qualified_name != "<module>"
    ):
        limitations.add("No matching definition was found in the analyzed revision.")

    if selected_definition is not None and target_file is not None:
        signature = _signature(selected_definition.node)
        _add_candidate(
            candidates,
            kind=ContextKind.SIGNATURE,
            file=target_file,
            start=selected_definition.node.lineno,
            end=selected_definition.node.lineno,
            symbol=selected_definition.qualified_name,
            excerpt=signature,
            reason=SelectionReason(
                "changed_definition",
                "signature of the changed definition",
                100,
            ),
        )
        docstring_node = _docstring_node(selected_definition.node)
        docstring = ast.get_docstring(selected_definition.node, clean=False)
        if docstring_node is not None and docstring is not None:
            _add_candidate(
                candidates,
                kind=ContextKind.DOCSTRING,
                file=target_file,
                start=docstring_node.lineno,
                end=getattr(docstring_node, "end_lineno", docstring_node.lineno),
                symbol=selected_definition.qualified_name,
                excerpt=docstring,
                reason=SelectionReason(
                    "changed_definition_docstring",
                    "documented contract on the changed definition",
                    95,
                ),
            )

    if target_file is not None:
        for import_record in target_file.imports:
            inside_symbol = (
                selected_definition is not None
                and import_record.owner is not None
                and (
                    import_record.owner == selected_definition.qualified_name
                    or import_record.owner.startswith(
                        selected_definition.qualified_name + "."
                    )
                )
            )
            enclosing_symbol = (
                selected_definition is not None
                and import_record.owner is not None
                and selected_definition.qualified_name.startswith(
                    import_record.owner + "."
                )
            )
            if (
                import_record.owner is not None
                and not inside_symbol
                and not enclosing_symbol
            ):
                continue
            reason = (
                SelectionReason(
                    "import_inside_changed_symbol",
                    "import executes inside the changed definition",
                    80,
                )
                if inside_symbol
                else SelectionReason(
                    "import_in_enclosing_scope",
                    "import appears in a lexical ancestor of the changed definition",
                    65,
                )
                if enclosing_symbol
                else SelectionReason(
                    "changed_module_import",
                    "module-level dependency of the changed definition",
                    55,
                )
            )
            _add_candidate(
                candidates,
                kind=ContextKind.IMPORT,
                file=target_file,
                start=import_record.start,
                end=import_record.end,
                symbol=import_record.owner,
                excerpt=import_record.excerpt,
                reason=reason,
            )
            if import_record.is_wildcard:
                limitations.add(
                    "A wildcard import in the changed module prevents precise name "
                    "resolution."
                )

    module_is_ambiguous = len(module_paths.get(target_module, ())) > 1
    if module_is_ambiguous:
        limitations.add(
            "The inferred import module maps to multiple repository paths; "
            "external callers were not resolved."
        )

    target_leaf = symbol.qualified_name.rsplit(".", 1)[-1]
    target_stem = PurePosixPath(identity_path).stem.casefold()
    can_resolve_callers = (
        selected_definition is not None
        and not duplicate_definition
        and symbol.qualified_name != "<module>"
    )

    if can_resolve_callers:
        for source_file in files:
            if module_is_ambiguous and source_file.path != target_path:
                continue
            for call in source_file.calls:
                if not _call_matches_symbol(
                    call,
                    source_file,
                    target_module,
                    symbol.qualified_name,
                    target_file,
                ):
                    continue
                definition = _definition_for_owner(source_file, call.owner)
                start = definition.start if definition else call.start
                end = definition.end if definition else call.end
                excerpt = _source_excerpt(source_file.lines, start, end)
                if source_file.is_test and definition is not None and _is_test_name(
                    definition.qualified_name
                ):
                    kind = ContextKind.NEARBY_TEST
                    reason = SelectionReason(
                        "test_calls_changed_symbol",
                        "test contains a statically resolved call at line "
                        f"{call.start}",
                        90,
                    )
                    name_reason = _nearby_test_selection_reason(
                        source_file,
                        definition,
                        target_leaf=target_leaf,
                        target_stem=target_stem,
                    )
                else:
                    kind = ContextKind.CALLER
                    reason = SelectionReason(
                        "static_call_match",
                        "call target resolves through local or imported names at "
                        f"line {call.start}",
                        75,
                    )
                    name_reason = None
                _add_candidate(
                    candidates,
                    kind=kind,
                    file=source_file,
                    start=start,
                    end=end,
                    symbol=call.owner,
                    excerpt=excerpt,
                    reason=reason,
                    additional_reasons=(
                        () if name_reason is None else (name_reason,)
                    ),
                )

    for source_file in files:
        if not source_file.is_test:
            continue
        for definition in source_file.definitions:
            if not _is_test_name(definition.qualified_name):
                continue
            reason = _nearby_test_selection_reason(
                source_file,
                definition,
                target_leaf=target_leaf,
                target_stem=target_stem,
            )
            if reason is None:
                continue
            excerpt = _source_excerpt(
                source_file.lines, definition.start, definition.end
            )
            _add_candidate(
                candidates,
                kind=ContextKind.NEARBY_TEST,
                file=source_file,
                start=definition.start,
                end=definition.end,
                symbol=definition.qualified_name,
                excerpt=excerpt,
                reason=reason,
            )

    ordered = sorted(
        candidates.items.values(),
        key=_candidate_order_key,
    )
    if candidates.truncated or len(ordered) > max_items:
        limitations.add(
            "Context was truncated to the "
            f"{max_items} highest-ranked items."
        )
        ordered = ordered[:max_items]

    items = tuple(
        ContextItem(
            kind=candidate.kind,
            path=candidate.path,
            start_line=candidate.start,
            end_line=candidate.end,
            symbol=candidate.symbol,
            excerpt=candidate.excerpt,
            excerpt_truncated=candidate.excerpt_truncated,
            score=candidate.score,
            rank=index,
            selection_reasons=tuple(
                sorted(
                    candidate.reasons.values(),
                    key=lambda reason: (-reason.weight, reason.code, reason.detail),
                )
            ),
        )
        for index, candidate in enumerate(ordered, start=1)
    )
    return SymbolContext(
        path=symbol.path,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        analyzed_path=target_path,
        base_path=symbol.base_path,
        patched_path=symbol.patched_path,
        base_occurrence=symbol.base_occurrence,
        patched_occurrence=symbol.patched_occurrence,
        revision_match=symbol.revision_match,
        items=items,
        limitations=tuple(sorted(limitations)),
    )


def _select_definition(
    definitions: list[_Definition],
    span: SourceSpan | None,
    occurrence: int | None,
) -> _Definition | None:
    if not definitions:
        return None
    if occurrence is not None:
        occurrence_matches = [
            item for item in definitions if item.occurrence == occurrence
        ]
        if len(occurrence_matches) == 1:
            return occurrence_matches[0]
    if span is not None:
        exact = [
            item
            for item in definitions
            if item.start == span.start_line and item.end == span.end_line
        ]
        if len(exact) == 1:
            return exact[0]
        containing = [
            item
            for item in definitions
            if item.start <= span.start_line <= item.end
        ]
        if len(containing) == 1:
            return containing[0]
    return definitions[0] if len(definitions) == 1 else None


def _revision_identity(
    symbol: ChangedSymbol,
    revision: ContextRevision,
) -> tuple[str | None, SourceSpan | None, int | None]:
    """Return the path, span, and occurrence belonging to ``revision``.

    Older or hand-authored ``ChangedSymbol`` values may omit both side paths;
    ``path`` remains a backwards-compatible fallback in that case.  Once either
    side-specific path is present, a missing path means the symbol is genuinely
    absent from that revision and must not fall back to the other side.
    """

    has_side_paths = symbol.base_path is not None or symbol.patched_path is not None
    if revision is ContextRevision.BASE:
        path = symbol.base_path if has_side_paths else symbol.path
        return path, symbol.base_span, symbol.base_occurrence
    path = symbol.patched_path if has_side_paths else symbol.path
    return path, symbol.patched_span, symbol.patched_occurrence


def _call_matches_symbol(
    call: _CallRecord,
    source_file: _PythonFile,
    target_module: str,
    qualified_name: str,
    target_file: _PythonFile | None,
) -> bool:
    target_parts = tuple(qualified_name.split("."))
    first = call.target[0]
    if first in call.lambda_parameters:
        return False
    target_reference = (
        tuple(part for part in target_module.split(".") if part) + target_parts
    )

    if source_file.path == (target_file.path if target_file else None):
        if len(target_parts) >= 2 and call.target == ("self", target_parts[-1]):
            parent = ".".join(target_parts[:-1])
            return _nearest_enclosing_class(source_file, call.owner) == parent
        if len(target_parts) >= 2 and call.target == ("cls", target_parts[-1]):
            parent = ".".join(target_parts[:-1])
            return _nearest_enclosing_class(source_file, call.owner) == parent
        if call.target == target_parts:
            if len(target_parts) > 1 and not _target_prefixes_are_classes(
                source_file, target_parts[:-1]
            ):
                return False
            return _local_definition_is_visible(
                source_file,
                call,
                target_parts[0],
                target_parts[0],
            )
        if len(target_parts) > 1 and call.target == (target_parts[-1],):
            parent = ".".join(target_parts[:-1])
            parent_definitions = [
                item
                for item in source_file.definitions
                if item.qualified_name == parent
            ]
            if (
                len(parent_definitions) != 1
                or parent_definitions[0].kind not in {"function", "async_function"}
                or call.owner is None
                or not (
                    call.owner == parent or call.owner.startswith(parent + ".")
                )
            ):
                return False
            return _local_definition_is_visible(
                source_file,
                call,
                target_parts[-1],
                qualified_name,
            )

    bindings = _visible_import_bindings(source_file, call, first)
    if bindings is None:
        return False
    if len(bindings) != 1:
        # Shadowing and repeated incompatible imports make this name unsafe to
        # resolve statically.  A false negative is preferable to a false caller.
        return False
    module, member, style = next(iter(bindings))
    if member is not None:
        imported_reference = tuple(module.split(".")) + (member,)
        if target_reference[: len(imported_reference)] != imported_reference:
            return False
        expected = (first,) + target_reference[len(imported_reference) :]
        return call.target == expected

    module_parts = tuple(module.split("."))
    if style == "import_without_alias":
        return call.target == target_reference
    if target_reference[: len(module_parts)] != module_parts:
        return False
    expected = (first,) + target_reference[len(module_parts) :]
    return call.target == expected


def _nearest_enclosing_class(
    source_file: _PythonFile, owner: str | None
) -> str | None:
    if owner is None:
        return None
    parts = owner.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        matches = [
            item
            for item in source_file.definitions
            if item.qualified_name == candidate
        ]
        if not matches:
            continue
        if any(item.kind == "class" for item in matches):
            if len(matches) == 1 and matches[0].kind == "class":
                return candidate
            return None
    return None


def _visible_import_bindings(
    source_file: _PythonFile,
    call: _CallRecord,
    name: str,
) -> set[tuple[str, str | None, str]] | None:
    """Resolve imports using Python's lexical scopes, conservatively.

    ``None`` means another binding or a wildcard import makes the name
    ambiguous.  An empty set means no visible import was found.
    """

    for scope in _lexical_scopes(source_file, call.owner):
        other_bindings = [
            item
            for item in source_file.bindings
            if item.owner == scope and item.name == name
        ]
        import_records = [
            item
            for item in source_file.imports
            if item.owner == scope
            and (
                item.is_wildcard
                or any(binding.local_name == name for binding in item.bindings)
            )
        ]
        if other_bindings or any(item.is_wildcard for item in import_records):
            return None
        if not import_records:
            continue

        # A module-level call and a call in the same function can only use an
        # import that executes earlier in that scope.  Imports captured from an
        # outer function are retained as likely matches because determining the
        # nested function's invocation order requires control-flow analysis.
        if scope == call.owner:
            eligible = [item for item in import_records if item.start <= call.start]
        else:
            eligible = import_records
        if not eligible:
            return None
        return {
            (binding.module, binding.member, binding.import_style)
            for item in eligible
            for binding in item.bindings
            if binding.local_name == name
        }
    return set()


def _local_definition_is_visible(
    source_file: _PythonFile,
    call: _CallRecord,
    name: str,
    expected_definition: str,
) -> bool:
    """Whether ``name`` resolves unambiguously to a local target definition."""

    for scope in _lexical_scopes(source_file, call.owner):
        bindings = [
            item
            for item in source_file.bindings
            if item.owner == scope and item.name == name
        ]
        imports = [
            item
            for item in source_file.imports
            if item.owner == scope
            and (
                item.is_wildcard
                or any(binding.local_name == name for binding in item.bindings)
            )
        ]
        if not bindings and not imports:
            continue
        if imports:
            return False
        return bool(bindings) and all(
            item.kind == "definition"
            and item.definition == expected_definition
            for item in bindings
        )
    return False


def _lexical_scopes(
    source_file: _PythonFile, owner: str | None
) -> tuple[str | None, ...]:
    if owner is None:
        return (None,)
    scopes: list[str | None] = [owner]
    parts = owner.split(".")
    for end in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:end])
        definitions = [
            item
            for item in source_file.definitions
            if item.qualified_name == candidate
        ]
        # A class namespace is not an enclosing lexical scope for methods.
        if definitions and all(item.kind == "class" for item in definitions):
            continue
        scopes.append(candidate)
    scopes.append(None)
    return tuple(scopes)


def _target_prefixes_are_classes(
    source_file: _PythonFile, prefixes: tuple[str, ...]
) -> bool:
    for end in range(1, len(prefixes) + 1):
        qualified_name = ".".join(prefixes[:end])
        matches = [
            item
            for item in source_file.definitions
            if item.qualified_name == qualified_name
        ]
        if len(matches) != 1 or matches[0].kind != "class":
            return False
    return True


def _import_bindings(
    node: ast.Import | ast.ImportFrom,
    current_module: str,
    path: str,
) -> tuple[tuple[_ImportBinding, ...], bool]:
    bindings: list[_ImportBinding] = []
    wildcard = False
    if isinstance(node, ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".", 1)[0]
            bindings.append(
                _ImportBinding(
                    local_name=local_name,
                    module=alias.name,
                    member=None,
                    import_style=(
                        "import_alias" if alias.asname else "import_without_alias"
                    ),
                )
            )
    else:
        module = _resolve_from_module(current_module, path, node.level, node.module)
        for alias in node.names:
            if alias.name == "*":
                wildcard = True
                continue
            bindings.append(
                _ImportBinding(
                    local_name=alias.asname or alias.name,
                    module=module,
                    member=alias.name,
                    import_style="from_import",
                )
            )
    return tuple(bindings), wildcard


def _resolve_from_module(
    current_module: str,
    path: str,
    level: int,
    imported_module: str | None,
) -> str:
    if level == 0:
        return imported_module or ""
    current_parts = current_module.split(".") if current_module else []
    if not path.endswith("/__init__.py") and not path.endswith("__init__.py"):
        current_parts = current_parts[:-1]
    remove = max(level - 1, 0)
    if remove:
        current_parts = current_parts[:-remove] if remove <= len(current_parts) else []
    if imported_module:
        current_parts.extend(imported_module.split("."))
    return ".".join(current_parts)


def _definition_for_owner(
    source_file: _PythonFile, owner: str | None
) -> _Definition | None:
    if owner is None:
        return None
    matches = [item for item in source_file.definitions if item.qualified_name == owner]
    return matches[0] if len(matches) == 1 else None


def _add_candidate(
    candidates: _CandidatePool,
    *,
    kind: ContextKind,
    file: _PythonFile,
    start: int,
    end: int,
    symbol: str | None,
    excerpt: str,
    reason: SelectionReason,
    additional_reasons: tuple[SelectionReason, ...] = (),
) -> None:
    bounded_excerpt, truncated = _bound_excerpt(excerpt)
    key = (kind, file.path, start, end, symbol)
    candidate = candidates.items.get(key)
    if candidate is None:
        candidate = _Candidate(
            kind=kind,
            path=file.path,
            start=start,
            end=end,
            symbol=symbol,
            excerpt=bounded_excerpt,
            excerpt_truncated=truncated,
        )
        candidates.items[key] = candidate
    for selection_reason in (reason, *additional_reasons):
        current = candidate.reasons.get(selection_reason.code)
        if current is None or (
            selection_reason.weight,
            selection_reason.detail,
        ) > (
            current.weight,
            current.detail,
        ):
            candidate.reasons[selection_reason.code] = selection_reason
    while len(candidates.items) > candidates.limit:
        worst_key = max(
            candidates.items,
            key=lambda item_key: _candidate_order_key(
                candidates.items[item_key]
            ),
        )
        del candidates.items[worst_key]
        candidates.truncated = True


def _candidate_order_key(candidate: _Candidate) -> tuple[object, ...]:
    return (
        -candidate.score,
        candidate.path,
        candidate.start,
        candidate.end,
        candidate.kind.value,
        candidate.symbol or "",
    )


def _nearby_test_selection_reason(
    source_file: _PythonFile,
    definition: _Definition,
    *,
    target_leaf: str,
    target_stem: str,
) -> SelectionReason | None:
    name_tokens = set(
        definition.qualified_name.casefold().replace(".", "_").split("_")
    )
    if target_leaf.casefold() in name_tokens:
        return SelectionReason(
            "test_name_mentions_symbol",
            "test name contains the changed symbol name",
            45,
        )
    if source_file.path.rsplit("/", 1)[-1].casefold() in {
        f"test_{target_stem}.py",
        f"{target_stem}_test.py",
    }:
        return SelectionReason(
            "test_file_matches_module",
            "test filename matches the changed module filename",
            35,
        )
    return None


def _changed_symbol_order_key(symbol: ChangedSymbol) -> tuple[object, ...]:
    def span_key(span: SourceSpan | None) -> tuple[int, int, int]:
        if span is None:
            return (0, 0, 0)
        return (1, span.start_line, span.end_line)

    return (
        symbol.path,
        symbol.qualified_name,
        symbol.kind,
        symbol.base_path is not None,
        symbol.base_path or "",
        symbol.patched_path is not None,
        symbol.patched_path or "",
        symbol.base_occurrence is not None,
        symbol.base_occurrence or 0,
        symbol.patched_occurrence is not None,
        symbol.patched_occurrence or 0,
        span_key(symbol.base_span),
        span_key(symbol.patched_span),
        symbol.removed_lines,
        symbol.added_lines,
        symbol.revision_match,
    )


def _source_excerpt(lines: tuple[str, ...], start: int, end: int) -> str:
    return "\n".join(lines[max(start - 1, 0) : max(end, start)])


def _bound_excerpt(excerpt: str) -> tuple[str, bool]:
    if len(excerpt) <= _MAX_EXCERPT_CHARS:
        return excerpt, False
    return excerpt[:_MAX_EXCERPT_CHARS] + "\n…", True


def _signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> str:
    if isinstance(node, ast.ClassDef):
        components = [ast.unparse(item) for item in node.bases]
        components.extend(ast.unparse(item) for item in node.keywords)
        suffix = f"({', '.join(components)})" if components else ""
        return f"class {node.name}{suffix}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}"


def _function_arguments(arguments: ast.arguments) -> tuple[ast.arg, ...]:
    items = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    if arguments.vararg is not None:
        items.append(arguments.vararg)
    if arguments.kwarg is not None:
        items.append(arguments.kwarg)
    return tuple(items)


def _docstring_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> ast.Expr | None:
    if not node.body:
        return None
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            return first
    return None


def _dotted_expression(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _dotted_expression(node.value)
        if parent:
            return (*parent, node.attr)
    return ()


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    if parts and parts[0] in _SOURCE_ROOT_NAMES and len(parts) > 1:
        parts = parts[1:]
    if not parts:
        return ""
    parts[-1] = parts[-1][:-3] if parts[-1].endswith(".py") else parts[-1]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_safe_relative_path(path: str) -> bool:
    if not path or "\x00" in path:
        return False
    candidate = PurePosixPath(path)
    return not candidate.is_absolute() and ".." not in candidate.parts


def _is_test_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    name = candidate.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or any(part in {"test", "tests"} for part in candidate.parts[:-1])
    )


def _is_test_name(qualified_name: str) -> bool:
    return qualified_name.rsplit(".", 1)[-1].startswith("test_")
