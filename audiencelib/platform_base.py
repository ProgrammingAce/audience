"""Platform interface for audience.

The platform-independent :class:`audiencelib.core.Audience` holds one of these as
``self.platform`` and routes every OS-specific primitive through it. Each
concrete platform (macOS, Windows) subclasses this and supplies the bodies.

Every probe method is expected to be defensive — on any failure it should return
the documented "missing" value (``None``, ``0.0``, ``"(...)"``) rather than
raising, because the stats/health code treats a missing probe as simply omitted.
"""

import sys


# xterm focus-reporting (DECSET 1004): when enabled the terminal emits ESC[I
# when audience's tab/window gains keyboard focus and ESC[O when it loses it.
# That per-tab focus state is what tells us whether the dragon would be looking
# at itself. Supported by iTerm2, Terminal.app, kitty, wezterm, alacritty, and
# Windows Terminal. Shared across platforms so both detect focus the same way.
FOCUS_REPORTING_ON = "\033[?1004h"
FOCUS_REPORTING_OFF = "\033[?1004l"


class Platform:
    """Abstract base for OS-specific behaviour. Subclasses override everything."""

    def __init__(self):
        # Whether audience's own tab holds keyboard focus, driven by terminal
        # focus-reporting events (ESC[I / ESC[O). Starts True: audience launches
        # in the foreground, so assume focused until the terminal says otherwise.
        self.focused = True
        # Cache of our PID + ancestor PIDs, computed lazily once by _own_pids().
        self._ancestors = None

    # --- focus / own-window detection (shared) ----------------------------
    def _write_focus_reporting(self, enable):
        """Turn terminal focus-reporting on/off; best-effort, never raises.

        Written to the live terminal (current stdout) before any platform-side
        stdout redirection, so the escape reaches the console, not a sink.
        """
        seq = FOCUS_REPORTING_ON if enable else FOCUS_REPORTING_OFF
        try:
            sys.stdout.write(seq)
            sys.stdout.flush()
        except Exception:
            pass

    def _frontmost_pid(self):
        """PID owning the frontmost on-screen window, or None. OS-specific."""
        raise NotImplementedError

    def _own_pids(self):
        """Set of our PID and all ancestor PIDs (shell -> terminal app)."""
        raise NotImplementedError

    # --- capture & change detection ---------------------------------------
    def capture(self):
        """Take a fresh screenshot of the active window; return PNG bytes or None."""
        raise NotImplementedError

    def image_ahash(self, png_bytes):
        """64-bit average hash of PNG bytes, or None on failure."""
        raise NotImplementedError

    # --- environment probes -----------------------------------------------
    def idle_seconds(self):
        """Seconds since the last user input event, or 0.0 on failure."""
        raise NotImplementedError

    def active_window_info(self):
        """``{"app": ..., "title": ...}`` for the frontmost window."""
        raise NotImplementedError

    def read_battery(self):
        """``{"percent": int, "state": str}`` or None on failure."""
        raise NotImplementedError

    def read_free_mem_mb(self):
        """Free memory in MB, or None on failure."""
        raise NotImplementedError

    def read_free_disk_gb(self):
        """Free disk space on the root volume in GB, or None on failure."""
        raise NotImplementedError

    def read_loadavg(self):
        """``(load1, load5, load15)`` floats, or None on failure."""
        raise NotImplementedError

    def cpu_count(self):
        """Number of logical CPUs (at least 1)."""
        raise NotImplementedError

    def read_uptime(self):
        """Human-readable uptime string, or None on failure."""
        raise NotImplementedError

    def now_playing(self):
        """Currently playing track/app as a string (may be empty)."""
        raise NotImplementedError

    def read_clipboard(self):
        """Current clipboard text, or None if empty/non-text/unavailable."""
        return None

    # --- session / UI lifecycle -------------------------------------------
    def begin_session(self):
        """Called once before curses starts (e.g. capture the launch window)."""

    def enter_ui(self):
        """Called as the curses UI starts (focus reporting / stdout redirect)."""

    def exit_ui(self):
        """Called as the curses UI tears down; must undo :meth:`enter_ui`."""

    def note_focus(self, focused):
        """Record a terminal focus change (ESC[I / ESC[O)."""
        self.focused = focused

    def is_own_window(self):
        """True if a screenshot now would catch the dragon watching itself.

        Shared across platforms: the primary signal is terminal focus-reporting
        (self.focused), which tracks focus at the tab level. That flag can go
        stale if a focus-out event is ever dropped, which would pause
        screenshots forever — so we cross-check the live frontmost window. If it
        belongs to some app that isn't our terminal (its owner PID isn't one of
        our ancestors), we're plainly not looking at ourselves and can shoot
        regardless of a stale focus flag.
        """
        try:
            fg = self._frontmost_pid()
        except Exception:
            fg = None
        if fg is not None and fg not in self._own_pids():
            return False
        return self.focused

    # --- capabilities ------------------------------------------------------
    #: Whether this platform exposes the write_file tool.
    supports_write_file = True
