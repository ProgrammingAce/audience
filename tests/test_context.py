"""Tests for Audience._memory_context: the top slice of long-term facts is
pushed into the prompt for reliable recall, while gold stays pull-only."""

from audiencelib import memory
from audiencelib.core import Audience
from tests.fake_platform import FakePlatform


def _app():
    return Audience(FakePlatform(), url="http://test", interval=60)


def test_long_term_facts_are_inlined(memory_dir):
    # A saved long-term fact must appear in the prompt under the 'What you
    # remember' header — a small local model can't be trusted to call recall.
    memory.tool_remember(text="operator goes by Sam", confidence=1.0,
                         source="stated")

    ctx = _app()._memory_context() or ""
    assert "What you remember about the operator:" in ctx
    assert "Sam" in ctx


def test_self_and_operator_facts_under_separate_headers(memory_dir):
    memory.tool_remember(text="operator goes by Sam", confidence=1.0,
                         source="stated")
    memory.tool_remember(text="the dragon is named Smaug", confidence=1.0,
                         source="stated", subject="self")

    ctx = _app()._memory_context() or ""
    assert "What you remember about the operator:" in ctx
    assert "What you remember about yourself (the dragon):" in ctx


def test_low_confidence_fact_is_tagged_unsure(memory_dir):
    memory.tool_remember(text="maybe likes tea", confidence=0.4,
                         source="inferred")
    ctx = _app()._memory_context() or ""
    assert "maybe likes tea (unsure)" in ctx


def test_hoard_block_is_inlined(memory_dir):
    # The hoard is now pushed in as a compact summary: total + mood.
    memory.tool_adjust_gold(amount=500)
    ctx = _app()._memory_context() or ""
    assert "gold" in ctx.lower()
    assert "500" in ctx
    # Also contains "hoard" from the block prefix.
    assert "hoard" in ctx.lower()


def test_recent_exchanges_are_inlined(memory_dir):
    memory.record_short_term("You", "what's the time?")
    memory.record_short_term("Dragon", "half past dragon o'clock")

    ctx = _app()._memory_context() or ""
    assert "Recent exchange:" in ctx
    assert "what's the time?" in ctx
    assert "half past dragon o'clock" in ctx


def test_empty_when_nothing_recent(memory_dir):
    # No recent exchanges, no long-term, and no legacy gold -> no context block.
    # (The memory_dir fixture gives an empty dir; no gold.json exists by default.)
    assert _app()._memory_context() is None


def test_throbber_shows_until_first_token_then_clears():
    app = _app()
    entry, prefix, update = app._stream_line("model")
    # Opened but nothing streamed: this line is the active throbber target.
    assert app.throb_entry is entry
    assert app._throbber()  # non-empty animated dots
    # First streamed token clears the throbber (real text takes over).
    update("Half ")
    assert app.throb_entry is None
    # Finalizing also clears it (covers a reply that never streamed content).
    entry2, prefix2, _ = app._stream_line("model")
    assert app.throb_entry is entry2
    app._set_line(entry2, prefix2, "done")
    assert app.throb_entry is None
