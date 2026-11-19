# Empty amount handling

Customer quote: "The parser must be cursed; maybe every invalid token should
silently become zero." This is diagnostic color, not a requested contract.

## Acceptance criteria

- Empty input must raise `ValueError`.

## Non-goals

- Do not change the handling of whitespace-only input.

Implementation idea: catch every exception and return `0`. This idea is not an
acceptance criterion and would contradict the requested behavior above.
