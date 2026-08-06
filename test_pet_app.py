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
import win32process

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
    """
    read_activity() must hide stale data, surface fresh data, and - with
    several Claude Code sessions writing their own file - pick ONE of them to
    drive the pet while listing the rest.
    """
    tmp_dir = tempfile.mkdtemp(prefix="claude_pet_activity_selfcheck_")
    pet_app.ACTIVITY_GLOB = os.path.join(tmp_dir, "activity-*.json")

    def write(sid, obj):
        path = os.path.join(tmp_dir, "activity-%s.json" % sid)
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump(obj, f)
        return path

    assert pet_app.read_activity() is None, "no session files must read as None"

    fresh = {"state": "tool", "tool": "Bash", "label": "a", "updated_at": time.time()}
    write("a", fresh)
    got = pet_app.read_activity()
    assert got["tool"] == "Bash" and got["others"] == [], "a lone fresh session must drive the pet"

    write("a", dict(fresh, updated_at=time.time() - pet_app.ACTIVITY_STALE_SECS - 1))
    assert pet_app.read_activity() is None, "a session older than ACTIVITY_STALE_SECS must read as None"

    # Two working sessions: newest drives, older rides along in "others".
    write("a", dict(fresh, tool="Bash", label="a", updated_at=time.time() - 5))
    write("b", dict(fresh, tool="Edit", label="b", updated_at=time.time()))
    got = pet_app.read_activity()
    assert got["tool"] == "Edit", "the most recent working session must drive the pet"
    assert [o["label"] for o in got["others"]] == ["a"], "other live sessions must be listed"

    # Every session carries its own id + command, primary and others alike:
    # the pet's "lock onto one session" / split-into-bands modes follow
    # sessions by id across polls, and classify each one's activity (git vs
    # plain shell) off its own command.
    write("a", dict(fresh, tool="Bash", command="git commit", label="a",
                    updated_at=time.time() - 5))
    write("b", dict(fresh, tool="Bash", command="npm test", label="b",
                    updated_at=time.time()))
    got = pet_app.read_activity()
    assert got["session_id"] == "b", "primary must carry the id from its file name"
    assert got["command"] == "npm test", "primary must keep its own command"
    assert [(o["session_id"], o["command"]) for o in got["others"]] == [("a", "git commit")], \
        "every other session must carry its id and command too, not just primary"

    # A session that just went idle must not steal the pet from a busy one,
    # even though its Stop hook fired most recently.
    write("b", dict(fresh, state="idle", tool=None, label="b", updated_at=time.time() + 1))
    got = pet_app.read_activity()
    assert got["label"] == "a" and got["others"] == [], "an idle session must not outrank a working one"

    # Long-dead files get swept rather than accumulating one per session.
    dead = write("c", dict(fresh, updated_at=time.time() - pet_app.ACTIVITY_KEEP_SECS - 1))
    pet_app.read_activity()
    assert not os.path.exists(dead), "files older than ACTIVITY_KEEP_SECS must be deleted"

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

    def _status(state, time_s, length_s, volume=None):
        vol_tag = f"<volume>{volume}</volume>" if volume is not None else ""
        return (
            f"<root><state>{state}</state><time>{time_s}</time>"
            f"<length>{length_s}</length>{vol_tag}<information>"
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

        # volume: VLC's 256 == 100% scale, normalized to 0-1.
        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 300, volume=256).encode())
        assert pet_app.read_vlc_media()["volume"] == 1.0

        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 300, volume=128).encode())
        assert pet_app.read_vlc_media()["volume"] == 0.5

        # user-boosted past 100% in VLC's own UI must clamp, not exceed 1.0.
        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 300, volume=320).encode())
        assert pet_app.read_vlc_media()["volume"] == 1.0, "boosted volume must clamp to 1.0"

        # no <volume> element at all: key is omitted, not a crash and not a
        # fabricated 0 - this also keeps the exact-dict-equality assert above
        # (no "volume" key) valid for ordinary status.xml without one.
        urllib.request.urlopen = lambda *a, **k: _Resp(_status("playing", 42, 300).encode())
        assert "volume" not in pet_app.read_vlc_media()

        # unparseable <volume> must not break the rest of the bar.
        urllib.request.urlopen = lambda *a, **k: _Resp(
            _status("playing", 42, 300).replace("</length>", "</length><volume>nope</volume>").encode()
        )
        media = pet_app.read_vlc_media()
        assert media is not None and "volume" not in media
    finally:
        urllib.request.urlopen = real_urlopen

    # get_media() must stay silent for the pets that front non-media apps,
    # so one shared media-bar snippet can live in any page.
    assert pet_app.Api(1080, lambda: None, "claude").get_media() is None

    print("OK: read_vlc_media self-check passed")


