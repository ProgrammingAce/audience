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

# "Dream": a background pass that consolidates memory (or runs on /dream). After
# dreaming, short-term is trimmed to _SHORT_TERM_AFTER_DREAM lines — the rest has
# been "slept on" and folded into long-term.
#
# Rather than dreaming every N exchanges, the trigger is "idle-plus-dirty": only
# consolidate once at least _DREAM_MIN_DIRTY new exchanges have piled up AND the
# operator has gone quiet for _DREAM_IDLE_SECONDS — mirroring "sleeping on it",
# and keeping the model call out of the operator's active flow. A long session
# that never goes idle still gets folded down at the _DREAM_MAX_DIRTY ceiling so
# memory can't grow unbounded. The watcher re-checks every _DREAM_POLL_SECONDS.
_DREAM_MIN_DIRTY = 8
_DREAM_IDLE_SECONDS = 90.0
_DREAM_MAX_DIRTY = 30
_DREAM_POLL_SECONDS = 15.0
_SHORT_TERM_AFTER_DREAM = 5

# "Reflect": a synthesis pass run right after a successful dream. It derives a few
# higher-level insights from the consolidated facts (e.g. "the operator is a Python
# developer" from several Python-project facts). It only fires once there are at
# least _REFLECT_MIN_FACTS facts to reason over, and adds at most
# _MAX_INSIGHTS_PER_REFLECT new insight entries. Insights are the dragon's own
# fallible deductions (source="reflected"), so their confidence is capped like an
# inferred fact and a later dream is free to prune the weak ones.
_REFLECT_MIN_FACTS = 5
_MAX_INSIGHTS_PER_REFLECT = 3
_INSIGHT_CATEGORY = "insight"

# Pinned ("absolute") facts — the operator's name, the dragon's own name, anything
# the operator explicitly pins — are never decayed, dropped, or rewritten by a
# dream, and are always surfaced in the prompt regardless of the budget below. A
# stated identity fact is pinned automatically; /pin and /unpin toggle it by hand.
_PIN_CATEGORY = "identity"

# Subject — WHOSE fact this is. The dragon learns facts about two distinct people:
# the operator it watches ("operator") and, occasionally, ITSELF ("self") — its
# own name, its own traits. Storing both in one undifferentiated pool is what made
# the dragon answer "what is your name?" with the operator's name: it had no way to
# tell an operator-fact from a self-fact. Every memory now carries a subject, the
# prompt presents the two pools under separate headers, and a fact's id folds the
# subject in so "named Ace" about the operator and "named Ace" about the dragon are
# distinct entries that never collide or dedupe into one. Legacy entries written
# before subjects existed default to the operator on read.
_SUBJECT_OPERATOR = "operator"
_SUBJECT_SELF = "self"
_DEFAULT_SUBJECT = _SUBJECT_OPERATOR
# Aliases a small local model might reach for when it means "about me, the dragon".
_SELF_ALIASES = frozenset({"self", "dragon", "me", "myself", "you", "yourself"})


def _normalize_subject(subject):
    """Coerce a model-supplied subject to 'self' or 'operator' (the default)."""
    s = (subject or "").strip().lower()
    return _SUBJECT_SELF if s in _SELF_ALIASES else _SUBJECT_OPERATOR


def _mem_id(text, subject=_DEFAULT_SUBJECT):
    """Content-hash id for a fact, namespaced by subject.

    An operator fact hashes its bare text, so ids minted before subjects existed
    stay stable; a self fact hashes a subject-tagged key, so the same words about
    the dragon get a distinct id and never collapse into the operator's copy.
    """
    subject = _normalize_subject(subject)
    key = text if subject == _SUBJECT_OPERATOR else f"[{subject}] {text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]

# Recall / prompt ranking. A memory's score blends lexical relevance to the query,
# its confidence, and its recency; a pinned fact gets a large constant boost so it
# always sorts first. _MEMORY_PROMPT_BUDGET caps how many characters of long-term
# memory get inlined into the system prompt (the store can hold _MAX_MEMORIES, far
# more than a small local model wants in context), pinned facts aside.
_RELEVANCE_WEIGHT = 1.0
_CONFIDENCE_WEIGHT = 0.5
_RECENCY_WEIGHT = 0.3
_RECENCY_HALFLIFE_DAYS = 30.0   # a fact's recency score halves every ~month
_PIN_SCORE_BOOST = 1000.0       # dwarfs every other term so pinned sorts first
_MEMORY_PROMPT_BUDGET = 4000    # chars of long-term memory inlined into the prompt

