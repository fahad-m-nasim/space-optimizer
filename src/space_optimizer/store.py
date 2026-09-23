"""SQLite storage for scan results: one saved scan per root folder, replaced when that folder is rescanned."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from space_optimizer.scanner import DirNode

DEFAULT_DB = Path.home() / ".space-optimizer" / "scans.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    root        TEXT NOT NULL UNIQUE,
    scanned_at  REAL NOT NULL,
    elapsed     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS folders (
    id            INTEGER PRIMARY KEY,
    scan_id       INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    parent_id     INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    rel           TEXT NOT NULL,  -- path relative to the scan root; '' for the root itself
    name          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    files         INTEGER NOT NULL,
    accessed      REAL NOT NULL,
    modified      REAL NOT NULL,
    errors        INTEGER NOT NULL,
    own_size      INTEGER NOT NULL,  -- files directly in this folder
    own_files     INTEGER NOT NULL,
    own_accessed  REAL NOT NULL,
    own_modified  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS folders_by_rel ON folders(scan_id, rel);
CREATE INDEX IF NOT EXISTS folders_by_parent ON folders(parent_id);
CREATE TABLE IF NOT EXISTS top_files (  -- the largest files directly in each folder
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    accessed   REAL NOT NULL,
    modified   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS top_files_by_folder ON top_files(folder_id);
"""

FOLDER_COLS = "id, rel, name, size, files, accessed, modified, errors"


class Store:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------- writing ----------

    def save_scan(self, root_path: Path, root: DirNode, elapsed: float) -> int:
        """Store a finished scan, replacing any earlier scan of the same folder. Returns the scan id."""
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT id FROM scans WHERE root = ?", (str(root_path),)).fetchone()
            if row:
                scan_id = row["id"]
                con.execute("DELETE FROM folders WHERE scan_id = ?", (scan_id,))
                con.execute("UPDATE scans SET scanned_at = ?, elapsed = ? WHERE id = ?", (time.time(), elapsed, scan_id))
            else:
                scan_id = con.execute(
                    "INSERT INTO scans (root, scanned_at, elapsed) VALUES (?, ?, ?)",
                    (str(root_path), time.time(), elapsed),
                ).lastrowid

            next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM folders").fetchone()[0]
            folder_rows, file_rows = [], []
            stack: list[tuple[DirNode, int | None, str]] = [(root, None, "")]
            while stack:  # parents are emitted before their children, as the foreign key requires
                node, parent_id, rel = stack.pop()
                fid, next_id = next_id, next_id + 1
                folder_rows.append((
                    fid, scan_id, parent_id, rel, node.name if rel else str(root_path),
                    node.size, node.file_count, node.last_accessed, node.last_modified, node.errors,
                    node.own_size, node.own_count, node.own_accessed, node.own_modified,
                ))
                file_rows.extend((fid, name, size, atime, mtime) for size, name, atime, mtime in node.top_files)
                for child in node.children.values():
                    stack.append((child, fid, f"{rel}/{child.name}" if rel else child.name))
            con.executemany(f"INSERT INTO folders VALUES ({', '.join('?' * 14)})", folder_rows)
            con.executemany("INSERT INTO top_files VALUES (?, ?, ?, ?, ?)", file_rows)
        return scan_id

    def remove_folder(self, scan_id: int, rel: str) -> None:
        """Drop a deleted folder (and everything under it) and take its totals off its ancestors."""
        with closing(self._connect()) as con, con:
            row = con.execute(
                "SELECT id, parent_id, size, files, errors FROM folders WHERE scan_id = ? AND rel = ?", (scan_id, rel)
            ).fetchone()
            if row is None or row["parent_id"] is None:
                return
            con.execute("DELETE FROM folders WHERE id = ?", (row["id"],))
            self._subtract_from_ancestors(con, row["parent_id"], row["size"], row["files"], row["errors"])

    def remove_file(self, scan_id: int, folder_rel: str, name: str) -> None:
        """Drop a deleted file from its folder's largest-files list and take its size off the totals."""
        with closing(self._connect()) as con, con:
            folder = con.execute("SELECT id FROM folders WHERE scan_id = ? AND rel = ?", (scan_id, folder_rel)).fetchone()
            if folder is None:
                return
            f = con.execute(
                "SELECT rowid, size FROM top_files WHERE folder_id = ? AND name = ?", (folder["id"], name)
            ).fetchone()
            if f is None:
                return
            con.execute("DELETE FROM top_files WHERE rowid = ?", (f["rowid"],))
            con.execute(
                "UPDATE folders SET own_size = MAX(own_size - ?, 0), own_files = MAX(own_files - 1, 0) WHERE id = ?",
                (f["size"], folder["id"]),
            )
            self._subtract_from_ancestors(con, folder["id"], f["size"], 1, 0)

    @staticmethod
    def _subtract_from_ancestors(con: sqlite3.Connection, folder_id: int | None, size: int, files: int, errors: int) -> None:
        while folder_id is not None:
            con.execute(
                "UPDATE folders SET size = MAX(size - ?, 0), files = MAX(files - ?, 0), errors = MAX(errors - ?, 0) "
                "WHERE id = ?",
                (size, files, errors, folder_id),
            )
            folder_id = con.execute("SELECT parent_id FROM folders WHERE id = ?", (folder_id,)).fetchone()[0]

    # ---------- reading ----------

    def list_scans(self) -> list[dict]:
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT s.id, s.root, s.scanned_at, s.elapsed, f.size, f.files "
                "FROM scans s JOIN folders f ON f.scan_id = s.id AND f.rel = '' ORDER BY s.scanned_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scan(self, scan_id: int) -> dict | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT id, root, scanned_at, elapsed FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return dict(row) if row else None

    def folder(self, scan_id: int, rel: str) -> dict | None:
        """Everything the UI needs to show one folder: its stats, subfolders and largest files."""
        with closing(self._connect()) as con:
            scan = con.execute("SELECT id, root, scanned_at, elapsed FROM scans WHERE id = ?", (scan_id,)).fetchone()
            node = con.execute(
                f"SELECT {FOLDER_COLS}, own_size, own_files, own_accessed, own_modified "
                "FROM folders WHERE scan_id = ? AND rel = ?",
                (scan_id, rel),
            ).fetchone()
            if scan is None or node is None:
                return None
            children = con.execute(
                f"SELECT {FOLDER_COLS}, (SELECT COUNT(*) FROM folders c WHERE c.parent_id = f.id) AS subfolders "
                "FROM folders f WHERE parent_id = ?",
                (node["id"],),
            ).fetchall()
            files = con.execute(
                "SELECT name, size, accessed, modified FROM top_files WHERE folder_id = ? ORDER BY size DESC",
                (node["id"],),
            ).fetchall()
            root_size = con.execute("SELECT size FROM folders WHERE scan_id = ? AND rel = ''", (scan_id,)).fetchone()[0]

        def public(r: sqlite3.Row) -> dict:
            d = dict(r)
            d.pop("id")
            d["path"] = str(Path(scan["root"]) / d["rel"]) if d["rel"] else scan["root"]
            return d

        folder = public(node)
        own = {k: folder.pop(f"own_{k}") for k in ("size", "files", "accessed", "modified")}
        folder["subfolders"] = len(children)
        return {
            "scan": dict(scan),
            "root": scan["root"],
            "root_size": root_size,
            "path": folder["path"],
            "folder": folder,
            "children": [public(c) for c in children],
            "own": own,
            "top_files": [dict(f) | {"path": str(Path(folder["path"]) / f["name"])} for f in files],
        }