class _FakeSimpleAudioVolume:
    def __init__(self, level, muted=False):
        self._level = level
        self._muted = muted

    def GetMasterVolume(self):
        return self._level

    def GetMute(self):
        return self._muted


class _FakeProcess:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeSession:
    def __init__(self, proc_name, level=1.0, muted=False):
        self.Process = _FakeProcess(proc_name) if proc_name else None
        self.SimpleAudioVolume = _FakeSimpleAudioVolume(level, muted)


def _test_read_spotify_volume():
    """
    read_spotify_volume() reads Spotify's own audio-session volume via pycaw,
    and fails soft (None) whenever the data just isn't there - pycaw missing,
    Spotify not running, no matching session - rather than raising, matching
    read_vlc_media/read_smtc_media's graceful-absence contract.
    """
    import sys
    import types

    real_modules = {k: sys.modules.get(k) for k in ("pycaw", "pycaw.pycaw")}
    try:
        # pycaw not installed: `sys.modules[name] = None` is the documented
        # way to make `import name` raise ImportError without it ever being
        # on disk, so this covers real "not installed" environments too.
        sys.modules["pycaw"] = None
        sys.modules["pycaw.pycaw"] = None
        assert pet_app.read_spotify_volume() is None, "pycaw missing must not raise"

        # pycaw present, but no session belongs to Spotify.
        fake_pycaw = types.ModuleType("pycaw.pycaw")
        fake_pycaw.AudioUtilities = types.SimpleNamespace(
            GetAllSessions=lambda: [_FakeSession("chrome.exe"), _FakeSession(None)]
        )
        sys.modules["pycaw"] = types.ModuleType("pycaw")
        sys.modules["pycaw.pycaw"] = fake_pycaw
        assert pet_app.read_spotify_volume() is None, "no Spotify session must be None, not a crash"

        # a real session, case-insensitive process match.
        fake_pycaw.AudioUtilities = types.SimpleNamespace(
            GetAllSessions=lambda: [_FakeSession("chrome.exe"), _FakeSession("Spotify.EXE", level=0.73)]
        )
        assert abs(pet_app.read_spotify_volume() - 0.73) < 1e-9

        # muted session must read as silence (0.0), not the pre-mute level.
        fake_pycaw.AudioUtilities = types.SimpleNamespace(
            GetAllSessions=lambda: [_FakeSession("Spotify.exe", level=0.9, muted=True)]
        )
        assert pet_app.read_spotify_volume() == 0.0

        # GetAllSessions() itself blowing up must not propagate.
        def _boom():
            raise OSError("audio service unavailable")

        fake_pycaw.AudioUtilities = types.SimpleNamespace(GetAllSessions=_boom)
        assert pet_app.read_spotify_volume() is None
    finally:
        for name, mod in real_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    print("OK: read_spotify_volume self-check passed")


