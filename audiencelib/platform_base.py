"""Platform interface for audience.

The platform-independent :class:`audiencelib.core.Audience` holds one of these as
``self.platform`` and routes every OS-specific primitive through it. Each
concrete platform (macOS, Windows) subclasses this and supplies the bodies.

Every probe method is expected to be defensive — on any failure it should return
the documented "missing" value (``None``, ``0.0``, ``"(...)"``) rather than
raising, because the stats/health code treats a missing probe as simply omitted.
"""


class Platform:
    """Abstract base for OS-specific behaviour. Subclasses override everything."""

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

    # --- session / UI lifecycle -------------------------------------------
    def begin_session(self):
        """Called once before curses starts (e.g. capture the launch window)."""

    def enter_ui(self):
        """Called as the curses UI starts (focus reporting / stdout redirect)."""

    def exit_ui(self):
        """Called as the curses UI tears down; must undo :meth:`enter_ui`."""

    def note_focus(self, focused):
        """Record a terminal focus change (ESC[I / ESC[O). May be a no-op."""

    def is_own_window(self):
        """True if a screenshot now would catch the dragon watching itself."""
        raise NotImplementedError

    # --- capabilities ------------------------------------------------------
    #: Whether this platform exposes the write_file tool.
    supports_write_file = True
