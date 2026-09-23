# Local Disk Space Optimizer

[![tests](https://github.com/fahad-m-nasim/space-optimizer/actions/workflows/tests.yml/badge.svg)](https://github.com/fahad-m-nasim/space-optimizer/actions/workflows/tests.yml)

Find out what's filling up your disk. Local Disk Space Optimizer scans a folder and shows how much space
each subfolder uses, plus when anything inside it was last accessed and last modified. Browse
the results in a local web UI or print them in the terminal.

- **Folder sizes as the disk sees them.** Counts allocated blocks, like `du` does, so sparse files such as VM disk images show their real footprint.
- **Last accessed / last modified** for every folder: the newest time of anything inside it. Folders untouched for over a year are flagged.
- **Web UI:** drill into folders, sort and filter, see the largest files, copy paths, reveal in Finder/Explorer, or move items to the Trash.
- **Saved scans:** results are stored locally in SQLite. The UI opens your last scan instantly and only rescans when you ask.
- **Runs as you:** it only reads what your user account can read. Nothing leaves your machine.
- **Works on macOS, Linux and Windows.** Tested on all three on every change.

## Install

Requires Python 3.10+. The easiest way is [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/fahad-m-nasim/space-optimizer
```

**Windows (PowerShell):** install uv first, then the tool. `git` must be installed for the `git+https` URL.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install git+https://github.com/fahad-m-nasim/space-optimizer
```

This installs two commands, `space-optimizer-web` and `space-optimizer`.
With pipx instead: `pipx install git+https://github.com/fahad-m-nasim/space-optimizer`.

To run it without installing:

```bash
git clone https://github.com/fahad-m-nasim/space-optimizer
cd space-optimizer
uv run space-optimizer-web
```

## Web UI

```bash
space-optimizer-web
```

This opens <http://127.0.0.1:8765> in your browser. Type a folder (for example `~` or
`~/Documents`, or `C:\Users\you` on Windows) and press **Scan**.

- **Browse:** click a folder to go into it. Breadcrumbs, **Up**, Backspace or the browser's back button go back up.
- **Sort and filter:** click column headers to sort. Filter by name or by "not modified in 90+ days / 1+ year / 3+ years".
- **Row actions:** copy the full path, show it in your file manager, or move it to the Trash (asks for confirmation first). The same buttons next to the breadcrumb act on the current folder.
- **Largest files:** the biggest files directly inside the current folder are listed below the folders.
- **Saved scans:** reopen any previously scanned folder from the dropdown. **Rescan** refreshes the current one.

| Option | Default | Meaning |
|---|---|---|
| `PATH` | home folder | Folder pre-filled in the Scan box |
| `-p, --port` | `8765` | Port to listen on (always localhost only) |
| `--db` | `~/.space-optimizer/scans.db` | Where scan results are saved (`%USERPROFILE%\.space-optimizer\scans.db` on Windows) |
| `--no-browser` | off | Don't open the browser automatically |
| `-x, --one-filesystem` | off | Don't descend into other mounted volumes |

Stop the server with Ctrl+C.

## Command line

For a quick one-off report in the terminal (nothing is saved):

```bash
space-optimizer ~                                  # biggest folders in your home folder
space-optimizer ~/Library -d 2                     # list two levels deep
space-optimizer ~ --stale 365                      # folders not modified in a year
space-optimizer ~ --min-size 1G --sort accessed    # big folders, least recently used first
```

| Option | Meaning |
|---|---|
| `-d, --depth N` | How many levels of subfolders to list (default 1) |
| `-n, --top N` | Show only N rows (default 25, `0` = all) |
| `-s, --sort` | `size` (default), `accessed`, `modified`, `name` |
| `--min-size` | Hide folders smaller than e.g. `500M`, `2G` |
| `--stale DAYS` | Only show folders not modified in DAYS days |
| `-x, --one-filesystem` | Don't descend into other mounted volumes |

## Permissions

Local Disk Space Optimizer runs with the permissions of the user who starts it. It only scans folders
your account can read and never asks for elevated access.

- **Unreadable folders** (permission denied) are skipped and marked with ⚠ and a count. Their sizes may be understated.
- **Don't run it with `sudo` or "Run as administrator".** The web UI refuses to start as root or as an elevated administrator, because it could then move system files to the Trash. The CLI prints a warning.
- **macOS:** the first scan of your home folder may trigger privacy prompts such as "Terminal would like to access your Documents folder". Some folders, like `~/Library/Mail`, stay unreadable unless you give your terminal **Full Disk Access** (System Settings → Privacy & Security). Either way works; blocked folders are just reported as unreadable.
- **Windows:** folders owned by other users or by the system (parts of `C:\Windows`, `C:\ProgramData`, other people's profiles) are reported as unreadable, which is expected.

## Safety and privacy

- **Local only:** the web server listens on `127.0.0.1` and rejects requests with any other `Host` header. It also requires a JSON body for actions, so other websites can't trigger them.
- **Nothing is uploaded or sent anywhere.** Scan results stay in a local SQLite file (`~/.space-optimizer/scans.db`). Delete that file to forget all saved scans.
- **Delete means "move to Trash".** Items go to the Trash / Recycle Bin (via [Send2Trash](https://github.com/arsenetar/send2trash)), so you can restore them. Only items inside the scanned folder can be trashed, never the scanned folder itself.
- **Read-only scanning.** Symlinks, and on Windows junctions and mount points, are not followed, so nothing is counted twice and scans can't loop.

## Notes

- **Last accessed is approximate.** Most systems don't update access times on every read (macOS and Linux's `relatime` both skip some).
- **Sizes use 1024-based units** (1 GB = 1024³ bytes), so numbers can look slightly smaller than Finder's, which uses 1000-based units.
- **"Largest files"** keeps the 15 biggest files per folder. After you trash some, the list may be shorter until you rescan.
- **Windows specifics:**
  - OneDrive "online-only" files count as 0 bytes, because they aren't stored on your disk.
  - Compressed and sparse files count at their real on-disk size.
  - Hard links are counted once per link (detecting them would need an extra system call per file).
  - NTFS often doesn't update "last accessed" at all.
- **Platforms:** tested on macOS, Linux and Windows with Python 3.10 and 3.14 on every change (see the badge above).

## Development

```bash
git clone https://github.com/fahad-m-nasim/space-optimizer
cd space-optimizer
uv sync
uv run space-optimizer-web --no-browser --db ./dev.db
uv run pytest                       # run the tests
```

```
src/space_optimizer/
├── scanner.py         # walks the tree once, builds per-folder stats
├── store.py           # SQLite persistence for saved scans
├── web.py             # local HTTP server + JSON API
├── cli.py             # terminal report
├── system.py          # OS helpers (root / administrator check)
└── static/index.html  # the web UI (plain HTML/CSS/JS, no build step)
tests/                 # pytest suite, run on macOS, Linux and Windows in CI
```

## License

[MIT](LICENSE)
