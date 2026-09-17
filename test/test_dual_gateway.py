"""通过 Higress 和 LiteLLM 直连地址，对同一组请求参数进行对比测试。

一、当前默认链路
  Higress 入口：
    本脚本 -> http://localhost:8080/v1/chat/completions
           -> Higress 普通路由 litellm-proxy（匹配 /v1 前缀）
           -> LiteLLM（容器地址 172.30.50.2:4000）-> 上游模型服务
    响应按 上游模型服务 -> LiteLLM -> Higress -> 本脚本 返回。
  LiteLLM 直连：
    本脚本 -> http://localhost:4000/v1/chat/completions -> 上游模型服务
    响应按 上游模型服务 -> LiteLLM -> 本脚本 返回，不经过 Higress。
  localhost 指运行本脚本的电脑；从其他机器测试时需指定网关所在机器的地址。

二、请求如何处理
  1. 两次请求携带相同的 Authorization: Bearer <Virtual Key> 和 model 别名。
     默认 Higress 普通路由负责转发，保留 /v1 路径并透传鉴权头给 LiteLLM。
     该链路无需为每个模型新增 Higress 路由，也不要求另配 Higress 消费者 Key。
     这不等于允许匿名调用：省略或伪造 Virtual Key 应被后面的 LiteLLM 拒绝。
  2. LiteLLM 校验 Virtual Key，并按 Key、团队等配置检查模型权限、有效期、
     预算与限流。一个 Key 可以授权多个模型，每次由请求中的 model 指定模型。
     All Team Models 表示继承团队模型权限，不能越过团队授权范围。
  3. LiteLLM 从 config/litellm.yaml 的 model_list 解析模型别名，选择上游地址
     和供应商凭证，并进行协议适配。以本脚本默认模型为例：
       my-qwen3.6-27b -> openai/Qwen3.6-27B
       上游 API Base：https://test2-aigc.campusapp.com.cn/api/v1
     Virtual Key 用于访问本地网关，上游请求使用服务端配置的供应商 Key。
  4. PostgreSQL（db:5432）保存 Key、团队及用量等数据，是 LiteLLM 的支撑服务，
     不负责转发模型请求。本项目 Compose 管理数据库，复用外部持久卷
     litellm_postgres_data；在新环境启动前需先创建或恢复该卷。

三、用量、费用与监控
  两个入口的请求都会进入 LiteLLM，因此都可以按实际认证的 Key、团队、模型
  记录用量。Higress 仅能观测经过它的请求，被它拦截的请求不会进入 LiteLLM。
  LiteLLM 的统计写入和页面聚合可能有延迟，可在以下页面选择时间范围后刷新：
    http://localhost:4000/ui/usage/  查看用量和估算费用汇总
    http://localhost:4000/ui/logs/   查看已记录的请求明细
  Token 统计取决于上游 usage 返回及适配器处理；缺少 usage 不等于零消耗。
  普通文本请求费用约为 输入 Token × 输入单价 + 输出 Token × 输出单价，
  缓存等费用按供应商规则另算。LiteLLM 标准预算和费用使用美元口径，不是人民币；
  必须核对实际模型价格，费用为 0 不代表免费，最终结算以供应商账单为准。
  本脚本的 usage 字段来自 API 响应，不查询数据库，也不直接验证费用入账。

四、与项目中另一条 AI 路由的区别
  项目还配置了 Host: ai-test.local 的专用 AI 路由，由 test_higress_route.py
  测试。它校验 Higress 消费者 Key，再使用提供者配置中的 LiteLLM Key 转发。
  本脚本默认请求 localhost，不设置该 Host，走的是上述普通路由。
  专用 AI 路由在 LiteLLM 中的用量归属取决于转发使用的 Key，不能自动按原始
  Higress 消费者区分；普通路由则保留客户端 Virtual Key 的归属。

五、本脚本的测试范围
  依次向 Higress、LiteLLM 各发送一次非流式聊天请求，参数包含 temperature=0、
  max_tokens=64；脚本不主动重试。两次是独立推理，通常产生两份用量及费用，
  网关或供应商侧的重试还可能增加用量。耗时包含上游推理与网络等待，不能把
  两次耗时差直接视为 Higress 开销，也不要求两次返回文本完全相同。
  输出 HTTP 状态、耗时、返回模型、usage 和正文预览；content=null 时预览为空。
  当前 ok 仅按 HTTP 2xx 判断，不能证明正文完整、授权隔离或计量准确；仍需检查
  响应和 Usage/Logs。任一入口 HTTP 或网络失败时，脚本返回非零退出码。

六、如何验证鉴权
  加上 --check-auth --forbidden-model <已配置但此Key无权访问的模型别名>，
  每个入口分别测试：不带 Authorization、随机错误 Key、正确 Key 调授权模型、
  正确 Key 调未授权模型。前两项应返回 401/403，授权调用应返回 2xx 聊天响应，
  未授权调用须返回 401/403 且错误类型为模型权限拒绝。404 或网络失败不算通过。
  正确 Key 的成功响应仅证明鉴权后可调用，content=null 不证明生成了完整正文。
  未授权模型必须真实存在；可由管理员在 Models + Endpoints 中核对。
  测试用于证明 Key 校验和模型权限生效，不证明模型只允许 ai-platform 使用。
  Key 归属由 LiteLLM 保存的 team_id 决定；其他团队的合法 Key 若也有相同模型
  权限，同样可以调用。管理员主密钥拥有更高权限，不能作为团队权限测试凭证。
  正常情况下该模式只有两次授权请求会进入上游，但鉴权失效时负例也可能产生用量。
  可通过 GATEWAY_API_KEY 环境变量传入 Key；输出会隐藏请求中使用的 Key。

七、本地鉴权验证记录（配置或权限修改后需重新验证）
  Key 别名 test-app，归属团队 ai-platform。管理接口确认该团队允许 3 个模型，
  此 Key 进一步限定为 qwen3.7-flash、my-qwen3.6-27b。
  用例                                     Higress:8080   LiteLLM:4000
  不带 Key                                 401            401
  随机错误 Key                             401            401
  test-app 调 my-qwen3.6-27b               200            200
  test-app 调 my-deepseek-v4-flash          403            403
  未授权模型已在全局模型列表中确认存在，两端拒绝类型均为 key_model_access_denied。
  这证明此 Key 的模型限制有效；本次没有单独验证团队限制或其他团队 Key 的隔离。
  两次授权调用各返回 82 Token（输入 18、输出 64），正文预览为空；这不是完整
  文本生成的验收结果，也不能据此断定空正文的原因或费用是否已正确入账。

运行示例（密钥为占位符，URL 使用纯文本，不要粘贴 Markdown 链接格式）：
  python -X utf8 test/test_dual_gateway.py --key sk-xxx
  python -X utf8 test/test_dual_gateway.py --key sk-xxx --model my-qwen3.6-27b
  python -X utf8 test/test_dual_gateway.py --check-auth --forbidden-model my-deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 鉴权验证不跟随重定向，避免把凭证交给另一个地址或误判登录页为成功。
        return None


def call(base_url: str, key: str | None, model: str, prompt: str, timeout: float) -> dict:
    """向指定入口发送一次聊天请求，并返回结构化测试结果。"""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 64,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    request = Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    # 直接连接被测入口，避免本机系统代理干扰对网关鉴权的判断。
    opener = build_opener(ProxyHandler({}), NoRedirect())
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": base_url, "ok": False, "error": str(exc).replace(key or "\0", "[REDACTED]"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

    result = {"url": base_url, "status": status,
              "ok": 200 <= status < 300,
              "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    try:
        raw = raw.replace(key, "[REDACTED]") if key else raw
        data = json.loads(raw)
        if not isinstance(data, dict):
            result.update(ok=False, error="响应 JSON 不是对象")
            return result
        result["model"] = data.get("model")
        result["usage"] = data.get("usage")
        choices = data.get("choices") or []
        first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        result["chat_response"] = isinstance(first_choice.get("message"), dict) and not data.get("error")
        message = first_choice.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        result["preview"] = content[:120]
        if not result["ok"] or data.get("error"):
            result["ok"] = False
            result["error"] = data.get("error", data)
        if not choices:
            result["response"] = data
    except json.JSONDecodeError:
        result["ok"] = False
        result["body"] = raw[:500]
    return result


def auth_passed(case: str, report: dict) -> bool:
    """区分预期的权限拒绝与网络故障、供应商错误，避免将所有失败都算作通过。"""
    if case == "authorized":
        return bool(report.get("ok") and report.get("chat_response"))
    error = report.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    if case == "forbidden-model":
        return report.get("status") in (401, 403) and error_type in {
            "key_model_access_denied", "team_model_access_denied", "user_model_access_denied",
        }
    return report.get("status") in (401, 403)


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 Higress 入口和 LiteLLM 直连入口")
    parser.add_argument("--key", default=os.environ.get("GATEWAY_API_KEY"),
                        help="LiteLLM Virtual Key，也可设置 GATEWAY_API_KEY 环境变量")
    parser.add_argument("--model", default="my-qwen3.6-27b")
    parser.add_argument("--prompt", default="请只回答：网关链路测试成功")
    parser.add_argument("--timeout", type=float, default=60, help="单个请求超时时间（秒）")
    parser.add_argument("--higress", default="http://localhost:8080/v1")
    parser.add_argument("--litellm", default="http://localhost:4000/v1")
    parser.add_argument("--check-auth", action="store_true", help="在两个入口验证鉴权与模型权限")
    parser.add_argument("--forbidden-model", help="已配置、但此 Key 无权访问的模型别名")
    args = parser.parse_args()
    if not args.key or not args.key.strip():
        parser.error("请通过 --key 或 GATEWAY_API_KEY 指定团队 Virtual Key")
    if args.check_auth and (not args.forbidden_model or args.forbidden_model == args.model):
        parser.error("鉴权测试需要 --forbidden-model，且不能与授权模型相同")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于零的有限数值")
    for base_url in (args.higress, args.litellm):
        try:
            parsed = urlsplit(base_url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
                     and not any(char.isspace() for char in base_url))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            parser.error("入口必须是纯 HTTP(S) URL，不能包含 Markdown 链接、凭证、查询参数或片段")

    print(f"模型：{args.model}")
    print(f"提示词：{args.prompt}")
    cases = [("authorized", args.key, args.model)]
    if args.check_auth:
        cases = [
            ("missing-key", None, args.model),
            ("invalid-key", "sk-invalid-" + uuid4().hex, args.model),
            *cases,
            ("forbidden-model", args.key, args.forbidden_model),
        ]
    failed = False
    for name, base_url in (("Higress", args.higress), ("LiteLLM", args.litellm)):
        for case, key, model in cases:
            print(f"\n{name} | {case} | {base_url}", flush=True)
            report = call(base_url, key, model, args.prompt, args.timeout)
            passed = auth_passed(case, report) if args.check_auth else report.get("ok", False)
            report.update(case=case, requested_model=model, passed=bool(passed))
            output = json.dumps(report, ensure_ascii=False, indent=2)
            print(output.replace(args.key, "[REDACTED]"), flush=True)
            failed |= not passed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
