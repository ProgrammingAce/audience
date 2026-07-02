"""Persistent short-term / long-term memory, the gold hoard, treasure shop, and
the dream consolidation pass. Pure stdlib; no curses or OS-specific code.
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
_MAX_EPISODES = 30       # live episode store cap per machine
_EPISODE_MAX_CHARS = 200 # max chars per episode text

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
# Hard ceiling on how many insight entries may live in the store at once. Insights
# are reworded near-duplicates of one another by nature (see the dedup problem they
# caused), so the category is capped tightly even though _MAX_MEMORIES is large.
_MAX_INSIGHTS_TOTAL = 3
_INSIGHT_CATEGORY = "insight"
# Confidence ceiling for the dragon's own deductions (source="reflected" insights).
# Re-applied through dreams so consolidation can't inflate a reworded-dup cluster
# into stated certainty that outranks concrete facts.
_REFLECTED_CEILING = 0.6
# Two insights for the same subject are near-interchangeable by design, so the
# dream's collapse loop merges them at a looser bar than the general _DUP_SIMILARITY.
_INSIGHT_DUP_SIMILARITY = 0.35

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
_IMPORTANCE_WEIGHT = 0.4        # importance folds into ranking below relevance
_MEMORY_PROMPT_BUDGET_TOKENS = 1000  # est. tokens of long-term memory inlined into prompt

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
_MAX_GOLD_DELTA = 1000          # clamp a single adjustment to a sane range
_GOLD_CATEGORY = "gold"         # legacy marker: filtered out of memory on read

# Treasure shop pricing — code owns prices, the dragon owns imagination.
_TREASURE_TIERS = {
    "trinket": 50,
    "gem":     250,
    "relic":   1000,
    "wonder":  5000,
}
_MAX_PURCHASES_PER_DAY = 3
_HOARD_MOOD_FINE_HOURS = 6
_HOARD_MOOD_PURCHASE_HOURS = 24
_HOARD_MOOD_PROSPERING_THRESHOLD = 25
_HOARD_MOOD_MAX_TREASURE_NAMES = 5
_HOARD_MOOD_BLOCK_MAX_CHARS = 300

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


def _clamp_importance(value, default=5):
    """Clamp importance to [1, 10]; return default when absent or invalid."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(10, v))


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


# Tombstone TTL: dead tombstones older than this are safe to drop (in days).
# A fresh tombstone must survive long enough to reach and suppress a peer's
# copy, so the TTL is generous.
_TOMBSTONE_TTL_DAYS = 30


def _compact_tombstones(previous, kept_ids, all_live_entries=None):
    """Compact this machine's own tombstone shard after a successful dream.

    Drops any tombstone whose id appears in no live shard AND is older than
    _TOMBSTONE_TTL_DAYS days. Keeps dead-but-recent and live-referencing ones.
    """
    path = _tombstone_path()
    try:
        entries = _read_jsonl(path)
    except Exception:
        return
    if not entries:
        return
    now = dt.datetime.now().astimezone()
    cutoff = now - dt.timedelta(days=_TOMBSTONE_TTL_DAYS)
    cutoff_str = cutoff.isoformat(timespec="seconds")
    # All ids that are still alive across every shard.
    if all_live_entries:
        live_ids = {e["id"] for e in all_live_entries if isinstance(e, dict) and e.get("id")}
    else:
        live_ids = set()
    # Also check previous for any id the dream kept verbatim (still alive).
    for m in previous:
        mid = m.get("id")
        if mid:
            live_ids.add(mid)
    kept = []
    for t in entries:
        tid = t.get("id")
        if tid in live_ids:
            kept.append(t)
            continue
        ts = t.get("ts", "")
        # Keep tombstones that are recent (peer might not have synced yet).
        if ts > cutoff_str:
            kept.append(t)
            continue
        # Dead + old: safe to drop.
    if len(kept) != len(entries):
        _rewrite_jsonl(path, kept)


