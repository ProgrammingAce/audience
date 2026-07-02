"""Tests for git_status tool."""

import os
import subprocess
import tempfile
import pytest

from audiencelib.tools import tool_git_status


class TestGitStatus:
    def test_in_git_repo(self, tmp_path):
        # Create a temp git repo
        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        subprocess.run(["git", "init"], capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], capture_output=True)
        (tmp_path / "hello.txt").write_text("hello")
        subprocess.run(["git", "add", "hello.txt"], capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], capture_output=True)
        # Patch _WORKDIR
        import audiencelib.tools as tools_mod
        original_workdir = tools_mod._WORKDIR
        tools_mod._WORKDIR = str(tmp_path)
        try:
            result = tool_git_status()
            assert "error" not in result
            assert "branch" in result
            assert "changes" in result
            assert "change_count" in result
            assert "last_commit" in result
            assert result["last_commit"]["subject"] == "initial"
        finally:
            tools_mod._WORKDIR = original_workdir
            os.chdir(old_cwd)

    def test_not_a_git_repo(self, tmp_path):
        import audiencelib.tools as tools_mod
        original_workdir = tools_mod._WORKDIR
        tools_mod._WORKDIR = str(tmp_path)
        try:
            result = tool_git_status()
            assert "error" in result
            assert "not a git repository" in result["error"]
        finally:
            tools_mod._WORKDIR = original_workdir

    def test_dirty_working_tree(self, tmp_path):
        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        subprocess.run(["git", "init"], capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], capture_output=True)
        (tmp_path / "hello.txt").write_text("hello")
        subprocess.run(["git", "add", "hello.txt"], capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], capture_output=True)
        # Modify a file (staged)
        (tmp_path / "hello.txt").write_text("modified")
        subprocess.run(["git", "add", "hello.txt"], capture_output=True)
        # Add an untracked file
        (tmp_path / "new.txt").write_text("new")
        import audiencelib.tools as tools_mod
        original_workdir = tools_mod._WORKDIR
        tools_mod._WORKDIR = str(tmp_path)
        try:
            result = tool_git_status()
            assert "error" not in result
            assert "changes" in result
            assert "modified" in result["changes"]
            assert "untracked" in result["changes"]
        finally:
            tools_mod._WORKDIR = original_workdir
            os.chdir(old_cwd)

    def test_change_count_matches(self, tmp_path):
        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        subprocess.run(["git", "init"], capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], capture_output=True)
        # Add untracked file
        (tmp_path / "untracked.txt").write_text("data")
        import audiencelib.tools as tools_mod
        original_workdir = tools_mod._WORKDIR
        tools_mod._WORKDIR = str(tmp_path)
        try:
            result = tool_git_status()
            assert "error" not in result
            # change_count should equal the total items across all states
            total_paths = sum(len(paths) for state, paths in result["changes"].items()
                             if state != "paths")
            assert total_paths == result["change_count"]
        finally:
            tools_mod._WORKDIR = original_workdir
            os.chdir(old_cwd)
