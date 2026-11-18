# Implementation roadmap

The project is being built around a deterministic core with a narrow model
boundary. Model output may propose requirements and tests; it never decides
whether an observed transition proves a regression.

## Milestone 0: deterministic evidence loop — complete

- Capture a committed base and textual patch without modifying the caller's
  checkout.
- Materialize isolated base and patched worktrees.
- Verify source excerpts for strong requirements.
- Inject identical test bytes into both revisions.
- Repeat tests, preserve raw output, and detect unstable outcomes.
- Check the origins of modules directly imported by candidate tests when a
  matching module exists in the repository root or a common source layout.
- Apply the conservative classification matrix.
- Write an integrity-checked evidence bundle that can be replayed against the
  retained source repository and recorded base commit.

## Milestone 1: repository context collection

- Expand changed-symbol analysis to signatures, docstrings, imports, likely
  callers, and nearby tests.
- Rank context by changed symbol and record why every file was selected.
- Detect unsupported dynamic patterns rather than implying a complete call
  graph.
- Expand import-origin checks beyond direct candidate-test imports and common
  source layouts, including indirect and dynamic import patterns.

## Milestone 2: requirement extraction

- Extract candidate requirements from the supplied issue, base documentation,
  and pre-existing tests.
- Preserve exact source spans and excerpts.
- Detect conflicts between issue intent and existing contracts.
- Require human review or explicit acceptance before inferred claims can be
  promoted to strong evidence.

## Milestone 3: model-driven candidate generation

- Define a provider-neutral generator protocol and structured response schema.
- Generate requested, preservation, boundary, invalid-input, caller-level, and
  state-sequence cases.
- Validate syntax and pytest collection before dual execution.
- Allow a small, bounded repair loop for collection and import errors.
- Keep generated assertions constrained to accepted requirement IDs.

## Milestone 4: minimization and evaluation

- Minimize supported counterexamples without changing their classification.
- Build a benchmark containing plausible wrong patches and known-good negative
  controls across at least five projects.
- Track supported-finding precision, counterexample recall, reproducibility,
  provenance accuracy, runtime, and model cost.
- Compare against a diff-only coding-agent review and available Testora or
  PatchGuru baselines where their supported scope overlaps.

## Deferred production work

- Container-level security isolation
- Automated dependency and environment construction
- Network, database, browser, and distributed-system tests
- GitHub or CI integration
- Additional languages and test frameworks
- Hosted multi-user execution and observability

