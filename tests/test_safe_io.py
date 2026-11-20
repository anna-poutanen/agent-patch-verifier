from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

import patch_verifier.safe_io as safe_io
from patch_verifier.safe_io import (
    BoundedFileReadError,
    read_bounded_regular_file,
)


def test_reads_one_bounded_regular_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(b"evidence\n")

    assert read_bounded_regular_file(artifact, max_bytes=9) == b"evidence\n"
    with pytest.raises(BoundedFileReadError, match="8-byte limit"):
        read_bounded_regular_file(artifact, max_bytes=8)


def test_root_confines_relative_artifact_paths(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    artifact = root / "nested" / "artifact.txt"
    artifact.parent.mkdir()
    artifact.write_bytes(b"evidence")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")

    assert (
        read_bounded_regular_file(artifact, root=root, max_bytes=100)
        == b"evidence"
    )
    with pytest.raises(BoundedFileReadError, match="escapes the allowed root"):
        read_bounded_regular_file(outside, root=root, max_bytes=100)


def test_symbolic_link_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    link = tmp_path / "artifact.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(BoundedFileReadError, match="symbolic link"):
        read_bounded_regular_file(link, max_bytes=100)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "artifact"
    os.mkfifo(fifo)

    started = time.perf_counter()
    with pytest.raises(BoundedFileReadError, match="not a regular file"):
        read_bounded_regular_file(fifo, max_bytes=100)
    assert time.perf_counter() - started < 1.0


def test_nested_ancestor_replacement_cannot_redirect_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "original"
    expected_parent = original / "inner"
    expected_parent.mkdir(parents=True)
    artifact = expected_parent / "artifact.txt"
    artifact.write_bytes(b"expected")

    alternate = tmp_path / "alternate"
    (alternate / "inner").mkdir(parents=True)
    (alternate / "inner" / "artifact.txt").write_bytes(b"attacker")
    displaced = tmp_path / "displaced"
    original_open = safe_io.os.open
    replaced = False

    def replace_ancestor(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal replaced
        if path == "original" and not replaced:
            replaced = True
            original.rename(displaced)
            alternate.rename(original)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "open", replace_ancestor)

    with pytest.raises(BoundedFileReadError, match="directory changed"):
        read_bounded_regular_file(artifact, max_bytes=100)
    assert replaced


def test_missing_secure_primitives_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(b"evidence")
    monkeypatch.setattr(safe_io, "_HAS_DESCRIPTOR_TRAVERSAL", False)

    with pytest.raises(BoundedFileReadError, match="unavailable on this platform"):
        read_bounded_regular_file(artifact, max_bytes=100)
