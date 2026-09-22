"""Run the P0 acceptance matrix against the isolated local Docker stack."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from test_p0_gateway import load_env, redact


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.p0-test"
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.p0-test.yml"
REPORT_ROOT = ROOT / "runtime" / "p0-test" / "reports"
BASE_URL = "http://ai-gateway-test.local:18080"
PROMETHEUS_URL = "http://127.0.0.1:19090"
EXPECTED_SERVICES = {"db", "redis", "litellm", "higress", "prometheus", "log-pruner"}


class Acceptance:
    def __init__(self) -> None:
        self.results: list[dict[str, str]] = []

    def add(self, status: str, category: str, message: str, evidence: str = "") -> None:
        self.results.append({"status": status, "category": category, "message": message, "evidence": evidence})
        print(f"{status} [{category}] {message}" + (f" — {evidence}" if evidence else ""))

    @property
    def failed(self) -> bool:
        return any(item["status"] == "FAIL" for item in self.results)

    @property
    def skipped(self) -> bool:
        return any(item["status"] in ("SKIP", "UNKNOWN") for item in self.results)


def compose(*args: str, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args],
        cwd=ROOT, input=input_text, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout,
    )


def safe_text(value: str, secrets: tuple[str, ...]) -> str:
    return redact(value, secrets).replace("\r", "").strip()[:600]


def http_json(url: str, timeout: int = 5) -> dict:
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def prometheus_query(query: str) -> list:
    encoded = urllib.parse.urlencode({"query": query})
    body = http_json(PROMETHEUS_URL + "/api/v1/query?" + encoded)
    return body.get("data", {}).get("result", [])


def hex_literal(value: str) -> str:
    return value.encode("utf-8").hex()


def run_psql(sql: str, timeout: int = 30) -> tuple[int, str, str]:
    result = compose("exec", "-T", "db", "psql", "-U", "llmproxy", "-d", "litellm", "-A", "-t", "-F", "|", input_text=sql, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def verify_spendlogs(report: dict, variables: dict[str, str], provider_key: str, results: Acceptance) -> dict:
    run_start = float(report.get("started_at_epoch", time.time())) - 10
    requests = report.get("requests", [])
    good = [item for item in requests if item.get("http_status") == "200" and item.get("endpoint") in {"chat", "stream", "responses", "responses-tool-call"}]
    wrong_host = next((item for item in requests if item.get("endpoint") == "wrong-host"), None)
    if not good:
        results.add("FAIL", "SpendLogs", "No successful model API calls are available for current-run correlation")
        return {"logged_requests": 0, "total_tokens": 0, "observed_spend": "unknown"}

    values = ",\n".join("('" + item["marker"] + "')" for item in good)
    sql = f"""
WITH expected(marker) AS (VALUES {values}),
matched AS (
  SELECT e.marker, s.request_id, s.proxy_server_request, s.response, s.total_tokens,
         s.spend, s.request_duration_ms
  FROM expected e
  LEFT JOIN "LiteLLM_SpendLogs" s
    ON s.model_group = 'my-qwen3.6-27b'
   AND s."endTime" >= to_timestamp({run_start:.3f})
   AND (COALESCE(s.proxy_server_request::text, '') LIKE '%' || e.marker || '%'
        OR COALESCE(s.messages::text, '') LIKE '%' || e.marker || '%')
)
SELECT marker, COUNT(request_id),
       COUNT(*) FILTER (WHERE request_id IS NOT NULL AND proxy_server_request IS NOT NULL),
       COUNT(*) FILTER (WHERE request_id IS NOT NULL AND response IS NOT NULL),
       COALESCE(SUM(total_tokens), 0), COALESCE(SUM(spend), 0),
       COALESCE(AVG(request_duration_ms), 0)::integer,
       COALESCE(string_agg(DISTINCT request_id, ','), '')
