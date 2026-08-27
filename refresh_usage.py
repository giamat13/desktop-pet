"""
Refresh DesktopPet's usage.json with REAL rate-limit numbers.

Claude Code only renders its statusLine (and therefore only runs
claude-usage-statusline.ps1) inside an interactive terminal session - it never
fires in the VS Code extension. This drives one short interactive session
inside a ConPTY the child process owns: no synthetic keyboard/mouse input, and
nothing appears on the user's screen.

`rate_limits` is only populated after the session's first API response, so this
sends one minimal prompt and waits for usage.json to come back with a non-null
five_hour percentage.

COST - the whole point of the flags below
-----------------------------------------
The first version of this script paid full price for that turn: a default
session carries every plugin, skill, MCP definition and tool schema into the
request. Measured on this machine: ~12k tokens of Opus per run, ~$0.18, every
5 minutes - about 57% of a Pro WEEKLY allowance spent on gauges.

Everything cheaper was tried and measured, not guessed:

  no local source exists      ~/.claude transcripts, sessions/, usage.db and
                              every hook payload were checked - none of them
                              carry the live percentages. Only a live API
                              turn does, so a turn has to happen.
  --print is 50x cheaper      and useless: a statusLine never runs under -p,
                              and print mode's rate_limit_event has resetsAt
                              but no percentage.
  --safe-mode strips the      it also strips the statusLine, and no --settings
  user's whole config         override brings it back. Verified: the session
                              ran, the footer stayed default, nothing wrote.
  --setting-sources project   works - an empty scratch workspace whose only
  + a one-key settings file   project setting is the statusLine.
  --system-prompt /           silently ignored by an interactive session; it
  --disallowed-tools          shipped 23k tokens anyway.
  --agent with tools: []      the one that mattered: 23k -> 1,206 tokens.

Measured end state: 1,206 in / ~240 out of Haiku per run, ~$0.0024. At the
10-minute interval install.ps1 registers, that is ~$2.43 a week against a
weekly limit calibrated at ~$6.40 per 1% (three gauge points were watched
against every token this machine spent in the same window) - i.e. ~0.4% of the
weekly allowance, down from ~57%. That margin is why the scheduled task now
ships ENABLED; the pet's settings window can still turn it off.

Exit codes: 0 real numbers written, 1 timed out / still null.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid

from winpty import PtyProcess

USAGE = os.path.expandvars(r"%LOCALAPPDATA%\DesktopPet\usage.json")


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


APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATUSLINE = os.path.join(APP_DIR, "claude-usage-statusline.ps1")

# One fixed id per run, so the transcript this session leaves behind can be
# deleted by name afterwards. (--no-session-persistence would be the obvious
# answer, but it only works with --print, and --print never runs a statusLine.)
SESSION_ID = str(uuid.uuid4())


def cheap_workspace():
    """
    A scratch directory whose project settings hold nothing but the statusLine.

    The statusLine is the only reason this session exists - it is what writes
    usage.json - but it normally lives in the user's own settings, and loading
    those drags in every plugin, hook and MCP server with it (that is the ~12k
    tokens). --safe-mode strips all of it, the statusLine included, and no
    --settings override brings the statusLine back: tested, and the footer
    stayed the default one.

    So instead of subtracting from the user's config, run somewhere that has
    none: an empty folder, `--setting-sources project`, and a .claude/
    settings.json containing exactly one key.
    """
    ws = os.path.join(os.path.dirname(USAGE), "refresh-workspace")
    dot = os.path.join(ws, ".claude")
    os.makedirs(dot, exist_ok=True)
    with open(os.path.join(dot, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"statusLine": {
            "type": "command",
            "command": ('powershell -NoProfile -ExecutionPolicy Bypass -File '
                        f'"{STATUSLINE}"'),
        }}, f)
    return ws


def cheap_args():
    """One refresh, ~$0.0024 instead of ~$0.18. See the module docstring."""
    return [
        "--setting-sources", "project",
        # MCP servers are configured in ~/.claude.json, which --setting-sources
        # does not cover - without these two the session still loaded every
        # server's tool schemas (103k tokens of cache read, measured).
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--model", "haiku",
        "--effort", "low",
        "--session-id", SESSION_ID,
        # The one flag that matters. An interactive session ignores
        # --system-prompt and --disallowed-tools and ships Claude Code's whole
        # prompt and tool set anyway (23k tokens, measured); starting it as an
        # agent with no tools cuts the request to 1,200.
        "--agents", json.dumps({"gauge": {
            "description": "usage probe",
            "prompt": "Answer with the single word: ok. Never explain.",
            "tools": []}}),
        "--agent", "gauge",
    ]


def drop_transcript():
    """
    Delete the session this run created.

    Left alone, a refresh every 10 minutes buries ~/.claude/projects under a
    thousand dead transcripts a week - the 5-minute version had already left
    5,172 of them. Deleting by our own --session-id touches nothing the user
    started themselves.
    """
    proj = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        for d in os.listdir(proj):
            f = os.path.join(proj, d, SESSION_ID + ".jsonl")
            if os.path.isfile(f):
                os.remove(f)
    except Exception:
        pass


CLAUDE = find_claude()
TIMEOUT = float(os.environ.get("REFRESH_TIMEOUT", "180"))
VERBOSE = os.environ.get("REFRESH_VERBOSE") == "1"

lock = threading.Lock()
buf = [""]


def safe_print(s):
    """
    print() that can't itself crash the script.

    Under the scheduled task this runs headless via pythonw.exe (no console -
    stdout is None, and print() would raise AttributeError, same trap as
    pet_app.py's log()). Run manually from a real console instead, Windows'
    legacy console codepage (cp1252) can't encode characters the TUI emits,
    such as the warning glyph U+26A0 - which is exactly what silently
    swallowed the real diagnostic output the first time this was debugged.
    """
    try:
        print(s, flush=True)
    except Exception:
        pass


def note(msg):
    if VERBOSE:
        safe_print(f"[{time.strftime('%H:%M:%S')}] {msg}")


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


def read_updated_at():
    try:
        with open(USAGE, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d.get("updated_at")
    except Exception:
        return None


def has_real_numbers(baseline_updated_at):
    """
    True only once usage.json reflects THIS session's write, not a stale one.

    A percentage-only check passes forever once any past run ever succeeded -
    the file already has a non-null used_percentage from yesterday, so the
    loop would declare victory on the first poll, before the prompt is even
    sent, and never actually wait for a fresh write. Requiring updated_at to
    advance past the pre-spawn baseline is what makes this an actual refresh.
    """
    try:
        with open(USAGE, encoding="utf-8-sig") as f:
            d = json.load(f)
        ua = d.get("updated_at")
        return (d.get("five_hour", {}).get("used_percentage") is not None
                and ua is not None
                and (baseline_updated_at is None or ua != baseline_updated_at))
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

baseline_updated_at = read_updated_at()
p = PtyProcess.spawn([CLAUDE] + cheap_args(), dimensions=(45, 130),
                     cwd=cheap_workspace(), env=env)
note("spawned")


def reader():
    while True:
        try:
            d = p.read(8192)
            if not d:
                time.sleep(0.05)
                continue
            with lock:
                # Only the tail is ever inspected, and an idle TUI redraws its
                # spinner forever - keeping the whole thing makes flat() scan
                # a growing buffer every 0.6s and the script crawls.
                buf[0] = (buf[0] + d)[-40000:]
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

    if has_real_numbers(baseline_updated_at):
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

# REFRESH_KEEP=1 leaves the transcript behind, which is the only way to read
# back what a real run actually cost (the print-mode equivalent is a proxy).
if os.environ.get("REFRESH_KEEP") != "1":
    drop_transcript()

if ok:
    with open(USAGE, encoding="utf-8-sig") as f:
        d = json.load(f)
    safe_print("5h=%s%%  7d=%s%%" % (
        d["five_hour"]["used_percentage"], d["seven_day"]["used_percentage"]))
    sys.exit(0)

note("no real numbers")
if VERBOSE:
    # A file, not stdout: under pythonw.exe there is no console at all, and
    # even in a real console the TUI's raw bytes can defeat print() in ways
    # that are themselves hard to see - which is exactly what happened
    # debugging this: a UnicodeEncodeError on a warning glyph, then safe_print
    # silently swallowing whatever came after it.
    dump_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refresh_debug.txt")
    with lock, open(dump_path, "w", encoding="utf-8") as f:
        f.write(f"buf length: {len(buf[0])}\n")
        f.write("--- flattened screen tail ---\n")
        f.write(flat(buf[0])[-1200:] + "\n")
        f.write("--- raw tail (escaped) ---\n")
        f.write(repr(buf[0][-800:]) + "\n")
    note(f"debug dump written to {dump_path}")
sys.exit(1)
