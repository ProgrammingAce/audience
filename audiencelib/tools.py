"""File tools and the per-platform tool registry.

Memory/gold tools live in :mod:`audiencelib.memory`; this module owns
the working-directory-confined file tools and wires every tool
(including the platform probes) into the registry build_tools returns.
"""

import json
import os

from .memory import (
    tool_remember, tool_recall, tool_forget,
    tool_adjust_gold, tool_gold_total,
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
                "description": "Save a durable, useful fact about the operator to "
                               "long-term memory so you recall it in future sessions "
                               "(e.g. their name, what they're building, tools they "
                               "favor, a stated preference). One crisp fact per call. "
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
                "description": "Search long-term memory for facts about the operator. "
                               "Use before answering when they ask what you remember, "
                               "or to check whether something is already saved. Returns "
                               "matching memories with their ids. Empty query returns "
                               "everything remembered.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword to match against remembered text "
                                           "and categories. Omit to list all.",
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