# Tiny stopword set so lexical relevance keys on content words, not glue.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "their them they this to was were will with you your".split())

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
            if not mid or mid in suppressed:
                continue
            if mid in by_id:
                # Same fact on another machine: keep the first copy, but let a pin
                # on any shard win so a /pin written as a local shadow reliably
                # sticks regardless of which shard read first.
                if m.get("pinned"):
                    by_id[mid]["pinned"] = True
                continue
            by_id[mid] = dict(m)
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
        with open(path, "r", newline="") as f:
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
    with open(path, "a", newline="") as f:
        f.write(json.dumps(obj) + "\n")


def _rewrite_jsonl(path, objs):
    _ensure_memory_dir()
    with open(path, "w", newline="") as f:
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
    if source == "reflected":
        # A synthesized insight is the dragon's own deduction, not something the
        # operator stated — hedge it like an inferred fact so it reads as tentative.
        ceiling = 0.7
        c = _clamp_confidence(confidence, 0.6)
        return min(ceiling, c)
    # Unknown provenance: trust the claim but default to the neutral baseline.
    return _clamp_confidence(confidence, _DEFAULT_CONFIDENCE)


def tool_remember(text="", category=None, confidence=None, source=None,
                  subject=None, **_):
    """Append a durable fact to long-term memory.

    Refuses empties and duplicates, trims to _MAX_MEMORY_TEXT, and enforces the
    _MAX_MEMORIES cap so an injected model can't flood the store. `subject` records
    WHOSE fact this is — the operator (default) or the dragon itself ("self") — so
    a fact about the dragon's own name is never confused with the operator's. The
    id folds the subject in, so the same text about each is two distinct entries.
    Confidence is clamped by the call's `source` (see _resolve_confidence), which
    the dispatcher injects — it is not model-controlled.
    """
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "nothing to remember (empty text)"}
    if len(text) > _MAX_MEMORY_TEXT:
        text = text[:_MAX_MEMORY_TEXT]
    subject = _normalize_subject(subject)
    conf = _resolve_confidence(confidence, source)
    try:
        memories = read_long_term()  # union across machines, minus tombstones
        if any(m.get("text") == text
               and _normalize_subject(m.get("subject")) == subject
               for m in memories):
            return {"success": False, "error": "already remembered"}
        # Also refuse near-duplicates: a fact too similar to one already held
        # (same subject) adds no signal and only grows a cluster the dream would
        # later have to collapse. Catch it at the door instead.
        sim, twin = _most_similar(text, subject, memories)
        if sim >= _DUP_SIMILARITY:
            return {"success": False,
                    "error": ("already know something too similar: "
                              f"\"{twin.get('text')}\""),
                    "similar_to": twin.get("id"),
                    "similarity": round(sim, 2)}
        if len(memories) >= _MAX_MEMORIES:
            return {"success": False,
                    "error": "memory is full; forget something first"}
        mem_id = _mem_id(text, subject)
        # If this fact was previously forgotten, lift its tombstone instead of
        # leaving the re-remember masked by the old suppression.
        _remove_tombstones({mem_id})
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        # A fact the operator states about who's who — their name, the dragon's
        # name — is an absolute: pin it so no dream can ever age it out.
        pinned = (source == "stated" and category == _PIN_CATEGORY)
        entry = {  # this machine's own shard
            "id": mem_id,
            "ts": now,
            "first_seen": now,
            "category": (category or None),
            "subject": subject,
            "text": text,
            "confidence": round(conf, 2),
        }
        if pinned:
            entry["pinned"] = True
        _append_jsonl(_long_term_path(), entry)
        return {"success": True, "id": mem_id, "confidence": round(conf, 2),
                "pinned": pinned, "subject": subject}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tokenize(text):
    """Lowercase content-word tokens of `text`, dropping stopwords and glue."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


# Two facts about the SAME subject count as near-duplicates when their
# content-word token sets overlap at least this much (Jaccard). New memories at
# or above this bar are refused, and a dream collapses such clusters to one so
# the store doesn't accrete piles of barely-different facts. This is a lexical
# measure, not semantic: 0.5 catches most rewordings ("writes a lot of Python"
# vs "mostly writes Python code") while leaving clearly distinct facts apart.
# Tune up to block less aggressively, down to merge more. Very terse facts that
# differ only in one content word (e.g. "uses Python" vs "uses Rust") can read
# as similar here — the ≥2-token guard in _text_similarity and subject-scoping
# limit the blast radius, but that's the inherent ceiling of token overlap.
_DUP_SIMILARITY = 0.5


def _text_similarity(a, b):
    """Jaccard overlap of the content-word tokens of two facts (0.0..1.0).

    Needs at least two content words on each side for the overlap to mean
    anything — otherwise tiny facts that merely share a word, or whose only
    distinguishing token (a digit, an initial) was filtered out, would look
    identical. Exact duplicates are caught separately by the callers.
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if len(ta) < 2 or len(tb) < 2:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return inter / len(ta | tb)