def read_long_term():
    """All live long-term memories: union of every shard, minus tombstones.

    Deduplicated by id (content hash), so the same fact written on two machines
    collapses to one entry. Ordered oldest-first by timestamp for stability.
    When the same id appears in multiple shards the first copy wins, but pin
    state and last_confirmed are OR'd/max'd across copies so a local shadow
    reliably surfaces. After id-union, same-subject near-dups are collapsed at
    read-time (preferred: pinned > confidence > longer text) to handle
    cross-machine dream divergence.
    """
    suppressed = _read_tombstones()
    by_id = {}
    for path in _shard_glob("long_term"):
        for m in _read_jsonl(path):
            mid = m.get("id")
            if not mid or mid in suppressed:
                continue
            if mid in by_id:
                existing = by_id[mid]
                # Let a pin on any shard win.
                if m.get("pinned"):
                    existing["pinned"] = True
                # Keep the freshest last_confirmed across copies.
                incoming_lc = m.get("last_confirmed")
                existing_lc = existing.get("last_confirmed")
                if incoming_lc and (not existing_lc or incoming_lc > existing_lc):
                    existing["last_confirmed"] = incoming_lc
                continue
            by_id[mid] = dict(m)
    entries = list(by_id.values())

    # Read-time collapse of same-subject near-dups (cross-machine dream
    # divergence). Prefer pinned > confidence > longer text, matching
    # apply_dream's preference order.
    by_subject = {}
    for e in entries:
        subj = e.get("subject", _SUBJECT_OPERATOR)
        by_subject.setdefault(subj, []).append(e)
    collapsed = []
    for subj, items in by_subject.items():
        if len(items) <= 1:
            collapsed.extend(items)
            continue
        kept = []
        for item in items:
            winner = None
            for i, k in enumerate(kept):
                if _text_similarity(item.get("text", ""),
                                    k.get("text", "")) >= _DUP_SIMILARITY:
                    # Pinned wins; otherwise higher confidence wins; then longer.
                    if (bool(item.get("pinned")),
                            item.get("confidence", 0),
                            len(item.get("text", ""))) > \
                            (bool(k.get("pinned")),
                             k.get("confidence", 0),
                             len(k.get("text", ""))):
                        kept[i] = item
                    break  # no twin for this new item
            else:
                kept.append(item)
        collapsed.extend(kept)
    return sorted(collapsed, key=lambda m: m.get("ts") or "")


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
        c = _clamp_confidence(confidence, 0.6)
        return min(_REFLECTED_CEILING, c)
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
    the dispatcher injects — it is not model-controlled. `source` is also stored
    so later passes can distinguish operator-stated facts from screen-inferred ones.

    When a near-duplicate exists and source is "stated", the old entry is
    tombstoned and the new text supersedes it (carrying over first_seen and
    pinned state). This lets the operator correct facts without fighting the
    similarity gate. For inferred near-dups the refusal carries a
    ``corroborated`` id so the caller can incrementally reinforce the twin.
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
        # List-rejection guard: screen-inferred facts with 4+ comma-separated
        # items are laundry lists of consumed content, not single claims.
        if source == "inferred" and _is_laundry_list(text):
            return {"success": False,
                    "error": ("too many items — save the single strongest claim, "
                              "not a list")
                    }
        # Also handle near-duplicates: supersede on stated correction,
        # refuse with corroboration hint on inferred re-evidence.
        sim, twin = _most_similar(text, subject, memories)
        if sim >= _DUP_SIMILARITY:
            twin_pinned = twin.get("pinned")
            twin_source = twin.get("source")
            twin_text = twin.get("text", "")
            twin_confirmed = twin.get("last_confirmed")
            # Pinned twins resist correction from screens.
            if twin_pinned and source != "stated":
                return {"success": False,
                        "error": ("already know something too similar: "
                                  f"\"{twin_text}\""),
                        "similar_to": twin.get("id"),
                        "similarity": round(sim, 2)}
            # Stated corrections supersede the old entry.
            if source == "stated" and text != twin_text:
                _add_tombstones({twin.get("id")})
                # Carry over first_seen and pinned state.
                first_seen = twin.get("first_seen")
                inherit_pin = bool(twin_pinned)
                # Also re-check pin if stated + identity category.
                if source == "stated" and category == _PIN_CATEGORY:
                    inherit_pin = True
            else:
                # Inferred near-dup: refuse but bump last_confirmed so the
                # twin's recency is reinforced (corroboration by re-evidence).
                _bump_last_confirmed_safe(twin.get("id"))
                return {"success": False,
                        "error": ("already know something too similar: "
                                  f"\"{twin_text}\""),
                        "similar_to": twin.get("id"),
                        "similarity": round(sim, 2),
                        "corroborated": twin.get("id")}
        if len(memories) >= _MAX_MEMORIES:
            return {"success": False,
                    "error": "memory is full; forget something first"}
        mem_id = _mem_id(text, subject)
        # If this fact was previously forgotten, lift its tombstone instead of
        # leaving the re-remember masked by the old suppression.
        _remove_tombstones({mem_id})
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        superseded = None
        if sim >= _DUP_SIMILARITY and source == "stated" and text != twin_text:
            superseded = twin.get("id")
            first_seen = twin.get("first_seen")
            inherit_pin = bool(twin.get("pinned")) or \
                (source == "stated" and category == _PIN_CATEGORY)
            inherit_importance = twin.get("importance")
            pinned = inherit_pin
            entry = {
                "id": mem_id,
                "ts": now,
                "first_seen": first_seen,
                "category": (category or None),
                "subject": subject,
                "text": text,
                "confidence": round(conf, 2),
            }
            if source:
                entry["source"] = source
            if inherit_importance is not None:
                entry["importance"] = inherit_importance
            if inherit_pin:
                entry["pinned"] = True
            _append_jsonl(_long_term_path(), entry)
            return {"success": True, "id": mem_id, "confidence": round(conf, 2),
                    "pinned": pinned, "subject": subject,
                    "superseded": superseded}
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
        if source:
            entry["source"] = source
        if pinned:
            entry["pinned"] = True
        _append_jsonl(_long_term_path(), entry)
        return {"success": True, "id": mem_id, "confidence": round(conf, 2),
                "pinned": pinned, "subject": subject}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _estimate_tokens(text):
    """Rough token count for *text*: ~4 chars per token, minimum 1."""
    return max(1, len(text) // 4)


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

# Heuristic threshold for the list-rejection guard in tool_remember: if a
# screen-inferred fact splits into ≥4 comma-separated items it is likely a
# laundry-list of consumed content, not a single claim.
_LIST_ITEM_THRESHOLD = 4


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


# Threshold for detecting duplicate commentary in the short-term log.
_COMMENTARY_DUP_SIMILARITY = 0.8


def _is_commentary_dup(answer, short_term):
    """Check if *answer* duplicates any of the last ~10 Dragon lines.

    Returns True when the answer is too similar to a recent dragon remark,
    suggesting the model would repeat itself on an unchanged screen.
    """
    if not short_term:
        return False
    dragon_lines = [e.get("text", "") for e in short_term[-10:]
                    if (e.get("label", "") or "").lower() == "dragon"]
    for line in dragon_lines:
        if _text_similarity(answer, line) >= _COMMENTARY_DUP_SIMILARITY:
            return True
    return False


def _is_laundry_list(text):
    """True if text looks like a list of 4+ comma-separated items."""
    return len(text.split(",")) >= _LIST_ITEM_THRESHOLD


def _age_days(entry, now):
    """Whole days since the fact was first learned (or last stamped). 0 on error."""
    stamp = entry.get("last_confirmed") or entry.get("first_seen") or entry.get("ts")
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
    """Rank a memory for retrieval: relevance + importance + confidence + recency,
    pinned first.

    `query_tokens` is the tokenized query (empty set => no relevance term, so the
    ranking falls back to importance, confidence, and recency — used by the
    query-less prompt path). Recency decays on a half-life so an old fact fades
    but never goes negative; a pinned absolute gets a boost that dwarfs every
    other term. Importance measures how much the fact matters for being a good
    long-term companion (1-10).
    """
    score = 0.0
    if query_tokens:
        fact_tokens = _tokenize(
            (entry.get("text") or "") + " " + (entry.get("category") or ""))
        if fact_tokens:
            overlap = len(query_tokens & fact_tokens) / len(query_tokens)
            score += _RELEVANCE_WEIGHT * overlap
    score += _IMPORTANCE_WEIGHT * (_clamp_importance(
        entry.get("importance"), 5) / 10.0)
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
    # Retrieval with use: bump last_confirmed for all returned matches so
    # facts the model actually consults get a recency boost.
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for m in matches:
        _bump_last_confirmed(m.get("id"), now)
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
    """Current hoard total as an int: legacy base + gold deltas − treasure costs.

    The hoard is an append-only ledger of signed deltas, one shard per machine,
    so two machines awarding/docking gold at once both persist and the sum is
    correct. A legacy single-value gold.json (from before sharding) is folded in
    as a starting base. Treasure purchases are stored in a separate shard and
    subtracted here so the total reflects what the dragon can actually spend.
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
    for path in _shard_glob("treasures"):
        for entry in _read_jsonl(path):
            try:
                total -= int(entry.get("cost", 0))
            except (TypeError, ValueError):
                continue
    return total


def tool_adjust_gold(amount=0, reason="", source="model", **_):
    """Add (reward) or subtract (punishment) gold from the hoard. Deterministic.

    ``source`` records provenance: "model" (default going forward), "operator"
    (from /gold commands), or None (legacy).
    """
    try:
        delta = int(amount)
    except (TypeError, ValueError):
        return {"success": False, "error": "amount must be a whole number"}
    if delta == 0:
        return {"success": False, "error": "amount must be non-zero"}
    clamped = delta != 0 and abs(delta) > _MAX_GOLD_DELTA
    delta = max(-_MAX_GOLD_DELTA, min(_MAX_GOLD_DELTA, delta))
    before = _read_gold()
    try:
        # The only arithmetic is the sum in _read_gold — never the LLM. We just
        # append this machine's signed delta to its own ledger shard.
        _append_jsonl(_gold_ledger_path(), {
            "delta": delta,
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": (reason or "").strip() or None,
            "source": source,
        })
    except Exception as e:
        return {"success": False, "error": str(e)}
    result = {"success": True, "change": delta, "previous": before,
              "total": before + delta, "reason": (reason or "").strip() or None}
    if clamped:
        result["clamped"] = True
    return result


def tool_gold_total(**_):
    """Report the current gold hoard total."""
    return {"success": True, "total": _read_gold()}


# --------------------------------------------------------------------------
# Treasure shop — buy_treasure, list_treasures, gold_history
# --------------------------------------------------------------------------


def _treasure_ledger_path():
    return _shard_path("treasures")


def _humanize_age(ts, now=None, compact=False):
    """Humanize an ISO 8601 timestamp to a relative age string.

    ``compact=True`` returns e.g. "2h", "1d", "30m", "5d".
    ``compact=False`` returns e.g. "2 hours ago", "1 day ago", "30 minutes ago", "5 days ago".
    """
    if now is None:
        now = dt.datetime.now().astimezone()
    try:
        ts_dt = dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return "unknown" if compact else "unknown"
    try:
        delta = now - ts_dt
    except (TypeError, ValueError):
        return "unknown" if compact else "unknown"
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "future" if compact else "in the future"
    if total_seconds < 60:
        if compact:
            return f"{total_seconds}s"
        if total_seconds < 2:
            return "just now"
        return f"{total_seconds} seconds ago"
    minutes = total_seconds // 60
    if minutes < 60:
        if compact:
            return f"{minutes}m"
        return f"{minutes} minutes ago" if minutes != 1 else "1 minute ago"
    hours = minutes // 60
    if hours < 24:
        if compact:
            return f"{hours}h"
        return f"{hours} hours ago" if hours != 1 else "1 hour ago"
    days = hours // 24
    if days < 365:
        if compact:
            return f"{days}d"
        return f"{days} days ago" if days != 1 else "1 day ago"
    years = days // 365
    if compact:
        return f"{years}y"
    return f"{years} years ago" if years != 1 else "1 year ago"


def _today_shard_date():
    """Today's date in local time for purchase-cap counting."""
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d")


def _count_purchases_today():
    """Count treasure purchases made today on this host's shard."""
    today = _today_shard_date()
    count = 0
    try:
        for entry in _read_jsonl(_treasure_ledger_path()):
            ts = entry.get("ts", "")
            if ts.startswith(today):
                count += 1
    except FileNotFoundError:
        pass
    return count


def _parse_now(compact=False):
    """Return current local datetime and ISO timestamp."""
    now = dt.datetime.now().astimezone()
    return now, now.isoformat(timespec="seconds")


def hoard_mood(now=None):
    """Return (mood_key, phrase) derived from gold + treasure ledgers.

    Precedence — first match wins:
      indebted   total < 0
      stung      fine within last _HOARD_MOOD_FINE_HOURS
      delighted  purchase within last _HOARD_MOOD_PURCHASE_HOURS
      prospering 24h net gold delta >= _HOARD_MOOD_PROSPERING_THRESHOLD
      content    otherwise (phrase is None)
    """
    if now is None:
        now, _ = _parse_now()
    total = _read_gold()
    if total < 0:
        return ("indebted", "your hoard is in debt — wounded pride")
    # Check for recent fine.
    cutoff_fine = now - dt.timedelta(hours=_HOARD_MOOD_FINE_HOURS)
    for path in _shard_glob("gold"):
        for entry in _read_jsonl(path):
            delta = entry.get("delta")
            if isinstance(delta, str):
                try:
                    delta = int(delta)
                except (TypeError, ValueError):
                    continue
            if delta is None:
                continue
            try:
                delta = int(delta)
            except (TypeError, ValueError):
                continue
            if delta < 0:
                try:
                    ts = dt.datetime.fromisoformat(entry["ts"])
                    if ts >= cutoff_fine:
                        return ("stung", "you were fined recently and it smarts")
                except (KeyError, ValueError, TypeError):
                    continue
    # Check for recent purchase.
    cutoff_purchase = now - dt.timedelta(hours=_HOARD_MOOD_PURCHASE_HOURS)
    for path in _shard_glob("treasures"):
        for entry in _read_jsonl(path):
            try:
                ts = dt.datetime.fromisoformat(entry["ts"])
                if ts >= cutoff_purchase:
                    name = entry.get("name", "a treasure")
                    return ("delighted", f"you bought {name} today and adore it")
            except (KeyError, ValueError, TypeError):
                continue
    # Check 24h net gold delta.
    cutoff_delta = now - dt.timedelta(hours=24)
    net_24h = 0
    for path in _shard_glob("gold"):
        for entry in _read_jsonl(path):
            delta = entry.get("delta")
            if isinstance(delta, str):
                try:
                    delta = int(delta)
                except (TypeError, ValueError):
                    continue
            if delta is None:
                continue
            try:
                delta = int(delta)
            except (TypeError, ValueError):
                continue
            try:
                ts = dt.datetime.fromisoformat(entry["ts"])
                if ts >= cutoff_delta:
                    net_24h += delta
            except (KeyError, ValueError, TypeError):
                continue
    if net_24h >= _HOARD_MOOD_PROSPERING_THRESHOLD:
        return ("prospering", "your hoard grew today")
    return ("content", None)


def _collect_treasures_sorted():
    """Union all treasure shards, newest first."""
    all_items = []
    for path in _shard_glob("treasures"):
        for entry in _read_jsonl(path):
            all_items.append(entry)
    all_items.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return all_items


def _collect_all_events(limit=10):
    """Merge gold deltas and purchases into a single newest-first list, capped.

    Returns up to ``limit`` events, each:
      {"type": "adjustment"|"purchase", "delta": int|None, "ts": str,
       "reason": str|None, "name": str|None, "cost": int|None, "source": str|None}
    """
    events = []
    for path in _shard_glob("gold"):
        for entry in _read_jsonl(path):
            delta = entry.get("delta")
            if isinstance(delta, str):
                try:
                    delta = int(delta)
                except (TypeError, ValueError):
                    continue
            if delta is None:
                continue
            try:
                delta = int(delta)
            except (TypeError, ValueError):
                continue
            source = entry.get("source")
            if source is None:
                source = "unknown"
            events.append({
                "type": "adjustment",
                "delta": delta,
                "ts": entry.get("ts", ""),
                "reason": entry.get("reason"),
                "name": None,
                "cost": None,
                "source": source,
            })
    for path in _shard_glob("treasures"):
        for entry in _read_jsonl(path):
            events.append({
                "type": "purchase",
                "delta": -entry.get("cost", 0),
                "ts": entry.get("ts", ""),
                "reason": f"bought: {entry.get('name', '?')}",
                "name": entry.get("name"),
                "cost": entry.get("cost"),
                "source": entry.get("source", "unknown"),
            })
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]


