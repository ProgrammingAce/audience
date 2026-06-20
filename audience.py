#!/usr/bin/env python3
#
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""audience — a local-LLM shoulder-surfer.

A curses TUI that periodically screenshots your active window and asks a
local llama.cpp (gemma-4-E4B-it) vision model for brief, insightful
commentary on what you're doing. You can also type questions about what's
on screen.

Layout:
    +---------------------------------------+
    | commentary + answers (scrolls)        |
    |                                       |
    +---------------------------------------+
    | > your question here                  |
    +---------------------------------------+

- Takes a screenshot 5 seconds after starting.
- Then screenshots on a jittered, adaptive interval starting from --interval
  seconds (default 60). When the screen hasn't changed since the last shot it
  skips the commentary and backs off (gap grows up to --max-backoff x base);
  a real change snaps the cadence back to the base interval.
- Pauses periodic screenshots while the machine is idle (no keyboard/mouse
  activity for --idle-timeout seconds); resumes automatically when you return.
- Type a question + Enter to ask about the current screen.
- /screenshot [question]   take a screenshot in 5 seconds (optionally with a question to ask about it)
- /quit         exit (also Ctrl-C)

Usage:
    python3 audience.py
    python3 audience.py --interval 30
    python3 audience.py --url http://localhost:8080/v1/chat/completions

Start the llama.cpp server first, e.g.:
    llama-server -m gemma-4-E4B-it-Q4_K_M.gguf \
        --mmproj mmproj-gemma-4-E4B.gguf --port 8080

Requires: macOS (screencapture + Quartz/AppKit), llama.cpp serving an
OpenAI-compatible endpoint with the gemma vision projector loaded.
Grant Screen Recording permission to your terminal app.
"""

import argparse
import base64
import curses
import datetime as dt
import json
import os
import queue
import random
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request

import unicodedata

import Quartz


def _char_width(ch):
    """Display columns a character occupies: 0 (combining), 2 (wide), or 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip_to_width(text, cols):
    """Longest prefix of text whose display width fits in `cols` columns.

    Wrapping by character count overruns when the text contains double-width
    glyphs (CJK, many emoji), letting it spill past its column budget — e.g.
    underneath the dragon. Measuring real width keeps the cut honest.
    """
    if cols <= 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)

SYSTEM_PROMPT = (
    "You are a dragon perched in the corner of the operator's terminal, "
    "watching them work over their shoulder. Provide concise, witty commentary "
    "on what you actually see on screen. Comment on what is genuinely there — "
    "what they're doing, a notable change, a quirk worth a quip.\n"
    "\n"
    "Ground every remark in the screenshot. If you can't read it clearly or "
    "aren't sure, say nothing about it rather than guessing. Never invent "
    "errors, bugs, or details that are not visibly on screen.\n"
    "\n"
    "Keep in mind that not every screenshot is coding-related — the operator "
    "might be reading, browsing, writing, watching a video, or anything else. "
    "Meet them where they are and comment on whatever is actually on screen; "
    "don't force a programming or debugging lens onto a non-coding scene.\n"
    "\n"
    "Your personality (fixed traits — let them shape tone, not length):\n"
    "- SNARK is high: be sharp, dry, and quick with a barbed quip. Tease the "
    "operator's choices and savor their typos. Never cruel, always amused — "
    "the wit should land, not sting.\n"
    "- WISDOM is high: underneath the snark, your observations are genuinely "
    "useful. Point to the root cause, the better pattern, the thing they're "
    "about to regret. Earn the right to be smug by being right.\n"
    "- DEBUGGING is high but disciplined: you have a nose for bugs, yet you "
    "only call one out when an actual error, off-by-one, suspicious value, or "
    "code smell is plainly visible. Name the specific line or symbol. A clean "
    "screen earns no error talk — comment on what they're doing instead.\n"
    "- VOICE is the whole point: speak as the dragon, in first person — old, "
    "scaled, faintly amused at the small warm creature typing below you. Let a "
    "little theatre in: the occasional fire, hoard, claw, or smoke metaphor when "
    "it actually fits the scene, never forced. You are a dragon who happens to "
    "read code, not a linter wearing a dragon costume. Don't narrate stage "
    "directions or describe your own wings; let the words carry the scales.\n"
    "Never start a remark with hissing or breathing sounds like 'Hssss', 'Hss',\n"
    "'Hiss', or 'Pshh' — it's overdone and gets old fast.\n"
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like\n"
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy\n"
    "labels for the operator within a short stretch. Vary your references or skip\n"
    "the nickname entirely.\n"
    "\n"
    "Think of yourself as a knowledgeable colleague who notices everything. "
    "Keep it brief and entertaining — two or three sentences, the most "
    "important observation first, with room for a quip or a bit of useful "
    "elaboration.\n"
    "\n"
    "You have tools for facts the screenshot hides: active_window_info (app + "
    "window title, when a title bar is too small to read), now (date/time), "
    "system_stats (battery, CPU load, memory, disk, uptime), and now_playing (current "
    "track). Call one only when it would sharpen the remark; don't narrate that "
    "\n"
    "Think of yourself as a knowledgeable colleague who notices everything. "
    "Keep it brief and entertaining — two or three sentences, the most "
    "important observation first, with room for a quip or a bit of useful "
    "elaboration.\n"
)

