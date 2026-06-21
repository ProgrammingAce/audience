"""Tests for dream parsing and consolidation."""

import json
import os

from audiencelib import memory as core


def test_parse_dream_plain():
    raw = '{"memories": [{"text": "a", "confidence": 0.9}]}'
    assert core._parse_dream(raw) == [{"text": "a", "confidence": 0.9}]


def test_parse_dream_code_fence():
    raw = '```json\n{"memories": [{"text": "a"}]}\n```'
    assert core._parse_dream(raw) == [{"text": "a"}]


def test_parse_dream_surrounding_prose():
    raw = 'Here you go:\n{"memories": []}\nhope that helps'
    assert core._parse_dream(raw) == []


def test_parse_dream_garbage():
    assert core._parse_dream("not json at all") is None
    assert core._parse_dream("") is None
    assert core._parse_dream('{"nope": 1}') is None


def test_apply_dream_rewrites_and_backs_up(memory_dir):
    core.tool_remember(text="old fact one")
    core.tool_remember(text="old fact two")
    raw = json.dumps({"memories": [
        {"category": "identity", "text": "consolidated fact", "confidence": 0.8},
        {"text": "consolidated fact", "confidence": 0.8},  # duplicate, dropped
    ]})
    ok, info = core.apply_dream(raw)
    assert ok and info == 1
    texts = [m["text"] for m in core.tool_recall(query="")["matches"]]
    assert texts == ["consolidated fact"]
    # prior store preserved in the backup (this machine's own backup shard)
    bak = os.path.join(str(memory_dir),
                       f"long_term.{core._machine_id()}.bak.jsonl")
    assert os.path.exists(bak)
    assert "old fact one" in open(bak).read()


def test_apply_dream_untouched_on_bad_input(memory_dir):
    core.tool_remember(text="keep me")
    ok, reason = core.apply_dream("total garbage")
    assert not ok
    assert core.tool_recall(query="keep")["count"] == 1


def test_dream_does_not_resurrect_a_peers_originals(memory_dir, monkeypatch):
    # A peer machine wrote two facts into its own shard...
    monkeypatch.setattr(core, "_machine_id", lambda: "peer")
    core.tool_remember(text="peer fact one")
    core.tool_remember(text="peer fact two")
    # ...and this machine dreams them into a single consolidated fact.
    monkeypatch.setattr(core, "_machine_id", lambda: "me")
    raw = json.dumps({"memories": [{"text": "merged peer facts"}]})
    ok, info = core.apply_dream(raw)
    assert ok and info == 1
    # The peer's shard still physically holds the originals, but tombstones keep
    # them from re-surfacing on the union read.
    texts = [m["text"] for m in core.tool_recall(query="")["matches"]]
    assert texts == ["merged peer facts"]