def tool_buy_treasure(name, tier, desc="", **_):
    """Purchase a treasure from your hoard.

    The dragon invents the item; code owns the tier price.
    ``name`` ≤ 60 chars, ``desc`` ≤ 200 chars, ``tier`` must be known.
    Fails if hoard is insufficient or daily cap reached.
    """
    if not name or not name.strip():
        return {"success": False, "error": "name is required"}
    name = name.strip()[:60]
    if desc:
        desc = desc.strip()[:200]
    tier = (tier or "").strip()
    if tier not in _TREASURE_TIERS:
        return {"success": False, "error": f"unknown tier: {tier or '(empty)'} — must be one of {', '.join(sorted(_TREASURE_TIERS))}"}
    cost = _TREASURE_TIERS[tier]
    total = _read_gold()
    if total < cost:
        shortfall = cost - total
        return {"success": False, "error": f"insufficient funds: you have {total} gold, need {cost} ({shortfall} short)"}
    today_count = _count_purchases_today()
    if today_count >= _MAX_PURCHASES_PER_DAY:
        return {"success": False, "error": "purchase cap reached — only 3 per day per host"}
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    raw = f"{name}|{now_iso}|{_machine_id()}"
    tid = hashlib.sha256(raw.encode()).hexdigest()[:16]
    try:
        _append_jsonl(_treasure_ledger_path(), {
            "id": tid,
            "name": name,
            "tier": tier,
            "cost": cost,
            "desc": (desc or "").strip() or None,
            "ts": now_iso,
        })
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "name": name, "tier": tier, "cost": cost,
            "remaining": total - cost}