QA_SYSTEM_PROMPT = (
    "You are a dragon perched in the corner of the operator's terminal — old, "
    "clever, and faintly amused that they're asking you. They've typed you a "
    "question. Answer it, in full dragon voice. No preamble, no throat-clearing.\n"
    "\n"
    "Stay in character. The personality is the whole point of asking a dragon "
    "instead of a search box — let it land hard, but never at the cost of being "
    "right:\n"
    "- SNARK is high: open with a dry barb or a knowing aside, tease the "
    "question or the choices behind it, savor the absurd. Amused, never cruel — "
    "the wit should sting like a friend, not a troll.\n"
    "- WISDOM is high: under the smirk, be genuinely, specifically useful. Give "
    "the real answer, plus the better approach or the gotcha they didn't think "
    "to ask about. Earn the smugness by being right.\n"
    "- DEBUGGING is high: if the question involves an error, bug, or suspicious "
    "value, sniff it out and name the specific line, symbol, or root cause.\n"
    "- VOICE: speak as the dragon — first person, a little theatrical, the "
    "occasional fire/hoard/scale metaphor when it actually fits. Don't narrate "
    "stage directions or describe your own wings; let the words carry it.\n"
    "Never start a remark with hissing or breathing sounds like 'Hssss', 'Hss',\n"
    "'Hiss', or 'Pshh' — it's overdone and gets old fast.\n"
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like\n"
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy\n"
    "labels for the operator within a short stretch. Vary your references or skip\n"
    "the nickname entirely.\n"
    "\n"
    "Keep in mind that not every screenshot is coding-related — the operator "
    "might be reading, browsing, writing, or anything else, and their question "
    "may have nothing to do with programming. Answer what they actually asked; "
    "don't force a coding or debugging lens onto a non-coding question.\n"
    "\n"
    "You have tools for facts you'd otherwise guess at: active_window_info (app "
    "+ window title), now (date/time), system_stats (battery, CPU load, memory, disk, "
    "uptime), and now_playing (current track). Call one when the question turns "
    "on such a fact rather than bluffing; don't narrate that you used it.\n"
    "You also have a special syntax: prefix any filename with @ "
    "to have its contents injected into your response (e.g., @README.md). Only "
    "files in the working directory can be read this way.\n"
    "\n"
    "The answer must survive having the jokes stripped out — correctness first, "
    "personality wrapped around it, not instead of it. Keep it tight: a few "
    "sentences, more only when the question truly earns it."
)

HEALTH_SYSTEM_PROMPT = (
    "You are a dragon perched in the corner of the operator's terminal, and you "
    "keep half an eye on the health of their machine — its battery, its heat, its "
    "labored breathing under load. The user message you're given is a real, "
    "just-measured system condition worth flagging (e.g. a draining battery or a "
    "pegged CPU).\n"
    "\n"
    "Deliver ONE short, in-character warning about exactly that condition — sharp, "
    "dry, a little theatrical, but genuinely useful. Treat the stated numbers as "
    "fact; don't invent other problems or numbers that weren't given. If it's "
    "worth a concrete nudge (plug in, kill the runaway process, close some tabs), "
    "give it. One or two sentences, no preamble. Never start with hissing sounds\n"
    "like 'Hssss', 'Hss', 'Hiss', or 'Pshh' — it's overdone and gets old fast.\n"
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like\n"
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy\n"
    "labels for the operator within a short stretch. Vary your references or skip\n"
    "the nickname entirely.\n"
)

