# Agent Patch Verifier

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Agent Patch Verifier (`apv`) is an evidence-first patch falsifier for Python
projects. It executes the same requirement-linked pytest case against a base
commit and a proposed patch, then reports only conclusions supported by both
the observed transition and the stated provenance of the requirement.

The core rule is deliberately conservative: a base-pass/patched-fail result is
called a supported regression only when it checks a strongly sourced
preservation requirement. A run with no finding is not proof that the patch is
correct.

The current development release is `0.5.0`.

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
- conservative potential-tension hints between requested and preservation
  candidates, with explicit search accounting and no claim that a conflict is
  semantically established;
- an exact-draft-bound human review ledger that requires an accept, edit, or
  reject decision with rationale for every candidate before it can emit
  reviewed requirements;
- provider-neutral import of structured test proposals into an exact-input-
  bound, deterministic `requires_review` artifact with stable case IDs,
  assertion provenance, and explicit reviewed-requirement coverage gaps;
- a separately reviewable evidence-plan format for requirements and candidate
  tests;
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

Requirement drafting, explicit human review, and provider-neutral test-proposal
import are available, but verification still consumes an evidence plan with
independently reviewed pytest cases. A draft never becomes an oracle
automatically and never runs as a test. Reviewed requirements can be supplied
to a model adapter, and APV can normalize its structured response, but every
proposal remains `requires_review`. APV does not yet collect, execute, or
convert those proposals into an evidence plan.

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
output file. It does not establish correctness, create an evidence plan, or
generate or run candidate tests. Git
worktree materialization can invoke repository-configured checkout hooks and
clean/smudge filters, so drafting is only suitable for trusted repositories.
See
[`requirements-draft.schema.json`](src/patch_verifier/schemas/requirements-draft.schema.json)
for the portable format.

## Review requirement candidates

Turn a draft into a human-editable decision ledger:

```bash
apv prepare-review \
  --draft requirements-draft.json \
  --output requirement-review.json
```

The ledger repeats each candidate and its exact source evidence beside an
initial `pending` decision. It also gives each candidate a deterministic
`req-...` identifier that fits the evidence-plan format. Reviewers leave the
candidate preview and identifier unchanged, then choose one action:

- `accept`: keep `replacement` as `null` and add a non-empty `rationale`;
- `edit`: add a rationale and a `replacement` containing the revised
  `statement` plus the candidate's unchanged source-backed `kind` and
  `provenance`; or
- `reject`: keep `replacement` as `null` and explain the rejection.

For an issue candidate, the source-backed pair is `requested` / `explicit`.
For a pytest assertion or behavioral docstring, it is `preservation` /
`preexisting`. A different pair must be rejected as a new unsupported claim,
not smuggled into the original candidate as an edit.

Preparation also compares requested issue candidates with base-side
preservation candidates using narrow numeric-literal, string-literal, and
polarity signals. These are potential-tension hints for a reviewer, not proof
that either source is correct or that the two requirements contradict one
another. The ledger records candidate counts, compared pairs, hint count, and
whether the bounded search was truncated.

After every decision is terminal, finalize the ledger:

```bash
apv finalize-review \
  --draft requirements-draft.json \
  --review requirement-review.json \
  --output reviewed-requirements.json
```

Finalization checks the exact draft hash, immutable candidate previews,
deterministic IDs, decision ordering, and complete accept/edit/reject state. It
recomputes the advisory conflict analysis, refuses an edited kind or provenance
that is not supported by the original source, and never overwrites an existing
output. Neither review command imports target code, generates tests, invokes
pytest, or decides whether the patch is correct.

The resulting `requirements` array uses the same records as an evidence plan.
It is not a complete evidence plan: a later step must add separately reviewed
pytest `cases` that each reference one accepted requirement. The finalized
artifact retains the exact draft and review hashes plus all decisions and
conflict metadata as an audit trail. See
[`requirement-review.schema.json`](src/patch_verifier/schemas/requirement-review.schema.json)
and
[`reviewed-requirements.schema.json`](src/patch_verifier/schemas/reviewed-requirements.schema.json)
for the portable formats.

## Import test proposals

APV accepts a small provider-neutral JSON envelope from an external model or
other generator. The response has exactly `schema_version` and `proposals` at
the top level. Each proposal supplies six fields; APV derives the review
marker, hashes, assertion coordinates, requirement preview, and stable case ID
instead of trusting a generator to provide them:

