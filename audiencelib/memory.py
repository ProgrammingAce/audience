"""Persistent short-term / long-term memory, the gold hoard, and the
dream consolidation pass. Pure stdlib; no curses or OS-specific code.
"""

import datetime as dt
import glob
import hashlib
import json
import os
import re
import socket

# Directory the script was launched from; the default memory dir sits beside it.
_WORKDIR = os.getcwd()

# --------------------------------------------------------------------------
# Memory — persistent short-term and long-term recall
#
# The dragon is otherwise stateless across sessions: every model call is built
# fresh. These two line-delimited JSON files give it continuity.
#
#   long_term.jsonl  — durable, distilled facts the model chooses to keep via the
#                      remember tool: who the operator is, what they're building,
#                      stated preferences. {id, ts, category, text} per line.
#   short_term.jsonl — a rolling, auto-captured tail of recent dragon/operator
#                      exchanges. {ts, label, text} per line, trimmed to the last
#                      _SHORT_TERM_KEEP lines.
#
# Like the file tools, the model is fully prompt-injectable, so memory is
# confined to one dedicated directory: the tools never take a caller-supplied
# path, only ever touching these fixed files, and entries are capped in
# length and count so an injected model can't bloat the store or the context.
#
# Sharing across machines (e.g. a Dropbox-synced --memory-dir): each install
# writes only to files suffixed with its own hostname — long_term.<host>.jsonl,
# short_term.<host>.jsonl, tombstones.<host>.jsonl, gold.<host>.jsonl — so two
# machines running at once never write the same file and Dropbox never has to
# mint a "conflicted copy". Reads union every shard (this machine's plus every
# peer's, plus any legacy un-suffixed file) keyed by entry id, which dedupes for
# free since ids are content hashes. Removal can't rewrite a peer's file, so
# forget and dream record tombstones (suppressed ids) rather than deleting:
#   read set = union(all long_term shards) minus union(all tombstone shards).
# Gold is an append-only per-machine delta ledger summed across shards, so two
# concurrent adjustments both survive. The store is eventually-consistent: a
# peer's writes appear once Dropbox syncs them in.
# --------------------------------------------------------------------------

# Default beside the working directory; overridable via --memory-dir so the same
# brain can follow the operator across projects (e.g. ~/.audience/memory).
_MEMORY_DIR = os.path.join(_WORKDIR, ".audience_memory")

_MAX_MEMORY_TEXT = 500   # chars per long-term entry
_MAX_MEMORIES = 200      # total long-term entries before writes are refused
_SHORT_TERM_KEEP = 40    # short-term lines retained
_RECALL_LIMIT = 10       # max matches returned by recall

# Confidence: how trustworthy a long-term fact is, in [0.0, 1.0]. Facts the
# operator states directly (the Q&A path) are trusted; facts the dragon infers
# from a screenshot (the commentary path) are hedged. Legacy entries written
# before confidence existed default to _DEFAULT_CONFIDENCE on read.
_DEFAULT_CONFIDENCE = 0.6
_LOW_CONFIDENCE = 0.5    # at/under this, a fact is flagged tentative in the prompt
_MIN_PROMPT_CONFIDENCE = 0.3  # below this, a fact is omitted from the prompt entirely

# "Dream": a background pass that consolidates memory every _DREAM_EVERY exchanges
# (or on /dream). After dreaming, short-term is trimmed to _SHORT_TERM_AFTER_DREAM
# lines — the rest has been "slept on" and folded into long-term.
_DREAM_EVERY = 10
_SHORT_TERM_AFTER_DREAM = 5

# Gold hoard: a reward/punishment ledger the operator drives ("take 10 gold",
# "I'm subtracting 100 gold"). The arithmetic lives in code, never the model: the
# tool reads the stored int, applies the operator's signed amount, and writes it
# back. The running total is mirrored into long-term memory at full confidence so
# the hoard is read fresh from gold.json each turn and injected into the prompt;
# it is NEVER written into long-term memory, so memory tools and the dream pass
# can't touch it.
_MAX_GOLD_DELTA = 1_000_000     # clamp a single adjustment to a sane range
_GOLD_CATEGORY = "gold"         # legacy marker: filtered out of memory on read

