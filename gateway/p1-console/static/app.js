const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
let apps = [];
let selectedPolicyApp = null;
let jobPoll = null;
let organizationAllowedModels = [];

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function formatCount(value) { return value == null ? "—" : Number(value).toLocaleString("zh-CN"); }
function statePill(runtime) {
  if (runtime?.error) return '<span class="pill bad">未知</span>';
  return runtime?.blocked ? '<span class="pill bad">LiteLLM 已禁用</span>' : '<span class="pill good">可用</span>';
}

function renderApps(rows) {
  apps = rows || [];
  const table = $("#appsTable");
  if (!apps.length) { table.innerHTML = '<tr><td colspan="5" class="empty">没有读取到应用清单。</td></tr>'; return; }
  table.innerHTML = apps.map((app) => {
    const r = app.runtime || {};
    const models = (r.models || app.models || []).map((m) => `<span class="model-chip">${esc(m)}</span>`).join("") || '<span class="muted">未读取</span>';
    const policy = `${formatCount(r.rpm_limit ?? app.rpm_limit)} RPM · ${formatCount(r.tpm_limit ?? app.tpm_limit)} TPM · ${formatCount(r.max_parallel_requests ?? app.max_parallel_requests)} 并发`;
    return `<tr><td><b>${esc(app.display_name)}</b><small>${esc(app.app_id)}</small></td><td>${models}<small>${esc(policy)}</small></td><td>${statePill(r)}</td><td><span class="key-state">${app.higress_key_configured ? "已配置（仅服务端读取）" : "缺失"}</span></td><td><div class="row-actions"><button data-connection="${esc(app.app_id)}">接入信息</button><button data-action="Get" data-app="${esc(app.app_id)}">查询</button><button data-action="Reconcile" data-app="${esc(app.app_id)}">对账</button><button data-policy="${esc(app.app_id)}">策略</button><button data-action="${r.blocked ? "Enable" : "Disable"}" data-app="${esc(app.app_id)}">${r.blocked ? "恢复" : "禁用"}</button><button data-action="RotateKeyH" data-app="${esc(app.app_id)}">轮换 Key-H</button><button data-action="RetireKeyH" data-app="${esc(app.app_id)}">退休旧 Key</button></div></td></tr>`;
  }).join("");
  table.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAppAction(button.dataset.action, button.dataset.app)));
  table.querySelectorAll("[data-policy]").forEach((button) => button.addEventListener("click", () => openPolicy(button.dataset.policy)));
  table.querySelectorAll("[data-connection]").forEach((button) => button.addEventListener("click", () => loadConnection(button.dataset.connection)));
  const old = $("#probeApp").value;
  $("#probeApp").innerHTML = apps.map((a) => `<option value="${esc(a.app_id)}">${esc(a.display_name)}</option>`).join("");
  if (apps.some((a) => a.app_id === old)) $("#probeApp").value = old;
  updateProbeModels();
  const connectionOld = $("#connectionApp").value;
  $("#connectionApp").innerHTML = apps.map((a) => `<option value="${esc(a.app_id)}">${esc(a.display_name)}</option>`).join("");
  if (apps.some((a) => a.app_id === connectionOld)) $("#connectionApp").value = connectionOld;
}

