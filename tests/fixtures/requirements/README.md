# Requirement-extraction benchmark fixtures

Each directory is a tiny base-revision repository. `case.json` identifies the
changed Python symbol used to collect a BASE `RepositoryContext`; `issue.md` is
the external issue description supplied to requirement extraction.

The corpus intentionally mixes support that can be cited exactly with text or
test code that must not be promoted into a requirement:

- `mixed_contracts` combines one explicit requested behavior, two established
  preservation assertions, and an unrelated test module.
- `misleading_issue` separates a normative acceptance criterion from quoted
  speculation, implementation ideas, and a non-goal.
- `parametrized` exercises a direct assertion whose operands are pytest
  parameters rather than concrete values in the assertion itself.
- `shadowing` includes an overwritten test definition and a locally shadowed
  import alongside two live, statically attributable assertions.
- `dynamic_assertions` pairs one direct assertion with `getattr` and
  helper-dispatched assertions that are not statically attributable.
- `resource_bounds` is a seed repository used to generate a large, deterministic
  set of nearby assertions without storing a large fixture in Git.

Every extracted candidate remains review-only. These fixtures test candidate
drafting and provenance, not automatic acceptance.
