"""Walk a directory tree once and build an in-memory tree of per-folder stats."""

from __future__ import annotations

import heapq
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

TOP_FILES_PER_FOLDER = 15

IS_WINDOWS = sys.platform == "win32"
# Windows file attribute / reparse tag bits (see winnt.h)
FILE_ATTRIBUTE_SPARSE_FILE = 0x200
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_COMPRESSED = 0x800
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_ATTRIBUTES = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
REPARSE_TAG_NAME_SURROGATE = 0x20000000  # set for symlinks, junctions and volume mount points


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


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD

    def _windows_compressed_size(path: str) -> int | None:
        high = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        low = _kernel32.GetCompressedFileSizeW(path, ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error():
            return None
        return (high.value << 32) + low


def file_disk_size(st: os.stat_result, path: str) -> int:
    """Bytes a file actually occupies on disk (like `du`), not its logical length."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is not None:  # macOS / Linux: st_blocks is in 512-byte units
        return blocks * 512
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & CLOUD_ONLY_ATTRIBUTES:  # OneDrive/Dropbox "online-only" placeholder: nothing stored locally
        return 0
    if IS_WINDOWS and attrs & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_SPARSE_FILE):
        size = _windows_compressed_size(path)
        if size is not None:
            return size
    return st.st_size


def is_link_like(st: os.stat_result, path: str) -> bool:
    """Symlinks, and on Windows also junctions and mount points: entries we must not descend into."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if not getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(st, "st_reparse_tag", 0)
    if not tag:  # directory listings don't always carry the tag; ask for it
        try:
            tag = os.lstat(path).st_reparse_tag
        except (OSError, AttributeError):
            return True  # can't tell: be safe and don't follow it
    # Cloud-sync folders (OneDrive etc.) are reparse points too, but not name surrogates: scan those.
    return bool(tag & REPARSE_TAG_NAME_SURROGATE)


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
        self._root_dev = self._device(root, st)
        node = DirNode(str(root), None)
        self._walk(root, st, node)
        return node

    @staticmethod
    def _device(path: Path | str, st: os.stat_result) -> int:
        # On Windows, directory listings report st_dev as 0; a real stat has the volume serial number.
        return st.st_dev or os.lstat(path).st_dev

    def _walk(self, path: Path, st: os.stat_result, node: DirNode) -> None:
        if self.cancelled:
            raise ScanCancelled
        self.current = str(path)
        node.size += file_disk_size(st, str(path))
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
                if is_link_like(est, entry.path):  # Windows junction / mount point: skip, like a symlink
                    continue
                if self.one_filesystem and self._device(entry.path, est) != self._root_dev:
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
            size = file_disk_size(est, entry.path)
            if est.st_nlink > 1:  # always 0 in Windows directory listings, so hard links aren't de-duplicated there
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