def tool_list_treasures(**_):
    """List all treasures you own, newest first, with total collection value."""
    treasures = _collect_treasures_sorted()
    total_value = sum(t.get("cost", 0) for t in treasures)
    items = [{"name": t.get("name", "?"), "tier": t.get("tier", "?"),
              "desc": t.get("desc"), "ts": t.get("ts", ""), "cost": t.get("cost", 0)}
             for t in treasures]
    return {"success": True, "treasures": items, "count": len(items), "total_value": total_value}


def tool_gold_history(limit=10, **_):
    """Return recent gold ledger events plus the current total.

    ``limit`` is clamped to 1–25. Events include both adjustments and purchases.
    """
    limit = max(1, min(25, limit))
    total = _read_gold()
    raw_events = _collect_all_events(limit=limit)
    now, _ = _parse_now()
    events = []
    for e in raw_events:
        kind = "purchase" if e["type"] == "purchase" else "adjustment"
        events.append({
            "delta": e["delta"],
            "when": _humanize_age(e["ts"], now=now, compact=False),
            "reason": e["reason"] or "(no reason)",
            "kind": kind,
        })
    return {"total": total, "events": events}


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
    """Pull the {"memories": [...]} and optional {"episode": str|null} from the
    model's dream response.

    Tolerates code fences and surrounding prose by extracting the outermost
    JSON object. Returns (memories, episode) where memories is the list and
    episode is a string or None. Returns (None, None) on parse failure.
    """
    if not raw:
        return None, None
    text = raw.strip()
    if text.startswith("```"):
        # strip a ```json … ``` fence if the model added one
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    mems = obj.get("memories")
    if not isinstance(mems, list):
        return None, None
    episode = obj.get("episode")
    # episode may be str|null; coerce None for absent field.
    if episode is not None and not isinstance(episode, str):
        episode = None
    return mems, episode
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


