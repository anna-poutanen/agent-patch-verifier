"""Shared validation helpers for Git patch inputs."""

from __future__ import annotations


def is_binary_patch(patch_bytes: bytes) -> bool:
    """Return whether bytes contain NUL data or Git's binary-patch record.

    The Git marker is metadata only when it occupies an unprefixed line.  A
    textual hunk may legitimately add source code containing the same phrase;
    those lines start with ``+`` and must remain valid text patches.
    """

    if b"\0" in patch_bytes:
        return True
    return any(
        line.removesuffix(b"\r") == b"GIT binary patch"
        for line in patch_bytes.split(b"\n")
    )


__all__ = ["is_binary_patch"]
