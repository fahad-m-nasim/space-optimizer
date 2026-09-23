"""Walk a directory tree once and build an in-memory tree of per-folder stats."""

from __future__ import annotations

import heapq
import os
import stat
from collections.abc import Iterator
from pathlib import Path

TOP_FILES_PER_FOLDER = 15


class ScanCancelled(Exception):
    pass


class DirNode:
    """Stats for one folder. Totals include everything below it; `own_*` are files directly inside it."""

    __slots__ = (
        "name", "parent", "children", "errors",
        "size", "file_count", "last_accessed", "last_modified",
        "own_size", "own_count", "own_accessed", "own_modified", "top_files",
    )

    def __init__(self, name: str, parent: DirNode | None) -> None:
        self.name = name
        self.parent = parent
        self.children: dict[str, DirNode] = {}
        self.errors = 0  # entries we could not read (usually permission denied)
        self.size = 0  # bytes actually allocated on disk (like `du`)
        self.file_count = 0
        self.last_accessed = 0.0  # newest atime of the folder or anything inside it
        self.last_modified = 0.0  # newest mtime of the folder or anything inside it
        self.own_size = 0
        self.own_count = 0
        self.own_accessed = 0.0
        self.own_modified = 0.0
        # min-heap of (size, name, atime, mtime): the largest files directly in this folder
        self.top_files: list[tuple[int, str, float, float]] = []

    @property
    def path(self) -> Path:
        parts = []
        node: DirNode | None = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return Path(*reversed(parts))

    def find(self, rel: str) -> DirNode | None:
        """Look up a descendant by a relative path like 'a/b/c' ('' or '.' is self)."""
        node = self
        for part in Path(rel).parts:
            if part == ".":
                continue
            node = node.children.get(part)
            if node is None:
                return None
        return node

    def iter_descendants(self, max_depth: int, _depth: int = 0) -> Iterator[tuple[DirNode, int]]:
        for child in self.children.values():
            yield child, _depth + 1
            if _depth + 1 < max_depth:
                yield from child.iter_descendants(max_depth, _depth + 1)


def _disk_size(st: os.stat_result) -> int:
    # st_blocks is in 512-byte units on macOS and Linux; fall back to logical size elsewhere.
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


class Scanner:
    """Build a DirNode tree. Progress is exposed via `files_seen` / `current` so other threads can poll it."""

    def __init__(self, one_filesystem: bool = False) -> None:
        self.one_filesystem = one_filesystem
        self.files_seen = 0
        self.current = ""
        self.cancelled = False
        self._seen_inodes: set[tuple[int, int]] = set()
        self._root_dev: int | None = None

    def scan(self, root: Path) -> DirNode:
        root = root.expanduser().resolve()
        st = root.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(root)
        self._root_dev = st.st_dev
        node = DirNode(str(root), None)
        self._walk(root, st, node)
        return node

    def _walk(self, path: Path, st: os.stat_result, node: DirNode) -> None:
        if self.cancelled:
            raise ScanCancelled
        self.current = str(path)
        node.size += _disk_size(st)
        node.last_accessed = max(node.last_accessed, st.st_atime)
        node.last_modified = max(node.last_modified, st.st_mtime)

        try:
            entries = list(os.scandir(path))
        except OSError:
            node.errors += 1
            return

        for entry in entries:
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError:
                node.errors += 1
                continue

            if stat.S_ISDIR(est.st_mode):
                if self.one_filesystem and est.st_dev != self._root_dev:
                    continue
                child = DirNode(entry.name, node)
                node.children[entry.name] = child
                self._walk(Path(entry.path), est, child)
                node.size += child.size
                node.file_count += child.file_count
                node.errors += child.errors
                node.last_accessed = max(node.last_accessed, child.last_accessed)
                node.last_modified = max(node.last_modified, child.last_modified)
                continue

            # Regular files, symlinks (the link itself, not its target), sockets, etc.
            size = _disk_size(est)
            if est.st_nlink > 1:
                key = (est.st_dev, est.st_ino)
                if key in self._seen_inodes:
                    size = 0  # hard link already counted elsewhere
                else:
                    self._seen_inodes.add(key)
            node.size += size
            node.file_count += 1
            node.last_accessed = max(node.last_accessed, est.st_atime)
            node.last_modified = max(node.last_modified, est.st_mtime)
            node.own_size += size
            node.own_count += 1
            node.own_accessed = max(node.own_accessed, est.st_atime)
            node.own_modified = max(node.own_modified, est.st_mtime)
            item = (size, entry.name, est.st_atime, est.st_mtime)
            if len(node.top_files) < TOP_FILES_PER_FOLDER:
                heapq.heappush(node.top_files, item)
            elif size > node.top_files[0][0]:
                heapq.heapreplace(node.top_files, item)
            self.files_seen += 1
