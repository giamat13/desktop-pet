"""
Desktop Pet overlay.
A frameless, transparent, always-on-top sprite that sits on the taskbar,
does not appear in the taskbar/alt-tab as its own app, can be dragged,
and falls back down to the taskbar when released.
"""
import os
import sys
import glob
import json
import time
import ctypes
import subprocess
import threading
import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "pets", "settings.html")

# Which pet to run: `python pet_app.py [claude|vlc]` (default claude).
PETS = {
    "claude": "pets/claude_pet.html",
    "vlc": "pets/vlc-pet.html",
    "vscode": "pets/vscode-pet.html",
    "curseforge": "pets/curseforge-pet.html",
    "chrome": "pets/chrome-pet.html",
    "spotify": "pets/spotify-pet.html",
}

WIN_W, WIN_H = 340, 220  # wider than the 280px-wide sprite so ears/hover-scale never clip
DEFAULT_TASKBAR_MARGIN = 46  # used only as an initial guess for calibration

CONFIG_DIR = os.path.expandvars(r"%LOCALAPPDATA%\DesktopPet")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "log.txt")
USAGE_PATH = os.path.join(CONFIG_DIR, "usage.json")
# usage.json is refreshed by refresh_usage.py on a 5-minute scheduled task
# (and by any interactive `claude` session's status line). Anything older than
# this means the refresher has stopped, not that usage is genuinely 0 - the
# gauges hide rather than show a frozen number. Kept comfortably above the
# 5-minute refresh interval so one missed run doesn't blink the gauges out.
USAGE_STALE_SECS = 12 * 60

ACTIVITY_GLOB = os.path.join(CONFIG_DIR, "activity-*.json")
# claude-activity-status.ps1 (a Claude Code hook, not a poller) rewrites one
# of these per Claude Code session, on every
# UserPromptSubmit/PreToolUse/PostToolUse/Stop event, for ANY session on the
# machine. Short staleness window: this is meant to feel live, and hooks fire
# multiple times per turn under normal use.
ACTIVITY_STALE_SECS = 20
# A session that ends leaves its file behind forever. Nothing reads a file
# this old, so the read path sweeps them - no scheduled task for 200 bytes.
ACTIVITY_KEEP_SECS = 6 * 60 * 60


def log(*args):
    """
    Safe logging that never touches sys.stdout/sys.stderr.

    This app is normally launched via pythonw.exe (no console window), and
    under pythonw.exe sys.stdout and sys.stderr are actually None - not
    just hidden. A plain print() call in that situation raises
    AttributeError, and if that happens inside a thread that pywebview's
    JS<->Python bridge depends on, it can wedge the whole UI ("Python not
    responding"). Writing to a log file instead is always safe.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] "))
            f.write(" ".join(str(a) for a in args))
            f.write("\n")
    except Exception:
        pass  # logging must never itself raise


def _load_all_config():
    """Raw file contents: {pet_key: {taskbar_margin, size, position}, ...}."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_all_config(all_cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(all_cfg, f)
    except Exception as exc:
        log("save_config failed:", exc)


def load_config(pet):
    """Each pet (claude/vlc) keeps its own margin/size/position settings."""
    return _load_all_config().get(pet, {})


def update_config(pet, **changes):
    """Merge changes into one pet's config section without touching others."""
    all_cfg = _load_all_config()
    all_cfg.setdefault(pet, {}).update(changes)
    _save_all_config(all_cfg)


def get_margin(pet):
    """
    The vertical distance (px) the pet should rest above the bottom of the
    screen. Prefers the value the user calibrated by hand (most reliable,
    since taskbar auto-location can be off with some themes/scaling), and
    only falls back to an automatic OS guess if nothing was calibrated yet.
    """
    cfg = load_config(pet)
    if "taskbar_margin" in cfg:
        return cfg["taskbar_margin"]
    return get_taskbar_height()


def make_dpi_aware():
    """
    Match pywebview/WebView2's own per-monitor DPI awareness so every
    coordinate agrees in real, physical pixels. Without this, Python stays
    DPI-unaware and get_screen_size() reports a virtualized screen (e.g.
    1600x900 on a 120%-scaled 1920x1080 display), while WebView2 then places
    the window in physical pixels - so the pet is positioned against the
    wrong screen height and floats up mid-screen instead of resting on the
    taskbar. Must run before get_screen_size() and before create_window().
    """
    if sys.platform != "win32":
        return
    try:
        # PER_MONITOR_AWARE_V2 == DPI_AWARENESS_CONTEXT (-4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # system-DPI (Vista+)
            except Exception as exc:
                log("make_dpi_aware failed:", exc)


def get_screen_size():
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return 1920, 1080