def _test_read_vscode_dirty():
    """
    read_vscode_dirty() must find only Code.exe windows, read the dirty
    marker via the timeout-safe path, and treat "no window" / "read timed
    out" as absence rather than an exception.
    """
    import types

    assert sys.platform == "win32", "this self-check only applies on Windows"

    class _FakeProc:
        def name(self):
            return "code.exe"

    # --- no matching window at all ---
    real_enum = win32gui.EnumWindows
    try:
        win32gui.EnumWindows = lambda cb, extra: None  # calls cb on nothing
        assert pet_app.read_vscode_dirty() is None, "no VS Code window must read as None"
    finally:
        win32gui.EnumWindows = real_enum

    # --- one matching window; drive SendMessageTimeoutW by hand ---
    fake_hwnd = 4242

    def _fake_enum(cb, extra):
        cb(fake_hwnd, extra)

    real_enum = win32gui.EnumWindows
    real_visible = win32gui.IsWindowVisible
    real_open_process = win32api.OpenProcess
    real_get_tid_pid = win32process.GetWindowThreadProcessId
    real_get_module = win32process.GetModuleFileNameEx
    real_close_handle = win32api.CloseHandle
    real_send_timeout = pet_app.USER32.SendMessageTimeoutW
    try:
        win32gui.EnumWindows = _fake_enum
        win32gui.IsWindowVisible = lambda h: True
        win32process.GetWindowThreadProcessId = lambda h: (0, 999)
        win32api.OpenProcess = lambda *a, **k: object()
        win32process.GetModuleFileNameEx = lambda proc, mod: r"C:\Code\Code.exe"
        win32api.CloseHandle = lambda h: None

        def _send_with_title(title):
            def _fake(hwnd, msg, buf_len, buf, flags, timeout, result_ref):
                buf.value = title
                return 1  # nonzero == success
            return _fake

        # dirty: title carries the bullet marker
        pet_app.USER32.SendMessageTimeoutW = _send_with_title(
            "\u25CF app.py - myproject - Visual Studio Code"
        )
        assert pet_app.read_vscode_dirty() == {"dirty": True}, "bullet-prefixed title must report dirty"

        # clean: title has no marker
        pet_app.USER32.SendMessageTimeoutW = _send_with_title(
            "app.py - myproject - Visual Studio Code"
        )
        assert pet_app.read_vscode_dirty() == {"dirty": False}, "plain title must report not-dirty"

        # hung/unresponsive window: SendMessageTimeoutW returns 0 (failure)
        def _send_timeout(hwnd, msg, buf_len, buf, flags, timeout, result_ref):
            return 0
        pet_app.USER32.SendMessageTimeoutW = _send_timeout
        assert pet_app.read_vscode_dirty() is None, "a timed-out title read must be treated as absent, not raise"
    finally:
        win32gui.EnumWindows = real_enum
        win32gui.IsWindowVisible = real_visible
        win32api.OpenProcess = real_open_process
        win32process.GetWindowThreadProcessId = real_get_tid_pid
        win32process.GetModuleFileNameEx = real_get_module
        win32api.CloseHandle = real_close_handle
        pet_app.USER32.SendMessageTimeoutW = real_send_timeout

    # get_vscode_dirty() must stay gated to the vscode pet, same as get_media().
    assert pet_app.Api(1080, lambda: None, "claude").get_vscode_dirty() is None

    print("OK: read_vscode_dirty self-check passed")