def _most_similar(text, subject, memories):
    """Most similar same-subject memory to `text`, as (similarity, memory).

    Returns (0.0, None) when nothing is close. Subject-scoped so a fact about the
    dragon itself never blocks or merges into one about the operator.
    """
    best_sim, best = 0.0, None
    for m in memories:
        if _normalize_subject(m.get("subject")) != subject:
            continue
        sim = _text_similarity(text, m.get("text"))
        if sim > best_sim:
            best_sim, best = sim, m
    return best_sim, best


def _age_days(entry, now):
    """Whole days since the fact was first learned (or last stamped). 0 on error."""
    stamp = entry.get("first_seen") or entry.get("ts")
    if not stamp:
        return 0.0
    try:
        when = dt.datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return 0.0
    if when.tzinfo is None:
        when = when.astimezone()
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _score_memory(entry, query_tokens, now):
    """Rank a memory for retrieval: relevance + confidence + recency, pinned first.

    `query_tokens` is the tokenized query (empty set ⇒ no relevance term, so the
    ranking falls back to confidence and recency — used by the query-less prompt
    path). Recency decays on a half-life so an old fact fades but never goes
    negative; a pinned absolute gets a boost that dwarfs every other term.
    """
    score = 0.0
    if query_tokens:
        fact_tokens = _tokenize(
            (entry.get("text") or "") + " " + (entry.get("category") or ""))
        if fact_tokens:
            overlap = len(query_tokens & fact_tokens) / len(query_tokens)
            score += _RELEVANCE_WEIGHT * overlap
    score += _CONFIDENCE_WEIGHT * _clamp_confidence(entry.get("confidence"),
                                                    _DEFAULT_CONFIDENCE)
    recency = 0.5 ** (_age_days(entry, now) / _RECENCY_HALFLIFE_DAYS)
    score += _RECENCY_WEIGHT * recency
    if entry.get("pinned"):
        score += _PIN_SCORE_BOOST
    return score


def rank_memories(memories, query="", limit=None):
    """Memories sorted best-first by _score_memory; optionally capped to `limit`."""
    now = dt.datetime.now().astimezone()
    query_tokens = _tokenize(query)
    ranked = sorted(memories,
                    key=lambda m: _score_memory(m, query_tokens, now),
                    reverse=True)
    return ranked[:limit] if limit is not None else ranked