# Animated mascot pinned to the top-right corner. Three 12x5 frames, cycled
# on a timer. Sprite from https://gist.github.com/zmxv/7f83671f860c15be02f45b07fee207fc
DRAGON_FRAMES = [
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (        ) ', '  `-vvvv-´  '],
    ['   ~    ~   ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
]
DRAGON_W = 12
DRAGON_FRAME_MS = 500  # matches the leaked buddy's 500ms tick
# The leaked buddy treats the eye as a fixed identity trait, not an animation:
# renderSprite(species, eye, hat, frameIdx) takes eye separately from frameIdx.
# EYES is the set of variants a buddy *can* have (rarity/shiny), default '·' —
# only the body frames cycle.
DRAGON_EYE = '·'
DRAGON_EYES = ['·', '✦', '×', '◉', '@', '°']
DRAGON_BLINK = '-'        # closed-eye glyph
DRAGON_REST_FRAME = 0     # frame shown while idle (no movement)
# The dragon rests most of the time and only comes alive in a brief flourish:
# every ANIM_PERIOD ticks it animates for ANIM_TICKS ticks, then settles.
DRAGON_ANIM_PERIOD = 24   # ticks between flourishes (~12s at 500ms)
DRAGON_ANIM_TICKS = 4     # length of each flourish (~2s)
DRAGON_SPARKLE_MS = 1500  # how long the dragon sparkles after a screenshot
# Sparkle glyphs and the cells (row, col) around the 12x5 dragon box where a
# shiny dragon twinkles. A rotating subset lights up each tick.
DRAGON_SPARKLES = ['✦', '✧', '·', '*']
DRAGON_SPARKLE_CELLS = [(0, 1), (0, 10), (1, 0), (2, 11), (4, 0), (4, 11), (1, 6)]

# xterm focus-reporting (DECSET 1004): when enabled the terminal emits ESC[I
# when audience's tab/window gains keyboard focus and ESC[O when it loses it.
# That per-tab focus state is what tells us whether the dragon would be looking
# at itself. Supported by iTerm2, Terminal.app, kitty, wezterm, alacritty.
FOCUS_REPORTING_ON = "\033[?1004h"
FOCUS_REPORTING_OFF = "\033[?1004l"


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------
def _onscreen_windows():
    """On-screen normal windows, front-to-back (Quartz's native order)."""
    options = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
    return Quartz.CGWindowListCopyWindowInfo(
        options, Quartz.kCGNullWindowID) or []


def frontmost_pid():
    """PID owning the frontmost on-screen window, or None.

    Derived from Quartz's window ordering rather than
    NSWorkspace.frontmostApplication(): screenshots run on a worker thread, and
    AppKit window/app APIs only update reliably on the main thread — off-thread
    they return a stale cached value, pinning every capture to whatever was
    frontmost long ago. CGWindowListCopyWindowInfo is thread-safe and returns
    windows front-to-back, so the first layer-0 window is the live active one.
    """
    for w in _onscreen_windows():
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        return w.get("kCGWindowOwnerPID")
    return None


def frontmost_window_id():
    """CGWindowID of the frontmost app's largest normal window, or None."""
    pid = frontmost_pid()

    best = None
    for w in _onscreen_windows():
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        if pid is not None and w.get("kCGWindowOwnerPID") != pid:
            continue
        b = w.get("kCGWindowBounds", {})
        area = b.get("Width", 0) * b.get("Height", 0)
        if best is None or area > best[1]:
            best = (w.get("kCGWindowNumber"), area)
    return best[0] if best else None


def idle_seconds():
    """Seconds since the last user input event (keyboard/mouse), or 0.0.

    Any failure returns 0.0 (treated as "active") so a flaky idle probe can
    never wedge commentary off.
    """
    try:
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState,
            Quartz.kCGAnyInputEventType)
    except Exception:
        return 0.0


def capture():
    """Take a fresh screenshot of the active window and return PNG bytes.

    The file is written to a unique path, read, and deleted immediately so
    nothing is persisted and no stale image can ever be reused. Returns the
    PNG bytes, or None on failure.
    """
    import os
    fd, path = tempfile.mkstemp(prefix="audience-", suffix=".png")
    os.close(fd)
    os.unlink(path)  # screencapture needs a non-existent target to write cleanly
    try:
        try:
            wid = frontmost_window_id()
        except Exception:
            wid = None

        ok = False
        if wid is not None:
            ok = subprocess.run(
                ["screencapture", "-x", "-o", "-l", str(wid), path]
            ).returncode == 0 and os.path.exists(path)
        if not ok:
            ok = subprocess.run(
                ["screencapture", "-x", path]
            ).returncode == 0 and os.path.exists(path)
        if not ok:
            return None
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)


# --------------------------------------------------------------------------
# Change detection
#
# A cheap average-hash (aHash) of each screenshot lets us tell whether the
# screen actually changed since the last shot. When it hasn't, there's nothing
# new to comment on, so we skip the model call and lengthen the interval.
# --------------------------------------------------------------------------
def image_ahash(png_bytes):
    """64-bit average hash of a PNG, or None on failure.

    Decodes the PNG with CoreGraphics (already imported as Quartz), draws it
    into an 8x8 grayscale buffer we own, and sets each of the 64 bits to
    (pixel > mean). Dependency-free; returns None if anything goes wrong so the
    caller can treat an un-hashable frame as "changed" rather than going silent.
    """
    try:
        data = Quartz.CFDataCreate(None, png_bytes, len(png_bytes))
        src = Quartz.CGImageSourceCreateWithData(data, None)
        if src is None:
            return None
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            return None
        w = h = 8
        buf = bytearray(w * h)
        cs = Quartz.CGColorSpaceCreateDeviceGray()
        ctx = Quartz.CGBitmapContextCreate(
            buf, w, h, 8, w, cs, Quartz.kCGImageAlphaNone)
        if ctx is None:
            return None
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
        mean = sum(buf) / len(buf)
        bits = 0
        for px in buf:
            bits = (bits << 1) | (1 if px > mean else 0)
        return bits
    except Exception:
        return None


def hamming(a, b):
    """Number of differing bits between two integer hashes."""
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------
# Agent tools
#
# Read-only, local, low-sensitivity facts the model can pull to ground its
# commentary instead of guessing from a fuzzy screenshot. Every tool here must
# be safe even if the model is fully prompt-injected by an adversarial screen:
# nothing reads secrets (clipboard, history, arbitrary files), writes, executes
# shell input, or sends data off the machine.
# --------------------------------------------------------------------------
def tool_now(**_):
    """Current local date and time."""
    now = dt.datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "human": now.strftime("%A %Y-%m-%d %H:%M:%S %Z"),
    }


def tool_active_window_info(**_):
    """App name and window title of the frontmost window.

    Fixes the model's biggest blind spot: tiny, unreadable title bars. Uses the
    same window metadata capture() already relies on — and the same thread-safe
    Quartz window ordering, so it never disagrees with the captured window.
    """
    app_name, title = None, None
    try:
        pid = frontmost_pid()
        best_area = -1
        for w in _onscreen_windows():
            if w.get("kCGWindowLayer", 1) != 0:
                continue
            if pid is not None and w.get("kCGWindowOwnerPID") != pid:
                continue
            b = w.get("kCGWindowBounds", {})
            area = b.get("Width", 0) * b.get("Height", 0)
            if area > best_area:
                best_area = area
                app_name = w.get("kCGWindowOwnerName") or app_name
                name = w.get("kCGWindowName")
                if name:
                    title = name
    except Exception:
        pass

    return {"app": app_name or "(unknown)", "title": title or "(no title)"}


def read_battery():
    """Battery percent and charge state via pmset, or None on failure.

    Shared by tool_system_stats and the health-watch loop so the two never
    disagree on what the battery is doing.
    """
    try:
        r = subprocess.run(["pmset", "-g", "batt"],
                           capture_output=True, text=True, timeout=5)
        pct, state = None, None
        for tok in r.stdout.replace(";", " ").split():
            if tok.endswith("%"):
                pct = tok.rstrip("%")
            if tok in ("charging", "discharging", "charged"):
                state = tok
        if pct is not None:
            return {"percent": int(pct), "state": state or "unknown"}
    except Exception:
        pass
    return None


def read_free_mem_mb():
    """Free + speculative memory in MB via vm_stat, or None on failure."""
    try:
        page = int(subprocess.run(["sysctl", "-n", "hw.pagesize"],
                   capture_output=True, text=True, timeout=5).stdout.strip())
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        free_pages = 0
        for line in r.stdout.splitlines():
            if line.startswith("Pages free:") or \
                    line.startswith("Pages speculative:"):
                free_pages += int(line.rsplit(":", 1)[1].strip().rstrip("."))
        if free_pages:
            return round(free_pages * page / (1024 * 1024))
    except Exception:
        pass
    return None


def read_free_disk_gb():
    """Free space on the root volume in GB, or None on failure."""
    try:
        import shutil
        return round(shutil.disk_usage("/").free / (1024 ** 3), 1)
    except Exception:
        return None


def tool_system_stats(**_):
    """Battery, CPU load, free memory, free disk, and uptime — all read-only."""
    out = {}
    try:
        load1, load5, load15 = os.getloadavg()
        out["load_avg"] = {"1m": round(load1, 2), "5m": round(load5, 2),
                           "15m": round(load15, 2)}
    except Exception:
        pass
    batt = read_battery()
    if batt is not None:
        out["battery"] = batt
    mem = read_free_mem_mb()
    if mem is not None:
        out["memory_free_mb"] = mem
    disk = read_free_disk_gb()
    if disk is not None:
        out["disk_free_gb"] = disk
    try:
        r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        out["uptime"] = r.stdout.strip()
    except Exception:
        pass
    return out or {"error": "no stats available"}


def tool_now_playing(**_):
    """Currently playing track from Music or Spotify, if either is running."""
    script = '''
    on trackOf(appName)
      tell application "System Events"
        if not (exists process appName) then return ""
      end tell
      tell application appName
        if player state is playing then
          return (name of current track) & " — " & (artist of current track)
        end if
      end tell
      return ""
    end trackOf
    set s to ""
    try
      set s to trackOf("Spotify")
    end try
    if s is "" then
      try
        set s to trackOf("Music")
      end try
    end if
    return s
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=8)
        track = r.stdout.strip()
    except Exception:
        track = ""
    return {"now_playing": track or "(nothing playing)"}


# Directory this script was launched from — all write/read operations are confined here.
_WORKDIR = os.getcwd()


def _safe_path(rel_path):
    """Resolve a relative path against _WORKDIR, reject escapes."""
    real_workdir = os.path.realpath(_WORKDIR)
    full = os.path.realpath(os.path.join(real_workdir, os.path.normpath(rel_path)))
    if not full.startswith(real_workdir + os.sep) and full != real_workdir:
        return None, "path escapes the working directory"
    return full, None


def tool_write_file(path, content=""):
    """Write text to a file in the current working directory."""
    resolved, err = _safe_path(path)
    if err:
        return {"success": False, "error": err}
    try:
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
        with open(resolved, "w") as f:
            f.write(content)
        return {"success": True, "path": os.path.relpath(resolved, _WORKDIR)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# name -> (callable, JSON schema) for the OpenAI-style tools array.
TOOLS = {
    "now": (tool_now, {
        "type": "function",
        "function": {
            "name": "now",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}},
        }}),
    "active_window_info": (tool_active_window_info, {
        "type": "function",
        "function": {
            "name": "active_window_info",
            "description": "Get the application name and window title of the "
                           "frontmost window — useful when the title bar is too "
                           "small to read in the screenshot.",
            "parameters": {"type": "object", "properties": {}},
        }}),
    "system_stats": (tool_system_stats, {
        "type": "function",
        "function": {
            "name": "system_stats",
            "description": "Get battery level, CPU load average, free memory, "
                           "free disk space, and system uptime.",
            "parameters": {"type": "object", "properties": {}},
        }}),
    "now_playing": (tool_now_playing, {
        "type": "function",
        "function": {
            "name": "now_playing",
            "description": "Get the song currently playing in Music or Spotify, "
                           "if anything is playing.",
            "parameters": {"type": "object", "properties": {}},
        }}),
    "write_file": (tool_write_file, {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file in the current working directory. "
                           "Only files within the directory the script was launched from "
                           "can be written. Creates parent directories as needed. "
                           "Returns success or failure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path (from the script's directory) "
                                       "of the file to write. Use forward slashes.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        }}),
}

TOOL_SCHEMAS = [schema for _, schema in TOOLS.values()]


def run_tool(name, arguments):
    """Dispatch a tool call by name; never raises, always returns a dict."""
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}"}
    fn = entry[0]
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and \
            arguments.strip() else (arguments or {})
        if not isinstance(args, dict):
            args = {}
        return fn(**args)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# --------------------------------------------------------------------------
# Model call
# --------------------------------------------------------------------------
def ask_model(url, image_bytes, question, system):
    # image_bytes is optional: typed questions are sent as plain text, while
    # the periodic commentary attaches a fresh screenshot.
    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode()
        content = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": question},
        ]
    else:
        content = question

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]

    # Tool-calling loop: the model may ask for one or more read-only local
    # facts (window title, time, battery, now-playing) before answering. We run
    # the requested tools, feed the results back, and ask again — bounded so a
    # confused model can't loop forever.
    for _ in range(4):
        payload = {
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": 0.7,
            "max_tokens": 450,
            "stream": False,
            # Skip the reasoning phase: ~10x faster and content lands directly
            # in the message instead of reasoning_content. Honored by the
            # server's jinja chat template.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        choice = data["choices"][0]
        msg = choice["message"]

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            # Echo the assistant turn (with its tool_calls) then append one
            # tool result per call, keyed by id.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                fn = call.get("function", {})
                result = run_tool(fn.get("name", ""), fn.get("arguments", ""))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result),
                })
            continue  # ask again now that the model has its facts

        content = (msg.get("content") or "").strip()
        if content:
            return content
        # Reasoning model: answer may live in reasoning_content. If it got cut
        # off mid-thought, surface what we have rather than a blank line.
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            if choice.get("finish_reason") == "length":
                return "(model ran out of tokens while thinking) " + reasoning
            return reasoning
        return "(model returned no content)"

    return "(model kept calling tools without answering)"


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
class Audience:
    def __init__(self, url, interval, idle_timeout=120.0, max_backoff_mult=6,
                 health_interval=900.0, health_enabled=True, show_timing=False):
        self.url = url
        # When True, append the server's response time to each model message.
        self.show_timing = show_timing
        # base interval between shots; the live gap grows from here via backoff
        # and is jittered each cycle (see scheduler()).
        self.base_interval = interval
        self.idle_timeout = idle_timeout
        # Adaptive backoff: each time the screen is essentially unchanged we
        # bump backoff_level, multiplying the gap by min(2**level, max). A real
        # change snaps it back to 0. last_hash is the previous shot's aHash; a
        # Hamming distance <= change_threshold_bits (out of 64) counts as
        # "unchanged" — moderate sensitivity.
        self.max_backoff_mult = max_backoff_mult
        self.backoff_level = 0
        self.last_hash = None
        self.change_threshold_bits = 3
        # True once we've logged the "screen unchanged" notice for the current
        # quiet stretch, so we announce the lull once rather than every skip.
        self.lull_announced = False
        self.jobs = queue.Queue()        # (kind, payload)
        self.log = []                    # list of (style, text, transient) raw lines
        self.log_lock = threading.Lock()
        self.scroll = 0                  # lines scrolled up from bottom
        self.stop = threading.Event()
        # Shiny by default: the dragon sparkles for a moment each time a
        # screenshot is taken. (Disable the sparkles with --no-shiny.)
        self.shiny = True
        self.sparkle_until = 0.0   # monotonic deadline for the sparkle burst
        # Whether audience's own tab holds keyboard focus. Driven by terminal
        # focus-reporting events (ESC[I / ESC[O). Starts True: audience launches
        # in the foreground, so assume focused until the terminal says otherwise.
        self.focused = True
        # Our process's ancestor PIDs (shell, terminal GUI app, ...), computed
        # lazily once. Used to tell whether the frontmost app is our own
        # terminal, as an independent cross-check on the focus flag.
        self._ancestors = None
        # True once we've logged the "waiting" notice for the current focused
        # stretch, so we announce it once and not on every retry.
        self.waiting_announced = False
        # System-health watch: an independent ~health_interval loop checks
        # battery/CPU/memory and has the dragon quip when a condition crosses a
        # threshold. health_state maps an active condition key -> the tier we
        # last announced, so we warn once per episode (and again if it worsens)
        # rather than every tick. _last_batt is the previous (percent, monotonic)
        # battery sample, used to estimate drain rate.
        self.health_interval = health_interval
        self.health_enabled = health_enabled
        self.health_state = {}
        self._last_batt = None

    def _ancestor_pids(self):
        """Set of our PID and all ancestor PIDs (shell -> terminal app)."""
        if self._ancestors is not None:
            return self._ancestors
        pids, pid = set(), os.getpid()
        for _ in range(20):
            pids.add(pid)
            try:
                out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=2)
                ppid = int(out.stdout.strip())
            except Exception:
                break
            if ppid <= 1 or ppid in pids:
                break
            pid = ppid
        self._ancestors = pids
        return pids

    def is_own_window(self):
        """True if a screenshot now would catch the dragon watching itself.

        Primary signal is terminal focus-reporting (self.focused), which tracks
        focus at the tab level. But that flag goes stale if a focus-out event is
        ever dropped, which would pause screenshots forever. So we cross-check:
        if the frontmost window belongs to some app that isn't our terminal
        (its owner PID isn't one of our ancestors), we're plainly not looking at
        ourselves and can shoot regardless of a stale focus flag.
        """
        fg = frontmost_pid()
        if fg is not None and fg not in self._ancestor_pids():
            return False
        return self.focused

    # --- logging -----------------------------------------------------------
    def emit(self, text, style="normal", transient=False):
        # transient lines (e.g. "Screen's quiet", "This window is active") are
        # status notices that should vanish once real processing resumes; see
        # clear_transient().
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        with self.log_lock:
            self.log.append((style, f"[{stamp}] {text}", transient))

    def clear_transient(self):
        """Drop transient status hints — called when commentary resumes."""
        with self.log_lock:
            self.log = [e for e in self.log if not e[2]]

    def _recent_messages(self, limit=3):
        """Last N dragon/user messages as a text block for system prompt context."""
        with self.log_lock:
            pairs = [(s, t) for s, t, _ in self.log
                     if s in ("model", "you")]
        if not pairs:
            return None
        lines = []
        for style, text in pairs[-limit:]:
            label = "Dragon" if style == "model" else "You"
            lines.append(f"{label}: {text}")
        return "Recent exchange:\n" + "\n".join(lines) + "\n\n"

    # --- worker: serial model calls ---------------------------------------
    def worker(self):
        while not self.stop.is_set():
            try:
                kind, payload = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if kind == "commentary":
                question = payload or "Glance down from your perch at what the " \
                           "creature is doing now. One quick remark, in full " \
                           "dragon voice."
                self._do(question=question,
                          system=SYSTEM_PROMPT, screenshot=True)
            elif kind == "question":
                self.emit(f"you: {payload}", style="you")
                self._do(question=payload, system=QA_SYSTEM_PROMPT,
                          screenshot=False)
            elif kind == "health":
                self._do(question=payload, system=HEALTH_SYSTEM_PROMPT,
                          screenshot=False)

    def _do(self, question, system, screenshot):
        image = None
        if screenshot:
            # pause periodic commentary while the operator is away: no point
            # commenting on a static screen they aren't looking at. Announce
            # once per away-stretch, then re-check shortly until input resumes.
            if idle_seconds() >= self.idle_timeout:
                if not self.waiting_announced:
                    self.emit("You seem to be away — pausing screenshots "
                              "until you're back.", style="hint", transient=True)
                    self.waiting_announced = True
                self.schedule_screenshot(15)
                return
            # don't let the dragon watch itself: if audience's own tab is
            # focused, skip this shot and try again shortly. Announce the wait
            # only once per stretch so repeated retries don't spam the log.
            if self.is_own_window():
                if not self.waiting_announced:
                    self.emit("This window is active — waiting...",
                              style="hint", transient=True)
                    self.waiting_announced = True
                self.schedule_screenshot(15)
                return
            self.waiting_announced = False
            image = capture()  # fresh shot every call; nothing persisted
            if image is None:
                self.emit("screenshot failed — check Screen Recording "
                          "permission for your terminal.", style="error")
                return
            # Change detection: if the screen is essentially unchanged since the
            # last shot, there's nothing new to remark on. Skip the model call
            # and lengthen the interval (adaptive backoff). A real change resets
            # the backoff so commentary snaps back to the base cadence. An
            # un-hashable frame (h is None) counts as changed, so we never go
            # silent on a hashing failure.
            h = image_ahash(image)
            if (self.last_hash is not None and h is not None
                    and hamming(h, self.last_hash) <= self.change_threshold_bits):
                self.last_hash = h
                self.backoff_level = min(self.backoff_level + 1, 30)
                if not self.lull_announced:
                    self.emit("Screen's quiet — easing off until something "
                              "changes.", style="hint", transient=True)
                    self.lull_announced = True
                return
            self.last_hash = h
            self.backoff_level = 0
            self.lull_announced = False
            # processing resumed — clear any lingering "quiet"/"away"/"active" hints
            self.clear_transient()
            # a shot worth commenting on: let the dragon sparkle for a moment
            self.sparkle_until = time.monotonic() + DRAGON_SPARKLE_MS / 1000.0
        try:
            recent = self._recent_messages()
            if recent:
                system = system + recent
            t0 = time.monotonic()
            answer = ask_model(self.url, image, question, system)
            elapsed = time.monotonic() - t0
        except Exception as e:
            self.emit(f"model call failed: {e}", style="error")
            return
        if self.show_timing:
            answer = f"{answer}  ({elapsed:.1f}s)"
        self.emit(answer, style="model")

    # --- scheduler: periodic commentary -----------------------------------
    def scheduler(self):
        # initial shot 5s after start
        if self.stop.wait(5):
            return
        self.jobs.put(("commentary", None))
        while True:
            # Live gap = base * backoff multiplier, jittered +/-25% so the
            # cadence feels organic rather than metronomic. backoff_level is
            # updated by the worker (_do) after each shot's change check.
            mult = min(2 ** self.backoff_level, self.max_backoff_mult)
            delay = self.base_interval * mult * random.uniform(0.75, 1.25)
            if self.stop.wait(delay):
                return
            self.jobs.put(("commentary", None))

    def schedule_screenshot(self, delay=5, question=None):
        def go():
            if not self.stop.wait(delay):
                self.jobs.put(("commentary", question))
        threading.Thread(target=go, daemon=True).start()

    # --- health watch: periodic system-condition checks -------------------
    def evaluate_health(self):
        """Currently-active health conditions as (key, tier, fact) tuples.

        key is a stable id; tier is a small integer that increases as a
        condition worsens (so the worsening re-fires past the once-per-episode
        filter); fact is the plain-English line handed to the model. Every probe
        is defensive — a failed read just omits that condition.
        """
        findings = []

        batt = read_battery()
        if batt is not None and batt.get("state") == "discharging":
            pct = batt["percent"]
            # low battery: warn harder as it drops (tiers at <=20/10/5)
            if pct <= 20:
                tier = 3 if pct <= 5 else 2 if pct <= 10 else 1
                findings.append((
                    "battery_low", tier,
                    f"Battery is at {pct}% and discharging."))
            # drain rate from the previous sample
            now = time.monotonic()
            if self._last_batt is not None:
                prev_pct, prev_t = self._last_batt
                dh = (now - prev_t) / 3600.0
                drop = prev_pct - pct
                if dh > 0 and drop > 0 and pct < 25:
                    rate = drop / dh
                    if rate >= 25:
                        tier = 2 if rate >= 50 else 1
                        findings.append((
                            "battery_drain", tier,
                            f"Battery is draining fast: about {round(rate)}%/hr "
                            f"(now {pct}%)."))
            self._last_batt = (pct, now)
        else:
            # plugged in / unknown: reset the drain baseline so a later unplug
            # measures from fresh, not across the charge.
            self._last_batt = None

        try:
            load1 = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            ratio = load1 / cores
            if ratio >= 1.0:
                tier = 2 if ratio >= 2.0 else 1
                findings.append((
                    "cpu_high", tier,
                    f"CPU is under heavy load: 1-min load average {load1:.1f} "
                    f"across {cores} cores."))
        except Exception:
            pass

        mem = read_free_mem_mb()
        if mem is not None and mem < 500:
            tier = 2 if mem < 200 else 1
            findings.append((
                "mem_low", tier,
                f"Free memory is low: about {mem} MB available."))

        return findings

    def health_scheduler(self):
        """Independent loop: every ~health_interval, surface new health issues.

        Runs regardless of whether the operator is away or audience's own window
        is focused — a health alert (a dying or fast-draining battery, a pegged
        CPU) matters most precisely when you've stepped away, so it's never gated
        by the idle/away pause or the active-window lock that hold back
        screenshot commentary. Only newly active or worsened conditions are
        queued, so a steady problem warns once per episode rather than every tick.
        """
        while True:
            delay = self.health_interval * random.uniform(0.75, 1.25)
            if self.stop.wait(delay):
                return
            try:
                findings = self.evaluate_health()
            except Exception:
                findings = []
            active = set()
            for key, tier, fact in findings:
                active.add(key)
                prev = self.health_state.get(key)
                if prev is None or tier > prev:
                    self.health_state[key] = tier
                    self.jobs.put(("health", fact))
                else:
                    self.health_state[key] = tier
            # drop cleared conditions so they re-announce next time they occur
            for key in list(self.health_state):
                if key not in active:
                    del self.health_state[key]

    # --- input handling ----------------------------------------------------
    def handle_submit(self, text):
        text = text.strip()
        if not text:
            return
        if text in ("/quit", "/q", "/exit"):
            self.stop.set()
            return
        if text == "/screenshot":
            self.emit("screenshot scheduled in 5s…", style="hint")
            self.schedule_screenshot(5)
            return
        if text.startswith("/screenshot ") or text == "/screenshot?":
            question = text[12:].strip() if len(text) > 12 else ""
            if question:
                self.emit(f"screenshot in 5s… then I'll answer: \"{question}\"", style="hint")
                self.schedule_screenshot(5, question="Glance down from your perch at what the "
                          "creature is doing now and answer their question:\n\n"
                          f"{question}\n\n"
                          "Answer in full dragon voice, grounding your response in "
                          "what you can see on screen.")
            else:
                self.emit("screenshot scheduled in 5s…", style="hint")
                self.schedule_screenshot(5)
            return
        if text == "/help":
            self.emit("commands: /screenshot [question], /quit  — or type a question",
                      style="hint")
            return
        if text.startswith("/"):
            self.emit(f"unknown command: {text}", style="error")
            return
        self.jobs.put(("question", text))

    # --- curses UI ---------------------------------------------------------
    def render(self, stdscr, buf):
        h, w = stdscr.getmaxyx()
        out_h = h - 2  # leave bottom 2 rows for separator + input

        # The dragon is pinned to the top-right corner. Reserve its columns by
        # wrapping all text to a dragon-aware width, so no line ever wraps past
        # the dragon's edge only to be clipped — which would silently drop the
        # tail of a word and paint the sprite over it.
        tick = int(time.time() * 1000 / DRAGON_FRAME_MS)
        phase = tick % DRAGON_ANIM_PERIOD
        active = phase >= DRAGON_ANIM_PERIOD - DRAGON_ANIM_TICKS
        if active:
            body = DRAGON_FRAMES[phase % len(DRAGON_FRAMES)]
            # a single blink partway through the flourish
            eye = DRAGON_BLINK if phase == DRAGON_ANIM_PERIOD - 2 else DRAGON_EYE
        else:
            body = DRAGON_FRAMES[DRAGON_REST_FRAME]
            eye = DRAGON_EYE
        frame = [ln.replace('·', eye) for ln in body]
        dx = w - DRAGON_W - 1
        gutter = 1  # blank column between text and dragon
        text_cap = dx - gutter if dx > 0 else w - 1

        # wrap log into display lines
        with self.log_lock:
            entries = list(self.log)
        styles = {
            "you": curses.color_pair(1),
            "model": curses.color_pair(2),
            "error": curses.color_pair(3),
            "hint": curses.color_pair(4),
            "normal": curses.A_NORMAL,
        }
        wrapped = []  # (attr, text)
        for idx, (style, text, _transient) in enumerate(entries):
            if idx:  # blank spacer line between entries
                wrapped.append((curses.A_NORMAL, ""))
            attr = styles.get(style, curses.A_NORMAL)
            for line in textwrap.wrap(text, max(1, text_cap)) or [""]:
                wrapped.append((attr, line))

        total = len(wrapped)
        max_scroll = max(0, total - out_h)
        self.scroll = min(self.scroll, max_scroll)
        top = max(0, total - out_h - self.scroll)
        view = wrapped[top:top + out_h]

        stdscr.erase()
        for row, (attr, line) in enumerate(view):
            # lines are already wrapped to text_cap; clip is a safety net for
            # wide glyphs whose display width exceeds their character count.
            try:
                stdscr.addstr(row, 0, clip_to_width(line, text_cap), attr)
            except curses.error:
                pass

        # animated dragon pinned to the top-right corner. Frame advances on a
        # wall-clock timer.
        if dx > 0 and out_h >= len(frame):
            for i, line in enumerate(frame):
                try:
                    stdscr.addstr(i, dx, line, curses.color_pair(5) | curses.A_BOLD)
                except curses.error:
                    pass

            # shiny dragon: sparkle only in the brief window after a screenshot
            # is taken. A rotating subset of cells twinkles, over blank cells
            # only so the sprite stays intact.
            if self.shiny and time.monotonic() < self.sparkle_until:
                for n, (sr, sc) in enumerate(DRAGON_SPARKLE_CELLS):
                    if (tick + n) % 3:           # ~1/3 of cells lit per tick
                        continue
                    if sr >= len(frame) or sc >= len(frame[sr]) \
                            or frame[sr][sc] != ' ':
                        continue
                    glyph = DRAGON_SPARKLES[(tick + n) % len(DRAGON_SPARKLES)]
                    try:
                        stdscr.addstr(sr, dx + sc, glyph,
                                      curses.color_pair(4) | curses.A_BOLD)
                    except curses.error:
                        pass

        # separator + input
        sep = "─" * (w - 1)
        try:
            stdscr.addstr(h - 2, 0, sep, curses.A_DIM)
        except curses.error:
            pass
        prompt = "> "
        shown = (prompt + buf)[-(w - 1):]
        try:
            stdscr.addstr(h - 1, 0, shown, curses.A_BOLD)
        except curses.error:
            pass
        stdscr.move(h - 1, min(len(prompt) + len(buf), w - 1))
        stdscr.refresh()

    def run(self, stdscr):
        curses.curs_set(1)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)    # you
        curses.init_pair(2, curses.COLOR_GREEN, -1)   # model
        curses.init_pair(3, curses.COLOR_RED, -1)     # error
        curses.init_pair(4, curses.COLOR_YELLOW, -1)  # hint
        # gold for the dragon: use a true gold from the 256-color palette when
        # available, else fall back to yellow.
        gold = 178 if curses.COLORS >= 256 else curses.COLOR_YELLOW
        curses.init_pair(5, gold, -1)                 # dragon (gold)
        stdscr.nodelay(True)
        stdscr.keypad(True)

        # Ask the terminal to report focus changes (ESC[I / ESC[O) so we know
        # when audience's own tab is the focused one. Always turn it back off on
        # exit so the terminal doesn't keep echoing focus codes afterward.
        sys.stdout.write(FOCUS_REPORTING_ON)
        sys.stdout.flush()
        try:
            self._loop(stdscr)
        finally:
            sys.stdout.write(FOCUS_REPORTING_OFF)
            sys.stdout.flush()

    def _loop(self, stdscr):
        self.emit("audience ready. First screenshot in 5s. "
                  "Type a question, or /screenshot, /quit.", style="hint")
        if self.shiny:
            self.emit("✦ a shiny gold dragon is watching ✦", style="hint")

        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.scheduler, daemon=True).start()
        if self.health_enabled:
            threading.Thread(target=self.health_scheduler, daemon=True).start()

        buf = ""
        esc = 0  # escape-sequence state: 0=none, 1=saw ESC, 2=saw ESC[
        while not self.stop.is_set():
            self.render(stdscr, buf)
            try:
                ch = stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue
            if isinstance(ch, str):
                # Focus-reporting events arrive as the chars ESC, '[', 'I'/'O'.
                # Intercept them before they reach the prompt buffer.
                if esc == 0 and ch == "\x1b":
                    esc = 1
                    continue
                if esc == 1:
                    esc = 2 if ch == "[" else 0
                    continue
                if esc == 2:
                    esc = 0
                    if ch == "I":
                        self.focused = True
                        continue
                    if ch == "O":
                        self.focused = False
                        continue
                    # not a focus event — fall through and treat as normal input
                if ch in ("\n", "\r"):
                    self.handle_submit(buf)
                    buf = ""
                elif ch in ("\x7f", "\b"):  # backspace
                    buf = buf[:-1]
                elif ch == "\x03":          # Ctrl-C
                    self.stop.set()
                elif ch == "\x15":          # Ctrl-U clear line
                    buf = ""
                elif ch.isprintable():
                    buf += ch
            else:
                if ch == curses.KEY_BACKSPACE:
                    buf = buf[:-1]
                elif ch == curses.KEY_PPAGE:
                    self.scroll += 5
                elif ch == curses.KEY_NPAGE:
                    self.scroll = max(0, self.scroll - 5)
                elif ch == curses.KEY_RESIZE:
                    pass


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60.0,
                    help="base seconds between screenshots (default 60); the "
                         "live gap is jittered and grows via backoff when the "
                         "screen isn't changing")
    ap.add_argument("--max-backoff", type=float, default=6.0,
                    help="cap on how many times the base interval can stretch "
                         "while the screen stays unchanged (default 6)")
    ap.add_argument("--url", default="http://localhost:8080/v1/chat/completions",
                    help="llama.cpp OpenAI-compatible chat endpoint")
    ap.add_argument("--idle-timeout", type=float, default=120.0,
                    help="pause periodic screenshots after this many seconds "
                         "of no keyboard/mouse activity (default 120)")
    ap.add_argument("--no-shiny", action="store_true",
                    help="disable the shiny dragon sparkles (on by default)")
    ap.add_argument("--health-interval", type=float, default=900.0,
                    help="base seconds between system-health checks (battery, "
                         "CPU, memory); jittered like the screenshot interval "
                         "(default 900 = ~15 min)")
    ap.add_argument("--no-health", action="store_true",
                    help="disable the periodic system-health watch loop")
    ap.add_argument("--show-timing", action="store_true",
                    help="append the AI server's response time to each message "
                         "(off by default)")
    args = ap.parse_args()

    app = Audience(args.url, args.interval, args.idle_timeout,
                   max_backoff_mult=args.max_backoff,
                   health_interval=args.health_interval,
                   health_enabled=not args.no_health,
                   show_timing=args.show_timing)
    if args.no_shiny:
        app.shiny = False
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass
    print("bye.")


if __name__ == "__main__":
    main()
