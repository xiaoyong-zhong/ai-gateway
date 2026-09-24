"""Loopback-only P1 application governance verification console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
ENV_FILE = ROOT / ".env.p1-test"
HIGRESS_ENV_FILE = ROOT / ".env.p1-higress-test"
APPS_FILE = ROOT / "config" / "p1-test" / "apps.json"
ORG_POLICY_FILE = ROOT / "config" / "p1-test" / "organization-model-policy.json"
LITELLM_CONFIG_FILE = ROOT / "config" / "litellm.yaml"
P1_LITELLM_CONFIG_FILE = ROOT / "config" / "litellm.p1-test.yaml"
SECRET_FILE = ROOT / "runtime" / "p1-test" / "secrets" / "p1_key_derivation_secret"
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.p1-test.yml"
REPORT_ROOT = ROOT / "runtime" / "p1-test" / "reports"
SNAPSHOT_ROOT = ROOT / "runtime" / "p1-test" / "higress" / "snapshots"
GATEWAY_URL = "http://127.0.0.1:28080"
GATEWAY_HOST = "ai-gateway-p1-test.local"
PORT = int(os.environ.get("P1_CONSOLE_PORT", "28770"))
MODEL = "my-qwen3.6-27b"
DOMAIN_SEPARATOR = b"campus-ai-gateway/litellm-key/v1/"
MAX_BODY = 16384
OPENER = build_opener(ProxyHandler({}))
JOBS: dict[str, dict[str, Any]] = {}
JOB_LOCK = threading.Lock()
ACTIVE_JOB: str | None = None
WINDOWS = {"15m": ("最近 15 分钟", "15 minutes"), "1h": ("最近 1 小时", "1 hour"),
           "24h": ("最近 24 小时", "24 hours"), "7d": ("最近 7 天", "7 days")}
MODEL_NAME_RE = re.compile(r"^\s*-\s*model_name:\s*[\"']?([^\"'#\s]+)")


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if separator:
                result[key] = value
    return result


def apps_manifest() -> list[dict[str, Any]]:
    try:
        document = json.loads(APPS_FILE.read_text(encoding="utf-8-sig"))
        apps = document.get("applications", [])
        return [item for item in apps if isinstance(item, dict) and isinstance(item.get("app_id"), str)]
    except (OSError, json.JSONDecodeError):
        return []


def configured_model_names(path: Path) -> list[str]:
    names: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = MODEL_NAME_RE.match(line)
            if match and match.group(1) not in names:
                names.append(match.group(1))
    except OSError:
        pass
    return names


def model_catalog(active_models: list[str] | None = None) -> list[dict[str, Any]]:
    active = set(active_models or [])
    p1_models = set(configured_model_names(P1_LITELLM_CONFIG_FILE))
    names = configured_model_names(LITELLM_CONFIG_FILE)
    for name in [*p1_models, *active]:
        if name not in names:
            names.append(name)
    return [{"id": name, "kind": model_kind(name), "configured": True,
             "p1_configured": name in p1_models, "gateway_active": name in active} for name in names]


def model_kind(model: str) -> str:
    lowered = model.lower()
    if "embedding" in lowered:
        return "embedding"
    if "image" in lowered:
        return "image"
    return "chat"


def active_configured_models() -> list[str]:
    return configured_model_names(P1_LITELLM_CONFIG_FILE)


def organization_policy() -> dict[str, Any]:
    default = {"version": 1, "organization_id": "org-p1-test", "organization_name": "P1 本地测试组织",
               "allowed_models": active_configured_models(), "updated_by": "p1-local-admin"}
    try:
        raw = json.loads(ORG_POLICY_FILE.read_text(encoding="utf-8-sig"))
        allowed = raw.get("allowed_models", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            return default
        return {**default, **raw, "allowed_models": allowed}
    except (OSError, json.JSONDecodeError):
        return default


def save_organization_policy(allowed_models: list[str], changed_by: str = "p1-console") -> dict[str, Any]:
    active = set(active_configured_models())
    selected = list(dict.fromkeys(allowed_models))
    if not selected:
        return {"ok": False, "error": "组织至少需要保留一个模型。"}
    if any(item not in active for item in selected):
        return {"ok": False, "error": "组织模型必须来自当前 P1 LiteLLM 配置。"}
    violations = []
    for app in apps_manifest():
        removed = [item for item in app.get("models", []) if item not in selected]
        if removed:
            violations.append({"app_id": app["app_id"], "display_name": app.get("display_name", ""), "removed_models": removed})
    if violations:
        return {"ok": False, "error": "不能直接收紧组织模型；请先把下列应用的模型策略改成组织集合的子集。", "violations": violations}
    payload = {"version": 1, "organization_id": organization_policy().get("organization_id", "org-p1-test"),
               "organization_name": organization_policy().get("organization_name", "P1 本地测试组织"),
               "allowed_models": selected, "updated_by": changed_by}
    try:
        ORG_POLICY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        return {"ok": False, "error": "组织模型策略写入失败：" + redact(str(error))}
    return {"ok": True, "organization_policy": payload}


def app_by_id(app_id: str) -> dict[str, Any] | None:
    return next((item for item in apps_manifest() if item.get("app_id") == app_id), None)


def secrets() -> tuple[str, ...]:
    values = [*load_env(ENV_FILE).values(), *load_env(HIGRESS_ENV_FILE).values()]
    # Do not treat the binary HMAC root secret as a text replacement string:
    # arbitrary byte sequences can accidentally match an innocuous app_id.
    return tuple(value for value in values if value and len(value) >= 12)


def redact(value: str) -> str:
    text = str(value or "")
    for secret in secrets():
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+|(?:api[_-]?key|token|secret|password|credential)[\"']?\s*[:=]\s*[\"']?)([^\s,\"']+)", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return text[:6000]


def derive_key(app_id: str) -> str:
    secret = SECRET_FILE.read_bytes()
    digest = hmac.new(secret, DOMAIN_SEPARATOR + app_id.encode("ascii"), hashlib.sha256).digest()
    return "sk-" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def higress_key(app: dict[str, Any]) -> str:
    return load_env(HIGRESS_ENV_FILE).get(str(app.get("higress_key_env", "")), "")


def compose(*args: str, timeout: int = 30, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=merged)


def compose_services() -> list[dict[str, Any]]:
    try:
        result = compose("ps", "--format", "json", timeout=12)
    except (OSError, subprocess.SubprocessError) as error:
        return [{"name": "docker", "state": "unavailable", "health": "", "detail": redact(str(error))}]
    if result.returncode != 0:
        return [{"name": "docker", "state": "unavailable", "health": "", "detail": redact(result.stderr or result.stdout)}]
    items: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        service = str(raw.get("Service") or raw.get("service") or raw.get("Name", "unknown"))
        status = str(raw.get("State") or raw.get("state") or "unknown")
        health = str(raw.get("Health") or raw.get("health") or "")
        items.append({"name": service, "state": status, "health": health,
                      "status": str(raw.get("Status", "")), "ports": str(raw.get("Ports", ""))})
    return items


def http_json(url: str, *, key: str = "", host: str = "", body: dict[str, Any] | None = None, timeout: int = 30) -> tuple[int, Any, dict[str, str]]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if host:
        headers["Host"] = host
    payload = None
    method = "GET"
    if body is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    except HTTPError as error:
        status = error.code
        raw = error.read().decode("utf-8", errors="replace")
        response_headers = {str(k).lower(): str(v) for k, v in error.headers.items()}
    except (URLError, OSError, TimeoutError) as error:
        return 0, {"error": redact(str(error))}, {}
    try:
        return status, json.loads(raw), response_headers
    except json.JSONDecodeError:
        return status, raw[:3000], response_headers


def safe_info(app_id: str) -> dict[str, Any]:
    key = derive_key(app_id)
    payload = {"key": key, "action": "Get"}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    code = (
        "import base64,json,os,urllib.parse,urllib.request; "
        "p=json.loads(base64.b64decode(os.environ['P1_CONSOLE_ADMIN_B64'])); "
        "h={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}; "
        "u='http://127.0.0.1:4000/key/info?'+urllib.parse.urlencode({'key':p['key']}); "
        "r=urllib.request.urlopen(urllib.request.Request(u,headers=h),timeout=15); "
        "i=json.load(r).get('info',{}); "
        "print(json.dumps({k:i.get(k) for k in ('key_alias','models','rpm_limit','tpm_limit','max_parallel_requests','blocked','expires','metadata')},ensure_ascii=False))"
    )
    try:
        result = compose("exec", "-T", "-e", "P1_CONSOLE_ADMIN_B64", "litellm", "python", "-c", code,
                         timeout=25, env={"P1_CONSOLE_ADMIN_B64": encoded})
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))}
    if result.returncode != 0:
        return {"ok": False, "error": redact(result.stderr or result.stdout)}
    try:
        return {"ok": True, "info": json.loads(result.stdout.strip().splitlines()[-1])}
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "error": "LiteLLM 策略返回格式无法解析。"}


def app_payload(include_info: bool = True) -> list[dict[str, Any]]:
    rows = []
    for app in apps_manifest():
        row = {"app_id": app["app_id"], "display_name": app.get("display_name", ""),
               "models": app.get("models", []), "rpm_limit": app.get("rpm_limit"),
               "tpm_limit": app.get("tpm_limit"), "max_parallel_requests": app.get("max_parallel_requests"),
               "higress_key_configured": bool(higress_key(app))}
        if include_info:
            info = safe_info(app["app_id"])
            row["runtime"] = info.get("info", {}) if info.get("ok") else {"error": info.get("error", "未知错误")}
        rows.append(row)
    return rows


def latest_report() -> dict[str, Any] | None:
    if not REPORT_ROOT.is_dir():
        return None
    paths = sorted(REPORT_ROOT.glob("p1-*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not paths:
        return None
    try:
        text = paths[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    result = "PASS" if re.search(r"(?:Conclusion|结论):\s*PASS", text, re.I) else "CHECK"
    if re.search(r"(?:Conclusion|结论):\s*FAIL", text, re.I):
        result = "FAIL"
    return {"name": paths[0].name, "status": result, "updated_at": datetime.fromtimestamp(paths[0].stat().st_mtime, timezone.utc).isoformat(),
            "preview": redact(text[-2500:])}


def status_payload() -> dict[str, Any]:
    services = compose_services()
    service_map = {item["name"]: item for item in services}
    expected = {"db", "litellm", "mapper", "higress", "log-pruner"}
    services_ok = expected.issubset(service_map) and all(service_map[name]["state"] == "running" for name in expected)
    app_a = next((item for item in apps_manifest()), None)
    model_status, model_body, _ = http_json(GATEWAY_URL + "/v1/models", key=higress_key(app_a) if app_a else "", host=GATEWAY_HOST, timeout=8)
    # /v1/models is intentionally queried with a Higress Consumer Key, so
    # LiteLLM may return only that application's authorized subset. Keep that
    # per-application view separate from the global P1 catalog used by the
    # organization policy editor.
    visible_model_ids = [item.get("id") for item in model_body.get("data", []) if isinstance(item, dict)] if isinstance(model_body, dict) else []
    catalog_model_ids = active_configured_models()
    mapper_health = service_map.get("mapper", {}).get("health", "") in ("healthy", "n/a", "")
    return {"ok": services_ok and model_status == 200, "checked_at": datetime.now(timezone.utc).isoformat(),
            "gateway_url": f"http://{GATEWAY_HOST}:28080/v1", "services": services,
            "services_ok": services_ok, "model_status": model_status, "models": catalog_model_ids,
            "visible_models": visible_model_ids, "model_catalog": model_catalog(catalog_model_ids), "baseline_model": MODEL,
            "mapper_healthy": mapper_health, "apps": app_payload(True),
            "organization_policy": organization_policy(),
            "latest_report": latest_report(),
            "boundaries": ["组织、用户、RBAC 和生产管理后台不在 P1", "LiteLLM Audit 本地授权能力待确认", "P1 Higress 指标当前为累计 Envoy 计数器", "P1 未发布生产域名/CIDR/mTLS"]}


def mask_key(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) <= 12:
        return value[:3] + "…"
    return value[:8] + "…" + value[-6:]


def connection_info(app_id: str, reveal: bool = False) -> dict[str, Any]:
    app = app_by_id(app_id)
    if not app:
        return {"ok": False, "error": "未知应用。"}
    key = higress_key(app)
    gateway_url = f"http://{GATEWAY_HOST}:28080"
    base_url = gateway_url + "/v1"
    return {"ok": True, "app_id": app_id, "display_name": app.get("display_name", ""),
            "ccswitch_url": gateway_url, "base_url": base_url, "models_url": base_url + "/models",
            "chat_url": base_url + "/chat/completions", "responses_url": base_url + "/responses",
            "authorization": "Authorization: Bearer <Higress Consumer Key>",
            "key_configured": bool(key), "key_masked": mask_key(key),
            "key": key if reveal else None, "models": app.get("models", []),
            "host_note": "客户端地址必须使用 ai-gateway-p1-test.local；请确认 hosts 已配置 127.0.0.1 ai-gateway-p1-test.local。",
            "key_note": "这是 Higress Consumer Key，不是 LiteLLM Master Key 或 Provider Key。"}


def recent_logs(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    sql = f'''SELECT COALESCE(json_agg(to_jsonb(log_row)), '[]'::json)::text FROM (
      SELECT request_id, call_type, model_group, model, status, prompt_tokens,
             completion_tokens, total_tokens, request_duration_ms, "startTime" AS started_at,
             "endTime" AS ended_at, proxy_server_request AS request_body, messages, response AS response_body
      FROM "LiteLLM_SpendLogs" WHERE "endTime" >= NOW() - INTERVAL '7 days'
      ORDER BY "endTime" DESC NULLS LAST LIMIT {limit}) AS log_row;'''
    try:
        result = compose("exec", "-T", "db", "psql", "-U", "llmproxy", "-d", "litellm", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=18)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error)), "items": []}
    if result.returncode != 0:
        return {"ok": False, "error": redact(result.stderr or result.stdout), "items": []}
    try:
        rows = json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": "SpendLogs 返回格式无法解析。", "items": []}
    items = []
    for row in rows if isinstance(rows, list) else []:
        items.append({"request_id": redact(row.get("request_id", "")), "call_type": redact(row.get("call_type", "")),
                      "model": redact(row.get("model_group") or row.get("model") or ""), "status": redact(row.get("status", "unknown")),
                      "prompt_tokens": row.get("prompt_tokens") or 0, "completion_tokens": row.get("completion_tokens") or 0,
                      "total_tokens": row.get("total_tokens") or 0, "duration_ms": row.get("request_duration_ms"),
                      "started_at": row.get("started_at"), "ended_at": row.get("ended_at"),
                      "request_body": redact(json.dumps(row.get("request_body"), ensure_ascii=False)) if row.get("request_body") is not None else None,
                      "messages": redact(json.dumps(row.get("messages"), ensure_ascii=False)) if row.get("messages") is not None else None,
                      "response_body": redact(json.dumps(row.get("response_body"), ensure_ascii=False)) if row.get("response_body") is not None else None})
    return {"ok": True, "retention_days": 7, "count": len(items), "items": items}


def usage_summary(window_key: str = "1h") -> dict[str, Any]:
    if window_key not in WINDOWS:
        return {"ok": False, "error": "统计区间只支持 15m、1h、24h、7d。"}
    label, interval = WINDOWS[window_key]
    sql = f'''SELECT json_build_object(
      'requests', COUNT(*),
      'successful_requests', COUNT(*) FILTER (WHERE lower(COALESCE(status,'')) IN ('success','successful','200')),
      'failed_requests', COUNT(*) FILTER (WHERE lower(COALESCE(status,'')) IN ('failure','failed','error') OR lower(COALESCE(status,'')) ~ '^[45][0-9][0-9]$'),
      'prompt_tokens', COALESCE(SUM(prompt_tokens),0), 'completion_tokens', COALESCE(SUM(completion_tokens),0),
      'total_tokens', COALESCE(SUM(total_tokens),0), 'average_duration_ms', ROUND(AVG(request_duration_ms)::numeric,1),
      'models', COALESCE((SELECT json_agg(to_jsonb(x) ORDER BY x.total_tokens DESC) FROM (
        SELECT COALESCE(NULLIF(model_group,''),NULLIF(model,''),'unknown') AS model, COUNT(*) AS requests,
        COALESCE(SUM(total_tokens),0) AS total_tokens FROM "LiteLLM_SpendLogs"
        WHERE "endTime" >= NOW() - INTERVAL '{interval}' AND "endTime" <= NOW()
        GROUP BY 1 ORDER BY COALESCE(SUM(total_tokens),0) DESC LIMIT 10) x),'[]'::json))::text
      FROM "LiteLLM_SpendLogs" WHERE "endTime" >= NOW() - INTERVAL '{interval}' AND "endTime" <= NOW();'''
    try:
        result = compose("exec", "-T", "db", "psql", "-U", "llmproxy", "-d", "litellm", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=18)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))}
    if result.returncode != 0:
        return {"ok": False, "error": redact(result.stderr or result.stdout)}
    try:
        usage = json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": "用量汇总返回格式无法解析。"}
    return {"ok": True, "window": window_key, "window_label": label,
            "refreshed_at": datetime.now(timezone.utc).isoformat(), "usage": usage,
            "gateway_metrics": current_gateway_metrics()}


def current_gateway_metrics() -> dict[str, Any]:
    status = 0
    text = ""
    try:
        request = Request("http://127.0.0.1:29090/stats/prometheus", headers={"Accept": "text/plain"})
        with OPENER.open(request, timeout=5) as response:
            status = response.status
            text = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError, TimeoutError):
        pass
    values: dict[str, float] = {}
    for name in ("envoy_http_downstream_rq_total", "envoy_http_downstream_rq_time_count"):
        matches = re.findall(rf"^{name}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)$", text, re.M)
        try:
            values[name] = sum(float(item) for item in matches)
        except ValueError:
            values[name] = 0
    available = status == 200 and bool(values)
    return {"ok": available, "requests_total": round(values.get("envoy_http_downstream_rq_total", 0)) if available else None,
            "latency_samples_total": round(values.get("envoy_http_downstream_rq_time_count", 0)) if available else None}


def probe(kind: str, app_id: str, model: str = MODEL) -> dict[str, Any]:
    app = app_by_id(app_id)
    if not app:
        return {"ok": False, "status": 400, "error": "未知应用。"}
    key = higress_key(app)
    if not key:
        return {"ok": False, "status": 0, "error": "应用 Key-H 未配置。"}
    started = time.monotonic()
    marker = "p1-console-" + uuid.uuid4().hex[:8]
    if kind == "models":
        status, body, _ = http_json(GATEWAY_URL + "/v1/models", key=key, host=GATEWAY_HOST, timeout=15)
        ids = [item.get("id") for item in body.get("data", []) if isinstance(item, dict)] if isinstance(body, dict) else []
        return {"ok": status == 200 and bool(ids), "status": status, "models": ids, "latency_ms": round((time.monotonic()-started)*1000)}
    if kind == "unauthorized-model":
        model = "not-authorized-model"
    if kind not in {"chat", "stream", "responses", "tool", "embedding", "image", "unauthorized-model"}:
        return {"ok": False, "status": 400, "error": "未知验证项目。"}
    if kind != "unauthorized-model" and model not in app.get("models", []):
        return {"ok": False, "status": 400, "error": "验证模型不在该应用授权清单中。"}
    if kind in {"chat", "stream", "responses", "tool"} and model_kind(model) != "chat":
        return {"ok": False, "status": 400, "error": "该模型不是聊天模型，请选择向量或图片验证。"}
    if kind == "embedding" and model_kind(model) != "embedding":
        return {"ok": False, "status": 400, "error": "请选择 Embedding 模型。"}
    if kind == "image" and model_kind(model) != "image":
        return {"ok": False, "status": 400, "error": "请选择图片生成模型。"}
    if kind == "embedding":
        status, body, _ = http_json(GATEWAY_URL + "/v1/embeddings", key=key, host=GATEWAY_HOST,
                                    body={"model": model, "input": ["校园卡挂失", "重置登录密码", "天气与户外运动"], "encoding_format": "float"}, timeout=90)
        entries = body.get("data", []) if isinstance(body, dict) else []
        dimensions = len(entries[0].get("embedding", [])) if entries and isinstance(entries[0], dict) and isinstance(entries[0].get("embedding"), list) else 0
        return {"ok": status == 200 and len(entries) == 3 and dimensions > 0, "status": status,
                "vectors": len(entries), "dimensions": dimensions, "usage": body.get("usage", {}) if isinstance(body, dict) else {},
                "latency_ms": round((time.monotonic()-started)*1000), "error": redact(json.dumps(body, ensure_ascii=False)[:1200]) if status != 200 else ""}
    if kind == "image":
        status, body, _ = http_json(GATEWAY_URL + "/v1/images/generations", key=key, host=GATEWAY_HOST,
                                    body={"model": model, "prompt": "A simple red flower on a white background", "n": 1}, timeout=120)
        images = body.get("data", []) if isinstance(body, dict) else []
        valid = any(isinstance(item, dict) and any(isinstance(item.get(field), str) and item[field].strip() for field in ("url", "b64_json")) for item in images)
        return {"ok": status == 200 and bool(images) and valid, "status": status, "images": len(images),
                "latency_ms": round((time.monotonic()-started)*1000), "error": redact(json.dumps(body, ensure_ascii=False)[:1200]) if status != 200 else ""}
    if kind in {"chat", "unauthorized-model"}:
        status, body, _ = http_json(GATEWAY_URL + "/v1/chat/completions", key=key, host=GATEWAY_HOST,
                                    body={"model": model, "messages": [{"role": "user", "content": "P1 console probe. Reply OK."}], "user": marker, "temperature": 0, "max_tokens": 128}, timeout=90)
        text = ""
        reasoning = ""
        has_choice = False
        if isinstance(body, dict):
            choices = body.get("choices", [])
            if choices and isinstance(choices[0], dict):
                has_choice = True
                message = choices[0].get("message", {})
                if isinstance(message, dict):
                    text = str(message.get("content") or "")
                    reasoning = str(message.get("reasoning_content") or "")
        expected_denied = kind == "unauthorized-model"
        return {"ok": status == 403 if expected_denied else status == 200 and has_choice, "status": status,
                "text": redact(text[:1000]), "reasoning": redact(reasoning[:1000]),
                "usage": body.get("usage", {}) if isinstance(body, dict) else {},
                "latency_ms": round((time.monotonic()-started)*1000), "error": redact(json.dumps(body, ensure_ascii=False)[:1200]) if status not in (200, 403) else ""}
    if kind == "responses":
        status, body, _ = http_json(GATEWAY_URL + "/v1/responses", key=key, host=GATEWAY_HOST,
                                    body={"model": model, "input": f"Reply with this marker only: {marker}", "max_output_tokens": 128}, timeout=90)
        output = body.get("output_text", "") if isinstance(body, dict) else ""
        valid_response = isinstance(body, dict) and bool(body.get("id") or body.get("object") or body.get("status"))
        return {"ok": status == 200 and valid_response, "status": status, "text": redact(str(output)[:1000]),
                "usage": body.get("usage", {}) if isinstance(body, dict) else {}, "latency_ms": round((time.monotonic()-started)*1000),
                "error": redact(json.dumps(body, ensure_ascii=False)[:1200]) if status != 200 else ""}
    headers = {"Accept": "text/event-stream", "Authorization": "Bearer " + key, "Content-Type": "application/json", "Host": GATEWAY_HOST}
    if kind == "stream":
        payload = {"model": model, "messages": [{"role": "user", "content": "只回复：P1_STREAM_OK"}], "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 128}
        request = Request(GATEWAY_URL + "/v1/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers, method="POST")
        parts: list[str] = []; done = False; usage: dict[str, Any] = {}; status = 0
        try:
            with OPENER.open(request, timeout=90) as response:
                status = response.status
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"): continue
                    item = line[5:].strip()
                    if item == "[DONE]": done = True; break
                    try: chunk = json.loads(item)
                    except json.JSONDecodeError: continue
                    if isinstance(chunk.get("usage"), dict): usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = (choice.get("delta") or {}) if isinstance(choice, dict) else {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
                        if isinstance(content, str): parts.append(content)
                        elif isinstance(reasoning, str): parts.append(reasoning)
        except HTTPError as error:
            status = error.code
        except (URLError, OSError, TimeoutError) as error:
            return {"ok": False, "status": status, "error": redact(str(error)), "latency_ms": round((time.monotonic()-started)*1000)}
        text = "".join(parts)
        return {"ok": status == 200 and done, "status": status, "done": done, "text": redact(text[:1000]), "usage": usage, "latency_ms": round((time.monotonic()-started)*1000)}
    # Keep this probe aligned with Test-P1ProtocolMatrix.ps1: the current
    # P1 acceptance path validates OpenAI Chat Completions function calling.
    tool_payload = {"model": model, "messages": [{"role": "user", "content": "Call the gateway_probe tool."}],
                    "tools": [{"type": "function", "function": {"name": "gateway_probe", "description": "P1 console probe",
                    "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}],
                    "tool_choice": {"type": "function", "function": {"name": "gateway_probe"}}, "max_tokens": 128}
    status, body, _ = http_json(GATEWAY_URL + "/v1/chat/completions", key=key, host=GATEWAY_HOST, body=tool_payload, timeout=90)
    calls: list[Any] = []
    if isinstance(body, dict):
        choices = body.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict): calls = message.get("tool_calls", []) or []
    return {"ok": status == 200, "status": status, "tool_call": bool(calls),
            "usage": body.get("usage", {}) if isinstance(body, dict) else {}, "latency_ms": round((time.monotonic()-started)*1000),
            "error": redact(json.dumps(body, ensure_ascii=False)[:1200]) if status != 200 else ""}


def manage_action(action: str, app: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"Get", "SetPolicy", "Disable", "Enable", "Reconcile", "RotateKeyH", "RetireKeyH"}
    if action not in allowed:
        return {"ok": False, "error": "不支持的管理动作。"}
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "Manage-P1Application.ps1"), "-Action", action, "-AppId", app["app_id"], "-ChangedBy", "p1-console"]
    if action == "SetPolicy":
        models = payload.get("models")
        rpm, tpm, parallel = payload.get("rpm_limit"), payload.get("tpm_limit"), payload.get("max_parallel_requests")
        if not isinstance(models, list) or not models or not all(isinstance(item, str) and item in app.get("models", []) for item in models):
            return {"ok": False, "error": "策略模型必须来自应用当前允许模型清单。"}
        organization_allowed = set(organization_policy().get("allowed_models", []))
        if not set(models).issubset(organization_allowed):
            return {"ok": False, "error": "应用模型不能超出当前组织允许模型集合。"}
        if not all(isinstance(value, int) and 0 < value <= 10000000 for value in (rpm, tpm, parallel)):
            return {"ok": False, "error": "RPM、TPM、最大并发必须是正整数。"}
        models_b64 = base64.b64encode(json.dumps(models, ensure_ascii=False).encode("utf-8")).decode("ascii")
        command += ["-ModelsB64", models_b64, "-RpmLimit", str(rpm), "-TpmLimit", str(tpm), "-MaxParallelRequests", str(parallel)]
    if action == "RetireKeyH":
        command.append("-RestartHigress")
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))}
    output = redact((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
    return {"ok": result.returncode == 0, "exit_code": result.returncode, "output": output or ("操作完成。" if result.returncode == 0 else "操作失败。")}


def run_script(script_name: str, args: list[str] | None = None, timeout: int = 240) -> dict[str, Any]:
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / script_name), *(args or [])]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))}
    output = redact((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))
    return {"ok": result.returncode == 0, "exit_code": result.returncode,
            "output": output or ("操作完成。" if result.returncode == 0 else "操作失败。")}


def create_app(payload: dict[str, Any]) -> dict[str, Any]:
    display_name = payload.get("display_name")
    models = payload.get("models")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80:
        return {"ok": False, "error": "应用名称不能为空且不得超过 80 个字符。"}
    if not isinstance(models, list) or not models or not all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", item) for item in models):
        return {"ok": False, "error": "模型列表格式无效。"}
    if not set(models).issubset(set(organization_policy().get("allowed_models", []))):
        return {"ok": False, "error": "创建应用的模型必须是当前组织允许模型的子集。"}
    limits = [payload.get("rpm_limit", 6), payload.get("tpm_limit", 6000), payload.get("max_parallel_requests", 1)]
    if not all(isinstance(value, int) and 0 < value <= 10000000 for value in limits):
        return {"ok": False, "error": "RPM、TPM、最大并发必须是正整数。"}
    models_b64 = base64.b64encode(json.dumps(models, ensure_ascii=False).encode("utf-8")).decode("ascii")
    args = ["-DisplayName", display_name.strip(), "-ModelsB64", models_b64,
            "-RpmLimit", str(limits[0]), "-TpmLimit", str(limits[1]), "-MaxParallelRequests", str(limits[2]),
            "-ChangedBy", "p1-console"]
    if isinstance(payload.get("app_id"), str) and payload["app_id"].strip():
        if not re.fullmatch(r"app-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", payload["app_id"].strip()):
            return {"ok": False, "error": "app_id 必须是 app-<UUID> 格式。"}
        args += ["-AppId", payload["app_id"].strip()]
    return run_script("New-P1Application.ps1", args, timeout=300)


def snapshot_names() -> list[str]:
    if not SNAPSHOT_ROOT.is_dir():
        return []
    return sorted([item.name for item in SNAPSHOT_ROOT.iterdir() if item.is_dir() and re.fullmatch(r"[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_-]+)?", item.name)], reverse=True)


def start_job(kind: str) -> dict[str, Any]:
    global ACTIVE_JOB
    scripts = {"technical-gate": "Run-P1TechnicalGate.ps1", "native-policy": "Test-P1NativePolicy.ps1",
               "mapper": "Test-P1Mapper.ps1", "protocol-matrix": "Test-P1ProtocolMatrix.ps1",
               "latency": "Benchmark-P1Latency.ps1"}
    if kind not in scripts:
        return {"ok": False, "error": "未知验证任务。"}
    with JOB_LOCK:
        if ACTIVE_JOB:
            return {"ok": False, "error": "已有验证任务运行中。"}
        job_id = "p1-" + uuid.uuid4().hex[:10]
        JOBS[job_id] = {"job_id": job_id, "kind": kind, "state": "running", "output": [], "started_at": datetime.now(timezone.utc).isoformat()}
        ACTIVE_JOB = job_id

    def worker() -> None:
        global ACTIVE_JOB
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / scripts[kind])]
        try:
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            assert process.stdout is not None
            for line in process.stdout:
                with JOB_LOCK: JOBS[job_id]["output"].append(redact(line.rstrip()))
            code = process.wait()
            with JOB_LOCK: JOBS[job_id].update(state="completed" if code == 0 else "failed", exit_code=code)
        except Exception as error:
            with JOB_LOCK: JOBS[job_id].update(state="failed", error=redact(str(error)))
        finally:
            with JOB_LOCK: ACTIVE_JOB = None
    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "p1-console"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def allowed(self) -> bool:
        return self.headers.get("Host", "").lower() in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self) -> None:
        if not self.allowed(): self.send_error(421); return
        path = urlsplit(self.path).path
        if path == "/api/status":
            try: self.send_json(status_payload())
            except Exception as error: self.send_json({"ok": False, "error": "状态读取失败：" + redact(str(error))}, 500)
            return
        if path == "/api/apps":
            self.send_json({"ok": True, "apps": app_payload(True)}); return
        if path == "/api/organization/policy":
            self.send_json({"ok": True, "organization_policy": organization_policy()}); return
        if path == "/api/snapshots":
            self.send_json({"ok": True, "snapshots": snapshot_names()}); return
        if path == "/api/usage":
            window = parse_qs(urlsplit(self.path).query).get("window", ["1h"])[0]
            self.send_json(usage_summary(window), 200 if window in WINDOWS else 400); return
        if path == "/api/logs":
            try: limit = int(parse_qs(urlsplit(self.path).query).get("limit", ["30"])[0])
            except ValueError: limit = 30
            self.send_json(recent_logs(limit)); return
        if path.startswith("/api/jobs/"):
            with JOB_LOCK: job = dict(JOBS.get(path.rsplit("/", 1)[-1], {}))
            self.send_json(job or {"ok": False, "error": "任务不存在。"}, 200 if job else 404); return
        files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        filename = files.get(path)
        if not filename: self.send_error(404); return
        target = STATIC / filename
        try: data = target.read_bytes()
        except OSError: self.send_error(404); return
        content_type = "text/html; charset=utf-8" if filename.endswith(".html") else ("text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8")
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self) -> None:
        if not self.allowed(): self.send_error(421); return
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = 0
        if length < 0 or length > MAX_BODY: self.send_json({"ok": False, "error": "请求体过大。"}, 413); return
        try: body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): self.send_json({"ok": False, "error": "请求必须是 JSON。"}, 400); return
        if not isinstance(body, dict): self.send_json({"ok": False, "error": "请求格式无效。"}, 400); return
        path = urlsplit(self.path).path
        if path == "/api/probe":
            kind, app_id, model = body.get("kind"), body.get("app_id"), body.get("model", MODEL)
            if kind not in {"models", "chat", "stream", "responses", "tool", "embedding", "image", "unauthorized-model"} or not isinstance(app_id, str) or not isinstance(model, str) or len(model) > 128:
                self.send_json({"ok": False, "error": "验证参数无效。"}, 400); return
            self.send_json(probe(kind, app_id, model)); return
        if path == "/api/app/connection":
            app_id = body.get("app_id")
            if not isinstance(app_id, str):
                self.send_json({"ok": False, "error": "应用参数无效。"}, 400); return
            reveal = body.get("reveal") is True
            if reveal and body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "显示完整 Key 前请先确认仅用于本机测试。"}, 409); return
            self.send_json(connection_info(app_id, reveal)); return
        if path == "/api/organization/policy":
            if body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "组织模型策略会影响新建和现有应用，请先确认。"}, 409); return
            models = body.get("allowed_models")
            if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
                self.send_json({"ok": False, "error": "组织模型列表格式无效。"}, 400); return
            self.send_json(save_organization_policy(models), 200); return
        if path == "/api/app/action":
            action, app_id = body.get("action"), body.get("app_id")
            app = app_by_id(app_id) if isinstance(app_id, str) else None
            if not app: self.send_json({"ok": False, "error": "未知应用。"}, 400); return
            if action in {"Disable", "Enable", "RotateKeyH", "RetireKeyH"} and body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "此操作会修改 P1 运行态，请先确认。"}, 409); return
            self.send_json(manage_action(action, app, body)); return
        if path == "/api/app/create":
            if body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "创建应用会写入清单、创建 Virtual Key、更新 Higress 并执行真实验证，请先确认。"}, 409)
                return
            self.send_json(create_app(body), 200); return
        if path == "/api/config/snapshot":
            if body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "创建 Higress 快照前请先确认。"}, 409); return
            self.send_json(run_script("Snapshot-P1Higress.ps1", timeout=120)); return
        if path == "/api/config/publish":
            if body.get("confirm") is not True:
                self.send_json({"ok": False, "error": "发布 Higress 配置会改变 P1 运行态，请先确认。"}, 409); return
            self.send_json(run_script("Publish-P1Higress.ps1", timeout=240)); return
        if path == "/api/config/rollback":
            snapshot_id = body.get("snapshot_id")
            if body.get("confirm") is not True or not isinstance(snapshot_id, str) or snapshot_id not in snapshot_names():
                self.send_json({"ok": False, "error": "回滚必须选择本机 runtime/p1-test/higress/snapshots 下的有效快照并确认。"}, 400); return
            snapshot_path = str(SNAPSHOT_ROOT / snapshot_id / "resources.json")
            self.send_json(run_script("Rollback-P1Higress.ps1", ["-SnapshotPath", snapshot_path, "-ChangedBy", "p1-console"], timeout=240)); return
        if path == "/api/job":
            if body.get("confirm") is not True: self.send_json({"ok": False, "error": "验证脚本会修改/重启测试组件，请先确认。"}, 409); return
            result = start_job(str(body.get("kind", ""))); self.send_json(result, 200 if result.get("ok") else 409); return
        self.send_error(404)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ConsoleHandler)
    print(f"P1 console listening on http://127.0.0.1:{PORT}/", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
