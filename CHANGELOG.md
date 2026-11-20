# Changelog

## 0.5.0 — 2026-11-19

- Add a provider-neutral test-proposal protocol and the
  `apv import-test-proposals` command for importing a bounded structured
  generator response against exact finalized reviewed-requirements bytes.
- Normalize requested, preservation, boundary, invalid-input, caller-level,
  and state-sequence proposals into a deterministic review artifact with
  stable case IDs, exact source and assertion hashes, requirement previews,
  generator-response bindings, and explicit coverage gaps.
- Require every proposal to reference a reviewed requirement, use a safe
  portable Python path, name a unique statically parsed test node, and contain
  exactly one Python `assert` in the complete candidate file.
- Keep the artifact and every proposal in `requires_review`. Proposal import
  does not write candidate files, import generated code, invoke pytest
  collection, execute tests, or convert proposals into an evidence plan.
- Deliberately defer dynamic collection: unreviewed Python cannot be
  authenticated before it may execute in the same-user local process. A later
  milestone must define an isolated review and materialization boundary.
- Add strict schema and semantic validation, duplicate-key rejection,
  deterministic encoding, atomic no-overwrite output, exact-input race
  checks, and resource limits for generator responses and proposal sources.

## 0.4.0 — 2026-11-19

- License the project under Apache License 2.0 and publish SPDX license
  metadata in built distributions.
- Add deterministic, review-only tension hints for narrowly matched numeric,
  string, and polarity differences between requested behavior and base-side
  preservation candidates. Hints remain advisory and explicitly do not claim
  a contradiction.
- Add `apv prepare-review` to create an exact-draft-bound human decision
  ledger with immutable candidate previews, stable EvidencePlan-compatible
  requirement IDs, conflict-search accounting, and pending decisions.
- Add `apv finalize-review` to require an explicit accept, edit, or reject
  decision with rationale for every candidate before producing reviewed
  requirements and a complete audit trail.
- Recompute conflict metadata during finalization, reject draft or preview
  drift, preserve exact source excerpts, and prevent edits from escalating or
  changing source-backed kind and provenance.
- Add strict JSON Schemas, duplicate-key rejection, bounded input and output,
  atomic no-overwrite writes, and adversarial state-machine tests for both
  review artifacts.

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
