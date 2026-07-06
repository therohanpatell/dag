"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let editor, boot = null, wf = null, selected = null;
let cfToDf = {}, dfToCf = {}, pollTimer = null, suppressCycle = false;

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._timer); t._timer = setTimeout(() => (t.hidden = true), 2600);
}
function esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

// ---------- Drawflow canvas ----------
function nodeHtml(node) {
  const params = Object.keys(node.params || {}).length;
  const status = node.status || "pending";
  return `<div class="cf-node s-${status}" data-cf="${node.id}">
    <div class="cf-node-title">${esc(node.dag_id) || "(set DAG ID)"}</div>
    <div class="cf-node-sub">${esc(node.run_name) || ""}</div>
    <div class="cf-node-status"><span class="dot"></span>
      <span class="txt">${status.toUpperCase()}</span> · ${params} param</div>
  </div>`;
}
function addNodeToCanvas(node) {
  const df = editor.addNode("dag", 1, 1, node.x || 40, node.y || 40, "cfwrap",
    { cf: node.id, dag_id: node.dag_id, run_name: node.run_name, params: node.params || {} },
    nodeHtml(node));
  cfToDf[node.id] = df; dfToCf[df] = node.id;
  return df;
}
function refreshNodeCard(cfId) {
  const df = cfToDf[cfId]; if (df == null) return;
  const data = editor.getNodeFromId(df).data;
  const host = $(`#node-${df} .cf-node`); if (!host) return;
  $(".cf-node-title", host).textContent = data.dag_id || "(set DAG ID)";
  $(".cf-node-sub", host).textContent = data.run_name || "";
  $(".cf-node-status .txt", host).nextSibling.textContent =
    ` · ${Object.keys(data.params || {}).length} param`;
}
function setNodeStatus(cfId, status) {
  const df = cfToDf[cfId]; if (df == null) return;
  const host = $(`#node-${df} .cf-node`); if (!host) return;
  host.className = "cf-node s-" + status;
  $(".cf-node-status .txt", host).textContent = status.toUpperCase();
}

function collectWorkflow() {
  const data = editor.export().drawflow.Home.data;
  const nodes = [], edges = [];
  for (const id in data) {
    const n = data[id];
    nodes.push({ id: n.data.cf, dag_id: n.data.dag_id || "", run_name: n.data.run_name || "",
      params: n.data.params || {}, x: n.pos_x, y: n.pos_y });
    const conns = (n.outputs.output_1 && n.outputs.output_1.connections) || [];
    for (const c of conns) {
      const tgt = data[c.node] && data[c.node].data.cf;
      if (tgt) edges.push({ source: n.data.cf, target: tgt });
    }
  }
  wf.nodes = nodes; wf.edges = edges;
  return wf;
}

function hasCycle(nodes, edges) {
  const succ = {}; nodes.forEach(n => (succ[n.id] = []));
  edges.forEach(e => { if (succ[e.source]) succ[e.source].push(e.target); });
  const state = {}; // 0=unseen 1=in-stack 2=done
  const dfs = (u) => {
    state[u] = 1;
    for (const v of succ[u] || []) {
      if (state[v] === 1) return true;
      if (!state[v] && dfs(v)) return true;
    }
    state[u] = 2; return false;
  };
  return nodes.some(n => !state[n.id] && dfs(n.id));
}

// ---------- workflow load / render ----------
function renderCanvas() {
  editor.clear(); cfToDf = {}; dfToCf = {};
  suppressCycle = true;
  (wf.nodes || []).forEach(addNodeToCanvas);
  (wf.edges || []).forEach(e => {
    const so = cfToDf[e.source], ti = cfToDf[e.target];
    if (so != null && ti != null) editor.addConnection(so, ti, "output_1", "input_1");
  });
  suppressCycle = false;
  $("#wfTitle").textContent = wf.name || "—";
  showProps(null); updateSteps();
}

async function openWorkflow(id) {
  const data = await api("/api/workflow?id=" + encodeURIComponent(id));
  if (!data || !data.id) { wf = null; return; }
  wf = data; renderCanvas();
}