def get_taskbar_height():
    """
    Returns the real height (in pixels) of the Windows taskbar by asking the
    OS directly, instead of a hardcoded guess. Works across different DPI /
    scaling settings and taskbar sizes (small/large icons, etc).
    """
    if sys.platform != "win32":
        return DEFAULT_TASKBAR_MARGIN

    try:
        import win32gui

        hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
        if hwnd:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            height = bottom - top
            if height > 0:
                return height
    except Exception as exc:
        log("get_taskbar_height (win32gui) failed:", exc)

    # Fallback: SHAppBarMessage, works even without pywin32
    try:
        class APPBARDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("hWnd", ctypes.c_void_p),
                ("uCallbackMessage", ctypes.c_uint32),
                ("uEdge", ctypes.c_uint32),
                ("rc", ctypes.c_long * 4),
                ("lParam", ctypes.c_long),
            ]

        ABM_GETTASKBARPOS = 0x00000005
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        res = ctypes.windll.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd))
        if res:
            top, bottom = abd.rc[1], abd.rc[3]
            height = bottom - top
            if height > 0:
                return height
    except Exception as exc:
        log("get_taskbar_height (SHAppBarMessage) failed:", exc)

    return DEFAULT_TASKBAR_MARGIN


def find_claude_desktop_exe():
    """
    Locates a classic (non-packaged) Claude Desktop executable, if one
    exists. Deliberately avoids Claude Code (the CLI, which opens in a
    console/CMD window instead of the actual desktop app). This is only a
    fallback: on most machines Claude Desktop ships as a packaged Windows
    app (MSIX), not a standalone .exe - see find_claude_app_user_model_id().
    """
    # 1) Known direct install locations for a classic Electron-style install.
    direct_candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\AnthropicClaude\claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Claude\Claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\Claude.exe"),
    ]
    for path in direct_candidates:
        if os.path.isfile(path):
            return path

    # 2) Windows "App Paths" registry entry (many installers register this).
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(
                    hive,
                    r"Software\Microsoft\Windows\CurrentVersion\App Paths\Claude.exe",
                )
                value, _ = winreg.QueryValueEx(key, None)
                if value and os.path.isfile(value):
                    return value
            except OSError:
                continue
    except Exception as exc:
        log("registry lookup for Claude.exe failed:", exc)

    # 3) Start Menu shortcuts - explicitly EXCLUDE anything mentioning "code"
    #    so we never pick "Claude Code.lnk" (that one opens a CMD window).
    search_dirs = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    ]
    candidates = []
    for d in search_dirs:
        if os.path.isdir(d):
            candidates += glob.glob(os.path.join(d, "**", "*laude*.lnk"), recursive=True)

    filtered = [c for c in candidates if "code" not in os.path.basename(c).lower()]
    if filtered:
        filtered.sort(key=lambda p: len(os.path.basename(p)))
        return filtered[0]

    return None


# Observed AppUserModelID for the packaged (MSIX) Claude Desktop app. Used
# only as a last-resort fallback if Get-StartApps can't be queried - the
# package-family hash is tied to Anthropic's signing identity, not to the
# individual machine, so it's expected to be stable across installs of the
# same build.
FALLBACK_APP_USER_MODEL_ID = "Claude_pzs8sxrjxfjjc!Claude"


def find_claude_app_user_model_id():
    """
    Claude Desktop today ships as a packaged Windows app (MSIX/UWP-style),
    installed under C:\\Program Files\\WindowsApps rather than as a plain
    .exe. Packaged apps are identified and launched by their
    AppUserModelID, not a file path. This asks Windows itself (via
    Get-StartApps, which lists BOTH packaged and classic Start Menu apps)
    for the entry that matches "Claude" - explicitly excluding "Claude
    Code", which is a console app and would pop up a CMD window instead.
    """
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        apps = json.loads(result.stdout)
        if isinstance(apps, dict):
            apps = [apps]
        candidates = [
            a
            for a in apps
            if "claude" in a.get("Name", "").lower()
            and "code" not in a.get("Name", "").lower()
        ]
        # Prefer the shortest / most exact name match (e.g. "Claude" over
        # "Claude - something").
        candidates.sort(key=lambda a: len(a.get("Name", "")))
        if candidates:
            return candidates[0]["AppID"]
    except Exception as exc:
        log("find_claude_app_user_model_id failed:", exc)
    return None


def launch_claude_desktop():
    """
    Opens the real Claude Desktop app. Because it's a packaged app, this
    must go through shell:AppsFolder\\<AppUserModelID> - NOT a raw .exe path
    and NOT the Start Menu .lnk directly - or Windows/Explorer may resolve
    it to the wrong registered handler (e.g. Claude Code's console entry).
    """
    app_id = find_claude_app_user_model_id() or FALLBACK_APP_USER_MODEL_ID

    try:
        os.startfile(f"shell:AppsFolder\\{app_id}")
        return
    except Exception as exc:
        log("shell:AppsFolder launch via os.startfile failed:", exc)

    try:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        return
    except Exception as exc:
        log("shell:AppsFolder launch via explorer.exe failed:", exc)

    # Last resort: maybe this machine actually has a classic .exe install.
    exe = find_claude_desktop_exe()
    if exe:
        try:
            os.startfile(exe)
            return
        except Exception as exc:
            log("Could not open Claude Desktop:", exc)


