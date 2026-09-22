const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const BASELINE_MODEL = "my-qwen3.6-27b";
let toastTimer;
let modelCatalog = [];

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 2500);
}

function setProgress(percent) {
  const node = $("#jobProgress");
  const step = Math.max(10, Math.min(100, Math.ceil(percent / 10) * 10));
  node.className = `progress-${step}`;
}

function setDot(selector, state) {
  const node = $(selector);
  node.classList.toggle("bad", state === "FAIL");
  node.classList.toggle("muted", state === "UNKNOWN" || state === "SKIP");
}

function renderServices(services) {
  const list = $("#serviceList");
  if (!services.length) {
    list.innerHTML = '<div class="empty-note">未找到 P0 容器。请先启动本地 P0 Compose 环境。</div>';
    $("#serviceCount").textContent = "未启动";
    $("#summaryServices").textContent = "未启动";
    setDot("#serviceDot", "FAIL");
    return false;
  }
  const expected = ["db", "redis", "litellm", "higress", "prometheus", "log-pruner"];
  const byName = Object.fromEntries(services.map((item) => [item.name, item]));
  const healthy = expected.every((name) => byName[name] && byName[name].state === "running" && ["healthy", "n/a", ""].includes(byName[name].health)) && services.every((item) => item.safe_ports);
  list.innerHTML = expected.map((name) => {
    const item = byName[name];
    const ok = item && item.state === "running" && ["healthy", "n/a", ""].includes(item.health) && item.safe_ports;
    const state = !item ? "MISSING" : item.health === "n/a" ? "RUNNING" : (item.health || item.state).toUpperCase();
    const port = item?.ports?.join(", ") || "内部网络";
    return `<div class="service-row"><span class="service-name"><i class="service-led ${ok ? "" : "bad"}"></i>${esc(name)}</span><span class="service-state ${ok ? "" : "bad"}">${esc(state)}</span><span class="service-port" title="${esc(port)}">${esc(port)}</span></div>`;
  }).join("");
  $("#serviceCount").textContent = `${services.length} SERVICES`;
  $("#summaryServices").textContent = `${services.filter((item) => item.state === "running").length} / ${expected.length} 运行`;
  setDot("#serviceDot", healthy ? "PASS" : "FAIL");
  return healthy;
}

function renderReport(report) {
  if (!report) {
    $("#summaryLogs").textContent = "尚未运行";
    $("#reportRun").textContent = "暂无验收报告";
    $("#reportLink").hidden = true;
    setDot("#logsDot", "UNKNOWN");
    return;
  }
  const failed = report.counts?.FAIL || 0;
  const passed = report.counts?.PASS || 0;
  $("#summaryLogs").textContent = failed ? `${failed} 项失败` : `${passed} 项通过`;
  const date = report.generated_at ? new Date(report.generated_at).toLocaleString("zh-CN", { hour12: false }) : "时间未知";
  $("#reportRun").textContent = `${report.run_id} · ${date}`;
  $("#reportLink").hidden = false;
  setDot("#logsDot", failed ? "FAIL" : "PASS");
}

function formatCount(value) {
  return value == null ? "—" : Number(value).toLocaleString("zh-CN");
}

function renderUsage(data) {
  const usage = data.usage || {};
  const gateway = usage.gateway || {};
  $("#usageRequests").textContent = formatCount(usage.requests);
  $("#usageSuccess").textContent = formatCount(usage.successful_requests);
  $("#usageFailures").textContent = formatCount(usage.failed_requests);
  $("#usageTokens").textContent = formatCount(usage.total_tokens);
  $("#usagePromptTokens").textContent = formatCount(usage.prompt_tokens);
  $("#usageCompletionTokens").textContent = formatCount(usage.completion_tokens);
  $("#metricGatewayRq").textContent = formatCount(gateway.requests);
  $("#metricGateway2xx").textContent = formatCount(gateway["2xx"]);
  $("#metricGatewayErrors").textContent = `${formatCount(gateway["4xx"])} / ${formatCount(gateway["5xx"])}`;
  $("#metricGatewayLatency").textContent = gateway.average_latency_ms == null ? "—" : `${formatCount(gateway.average_latency_ms)} ms`;
  const models = usage.models || [];
  $("#usageModels").innerHTML = models.length ? `<table><thead><tr><th>模型</th><th>请求</th><th>Tokens</th></tr></thead><tbody>${models.map((item) => `<tr><td title="${esc(item.model)}">${esc(item.model)}</td><td>${formatCount(item.requests)}</td><td>${formatCount(item.total_tokens)}</td></tr>`).join("")}</tbody></table>` : '<span class="empty-note">此时间范围内暂无 LiteLLM 模型调用记录</span>';
  const updated = data.refreshed_at ? new Date(data.refreshed_at).toLocaleTimeString("zh-CN", { hour12: false }) : "时间未知";
  $("#usageUpdatedAt").textContent = `${data.window_label || "所选区间"} · 更新于 ${updated}`;
}

