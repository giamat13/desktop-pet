"""
Self-check for apply_window_mode(). Run directly: `python test_pet_app.py`.

Creates one throwaway, invisible win32 window (never shown, never a "pet")
to verify the taskbar-hiding ex-style hack and the on_top hand-off to
pywebview's Window object actually do what apply_window_mode claims -
without ever creating a real, visible pet window on screen.
"""
import os
import sys
import time
import tempfile

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


class _FakeWindow:
    def __init__(self, hwnd):
        self.native = _FakeNative(hwnd)
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


def main():
    assert sys.platform == "win32", "this self-check only applies on Windows"

    # Isolate config writes from the user's real %LOCALAPPDATA%\ClaudePet\config.json.
    tmp_dir = tempfile.mkdtemp(prefix="claude_pet_selfcheck_")
    pet_app.CONFIG_PATH = os.path.join(tmp_dir, "config.json")

    hwnd = _make_hidden_window()
    try:
        fake = _FakeWindow(hwnd)

        # --- taskbar mode: topmost, hidden from the taskbar ---
        pet_app.apply_window_mode(fake, desktop=False, pet="selfcheck")
        _wait_for(lambda: fake.on_top is not None)
        assert fake.on_top is True, "taskbar mode must set window.on_top = True"
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        assert style & win32con.WS_EX_TOOLWINDOW, "must hide from taskbar/alt-tab"
        assert not (style & win32con.WS_EX_APPWINDOW), "must not have an appwindow button"
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
