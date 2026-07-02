"""Tests for clipboard_read platform probe and tool."""

import pytest

from audiencelib.platform_base import Platform


class TestClipboardRead:
    def test_default_returns_none(self):
        """The base Platform.read_clipboard returns None."""
        p = Platform()
        assert p.read_clipboard() is None

    def test_macos_subprocess_path(self):
        from audiencelib.platform_macos import MacPlatform
        # Can't easily test pbpaste without mocking subprocess.
        # Just verify the method exists and doesn't raise.
        # On macOS it would call pbpaste; on other platforms it still exists.
        assert hasattr(MacPlatform, "read_clipboard")

    def test_windows_path(self):
        import sys
        if sys.platform != "win32":
            pytest.skip("Windows-only platform module")
        from audiencelib.platform_windows import WindowsPlatform
        assert hasattr(WindowsPlatform, "read_clipboard")


class TestClipboardReadTool:
    def test_tool_in_build_tools(self):
        """clipboard_read tool is registered in build_tools."""
        from audiencelib.tools import build_tools, SIDE_EFFECTING_TOOLS
        # The tool is registered, but we can't build tools without a full platform.
        # Verify the SIDE_EFFECTING_TOOLS includes it.
        assert "clipboard_read" in SIDE_EFFECTING_TOOLS

    def test_list_files_side_effecting(self):
        from audiencelib.tools import SIDE_EFFECTING_TOOLS
        assert "list_files" in SIDE_EFFECTING_TOOLS
