"""Tests for the long-term memory, gold ledger, and confidence helpers."""

from audiencelib import memory as core
from audiencelib import tools


def test_clamp_confidence():
    assert core._clamp_confidence(0.5, 0.6) == 0.5
    assert core._clamp_confidence(2.0, 0.6) == 1.0
    assert core._clamp_confidence(-1.0, 0.6) == 0.0
    assert core._clamp_confidence("nope", 0.6) == 0.6
    assert core._clamp_confidence(None, 0.6) == 0.6


def test_resolve_confidence_by_source():
    assert core._resolve_confidence(0.2, "stated") == 0.9    # floored high
    assert core._resolve_confidence(1.0, "stated") == 1.0
    assert core._resolve_confidence(1.0, "inferred") == 0.7  # capped
    assert core._resolve_confidence(0.4, "inferred") == 0.4
    assert core._resolve_confidence(0.55, None) == 0.55      # unknown: trust claim


def test_remember_clamps_confidence_by_source(memory_dir):
    # A screenshot-inferred fact is capped even if the model claims certainty.
    r = core.tool_remember(text="inferred high claim", confidence=1.0,
                           source="inferred")
    assert r["confidence"] == 0.7
    # An operator-stated fact is floored high even on a low claim.
    r = core.tool_remember(text="stated low claim", confidence=0.1,
                           source="stated")
    assert r["confidence"] == 0.9


def test_run_tool_injects_source_over_model_value(memory_dir):
    registry = {"remember": (core.tool_remember, {})}
    # Model tries to pass source="stated"; dispatcher must override with the
    # real provenance ("inferred"), so the fact is capped, not floored.
    res = tools.run_tool(registry, "remember",
                         '{"text": "injected", "source": "stated"}',
                         source="inferred")
    # inferred default (no claim) is 0.5; had the model's "stated" stuck it
    # would have floored to 0.9, so 0.5 proves the override.
    assert res["confidence"] == 0.5


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


# --- cross-machine sharing (Dropbox-synced --memory-dir) --------------------

def _as_machine(monkeypatch, name):
    """Pretend the current process is running on host `name`."""
    monkeypatch.setattr(core, "_machine_id", lambda: name)


def test_writes_land_in_per_machine_shard(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    core.tool_remember(text="a fact")
    import os
    assert os.path.exists(os.path.join(str(memory_dir), "long_term.laptop.jsonl"))
    assert not os.path.exists(os.path.join(str(memory_dir), "long_term.jsonl"))


def test_recall_unions_across_machine_shards(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    core.tool_remember(text="written on the laptop")
    _as_machine(monkeypatch, "desktop")
    core.tool_remember(text="written on the desktop")
    # From either machine, recall sees both shards.
    texts = {m["text"] for m in core.tool_recall(query="")["matches"]}
    assert texts == {"written on the laptop", "written on the desktop"}


def test_duplicate_fact_across_machines_collapses(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    core.tool_remember(text="same fact")
    _as_machine(monkeypatch, "desktop")
    core.tool_remember(text="same fact")  # identical -> same content-hash id
    assert core.tool_recall(query="")["count"] == 1


def test_forget_suppresses_a_peers_memory(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    mem_id = core.tool_remember(text="a peer wrote this")["id"]
    # A different machine forgets it; it must not need to rewrite the peer shard.
    _as_machine(monkeypatch, "desktop")
    assert core.tool_forget(id=mem_id)["success"]
    assert core.tool_recall(query="peer")["count"] == 0
    # The laptop's data shard is left intact; suppression is via a tombstone.
    import os
    assert os.path.exists(os.path.join(str(memory_dir), "long_term.laptop.jsonl"))


def test_re_remember_lifts_own_tombstone(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    mem_id = core.tool_remember(text="on again off again")["id"]
    core.tool_forget(id=mem_id)
    assert core.tool_recall(query="again")["count"] == 0
    assert core.tool_remember(text="on again off again")["success"]
    assert core.tool_recall(query="again")["count"] == 1


def test_gold_ledger_sums_across_machines(memory_dir, monkeypatch):
    _as_machine(monkeypatch, "laptop")
    core.tool_adjust_gold(amount=10)
    _as_machine(monkeypatch, "desktop")
    core.tool_adjust_gold(amount=-3)
    # Both machines' deltas survive and the total is their sum.
    assert core.tool_gold_total()["total"] == 7
