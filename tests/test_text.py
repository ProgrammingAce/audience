"""Tests for the pure text/hash helpers in audiencelib.core."""

from audiencelib.core import clip_to_width, _char_width, hamming


def test_char_width_basic():
    assert _char_width("a") == 1
    assert _char_width("世") == 2          # wide CJK
    assert _char_width("́") == 0      # combining accent


def test_clip_to_width_ascii():
    assert clip_to_width("hello", 3) == "hel"
    assert clip_to_width("hello", 10) == "hello"
    assert clip_to_width("hello", 0) == ""


def test_clip_to_width_respects_wide_glyphs():
    # Two wide chars = 4 columns; a 3-col budget fits only one.
    assert clip_to_width("世界", 3) == "世"
    assert clip_to_width("世界", 4) == "世界"


def test_hamming():
    assert hamming(0, 0) == 0
    assert hamming(0b1010, 0b0000) == 2
    assert hamming(0xFFFFFFFFFFFFFFFF, 0) == 64
