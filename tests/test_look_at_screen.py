"""look_at_screen: the captured PNG is injected as an image user turn, and
side-effecting tools are withdrawn once the screen is in the conversation."""

import base64
import json

from audiencelib import llm
from audiencelib.tools import build_tools, run_tool

from .fake_platform import FakePlatform


def test_tool_look_at_screen_returns_sentinel_png():
    tools = build_tools(FakePlatform(png=b"PNGBYTES"))
    result = run_tool(tools, "look_at_screen", "{}")
    assert result["_screenshot_png_b64"] == base64.b64encode(b"PNGBYTES").decode()


def test_tool_look_at_screen_handles_capture_failure():
    tools = build_tools(FakePlatform(png=None))
    result = run_tool(tools, "look_at_screen", "{}")
    assert "error" in result and "_screenshot_png_b64" not in result


def _sse(*chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    import io
    return io.BytesIO(body.encode())


def _delta(content=None, tool_calls=None, finish=None):
    d = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {"choices": [{"delta": d, "finish_reason": finish}]}


def _tools():
    png = b"\x89PNG\r\n\x1a\nFAKE"
    return {
        "look_at_screen": (
            lambda **_: {"_screenshot_png_b64": base64.b64encode(png).decode()},
            {"type": "function", "function": {"name": "look_at_screen"}}),
        # a side-effecting tool that must vanish once a screenshot lands
        "write_file": (lambda **_: {"ok": True},
                       {"type": "function", "function": {"name": "write_file"}}),
    }, base64.b64encode(png).decode()


def test_look_at_screen_injects_image_and_strips_side_effects(monkeypatch):
    turns = [
        _sse(_delta(tool_calls=[{"index": 0, "id": "c1",
                                 "function": {"name": "look_at_screen",
                                              "arguments": "{}"}}],
                    finish="tool_calls")),
        _sse(_delta(content="A dragon-worthy desktop.", finish="stop")),
    ]
    payloads = []

    def fake_urlopen(req, timeout=None):
        payloads.append(json.loads(req.data.decode()))

        class _Ctx:
            def __enter__(self_):
                return turns.pop(0)
            def __exit__(self_, *a):
                return False
        return _Ctx()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    tools, png_b64 = _tools()
    answer = llm.ask_model("http://test", None, "what's on my screen?",
                           "sys", tools)
    assert answer == "A dragon-worthy desktop."

    # First request advertised both tools (untrusted turn hadn't started).
    first_tool_names = {t["function"]["name"] for t in payloads[0]["tools"]}
    assert {"look_at_screen", "write_file"} <= first_tool_names

    # Second request: the screenshot was injected as an image user turn...
    msgs = payloads[1]["messages"]
    image_turns = [m for m in msgs if isinstance(m.get("content"), list)
                   and any(p.get("type") == "image_url" for p in m["content"])]
    assert len(image_turns) == 1
    assert png_b64 in image_turns[0]["content"][0]["image_url"]["url"]

    # ...and side-effecting tools were withdrawn.
    second_tool_names = {t["function"]["name"] for t in payloads[1]["tools"]}
    assert "write_file" not in second_tool_names
    assert "look_at_screen" in second_tool_names

    # The raw base64 never leaked into the text tool-result channel.
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert any(json.loads(m["content"]) == {"ok": "screen captured"}
               for m in tool_msgs)
