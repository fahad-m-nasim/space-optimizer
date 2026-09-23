from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import write
from space_optimizer.scanner import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_REPARSE_POINT,
    ScanCancelled,
    Scanner,
    file_disk_size,
    is_link_like,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only behaviour")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only behaviour")


def test_sizes_counts_and_structure(tree: Path) -> None:
    root = Scanner().scan(tree)

    assert set(root.children) == {"big", "small", "with space"}
    assert root.file_count == 5
    assert root.own_count == 1  # root.txt

    big = root.children["big"]
    assert big.file_count == 2
    assert big.own_count == 1
    assert big.size >= 400_000  # allocated size is at least the data written
    assert big.size >= big.own_size + big.children["inner"].size
    assert root.size >= big.size + root.children["small"].size
    assert root.last_modified >= big.last_modified > 0


def test_top_files_are_largest_first(tree: Path) -> None:
    root = Scanner().scan(tree)
    for i in range(20):
        write(tree / "many" / f"f{i:02}.bin", 1_000 * (i + 1))
    many = Scanner().scan(tree).children["many"]
    sizes = [size for size, *_ in sorted(many.top_files, reverse=True)]
    assert len(sizes) == 15  # capped per folder
    assert sizes == sorted(sizes, reverse=True)
    assert "f19.bin" in {name for _, name, *_ in many.top_files}
    assert root.children["big"].top_files[0][1] == "a.bin"


def test_one_filesystem_keeps_ordinary_subfolders(tree: Path) -> None:
    # Regression guard: on Windows, directory listings report st_dev = 0.
    root = Scanner(one_filesystem=True).scan(tree)
    assert set(root.children) == {"big", "small", "with space"}


def test_symlinked_folder_is_not_followed(tree: Path, tmp_path: Path) -> None:
    outside = write(tmp_path / "outside" / "huge.bin", 2_000_000).parent
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    root = Scanner().scan(tree)
    assert "link" not in root.children
    assert root.size < 2_000_000


@windows_only
def test_junction_is_not_followed(tree: Path, tmp_path: Path) -> None:
    import _winapi

    outside = write(tmp_path / "outside" / "huge.bin", 2_000_000).parent
    _winapi.CreateJunction(str(outside), str(tree / "junction"))
    root = Scanner().scan(tree)
    assert "junction" not in root.children
    assert root.size < 2_000_000
    assert root.file_count == 5  # the junction isn't counted as a file either


@posix_only
def test_hard_links_are_counted_once(tree: Path) -> None:
    before = Scanner().scan(tree).size
    os.link(tree / "big" / "a.bin", tree / "small" / "a-hardlink.bin")
    after = Scanner().scan(tree).size
    assert after - before < 50_000  # the 100 KB file isn't counted twice


@posix_only
def test_sparse_file_counts_allocated_space(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse" / "disk.img"
    sparse.parent.mkdir()
    with open(sparse, "wb") as f:
        f.seek(50_000_000)
        f.write(b"x")
    root = Scanner().scan(tmp_path / "sparse")
    assert sparse.stat().st_size > 50_000_000
    assert root.size < 5_000_000


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read everything")
def test_unreadable_folder_is_reported_not_fatal(tree: Path) -> None:
    locked = tree / "locked"
    write(locked / "secret.bin", 1_000)
    locked.chmod(0)
    try:
        root = Scanner().scan(tree)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert root.errors >= 1
    assert root.children["locked"].errors >= 1
    assert root.children["big"].file_count == 2  # the rest still scanned


def test_cancel_stops_scan(tree: Path) -> None:
    scanner = Scanner()
    scanner.cancelled = True
    with pytest.raises(ScanCancelled):
        scanner.scan(tree)


def test_not_a_directory(tree: Path) -> None:
    with pytest.raises(NotADirectoryError):
        Scanner().scan(tree / "root.txt")


# ---- Windows-specific size / link rules, tested with fake stat results so they run everywhere ----

def fake_stat(**kw) -> SimpleNamespace:
    return SimpleNamespace(**{"st_mode": stat.S_IFREG | 0o644, "st_size": 10_000_000, **kw})


def test_cloud_only_placeholder_counts_as_zero() -> None:
    st = fake_stat(st_file_attributes=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    assert file_disk_size(st, "x") == 0


def test_size_without_st_blocks_falls_back_to_logical() -> None:
    assert file_disk_size(fake_stat(st_file_attributes=0), "x") == 10_000_000


def test_st_blocks_used_when_available() -> None:
    assert file_disk_size(fake_stat(st_blocks=8), "x") == 4096


def test_junction_is_link_like_but_onedrive_folder_is_not() -> None:
    folder = stat.S_IFDIR | 0o755
    junction = fake_stat(st_mode=folder, st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT, st_reparse_tag=0xA0000003)
    onedrive = fake_stat(st_mode=folder, st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT, st_reparse_tag=0x9000601A)
    plain = fake_stat(st_mode=folder, st_file_attributes=0)
    assert is_link_like(junction, "x")
    assert not is_link_like(onedrive, "x")
    assert not is_link_like(plain, "x")
    assert is_link_like(fake_stat(st_mode=stat.S_IFLNK | 0o777), "x")