let usageRefreshInFlight = false;
async function refreshUsage() {
  if (usageRefreshInFlight) return;
  usageRefreshInFlight = true;
  const windowKey = $("#usageWindow").value;
  $("#usageUpdatedAt").textContent = "正在汇总 LiteLLM 与 Higress 数据…";
  try {
    const data = await api(`/api/usage?window=${encodeURIComponent(windowKey)}`);
    if (!data.ok) throw new Error(data.error || "用量读取失败");
    if (windowKey === $("#usageWindow").value) renderUsage(data);
  } catch (error) {
    $("#usageUpdatedAt").textContent = `统计读取失败：${error.message}`;
    $("#usageModels").innerHTML = `<span class="error">${esc(error.message)}</span>`;
  } finally {
    usageRefreshInFlight = false;
    if (windowKey !== $("#usageWindow").value) refreshUsage();
  }
}

function renderChecks(checks) {
  const byId = Object.fromEntries(checks.map((item) => [item.id, item]));
  const gateway = byId.gateway?.status || "UNKNOWN";
  const prometheus = byId.prometheus?.status || "UNKNOWN";
  $("#summaryGateway").textContent = gateway === "PASS" ? "可用" : "未就绪";
  $("#summaryMetrics").textContent = prometheus === "PASS" ? "采集中" : "未就绪";
  $("#flowHigress").textContent = gateway === "PASS" ? "✓" : "!";
  $("#flowLiteLLM").textContent = byId.litellm?.status === "PASS" ? "✓" : "!";
  setDot("#gatewayDot", gateway);
  setDot("#metricsDot", prometheus);
  const failed = checks.some((item) => item.status !== "PASS");
  $("#overallLabel").textContent = failed ? "环境需要检查" : "P0 环境就绪";
  $("#footerHealth").textContent = failed ? "存在未就绪项" : "所有运行检查通过";
}

function updateModelSelection(resetConfirmation = true) {
  const selected = $("#modelSelect").value;
  const isAlternate = selected !== BASELINE_MODEL;
  $("#modelBadge").textContent = isAlternate ? "需单项确认" : "P0 基准";
  $("#alternateModelGuard").hidden = !isAlternate;
  if (resetConfirmation) $("#alternateModelConfirm").checked = false;
}

function renderModelSelector(models) {
  const listed = Array.isArray(models) ? models.filter((item) => typeof item === "string" && item.trim()) : [];
  if (listed.length) modelCatalog = [...new Set(listed)];
  const select = $("#modelSelect");
  const previous = select.value;
  const options = [...new Set([BASELINE_MODEL, ...modelCatalog])];
  options.sort((a, b) => a === BASELINE_MODEL ? -1 : b === BASELINE_MODEL ? 1 : a.localeCompare(b));
  select.innerHTML = options.map((model) => {
    const label = model === BASELINE_MODEL ? `${model}（P0 基准）` : `${model}（需确认真实调用）`;
    return `<option value="${esc(model)}">${esc(label)}</option>`;
  }).join("");
  select.value = options.includes(previous) ? previous : BASELINE_MODEL;
  updateModelSelection(false);
}

