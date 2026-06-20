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
"""audience — a local-LLM shoulder-surfer for Windows.

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
    python audience.py
    python audience.py --interval 30
    python audience.py --url http://localhost:8080/v1/chat/completions

Start the llama.cpp server first, e.g.:
    llama-server.exe -m gemma-4-E4B-it-Q4_K_M.gguf ^
        --mmproj mmproj-gemma-4-E4B.gguf --port 8080

Requires: Windows 10+, Python 3.9+, mss, Pillow, psutil.
"""

import argparse
import base64
import ctypes
import ctypes.wintypes
import curses
import datetime as dt
import io
import json
import os
import queue
import random
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
import shutil

import unicodedata
import mss
import psutil

WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.wintypes.HWND, ctypes.c_long)


def hamming(a, b):
    """Number of differing bits between two integer hashes."""
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------
# Platform abstraction layer
# --------------------------------------------------------------------------

def _capture_active_window():
    """Take a fresh screenshot of the active window, return PIL Image or None."""
    from PIL import Image

    try:
        sct = mss.MSS()
        pid = _get_active_window_pid()
        if pid is not None:
            rect = _get_window_rect(pid)
            if rect is not None:
                shot = sct.grab(rect)
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
                sct.close()
                return img
        shot = sct.grab(sct.monitors[0])
        img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
        sct.close()
        return img
    except Exception:
        pass
    return None


def _image_ahash(img):
    """64-bit average hash of a PIL Image, or None on failure."""
    try:
        from PIL import Image
        gray = img.convert("L").resize((8, 8), Image.LANCZOS)
        px = list(gray.get_flattened_data()[:8 * 8])
        mean = sum(px) / len(px)
        bits = 0
        for p in px:
            bits = (bits << 1) | (1 if p > mean else 0)
        return bits
    except Exception:
        return None


def _idle_seconds():
    """Seconds since last keyboard/mouse input, or 0.0 on failure."""
    try:
        info = ctypes.wintypes.LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return (time.time() * 1000 - info.dwTime) / 1000.0
    except Exception:
        pass
    return 0.0


def _get_active_window_pid():
    """PID of the process owning the foreground window, or None."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd:
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value
    except Exception:
        pass
    return None


def _get_window_rect(pid):
    """Largest visible window rectangle for the given PID, or None."""
    rect = None
    best_area = -1

    def enum_proc(hwnd, user_data):
        nonlocal rect, best_area
        try:
            w_pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
            if w_pid.value != pid:
                return True

            is_visible = ctypes.windll.user32.IsWindowVisible(hwnd)
            if not is_visible:
                return True

            w_rect = ctypes.wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(w_rect)):
                return True

            width = w_rect.right - w_rect.left
            height = w_rect.bottom - w_rect.top
            if width <= 0 or height <= 0:
                return True

            area = width * height
            if area > best_area:
                best_area = area
                rect = (w_rect.left, w_rect.top, w_rect.right, w_rect.bottom)
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
    return rect


def _get_active_window_title():
    """App name and window title of the foreground window."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return {"app": "(unknown)", "title": "(no title)"}

        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        try:
            proc = psutil.Process(pid.value)
            app_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            app_name = "(unknown)"

        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip() or "(no title)"

        return {"app": app_name, "title": title}
    except Exception:
        return {"app": "(unknown)", "title": "(no title)"}


def _get_battery_info():
    """Battery percent and state, or None on failure."""
    # Suppress wmi module stderr (e.g. "Invalid query" on desktops without
    # a battery) from leaking into the TUI via sys.stderr.
    _err = open(os.devnull, "w")
    _old = sys.stderr
    sys.stderr = _err
    try:
        import wmi
        try:
            c = wmi.WMI(wmi="root/WMI")
            for battery in c.Win32_Battery():
                try:
                    pct = getattr(battery, "EstimatedChargeRemaining", None)
                    if pct is not None:
                        charging = battery.Charging and battery.Charging != False
                        return {"percent": int(pct),
                                "state": "charging" if charging else "discharging"}
                except Exception:
                    continue
            # Fallback: WMI power management
            c2 = wmi.WMI(namespace="root/WMI")
            for b in c2.BatteryStatus():
                if hasattr(b, "Charging") and b.Charging:
                    return {"percent": 100, "state": "charging"}
                if hasattr(b, "BatteryStatus") and b.BatteryStatus == 1:
                    return {"percent": 0, "state": "discharging"}
        except Exception:
            pass
    except Exception:
        pass
    finally:
        sys.stderr = _old
        _err.close()

    # Last resort: WMIC
    try:
        out = subprocess.check_output(
            ["wmic", "path", "Win32_Battery", "get",
             "EstimatedChargeRemaining,Charging", "/value"],
            text=True, timeout=5, stderr=subprocess.DEVNULL)
        pct = state = None
        for line in out.strip().split("\n"):
            if line.startswith("EstimatedChargeRemaining="):
                pct = int(line.split("=", 1)[1].strip())
            if line.startswith("Charging="):
                val = line.split("=", 1)[1].strip()
                state = "charging" if val == "True" else "discharging"
        if pct is not None:
            return {"percent": pct, "state": state or "unknown"}
    except Exception:
        pass
    return None