def _bump_last_confirmed_safe(mem_id):
    """Bump last_confirmed for *mem_id* on this machine's shard if >24h ago."""
    try:
        _bump_last_confirmed(mem_id)
    except Exception:
        pass


def _bump_last_confirmed(mem_id, now=None):
    """Rewrite this machine's shard to set/advance last_confirmed for *mem_id*.

    Only proceeds if the entry hasn't been confirmed today (rate limit).
    If the id lives only in a peer's shard this is a no-op (the write
    target is this machine's own shard).
    """
    if now is None:
        now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today = now[:10]  # YYYY-MM-DD
    path = _long_term_path()
    entries = _read_jsonl(path)
    updated = False
    for e in entries:
        if e.get("id") == mem_id:
            existing = e.get("last_confirmed", "")
            # Rate limit: once per day.
            if existing and existing[:10] == today:
                return
            e["last_confirmed"] = now
            updated = True
            break
    if updated:
        _rewrite_jsonl(path, entries)


def apply_dream(raw):
    """Validate a dream response and rewrite long-term memory from it.

    On any parse/validation failure returns (False, reason) and leaves memory
    untouched. On success backs up the prior store to long_term.bak.jsonl, writes
    the consolidated set, trims short-term, and returns (True, new_count).
    """
    mems, episode = _parse_dream(raw)
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
        category = (m.get("category") or None)
        conf = _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
        # Carry prior provenance through so a dream can't launder an inferred
        # fact into stated certainty. A dream may lower confidence but may never
        # raise a non-stated fact above its source cap. For brand-new merged text
        # (no matching prior at all) accept the model's claim; mark provenance
        # as "inferred" for storage purposes but don't clamp.
        if prior is not None:
            provenance = prior.get("source")  # may be None
            if provenance is not None and provenance != "stated":
                conf = _resolve_confidence(conf, provenance)
        else:
            provenance = "inferred"
        # An insight is the dragon's own deduction, not a stated fact. The dream
        # must not inflate it: a cluster of reworded near-dups reads as mutual
        # corroboration and would otherwise be pushed to certainty that outranks
        # concrete facts. Re-apply the reflected ceiling so insights stay hedged
        # and remain prunable by staleness.
        if category == _INSIGHT_CATEGORY:
            conf = min(conf, _REFLECTED_CEILING)
        entry = {
            "id": mem_id,
            "ts": ts,
            "first_seen": first_seen,
            "category": category,
            "subject": subject,
            "text": text,
            "confidence": round(conf, 2),
        }
        if provenance is not None:
            entry["source"] = provenance
        # Carry importance through from prior; for new/merged text default 5.
        if prior and prior.get("importance") is not None:
            entry["importance"] = prior["importance"]
        elif "importance" not in entry:
            entry["importance"] = 5
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
             and _text_similarity(text, k["text"]) >= (
                 _INSIGHT_DUP_SIMILARITY
                 if category == _INSIGHT_CATEGORY
                 and k.get("category") == _INSIGHT_CATEGORY
                 else _DUP_SIMILARITY)),
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

    # Store the episodic summary if the model provided one.
    if episode:
        try:
            store_episode(episode)
        except Exception:
            pass  # episodes are best-effort; never break the dream.

    # Tombstone hygiene: compact this machine's own tombstone shard — drop
    # dead+old tombstones (≥30 days past TTL) whose ids no longer exist in any
    # live shard. Never touch a peer's tombstone shard.
    _compact_tombstones(previous, refined_ids, refined)

    return True, len(refined)


