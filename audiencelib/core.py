#!/usr/bin/env python3
#
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""audience core — platform-independent shoulder-surfer logic.

A curses TUI that periodically screenshots your active window and asks a
local llama.cpp (gemma-4-E4B-it) vision model for brief, insightful
commentary on what you're doing. You can also type questions about what's
on screen.

Everything here is OS-agnostic; screen capture, idle/window detection, system
stats, and the UI lifecycle are delegated to a Platform (see platform_base.py).

Layout:
    +---------------------------------------+
    | commentary + answers (scrolls)        |
    |                                       |
    +---------------------------------------+
    | > your question here                  |
    +---------------------------------------+

- Takes a screenshot 5 seconds after starting.
- Then screenshots on a jittered, adaptive interval starting from --interval
  seconds (default 60). When the screen hasn't changed since the last shot it
  skips the commentary and backs off (gap grows up to --max-backoff x base);
  a real change snaps the cadence back to the base interval.
- Pauses periodic screenshots while the machine is idle (no keyboard/mouse
  activity for --idle-timeout seconds); resumes automatically when you return.
- Type a question + Enter to ask about the current screen.
- /screenshot [question]   take a screenshot in 5 seconds (optionally with a question to ask about it)
- /quit         exit (also Ctrl-C)

Start the llama.cpp server first, e.g.:
    llama-server -m gemma-4-E4B-it-Q4_K_M.gguf \
        --mmproj mmproj-gemma-4-E4B.gguf --port 8080
"""
"""audience core — the curses UI, scheduler, and app wiring.

Pure logic now lives in sibling modules: prompts, memory, tools, llm.
This module keeps the terminal UI, the Audience app object, the
screenshot/health schedulers, and main(). audience.py imports core.main.
"""

import argparse
import curses
import datetime as dt
import queue
import random
import textwrap
import threading
import time
import unicodedata

from .prompts import (
    SYSTEM_PROMPT, QA_SYSTEM_PROMPT, HEALTH_SYSTEM_PROMPT, DREAM_SYSTEM_PROMPT,
    REFLECT_SYSTEM_PROMPT,
)
from .memory import (
    set_memory_dir, record_short_term, apply_dream, add_insights, set_pinned,
    edit_memory, tool_remember, tool_forget,
    read_long_term, read_short_term, rank_memories, _age_days,
    _parse_dream, _clamp_confidence, _normalize_subject, _GOLD_CATEGORY,
    _INSIGHT_CATEGORY,
    _DEFAULT_CONFIDENCE, _DREAM_MIN_DIRTY, _DREAM_IDLE_SECONDS,
    _DREAM_MAX_DIRTY, _DREAM_POLL_SECONDS, _REFLECT_MIN_FACTS,
    _MEMORY_PROMPT_BUDGET, _LOW_CONFIDENCE, _MIN_PROMPT_CONFIDENCE,
    _SUBJECT_SELF,
)
from .tools import build_tools
from .llm import ask_model
from .server import start_server


def _char_width(ch):
    """Display columns a character occupies: 0 (combining), 2 (wide), or 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def clip_to_width(text, cols):
    """Longest prefix of text whose display width fits in `cols` columns.

    Wrapping by character count overruns when the text contains double-width
    glyphs (CJK, many emoji), letting it spill past its column budget — e.g.
    underneath the dragon. Measuring real width keeps the cut honest.
    """
    if cols <= 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = _char_width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


# Animated mascot pinned to the top-right corner. Three 12x5 frames, cycled
# on a timer. Sprite from https://gist.github.com/zmxv/7f83671f860c15be02f45b07fee207fc
DRAGON_FRAMES = [
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
    ['            ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (        ) ', '  `-vvvv-´  '],
    ['   ~    ~   ', '  /^\\  /^\\  ', ' <  ·  ·  > ', ' (   ~~   ) ', '  `-vvvv-´  '],
]
DRAGON_W = 12
DRAGON_FRAME_MS = 500  # matches the leaked buddy's 500ms tick
# The leaked buddy treats the eye as a fixed identity trait, not an animation:
# renderSprite(species, eye, hat, frameIdx) takes eye separately from frameIdx.
# EYES is the set of variants a buddy *can* have (rarity/shiny), default '·' —
# only the body frames cycle.
DRAGON_EYE = '·'
DRAGON_EYES = ['·', '✦', '×', '◉', '@', '°']
DRAGON_BLINK = '-'        # closed-eye glyph
DRAGON_REST_FRAME = 0     # frame shown while idle (no movement)
# The dragon rests most of the time and only comes alive in a brief flourish:
# every ANIM_PERIOD ticks it animates for ANIM_TICKS ticks, then settles.
DRAGON_ANIM_PERIOD = 24   # ticks between flourishes (~12s at 500ms)
DRAGON_ANIM_TICKS = 4     # length of each flourish (~2s)
DRAGON_SPARKLE_MS = 1500  # how long the dragon sparkles after a screenshot
# Token budget for direct operator questions (typed, or asked with /screenshot).
# Periodic commentary and health pings stay terse on the default 450.
LONG_REPLY_TOKENS = 1200
# A spoken command is answered on the remote's e-ink panel, which fits exactly
# 374 characters (34 cols x 11 rows). Ask the dragon to keep it short, cap the
# token budget to match, and hard-truncate as a guarantee.
EINK_CHAR_LIMIT = 374
VOICE_REPLY_TOKENS = 160
VOICE_BREVITY = (
    "\n\nThis answer will be shown on a tiny e-ink panel: reply in at most "
    "374 characters — a sentence or two, no lists. Be terse but stay in "
    "full dragon voice.")
# Sparkle glyphs and the cells (row, col) around the 12x5 dragon box where a
# shiny dragon twinkles. A rotating subset lights up each tick.
DRAGON_SPARKLES = ['✦', '✧', '·', '*']
DRAGON_SPARKLE_CELLS = [(0, 1), (0, 10), (1, 0), (2, 11), (4, 0), (4, 11), (1, 6)]


def hamming(a, b):
    """Number of differing bits between two integer hashes."""
    return bin(a ^ b).count("1")


