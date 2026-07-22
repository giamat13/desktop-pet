"""
Claude Pet desktop app.
Shows the animated pet in a small always-on-top transparent window
and opens Claude Desktop when the pet is clicked.
"""
import os
import sys
import glob
import webview

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(APP_DIR, "claude_pet.html")


def find_claude_shortcut():
    """Look for the Claude Desktop Start Menu shortcut (works for both
    the traditional installer and the MSIX/Store install)."""
    search_dirs = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    ]
    candidates = []
    for d in search_dirs:
        if os.path.isdir(d):
            candidates += glob.glob(os.path.join(d, "**", "*laude*.lnk"), recursive=True)
    return candidates[0] if candidates else None


class Api:
    def open_claude(self):
        shortcut = find_claude_shortcut()
        try:
            if shortcut:
                os.startfile(shortcut)
                return
            # fallback: the installer also registers a Claude.exe alias
            os.startfile("Claude.exe")
        except Exception as exc:
            print("Could not open Claude Desktop:", exc)


def main():
    if sys.platform != "win32":
        print("This app is built for Windows (taskbar + Claude Desktop launch).")

    api = Api()
    webview.create_window(
        title="Claude Pet",
        url=HTML_PATH,
        width=220,
        height=220,
        x=None,
        y=None,
        frameless=True,
        easy_drag=True,
        on_top=True,
        transparent=True,
        resizable=False,
        js_api=api,
    )
    webview.start()


if __name__ == "__main__":
    main()
