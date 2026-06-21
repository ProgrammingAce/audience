"""Tests for lexical recall ranking and the age helper behind staleness."""

import datetime as dt

from audiencelib import memory as core


def test_age_days_uses_first_seen_then_ts():
    now = dt.datetime.now().astimezone()
    past = (now - dt.timedelta(days=10)).isoformat(timespec="seconds")
    assert round(core._age_days({"first_seen": past}, now)) == 10
    assert round(core._age_days({"ts": past}, now)) == 10  # falls back to ts
    assert core._age_days({}, now) == 0.0                  # nothing to go on
    assert core._age_days({"first_seen": "garbage"}, now) == 0.0


def test_tokenize_drops_stopwords_and_glue():
    assert core._tokenize("the operator writes Python") == {"operator", "writes",
                                                            "python"}


def test_rank_memories_relevance_beats_insertion_order(memory_dir):
    core.tool_remember(text="enjoys hiking on weekends")
    core.tool_remember(text="writes Python code daily")
    ranked = core.rank_memories(core.read_long_term(), query="python code")
    assert ranked[0]["text"] == "writes Python code daily"


def test_recall_empty_query_orders_by_confidence(memory_dir):
    core.tool_remember(text="low confidence guess", confidence=0.3,
                       source="inferred")
    core.tool_remember(text="high confidence claim", confidence=0.95,
                       source="stated")
    first = core.tool_recall(query="")["matches"][0]["text"]
    assert first == "high confidence claim"


def test_rank_memories_puts_pinned_first(memory_dir):
    core.tool_remember(text="just a passing note")
    pid = core.tool_remember(text="the operator is named Ace", category="identity",
                             source="stated")["id"]  # auto-pinned
    ranked = core.rank_memories(core.read_long_term())
    assert ranked[0]["id"] == pid
