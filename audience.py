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

Cross-platform entrypoint: selects the OS-specific Platform implementation and
hands it to the shared core. All common logic lives in the audiencelib package
(audiencelib.core); only screen capture, window/idle detection, system stats,
and the UI lifecycle are platform-specific (audiencelib.platform_macos /
audiencelib.platform_windows).

Usage:
    python3 audience.py
    python3 audience.py --interval 30
    python3 audience.py --url http://localhost:8080/v1/chat/completions

Start the llama.cpp server first, e.g.:
    llama-server -m gemma-4-E4B-it-Q4_K_M.gguf \
        --mmproj mmproj-gemma-4-E4B.gguf --port 8080
"""

import sys

from audiencelib import core


def _platform_factory():
    if sys.platform == "darwin":
        try:
            from audiencelib.platform_macos import MacPlatform
        except ImportError as e:
            sys.exit(f"audience is missing a macOS dependency ({e.name}). "
                     "Install them with:\n"
                     "    pip install -r requirements-macos.txt")
        return MacPlatform()
    if sys.platform.startswith("win"):
        try:
            from audiencelib.platform_windows import WindowsPlatform
        except ImportError as e:
            sys.exit(f"audience is missing a Windows dependency ({e.name}). "
                     "Install them with:\n"
                     "    pip install -r requirements-windows.txt")
        return WindowsPlatform()
    sys.exit("audience supports macOS and Windows only "
             f"(this is {sys.platform!r}).")


if __name__ == "__main__":
    core.main(_platform_factory)
