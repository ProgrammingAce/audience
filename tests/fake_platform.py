"""A configurable, dependency-free Platform for headless tests.

Returns canned values for every probe so the platform-independent Audience
logic (scheduler/backoff, health evaluation, tool registry) can be exercised
without a real macOS/Windows host. Tweak the public attributes per test.
"""

from audiencelib.platform_base import Platform


class FakePlatform(Platform):
    supports_write_file = True

    def __init__(self, **overrides):
        # Probe return values; override any of these per test.
        self.png = b"\x89PNG\r\n\x1a\n"   # capture() result
        self.ahash = 0                    # image_ahash() result
        self.idle = 0.0                   # idle_seconds()
        self.window = {"app": "TestApp", "title": "Test Window"}
        self.battery = None               # e.g. {"percent": 50, "state": "discharging"}
        self.free_mem_mb = 8000
        self.free_disk_gb = 100.0
        self.loadavg = (0.5, 0.5, 0.5)
        self.cpus = 4
        self.uptime = "up 1h"
        self.playing = ""
        self.own_window = False
        # Call counters / lifecycle flags for assertions.
        self.captures = 0
        self.focus_events = []
        self.in_ui = False
        for k, v in overrides.items():
            setattr(self, k, v)

    # --- capture & change detection ---------------------------------------
    def capture(self):
        self.captures += 1
        return self.png

    def image_ahash(self, png_bytes):
        return self.ahash

    # --- environment probes -----------------------------------------------
    def idle_seconds(self):
        return self.idle

    def active_window_info(self):
        return self.window

    def read_battery(self):
        return self.battery

    def read_free_mem_mb(self):
        return self.free_mem_mb

    def read_free_disk_gb(self):
        return self.free_disk_gb

    def read_loadavg(self):
        return self.loadavg

    def cpu_count(self):
        return self.cpus

    def read_uptime(self):
        return self.uptime

    def now_playing(self):
        return self.playing

    # --- session / UI lifecycle -------------------------------------------
    def enter_ui(self):
        self.in_ui = True

    def exit_ui(self):
        self.in_ui = False

    def note_focus(self, focused):
        self.focus_events.append(focused)

    def is_own_window(self):
        return self.own_window