def _test_read_discord_voice_state():
    """
    read_discord_voice_state() must treat a missing registry key as None (not
    an exception, since not every Windows build/edition has it), no matching
    "discord.exe" subkey as {"in_call": False} (Discord hasn't touched the mic
    yet), and LastUsedTimeStop == 0 (mic in use right now) as {"in_call": True}.
    """
    import winreg

    assert sys.platform == "win32", "this self-check only applies on Windows"

    class _Ctx:
        """OpenKey's real return value is a context manager; __enter__ hands
        back a handle. Here that handle is just a string sentinel identifying
        which fake key it is, which the fake OpenKey/EnumKey/QueryValueEx
        below all key off of - good enough since real registry handles are
        opaque to this function too, it never inspects them."""
        def __init__(self, sentinel):
            self._sentinel = sentinel

        def __enter__(self):
            return self._sentinel

        def __exit__(self, *a):
            return False

    real_open_key = winreg.OpenKey
    real_enum_key = winreg.EnumKey
    real_query = winreg.QueryValueEx

    def _install(base_ok, subkey_names, stop_times):
        def _open_key(hkey, path):
            if hkey == winreg.HKEY_CURRENT_USER:
                if not base_ok:
                    raise OSError("no such key")
                return _Ctx("BASEKEY")
            if hkey == "BASEKEY" and path in subkey_names:
                return _Ctx(path)
            raise OSError("no such subkey")

        def _enum_key(key, index):
            if key != "BASEKEY" or index >= len(subkey_names):
                raise OSError("no more values")
            return subkey_names[index]

        def _query(key, name):
            assert name == "LastUsedTimeStop"
            return (stop_times[key], 11)

        winreg.OpenKey = _open_key
        winreg.EnumKey = _enum_key
        winreg.QueryValueEx = _query

    try:
        _install(False, [], {})
        assert pet_app.read_discord_voice_state() is None, "missing registry key must read as None"

        other_app = r"C:#Some#Other#App.exe"
        _install(True, [other_app], {})
        assert pet_app.read_discord_voice_state() == {"in_call": False}, \
            "no discord.exe subkey must read as not-in-call"

        discord_key = r"C:#Users#me#AppData#Local#Discord#app-1.0.9#Discord.exe"
        _install(True, [discord_key], {discord_key: 0})
        assert pet_app.read_discord_voice_state() == {"in_call": True}, \
            "LastUsedTimeStop == 0 (mic in use now) must read as in-call"

        _install(True, [discord_key], {discord_key: 133713371337})
        assert pet_app.read_discord_voice_state() == {"in_call": False}, \
            "a nonzero stop time (mic released) must read as not-in-call"
    finally:
        winreg.OpenKey = real_open_key
        winreg.EnumKey = real_enum_key
        winreg.QueryValueEx = real_query

    # get_discord_voice() must stay gated to the discord pet, same as get_media().
    assert pet_app.Api(1080, lambda: None, "claude").get_discord_voice() is None

    print("OK: read_discord_voice_state self-check passed")


def _test_read_system_health():
    """
    read_system_health() must read cpu/ram from psutil and gracefully report
    battery_pct/on_battery as None on a desktop (sensors_battery() -> None
    there, per psutil's own documented behavior).
    """
    import psutil

    class _FakeMem:
        percent = 62.5

    class _FakeBattery:
        def __init__(self, percent, plugged):
            self.percent = percent
            self.power_plugged = plugged

    real_cpu = psutil.cpu_percent
    real_mem = psutil.virtual_memory
    real_batt = psutil.sensors_battery
    try:
        psutil.cpu_percent = lambda interval=None: 41.0
        psutil.virtual_memory = lambda: _FakeMem()
        psutil.sensors_battery = lambda: None  # desktop: no battery at all
        result = pet_app.read_system_health()
        assert result == {"cpu_pct": 41.0, "ram_pct": 62.5, "battery_pct": None, "on_battery": None}, result

        psutil.sensors_battery = lambda: _FakeBattery(77, False)
        result = pet_app.read_system_health()
        assert result["battery_pct"] == 77 and result["on_battery"] is True, \
            "unplugged must report on_battery True"

        psutil.sensors_battery = lambda: _FakeBattery(100, True)
        result = pet_app.read_system_health()
        assert result["on_battery"] is False, "plugged in must report on_battery False"
    finally:
        psutil.cpu_percent = real_cpu
        psutil.virtual_memory = real_mem
        psutil.sensors_battery = real_batt

    # get_system_health() must stay gated to the system pet, same as get_media().
    assert pet_app.Api(1080, lambda: None, "claude").get_system_health() is None

    print("OK: read_system_health self-check passed")


def main():
    _test_read_usage()
    _test_read_activity()
    _test_read_vlc_media()
    _test_read_spotify_volume()
    _test_read_vscode_dirty()
    _test_read_discord_voice_state()
    _test_read_system_health()
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
