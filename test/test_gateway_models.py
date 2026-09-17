"""通过 Higress -> LiteLLM -> 模型，测试聊天、图片输入、图片生成和向量调用。

默认测试全部：python -X utf8 test/test_gateway_models.py
指定模型列表：python -X utf8 test/test_gateway_models.py --models 模型名1 模型名2
模型名使用 LiteLLM 列表中的 id，也就是配置中的 model_name。
图片模型需要通过 --image-models 指定，调用 /images/generations，只测试一次非流式生成。
图片结果在终端输出完整 URL；Base64 图片保存到 runtime/generated-images，可用 --image-output-dir 修改。
向量模型通过 --embedding-models 指定；--input-image 为聊天模型附加本地图片。
混合测试示例：--models my-agnes-2.5-pro my-agnes-image-2.5-flash --image-models my-agnes-image-2.5-flash
环境变量 GATEWAY_API_KEY 可以覆盖本地测试密钥。
默认每个聊天模型分别进行非流式和流式测试，共两次真实请求，可能产生调用费用。
"""

import argparse
import base64
import binascii
import json
import math
import mimetypes
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


def print_embeddings(result, inputs):
    """校验向量协议并显示语义比较数据；相似度需结合输入人工判断。"""
    entries = result.get("data")
    if not isinstance(entries, list) or len(entries) != len(inputs):
        raise RuntimeError("Embedding count does not match input count")
    vectors = {}
    dimension = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid embedding entry")
        index = entry.get("index")
        vector = entry.get("embedding")
        if type(index) is not int or index not in range(len(inputs)) or index in vectors:
            raise RuntimeError("Invalid or duplicate embedding index")
        if not isinstance(vector, list) or not vector or not all(
            type(value) in (int, float) and math.isfinite(value) for value in vector
        ):
            raise RuntimeError("Embedding must contain finite numbers")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension:
            raise RuntimeError("Embedding dimensions differ between inputs")
        norm = math.hypot(*vector)
        if not norm or not math.isfinite(norm):
            raise RuntimeError("Embedding has an invalid or zero norm")
        vectors[index] = [value / norm for value in vector]
        print("Vector {}: dimensions={} | first 8={}".format(
            index, dimension, vector[:8]), flush=True)
    for index in range(1, len(inputs)):
        similarity = sum(a * b for a, b in zip(vectors[0], vectors[index]))
        print("Cosine(input 0, input {}) = {:.6f}".format(index, similarity), flush=True)


def print_images(images, output_dir, key):
    """输出完整图片地址，将内嵌图片解码保存，避免 Base64 内容刷屏。"""
    print("--- Image output ---", flush=True)
    try:
        for index, item in enumerate(images, 1):
            url = item.get("url")
            encoded = item.get("b64_json")
            if isinstance(url, str) and url.strip():
                if url.startswith("data:"):
                    header, separator, encoded = url.partition(",")
                    if (not separator or not encoded.strip()
                            or not header.startswith("data:image/") or not header.endswith(";base64")):
                        raise RuntimeError("Unsupported image data URL")
                else:
                    print("Image {} URL: {}".format(
                        index, url.replace(key, "[REDACTED]")), flush=True)
            if isinstance(encoded, str) and encoded.strip():
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise RuntimeError("Image {} contains invalid Base64 data".format(index)) from None
                if not data:
                    raise RuntimeError("Image {} decoded to an empty file".format(index))
                # 根据文件签名选择扩展名；未知格式保留原始字节，不冒充 PNG。
                extension = ".bin"
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    extension = ".png"
                elif data.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif data.startswith((b"GIF87a", b"GIF89a")):
                    extension = ".gif"
                elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                    extension = ".webp"
                output_dir.mkdir(parents=True, exist_ok=True)
                path = output_dir.resolve() / ("image-" + uuid4().hex + extension)
                with path.open("xb") as file:
                    file.write(data)
                print("Image {} file: {} ({} bytes)".format(
                    index, str(path).replace(key, "[REDACTED]"), len(data)), flush=True)
            revised_prompt = item.get("revised_prompt")
            if isinstance(revised_prompt, str) and revised_prompt.strip():
                print("Revised prompt: " + revised_prompt.replace(key, "[REDACTED]"), flush=True)
    finally:
        print("--- End image output ---\n", flush=True)


