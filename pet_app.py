"""
Claude Pet desktop overlay.
A frameless, transparent, always-on-top sprite that can live either
anchored to the taskbar (falls back down to it when dropped) or free on
the desktop (stays exactly wherever it's placed). Does not appear in the
taskbar/alt-tab as its own app, and can be dragged around either way.

Each pet has its own id and its own saved settings (mode, taskbar margin,
desktop position, size). Only one pet ("claude") is wired up today, but
the config format and Api class are already per-pet so more sprites can
be added later without another rewrite.
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
HTML_PATH = os.path.join(APP_DIR, "claude_pet.html")
SETTINGS_HTML_PATH = os.path.join(APP_DIR, "settings_pet.html")

WIN_W, WIN_H = 340, 220  # wider than the 280px-wide sprite so ears/hover-scale never clip
DEFAULT_TASKBAR_MARGIN = 46  # used only as an initial guess for calibration

# Size of the settings window. This used to be done by growing the pet's
# own transparent overlay window bigger and drawing an HTML card inside it;
# now the settings UI is a genuinely separate pywebview window (framed,
# normal, shows in the taskbar like any other app window), created on
# demand via webview.create_window() - pywebview supports spinning up new
# windows like this even after the main GUI loop is already running. The
# pet's little sprite window is never resized at all anymore.
SETTINGS_WIN_W, SETTINGS_WIN_H = 360, 460

DEFAULT_PET_ID = "claude"

CONFIG_DIR = os.path.expandvars(r"%LOCALAPPDATA%\ClaudePet")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "log.txt")


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


# ---------------------------------------------------------------------------
# Config: versioned, per-pet. Each pet gets its own dict keyed by pet id:
#   {
#     "version": 2,
#     "pets": {
#       "claude": {
#         "mode": "taskbar" | "desktop",
#         "taskbar_margin": 46,
#         "desktop_x": 800, "desktop_y": 500,
#         "scale_pct": 100
#       }
#     }
#   }
# Older installs only ever wrote a flat {"taskbar_margin": 46}; that gets
# migrated into this shape (as the "claude" pet) the first time it's read.
# ---------------------------------------------------------------------------

def _default_pet_config():
    return {
        "mode": "taskbar",       # "taskbar" (snaps to the bottom) or "desktop" (stays put)
        "taskbar_margin": None,  # None => not calibrated yet, fall back to OS auto-detect
        "desktop_x": None,
        "desktop_y": None,
        "scale_pct": 100,
    }


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    if "pets" not in cfg:
        # Legacy flat format from before multi-pet / desktop-mode support.
        legacy_margin = cfg.get("taskbar_margin")
        migrated = {"version": 2, "pets": {}}
        if legacy_margin is not None:
            migrated["pets"][DEFAULT_PET_ID] = {
                **_default_pet_config(),
                "taskbar_margin": legacy_margin,
            }
        cfg = migrated

    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as exc:
        log("save_config failed:", exc)


def get_pet_config(pet_id):
    """Full, defaulted settings for one pet. taskbar_margin is resolved to a
    real number (auto-detected if the user never calibrated it)."""
    cfg = load_config()
    pet_cfg = {**_default_pet_config(), **cfg.get("pets", {}).get(pet_id, {})}
    if pet_cfg["taskbar_margin"] is None:
        pet_cfg["taskbar_margin"] = get_taskbar_height()
    return pet_cfg


def update_pet_config(pet_id, **updates):
    """Merge `updates` into the saved config for one pet and persist it."""
    cfg = load_config()
    cfg.setdefault("version", 2)
    pets = cfg.setdefault("pets", {})
    pet_cfg = {**_default_pet_config(), **pets.get(pet_id, {})}
    pet_cfg.update(updates)
    pets[pet_id] = pet_cfg
    save_config(cfg)
    return pet_cfg


def get_margin(pet_id=DEFAULT_PET_ID):
    return get_pet_config(pet_id)["taskbar_margin"]


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

# Cache: once Get-StartApps successfully resolves the AppID, reuse it for
# the rest of this run instead of re-running that (slow, sometimes
# multi-second) PowerShell query on every single click of the pet. A
# failure is deliberately NOT cached, so a transient hiccup can still
# succeed next time - but this means launch_claude_desktop() should always
# be called off the UI thread (see Api.open_claude), since even one such
# call can take several seconds, and it can occasionally hit its own
# internal timeout.
_app_user_model_id_cache = None


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
    global _app_user_model_id_cache
    if _app_user_model_id_cache is not None:
        return _app_user_model_id_cache

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
            _app_user_model_id_cache = candidates[0]["AppID"]
            return _app_user_model_id_cache
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


def hide_from_taskbar(title):
    """Strip the taskbar/alt-tab entry so only the sprite is visible, no app window."""
    try:
        import win32gui
        import win32con
    except ImportError:
        log("pywin32 not installed - window may still show in the taskbar. Run: pip install pywin32")
        return

    def _apply():
        try:
            for _ in range(50):
                hwnd = win32gui.FindWindow(None, title)
                if hwnd:
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    style = (style | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW
                    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    return
                time.sleep(0.1)
        except Exception as exc:
            log("hide_from_taskbar thread failed:", exc)

    threading.Thread(target=_apply, daemon=True).start()


class SettingsApi:
    """
    js_api object for the settings window. A thin wrapper around the pet's
    own Api - it just forwards calls to it - except it also owns the
    settings window handle itself, so it can close that window (e.g. after
    Save) without the pet's Api needing to know about it.
    """

    def __init__(self, pet_api):
        self._pet_api = pet_api
        self._window = None

    def get_settings(self):
        return self._pet_api.get_settings()

    def set_mode(self, mode):
        self._pet_api.set_mode(mode)

    def set_calibration_margin(self, margin):
        self._pet_api.set_calibration_margin(margin)

    def finish_calibration(self, margin):
        self._pet_api.finish_calibration(margin)

    def set_scale(self, pct):
        # Persist it, and also live-preview it on the pet sprite itself
        # (which lives in a different window) while the slider is dragged.
        self._pet_api.set_scale(pct)
        self._pet_api.preview_scale(pct)

    def close(self):
        """
        Hides the window rather than destroying it - this settings window
        is created once up front (see main()) and reused every time via
        show()/hide(), instead of being created fresh each time. Creating
        native windows on demand, after the GUI loop is already running,
        turned out to be the thing causing "Python is not responding"
        freezes - show()/hide() on an already-existing window is a much
        more basic, reliable operation.
        """
        if self._window:
            try:
                self._window.hide()
            except Exception as exc:
                log("SettingsApi.close failed:", exc)
        self._pet_api._settings_open = False


class Api:
    def __init__(self, pet_id, screen_w, screen_h):
        # NOTE: must stay underscore-prefixed. pywebview introspects every
        # public attribute of the js_api object to expose it to JS, and would
        # otherwise recurse forever into window.native.AccessibilityObject
        # .Bounds.Empty... (WinForms Rectangle.Empty returns a Rectangle),
        # hitting "maximum recursion depth exceeded" during page load.
        self._window = None
        self._settings_window = None  # set once by main() after creating it
        self._settings_open = False
        self.pet_id = pet_id
        self.dragging = False
        self.screen_w = screen_w
        self.screen_h = screen_h

    def open_claude(self):
        """
        Runs on its own thread and returns immediately. launch_claude_desktop()
        can shell out to PowerShell (Get-StartApps) the first time it's
        called, which can take a few seconds and occasionally even hits its
        own internal timeout - if that ran directly on this method (which
        pywebview calls synchronously from JS), it would block the whole
        JS<->Python bridge for that long, which is exactly what was
        producing the "Python is not responding" freezes on click.
        """
        threading.Thread(target=launch_claude_desktop, daemon=True).start()

    # ---- drag & drop ----------------------------------------------------
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
        """
        Released after a drag. A taskbar-mode pet falls back down to its
        calibrated resting line; a desktop-mode pet simply stays wherever
        it was let go, and that spot is remembered.
        """
        self.dragging = False
        pet_cfg = get_pet_config(self.pet_id)
        if pet_cfg["mode"] == "desktop":
            threading.Thread(target=self._save_desktop_position, daemon=True).start()
        else:
            threading.Thread(target=self._fall, daemon=True).start()

    def _save_desktop_position(self):
        if not self._window:
            return
        try:
            update_pet_config(
                self.pet_id,
                desktop_x=int(self._window.x),
                desktop_y=int(self._window.y),
            )
        except Exception as exc:
            log("_save_desktop_position failed:", exc)

    def _glide(self, target_x, target_y, duration=0.35, steps=18):
        """Smoothly animate the window from its current spot to (target_x,
        target_y) with a gentle ease-out, instead of teleporting there."""
        if not self._window:
            return
        try:
            start_x, start_y = self._window.x, self._window.y
            for i in range(1, steps + 1):
                t = i / steps
                t = 1 - (1 - t) ** 2  # ease-out
                x = int(start_x + (target_x - start_x) * t)
                y = int(start_y + (target_y - start_y) * t)
                self._window.move(x, y)
                time.sleep(duration / steps)
        except Exception as exc:
            log("_glide failed:", exc)

    def _fall(self):
        if not self._window:
            return
        try:
            screen_w, screen_h = get_screen_size()
            margin = get_margin(self.pet_id)
            target_y = screen_h - WIN_H - margin
            x = min(max(0, self._window.x), max(0, screen_w - WIN_W))
            self._window.move(int(x), int(self._window.y))
            self._glide(x, target_y)
        except Exception as exc:
            log("_fall failed:", exc)

    # ---- taskbar calibration ---------------------------------------------
    def set_calibration_margin(self, margin):
        """Live-preview while the calibration slider is being dragged. Only
        meaningful in taskbar mode - it's only called from the taskbar
        section of the settings panel, so that's always the case here."""
        if not self._window:
            return
        try:
            margin = int(margin)
            y = self.screen_h - WIN_H - margin
            self._window.move(int(self._window.x), int(y))
        except Exception as exc:
            log("set_calibration_margin failed:", exc)

    def finish_calibration(self, margin):
        """Persist the user's chosen margin so setup never has to run again."""
        try:
            update_pet_config(self.pet_id, taskbar_margin=int(margin))
            log(f"[{self.pet_id}] Taskbar margin calibrated and saved: {margin}px")
        except Exception as exc:
            log("finish_calibration failed:", exc)

    # ---- mode switching (desktop vs. taskbar) ----------------------------
    def set_mode(self, mode):
        """Switch this pet between 'taskbar' (snaps to the bottom) and
        'desktop' (stays wherever it's placed), applying the change live."""
        if mode not in ("taskbar", "desktop"):
            return
        pet_cfg = update_pet_config(self.pet_id, mode=mode)
        if not self._window:
            return
        try:
            if mode == "desktop" and pet_cfg.get("desktop_x") is None:
                # First time going to desktop mode: anchor it right where it
                # currently sits instead of jumping somewhere unexpected.
                update_pet_config(
                    self.pet_id,
                    desktop_x=int(self._window.x),
                    desktop_y=int(self._window.y),
                )
        except Exception as exc:
            log("set_mode failed:", exc)

    def set_scale(self, pct):
        try:
            update_pet_config(self.pet_id, scale_pct=int(pct))
        except Exception as exc:
            log("set_scale failed:", exc)

    def get_settings(self):
        pet_cfg = get_pet_config(self.pet_id)
        pet_cfg["screen_w"], pet_cfg["screen_h"] = get_screen_size()
        return pet_cfg

    # Kept for use at startup (see main()) to decide whether to auto-open
    # the settings window on first run.
    def get_calibration_state(self):
        cfg = load_config()
        pet_cfg = cfg.get("pets", {}).get(self.pet_id)
        if pet_cfg and pet_cfg.get("taskbar_margin") is not None:
            return {"calibrate": False, "margin": pet_cfg["taskbar_margin"]}
        return {"calibrate": True, "margin": get_taskbar_height()}

    # ---- the settings window -----------------------------------------------
    def open_settings_window(self):
        """
        Shows the settings window (a genuine, separate pywebview window:
        normal frame, shows in the taskbar, closable on its own). The pet
        sprite's window is left completely alone; it just keeps sitting
        wherever it was.

        The settings window itself is created once, up front, in main() -
        hidden until needed - and simply shown/hidden from here on out.
        Earlier versions of this tried creating it fresh on demand (via
        webview.create_window() after the GUI loop was already running),
        and separately tried a second GUI toolkit (tkinter) in its own
        thread; both caused "Python is not responding" freezes. show()/
        hide() on a window that already exists is a much more basic,
        reliable operation, so that's all this does now.
        """
        if self._settings_open or not self._settings_window:
            return
        self._settings_open = True
        try:
            pet_cfg = get_pet_config(self.pet_id)
            self._settings_window.evaluate_js(
                "window.__applySettings && window.__applySettings("
                + json.dumps(pet_cfg) + ");"
            )
            self._settings_window.show()
        except Exception as exc:
            log("open_settings_window failed:", exc)
            self._settings_open = False

    def preview_scale(self, pct):
        """Live-updates the pet sprite's on-screen size while the settings
        window's size slider is being dragged, without persisting anything
        (persisting happens separately via set_scale)."""
        if not self._window:
            return
        try:
            pct = int(pct)
            self._window.evaluate_js(
                "document.getElementById('petWrap').style.setProperty("
                f"'--pet-scale', '{pct / 100}');"
            )
        except Exception as exc:
            log("preview_scale failed:", exc)


