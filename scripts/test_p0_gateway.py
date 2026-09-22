"""End-to-end P0 acceptance checks against the isolated Higress gateway."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[dict[str, str]] = []
REQUESTS: list[dict[str, str]] = []


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    return values


def redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def call(opener, base_url: str, host: str | None, key: str | None, path: str, payload: dict | None, timeout: int) -> tuple[int, dict | str, dict]:
    headers = {"Accept": "application/json"}
    body = None
    if key:
        headers["Authorization"] = "Bearer " + key
    if host:
        headers["Host"] = host
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status, headers = response.status, dict(response.headers.items())
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status, headers = error.code, dict(error.headers.items())
    try:
        return status, json.loads(raw), headers
    except json.JSONDecodeError:
        return status, raw[:1000], headers


def call_stream(opener, base_url: str, host: str | None, key: str, payload: dict, timeout: int) -> tuple[int, bool, bool, bool, str]:
    headers = {
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    if host:
        headers["Host"] = host
    request = Request(base_url.rstrip("/") + "/v1/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                return response.status, False, False, False, ""
            saw_done = saw_text = saw_usage = False
            response_id = ""
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                event = line[5:].strip()
                if event == "[DONE]":
                    saw_done = True
                    break
                chunk = json.loads(event)
                response_id = response_id or str(chunk.get("id", ""))
                saw_usage = saw_usage or isinstance(chunk.get("usage"), dict)
                for choice in chunk.get("choices", []):
                    content = (choice.get("delta") or {}).get("content") if isinstance(choice, dict) else None
                    saw_text = saw_text or (isinstance(content, str) and bool(content.strip()))
            return response.status, saw_done, saw_text, saw_usage, response_id
    except HTTPError as error:
        error.read()
        return error.code, False, False, False, ""


def call_responses_tool_stream(opener, base_url: str, host: str | None, key: str, payload: dict, timeout: int) -> tuple[int, bool, bool, bool, str]:
    headers = {
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    if host:
        headers["Host"] = host
    request = Request(base_url.rstrip("/") + "/v1/responses", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                return response.status, False, False, False, ""
            events = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    value = line[5:].strip()
                    if value and value != "[DONE]":
                        events.append(json.loads(value))
            completed_events = [event for event in events if event.get("type") == "response.completed"]
            errors = any(event.get("type") in ("error", "response.failed") for event in events)
            response_body = completed_events[0].get("response", {}) if len(completed_events) == 1 else {}
            output = response_body.get("output", []) if isinstance(response_body, dict) else []
            calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"] if isinstance(output, list) else []
            arguments_match = False
            if len(calls) == 1 and calls[0].get("name") == payload.get("tools", [{}])[0].get("name"):
                try:
                    arguments_match = json.loads(calls[0].get("arguments", "{}")) == {"value": payload.get("input", "").split(" with value ", 1)[-1].split(".", 1)[0]}
                except (json.JSONDecodeError, AttributeError):
                    arguments_match = False
            saw_arguments_done = any(event.get("type") == "response.function_call_arguments.done" for event in events)
            usage = response_body.get("usage") if isinstance(response_body, dict) else None
            response_id = str(response_body.get("id", "")) if isinstance(response_body, dict) else ""
            ok = len(completed_events) == 1 and response_body.get("status") == "completed" and not errors and saw_arguments_done
            return response.status, ok, arguments_match, isinstance(usage, dict) and usage.get("total_tokens", 0) > 0, response_id
    except HTTPError as error:
        error.read()
        return error.code, False, False, False, ""


def check(condition: bool, message: str) -> None:
    RESULTS.append({"status": "PASS" if condition else "FAIL", "message": message})
    print(("PASS" if condition else "FAIL") + " " + message)
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.p0-test")
    parser.add_argument("--base-url", default="http://ai-gateway-test.local:18080")
    parser.add_argument("--host", help="Override the HTTP Host header; useful before the hosts entry is installed")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--verify-rate-limit", action="store_true")
    parser.add_argument("--skip-stream", action="store_true")
    parser.add_argument("--skip-responses", action="store_true")
    parser.add_argument("--skip-tools", action="store_true")
    parser.add_argument("--json-report", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    variables = load_env(args.env_file)
    key = variables.get("P0_HIGRESS_CONSUMER_KEY", "")
    legacy_key = variables.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", "")
    if not key:
        parser.error("P0_HIGRESS_CONSUMER_KEY is missing")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    secrets = (key, variables.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", ""))
    run_id = uuid.uuid4().hex
    marker_prefix = "p0accept-" + run_id
    started_at_epoch = time.time()

    try:
        status, body, _ = call(opener, args.base_url, args.host, None, "/v1/models", None, args.timeout)
        check(status == 401, "missing Consumer Key is rejected by Higress")

        status, body, _ = call(opener, args.base_url, args.host, "gw-p0-invalid", "/v1/models", None, args.timeout)
        check(status == 401, "invalid Consumer Key is rejected by Higress")

        status, body, _ = call(opener, args.base_url, args.host, key, "/v1/models", None, args.timeout)
        model_ids = {item.get("id") for item in body.get("data", [])} if isinstance(body, dict) else set()
        check(status == 200 and "my-qwen3.6-27b" in model_ids, "new Consumer Key reaches LiteLLM model directory")

        if legacy_key:
            status, body, _ = call(opener, args.base_url, args.host, legacy_key, "/v1/models", None, args.timeout)
            check(status == 200, "legacy Consumer Key remains valid during the rotation window")

        # A valid credential must not bypass the explicit Host route. The
        # unique marker is checked against SpendLogs by the outer acceptance
        # runner to prove this request never reached LiteLLM.
        wrong_host_marker = marker_prefix + "-wrong-host"
        status, _, _ = call(
            opener, args.base_url, "p0-invalid-host.invalid", key,
            "/v1/chat/completions",
            {"model": "my-qwen3.6-27b", "messages": [{"role": "user", "content": wrong_host_marker}]},
            args.timeout,
        )
        REQUESTS.append({"endpoint": "wrong-host", "marker": wrong_host_marker, "response_id": "", "http_status": str(status)})
        check(status == 404, "valid Consumer Key with an unknown Host is rejected before upstream routing")

        direct_url = "http://127.0.0.1:14000"
        status, _, _ = call(opener, direct_url, None, key, "/v1/models", None, args.timeout)
        check(status == 401, "Higress Consumer Key cannot authenticate directly to the local LiteLLM port")
        status, _, _ = call(opener, direct_url, None, None, "/v1/models", None, args.timeout)
        check(status == 401, "LiteLLM direct port still requires its internal Master Key")

        chat_marker = marker_prefix + "-chat"
        request = {
            "model": "my-qwen3.6-27b",
            "messages": [{"role": "user", "content": "只回复：P0_OK"}],
            "user": chat_marker,
            "temperature": 0,
            # Qwen3.6 may emit reasoning tokens before visible text; 32 can
            # finish the response before a user-visible completion is emitted.
            "max_tokens": 512,
        }
        status, body, _ = call(opener, args.base_url, args.host, key, "/v1/chat/completions", request, args.timeout)
        choices = body.get("choices", []) if isinstance(body, dict) else []
        content = choices[0].get("message", {}).get("content") if choices and isinstance(choices[0], dict) else None
        usage = body.get("usage") if isinstance(body, dict) else None
        response_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        REQUESTS.append({"endpoint": "chat", "marker": chat_marker, "response_id": response_id, "http_status": str(status)})
        check(status == 200 and isinstance(content, str) and content.strip(),
              f"Higress -> LiteLLM -> internal Qwen3.6-27B returns text (HTTP {status}, content={type(content).__name__})")
        check(isinstance(usage, dict), "model response contains usage for LiteLLM SpendLogs")

        if not args.skip_stream:
            stream_marker = marker_prefix + "-stream"
            stream_status, stream_done, stream_text, stream_usage, stream_id = call_stream(
                opener, args.base_url, args.host, key,
                {**request, "user": stream_marker, "messages": [{"role": "user", "content": "只回复：P0_OK"}],
                 "stream": True, "stream_options": {"include_usage": True}}, args.timeout,
            )
            REQUESTS.append({"endpoint": "stream", "marker": stream_marker, "response_id": stream_id, "http_status": str(stream_status)})
            check(stream_status == 200 and stream_done and stream_text,
                  f"streaming Qwen response returns visible text and [DONE] (HTTP {stream_status}, done={stream_done}, text={stream_text}, usage={stream_usage})")
            check(stream_usage, "streaming response includes usage")

        if not args.skip_responses:
            responses_marker = marker_prefix + "-responses"
            status, body, _ = call(opener, args.base_url, args.host, key, "/v1/responses", {
                "model": "my-qwen3.6-27b",
                "input": f"Reply with this marker only: {responses_marker}",
                "max_output_tokens": 128,
            }, args.timeout)
            output_text = body.get("output_text", "") if isinstance(body, dict) else ""
            if isinstance(body, dict) and not output_text:
                output_text_parts = []
                output_items = body.get("output", [])
                if isinstance(output_items, list):
                    for output_item in output_items:
                        content_items = output_item.get("content", []) if isinstance(output_item, dict) else []
                        if isinstance(content_items, list):
                            output_text_parts.extend(
                                item["text"] for item in content_items
                                if isinstance(item, dict) and isinstance(item.get("text"), str)
                            )
                output_text = " ".join(output_text_parts)
            responses_usage = body.get("usage") if isinstance(body, dict) else None
            responses_id = str(body.get("id", "")) if isinstance(body, dict) else ""
            REQUESTS.append({"endpoint": "responses", "marker": responses_marker, "response_id": responses_id, "http_status": str(status)})
            check(status == 200 and isinstance(output_text, str) and bool(output_text.strip()),
                  f"Qwen Responses API returns visible output (HTTP {status})")
            check(isinstance(responses_usage, dict), "Responses API returns usage")
        else:
            RESULTS.append({"status": "SKIP", "message": "Responses API was explicitly skipped"})
            print("SKIP Responses API")

        if not args.skip_tools:
            tool_marker = marker_prefix + "-tool"
            tool_payload = {
                "model": "my-qwen3.6-27b",
                "stream": True,
                "input": f"Call gateway_probe with value {tool_marker}. Do not answer in text.",
                "tools": [{
                    "type": "function", "name": "gateway_probe",
                    "description": "Gateway test probe",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"], "additionalProperties": False,
                    },
                }],
                "tool_choice": "auto",
            }
            status, completed, has_tool_call, tool_usage, tool_id = call_responses_tool_stream(
                opener, args.base_url, args.host, key, tool_payload, args.timeout,
            )
            REQUESTS.append({"endpoint": "responses-tool-call", "marker": tool_marker, "response_id": tool_id, "http_status": str(status)})
            check(status == 200 and completed and has_tool_call,
                  f"Qwen Responses streaming completes an automatic gateway_probe tool call (HTTP {status})")
            check(tool_usage, "Responses automatic tool-call stream returns nonzero usage")
        else:
            RESULTS.append({"status": "SKIP", "message": "Responses tool-call test was explicitly skipped"})
            print("SKIP Responses tool-call test")

        if args.verify_rate_limit:
            # The chat request above consumed one of the ten RPM slots. This
            # loop exercises only /models afterwards, avoiding extra model cost.
            limited = False
            for _ in range(10):
                status, body, headers = call(opener, args.base_url, args.host, key, "/v1/models", None, args.timeout)
                if status == 429:
                    limited = True
                    break
            check(limited, "per-Consumer 10 RPM Higress limit returns HTTP 429")
        else:
            print("SKIP rate-limit exhaustion check (run with -VerifyRateLimit when desired)")
    except (AssertionError, URLError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print("P0 acceptance failed: " + redact(str(error), secrets), file=sys.stderr)
        return 1
    finally:
        if args.json_report:
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            args.json_report.write_text(json.dumps({
                "run_id": run_id,
                "model": "my-qwen3.6-27b",
                "started_at_epoch": started_at_epoch,
                "requests": REQUESTS,
                "results": RESULTS,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
