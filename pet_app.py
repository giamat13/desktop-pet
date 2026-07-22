"""
Claude Pet desktop overlay.
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
HTML_PATH = os.path.join(APP_DIR, "claude_pet.html")

WIN_W, WIN_H = 220, 220
DEFAULT_TASKBAR_MARGIN = 46  # used only as an initial guess for calibration

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


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as exc:
        log("save_config failed:", exc)


def get_margin():
    """
    The vertical distance (px) the pet should rest above the bottom of the
    screen. Prefers the value the user calibrated by hand (most reliable,
    since taskbar auto-location can be off with some themes/scaling), and
    only falls back to an automatic OS guess if nothing was calibrated yet.
    """
    cfg = load_config()
    if "taskbar_margin" in cfg:
        return cfg["taskbar_margin"]
    return get_taskbar_height()


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


class Api:
    def __init__(self, screen_h):
        # NOTE: must stay underscore-prefixed. pywebview introspects every
        # public attribute of the js_api object to expose it to JS, and would
        # otherwise recurse forever into window.native.AccessibilityObject
        # .Bounds.Empty... (WinForms Rectangle.Empty returns a Rectangle),
        # hitting "maximum recursion depth exceeded" during page load.
        self._window = None
        self.dragging = False
        self.screen_h = screen_h

    def open_claude(self):
        launch_claude_desktop()

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
        threading.Thread(target=self._fall, daemon=True).start()

    def _fall(self):
        if not self._window:
            return
        try:
            screen_w, screen_h = get_screen_size()
            target_y = screen_h - WIN_H - get_margin()
            x = min(max(0, self._window.x), max(0, screen_w - WIN_W))
            y = self._window.y
            self._window.move(int(x), int(y))
            while y < target_y - 1:
                step = max(4, int((target_y - y) * 0.25))
                y = min(target_y, y + step)
                self._window.move(int(x), int(y))
                time.sleep(0.016)
        except Exception as exc:
            log("_fall failed:", exc)

    def set_calibration_margin(self, margin):
        """Live-preview: move the window as the user drags the calibration slider."""
        if not self._window:
            return
        try:
            margin = int(margin)
            y = self.screen_h - WIN_H - margin
            self._window.move(int(self._window.x), int(y))
        except Exception as exc:
            log("set_calibration_margin failed:", exc)

    def get_calibration_state(self):
        """
        Called by the page itself once window.pywebview.api is ready (JS ->
        Python is the safe/proven direction here; the reverse - Python
        pushing into JS via evaluate_js from the "loaded" event - can
        deadlock the GUI thread, which is what caused Python to hang).
        """
        cfg = load_config()
        if "taskbar_margin" in cfg:
            return {"calibrate": False, "margin": cfg["taskbar_margin"]}
        return {"calibrate": True, "margin": get_taskbar_height()}

    def finish_calibration(self, margin):
        """Persist the user's chosen margin so setup never has to run again."""
        try:
            margin = int(margin)
            save_config({"taskbar_margin": margin})
            log(f"Taskbar margin calibrated and saved: {margin}px")
        except Exception as exc:
            log("finish_calibration failed:", exc)


def main():
    screen_w, screen_h = get_screen_size()
    cfg = load_config()
    margin = cfg.get("taskbar_margin", get_taskbar_height())

    start_x = screen_w - WIN_W - 80
    start_y = screen_h - WIN_H - margin

    api = Api(screen_h)

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

    def _on_loaded():
        if sys.platform == "win32":
            hide_from_taskbar("Claude Pet")

    window.events.loaded += _on_loaded

    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        log("FATAL uncaught exception in main():\n" + traceback.format_exc())