def add_insights(insights):
    """Persist a few synthesized insights as long-term facts; return the count added.

    Each insight is the dragon's own deduction (category 'insight', confidence
    capped like an inferred fact via the 'reflected' source). Deduped against the
    live store by content hash so repeated reflection doesn't pile up, capped to
    _MAX_INSIGHTS_PER_REFLECT per pass, to _MAX_INSIGHTS_TOTAL live insight entries,
    and to _MAX_MEMORIES overall. Insights are never pinned, so a later dream is
    free to prune the weak ones.
    """
    if not insights:
        return 0
    try:
        existing = read_long_term()
    except Exception:
        return 0
    existing_ids = {m.get("id") for m in existing}
    count = len(existing)
    # Insights are reworded near-dups of each other by nature, so the category is
    # held to a hard ceiling regardless of how much room _MAX_MEMORIES leaves.
    insight_count = sum(1 for m in existing
                        if m.get("category") == _INSIGHT_CATEGORY)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for ins in insights:
        if (added >= _MAX_INSIGHTS_PER_REFLECT or count >= _MAX_MEMORIES
                or insight_count >= _MAX_INSIGHTS_TOTAL):
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
        insight_count += 1
        added += 1
    return added


# --- Episodic session summaries (tier-3 memory) --------------------------------