def main():
    screen_w, screen_h = get_screen_size()
    pet_id = DEFAULT_PET_ID
    pet_cfg = get_pet_config(pet_id)

    if pet_cfg["mode"] == "desktop" and pet_cfg["desktop_x"] is not None:
        start_x = pet_cfg["desktop_x"]
        start_y = pet_cfg["desktop_y"]
    else:
        start_x = screen_w - WIN_W - 80
        start_y = screen_h - WIN_H - pet_cfg["taskbar_margin"]

    api = Api(pet_id, screen_w, screen_h)

    window = webview.create_window(
        title="Claude Pet",
        url=HTML_PATH,
        width=WIN_W,
        height=WIN_H,
        x=start_x,
        y=start_y,
        frameless=True,
        easy_drag=False,
        on_top=True,
        transparent=True,
        resizable=False,
        js_api=api,
    )
    api._window = window

    # The settings window is created here too - hidden - instead of on
    # demand later. Creating it now, before webview.start() runs the GUI
    # loop, is the well-supported way to have multiple pywebview windows;
    # creating one later at an arbitrary moment (e.g. from a button click)
    # turned out to be what caused the earlier "Python is not responding"
    # freezes. From here on, open_settings_window() just calls show()/
    # hide() on this same window instead of creating/destroying it.
    #
    # Loaded via html=<content> rather than url=<path>: pywebview runs a
    # small local HTTP server to serve url= file paths, and that server
    # failed to serve this window's file correctly when combined with
    # hidden=True (404 Not Found), even though the file exists right next
    # to claude_pet.html and pet_app.py. Passing the HTML content directly
    # sidesteps that local-file-serving path entirely - the settings page
    # is fully self-contained (inline CSS/JS, no external assets) so this
    # loses nothing.
    with open(SETTINGS_HTML_PATH, "r", encoding="utf-8") as f:
        settings_html = f.read()

    settings_api = SettingsApi(api)
    settings_window = webview.create_window(
        title="הגדרות חיה",
        html=settings_html,
        width=SETTINGS_WIN_W,
        height=SETTINGS_WIN_H,
        resizable=False,
        js_api=settings_api,
        hidden=True,
    )
    settings_api._window = settings_window
    api._settings_window = settings_window

    def _on_settings_closing():
        # Clicking the settings window's own [X] hides it instead of
        # destroying it, so it's always there ready for next time and we
        # never need to create a window while the app is already running.
        settings_window.hide()
        api._settings_open = False
        return False  # cancel the default close/destroy

    settings_window.events.closing += _on_settings_closing

    def _on_loaded():
        # IMPORTANT: this callback fires on pywebview's own GUI/message-loop
        # thread. Calling things like evaluate_js() directly from here can
        # deadlock that loop with itself (the call needs the loop free to
        # dispatch it, but the loop is busy running this very callback
        # while it waits) - the exact "Python is not responding" freeze.
        # So everything below runs on its own thread, the same way
        # hide_from_taskbar already does internally.
        def _post_load():
            if sys.platform == "win32":
                hide_from_taskbar("Claude Pet")
            api.preview_scale(pet_cfg["scale_pct"])
            if api.get_calibration_state()["calibrate"]:
                api.open_settings_window()

        threading.Thread(target=_post_load, daemon=True).start()

    window.events.loaded += _on_loaded

    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        log("FATAL uncaught exception in main():\n" + traceback.format_exc())