def launch_vlc():
    """Open VLC media player (the app the vlc-pet fronts for)."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\VideoLAN\VLC\vlc.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                os.startfile(path)
                return
            except Exception as exc:
                log("launch_vlc failed:", exc)
    # Fall back to the shell association (works if VLC is on PATH / registered).
    try:
        os.startfile("vlc")
    except Exception as exc:
        log("launch_vlc fallback failed:", exc)


def activate_app_window(exe_path):
    """
    Bring an already-running instance of exe_path to the front.
    Returns True if one of its windows was found.

    Needed because re-launching Code.exe while VS Code is already running is a
    no-op: the second process just hands off to the first and exits, without
    restoring or focusing anything. Clicking the pet then appears to do nothing
    at all. Once the app is up, "open it" has to mean "activate its window".
    """
    if sys.platform != "win32":
        return False
    try:
        import win32gui
        import win32con
        import win32api
        import win32process
    except ImportError:
        return False

    target = os.path.normcase(os.path.abspath(exe_path))
    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
            proc = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
            try:
                path = win32process.GetModuleFileNameEx(proc, 0)
            finally:
                win32api.CloseHandle(proc)
            if os.path.normcase(path) == target:
                found.append(hwnd)
        except Exception:
            pass  # most PIDs simply aren't ours to open - not an error
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:
        log("activate_app_window enum failed:", exc)
        return False

    if not found:
        return False
    hwnd = found[0]
    try:
        if win32gui.IsIconic(hwnd):
            show_window_async(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        # Windows refuses SetForegroundWindow unless the calling process owns
        # the current foreground. The user's click on the pet normally grants
        # that right; if it was refused anyway the window is at least restored.
        log("activate_app_window could not focus:", exc)
    return True


def launch_vscode():
    """Open VS Code, or focus the running instance (the app the vscode-pet fronts for)."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            if activate_app_window(path):
                return
            try:
                os.startfile(path)
                return
            except Exception as exc:
                log("launch_vscode failed:", exc)
    # Fall back to the "code" launcher on PATH.
    try:
        os.startfile("code")
    except Exception as exc:
        log("launch_vscode fallback failed:", exc)


def launch_curseforge():
    """Open CurseForge, or focus the running instance (the app the curseforge-pet fronts for)."""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\curseforge\CurseForge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Overwolf\CurseForge\CurseForge.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            if activate_app_window(path):
                return
            try:
                os.startfile(path)
                return
            except Exception as exc:
                log("launch_curseforge failed:", exc)
    try:
        os.startfile("curseforge")
    except Exception as exc:
        log("launch_curseforge fallback failed:", exc)


def launch_chrome():
    """Open Google Chrome, or focus the running instance (the app the chrome-pet fronts for)."""
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            if activate_app_window(path):
                return
            try:
                os.startfile(path)
                return
            except Exception as exc:
                log("launch_chrome failed:", exc)
    try:
        os.startfile("chrome")
    except Exception as exc:
        log("launch_chrome fallback failed:", exc)


def launch_spotify():
    """Open Spotify, or focus the running instance (the app the spotify-pet fronts for)."""
    # Spotify ships as a packaged (MSIX) app on this machine, so there is no
    # .exe to start - but it registers the `spotify:` URI scheme either way,
    # which Windows resolves to whichever install is present. Falls back to
    # the packaged app's AppUserModelID if the scheme isn't registered.
    for target in ("spotify:", "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"):
        try:
            os.startfile(target)
            return
        except Exception as exc:
            log("launch_spotify failed for", target, ":", exc)


_watched_pets = set()  # hwnds that already have a sync_desktop_visibility watcher


def show_window_async(hwnd, cmd):
    """
    ShowWindowAsync - never plain ShowWindow.

    Everything here runs on a background thread while the window itself belongs
    to pywebview's UI thread. Blocking ShowWindow *sends* its messages and waits
    for that thread; during WebView2 startup the UI thread is busy, so the wait
    wedges our setup thread mid-sequence. That left pets hidden forever - hidden
    by the SW_HIDE that did land, never re-shown by the SW_SHOWNA that never ran,
    with Windows spawning a "Ghost" window for the stalled one. Posting instead
    of sending removes the wait entirely.
    """
    try:
        USER32.ShowWindowAsync(hwnd, cmd)
    except Exception as exc:
        log("show_window_async failed:", exc)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# Shell-owned windows that are desktop-sized but are not "an app in front":
# the wallpaper host, the taskbar, and the various always-present shell
# surfaces. Matched by class name, which is free to read - see _is_app_window.
_NON_APP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "SHELLDLL_DefView", "SysListView32", "Button",
    "ForegroundStaging", "MultitaskingViewFrame", "XamlExplorerHostIslandWindow",
}


def _make_user32():
    """
    A private user32 handle with explicit signatures for everything this
    module calls.

    Two things this buys over plain `ctypes.windll.user32`:

    * ctypes assumes a C `int` return by default, which silently truncates
      the 64-bit HWNDs FindWindowW/GetWindow/GetForegroundWindow return on
      64-bit Python - the Z-order walk would then pass around corrupted
      handles.
    * `ctypes.windll.user32` is a process-wide cached object shared with
      every other library in this process (pywebview, pythonnet). Declaring
      argtypes on it would impose our signatures on their calls too. A
      separate WinDLL instance keeps that blast radius at zero.
    """
    if sys.platform != "win32":
        return None
    u = ctypes.WinDLL("user32")
    HWND = ctypes.c_void_p
    sigs = {
        "FindWindowW": (HWND, [ctypes.c_wchar_p, ctypes.c_wchar_p]),
        "GetWindow": (HWND, [HWND, ctypes.c_uint]),
        "GetForegroundWindow": (HWND, []),
        "IsWindow": (ctypes.c_bool, [HWND]),
        "IsWindowVisible": (ctypes.c_bool, [HWND]),
        "IsIconic": (ctypes.c_bool, [HWND]),
        "GetWindowLongW": (ctypes.c_long, [HWND, ctypes.c_int]),
        "SetWindowLongW": (ctypes.c_long, [HWND, ctypes.c_int, ctypes.c_long]),
        "GetWindowTextLengthW": (ctypes.c_int, [HWND]),
        "GetClassNameW": (ctypes.c_int, [HWND, ctypes.c_wchar_p, ctypes.c_int]),
        "GetWindowRect": (ctypes.c_bool, [HWND, ctypes.POINTER(_RECT)]),
        "ShowWindowAsync": (ctypes.c_bool, [HWND, ctypes.c_int]),
        "SetWindowPos": (ctypes.c_bool, [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_uint]),
    }
    for name, (restype, argtypes) in sigs.items():
        fn = getattr(u, name)
        fn.restype = restype
        fn.argtypes = argtypes
    return u