async function refreshStatus(showToast = false) {
  const button = $("#refreshButton");
  button.disabled = true;
  $("#updatedAt").textContent = "正在读取容器和服务状态…";
  try {
    const data = await api("/api/status");
    const servicesOk = renderServices(data.services || []);
    renderChecks(data.checks || []);
    renderModelSelector(data.models || []);
    renderReport(data.latest_report);
    const snapshots = data.release_snapshots || {};
    const latestSnapshot = snapshots.latest?.release_id ? ` · 最新 ${snapshots.latest.release_id}` : "";
    $("#snapshotState").textContent = `本地恢复快照 ${snapshots.count || 0} 个${latestSnapshot}`;
    $("#keyState").textContent = data.key_configured ? (data.legacy_key_configured ? "双 Key 窗口" : "已配置") : "未配置";
    $("#keyState").className = `pill ${data.key_configured ? "pill-green" : "pill-amber"}`;
    $("#updatedAt").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    if (!servicesOk) $("#overallLabel").textContent = "检查容器状态";
    if (showToast) toast("环境状态已刷新");
  } catch (error) {
    $("#updatedAt").textContent = "读取失败：" + error.message;
    $("#overallLabel").textContent = "控制台服务异常";
    $("#footerHealth").textContent = "API 读取失败";
    if (showToast) toast("状态读取失败");
  } finally {
    button.disabled = false;
  }
}

let spendLogs = [];
function logStatusClass(status) {
  const value = String(status || "").toLowerCase();
  if (value === "success" || value === "successful" || value === "200") return "log-success";
  if (value === "failure" || value === "failed" || value === "error") return "log-failure";
  return "log-unknown";
}

function renderLogs() {
  const filter = $("#logsFilter").value;
  const rows = spendLogs.filter((item) => {
    const kind = logStatusClass(item.status);
    return filter === "all" || (filter === "success" && kind === "log-success") || (filter === "failure" && kind === "log-failure");
  });
  $("#logsCount").textContent = `${rows.length} 条记录显示 · 最近 7 天 · 最多读取 30 条`;
  if (!rows.length) {
    $("#logsList").innerHTML = '<div class="logs-empty">没有符合条件的调用记录。</div>';
    return;
  }
  $("#logsList").innerHTML = rows.map((item) => {
    const requestId = item.request_id || "unknown";
    const timestamp = item.ended_at || item.started_at;
    const when = timestamp ? new Date(timestamp).toLocaleString("zh-CN", { hour12: false }) : "时间未知";
    const stateClass = logStatusClass(item.status);
    const total = Number(item.total_tokens || 0).toLocaleString("zh-CN");
    const duration = item.duration_ms == null ? "—" : `${item.duration_ms} ms`;
    const request = item.request_body ?? item.messages;
    const response = item.response_body;
    const body = (title, value) => `<div class="log-body"><div class="log-body-title">${title}</div><pre>${value == null ? "（无正文记录）" : esc(JSON.stringify(value, null, 2))}</pre></div>`;
    return `<details class="log-entry"><summary><span class="log-status ${stateClass}">${esc(item.status || "UNKNOWN")}</span><span class="log-time">${esc(when)}</span><span class="log-model">${esc(item.model || "unknown")}</span><span class="log-type">${esc(item.call_type || "")}</span><span class="log-tokens">${total} tokens</span><span class="log-duration">${esc(duration)}</span><span class="log-expand">展开</span></summary><div class="log-details"><div class="log-id">Request ID <code>${esc(requestId)}</code></div><div class="log-token-breakdown">输入 ${Number(item.prompt_tokens || 0).toLocaleString("zh-CN")} · 输出 ${Number(item.completion_tokens || 0).toLocaleString("zh-CN")} · 总计 ${total} · Spend 金额未知</div><div class="log-bodies">${body("请求正文", request)}${body("响应正文", response)}</div></div></details>`;
  }).join("");
}

