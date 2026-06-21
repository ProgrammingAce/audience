"""The model call: a bounded tool-calling loop against a llama.cpp
OpenAI-compatible chat endpoint."""

import base64
import json
import urllib.request

from .tools import run_tool, SIDE_EFFECTING_TOOLS


# --------------------------------------------------------------------------
# Model call
# --------------------------------------------------------------------------
def ask_model(url, image_bytes, question, system, tools, max_tokens=450,
              source=None):
    # image_bytes is optional: typed questions are sent as plain text, while
    # the periodic commentary attaches a fresh screenshot. `source` is the call
    # provenance, threaded to run_tool so the remember tool clamps confidence.
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
    schemas = [schema for name, (_, schema) in tools.items()
               if not (image_present and name in SIDE_EFFECTING_TOOLS)]

    # Tool-calling loop: the model may ask for one or more read-only local
    # facts (window title, time, battery, now-playing) before answering. We run
    # the requested tools, feed the results back, and ask again — bounded so a
    # confused model can't loop forever.
    for _ in range(4):
        payload = {
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max_tokens,
            "stream": False,
            # Skip the reasoning phase: ~10x faster and content lands directly
            # in the message instead of reasoning_content. Honored by the
            # server's jinja chat template.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        # Only advertise tools when there are some — a dream call passes none, and
        # some servers reject an empty tools array paired with tool_choice.
        if schemas:
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        choice = data["choices"][0]
        msg = choice["message"]

        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            # Echo the assistant turn (with its tool_calls) then append one
            # tool result per call, keyed by id.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
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
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result),
                })
            continue  # ask again now that the model has its facts

        content = (msg.get("content") or "").strip()
        if content:
            return content
        # Reasoning model: answer may live in reasoning_content. If it got cut
        # off mid-thought, surface what we have rather than a blank line.
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            if choice.get("finish_reason") == "length":
                return "(model ran out of tokens while thinking) " + reasoning
            return reasoning
        return "(model returned no content)"

    return "(model kept calling tools without answering)"