USER32 = _make_user32()


def desktop_is_showing(ignore_hwnd=0):
    """
    Is the desktop itself currently on show (WIN+D, "Show desktop", or every
    window minimized)?

    Two signals, OR'd:

    1) Z-order: walk top-level windows downwards from the top; whichever comes
       first settles it - the wallpaper above every app means the desktop is
       showing, a real app window first means it is not.
    2) Focus: GetForegroundWindow() is Progman/WorkerW.

    An earlier version used Z-order only, on the theory that focus was
    unreliable for WIN+D specifically - based on a test where synthetic input
    (Shell.Application.ToggleDesktop() / keybd_event from a spawned process)
    moved the Z-order but not the focus. That was an artifact of the test, not
    of WIN+D: Windows' foreground-lock rules let a genuine physical keypress
    move focus in cases where injected input from another process is refused.
    Live testing showed real WIN+D behaving differently from either signal
    alone, so both are checked and either is enough.

    Every win32 call below goes through ctypes, not win32gui/pywin32. This
    whole function runs on a background thread (sync_desktop_visibility's
    watcher, once per desktop-mode pet, every 0.35s), and on this machine
    several pywin32-wrapped user32 calls hang forever when made cross-thread
    while the same call via raw ctypes returns instantly - see
    apply_window_mode's GWL_EXSTYLE read/write for the first instance of this.
    A version of this function that used win32gui only for GetWindowLong
    still froze pythonw.exe hard enough for Windows to force-kill it
    (AppHangB1) twice in one session, so every call in this hot path was
    moved to ctypes rather than guessing which one was the culprit.
    """
    if sys.platform != "win32":
        return False
    user32 = USER32

    GW_HWNDFIRST = 0
    GW_HWNDNEXT = 2
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080

    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return False

    def _is_app_window(h):
        if h == ignore_hwnd or not user32.IsWindowVisible(h) or user32.IsIconic(h):
            return False
        # tool windows don't count, which conveniently also skips the other pets
        if user32.GetWindowLongW(h, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return False
        # Deliberately NOT a window-text check. GetWindowText/GetWindowTextLength
        # SEND WM_GETTEXT(LENGTH) to the owning process, so walking the Z-order
        # blocks on any app that is busy or hung - and this walk runs every
        # 0.35s per desktop-mode pet. That is what froze pythonw.exe hard
        # enough for Windows to force-kill it (AppHangB1), intermittently,
        # depending on which apps happened to be open. Class name is read from
        # kernel-side window state and sends nothing, so it can never block.
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, buf, 64)
        if buf.value in _NON_APP_CLASSES:
            return False
        rect = _RECT()
        user32.GetWindowRect(h, ctypes.byref(rect))
        return (rect.right - rect.left) > 200 and (rect.bottom - rect.top) > 200

    try:
        h = user32.GetWindow(progman, GW_HWNDFIRST)
        for _ in range(400):  # bounded: never spin on a malformed Z-order
            if not h:
                break
            if h == progman:
                return True
            if _is_app_window(h):
                break  # a real app is in front by z-order; fall through to the focus check
            h = user32.GetWindow(h, GW_HWNDNEXT)
    except Exception as exc:
        log("desktop_is_showing z-order check failed:", exc)

    try:
        fg = user32.GetForegroundWindow()
        if fg and fg != ignore_hwnd:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(fg, buf, 256)
            if buf.value in ("Progman", "WorkerW"):
                return True
    except Exception as exc:
        log("desktop_is_showing focus check failed:", exc)
    return False