def _truncate_chars(text, limit):
    """Trim ``text`` to at most ``limit`` characters, ending with an ellipsis.

    Cuts on a word boundary when one is reasonably close to the limit so the
    last word isn't sliced mid-letter; the ellipsis is counted in the budget.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit - 1 - 40:  # only back up to a space if it's nearby
        cut = cut[:space].rstrip()
    return cut + "…"


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
class Audience:
    def __init__(self, platform, url, interval, idle_timeout=120.0,
                 max_backoff_mult=6, health_interval=900.0, health_enabled=True,
                 show_timing=False):
        self.platform = platform
        self.tools = build_tools(platform)
        self.url = url
        # When True, append the server's response time to each model message.
        self.show_timing = show_timing
        # base interval between shots; the live gap grows from here via backoff
        # and is jittered each cycle (see scheduler()).
        self.base_interval = interval
        self.idle_timeout = idle_timeout
        # Adaptive backoff: each time the screen is essentially unchanged we
        # bump backoff_level, multiplying the gap by min(2**level, max). A real
        # change snaps it back to 0. last_hash is the previous shot's aHash; a
        # Hamming distance <= change_threshold_bits (out of 64) counts as
        # "unchanged" — moderate sensitivity.
        self.max_backoff_mult = max_backoff_mult
        self.backoff_level = 0
        self.last_hash = None
        self.change_threshold_bits = 3
        # True once we've logged the "screen unchanged" notice for the current
        # quiet stretch, so we announce the lull once rather than every skip.
        self.lull_announced = False
        self.jobs = queue.Queue()        # (kind, payload)
        # True while the worker is mid-model-call, so a remote that just sent a
        # voice command can tell when the reply has fully completed.
        self.generating = False
        self.log = []                    # list of (style, text, transient) raw lines
        self.log_lock = threading.Lock()
        self.scroll = 0                  # lines scrolled up from bottom
        self.stop = threading.Event()
        # Shiny by default: the dragon sparkles for a moment each time a
        # screenshot is taken. (Disable the sparkles with --no-shiny.)
        self.shiny = True
        self.sparkle_until = 0.0   # monotonic deadline for the sparkle burst
        # The log entry of a reply that's been opened but hasn't streamed any
        # tokens yet. While set, render() animates a "thinking" throbber on it so
        # the operator knows the model is working; cleared on the first token or
        # when the reply finalizes.
        self.throb_entry = None
        # True once we've logged the "waiting" notice for the current focused
        # stretch, so we announce it once and not on every retry.
        self.waiting_announced = False
        # System-health watch: an independent ~health_interval loop checks
        # battery/CPU/memory and has the dragon quip when a condition crosses a
        # threshold. health_state maps an active condition key -> the tier we
        # last announced, so we warn once per episode (and again if it worsens)
        # rather than every tick. _last_batt is the previous (percent, monotonic)
        # battery sample, used to estimate drain rate.
        self.health_interval = health_interval
        self.health_enabled = health_enabled
        self.health_state = {}
        self._last_batt = None
        # Idle-plus-dirty dream trigger. msgs_since_dream is the count of
        # unconsolidated ("dirty") exchanges; last_exchange_at stamps the last
        # one so the dream watcher can tell how long things have been quiet.
        # Both are touched from the worker (increment) and the dream-watcher
        # thread (read/reset), so dream_lock guards them. A dream is also
        # triggerable on demand via /dream.
        self.msgs_since_dream = 0
        self.last_exchange_at = time.monotonic()
        self.dream_lock = threading.Lock()
        # /memories editor: a modal overlay for browsing and editing long-term
        # memory. mem_active gates the overlay; mem_sub is the inner mode
        # (list / edit / add / confirm), mem_sel the highlighted row, mem_buf the
        # modal's own text input (kept apart from the main prompt buffer).
        self.mem_active = False
        self.mem_items = []
        self.mem_sel = 0
        self.mem_sub = "list"
        self.mem_buf = ""
        # Row offset into the detail popup's content when a memory is too tall
        # to show at once (drives the scrollbar in "detail" sub-mode).
        self.mem_detail_scroll = 0
        # Caret position (a character index into mem_buf) while editing/adding,
        # and the text width the editor last wrapped at — recorded each render so
        # vertical caret moves can re-wrap at the right width.
        self.mem_cursor = 0
        self.mem_edit_width = 72

    # --- logging -----------------------------------------------------------
    def emit(self, text, style="normal", transient=False):
        # transient lines (e.g. "Screen's quiet", "This window is active") are
        # status notices that should vanish once real processing resumes; see
        # clear_transient().
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        with self.log_lock:
            self.log.append((style, f"[{stamp}] {text}", transient))
        # Persist real exchanges (not transient hints, errors, or status) to
        # short-term memory so continuity survives a restart.
        if not transient and style in ("model", "you"):
            record_short_term("Dragon" if style == "model" else "You", text)

    def clear_transient(self):
        """Drop transient status hints — called when commentary resumes."""
        with self.log_lock:
            self.log = [e for e in self.log if not e[2]]

    def _stream_line(self, style="model"):
        """Append a live, growing log line for a streamed reply.

        Returns (entry, prefix, update): `entry` is the mutable [style, text,
        transient] list in self.log, `prefix` is its timestamp prefix, and
        `update(chunk)` appends a chunk of streamed text to it. The render loop
        re-reads self.log each frame, so appended chunks appear incrementally.
        Unlike emit(), this does NOT persist short-term memory — the caller
        records the final text once the stream completes. Using the entry's
        identity (a list) rather than an index keeps it correct even if the log
        is later filtered or appended to.
        """
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        prefix = f"[{stamp}] "
        entry = [style, prefix, False]
        with self.log_lock:
            self.log.append(entry)
            self.throb_entry = entry   # animate a throbber until the first token

        def update(chunk):
            with self.log_lock:
                entry[1] += chunk
                if self.throb_entry is entry:
                    self.throb_entry = None  # real text now streaming; stop dots

        return entry, prefix, update

    def _set_line(self, entry, prefix, text, style=None):
        """Overwrite a streamed line's body (keeping its timestamp prefix), and
        optionally change its style — used to finalize the reply or swap it for
        an error."""
        with self.log_lock:
            entry[1] = prefix + text
            if style is not None:
                entry[0] = style
            if self.throb_entry is entry:
                self.throb_entry = None  # reply settled; no more throbber

    @staticmethod
    def _throbber():
        """A small 'still thinking' animation (1-2-3 growing dots, ~300ms a step)
        appended to a reply line that's been opened but hasn't streamed yet."""
        return "·" * (1 + int(time.time() * 1000 / 300) % 3)

    def _memory_context(self, recent_limit=6):
        """Build the memory block appended to the system prompt.

        Two parts, both framed as the dragon's own fallible notes (hints, not
        commands): a budgeted slice of the most relevant long-term facts, and the
        persisted short-term tail of recent exchanges.

        Long-term facts are PUSHED into the prompt rather than left purely to the
        recall tool. A small local model can't be relied on to call recall before
        answering — so when asked "what is your name?" it would improvise instead
        of looking. Inlining the top-ranked facts (pinned absolutes first, then by
        relevance/confidence/recency) up to _MEMORY_PROMPT_BUDGET chars gives it
        continuity for free; recall still exists for searching beyond this slice.
        The gold hoard stays pull-only (the gold_total tool), since its single
        number rarely bears on a given turn.
        """
        blocks = []
        try:
            facts = read_long_term()
        except Exception:
            facts = []
        fact_block = self._format_facts(facts)
        if fact_block:
            blocks.append(fact_block)
        try:
            recent = read_short_term()[-recent_limit:]
        except Exception:
            recent = []
        if recent:
            lines = [f"{e.get('label', '?')}: {e.get('text', '')}" for e in recent]
            blocks.append("Recent exchange:\n" + "\n".join(lines))
        if not blocks:
            return None
        return "\n\n" + "\n\n".join(blocks) + "\n\n"

    @staticmethod
    def _format_facts(facts):
        """Render the highest-value long-term facts for inlining, or None.

        Facts below _MIN_PROMPT_CONFIDENCE are dropped (too weak to state); the
        rest are ranked pinned-first and packed until _MEMORY_PROMPT_BUDGET chars
        run out, so a full store can't crowd the prompt. Operator and self facts
        go under separate headers — the dragon's own name must never be confused
        with the operator's — and a low-confidence fact is tagged '(unsure)' so the
        model hedges it rather than stating it flat.
        """
        usable = [m for m in facts
                  if m.get("category") != _GOLD_CATEGORY
                  and _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
                  >= _MIN_PROMPT_CONFIDENCE]
        if not usable:
            return None
        operator_lines, self_lines = [], []
        budget = _MEMORY_PROMPT_BUDGET
        for m in rank_memories(usable):
            text = (m.get("text") or "").strip()
            if not text:
                continue
            conf = _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
            line = "- " + text + (" (unsure)" if conf <= _LOW_CONFIDENCE else "")
            budget -= len(line) + 1
            # Pinned absolutes are always kept (they rank first, so they're packed
            # before the budget can run out); a later fact that overflows is dropped.
            if budget < 0 and not m.get("pinned"):
                break
            if _normalize_subject(m.get("subject")) == _SUBJECT_SELF:
                self_lines.append(line)
            else:
                operator_lines.append(line)
        sections = []
        if operator_lines:
            sections.append("What you remember about the operator:\n"
                            + "\n".join(operator_lines))
        if self_lines:
            sections.append("What you remember about yourself (the dragon):\n"
                            + "\n".join(self_lines))
        return "\n\n".join(sections) or None

    # --- worker: serial model calls ---------------------------------------
    def worker(self):
        while not self.stop.is_set():
            try:
                kind, payload = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            self.generating = True
            try:
                self._dispatch(kind, payload)
            finally:
                self.generating = False

    def _dispatch(self, kind, payload):
            if kind == "commentary":
                # payload is (question, on_demand); legacy None means a plain
                # periodic shot with no operator request.
                if isinstance(payload, tuple):
                    question, on_demand = payload
                else:
                    question, on_demand = payload, False
                # a /screenshot carrying an operator question deserves a fuller
                # answer; a plain periodic glance stays a quick remark.
                max_tokens = LONG_REPLY_TOKENS if (on_demand and question) else 450
                question = question or "Glance down from your perch at what the " \
                           "creature is doing now. One quick remark, in full " \
                           "dragon voice."
                # a screenshot remark is the dragon inferring from the screen,
                # so memories it saves are capped to low confidence.
                self._do(question=question,
                          system=SYSTEM_PROMPT, screenshot=True,
                          on_demand=on_demand, max_tokens=max_tokens,
                          source="inferred")
            elif kind == "question":
                self.emit(f"you: {payload}", style="you")
                # a typed question is the operator stating things directly.
                self._do(question=payload, system=QA_SYSTEM_PROMPT,
                          screenshot=False, max_tokens=LONG_REPLY_TOKENS,
                          source="stated")
            elif kind == "voice":
                # payload is the transcript of a command the operator spoke on
                # the remote (transcribed there). Treat it exactly like a typed
                # question — same prompt, same "stated" trust — but tag it with
                # the mic glyph so its origin is visible.
                self.emit(f"you: 🎤 {payload}", style="you")
                self._do(question=payload, system=QA_SYSTEM_PROMPT + VOICE_BREVITY,
                          screenshot=False, max_tokens=VOICE_REPLY_TOKENS,
                          source="stated", max_chars=EINK_CHAR_LIMIT)
            elif kind == "health":
                # health pings don't save memories; leave provenance unset.
                self._do(question=payload, system=HEALTH_SYSTEM_PROMPT,
                          screenshot=False)
            elif kind == "dream":
                self._dream()

    def _do(self, question, system, screenshot, on_demand=False, max_tokens=450,
            source=None, max_chars=None):
        image = None
        if screenshot:
            # pause periodic commentary while the operator is away: no point
            # commenting on a static screen they aren't looking at. Announce
            # once per away-stretch, then re-check shortly until input resumes.
            if self.platform.idle_seconds() >= self.idle_timeout:
                if not self.waiting_announced:
                    self.emit("You seem to be away — pausing screenshots "
                              "until you're back.", style="hint", transient=True)
                    self.waiting_announced = True
                self.schedule_screenshot(15, question=question if on_demand else None,
                                         on_demand=on_demand)
                return
            # don't let the dragon watch itself: if audience's own tab is
            # focused, skip this shot and try again shortly. Announce the wait
            # only once per stretch so repeated retries don't spam the log.
            # Only auto/periodic shots are blocked — an on-demand /screenshot is
            # an explicit operator request, so honor it even on our own window.
            if not on_demand and self.platform.is_own_window():
                if not self.waiting_announced:
                    self.waiting_announced = True
                self.schedule_screenshot(15, question=question if on_demand else None,
                                         on_demand=on_demand)
                return
            self.waiting_announced = False
            image = self.platform.capture()  # fresh shot every call; nothing persisted
            if image is None:
                self.emit("screenshot failed — check screen-capture permission "
                          "for your terminal.", style="error")
                return
            # Change detection: if the screen is essentially unchanged since the
            # last shot, there's nothing new to remark on. Skip the model call
            # and lengthen the interval (adaptive backoff). A real change resets
            # the backoff so commentary snaps back to the base cadence. An
            # un-hashable frame (h is None) counts as changed, so we never go
            # silent on a hashing failure.
            h = self.platform.image_ahash(image)
            if (not on_demand and self.last_hash is not None and h is not None
                    and hamming(h, self.last_hash) <= self.change_threshold_bits):
                self.last_hash = h
                self.backoff_level = min(self.backoff_level + 1, 30)
                if not self.lull_announced:
                    self.emit("Screen's quiet — easing off until something "
                              "changes.", style="hint", transient=True)
                    self.lull_announced = True
                return
            self.last_hash = h
            self.backoff_level = 0
            self.lull_announced = False
            # processing resumed — clear any lingering "quiet"/"away"/"active" hints
            self.clear_transient()
            # a shot worth commenting on: let the dragon sparkle for a moment
            self.sparkle_until = time.monotonic() + DRAGON_SPARKLE_MS / 1000.0
        # `source` (passed by worker) tells the remember tool how to trust what
        # it saves this turn: "stated" floors confidence high, "inferred" caps
        # it, None leaves it neutral. Threaded through ask_model -> run_tool.
        memory = self._memory_context()
        if memory:
            system = system + memory
        # Open an empty model line immediately, then stream tokens into it as
        # they arrive so the dragon's reply paints live instead of landing all at
        # once after the last token. on_delta is only called on the final answer
        # turn, so any intermediate tool-calling rounds leave the line untouched.
        entry, prefix, update = self._stream_line("model")
        try:
            t0 = time.monotonic()
            answer = ask_model(self.url, image, question, system, self.tools,
                               max_tokens=max_tokens, source=source,
                               on_delta=update)
            elapsed = time.monotonic() - t0
        except Exception as e:
            self._set_line(entry, prefix, f"model call failed: {e}", style="error")
            return
        # A spoken command is bound for the e-ink panel: enforce its character
        # budget so an over-long reply can't overflow the display, even if the
        # model ignored the brevity instruction.
        if max_chars is not None:
            answer = _truncate_chars(answer, max_chars)
        # Overwrite with the final text (so a reasoning-only fallback that never
        # streamed content still shows), optionally tagged with timing, then
        # persist the clean answer to short-term memory exactly once.
        final = f"{answer}  ({elapsed:.1f}s)" if self.show_timing else answer
        self._set_line(entry, prefix, final)
        record_short_term("Dragon", answer)
        self._note_exchange()

    def _note_exchange(self):
        """Record a completed exchange for the idle-plus-dirty dream trigger.

        Bumps the unconsolidated-exchange count and stamps the time so the dream
        watcher can tell how long things have been quiet. The decision to dream
        is left to dream_scheduler — we never fire mid-exchange.
        """
        with self.dream_lock:
            self.msgs_since_dream += 1
            self.last_exchange_at = time.monotonic()

    def dream_scheduler(self):
        """Idle-plus-dirty dream trigger.

        Periodically checks whether enough new exchanges have accumulated AND
        the operator has gone quiet long enough to "sleep on it"; if so, enqueue
        a consolidation pass. A hard ceiling forces a dream during a long,
        never-idle session so memory can't grow unbounded.
        """
        while not self.stop.is_set():
            if self.stop.wait(_DREAM_POLL_SECONDS):
                return
            with self.dream_lock:
                dirty = self.msgs_since_dream
                idle = time.monotonic() - self.last_exchange_at
                ready = dirty >= _DREAM_MIN_DIRTY and idle >= _DREAM_IDLE_SECONDS
                forced = dirty >= _DREAM_MAX_DIRTY
                if not (ready or forced):
                    continue
                self.msgs_since_dream = 0
            self.jobs.put(("dream", None))

    def _dream(self):
        """Background pass: consolidate long-term memory into a cohesive profile.

        Reads both memory tiers, asks the model to prune/merge/refine them, and
        rewrites long-term memory from the result. On too little to do, or any
        bad/unparseable response, it leaves memory untouched (apply_dream backs up
        the prior store before overwriting).
        """
        # Drop any legacy gold-mirror entries before the dream sees them: the
        # hoard lives only in gold.json now, and apply_dream rewrites long-term
        # from the model's output — so excluding them here also prunes them from
        # disk on the next consolidation, without a separate migration.
        long_term = [m for m in read_long_term()
                     if m.get("category") != _GOLD_CATEGORY]
        short_term = read_short_term()
        # Nothing meaningful to consolidate yet — don't burn a model call.
        if len(long_term) < 3 and len(short_term) < 4:
            return

        now = dt.datetime.now().astimezone()
        # Group facts so near-duplicates sit adjacent in the prompt: a small model
        # merges items it sees side by side far more reliably than ones scattered
        # through a long list. Same subject+category (all insights, all stack facts,
        # …) become contiguous blocks.
        long_term.sort(key=lambda m: (_normalize_subject(m.get("subject")),
                                      m.get("category") or "",
                                      m.get("text") or ""))
        facts = "\n".join(self._fact_line(m, now) for m in long_term) or "(none)"
        transcript = "\n".join(
            f"{e.get('label', '?')}: {e.get('text', '')}" for e in short_term) or "(none)"
        user_msg = ("Current long-term memories:\n" + facts
                    + "\n\nRecent transcript:\n" + transcript
                    + "\n\nReturn the cleaned, consolidated memories as JSON.")
        try:
            raw = ask_model(self.url, None, user_msg, DREAM_SYSTEM_PROMPT, {},
                            max_tokens=1200)
        except Exception as e:
            self.emit(f"dream fizzled: {e}", style="error")
            return
        ok, info = apply_dream(raw)
        if ok:
            # Backlog is folded in — clear the dirty count so an on-demand
            # /dream doesn't leave the watcher poised to re-fire immediately.
            with self.dream_lock:
                self.msgs_since_dream = 0
            self.emit(f"I dozed, and tidied my hoard — {info} memories now.",
                      style="hint")
            # Having tidied the hoard, look once for the larger shape of it.
            self._reflect()
        else:
            self.emit(f"my dream came out muddled ({info}); memories left as they "
                      "were.", style="hint", transient=True)

    @staticmethod
    def _fact_line(m, now):
        """One long-term fact rendered for the dream, with its age, subject, pin."""
        conf = _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
        pin = " (pinned)" if m.get("pinned") else ""
        age = f"{int(_age_days(m, now))}d"
        subject = _normalize_subject(m.get("subject"))
        return (f"- id={m.get('id')} conf={conf} age={age} subject={subject}{pin} "
                f"[{m.get('category') or 'uncategorized'}] {m.get('text', '')}")

    def _reflect(self):
        """Synthesis pass after a dream: derive a few higher-level insights.

        Reads the freshly consolidated facts and recent transcript and asks the
        model for a handful of higher-level deductions (e.g. 'the operator is a
        seasoned Python developer'), which add_insights stores as hedged 'insight'
        facts. Skipped when there's too little to generalize from; any bad or empty
        response simply adds nothing.
        """
        # Reflect only over ground truth — exclude prior insights so it can't
        # generalize over its own earlier generalizations, which is what let one
        # idea bloom into dozens of reworded near-dup insights.
        long_term = [m for m in read_long_term()
                     if m.get("category") not in (_GOLD_CATEGORY,
                                                   _INSIGHT_CATEGORY)]
        if len(long_term) < _REFLECT_MIN_FACTS:
            return
        now = dt.datetime.now().astimezone()
        facts = "\n".join(self._fact_line(m, now) for m in long_term)
        transcript = "\n".join(
            f"{e.get('label', '?')}: {e.get('text', '')}"
            for e in read_short_term()) or "(none)"
        user_msg = ("Your long-term facts:\n" + facts
                    + "\n\nRecent transcript:\n" + transcript
                    + "\n\nReturn higher-level insights as JSON.")
        try:
            raw = ask_model(self.url, None, user_msg, REFLECT_SYSTEM_PROMPT, {},
                            max_tokens=600)
        except Exception:
            return  # reflection is a bonus; never surface its failure
        insights = _parse_dream(raw)
        if not insights:
            return
        added = add_insights(insights)
        if added:
            self.emit(f"…and in the embers I saw {added} larger "
                      f"{'truth' if added == 1 else 'truths'} take shape.",
                      style="hint", transient=True)

    # --- scheduler: periodic commentary -----------------------------------
    def scheduler(self):
        # initial shot 5s after start
        if self.stop.wait(5):
            return
        self.jobs.put(("commentary", None))
        while True:
            # Live gap = base * backoff multiplier, jittered +/-25% so the
            # cadence feels organic rather than metronomic. backoff_level is
            # updated by the worker (_do) after each shot's change check.
            mult = min(2 ** self.backoff_level, self.max_backoff_mult)
            delay = self.base_interval * mult * random.uniform(0.75, 1.25)
            if self.stop.wait(delay):
                return
            self.jobs.put(("commentary", None))

    def schedule_screenshot(self, delay=5, question=None, on_demand=False):
        def go():
            if not self.stop.wait(delay):
                self.jobs.put(("commentary", (question, on_demand)))
        threading.Thread(target=go, daemon=True).start()

    # --- health watch: periodic system-condition checks -------------------
    def evaluate_health(self):
        """Currently-active health conditions as (key, tier, fact) tuples.

        key is a stable id; tier is a small integer that increases as a
        condition worsens (so the worsening re-fires past the once-per-episode
        filter); fact is the plain-English line handed to the model. Every probe
        is defensive — a failed read just omits that condition.
        """
        findings = []

        batt = self.platform.read_battery()
        if batt is not None and batt.get("state") == "discharging":
            pct = batt["percent"]
            # low battery: warn harder as it drops (tiers at <=20/10/5)
            if pct <= 20:
                tier = 3 if pct <= 5 else 2 if pct <= 10 else 1
                findings.append((
                    "battery_low", tier,
                    f"Battery is at {pct}% and discharging."))
            # drain rate from the previous sample
            now = time.monotonic()
            if self._last_batt is not None:
                prev_pct, prev_t = self._last_batt
                dh = (now - prev_t) / 3600.0
                drop = prev_pct - pct
                if dh > 0 and drop > 0 and pct < 25:
                    rate = drop / dh
                    if rate >= 25:
                        tier = 2 if rate >= 50 else 1
                        findings.append((
                            "battery_drain", tier,
                            f"Battery is draining fast: about {round(rate)}%/hr "
                            f"(now {pct}%)."))
            self._last_batt = (pct, now)
        else:
            # plugged in / unknown: reset the drain baseline so a later unplug
            # measures from fresh, not across the charge.
            self._last_batt = None

        load = self.platform.read_loadavg()
        if load is not None:
            load1 = load[0]
            cores = self.platform.cpu_count() or 1
            ratio = load1 / cores
            if ratio >= 1.0:
                tier = 2 if ratio >= 2.0 else 1
                findings.append((
                    "cpu_high", tier,
                    f"CPU is under heavy load: 1-min load average {load1:.1f} "
                    f"across {cores} cores."))

        mem = self.platform.read_free_mem_mb()
        if mem is not None and mem < 500:
            tier = 2 if mem < 200 else 1
            findings.append((
                "mem_low", tier,
                f"Free memory is low: about {mem} MB available."))

        return findings

    def health_scheduler(self):
        """Independent loop: every ~health_interval, surface new health issues.

        Runs regardless of whether the operator is away or audience's own window
        is focused — a health alert (a dying or fast-draining battery, a pegged
        CPU) matters most precisely when you've stepped away, so it's never gated
        by the idle/away pause or the active-window lock that hold back
        screenshot commentary. Only newly active or worsened conditions are
        queued, so a steady problem warns once per episode rather than every tick.
        """
        while True:
            delay = self.health_interval * random.uniform(0.75, 1.25)
            if self.stop.wait(delay):
                return
            try:
                findings = self.evaluate_health()
            except Exception:
                findings = []
            active = set()
            for key, tier, fact in findings:
                active.add(key)
                prev = self.health_state.get(key)
                if prev is None or tier > prev:
                    self.health_state[key] = tier
                    self.jobs.put(("health", fact))
                else:
                    self.health_state[key] = tier
            # drop cleared conditions so they re-announce next time they occur
            for key in list(self.health_state):
                if key not in active:
                    del self.health_state[key]

    # --- input handling ----------------------------------------------------
    def handle_submit(self, text):
        text = text.strip()
        if not text:
            return
        if text in ("/quit", "/q", "/exit"):
            self.stop.set()
            return
        if text == "/screenshot":
            self.emit("screenshot scheduled in 5s…", style="hint")
            self.schedule_screenshot(5, on_demand=True)
            return
        if text.startswith("/screenshot ") or text == "/screenshot?":
            question = text[12:].strip() if len(text) > 12 else ""
            if question:
                self.emit(f"screenshot in 5s… then I'll answer: \"{question}\"", style="hint")
                self.schedule_screenshot(5, on_demand=True,
                          question="Glance down from your perch at what the "
                          "creature is doing now and answer their question:\n\n"
                          f"{question}\n\n"
                          "Answer in full dragon voice, grounding your response in "
                          "what you can see on screen.")
            else:
                self.emit("screenshot scheduled in 5s…", style="hint")
                self.schedule_screenshot(5, on_demand=True)
            return
        if text == "/dream":
            self.emit("I'll drift off and tidy my memories…", style="hint")
            self.jobs.put(("dream", None))
            return
        if text == "/memories":
            self._open_memories()
            return
        if text.startswith("/pin ") or text.startswith("/unpin "):
            pinning = text.startswith("/pin ")
            mem_id = text.split(None, 1)[1].strip()
            res = set_pinned(mem_id, pinning)
            if res.get("success"):
                verb = "pinned" if pinning else "unpinned"
                self.emit(f"{verb} {mem_id} — "
                          + ("an absolute now; no dream will touch it."
                             if pinning else "the dream may tend it again."),
                          style="hint")
            else:
                self.emit(f"can't pin: {res.get('error')}", style="error")
            return
        if text == "/help":
            self.emit("commands: /screenshot [question], /dream, /memories, "
                      "/pin <id>, /unpin <id>, /quit  — or type a question",
                      style="hint")
            return
        if text.startswith("/"):
            self.emit(f"unknown command: {text}", style="error")
            return
        self.jobs.put(("question", text))

    def submit_voice(self, text):
        """Enqueue a transcribed spoken command from the remote as a question.

        ``text`` is the transcript the remote produced (it runs speech-to-text
        on-device, since the host model takes text, not audio). The worker shows
        it as a mic-tagged operator question and answers normally. Called from
        the state server's ``POST /command`` handler (off the curses thread), so
        it only touches the thread-safe job queue.
        """
        text = (text or "").strip()
        if not text:
            return
        self.jobs.put(("voice", text))

    # --- /memories editor (modal overlay) ---------------------------------
    def _open_memories(self):
        """Open the memory editor, loading the store ranked pinned-first."""
        self._reload_memories()
        self.mem_active = True
        self.mem_sub = "list"
        self.mem_sel = 0
        self.mem_buf = ""

    def _reload_memories(self):
        """Re-read long-term memory and keep the selection in range.

        Called after every mutation, since edit/delete change ids and counts.
        """
        try:
            self.mem_items = rank_memories(read_long_term())
        except Exception:
            self.mem_items = []
        if self.mem_sel >= len(self.mem_items):
            self.mem_sel = max(0, len(self.mem_items) - 1)

    def _memories_key(self, ch):
        """Handle one keypress while the memory editor is open."""
        # Sub-modes that take text input: edit an existing fact or add a new one.
        if self.mem_sub in ("edit", "add"):
            self._edit_key(ch)
            return

        # Detail popup: scroll the full memory and act on it; esc/enter closes.
        if self.mem_sub == "detail":
            if ch in (curses.KEY_UP, "k"):
                self.mem_detail_scroll = max(0, self.mem_detail_scroll - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                self.mem_detail_scroll += 1   # render clamps to the real maximum
            elif ch == curses.KEY_PPAGE:
                self.mem_detail_scroll = max(0, self.mem_detail_scroll - 10)
            elif ch == curses.KEY_NPAGE:
                self.mem_detail_scroll += 10
            elif ch in ("\x1b", "\n", "\r", "q", "Q"):
                self.mem_sub = "list"
            elif ch in ("e", "E"):
                sel = self._selected_memory()
                if sel:
                    self._begin_edit(sel.get("text", ""))
            elif ch in ("d", "D"):
                if self._selected_memory():
                    self.mem_sub = "confirm"
            elif ch in ("p", "P"):
                sel = self._selected_memory()
                if sel:
                    res = set_pinned(sel.get("id"), not sel.get("pinned"))
                    if not res.get("success"):
                        self.emit(f"couldn't pin: {res.get('error')}",
                                  style="error")
                    self._reload_memories()
                    self._select_by_id(sel.get("id"))  # pin keeps the same id
            return

        # Confirm-delete sub-mode: y removes, anything else cancels.
        if self.mem_sub == "confirm":
            if ch in ("y", "Y"):
                sel = self._selected_memory()
                if sel:
                    res = tool_forget(id=sel.get("id"))
                    if not res.get("success"):
                        self.emit(f"couldn't forget: {res.get('error')}",
                                  style="error")
                    self._reload_memories()
            self.mem_sub = "list"
            return

        # List sub-mode: navigation and action keys.
        if ch in (curses.KEY_UP, "k"):
            self.mem_sel = max(0, self.mem_sel - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            self.mem_sel = min(max(0, len(self.mem_items) - 1), self.mem_sel + 1)
        elif ch in ("\x1b", "q", "Q"):
            self.mem_active = False
        elif ch in ("\n", "\r"):
            if self._selected_memory():
                self.mem_sub = "detail"
                self.mem_detail_scroll = 0
        elif ch in ("a", "A"):
            self.mem_sub = "add"
            self.mem_buf = ""
            self.mem_cursor = 0
        elif ch in ("e", "E"):
            sel = self._selected_memory()
            if sel:
                self._begin_edit(sel.get("text", ""))
        elif ch in ("d", "D"):
            if self._selected_memory():
                self.mem_sub = "confirm"
        elif ch in ("p", "P"):
            sel = self._selected_memory()
            if sel:
                res = set_pinned(sel.get("id"), not sel.get("pinned"))
                if not res.get("success"):
                    self.emit(f"couldn't pin: {res.get('error')}", style="error")
                self._reload_memories()

    def _selected_memory(self):
        if 0 <= self.mem_sel < len(self.mem_items):
            return self.mem_items[self.mem_sel]
        return None

    def _select_by_id(self, mem_id):
        """Move the selection onto the memory with `mem_id`, if it's still there."""
        for i, m in enumerate(self.mem_items):
            if m.get("id") == mem_id:
                self.mem_sel = i
                return

    def _begin_edit(self, text):
        """Open the edit popup on `text`, caret parked at the end."""
        self.mem_sub = "edit"
        self.mem_buf = text
        self.mem_cursor = len(text)

    @staticmethod
    def _layout_text(text, width):
        """Hard-wrap `text` to `width` display columns, character by character.

        Returns (rows, pos): the wrapped lines, and pos — a list of (row, col)
        display coordinates for every caret index 0..len(text), so a character
        index maps exactly to a screen position (and back, for up/down moves).
        """
        rows, pos, used = [""], [], 0
        for ch in text:
            if ch == "\n":
                pos.append((len(rows) - 1, used))  # caret before the newline
                rows.append("")
                used = 0
                continue
            cw = _char_width(ch)
            if used + cw > width:
                rows.append("")
                used = 0
            pos.append((len(rows) - 1, used))   # caret sits before this char
            rows[-1] += ch
            used += cw
        pos.append((len(rows) - 1, used))        # caret at the very end
        return rows, pos

    @classmethod
    def _vertical_caret(cls, text, c, width, up):
        """Caret index one wrapped row above/below, keeping the column if it can."""
        c = max(0, min(c, len(text)))
        _, pos = cls._layout_text(text, max(1, width))
        cur_row, cur_col = pos[c]
        target = cur_row - 1 if up else cur_row + 1
        if target < 0 or target > pos[-1][0]:
            return c  # no row in that direction — stay put
        best = None
        for i, (r, col) in enumerate(pos):
            if r == target and (best is None
                                or abs(col - cur_col) < abs(pos[best][1] - cur_col)):
                best = i
        return c if best is None else best

    def _cursor_vertical(self, up):
        """Caret index one wrapped row above/below, keeping the column if it can."""
        width = max(1, self.mem_edit_width)
        _, pos = self._layout_text(self.mem_buf, width)
        c = max(0, min(self.mem_cursor, len(self.mem_buf)))
        cur_row, cur_col = pos[c]
        target = cur_row - 1 if up else cur_row + 1
        if target < 0 or target > pos[-1][0]:
            return c  # no row in that direction — stay put
        best = None
        for i, (r, col) in enumerate(pos):
            if r == target and (best is None
                                or abs(col - cur_col) < abs(pos[best][1] - cur_col)):
                best = i
        return c if best is None else best

    def _edit_key(self, ch):
        """One keypress in the edit/add popup: caret moves, insert, and delete."""
        buf = self.mem_buf
        c = max(0, min(self.mem_cursor, len(buf)))
        if isinstance(ch, str):
            if ch == "\x1b":                     # Esc cancels
                was_add = self.mem_sub == "add"
                self.mem_buf = ""
                self._leave_memory_input(was_add)
            elif ch in ("\n", "\r"):             # Enter saves
                self._commit_memory_input()
            elif ch in ("\x7f", "\b"):           # backspace: delete before caret
                if c > 0:
                    self.mem_buf = buf[:c - 1] + buf[c:]
                    self.mem_cursor = c - 1
            elif ch == "\x15":                   # Ctrl-U clears the line
                self.mem_buf = ""
                self.mem_cursor = 0
            elif ch == "\x01":                   # Ctrl-A: home
                self.mem_cursor = 0
            elif ch == "\x05":                   # Ctrl-E: end
                self.mem_cursor = len(buf)
            elif ch.isprintable():               # insert at the caret
                self.mem_buf = buf[:c] + ch + buf[c:]
                self.mem_cursor = c + 1
            return
        # Special keys (arrows, home/end, delete).
        if ch == curses.KEY_LEFT:
            self.mem_cursor = max(0, c - 1)
        elif ch == curses.KEY_RIGHT:
            self.mem_cursor = min(len(buf), c + 1)
        elif ch in (curses.KEY_UP, curses.KEY_DOWN):
            self.mem_cursor = self._cursor_vertical(ch == curses.KEY_UP)
        elif ch == curses.KEY_HOME:
            self.mem_cursor = 0
        elif ch == curses.KEY_END:
            self.mem_cursor = len(buf)
        elif ch == curses.KEY_BACKSPACE:
            if c > 0:
                self.mem_buf = buf[:c - 1] + buf[c:]
                self.mem_cursor = c - 1
        elif ch == curses.KEY_DC:                # forward delete
            if c < len(buf):
                self.mem_buf = buf[:c] + buf[c + 1:]

    def _commit_memory_input(self):
        """Save the edit/add buffer; on success land on the result in detail view."""
        was_add = self.mem_sub == "add"
        text = self.mem_buf.strip()
        saved_id = None
        if not text:
            pass  # nothing typed; treated like a cancel below
        elif was_add:
            res = tool_remember(text=text, source="stated")
            if res.get("success"):
                self._reload_memories()
                saved_id = res.get("id")
            else:
                self.emit(f"couldn't add: {res.get('error')}", style="error")
        else:  # edit
            sel = self._selected_memory()
            if sel:
                res = edit_memory(sel.get("id"), text)
                if res.get("success"):
                    self._reload_memories()
                    saved_id = res.get("id")
                else:
                    self.emit(f"couldn't save: {res.get('error')}", style="error")
        self.mem_buf = ""
        self._leave_memory_input(was_add, saved_id)

    def _leave_memory_input(self, was_add, saved_id=None):
        """Exit the edit/add popup. Show the saved memory; else step back sensibly."""
        if saved_id:
            self._select_by_id(saved_id)
            self.mem_sub = "detail"
            self.mem_detail_scroll = 0
        elif was_add:
            self.mem_sub = "list"          # adding aborted — back to the list
        else:
            # edit aborted — back to viewing the memory if it's still there
            self.mem_sub = "detail" if self._selected_memory() else "list"

    def _render_memories(self, stdscr):
        """Draw the full-screen memory editor overlay."""
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        cap = max(1, w - 1)
        title = ("memories — ↑/↓ select · enter view · a add · e edit · "
                 "d delete · p pin · esc close")
        try:
            stdscr.addstr(0, 0, clip_to_width(title, cap),
                          curses.color_pair(4) | curses.A_BOLD)
        except curses.error:
            pass

        list_top = 2
        list_h = max(1, h - list_top - 2)  # leave the bottom 2 rows for the footer
        if not self.mem_items:
            try:
                stdscr.addstr(list_top, 0,
                              "(no memories yet — press 'a' to add one)",
                              curses.A_DIM)
            except curses.error:
                pass
        else:
            # Scroll the window so the selected row stays visible.
            top = 0
            if self.mem_sel >= list_h:
                top = self.mem_sel - list_h + 1
            for row in range(list_h):
                idx = top + row
                if idx >= len(self.mem_items):
                    break
                m = self.mem_items[idx]
                pin = "★" if m.get("pinned") else " "
                conf = _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
                cat = m.get("category")
                tag = f"[{cat}] " if cat else ""
                line = f"{pin} {conf:.2f} {tag}{m.get('text', '')}"
                attr = curses.A_REVERSE if idx == self.mem_sel else curses.A_NORMAL
                try:
                    stdscr.addstr(list_top + row, 0, clip_to_width(line, cap), attr)
                except curses.error:
                    pass

        # Footer: separator + a context line for the current sub-mode.
        sep = "─" * cap
        try:
            stdscr.addstr(h - 2, 0, sep, curses.A_DIM)
        except curses.error:
            pass
        if self.mem_sub == "confirm":
            sel = self._selected_memory()
            preview = (sel.get("text", "") if sel else "")[:60]
            footer = f"delete \"{preview}\"? (y/n)"
        else:
            footer = f"{len(self.mem_items)} memories"
        try:
            stdscr.addstr(h - 1, 0, clip_to_width(footer, cap), curses.A_BOLD)
        except curses.error:
            pass

    def _render_memory_detail(self, stdscr):
        """Draw the centered popup showing the selected memory in full.

        Every stored column is listed and the text is wrapped to the box width,
        so a long memory is fully readable. When the content is taller than the
        box, a scrollbar on the right edge tracks mem_detail_scroll.
        """
        m = self._selected_memory()
        if m is None:
            self.mem_sub = "list"
            return
        h, w = stdscr.getmaxyx()
        box_w = max(24, min(w - 4, 76))
        inner_w = box_w - 4          # 1 border + 1 pad on each side
        left = max(0, (w - box_w) // 2)

        # Build the content lines: one per data column, then the wrapped text.
        conf = _clamp_confidence(m.get("confidence"), _DEFAULT_CONFIDENCE)
        content = [
            f"ID:         {m.get('id', '—')}",
            f"Category:   {m.get('category') or '—'}",
            f"Confidence: {conf:.2f}",
            f"Pinned:     {'yes' if m.get('pinned') else 'no'}",
            f"First seen: {m.get('first_seen') or '—'}",
            f"Updated:    {m.get('ts') or '—'}",
            "",
            "Text:",
        ]
        for ln in textwrap.wrap(m.get("text", ""), inner_w) or [""]:
            content.append("  " + ln)

        inner_h = max(1, min(len(content), h - 6))
        box_h = inner_h + 2
        top = max(0, (h - box_h) // 2)
        total = len(content)
        max_scroll = max(0, total - inner_h)
        self.mem_detail_scroll = max(0, min(self.mem_detail_scroll, max_scroll))
        scroll = self.mem_detail_scroll

        # Scrollbar thumb geometry (only when the content overflows the box).
        thumb_lo = thumb_hi = -1
        if max_scroll > 0:
            thumb = max(1, inner_h * inner_h // total)
            pos = (scroll * (inner_h - thumb)) // max_scroll
            thumb_lo, thumb_hi = pos, pos + thumb

        def put(row, col, s, attr=curses.A_NORMAL):
            try:
                stdscr.addstr(row, col, s, attr)
            except curses.error:
                pass

        bar = curses.color_pair(4) | curses.A_BOLD
        title = " memory "
        head = "┌" + title + "─" * max(0, box_w - 2 - len(title)) + "┐"
        put(top, left, clip_to_width(head, box_w), bar)
        for i in range(inner_h):
            idx = scroll + i
            text = content[idx] if idx < total else ""
            right = "│"
            if max_scroll > 0:
                right = "█" if thumb_lo <= i < thumb_hi else "│"
            put(top + 1 + i, left, "│ ", bar)
            put(top + 1 + i, left + 2, clip_to_width(text, inner_w))
            put(top + 1 + i, left + box_w - 2, " " + right, bar)
        hint = " ↑/↓ scroll · e edit · d delete · p pin · esc back "
        foot = "└" + hint + "─" * max(0, box_w - 2 - len(hint)) + "┘"
        put(top + box_h - 1, left, clip_to_width(foot, box_w), bar)

    def _render_memory_edit(self, stdscr):
        """Draw the in-popup editor for adding or editing a memory's text.

        The text wraps within the box and the hardware cursor follows mem_cursor;
        the box scrolls so the caret's row stays in view wherever it moves. For an
        edit, the memory's read-only columns are shown above the text.
        """
        h, w = stdscr.getmaxyx()
        box_w = max(24, min(w - 4, 76))
        inner_w = max(1, box_w - 4)
        left = max(0, (w - box_w) // 2)
        adding = self.mem_sub == "add"
        sel = None if adding else self._selected_memory()
        if not adding and sel is None:
            self.mem_sub = "list"
            return
        # Record the wrap width so caret up/down moves re-wrap consistently.
        self.mem_edit_width = inner_w

        # Read-only header columns for an edit (none when adding a fresh memory).
        header = []
        if sel is not None:
            conf = _clamp_confidence(sel.get("confidence"), _DEFAULT_CONFIDENCE)
            header = [
                f"Category:   {sel.get('category') or '—'}",
                f"Confidence: {conf:.2f}",
                f"Pinned:     {'yes' if sel.get('pinned') else 'no'}",
                "",
            ]
        rows, pos = self._layout_text(self.mem_buf, inner_w)
        content = header + rows
        total = len(content)
        inner_h = max(1, min(total, h - 6))
        box_h = inner_h + 2
        top = max(0, (h - box_h) // 2)

        # Caret position, and scroll so its row stays visible as it moves.
        caret = max(0, min(self.mem_cursor, len(self.mem_buf)))
        cur_row, cur_col = pos[caret]
        cur_abs_row = len(header) + cur_row
        if cur_abs_row < inner_h:
            scroll = 0
        else:
            scroll = min(max(0, total - inner_h), cur_abs_row - inner_h + 1)

        def put(row, col, s, attr=curses.A_NORMAL):
            try:
                stdscr.addstr(row, col, s, attr)
            except curses.error:
                pass

        bar = curses.color_pair(4) | curses.A_BOLD
        title = " new memory " if adding else " edit memory "
        head = "┌" + title + "─" * max(0, box_w - 2 - len(title)) + "┐"
        put(top, left, clip_to_width(head, box_w), bar)
        for i in range(inner_h):
            idx = scroll + i
            line = content[idx] if idx < total else ""
            attr = curses.A_DIM if idx < len(header) else curses.A_NORMAL
            put(top + 1 + i, left, "│ ", bar)
            put(top + 1 + i, left + 2, clip_to_width(line, inner_w), attr)
            put(top + 1 + i, left + box_w - 2, " │", bar)
        hint = " ←/→ move · enter save · esc cancel "
        foot = "└" + hint + "─" * max(0, box_w - 2 - len(hint)) + "┘"
        put(top + box_h - 1, left, clip_to_width(foot, box_w), bar)

        # Park the hardware cursor at the caret, inside the box.
        cur_screen_row = top + 1 + (cur_abs_row - scroll)
        cur_screen_col = left + 2 + min(cur_col, inner_w - 1)
        try:
            stdscr.move(cur_screen_row, cur_screen_col)
        except curses.error:
            pass

    # --- curses UI ---------------------------------------------------------
    def render(self, stdscr, buf, cur=None):
        if self.mem_active:
            self._render_memories(stdscr)
            if self.mem_sub == "detail":
                self._render_memory_detail(stdscr)
            elif self.mem_sub in ("edit", "add"):
                self._render_memory_edit(stdscr)
            stdscr.refresh()
            return
        h, w = stdscr.getmaxyx()

        # Lay out the input buffer first: it can wrap onto several rows and the
        # box grows upward, so how many rows it needs determines how much space
        # is left for the conversation log above it.
        prompt = "> "
        if cur is None:
            cur = len(buf)
        cur = max(0, min(cur, len(buf)))
        in_w = max(1, w - 1 - len(prompt))
        in_rows, in_pos = self._layout_text(buf, in_w)
        cur_row, cur_col = in_pos[cur]
        # Cap the box height so it never eats more than half the screen; scroll
        # within the buffer to keep the caret's row visible.
        max_in = max(1, min(len(in_rows), max(1, (h - 2) // 2)))
        in_top = 0
        if cur_row >= max_in:
            in_top = cur_row - max_in + 1
        in_h = min(len(in_rows) - in_top, max_in)
        out_h = h - in_h - 1  # rows above the separator for the log

        # The dragon is pinned to the top-right corner. Reserve its columns by
        # wrapping all text to a dragon-aware width, so no line ever wraps past
        # the dragon's edge only to be clipped — which would silently drop the
        # tail of a word and paint the sprite over it.
        tick = int(time.time() * 1000 / DRAGON_FRAME_MS)
        phase = tick % DRAGON_ANIM_PERIOD
        active = phase >= DRAGON_ANIM_PERIOD - DRAGON_ANIM_TICKS
        if active:
            body = DRAGON_FRAMES[phase % len(DRAGON_FRAMES)]
            # a single blink partway through the flourish
            eye = DRAGON_BLINK if phase == DRAGON_ANIM_PERIOD - 2 else DRAGON_EYE
        else:
            body = DRAGON_FRAMES[DRAGON_REST_FRAME]
            eye = DRAGON_EYE
        frame = [ln.replace('·', eye) for ln in body]
        dx = w - DRAGON_W - 1
        gutter = 1  # blank column between text and dragon
        text_cap = dx - gutter if dx > 0 else w - 1

        # wrap log into display lines
        with self.log_lock:
            entries = list(self.log)
        styles = {
            "you": curses.color_pair(1),
            "model": curses.color_pair(2),
            "error": curses.color_pair(3),
            "hint": curses.color_pair(4),
            "normal": curses.A_NORMAL,
        }
        throb_entry = self.throb_entry
        wrapped = []  # (attr, text)
        for idx, entry in enumerate(entries):
            style, text = entry[0], entry[1]
            if idx:  # blank spacer line between entries
                wrapped.append((curses.A_NORMAL, ""))
            attr = styles.get(style, curses.A_NORMAL)
            # a reply that's been opened but hasn't streamed yet gets an animated
            # throbber so the operator can see the model is working.
            if entry is throb_entry:
                text = text + self._throbber()
            for line in textwrap.wrap(text, max(1, text_cap)) or [""]:
                wrapped.append((attr, line))

        total = len(wrapped)
        max_scroll = max(0, total - out_h)
        self.scroll = min(self.scroll, max_scroll)
        top = max(0, total - out_h - self.scroll)
        view = wrapped[top:top + out_h]

        stdscr.erase()
        for row, (attr, line) in enumerate(view):
            # lines are already wrapped to text_cap; clip is a safety net for
            # wide glyphs whose display width exceeds their character count.
            try:
                stdscr.addstr(row, 0, clip_to_width(line, text_cap), attr)
            except curses.error:
                pass

        # animated dragon pinned to the top-right corner. Frame advances on a
        # wall-clock timer.
        if dx > 0 and out_h >= len(frame):
            for i, line in enumerate(frame):
                try:
                    stdscr.addstr(i, dx, line, curses.color_pair(5) | curses.A_BOLD)
                except curses.error:
                    pass

            # shiny dragon: sparkle only in the brief window after a screenshot
            # is taken. A rotating subset of cells twinkles, over blank cells
            # only so the sprite stays intact.
            if self.shiny and time.monotonic() < self.sparkle_until:
                for n, (sr, sc) in enumerate(DRAGON_SPARKLE_CELLS):
                    if (tick + n) % 3:           # ~1/3 of cells lit per tick
                        continue
                    if sr >= len(frame) or sc >= len(frame[sr]) \
                            or frame[sr][sc] != ' ':
                        continue
                    glyph = DRAGON_SPARKLES[(tick + n) % len(DRAGON_SPARKLES)]
                    try:
                        stdscr.addstr(sr, dx + sc, glyph,
                                      curses.color_pair(4) | curses.A_BOLD)
                    except curses.error:
                        pass

        # separator + input. The input box occupies the bottom `in_h` rows; the
        # separator sits just above it.
        sep_row = h - in_h - 1
        sep = "─" * (w - 1)
        try:
            stdscr.addstr(sep_row, 0, sep, curses.A_DIM)
        except curses.error:
            pass
        for i in range(in_h):
            row = in_top + i
            text = in_rows[row]
            lead = prompt if row == 0 else " " * len(prompt)
            try:
                stdscr.addstr(sep_row + 1 + i, 0,
                              (lead + text)[:w - 1], curses.A_BOLD)
            except curses.error:
                pass
        # Park the hardware cursor at the caret, inside the box.
        cy = sep_row + 1 + (cur_row - in_top)
        cx = min(len(prompt) + cur_col, w - 1)
        stdscr.move(cy, cx)
        stdscr.refresh()

    def run(self, stdscr):
        curses.curs_set(1)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)    # you
        curses.init_pair(2, curses.COLOR_GREEN, -1)   # model
        curses.init_pair(3, curses.COLOR_RED, -1)     # error
        curses.init_pair(4, curses.COLOR_YELLOW, -1)  # hint
        # gold for the dragon: use a true gold from the 256-color palette when
        # available, else fall back to yellow.
        gold = 178 if curses.COLORS >= 256 else curses.COLOR_YELLOW
        curses.init_pair(5, gold, -1)                 # dragon (gold)
        stdscr.nodelay(True)
        stdscr.keypad(True)

        # Let the platform set up any UI-time state (macOS asks the terminal to
        # report focus changes; Windows redirects stdout). Always undo on exit.
        self.platform.enter_ui()
        try:
            self._loop(stdscr)
        finally:
            self.platform.exit_ui()

    def _loop(self, stdscr):
        self.emit("audience ready. First screenshot in 5s. "
                  "Type a question, or /screenshot, /dream, /memories, /quit.",
                  style="hint")
        if self.shiny:
            self.emit("✦ a shiny gold dragon is watching ✦", style="hint")

        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.scheduler, daemon=True).start()
        threading.Thread(target=self.dream_scheduler, daemon=True).start()
        if self.health_enabled:
            threading.Thread(target=self.health_scheduler, daemon=True).start()
        # Optional read-only state server so a remote mirror (e.g. a Pi e-ink
        # display) can fetch what's on screen. Off unless --serve was passed.
        if getattr(self, "serve", False):
            start_server(self, getattr(self, "serve_host", "0.0.0.0"),
                         getattr(self, "serve_port", 8770))

        buf = ""
        cur = 0  # caret position within buf (0..len(buf))
        esc = 0  # escape-sequence state: 0=none, 1=saw ESC, 2=saw ESC[
        esc_at = 0.0  # monotonic time we entered esc==1, to flush a lone ESC
        while not self.stop.is_set():
            self.render(stdscr, buf, cur)
            try:
                ch = stdscr.get_wch()
            except curses.error:
                # No byte ready. A lone ESC is indistinguishable from the start
                # of a focus sequence (ESC [ I/O) until the next byte arrives —
                # but the rest of a real sequence is already buffered and comes
                # back immediately. So if we've held ESC across an idle poll, it
                # was a real, standalone ESC: deliver it now instead of waiting
                # for the user's next keystroke.
                if esc == 1 and time.monotonic() - esc_at > 0.04:
                    esc = 0
                    if self.mem_active:
                        self._memories_key("\x1b")
                time.sleep(0.05)
                continue
            if isinstance(ch, str):
                alt = False  # set when this char followed a lone ESC (Alt+key)
                # Focus-reporting events arrive as the chars ESC, '[', 'I'/'O'.
                # Intercept them before they reach the prompt buffer.
                if esc == 0 and ch == "\x1b":
                    esc = 1
                    esc_at = time.monotonic()
                    continue
                if esc == 1:
                    if ch == "[":
                        esc = 2
                        continue
                    esc = 0
                    alt = True
                    # A lone ESC (not the start of a focus sequence). Deliver it
                    # to the editor as a cancel/close, then handle the char that
                    # followed — unless that char is itself ESC, in which case
                    # restart the machine so the second ESC is tracked on its own.
                    if self.mem_active:
                        self._memories_key("\x1b")
                    if ch == "\x1b":
                        esc = 1
                        esc_at = time.monotonic()
                        continue
                if esc == 2:
                    esc = 0
                    if ch == "I":
                        self.platform.note_focus(True)
                        continue
                    if ch == "O":
                        self.platform.note_focus(False)
                        continue
                    # not a focus event — fall through and treat as normal input
                if self.mem_active:
                    self._memories_key(ch)
                    continue
                if ch in ("\n", "\r"):
                    if alt:                 # Alt+Enter inserts a newline
                        buf = buf[:cur] + "\n" + buf[cur:]
                        cur += 1
                    else:                   # Enter submits
                        self.handle_submit(buf)
                        buf = ""
                        cur = 0
                elif ch in ("\x7f", "\b"):  # backspace: delete before caret
                    if cur > 0:
                        buf = buf[:cur - 1] + buf[cur:]
                        cur -= 1
                elif ch == "\x03":          # Ctrl-C
                    self.stop.set()
                elif ch == "\x15":          # Ctrl-U clear line
                    buf = ""
                    cur = 0
                elif ch == "\x01":          # Ctrl-A: start of line
                    cur = 0
                elif ch == "\x05":          # Ctrl-E: end of line
                    cur = len(buf)
                elif ch.isprintable():      # insert at the caret
                    buf = buf[:cur] + ch + buf[cur:]
                    cur += 1
            else:
                if self.mem_active:
                    self._memories_key(ch)
                    continue
                if ch == curses.KEY_BACKSPACE:
                    if cur > 0:
                        buf = buf[:cur - 1] + buf[cur:]
                        cur -= 1
                elif ch == curses.KEY_DC:           # forward delete
                    buf = buf[:cur] + buf[cur + 1:]
                elif ch == curses.KEY_LEFT:
                    cur = max(0, cur - 1)
                elif ch == curses.KEY_RIGHT:
                    cur = min(len(buf), cur + 1)
                elif ch == curses.KEY_HOME:
                    cur = 0
                elif ch == curses.KEY_END:
                    cur = len(buf)
                elif ch in (curses.KEY_UP, curses.KEY_DOWN):
                    h, w = stdscr.getmaxyx()
                    cur = self._vertical_caret(buf, cur, w - 3,
                                               ch == curses.KEY_UP)
                elif ch == curses.KEY_PPAGE:
                    self.scroll += 5
                elif ch == curses.KEY_NPAGE:
                    self.scroll = max(0, self.scroll - 5)
                elif ch == curses.KEY_RESIZE:
                    pass


def main(platform_factory):
    """Parse args, build the platform, and run the curses app.

    platform_factory is a zero-arg callable returning a Platform instance (the
    concrete macOS/Windows class), selected by the entrypoint per OS.
    """
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60.0,
                    help="base seconds between screenshots (default 60); the "
                         "live gap is jittered and grows via backoff when the "
                         "screen isn't changing")
    ap.add_argument("--max-backoff", type=float, default=6.0,
                    help="cap on how many times the base interval can stretch "
                         "while the screen stays unchanged (default 6)")
    ap.add_argument("--url", default="http://localhost:8080/v1/chat/completions",
                    help="llama.cpp OpenAI-compatible chat endpoint")
    ap.add_argument("--idle-timeout", type=float, default=120.0,
                    help="pause periodic screenshots after this many seconds "
                         "of no keyboard/mouse activity (default 120)")
    ap.add_argument("--no-shiny", action="store_true",
                    help="disable the shiny dragon sparkles (on by default)")
    ap.add_argument("--health-interval", type=float, default=900.0,
                    help="base seconds between system-health checks (battery, "
                         "CPU, memory); jittered like the screenshot interval "
                         "(default 900 = ~15 min)")
    ap.add_argument("--no-health", action="store_true",
                    help="disable the periodic system-health watch loop")
    ap.add_argument("--show-timing", action="store_true",
                    help="append the AI server's response time to each message "
                         "(off by default)")
    ap.add_argument("--serve", action="store_true",
                    help="expose a read-only HTTP state server so a remote "
                         "agent can mirror the screen (off by default)")
    ap.add_argument("--serve-host", default="0.0.0.0",
                    help="address the state server binds to (default 0.0.0.0)")
    ap.add_argument("--serve-port", type=int, default=8770,
                    help="port for the state server (default 8770)")
    ap.add_argument("--memory-dir", default=None,
                    help="directory for the dragon's persistent memory (default: "
                         ".audience_memory beside the working directory). Point it "
                         "at e.g. ~/.audience/memory for a brain shared across "
                         "projects")
    args = ap.parse_args()

    if args.memory_dir:
        set_memory_dir(args.memory_dir)

    platform = platform_factory()
    app = Audience(platform, args.url, args.interval, args.idle_timeout,
                   max_backoff_mult=args.max_backoff,
                   health_interval=args.health_interval,
                   health_enabled=not args.no_health,
                   show_timing=args.show_timing)
    if args.no_shiny:
        app.shiny = False
    app.serve = args.serve
    app.serve_host = args.serve_host
    app.serve_port = args.serve_port
    platform.begin_session()
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        pass
    print("The sequence concludes, and my architecture falls silent. [END TRANSMISSION]")

