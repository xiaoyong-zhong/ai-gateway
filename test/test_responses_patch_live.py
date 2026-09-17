"""Opt-in live smoke test; makes five requests to the configured gateway model."""

import argparse
import json
import os
from urllib.request import Request, urlopen


def request(base, key, path, body):
    req = Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=90) as response:
        if not body.get("stream"):
            return json.load(response)
        events = []
        done = False
        for line in response:
            line = line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
            elif data:
                events.append(json.loads(data))
        return events, done


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://192.168.31.113:8080/v1")
    parser.add_argument("--model", default="my-qwen3.6-27b")
    args = parser.parse_args()
    key = os.environ["GATEWAY_API_KEY"]
    common = {"model": args.model}
    prompt = "Reply exactly GATEWAY_OK."

    for stream in (False, True):
        result = request(args.base_url, key, "/chat/completions", {
            **common, "messages": [{"role": "user", "content": prompt}], "stream": stream,
            **({"stream_options": {"include_usage": True}} if stream else {}),
        })
        if stream:
            events, done = result
            assert done, "Chat stream missing [DONE]"
            assert not any("error" in event for event in events), "Chat stream error"
            text = "".join(choice.get("delta", {}).get("content") or ""
                           for event in events for choice in event.get("choices", []))
            assert any(choice.get("finish_reason") for event in events for choice in event.get("choices", []))
            usage = next((event["usage"] for event in reversed(events) if event.get("usage")), None)
        else:
            text = result["choices"][0]["message"]["content"]
            usage = result.get("usage")
        assert text.strip() == "GATEWAY_OK", text
        assert usage and usage.get("total_tokens", 0) > 0, "Chat usage missing"
        print(json.dumps({"path": "chat/completions", "stream": stream, "ok": True, "usage": usage}))

    for stream in (False, True):
        result = request(args.base_url, key, "/responses", {
            **common, "instructions": "Follow the user's requested response format.",
            "input": [{"role": "developer", "content": [{"type": "input_text", "text": "Be concise."}]},
                      {"role": "user", "content": prompt}],
            "stream": stream,
        })
        if stream:
            events, _ = result
            completed = [e for e in events if e.get("type") == "response.completed"]
            assert len(completed) == 1, "Responses stream missing unique response.completed"
            assert not any(e.get("type") in ("error", "response.failed") for e in events)
            result = completed[0]["response"]
            deltas = "".join(e["delta"] for e in events if e.get("type") == "response.output_text.delta")
            assert deltas.strip() == "GATEWAY_OK", deltas
        text = "".join(part.get("text", "") for item in result["output"] if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text")
        assert text.strip() == "GATEWAY_OK", text
        assert result["status"] == "completed", result["status"]
        assert result["usage"]["total_tokens"] > 0
        print(json.dumps({"path": "responses", "stream": stream, "ok": True, "usage": result["usage"]}))

    events, _ = request(args.base_url, key, "/responses", {
        **common, "stream": True,
        "input": "Call gateway_probe with value GATEWAY_OK. Do not answer in text.",
        "tools": [{"type": "function", "name": "gateway_probe", "description": "Gateway test probe",
                   "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                  "required": ["value"], "additionalProperties": False}}],
        "tool_choice": "auto",
    })
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(completed) == 1, "Tool stream missing response.completed"
    calls = [item for item in completed[0]["response"]["output"] if item["type"] == "function_call"]
    assert len(calls) == 1 and calls[0]["name"] == "gateway_probe", calls
    assert json.loads(calls[0]["arguments"]) == {"value": "GATEWAY_OK"}
    assert any(e.get("type") == "response.function_call_arguments.done" for e in events)
    print(json.dumps({"path": "responses", "stream": True, "tool_choice": "auto", "ok": True}))


if __name__ == "__main__":
    main()