FROM matched GROUP BY marker ORDER BY marker;
"""
    rc, output, error = run_psql(sql)
    if rc != 0:
        results.add("FAIL", "SpendLogs", "Unable to query the local P0 LiteLLM database", safe_text(error, ()))
        return {"logged_requests": 0, "total_tokens": 0, "observed_spend": "unknown"}

    rows: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split("|", 7)
        if len(parts) == 8:
            rows[parts[0]] = dict(zip(("count", "request_bodies", "response_bodies", "tokens", "spend", "duration_ms", "request_ids"), parts[1:]))
    missing = [item["endpoint"] for item in good if int(rows.get(item["marker"], {}).get("count", "0") or 0) < 1]
    log_count = sum(int(row.get("count", "0") or 0) for row in rows.values())
    request_bodies = sum(int(row.get("request_bodies", "0") or 0) for row in rows.values())
    response_bodies = sum(int(row.get("response_bodies", "0") or 0) for row in rows.values())
    total_tokens = sum(int(row.get("tokens", "0") or 0) for row in rows.values())
    observed_spend = sum(float(row.get("spend", "0") or 0) for row in rows.values())
    durations = [int(row.get("duration_ms", "0") or 0) for row in rows.values()]
    request_ids = {item["endpoint"]: item.get("response_id", "") for item in good}

    secrets = [variables.get("P0_HIGRESS_CONSUMER_KEY", ""), variables.get("P0_LITELLM_MASTER_KEY", ""), provider_key]
    encoded_secrets = [hex_literal(secret) for secret in secrets if secret]
    secret_expr = " OR ".join(
        "(COALESCE(proxy_server_request::text, '') || COALESCE(response::text, '') || COALESCE(messages::text, '')) LIKE '%' || convert_from(decode('" + secret_hex + "','hex'),'UTF8') || '%'"
        for secret_hex in encoded_secrets
    ) or "FALSE"
    leak_sql = f"""
SELECT COUNT(*) FILTER (WHERE {secret_expr})
FROM "LiteLLM_SpendLogs"
WHERE model_group = 'my-qwen3.6-27b' AND "endTime" >= to_timestamp({run_start:.3f});
"""
    leak_rc, leak_output, leak_error = run_psql(leak_sql)
    if leak_rc != 0:
        results.add("FAIL", "脱敏", "Unable to verify local SpendLogs for credential leakage", safe_text(leak_error, tuple(secrets)))
        leak_count = -1
    else:
        try:
            leak_count = int(leak_output.strip() or "0")
        except ValueError:
            leak_count = -1

    wrong_host_logged = False
    if wrong_host:
        marker = wrong_host["marker"]
        wrong_sql = f"""
SELECT COUNT(*) FROM "LiteLLM_SpendLogs"
WHERE model_group = 'my-qwen3.6-27b' AND "endTime" >= to_timestamp({run_start:.3f})
  AND (COALESCE(proxy_server_request::text, '') LIKE '%{marker}%'
       OR COALESCE(messages::text, '') LIKE '%{marker}%');
"""
        wrong_rc, wrong_output, _ = run_psql(wrong_sql)
        wrong_host_logged = wrong_rc != 0 or int(wrong_output.strip() or "0") > 0

    complete = not missing and log_count >= len(good) and request_bodies > 0 and response_bodies > 0 and total_tokens > 0
    results.add(
        "PASS" if complete else "FAIL", "SpendLogs",
        "Current-run model calls correlate to LiteLLM SpendLogs with prompt/response bodies, usage and duration",
        f"rows={log_count}; request_bodies={request_bodies}; response_bodies={response_bodies}; tokens={total_tokens}; missing={','.join(missing) or 'none'}",
    )
    results.add("PASS" if leak_count == 0 else "FAIL", "脱敏", "Consumer/Master/Provider credentials are absent from current-run request/response bodies", f"secret_leak_rows={leak_count}")
    results.add("PASS" if not wrong_host_logged else "FAIL", "Host 路由", "Unknown Host request did not create an upstream SpendLog", "no matching log row" if not wrong_host_logged else "marker found or database query failed")
    results.add("UNKNOWN", "费用", "Qwen unit price is unknown; LiteLLM Spend is recorded but is not a validated cost estimate", f"stored_spend={observed_spend:.8f}; ignored for cost acceptance")
    return {"logged_requests": log_count, "request_bodies": request_bodies, "response_bodies": response_bodies,
            "total_tokens": total_tokens, "observed_spend": f"{observed_spend:.8f}",
            "secret_leak_rows": leak_count, "missing_markers": missing,
            "response_ids": request_ids, "duration_ms": durations}


def verify_retention(variables: dict[str, str], results: Acceptance) -> None:
    retention_days = variables.get("P0_LOG_RETENTION_DAYS", "7")
    if retention_days != "7":
        results.add("FAIL", "日志保留", "Local P0 body-log retention is not configured to the agreed seven days", f"configured_days={retention_days}")
        return
    synthetic_id = "p0-retention-test-" + str(int(time.time()))
    sql = f"""
