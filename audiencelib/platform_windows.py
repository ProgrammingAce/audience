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
"""Windows platform for audience.

Screen capture via mss + Pillow; window/idle introspection via ctypes (user32);
system stats via psutil and WMI; now-playing by detecting the Spotify process.

Requires: Windows 10+, Python 3.9+, mss, Pillow, psutil (wmi optional).
"""

import ctypes
import ctypes.wintypes
import io
import os
import shutil
import subprocess
import sys
import time

import mss
import psutil

from .platform_base import Platform

WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.wintypes.HWND, ctypes.c_long)


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


class WindowsPlatform(Platform):
    supports_write_file = True

    def __init__(self):
        # Capture the foreground HWND *before* curses takes over so we know
        # which window the operator was looking at when the script started.
        self._start_hwnd = None
        # stdout saved across the curses session (see enter_ui/exit_ui).
        self._saved_stdout = None

    # --- capture & change detection ---------------------------------------
    def capture(self):
        """Take a fresh screenshot of the active window; return PNG bytes or None."""
        from PIL import Image

        img = None
        try:
            sct = mss.mss()
            pid = _get_active_window_pid()
            if pid is not None:
                rect = _get_window_rect(pid)
                if rect is not None:
                    shot = sct.grab(rect)
                    img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
            if img is None:
                shot = sct.grab(sct.monitors[0])
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
            sct.close()
        except Exception:
            return None

        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    def image_ahash(self, png_bytes):
        """64-bit average hash of PNG bytes, or None on failure."""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            gray = img.convert("L").resize((8, 8), Image.LANCZOS)
            px = list(gray.getdata())[:8 * 8]
            mean = sum(px) / len(px)
            bits = 0
            for p in px:
                bits = (bits << 1) | (1 if p > mean else 0)
            return bits
        except Exception:
            return None

    # --- environment probes -----------------------------------------------
    def idle_seconds(self):
        """Seconds since last keyboard/mouse input, or 0.0 on failure."""
        try:
            info = ctypes.wintypes.LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return (time.time() * 1000 - info.dwTime) / 1000.0
        except Exception:
            pass
        return 0.0

    def active_window_info(self):
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

    def read_battery(self):
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

    def read_free_mem_mb(self):
        """Free + available memory in MB, or None on failure."""
        try:
            mem = psutil.virtual_memory()
            return round(mem.available / (1024 * 1024))
        except Exception:
            return None

    def read_free_disk_gb(self):
        """Free disk space on root drive in GB, or None on failure."""
        try:
            usage = shutil.disk_usage("C:\\")
            return round(usage.free / (1024 ** 3), 1)
        except Exception:
            return None

    def read_loadavg(self):
        """(1m, 5m, 15m) load averages via psutil, or None on failure."""
        try:
            return psutil.getloadavg()
        except Exception:
            return None

    def cpu_count(self):
        return psutil.cpu_count(logical=True) or 1

    def read_uptime(self):
        """Human-readable uptime string, or None on failure."""
        try:
            uptime_secs = time.time() - psutil.boot_time()
            days = int(uptime_secs // 86400)
            hours = int((uptime_secs % 86400) // 3600)
            mins = int((uptime_secs % 3600) // 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{mins}m")
            return "up " + " ".join(parts)
        except Exception:
            return None

    def now_playing(self):
        """Returns 'Spotify' if the Spotify desktop app is running, else ''."""
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
        return ""

    # --- session / UI lifecycle -------------------------------------------
    def begin_session(self):
        self._start_hwnd = ctypes.windll.user32.GetForegroundWindow()

    def enter_ui(self):
        # Redirect stdout to stderr while curses is running. PSReadline
        # (PowerShell) captures stdout writes and can render BEL escape
        # sequences as garbled glyphs in the input box. Curses writes directly
        # to the console handle, so this doesn't affect the TUI display.
        self._saved_stdout = sys.stdout
        sys.stdout = sys.stderr

    def exit_ui(self):
        if self._saved_stdout is not None:
            sys.stdout = self._saved_stdout

    def is_own_window(self):
        """True if a screenshot now would catch the dragon watching itself."""
        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()

        # If the user switched to a DIFFERENT window, it's safe to screenshot.
        # If the same window is still on top the user clicked back to the dragon
        # — skip.
        if fg_hwnd != self._start_hwnd:
            return False

        # The same window handle is still on top, but it *might* be running a
        # *different* process (e.g. the user closed the original PowerShell and
        # opened another one that reused the same console handle). In that case
        # it's safe to screenshot. We compare PIDs as a tie-breaker.
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
