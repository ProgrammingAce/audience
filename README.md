# audience

A local-LLM shoulder-surfer: a curses TUI that periodically screenshots your
active window and asks a local llama.cpp vision model for brief, in-character
commentary. You can also type questions about what's on screen.

## Architecture

- `audience.py` — cross-platform entrypoint; selects the OS-specific platform.
- `audiencelib/core.py` — all OS-agnostic logic (UI, scheduling, memory, tools,
  model client).
- `audiencelib/platform_base.py` — the `Platform` interface every OS implements.
- `audiencelib/platform_macos.py`, `audiencelib/platform_windows.py` — the
  OS-specific probes (capture, idle/window detection, system stats).

## Install

Install the dependencies for your OS:

```
pip install -r requirements-macos.txt      # macOS
pip install -r requirements-windows.txt    # Windows
```

If a dependency is missing, `audience.py` exits with a message pointing at the
right requirements file rather than a raw traceback.

## Running

Start a llama.cpp server first, then:

```
python3 audience.py
python3 audience.py --interval 30 --url http://localhost:8080/v1/chat/completions
```

## Sharing memory across machines

Point `--memory-dir` at a synced folder (e.g. Dropbox) and run the app on each
machine:

```
python3 audience.py --memory-dir ~/Dropbox/.audience_memory
```

Each install writes only to files suffixed with its own hostname
(`long_term.<host>.jsonl`, `short_term.<host>.jsonl`, `gold.<host>.jsonl`,
`tombstones.<host>.jsonl`), so two machines running at once never write the same
file and the sync service never has to make a "conflicted copy". Reads union
every machine's shard, keyed by each entry's content-hash id (so a fact written
on both machines collapses to one). Forgetting and dream consolidation record
tombstones rather than rewriting a peer's file, and gold is an append-only delta
ledger summed across machines. The store is eventually-consistent: a peer's
changes appear once the folder syncs them in.

## Development

```
pip install -r requirements-dev.txt
pytest
```

The tests cover the platform-independent logic in `audiencelib/core.py`
(path confinement, memory store, dream consolidation, health evaluation, text
helpers) and run on any OS — they use `tests/fake_platform.FakePlatform`
instead of a real macOS/Windows host.
