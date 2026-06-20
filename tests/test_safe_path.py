"""Tests for _safe_path — the working-directory confinement guard."""

import os

from audiencelib import core


def _set_workdir(monkeypatch, path):
    monkeypatch.setattr(core, "_WORKDIR", str(path))


def test_inside_workdir_ok(tmp_path, monkeypatch):
    _set_workdir(monkeypatch, tmp_path)
    resolved, err = core._safe_path("notes.txt")
    assert err is None
    assert resolved == os.path.realpath(os.path.join(str(tmp_path), "notes.txt"))


def test_parent_escape_rejected(tmp_path, monkeypatch):
    _set_workdir(monkeypatch, tmp_path)
    resolved, err = core._safe_path("../escape.txt")
    assert resolved is None
    assert "escapes" in err


def test_absolute_escape_rejected(tmp_path, monkeypatch):
    _set_workdir(monkeypatch, tmp_path)
    resolved, err = core._safe_path("/etc/passwd")
    assert resolved is None


def test_sibling_prefix_not_confused(tmp_path, monkeypatch):
    # /work-evil must not pass as being inside /work (string-prefix bug guard).
    work = tmp_path / "work"
    evil = tmp_path / "work-evil"
    work.mkdir()
    evil.mkdir()
    _set_workdir(monkeypatch, work)
    resolved, err = core._safe_path("../work-evil/x.txt")
    assert resolved is None


def test_symlink_escape_rejected(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "link").symlink_to(outside)
    _set_workdir(monkeypatch, work)
    resolved, err = core._safe_path("link/secret.txt")
    assert resolved is None