# Provenance of a model call, threaded explicitly from Audience._do() through
# ask_model to the remember tool so it can clamp confidence by source rather
# than trusting the model's self-reported number. "stated" = operator told us
# (Q&A); "inferred" = deduced from a screenshot (commentary); None = unknown.


def _clamp_confidence(value, default):
    try:
        c = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, c))


def set_memory_dir(path):
    """Point the memory store at a different directory (from --memory-dir)."""
    global _MEMORY_DIR
    _MEMORY_DIR = os.path.realpath(os.path.expanduser(path))


def _machine_id():
    """A filesystem-safe, stable per-machine tag taken from the hostname.

    Lowercased and reduced to [a-z0-9_-]; the first label of a dotted name is
    enough (drop any domain suffix). Falls back to 'unknown' so a host with an
    unusable name still gets a private, non-colliding shard.
    """
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    host = (host or "").split(".")[0].strip().lower()
    host = re.sub(r"[^a-z0-9_-]", "-", host).strip("-")
    return host or "unknown"


# A shard filename is "<base>.<machine>.<ext>"; the backup adds a ".bak" infix.
# Reads glob "<base>*.<ext>" so this machine's shard, every peer's shard, and any
# legacy un-suffixed "<base>.<ext>" all come in together — minus backups.
def _shard_path(base, ext="jsonl"):
    """This machine's own shard to write to."""
    return os.path.join(_MEMORY_DIR, f"{base}.{_machine_id()}.{ext}")


def _shard_glob(base, ext="jsonl"):
    """Every readable shard for `base`, excluding backup files."""
    paths = glob.glob(os.path.join(_MEMORY_DIR, f"{base}*.{ext}"))
    return sorted(p for p in paths if ".bak." not in os.path.basename(p))


def _long_term_path():
    return _shard_path("long_term")


def _short_term_path():
    return _shard_path("short_term")


def _tombstone_path():
    return _shard_path("tombstones")


def _read_tombstones():
    """Union of suppressed ids across every tombstone shard."""
    suppressed = set()
    for path in _shard_glob("tombstones"):
        for obj in _read_jsonl(path):
            tid = obj.get("id")
            if tid:
                suppressed.add(tid)
    return suppressed


def _add_tombstones(ids):
    """Append suppressed ids to this machine's tombstone shard."""
    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for tid in ids:
        _append_jsonl(_tombstone_path(), {"id": tid, "ts": ts})


def _remove_tombstones(ids):
    """Lift suppression for `ids` from this machine's own tombstone shard.

    Only this machine's shard can be rewritten safely; a tombstone a peer wrote
    is lifted on that peer's side once its own re-remember syncs in.
    """
    path = _tombstone_path()
    existing = _read_jsonl(path)
    kept = [t for t in existing if t.get("id") not in ids]
    if len(kept) != len(existing):
        _rewrite_jsonl(path, kept)


def read_long_term():
    """All live long-term memories: union of every shard, minus tombstones.

    Deduplicated by id (content hash), so the same fact written on two machines
    collapses to one entry. Ordered oldest-first by timestamp for stability.
    """
    suppressed = _read_tombstones()
    by_id = {}
    for path in _shard_glob("long_term"):
        for m in _read_jsonl(path):
            mid = m.get("id")
            if not mid or mid in suppressed or mid in by_id:
                continue
            by_id[mid] = m
    return sorted(by_id.values(), key=lambda m: m.get("ts") or "")


def read_short_term():
    """Recent exchanges across every shard, ordered oldest-first by timestamp."""
    entries = []
    for path in _shard_glob("short_term"):
        entries.extend(_read_jsonl(path))
    return sorted(entries, key=lambda e: e.get("ts") or "")


def _ensure_memory_dir():
    os.makedirs(_MEMORY_DIR, exist_ok=True)


def _read_jsonl(path):
    """Read a list of objects from a .jsonl file; skip corrupt/blank lines."""
    out = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue  # tolerate a hand-edited or truncated line
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return out


def _append_jsonl(path, obj):
    _ensure_memory_dir()
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def _rewrite_jsonl(path, objs):
    _ensure_memory_dir()
    with open(path, "w") as f:
        for obj in objs:
            f.write(json.dumps(obj) + "\n")


