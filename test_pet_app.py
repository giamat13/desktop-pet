"""
Self-check for apply_window_mode(). Run directly: `python test_pet_app.py`.

Creates one throwaway, invisible win32 window (never shown, never a "pet")
to verify the taskbar-hiding ex-style hack and the on_top hand-off to
pywebview's Window object actually do what apply_window_mode claims -
without ever creating a real, visible pet window on screen.
"""
import json
import os
import sys
import time
import tempfile
import threading

import win32api
import win32con
import win32gui

import pet_app


class _FakeHandle:
    def __init__(self, hwnd):
        self._hwnd = hwnd

    def ToInt32(self):
        return self._hwnd


class _FakeNative:
    def __init__(self, hwnd):
        self.Handle = _FakeHandle(hwnd)


class _FakeEvent:
    """Stand-in for one pywebview Event: subscribable with +=, fired by hand."""

    def __init__(self):
        self._handlers = []

    def __add__(self, handler):
        self._handlers.append(handler)
        return self

    def fire(self, window):
        for handler in self._handlers:
            handler(window)


class _FakeEvents:
    def __init__(self):
        self.before_show = _FakeEvent()


class _FakeWindow:
    def __init__(self, hwnd):
        self.native = _FakeNative(hwnd)
        self.events = _FakeEvents()
        self.on_top = None  # apply_window_mode is expected to set this


def _make_hidden_window():
    # "Static" is a predefined system class with its own working WNDPROC, so
    # this needs no RegisterClass/custom-callback wiring of its own.
    hinst = win32api.GetModuleHandle(None)
    return win32gui.CreateWindow(
        "Static", "claude-pet-selfcheck", win32con.WS_POPUP,
        0, 0, 10, 10, 0, 0, hinst, None,
    )


