from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from space_optimizer import web
from space_optimizer.store import Store


@pytest.fixture(autouse=True)
def trash_for_real_only_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """On CI, exercise the real Trash / Recycle Bin. Locally, just delete, so test runs don't fill your Trash."""
    if os.environ.get("CI"):
        return

    def fake_send2trash(path: str) -> None:
        p = Path(path)
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()

    monkeypatch.setattr(web, "send2trash", fake_send2trash)


@pytest.fixture
def server(tmp_path: Path):
    store = Store(tmp_path / "db" / "scans.db")
    job = web.ScanJob(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(store, job, tmp_path, one_filesystem=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def call(url: str, body: object | None = None, headers: dict | None = None) -> tuple[int, dict | list | str]:
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw.decode()


def scan(server: str, path: Path) -> int:
    status, _ = call(f"{server}/api/scan", {"path": str(path)})
    assert status == 200
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _, s = call(f"{server}/api/status")
        if s["state"] == "done":
            return s["scan_id"]
        assert s["state"] != "error", s["error"]
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_page_and_status(server: str) -> None:
    status, page = call(f"{server}/")
    assert status == 200 and "Local Disk Space Optimizer" in page
    _, s = call(f"{server}/api/status")
    assert s["state"] == "idle" and s["platform"] == sys.platform


def test_scan_then_browse(server: str, tree: Path) -> None:
    scan_id = scan(server, tree)
    _, scans = call(f"{server}/api/scans")
    assert [s["id"] for s in scans] == [scan_id]

    _, top = call(f"{server}/api/folder?scan={scan_id}&rel=")
    assert {c["name"] for c in top["children"]} == {"big", "small", "with space"}
    _, spaced = call(f"{server}/api/folder?scan={scan_id}&rel=with%20space")
    assert Path(spaced["path"]) == tree.resolve() / "with space"

    status, _ = call(f"{server}/api/folder?scan={scan_id}&rel=nope")
    assert status == 404


def test_trash_file_and_folder(server: str, tree: Path) -> None:
    scan_id = scan(server, tree)

    status, res = call(f"{server}/api/trash", {"scan": scan_id, "rel": "big/a.bin", "kind": "file"})
    assert status == 200, res
    assert not (tree / "big" / "a.bin").exists()
    _, big = call(f"{server}/api/folder?scan={scan_id}&rel=big")
    assert big["top_files"] == [] and big["folder"]["files"] == 1

    status, res = call(f"{server}/api/trash", {"scan": scan_id, "rel": "with space", "kind": "folder"})
    assert status == 200, res
    assert not (tree / "with space").exists()
    _, top = call(f"{server}/api/folder?scan={scan_id}&rel=")
    assert {c["name"] for c in top["children"]} == {"big", "small"}


def bad_rels(tree: Path) -> list[str]:
    rels = ["", "..", "big/../..", str(tree.resolve() / "big")]
    if sys.platform == "win32":
        rels += ["C:x", "\\Windows", "C:\\Windows"]
    else:
        rels += ["/etc"]
    return rels


def test_trash_refuses_paths_outside_the_scan(server: str, tree: Path) -> None:
    scan_id = scan(server, tree)
    for rel in bad_rels(tree):
        status, res = call(f"{server}/api/trash", {"scan": scan_id, "rel": rel, "kind": "folder"})
        assert status == 400, (rel, res)
    assert (tree / "big" / "a.bin").exists()


def test_trash_checks_kind_and_existence(server: str, tree: Path) -> None:
    scan_id = scan(server, tree)
    cases = [
        {"scan": scan_id, "rel": "small/b.bin", "kind": "folder"},  # file sent as folder
        {"scan": scan_id, "rel": "small", "kind": "file"},  # folder sent as file
        {"scan": scan_id, "rel": "missing", "kind": "folder"},
        {"scan": scan_id, "rel": "small", "kind": "bogus"},
        {"scan": 999, "rel": "small", "kind": "folder"},
    ]
    for body in cases:
        status, res = call(f"{server}/api/trash", body)
        assert status == 400, (body, res)
    assert (tree / "small" / "b.bin").exists()


def test_rejects_foreign_host_and_non_json(server: str, tree: Path) -> None:
    status, _ = call(f"{server}/api/scans", headers={"Host": "evil.example"})
    assert status == 403
    req = urllib.request.Request(f"{server}/api/scan", data=b"path=/", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 415


def test_web_refuses_to_start_elevated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web, "running_elevated", lambda: True)
    app = typer.Typer()
    app.command()(web.serve)
    result = CliRunner().invoke(app, ["--no-browser", "--db", str(tmp_path / "x.db")])
    assert result.exit_code == 1
    assert "Refusing to run as root / administrator" in result.output


def test_cli_report(tree: Path) -> None:
    from space_optimizer.cli import app

    result = CliRunner().invoke(app, [str(tree), "--top", "0"])
    assert result.exit_code == 0, result.output
    assert "big" in result.output and "small" in result.output
