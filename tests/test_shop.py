"""Tests for the treasure shop, hoard mood, /gold, and gold_history."""

import datetime as dt
import os
import time

import pytest

from audiencelib import memory as core
from audiencelib import tools


# --------------------------------------------------------------------------
# _humanize_age
# --------------------------------------------------------------------------

class TestHumanizeAge:
    def test_seconds(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(seconds=15)).isoformat()
        assert "15s" in core._humanize_age(ts, now=now, compact=True)
        assert "15 seconds ago" in core._humanize_age(ts, now=now, compact=False)

    def test_one_second(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(seconds=1)).isoformat()
        assert core._humanize_age(ts, now=now, compact=False) == "just now"

    def test_minutes(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(minutes=30)).isoformat()
        assert "30m" == core._humanize_age(ts, now=now, compact=True)
        assert "30 minutes ago" == core._humanize_age(ts, now=now, compact=False)

    def test_one_minute(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(minutes=1)).isoformat()
        assert core._humanize_age(ts, now=now, compact=False) == "1 minute ago"

    def test_hours(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(hours=3)).isoformat()
        assert "3h" == core._humanize_age(ts, now=now, compact=True)
        assert "3 hours ago" == core._humanize_age(ts, now=now, compact=False)

    def test_one_hour(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(hours=1)).isoformat()
        assert core._humanize_age(ts, now=now, compact=False) == "1 hour ago"

    def test_days(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(days=5)).isoformat()
        assert "5d" == core._humanize_age(ts, now=now, compact=True)
        assert "5 days ago" == core._humanize_age(ts, now=now, compact=False)

    def test_one_day(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(days=1)).isoformat()
        assert core._humanize_age(ts, now=now, compact=False) == "1 day ago"

    def test_years(self):
        now = dt.datetime.now().astimezone()
        ts = (now - dt.timedelta(days=730)).isoformat()
        assert "2y" == core._humanize_age(ts, now=now, compact=True)
        assert "2 years ago" == core._humanize_age(ts, now=now, compact=False)

    def test_future(self):
        now = dt.datetime.now().astimezone()
        ts = (now + dt.timedelta(hours=1)).isoformat()
        assert "future" == core._humanize_age(ts, now=now, compact=True)
        assert "in the future" == core._humanize_age(ts, now=now, compact=False)

    def test_invalid_ts(self):
        assert "unknown" == core._humanize_age("not-a-date", compact=True)
        assert "unknown" == core._humanize_age("", compact=False)

    def test_zero_seconds(self):
        now = dt.datetime.now().astimezone()
        ts = now.isoformat()
        assert core._humanize_age(ts, now=now, compact=False) == "just now"


# --------------------------------------------------------------------------
# Treasure ledger — _gold() subtracts treasure costs
# --------------------------------------------------------------------------

