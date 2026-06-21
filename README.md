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

## Development

```
pip install -r requirements-dev.txt
pytest
```

The tests cover the platform-independent logic in `audiencelib/core.py`
(path confinement, memory store, dream consolidation, health evaluation, text
helpers) and run on any OS — they use `tests/fake_platform.FakePlatform`
instead of a real macOS/Windows host.