INSERT INTO "LiteLLM_SpendLogs"
 (request_id, call_type, api_key, spend, total_tokens, prompt_tokens, completion_tokens,
  "startTime", "endTime", model, status, created_at, updated_at)
VALUES
 ('{synthetic_id}-old', 'chat', 'local-retention-test', 0, 0, 0, 0, NOW() - INTERVAL '8 days', NOW() - INTERVAL '8 days', 'p0-retention-test', 'success', NOW(), NOW()),
 ('{synthetic_id}-new', 'chat', 'local-retention-test', 0, 0, 0, 0, NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day', 'p0-retention-test', 'success', NOW(), NOW());
DELETE FROM "LiteLLM_SpendLogs"
 WHERE request_id LIKE '{synthetic_id}-%' AND "endTime" < NOW() - INTERVAL '7 days';
SELECT COUNT(*) FILTER (WHERE request_id = '{synthetic_id}-old'),
       COUNT(*) FILTER (WHERE request_id = '{synthetic_id}-new')
FROM "LiteLLM_SpendLogs" WHERE request_id LIKE '{synthetic_id}-%';
DELETE FROM "LiteLLM_SpendLogs" WHERE request_id LIKE '{synthetic_id}-%';
"""
    rc, output, error = run_psql(sql)
    if rc != 0:
        results.add("FAIL", "日志保留", "Could not run the isolated synthetic 7-day retention check", safe_text(error, ()))
        return
    rows = [line.split("|") for line in output.splitlines() if "|" in line]
    counts = rows[-1] if rows else []
    ok = counts == ["0", "1"]
    results.add("PASS" if ok else "FAIL", "日志保留", "Synthetic over-age row is deleted while the under-age row survives; test rows are then removed", f"older={counts[0] if counts else '?'}; recent={counts[1] if len(counts) > 1 else '?'}; configured_days=7")


def write_report(report_path: Path, data: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = report_path.with_suffix(".md")
    rows = [
        "# 本地 P0 自动验收报告", "", f"- 时间：{data['generated_at']}", f"- Run ID：`{data['run_id']}`",
        f"- Git Commit：`{data['git_commit']}`", "- 环境：独立本机 Docker P0；不代表生产验收", "",
        "| 类别 | 状态 | 结果 | 证据 |", "|---|---|---|---|",
    ]
    for item in data["results"]:
        evidence = item.get("evidence", "").replace("|", "&#124;").replace("\n", " ")
        rows.append(f"| {item['category']} | {item['status']} | {item['message']} | {evidence} |")
    rows.extend(["", "## 调用关联", "", "| 接口 | 响应 request_id |", "|---|---|"])
    for endpoint, request_id in data.get("spendlogs", {}).get("response_ids", {}).items():
        rows.append(f"| {endpoint} | `{request_id or '未返回'}` |")
    rows.extend([
        "", "## 费用口径", "", "Qwen 单价未提供；报告中的 LiteLLM Spend 仅为存储值，不视作价格已配置或财务结算。",
        "", "## 未纳入本地验收的决策项", "", "- IP Restriction：等待运维批准的具体 CIDR；当前必须保持未应用。",
        "- 每日/总预算：P0 暂不启用；RPM/TPM 不等价于业务预算。",
        "- 正文日志：仅写本机 P0 PostgreSQL 卷，目标保留 7 天；禁止提交 `runtime/p0-test`。",
        "", "## 总结", "", data["conclusion"], "",
    ])
    md_path.write_text("\n".join(rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-rate-limit", action="store_true", help="Verify Redis-backed P0 RPM behavior")
    parser.add_argument("--explicit-host", action="store_true", help="Set the expected HTTP Host header explicitly")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not ENV_FILE.is_file():
        print("Missing .env.p0-test; initialize the isolated P0 environment first.", file=sys.stderr)
        return 2

    variables = load_env(ENV_FILE)
    provider_key = ""
    root_env = ROOT / ".env"
    if root_env.is_file():
        for line in root_env.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("ZHILIN_aigc_API_KEY="):
                provider_key = line.partition("=")[2]
                break
    secrets = tuple(value for value in (variables.get("P0_HIGRESS_CONSUMER_KEY", ""), variables.get("P0_LITELLM_MASTER_KEY", ""), provider_key) if value)
    results = Acceptance()
    run_id = "p0-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex()
    now = datetime.now(timezone.utc).isoformat()
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    functional_path = REPORT_ROOT / f"{run_id}-functional.json"
    report_path = REPORT_ROOT / f"{run_id}.json"

    compose_config = compose("config", "-q")
    results.add("PASS" if compose_config.returncode == 0 else "FAIL", "环境隔离", "P0 Compose configuration validates", safe_text(compose_config.stderr, secrets))
    if compose_config.returncode != 0:
        conclusion = "验收失败：P0 Compose 配置无效；未执行模型测试。"
        write_report(report_path, {"run_id": run_id, "generated_at": now, "git_commit": "unknown", "results": results.results, "conclusion": conclusion})
        return 1

    try:
        address = socket.gethostbyname("ai-gateway-test.local")
        results.add("PASS" if address == "127.0.0.1" else "FAIL", "入口", "P0 test hostname resolves only to local loopback", address)
    except OSError as error:
        results.add("FAIL", "入口", "P0 test hostname does not resolve", safe_text(str(error), secrets))

    ps = compose("ps", "--format", "json")
    services: list[dict] = []
    if ps.returncode == 0:
        for line in ps.stdout.splitlines():
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    service_names = {item.get("Service") for item in services}
    healthy = all(item.get("State") == "running" and (item.get("Health") in ("healthy", "") or item.get("Service") == "prometheus") for item in services)
    missing = sorted(EXPECTED_SERVICES - service_names)
    results.add("PASS" if not missing and healthy else "FAIL", "容器", "All isolated P0 services are running and health-checked", f"services={','.join(sorted(str(x) for x in service_names))}; missing={','.join(missing) or 'none'}")
    exposed = []
    for item in services:
        for publisher in item.get("Publishers", []) or []:
            if publisher.get("PublishedPort") and publisher.get("URL") != "127.0.0.1":
                exposed.append(f"{item.get('Service')}:{publisher.get('PublishedPort')}@{publisher.get('URL')}")
    db_redis_published = any(item.get("Service") in ("db", "redis") and any(p.get("PublishedPort") for p in item.get("Publishers", []) or []) for item in services)
    results.add("PASS" if not exposed and not db_redis_published else "FAIL", "暴露面", "Published P0 ports are loopback-only; PostgreSQL and Redis have no host publishing", ",".join(exposed) or "loopback bindings only")

    tpm_result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "test_p0_tpm.py")], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=180)
    for line in (tpm_result.stdout + "\n" + tpm_result.stderr).splitlines():
        if line.startswith(("PASS [TPM]", "FAIL [TPM]")):
            status = "PASS" if line.startswith("PASS") else "FAIL"
            results.add(status, "TPM", line.partition("] ")[2][:300])
    if tpm_result.returncode != 0 and not any(row["category"] == "TPM" and row["status"] == "FAIL" for row in results.results):
        results.add("FAIL", "TPM", "TPM test helper failed", safe_text(tpm_result.stderr or tpm_result.stdout, secrets))

    api_command = [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "test_p0_gateway.py"), "--env-file", str(ENV_FILE), "--base-url", BASE_URL, "--timeout", str(args.timeout), "--json-report", str(functional_path)]
    if args.verify_rate_limit:
        api_command.append("--verify-rate-limit")
    if args.explicit_host:
        api_command += ["--host", "ai-gateway-test.local"]
    api_result = subprocess.run(api_command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=args.timeout * 7)
    api_output = safe_text(api_result.stdout + "\n" + api_result.stderr, secrets)
    for line in (api_result.stdout + "\n" + api_result.stderr).splitlines():
        if line.startswith(("PASS ", "FAIL ", "SKIP ")):
            status, _, message = line.partition(" ")
            results.add(status, "API", message[:300])
    functional = {}
    if functional_path.exists():
        try:
            functional = json.loads(functional_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            functional = {}
    if api_result.returncode != 0 and not any(row["status"] == "FAIL" and row["category"] == "API" for row in results.results):
        results.add("FAIL", "API", "Functional API test process failed", api_output)

    spendlogs = verify_spendlogs(functional, variables, provider_key, results) if functional else {"logged_requests": 0, "total_tokens": 0, "observed_spend": "unknown"}
    if not functional:
        results.add("FAIL", "SpendLogs", "Functional test did not produce a run manifest; cannot correlate logs")
    verify_retention(variables, results)

    try:
        deadline = time.monotonic() + 45
        scrape = []
        while time.monotonic() < deadline:
            scrape = prometheus_query('up{job="higress"}')
            if scrape and float(scrape[0].get("value", [0, "0"])[1]) == 1.0:
                break
            time.sleep(3)
        scrape_ok = bool(scrape) and float(scrape[0].get("value", [0, "0"])[1]) == 1.0
        results.add("PASS" if scrape_ok else "FAIL", "Envoy 指标", "P0 Prometheus scrapes the native Higress/Envoy endpoint", "up=1" if scrape_ok else "target is not up")
        names = http_json(PROMETHEUS_URL + "/api/v1/label/__name__/values").get("data", [])
        envoy_names = [name for name in names if name.startswith("envoy_")]
        request_metric = "envoy_http_downstream_rq_total"
        gateway_labels = '{job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080"}'
        request_value = []
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            request_value = prometheus_query("sum(" + request_metric + gateway_labels + ")") if request_metric in envoy_names else []
            if request_value and float(request_value[0].get("value", [0, "0"])[1]) > 0:
                break
            time.sleep(3)
        nonzero = bool(request_value) and float(request_value[0].get("value", [0, "0"])[1]) > 0
        results.add("PASS" if nonzero else "FAIL", "Envoy 指标", "Gateway listener request counter has a non-zero sample after traffic", request_metric + gateway_labels if request_metric in envoy_names else "metric is absent")
        status_series = prometheus_query('sum(envoy_http_downstream_rq{job="higress",http_conn_manager_prefix="outbound_0.0.0.0_8080",response_code_class="2xx"})')
        status_nonzero = bool(status_series) and float(status_series[0].get("value", [0, "0"])[1]) > 0
        results.add("PASS" if status_nonzero else "FAIL", "Envoy 指标", "Gateway listener response-code-class metric has a non-zero 2xx sample", "gateway response_code_class=2xx" if status_nonzero else "gateway 2xx series is absent/zero")
        latency_metric = "envoy_http_downstream_rq_time_count"
        latency_series = prometheus_query("sum(" + latency_metric + gateway_labels + ")") if latency_metric in envoy_names else []
        latency_nonzero = bool(latency_series) and float(latency_series[0].get("value", [0, "0"])[1]) > 0
        results.add("PASS" if latency_nonzero else "FAIL", "Envoy 指标", "Gateway listener request-latency histogram has observations", latency_metric + gateway_labels if latency_nonzero else "gateway request duration histogram is absent/zero")
    except (URLError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        results.add("FAIL", "Envoy 指标", "Prometheus query failed", safe_text(str(error), secrets))

    ip_check = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "check_p0_ip_state.py")], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30)
    ip_output = (ip_check.stdout + "\n" + ip_check.stderr).strip()
    if ip_output.startswith("SKIP:"):
        results.add("SKIP", "IP 策略", ip_output[5:].strip())
    elif ip_check.returncode != 0:
        results.add("FAIL", "IP 策略", safe_text(ip_output, secrets))
    else:
        results.add("PASS", "IP 策略", ip_output[:300])

    try:
        dns_result = compose("exec", "-T", "litellm", "python", "-c", "import json,socket; print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo('test2-aigc.campusapp.com.cn',443,type=socket.SOCK_STREAM)})))", timeout=20)
        addresses = json.loads(dns_result.stdout.strip().splitlines()[-1]) if dns_result.returncode == 0 else []
        private = [address for address in addresses if ipaddress.ip_address(address).is_private]
        results.add("PASS" if private else "UNKNOWN", "Provider 路由", "LiteLLM container DNS resolution for the configured internal Qwen Provider", ",".join(addresses) if addresses else safe_text(dns_result.stderr, secrets))
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as error:
        results.add("UNKNOWN", "Provider 路由", "Could not establish an internal DNS/routing classification from the LiteLLM container", safe_text(str(error), secrets))

    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if results.failed:
        conclusion = "验收失败：至少一项必测断言失败；不得视为 P0 通过。"
        exit_code = 1
    elif results.skipped:
        conclusion = "本轮自动化必测通过，但存在明确延期/未知项；不能标记为 P0 全部完成或生产验收。"
        exit_code = 0
    else:
        conclusion = "本轮自动化验收通过；仍仅代表本机 P0 Docker 测试环境。"
        exit_code = 0
    data = {"run_id": run_id, "generated_at": now, "git_commit": git.stdout.strip() or "unknown",
            "environment": "local-isolated-docker", "results": results.results,
            "spendlogs": spendlogs, "conclusion": conclusion}
    write_report(report_path, data)
    print(f"Report: {report_path.relative_to(ROOT)}")
    print(f"Conclusion: {conclusion}")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print("P0 acceptance timed out.", file=sys.stderr)
        raise SystemExit(1)