async function loadLogs(showToast = false) {
  const button = $("#refreshLogs");
  button.disabled = true;
  $("#logsCount").textContent = "正在读取本机 LiteLLM 数据库…";
  try {
    const data = await api("/api/logs?limit=30");
    if (!data.ok) throw new Error(data.error || "读取日志失败");
    spendLogs = data.items || [];
    renderLogs();
    if (showToast) toast("调用日志已刷新");
  } catch (error) {
    $("#logsCount").textContent = "日志读取失败";
    $("#logsList").innerHTML = `<div class="logs-error">${esc(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
}

function renderAuth(data) {
  const root = $("#authResults");
  root.innerHTML = (data.cases || []).map((item) => {
    const pass = item.status === "PASS";
    const mark = pass ? "✓" : item.status === "SKIP" ? "·" : "!";
    const cls = pass ? "" : "fail";
    return `<div class="check-row"><span class="mini-status ${cls}">${mark}</span><span>${esc(item.label)}</span><small>${esc(item.detail)}</small></div>`;
  }).join("");
}

async function runAuth() {
  const button = $("#authButton");
  button.disabled = true;
  button.textContent = "正在检查认证与路由…";
  try {
    const data = await api("/api/auth-check", { method: "POST", body: "{}" });
    renderAuth(data);
    toast(data.ok ? "认证与路由检查完成" : "认证检查存在失败项");
  } catch (error) {
    $("#authResults").innerHTML = `<span class="error">${esc(error.message)}</span>`;
  } finally {
    button.disabled = false;
    button.innerHTML = '运行认证检查 <span>→</span>';
  }
}

function renderProbe(kind, data) {
  const title = { models: "模型目录", chat: "Chat", stream: "流式 SSE", responses: "Responses", tool: "Responses 工具调用" }[kind] || kind;
  const resultClass = data.ok ? "ok" : "error";
  let summary = data.ok ? "验证通过" : "验证失败";
  if (data.status !== undefined) summary += ` · HTTP ${data.status || "不可达"}`;
  if (data.latency_ms !== undefined) summary += ` · ${data.latency_ms} ms`;
  let detail = "";
  if (kind === "models") detail = (data.models || []).map(esc).join(" · ");
  if (["chat", "stream", "responses"].includes(kind) && data.text) detail = data.text;
  if (kind === "tool" && data.ok) detail = "response.completed · gateway_probe function_call · usage 已返回";
  if (kind === "stream" && data.ok) detail += (detail ? "\n" : "") + `终止标记 [DONE] · usage ${JSON.stringify(data.usage || {})}`;
  if (["chat", "responses"].includes(kind) && data.ok) detail += (detail ? "\n" : "") + `usage ${JSON.stringify(data.usage || {})}`;
  if (!data.ok && data.error) detail = data.error;
  $("#probeOutput").innerHTML = `<span class="${resultClass}">${esc(title)}：${esc(summary)}</span>${detail ? `<pre>${esc(detail)}</pre>` : ""}`;
}

async function runProbe(kind, button) {
  const selectedModel = $("#modelSelect").value || BASELINE_MODEL;
  const confirmOtherModel = $("#alternateModelConfirm").checked;
  if (kind !== "models" && selectedModel !== BASELINE_MODEL && !confirmOtherModel) {
    toast("请先确认非基准模型的真实调用与用量风险");
    $("#alternateModelGuard").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const buttons = [...document.querySelectorAll("[data-probe]")];
  buttons.forEach((item) => { item.disabled = true; });
  const previous = button.innerHTML;
  button.classList.add("busy");
  button.querySelector(".probe-arrow").textContent = "…";
  $("#probeOutput").innerHTML = '<span class="empty-note">正在请求本地 P0 网关…</span>';
  try {
    const data = await api("/api/probe", { method: "POST", body: JSON.stringify({
      kind, model: selectedModel, confirm_other_model: confirmOtherModel,
    }) });
    if (kind === "models" && data.ok) renderModelSelector(data.models || []);
    renderProbe(kind, data);
    if (kind !== "models") {
      refreshStatus(false);
      refreshUsage();
      loadLogs(false);
    }
  } catch (error) {
    $("#probeOutput").innerHTML = `<span class="error">${esc(error.message)}</span>`;
  } finally {
    buttons.forEach((item) => { item.disabled = false; });
    button.classList.remove("busy");
    button.innerHTML = previous;
  }
}

function classifyLine(line) {
  const lower = line.toLowerCase();
  if (lower.startsWith("pass")) return "pass";
  if (lower.startsWith("fail")) return "fail";
  if (lower.startsWith("skip")) return "skip";
  if (lower.startsWith("unknown")) return "unknown";
  return "";
}

async function runAcceptance() {
  const button = $("#acceptanceButton");
  button.disabled = true;
  $("#refreshButton").disabled = true;
  $("#authButton").disabled = true;
  $("#modelSelect").disabled = true;
  $("#alternateModelConfirm").disabled = true;
  document.querySelectorAll("[data-probe]").forEach((item) => { item.disabled = true; });
  button.innerHTML = "正在启动…";
  $("#jobPanel").hidden = false;
  $("#jobTitle").textContent = "完整验收准备中";
  $("#jobState").textContent = "STARTING";
  setProgress(7);
  $("#jobOutput").textContent = "创建验收任务…";
  $("#jobSummary").textContent = "";
  try {
    const started = await api("/api/acceptance", { method: "POST", body: "{}" });
    await pollJob(started.job_id);
  } catch (error) {
    $("#jobTitle").textContent = "验收未能启动";
    $("#jobState").textContent = "ERROR";
    $("#jobOutput").textContent = error.message;
  } finally {
    button.disabled = false;
    $("#refreshButton").disabled = false;
    $("#authButton").disabled = false;
    $("#modelSelect").disabled = false;
    $("#alternateModelConfirm").disabled = false;
    document.querySelectorAll("[data-probe]").forEach((item) => { item.disabled = false; });
    button.innerHTML = '开始完整验收 <span>→</span>';
  }
}

async function pollJob(jobId) {
  let previousCount = 0;
  for (;;) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    const output = job.output || [];
    if (output.length !== previousCount) {
      $("#jobOutput").innerHTML = output.map((line) => `<div class="job-line ${classifyLine(line)}">${esc(line)}</div>`).join("");
      $("#jobOutput").scrollTop = $("#jobOutput").scrollHeight;
      previousCount = output.length;
    }
    const progress = Math.min(92, 10 + output.length * 2.5);
    setProgress(job.state === "running" ? progress : 100);
    if (job.state === "running") {
      $("#jobTitle").textContent = "完整验收运行中";
      $("#jobState").textContent = "RUNNING";
      await new Promise((resolve) => setTimeout(resolve, 1100));
      continue;
    }
    const report = job.report;
    const failed = job.state !== "completed" || (report?.counts?.FAIL || 0) > 0;
    $("#jobTitle").textContent = failed ? "验收完成：存在失败项" : "验收完成：本地自动化必测通过";
    $("#jobState").textContent = failed ? "CHECK" : "DONE";
    if (report) {
      const c = report.counts || {};
      $("#jobSummary").textContent = `PASS ${c.PASS || 0}  ·  FAIL ${c.FAIL || 0}  ·  SKIP ${c.SKIP || 0}  ·  UNKNOWN ${c.UNKNOWN || 0}　|　${report.conclusion}`;
      renderReport(report);
    } else if (job.error) $("#jobSummary").textContent = job.error;
    await refreshStatus(false);
    await refreshUsage();
    await loadLogs(false);
    toast(failed ? "完整验收结束，请查看失败项" : "本地 P0 自动验收完成");
    return;
  }
}

$("#refreshButton").addEventListener("click", () => refreshStatus(true));
$("#usageWindow").addEventListener("change", refreshUsage);
$("#refreshLogs").addEventListener("click", () => loadLogs(true));
$("#logsFilter").addEventListener("change", renderLogs);
$("#authButton").addEventListener("click", runAuth);
$("#acceptanceButton").addEventListener("click", runAcceptance);
$("#modelSelect").addEventListener("change", () => updateModelSelection(true));
document.querySelectorAll("[data-probe]").forEach((button) => {
  button.addEventListener("click", () => runProbe(button.dataset.probe, button));
});
$("#copyUrl").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText("http://ai-gateway-test.local:18080/v1"); toast("网关地址已复制"); }
  catch { toast("复制失败，请手动复制地址"); }
});

refreshStatus(false);
loadLogs(false);
refreshUsage();
setInterval(() => {
  if (!document.hidden) refreshUsage();
}, 15000);
