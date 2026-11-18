# Agent Patch Verifier

Agent Patch Verifier (`apv`) is an evidence-first patch falsifier for Python
projects. It executes the same requirement-linked pytest case against a base
commit and a proposed patch, then reports only conclusions supported by both
the observed transition and the stated provenance of the requirement.

The core rule is deliberately conservative: a base-pass/patched-fail result is
called a supported regression only when it checks a strongly sourced
preservation requirement. A run with no finding is not proof that the patch is
correct.

## Current status

This repository contains the first deterministic vertical slice:

- isolated detached Git worktrees for base and patched source;
- exact base-commit and patched-tree identities, plus hashes for the patch,
  issue, evidence plan, results, candidate tests, and pytest harness;
- Python diff-to-symbol mapping;
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

Automated requirement extraction and model-driven test generation are the next
milestone. For now, `apv` consumes an explicit evidence plan. Keeping this
boundary manual first makes the execution and classification layer testable
without treating model output as ground truth.

## Install for development

```bash
python -m pip install -e .
python -m pytest -q
```

Python 3.11 or newer, Git, and pytest are required. Target-project dependencies
must already be available in the selected Python environment.

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
the target repository. The complete format is in
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
- `1`: at least one supported regression or incomplete fix was found; and
- `2`: the request was invalid, execution was inconclusive, or a replay drifted.

## Evidence bundle

Each successful run contains:

```text
manifest.json
results.json
report.md
patch.diff
issue.md
evidence-plan.json
tests/
harness/
```

`results.json` retains every attempt's exit code, duration, stdout, and stderr.
The normalized evidence plan refers only to tests inside the bundle. To repeat
the complete base/patched run:

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
results, candidate tests, and pytest harness before running. It also requires
the installed verifier's harness to match the recorded harness. These hashes
detect mutation relative to `manifest.json`; they do not authenticate the
manifest itself. A bundle also does not contain the source repository or
dependencies, so retain the recorded base commit and reproduce only bundles
from trusted sources.

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

See [`docs/roadmap.md`](docs/roadmap.md) for the next implementation stages.

