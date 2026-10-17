"""Find external programs (claude, tectonic) even when the current shell's PATH is stale."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    dirs = [home / ".local" / "bin"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs += [Path(appdata) / "SPB_Data" / ".local" / "bin", Path(appdata) / "npm"]
    return dirs


def find_exe(name: str, env_var: str | None = None) -> str | None:
    """`env_var` override, then PATH, then the usual per-user install folders."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    found = shutil.which(name)
    if found:
        return found
    for d in _candidate_dirs():
        for suffix in (".exe", ".cmd", ""):
            p = d / f"{name}{suffix}"
            if p.is_file():
                return str(p)
    return None
