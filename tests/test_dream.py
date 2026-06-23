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


def test_dream_preserves_age_of_carried_fact(memory_dir):
    core.tool_remember(text="a lasting fact")
    original = core.read_long_term()[0]
    orig_ts, orig_first = original["ts"], original["first_seen"]
    # The dream returns the same fact verbatim (same content-hash id), so its age
    # must survive rather than reset to now.
    raw = json.dumps({"memories": [{"text": "a lasting fact", "confidence": 0.7}]})
    ok, _ = core.apply_dream(raw)
    assert ok
    after = core.read_long_term()[0]
    assert after["ts"] == orig_ts
    assert after["first_seen"] == orig_first


def test_dream_cannot_drop_a_pinned_fact(memory_dir):
    core.tool_remember(text="the operator is named Ace", category="identity",
                       source="stated")  # auto-pinned
    core.tool_remember(text="ephemeral note")
    # The model's dream omits the pinned fact entirely; it must be re-injected.
    raw = json.dumps({"memories": [{"text": "something else", "confidence": 0.6}]})
    ok, _ = core.apply_dream(raw)
    assert ok
    survivors = core.read_long_term()
    pin = [m for m in survivors if m["text"] == "the operator is named Ace"]
    assert pin and pin[0].get("pinned") is True
    # ...and it isn't tombstoned away on the next read either.
    assert core.tool_recall(query="Ace")["count"] == 1


def test_dream_keeps_subject_of_carried_self_fact(memory_dir):
    # A self fact carried through verbatim must stay a self fact even when the
    # dream model forgets to echo the subject — recovered from the prior entry.
    core.tool_remember(text="the dragon is named Smaug", category="identity",
                       source="stated", subject="self")
    core.tool_remember(text="the operator uses Rust")
    raw = json.dumps({"memories": [
        {"text": "the dragon is named Smaug", "confidence": 0.9},  # subject omitted
        {"text": "the operator uses Rust", "subject": "operator", "confidence": 0.8},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    by_text = {m["text"]: m for m in core.read_long_term()}
    assert by_text["the dragon is named Smaug"]["subject"] == core._SUBJECT_SELF
    assert by_text["the operator uses Rust"]["subject"] == core._SUBJECT_OPERATOR


def test_dream_does_not_merge_across_subjects(memory_dir):
    # Same text under two subjects stays two distinct memories after a dream.
    core.tool_remember(text="named Ace", subject="operator")
    core.tool_remember(text="named Ace", subject="self")
    raw = json.dumps({"memories": [
        {"text": "named Ace", "subject": "operator", "confidence": 0.8},
        {"text": "named Ace", "subject": "self", "confidence": 0.8},
    ]})
    ok, info = core.apply_dream(raw)
    assert ok and info == 2
    subjects = sorted(m["subject"] for m in core.read_long_term())
    assert subjects == [core._SUBJECT_OPERATOR, core._SUBJECT_SELF]


def test_dream_collapses_near_duplicates(memory_dir):
    # The model's dream returns two barely-different facts about the operator
    # plus a distinct one; the near-dups collapse to the strongest (highest
    # confidence), and the distinct fact survives.
    raw = json.dumps({"memories": [
        {"text": "the operator writes Python code every day", "confidence": 0.6},
        {"text": "the operator writes Python code daily", "confidence": 0.9},
        {"text": "the operator drinks black coffee", "confidence": 0.7},
    ]})
    ok, info = core.apply_dream(raw)
    assert ok and info == 2
    survivors = {m["text"]: m for m in core.read_long_term()}
    assert "the operator drinks black coffee" in survivors
    python = [m for m in survivors.values() if "Python" in m["text"]]
    assert len(python) == 1
    assert python[0]["confidence"] == 0.9  # the strongest of the cluster kept


def test_dream_collapse_keeps_a_pin_over_a_stronger_twin(memory_dir):
    # A pinned fact must win a near-dup collapse even against a higher-confidence
    # reworded twin the dream proposes.
    core.tool_remember(text="the operator is named Ace Programmer",
                       category="identity", source="stated")  # auto-pinned
    raw = json.dumps({"memories": [
        {"text": "operator goes by the name Ace Programmer", "confidence": 0.95},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    survivors = core.read_long_term()
    assert len(survivors) == 1
    assert survivors[0]["text"] == "the operator is named Ace Programmer"
    assert survivors[0].get("pinned") is True


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


def test_dream_caps_insight_confidence(memory_dir):
    # The model returns an insight at full certainty (as the corroboration loop
    # would push it); the dream must hold it to the reflected ceiling so a hedged
    # deduction can't outrank concrete facts.
    raw = json.dumps({"memories": [
        {"category": "insight", "text": "the operator likes detail",
         "confidence": 1.0},
        {"category": "project", "text": "the operator builds a cyberdeck",
         "confidence": 0.9},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    by_text = {m["text"]: m for m in core.read_long_term()}
    assert by_text["the operator likes detail"]["confidence"] == core._REFLECTED_CEILING
    # a non-insight fact is unaffected by the cap
    assert by_text["the operator builds a cyberdeck"]["confidence"] == 0.9


def test_dream_collapses_insights_at_looser_bar(memory_dir):
    # Two insights whose similarity (~0.38) sits between the insight bar (0.35) and
    # the general bar (0.5): they collapse because both are insights, where the same
    # pair as ordinary facts would survive. (Genuinely reworded dups below 0.35 are
    # beyond any token check and rely on the model + throttling instead.)
    a = "operator values precise granular technical detail"
    b = "operator values granular mechanical rigor"
    assert (core._INSIGHT_DUP_SIMILARITY
            <= core._text_similarity(a, b) < core._DUP_SIMILARITY)
    raw = json.dumps({"memories": [
        {"category": "insight", "subject": "operator", "text": a, "confidence": 0.7},
        {"category": "insight", "subject": "operator", "text": b, "confidence": 0.6},
    ]})
    ok, _ = core.apply_dream(raw)
    assert ok
    insights = [m for m in core.read_long_term()
                if m["category"] == core._INSIGHT_CATEGORY]
    assert len(insights) == 1


def test_add_insights_capped_at_total(memory_dir):
    insights = [{"text": f"insight number {i}", "confidence": 0.6}
                for i in range(core._MAX_INSIGHTS_TOTAL + 4)]
    # Per-pass cap limits one call, so add across enough passes to hit the total.
    for _ in range(core._MAX_INSIGHTS_TOTAL + 4):
        core.add_insights(insights)
    live = [m for m in core.read_long_term()
            if m["category"] == core._INSIGHT_CATEGORY]
    assert len(live) == core._MAX_INSIGHTS_TOTAL