def _wait_for(cond, timeout=3.0):
    """
    Poll cond() while pumping this thread's message queue. The test window is
    owned by this (main) thread, and apply_window_mode's SetWindowLong style
    change is sent (not posted) to the owning thread - without pumping here,
    that call would block the background thread forever waiting for a
    message loop that never runs, deadlocking the test.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        win32gui.PumpWaitingMessages()
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def _test_read_usage():
    """read_usage() must hide stale data and surface fresh data untouched."""
    tmp_dir = tempfile.mkdtemp(prefix="claude_pet_usage_selfcheck_")
    pet_app.USAGE_PATH = os.path.join(tmp_dir, "usage.json")

    assert pet_app.read_usage() is None, "missing usage.json must read as None"

    fresh = {
        "five_hour": {"used_percentage": 42, "resets_at": "2026-08-01T12:00:00Z"},
        "seven_day": {"used_percentage": 17, "resets_at": "2026-08-05T00:00:00Z"},
        "updated_at": time.time(),
    }
    with open(pet_app.USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(fresh, f)
    assert pet_app.read_usage() == fresh, "fresh usage.json must be returned as-is"

    stale = dict(fresh, updated_at=time.time() - pet_app.USAGE_STALE_SECS - 1)
    with open(pet_app.USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(stale, f)
    assert pet_app.read_usage() is None, "usage.json older than USAGE_STALE_SECS must read as None"

    # The real writer is PowerShell's Set-Content -Encoding utf8, which emits a
    # BOM. Reading that as plain utf-8 throws, which silently hid every real
    # reading; only Python-written (BOM-less) fixtures passed before.
    with open(pet_app.USAGE_PATH, "w", encoding="utf-8-sig") as f:
        json.dump(fresh, f)
    with open(pet_app.USAGE_PATH, "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf", "fixture must actually carry a BOM"
    assert pet_app.read_usage() is not None, "usage.json with a UTF-8 BOM must still parse"

    print("OK: read_usage self-check passed")


def _test_read_activity():
    """read_activity() must hide stale data and surface fresh data untouched."""
    tmp_dir = tempfile.mkdtemp(prefix="claude_pet_activity_selfcheck_")
    pet_app.ACTIVITY_PATH = os.path.join(tmp_dir, "activity.json")

    assert pet_app.read_activity() is None, "missing activity.json must read as None"

    fresh = {"state": "tool", "tool": "Bash", "updated_at": time.time()}
    with open(pet_app.ACTIVITY_PATH, "w", encoding="utf-8-sig") as f:
        json.dump(fresh, f)
    assert pet_app.read_activity() == fresh, "fresh activity.json must be returned as-is"

    stale = dict(fresh, updated_at=time.time() - pet_app.ACTIVITY_STALE_SECS - 1)
    with open(pet_app.ACTIVITY_PATH, "w", encoding="utf-8-sig") as f:
        json.dump(stale, f)
    assert pet_app.read_activity() is None, "activity.json older than ACTIVITY_STALE_SECS must read as None"

    print("OK: read_activity self-check passed")


def _test_desktop_is_showing_no_hang():
    """
    desktop_is_showing() is called every 0.35s from a background thread per
    desktop-mode pet (sync_desktop_visibility). It walks every top-level
    window on the real desktop - read-only, no window of ours is touched -
    so this call is safe to make directly against live system state.
    Regression check for the cross-thread win32gui.GetWindowLong hang that
    froze pythonw.exe hard enough for Windows to kill it (AppHangB1) once
    the fix in _is_app_window was missed.
    """
    assert sys.platform == "win32", "this self-check only applies on Windows"
    result = {}

    def _call():
        result["value"] = pet_app.desktop_is_showing()

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "desktop_is_showing() did not return within 2s - hang regression"
    assert "value" in result and isinstance(result["value"], bool)
    print("OK: desktop_is_showing no-hang self-check passed")


def _test_read_vlc_media():
    """
    read_vlc_media() parses VLC's status.xml into the shape the progress bar
    expects, and refuses the cases where a bar would be a lie (stopped, or a
    stream with no known length).
    """
    import io
    import urllib.request

    def _status(state, time_s, length_s):
        return (
            f"<root><state>{state}</state><time>{time_s}</time>"
            f"<length>{length_s}</length><information>"
            "<category name='meta'><info name='filename'>holiday.mkv</info>"
            "</category></information></root>"
        )

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    real_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 300).encode())
        media = pet_app.read_vlc_media()
        assert media == {
            "title": "holiday.mkv",
            "position": 42.0,
            "duration": 300.0,
            "playing": True,
        }, f"unexpected parse: {media}"

        urllib.request.urlopen = lambda *a, **k: _Resp(_status("paused", 42, 300).encode())
        assert pet_app.read_vlc_media()["playing"] is False, "paused must still render a bar"

        urllib.request.urlopen = lambda *a, **k: _Resp(_status("stopped", 0, 300).encode())
        assert pet_app.read_vlc_media() is None, "stopped VLC must show no bar"

        # A live stream reports length 0 - a progress bar has nothing to fill.
        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 0).encode())
        assert pet_app.read_vlc_media() is None, "unknown length must show no bar"

        def _boom(*a, **k):
            raise OSError("connection refused")

        urllib.request.urlopen = _boom
        assert pet_app.read_vlc_media() is None, "VLC being closed is not an error"
    finally:
        urllib.request.urlopen = real_urlopen

    # get_media() must stay silent for the pets that front non-media apps,
    # so one shared media-bar snippet can live in any page.
    assert pet_app.Api(1080, lambda: None, "claude").get_media() is None

    print("OK: read_vlc_media self-check passed")


def main():
    _test_read_usage()
    _test_read_activity()
    _test_read_vlc_media()
    _test_desktop_is_showing_no_hang()

    assert sys.platform == "win32", "the rest of this self-check only applies on Windows"

    # Isolate config writes from the user's real %LOCALAPPDATA%\DesktopPet\config.json.
    tmp_dir = tempfile.mkdtemp(prefix="claude_pet_selfcheck_")
    pet_app.CONFIG_PATH = os.path.join(tmp_dir, "config.json")

    hwnd = _make_hidden_window()
    try:
        fake = _FakeWindow(hwnd)

        # --- taskbar/alt-tab button dropped, from before_show ---
        #
        # Deliberately no longer part of apply_window_mode: doing it after the
        # page loaded meant hiding and re-showing an already-visible window,
        # which raced pywebview's own Show()/Hide() transparency hack and left
        # pets rendering as opaque light rectangles. This must therefore happen
        # in the before_show handler and nowhere else.
        before = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        assert not (before & win32con.WS_EX_TOOLWINDOW), "fixture must start as a normal window"
        pet_app.hide_from_taskbar(fake)
        fake.events.before_show.fire(fake)
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        assert style & win32con.WS_EX_TOOLWINDOW, "must hide from taskbar/alt-tab"
        assert not (style & win32con.WS_EX_APPWINDOW), "must not have an appwindow button"

        # --- taskbar mode: topmost ---
        pet_app.apply_window_mode(fake, desktop=False, pet="selfcheck")
        _wait_for(lambda: fake.on_top is not None)
        assert fake.on_top is True, "taskbar mode must set window.on_top = True"
        assert hwnd not in pet_app._watched_pets, "taskbar mode must not start a desktop watcher"

        # --- switch to desktop mode: non-topmost, visibility watcher running ---
        pet_app.update_config("selfcheck", position="desktop")
        fake.on_top = None
        pet_app.apply_window_mode(fake, desktop=True, pet="selfcheck")
        _wait_for(lambda: fake.on_top is not None)
        assert fake.on_top is False, "desktop mode must set window.on_top = False"
        _wait_for(lambda: hwnd in pet_app._watched_pets)

        print("OK: apply_window_mode self-check passed")
    finally:
        win32gui.DestroyWindow(hwnd)
        time.sleep(0.5)  # let the watcher thread notice the window is gone and exit


if __name__ == "__main__":
    main()
