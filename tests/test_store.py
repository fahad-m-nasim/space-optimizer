from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from conftest import write
from space_optimizer.scanner import Scanner
from space_optimizer.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db" / "scans.db")


def save(store: Store, tree: Path) -> int:
    return store.save_scan(tree.resolve(), Scanner().scan(tree), elapsed=0.1)


def row_count(store: Store, table: str) -> int:
    with closing(sqlite3.connect(store.path)) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_save_and_read_back(store: Store, tree: Path) -> None:
    scan_id = save(store, tree)
    top = store.folder(scan_id, "")
    assert top is not None
    assert {c["name"] for c in top["children"]} == {"big", "small", "with space"}
    assert top["folder"]["files"] == 5
    assert top["own"]["files"] == 1
    assert Path(top["path"]) == tree.resolve()

    big = store.folder(scan_id, "big")
    assert big["folder"]["subfolders"] == 1
    assert [f["name"] for f in big["top_files"]] == ["a.bin"]
    assert Path(big["top_files"][0]["path"]) == tree.resolve() / "big" / "a.bin"
    assert Path(big["children"][0]["path"]) == tree.resolve() / "big" / "inner"

    [listed] = store.list_scans()
    assert listed["id"] == scan_id and listed["files"] == 5
    assert store.folder(scan_id, "does/not/exist") is None


def test_remove_file_updates_totals(store: Store, tree: Path) -> None:
    scan_id = save(store, tree)
    before = store.folder(scan_id, "big")["folder"]
    root_before = store.folder(scan_id, "")["folder"]
    file_size = store.folder(scan_id, "big")["top_files"][0]["size"]

    store.remove_file(scan_id, "big", "a.bin")

    after = store.folder(scan_id, "big")
    assert after["top_files"] == []
    assert after["own"]["files"] == 0
    assert after["folder"]["size"] == before["size"] - file_size
    assert after["folder"]["files"] == before["files"] - 1
    assert store.folder(scan_id, "")["folder"]["size"] == root_before["size"] - file_size


def test_remove_folder_drops_subtree_and_updates_ancestors(store: Store, tree: Path) -> None:
    scan_id = save(store, tree)
    root_before = store.folder(scan_id, "")["folder"]
    big = store.folder(scan_id, "big")["folder"]

    store.remove_folder(scan_id, "big")

    root_after = store.folder(scan_id, "")
    assert {c["name"] for c in root_after["children"]} == {"small", "with space"}
    assert root_after["folder"]["size"] == root_before["size"] - big["size"]
    assert root_after["folder"]["files"] == root_before["files"] - big["files"]
    assert store.folder(scan_id, "big/inner") is None


def test_root_folder_cannot_be_removed(store: Store, tree: Path) -> None:
    scan_id = save(store, tree)
    store.remove_folder(scan_id, "")
    assert store.folder(scan_id, "") is not None


def test_rescan_replaces_result_and_keeps_id(store: Store, tree: Path) -> None:
    first = save(store, tree)
    rows = row_count(store, "folders"), row_count(store, "top_files")
    write(tree / "new" / "file.bin", 1_000)
    second = save(store, tree)

    assert second == first
    assert len(store.list_scans()) == 1
    assert "new" in {c["name"] for c in store.folder(second, "")["children"]}
    assert row_count(store, "folders") == rows[0] + 1  # no stale rows left behind
    assert row_count(store, "top_files") == rows[1] + 1


def test_separate_folders_get_separate_scans(store: Store, tree: Path) -> None:
    a = save(store, tree / "big")
    b = save(store, tree / "small")
    assert a != b
    assert len(store.list_scans()) == 2
