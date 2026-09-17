"""网关异常与聊天并发测试。真实调用可能产生费用；默认只运行异常用例。"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from test_gateway_models import sse_data


def read_chat(response, stream, started, diagnostics=None):
    if not stream:
        result = json.load(response)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("Invalid chat response or API error")
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("Missing chat choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        finish_reason = choices[0].get("finish_reason")
        if not isinstance(content, str) or not content.strip():
            if diagnostics is not None:
                # 只记录结构和数量，不把回答、推理正文或工具参数写入压测报告。
                diagnostics.update({
                    "finish_reason": str(finish_reason),
                    "content_type": type(content).__name__,
                    "content_chars": len(content) if isinstance(content, str) else None,
                    "message_type": type(message).__name__,
                })
                for field in ("id", "model"):
                    if isinstance(result.get(field), str):
                        diagnostics["response_" + field] = result[field]
                if isinstance(message, dict):
                    diagnostics["content_present"] = "content" in message
                    for field in ("reasoning_content", "reasoning", "refusal"):
                        value = message.get(field)
                        diagnostics[field + "_type"] = type(value).__name__
                        diagnostics[field + "_chars"] = len(value) if isinstance(value, str) else None
                    calls = message.get("tool_calls")
                    diagnostics["tool_calls_count"] = len(calls) if isinstance(calls, list) else 0
                usage = result.get("usage")
                if isinstance(usage, dict):
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        if type(usage.get(field)) is int:
                            diagnostics[field] = usage[field]
                    details = usage.get("completion_tokens_details")
                    if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                        diagnostics["reasoning_tokens"] = details["reasoning_tokens"]
            hint = ("Output was truncated; check the output token budget." if finish_reason == "length"
                    else "Inspect response_diagnostics and request_id; token exhaustion is not established.")
            raise ValueError("Missing text answer; finish_reason={!r}. {}".format(finish_reason, hint))
        return {"text_chars": len(content), "first_text_s": None}

    if response.headers.get_content_type() != "text/event-stream":
        raise ValueError("Expected text/event-stream")
    text_chars, first_text, finished, done = 0, None, False, False
    for data in sse_data(response):
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise ValueError("Invalid SSE chunk or API error")
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise ValueError("Missing SSE choices")
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError("Invalid SSE choice")
            if choice.get("index") != 0:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("Invalid SSE delta")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("Invalid SSE content")
            if content and content.strip():
                if first_text is None:
                    first_text = time.monotonic() - started
                text_chars += len(content)
            finished = finished or bool(choice.get("finish_reason"))
    if not done or not finished or not text_chars:
        raise ValueError("Incomplete SSE response: text, finish_reason and [DONE] required")
    return {"text_chars": text_chars, "first_text_s": first_text}


def perform(base_url, key, payload, timeout, expected, stream=False, secrets=()):
    started = time.monotonic()
    result = {"status": None, "passed": False, "error": "", "server": "", "request_id": ""}

    def clean(value):
        value = str(value)
        for secret in (key, *secrets):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return " ".join(value.split())[:600]

    def consume(response):
        result["status"] = response.code
        result["server"] = clean(response.headers.get("Server", ""))
        result["request_id"] = clean(response.headers.get("x-request-id", "") or
                                     response.headers.get("x-litellm-call-id", ""))
        if response.code != 200:
            result["error"] = clean(response.read(65536).decode("utf-8", errors="replace"))
        if "envoy" not in result["server"].lower():
            raise ValueError("Expected Higress/Envoy response; use gateway port 8080")
        if response.code not in expected:
            raise ValueError("Unexpected HTTP {} (expected {}): {}".format(
                response.code, sorted(expected), result["error"]))
        if response.code == 200:
            diagnostics = {}
            try:
                result.update(read_chat(response, stream, started, diagnostics))
            finally:
                if diagnostics:
                    result["response_diagnostics"] = {
                        field: clean(value) if isinstance(value, str) else value
                        for field, value in diagnostics.items()
                    }
        result["passed"] = True

    try:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        req = Request(base_url + "/chat/completions", data=body, headers=headers)
        # 每个工作线程独立连接，不共享 opener；客户端不自动重试。
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as response:
                consume(response)
        except HTTPError as exc:
            with exc:
                consume(exc)
    except (URLError, OSError, HTTPException, ValueError, RuntimeError) as exc:
        result["error"] = clean(exc)
        result["error_type"] = type(exc).__name__
    result["latency_s"] = time.monotonic() - started
    return result


def latency_summary(values):
    if not values:
        return None
    values = sorted(values)
    return {"count": len(values), "mean_s": sum(values) / len(values),
            "p50_s": values[math.ceil(len(values) * 0.50) - 1],
            "p95_s": values[math.ceil(len(values) * 0.95) - 1], "max_s": values[-1]}


def show(name, result):
    print("{} {} | HTTP {} | {:.2f}s{}".format(
        "PASS" if result["passed"] else "FAIL", name, result["status"], result["latency_s"],
        " | " + result["error"] if result["error"] else ""), flush=True)
    if result.get("response_diagnostics"):
        print("  response_diagnostics: " + json.dumps(result["response_diagnostics"], ensure_ascii=False), flush=True)
    if not result["passed"] and result.get("request_id"):
        print("  request_id: " + result["request_id"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="已确认可用的聊天模型别名")
    parser.add_argument("--suite", choices=("errors", "load", "all"), default="errors")
    parser.add_argument("--base-url", default="http://localhost:8080/v1")
    parser.add_argument("--concurrency", type=int, default=2, help="最大同时请求数，默认2")
    parser.add_argument("--requests", type=int, default=6, help="并发阶段总请求数，默认6")
    parser.add_argument("--stream", action="store_true", help="并发阶段测试SSE，异常用例始终非流式")
    parser.add_argument("--timeout", type=float, default=60, help="连接及读取等待超时秒数，不是总时长上限")
    parser.add_argument("--max-tokens", type=int, default=512, help="输出预算，默认512；思考模型可能需更高预算")
    parser.add_argument("--prompt", default="Reply only OK.")
    parser.add_argument("--report-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "runtime" / "gateway-checks")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        parser.error("--base-url must be an HTTP(S) URL without credentials, query or fragment")
    if min(args.concurrency, args.requests, args.max_tokens) <= 0:
        parser.error("--concurrency, --requests and --max-tokens must be positive")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    key = os.environ.get("GATEWAY_API_KEY", "sk-local-test").strip()
    if not key:
        parser.error("GATEWAY_API_KEY is empty")
    payload = {"model": args.model, "messages": [{"role": "user", "content": args.prompt}],
               "stream": False, "max_tokens": args.max_tokens}
    report = {"model": args.model, "base_url": base_url, "suite": args.suite,
              "stream": args.stream, "timeout_s": args.timeout, "max_tokens": args.max_tokens,
              "errors": [], "load": []}
    print("Route: Higress -> LiteLLM -> model | " + base_url, flush=True)
    baseline = perform(base_url, key, payload, args.timeout, {200})
    report["baseline"] = baseline
    show("baseline (not included in load metrics)", baseline)
    if not baseline["passed"]:
        print("Baseline failed; fix model/key/connectivity before running further cases.", flush=True)
    else:
        if args.suite in ("errors", "all"):
            cases = [
                ("missing-key", None, payload, {401, 403}),
                ("invalid-key", "sk-invalid-" + uuid4().hex, payload, {401, 403}),
                ("unknown-model", key, {**payload, "model": "missing-" + uuid4().hex}, {400, 404}),
                ("missing-model", key, {k: v for k, v in payload.items() if k != "model"}, {400, 422}),
                ("missing-messages", key, {k: v for k, v in payload.items() if k != "messages"}, {400, 422}),
                ("invalid-messages-type", key, {**payload, "messages": "not-an-array"}, {400, 422}),
                ("malformed-json", key, b'{"model":', {400, 422}),
                ("empty-body", key, b"", {400, 422}),
            ]
            for name, case_key, body, expected in cases:
                result = perform(base_url, case_key, body, args.timeout, expected, secrets=(key,))
                result.update({"case": name, "expected_status": sorted(expected)})
                report["errors"].append(result)
                show(name, result)
        if args.suite in ("load", "all"):
            load_payload = {**payload, "stream": args.stream}
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=min(args.concurrency, args.requests)) as executor:
                futures = {executor.submit(perform, base_url, key, load_payload, args.timeout,
                                           {200}, args.stream): i for i in range(args.requests)}
                for future in as_completed(futures):
                    result = future.result()
                    result["request_number"] = futures[future] + 1
                    report["load"].append(result)
                    show("request {}".format(result["request_number"]), result)
            elapsed = time.monotonic() - started
            successful = [result for result in report["load"] if result["passed"]]
            report["summary"] = {
                "requests": args.requests, "concurrency": min(args.concurrency, args.requests),
                "passed": len(successful), "failed": args.requests - len(successful),
                "success_rate": len(successful) / args.requests, "elapsed_s": elapsed,
                "completed_rps": args.requests / elapsed, "successful_rps": len(successful) / elapsed,
                "status_counts": dict(Counter(str(result["status"]) for result in report["load"])),
                "all_latency": latency_summary([result["latency_s"] for result in report["load"]]),
                "success_latency": latency_summary([result["latency_s"] for result in successful]),
                "success_first_text": latency_summary([result["first_text_s"] for result in successful
                                                       if result.get("first_text_s") is not None]),
            }
            print(json.dumps(report["summary"], indent=2), flush=True)
    results = [baseline, *report["errors"], *report["load"]]
    failures = sum(not result["passed"] for result in results)
    print("Total: {} | Passed: {} | Failed: {}".format(len(results), len(results) - failures, failures))
    args.report_dir.mkdir(parents=True, exist_ok=True)
    path = args.report_dir.resolve() / ("check-" + uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2).replace(key, "[REDACTED]"))
    print("Report: " + str(path))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
