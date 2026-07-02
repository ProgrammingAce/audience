"""Tests for session summary retention logic."""

import datetime as dt
import os
import tempfile

from audiencelib.memory import (
    _read_jsonl, _append_jsonl, _rewrite_jsonl, _add_tombstones,
    _long_term_path, _machine_id,
)


def _make_session_fact(date_str, text):
    """Create a session-category fact dict."""
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    text_full = f"{date_str}: {text}" if not text.startswith(date_str) else text
    mem_id = f"session-{date_str}-{text[:20]}"
    return {
        "id": mem_id,
        "ts": now,
        "first_seen": now,
        "category": "session",
        "subject": "operator",
        "text": text_full,
        "confidence": 0.7,
    }


class TestSessionRetention:
    def test_seven_fits(self, tmp_path):
        from audiencelib import memory as mem_mod
        original = mem_mod._MEMORY_DIR
        mem_mod._MEMORY_DIR = str(tmp_path)
        try:
            for i in range(7):
                day = f"2026-07-{i+1:02d}"
                _append_jsonl(_long_term_path(),
                              _make_session_fact(day, f"worked on session {i}"))
            existing = [m for m in _read_jsonl(_long_term_path())
                       if m.get("category") == "session"]
            assert len(existing) == 7
        finally:
            mem_mod._MEMORY_DIR = original

    def test_eighth_tombstones_oldest(self, tmp_path):
        from audiencelib import memory as mem_mod
        original = mem_mod._MEMORY_DIR
        mem_mod._MEMORY_DIR = str(tmp_path)
        try:
            for i in range(7):
                day = f"2026-07-{i+1:02d}"
                _append_jsonl(_long_term_path(),
                              _make_session_fact(day, f"worked on session {i}"))
            # 8th entry
            day8 = "2026-07-08"
            _append_jsonl(_long_term_path(),
                          _make_session_fact(day8, "worked on session 7"))
            # Read all session facts
            all_entries = _read_jsonl(_long_term_path())
            session_entries = [e for e in all_entries if e.get("category") == "session"]
            # Should have 7 + 1 = 8 in file (tombstones written separately)
            assert len(session_entries) == 8
        finally:
            mem_mod._MEMORY_DIR = original
