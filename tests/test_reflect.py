"""Tests for the reflection pass that stores synthesized insights."""

from audiencelib import memory as core


def test_add_insights_stores_hedged_insight(memory_dir):
    added = core.add_insights([
        {"text": "the operator is a seasoned Python developer", "confidence": 0.9},
    ])
    assert added == 1
    m = core.read_long_term()[0]
    assert m["category"] == core._INSIGHT_CATEGORY
    # An insight is the dragon's own deduction, capped like an inferred fact even
    # when the model claims high certainty.
    assert m["confidence"] <= 0.7
    assert m["first_seen"] == m["ts"]


def test_add_insights_dedups_on_repeat(memory_dir):
    assert core.add_insights([{"text": "repeated insight"}]) == 1
    assert core.add_insights([{"text": "repeated insight"}]) == 0
    assert core.tool_recall(query="repeated")["count"] == 1


def test_add_insights_caps_per_pass(memory_dir, monkeypatch):
    monkeypatch.setattr(core, "_MAX_INSIGHTS_PER_REFLECT", 2)
    added = core.add_insights([{"text": f"insight number {i}"} for i in range(5)])
    assert added == 2


def test_add_insights_ignores_empty_and_garbage(memory_dir):
    assert core.add_insights([]) == 0
    assert core.add_insights([{"text": ""}, "not a dict", {"nope": 1}]) == 0


def test_insights_are_prunable_not_pinned(memory_dir):
    core.add_insights([{"text": "a soft conclusion"}])
    assert core.read_long_term()[0].get("pinned") is None
