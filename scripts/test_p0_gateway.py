"""End-to-end P0 acceptance checks against the isolated Higress gateway."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


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


def call_stream(opener, base_url: str, host: str | None, key: str, payload: dict, timeout: int) -> tuple[int, bool, bool]:
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
                return response.status, False, False
            saw_done = saw_text = saw_usage = False
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                event = line[5:].strip()
                if event == "[DONE]":
                    saw_done = True
                    break
                chunk = json.loads(event)
                saw_usage = saw_usage or isinstance(chunk.get("usage"), dict)
                for choice in chunk.get("choices", []):
                    content = (choice.get("delta") or {}).get("content") if isinstance(choice, dict) else None
                    saw_text = saw_text or (isinstance(content, str) and bool(content.strip()))
            return response.status, saw_done and saw_text, saw_usage
    except HTTPError as error:
        error.read()
        return error.code, False, False


def check(condition: bool, message: str) -> None:
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

        request = {
            "model": "my-qwen3.6-27b",
            "messages": [{"role": "user", "content": "只回复：P0_OK"}],
            "temperature": 0,
            # Qwen3.6 may emit reasoning tokens before visible text; 32 can
            # finish the response before a user-visible completion is emitted.
            "max_tokens": 512,
        }
        status, body, _ = call(opener, args.base_url, args.host, key, "/v1/chat/completions", request, args.timeout)
        choices = body.get("choices", []) if isinstance(body, dict) else []
        content = choices[0].get("message", {}).get("content") if choices and isinstance(choices[0], dict) else None
        usage = body.get("usage") if isinstance(body, dict) else None
        check(status == 200 and isinstance(content, str) and content.strip(),
              f"Higress -> LiteLLM -> internal Qwen3.6-27B returns text (HTTP {status}, content={type(content).__name__})")
        check(isinstance(usage, dict), "model response contains usage for LiteLLM SpendLogs")

        if not args.skip_stream:
            stream_status, stream_text, stream_usage = call_stream(
                opener, args.base_url, args.host, key,
                {**request, "stream": True, "stream_options": {"include_usage": True}}, args.timeout,
            )
            check(stream_status == 200 and stream_text,
                  f"streaming Qwen response ends with visible text (HTTP {stream_status})")
            check(stream_usage, "streaming response includes usage")

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
    except (AssertionError, URLError, OSError) as error:
        print("P0 acceptance failed: " + redact(str(error), secrets), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
