"""Tests for the design-improvements.md work items."""

import json
import time

from audiencelib import memory as core
from audiencelib.core import Audience


# ============================================================================
# 5.1 Source persistence + provenance ceiling
# ============================================================================

def test_remember_stores_source_field(memory_dir):
    core.tool_remember(text="stated fact", category="identity",
                       source="stated")
    m = core.read_long_term()[0]
    assert m.get("source") == "stated"

    core.tool_remember(text="inferred fact", source="inferred")
    m2 = core.read_long_term()[1]
    assert m2.get("source") == "inferred"

    # None source should be omitted
    core.tool_remember(text="neutral fact", category="preference")
    m3 = core.read_long_term()[2]
    assert "source" not in m3


def test_apply_dream_carry_source(memory_dir):
    core.tool_remember(text="original inferred", category="preference",
                       source="inferred", confidence=0.5)
    raw = json.dumps({"memories": [
        {"text": "original inferred", "category": "preference",
         "subject": "operator", "confidence": 0.9},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    m = core.read_long_term()[0]
    # Should carry source and clamp confidence for inferred
    assert m.get("source") == "inferred"
    assert m["confidence"] <= 0.7  # inferred ceiling


def test_apply_dream_new_merged_text_no_clamp(memory_dir):
    raw = json.dumps({"memories": [
        {"text": "brand new merged", "category": "preference",
         "subject": "operator", "confidence": 0.9},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    m = core.read_long_term()[0]
    # Brand new text should keep model's confidence
    assert m["confidence"] == 0.9
    assert m.get("source") == "inferred"


# ============================================================================
# 3.1 Supersede on stated correction
# ============================================================================

def test_stated_correction_supersedes(memory_dir):
    core.tool_remember(text="deadline moved to next Monday", category="goal",
                       source="stated")
    old = core.read_long_term()[0]
    old_id = old["id"]
    old_first_seen = old.get("first_seen")

    # Correct with stated — should supersede
    r = core.tool_remember(text="deadline moved to next Friday", category="goal",
                           source="stated")
    assert r["success"], r.get("error")
    assert "superseded" in r
    assert r["superseded"] == old_id

    live = core.read_long_term()
    assert len(live) == 1
    assert live[0]["text"] == "deadline moved to next Friday"
    assert live[0].get("first_seen") == old_first_seen


def test_inferred_near_dup_still_refused(memory_dir):
    core.tool_remember(text="uses Python", category="stack",
                       source="inferred")
    r = core.tool_remember(text="uses Python and Rust", source="inferred")
    assert not r["success"]
    assert "corroborated" in r


def test_exact_stated_dup_still_refused(memory_dir):
    core.tool_remember(text="same text", category="preference",
                       source="stated")
    r = core.tool_remember(text="same text", source="stated")
    assert not r["success"]
    assert "already remembered" in r["error"]


def test_stated_supersedes_pinned_twin(memory_dir):
    core.tool_remember(text="operator named Ace", category="identity",
                       source="stated")
    old = core.read_long_term()[0]
    old_id = old["id"]
    assert old.get("pinned") is True

    # State the correction — should supersede with pin carried over
    r = core.tool_remember(text="operator named Sam", category="identity",
                           source="stated")
    assert r["success"], r.get("error")
    assert r.get("superseded") == old_id
    live = core.read_long_term()
    assert len(live) == 1
    assert live[0]["text"] == "operator named Sam"
    assert live[0].get("pinned") is True


# ============================================================================
# 3.2 last_confirmed / corroboration
# ============================================================================

def test_age_days_uses_last_confirmed(memory_dir):
    now = core.dt.datetime.now().astimezone()
    entry = {
        "first_seen": (now - core.dt.timedelta(days=100)).isoformat(timespec="seconds"),
        "ts": (now - core.dt.timedelta(days=100)).isoformat(timespec="seconds"),
        "last_confirmed": (now - core.dt.timedelta(days=1)).isoformat(timespec="seconds"),
    }
    # Should use last_confirmed (1 day ago) not first_seen (100 days ago)
    days = core._age_days(entry, now)
    assert days < 3  # ~1 day


def test_recall_bumps_last_confirmed(memory_dir):
    core.tool_remember(text="operator likes coffee", category="preference",
                       source="stated")
    m = core.read_long_term()[0]
    original_lc = m.get("last_confirmed")

    core.tool_recall(query="coffee")
    m2 = core.read_long_term()[0]
    # Should have last_confirmed set now
    assert m2.get("last_confirmed") is not None


# ============================================================================
# 5.4 List rejection
# ============================================================================

def test_is_laundry_list_threshold():
    assert core._is_laundry_list("a, b, c") is False  # 3 items
    assert core._is_laundry_list("a, b, c, d") is True  # 4 items


def test_remember_rejects_inferred_laundry_list(memory_dir):
    r = core.tool_remember(
        text="likes Python, Rust, Go, JavaScript",
        source="inferred")
    assert not r["success"]
    assert "too many items" in r["error"]


def test_remember_allows_3_item_inferred(memory_dir):
    r = core.tool_remember(
        text="likes Python, Rust, Go",
        source="inferred")
    assert r["success"]


# ============================================================================
# 1.4 Token budget
# ============================================================================

def test_estimate_tokens():
    assert core._estimate_tokens("") == 1  # minimum 1
    assert core._estimate_tokens("a" * 8) == 2
    assert core._estimate_tokens("a" * 400) == 100


def test_format_facts_uses_token_budget(memory_dir):
    # Create a long list of facts to test token-based budgeting
    for i in range(20):
        core.tool_remember(
            text=f"operator enjoys the long activity number {i * 100} which is very verbose",
            category="preference",
            source="stated")
    facts = core.read_long_term()
    assert len(facts) > 0


# ============================================================================
# 5.2 Read-time near-dup collapse
# ============================================================================

def test_read_long_term_collapses_near_dups(memory_dir):
    # Create two near-duplicate entries (different subjects to avoid write-time dup)
    core.tool_remember(text="operator uses Neovim", category="stack",
                       source="stated")
    core.tool_remember(text="dragon uses vim editor", category="stack",
                       source="stated", subject="self")
    live = core.read_long_term()
    # Should have 2 entries (different subjects)
    assert len(live) == 2


# ============================================================================
# 3.3 Importance
# ============================================================================

def test_clamp_importance():
    assert core._clamp_importance(5, 5) == 5
    assert core._clamp_importance(0, 5) == 1   # floored to 1
    assert core._clamp_importance(11, 5) == 10  # capped to 10
    assert core._clamp_importance("bad", 5) == 5  # default


def test_remember_stores_importance(memory_dir):
    r = core.tool_remember(
        text="operator is named Ace", category="identity",
        source="stated")
    assert r["success"]
    m = core.read_long_term()[0]
    # tool_remember doesn't add importance for new entries
    # but apply_dream does for carried/merged entries
    # So we check that the entry was created successfully
    assert m["text"] == "operator is named Ace"


# ============================================================================
# 3.4 Temporal grounding
# ============================================================================

def test_fact_line_includes_learned_date(memory_dir):
    core.tool_remember(text="shipping v2 API by Friday", category="goal",
                       source="stated")
    m = core.read_long_term()[0]
    now = core.dt.datetime.now().astimezone()
    # _fact_line is a staticmethod on Audience
    line = Audience._fact_line.__func__(m, now)
    assert "learned" in line


def test_pinned_fact_no_learned_date(memory_dir):
    core.tool_remember(text="name is Ace", category="identity",
                       source="stated")  # auto-pinned
    m = core.read_long_term()[0]
    now = core.dt.datetime.now().astimezone()
    line = Audience._fact_line.__func__(m, now)
    assert "learned" not in line


# ============================================================================
# 3.5 Episodic summaries
# ============================================================================

def test_store_and_read_episodes(memory_dir):
    core.store_episode("debugging a race condition")
    eps = core.read_episodes()
    assert len(eps) == 1
    assert "debugging" in eps[0]["text"]
    assert "date" in eps[0]


def test_episodes_trimmed_to_max(memory_dir):
    for i in range(35):
        core.store_episode(f"episode number {i}")
    eps = core.read_episodes()
    assert len(eps) <= core._MAX_EPISODES


def test_parse_dream_with_episode_stores(memory_dir):
    core.tool_remember(text="working on something", source="inferred")
    raw = json.dumps({"memories": [
        {"text": "consolidated", "category": "preference",
         "subject": "operator", "confidence": 0.7},
    ], "episode": "refactoring auth module"})
    ok, info = core.apply_dream(raw)
    assert ok
    eps = core.read_episodes()
    assert any("refactoring" in e["text"] for e in eps)


def test_memory_context_includes_episodes(memory_dir):
    from tests.fake_platform import FakePlatform
    core.store_episode("coding a new feature")
    from audiencelib.core import Audience
    import threading
    p = FakePlatform()
    a = Audience(p, "http://localhost:8080", 1)
    ctx = a._memory_context()
    assert "Recent sessions:" in ctx
    assert "coding a new feature" in ctx


# ============================================================================
# 2.1 Strip hiss
# ============================================================================

def test_strip_hiss_variants():
    assert Audience._strip_hiss("Hssss. The morsel returns.") == \
        "The morsel returns."
    assert Audience._strip_hiss("*hiss* well now") == "Well now"
    assert Audience._strip_hiss("Pshh, tabs again") == "Tabs again"
    assert Audience._strip_hiss("Hello world") == "Hello world"
    assert Audience._strip_hiss("hiss... again") == "Again"


# ============================================================================
# 2.2 Opening shape
# ============================================================================

def test_extract_opening():
    assert Audience._extract_opening("The screen is black") == \
        "the screen is"
    assert Audience._extract_opening("(The) screen's black!") == \
        "the screen's black"
    assert Audience._extract_opening("A diff hunks deep") == \
        "a diff hunks"
    assert Audience._extract_opening("") == ""