def _resolve_confidence(confidence, source):
    """Decide a fact's confidence from the model's claim and the call's source.

    The clamp — not the model's number — is the guarantee: an operator-stated
    fact is floored high, and a screen-inferred one is capped, so an injected
    model can't mint a high-confidence memory off a screenshot.
    """
    if source == "stated":
        floor = 0.9
        c = _clamp_confidence(confidence, 1.0)
        return max(floor, c)
    if source == "inferred":
        ceiling = 0.7
        c = _clamp_confidence(confidence, 0.5)
        return min(ceiling, c)
    # Unknown provenance: trust the claim but default to the neutral baseline.
    return _clamp_confidence(confidence, _DEFAULT_CONFIDENCE)


def tool_remember(text="", category=None, confidence=None, source=None, **_):
    """Append a durable fact to long-term memory.

    Refuses empties and duplicates, trims to _MAX_MEMORY_TEXT, and enforces the
    _MAX_MEMORIES cap so an injected model can't flood the store. The id is a
    short hash of the text, used later by forget. Confidence is clamped by the
    call's `source` (see _resolve_confidence), which the dispatcher injects — it
    is not model-controlled.
    """
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "nothing to remember (empty text)"}
    if len(text) > _MAX_MEMORY_TEXT:
        text = text[:_MAX_MEMORY_TEXT]
    conf = _resolve_confidence(confidence, source)
    try:
        memories = read_long_term()  # union across machines, minus tombstones
        if any(m.get("text") == text for m in memories):
            return {"success": False, "error": "already remembered"}
        if len(memories) >= _MAX_MEMORIES:
            return {"success": False,
                    "error": "memory is full; forget something first"}
        mem_id = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        # If this fact was previously forgotten, lift its tombstone instead of
        # leaving the re-remember masked by the old suppression.
        _remove_tombstones({mem_id})
        _append_jsonl(_long_term_path(), {  # this machine's own shard
            "id": mem_id,
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "category": (category or None),
            "text": text,
            "confidence": round(conf, 2),
        })
        return {"success": True, "id": mem_id, "confidence": round(conf, 2)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_recall(query="", **_):
    """Return long-term memories whose text or category matches query (substring)."""
    query = (query or "").strip().lower()
    try:
        memories = read_long_term()
    except Exception as e:
        return {"success": False, "error": str(e)}
    if not query:
        matches = memories
    else:
        matches = [m for m in memories
                   if query in (m.get("text") or "").lower()
                   or query in (m.get("category") or "").lower()]
    matches = matches[:_RECALL_LIMIT]
    return {
        "success": True,
        "matches": [{"id": m.get("id"), "category": m.get("category"),
                     "text": m.get("text"),
                     "confidence": _clamp_confidence(m.get("confidence"),
                                                     _DEFAULT_CONFIDENCE)}
                    for m in matches],
        "count": len(matches),
    }


def tool_forget(id="", **_):
    """Drop the long-term memory with the given id, by id only (no bulk wipe).

    The entry may live in any machine's shard, which we must not rewrite, so
    forgetting records a tombstone that suppresses the id everywhere on read.
    """
    mem_id = (id or "").strip()
    if not mem_id:
        return {"success": False, "error": "no id given"}
    try:
        if not any(m.get("id") == mem_id for m in read_long_term()):
            return {"success": False, "error": "no memory with that id"}
        _add_tombstones({mem_id})
        return {"success": True, "id": mem_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _legacy_gold_path():
    return os.path.join(_MEMORY_DIR, "gold.json")


def _gold_ledger_path():
    return _shard_path("gold")


def _read_gold():
    """Current hoard total as an int: the legacy base plus every shard's deltas.

    The hoard is an append-only ledger of signed deltas, one shard per machine,
    so two machines awarding/docking gold at once both persist and the sum is
    correct. A legacy single-value gold.json (from before sharding) is folded in
    as a starting base.
    """
    total = 0
    try:
        with open(_legacy_gold_path(), "r") as f:
            total += int(json.load(f).get("total", 0))
    except (FileNotFoundError, ValueError, TypeError, OSError):
        pass
    for path in _shard_glob("gold"):
        for entry in _read_jsonl(path):
            try:
                total += int(entry.get("delta", 0))
            except (TypeError, ValueError):
                continue
    return total


def tool_adjust_gold(amount=0, reason="", **_):
    """Add (reward) or subtract (punishment) gold from the hoard. Deterministic."""
    try:
        delta = int(amount)
    except (TypeError, ValueError):
        return {"success": False, "error": "amount must be a whole number"}
    if delta == 0:
        return {"success": False, "error": "amount must be non-zero"}
    delta = max(-_MAX_GOLD_DELTA, min(_MAX_GOLD_DELTA, delta))
    before = _read_gold()
    try:
        # The only arithmetic is the sum in _read_gold — never the LLM. We just
        # append this machine's signed delta to its own ledger shard.
        _append_jsonl(_gold_ledger_path(), {
            "delta": delta,
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": (reason or "").strip() or None,
        })
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "change": delta, "previous": before,
            "total": before + delta, "reason": (reason or "").strip() or None}


def tool_gold_total(**_):
    """Report the current gold hoard total."""
    return {"success": True, "total": _read_gold()}


def record_short_term(label, text):
    """Append one exchange line to short-term memory, trimming to the cap."""
    text = (text or "").strip()
    if not text:
        return
    try:
        # Append to and trim only this machine's own shard — never a peer's.
        _append_jsonl(_short_term_path(), {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": label,
            "text": text,
        })
        entries = _read_jsonl(_short_term_path())
        if len(entries) > _SHORT_TERM_KEEP:
            _rewrite_jsonl(_short_term_path(), entries[-_SHORT_TERM_KEEP:])
    except Exception:
        pass  # memory is best-effort; never break commentary over a write error


def _parse_dream(raw):
    """Pull the {"memories": [...]} object out of the model's dream response.

    Tolerates code fences and surrounding prose by extracting the outermost
    JSON object. Returns the memories list, or None if nothing valid is found —
    the caller leaves memory untouched on None, so a bad dream never deletes.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        # strip a ```json … ``` fence if the model added one
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    mems = obj.get("memories")
    if not isinstance(mems, list):
        return None
    return mems


def apply_dream(raw):
    """Validate a dream response and rewrite long-term memory from it.

    On any parse/validation failure returns (False, reason) and leaves memory
    untouched. On success backs up the prior store to long_term.bak.jsonl, writes
    the consolidated set, trims short-term, and returns (True, new_count).
    """
    mems = _parse_dream(raw)
    if mems is None:
        return False, "unparseable dream"

    refined = []
    seen = set()
    for m in mems:
        if not isinstance(m, dict):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _MAX_MEMORY_TEXT:
            text = text[:_MAX_MEMORY_TEXT]
        if text in seen:
            continue  # collapse any duplicates the dream left behind
        seen.add(text)
        cat = m.get("category")
        refined.append({
            "id": hashlib.sha1(text.encode("utf-8")).hexdigest()[:8],
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "category": (cat or None),
            "text": text,
            "confidence": round(_clamp_confidence(m.get("confidence"),
                                                  _DEFAULT_CONFIDENCE), 2),
        })
        if len(refined) >= _MAX_MEMORIES:
            break

    try:
        previous = read_long_term()  # union of every machine's live memories
        refined_ids = {m["id"] for m in refined}
        # Back up the pre-dream store so a regrettable consolidation is
        # recoverable, in this machine's own backup shard.
        _rewrite_jsonl(
            os.path.join(_MEMORY_DIR, f"long_term.{_machine_id()}.bak.jsonl"),
            previous)
        # The consolidated set lives in this machine's shard; peers' shards must
        # not be rewritten, so suppress every prior id that the dream didn't keep
        # verbatim. Anything carried forward unchanged (same content hash) is left
        # untombstoned so it survives. Lift suppression on the kept ids too, in
        # case a peer had previously forgotten one.
        _rewrite_jsonl(_long_term_path(), refined)
        _remove_tombstones(refined_ids)
        _add_tombstones({m.get("id") for m in previous
                         if m.get("id") and m.get("id") not in refined_ids})
        # The recent exchanges have been slept on; keep only a short tail of this
        # machine's own short-term shard for immediate continuity.
        short = _read_jsonl(_short_term_path())
        if len(short) > _SHORT_TERM_AFTER_DREAM:
            _rewrite_jsonl(_short_term_path(), short[-_SHORT_TERM_AFTER_DREAM:])
    except Exception as e:
        return False, str(e)
    return True, len(refined)
