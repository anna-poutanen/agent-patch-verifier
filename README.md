# Agent Patch Verifier

Agent Patch Verifier (`apv`) is an evidence-first patch falsifier for Python
projects. It executes the same requirement-linked pytest case against a base
commit and a proposed patch, then reports only conclusions supported by both
the observed transition and the stated provenance of the requirement.

The core rule is deliberately conservative: a base-pass/patched-fail result is
called a supported regression only when it checks a strongly sourced
preservation requirement. A run with no finding is not proof that the patch is
correct.

The current development release is `0.3.0`.

## Current status

This repository contains the first deterministic vertical slice:

- isolated detached Git worktrees for base and patched source;
- exact base-commit and patched-tree identities, plus hashes for the patch,
  issue, evidence plan, repository context, results, candidate tests, and
  pytest harness and human-readable report;
- Python diff-to-symbol mapping;
- deterministic, separately labeled base and patched static context for changed
  symbols, including signatures, docstrings, imports, likely callers, nearby
  tests, selection reasons, diagnostics, analysis limitations, and explicit
  resource bounds;
- deterministic, review-only requirement drafting from conservative issue
  criteria, relevant base-side pytest assertions, and behavioral docstrings,
  with exact source spans, hashes, confidence, rationale, and intent;
- a reviewable requirement and candidate-test ledger;
- fresh worktrees for every case and repeat, plus shell-free pytest execution
  with timeouts and bounded output capture;
- an import-origin guard for modules directly imported by candidate tests when
  a matching module exists in a common repository source layout;
- JUnit-backed separation of assertion failures from collection, fixture, skip,
  and generated-test errors;
- conservative outcome classification;
- Markdown and JSON evidence bundles with exact commands and captured output;
  and
- hash-checked bundle reproduction with drift detection.

Requirement drafting is now available, but verification still consumes an
explicit evidence plan. A draft never becomes an oracle automatically and is
never runs as a test. Conflict review, explicit acceptance, and model-driven
test generation remain future work; this boundary prevents generated or
heuristic text from being treated as ground truth.

## Install for development

```bash
python -m pip install -e .
python -m pytest -q
```

Python 3.11–3.13, Git, and pytest are required. Target-project dependencies
must already be available in the selected Python environment. Check the
installed release with `apv --version`.

## Draft requirement candidates

Create a review queue from an issue and an exact textual patch:

```bash
apv draft-requirements \
  --repo /path/to/project \
  --base HEAD \
  --patch proposed.diff \
  --issue issue.md \
  --output requirements-draft.json
```

The command materializes isolated base and patched worktrees, maps the diff to
changed Python symbols, collects base-side context, and emits only unaccepted
candidate requirements. Issue candidates are labeled `requested`; relevant
base assertions and behavioral docstrings are labeled `preservation`. Every
candidate remains `requires_review` and records an exact excerpt, source span,
SHA-256, confidence, and rationale.

The parser deliberately skips prose under headings such as current or broken
behavior and does not treat skipped, conditionally skipped, or expected-failure
pytest assertions as preservation contracts. Diagnostics and limitations from
the base-context pass are carried into the draft so an incomplete search is
visible to reviewers.

The JSON envelope binds the draft to the base commit, patched Git tree, patch
bytes, issue bytes, base context, changed symbols, verifier version, and Python
runtime used for static parsing. The command refuses to overwrite an existing
output file. It does not resolve
conflicts, create an evidence plan, or generate or run candidate tests. Git
worktree materialization can invoke repository-configured checkout hooks and
clean/smudge filters, so drafting is only suitable for trusted repositories.
See
[`requirements-draft.schema.json`](src/patch_verifier/schemas/requirements-draft.schema.json)
for the portable format.

## Evidence plan

An evidence plan links each pytest node to exactly one requirement:

```json
{
  "schema_version": 1,
  "requirements": [
    {
      "id": "REQ-ZERO",
      "statement": "Explicit zero returns zero.",
      "kind": "requested",
      "provenance": "explicit",
      "source": "issue:1",
      "excerpt": "Explicit zero must return zero."
    },
    {
      "id": "REQ-THREE",
      "statement": "A supplied three remains three.",
      "kind": "preservation",
      "provenance": "preexisting",
      "source": "tests/test_defaults.py:18",
      "excerpt": "assert resolve(3) == 3"
    }
  ],
  "cases": [
    {
      "id": "CASE-ZERO",
      "requirement_id": "REQ-ZERO",
      "test_file": "candidate.py",
      "node": "test_zero"
    },
    {
      "id": "CASE-THREE",
      "requirement_id": "REQ-THREE",
      "test_file": "candidate.py",
      "node": "test_three"
    }
  ]
}
```

`test_file` must remain inside the plan directory. Explicit requirements cite
`issue:<line>` and are verified against the supplied issue text at that line.
Pre-existing requirements cite `path:<line>` and their excerpts are verified
against the recorded base revision before tests run. Each selected test node
must contain exactly one Python `assert` statement and may map to only one
requirement. Candidate files must be self-contained: sibling helper files or
data from the plan directory are not copied, though they may import code from
the target repository. A plan is limited to 200 requirements, 50 cases, 5 MiB
of JSON, 2 MiB per candidate or cited base-source file, and 20 MiB across each
set of unique files. The complete format is in
[`evidence-plan.schema.json`](src/patch_verifier/schemas/evidence-plan.schema.json),
which is also included in installed wheels.

## Run a verification

```bash
apv verify \
  --repo /path/to/project \
  --base HEAD \
  --patch proposed.diff \
  --issue issue.md \
  --evidence evidence.json \
  --repeat 3
```

