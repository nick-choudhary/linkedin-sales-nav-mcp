"""Tests for orphaned-profile recovery.

The important ones are the negatives. This code terminates processes, so a
false match closes someone's real browser — which is the single worst thing it
could do. The filter must stay narrow enough that anything ambiguous matches
nothing.
"""

import os

import pytest

from sales_nav_mcp.orphans import (
    find_profile_processes,
    looks_like_profile_lock,
    reclaim_profile,
)

PROFILE = r"C:\Users\Lenovo\.linkedin-sales-nav\profile"
CHROMIUM = r"C:\ms-playwright\chromium-1234\chrome-win64\chrome.exe"


def lister(rows):
    return lambda: rows


class TestLockDetection:
    def test_recognises_the_playwright_message(self):
        msg = (
            "BrowserType.launch_persistent_context: Opening in existing browser "
            "session. This usually means that the profile is already in use."
        )
        assert looks_like_profile_lock(msg) is True

    def test_other_failures_are_not_lock_failures(self):
        """An install problem must fall through to the normal error, not
        trigger a process hunt."""
        assert looks_like_profile_lock("Executable doesn't exist") is False
        assert looks_like_profile_lock("net::ERR_CONNECTION_REFUSED") is False
        assert looks_like_profile_lock("") is False
        assert looks_like_profile_lock(None) is False


class TestFindProfileProcesses:
    def test_matches_a_browser_holding_the_profile(self):
        rows = [(111, f'"{CHROMIUM}" --user-data-dir={PROFILE} --no-sandbox')]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == [111]

    def test_ignores_a_different_profile(self):
        rows = [(111, f'"{CHROMIUM}" --user-data-dir=C:\\Users\\Lenovo\\other')]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == []

    def test_ignores_your_real_chrome(self):
        """The constraint that matters: normal Chrome must never match."""
        rows = [
            (222, r'"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"'),
            (
                223,
                r'"C:\Program Files\Google\Chrome\Application\chrome.exe" --type=gpu',
            ),
        ]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == []

    def test_ignores_a_non_browser_mentioning_the_path(self):
        """An editor or a grep over the profile path is not a browser."""
        rows = [
            (333, f"python backup_script.py {PROFILE}"),
            (334, f"notepad.exe {PROFILE}\\Preferences"),
        ]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == []

    def test_never_matches_this_process(self):
        rows = [(os.getpid(), f"chrome.exe --user-data-dir={PROFILE}")]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == []

    def test_matches_all_children_of_one_browser(self):
        rows = [
            (1, f"{CHROMIUM} --user-data-dir={PROFILE}"),
            (2, f"{CHROMIUM} --type=renderer --user-data-dir={PROFILE}"),
            (3, f"{CHROMIUM} --type=gpu-process --user-data-dir={PROFILE}"),
        ]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == [1, 2, 3]

    def test_case_insensitive_on_the_path(self):
        rows = [(444, f"{CHROMIUM} --user-data-dir={PROFILE.upper()}")]
        assert find_profile_processes(PROFILE, lister=lister(rows)) == [444]

    def test_empty_profile_matches_nothing(self):
        """A blank needle must not match every process on the machine."""
        rows = [(555, f"{CHROMIUM} --user-data-dir={PROFILE}")]
        assert find_profile_processes("", lister=lister(rows)) == []
        assert find_profile_processes("   ", lister=lister(rows)) == []

    def test_blank_command_lines_are_skipped(self):
        assert find_profile_processes(PROFILE, lister=lister([(666, "")])) == []

    def test_no_processes_at_all(self):
        assert find_profile_processes(PROFILE, lister=lister([])) == []


class TestReclaim:
    def test_nothing_to_do_reports_zero(self):
        outcome = reclaim_profile(PROFILE, lister=lister([]), grace_seconds=0)
        assert outcome == {"found": 0, "terminated": [], "forced": []}

    def test_terminates_then_reports(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            "sales_nav_mcp.orphans._terminate",
            lambda pid, *, force: killed.append((pid, force)),
        )
        rows = [(777, f"{CHROMIUM} --user-data-dir={PROFILE}")]
        # The process is still listed afterwards, so it gets forced too.
        outcome = reclaim_profile(PROFILE, lister=lister(rows), grace_seconds=0)
        assert outcome["found"] == 1
        assert outcome["terminated"] == [777]
        assert outcome["forced"] == [777]
        assert killed == [(777, False), (777, True)]

    def test_graceful_exit_is_not_forced(self, monkeypatch):
        """If it goes away politely, do not send a kill."""
        state = {"listed": True}
        killed = []

        def fake_terminate(pid, *, force):
            killed.append((pid, force))
            state["listed"] = False

        def changing_lister():
            return (
                [(888, f"{CHROMIUM} --user-data-dir={PROFILE}")]
                if state["listed"]
                else []
            )

        monkeypatch.setattr("sales_nav_mcp.orphans._terminate", fake_terminate)
        outcome = reclaim_profile(PROFILE, lister=changing_lister, grace_seconds=0)
        assert outcome["forced"] == []
        assert killed == [(888, False)]

    def test_a_failing_kill_does_not_raise(self, monkeypatch):
        """Cleanup must never break the launch path it is trying to rescue."""

        def boom(pid, *, force):
            raise PermissionError("access denied")

        monkeypatch.setattr("sales_nav_mcp.orphans._terminate", boom)
        rows = [(999, f"{CHROMIUM} --user-data-dir={PROFILE}")]
        outcome = reclaim_profile(PROFILE, lister=lister(rows), grace_seconds=0)
        assert outcome["found"] == 1


class TestListerFailureIsSafe:
    def test_a_broken_lister_matches_nothing(self):
        def broken():
            raise OSError("ps not found")

        with pytest.raises(OSError):
            find_profile_processes(PROFILE, lister=broken)
