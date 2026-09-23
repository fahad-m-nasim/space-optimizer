from __future__ import annotations

import os
from pathlib import Path

import pytest


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """
    tree/
    ├── big/
    │   ├── a.bin            100 KB
    │   └── inner/blob.bin   300 KB
    ├── small/b.bin           10 KB
    ├── with space/note.txt
    └── root.txt
    """
    root = tmp_path / "tree"
    write(root / "big" / "a.bin", 100_000)
    write(root / "big" / "inner" / "blob.bin", 300_000)
    write(root / "small" / "b.bin", 10_000)
    write(root / "with space" / "note.txt", 20)
    write(root / "root.txt", 10)
    return root