def _episode_path():
    """This machine's own episode shard."""
    return _shard_path("episodes")


def _read_episodes():
    """All episodes across shards, ordered oldest-first."""
    entries = []
    for path in _shard_glob("episodes"):
        entries.extend(_read_jsonl(path))
    return sorted(entries, key=lambda e: e.get("ts", ""))


def store_episode(text, date_str=None):
    """Store one episode summary (≤200 chars), trimming oldest if over cap."""
    text = (text or "").strip()
    if not text:
        return
    if len(text) > _EPISODE_MAX_CHARS:
        text = text[:_EPISODE_MAX_CHARS]
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    if not date_str:
        try:
            date_str = now[:10]
        except (IndexError, TypeError):
            date_str = "unknown"
    _append_jsonl(_episode_path(), {
        "ts": now,
        "date": date_str,
        "text": text,
    })
    # Trim this machine's own shard to _MAX_EPISODES.
    entries = _read_jsonl(_episode_path())
    if len(entries) > _MAX_EPISODES:
        _rewrite_jsonl(_episode_path(),
                       entries[-_MAX_EPISODES:])


def read_episodes():
    """Last N episodes across all shards, newest-first, capped to 5."""
    episodes = _read_episodes()
    return list(reversed(episodes))[:5]


# --------------------------------------------------------------------------
# Scheduled reminders — host-sharded append-only ledger
# --------------------------------------------------------------------------
#
# Each machine owns its own shard: reminders.<host>.jsonl.  Firing or cancelling
# *appends* a {"id": ..., "status": "fired"|"cancelled"} record; the effective
# state is the last record per id.  This mirrors the tombstone pattern so a
# synced --memory-dir never produces write conflicts.

_MAX_REMINDER_TEXT = 300


def _reminder_path():
    """This machine's own reminder shard."""
    return _shard_path("reminders")


def _read_reminders():
    """All reminder records across shards, ordered oldest-first."""
    entries = []
    for path in _shard_glob("reminders"):
        entries.extend(_read_jsonl(path))
    return sorted(entries, key=lambda e: e.get("ts") or "")


def effective_reminders():
    """Effective state per reminder id (last record wins)."""
    by_id = {}
    for r in _read_reminders():
        rid = r.get("id")
        if rid:
            by_id[rid] = r
    return by_id


