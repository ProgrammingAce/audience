"""Tests for the shared is_own_window / focus logic in platform_base."""

from audiencelib.platform_base import Platform


class _FakeFocusPlatform(Platform):
    """Exercises the base is_own_window with a scriptable frontmost PID."""

    def __init__(self, frontmost, own_pids):
        super().__init__()
        self._frontmost = frontmost
        self._pids = set(own_pids)

    def _frontmost_pid(self):
        return self._frontmost

    def _own_pids(self):
        return self._pids


def test_starts_focused():
    assert Platform().focused is True


def test_note_focus_updates_flag():
    p = _FakeFocusPlatform(frontmost=100, own_pids={100})
    p.note_focus(False)
    assert p.focused is False
    p.note_focus(True)
    assert p.focused is True


def test_foreign_frontmost_overrides_stale_focus():
    # Frontmost app isn't ours -> not our window, even if focus flag is stale.
    p = _FakeFocusPlatform(frontmost=999, own_pids={100, 200})
    p.focused = True
    assert p.is_own_window() is False


def test_own_frontmost_defers_to_focus_flag():
    p = _FakeFocusPlatform(frontmost=100, own_pids={100, 200})
    p.focused = True
    assert p.is_own_window() is True
    p.focused = False
    assert p.is_own_window() is False


def test_unknown_frontmost_defers_to_focus_flag():
    # PID lookup failed (None) -> fall back to the focus flag.
    p = _FakeFocusPlatform(frontmost=None, own_pids={100})
    p.focused = True
    assert p.is_own_window() is True
    p.focused = False
    assert p.is_own_window() is False


def test_frontmost_lookup_error_is_safe():
    class Boom(_FakeFocusPlatform):
        def _frontmost_pid(self):
            raise RuntimeError("user32 hiccup")

    p = Boom(frontmost=0, own_pids={100})
    p.focused = True
    assert p.is_own_window() is True   # error treated as unknown -> focus flag
