"""Shared fixtures: redirect the memory store at a tmp dir for every test.

The memory tools in audiencelib.core read/write module-level paths derived from
core._MEMORY_DIR. Pointing it at a per-test tmp_path keeps tests hermetic and
off the real ~/.audience store.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audiencelib import core


@pytest.fixture
def memory_dir(tmp_path):
    """Point the core memory store at a fresh tmp dir, restoring afterward."""
    prev = core._MEMORY_DIR
    core.set_memory_dir(str(tmp_path))
    try:
        yield tmp_path
    finally:
        core._MEMORY_DIR = prev