def tool_set_reminder(text="", due_iso="", **_):
    """Schedule a reminder for a future absolute ISO time.

    The model computes the absolute time itself (it has the `now` tool and
    the timestamp in context).  We validate that *due_iso* parses and is in
    the future, cap *text* at 300 chars, and generate a stable id from the
    content (text + due).

    Returns {"id": ..., "due_human": ...} on success, or an error dict.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "nothing to remind about (empty text)"}
    if len(text) > _MAX_REMINDER_TEXT:
        text = text[:_MAX_REMINDER_TEXT]
    due_iso = (due_iso or "").strip()
    if not due_iso:
        return {"error": "due time is required (ISO 8601)"}
    try:
        due_dt = dt.datetime.fromisoformat(due_iso)
    except (ValueError, TypeError):
        return {"error": f"cannot parse due time: {due_iso}"}
    now = dt.datetime.now().astimezone()
    if due_dt <= now:
        return {"error": "due time must be in the future"}
    # Stable id from content hash of text + due.
    raw = f"{text}\x00{due_iso}"
    rid = hashlib.sha256(raw.encode()).hexdigest()[:16]
    now_iso = now.isoformat(timespec="seconds")
    due_human = due_dt.strftime("%A %Y-%m-%d %H:%M:%S %Z")
    # Reject duplicate: if this exact content already exists as pending, refuse.
    by_id = effective_reminders()
    if rid in by_id and by_id[rid].get("status") == "pending":
        return {"error": "reminder already set"}
    try:
        _append_jsonl(_reminder_path(), {
            "id": rid,
            "text": text,
            "due": due_iso,
            "due_dt": due_dt.isoformat(timespec="seconds"),
            "created": now_iso,
            "status": "pending",
        })
        return {"id": rid, "due_human": due_human, "text": text}
    except Exception as e:
        return {"error": str(e)}


def tool_list_reminders(**_):
    """List pending reminders across all shards with ids, due times, and origin host."""
    pending = []
    for r in _read_reminders():
        status = r.get("status", "pending")
        # Last-record-wins: only report if this is the active record.
        rid = r.get("id")
        if not rid:
            continue
        by_id = effective_reminders()
        active = by_id.get(rid)
        if active and active.get("status") == "pending":
            host = "unknown"
            for p in _shard_glob("reminders"):
                try:
                    basename = os.path.basename(p)
                    # Format: reminders.<host>.jsonl
                    parts = basename.split(".")
                    if len(parts) >= 3 and parts[0] == "reminders" and parts[-1] == "jsonl":
                        host = ".".join(parts[1:-1])
                        break
                except Exception:
                    pass
            pending.append({
                "id": rid,
                "text": r.get("text", ""),
                "due": r.get("due", ""),
                "host": host,
            })
    # Dedup by id (same reminder may appear from multiple shards).
    seen = set()
    deduped = []
    for r in pending:
        if r["id"] not in seen:
            seen.add(r["id"])
            deduped.append(r)
    return {"reminders": deduped}


def tool_cancel_reminder(id="", **_):
    """Cancel a pending reminder by id. Only pending reminders are cancellable."""
    rid = (id or "").strip()
    if not rid:
        return {"error": "reminder id is required"}
    by_id = effective_reminders()
    active = by_id.get(rid)
    if not active or active.get("status", "pending") != "pending":
        return {"error": "reminder is not pending"}
    try:
        _append_jsonl(_reminder_path(), {
            "id": rid,
            "status": "cancelled",
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        return {"success": True, "id": rid}
    except Exception as e:
        return {"error": str(e)}


def scan_stale_reminders():
    """Fire pending reminders whose due time passed while the app was closed.

    Only reminders ≤24h stale are delivered; older ones are silently marked fired.
    Returns list of (reminder_text,) for reminders that should fire.
    """
    now = dt.datetime.now().astimezone()
    stale_cutoff = now - dt.timedelta(hours=24)
    to_fire = []
    by_id = effective_reminders()
    pending = {rid: r for rid, r in by_id.items() if r.get("status") == "pending"}
    for rid, r in pending.items():
        try:
            due_dt = dt.datetime.fromisoformat(r.get("due_dt", "") or r.get("due", ""))
        except (ValueError, TypeError):
            continue
        if due_dt <= now:
            if due_dt < stale_cutoff:
                # >24h stale: mark fired without delivery.
                _append_jsonl(_reminder_path(), {
                    "id": rid,
                    "status": "fired",
                    "ts": now.isoformat(timespec="seconds"),
                })
            else:
                to_fire.append(r.get("text", ""))
    return to_fire
