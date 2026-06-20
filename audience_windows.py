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
"""Back-compat shim. The cross-platform entrypoint is now audience.py, which
auto-detects the OS. This file remains so `python audience_windows.py` keeps
working on Windows.
"""

from audiencelib import core
from audiencelib.platform_windows import WindowsPlatform


if __name__ == "__main__":
    core.main(WindowsPlatform)