Unless `--output` is supplied, bundles are stored under the repository's Git
metadata directory so verification does not add files to the working tree.

By default, the runner invokes the current interpreter with a focused node,
`--runxfail`, and a private JUnit output path. Conceptually:

```text
python -m pytest -q --tb=short <generated-test-node> --runxfail --junitxml=...
```

Use `--test-command` to supply another shell-like command prefix. It is parsed
into an argument vector; a shell is not executed. Supply the pytest executable
and options only—`apv` appends the generated node and JUnit arguments. Exactly
one matching, non-skipped test item must run or the case is inconclusive.

Exit codes:

- `0`: no supported counterexample was found;
- `1`: at least one supported regression or incomplete fix was found, including
  a mixed run with additional inconclusive cases; and
- `2`: the request was invalid, a replay drifted, or execution was inconclusive
  and no supported failure was found.

For `reproduce`, exit `1` means the supported patch failure was reproduced; it
is not a replay error.

## Evidence bundle

Each successful run contains:

```text
manifest.json
results.json
context.json
report.md
patch.diff
issue.md
evidence-plan.json
tests/
harness/
```

`results.json` retains every attempt's exit code, duration, stdout, and stderr.
`context.json` keeps base and patched snapshots separate so patch-added context
cannot masquerade as pre-existing evidence. Its portable format is documented
by [`context.schema.json`](src/patch_verifier/schemas/context.schema.json).
Repository context is review material and does not affect deterministic finding
classification.
The normalized evidence plan refers only to tests inside the bundle. To repeat
the hash-checked base/patched execution:

```bash
apv reproduce /path/to/run-directory
```

If the original repository moved, use:

```bash
apv reproduce /path/to/run-directory --repo /new/path/to/project
```

If the recorded interpreter no longer exists, `--test-command` can override
it. The replay is then explicitly reported as non-exact:

```bash
apv reproduce /path/to/run-directory \
  --test-command "python -m pytest -q --tb=short"
```

Reproduction verifies the recorded hashes for the patch, issue, evidence plan,
repository context, results, candidate tests, pytest harness, and report before
running. It also requires the installed verifier's harness to match the
recorded harness and reports if freshly collected context differs. These
hashes detect mutation relative to `manifest.json`; they do not authenticate
the manifest itself. A bundle also does not contain the source repository or
dependencies, interpreter build, operating system, pytest plugins, services, or
external state. Replay is therefore not hermetic: retain the recorded base
commit, expect environment drift to remain possible, and reproduce only bundles
from trusted sources. Legacy manifest-v1 bundles may omit context and therefore
skip context drift comparison. Legacy replay is accepted only when its repeat,
case count, artifact sizes, and output-capture limit fit the current safety and
execution contract; incompatible v1 bundles fail preflight with an explicit
error instead of running under silently different limits.

The current artifact versions are independent: release `0.3.0` writes manifest
schema v2, context schema v1, evidence-plan schema v1, and requirement-draft
schema v1. Consumers should use each artifact's own `schema_version`, not the
package version, for compatibility decisions. The packaged contracts are
[`manifest.schema.json`](src/patch_verifier/schemas/manifest.schema.json),
[`context.schema.json`](src/patch_verifier/schemas/context.schema.json), and
[`evidence-plan.schema.json`](src/patch_verifier/schemas/evidence-plan.schema.json),
plus
[`requirements-draft.schema.json`](src/patch_verifier/schemas/requirements-draft.schema.json).

## Scope and safety

The current implementation supports committed bases, UTF-8 textual Git
patches, deterministic pytest cases, and trusted local inputs. The repository,
proposed patch, candidate tests, and reproduced bundle must all be trusted. It
does not install target-project dependencies, start services, support binary
patches, or provide a security sandbox. Tests run with the permissions and
network access of the invoking user. A timed-out process that deliberately
escapes its process group may survive; container or OS-level isolation is
required before accepting untrusted code. The import-origin guard is a
correctness check, not an isolation boundary: it covers direct absolute imports
in candidate tests and the repository root plus common `src`, `lib`, `python`,
and `source` layouts, but not every indirect or dynamic import pattern.
Creating the isolated Git worktrees may itself run trusted repository checkout
hooks or filters before any verifier subprocess begins.

Static context is heuristic and may be truncated or absent. Collection skips
symlinks, selected build/cache directories, unsafe or unparseable files, files
over 2 MB, and work beyond fixed file, source-byte, directory-entry, symbol,
diagnostic, and ranked-item budgets. Exact diagnostics are retained in
`context.json`; likely callers are not a complete call graph.

Verification also bounds every local and bundled artifact read. Patches are
limited to 20 MiB, issues to 2 MiB, repeat to five, and a run to 50 total
case-repetitions. Each subprocess timeout may be at most one hour. These are
safety and predictability limits, not claims that inputs near the limits will
be fast on every project.

Requirement drafting separately limits issue input to 1,000,000 UTF-8 bytes and
20,000 lines, relevant base source to 2,000,000 bytes per file and 20,000,000
bytes in aggregate, and output to 500 candidates and 200 diagnostics. Static
selection can omit valid contracts and conservative issue parsing can miss
valid prose; a draft is a review aid, not a completeness or correctness claim.

Evidence bundles can contain issue text, candidate code, source excerpts,
commands, filesystem paths, and captured test output. Review them before
sharing: project data or secrets printed by tests can be present even though
captured streams are size-bounded. Fresh Git worktrees isolate repository files,
not writes to the user home directory, network, databases, or other external
systems.

See [`docs/roadmap.md`](docs/roadmap.md) for the next implementation stages.

