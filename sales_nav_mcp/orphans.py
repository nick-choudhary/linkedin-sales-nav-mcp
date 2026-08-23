"""Reclaim a browser profile left locked by an orphaned Chromium.

The browser is closed by the server's lifespan hook. When the server dies
ungracefully -- a killed process, a dropped stdio transport, a crash -- that
hook never runs, and Chromium keeps running while holding the profile
directory. Every subsequent launch then fails with:

    BrowserType.launch_persistent_context: Opening in existing browser session.
    This usually means that the profile is already in use by another instance.

...and stays broken until someone kills the process by hand.

The orphan cannot be adopted. `launch_persistent_context` always starts a new
browser, and patchright launches with `--remote-debugging-pipe` rather than a
TCP port, so there is no CDP endpoint to attach to. Reclaiming therefore means
terminating the orphan and launching fresh. That is safe for the session: the
LinkedIn cookies live in the profile directory on disk, not in the process.

## Why this is narrow on purpose

It matches on the *resolved user_data_dir appearing literally in the command
line*, and additionally requires the executable to look like a browser. It will
not touch a normal Chrome, an unrelated automation browser, or any process that
merely mentions the path. Getting this wrong would close someone's real browser,
so the filter is deliberately conservative: when in doubt, match nothing.
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# Markers Playwright/patchright use when the profile is already locked.
PROFILE_IN_USE_MARKERS = (
    "opening in existing browser session",
    "profile is already in use",
    "already in use by another instance",
)

# The orphan must look like a browser, not merely mention the path.
_BROWSER_NAMES = ("chrome", "chromium", "msedge", "headless_shell")

ProcessLister = Callable[[], Iterable[tuple[int, str]]]


def looks_like_profile_lock(message: str) -> bool:
    """Is this launch failure the 'profile already in use' one?"""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in PROFILE_IN_USE_MARKERS)


def _list_processes_windows() -> list[tuple[int, str]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine } | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    out = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return _parse_pid_lines(out.stdout, sep="\t")


def _list_processes_posix() -> list[tuple[int, str]]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return _parse_pid_lines(out.stdout, sep=None)


def _parse_pid_lines(text: str, *, sep: str | None) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, rest = line.partition(sep) if sep else line.partition(" ")
        pid_text = pid_text.strip()
        if not pid_text.isdigit():
            continue
        rows.append((int(pid_text), rest.strip()))
    return rows


def default_lister() -> list[tuple[int, str]]:
    try:
        if sys.platform.startswith("win"):
            return _list_processes_windows()
        return _list_processes_posix()
    except Exception:  # noqa: BLE001 - never let cleanup break a launch
        logger.debug("could not enumerate processes", exc_info=True)
        return []


def find_profile_processes(
    user_data_dir: str | Path, *, lister: ProcessLister | None = None
) -> list[int]:
    """PIDs of browsers holding `user_data_dir`. Empty when unsure.

    Both the profile path and a browser-looking executable must be present, and
    the current process is never included.
    """
    needle = str(user_data_dir).strip()
    if not needle:
        return []
    rows = list((lister or default_lister)())
    me = os.getpid()
    hits: list[int] = []
    for pid, cmdline in rows:
        if pid == me or not cmdline:
            continue
        lowered = cmdline.lower()
        if needle.lower() not in lowered:
            continue
        if not any(name in lowered for name in _BROWSER_NAMES):
            continue
        hits.append(pid)
    return hits


def _terminate(pid: int, *, force: bool) -> None:
    if sys.platform.startswith("win"):
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.append("/F")
        subprocess.run(args, capture_output=True, timeout=15)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def reclaim_profile(
    user_data_dir: str | Path,
    *,
    lister: ProcessLister | None = None,
    grace_seconds: float = 3.0,
) -> dict[str, object]:
    """Terminate browsers holding `user_data_dir`. Returns what it did.

    Asks politely first and only forces what survives, so Chromium gets the
    chance to flush its cookie database rather than having it yanked.
    """
    pids = find_profile_processes(user_data_dir, lister=lister)
    if not pids:
        return {"found": 0, "terminated": [], "forced": []}

    logger.warning(
        "Reclaiming profile %s from orphaned browser process(es): %s",
        user_data_dir,
        pids,
    )
    for pid in pids:
        with _suppressed():
            _terminate(pid, force=False)
    time.sleep(grace_seconds)

    still = set(find_profile_processes(user_data_dir, lister=lister))
    forced: list[int] = []
    for pid in pids:
        if pid in still:
            with _suppressed():
                _terminate(pid, force=True)
            forced.append(pid)
    if forced:
        time.sleep(1.0)
    return {"found": len(pids), "terminated": pids, "forced": forced}


class _suppressed:
    """Cleanup must never raise into a launch path."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            logger.debug("process termination failed", exc_info=(exc_type, exc, tb))
        return True
