"""The remote's transcribed voice command becomes a mic-tagged question turn.

``submit_voice`` is the seam between the state server's ``POST /command`` handler
and the model worker. We exercise it on a bare instance (bypassing the heavy
curses ``__init__``) so the test stays fast and hermetic.
"""

import queue

from audiencelib.core import Audience, EINK_CHAR_LIMIT, _truncate_chars


def _bare_app():
    app = Audience.__new__(Audience)
    app.jobs = queue.Queue()
    return app


def test_submit_voice_enqueues_voice_job():
    app = _bare_app()
    app.submit_voice("turn left")
    assert app.jobs.get_nowait() == ("voice", "turn left")


def test_submit_voice_strips_and_drops_empty():
    app = _bare_app()
    app.submit_voice("  look up  ")
    assert app.jobs.get_nowait() == ("voice", "look up")
    # blank/whitespace-only transcripts are dropped, not enqueued
    app.submit_voice("   ")
    app.submit_voice(None)
    assert app.jobs.empty()


def test_truncate_chars_short_text_untouched():
    assert _truncate_chars("a short reply", 374) == "a short reply"


def test_truncate_chars_enforces_eink_limit():
    long = "word " * 200  # 1000 chars
    out = _truncate_chars(long, EINK_CHAR_LIMIT)
    assert len(out) <= EINK_CHAR_LIMIT
    assert out.endswith("…")
    # cut on a word boundary, so no partial "wor" at the end
    assert out[:-1].rstrip().endswith("word")