// ---------- properties ----------
function showProps(cfId) {
  selected = cfId;
  const empty = $("#propsEmpty"), body = $("#propsBody");
  if (cfId == null) { empty.hidden = false; body.hidden = true; return; }
  empty.hidden = true; body.hidden = false;
  const data = editor.getNodeFromId(cfToDf[cfId]).data;
  $("#pDagId").value = data.dag_id || "";
  $("#pRunName").value = data.run_name || "";
  renderParams(data.params || {});
  updateJson();
  updateSteps();
}
function currentData() { return editor.getNodeFromId(cfToDf[selected]).data; }
function commitData(patch) {
  const df = cfToDf[selected];
  const data = { ...editor.getNodeFromId(df).data, ...patch };
  editor.updateNodeDataFromId(df, data);
  refreshNodeCard(selected); updateJson();
}
function renderParams(params) {
  const tb = $("#paramsTable tbody"); tb.innerHTML = "";
  const rows = Object.entries(params);
  if (!rows.length) rows.push(["", ""]);
  rows.forEach(([k, v]) => addParamRow(k, v));
}
function addParamRow(k = "", v = "") {
  const tb = $("#paramsTable tbody");
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="k" placeholder="key" /></td>
    <td><input class="v" placeholder="value" /></td>
    <td><span class="del" title="remove">✕</span></td>`;
  $(".k", tr).value = k; $(".v", tr).value = v;
  $(".k", tr).oninput = $(".v", tr).oninput = syncParams;
  $(".del", tr).onclick = () => { tr.remove(); syncParams(); };
  tb.appendChild(tr);
}
function syncParams() {
  const params = {};
  $$("#paramsTable tbody tr").forEach(tr => {
    const k = $(".k", tr).value.trim();
    if (k) params[k] = $(".v", tr).value.trim();
  });
  commitData({ params });
}
function updateJson() {
  if (selected == null) return;
  $("#pJson").textContent = JSON.stringify(currentData().params || {}, null, 2);
}

// ---------- steps ribbon ----------
function updateSteps(step) {
  if (!step) {
    if (pollTimer) step = 4;
    else if (wf && wf.nodes && wf.nodes.length && wf.nodes.every(n => (n.dag_id || "").trim())) step = 3;
    else if (selected != null) step = 2; else step = 1;
  }
  $$("#steps .step").forEach(el => {
    const s = +el.dataset.step;
    el.classList.toggle("active", s === step);
    el.classList.toggle("done", s < step);
  });
}

// ---------- top bar: env / auth ----------
function renderEnv() {
  const sel = $("#envSelect"); sel.innerHTML = "";
  boot.profiles.forEach(p => {
    const o = document.createElement("option"); o.value = p; o.textContent = p;
    if (p === boot.active_profile) o.selected = true; sel.appendChild(o);
  });
  renderTargetChip(boot.target, boot.active_profile);
}
function renderTargetChip(t, profile) {
  const chip = $("#targetChip");
  chip.className = "chip" + (profile === "PRD" ? " chip-prd" : "");
  chip.textContent = t.complete ? `${t.project} / ${t.location} / ${t.environment}`
    : "not configured — see Settings";
}
function renderAuth() {
  const chip = $("#authChip"), a = boot.auth || {};
  if (a.authenticated) { chip.className = "chip chip-ok"; chip.textContent = "gcloud: " + a.account; }
  else { chip.className = "chip chip-bad"; chip.textContent = "gcloud: not signed in"; }
}

// ---------- workflows list ----------
function renderWorkflows(list, keep) {
  boot.workflows = list || boot.workflows;
  const sel = $("#workflowSelect"); sel.innerHTML = "";
  boot.workflows.forEach(w => {
    const o = document.createElement("option");
    o.value = w.id; o.textContent = `${w.name} (${w.node_count} DAGs)`;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
  return sel.value;
}

// ---------- console / run ----------
function appendConsole(lines) {
  const box = $("#console");
  box.innerHTML = lines.map((l, i) =>
    `<div><span class="ts">[${String(i + 1).padStart(3, "0")}]</span> ` +
    `<span class="l-${l.level}">${esc(l.message)}</span></div>`).join("");
  box.scrollTop = box.scrollHeight;
}
function applyRunState(st) {
  for (const [cf, status] of Object.entries(st.statuses || {})) setNodeStatus(cf, status);
  appendConsole(st.log || []);
  const { done, total } = st.progress || { done: 0, total: 0 };
  $("#progressBar").style.width = total ? (100 * done / total) + "%" : "0%";
  $("#progressText").textContent = total ? `${done}/${total} DAGs` : "";
  $("#etaText").textContent = st.eta || "";
  if (!st.running && st.final_status) {
    stopPolling();
    if (st.final_status === "success") toast("Workflow completed successfully ✔");
    else if (st.final_status === "failed") toast("Workflow FAILED — see console");
    else toast("Workflow " + st.final_status);
  }
}
function startPolling() {
  $("#runStrip").hidden = false; $("#runBtn").disabled = true; $("#stopBtn").disabled = false;
  updateSteps(4);
  const tick = async () => {
    try { applyRunState(await api("/api/run-state")); } catch (e) {}
  };
  tick(); pollTimer = setInterval(tick, 1200);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer); pollTimer = null;
  $("#runBtn").disabled = false; $("#stopBtn").disabled = true; updateSteps();
}

// ---------- actions ----------
async function saveWorkflow(silent) {
  collectWorkflow();
  const res = await api("/api/workflow/save", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(wf) });
  renderWorkflows(res.workflows, wf.id);
  if (!silent) toast("Saved.");
}
async function validateWorkflow() {
  collectWorkflow();
  const res = await api("/api/validate", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(wf) });
  if (res.ok && !res.issues.length) { toast("Workflow is valid ✔"); }
  else {
    const errs = res.issues.filter(i => i.level === "error");
    toast((errs.length ? "❌ " : "⚠ ") + (res.issues[0] ? res.issues[0].message : ""));
    alert(res.issues.map(i => `[${i.level.toUpperCase()}] ${i.message}`).join("\n"));
  }
  updateSteps();
}
async function runWorkflow() {
  await saveWorkflow(true);
  const profile = boot.active_profile;
  if (!boot.target.complete) { alert("Configure the " + profile + " environment in Settings first."); return; }
  if (!confirm(`Trigger workflow "${wf.name}" against ${profile}\n(${boot.target.project} / ${boot.target.environment})?`)) return;
  $("#console").innerHTML = "";
  try {
    await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(wf) });
    startPolling();
  } catch (e) { alert("Could not start run: " + e.message); }
}

// ---------- settings modal ----------
function openSettings() {
  const s = boot.settings, body = $("#settingsBody");
  let html = `<p class="muted">Fill each environment once; pick the active one from the top bar.</p>
    <div class="grid4"><div class="h"></div><div class="h">Composer environment</div>
    <div class="h">Location</div><div class="h">GCP project</div>`;
  boot.profiles.forEach(p => {
    html += `<div class="h">${p}</div>
      <input data-k="profile_${p}_environment" value="${esc(s["profile_" + p + "_environment"] || "")}" />
      <input data-k="profile_${p}_location" value="${esc(s["profile_" + p + "_location"] || "")}" placeholder="europe-west1" />
      <input data-k="profile_${p}_project" value="${esc(s["profile_" + p + "_project"] || "")}" />`;
  });
  html += `</div><hr/>
    <label>Status poll interval (s)</label><input data-k="poll_interval_seconds" value="${s.poll_interval_seconds}" />
    <label>Trigger command timeout (s)</label><input data-k="trigger_timeout_seconds" value="${s.trigger_timeout_seconds}" />
    <label>Max parallel DAGs</label><input data-k="max_parallel_dags" value="${s.max_parallel_dags}" />
    <label>CLI retries (transient errors)</label><input data-k="cli_retry_count" value="${s.cli_retry_count}" />`;
  body.innerHTML = html;
  $("#settingsModal").hidden = false;
}
async function saveSettings() {
  const payload = {};
  $$("#settingsBody [data-k]").forEach(i => (payload[i.dataset.k] = i.value.trim()));
  const res = await api("/api/settings/save", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  Object.assign(boot.settings, payload); boot.target = res.target;
  renderTargetChip(res.target, boot.active_profile);
  $("#settingsModal").hidden = true; toast("Settings saved.");
}

// ---------- history modal ----------
async function openHistory() {
  const res = await api("/api/history");
  const rows = res.executions.map(h =>
    `<tr class="clickable" data-id="${h.id}"><td>${esc(h.workflow_name)}</td>
     <td>${h.status.toUpperCase()}</td><td>${h.started_at}</td><td>${h.finished_at}</td>
     <td>${esc(h.error || "")}</td></tr>`).join("");
  $("#historyBody").innerHTML =
    `<table class="data"><thead><tr><th>Workflow</th><th>Status</th><th>Started</th>
     <th>Finished</th><th>Error</th></tr></thead><tbody>${rows || ""}</tbody></table>
     <div id="histDetail"></div>`;
  $$("#historyBody tbody tr").forEach(tr => tr.onclick = () => showHistoryDetail(tr.dataset.id,
    res.executions.find(e => e.id === tr.dataset.id)));
  $("#historyModal").hidden = false;
}
async function showHistoryDetail(id, row) {
  const res = await api("/api/execution?id=" + encodeURIComponent(id));
  const rows = res.nodes.map(n =>
    `<tr><td>${esc(n.dag_id)}</td><td>${n.status.toUpperCase()}</td>
     <td>${esc(n.airflow_run_id)}</td><td>${(n.duration_seconds || 0).toFixed(0)}s</td>
     <td>${n.retry_count}</td><td>${esc((n.error || "").slice(0, 120))}</td></tr>`).join("");
  let html = `<h4>DAG runs</h4><table class="data"><thead><tr><th>DAG</th><th>Status</th>
    <th>Run-id</th><th>Duration</th><th>Retries</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`;
  if (row && row.status === "failed") html += `<button class="btn btn-primary" id="rerunBtn">🔁 Rerun failed DAGs only</button>`;
  $("#histDetail").innerHTML = html;
  const rb = $("#rerunBtn");
  if (rb) rb.onclick = async () => {
    const r = await api("/api/rerun-failed", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ execution_id: id }) });
    if (r.workflow) { wf = r.workflow; renderCanvas(); }
    $("#historyModal").hidden = true; startPolling();
  };
}

// ---------- init ----------
function initEditor() {
  editor = new Drawflow($("#drawflow"));
  editor.reroute = true;
  editor.start();
  window.editor = editor;  // exposed for debugging/automation
  editor.on("nodeSelected", id => showProps(dfToCf[id]));
  editor.on("nodeUnselected", () => showProps(null));
  editor.on("nodeRemoved", id => { const cf = dfToCf[id]; delete dfToCf[id]; delete cfToDf[cf];
    if (selected === cf) showProps(null); });
  editor.on("connectionCreated", info => {
    if (suppressCycle) return;
    collectWorkflow();
    if (hasCycle(wf.nodes, wf.edges)) {
      editor.removeSingleConnection(info.output_id, info.input_id, info.output_class, info.input_class);
      toast("That connection would create a cycle — not allowed.");
    }
  });
}

async function boot_load() {
  boot = await api("/api/bootstrap");
  renderEnv(); renderAuth();
  const id = renderWorkflows();
  if (id) await openWorkflow(id);
  updateSteps();
}

function wire() {
  $("#envSelect").onchange = async (e) => {
    const r = await api("/api/profile", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: e.target.value }) });
    boot.active_profile = e.target.value; boot.target = r.target;
    renderTargetChip(r.target, boot.active_profile);
  };
  $("#authBtn").onclick = async () => {
    try { await api("/api/auth/login", { method: "POST" });
      toast("Browser opening for Google sign-in… then click here again to refresh.");
      setTimeout(async () => { try { boot.auth = await api("/api/auth?force=1"); renderAuth(); } catch (e) {} }, 4000);
    } catch (e) { alert(e.message); }
  };
  $("#authChip").onclick = async () => { boot.auth = await api("/api/auth?force=1"); renderAuth(); };
  $("#workflowSelect").onchange = e => openWorkflow(e.target.value);
  $("#newBtn").onclick = async () => {
    const name = $("#newName").value.trim(); if (!name) return toast("Enter a name.");
    const r = await api("/api/workflow/new", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    $("#newName").value = ""; renderWorkflows(r.workflows, r.id); await openWorkflow(r.id);
  };
  $("#deleteBtn").onclick = async () => {
    if (!wf || !confirm(`Delete workflow "${wf.name}"?`)) return;
    const r = await api("/api/workflow/delete", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: wf.id }) });
    wf = null; editor.clear(); $("#wfTitle").textContent = "—";
    const id = renderWorkflows(r.workflows); if (id) await openWorkflow(id);
  };
  $("#addNodeBtn").onclick = () => {
    if (!wf) return toast("Create or open a workflow first.");
    const id = crypto.randomUUID().replace(/-/g, "");
    const node = { id, dag_id: "", run_name: "", params: {}, x: 80 + Math.random() * 80, y: 80 + Math.random() * 80 };
    const df = addNodeToCanvas(node);
    editor.getNodeFromId(df); showProps(id);
  };
  $("#autoBtn").onclick = () => { collectWorkflow(); autoLayout(); };
  $("#validateBtn").onclick = validateWorkflow;
  $("#saveBtn").onclick = () => saveWorkflow(false);
  $("#runBtn").onclick = runWorkflow;
  $("#stopBtn").onclick = async () => { await api("/api/cancel", { method: "POST" }); toast("Stopping…"); };
  $("#pDagId").oninput = e => commitData({ dag_id: e.target.value.trim() });
  $("#pRunName").oninput = e => commitData({ run_name: e.target.value.trim() });
  $("#addParamBtn").onclick = () => addParamRow();
  $("#delNodeBtn").onclick = () => { if (selected != null) { editor.removeNodeId("node-" + cfToDf[selected]); showProps(null); } };
  $("#settingsBtn").onclick = openSettings;
  $("#saveSettingsBtn").onclick = saveSettings;
  $("#historyBtn").onclick = openHistory;
  $("#exportBtn").onclick = () => {
    if (!wf) return; collectWorkflow();
    const blob = new Blob([JSON.stringify(wf, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = (wf.name || "workflow") + ".json"; a.click();
  };
  $("#importFile").onchange = async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const r = await api("/api/workflow/import", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workflow: data }) });
      renderWorkflows(r.workflows, r.id); await openWorkflow(r.id); toast("Imported.");
    } catch (err) { alert("Import failed: " + err.message); }
    e.target.value = "";
  };
  $$("[data-close]").forEach(b => b.onclick = () => { b.closest(".modal").hidden = true; });
}

function autoLayout() {
  // simple topological banding by longest-path depth
  const succ = {}, indeg = {};
  wf.nodes.forEach(n => { succ[n.id] = []; indeg[n.id] = 0; });
  wf.edges.forEach(e => { succ[e.source].push(e.target); indeg[e.target]++; });
  const depth = {}; const q = wf.nodes.filter(n => !indeg[n.id]).map(n => n.id);
  q.forEach(id => (depth[id] = 0));
  const queue = [...q];
  while (queue.length) {
    const u = queue.shift();
    for (const v of succ[u]) {
      depth[v] = Math.max(depth[v] || 0, depth[u] + 1);
      if (--indeg[v] === 0) queue.push(v);
    }
  }
  const band = {};
  wf.nodes.forEach(n => { const d = depth[n.id] || 0; band[d] = band[d] || 0;
    const df = cfToDf[n.id];
    editor.getNodeFromId(df); // ensure exists
    const x = 80 + (band[d]) * 250, y = 60 + d * 170; band[d]++;
    editor.updateNodeDataFromId(df, editor.getNodeFromId(df).data);
    const el = $(`#node-${df}`); if (el) { el.style.left = x + "px"; el.style.top = y + "px"; }
    const node = editor.drawflow.drawflow.Home.data[df]; if (node) { node.pos_x = x; node.pos_y = y; }
  });
  editor.import(editor.export()); // redraw connections
  rebindAfterImport();
}
function rebindAfterImport() {
  // after import, DOM ids stay; nothing extra needed for our maps
}

window.addEventListener("DOMContentLoaded", async () => {
  initEditor(); wire();
  try { await boot_load(); } catch (e) { alert("Failed to load app: " + e.message); }
});