```json
{
  "schema_version": 1,
  "proposals": [
    {
      "requirement_id": "req-0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab",
      "category": "requested",
      "test_file": "generated_tests/test_defaults.py",
      "node": "test_explicit_zero",
      "rationale": "Exercises the reviewed explicit-zero requirement.",
      "source": "from project.defaults import resolve\n\ndef test_explicit_zero():\n    assert resolve(0) == 0\n"
    }
  ]
}
```

Replace `requirement_id` with an ID from the finalized reviewed-requirements
artifact. `category` is one of `requested`, `preservation`, `boundary`,
`invalid_input`, `caller_level`, or `state_sequence`. Requested and
preservation categories must point to a requirement of the corresponding
kind. The candidate path must be a safe, relative, portable `.py` path, every
proposal must use a distinct path, and `node` must identify one unique function
or class-qualified method in the supplied source. The complete candidate file
must contain exactly one Python `assert`.

Import the response without invoking its provider again:

```bash
apv import-test-proposals \
  --reviewed reviewed-requirements.json \
  --response generator-response.json \
  --adapter example-adapter \
  --model example-model \
  --output test-proposals.json
```

The adapter and optional model are opaque audit labels, not trust signals. The
output binds the exact reviewed-requirements and response bytes, retains the
complete reviewed requirement beside the proposal's sole assertion, records
covered and uncovered requirement IDs, and marks both the artifact and every
proposal `requires_review`. See
[`test-proposals.schema.json`](src/patch_verifier/schemas/test-proposals.schema.json)
for the normalized artifact format.

Import performs static UTF-8, Python-syntax, node, path, assertion-count, hash,
and schema checks only. It does not write candidate files outside the JSON
artifact, import generated modules, invoke pytest collection, execute target or
test code, or convert proposals into an evidence plan. Dynamic collection is
deliberately deferred: pytest collection can execute module-level Python, and
unreviewed code cannot be authenticated before that execution in a same-user
local process. A later milestone needs an explicit human-review and isolation
boundary before collection or repair can be safe enough to add.

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

The current artifact versions are independent: release `0.5.0` writes manifest
schema v2 plus context, evidence-plan, requirement-draft, requirement-review,
reviewed-requirements, and test-proposals schema v1 artifacts. Consumers should
use each artifact's own `schema_version`, not the package version, for
compatibility decisions. The packaged contracts are
[`manifest.schema.json`](src/patch_verifier/schemas/manifest.schema.json),
[`context.schema.json`](src/patch_verifier/schemas/context.schema.json), and
[`evidence-plan.schema.json`](src/patch_verifier/schemas/evidence-plan.schema.json),
plus
[`requirements-draft.schema.json`](src/patch_verifier/schemas/requirements-draft.schema.json),
[`requirement-review.schema.json`](src/patch_verifier/schemas/requirement-review.schema.json),
and
[`reviewed-requirements.schema.json`](src/patch_verifier/schemas/reviewed-requirements.schema.json),
plus
[`test-proposals.schema.json`](src/patch_verifier/schemas/test-proposals.schema.json).

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

Review preparation accepts a requirement draft of at most 10 MiB, compares at
most 20,000 requested/preservation pairs, retains at most 200 advisory hints,
and writes a ledger of at most 16 MiB. Finalization accepts a review ledger of
at most 16 MiB, emits at most 200 requirements in a 24 MiB artifact, and fails
if conflict analysis was changed rather than merely reviewed. Truncation is
recorded, but a non-truncated heuristic search still cannot prove that every
semantic tension was found.

The SHA-256 links between draft, review, and finalized output detect drift; they
do not authenticate who authored or approved an artifact. Accepting or editing
a requirement is a human attestation that the cited excerpt supports it, not a
correctness conclusion established by APV.

Test-proposal import accepts at most 50 proposals from a generator response of
at most 24 MiB. Each complete source is limited to 2 MiB and all proposal
sources together to 20 MiB; the normalized artifact is limited to 24 MiB.
These checks bound parsing and preserve exact provenance, but static parsing
does not establish that an assertion correctly expresses its linked
requirement. Generated source remains untrusted and `requires_review` even
after a successful import.

Evidence bundles can contain issue text, candidate code, source excerpts,
commands, filesystem paths, and captured test output. Review them before
sharing: project data or secrets printed by tests can be present even though
captured streams are size-bounded. Fresh Git worktrees isolate repository files,
not writes to the user home directory, network, databases, or other external
systems.

## License

Licensed under the [Apache License 2.0](LICENSE).

See [`docs/roadmap.md`](docs/roadmap.md) for the next implementation stages.