def sse_data(response):
    """按 SSE 空行边界读取事件，合并多行 data，忽略心跳和其他字段。"""
    lines = []
    for raw_line in response:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield "\n".join(lines)
                lines = []
        elif line.startswith("data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(" ") else value)
    if lines:
        raise RuntimeError("SSE 事件未结束，连接已关闭")


def main():
    # 不传 --models 时测试全部；传入时至少指定一个模型，多个名称用空格分隔。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080/v1",
                        help="Higress API 基础地址，默认 http://localhost:8080/v1")
    parser.add_argument("--models", nargs="+", metavar="MODEL",
                        help="只测试指定模型，多个名称用空格分隔；默认测试全部")
    parser.add_argument("--image-models", nargs="+", default=[], metavar="MODEL",
                        help="将指定模型作为图片生成模型测试；名称必须在本次测试列表中")
    parser.add_argument("--embedding-models", nargs="+", default=[], metavar="MODEL",
                        help="将指定模型作为向量模型测试，只发送一次非流式批量请求")
    parser.add_argument("--embedding-inputs", nargs="+", default=[
        "如何修改账户密码？", "忘记密码后怎样重置登录密码？", "今天的天气晴朗，适合户外运动。"],
                        help="向量输入文本；默认三句用于比较相关和无关文本的相似度")
    parser.add_argument("--input-image", type=Path,
                        help="发送给本次聊天模型的本地 PNG/JPEG/WebP/GIF 图片")
    parser.add_argument("--mode", choices=("both", "non-stream", "stream"), default="both",
                        help="聊天测试模式：both 两种都测（默认），non-stream 非流式，stream 流式；图片始终非流式")
    parser.add_argument("--timeout", type=float, default=120,
                        help="每次网络请求的超时时间，单位秒，默认 120")
    parser.add_argument("--prompt", default=(
        "请用中文简要介绍你自己，三句话以内。"),
        help="发送给聊天模型的测试问题")
    parser.add_argument("--image-prompt", default="画一朵白色背景上的红色花朵。",
                        help="发送给图片模型的生成提示词")
    parser.add_argument("--image-output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "runtime" / "generated-images",
                        help="Base64 图片的保存目录，默认项目下 runtime/generated-images")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        parser.error("--base-url must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("--base-url must not contain credentials, a query, or a fragment")
    # 这里只使用访问网关的密钥；供应商 API Key 由 LiteLLM 管理。
    key = os.environ.get("GATEWAY_API_KEY", "sk-local-test").strip()
    if not key:
        parser.error("GATEWAY_API_KEY is empty")
    if set(args.image_models) & set(args.embedding_models):
        parser.error("同一模型不能同时指定为图片生成和向量模型")
    chat_content = args.prompt
    if args.input_image:
        mime_type = mimetypes.guess_type(str(args.input_image))[0]
        if mime_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            parser.error("--input-image 仅支持 PNG/JPEG/WebP/GIF")
        try:
            image_bytes = args.input_image.read_bytes()
        except OSError as exc:
            parser.error("无法读取图片：" + str(exc))
        if not image_bytes:
            parser.error("--input-image 不能为空文件")
        chat_content = [
            {"type": "text", "text": args.prompt},
            {"type": "image_url", "image_url": {
                "url": "data:{};base64,{}".format(
                    mime_type, base64.b64encode(image_bytes).decode("ascii"))}},
        ]

    # 直接连接网关，避免本机系统代理干扰 localhost 请求。
    opener = build_opener(ProxyHandler({}))

    def safe_text(value, limit=600):
        """错误和状态信息隐藏网关密钥、合并换行并截短；模型回答单独完整输出。"""
        return " ".join(str(value).replace(key, "[REDACTED]").split())[:limit]

    def request(path, payload=None, started=None):
        """统一校验网关响应；非流式解析 JSON，流式保持连接读取 SSE。"""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(base_url + path, data=data, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        try:
            with opener.open(req, timeout=args.timeout) as response:
                status = response.status
                server = response.headers.get("Server", "")
                # 当前部署用 Envoy 响应头识别 Higress；不能据此测试直连 4000。
                if "envoy" not in server.lower():
                    raise RuntimeError("HTTP {} but Server={!r}; expected Higress/Envoy. "
                                       "Check that the URL uses port 8080.".format(status, server))
                if payload and payload.get("stream"):
                    return read_stream(response, started)
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("HTTP {}: {}".format(exc.code, safe_text(body))) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Connection failed: " + safe_text(exc)) from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError("HTTP {} returned invalid JSON".format(status)) from None
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError("Unexpected API response: " + safe_text(result))
        return result

    def read_stream(response, started):
        """验证流式协议、文字分片和结束标记，统计首段文字到达时间。"""
        if response.headers.get_content_type() != "text/event-stream":
            raise RuntimeError("流式请求没有返回 text/event-stream")
        parts = []
        first_text = None
        finish_reason = None
        done = False
        pending = ""
        print("--- Streaming output ---", flush=True)
        try:
            for data in sse_data(response):
                if data == "[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                if not isinstance(chunk, dict) or chunk.get("error"):
                    raise RuntimeError("SSE 错误：" + safe_text(chunk))
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    raise RuntimeError("SSE 数据缺少 choices 数组")
                # usage 统计事件允许 choices 为空；只检查第一个候选回答。
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise RuntimeError("SSE choice 格式错误")
                    if choice.get("index") != 0:
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise RuntimeError("SSE delta 格式错误")
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        raise RuntimeError("SSE content 不是文字")
                    if content:
                        if first_text is None:
                            first_text = time.monotonic() - started
                        parts.append(content)
                        # 保留尾部少量字符，避免密钥横跨两个分片时漏脱敏。
                        pending = (pending + content).replace(key, "[REDACTED]")
                        cut = max(0, len(pending) - len(key) + 1)
                        print(pending[:cut], end="", flush=True)
                        pending = pending[cut:]
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice["finish_reason"]
        finally:
            print(pending.replace(key, "[REDACTED]"), flush=True)
            print("--- End streaming output ---", flush=True)
        if not done:
            raise RuntimeError("流式连接提前结束，未收到 [DONE]")
        if not finish_reason:
            raise RuntimeError("流式响应缺少 finish_reason")
        if not "".join(parts).strip():
            raise RuntimeError("流式响应未返回文字")
        return {"chunks": len(parts), "first_text": first_text, "finish_reason": finish_reason}

    print("Route: Higress -> LiteLLM -> model", flush=True)
    print("Gateway: " + base_url, flush=True)
    print("Prompt: " + args.prompt.replace(key, "[REDACTED]"), flush=True)
    if args.image_models:
        print("Image prompt: " + args.image_prompt.replace(key, "[REDACTED]"), flush=True)
    if args.input_image:
        print("Input image: " + str(args.input_image.resolve()).replace(key, "[REDACTED]"), flush=True)
    if args.embedding_models:
        for index, value in enumerate(args.embedding_inputs):
            print("Embedding input {}: {}".format(index, value.replace(key, "[REDACTED]")), flush=True)
    try:
        # 先通过 Higress 获取模型列表，校验模型名称并按返回顺序去重。
        listing = request("/models")
        entries = listing.get("data")
        if not isinstance(entries, list):
            raise RuntimeError("Model list response has no data array")
        models = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise RuntimeError("Model list contains an invalid model ID")
            if entry["id"] not in models:
                models.append(entry["id"])
        if not models:
            raise RuntimeError("No models advertised; configure LiteLLM before testing")
        available_count = len(models)
        if args.models is not None:
            # 保持用户指定的顺序，重复名称只测试一次。
            selected = list(dict.fromkeys(args.models))
            unknown = [model for model in selected if model not in models]
            if unknown:
                raise RuntimeError("指定模型不在 LiteLLM 列表中：{}；可用模型：{}".format(
                    ", ".join(unknown), ", ".join(models)))
            models = selected
        unknown_images = [model for model in args.image_models if model not in models]
        if unknown_images:
            raise RuntimeError("图片模型不在本次测试列表中：" + ", ".join(unknown_images))
        unknown_embeddings = [model for model in args.embedding_models if model not in models]
        if unknown_embeddings:
            raise RuntimeError("向量模型不在本次测试列表中：" + ", ".join(unknown_embeddings))
        if args.input_image and not (set(models) - set(args.image_models) - set(args.embedding_models)):
            raise RuntimeError("--input-image 需要至少选中一个聊天模型")
    except Exception as exc:
        print("FAIL model discovery: " + safe_text(exc), flush=True)
        return 1

    print("Discovered {} model(s); testing {} sequentially.\n".format(
        available_count, len(models)), flush=True)
    failed = []
    modes = ("non-stream", "stream") if args.mode == "both" else (args.mode,)
    image_models = set(args.image_models)
    embedding_models = set(args.embedding_models)
    total = sum(1 if model in image_models | embedding_models else len(modes) for model in models)
    # 串行调用选中的模型；一个失败不会中断其他模型的测试。
    for index, model in enumerate(models, 1):
        model_modes = (("image",) if model in image_models else
                       ("embedding",) if model in embedding_models else modes)
        for mode in model_modes:
            print("[{}/{}] {} | {} ...".format(
                index, len(models), safe_text(model), mode), flush=True)
            started = time.monotonic()
            try:
                if mode == "embedding":
                    result = request("/embeddings", {
                        "model": model, "input": args.embedding_inputs, "encoding_format": "float",
                    })
                    print_embeddings(result, args.embedding_inputs)
                    print("  PASS {:.2f}s | vectors={} (protocol check)\n".format(
                        time.monotonic() - started, len(args.embedding_inputs)), flush=True)
                    continue
                if mode == "image":
                    result = request("/images/generations", {
                        "model": model,
                        "prompt": args.image_prompt,
                        "n": 1,
                    })
                    images = result.get("data")
                    if not isinstance(images, list) or not images:
                        raise RuntimeError("HTTP 200 but no image data returned")
                    for item in images:
                        if not isinstance(item, dict) or not any(
                            isinstance(item.get(field), str) and item[field].strip()
                            for field in ("url", "b64_json")
                        ):
                            raise RuntimeError("HTTP 200 but image has no URL or Base64 data")
                    print_images(images, args.image_output_dir, key)
                    print("  PASS {:.2f}s | images={}\n".format(
                        time.monotonic() - started, len(images)), flush=True)
                    continue
                result = request("/chat/completions", {
                    "model": model,
                    "messages": [{"role": "user", "content": chat_content}],
                    "stream": mode == "stream",
                }, started=started)
                if mode == "stream":
                    print("  PASS {:.2f}s | text chunks={} | first text={:.2f}s | finish={}\n".format(
                        time.monotonic() - started, result["chunks"],
                        result["first_text"], result["finish_reason"]), flush=True)
                    continue
                # 非流式聊天应返回非空文字，不强制判断答案内容。
                choices = result.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise RuntimeError("HTTP 200 but no chat choices returned")
                choice = choices[0]
                message = choice.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("HTTP 200 but no text answer; finish_reason={!r}".format(
                        choice.get("finish_reason")))
                print("  PASS {:.2f}s".format(time.monotonic() - started), flush=True)
                print("--- Model output ---", flush=True)
                print(content.replace(key, "[REDACTED]"), flush=True)
                print("--- End output ---\n", flush=True)
            except Exception as exc:
                failed.append("{} [{}]".format(model, mode))
                print("  FAIL {:.2f}s | {}".format(
                    time.monotonic() - started, safe_text(exc)), flush=True)

    print("\nTotal: {} | Passed: {} | Failed: {}".format(
        total, total - len(failed), len(failed)), flush=True)
    if failed:
        print("Failed models: " + ", ".join(safe_text(model) for model in failed))
    # 退出码供自动化任务判断结果：全部通过为 0，存在失败为 1。
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nTest interrupted.")
        sys.exit(130)
