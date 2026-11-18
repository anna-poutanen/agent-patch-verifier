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
- Write a bundle of hash-checked recorded artifacts that can be replayed against
  the retained source repository and recorded base commit.
- Bound plan size, candidate and cited-source bytes, execution repetitions,
  captured output, and every bundle artifact read.

## Milestone 1: repository context collection — complete

- Collect signatures, docstrings, imports, likely callers, and nearby tests for
  changed symbols in both revisions.
- Keep base and patched snapshots separate, rank every selected item, and
  record selection reasons, diagnostics, and static-analysis limitations.
- Hash and validate the portable context artifact, include a bounded context
  section in the report, and compare context during reproduction.
- Bound repository discovery, source bytes, changed-symbol work, diagnostics,
  and retained context items with explicit truncation diagnostics.
- Avoid treating context as classification evidence or implying a complete
  call graph.

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
- Compare against a diff-only coding-agent review and available
  [ADAPT](https://arxiv.org/html/2611.16114v1),
  [Testora](https://arxiv.org/html/2503.18597), or
  [PatchGuru](https://arxiv.org/html/2602.05270v2) baselines where their
  supported scope overlaps.
- Expand import-origin checks beyond direct candidate-test imports and common
  source layouts, including indirect and dynamic import patterns.

## Deferred production work

- Container-level security isolation
- Automated dependency and environment construction
- Network, database, browser, and distributed-system tests
- GitHub or CI integration
- Additional languages and test frameworks
- Hosted multi-user execution and observability

