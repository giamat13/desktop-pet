"""
Refresh ClaudePet's usage.json with REAL rate-limit numbers.

Claude Code only renders its statusLine (and therefore only runs
claude-usage-statusline.ps1) inside an interactive terminal session - it never
fires in the VS Code extension. This drives one short interactive session
inside a ConPTY the child process owns: no synthetic keyboard/mouse input, and
nothing appears on the user's screen.

`rate_limits` is only populated after the session's first API response, so this
sends one minimal prompt and waits for usage.json to come back with a non-null
five_hour percentage.

Exit codes: 0 real numbers written, 1 timed out / still null.
"""
import json
import os
import re
import shutil
import sys
import threading
import time

from winpty import PtyProcess

USAGE = os.path.expandvars(r"%LOCALAPPDATA%\ClaudePet\usage.json")
WORKDIR = os.path.expanduser("~")


def find_claude():
    """
    Locate the claude CLI. PATH first, then the usual per-user install spots,
    so this keeps working on a machine that installed it a different way.
    """
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\claude.exe"),
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expanduser(r"~\.local\bin\claude.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


CLAUDE = find_claude()
TIMEOUT = float(os.environ.get("REFRESH_TIMEOUT", "180"))
VERBOSE = os.environ.get("REFRESH_VERBOSE") == "1"

lock = threading.Lock()
buf = [""]


def note(msg):
    if VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]")


def flat(s):
    """
    Screen text with ANSI stripped and ALL whitespace removed.

    The TUI positions words with cursor-forward sequences instead of spaces,
    so on screen "Yes, I trust this folder" arrives as
    "Yes,\x1b[12GI\x1b[14Gtrust\x1b[20Gthis\x1b[25Gfolder" - the literal
    phrase never appears and a plain substring test silently never matches.
    """
    return re.sub(r"\s+", "", _ANSI.sub("", s)).lower()


def has_real_numbers():
    try:
        with open(USAGE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d.get("five_hour", {}).get("used_percentage") is not None
    except Exception:
        return False


# A session spawned from inside another Claude Code session inherits markers
# that put it in child-session mode; drop them so this behaves like a normal
# top-level session.
env = {k: v for k, v in os.environ.items()
       if not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}

if not CLAUDE:
    note("claude CLI not found on PATH or in the usual install locations")
    sys.exit(2)

p = PtyProcess.spawn([CLAUDE], dimensions=(45, 130), cwd=WORKDIR, env=env)
note("spawned")


def reader():
    while True:
        try:
            d = p.read(8192)
            if not d:
                time.sleep(0.05)
                continue
            with lock:
                buf[0] += d
        except Exception:
            return


threading.Thread(target=reader, daemon=True).start()

start = time.time()
trust_seen = False
trust_cleared_at = None
asked = False
ok = False

while time.time() - start < TIMEOUT:
    el = time.time() - start
    with lock:
        s = buf[0]

    # 1) The folder-trust gate swallows keystrokes, so it must be cleared
    #    BEFORE the prompt is typed - otherwise "hi" lands in the dialog and
    #    no turn ever runs (which is exactly what happened first time round).
    if not trust_seen and "trustthisfolder" in flat(s):
        trust_seen = True
        p.write("\r")
        trust_cleared_at = time.time()
        note("cleared trust prompt")
        time.sleep(4)
        continue

    # 2) Send one tiny prompt once the REPL is actually accepting input.
    if not asked:
        ready = (trust_cleared_at and time.time() - trust_cleared_at > 5) or \
                (not trust_seen and el > 20)
        if ready:
            p.write("hi")
            time.sleep(1.0)
            p.write("\r")
            asked = True
            note("sent prompt")

    if has_real_numbers():
        note("real numbers present")
        ok = True
        break
    time.sleep(0.6)

try:
    p.write("\x03")
    time.sleep(0.3)
    p.terminate(force=True)
except Exception:
    pass

if ok:
    with open(USAGE, encoding="utf-8-sig") as f:
        d = json.load(f)
    print("5h=%s%%  7d=%s%%" % (
        d["five_hour"]["used_percentage"], d["seven_day"]["used_percentage"]))
    sys.exit(0)

note("no real numbers")
if VERBOSE:
    with lock:
        print("--- flattened screen tail ---", flush=True)
        print(flat(buf[0])[-1200:], flush=True)
sys.exit(1)
