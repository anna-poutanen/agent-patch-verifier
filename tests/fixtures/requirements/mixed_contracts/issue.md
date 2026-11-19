# Preserve explicit zero

The current fallback expression loses an explicitly supplied zero.

## Acceptance criteria

- An explicit zero must return zero.

A developer wondered whether every falsey object should become five; that is
not requested. One implementation sketch was to replace the function with
`return 0`, but implementation sketches are not behavioral requirements.
