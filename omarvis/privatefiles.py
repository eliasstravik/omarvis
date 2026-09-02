"""Descriptor-based access to Omarvis' private files (config, secrets).

Every read and write goes through a directory descriptor and ``O_NOFOLLOW``
opens, and the file is validated by ``fstat`` before its contents are
trusted, so a pre-planted symlink, FIFO, device node, hard link, or oversized
file at a predictable pathname cannot redirect writes, leak credentials, or
block the daemon. Writes land in a fresh ``O_EXCL`` temporary file that is
renamed over the target, so readers only ever see complete 0600 files.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

MAX_SECRET_BYTES = 4096
MAX_CONFIG_BYTES = 1024 * 1024
_OPEN_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class PrivateFileError(RuntimeError):
    """The file or directory is not the plain private file Omarvis expects."""


@contextmanager
def private_dir(
    path: Path, *, create: bool = True, mode: int = 0o700, private: bool = True
) -> Iterator[int]:
    """Yield a descriptor for ``path`` after proving it is our own directory.

    With ``private`` the directory is also made 0700; without it (shared
    assets such as fonts) only ownership and type are enforced.
    """
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(path, mode)
        except FileExistsError:
            pass
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise PrivateFileError(f"{path} is not a private directory: {error}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise PrivateFileError(f"{path} is not a directory")
        if info.st_uid != os.geteuid():
            raise PrivateFileError(f"{path} is not owned by the current user")
        if private and stat.S_IMODE(info.st_mode) & 0o077:
            try:
                os.fchmod(fd, mode)
            except OSError as error:
                raise PrivateFileError(f"{path} cannot be made private: {error}") from error
        yield fd
    finally:
        os.close(fd)


def _validate(fd: int, label: str, *, limit: int, private: bool) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PrivateFileError(f"{label} is not a regular file")
    if info.st_uid != os.geteuid():
        raise PrivateFileError(f"{label} is not owned by the current user")
    if info.st_nlink != 1:
        raise PrivateFileError(f"{label} has extra hard links")
    if info.st_size > limit:
        raise PrivateFileError(f"{label} is larger than {limit} bytes")
    if private and stat.S_IMODE(info.st_mode) & 0o077:
        os.fchmod(fd, 0o600)
    return info


@contextmanager
def open_private_file(
    dir_fd: int, name: str, *, limit: int, private: bool = True
) -> Iterator[BinaryIO]:
    """Open ``name`` inside ``dir_fd`` for reading once it passes validation."""
    try:
        fd = os.open(name, os.O_RDONLY | _OPEN_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        raise PrivateFileError(f"{name} cannot be opened safely: {error}") from error
    try:
        _validate(fd, name, limit=limit, private=private)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        yield handle


def read_private_file(
    dir_fd: int, name: str, *, limit: int, private: bool = True
) -> bytes | None:
    """Return the file's bytes, ``None`` if absent, or raise on anything odd."""
    try:
        fd = os.open(name, os.O_RDONLY | _OPEN_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PrivateFileError(f"{name} cannot be opened safely: {error}") from error
    try:
        _validate(fd, name, limit=limit, private=private)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PrivateFileError(f"{name} is larger than {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_private_file(dir_fd: int, name: str, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace ``name`` inside ``dir_fd`` with a fresh private file."""
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_FLAGS,
        mode,
        dir_fd=dir_fd,
    )
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def read_private_path(path: Path, *, limit: int, private: bool = True) -> bytes | None:
    """Read ``path`` through its parent directory descriptor."""
    try:
        with private_dir(path.parent, create=False, private=private) as dir_fd:
            return read_private_file(dir_fd, path.name, limit=limit, private=private)
    except FileNotFoundError:
        return None


def write_private_path(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    with private_dir(path.parent) as dir_fd:
        write_private_file(dir_fd, path.name, data, mode=mode)
