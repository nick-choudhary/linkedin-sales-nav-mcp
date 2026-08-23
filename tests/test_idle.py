"""Tests for the idle browser timeout.

The behaviour that matters is not "does it close" but "does it never close
something in use". The watchdog takes the same lock the tools do, so a running
scrape blocks it; these tests pin that.
"""

import asyncio
import time

import pytest

from sales_nav_mcp.browser import BrowserManager
from sales_nav_mcp.config import AppConfig, BrowserConfig, ConfigurationError


@pytest.fixture
def cfg(monkeypatch):
    app = AppConfig()
    app.browser = BrowserConfig(idle_timeout_seconds=100.0)
    monkeypatch.setattr("sales_nav_mcp.browser.get_config", lambda: app)
    return app


@pytest.fixture
def manager(cfg):
    m = BrowserManager()
    m._context = object()  # stand-in for a live context
    return m


class TestConfig:
    def test_default_is_one_hour(self):
        assert BrowserConfig().idle_timeout_seconds == 3600.0

    def test_zero_is_allowed(self):
        BrowserConfig(idle_timeout_seconds=0).validate()

    def test_negative_is_rejected(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(idle_timeout_seconds=-1).validate()

    def test_non_finite_is_rejected(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(idle_timeout_seconds=float("inf")).validate()


class TestShouldClose:
    def test_fresh_browser_is_not_idle(self, manager):
        manager.touch()
        assert manager.should_close_for_idle() is False

    def test_idle_past_the_timeout(self, manager):
        manager._last_used = time.monotonic() - 101
        assert manager.should_close_for_idle() is True

    def test_exactly_at_the_timeout_closes(self, manager):
        manager._last_used = time.monotonic() - 100
        assert manager.should_close_for_idle() is True

    def test_zero_timeout_never_closes(self, manager, cfg):
        """0 means keep it open indefinitely, not close immediately."""
        cfg.browser.idle_timeout_seconds = 0
        manager._last_used = time.monotonic() - 10_000
        assert manager.should_close_for_idle() is False

    def test_no_browser_nothing_to_close(self, cfg):
        m = BrowserManager()
        m._last_used = time.monotonic() - 10_000
        assert m.should_close_for_idle() is False

    def test_touch_defers_the_close(self, manager):
        manager._last_used = time.monotonic() - 101
        assert manager.should_close_for_idle() is True
        manager.touch()
        assert manager.should_close_for_idle() is False


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_closes_an_idle_browser(self, manager, cfg, monkeypatch):
        cfg.browser.idle_timeout_seconds = 0.05
        monkeypatch.setattr(BrowserManager, "IDLE_CHECK_SECONDS", 0.01)
        torn = []
        monkeypatch.setattr(manager, "_teardown", lambda: _record(torn, manager))
        manager._ensure_watchdog()
        await asyncio.sleep(0.3)
        manager._watchdog.cancel()
        assert torn, "watchdog should have closed the idle browser"

    @pytest.mark.asyncio
    async def test_never_closes_while_the_lock_is_held(self, manager, cfg, monkeypatch):
        """A running scrape holds the lock; the browser must survive it."""
        cfg.browser.idle_timeout_seconds = 0.05
        monkeypatch.setattr(BrowserManager, "IDLE_CHECK_SECONDS", 0.01)
        torn = []
        monkeypatch.setattr(manager, "_teardown", lambda: _record(torn, manager))
        manager._ensure_watchdog()
        async with manager.lock:
            # Simulate work that outlives the timeout, touching as it goes the
            # way a real tool call does.
            for _ in range(15):
                await asyncio.sleep(0.02)
                manager.touch()
            assert not torn, "watchdog closed the browser mid-operation"
        manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_close_cancels_the_watchdog(self, manager, cfg, monkeypatch):
        monkeypatch.setattr(manager, "_teardown", lambda: _record([], manager))
        manager._ensure_watchdog()
        task = manager._watchdog
        await manager.close()
        assert task.cancelled() or task.done()
        assert manager._watchdog is None

    @pytest.mark.asyncio
    async def test_watchdog_survives_a_failing_check(self, manager, cfg, monkeypatch):
        """A watchdog that dies on one error stops protecting anything."""
        monkeypatch.setattr(BrowserManager, "IDLE_CHECK_SECONDS", 0.01)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return False

        monkeypatch.setattr(manager, "should_close_for_idle", boom)
        manager._ensure_watchdog()
        await asyncio.sleep(0.15)
        assert not manager._watchdog.done()
        assert calls["n"] >= 3
        manager._watchdog.cancel()


async def _record(bucket, manager):
    bucket.append(True)
    manager._context = None