class TestReadGoldWithTreasures:
    @pytest.mark.usefixtures("memory_dir")
    def test_purchase_debits_total(self):
        # Set up a legacy gold base of 1000.
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        # Buy a trinket (50 gold).
        r = core.tool_buy_treasure("a shiny pebble", "trinket", "A pebble that shines")
        assert r["success"]
        assert _gold() == 1000 - 50

    @pytest.mark.usefixtures("memory_dir")
    def test_total_after_multiple_purchases(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        core.tool_buy_treasure("trinket one", "trinket")
        core.tool_buy_treasure("a gem", "gem")
        total = _gold()
        assert total == 1000 - 50 - 250


def _gold():
    return core._read_gold()


# --------------------------------------------------------------------------
# tool_adjust_gold — source and clamping
# --------------------------------------------------------------------------

class TestAdjustGoldClamp:
    @pytest.mark.usefixtures("memory_dir")
    def test_clamp_returns_flag(self):
        r = core.tool_adjust_gold(amount=999999, reason="big award")
        assert r["success"]
        assert r["clamped"] is True
        assert r["change"] == core._MAX_GOLD_DELTA

    @pytest.mark.usefixtures("memory_dir")
    def test_negative_clamp_returns_flag(self):
        r = core.tool_adjust_gold(amount=-999999, reason="big fine")
        assert r["success"]
        assert r["clamped"] is True
        assert r["change"] == -core._MAX_GOLD_DELTA

    @pytest.mark.usefixtures("memory_dir")
    def test_under_limit_no_clamp(self):
        r = core.tool_adjust_gold(amount=500, reason="small award")
        assert r["success"]
        assert "clamped" not in r
        assert r["change"] == 500

    @pytest.mark.usefixtures("memory_dir")
    def test_source_recorded(self):
        r = core.tool_adjust_gold(amount=10, reason="test", source="model")
        assert r["success"]
        # Verify the record has source.
        entries = core._read_jsonl(core._gold_ledger_path())
        assert entries[-1]["source"] == "model"

    @pytest.mark.usefixtures("memory_dir")
    def test_zero_amount_rejected(self):
        r = core.tool_adjust_gold(amount=0, reason="nope")
        assert not r["success"]
        assert "non-zero" in r["error"]

    @pytest.mark.usefixtures("memory_dir")
    def test_non_numeric_rejected(self):
        r = core.tool_adjust_gold(amount="abc", reason="nope")
        assert not r["success"]


# --------------------------------------------------------------------------
# tool_buy_treasure
# --------------------------------------------------------------------------

class TestBuyTreasure:
    @pytest.mark.usefixtures("memory_dir")
    def test_basic_purchase(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 500}, f)
        r = core.tool_buy_treasure("a ruby the size of a fist", "gem",
                                   "Pried from a mountain")
        assert r["success"]
        assert r["tier"] == "gem"
        assert r["cost"] == 250
        assert r["remaining"] == 250

    @pytest.mark.usefixtures("memory_dir")
    def test_insufficient_funds(self):
        r = core.tool_buy_treasure("a star", "wonder")
        total = _gold()
        assert not r["success"]
        assert "short" in r["error"]
        assert str(5000 - total) in r["error"]

    @pytest.mark.usefixtures("memory_dir")
    def test_invalid_tier(self):
        r = core.tool_buy_treasure("something", "platinum")
        assert not r["success"]
        assert "unknown tier" in r["error"]

    @pytest.mark.usefixtures("memory_dir")
    def test_empty_name(self):
        r = core.tool_buy_treasure("", "trinket")
        assert not r["success"]

    @pytest.mark.usefixtures("memory_dir")
    def test_name_truncated_to_60_chars(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 50}, f)
        long_name = "a" * 100
        r = core.tool_buy_treasure(long_name, "trinket")
        assert r["success"]
        # Verify name in treasure is truncated.
        treasures = core._collect_treasures_sorted()
        assert len(treasures[0]["name"]) == 60

    @pytest.mark.usefixtures("memory_dir")
    def test_desc_truncated_to_200_chars(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 50}, f)
        long_desc = "x" * 500
        r = core.tool_buy_treasure("a stone", "trinket", long_desc)
        assert r["success"]
        treasures = core._collect_treasures_sorted()
        assert len(treasures[0]["desc"]) == 200

    @pytest.mark.usefixtures("memory_dir")
    def test_tier_price_matches_tier(self):
        """Each tier charges its fixed price (trinket/gem/relic)."""
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 10000}, f)
        # Only 3 tiers fit per day (purchase cap).
        for tier, price in list(core._TREASURE_TIERS.items())[:3]:
            name = f"a {tier} item test"
            r = core.tool_buy_treasure(name, tier)
            assert r["success"], f"tier={tier} should succeed (remaining: {_gold()})"
            assert r["cost"] == price

    @pytest.mark.usefixtures("memory_dir")
    def test_wonder_tier_price(self):
        """Wonder tier charges 5000."""
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 10000}, f)
        r = core.tool_buy_treasure("a falling star", "wonder")
        assert r["success"]
        assert r["cost"] == 5000

    @pytest.mark.usefixtures("memory_dir")
    def test_id_is_content_hash(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 50}, f)
        r = core.tool_buy_treasure("unique item x", "trinket")
        assert r["success"]
        treasures = core._collect_treasures_sorted()
        tid = treasures[0]["id"]
        assert len(tid) == 16
        # Verify it's valid hex (16 hex chars = 64 bits of SHA-256 prefix).
        int(tid, 16)

    @pytest.mark.usefixtures("memory_dir")
    def test_daily_purchase_cap(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 200}, f)
        # Buy 3 (the cap).
        r1 = core.tool_buy_treasure("trinket one", "trinket")
        r2 = core.tool_buy_treasure("trinket two", "trinket")
        r3 = core.tool_buy_treasure("trinket three", "trinket")
        assert r1["success"] and r2["success"] and r3["success"]
        # 4th should fail.
        r4 = core.tool_buy_treasure("trinket four", "trinket")
        assert not r4["success"]
        assert "cap" in r4["error"].lower()

    @pytest.mark.usefixtures("memory_dir")
    def test_daily_cap_per_host(self):
        """Only this host's shard is counted; other hosts' purchases don't count."""
        # This tests that _count_purchases_today only looks at this host's shard.
        # We verify by directly checking the count.
        core._append_jsonl(core._treasure_ledger_path(), {
            "id": "a" * 16, "name": "existing", "tier": "trinket",
            "cost": 50, "ts": dt.datetime.now().astimezone().strftime("%Y-%m-%d") + "T00:00:00+00:00",
        })
        count = core._count_purchases_today()
        assert count == 1