def _free_mem_mb():
    """Free + available memory in MB, or None on failure."""
    try:
        mem = psutil.virtual_memory()
        return round(mem.available / (1024 * 1024))
    except Exception:
        return None


def _free_disk_gb():
    """Free disk space on root drive in GB, or None on failure."""
    try:
        usage = shutil.disk_usage("C:\\")
        return round(usage.free / (1024 ** 3), 1)
    except Exception:
        return None


def _get_system_stats():
    """System stats dict with battery, memory, disk, uptime info."""
    out = {}

    try:
        cores = psutil.cpu_count(logical=True) or 1
        load = psutil.getloadavg()
        out["load_avg"] = {
            "1m": round(load[0], 2),
            "5m": round(load[1], 2),
            "15m": round(load[2], 2),
        }
    except Exception:
        pass

    batt = _get_battery_info()
    if batt:
        out["battery"] = batt

    mem = _free_mem_mb()
    if mem is not None:
        out["memory_free_mb"] = mem

    disk = _free_disk_gb()
    if disk is not None:
        out["disk_free_gb"] = disk

    try:
        boot_time = psutil.boot_time()
        uptime_secs = time.time() - boot_time
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        mins = int((uptime_secs % 3600) // 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        out["uptime"] = "up " + " ".join(parts)
    except Exception:
        pass

    return out or {"error": "no stats available"}


def _now_playing():
    """Now playing from Spotify desktop, or empty."""
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = proc.info["name"] or ""
                exe = (proc.info["exe"] or "").lower()
                if "spotify" in name.lower() or "spotify" in exe:
                    return "Spotify"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return "(nothing playing)"


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


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
    "directions or describe your own wings; let the words carry the scales. "
    "Never start a remark with hissing or breathing sounds like 'Hssss', 'Hss', "
    "'Hiss', or 'Pshh' — it's overdone and gets old fast.\n"
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like "
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy "
    "labels for the operator within a short stretch. Vary your references or skip "
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
    "you used it."
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
    "stage directions or describe your own wings; let the words carry it. "
    "Never start a remark with hissing or breathing sounds like 'Hssss', 'Hss', "
    "'Hiss', or 'Pshh' — it's overdone and gets old fast.\n"
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like "
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy "
    "labels for the operator within a short stretch. Vary your references or skip "
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
    "give it. One or two sentences, no preamble. Never start with hissing sounds "
    "like 'Hssss', 'Hss', 'Hiss', or 'Pshh' — it's overdone and gets old fast. "
    "Don't recycle the same nicknames or diminutives — avoid repeating phrases like "
    "'the little spark', 'the little morsel', 'the little one', or similar cutesy "
    "labels for the operator within a short stretch. Vary your references or skip "
    "the nickname entirely."
)

