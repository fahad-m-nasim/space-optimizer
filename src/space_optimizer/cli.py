"""Command-line interface: disk overview plus a per-folder size / last-used table."""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress_bar import ProgressBar
from rich.table import Table

from space_optimizer.scanner import Scanner
from space_optimizer.system import running_elevated

console = Console()
app = typer.Typer(add_completion=False, help=__doc__)


@dataclass
class Row:
    label: str
    size: int
    file_count: int
    last_accessed: float
    last_modified: float
    errors: int = 0
    is_loose_files: bool = False


class SortKey(str, Enum):
    size = "size"
    accessed = "accessed"
    modified = "modified"
    name = "name"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", text, re.IGNORECASE)
    if not m:
        raise typer.BadParameter(f"can't parse size {text!r} (try 500M, 2G, 100K)")
    power = " KMGT".index(m.group(2).upper() or " ")
    return int(float(m.group(1)) * 1024**power)


def human_age(ts: float, now: float) -> str:
    if not ts:
        return "-"
    secs = max(0, now - ts)
    for limit, div, unit in (
        (3600, 60, "min"),
        (86400, 3600, "h"),
        (86400 * 60, 86400, "d"),
        (86400 * 730, 86400 * 30, "mo"),
    ):
        if secs < limit:
            return f"{int(secs // div)}{unit} ago"
    return f"{secs / (86400 * 365):.1f}y ago"


def fmt_time(ts: float, now: float) -> str:
    if not ts:
        return "-"
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return f"{date} [dim]({human_age(ts, now)})[/dim]"


def age_style(ts: float, now: float) -> str:
    days = (now - ts) / 86400
    if days > 365:
        return "red"
    if days > 90:
        return "yellow"
    return ""


def print_disk_usage(path: Path) -> None:
    usage = shutil.disk_usage(path)
    pct = usage.used / usage.total * 100
    table = Table.grid(padding=(0, 2))
    table.add_row(
        "[bold]Disk[/bold]",
        ProgressBar(total=usage.total, completed=usage.used, width=40),
        f"{human_size(usage.used)} used of {human_size(usage.total)} ({pct:.0f}%)",
        f"[green]{human_size(usage.free)} free[/green]",
    )
    console.print(table)


@app.command()
def scan(
    path: Annotated[Path, typer.Argument(help="Folder to analyse.")] = Path("."),
    depth: Annotated[int, typer.Option("--depth", "-d", min=1, help="How many levels of subfolders to list.")] = 1,
    top: Annotated[int, typer.Option("--top", "-n", min=0, help="Show only the N biggest rows (0 = all).")] = 25,
    sort: Annotated[SortKey, typer.Option("--sort", "-s", help="Sort rows by this column.")] = SortKey.size,
    min_size: Annotated[str | None, typer.Option("--min-size", help="Hide folders smaller than this, e.g. 500M, 2G.")] = None,
    stale_days: Annotated[int | None, typer.Option("--stale", help="Only show folders not modified in this many days.")] = None,
    one_filesystem: Annotated[bool, typer.Option("--one-filesystem", "-x", help="Don't descend into other mounted volumes.")] = False,
) -> None:
    """Show disk usage and which folders under PATH take the most space, with last access/modify times."""
    path = path.expanduser().resolve()
    if running_elevated():
        console.print("[yellow]Running as root / administrator: this will read folders your normal user can't. "
                      "Run it as your normal user to scan only what you have access to.[/yellow]")
    if not path.is_dir():
        console.print(f"[red]Not a directory:[/red] {path}")
        raise typer.Exit(1)

    min_bytes = parse_size(min_size) if min_size else None
    print_disk_usage(path)

    scanner = Scanner(one_filesystem=one_filesystem)
    with console.status(f"Scanning {path} ...") as status:
        results: list = []
        worker = threading.Thread(target=lambda: results.append(scanner.scan(path)), daemon=True)
        start = time.monotonic()
        worker.start()
        while worker.is_alive():
            worker.join(0.2)
            status.update(
                f"Scanning {path} ... {scanner.files_seen:,} files  [dim]{scanner.current[-70:]}[/dim]"
            )
        elapsed = time.monotonic() - start
    if not results:
        console.print("[red]Scan failed.[/red]")
        raise typer.Exit(1)
    root = results[0]

    now = time.time()
    rows = [
        Row(str(n.path.relative_to(path)), n.size, n.file_count, n.last_accessed, n.last_modified, n.errors)
        for n, _ in root.iter_descendants(depth)
    ]
    if root.own_count:
        rows.append(
            Row("(files in this folder)", root.own_size, root.own_count, root.own_accessed, root.own_modified,
                is_loose_files=True)
        )
    if min_bytes is not None:
        rows = [r for r in rows if r.size >= min_bytes]
    if stale_days is not None:
        cutoff = now - stale_days * 86400
        rows = [r for r in rows if r.last_modified < cutoff]

    sorters = {
        SortKey.size: (lambda r: r.size, True),
        SortKey.accessed: (lambda r: r.last_accessed, False),  # least recently used first
        SortKey.modified: (lambda r: r.last_modified, False),
        SortKey.name: (lambda r: r.label.lower(), False),
    }
    key, reverse = sorters[sort]
    rows.sort(key=key, reverse=reverse)
    hidden = max(0, len(rows) - top) if top else 0
    if top:
        rows = rows[:top]

    total = root.size or 1
    table = Table(title=f"[bold]{path}[/bold]", title_justify="left", header_style="bold cyan")
    table.add_column("Folder", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("%", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Last accessed")
    table.add_column("Last modified")

    for r in rows:
        name = r.label
        if r.is_loose_files:
            name = f"[dim]{name}[/dim]"
        elif r.errors:
            name += f" [yellow](!{r.errors})[/yellow]"
        table.add_row(
            name,
            human_size(r.size),
            f"{r.size / total * 100:.1f}",
            f"{r.file_count:,}",
            fmt_time(r.last_accessed, now),
            fmt_time(r.last_modified, now),
            style=age_style(r.last_modified, now),
        )
    console.print(table)

    summary = (
        f"Total: [bold]{human_size(root.size)}[/bold] in {root.file_count:,} files, "
        f"scanned in {elapsed:.1f}s"
    )
    if hidden:
        summary += f" · {hidden} more rows hidden (use --top 0 to show all)"
    console.print(summary)
    if root.errors:
        console.print(
            f"[yellow]{root.errors:,} entries could not be read (permission denied); "
            "sizes may be understated. (!N) marks affected folders.[/yellow]"
        )
    console.print(
        "[dim]Last accessed/modified = newest time of anything inside the folder. "
        "Yellow rows: untouched > 90 days, red: > 1 year. "
        "Note: most systems don't update access times on every read, so 'last accessed' is approximate.[/dim]"
    )
