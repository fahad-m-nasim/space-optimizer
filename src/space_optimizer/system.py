"""Small OS-specific helpers."""

from __future__ import annotations

import os
import sys


def running_elevated() -> bool:
    """True when running as root (macOS/Linux) or as an elevated administrator (Windows)."""
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return False
