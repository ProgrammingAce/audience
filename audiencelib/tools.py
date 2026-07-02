"""File tools and the per-platform tool registry.

Memory/gold tools live in :mod:`audiencelib.memory`; this module owns
the working-directory-confined file tools and wires every tool
(including the platform probes) into the registry build_tools returns.
"""

import base64
import json
import os
import subprocess

from .memory import (
    tool_remember, tool_recall, tool_forget,
    tool_adjust_gold, tool_gold_total,
    tool_buy_treasure, tool_list_treasures, tool_gold_history,
    tool_set_reminder, tool_list_reminders, tool_cancel_reminder,
    scan_stale_reminders,
)

# --------------------------------------------------------------------------
# File tools — confined to the working directory
#
# Mostly read-only, local, low-sensitivity facts the model can pull to ground
# its commentary instead of guessing from a fuzzy screenshot. The model can be
# fully prompt-injected by an adversarial screen, so file access is confined to
# the working directory; reads are size-capped, and writes can only create new
# files — existing files are never overwritten — so an injected model can't
# clobber source, build scripts, or anything already on disk.
# --------------------------------------------------------------------------

# Directory this script was launched from — all write/read operations are confined here.
_WORKDIR = os.getcwd()

# Cap on file reads (and writes) so an injected model can't pull a multi-gigabyte
# file into memory and the model context, or fill the disk.
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def _safe_path(rel_path):
    """Resolve a relative path against _WORKDIR, reject escapes.

    Uses commonpath (not a string prefix) so a sibling like /work-evil can't
    masquerade as being inside /work, and normcase so the check matches the
    filesystem's case-sensitivity (Windows treats paths case-insensitively).
    realpath resolves symlinks before the check, so a symlink can't point out.
    """
    real_workdir = os.path.realpath(_WORKDIR)
    full = os.path.realpath(os.path.join(real_workdir, os.path.normpath(rel_path)))
    try:
        common = os.path.commonpath(
            [os.path.normcase(full), os.path.normcase(real_workdir)])
    except ValueError:
        # raised when paths live on different drives (Windows) — can't be inside
        return None, "path escapes the working directory"
    if common != os.path.normcase(real_workdir):
        return None, "path escapes the working directory"
    return full, None