def sync_desktop_visibility(hwnd, pet):
    """
    Keep a desktop-mode pet always shown, but nudge it back above the
    wallpaper during Show Desktop (WIN+D) so it survives the one moment
    Progman gets raised to the top of the Z-order. The rest of the time this
    thread does nothing, so a real app in front keeps covering the pet
    exactly like it would any other ordinary, un-parented window.

    "Show Desktop" (WIN+D) does NOT minimize anything - measured: windows keep
    IsWindowVisible()==1 and IsIconic()==0 right through it. It raises the
    wallpaper window (Progman) to the top of the Z-order, burying whatever sits
    below. Earlier attempts got this wrong in both directions:

      * HWND_BOTTOM pinned the pet *under* the full-screen wallpaper, so Show
        Desktop covered it.
      * Re-parenting into Progman survived WIN+D but put the pet in the
        wallpaper layer, where it receives no mouse input at all - click, drag
        and middle-click settings all died.
      * An unconditional/periodic HWND_TOP reassert - even gated on "pet
        should currently be visible" - fights the user's own window
        switching: it isn't scoped to "only when nothing real is in front",
        so it also fires while a real app *is* in front, and each tick wins
        the race back over whatever the user just focused, leaving the pet
        floating above every background app until that app is clicked again.
      * Actually hiding the pet during Show Desktop (rather than just letting
        it fall behind whatever's in front) was the opposite of what a
        "desktop pet" should do - you want to see it precisely when you're
        looking at the desktop, not have it vanish then.

    Gating the nudge strictly on desktop_is_showing() sidesteps the third
    failure mode: by definition, nothing real is "in front" to fight with
    while the desktop itself is what's on show.
    """
    if hwnd in _watched_pets:  # toggling modes must not stack watchers
        return
    _watched_pets.add(hwnd)

    def _watch():
        # Every call here is ctypes, not win32gui/pywin32 - same cross-thread
        # hang risk as desktop_is_showing(), which this thread also calls.
        user32 = USER32
        HWND_TOP = 0
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        # post the request instead of sending it, so this thread never waits
        # on the UI thread - see show_window_async().
        SWP_ASYNCWINDOWPOS = 0x4000
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS
        try:
            while True:
                time.sleep(0.35)
                try:
                    if not user32.IsWindow(hwnd):
                        return  # pet closed
                    if load_config(pet).get("position", "taskbar") != "desktop":
                        return  # switched to taskbar mode - nothing left for this thread to do
                    if desktop_is_showing(ignore_hwnd=hwnd):
                        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags)
                except Exception:
                    pass  # a transient shell state must never kill the watcher
        finally:
            _watched_pets.discard(hwnd)

    threading.Thread(target=_watch, daemon=True).start()


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000


def hide_from_taskbar(window):
    """
    Drop the taskbar/alt-tab button - before the window is ever shown.

    WS_EX_TOOLWINDOW is only read by the shell at the moment a window first
    becomes visible; flipping it later has no effect on an existing taskbar
    button. That is why this used to live in apply_window_mode as a
    hide -> set style -> re-show dance, running from the "loaded" event.

    That dance was the bug behind both "the pets sometimes have a white
    background" and "sometimes they show up as windows". pywebview makes a
    transparent WebView2 window work with a Show()/Hide() sequence of its own
    around WebView2 startup, plus another Show() from the NavigationStarting
    handler - a hack its own source comments admit it does not understand
    ("no idea why this works", platforms/winforms.py). Our extra
    SW_HIDE/SW_SHOWNA landed in the middle of that, at a moment whose timing
    depends on how far along the other four pets are in their own WebView2
    startup - so it raced, differently on every login. A pet that lost the
    race ended up with a WebView2 that never went transparent, which shows
    the WinForms form's own SystemColors.Control backdrop: a plain, opaque,
    near-white 340x220 rectangle. That is the "white background", and it is
    also exactly what makes a pet "look like a window".

    pywebview's `before_show` event fires on the GUI thread after the Form is
    constructed (so window.native and its HWND already exist) and before
    anything shows it. Setting the style there means the shell never sees a
    normal window to begin with, so nothing has to be hidden and re-shown to
    correct it - and WebView2's startup is left completely alone.

    Must be called before webview.start() (windows are only really created
    there, so the event has not fired yet).
    """
    if sys.platform != "win32":
        return

    def _before_show(window):
        try:
            hwnd = window.native.Handle.ToInt32()
            style = USER32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            USER32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception as exc:
            log("hide_from_taskbar failed:", exc)

    window.events.before_show += _before_show


def apply_window_mode(window, desktop, pet):
    """
    Place the pet in the window stack, and keep it there.

    Topmost state goes through pywebview's own `window.on_top` instead of a
    raw SetWindowPos(HWND_TOPMOST/NOTOPMOST) hack: pywebview's WinForms
    backend sets the .NET Form's TopMost once from the `on_top=` constructor
    arg and never looks at it again, so a raw native override left pywebview
    believing the window was still topmost when it wasn't (or vice versa) -
    a divergence between what the app thinks and what's actually on screen.

    Taskbar/alt-tab hiding deliberately does NOT happen here - see
    hide_from_taskbar() for why doing it from this (post-load) path broke
    window transparency.

    desktop=False -> topmost, the classic always-above-the-taskbar pet.
    desktop=True  -> non-topmost; visibility is driven by
                     sync_desktop_visibility(): on show while the desktop is,
                     hidden otherwise.
    """
    if sys.platform != "win32":
        return

    def _apply():
        try:
            # window.native is the WinForms Form; its handle is realized in
            # the Form constructor, long before "loaded" fires, so it's
            # always ready here - no FindWindow(title) polling needed.
            hwnd = window.native.Handle.ToInt32()

            # Place it in the stack.
            #
            #    Only when it actually has to change. pywebview's on_top
            #    getter reads a cached Python value, but its setter does
            #    `Form.TopMost = x`, which marshals onto that window's UI
            #    thread and blocks until that thread pumps messages. At
            #    startup the UI thread is still bringing up WebView2 while the
            #    main thread is busy creating the remaining pets, so this
            #    assignment deadlocked the process: py-spy caught _apply
            #    parked in set_on_top, MainThread parked in create_window, and
            #    two pywebview generate_js_object threads parked on evaluate_js
            #    locks. Windows then killed the whole app as unresponsive
            #    (AppHangB1), taking every pet down at once.
            #
            #    create_window(on_top=...) already set this correctly, so at
            #    startup the guard makes it a no-op and no marshaling happens.
            #    A later switch from the settings window still applies, and by
            #    then the UI thread is idle and servicing messages normally.
            if window.on_top != (not desktop):
                window.on_top = not desktop
            if not desktop:
                return

            # Desktop mode: an ordinary window, shown only while the desktop is.
            sync_desktop_visibility(hwnd, pet)
        except Exception as exc:
            log("apply_window_mode failed:", exc)

    threading.Thread(target=_apply, daemon=True).start()