# --------------------------------------------------------------------------
# tool_list_treasures
# --------------------------------------------------------------------------

class TestListTreasures:
    @pytest.mark.usefixtures("memory_dir")
    def test_empty_list(self):
        r = core.tool_list_treasures()
        assert r["success"]
        assert r["count"] == 0
        assert r["treasures"] == []
        assert r["total_value"] == 0

    @pytest.mark.usefixtures("memory_dir")
    def test_lists_owned(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        core.tool_buy_treasure("a ruby", "gem")
        core.tool_buy_treasure("a key", "trinket")
        r = core.tool_list_treasures()
        assert r["count"] == 2
        assert r["total_value"] == 300
        # Newest first.
        assert r["treasures"][0]["name"] == "a ruby"

    @pytest.mark.usefixtures("memory_dir")
    def test_newest_first(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        # Insert entries manually to ensure distinct timestamps.
        now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        old_iso = (dt.datetime.now().astimezone() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        core._append_jsonl(core._treasure_ledger_path(), {
            "id": "a" * 16, "name": "older", "tier": "trinket",
            "cost": 50, "ts": old_iso,
        })
        core._append_jsonl(core._treasure_ledger_path(), {
            "id": "b" * 16, "name": "newer", "tier": "trinket",
            "cost": 50, "ts": now_iso,
        })
        r = core.tool_list_treasures()
        assert r["treasures"][0]["name"] == "newer"


# --------------------------------------------------------------------------
# tool_gold_history
# --------------------------------------------------------------------------

class TestGoldHistory:
    @pytest.mark.usefixtures("memory_dir")
    def test_basic(self):
        r = core.tool_gold_history()
        assert "total" in r
        assert "events" in r
        assert isinstance(r["events"], list)

    @pytest.mark.usefixtures("memory_dir")
    def test_limit_clamp(self):
        r = core.tool_gold_history(limit=0)
        assert isinstance(r["events"], list)
        r = core.tool_gold_history(limit=100)
        assert len(r["events"]) <= 25

    @pytest.mark.usefixtures("memory_dir")
    def test_events_have_structure(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        core.tool_adjust_gold(10, reason="test reason")
        r = core.tool_gold_history(limit=5)
        ev = r["events"][0]
        assert "delta" in ev
        assert "when" in ev
        assert "reason" in ev
        assert "kind" in ev
        assert ev["kind"] == "adjustment"

    @pytest.mark.usefixtures("memory_dir")
    def test_purchases_in_history(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        core.tool_buy_treasure("a gem", "gem")
        r = core.tool_gold_history(limit=5)
        kinds = [e["kind"] for e in r["events"]]
        assert "purchase" in kinds

    @pytest.mark.usefixtures("memory_dir")
    def test_humanized_when(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        core.tool_adjust_gold(10, reason="test")
        r = core.tool_gold_history(limit=1)
        when = r["events"][0]["when"]
        assert "ago" in when.lower() or "just now" == when


# --------------------------------------------------------------------------
# hoard_mood
# --------------------------------------------------------------------------

class TestHoardMood:
    @pytest.mark.usefixtures("memory_dir")
    def test_content_default(self):
        # No gold history, no treasures, positive total from legacy.
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        key, phrase = core.hoard_mood()
        assert key == "content"
        assert phrase is None

    @pytest.mark.usefixtures("memory_dir")
    def test_indebted(self):
        # Negative total.
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": -10}, f)
        key, phrase = core.hoard_mood()
        assert key == "indebted"
        assert "debt" in phrase

    @pytest.mark.usefixtures("memory_dir")
    def test_stung_recent_fine(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        now = dt.datetime.now().astimezone()
        core._append_jsonl(core._gold_ledger_path(), {
            "delta": -50, "ts": (now - dt.timedelta(hours=2)).isoformat(),
            "reason": "a recent fine",
        })
        key, phrase = core.hoard_mood()
        assert key == "stung"

    @pytest.mark.usefixtures("memory_dir")
    def test_stung_fine_outside_window(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        now = dt.datetime.now().astimezone()
        core._append_jsonl(core._gold_ledger_path(), {
            "delta": -50, "ts": (now - dt.timedelta(hours=8)).isoformat(),
            "reason": "an old fine",
        })
        key, phrase = core.hoard_mood()
        assert key != "stung"

    @pytest.mark.usefixtures("memory_dir")
    def test_delighted_recent_purchase(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        core.tool_buy_treasure("a gem", "gem")
        key, phrase = core.hoard_mood()
        assert key == "delighted"
        assert "gem" in phrase.lower() or "treasure" in phrase.lower()

    @pytest.mark.usefixtures("memory_dir")
    def test_prospering(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 100}, f)
        now = dt.datetime.now().astimezone()
        core._append_jsonl(core._gold_ledger_path(), {
            "delta": 30, "ts": (now - dt.timedelta(hours=1)).isoformat(),
            "reason": "a generous award",
        })
        key, phrase = core.hoard_mood()
        assert key == "prospering"

    @pytest.mark.usefixtures("memory_dir")
    def test_indebted_before_stung(self):
        """indebted has highest precedence."""
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": -10}, f)
        now = dt.datetime.now().astimezone()
        core._append_jsonl(core._gold_ledger_path(), {
            "delta": -50, "ts": (now - dt.timedelta(hours=2)).isoformat(),
            "reason": "a fine",
        })
        key, _ = core.hoard_mood()
        assert key == "indebted"

    @pytest.mark.usefixtures("memory_dir")
    def test_stung_before_delighted(self):
        """Fine precedence over purchase."""
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        now = dt.datetime.now().astimezone()
        core._append_jsonl(core._gold_ledger_path(), {
            "delta": -10, "ts": (now - dt.timedelta(hours=1)).isoformat(),
            "reason": "fine",
        })
        core.tool_buy_treasure("a gem", "gem")
        key, _ = core.hoard_mood()
        assert key == "stung"


# --------------------------------------------------------------------------
# _collect_all_events
# --------------------------------------------------------------------------

class TestCollectAllEvents:
    @pytest.mark.usefixtures("memory_dir")
    def test_merges_gold_and_treasures(self):
        import json
        with open(core._legacy_gold_path(), "w") as f:
            json.dump({"total": 1000}, f)
        core.tool_adjust_gold(10, reason="award")
        core.tool_buy_treasure("a gem", "gem")
        events = core._collect_all_events(limit=10)
        assert len(events) == 2
        types = [e["type"] for e in events]
        assert "adjustment" in types
        assert "purchase" in types

    @pytest.mark.usefixtures("memory_dir")
    def test_limit(self):
        events = core._collect_all_events(limit=3)
        assert len(events) <= 3


# --------------------------------------------------------------------------
# SIDE_EFFECTING_TOOLS includes buy_treasure
# --------------------------------------------------------------------------

class TestGating:
    def test_buy_treasure_in_side_effecting(self):
        assert "buy_treasure" in tools.SIDE_EFFECTING_TOOLS

    def test_list_treasures_not_side_effecting(self):
        assert "list_treasures" not in tools.SIDE_EFFECTING_TOOLS

    def test_gold_history_not_side_effecting(self):
        assert "gold_history" not in tools.SIDE_EFFECTING_TOOLS


# --------------------------------------------------------------------------
# tool registry has all new tools
# --------------------------------------------------------------------------

class TestToolRegistry:
    def _tools(self):
        from tests.fake_platform import FakePlatform
        return tools.build_tools(FakePlatform())

    def test_buy_treasure_registered(self):
        assert "buy_treasure" in self._tools()

    def test_list_treasures_registered(self):
        assert "list_treasures" in self._tools()

    def test_gold_history_registered(self):
        assert "gold_history" in self._tools()
