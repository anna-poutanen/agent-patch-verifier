# Changelog

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
