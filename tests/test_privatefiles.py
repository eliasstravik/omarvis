from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from omarvis.privatefiles import (
    PrivateFileError,
    private_dir,
    read_private_path,
    write_private_path,
)


def test_write_then_read_round_trips_as_private_file(tmp_path: Path) -> None:
    target = tmp_path / "state" / "secret"

    write_private_path(target, b"value\n")

    assert read_private_path(target, limit=100) == b"value\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert [p.name for p in target.parent.iterdir()] == ["secret"]


def test_missing_file_and_missing_directory_read_as_absent(tmp_path: Path) -> None:
    assert read_private_path(tmp_path / "absent", limit=10) is None
    assert read_private_path(tmp_path / "nodir" / "absent", limit=10) is None


def test_symlink_is_never_followed_on_read(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("leak")
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(PrivateFileError):
        read_private_path(link, limit=100)


def test_write_replaces_a_planted_symlink_instead_of_following_it(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("untouched")
    link = tmp_path / "config"
    link.symlink_to(elsewhere)

    write_private_path(link, b"new")

    assert elsewhere.read_text() == "untouched"
    assert not link.is_symlink()
    assert link.read_bytes() == b"new"


def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(PrivateFileError, match="not a regular file"):
        read_private_path(fifo, limit=100)


def test_oversized_and_hard_linked_files_are_rejected(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.write_bytes(b"x" * 11)
    with pytest.raises(PrivateFileError, match="larger"):
        read_private_path(big, limit=10)

    linked = tmp_path / "linked"
    linked.write_bytes(b"ok")
    os.link(linked, tmp_path / "twin")
    with pytest.raises(PrivateFileError, match="hard links"):
        read_private_path(linked, limit=10)


def test_private_read_tightens_loose_modes(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.write_bytes(b"k")
    loose.chmod(0o644)

    assert read_private_path(loose, limit=10) == b"k"
    assert stat.S_IMODE(loose.stat().st_mode) == 0o600


def test_non_private_read_leaves_mode_alone(tmp_path: Path) -> None:
    shared = tmp_path / "config.json"
    shared.write_bytes(b"{}")
    shared.chmod(0o644)

    assert read_private_path(shared, limit=10, private=False) == b"{}"
    assert stat.S_IMODE(shared.stat().st_mode) == 0o644


def test_symlinked_directory_is_rejected(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    with pytest.raises(PrivateFileError):
        with private_dir(link_dir):
            pass