# --------------------------------------------------------------------------
# Animated mascot
# --------------------------------------------------------------------------
DRAGON_FRAMES = [
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (        ) ', '  `-vvvv-´  '],
    ['   ~    ~   ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
]
DRAGON_W = 12
DRAGON_FRAME_MS = 500
DRAGON_EYE = '·'
DRAGON_EYES = ['·', '✦', '×', '◉', '@', '°']
DRAGON_BLINK = '-'
DRAGON_REST_FRAME = 0
DRAGON_ANIM_PERIOD = 24
DRAGON_ANIM_TICKS = 4
DRAGON_SPARKLE_MS = 1500
DRAGON_SPARKLES = ['✦', '✧', '·', '*']
DRAGON_SPARKLE_CELLS = [(0, 1), (0, 10), (1, 0), (2, 11), (4, 0), (4, 11), (1, 6)]


# --------------------------------------------------------------------------
# Agent tools
# --------------------------------------------------------------------------
def tool_now(**_):
    """Current local date and time."""
    now = dt.datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "human": now.strftime("%A %Y-%m-%d %H:%M:%S %Z"),
    }


def tool_active_window_info(**_):
    """App name and window title of the frontmost window."""
    return _get_active_window_title()


def tool_system_stats(**_):
    """Battery, CPU load, free memory, free disk, and uptime."""
    return _get_system_stats()


def tool_now_playing(**_):
    """Currently playing track from Spotify, if running."""
    player = _now_playing()
    return {"now_playing": player if player != "(nothing playing)" else "(nothing playing)"}


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
            "description": "Get the song currently playing in Spotify, "
                           "if anything is playing.",
            "parameters": {"type": "object", "properties": {}},
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

    for _ in range(4):
        payload = {
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": 0.7,
            "max_tokens": 450,
            "stream": False,
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
            continue

        content = (msg.get("content") or "").strip()
        if content:
            return content
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
        self.show_timing = show_timing
        self.base_interval = interval
        self.idle_timeout = idle_timeout
        self.max_backoff_mult = max_backoff_mult
        self.backoff_level = 0
        self.last_hash = None
        self.change_threshold_bits = 3
        self.lull_announced = False
        self.jobs = queue.Queue()
        self.log = []
        self.log_lock = threading.Lock()
        self.scroll = 0
        self.stop = threading.Event()
        self.shiny = True
        self.sparkle_until = 0.0
        self.focused = True
        self._ancestors = None
        self.waiting_announced = False
        # Capture the foreground HWND *before* curses takes over so we know
        # which window the operator was looking at when the script started.
        self._start_hwnd = ctypes.windll.user32.GetForegroundWindow()
        self.health_interval = health_interval
        self.health_enabled = health_enabled
        self.health_state = {}
        self._last_batt = None

    def _ancestor_pids(self):
        """Set of our PID and all ancestor PIDs (shell -> terminal app)."""
        if self._ancestors is not None:
            return self._ancestors

        pids = set()
        try:
            proc = psutil.Process()
            pids.add(proc.pid)
            while True:
                try:
                    parent = proc.parent()
                    if parent is None:
                        break
                    if parent.pid in pids:
                        break
                    pids.add(parent.pid)
                    proc = parent
                except psutil.NoSuchProcess:
                    break
        except Exception:
            pass

        self._ancestors = pids
        return pids

    def is_own_window(self):
        """True if a screenshot now would catch the dragon watching itself."""
        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()

        # If the user switched to a DIFFERENT window, it's safe to
        # screenshot.  If the same window is still on top the user
        # clicked back to the dragon — skip.
        if fg_hwnd != self._start_hwnd:
            return False

        # The same window handle is still on top, but it *might* be
        # running a *different* process (e.g. the user closed the
        # original PowerShell and opened another one that reused the
        # same console handle).  In that case it's safe to screenshot.
        # We compare PIDs as a tie-breaker.
        try:
            start_pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                self._start_hwnd, ctypes.byref(start_pid))
            fg_pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                fg_hwnd, ctypes.byref(fg_pid))
            if start_pid.value != fg_pid.value:
                return False
        except Exception:
            pass

        return True

    # --- logging -----------------------------------------------------------
    def emit(self, text, style="normal", transient=False):
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        with self.log_lock:
            self.log.append((style, f"[{stamp}] {text}", transient))

    def clear_transient(self):
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

    def clear_transient(self):
        with self.log_lock:
            self.log = [e for e in self.log if not e[2]]

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
        img = None
        if screenshot:
            if _idle_seconds() >= self.idle_timeout:
                if not self.waiting_announced:
                    self.emit("You seem to be away — pausing screenshots "
                              "until you're back.", style="hint", transient=True)
                    self.waiting_announced = True
                self.schedule_screenshot(15)
                return

            if self.is_own_window():
                if not self.waiting_announced:
                    self.emit("This window is active — waiting...",
                              style="hint", transient=True)
                    self.waiting_announced = True
                self.schedule_screenshot(15)
                return
            self.waiting_announced = False

            img = _capture_active_window()
            if img is None:
                self.emit("screenshot failed — check that your terminal has "
                          "access to capture the screen.", style="error")
                return

            h = _image_ahash(img)
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
            self.clear_transient()
            self.sparkle_until = time.monotonic() + DRAGON_SPARKLE_MS / 1000.0

            # Convert PIL Image to PNG bytes
            buf = io.BytesIO()
            try:
                img.save(buf, format="PNG")
                image = buf.getvalue()
            except Exception:
                self.emit("screenshot failed — unable to encode image.",
                          style="error")
                return

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
        if self.stop.wait(5):
            return
        self.jobs.put(("commentary", None))
        while True:
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

    # --- health watch -----------------------------------------------------
    def evaluate_health(self):
        findings = []

        batt = _get_battery_info()
        if batt is not None and batt.get("state") == "discharging":
            pct = batt["percent"]
            if pct <= 20:
                tier = 3 if pct <= 5 else 2 if pct <= 10 else 1
                findings.append((
                    "battery_low", tier,
                    f"Battery is at {pct}% and discharging."))
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
            self._last_batt = None

        try:
            stats = _get_system_stats()
            if "load_avg" in stats:
                load1 = stats["load_avg"]["1m"]
                cores = psutil.cpu_count(logical=True) or 1
                ratio = load1 / cores
                if ratio >= 1.0:
                    tier = 2 if ratio >= 2.0 else 1
                    findings.append((
                        "cpu_high", tier,
                        f"CPU is under heavy load: 1-min load average {load1:.1f} "
                        f"across {cores} cores."))
        except Exception:
            pass

        try:
            mem = _free_mem_mb()
            if mem is not None and mem < 500:
                tier = 2 if mem < 200 else 1
                findings.append((
                    "mem_low", tier,
                    f"Free memory is low: about {mem} MB available."))
        except Exception:
            pass

        return findings

    def health_scheduler(self):
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
        out_h = h - 2

        tick = int(time.time() * 1000 / DRAGON_FRAME_MS)
        phase = tick % DRAGON_ANIM_PERIOD
        active = phase >= DRAGON_ANIM_PERIOD - DRAGON_ANIM_TICKS
        if active:
            body = DRAGON_FRAMES[phase % len(DRAGON_FRAMES)]
            eye = DRAGON_BLINK if phase == DRAGON_ANIM_PERIOD - 2 else DRAGON_EYE
        else:
            body = DRAGON_FRAMES[DRAGON_REST_FRAME]
            eye = DRAGON_EYE
        frame = [ln.replace('·', eye) for ln in body]
        dx = w - DRAGON_W - 1
        gutter = 1
        text_cap = dx - gutter if dx > 0 else w - 1

        with self.log_lock:
            entries = list(self.log)
        styles = {
            "you": curses.color_pair(1),
            "model": curses.color_pair(2),
            "error": curses.color_pair(3),
            "hint": curses.color_pair(4),
            "normal": curses.A_NORMAL,
        }
        wrapped = []
        for idx, (style, text, _transient) in enumerate(entries):
            if idx:
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
            try:
                stdscr.addstr(row, 0, clip_to_width(line, text_cap), attr)
            except curses.error:
                pass

        if dx > 0 and out_h >= len(frame):
            for i, line in enumerate(frame):
                try:
                    stdscr.addstr(i, dx, line, curses.color_pair(5) | curses.A_BOLD)
                except curses.error:
                    pass

            if self.shiny and time.monotonic() < self.sparkle_until:
                for n, (sr, sc) in enumerate(DRAGON_SPARKLE_CELLS):
                    if (tick + n) % 3:
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
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        gold = 178 if curses.COLORS >= 256 else curses.COLOR_YELLOW
        curses.init_pair(5, gold, -1)
        stdscr.nodelay(True)
        stdscr.keypad(True)

        self._loop(stdscr)

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
        esc = 0
        while not self.stop.is_set():
            self.render(stdscr, buf)
            try:
                ch = stdscr.get_wch()
            except curses.error:
                time.sleep(0.05)
                continue
            if isinstance(ch, str):
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
                if ch in ("\n", "\r"):
                    self.handle_submit(buf)
                    buf = ""
                elif ch in ("\x7f", "\b"):
                    buf = buf[:-1]
                elif ch == "\x03":
                    self.stop.set()
                elif ch == "\x15":
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
    # Redirect stdout to stderr while curses is running. PSReadline (PowerShell)
    # captures stdout writes and can render BEL escape sequences as garbled
    # glyphs in the input box. Curses writes directly to the console handle,
    # so this doesn't affect the TUI display.
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = _saved_stdout
    print("bye.")


if __name__ == "__main__":
    main()
