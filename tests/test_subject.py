"""Tests for the memory subject dimension — keeping facts about the operator
distinct from facts about the dragon itself.

Regression cover for the identity-confusion bug: with one undifferentiated pool,
asking "what is your name?" handed back the operator's name. Subjects fix that by
filing self-facts apart from operator-facts.
"""

from audiencelib import memory as core


def test_normalize_subject_maps_aliases():
    assert core._normalize_subject("self") == core._SUBJECT_SELF
    assert core._normalize_subject("dragon") == core._SUBJECT_SELF
    assert core._normalize_subject("me") == core._SUBJECT_SELF
    assert core._normalize_subject("operator") == core._SUBJECT_OPERATOR
    assert core._normalize_subject(None) == core._SUBJECT_OPERATOR   # default
    assert core._normalize_subject("anything else") == core._SUBJECT_OPERATOR


def test_remember_defaults_to_operator_subject(memory_dir):
    r = core.tool_remember(text="builds a Rust CLI")
    assert r["subject"] == core._SUBJECT_OPERATOR
    assert core.read_long_term()[0]["subject"] == core._SUBJECT_OPERATOR


def test_same_text_distinct_per_subject(memory_dir):
    """The operator's name and the dragon's name can both be 'named Ace' without
    colliding or deduping into one entry."""
    op = core.tool_remember(text="named Ace", category="identity",
                            source="stated", subject="operator")
    me = core.tool_remember(text="named Ace", category="identity",
                            source="stated", subject="self")
    assert op["success"] and me["success"]
    assert op["id"] != me["id"]
    assert len(core.read_long_term()) == 2


def test_dupe_within_same_subject_refused(memory_dir):
    assert core.tool_remember(text="named Ace", subject="self")["success"]
    assert core.tool_remember(text="named Ace", subject="self")["error"] \
        == "already remembered"


def test_recall_filters_by_subject(memory_dir):
    core.tool_remember(text="the operator is named Ace", subject="operator")
    core.tool_remember(text="the dragon is named Smaug", subject="self")
    # Asked for the dragon's own name, recall returns only the self-fact.
    self_hits = core.tool_recall(query="named", subject="self")["matches"]
    assert [m["text"] for m in self_hits] == ["the dragon is named Smaug"]
    op_hits = core.tool_recall(query="named", subject="operator")["matches"]
    assert [m["text"] for m in op_hits] == ["the operator is named Ace"]
    # No filter sees both.
    assert core.tool_recall(query="named")["count"] == 2


def test_recall_reports_subject(memory_dir):
    core.tool_remember(text="a self note", subject="self")
    m = core.tool_recall(query="self note")["matches"][0]
    assert m["subject"] == core._SUBJECT_SELF


def test_edit_preserves_subject(memory_dir):
    mid = core.tool_remember(text="the dragon hoards gold", subject="self")["id"]
    new_id = core.edit_memory(mid, "the dragon hoards rubies")["id"]
    m = next(m for m in core.read_long_term() if m["id"] == new_id)
    assert m["subject"] == core._SUBJECT_SELF


def test_legacy_entry_without_subject_reads_as_operator(memory_dir):
    # An entry written before subjects existed (no subject key) is treated as an
    # operator fact on read, not silently mis-bucketed as self.
    core._append_jsonl(core._long_term_path(), {
        "id": "legacy01", "ts": "2020-01-01T00:00:00", "text": "old fact",
    })
    m = core.tool_recall(query="old")["matches"][0]
    assert m["subject"] == core._SUBJECT_OPERATOR
