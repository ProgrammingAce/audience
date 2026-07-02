"""Tests for scheduled reminders storage and tools."""

import datetime as dt
import os
import tempfile
import pytest

from audiencelib.memory import (
    tool_set_reminder, tool_list_reminders, tool_cancel_reminder,
    scan_stale_reminders, effective_reminders, _reminder_path,
    _read_reminders, _shard_path, _machine_id, _append_jsonl,
    _read_jsonl,
)


@pytest.fixture
def memory_dir(tmp_path):
    """Set up a temporary memory directory for one test."""
    from audiencelib import memory as mem_mod
    original = mem_mod._MEMORY_DIR
    mem_mod._MEMORY_DIR = str(tmp_path)
    yield tmp_path
    mem_mod._MEMORY_DIR = original


@pytest.fixture
def machine_id_patch(monkeypatch):
    """Ensure a stable machine id for tests."""
    monkeypatch.setattr("socket.gethostname", lambda: "testhost")
    return "testhost"


class TestSetReminder:
    def test_basic_success(self, memory_dir):
        result = tool_set_reminder(
            text="stretch",
            due_iso=dt.datetime.now().astimezone().replace(
                hour=dt.datetime.now().astimezone().hour + 1).isoformat()
        )
        assert "error" not in result
        assert result["text"] == "stretch"
        assert "due_human" in result
        assert "id" in result

    def test_rejects_empty_text(self, memory_dir):
        result = tool_set_reminder(text="", due_iso="2030-01-01T00:00:00+00:00")
        assert "error" in result

    def test_rejects_past_due(self, memory_dir):
        result = tool_set_reminder(
            text="stretch",
            due_iso="2020-01-01T00:00:00+00:00"
        )
        assert "error" in result
        assert "future" in result["error"]

    def test_rejects_bad_iso(self, memory_dir):
        result = tool_set_reminder(text="stretch", due_iso="not-a-date")
        assert "error" in result

    def test_rejects_empty_due(self, memory_dir):
        result = tool_set_reminder(text="stretch", due_iso="")
        assert "error" in result

    def test_caps_text(self, memory_dir):
        long_text = "x" * 500
        result = tool_set_reminder(text=long_text, due_iso="2030-01-01T00:00:00+00:00")
        assert "error" not in result
        assert len(result["text"]) <= 300

    def test_deterministic_id(self, memory_dir):
        due = "2030-01-01T00:00:00+00:00"
        r1 = tool_set_reminder(text="stretch", due_iso=due)
        r2 = tool_set_reminder(text="stretch", due_iso=due)
        # First should succeed, second should be rejected as duplicate
        assert "error" not in r1
        assert "error" in r2
        assert "already" in r2["error"]


class TestListReminders:
    def test_empty_when_none(self, memory_dir):
        result = tool_list_reminders()
        assert result["reminders"] == []

    def test_shows_pending_reminders(self, memory_dir):
        due = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour + 1
        ).isoformat()
        tool_set_reminder(text="stretch", due_iso=due)
        result = tool_list_reminders()
        assert len(result["reminders"]) >= 1
        assert any(r["text"] == "stretch" for r in result["reminders"])

    def test_excludes_cancelled(self, memory_dir):
        due = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour + 1
        ).isoformat()
        r = tool_set_reminder(text="stretch", due_iso=due)
        tool_cancel_reminder(id=r["id"])
        result = tool_list_reminders()
        assert not any(rm["text"] == "stretch" for rm in result["reminders"])


class TestCancelReminder:
    def test_cancel_pending(self, memory_dir):
        due = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour + 1
        ).isoformat()
        r = tool_set_reminder(text="stretch", due_iso=due)
        result = tool_cancel_reminder(id=r["id"])
        assert result.get("success") is True
        assert "error" not in result

    def test_rejects_nonexistent(self, memory_dir):
        result = tool_cancel_reminder(id="nonexistent-id")
        assert "error" in result

    def test_rejects_already_cancelled(self, memory_dir):
        due = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour + 1
        ).isoformat()
        r = tool_set_reminder(text="stretch", due_iso=due)
        tool_cancel_reminder(id=r["id"])
        result = tool_cancel_reminder(id=r["id"])
        assert "error" in result
        assert "not pending" in result["error"]

    def test_rejects_empty_id(self, memory_dir):
        result = tool_cancel_reminder(id="")
        assert "error" in result


class TestScanStaleReminders:
    def test_no_stale(self, memory_dir):
        due = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour + 1
        ).isoformat()
        tool_set_reminder(text="stretch", due_iso=due)
        result = scan_stale_reminders()
        assert result == []

    def test_fires_within_24h(self, memory_dir):
        # Manually append a reminder that's 1 hour past due (within 24h)
        past = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour - 1
        ).isoformat()
        past_dt = dt.datetime.now().astimezone().replace(
            hour=dt.datetime.now().astimezone().hour - 1
        ).isoformat(timespec="seconds")
        _append_jsonl(_reminder_path(), {
            "id": "stale-1h-id",
            "text": "stretch",
            "due": past,
            "due_dt": past_dt,
            "created": dt.datetime.now().astimezone().isoformat(),
            "status": "pending",
        })
        result = scan_stale_reminders()
        assert "stretch" in result

    def test_skips_over_24h(self, memory_dir):
        # Create a reminder that's 25 hours past due
        past = dt.datetime(2020, 1, 1, 0, 0, 0).astimezone().isoformat()
        # Manually append a record with old due time
        path = _reminder_path()
        _append_jsonl(path, {
            "id": "old-stale-id",
            "text": "old reminder",
            "due": past,
            "due_dt": past,
            "created": dt.datetime.now().astimezone().isoformat(),
            "status": "pending",
        })
        result = scan_stale_reminders()
        assert "old reminder" not in result
        # Verify the record was marked as fired
        by_id = effective_reminders()
        assert by_id.get("old-stale-id", {}).get("status") == "fired"