function updateProbeModels() {
  const app = apps.find((item) => item.app_id === $("#probeApp").value) || apps[0];
  const models = [...new Set([...(app?.models || []), "not-authorized-model"])];
  const old = $("#probeModel").value;
  const kindOf = (m) => m.toLowerCase().includes("embedding") ? "Embedding" : m.toLowerCase().includes("image") ? "Image" : "Chat";
  $("#probeModel").innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)} · ${kindOf(m)}${m === "not-authorized-model" ? "（预期拒绝）" : ""}</option>`).join("");
  if (models.includes(old)) $("#probeModel").value = old;
}

function renderStatus(data) {
  const running = (data.services || []).filter((x) => x.state === "running").length;
  $("#serviceSummary").textContent = `${running} / ${(data.services || []).length || 5}`;
  $("#serviceDetail").textContent = data.services_ok ? "服务运行正常" : "存在未就绪服务";
  $("#appSummary").textContent = `${(data.apps || []).length} 个应用`;
  $("#modelSummary").textContent = `${(data.models || []).length} 个可调用`;
  $("#modelDetail").textContent = data.model_status === 200 ? `完整配置 ${((data.model_catalog || []).length || (data.models || []).length)} 个` : `HTTP ${data.model_status || "不可达"}`;
  const report = data.latest_report;
  $("#reportSummary").textContent = report?.status || "暂无";
  $("#reportDetail").textContent = report ? report.name : "尚未生成 P1 报告";
  $("#overallState").innerHTML = `<i class="${data.ok ? "" : "bad"}"></i>${data.ok ? "P1 环境就绪" : "环境需要检查"}`;
  $("#checkedAt").textContent = `更新于 ${new Date(data.checked_at).toLocaleTimeString("zh-CN", { hour12: false })}`;
  $("#boundaries").innerHTML = (data.boundaries || []).map((item) => `<div><span>!</span>${esc(item)}</div>`).join("");
  renderApps(data.apps || []);
  renderModelCatalog(data.model_catalog || (data.models || []).map((id) => ({ id, configured: false, p1_configured: true, gateway_active: true })));
  renderOrganizationPolicy(data.organization_policy || { allowed_models: data.models || [] }, data.model_catalog || []);
  ensureOrganizationScopeHint();
  renderMultiChoice("organizationModels");
  const catalog = (data.model_catalog || []).filter((item) => item.gateway_active && (data.organization_policy?.allowed_models || []).includes(item.id));
  $("#createModel").innerHTML = catalog.map((item) => `<option value="${esc(item.id)}" ${item.gateway_active ? "" : "disabled"}>${esc(item.id)}${item.gateway_active ? "" : "（当前 P1 未启用）"}</option>`).join("");
  $("#createModel").multiple = true;
  if (catalog.length && !$("#createModel").selectedOptions.length) $("#createModel").options[0].selected = true;
  if (!$("#createModelHint")) { const hint = document.createElement("small"); hint.id = "createModelHint"; hint.textContent = "可多选模型（按 Ctrl/Command 选择多个）"; $("#createModel").parentElement.appendChild(hint); }
  renderMultiChoice("createModel");
  if ($("#createModelHint")) { $("#createModelHint").textContent = "点击模型标签切换选中状态，再次点击即可取消"; }
}

function renderOrganizationPolicy(policy, catalog) {
  const allowed = new Set(policy.allowed_models || []);
  organizationAllowedModels = [...allowed];
  const options = (catalog || []).filter((item) => item.gateway_active).map((item) => `<option value="${esc(item.id)}" ${allowed.has(item.id) ? "selected" : ""}>${esc(item.id)} · ${esc(item.kind || "chat")}</option>`).join("");
  $("#organizationModels").innerHTML = options || '<option disabled>暂无可用模型</option>';
  $("#organizationTitle").textContent = `${policy.organization_name || "当前测试组织"} · ${allowed.size} 个允许模型`;
}

function organizationPolicySummary(policy) {
  const models = policy?.allowed_models || [];
  return [`作用范围：组织级（不是某个应用）`, `组织：${policy?.organization_name || "当前测试组织"}（${policy?.organization_id || "org-p1-test"}）`, `允许模型：${models.length} 个`, "", ...models.map((model) => `- ${model}`), "", `最后修改：${policy?.updated_by || "未知"}`, "应用实际可用模型请到“应用与 LiteLLM 原生策略”中查看。"].join("\\n");
}

function ensureOrganizationScopeHint() {
  if ($("#organizationScopeHint")) return;
  const hint = document.createElement("div");
  hint.id = "organizationScopeHint";
  hint.className = "hint-box";
  hint.textContent = "这是组织级模型白名单，不属于某一个应用。它决定新应用可以选择的范围；每个应用最终能调用哪些模型，请查看上方应用列表中的“当前策略”。";
  $("#organizationModels").closest("label").after(hint);
}

function renderMultiChoice(selectId) {
  const select = $("#" + selectId);
  if (!select) return;
  let host = $("#" + selectId + "Choices");
  if (!host) {
    host = document.createElement("div");
    host.id = selectId + "Choices";
    host.className = "multi-choice-list";
    select.after(host);
    select.hidden = true;
  }
  host.innerHTML = "";
  Array.from(select.options).forEach((option) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `multi-choice${option.selected ? " selected" : ""}${option.disabled ? " disabled" : ""}`;
    item.disabled = option.disabled;
    item.textContent = option.textContent;
    item.addEventListener("click", () => { option.selected = !option.selected; renderMultiChoice(selectId); });
    host.appendChild(item);
  });
  const count = document.createElement("small");
  count.className = "multi-choice-count";
  count.textContent = `已选择 ${select.selectedOptions.length} 个模型；再次点击可取消`;
  host.appendChild(count);
}

function ensurePolicyModelPicker() {
  if ($("#policyModels")) return;
  const label = document.createElement("label"); label.textContent = "允许模型";
  const select = document.createElement("select"); select.id = "policyModels"; select.multiple = true; select.size = 4;
  label.appendChild(select); $(".policy-fields").prepend(label);
}

function renderModelCatalog(catalog) {
  const active = catalog.filter((item) => item.gateway_active).length;
  $("#catalogSummary").textContent = `${catalog.length} 个模型 · ${active} 个当前可用`;
  $("#modelCatalog").innerHTML = catalog.length ? catalog.map((item) => `<div class="catalog-card"><div><b>${esc(item.id)}</b><span class="catalog-state ${item.gateway_active ? "active" : "inactive"}">${item.gateway_active ? "P1 可调用" : item.p1_configured ? "P1 已配置，网关未返回" : "根配置存在，P1 未启用"}</span></div><small>类型：${esc(item.kind || "chat")} · 统一入口：/v1 · 上游由 LiteLLM 代理</small></div>`).join("") : '<div class="empty">未读取到模型配置。</div>';
}

async function refreshStatus(showMessage = false) {
  try { renderStatus(await api("/api/status")); if (showMessage) flash("P1 状态已刷新"); }
  catch (error) { $("#overallState").innerHTML = `<i class="bad"></i>控制台读取失败`; $("#checkedAt").textContent = error.message; }
}

let connectionAppId = null;
let connectionKeyValue = "";

async function loadConnection(appId = $("#connectionApp").value) {
  if (!appId) return;
  try {
    const data = await api("/api/app/connection", { method: "POST", body: JSON.stringify({ app_id: appId }) });
    if (!data.ok) throw new Error(data.error || "接入信息读取失败");
    connectionAppId = appId; connectionKeyValue = "";
    $("#connectionTitle").textContent = `接入信息：${data.display_name}`;
    $("#connectionBaseUrl").textContent = data.ccswitch_url; $("#connectionOpenAiUrl").textContent = data.base_url; $("#connectionModelsUrl").textContent = data.models_url;
    $("#connectionChatUrl").textContent = data.chat_url; $("#connectionResponsesUrl").textContent = data.responses_url;
    $("#connectionAuthorization").textContent = data.authorization; $("#connectionKey").textContent = data.key_masked;
    $("#connectionNote").textContent = `${data.host_note} ${data.key_note} 当前授权模型：${(data.models || []).join(", ") || "无"}`;
    $("#copyKey").disabled = true; $("#connectionPanel").hidden = false;
    $("#connectionPanel").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) { flash(error.message); }
}

async function revealConnectionKey() {
  if (!connectionAppId || !window.confirm("完整 Key 只用于本机测试客户端配置，确认显示？")) return;
  try {
    const data = await api("/api/app/connection", { method: "POST", body: JSON.stringify({ app_id: connectionAppId, reveal: true, confirm: true }) });
    if (!data.ok || !data.key) throw new Error(data.error || "Key 未配置");
    connectionKeyValue = data.key; $("#connectionKey").textContent = data.key; $("#copyKey").disabled = false; flash("完整 Key 已显示，仅用于本机测试");
  } catch (error) { flash(error.message); }
}

async function copyText(value) {
  try { await navigator.clipboard.writeText(value); flash("已复制"); }
  catch (_error) { flash("浏览器不允许自动复制，请手动选择文本"); }
}

function copyConnectionField(id) { const value = $("#" + id).textContent; if (value && value !== "—") copyText(value); }

async function saveOrganizationPolicy() {
  const models = Array.from($("#organizationModels").selectedOptions).map((option) => option.value);
  if (!models.length || !window.confirm("确认更新当前组织允许模型？这会限制新建应用和后续应用策略。")) return;
  try {
    const data = await api("/api/organization/policy", { method: "POST", body: JSON.stringify({ allowed_models: models, confirm: true }) });
    $("#organizationOutput").textContent = data.ok ? organizationPolicySummary(data.organization_policy) : `${data.error}\n${JSON.stringify(data.violations || [], null, 2)}`;
    $("#organizationOutput").className = `output ${data.ok ? "success" : "failure"}`;
    flash(data.ok ? "组织模型策略已更新" : "组织模型策略未更新");
    if (data.ok) await refreshStatus();
  } catch (error) { $("#organizationOutput").textContent = error.message; $("#organizationOutput").className = "output failure"; }
}

function openPolicy(appId) {
  const app = apps.find((item) => item.app_id === appId); if (!app) return;
  const r = app.runtime || {};
  ensurePolicyModelPicker();
  selectedPolicyApp = appId;
  $("#policyTitle").textContent = `编辑：${app.display_name}`;
  $("#policyRpm").value = r.rpm_limit ?? app.rpm_limit;
  $("#policyTpm").value = r.tpm_limit ?? app.tpm_limit;
  $("#policyParallel").value = r.max_parallel_requests ?? app.max_parallel_requests;
  const policyModels = [...new Set([...organizationAllowedModels, ...(app.models || [])])];
  $("#policyModels").innerHTML = policyModels.map((model) => `<option value="${esc(model)}" ${app.models?.includes(model) ? "selected" : ""} ${organizationAllowedModels.includes(model) ? "" : "disabled"}>${esc(model)}${organizationAllowedModels.includes(model) ? "" : "（组织未允许）"}</option>`).join("");
  renderMultiChoice("policyModels");
  $("#policyEditor").hidden = false;
  $("#policyEditor").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function runAppAction(action, appId) {
  const labels = { Get: "查询", Reconcile: "对账", Disable: "禁用", Enable: "恢复", RotateKeyH: "轮换 Key-H", RetireKeyH: "退休旧 Key-H" };
  if (["Disable", "Enable", "RotateKeyH", "RetireKeyH"].includes(action) && !window.confirm(`确认执行“${labels[action]}”？这会修改 P1 测试环境。`)) return;
  try {
    const data = await api("/api/app/action", { method: "POST", body: JSON.stringify({ action, app_id: appId, confirm: true }) });
    flash(`${labels[action]}${data.ok ? "完成" : "失败"}`); showAppOutput(data.output || data.error || JSON.stringify(data, null, 2), data.ok);
    await refreshStatus();
  } catch (error) { showAppOutput(error.message, false); }
}

async function savePolicy() {
  if (!selectedPolicyApp) return;
  const app = apps.find((item) => item.app_id === selectedPolicyApp); if (!app) return;
  try {
    const models = Array.from($("#policyModels").selectedOptions).map((option) => option.value);
    const data = await api("/api/app/action", { method: "POST", body: JSON.stringify({ action: "SetPolicy", app_id: selectedPolicyApp, models, rpm_limit: Number($("#policyRpm").value), tpm_limit: Number($("#policyTpm").value), max_parallel_requests: Number($("#policyParallel").value) }) });
    showAppOutput(data.output || data.error || "策略已更新。", data.ok); flash(data.ok ? "策略更新完成" : "策略更新失败");
    if (data.ok) { $("#policyEditor").hidden = true; await refreshStatus(); }
  } catch (error) { showAppOutput(error.message, false); }
}

async function createApplication() {
  if (!$("#createConfirm").checked) { flash("请先确认本地创建操作"); return; }
  const name = $("#createName").value.trim();
  if (!name) { flash("请填写应用名称"); return; }
  try {
    const models = Array.from($("#createModel").selectedOptions).map((option) => option.value);
    if (!models.length) { flash("至少选择一个组织允许模型"); return; }
    const data = await api("/api/app/create", { method: "POST", body: JSON.stringify({ confirm: true, display_name: name, models, rpm_limit: Number($("#createRpm").value), tpm_limit: Number($("#createTpm").value), max_parallel_requests: Number($("#createParallel").value) }) });
    showAppOutput(data.output || data.error || "创建完成。", data.ok);
    flash(data.ok ? "应用创建并验证完成" : "应用创建失败");
    if (data.ok) { $("#createEditor").hidden = true; $("#createConfirm").checked = false; $("#createName").value = ""; await refreshStatus(); await refreshSnapshots(); }
  } catch (error) { showAppOutput(error.message, false); }
}

function showOutput(text, ok = true) { $("#probeOutput").textContent = text; $("#probeOutput").className = `output ${ok ? "success" : "failure"}`; }
function showAppOutput(text, ok = true) { $("#appOutput").textContent = text; $("#appOutput").className = `output ${ok ? "success" : "failure"}`; }
function flash(message) { const node = document.createElement("div"); node.className = "toast"; node.textContent = message; document.body.appendChild(node); setTimeout(() => node.remove(), 2600); }

async function runProbe(kind) {
  const appId = $("#probeApp").value; let model = $("#probeModel").value || "my-qwen3.6-27b";
  if (kind === "unauthorized-model") model = "not-authorized-model";
  document.querySelectorAll("[data-probe]").forEach((b) => { b.disabled = true; });
  showOutput("正在通过 P1 Higress 发起验证…", true);
  try { const data = await api("/api/probe", { method: "POST", body: JSON.stringify({ kind, app_id: appId, model }) }); showOutput(JSON.stringify(data, null, 2), data.ok); await refreshUsage(); await loadLogs(); }
  catch (error) { showOutput(error.message, false); }
  finally { document.querySelectorAll("[data-probe]").forEach((b) => { b.disabled = false; }); }
}

async function refreshUsage() {
  try {
    const data = await api(`/api/usage?window=${encodeURIComponent($("#usageWindow").value)}`); if (!data.ok) throw new Error(data.error);
    const u = data.usage || {}; const g = data.gateway_metrics || {};
    const gatewayCard = $("#gatewayRequests").parentElement;
    if (gatewayCard) { gatewayCard.querySelector("small").textContent = g.ok ? "网关累计请求（自 Higress 启动）" : "网关累计请求（指标不可用）"; gatewayCard.querySelector("span").textContent = g.ok ? "Envoy 原生累计计数器，不受上方时间区间影响" : "请检查 Higress Prometheus 指标端口"; }
    $("#gatewayRequests").textContent = g.ok ? formatCount(g.requests_total) : "暂不可用";
    $("#usageRequests").textContent = formatCount(u.requests); $("#usageSuccess").textContent = formatCount(u.successful_requests); $("#usageFailure").textContent = formatCount(u.failed_requests); $("#usageTokens").textContent = formatCount(u.total_tokens); $("#usagePrompt").textContent = formatCount(u.prompt_tokens); $("#usageCompletion").textContent = formatCount(u.completion_tokens); $("#gatewayRequests").textContent = formatCount(g.requests_total); $("#usageLatency").textContent = u.average_duration_ms == null ? "—" : `${u.average_duration_ms} ms`;
    $("#usageUpdated").textContent = `${data.window_label} · ${new Date(data.refreshed_at).toLocaleTimeString("zh-CN", { hour12: false })}`;
    const models = u.models || []; $("#modelUsage").className = models.length ? "model-usage" : "model-usage empty"; $("#modelUsage").innerHTML = models.length ? `<table><thead><tr><th>模型</th><th>请求</th><th>Tokens</th></tr></thead><tbody>${models.map((m) => `<tr><td>${esc(m.model)}</td><td>${formatCount(m.requests)}</td><td>${formatCount(m.total_tokens)}</td></tr>`).join("")}</tbody></table>` : "此时间范围内暂无调用";
  } catch (error) { $("#usageUpdated").textContent = `读取失败：${error.message}`; }
}

async function loadLogs() {
  try { const data = await api("/api/logs?limit=20"); if (!data.ok) throw new Error(data.error); const list = $("#logsList"); list.innerHTML = data.items?.length ? data.items.map((item) => `<details><summary><b>${esc(item.status)}</b><span>${esc(item.model)}</span><span>${formatCount(item.total_tokens)} tokens</span><time>${esc(item.ended_at || item.started_at || "")}</time></summary><pre>${esc(item.response_body || item.request_body || item.messages || "无正文")}</pre></details>`).join("") : '<div class="empty">暂无最近调用。</div>'; }
  catch (error) { $("#logsList").innerHTML = `<div class="failure">${esc(error.message)}</div>`; }
}

async function startJob(kind) {
  if (!$("#jobConfirm").checked) { flash("请先确认测试环境变更风险"); return; }
  try { const start = await api("/api/job", { method: "POST", body: JSON.stringify({ kind, confirm: true }) }); if (!start.ok) throw new Error(start.error); $("#jobState").textContent = `运行中：${kind}`; $("#jobOutput").textContent = "任务已启动…"; if (jobPoll) clearInterval(jobPoll); jobPoll = setInterval(async () => { const job = await api(`/api/jobs/${encodeURIComponent(start.job_id)}`); $("#jobOutput").textContent = (job.output || []).join("\n"); $("#jobOutput").scrollTop = $("#jobOutput").scrollHeight; if (job.state !== "running") { clearInterval(jobPoll); jobPoll = null; $("#jobState").textContent = `任务${job.state === "completed" ? "完成" : "失败"}（退出码 ${job.exit_code ?? "?"}）`; await refreshStatus(); } }, 1000); }
  catch (error) { $("#jobState").textContent = error.message; }
}

async function refreshSnapshots() {
  try {
    const data = await api("/api/snapshots");
    $("#snapshotSelect").innerHTML = '<option value="">选择回滚快照</option>' + (data.snapshots || []).map((item) => `<option value="${esc(item)}">${esc(item)}</option>`).join("");
  } catch (error) { $("#configOutput").textContent = error.message; }
}

async function configAction(path, body = {}) {
  if (!window.confirm("确认执行此 P1 配置操作？这可能改变 Higress 测试运行态。")) return;
  try { const data = await api(path, { method: "POST", body: JSON.stringify({ ...body, confirm: true }) }); $("#configOutput").textContent = data.output || data.error || JSON.stringify(data, null, 2); flash(data.ok ? "配置操作完成" : "配置操作失败"); await refreshSnapshots(); await refreshStatus(); }
  catch (error) { $("#configOutput").textContent = error.message; }
}

$("#refreshButton").addEventListener("click", () => refreshStatus(true));
$("#usageWindow").addEventListener("change", refreshUsage); $("#refreshLogs").addEventListener("click", loadLogs); $("#probeApp").addEventListener("change", updateProbeModels); $("#savePolicy").addEventListener("click", savePolicy); $("#cancelPolicy").addEventListener("click", () => { $("#policyEditor").hidden = true; });
$("#createAppButton").addEventListener("click", () => { $("#createEditor").hidden = false; $("#createEditor").scrollIntoView({ behavior: "smooth", block: "center" }); });
$("#cancelCreate").addEventListener("click", () => { $("#createEditor").hidden = true; }); $("#submitCreate").addEventListener("click", createApplication);
$("#loadConnection").addEventListener("click", () => loadConnection()); $("#closeConnection").addEventListener("click", () => { $("#connectionPanel").hidden = true; }); $("#revealKey").addEventListener("click", revealConnectionKey); $("#copyKey").addEventListener("click", () => { if (connectionKeyValue) copyText(connectionKeyValue); }); document.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", () => copyConnectionField(button.dataset.copy)));
$("#saveOrganizationPolicy").addEventListener("click", saveOrganizationPolicy);
$("#snapshotButton").addEventListener("click", () => configAction("/api/config/snapshot")); $("#publishButton").addEventListener("click", () => configAction("/api/config/publish")); $("#rollbackButton").addEventListener("click", () => { const value = $("#snapshotSelect").value; if (!value) { flash("请选择回滚快照"); return; } configAction("/api/config/rollback", { snapshot_id: value }); });
document.querySelectorAll("[data-probe]").forEach((button) => button.addEventListener("click", () => runProbe(button.dataset.probe)));
document.querySelectorAll("[data-job]").forEach((button) => button.addEventListener("click", () => startJob(button.dataset.job)));
refreshStatus(); refreshUsage(); loadLogs(); refreshSnapshots(); setInterval(() => { if (!document.hidden) refreshUsage(); }, 15000);
