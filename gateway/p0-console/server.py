"""Loopback-only web console for the isolated local P0 gateway."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATIC = Path(__file__).resolve().parent / "static"
ENV_FILE = ROOT / ".env.p0-test"
ROOT_ENV = ROOT / ".env"
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.p0-test.yml"
LITELLM_CONFIG = ROOT / "config" / "litellm.yaml"
REPORT_ROOT = ROOT / "runtime" / "p0-test" / "reports"
GATEWAY_URL = "http://127.0.0.1:18080"
GATEWAY_HOST = "ai-gateway-test.local"
MODEL = "my-qwen3.6-27b"
USAGE_WINDOWS = {"15m": ("最近 15 分钟", "15 minutes"), "1h": ("最近 1 小时", "1 hour"),
                 "24h": ("最近 24 小时", "24 hours"), "7d": ("最近 7 天", "7 days")}
PORT = int(os.environ.get("P0_CONSOLE_PORT", "18770"))
MAX_BODY = 8192
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_JOB: str | None = None
OPENER = build_opener(ProxyHandler({}))


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    return values


def configured_model_aliases() -> set[str]:
    """Return the model aliases LiteLLM is configured to expose."""
    try:
        source = LITELLM_CONFIG.read_text(encoding="utf-8-sig")
    except OSError:
        return {MODEL}
    aliases = set(re.findall(r"(?m)^\s*-\s*model_name:\s*['\"]?([^'\"\s#]+)", source))
    return aliases or {MODEL}


def secrets() -> tuple[str, ...]:
    values = load_env(ENV_FILE)
    other = load_env(ROOT_ENV)
    selected = [
        values.get("P0_HIGRESS_CONSUMER_KEY", ""),
        values.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", ""),
        values.get("P0_LITELLM_MASTER_KEY", ""),
        *[value for key, value in other.items() if "KEY" in key.upper() or "TOKEN" in key.upper()],
    ]
    return tuple(value for value in selected if value)


def redact(value: str) -> str:
    for secret in secrets():
        value = value.replace(secret, "[REDACTED]")
    return value


def gateway_request(path: str, key: str | None = None, body: dict[str, Any] | None = None,
                    host: str | None = GATEWAY_HOST, timeout: int = 45) -> tuple[int, Any, dict[str, str]]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if host:
        headers["Host"] = host
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(GATEWAY_URL + path, data=payload, headers=headers,
                      method="POST" if payload is not None else "GET")
    try:
        with OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
            response_headers = dict(response.headers.items())
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = error.code
        response_headers = dict(error.headers.items())
    except (URLError, OSError, TimeoutError) as error:
        return 0, {"error": redact(str(error))}, {}
    try:
        return status, json.loads(raw), response_headers
    except json.JSONDecodeError:
        return status, redact(raw[:3000]), response_headers


def compose(*args: str, timeout: int = 12) -> subprocess.CompletedProcess[str]:
    if not ENV_FILE.is_file():
        raise FileNotFoundError("本地 P0 环境尚未初始化：缺少 .env.p0-test")
    return subprocess.run(
        ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def run_json(url: str, timeout: int = 4) -> tuple[int, Any]:
    try:
        with OPENER.open(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return error.code, {}
    except (URLError, OSError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def latest_report() -> dict[str, Any] | None:
    if not REPORT_ROOT.is_dir():
        return None
    paths = [p for p in REPORT_ROOT.glob("p0-*.json") if not p.name.endswith("-functional.json")]
    if not paths:
        return None
    try:
        data = json.loads(max(paths, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "UNKNOWN": 0}
    for item in data.get("results", []):
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    spend = data.get("spendlogs", {})
    return {
        "run_id": data.get("run_id", ""),
        "generated_at": data.get("generated_at", ""),
        "git_commit": data.get("git_commit", ""),
        "conclusion": data.get("conclusion", ""),
        "counts": counts,
        "results": [{
            "status": item.get("status"),
            "category": item.get("category"),
            "message": item.get("message"),
            "evidence": item.get("evidence", ""),
        } for item in data.get("results", [])],
        "spendlogs": {
            "logged_requests": spend.get("logged_requests", 0),
            "request_bodies": spend.get("request_bodies", 0),
            "response_bodies": spend.get("response_bodies", 0),
            "total_tokens": spend.get("total_tokens", 0),
            "observed_spend": spend.get("observed_spend", "unknown"),
            "secret_leak_rows": spend.get("secret_leak_rows", 0),
        },
    }


def release_snapshot_summary() -> dict[str, Any]:
    release_root = ROOT / "runtime" / "p0-test" / "releases"
    snapshots = []
    if release_root.is_dir():
        for manifest_path in release_root.glob("*/manifest.json"):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            release_id = data.get("release_id", "")
            if data.get("kind") == "release" and re.fullmatch(r"[A-Za-z0-9-]+", release_id):
                snapshots.append({"release_id": release_id, "created_at": data.get("created_at", "")})
    snapshots.sort(key=lambda item: item["created_at"])
    latest = snapshots[-1] if snapshots else None
    return {"count": len(snapshots), "latest": latest}


def sanitize_log_value(value: Any, key_name: str = "") -> Any:
    sensitive_key = re.compile(r"(authorization|api[_-]?key|(?:^|[_-])token(?:$|[_-])|secret|password|credential)", re.I)
    if key_name and sensitive_key.search(key_name):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): sanitize_log_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, str):
        result = redact(value)
        result = re.sub(r"(?i)Bearer\s+[^\s,\"']+", "Bearer [REDACTED]", result)
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", result)
        if len(result) > 16000:
            return result[:16000] + "…[正文已截断]"
        return result
    return value


def recent_spendlogs(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    sql = f'''SELECT COALESCE(json_agg(to_jsonb(log_row)), '[]'::json)::text
FROM (
  SELECT request_id, call_type, model_group, model, status,
         prompt_tokens, completion_tokens, total_tokens,
         request_duration_ms, "startTime" AS started_at, "endTime" AS ended_at,
         proxy_server_request AS request_body, messages, response AS response_body
  FROM "LiteLLM_SpendLogs"
  WHERE "endTime" >= NOW() - INTERVAL '7 days'
  ORDER BY "endTime" DESC NULLS LAST
  LIMIT {limit}
) AS log_row;'''
    try:
        result = compose("exec", "-T", "db", "psql", "-U", "llmproxy", "-d", "litellm",
                         "-At", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=18)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))[:400], "items": []}
    if result.returncode != 0:
        return {"ok": False, "error": redact((result.stderr or result.stdout).strip())[:400], "items": []}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        rows = json.loads(lines[-1]) if lines else []
    except json.JSONDecodeError:
        return {"ok": False, "error": "LiteLLM SpendLogs 返回格式无法解析。", "items": []}
    items = []
    for row in rows if isinstance(rows, list) else []:
        items.append({
            "request_id": redact(str(row.get("request_id") or "")),
            "call_type": redact(str(row.get("call_type") or "")),
            "model": redact(str(row.get("model_group") or row.get("model") or "")),
            "status": redact(str(row.get("status") or "unknown")),
            "prompt_tokens": row.get("prompt_tokens") or 0,
            "completion_tokens": row.get("completion_tokens") or 0,
            "total_tokens": row.get("total_tokens") or 0,
            "duration_ms": row.get("request_duration_ms"),
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "request_body": sanitize_log_value(row.get("request_body")),
            "messages": sanitize_log_value(row.get("messages")),
            "response_body": sanitize_log_value(row.get("response_body")),
        })
    return {"ok": True, "retention_days": 7, "count": len(items), "items": items}


def usage_summary(window_key: str = "1h") -> dict[str, Any]:
    """Aggregate LiteLLM SpendLogs and native Higress metrics for a bounded window."""
    window = USAGE_WINDOWS.get(window_key)
    if not window:
        return {"ok": False, "error": "不支持的统计区间。"}
    window_label, interval = window
    sql = f'''SELECT json_build_object(
  'requests', COUNT(*),
  'successful_requests', COUNT(*) FILTER (WHERE lower(COALESCE(status, '')) IN ('success', 'successful', '200')),
  'failed_requests', COUNT(*) FILTER (WHERE lower(COALESCE(status, '')) IN ('failure', 'failed', 'error') OR lower(COALESCE(status, '')) ~ '^[45][0-9][0-9]$'),
  'prompt_tokens', COALESCE(SUM(prompt_tokens), 0),
  'completion_tokens', COALESCE(SUM(completion_tokens), 0),
  'total_tokens', COALESCE(SUM(total_tokens), 0),
  'average_duration_ms', ROUND(AVG(request_duration_ms)::numeric, 1),
  'models', COALESCE((
    SELECT json_agg(to_jsonb(model_row) ORDER BY model_row.total_tokens DESC)
    FROM (
      SELECT COALESCE(NULLIF(model_group, ''), NULLIF(model, ''), 'unknown') AS model,
             COUNT(*) AS requests,
             COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
             COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
             COALESCE(SUM(total_tokens), 0) AS total_tokens
      FROM "LiteLLM_SpendLogs"
      WHERE "endTime" >= NOW() - INTERVAL '{interval}' AND "endTime" <= NOW()
      GROUP BY 1
      ORDER BY COALESCE(SUM(total_tokens), 0) DESC
      LIMIT 10
    ) AS model_row
  ), '[]'::json)
)::text
FROM "LiteLLM_SpendLogs"
WHERE "endTime" >= NOW() - INTERVAL '{interval}' AND "endTime" <= NOW();'''
    try:
        result = compose("exec", "-T", "db", "psql", "-U", "llmproxy", "-d", "litellm",
                         "-At", "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=18)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": redact(str(error))[:400]}
    if result.returncode != 0:
        return {"ok": False, "error": redact((result.stderr or result.stdout).strip())[:400]}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        usage = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "LiteLLM 用量汇总无法解析。"}

    matcher = 'job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080"'
    code_query = f"sum by (response_code_class) (increase(envoy_http_downstream_rq{{{matcher}}}[{window_key}]))"
    code_status, code_data = run_json(
        "http://127.0.0.1:19090/api/v1/query?" + urlencode({"query": code_query}), timeout=4)
    code_series = code_data.get("data", {}).get("result", []) if isinstance(code_data, dict) else []
    code_counts: dict[str, float] = {}
    if code_status == 200:
        for row in code_series:
            labels = row.get("metric", {})
            sample = row.get("value", [None, "0"])
            try:
                code_counts[str(labels.get("response_code_class", "unknown"))] = float(sample[1])
            except (TypeError, ValueError, IndexError):
                continue

    latency_query = f"sum(increase(envoy_http_downstream_rq_time_sum{{{matcher}}}[{window_key}])) / clamp_min(sum(increase(envoy_http_downstream_rq_time_count{{{matcher}}}[{window_key}])), 1)"
    latency_status, latency_data = run_json(
        "http://127.0.0.1:19090/api/v1/query?" + urlencode({"query": latency_query}), timeout=4)
    latency_series = latency_data.get("data", {}).get("result", []) if isinstance(latency_data, dict) else []
    average_gateway_latency = None
    if latency_status == 200 and latency_series:
        try:
            average_gateway_latency = round(float(latency_series[0]["value"][1]), 1)
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    usage["models"] = usage.get("models") or []
    usage["gateway"] = {
        "ok": code_status == 200,
        "requests": round(sum(code_counts.values())) if code_status == 200 else None,
        "2xx": round(code_counts.get("2xx", 0)) if code_status == 200 else None,
        "4xx": round(code_counts.get("4xx", 0)) if code_status == 200 else None,
        "5xx": round(code_counts.get("5xx", 0)) if code_status == 200 else None,
        "average_latency_ms": average_gateway_latency,
    }
    return {"ok": True, "window": window_key, "window_label": window_label,
            "refreshed_at": datetime.now(timezone.utc).isoformat(), "usage": usage}


def status_payload() -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    services: list[dict[str, Any]] = []
    try:
        result = compose("ps", "--format", "json")
        if result.returncode == 0:
            try:
                parsed = json.loads(result.stdout)
                raw_services = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                raw_services = []
                for line in result.stdout.splitlines():
                    try:
                        item = json.loads(line)
                        raw_services.extend(item if isinstance(item, list) else [item])
                    except json.JSONDecodeError:
                        continue
            for item in raw_services:
                name = item.get("Service") or item.get("Name") or "unknown"
                publishers = item.get("Publishers") or []
                ports = []
                unsafe = False
                for publisher in publishers:
                    published = publisher.get("PublishedPort")
                    if published:
                        host = publisher.get("URL") or ""
                        ports.append(f"{host}:{published}")
                        if host not in ("127.0.0.1", "localhost", "::1"):
                            unsafe = True
                health = item.get("Health") or "n/a"
                state = item.get("State") or "unknown"
                services.append({
                    "name": name, "state": state, "health": health,
                    "ports": ports, "safe_ports": not unsafe,
                })
            expected = {"db", "redis", "litellm", "higress", "prometheus", "log-pruner"}
            present = {item["name"] for item in services}
            stack_ok = expected.issubset(present) and all(
                item["state"] == "running" and item["health"] in ("healthy", "n/a", "")
                for item in services
            ) and all(item["safe_ports"] for item in services)
            checks.append({"id": "containers", "label": "P0 服务容器", "status": "PASS" if stack_ok else "FAIL",
                           "detail": f"{len(services)} 个服务；预期 {len(expected)} 个"})
        else:
            checks.append({"id": "containers", "label": "P0 服务容器", "status": "FAIL",
                           "detail": redact((result.stderr or result.stdout).strip()[:240])})
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as error:
        checks.append({"id": "containers", "label": "P0 服务容器", "status": "FAIL", "detail": redact(str(error)[:240])})

    try:
        resolved = sorted({entry[4][0] for entry in socket.getaddrinfo(GATEWAY_HOST, 18080, type=socket.SOCK_STREAM)})
    except OSError:
        resolved = []
    host_ok = resolved == ["127.0.0.1"]
    checks.append({"id": "host", "label": "测试域名解析", "status": "PASS" if host_ok else "FAIL",
                   "detail": ", ".join(resolved) if resolved else "未解析到地址"})

    env = load_env(ENV_FILE)
    key = env.get("P0_HIGRESS_CONSUMER_KEY", "")
    legacy = env.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", "")
    status, models, _ = gateway_request("/v1/models", key=key, timeout=5) if key else (0, {}, {})
    model_ids = [item.get("id") for item in models.get("data", []) if isinstance(item, dict)] if isinstance(models, dict) else []
    gateway_ok = status == 200 and MODEL in model_ids
    checks.append({"id": "gateway", "label": "Higress → LiteLLM", "status": "PASS" if gateway_ok else "FAIL",
                   "detail": f"HTTP {status}；模型目录 {len(model_ids)} 项"})

    direct_status, _ = run_json("http://127.0.0.1:14000/health/liveliness", timeout=3)
    checks.append({"id": "litellm", "label": "LiteLLM 存活", "status": "PASS" if direct_status == 200 else "FAIL",
                   "detail": f"HTTP {direct_status or '不可达'}"})

    prom_status, prom = run_json("http://127.0.0.1:19090/api/v1/query?" + urlencode({"query": 'up{job="higress"}'}), timeout=3)
    prom_values = prom.get("data", {}).get("result", []) if isinstance(prom, dict) else []
    prom_up = prom_status == 200 and any(row.get("value", [None, "0"])[1] == "1" for row in prom_values)
    checks.append({"id": "prometheus", "label": "Prometheus 抓取 Higress", "status": "PASS" if prom_up else "FAIL",
                   "detail": "up=1" if prom_up else f"HTTP {prom_status or '不可达'}；target 未就绪"})

    metric_values: dict[str, int | None] = {}
    metric_queries = {
        "gateway_requests": 'sum(envoy_http_downstream_rq_total{job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080"})',
        "gateway_2xx": 'sum(envoy_http_downstream_rq{job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080",response_code_class="2xx"})',
        "gateway_latency_observations": 'sum(envoy_http_downstream_rq_time_count{job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080"})',
    }
    for metric_name, query in metric_queries.items():
        metric_http, metric_response = run_json(
            "http://127.0.0.1:19090/api/v1/query?" + urlencode({"query": query}), timeout=3)
        series = metric_response.get("data", {}).get("result", []) if isinstance(metric_response, dict) else []
        try:
            metric_values[metric_name] = int(float(series[0]["value"][1])) if metric_http == 200 and series else 0
        except (KeyError, TypeError, ValueError, IndexError):
            metric_values[metric_name] = None

    latest = latest_report()
    ready = all(item["status"] == "PASS" for item in checks)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "本机隔离 Docker P0",
        "ready": ready,
        "model": MODEL,
        "gateway_url": "http://ai-gateway-test.local:18080/v1",
        "key_configured": bool(key),
        "legacy_key_configured": bool(legacy),
        "services": services,
        "checks": checks,
        "metrics": metric_values,
        "models": model_ids,
        "latest_report": latest,
        "release_snapshots": release_snapshot_summary(),
        "deferred": [
            {"id": "price", "label": "Qwen 单价", "status": "UNKNOWN", "detail": "Spend 只作观测值，不作计费依据"},
            {"id": "ip", "label": "IP 白名单", "status": "SKIP", "detail": "等待运维批准精确 CIDR"},
            {"id": "provider", "label": "Provider 网络归属", "status": "UNKNOWN", "detail": "目标服务器网络路径待运维确认"},
        ],
    }


def probe(kind: str, requested_model: str = MODEL, confirm_other_model: bool = False) -> dict[str, Any]:
    env = load_env(ENV_FILE)
    key = env.get("P0_HIGRESS_CONSUMER_KEY", "")
    if not key:
        return {"ok": False, "status": 0, "error": "没有找到本地 P0 Consumer Key，请先初始化 P0 测试环境。"}
    if kind != "models":
        if requested_model not in configured_model_aliases():
            return {"ok": False, "status": 400, "error": "所选模型不在本地 LiteLLM 配置别名中。"}
        if requested_model != MODEL and not confirm_other_model:
            return {"ok": False, "status": 409,
                    "error": "非 P0 基准模型尚未确认。请确认此别名允许真实调用且可能产生用量后再试。"}
    started = time.monotonic()
    marker = "p0-console-" + uuid.uuid4().hex[:10]
    if kind == "models":
        code, body, _ = gateway_request("/v1/models", key=key, timeout=10)
        models = [item.get("id") for item in body.get("data", []) if isinstance(item, dict)] if isinstance(body, dict) else []
        return {"ok": code == 200 and MODEL in models, "status": code, "models": models,
                "detail": f"已返回 {len(models)} 个模型别名；本地端到端验收模型为 {MODEL}。",
                "latency_ms": round((time.monotonic() - started) * 1000)}

    if kind == "chat":
        code, body, _ = gateway_request("/v1/chat/completions", key=key, body={
            "model": requested_model, "messages": [{"role": "user", "content": "只回复：P0_OK"}],
            "user": marker, "temperature": 0, "max_tokens": 512,
        })
        choices = body.get("choices", []) if isinstance(body, dict) else []
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
        text = message.get("content") or ""
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return {"ok": code == 200 and isinstance(text, str) and bool(text.strip()), "status": code,
                "text": redact(text[:2500]) if isinstance(text, str) else "", "usage": usage,
                "request_id": body.get("id", "") if isinstance(body, dict) else "",
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": redact(json.dumps(body, ensure_ascii=False)[:1600]) if code != 200 else ""}

    if kind == "stream":
        headers = {"Accept": "text/event-stream", "Authorization": "Bearer " + key,
                   "Content-Type": "application/json", "Host": GATEWAY_HOST}
        payload = json.dumps({"model": requested_model, "messages": [{"role": "user", "content": "只回复：P0_STREAM_OK"}],
                              "user": marker, "temperature": 0, "max_tokens": 512,
                              "stream": True, "stream_options": {"include_usage": True}}, ensure_ascii=False).encode("utf-8")
        request = Request(GATEWAY_URL + "/v1/chat/completions", data=payload, headers=headers, method="POST")
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        done = False
        code = 0
        try:
            with OPENER.open(request, timeout=75) as response:
                code = response.status
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event = line[5:].strip()
                    if event == "[DONE]":
                        done = True
                        break
                    try:
                        chunk = json.loads(event)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str):
                            pieces.append(content)
        except HTTPError as error:
            code = error.code
            error_body = redact(error.read().decode("utf-8", errors="replace")[:1600])
            return {"ok": False, "status": code, "error": error_body,
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        except (URLError, OSError, TimeoutError) as error:
            return {"ok": False, "status": code, "error": redact(str(error)),
                    "latency_ms": round((time.monotonic() - started) * 1000)}
        text = "".join(pieces)
        return {"ok": code == 200 and done and bool(text.strip()), "status": code, "done": done,
                "text": redact(text[:2500]), "usage": usage,
                "latency_ms": round((time.monotonic() - started) * 1000)}

    if kind == "responses":
        code, body, _ = gateway_request("/v1/responses", key=key, body={
            "model": requested_model, "input": f"Reply with this marker only: {marker}", "max_output_tokens": 128,
        })
        output = body.get("output_text", "") if isinstance(body, dict) else ""
        if not output and isinstance(body, dict):
            parts = []
            for item in body.get("output", []):
                for content in item.get("content", []) if isinstance(item, dict) else []:
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        parts.append(content["text"])
            output = " ".join(parts)
        return {"ok": code == 200 and bool(output), "status": code, "text": redact(str(output)[:2500]),
                "usage": body.get("usage", {}) if isinstance(body, dict) else {},
                "request_id": body.get("id", "") if isinstance(body, dict) else "",
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": redact(json.dumps(body, ensure_ascii=False)[:1600]) if code != 200 else ""}

    if kind == "tool":
        from scripts.test_p0_gateway import call_responses_tool_stream

        payload = {
            "model": requested_model, "stream": True,
            "input": f"Call gateway_probe with value {marker}. Do not answer in text.",
            "tools": [{"type": "function", "name": "gateway_probe", "description": "Gateway test probe",
                       "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                      "required": ["value"], "additionalProperties": False}}],
            "tool_choice": "auto",
        }
        code, completed, arguments, usage, response_id = call_responses_tool_stream(
            OPENER, GATEWAY_URL, GATEWAY_HOST, key, payload, 75)
        return {"ok": code == 200 and completed and arguments and usage, "status": code,
                "completed": completed, "valid_function_call": arguments, "has_usage": usage,
                "request_id": redact(response_id), "latency_ms": round((time.monotonic() - started) * 1000)}

    return {"ok": False, "status": 400, "error": "未知的验证项目。"}


def auth_check() -> dict[str, Any]:
    env = load_env(ENV_FILE)
    current = env.get("P0_HIGRESS_CONSUMER_KEY", "")
    legacy = env.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", "")
    cases = []
    probes = [
        ("缺少 Key", None, GATEWAY_HOST, 401),
        ("错误 Key", "p0-console-invalid-key", GATEWAY_HOST, 401),
        ("当前 Consumer Key", current, GATEWAY_HOST, 200),
        ("兼容期旧 Key", legacy, GATEWAY_HOST, 200),
        ("错误 Host", current, "p0-invalid-host.invalid", 404),
    ]
    for label, key, host, expected in probes:
        if label == "兼容期旧 Key" and not legacy:
            cases.append({"label": label, "status": "SKIP", "actual": None,
                          "detail": "当前未配置旧 Key 兼容项"})
            continue
        actual, _, _ = gateway_request("/v1/models", key=key, host=host, timeout=8)
        cases.append({"label": label, "status": "PASS" if actual == expected else "FAIL",
                      "actual": actual, "expected": expected,
                      "detail": f"HTTP {actual or '不可达'}，预期 HTTP {expected}"})
    for label, key in (("Consumer Key 不能直连 LiteLLM", current), ("LiteLLM 无 Key 拒绝直连", None)):
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = Request("http://127.0.0.1:14000/v1/models", headers=headers)
        try:
            with OPENER.open(request, timeout=5) as response:
                actual = response.status
        except HTTPError as error:
            actual = error.code
        except (URLError, OSError, TimeoutError):
            actual = 0
        cases.append({"label": label, "status": "PASS" if actual == 401 else "FAIL",
                      "actual": actual, "expected": 401,
                      "detail": f"HTTP {actual or '不可达'}，预期 HTTP 401"})
    return {"ok": all(row["status"] in ("PASS", "SKIP") for row in cases), "cases": cases}


def read_latest_markdown() -> tuple[str | None, str | None]:
    report = latest_report()
    if not report:
        return None, None
    if not re.fullmatch(r"p0-[A-Za-z0-9TzZ-]+", report["run_id"]):
        return None, None
    path = REPORT_ROOT / f"{report['run_id']}.md"
    try:
        return report["run_id"], path.read_text(encoding="utf-8")
    except OSError:
        return report["run_id"], None


def run_acceptance(job_id: str) -> None:
    global ACTIVE_JOB
    command = [sys.executable, "-u", "-X", "utf8", str(ROOT / "scripts" / "run_p0_acceptance.py"),
               "--verify-rate-limit", "--timeout", "120"]
    try:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = redact(line.rstrip())
            with JOB_LOCK:
                job = JOBS.get(job_id)
                if job:
                    job["output"].append(clean[:600])
                    job["output"] = job["output"][-180:]
        code = proc.wait()
        report = latest_report()
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job.update({"state": "completed" if code == 0 else "failed", "exit_code": code,
                            "report": report, "finished_at": datetime.now(timezone.utc).isoformat()})
    except Exception as error:
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job.update({"state": "failed", "exit_code": 1, "error": redact(str(error)[:500])})
    finally:
        with JOB_LOCK:
            ACTIVE_JOB = None


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "P0Console/1.0"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # Avoid writing request data, user prompts, or credentials to console logs.
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def host_allowed(self) -> bool:
        host = self.headers.get("Host", "").lower()
        return host in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

    def send_json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self.host_allowed():
            self.send_error(421)
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            try:
                self.send_json(status_payload())
            except Exception as error:
                self.send_json({"error": "状态检查失败：" + redact(str(error))[:400]}, 500)
            return
        if path == "/api/report/latest":
            self.send_json({"report": latest_report()})
            return
        if path == "/api/usage":
            values = parse_qs(urlsplit(self.path).query)
            window = values.get("window", ["1h"])[0]
            if window not in USAGE_WINDOWS:
                self.send_json({"ok": False, "error": "统计区间只支持 15m、1h、24h、7d。"}, 400)
                return
            try:
                self.send_json(usage_summary(window))
            except Exception as error:
                self.send_json({"ok": False, "error": "用量汇总失败：" + redact(str(error))[:400]}, 500)
            return
        if path == "/api/logs":
            values = parse_qs(urlsplit(self.path).query)
            try:
                limit = int(values.get("limit", ["30"])[0])
            except ValueError:
                limit = 30
            self.send_json(recent_spendlogs(limit))
            return
        if path == "/api/report/latest.md":
            _run_id, markdown = read_latest_markdown()
            if markdown is None:
                self.send_error(404, "No P0 acceptance report is available")
                return
            data = markdown.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/docs/p0-decisions":
            target = ROOT / "doc" / "P0上线前决策清单-2026-09-22.md"
            try:
                data = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOB_LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                self.send_json({"error": "验收任务不存在或已过期。"}, 404)
            else:
                self.send_json(job)
            return
        files = {"/": "index.html", "/index.html": "index.html",
                 "/app.js": "app.js", "/styles.css": "styles.css",
                 "/console-extra.css": "console-extra.css", "/logs.css": "logs.css"}
        filename = files.get(path)
        if not filename:
            self.send_error(404)
            return
        target = STATIC / filename
        content_type = "text/html; charset=utf-8" if filename.endswith(".html") else (
            "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8")
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        global ACTIVE_JOB
        if not self.host_allowed() or not self.origin_allowed():
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY:
            self.send_json({"error": "请求体大小无效。"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "请求必须是有效 JSON。"}, 400)
            return
        if not isinstance(body, dict):
            self.send_json({"error": "请求内容格式无效。"}, 400)
            return
        path = urlsplit(self.path).path
        if path == "/api/auth-check":
            with JOB_LOCK:
                if ACTIVE_JOB:
                    self.send_json({"error": "完整验收运行期间，请等待完成后再执行认证检查。"}, 409)
                    return
            self.send_json(auth_check())
            return
        if path == "/api/probe":
            kind = body.get("kind")
            if kind not in {"models", "chat", "stream", "responses", "tool"}:
                self.send_json({"error": "不支持的验证项目。"}, 400)
                return
            requested_model = body.get("model", MODEL)
            if not isinstance(requested_model, str) or not requested_model or len(requested_model) > 128:
                self.send_json({"error": "模型别名格式无效。"}, 400)
                return
            if kind != "models" and requested_model not in configured_model_aliases():
                self.send_json({"error": "所选模型不在本地 LiteLLM 配置别名中。"}, 400)
                return
            confirmed = body.get("confirm_other_model") is True
            if kind != "models" and requested_model != MODEL and not confirmed:
                self.send_json({"error": "非 P0 基准模型可能走其他 Provider 或产生用量；请先勾选确认。"}, 409)
                return
            with JOB_LOCK:
                if ACTIVE_JOB:
                    self.send_json({"error": "完整验收运行期间，暂不能并发执行单项验证。"}, 409)
                    return
            try:
                self.send_json(probe(kind, requested_model, confirmed))
            except Exception as error:
                self.send_json({"ok": False, "error": "单项验证失败：" + redact(str(error))[:400]}, 502)
            return
        if path == "/api/acceptance":
            with JOB_LOCK:
                if ACTIVE_JOB:
                    self.send_json({"error": "已有完整验收正在运行。"}, 409)
                    return
                job_id = uuid.uuid4().hex[:12]
                JOBS[job_id] = {"job_id": job_id, "state": "running", "started_at": datetime.now(timezone.utc).isoformat(), "output": []}
                ACTIVE_JOB = job_id
                for old_id in list(JOBS)[:-8]:
                    JOBS.pop(old_id, None)
            threading.Thread(target=run_acceptance, args=(job_id,), daemon=True).start()
            self.send_json({"job_id": job_id, "state": "running"}, 202)
            return
        self.send_json({"error": "未知操作。"}, 404)

    def do_OPTIONS(self) -> None:
        self.send_error(405)


def main() -> int:
    global PORT
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), ConsoleHandler)
    except OSError as error:
        print(f"无法启动 P0 控制台：{error}", file=sys.stderr)
        return 1
    server.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    print("P0 本地验证台已启动")
    print(f"打开：{url}")
    print("服务仅监听 127.0.0.1；按 Ctrl+C 停止。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nP0 本地验证台已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
