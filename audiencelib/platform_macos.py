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
"""macOS platform for audience.

Screen capture via `screencapture` + Quartz; window/idle introspection via
Quartz; system stats via pmset/vm_stat/uptime; now-playing via osascript.

Requires: macOS (screencapture + Quartz/AppKit). Grant Screen Recording
permission to your terminal app.
"""

import os
import subprocess
import sys
import tempfile

import Quartz

from .platform_base import Platform

# screencapture emits this on stderr when a target window can't be grabbed
# (e.g. it closed between picking the id and capturing). It's expected, not an
# error worth showing — everything else from screencapture is forwarded to the
# console.
_SCREENCAPTURE_NOISE = "could not create image from window"


def _screencapture(cmd):
    """Run a screencapture command, returning True on success.

    stderr is captured so subprocess output never bleeds raw into the curses
    TUI. The known "could not create image from window" noise is dropped; any
    other diagnostics are forwarded to the real console via sys.stderr.
    """
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True)
    err = "\n".join(
        line for line in (r.stderr or "").splitlines()
        if line.strip() and _SCREENCAPTURE_NOISE not in line
    )
    if err:
        print(err, file=sys.stderr, flush=True)
    return r.returncode == 0


def _onscreen_windows():
    """On-screen normal windows, front-to-back (Quartz's native order)."""
    options = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
    return Quartz.CGWindowListCopyWindowInfo(
        options, Quartz.kCGNullWindowID) or []


def _frontmost_pid():
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


def _frontmost_window_id():
    """CGWindowID of the frontmost app's largest normal window, or None."""
    pid = _frontmost_pid()

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


class MacPlatform(Platform):
    supports_write_file = True

    def __init__(self):
        super().__init__()

    # --- capture & change detection ---------------------------------------
    def capture(self):
        """Take a fresh screenshot of the active window and return PNG bytes.

        The file is written to a unique path, read, and deleted immediately so
        nothing is persisted and no stale image can ever be reused. Returns the
        PNG bytes, or None on failure.
        """
        fd, path = tempfile.mkstemp(prefix="audience-", suffix=".png")
        os.close(fd)
        os.unlink(path)  # screencapture needs a non-existent target to write cleanly
        try:
            try:
                wid = _frontmost_window_id()
            except Exception:
                wid = None

            ok = False
            if wid is not None:
                ok = _screencapture(
                    ["screencapture", "-x", "-o", "-l", str(wid), path]
                ) and os.path.exists(path)
            if not ok:
                ok = _screencapture(
                    ["screencapture", "-x", path]
                ) and os.path.exists(path)
            if not ok:
                return None
            with open(path, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def image_ahash(self, png_bytes):
        """64-bit average hash of a PNG, or None on failure.

        Decodes the PNG with CoreGraphics (already imported as Quartz), draws it
        into an 8x8 grayscale buffer we own, and sets each of the 64 bits to
        (pixel > mean). Dependency-free; returns None if anything goes wrong so
        the caller can treat an un-hashable frame as "changed" rather than going
        silent.
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

    # --- environment probes -----------------------------------------------
    def idle_seconds(self):
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

    def active_window_info(self):
        """App name and window title of the frontmost window.

        Fixes the model's biggest blind spot: tiny, unreadable title bars. Uses
        the same window metadata capture() relies on — and the same thread-safe
        Quartz window ordering, so it never disagrees with the captured window.
        """
        app_name, title = None, None
        try:
            pid = _frontmost_pid()
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

    def read_battery(self):
        """Battery percent and charge state via pmset, or None on failure."""
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

    def read_free_mem_mb(self):
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

    def read_free_disk_gb(self):
        """Free space on the root volume in GB, or None on failure."""
        try:
            import shutil
            return round(shutil.disk_usage("/").free / (1024 ** 3), 1)
        except Exception:
            return None

    def read_loadavg(self):
        """(1m, 5m, 15m) load averages, or None on failure."""
        try:
            return os.getloadavg()
        except Exception:
            return None

    def cpu_count(self):
        return os.cpu_count() or 1

    def read_uptime(self):
        """uptime(1) output, or None on failure."""
        try:
            r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        except Exception:
            return None

    def now_playing(self):
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
            return r.stdout.strip()
        except Exception:
            return ""

    def read_clipboard(self):
        """Current clipboard text via pbpaste, or None on failure."""
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                return r.stdout
            return None
        except Exception:
            return None

    # --- session / UI lifecycle -------------------------------------------
    def enter_ui(self):
        # Ask the terminal to report focus changes (ESC[I / ESC[O) so we know
        # when audience's own tab is the focused one.
        self._write_focus_reporting(True)

    def exit_ui(self):
        # Always turn focus-reporting back off so the terminal doesn't keep
        # echoing focus codes afterward.
        self._write_focus_reporting(False)

    # --- own-window detection: feed the shared base logic -----------------
    def _frontmost_pid(self):
        return _frontmost_pid()

    def _own_pids(self):
        """Our PID and all ancestor PIDs (shell -> terminal app), cached once."""
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
