"""使用 UTF-8 聊天请求测试一条 Higress AI 路由，仅依赖 Python 标准库。

运行方式：python -X utf8 test/test_higress_route.py
运行前在下方 GATEWAY_API_KEY 中填写 Higress 消费者密钥。
每次运行只发送一次请求，不自动重试。
添加 --stream 可实时显示正文，并测量首段推理和首段正文的等待时间。

Python 测试脚本
  │ localhost:8080 + Host: ai-test.local + 消费者 Key
  ▼
Higress
  │ 按域名和 /v1 路径匹配 AI 路由
  │ 校验消费者 Key
  │ 使用提供者配置中的 LiteLLM Key
  ▼
LiteLLM（172.30.50.2:4000）
  │ 将 my-qwen3.6-27b 映射为 Qwen3.6-27B
  │ 使用配置的供应商 Key
  ▼
远程模型接口：test2-aigc.campusapp.com.cn/api/v1
  │ 推理并生成回答
  ▼
经 LiteLLM → Higress → 返回测试脚本

"""

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


# 在引号内填写 Higress 消费者 Key，不要加 Bearer 前缀。
GATEWAY_API_KEY = "3f8f9e70-8a3f-4470-825c-f1f783f72737"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 禁止自动重定向，避免将消费者密钥转发到其他地址。
        return None


def sse_events(response):
    """按空行分隔 SSE 事件，支持多行 data 和心跳注释。"""
    lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield "\n".join(lines)
                lines = []
        elif line.startswith("data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(" ") else value)
    if lines:
        raise ValueError("SSE 事件尚未结束，连接已关闭")


def read_stream(response, elapsed, output, timings):
    """记录客户端收到事件的时间；推理内容仅计时，不打印。"""
    parts = []
    result = {"model": None, "usage": None}
    finish_reason = None
    completed = False
    for event in sse_events(response):
        if event == "[DONE]":
            completed = True
            break
        chunk = json.loads(event)
        if chunk.get("error"):
            raise ValueError("流式错误：" + json.dumps(chunk["error"], ensure_ascii=False))
        timings.setdefault("首个数据事件", elapsed())
        if chunk.get("model"):
            result["model"] = chunk["model"]
        if chunk.get("usage") is not None:
            result["usage"] = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning and "首段推理" not in timings:
                timings["首段推理"] = elapsed()
                output("\n收到首段推理：{:.3f}s（不展示推理内容）".format(timings["首段推理"]))
            text = delta.get("content")
            if isinstance(text, str) and text:
                if "首段正文" not in timings:
                    timings["首段正文"] = elapsed()
                    output("\n收到首段正文：{:.3f}s\n--- 实时回答 ---".format(timings["首段正文"]))
                parts.append(text)
                output(text, end="")
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
    output("")
    if not completed or finish_reason is None:
        raise ValueError("流式响应不完整：缺少 [DONE] 或 finish_reason")
    result["choices"] = [{"finish_reason": finish_reason,
                          "message": {"content": "".join(parts)}}]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080/v1")
    parser.add_argument("--host", default="ai-test.local",
                        help="Route Host header; use --host '' to omit it")
    parser.add_argument("--model", default="my-qwen3.6-27b")
    parser.add_argument("--prompt", default="\u8bf7\u7528\u4e2d\u6587\u7b80\u77ed\u4ecb\u7ecd\u4e00\u4e0b\u4f60\u81ea\u5df1\u3002")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--stream", action="store_true", help="流式显示正文并测量首段输出时间")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        parser.error("--base-url must be an HTTP(S) URL without credentials/query/fragment")
    if args.max_tokens <= 0 or not 0 < args.timeout < float("inf"):
        parser.error("--max-tokens and --timeout must be positive and finite")
    if args.host and any(ord(c) <= 32 or ord(c) >= 127 for c in args.host):
        parser.error("--host must contain printable ASCII without spaces")

    key = GATEWAY_API_KEY.strip()
    if not key or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
        parser.error("请在脚本顶部的 GATEWAY_API_KEY 中填写有效密钥，不要包含空格或中文")

    def output(value, end="\n"):
        print(str(value).replace(key, "[REDACTED]"), end=end, flush=True)

    request_id = str(uuid4())
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream" if args.stream else "application/json",
        "X-Request-ID": request_id,
    }
    if args.host:
        headers["Host"] = args.host
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "stream": args.stream,
    }
    if args.stream:
        payload["stream_options"] = {"include_usage": True}
    url = args.base_url.rstrip("/") + "/chat/completions"
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers=headers, method="POST")
    output("URL: {} | Host: {} | Model: {}".format(url, args.host, args.model))
    output("Request ID (sent): " + request_id)
    output("模式：" + ("流式" if args.stream else "非流式（等待完整响应后显示正文）"))
    opener = build_opener(ProxyHandler({}), NoRedirect())
    timings = {}
    started = time.perf_counter()

    def elapsed():
        return time.perf_counter() - started

    try:
        with opener.open(request, timeout=args.timeout) as response:
            timings["收到响应头"] = elapsed()
            output("HTTP {} | 收到响应头：{:.3f}s".format(response.status, timings["收到响应头"]))
            output("Request ID (response): " + response.headers.get("X-Request-ID", "not provided"))
            if args.stream:
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise ValueError("请求了流式响应，但服务未返回 text/event-stream")
                result = read_stream(response, elapsed, output, timings)
            else:
                raw = response.read()
                timings["响应体接收完成"] = elapsed()
                result = json.loads(raw.decode("utf-8-sig"))
            timings["完整响应处理完成"] = elapsed()
        if not isinstance(result, dict) or not result.get("choices"):
            raise ValueError("Response does not contain chat choices")
        choice = result["choices"][0]
        content = choice.get("message", {}).get("content")
        output("Response model: " + str(result.get("model")))
        output("Finish reason: " + str(choice.get("finish_reason")))
        output("Usage: " + json.dumps(result.get("usage"), ensure_ascii=False))
        if not isinstance(content, str) or not content.strip():
            output("FAIL: No final text. If finish_reason=length, increase --max-tokens.")
            return 1
        if not args.stream:
            output("\n--- Answer ---\n" + content)
        if choice.get("finish_reason") == "length":
            output("INCOMPLETE: Output reached the token limit; increase --max-tokens.")
            return 1
        output("\nPASS: Chat response received (verify the answer manually).")
        return 0
    except HTTPError as exc:
        output("FAIL: HTTP {} {}".format(exc.code, exc.reason))
        output(exc.read().decode("utf-8", errors="replace")[:4000])
        return 1
    except (URLError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        output("FAIL: {}: {}".format(type(exc).__name__, exc))
        return 1
    finally:
        output("\n--- 客户端耗时（均从发起请求开始计算）---")
        for label, seconds in timings.items():
            output("{}：{:.3f}s".format(label, seconds))
        if "完整响应处理完成" not in timings:
            output("失败前已等待：{:.3f}s".format(elapsed()))
        if not args.stream:
            output("非流式无法测量模型首段输出时间；使用 --stream 对比。")
        else:
            for label in ("首段推理", "首段正文"):
                if label not in timings:
                    output(label + "：未收到")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
