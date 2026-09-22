"""Generate a factual, local-only P0 deployment report from current evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.p0-test"
COMPOSE = ROOT / "deploy" / "docker-compose.p0-test.yml"
REPORTS = ROOT / "runtime" / "p0-test" / "reports"
FILES = (
    "deploy/docker-compose.p0-test.yml",
    "config/higress/p0-test/resources.json",
    "config/litellm.yaml",
)


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_acceptance() -> tuple[Path | None, dict | None]:
    if not REPORTS.is_dir():
        return None, None
    candidates = [path for path in REPORTS.glob("p0-*.json") if not path.name.endswith("-functional.json")]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            return path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None, None


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / f"p0-deploy-{stamp}.md"
    git_commit = command("git", "rev-parse", "--short", "HEAD").stdout.strip() or "unknown"
    git_branch = command("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "unknown"
    compose = command("docker", "compose", "--env-file", str(ENV), "-f", str(COMPOSE), "images", "--format", "json") if ENV.is_file() else None
    image_rows = []
    if compose and compose.returncode == 0:
        try:
            for image in json.loads(compose.stdout or "[]"):
                image_rows.append(f"| {image.get('Service', 'unknown')} | {image.get('Repository', '')}:{image.get('Tag', '')} | `{image.get('ID', '')}` |")
        except json.JSONDecodeError:
            pass
    ps = command("docker", "compose", "--env-file", str(ENV), "-f", str(COMPOSE), "ps", "--format", "json") if ENV.is_file() else None
    service_rows = []
    if ps and ps.returncode == 0:
        for line in ps.stdout.splitlines():
            try:
                item = json.loads(line)
                service_rows.append(f"| {item.get('Service', '')} | {item.get('State', '')} | {item.get('Health') or 'not configured'} | {item.get('Ports', '')} |")
            except json.JSONDecodeError:
                pass

    plugin_names = []
    resources_path = ROOT / FILES[1]
    if resources_path.is_file():
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
        for resource in resources:
            if resource.get("kind") == "WasmPlugin":
                name = resource.get("metadata", {}).get("name", "unknown")
                disabled = bool(resource.get("spec", {}).get("defaultConfigDisable", False)) or all(rule.get("configDisable", False) for rule in resource.get("spec", {}).get("matchRules", []))
                plugin_names.append(f"- `{name}` — " + ("disabled by P0 policy" if disabled else "declared; runtime test required"))

    model_names = []
    model_path = ROOT / "config/litellm.yaml"
    if model_path.is_file():
        model_names = re.findall(r"^\s*-\s*model_name:\s*(.+?)\s*$", model_path.read_text(encoding="utf-8"), re.M)
    model_rows = ["- `" + name + "`" for name in model_names] or ["- 未读取到模型别名"]
    acceptance_path, acceptance = latest_acceptance()
    if acceptance:
        status = acceptance.get("conclusion", "UNKNOWN")
        acceptance_ref = acceptance_path.relative_to(ROOT).as_posix()
        test_rows = [f"| {item.get('category')} | {item.get('status')} | {item.get('message')} | {item.get('evidence', '')} |" for item in acceptance.get("results", [])]
    else:
        status = "NOT RUN — no P0 acceptance report found"
        acceptance_ref = "none"
        test_rows = ["| all | NOT RUN | Run scripts/Run-P0EndToEnd.ps1 | |"]

    file_rows = [f"| `{rel}` | `{digest(ROOT / rel)}` |" for rel in FILES if (ROOT / rel).is_file()]
    report = [
        "# 本地 P0 部署与配置报告", "", f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat()}",
        f"- Git 分支 / Commit：`{git_branch}` / `{git_commit}`", f"- P0 验收结论：{status}",
        f"- 验收证据：`{acceptance_ref}`", "- 范围：仅本机隔离 Docker 测试环境；不是生产验收", "",
        "## 容器", "", "| 服务 | 状态 | 健康 | 发布端口 |", "|---|---|---|---|",
        *(service_rows or ["| — | 未运行/无法读取 | — | — |"]), "",
        "## 镜像", "", "| 服务 | 镜像 | Image ID |", "|---|---|---|", *(image_rows or ["| — | 未运行/无法读取 | — |"]), "",
        "## 配置摘要", "", "| 文件 | SHA-256 |", "|---|---|", *file_rows, "",
        "### P0 Higress 插件声明", "", *(plugin_names or ["- 未读取到插件声明"]), "",
        "### LiteLLM 模型别名", "", *model_rows, "",
        "## 本轮验收结果", "", "| 类别 | 状态 | 断言 | 证据 |", "|---|---|---|---|", *test_rows, "",
        "## 明确延期与数据边界", "",
        "- Qwen 单价未知：LiteLLM Spend 仅记作观测值，不作为已验证费用估算或财务结算。",
        "- IP Restriction 等待运维确认具体 CIDR，未批准前保持未应用。",
        "- Prompt/Response 仅保存在本机 P0 PostgreSQL，按 7 天策略清理；`runtime/p0-test` 为 Git 忽略目录，不得提交。",
        "- 每日/总预算未纳入 P0；RPM/TPM 仅为请求/Token 速率限制。", "",
    ]
    out.write_text("\n".join(report), encoding="utf-8")
    print(f"Deployment report written to {out.relative_to(ROOT)}")
    if not acceptance:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