def tool_recall(query="", subject=None, **_):
    """Return long-term memories matching `query`, best-first by relevance.

    Matches on a substring of the text/category as before, then ranks the hits by
    _score_memory (relevance + confidence + recency, pinned first) rather than the
    order they happen to sit in the file. An empty query returns the whole store
    ranked by confidence and recency. Pass `subject` ('operator' or 'self') to
    limit the search to facts about that one — e.g. recall your own name with
    subject='self' rather than fishing the operator's name out by mistake.
    """
    query = (query or "").strip().lower()
    want_subject = _normalize_subject(subject) if subject else None
    try:
        memories = read_long_term()
    except Exception as e:
        return {"success": False, "error": str(e)}
    if want_subject is not None:
        memories = [m for m in memories
                    if _normalize_subject(m.get("subject")) == want_subject]
    if not query:
        matches = memories
    else:
        # Match on token overlap, not whole-query substring: the model asks in
        # natural language ("what is my child's name"), which never appears
        # verbatim in a fact, so a substring filter drops every candidate before
        # the ranker can score it. A fact matches if it shares any content word
        # with the query, OR contains the raw query as a substring (keeps exact
        # phrase lookups working); _score_memory then ranks by overlap.
        query_tokens = _tokenize(query)
        matches = [m for m in memories
                   if (query_tokens & _tokenize(
                           (m.get("text") or "") + " " + (m.get("category") or "")))
                   or query in (m.get("text") or "").lower()
                   or query in (m.get("category") or "").lower()]
    matches = rank_memories(matches, query=query, limit=_RECALL_LIMIT)
    return {
        "success": True,
        "matches": [{"id": m.get("id"), "category": m.get("category"),
                     "subject": _normalize_subject(m.get("subject")),
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


def set_pinned(mem_id, pinned):
    """Pin or unpin a long-term fact by id; pin state lives in this machine's shard.

    A pinned ("absolute") fact — the operator's name, the dragon's name — is never
    decayed or dropped by a dream and always makes it into the prompt. If the fact
    sits only in a peer's shard (which we must not rewrite), pinning writes a pinned
    shadow copy to this machine's shard; read_long_term ORs the pin across shards.
    """
    mem_id = (mem_id or "").strip()
    if not mem_id:
        return {"success": False, "error": "no id given"}
    target = next((m for m in read_long_term() if m.get("id") == mem_id), None)
    if target is None:
        return {"success": False, "error": "no memory with that id"}
    local = _read_jsonl(_long_term_path())
    found = False
    for e in local:
        if e.get("id") == mem_id:
            found = True
            if pinned:
                e["pinned"] = True
            else:
                e.pop("pinned", None)
    if found:
        _rewrite_jsonl(_long_term_path(), local)
    elif pinned:
        # Fact lives only in a peer shard: write a pinned shadow locally so the
        # union sees it pinned without our touching the peer's file.
        shadow = dict(target)
        shadow["pinned"] = True
        _append_jsonl(_long_term_path(), shadow)
    return {"success": True, "id": mem_id, "pinned": bool(pinned)}


def edit_memory(mem_id, new_text):
    """Replace a long-term fact's text, preserving its category/confidence/pin.

    The id is a content hash, so editing the text mints a new id: we write a fresh
    entry (carrying the original metadata) to this machine's shard and tombstone the
    old id so the prior text is suppressed across every shard — the same edit-as-
    replace dance set_pinned/forget use for the sharded store. A no-op edit (text
    unchanged) succeeds without touching disk; editing onto text that already exists
    as another live memory is refused so two ids never share content.
    """
    mem_id = (mem_id or "").strip()
    new_text = (new_text or "").strip()
    if not mem_id:
        return {"success": False, "error": "no id given"}
    if not new_text:
        return {"success": False, "error": "nothing to save (empty text)"}
    if len(new_text) > _MAX_MEMORY_TEXT:
        new_text = new_text[:_MAX_MEMORY_TEXT]
    try:
        memories = read_long_term()
        target = next((m for m in memories if m.get("id") == mem_id), None)
        if target is None:
            return {"success": False, "error": "no memory with that id"}
        # The edited fact stays about whoever the original was about, so its new
        # id is namespaced by the same subject the dedup/lookup keys on.
        subject = _normalize_subject(target.get("subject"))
        new_id = _mem_id(new_text, subject)
        if new_id == mem_id:
            return {"success": True, "id": mem_id}  # unchanged; nothing to do
        if any(m.get("id") == new_id for m in memories):
            return {"success": False, "error": "already remembered"}
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        entry = dict(target)
        entry["id"] = new_id
        entry["text"] = new_text
        entry["ts"] = now
        entry["first_seen"] = target.get("first_seen") or target.get("ts") or now
        _remove_tombstones({new_id})
        _append_jsonl(_long_term_path(), entry)
        _add_tombstones({mem_id})
        return {"success": True, "id": new_id, "previous_id": mem_id}
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
        with open(_legacy_gold_path(), "r", newline="") as f:
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

    previous = read_long_term()  # union of every machine's live memories
    prev_by_id = {m.get("id"): m for m in previous if m.get("id")}
    # Fallback for recovering a dropped subject: map a fact's exact text back to the
    # subject it was filed under before the dream.
    prev_subject_by_text = {m.get("text"): _normalize_subject(m.get("subject"))
                            for m in previous}
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

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
        # A fact keeps WHOSE it is across the dream: the model is told to echo the
        # subject. If it dropped it, recover the subject from a prior fact with the
        # same text, so a self fact carried through verbatim isn't re-filed under the
        # operator. Distinct subjects make distinct ids — a self fact never merges
        # into the operator's pool.
        if m.get("subject") is not None:
            subject = _normalize_subject(m.get("subject"))
        else:
            subject = prev_subject_by_text.get(text, _DEFAULT_SUBJECT)
        if (text, subject) in seen:
            continue  # collapse any duplicates the dream left behind
        seen.add((text, subject))
        mem_id = _mem_id(text, subject)
        prior = prev_by_id.get(mem_id)
        # A fact carried through the dream unchanged (same content hash) keeps its
        # original age — only genuinely new or merged text gets a fresh stamp, so
        # consolidation no longer resets the clock the staleness check reads.
        ts = (prior or {}).get("ts") or now
        first_seen = (prior or {}).get("first_seen") or ts
        entry = {
            "id": mem_id,
            "ts": ts,
            "first_seen": first_seen,
            "category": (m.get("category") or None),
            "subject": subject,
            "text": text,
            "confidence": round(_clamp_confidence(m.get("confidence"),
                                                  _DEFAULT_CONFIDENCE), 2),
        }
        # A pin survives the dream no matter what the model said: if it was pinned
        # before, it stays pinned (and keeps its full prior confidence).
        if prior and prior.get("pinned"):
            entry["pinned"] = True
            entry["confidence"] = round(_clamp_confidence(
                prior.get("confidence"), _DEFAULT_CONFIDENCE), 2)
        # Collapse near-duplicates the model left behind: if this fact is too
        # similar to one already kept for the same subject, keep only the
        # stronger of the two rather than letting the cluster survive the dream.
        # Preference: a pin always wins; otherwise higher confidence, then the
        # longer (more specific) text.
        twin_idx = next(
            (i for i, k in enumerate(refined)
             if k["subject"] == subject
             and _text_similarity(text, k["text"]) >= _DUP_SIMILARITY),
            None)
        if twin_idx is not None:
            kept = refined[twin_idx]
            if kept.get("pinned"):
                continue
            if (bool(entry.get("pinned")),
                    entry["confidence"], len(text)) > (
                    bool(kept.get("pinned")),
                    kept["confidence"], len(kept["text"])):
                refined[twin_idx] = entry
            continue
        refined.append(entry)
        if len(refined) >= _MAX_MEMORIES:
            break

    # Safety net for absolutes: re-inject any pinned fact the dream dropped (the
    # model may ignore the instruction). Prepend so a full store can't crowd them
    # out, then re-cap. After this, no pinned id can be lost or tombstoned. Keyed by
    # id (subject-aware), so a pinned self fact survives even if an operator fact
    # happens to share its text.
    refined_ids_so_far = {e["id"] for e in refined}
    dropped_pins = [m for m in previous
                    if m.get("pinned") and m.get("id") not in refined_ids_so_far]
    if dropped_pins:
        # A re-injected pin also wins any near-dup collapse: drop a kept non-pin
        # that merely rewords a pin we're restoring, so the pin doesn't return
        # alongside a twin (the collapse loop above couldn't see these pins yet).
        def _rewords_a_pin(e):
            if e.get("pinned"):
                return False
            return any(
                _normalize_subject(p.get("subject")) == e["subject"]
                and _text_similarity(e["text"], p.get("text")) >= _DUP_SIMILARITY
                for p in dropped_pins)
        refined = [e for e in refined if not _rewords_a_pin(e)]
        refined = dropped_pins + refined
        refined = refined[:_MAX_MEMORIES]

    try:
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


def add_insights(insights):
    """Persist a few synthesized insights as long-term facts; return the count added.

    Each insight is the dragon's own deduction (category 'insight', confidence
    capped like an inferred fact via the 'reflected' source). Deduped against the
    live store by content hash so repeated reflection doesn't pile up, capped to
    _MAX_INSIGHTS_PER_REFLECT per pass and to _MAX_MEMORIES overall. Insights are
    never pinned, so a later dream is free to prune the weak ones.
    """
    if not insights:
        return 0
    try:
        existing = read_long_term()
    except Exception:
        return 0
    existing_ids = {m.get("id") for m in existing}
    count = len(existing)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for ins in insights:
        if added >= _MAX_INSIGHTS_PER_REFLECT or count >= _MAX_MEMORIES:
            break
        if not isinstance(ins, dict):
            continue
        text = (ins.get("text") or "").strip()
        if not text:
            continue
        if len(text) > _MAX_MEMORY_TEXT:
            text = text[:_MAX_MEMORY_TEXT]
        # Reflection generalizes about the operator, so insights are operator facts.
        mem_id = _mem_id(text, _SUBJECT_OPERATOR)
        if mem_id in existing_ids:
            continue  # already known — don't re-add on the next reflection
        conf = _resolve_confidence(ins.get("confidence"), "reflected")
        try:
            _remove_tombstones({mem_id})
            _append_jsonl(_long_term_path(), {
                "id": mem_id,
                "ts": now,
                "first_seen": now,
                "category": _INSIGHT_CATEGORY,
                "subject": _SUBJECT_OPERATOR,
                "text": text,
                "confidence": round(conf, 2),
            })
        except Exception:
            continue
        existing_ids.add(mem_id)
        count += 1
        added += 1
    return added
