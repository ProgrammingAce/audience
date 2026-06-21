"""Tests for ask_model's streamed (SSE) transport: content is assembled and
forwarded to on_delta, and fragmented tool_calls are reassembled and run."""

import io
import json

from audiencelib import llm


def _sse(*chunks):
    """Render chat-completion chunks as an SSE byte stream, [DONE]-terminated."""
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return io.BytesIO(body.encode())


def _delta(content=None, tool_calls=None, finish=None):
    d = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {"choices": [{"delta": d, "finish_reason": finish}]}


def test_read_stream_assembles_content_and_calls_on_delta():
    resp = _sse(_delta(content="Half "), _delta(content="past "),
                _delta(content="dragon.", finish="stop"))
    seen = []
    msg, finish = llm._read_stream(resp, seen.append)
    assert msg["content"] == "Half past dragon."
    assert seen == ["Half ", "past ", "dragon."]
    assert finish == "stop"
    assert "tool_calls" not in msg


def test_read_stream_reassembles_fragmented_tool_calls():
    # name arrives whole in the first fragment; arguments stream in pieces.
    resp = _sse(
        _delta(tool_calls=[{"index": 0, "id": "call_x",
                            "function": {"name": "now", "arguments": ""}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"tz":'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": ' "utc"}'}}],
               finish="tool_calls"),
    )
    on_delta_calls = []
    msg, finish = llm._read_stream(resp, on_delta_calls.append)
    assert finish == "tool_calls"
    assert on_delta_calls == []  # tool-call turns carry no content
    assert msg["tool_calls"] == [{
        "id": "call_x", "type": "function",
        "function": {"name": "now", "arguments": '{"tz": "utc"}'}}]


def test_read_stream_synthesizes_missing_tool_call_id():
    resp = _sse(_delta(tool_calls=[{"index": 0,
                                    "function": {"name": "now", "arguments": "{}"}}],
                       finish="tool_calls"))
    msg, _ = llm._read_stream(resp, None)
    assert msg["tool_calls"][0]["id"] == "call_0"


def test_ask_model_runs_streamed_tool_call_then_answers(monkeypatch):
    # First turn streams a tool call; second turn streams the final answer.
    turns = [
        _sse(_delta(tool_calls=[{"index": 0, "id": "c1",
                                 "function": {"name": "ping", "arguments": "{}"}}],
                    finish="tool_calls")),
        _sse(_delta(content="pong!", finish="stop")),
    ]

    def fake_urlopen(req, timeout=None):
        class _Ctx:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self.body
            def __exit__(self, *a):
                return False
        return _Ctx(turns.pop(0))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    ran = []
    tools = {"ping": (lambda **kw: ran.append(kw) or {"ok": True},
                      {"type": "function", "function": {"name": "ping"}})}

    streamed = []
    answer = llm.ask_model("http://test", None, "hi", "sys", tools,
                           on_delta=streamed.append)
    assert answer == "pong!"
    assert streamed == ["pong!"]   # only the final turn streams content
    assert ran == [{}]             # the tool actually ran