def read_usage():
    """
    Live (5h) and weekly (7d) Claude usage percentages, as last written by
    claude-usage-statusline.ps1. Returns None (no data / stale) or
    {"five_hour": {"used_percentage", "resets_at"}, "seven_day": {...}} -
    the pet hides its gauges on None instead of showing frozen numbers.
    """
    # utf-8-sig, not utf-8: claude-usage-statusline.ps1 writes this file with
    # Set-Content -Encoding utf8, which emits a BOM. Plain utf-8 leaves the BOM
    # in the text and json.load then fails on it, so every real status-line
    # write was silently discarded and the gauges stayed hidden - while tests
    # that wrote the file from Python (no BOM) passed. utf-8-sig reads both.
    try:
        with open(USAGE_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return None
    if time.time() - data.get("updated_at", 0) > USAGE_STALE_SECS:
        return None
    return data


def read_activity():
    """
    Claude's current activity ("thinking" / "tool" + tool name / "idle"), as
    last written by the claude-activity-status.ps1 hook. Returns None (no
    data / stale) or {"state", "tool", "label", "updated_at", "others"} -
    stale here just means no session has fired a hook recently, so the pet
    hides the badge instead of showing a frozen state.

    One hook file per session, so with several Claude Code windows open the
    pet still has ONE body: the newest working session drives the animation
    and the rest ride along in "others" as small chips. Sorting by updated_at
    and preferring a working session over an idle one is what stops a
    finished session from stealing the pet away from a running one.
    """
    now = time.time()
    sessions = []
    for path in glob.glob(ACTIVITY_GLOB):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue
        age = now - data.get("updated_at", 0)
        if age > ACTIVITY_KEEP_SECS:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if age > ACTIVITY_STALE_SECS:
            continue
        sessions.append(data)

    if not sessions:
        return None
    sessions.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
    working = [d for d in sessions if d.get("state") not in (None, "idle")]
    if not working:
        return sessions[0]  # everything idle - the pet stands down
    primary = dict(working[0])
    primary["others"] = [
        {"state": d.get("state"), "tool": d.get("tool"), "label": d.get("label")}
        for d in working[1:]
    ]
    return primary


# ---------------------------------------------------------------- media ---
# Two different sources, because the two apps expose "what is playing and how
# far in" in two completely different ways:
#
#   Spotify -> SMTC (Windows' System Media Transport Controls), the same
#              session data behind the volume-key media flyout. No config
#              needed on Spotify's side.
#   VLC     -> its own HTTP interface. VLC 3.x does not publish to SMTC at all
#              (there is no such plugin in its plugins/ tree, and the media
#              flyout stays empty while it plays), so SMTC would show an empty
#              bar forever. install.ps1 enables the HTTP interface in vlcrc.
VLC_HTTP_PORT = 8080
VLC_HTTP_PASSWORD = "desktoppet"
MEDIA_CACHE_SECS = 0.4  # the pages poll ~1/s; this only absorbs double-polls
_media_cache = {}  # source -> (fetched_at, value)


def read_vlc_media():
    """
    What VLC is playing right now, from its localhost HTTP interface.
    Returns None when VLC is closed, stopped, or the interface is off (the
    pet then just doesn't show a bar). Otherwise
    {"title", "position", "duration", "playing"} with seconds as floats.
    """
    import base64
    import urllib.request
    import xml.etree.ElementTree as ET

    url = f"http://127.0.0.1:{VLC_HTTP_PORT}/requests/status.xml"
    # VLC's HTTP interface uses Basic auth with an empty username.
    auth = base64.b64encode(f":{VLC_HTTP_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
    try:
        # Short timeout on purpose: this is polled from the UI. A dead or
        # firewalled port must fail fast, not stall the bar.
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            root = ET.parse(resp).getroot()
    except Exception:
        return None  # VLC not running / interface not enabled - not an error

    state = (root.findtext("state") or "").lower()
    if state == "stopped":
        return None
    try:
        duration = float(root.findtext("length") or 0)
        position = float(root.findtext("time") or 0)
    except ValueError:
        return None
    if duration <= 0:
        return None  # live stream or nothing loaded: a progress bar is meaningless

    title = None
    meta = root.find("./information/category[@name='meta']")
    if meta is not None:
        # "title" is the tag if the file has one, "filename" always exists.
        for key in ("now_playing", "title", "filename"):
            el = meta.find(f"./info[@name='{key}']")
            if el is not None and el.text:
                title = el.text
                break
    return {
        "title": title or "VLC",
        "position": position,
        "duration": duration,
        "playing": state == "playing",
    }


def read_smtc_media(app_match):
    """
    What `app_match` (matched against the media session's source app id) is
    playing right now, via Windows' System Media Transport Controls.
    Returns the same shape as read_vlc_media(), or None.
    """
    import asyncio
    import datetime as dt

    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )
    except ImportError:
        return None  # winrt packages not installed - pet just shows no bar

    async def _read():
        manager = await SessionManager.request_async()
        for session in manager.get_sessions():
            source = session.source_app_user_model_id or ""
            if app_match.lower() not in source.lower():
                continue
            props = await session.try_get_media_properties_async()
            timeline = session.get_timeline_properties()
            duration = timeline.end_time.total_seconds()
            position = timeline.position.total_seconds()
            # PlaybackStatus.PLAYING == 4
            playing = int(session.get_playback_info().playback_status) == 4
            if playing:
                # SMTC only refreshes `position` when the app pushes an
                # update, which Spotify does every few seconds at best. Left
                # raw, the bar would visibly stutter and lag; last_updated_time
                # is exactly the anchor needed to carry it forward smoothly.
                try:
                    elapsed = (dt.datetime.now(dt.timezone.utc) - timeline.last_updated_time).total_seconds()
                    position += max(0.0, elapsed)
                except Exception:
                    pass
            if duration <= 0:
                return None
            title = props.title or ""
            artist = props.artist or ""
            return {
                "title": f"{artist} - {title}" if artist and title else (title or artist or "Spotify"),
                "position": min(position, duration),
                "duration": duration,
                "playing": playing,
            }
        return None

    try:
        return asyncio.run(_read())
    except Exception as exc:
        log("read_smtc_media failed:", exc)
        return None


def read_media(source):
    """Cached front door for the pets' progress bars. source: 'vlc'|'spotify'."""
    cached = _media_cache.get(source)
    if cached and time.time() - cached[0] < MEDIA_CACHE_SECS:
        return cached[1]
    value = read_vlc_media() if source == "vlc" else read_smtc_media("spotify")
    _media_cache[source] = (time.time(), value)
    return value


def get_pet_config(pet):
    """Everything the pet / settings pages need to render themselves."""
    cfg = load_config(pet)
    return {
        "margin": get_margin(pet),
        "size": cfg.get("size", 100),
        "position": cfg.get("position", "taskbar"),
    }


class Api:
    def __init__(self, screen_h, open_target, pet):
        # NOTE: must stay underscore-prefixed. pywebview introspects every
        # public attribute of the js_api object to expose it to JS, and would
        # otherwise recurse forever into window.native.AccessibilityObject
        # .Bounds.Empty... (WinForms Rectangle.Empty returns a Rectangle),
        # hitting "maximum recursion depth exceeded" during page load.
        self._window = None
        self._open_target = open_target  # callable that opens the fronted app
        self._pet = pet  # config namespace: "claude" or "vlc" - each keeps its own settings
        self._settings_window = None
        self.dragging = False
        self.screen_h = screen_h

    # Both pets call their own "open" name; expose both, same action.
    def open_claude(self):
        self._open_target()

    def open_app(self):
        self._open_target()

    def get_pet_config(self):
        """
        JS -> Python is the safe/proven direction here; the reverse (Python
        pushing into JS via evaluate_js from the "loaded" event) can deadlock
        the GUI thread. The page calls this once pywebview.api is ready.
        """
        return get_pet_config(self._pet)

    def get_usage(self):
        """Polled from JS on a timer to drive the live/weekly usage gauges."""
        return read_usage()

    def get_activity(self):
        """Polled from JS on a short timer to drive the activity badge."""
        return read_activity()

    def get_media(self):
        """
        Polled from JS to drive the VLC / Spotify progress bars. None for
        every other pet, so one shared media-bar snippet can live in any page.
        """
        if self._pet not in ("vlc", "spotify"):
            return None
        return read_media(self._pet)

    def open_settings_window(self):
        """Middle-click the pet -> open settings in its own native window."""
        try:
            if self._settings_window is not None:
                return  # already open
            sapi = SettingsApi(self._window, self.screen_h, self._pet)
            sw = webview.create_window(
                title="הגדרות",
                url=SETTINGS_PATH,
                width=320,
                height=340,
                on_top=True,
                resizable=False,
                js_api=sapi,
            )
            sapi._window = sw
            self._settings_window = sw

            def _closed():
                self._settings_window = None

            sw.events.closed += _closed
        except Exception as exc:
            log("open_settings_window failed:", exc)

    def start_drag(self):
        self.dragging = True

    def drag_move(self, dx, dy):
        if not self.dragging or not self._window:
            return
        try:
            self._window.move(int(self._window.x + dx), int(self._window.y + dy))
        except Exception as exc:
            log("drag_move error:", exc)

    def drop(self):
        self.dragging = False
        # Desktop mode: the pet stays wherever it's dropped (it lives on the
        # wallpaper). Only taskbar mode snaps back down onto the taskbar.
        if load_config(self._pet).get("position", "taskbar") != "desktop":
            threading.Thread(target=self._fall, daemon=True).start()
        else:
            # Remember exactly where it was dropped so it reappears there after a restart.
            try:
                update_config(self._pet, x=int(self._window.x), y=int(self._window.y))
            except Exception as exc:
                log("drop save position failed:", exc)

    def _fall(self):
        if not self._window:
            return
        try:
            screen_w, screen_h = get_screen_size()
            target_y = screen_h - WIN_H - get_margin(self._pet)
            x = min(max(0, self._window.x), max(0, screen_w - WIN_W))
            y = self._window.y
            self._window.move(int(x), int(y))
            while y < target_y - 1:
                step = max(4, int((target_y - y) * 0.25))
                y = min(target_y, y + step)
                self._window.move(int(x), int(y))
                time.sleep(0.016)
            # Remember horizontal spot across restarts (y is derived from the margin).
            update_config(self._pet, x=int(x))
        except Exception as exc:
            log("_fall failed:", exc)


class SettingsApi:
    """js_api for the separate settings window. Edits the pet live + persists."""

    def __init__(self, pet_window, screen_h, pet):
        self._window = None          # the settings window itself
        self._pet_window = pet_window
        self._pet = pet  # config namespace: "claude" or "vlc"
        self.screen_h = screen_h

    def get_pet_config(self):
        return get_pet_config(self._pet)

    def _move_pet(self, margin):
        if not self._pet_window:
            return
        try:
            y = self.screen_h - WIN_H - int(margin)
            self._pet_window.move(int(self._pet_window.x), int(y))
        except Exception as exc:
            log("settings _move_pet failed:", exc)

    def set_margin(self, margin):
        """Fine-tune slider: live-move the pet and persist the exact margin."""
        try:
            margin = int(margin)
            self._move_pet(margin)
            update_config(self._pet, taskbar_margin=margin)
        except Exception as exc:
            log("set_margin failed:", exc)

    def set_position(self, mode):
        """
        Switch placement mode. 'taskbar' = always-on-top, rests on the taskbar
        and falls back to it. 'desktop' = sinks to the wallpaper layer (only
        visible when everything is minimized) and no longer falls. Returns the
        resting margin so the settings slider can re-sync (None for desktop).
        """
        try:
            desktop = (mode == "desktop")
            update_config(self._pet, position="desktop" if desktop else "taskbar")
            apply_window_mode(self._pet_window, desktop, self._pet)
            if desktop:
                return None
            margin = get_margin(self._pet)
            self._move_pet(margin)
            return margin
        except Exception as exc:
            log("set_position failed:", exc)
            return None

    def set_size(self, pct):
        """Relay size to the pet page (evaluate_js is safe from a click)."""
        try:
            pct = int(pct)
            update_config(self._pet, size=pct)
            if self._pet_window:
                self._pet_window.evaluate_js(f"applyPetScale({pct})")
        except Exception as exc:
            log("set_size failed:", exc)

    def close_settings(self):
        try:
            if self._window:
                self._window.destroy()
        except Exception as exc:
            log("close_settings failed:", exc)


def main():
    make_dpi_aware()
    # `pet_app.py` (no args) shows every pet side by side.
    # `pet_app.py claude` / `pet_app.py vlc` shows just one.
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else None
    pets = [arg] if arg in PETS else list(PETS)

    screen_w, screen_h = get_screen_size()
    gap = 20
    start_x = screen_w - len(pets) * (WIN_W + gap) - 60

    for i, pet in enumerate(pets):
        cfg = load_config(pet)
        html_path = os.path.join(APP_DIR, PETS[pet])
        open_target = {"vlc": launch_vlc, "vscode": launch_vscode, "curseforge": launch_curseforge,
                       "chrome": launch_chrome, "spotify": launch_spotify}.get(pet, launch_claude_desktop)
        window_title = {"vlc": "VLC Pet", "vscode": "VS Code Pet", "curseforge": "CurseForge Pet",
                        "chrome": "Chrome Pet", "spotify": "Spotify Pet"}.get(pet, "Desktop Pet")

        # Reopen where the user last left it; fall back to the side-by-side layout.
        default_x = start_x + i * (WIN_W + gap)
        resting_y = screen_h - WIN_H - get_margin(pet)
        win_x = cfg.get("x", default_x)
        desktop = cfg.get("position", "taskbar") == "desktop"
        # Taskbar pets always rest on the taskbar; only desktop pets keep a free y.
        win_y = cfg.get("y", resting_y) if desktop else resting_y

        api = Api(screen_h, open_target, pet)

        window = webview.create_window(
            title=window_title,
            url=html_path,
            width=WIN_W,
            height=WIN_H,
            x=int(win_x),
            y=int(win_y),
            frameless=True,
            easy_drag=False,
            on_top=not desktop,
            transparent=True,
            resizable=False,
            js_api=api,
        )
        api._window = window
        # Must be armed before webview.start(); see hide_from_taskbar().
        hide_from_taskbar(window)

        def _on_loaded(window=window, pet=pet):
            if sys.platform == "win32":
                desktop = load_config(pet).get("position", "taskbar") == "desktop"
                apply_window_mode(window, desktop, pet)

        window.events.loaded += _on_loaded

    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        log("FATAL uncaught exception in main():\n" + traceback.format_exc())
