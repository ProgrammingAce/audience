"""Tests for the long-term memory, gold ledger, and confidence helpers."""

from audiencelib import core


def test_clamp_confidence():
    assert core._clamp_confidence(0.5, 0.6) == 0.5
    assert core._clamp_confidence(2.0, 0.6) == 1.0
    assert core._clamp_confidence(-1.0, 0.6) == 0.0
    assert core._clamp_confidence("nope", 0.6) == 0.6
    assert core._clamp_confidence(None, 0.6) == 0.6


def test_resolve_confidence_by_source():
    core.set_active_source("stated")
    try:
        assert core._resolve_confidence(0.2) == 0.9   # floored high
        assert core._resolve_confidence(1.0) == 1.0
    finally:
        core.set_active_source(None)

    core.set_active_source("inferred")
    try:
        assert core._resolve_confidence(1.0) == 0.7   # capped
        assert core._resolve_confidence(0.4) == 0.4
    finally:
        core.set_active_source(None)

    assert core._resolve_confidence(0.55) == 0.55     # unknown source: trust claim


def test_remember_recall_forget(memory_dir):
    r = core.tool_remember(text="operator goes by Sam", category="identity")
    assert r["success"]
    mem_id = r["id"]

    got = core.tool_recall(query="sam")
    assert got["count"] == 1
    assert got["matches"][0]["text"] == "operator goes by Sam"

    f = core.tool_forget(id=mem_id)
    assert f["success"]
    assert core.tool_recall(query="sam")["count"] == 0


def test_remember_rejects_empty_and_dupes(memory_dir):
    assert not core.tool_remember(text="")["success"]
    assert core.tool_remember(text="builds a Rust CLI")["success"]
    assert core.tool_remember(text="builds a Rust CLI")["error"] == "already remembered"


def test_remember_truncates_long_text(memory_dir):
    long = "x" * (core._MAX_MEMORY_TEXT + 50)
    core.tool_remember(text=long)
    stored = core.tool_recall(query="")["matches"][0]["text"]
    assert len(stored) == core._MAX_MEMORY_TEXT


def test_remember_enforces_cap(memory_dir, monkeypatch):
    monkeypatch.setattr(core, "_MAX_MEMORIES", 3)
    for i in range(3):
        assert core.tool_remember(text=f"fact {i}")["success"]
    full = core.tool_remember(text="one too many")
    assert not full["success"]
    assert "full" in full["error"]


def test_forget_unknown_id(memory_dir):
    core.tool_remember(text="a fact")
    assert not core.tool_forget(id="deadbeef")["success"]


def test_gold_adjust_and_total(memory_dir):
    assert core.tool_gold_total()["total"] == 0
    r = core.tool_adjust_gold(amount=10)
    assert r["total"] == 10 and r["change"] == 10
    r = core.tool_adjust_gold(amount=-3)
    assert r["total"] == 7
    assert core.tool_gold_total()["total"] == 7


def test_gold_rejects_zero_and_nonint(memory_dir):
    assert not core.tool_adjust_gold(amount=0)["success"]
    assert not core.tool_adjust_gold(amount="lots")["success"]


def test_gold_clamps_delta(memory_dir):
    r = core.tool_adjust_gold(amount=10 ** 9)
    assert r["change"] == core._MAX_GOLD_DELTA