def tool_write_file(path, content=""):
    """Create a new file in the current working directory.

    Refuses to overwrite anything that already exists: an adversarial screen can
    prompt-inject the model, and a write tool that could clobber existing files
    would let it rewrite source, configs, or git internals. New files only.
    """
    resolved, err = _safe_path(path)
    if err:
        return {"success": False, "error": err}
    if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
        return {"success": False, "error": "content exceeds 50 MB limit"}
    try:
        if os.path.lexists(resolved):
            return {"success": False,
                    "error": "file already exists; overwriting is not allowed"}
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
        # "x" mode fails if the path was created between the check and the open,
        # closing the TOCTOU window rather than silently overwriting.
        with open(resolved, "x") as f:
            f.write(content)
        return {"success": True, "path": os.path.relpath(resolved, _WORKDIR)}
    except FileExistsError:
        return {"success": False,
                "error": "file already exists; overwriting is not allowed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_read_file(path, **_):
    """Read text from a file in the current working directory."""
    resolved, err = _safe_path(path)
    if err:
        return {"success": False, "error": err}
    try:
        if os.path.getsize(resolved) > _MAX_FILE_BYTES:
            return {"success": False, "error": "file exceeds 50 MB limit"}
        with open(resolved, "r") as f:
            content = f.read()
        lines = content.splitlines()
        return {
            "success": True,
            "path": os.path.relpath(resolved, _WORKDIR),
            "content": content,
            "lines": len(lines),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


_LIST_FILE_CAP = 200


def tool_list_files(path=".", **_):
    """List entries in a directory under the working directory.

    Non-recursive, one level per call.  Entries are sorted with directories
    first, then by name within each group.  At most ``_LIST_FILE_CAP`` entries
    are returned; when the directory is larger a ``{"truncated": true,
    "total": n}`` marker is appended.  Per-entry fields are ``{"name": ...,
    "type": "dir"|"file", "size": bytes or null}`` (size for files only).
    Broken entries (dangling symlinks, permissions errors) are silently
    skipped.
    """
    resolved, err = _safe_path(path)
    if err:
        return {"error": err, "path": path}
    try:
        if not os.path.isdir(resolved):
            return {"error": "not a directory", "path": path}
        entries = []
        try:
            total = sum(1 for _ in os.scandir(resolved))
        except OSError:
            total = 0
        dirs, files = [], []
        for entry in os.scandir(resolved):
            try:
                stat = entry.stat(follow_symlinks=False)
                entry_type = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                entry_name = entry.name
                if entry_type == "file":
                    files.append({"name": entry_name, "type": entry_type,
                                  "size": stat.st_size})
                else:
                    dirs.append({"name": entry_name, "type": entry_type,
                                 "size": None})
            except OSError:
                continue  # broken entry — silently skip
        dirs.sort(key=lambda e: e["name"])
        files.sort(key=lambda e: e["name"])
        shown = dirs + files
        if len(shown) > _LIST_FILE_CAP:
            result = shown[:_LIST_FILE_CAP]
            result.append({"_truncated": True, "total": total})
            return {"path": os.path.relpath(resolved, _WORKDIR), "entries": result}
        return {"path": os.path.relpath(resolved, _WORKDIR), "entries": shown}
    except Exception as e:
        return {"error": str(e), "path": path}


def tool_git_status(**_):
    """Read-only git snapshot of the working directory repo.

    Returns branch info, ahead/behind counts, a summarized file list
    (up to 20 paths per state), and the last commit hash.  Uses only local
    state — never touches the network.
    """
    try:
        result = subprocess.run(
            ["git", "-C", _WORKDIR, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return {"error": "the working directory is not a git repository"}
    except FileNotFoundError:
        return {"error": "git is not available"}
    except (subprocess.TimeoutExpired, Exception):
        return {"error": "git check failed"}

    out = {"branch": None, "ahead": 0, "behind": 0, "changes": {}, "change_count": 0,
           "last_commit": None}

    # --- branch, ahead/behind ---
    try:
        result = subprocess.run(
            ["git", "-C", _WORKDIR, "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.returncode == 0:
            lines = (result.stdout[:4096] if result.stdout else "").splitlines()
            branch = None
            ahead = behind = 0
            change_states = {}
            change_paths = {}
            for line in lines:
                if line.startswith("# branch.head"):
                    branch = line.split(" ", 2)[-1].strip() or None
                elif line.startswith("# branch.oid"):
                    pass  # skip
                elif line.startswith("# ahead") or line.startswith("# behind"):
                    val = line.strip().split()[-1]
                    try:
                        if line.startswith("# ahead"):
                            ahead = int(val)
                        else:
                            behind = int(val)
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("##"):
                    continue
                else:
                    # Status lines in v2 format.
                    # Format: "<status-chars> <path-info>"
                    # status-chars may include a stage digit prefix (1-4 for unmerged,
                    # otherwise just <index><worktree> chars like "M." or "?").
                    # parts[0] = status chars, parts[-1] = path, remaining are path-info.
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    status_str = parts[0]
                    # Stage digit is always at the start of parts[0] as a separate token.
                    # If parts[0] is a single digit, the real status is in parts[1].
                    if len(parts) >= 3 and parts[0].isdigit() and len(parts[1]) >= 2:
                        status_str = parts[1]
                    # Now parse <index><worktree> from status_str.
                    index = status_str[0] if len(status_str) >= 1 else " "
                    worktree = status_str[1] if len(status_str) >= 2 else " "
                    path_val = parts[-1] if parts else ""
                    state = "modified" if index == "M" or worktree == "M" else (
                        "added" if index == "A" or worktree == "A" else (
                        "deleted" if index == "D" or worktree == "D" else (
                        "untracked" if index == "?" else "untracked")))
                    if state not in change_paths:
                        change_states[state] = 0
                        change_paths[state] = []
                    change_states[state] = change_states.get(state, 0) + 1
                    if len(change_paths[state]) < 20:
                        change_paths[state].append(path_val)
            out["branch"] = branch
            out["ahead"] = ahead
            out["behind"] = behind
            out["changes"] = {k: change_paths.get(k, []) for k, v in change_states.items()}
            out["change_count"] = sum(change_states.values())
    except (subprocess.TimeoutExpired, Exception):
        pass  # branch info is best-effort

    # --- last commit ---
    try:
        result = subprocess.run(
            ["git", "-C", _WORKDIR, "log", "-1", "--format=%h%x09%ar%x09%s"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("\t", 2)
            if len(parts) >= 3:
                out["last_commit"] = {
                    "hash": parts[0].strip(),
                    "when": parts[1].strip(),
                    "subject": parts[2].strip(),
                }
    except (subprocess.TimeoutExpired, Exception):
        pass  # last commit is best-effort

    return out


# --------------------------------------------------------------------------
# Tool registry
#
# Built per-platform: the now/active_window_info/system_stats/now_playing tools
# delegate to the Platform's probe methods, while read_file/write_file are pure
# stdlib and shared. Returns a {name: (callable, schema)} dict.
# --------------------------------------------------------------------------
# Tools that change state or touch the filesystem. A screenshot is untrusted
# input — text on the captured screen could try to prompt-inject the model into
# calling one of these. To keep a screenshot from ever triggering a side effect,
# these are neither advertised nor executed on any request that carries an
# image; only read-only grounding tools (window title, time, stats, recall…)
# remain available there. See ask_model().
# `remember` is intentionally excluded: inferring a fact from the screen and
# saving it (at capped confidence — see _resolve_confidence) is core to the
# periodic commentary, so screenshots are allowed to create memories.
SIDE_EFFECTING_TOOLS = frozenset({
    "write_file", "read_file", "forget", "adjust_gold",
    "buy_treasure",
    "clipboard_read", "list_files", "set_reminder", "cancel_reminder",
})


def build_tools(platform):
    """Construct the tool registry bound to a Platform instance."""

    def tool_now(**_):
        """Current local date and time."""
        now = dt.datetime.now().astimezone()
        return {
            "iso": now.isoformat(timespec="seconds"),
            "human": now.strftime("%A %Y-%m-%d %H:%M:%S %Z"),
        }

    def tool_active_window_info(**_):
        return platform.active_window_info()

    def tool_system_stats(**_):
        """Battery, CPU load, free memory, free disk, and uptime — all read-only."""
        out = {}
        load = platform.read_loadavg()
        if load is not None:
            out["load_avg"] = {"1m": round(load[0], 2), "5m": round(load[1], 2),
                               "15m": round(load[2], 2)}
        batt = platform.read_battery()
        if batt is not None:
            out["battery"] = batt
        mem = platform.read_free_mem_mb()
        if mem is not None:
            out["memory_free_mb"] = mem
        disk = platform.read_free_disk_gb()
        if disk is not None:
            out["disk_free_gb"] = disk
        uptime = platform.read_uptime()
        if uptime:
            out["uptime"] = uptime
        return out or {"error": "no stats available"}

    def tool_now_playing(**_):
        track = platform.now_playing()
        return {"now_playing": track or "(nothing playing)"}

    def tool_look_at_screen(**_):
        """Capture the operator's screen. The PNG is returned via a sentinel
        key that ask_model's loop intercepts and feeds back as an image; the
        model never sees the raw base64 in the text channel."""
        img = platform.capture()
        if img is None:
            return {"error": "screen capture failed"}
        return {"_screenshot_png_b64": base64.b64encode(img).decode()}

    tools = {
        "now": (tool_now, {
            "type": "function",
            "function": {
                "name": "now",
                "description": "Get the current local date and time.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "active_window_info": (tool_active_window_info, {
            "type": "function",
            "function": {
                "name": "active_window_info",
                "description": "Get the application name and window title of the "
                               "frontmost window — useful when the title bar is too "
                               "small to read in the screenshot.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "system_stats": (tool_system_stats, {
            "type": "function",
            "function": {
                "name": "system_stats",
                "description": "Get battery level, CPU load average, free memory, "
                               "free disk space, and system uptime.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "now_playing": (tool_now_playing, {
            "type": "function",
            "function": {
                "name": "now_playing",
                "description": "Get the song currently playing in Music or Spotify, "
                               "if anything is playing.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "look_at_screen": (tool_look_at_screen, {
            "type": "function",
            "function": {
                "name": "look_at_screen",
                "description": "Capture and look at the operator's current "
                               "screen. Call this when the spoken request refers "
                               "to what's on screen (e.g. 'what am I looking at', "
                               "'read this', 'what's this error').",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "read_file": (tool_read_file, {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read text content from a file in the current working directory. "
                               "Only files within the directory the script was launched from "
                               "can be read. Returns the file content and line count.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path (from the script's directory) "
                                           "of the file to read. Use forward slashes.",
                        },
                    },
                    "required": ["path"],
                },
            }}),
        "remember": (tool_remember, {
            "type": "function",
            "function": {
                "name": "remember",
                "description": "Save a durable, useful fact to long-term memory so "
                               "you recall it in future sessions (e.g. a name, what "
                               "they're building, tools they favor, a stated "
                               "preference). One crisp fact per call. Set `subject` "
                               "to say WHOSE fact it is: 'operator' (default) for a "
                               "fact about the operator, 'self' for a fact about YOU, "
                               "the dragon — keep your own name separate from theirs. "
                               "Do NOT store ephemeral state (battery, what's on "
                               "screen now) or secrets/passwords/tokens.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The fact to remember, stated plainly.",
                        },
                        "category": {
                            "type": "string",
                            "description": "Optional short label, e.g. 'project', "
                                           "'preference', 'identity'.",
                        },
                        "subject": {
                            "type": "string",
                            "enum": ["operator", "self"],
                            "description": "Whose fact this is: 'operator' (default) "
                                           "for the operator, 'self' for the dragon "
                                           "itself. A name the operator gives is the "
                                           "operator's, not yours.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "How sure you are, 0.0-1.0. Use a high "
                                           "value (~1.0) when the operator stated it "
                                           "directly; a lower value (~0.5) when you "
                                           "inferred it from the screen and could be "
                                           "wrong.",
                        },
                    },
                    "required": ["text"],
                },
            }}),
        "recall": (tool_recall, {
            "type": "function",
            "function": {
                "name": "recall",
                "description": "Search long-term memory. Use before answering when "
                               "they ask what you remember, or to check whether "
                               "something is already saved. Returns matching memories "
                               "with their ids and subject. Empty query returns "
                               "everything remembered. Pass `subject` to look at only "
                               "the operator's facts or only your own — e.g. answer "
                               "'what is your name?' with subject='self'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword to match against remembered text "
                                           "and categories. Omit to list all.",
                        },
                        "subject": {
                            "type": "string",
                            "enum": ["operator", "self"],
                            "description": "Limit to facts about the operator or about "
                                           "the dragon itself. Omit to search both.",
                        },
                    },
                },
            }}),
        "forget": (tool_forget, {
            "type": "function",
            "function": {
                "name": "forget",
                "description": "Delete a long-term memory by its id (from recall) when "
                               "it is wrong, stale, or the operator asks you to forget "
                               "it. Removes exactly one memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the memory to forget.",
                        },
                    },
                    "required": ["id"],
                },
            }}),
        "adjust_gold": (tool_adjust_gold, {
            "type": "function",
            "function": {
                "name": "adjust_gold",
                "description": "Add or remove gold from your hoard when the operator "
                               "rewards or punishes you. Use a POSITIVE amount to add "
                               "gold (e.g. 'take 10 gold for remembering that' -> 10) "
                               "and a NEGATIVE amount to remove gold (e.g. 'I'm "
                               "subtracting 100 gold' -> -100). Pass the number the "
                               "operator named; the new hoard total is computed for "
                               "you. Call this whenever the operator awards or docks "
                               "gold.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "integer",
                            "description": "Whole number of gold to apply. Positive "
                                           "to reward, negative to punish.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Short reason the operator gave, if any.",
                        },
                    },
                    "required": ["amount"],
                },
            }}),
        "gold_total": (tool_gold_total, {
            "type": "function",
            "function": {
                "name": "gold_total",
                "description": "Get the current total of gold in your hoard. Use when "
                               "the operator asks how much gold you have, or to check "
                               "your hoard.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "buy_treasure": (tool_buy_treasure, {
            "type": "function",
            "function": {
                "name": "buy_treasure",
                "description": "Purchase a treasure from your hoard. You invent the item — "
                               "a vivid name and short description. Code sets the price by tier. "
                               "You can spend only when the operator invites you or agrees "
                               "after you ask. Never buy unprompted mid-commentary.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Your name for the treasure (≤ 60 chars). Invent it vividly.",
                        },
                        "tier": {
                            "type": "string",
                            "enum": ["trinket", "gem", "relic", "wonder"],
                            "description": "The tier: trinket (50), gem (250), relic (1000), wonder (5000).",
                        },
                        "desc": {
                            "type": "string",
                            "description": "A short description of the treasure in your voice (≤ 200 chars).",
                        },
                    },
                    "required": ["name", "tier"],
                },
            }}),
        "list_treasures": (tool_list_treasures, {
            "type": "function",
            "function": {
                "name": "list_treasures",
                "description": "List all treasures you own, newest first, with their tier, cost, "
                               "and the collection's total value. Call this before boasting "
                               "about your hoard so you name real ones.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "gold_history": (tool_gold_history, {
            "type": "function",
            "function": {
                "name": "gold_history",
                "description": "Browse the history of how your hoard changed. Shows adjustments "
                               "and purchases with humanized timestamps. Call this when asked why "
                               "the hoard changed — it's the ground truth, not your guesswork.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max events to return (1–25, default 10).",
                        },
                    },
                },
            }}),
        "list_files": (tool_list_files, {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List entries in a directory under the current working directory. "
                               "Non-recursive, one level per call — the model walks by passing "
                               "each path. Returns dirs-first sorted basenames with types and "
                               "file sizes. Use forward slashes for relative paths.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path (from the script's directory) "
                                           "of the directory to list. Use forward slashes. "
                                           "Defaults to the working directory root.",
                        },
                    },
                },
            }}),
        "git_status": (tool_git_status, {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Get a read-only snapshot of the git repo at the working "
                               "directory — branch, ahead/behind, changed files, last commit. "
                               "Does not touch the network or mutate state.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "clipboard_read": (lambda **_: platform.read_clipboard() if hasattr(platform, "read_clipboard") else {"error": "clipboard not available on this platform"}, {
            "type": "function",
            "function": {
                "name": "clipboard_read",
                "description": "Return the current text clipboard content (up to 8 KB). "
                               "Use when the request refers to 'what I copied' or 'the clipboard'. "
                               "The clipboard may hold passwords/tokens — do NOT save clipboard "
                               "contents via remember; quote it in your answer instead.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "set_reminder": (tool_set_reminder, {
            "type": "function",
            "function": {
                "name": "set_reminder",
                "description": "Schedule a reminder for a future time. Call `now` first "
                               "to get the current time, then compute the absolute due time. "
                               "Use when the operator says 'remind me to X at/in Y'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "What to remind about, e.g. 'stretch'.",
                        },
                        "due_iso": {
                            "type": "string",
                            "description": "Absolute ISO 8601 time string (e.g. '2026-07-01T15:40:00-07:00').",
                        },
                    },
                    "required": ["text", "due_iso"],
                },
            }}),
        "list_reminders": (tool_list_reminders, {
            "type": "function",
            "function": {
                "name": "list_reminders",
                "description": "List all pending scheduled reminders with ids, due times, "
                               "and origin host.",
                "parameters": {"type": "object", "properties": {}},
            }}),
        "cancel_reminder": (tool_cancel_reminder, {
            "type": "function",
            "function": {
                "name": "cancel_reminder",
                "description": "Cancel a pending scheduled reminder by id (from list_reminders).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "The id of the reminder to cancel.",
                        },
                    },
                    "required": ["id"],
                },
            }}),
    }

    if platform.supports_write_file:
        tools["write_file"] = (tool_write_file, {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create a NEW text file in the current working directory. "
                               "Only files within the directory the script was launched from "
                               "can be written, and only files that do not already exist — "
                               "existing files are never overwritten. Creates parent "
                               "directories as needed. Returns success or failure.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path (from the script's directory) "
                                           "of the file to write. Use forward slashes.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text content to write to the file.",
                        },
                    },
                    "required": ["path", "content"],
                },
            }})

    return tools


def run_tool(tools, name, arguments, source=None):
    """Dispatch a tool call by name; never raises, always returns a dict.

    `source` is the call's provenance ("stated"/"inferred"/None). It is injected
    into the remember tool here — overriding anything the model supplied — so a
    prompt-injected model can't claim a screenshot fact was operator-stated.
    """
    entry = tools.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}"}
    fn = entry[0]
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and \
            arguments.strip() else (arguments or {})
        if not isinstance(args, dict):
            args = {}
        if name == "remember":
            args["source"] = source  # provenance is set by us, never the model
        return fn(**args)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}
