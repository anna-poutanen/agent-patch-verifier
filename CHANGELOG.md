# Changelog

## 0.3.0 — 2026-11-19

- Add deterministic, review-only requirement candidate extraction from issue
  acceptance criteria, relevant base-side pytest assertions, and behavioral
  docstrings.
- Preserve exact source spans, UTF-8 excerpt hashes, intent, confidence,
  rationale, and stable candidate identifiers without promoting candidates to
  executable or accepted requirements.
- Add `apv draft-requirements` and a versioned, provenance-bound JSON artifact
  containing the base commit, patched tree, patch, issue, context, and changed
  symbol identities, plus the Python runtime used for static parsing.
- Bound issue, source, AST, assertion, candidate, diagnostic, and output work;
  fail closed on unsafe paths, symlinks, malformed encodings, and ambiguous
  context.
- Exclude current/broken-behavior prose and skipped or expected-failure tests
  from candidate contracts, and propagate base-context omissions into the
  review artifact.
- Add adversarial benchmark fixtures for misleading prose, preservation
  contracts, parametrization, duplicate definitions, dynamic calls, and
  resource exhaustion.
- Bound timeout pipe cleanup across stdout and stderr so an escaped descendant
  cannot multiply cleanup delays by retaining both descriptors.

## 0.2.0 — 2026-11-18

- Add deterministic base and patched repository-context collection for changed
  Python symbols.
- Record ranked signatures, docstrings, imports, likely callers, nearby tests,
  diagnostics, and static-analysis limitations in a versioned context artifact.
- Add hard resource budgets for discovery, source parsing, changed symbols,
  diagnostics, retained context items, evidence plans, candidate tests, cited
  sources, execution repetitions, and bundle artifacts.
- Improve rename, copy, duplicate-definition, decorator, and deep-AST change
  mapping.
- Pin replay execution to the exact validated patch, issue, plan, candidate,
  harness, context, result, and report bytes.
- Publish versioned JSON Schemas for manifest, context, and evidence-plan
  artifacts, and safely bound repository-derived Markdown in reports.
- Add Python 3.11–3.13 CI, installed-wheel checks, and CLI version reporting.

## 0.1.0 — 2026-11-18

- Introduce the deterministic verification vertical slice: isolated Git
  worktrees, requirement-linked pytest execution, conservative classification,
  evidence bundles, and bundle replay.
