"""The model call: a bounded tool-calling loop against a llama.cpp
OpenAI-compatible chat endpoint."""

import base64
import json
import urllib.error
import urllib.request

from .tools import run_tool, SIDE_EFFECTING_TOOLS


# --------------------------------------------------------------------------
# JSON-schema response formats for dream / reflect (strict, no prose).
# --------------------------------------------------------------------------

DREAM_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "dream",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "subject": {"type": "string", "enum": ["operator", "self"]},
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["category", "subject", "text", "confidence"],
                        "additionalProperties": False,
                    },
                },
                "episode": {
                    "type": ["string", "null"],
                    "description": "One-line summary ≤25 words of what the operator "
                    "was doing/struggling with this session, or null if nothing "
                    "notable."
                },
            },
            "required": ["memories"],
            "additionalProperties": False,
        },
    },
}

REFLECT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "reflect",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["text", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["memories"],
            "additionalProperties": False,
        },
    },
}


# --------------------------------------------------------------------------
# Model call
# --------------------------------------------------------------------------
def _read_stream(resp, on_delta):
    """Consume a streamed (SSE) chat completion into a single assistant message.

    llama.cpp emits `data: {...}` lines, one JSON chunk each, terminated by
    `data: [DONE]`. We accumulate the answer text (calling on_delta with each
    new piece for live display), any reasoning_content, and — since tool calls
    also arrive fragmented — reassemble tool_calls from their per-index deltas
    (partial id / function.name / function.arguments). Returns
    (message_dict, finish_reason) shaped like the non-streamed message.
    """
    content_parts, reasoning_parts = [], []
    tool_calls = {}   # index -> {id, type, function: {name, arguments}}
    finish_reason = None
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        piece = delta.get("content")
        if piece:
            content_parts.append(piece)
            if on_delta is not None:
                on_delta(piece)
        rpiece = delta.get("reasoning_content")
        if rpiece:
            reasoning_parts.append(rpiece)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(
                idx, {"id": None, "type": "function",
                      "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    msg = {"content": "".join(content_parts),
           "reasoning_content": "".join(reasoning_parts)}
    if tool_calls:
        calls = []
        for idx in sorted(tool_calls):
            call = tool_calls[idx]
            if not call["id"]:
                call["id"] = f"call_{idx}"  # some servers omit ids when streaming
            calls.append(call)
        msg["tool_calls"] = calls
    return msg, finish_reason


def ask_model(url, image_bytes, question, system, tools, max_tokens=450,
              source=None, on_delta=None, temperature=0.7,
              response_format=None):
    # image_bytes is optional: typed questions are sent as plain text, while
    # the periodic commentary attaches a fresh screenshot. `source` is the call
    # provenance, threaded to run_tool so the remember tool clamps confidence.
    # on_delta, when given, is called with each chunk of answer text as it
    # streams, so callers can paint the reply live instead of after the last
    # token. It is only invoked on the final answer turn, never for tool-call
    # turns (which carry no content).
    if image_bytes is not None:
        b64 = base64.b64encode(image_bytes).decode()
        content = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": question},
        ]
    else:
        content = question

    # A request carrying a screenshot is untrusted: text on the captured screen
    # could try to prompt-inject the model into acting. Strip side-effecting
    # tools so a screenshot can never lead to a file write, memory edit, gold
    # change, etc. — only read-only grounding tools survive.
    image_present = image_bytes is not None

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]

    def advertised_schemas():
        # Recomputed whenever image_present flips: once any screenshot is in the
        # conversation (attached or pulled in via look_at_screen), the turn is
        # untrusted and side-effecting tools are withdrawn.
        return [schema for name, (_, schema) in tools.items()
                if not (image_present and name in SIDE_EFFECTING_TOOLS)]

    schemas = advertised_schemas()

    # Tool-calling loop: the model may ask for one or more read-only local
    # facts (window title, time, battery, now-playing) before answering. We run
    # the requested tools, feed the results back, and ask again — bounded so a
    # confused model can't loop forever.
    for _ in range(4):
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            # The custom chat template (gemma_fixed.jinja) supports thinking,
            # and llama-server defaults enable_thinking to ON for such
            # templates. Left unset, every reply burns its token budget on
            # reasoning; this must stay explicitly off.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            payload["response_format"] = response_format
        # Only advertise tools when there are some — a dream call passes none, and
        # some servers reject an empty tools array paired with tool_choice.
        if schemas:
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                msg, finish_reason = _read_stream(resp, on_delta)
        except urllib.error.HTTPError as e:
            # Some servers don't support response_format — fall back once.
            if (response_format is not None
                    and e.code is not None
                    and "response_format" in (e.read().decode("utf-8", "replace")
                                              + e.reason)):
                response_format = None
                payload.pop("response_format", None)
                req = urllib.request.Request(
                    url, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    msg, finish_reason = _read_stream(resp, on_delta)
            else:
                raise

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            # Echo the assistant turn (with its tool_calls) then append one
            # tool result per call, keyed by id.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            pending_shots = []
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                # Belt-and-suspenders: even though side-effecting tools aren't
                # advertised on screenshot requests, never run one if the model
                # asks anyway. Keeps a prompt-injected screenshot inert.
                if image_present and name in SIDE_EFFECTING_TOOLS:
                    result = {"error": f"{name} is disabled while a screenshot "
                                       "is attached"}
                else:
                    result = run_tool(tools, name, fn.get("arguments", ""),
                                      source=source)
                # look_at_screen returns the PNG via a sentinel: ack it in the
                # text channel, but route the actual image into a user turn below
                # so the multimodal model can see it.
                shot = result.pop("_screenshot_png_b64", None) \
                    if isinstance(result, dict) else None
                if shot is not None:
                    pending_shots.append(shot)
                    result = {"ok": "screen captured"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result),
                })
            if pending_shots:
                # Feed the captured screen back as an image-bearing user turn.
                parts = [{"type": "image_url",
                          "image_url": {"url": f"data:image/png;base64,{b}"}}
                         for b in pending_shots]
                parts.append({"type": "text",
                              "text": "Here is the screen you asked to see."})
                messages.append({"role": "user", "content": parts})
                # The conversation is now untrusted — withdraw side-effecting
                # tools for the remaining rounds.
                image_present = True
                schemas = advertised_schemas()
            continue  # ask again now that the model has its facts

        content = (msg.get("content") or "").strip()
        if content:
            return content
        # Reasoning model: answer may live in reasoning_content. If it got cut
        # off mid-thought, surface what we have rather than a blank line.
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            if finish_reason == "length":
                return "(model ran out of tokens while thinking) " + reasoning
            return reasoning
        return "(model returned no content)"

    return "(model kept calling tools without answering)"
