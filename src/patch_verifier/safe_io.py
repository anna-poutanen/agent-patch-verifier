"""Bounded, race-resistant reads for local verification artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import stat


class BoundedFileReadError(OSError):
    """A local artifact could not be frozen as one bounded regular file."""


_REQUIRED_FLAGS = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
_HAS_DESCRIPTOR_TRAVERSAL = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and all(hasattr(os, name) for name in _REQUIRED_FLAGS)
)


def read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    root: Path | None = None,
) -> bytes:
    """Return exact bytes from one non-link regular file within a byte limit.

    On platforms with descriptor-relative POSIX primitives, the directory
    chain is held open, each component is opened without following links, and
    the final file uses ``O_NONBLOCK``. This makes inspection and use one
    descriptor-bound operation: a concurrent FIFO or symlink replacement is
    rejected rather than blocking indefinitely or redirecting the read.

    ``root`` additionally confines the path lexically and through no-follow
    descriptor traversal. It is intended for artifact bundles whose relative
    layout is part of their authenticated format.

    Platforms without the complete descriptor-relative, no-follow, and
    non-blocking primitive set fail closed before opening the artifact.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise TypeError("max_bytes must be an integer")
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")

    requested = Path(path)
    checkpoint: tuple[int, int, int] | None
    if root is None:
        requested_absolute = Path(os.path.abspath(requested))
        try:
            canonical_parent = requested_absolute.parent.resolve(strict=True)
            parent_status = canonical_parent.stat(follow_symlinks=False)
        except (OSError, TypeError, ValueError) as exc:
            raise BoundedFileReadError(
                f"cannot inspect artifact parent {requested_absolute.parent}: {exc}"
            ) from exc
        absolute = canonical_parent / requested_absolute.name
        directory = Path(canonical_parent.anchor)
        parts = (*canonical_parent.parts[1:], requested_absolute.name)
        checkpoint = (
            len(canonical_parent.parts) - 1,
            parent_status.st_dev,
            parent_status.st_ino,
        )
    else:
        requested_root = Path(os.path.abspath(root))
        requested_absolute = (
            Path(os.path.abspath(requested))
            if requested.is_absolute()
            else requested_root / requested
        )
        try:
            relative = requested_absolute.relative_to(requested_root)
        except ValueError as exc:
            raise BoundedFileReadError(
                f"path escapes the allowed root {requested_root}: {requested}"
            ) from exc
        try:
            canonical_root = requested_root.resolve(strict=True)
            root_status = canonical_root.stat(follow_symlinks=False)
        except (OSError, TypeError, ValueError) as exc:
            raise BoundedFileReadError(
                f"cannot inspect artifact root {requested_root}: {exc}"
            ) from exc
        absolute = canonical_root.joinpath(*relative.parts)
        directory = Path(canonical_root.anchor)
        root_parts = canonical_root.parts[1:]
        parts = (*root_parts, *relative.parts)
        checkpoint = (
            len(root_parts),
            root_status.st_dev,
            root_status.st_ino,
        )

    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BoundedFileReadError(f"unsafe artifact path: {requested}")

    if _HAS_DESCRIPTOR_TRAVERSAL:
        return _read_with_descriptors(
            directory,
            parts,
            display_path=absolute,
            max_bytes=max_bytes,
            directory_checkpoint=checkpoint,
        )
    raise BoundedFileReadError(
        "race-resistant regular-file reads are unavailable on this platform"
    )


def _read_with_descriptors(
    directory: Path,
    parts: tuple[str, ...],
    *,
    display_path: Path,
    max_bytes: int,
    directory_checkpoint: tuple[int, int, int] | None,
) -> bytes:
    common_flags = getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | common_flags
    )
    file_flags = (
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | common_flags
    )
    directory_descriptors: list[int] = []
    descriptor = -1
    try:
        current = os.open(directory, directory_flags)
        directory_descriptors.append(current)
        _verify_directory_checkpoint(
            current,
            traversed_components=0,
            checkpoint=directory_checkpoint,
            display_path=display_path,
        )
        for traversed_components, component in enumerate(parts[:-1], start=1):
            current = os.open(component, directory_flags, dir_fd=current)
            directory_descriptors.append(current)
            _verify_directory_checkpoint(
                current,
                traversed_components=traversed_components,
                checkpoint=directory_checkpoint,
                display_path=display_path,
            )

        before = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise BoundedFileReadError(
                f"artifact must not be a symbolic link: {display_path}"
            )
        if not stat.S_ISREG(before.st_mode):
            raise BoundedFileReadError(
                f"artifact is not a regular file: {display_path}"
            )
        if before.st_size > max_bytes:
            raise BoundedFileReadError(
                f"artifact exceeds the {max_bytes}-byte limit: {display_path}"
            )

        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise BoundedFileReadError(
                f"artifact is not a regular file: {display_path}"
            )
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BoundedFileReadError(
                f"artifact changed while it was being opened: {display_path}"
            )
        if opened.st_size > max_bytes:
            raise BoundedFileReadError(
                f"artifact exceeds the {max_bytes}-byte limit: {display_path}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            document = stream.read(max_bytes + 1)
    except BoundedFileReadError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise BoundedFileReadError(
            f"cannot open artifact without following symbolic links "
            f"{display_path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for directory_descriptor in reversed(directory_descriptors):
            try:
                os.close(directory_descriptor)
            except OSError:
                pass

    if len(document) > max_bytes:
        raise BoundedFileReadError(
            f"artifact exceeds the {max_bytes}-byte limit: {display_path}"
        )
    return document


def _verify_directory_checkpoint(
    descriptor: int,
    *,
    traversed_components: int,
    checkpoint: tuple[int, int, int] | None,
    display_path: Path,
) -> None:
    if checkpoint is None or traversed_components != checkpoint[0]:
        return
    status = os.fstat(descriptor)
    if (status.st_dev, status.st_ino) != checkpoint[1:]:
        raise BoundedFileReadError(
            f"artifact directory changed while it was being opened: {display_path}"
        )


__all__ = ["BoundedFileReadError", "read_bounded_regular_file"]
