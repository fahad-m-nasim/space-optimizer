"""Local web UI: browse saved scans, rescan on demand, reveal / copy / trash folders and files."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlparse

import typer
from send2trash import send2trash

from space_optimizer.scanner import ScanCancelled, Scanner
from space_optimizer.store import DEFAULT_DB, Store

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class ScanJob:
    """The one scan that may be running. Starting a new scan cancels the old one; results go to the store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.lock = threading.Lock()
        self.scanner: Scanner | None = None
        self.root_path: Path | None = None
        self.state = "idle"  # idle | scanning | saving | done | error
        self.error = ""
        self.started = 0.0
        self.scan_id: int | None = None

    def start(self, path: Path, one_filesystem: bool) -> None:
        with self.lock:
            if self.scanner:
                self.scanner.cancelled = True
            scanner = Scanner(one_filesystem=one_filesystem)
            self.scanner, self.root_path, self.scan_id = scanner, path, None
            self.state, self.error, self.started = "scanning", "", time.monotonic()
        threading.Thread(target=self._run, args=(scanner, path), daemon=True).start()

    def _run(self, scanner: Scanner, path: Path) -> None:
        try:
            root = scanner.scan(path)
            with self.lock:
                if self.scanner is not scanner:
                    return
                self.state = "saving"
            scan_id = self.store.save_scan(path, root, time.monotonic() - self.started)
        except ScanCancelled:
            return
        except Exception as exc:  # report anything (e.g. permission denied on the root) to the UI
            with self.lock:
                if self.scanner is scanner:
                    self.state, self.error = "error", str(exc)
            return
        with self.lock:
            if self.scanner is scanner:
                self.state, self.scan_id = "done", scan_id

    def status(self) -> dict:
        with self.lock:
            s = self.scanner
            return {
                "state": self.state,
                "error": self.error,
                "root": str(self.root_path) if self.root_path else None,
                "files": s.files_seen if s else 0,
                "current": s.current if s else "",
                "elapsed": time.monotonic() - self.started if s else 0,
                "scan_id": self.scan_id,
            }


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


class BadRequest(Exception):
    pass


def make_handler(store: Store, job: ScanJob, default_path: Path, one_filesystem: bool) -> type[BaseHTTPRequestHandler]:
    index_html = files("space_optimizer").joinpath("static/index.html").read_bytes()

    def resolve_target(scan_id: int, rel: str, allow_root: bool = False) -> Path:
        """Turn (scan, relative path) into an absolute path, refusing anything outside the scanned folder."""
        scan = store.get_scan(scan_id)
        if scan is None:
            raise BadRequest("unknown scan")
        root = Path(scan["root"])
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or (not rel and not allow_root):
            raise BadRequest("invalid path")
        target = root / rel if rel else root
        # Resolve the parent only, so a symlink as the final component is handled as the link itself.
        if rel and not target.parent.resolve().is_relative_to(root.resolve()):
            raise BadRequest("path outside the scanned folder")
        if not (target.exists() or target.is_symlink()):
            raise BadRequest(f"no longer exists: {target}")
        return target

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: object, status: int = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _host_ok(self) -> bool:
            # Reject requests whose Host isn't localhost (guards against DNS-rebinding from web pages).
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host in ALLOWED_HOSTS:
                return True
            self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
            return False

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(HTTPStatus.OK, index_html, "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    self._json(job.status() | {"default_path": str(default_path), "platform": sys.platform})
                elif url.path == "/api/scans":
                    self._json(store.list_scans())
                elif url.path == "/api/disk":
                    u = shutil.disk_usage(Path(q.get("path") or default_path).expanduser())
                    self._json({"total": u.total, "used": u.used, "free": u.free})
                elif url.path == "/api/folder":
                    data = store.folder(int(q.get("scan", 0)), q.get("rel", ""))
                    if data is None:
                        self._json({"error": "folder not found in saved scan"}, HTTPStatus.NOT_FOUND)
                    else:
                        self._json(data)
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except sqlite3.Error as exc:
                self._json({"error": f"database error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            # Requiring a JSON body means cross-site pages can't trigger this without a CORS preflight.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                url = urlparse(self.path)
                if url.path == "/api/scan":
                    path = Path(body.get("path") or default_path).expanduser().resolve()
                    if not path.is_dir():
                        raise BadRequest(f"Not a folder: {path}")
                    job.start(path, one_filesystem)
                    self._json(job.status())
                elif url.path == "/api/reveal":
                    self._reveal(resolve_target(int(body["scan"]), body.get("rel", ""), allow_root=True))
                elif url.path == "/api/trash":
                    self._trash(int(body["scan"]), body.get("rel", ""), body.get("kind"))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (BadRequest, KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (OSError, sqlite3.Error) as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _reveal(self, target: Path) -> None:
            """Show a folder/file in Finder (or the OS file manager)."""
            if sys.platform == "darwin":
                cmd = ["open", "-R", str(target)]
            elif sys.platform == "win32":
                cmd = ["explorer", f"/select,{target}"]
            else:
                cmd = ["xdg-open", str(target if target.is_dir() else target.parent)]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json({"ok": True})

        def _trash(self, scan_id: int, rel: str, kind: str | None) -> None:
            """Move a folder/file to the Trash and update the saved scan to match."""
            if kind not in ("folder", "file"):
                raise BadRequest("kind must be 'folder' or 'file'")
            target = resolve_target(scan_id, rel)
            if kind == "folder" and (target.is_symlink() or not target.is_dir()):
                raise BadRequest(f"not a folder: {target}")
            if kind == "file" and target.is_dir() and not target.is_symlink():
                raise BadRequest(f"not a file: {target}")
            send2trash(str(target))
            if kind == "folder":
                store.remove_folder(scan_id, rel)
            else:
                folder_rel, _, name = rel.rpartition("/")
                store.remove_file(scan_id, folder_rel, name)
            self._json({"ok": True, "path": str(target)})

    return Handler


def serve(
    path: Annotated[Path | None, typer.Argument(help="Folder to pre-fill in the Scan box (default: home).")] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on (localhost only).")] = 8765,
    db: Annotated[Path, typer.Option("--db", help="SQLite file where scan results are saved.")] = DEFAULT_DB,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Don't open the browser automatically.")] = False,
    one_filesystem: Annotated[bool, typer.Option("--one-filesystem", "-x", help="Don't descend into other mounted volumes.")] = False,
    allow_root: Annotated[bool, typer.Option("--allow-root", help="Allow running as root (not recommended).")] = False,
) -> None:
    """Start the Space Optimizer web UI on http://127.0.0.1:PORT. Nothing is scanned until you press Scan."""
    if running_as_root() and not allow_root:
        typer.echo(
            "Refusing to run as root: the UI can move files to the Trash, and as root it could touch "
            "system files. Run it as your normal user (it only sees what you can access), "
            "or pass --allow-root if you really mean it.",
            err=True,
        )
        raise typer.Exit(1)
    default_path = (path or Path.home()).expanduser().resolve()
    store = Store(db.expanduser())
    job = ScanJob(store)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, job, default_path, one_filesystem))
    url = f"http://127.0.0.1:{port}/"
    typer.echo(f"Space Optimizer running at {url}  (results saved in {store.path}; Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nStopped.")
    finally:
        server.server_close()


def main() -> None:
    typer.run(serve)
