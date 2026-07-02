"""Tests for list_files tool."""

import os
import tempfile
import pytest

from audiencelib.tools import tool_list_files, _safe_path


@pytest.fixture
def workdir_patch(monkeypatch, tmp_path):
    """Patch _WORKDIR to point to a temporary directory for testing."""
    import audiencelib.tools as tools_mod
    original = tools_mod._WORKDIR
    tools_mod._WORKDIR = str(tmp_path)
    monkeypatch.setattr("audiencelib.tools._WORKDIR", str(tmp_path))
    yield tmp_path
    # Restore (monkeypatch handles this, but be explicit)


class TestListFiles:
    def test_lists_files_in_dir(self, workdir_patch, tmp_path):
        (tmp_path / "alpha.txt").touch()
        (tmp_path / "beta.py").touch()
        (tmp_path / "subdir").mkdir()
        result = tool_list_files(path=".")
        names = [e["name"] for e in result["entries"]]
        assert "alpha.txt" in names
        assert "beta.py" in names
        assert "subdir" in names

    def test_dirs_first_then_name(self, workdir_patch, tmp_path):
        (tmp_path / "zebra.txt").touch()
        (tmp_path / "alpha.txt").touch()
        (tmp_path / "zebra_dir").mkdir()
        (tmp_path / "alpha_dir").mkdir()
        result = tool_list_files(path=".")
        names = [e["name"] for e in result["entries"]]
        dir_count = sum(1 for n in names if n.endswith("_dir"))
        file_count = sum(1 for n in names if n.endswith(".txt"))
        assert dir_count == 2
        # All dirs come before all files
        for name in names:
            if name.endswith(".txt"):
                break
        # After the break point there should be no more dirs
        remaining = names[names.index(name):]
        assert all(not n.endswith("_dir") for n in remaining)
        # Within files: alpha before zebra
        file_names = [n for n in names if n.endswith(".txt")]
        assert file_names == sorted(file_names)

    def test_file_size_for_files(self, workdir_patch, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"x" * 100)
        subdir = tmp_path / "mydir"
        subdir.mkdir()
        result = tool_list_files(path=".")
        data_entry = next(e for e in result["entries"] if e["name"] == "data.bin")
        assert data_entry["type"] == "file"
        assert data_entry["size"] == 100
        dir_entry = next(e for e in result["entries"]
                        if e["name"] == "mydir")
        assert dir_entry["type"] == "dir"
        assert dir_entry["size"] is None

    def test_capped_at_200(self, workdir_patch, tmp_path):
        for i in range(250):
            (tmp_path / f"file_{i}.txt").touch()
        result = tool_list_files(path=".")
        entries = [e for e in result["entries"] if e.get("name") is not None]
        assert len(entries) == 200
        assert result["entries"][-1].get("_truncated") is True
        assert result["entries"][-1]["total"] == 250

    def test_path_is_relative(self, workdir_patch, tmp_path):
        result = tool_list_files(path=".")
        assert result["path"] == "."

    def test_escape_attempt_rejected(self, workdir_patch, tmp_path):
        # Create an outside directory
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret")
        result = tool_list_files(path="../outside")
        assert "error" in result

    def test_not_a_directory(self, workdir_patch, tmp_path):
        f = tmp_path / "notadir.txt"
        f.touch()
        result = tool_list_files(path="notadir.txt")
        assert "error" in result
        assert "directory" in result["error"]

    def test_empty_dir(self, workdir_patch, tmp_path):
        result = tool_list_files(path=".")
        assert result["entries"] == []

    def test_default_path(self, workdir_patch, tmp_path):
        result = tool_list_files()
        assert "path" in result
        assert "entries" in result

    def test_hidden_entries_included(self, workdir_patch, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".hidden").touch()
        result = tool_list_files(path=".")
        names = [e["name"] for e in result["entries"]]
        assert ".git" in names
        assert ".hidden" in names
