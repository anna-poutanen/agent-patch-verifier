"""JSON Schema validation for portable requirement-draft artifacts."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
import hashlib
from importlib.resources import files
import inspect
import json
from pathlib import PurePosixPath
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .requirements import looks_behavioral_docstring


def validate_requirement_draft_artifact(payload: object) -> None:
    """Validate a requirement-draft-v1 artifact.

    The extractor returns a deliberately review-only core draft.  The CLI adds
    the exact repository, revision, patch, issue, and change-scope provenance
    validated here so a reviewer can reproduce where every candidate came
    from.
    """

    errors = tuple(_validator().iter_errors(payload))
    if errors:
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
        raise ValueError(f"{_format_path(path)}: {message}")

    assert isinstance(payload, Mapping)
    _validate_semantics(payload)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    resource = (
        files("patch_verifier")
        .joinpath("schemas")
        .joinpath("requirements-draft.schema.json")
    )
    try:
        schema = json.loads(resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        raise RuntimeError("packaged requirement-draft-v1 schema is invalid") from exc
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
        choices = ", ".join(repr(item) for item in error.validator_value)
        return path, f"expected one of {choices}"
    if error.validator == "pattern":
        return path, "value does not match the required pattern"
    if error.validator == "minimum":
        return path, f"must be at least {error.validator_value}"
    if error.validator == "maximum":
        return path, f"must be at most {error.validator_value}"
    if error.validator == "minLength":
        return path, f"must contain at least {error.validator_value} characters"
    if error.validator == "maxLength":
        return path, f"must contain at most {error.validator_value} characters"
    if error.validator == "maxItems":
        return path, f"must contain at most {error.validator_value} items"
    return path, " ".join(error.message.split())[:240]


def _format_path(path: Sequence[object]) -> str:
    result = "requirement_draft"
    for item in path:
        if isinstance(item, int):
            result += f"[{item}]"
        else:
            result += f".{item}"
    return result


def _validate_semantics(payload: Mapping[str, object]) -> None:
    """Check correlations and canonical values JSON Schema cannot express."""

    inputs = payload["inputs"]
    draft = payload["draft"]
    changed_symbols = payload["changed_symbols"]
    assert isinstance(inputs, Mapping)
    assert isinstance(draft, Mapping)
    assert isinstance(changed_symbols, list)

    _validate_changed_symbols(changed_symbols)

    candidates = draft["candidates"]
    diagnostics = draft["diagnostics"]
    assert isinstance(candidates, list)
    assert isinstance(diagnostics, list)
    issue_source = inputs["issue_source"]
    assert isinstance(issue_source, str)

    seen_ids: set[str] = set()
    order_keys: list[tuple[object, ...]] = []
    kind_order = {"issue": 0, "pytest_assertion": 1, "docstring": 2}
    expected_intents = {
        "issue": "requested",
        "pytest_assertion": "preservation",
        "docstring": "preservation",
    }
    for index, candidate in enumerate(candidates):
        assert isinstance(candidate, Mapping)
        identifier = candidate["id"]
        source_kind = candidate["source_kind"]
        statement = candidate["statement"]
        intent = candidate["intent"]
        source = candidate["source"]
        assert isinstance(identifier, str)
        assert isinstance(source_kind, str)
        assert isinstance(statement, str)
        assert isinstance(intent, str)
        assert isinstance(source, Mapping)

        if identifier in seen_ids:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].id: "
                "candidate id is duplicated"
            )
        seen_ids.add(identifier)

        expected_intent = expected_intents[source_kind]
        if intent != expected_intent:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].intent: "
                f"{source_kind} candidates must be {expected_intent}"
            )
        path = source["path"]
        excerpt = source["excerpt"]
        start_line = source["start_line"]
        end_line = source["end_line"]
        start_column = source["start_column"]
        end_column = source["end_column"]
        digest = source["sha256"]
        assert isinstance(path, str)
        assert isinstance(excerpt, str)
        assert isinstance(start_line, int)
        assert isinstance(end_line, int)
        assert isinstance(start_column, int)
        assert isinstance(end_column, int)
        assert isinstance(digest, str)

        if any(character in path for character in "\r\n\0"):
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source.path: "
                "path must be a single line without NUL characters"
            )

        if end_line < start_line:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source: "
                "end_line precedes start_line"
            )
        line_breaks = tuple(re.finditer(r"\r\n|\r|\n", excerpt))
        if len(line_breaks) != end_line - start_line:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source.excerpt: "
                "line count does not match the recorded span"
            )
        excerpt_bytes = excerpt.encode("utf-8")
        if len(excerpt_bytes) > 16_000:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source.excerpt: "
                "UTF-8 form exceeds the 16000-byte limit"
            )
        if start_line == end_line and end_column - start_column != len(excerpt_bytes):
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source: "
                "single-line columns do not delimit the excerpt UTF-8 bytes"
            )
        if line_breaks:
            final_line = excerpt[line_breaks[-1].end() :]
            if len(final_line.encode("utf-8")) != end_column:
                raise ValueError(
                    f"requirement_draft.draft.candidates[{index}].source: "
                    "end_column does not delimit the final excerpt line"
                )
        observed_digest = hashlib.sha256(excerpt_bytes).hexdigest()
        if digest != observed_digest:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source.sha256: "
                "digest does not match the exact excerpt"
            )

        if source_kind == "issue":
            if path != issue_source:
                raise ValueError(
                    f"requirement_draft.draft.candidates[{index}].source.path: "
                    "issue candidate does not cite inputs.issue_source"
                )
        else:
            pure_path = PurePosixPath(path)
            if (
                pure_path.is_absolute()
                or any(part in {"", ".", ".."} for part in pure_path.parts)
            ):
                raise ValueError(
                    f"requirement_draft.draft.candidates[{index}].source.path: "
                    "base source must be a safe repository-relative path"
                )

        expected_statement = canonical_candidate_statement(
            source_kind,
            excerpt,
            error_path=f"requirement_draft.draft.candidates[{index}].source.excerpt",
        )
        if statement != expected_statement:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].statement: "
                "statement is not the deterministic transform of its exact "
                "source excerpt"
            )
        if source_kind == "docstring" and not looks_behavioral_docstring(
            statement
        ):
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].statement: "
                "statement does not satisfy behavioral docstring selection"
            )

        if source_kind == "docstring" and not _docstring_matches_changed_symbol(
            changed_symbols,
            path=path,
            start_line=start_line,
            end_line=end_line,
        ):
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].source: "
                "docstring source does not fall within a base-side changed-"
                "symbol span"
            )

        identifier_payload = {
            "intent": intent,
            "source": dict(source),
            "source_kind": source_kind,
            "statement": statement,
        }
        canonical = json.dumps(
            identifier_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_identifier = "cand-" + hashlib.sha256(canonical).hexdigest()
        if identifier != expected_identifier:
            raise ValueError(
                f"requirement_draft.draft.candidates[{index}].id: "
                "identifier does not match canonical candidate provenance"
            )

        order_keys.append(
            (
                kind_order[source_kind],
                path,
                start_line,
                start_column,
                end_line,
                end_column,
                statement,
                identifier,
            )
        )

    if order_keys != sorted(order_keys):
        raise ValueError(
            "requirement_draft.draft.candidates: candidates are not in "
            "deterministic source order"
        )

    diagnostic_keys = []
    truncated = draft["truncated"]
    assert isinstance(truncated, bool)
    for index, diagnostic in enumerate(diagnostics):
        assert isinstance(diagnostic, Mapping)
        line = diagnostic["line"]
        code = diagnostic["code"]
        assert isinstance(code, str)
        if not truncated and _diagnostic_requires_truncation(code):
            raise ValueError(
                "requirement_draft.draft.truncated: must be true when "
                f"diagnostic {code!r} reports truncated extraction"
            )
        diagnostic_keys.append(
            (
                diagnostic["source"],
                0 if line is None else line,
                code,
                diagnostic["message"],
            )
        )
    if diagnostic_keys != sorted(diagnostic_keys):
        raise ValueError(
            "requirement_draft.draft.diagnostics: diagnostics are not in "
            "deterministic source order"
        )


def _diagnostic_requires_truncation(code: str) -> bool:
    """Return whether an extractor diagnostic is emitted with truncation."""

    return code.startswith("context_") or code in {
        "aggregate_source_limit_reached",
        "assertion_limit_reached",
        "ast_node_limit_reached",
        "candidate_excerpt_too_large",
        "candidate_limit_reached",
        "candidate_scan_limit_reached",
        "candidate_statement_too_large",
        "diagnostic_limit_reached",
        "relevant_file_limit_reached",
        "relevant_item_limit_reached",
        "source_file_too_large",
        "source_token_limit_reached",
        "unterminated_html_comment",
        "unterminated_markdown_fence",
    }


def _validate_changed_symbols(changed_symbols: list[object]) -> None:
    """Validate correlations guaranteed by change-analysis construction."""

    two_sided = {"unique", "unique_identity", "path_change_unique_identity"}
    base_only = {"base_only", "path_change_base_only"}
    patched_only = {"patched_only", "path_change_patched_only"}
    ambiguous = {
        "ambiguous_duplicate_unpaired",
        "path_change_ambiguous_duplicate_unpaired",
    }
    module_matches = {"module", "path_change_module"}

    for index, raw_symbol in enumerate(changed_symbols):
        assert isinstance(raw_symbol, Mapping)
        symbol = raw_symbol
        kind = symbol["kind"]
        qualified_name = symbol["qualified_name"]
        revision_match = symbol["revision_match"]
        path = symbol["path"]
        assert isinstance(kind, str)
        assert isinstance(qualified_name, str)
        assert isinstance(revision_match, str)
        assert isinstance(path, str)

        side_state: dict[str, tuple[object, object, object, list[object]]] = {}
        for side, lines_name in (
            ("base", "removed_lines"),
            ("patched", "added_lines"),
        ):
            path_value = symbol[f"{side}_path"]
            span = symbol[f"{side}_span"]
            occurrence = symbol[f"{side}_occurrence"]
            lines = symbol[lines_name]
            assert path_value is None or isinstance(path_value, str)
            assert span is None or isinstance(span, Mapping)
            assert occurrence is None or isinstance(occurrence, int)
            assert isinstance(lines, list)

            if lines != sorted(lines):
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}].{lines_name}: "
                    "line coordinates must be in strictly increasing order"
                )
            if (span is None) != (occurrence is None):
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}]: {side}_span "
                    f"and {side}_occurrence must either both be present or "
                    "both be null"
                )
            if path_value is None and (span is not None or lines):
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}]: {side}_path "
                    f"is required when {side} provenance is present"
                )

            if span is not None:
                start_line = span["start_line"]
                end_line = span["end_line"]
                assert isinstance(start_line, int)
                assert isinstance(end_line, int)
                if end_line < start_line:
                    raise ValueError(
                        f"requirement_draft.changed_symbols[{index}]."
                        f"{side}_span: end_line precedes start_line"
                    )
                if any(
                    not start_line <= int(line) <= end_line for line in lines
                ):
                    raise ValueError(
                        f"requirement_draft.changed_symbols[{index}]."
                        f"{lines_name}: line coordinate falls outside "
                        f"{side}_span"
                    )
            elif kind != "module" and lines:
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}].{lines_name}: "
                    f"non-module changed lines require a {side}_span"
                )

            side_state[side] = (path_value, span, occurrence, lines)

        base_path, base_span, base_occurrence, removed_lines = side_state["base"]
        patched_path, patched_span, patched_occurrence, added_lines = side_state[
            "patched"
        ]
        expected_path = patched_path or base_path
        ambiguous_base_side = revision_match in ambiguous and patched_path is None
        if expected_path is None or (
            not ambiguous_base_side and path != expected_path
        ):
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].path: path must "
                "equal patched_path when present, otherwise base_path"
            )

        has_base = base_span is not None
        has_patched = patched_span is not None
        state = (has_base, has_patched)
        if revision_match in module_matches:
            if kind != "module" or state != (False, False):
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}].revision_match: "
                    f"{revision_match} requires module scope without symbol spans"
                )
        elif kind == "module":
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                "module scope requires a module revision_match"
            )
        elif revision_match in two_sided and state != (True, True):
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                f"{revision_match} requires base and patched symbol spans"
            )
        elif revision_match in base_only and state != (True, False):
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                f"{revision_match} requires only a base symbol span"
            )
        elif revision_match in patched_only and state != (False, True):
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                f"{revision_match} requires only a patched symbol span"
            )
        elif revision_match in ambiguous and state not in {
            (True, False),
            (False, True),
        }:
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                f"{revision_match} requires exactly one symbol span"
            )

        if kind == "module":
            if qualified_name != "<module>" or any(
                value is not None for value in (base_occurrence, patched_occurrence)
            ):
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}]: module scope "
                    "must use <module> and null occurrences"
                )
        elif qualified_name == "<module>":
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].qualified_name: "
                "non-module symbols cannot use <module>"
            )

        if revision_match in ambiguous:
            opposite_path = patched_path if has_base else base_path
            if opposite_path is not None:
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}]: an ambiguous "
                    "unpaired symbol must not claim the opposite revision path"
                )
        elif any(
            occurrence not in (None, 1)
            for occurrence in (base_occurrence, patched_occurrence)
        ):
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}]: uniquely matched "
                "symbols must use occurrence 1"
            )

        if revision_match.startswith("path_change_"):
            if removed_lines or added_lines:
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}].revision_match: "
                    "path-change-only symbols cannot contain changed line "
                    "coordinates"
                )
            if base_path == patched_path:
                raise ValueError(
                    f"requirement_draft.changed_symbols[{index}].revision_match: "
                    "path-change-only symbols require different revision paths"
                )
        elif not removed_lines and not added_lines:
            raise ValueError(
                f"requirement_draft.changed_symbols[{index}].revision_match: "
                "non-path-change symbols require at least one changed line "
                "coordinate"
            )


def canonical_candidate_statement(
    source_kind: str,
    excerpt: str,
    *,
    error_path: str,
) -> str:
    """Return the deterministic candidate wording for one exact excerpt.

    Draft and review validation share this transform so a portable review
    ledger cannot substitute unrelated wording and merely recompute its IDs.
    ``error_path`` keeps failures anchored to the artifact being validated.
    """

    if source_kind == "issue":
        return excerpt

    try:
        tree = ast.parse(excerpt)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise ValueError(
            f"{error_path}: "
            f"{source_kind} excerpt is not valid static Python syntax"
        ) from exc
    if len(tree.body) != 1:
        raise ValueError(
            f"{error_path}: "
            f"{source_kind} excerpt must contain exactly one Python statement"
        )

    node = tree.body[0]
    if source_kind == "pytest_assertion":
        if not isinstance(node, ast.Assert):
            raise ValueError(
                f"{error_path}: "
                "pytest_assertion excerpt must be an assert statement"
            )
        expression = ast.get_source_segment(excerpt, node.test)
        if not expression:
            raise ValueError(
                f"{error_path}: "
                "pytest assertion expression is unavailable"
            )
        return "Preserve the behavior asserted by the base test: " + expression.strip()

    if (
        not isinstance(node, ast.Expr)
        or not isinstance(node.value, ast.Constant)
        or not isinstance(node.value.value, str)
    ):
        raise ValueError(
            f"{error_path}: "
            "docstring excerpt must be one literal string expression"
        )
    cleaned = inspect.cleandoc(node.value.value)
    return inspect.cleandoc(cleaned).strip()


def _docstring_matches_changed_symbol(
    changed_symbols: list[object],
    *,
    path: str,
    start_line: int,
    end_line: int,
) -> bool:
    for raw_symbol in changed_symbols:
        assert isinstance(raw_symbol, Mapping)
        if raw_symbol["base_path"] != path:
            continue
        span = raw_symbol["base_span"]
        if not isinstance(span, Mapping):
            continue
        span_start = span["start_line"]
        span_end = span["end_line"]
        assert isinstance(span_start, int)
        assert isinstance(span_end, int)
        if span_start <= start_line and end_line <= span_end:
            return True
    return False
