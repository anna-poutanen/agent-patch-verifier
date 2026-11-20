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

## Milestone 2: requirement extraction and review — complete

- Extract review-only candidate requirements from conservative issue criteria,
  relevant base documentation, and statically selected pre-existing pytest
  assertions.
- Preserve exact source spans, excerpts, hashes, confidence, rationale, and
  requested-versus-preservation intent.
- Bind portable draft artifacts to the exact base commit, patched tree, patch,
  issue, repository context, changed symbols, and tool version.
- Keep every candidate in `requires_review`; drafting cannot execute tests or
  promote a candidate to a strong requirement.
- Exercise misleading prose, dynamic calls, duplicate definitions,
  parametrization, unrelated tests, and resource limits in a benchmark corpus.
- Detect and present potential conflicts between issue intent and existing
  contracts without claiming a semantic contradiction that static analysis
  cannot establish.
- Record whether conflict analysis was truncated and how many candidates and
  pairs it considered so a partial search cannot look complete.
- Bind an editable decision ledger to the exact draft bytes, keep the complete
  candidate evidence beside every decision, and require a human rationale for
  every acceptance, edit, or rejection.
- Recompute advisory conflict metadata and reject pending decisions, drift,
  duplicate or unknown candidates, unstable requirement IDs, and source-kind
  or provenance escalation during finalization.
- Produce a reviewed requirement set whose records can be copied directly into
  an evidence plan before candidate tests are generated.

## Milestone 3a: review-only test proposal ingestion — complete

- Define a provider-neutral generator protocol and a strict structured response
  envelope for requested, preservation, boundary, invalid-input, caller-level,
  and state-sequence proposals.
- Bind every proposal set to the exact finalized reviewed-requirements bytes and
  preserve the generator adapter, optional model label, exact response hash,
  canonical response hash, and reviewed-requirement coverage.
- Constrain every proposal to an accepted requirement ID, a safe portable
  candidate path, a uniquely named statically parsed test node, and exactly one
  Python `assert` in the complete candidate source.
- Derive stable case IDs plus exact source and assertion spans and hashes rather
  than trusting those fields from a generator.
- Keep the artifact and every proposal in `requires_review`; importing a
  response neither writes candidate files nor imports, collects, executes, or
  converts generated code into an evidence plan.

## Milestone 3b: reviewed proposal materialization — complete

- Define explicit human decisions for accepting, editing, or rejecting test
  proposals without weakening their reviewed-requirement bindings.
- Bind the review to the exact proposal artifact, reviewed requirements,
  issue, generator response, and immutable proposal previews; require a
  rationale for every terminal decision.
- Revalidate edited source without importing it, derive a new content-based
  case ID, and retain the original proposal ID and every decision in the final
  audit artifact.
- Convert only accepted and edited proposals into EvidencePlan v2 cases whose
  `test_sha256` values bind exact reviewed candidate bytes.
- Materialize candidates, issue, plan, and review audit into a new staging
  directory; inventory every file and publish the completion manifest last.
- Authenticate the complete staging inventory and cross-check its plan,
  review, issue, candidate paths, and hashes before an explicit
  `verify-materialized` handoff invokes the existing dual-execution verifier.
- Keep proposal preparation, finalization, and materialization inert: none
  imports, collects, or executes candidate Python.
- Preserve strict legacy v1 loading for reviewed requirements, test proposals,
  and EvidencePlans while requiring the v2 issue and candidate-hash bindings
  for new reviewed materializations.

## Milestone 3c: isolated collection preflight and repair — deferred

- Design an OS- or container-level isolation boundary for pytest collection
  and import preflight. Pytest collection can execute module-level, plugin, and
  fixture code, so same-user static validation is not a security boundary.
- After that boundary exists, dynamically validate collection and allow a
  small, bounded model-assisted repair loop for collection and import errors.
- Preserve the explicit human decision and exact-byte audit when a repair
  changes candidate source; a model must not silently revise accepted code.
- Decide how a future evidence-bundle schema should carry the staging review
  audit. Bundle v2 currently retains effective tests and requirements but not
  the reviewed-proposal or materialization artifacts.

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

