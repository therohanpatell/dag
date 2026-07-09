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

// inline SVG icons for dynamically-built markup
const ICON = {
  link: '<svg class="ic" viewBox="0 0 24 24"><path d="M9 15l6-6M10.5 6.5 12 5a4 4 0 0 1 6 6l-1.5 1.5M13.5 17.5 12 19a4 4 0 0 1-6-6l1.5-1.5"/></svg>',
  repeat: '<svg class="ic" viewBox="0 0 24 24"><path d="M17 2l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 22l-4-4 4-4M21 13v2a4 4 0 0 1-4 4H3"/></svg>',
  close: '<svg class="ic" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
};

// ---------- auto-save (no manual Save button) ----------
let autosaveTimer = null;
function setDirty(v) {
  const b = $("#dirtyBadge");
  if (v) {
    if (b) { b.hidden = false; b.textContent = "unsaved"; }
    scheduleAutosave();
  } else if (b) { b.hidden = true; }
}
function scheduleAutosave() {
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(doAutosave, 700);
}
async function doAutosave() {
  if (!wf || pollTimer) return;   // nothing open, or a run is in progress
  const b = $("#dirtyBadge"); if (b) b.textContent = "saving...";
  try { await saveWorkflow(true); }
  catch (e) { if (b) { b.hidden = false; b.textContent = "save failed"; } }
}

// ---------- Drawflow canvas ----------
function sharedCount() {
  return wf && wf.shared_params ? Object.keys(wf.shared_params).length : 0;
}
function nodeInner(data) {
  const params = Object.keys(data.params || {}).length;
  const status = data.status || "pending";
  const sc = sharedCount();
  const badge = sc ? `<div class="node-shared-badge" title="This DAG also receives the workflow's shared parameters">${ICON.link} +${sc} shared</div>` : "";
  return `<div class="cf-node-title">${esc(data.dag_id) || "(set DAG ID)"}</div>
    <div class="cf-node-sub">${esc(data.run_name) || ""}</div>
    <div class="cf-node-status"><span class="dot"></span>
      <span class="txt">${status.toUpperCase()}</span> · ${params} param</div>${badge}`;
}
function nodeHtml(node) {
  const status = node.status || "pending";
  return `<div class="cf-node s-${status}" data-cf="${node.id}">${nodeInner(node)}</div>`;
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
  const host = $(`#node-${df} .cf-node`); if (!host) return;
  host.innerHTML = nodeInner(editor.getNodeFromId(df).data);
}
function refreshAllCards() { Object.keys(cfToDf).forEach(refreshNodeCard); }
function setNodeStatus(cfId, status) {
  const df = cfToDf[cfId]; if (df == null) return;
  const node = editor.getNodeFromId(df); if (!node) return;
  node.data.status = status;
  const host = $(`#node-${df} .cf-node`); if (!host) return;
  host.className = "cf-node s-" + status;
  const txt = $(".cf-node-status .txt", host); if (txt) txt.textContent = status.toUpperCase();
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
  $("#wfTitle").textContent = wf.name || "-";
  showProps(null);
  renderShared();
  setDirty(false);
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
  updateSharedNote();
}
function updateSharedNote() {
  const note = $("#sharedNote");
  const keys = Object.keys((wf && wf.shared_params) || {});
  if (!keys.length) { note.hidden = true; return; }
  note.hidden = false;
  note.innerHTML = `${ICON.link} This DAG also gets <b>${keys.length}</b> shared parameter(s): ` +
    keys.map(esc).join(", ");
}
function currentData() { return editor.getNodeFromId(cfToDf[selected]).data; }
function commitData(patch) {
  const df = cfToDf[selected];
  const data = { ...editor.getNodeFromId(df).data, ...patch };
  editor.updateNodeDataFromId(df, data);
  refreshNodeCard(selected); updateJson(); setDirty(true);
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
    <td><span class="del" title="remove">${ICON.close}</span></td>`;
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

// ---------- shared (workflow-wide) parameters ----------
function renderShared() {
  const tb = $("#sharedTable tbody"); tb.innerHTML = "";
  const rows = Object.entries((wf && wf.shared_params) || {});
  if (!rows.length) rows.push(["", ""]);
  rows.forEach(([k, v]) => addSharedRow(k, v));
}
function addSharedRow(k = "", v = "") {
  const tb = $("#sharedTable tbody");
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="k" placeholder="key" /></td>
    <td><input class="v" placeholder="value applied to all DAGs" /></td>
    <td><span class="del" title="remove">${ICON.close}</span></td>`;
  $(".k", tr).value = k; $(".v", tr).value = v;
  $(".k", tr).oninput = $(".v", tr).oninput = syncShared;
  $(".del", tr).onclick = () => { tr.remove(); syncShared(); };
  tb.appendChild(tr);
}
function syncShared() {
  if (!wf) return;
  const params = {};
  $$("#sharedTable tbody tr").forEach(tr => {
    const k = $(".k", tr).value.trim();
    if (k) params[k] = $(".v", tr).value.trim();
  });
  wf.shared_params = params;
  refreshAllCards();   // update the "+N shared" badge on every node
  updateSharedNote(); setDirty(true);
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
    : "not configured - see Settings";
  chip.title = chip.textContent;  // full text on hover (chip is ellipsized)
}
function renderAuth() {
  const chip = $("#authChip"), a = boot.auth || {};
  if (a.authenticated) { chip.className = "chip chip-ok"; chip.textContent = "gcloud: " + a.account; }
  else { chip.className = "chip chip-bad"; chip.textContent = "gcloud: not signed in"; }
  chip.title = chip.textContent;
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
    if (st.final_status === "success") toast("Workflow completed successfully");
    else if (st.final_status === "failed") toast("Workflow FAILED - see console");
    else toast("Workflow " + st.final_status);
  }
}
function startPolling() {
  $("#runStrip").hidden = false; $("#runBtn").disabled = true; $("#stopBtn").disabled = false;
  const tick = async () => {
    try { applyRunState(await api("/api/run-state")); } catch (e) {}
  };
  tick(); pollTimer = setInterval(tick, 1200);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer); pollTimer = null;
  $("#runBtn").disabled = false; $("#stopBtn").disabled = true;
}

// ---------- actions ----------
async function saveWorkflow(silent) {
  collectWorkflow();
  const res = await api("/api/workflow/save", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(wf) });
  renderWorkflows(res.workflows, wf.id);
  setDirty(false);
  if (!silent) toast("Saved.");
}
async function validateWorkflow() {
  collectWorkflow();
  const res = await api("/api/validate", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(wf) });
  if (res.ok && !res.issues.length) { toast("Workflow is valid"); }
  else {
    toast(res.issues[0] ? res.issues[0].message : "");
    alert(res.issues.map(i => `[${i.level.toUpperCase()}] ${i.message}`).join("\n"));
  }
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

// ---------- views (pages) ----------
function showView(name) {
  $$("#nav .nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach(v => (v.hidden = v.id !== "view-" + name));
  if (name === "settings") openSettings();
  if (name === "history") openHistory();
}

// ---------- settings page ----------
function openSettings() {
  const s = boot.settings, body = $("#settingsBody");
  let html = `<div class="card" style="padding:16px;margin-bottom:16px">
    <div class="panel-head">Environment profiles</div>
    <p class="muted" style="margin-top:0">Fill each environment once; pick the active
      one from the top-right selector. Values are stored locally.</p>
    <div class="grid4"><div class="h"></div><div class="h">Composer environment</div>
    <div class="h">Location</div><div class="h">GCP project</div>`;
  boot.profiles.forEach(p => {
    html += `<div class="h">${p}</div>
      <input data-k="profile_${p}_environment" value="${esc(s["profile_" + p + "_environment"] || "")}" />
      <input data-k="profile_${p}_location" value="${esc(s["profile_" + p + "_location"] || "")}" placeholder="europe-west2" />
      <input data-k="profile_${p}_project" value="${esc(s["profile_" + p + "_project"] || "")}" />`;
  });
  html += `</div></div>
    <div class="card" style="padding:16px">
    <div class="panel-head">Execution options</div>
    <div class="settings-form">
      <label>Status poll interval (s)</label><input data-k="poll_interval_seconds" value="${s.poll_interval_seconds}" />
      <label>Trigger command timeout (s)</label><input data-k="trigger_timeout_seconds" value="${s.trigger_timeout_seconds}" />
      <label>Max parallel DAGs</label><input data-k="max_parallel_dags" value="${s.max_parallel_dags}" />
      <label>CLI retries (transient errors)</label><input data-k="cli_retry_count" value="${s.cli_retry_count}" />
    </div></div>`;
  body.innerHTML = html;
}
async function saveSettings() {
  const payload = {};
  $$("#settingsBody [data-k]").forEach(i => (payload[i.dataset.k] = i.value.trim()));
  const res = await api("/api/settings/save", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  Object.assign(boot.settings, payload); boot.target = res.target;
  renderTargetChip(res.target, boot.active_profile);
  toast("Settings saved.");
}

// ---------- history page ----------
function statusPill(s) { return `<span class="pill ${s}">${(s || "").toUpperCase()}</span>`; }
let _histExecs = [];
async function openHistory() {
  const search = $("#histSearch").value.trim();
  const status = $("#histStatus").value;
  const res = await api("/api/history?search=" + encodeURIComponent(search) +
    "&status=" + encodeURIComponent(status));
  _histExecs = res.executions;
  const rows = res.executions.map(h =>
    `<tr class="clickable" data-id="${h.id}">
      <td><b>${esc(h.workflow_name)}</b></td><td>${statusPill(h.status)}</td>
      <td>${esc(h.started_at)}</td></tr>`).join("");
  $("#historyBody").innerHTML = res.executions.length
    ? `<table class="data"><thead><tr><th>Workflow</th><th>Status</th><th>Started</th></tr></thead>
       <tbody>${rows}</tbody></table>`
    : `<p class="muted" style="padding:12px">No executions match.</p>`;
  $$("#historyBody tbody tr").forEach(tr => tr.onclick = () => {
    $$("#historyBody tbody tr").forEach(r => r.classList.remove("sel"));
    tr.classList.add("sel");
    showHistoryDetail(tr.dataset.id);
  });
}
function confFromCommand(cmd) {
  const i = (cmd || "").indexOf("--conf");
  if (i < 0) return "";
  const rest = cmd.slice(i + 6).trim();
  // the conf is the remainder (our commands put --conf last)
  return rest.replace(/^["']|["']$/g, "");
}
async function showHistoryDetail(id) {
  const row = _histExecs.find(e => e.id === id);
  const res = await api("/api/execution?id=" + encodeURIComponent(id));
  const rowsHtml = res.nodes.map((n, idx) => {
    const conf = confFromCommand(n.command);
    return `<div class="hist-node">
      <div class="hist-node-head">
        <b>${esc(n.dag_id)}</b> ${statusPill(n.status)}
        <span class="muted">${(n.duration_seconds || 0).toFixed(0)}s · ${n.retry_count} retr.</span>
      </div>
      ${n.airflow_run_id ? `<div class="muted">run-id: ${esc(n.airflow_run_id)}</div>` : ""}
      ${conf ? `<div class="muted" style="margin-top:4px">Parameters passed:</div>
                <div class="codeblock">${esc(conf)}</div>` : ""}
      ${n.command ? `<details><summary class="muted">Full command</summary>
                <div class="codeblock">${esc(n.command)}</div></details>` : ""}
      ${n.error ? `<div class="codeblock" style="color:#fca5a5">${esc(n.error)}</div>` : ""}
    </div>`;
  }).join("");
  let html = `<div class="hist-detail-head">
      <h3>${esc(row ? row.workflow_name : "")} ${statusPill(row ? row.status : "")}</h3>
      <div class="muted">${esc(row ? row.started_at : "")} → ${esc(row ? row.finished_at : "")}</div>
      ${row && row.error ? `<div class="codeblock" style="color:#fca5a5">${esc(row.error)}</div>` : ""}
    </div>${rowsHtml}`;
  if (row && row.status === "failed")
    html += `<button class="btn btn-primary" id="rerunBtn">${ICON.repeat} Rerun failed DAGs only</button>`;
  $("#histDetail").innerHTML = html;
  const rb = $("#rerunBtn");
  if (rb) rb.onclick = async () => {
    const r = await api("/api/rerun-failed", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ execution_id: id }) });
    if (r.workflow) { wf = r.workflow; renderCanvas(); }
    showView("design"); startPolling();
  };
}

// ---------- console resize + font ----------
let consoleFont = 12;
function setConsoleFont(px) {
  consoleFont = Math.max(10, Math.min(20, px));
  $("#console").style.fontSize = consoleFont + "px";
}
function initConsoleResize() {
  const handle = $("#consoleResize"), con = $("#console");
  let dragging = false;
  handle.addEventListener("mousedown", e => {
    dragging = true; handle.classList.add("dragging");
    document.body.style.cursor = "row-resize"; e.preventDefault();
  });
  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    const h = window.innerHeight - e.clientY - 34;   // minus header
    con.style.height = Math.max(80, Math.min(window.innerHeight * 0.72, h)) + "px";
  });
  document.addEventListener("mouseup", () => {
    dragging = false; handle.classList.remove("dragging"); document.body.style.cursor = "";
  });
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
    if (selected === cf) showProps(null); setDirty(true); });
  editor.on("connectionRemoved", () => { if (!suppressCycle) setDirty(true); });
  editor.on("connectionCreated", info => {
    if (suppressCycle) return;
    collectWorkflow();
    if (hasCycle(wf.nodes, wf.edges)) {
      editor.removeSingleConnection(info.output_id, info.input_id, info.output_class, info.input_class);
      toast("That connection would create a cycle - not allowed.");
    } else {
      setDirty(true);
    }
  });
}

async function boot_load() {
  boot = await api("/api/bootstrap");
  renderEnv(); renderAuth();
  const id = renderWorkflows();
  if (id) await openWorkflow(id);
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
      toast("Browser opening for Google sign-in, then click here again to refresh.");
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
    wf = null; editor.clear(); $("#wfTitle").textContent = "-";
    const id = renderWorkflows(r.workflows); if (id) await openWorkflow(id);
  };
  $("#addNodeBtn").onclick = () => {
    if (!wf) return toast("Create or open a workflow first.");
    const id = crypto.randomUUID().replace(/-/g, "");
    const node = { id, dag_id: "", run_name: "", params: {}, x: 80 + Math.random() * 80, y: 80 + Math.random() * 80 };
    const df = addNodeToCanvas(node);
    editor.getNodeFromId(df); showProps(id); setDirty(true);
  };
  $("#autoBtn").onclick = () => { collectWorkflow(); autoLayout(); setDirty(true); };
  $("#validateBtn").onclick = validateWorkflow;
  $("#runBtn").onclick = runWorkflow;
  $("#stopBtn").onclick = async () => { await api("/api/cancel", { method: "POST" }); toast("Stopping..."); };
  $("#pDagId").oninput = e => commitData({ dag_id: e.target.value.trim() });
  $("#pRunName").oninput = e => commitData({ run_name: e.target.value.trim() });
  $("#addParamBtn").onclick = () => addParamRow();
  $("#addSharedBtn").onclick = () => addSharedRow();
  $("#consoleToggle").onclick = () => $("#consoleDock").classList.toggle("collapsed");
  $("#consoleTools").onclick = e => e.stopPropagation();  // don't toggle when using tools
  $("#fontPlus").onclick = () => setConsoleFont(consoleFont + 1);
  $("#fontMinus").onclick = () => setConsoleFont(consoleFont - 1);
  $("#consoleClear").onclick = () => { $("#console").innerHTML = ""; };
  $("#delNodeBtn").onclick = () => { if (selected != null) { editor.removeNodeId("node-" + cfToDf[selected]); showProps(null); } };
  $("#saveSettingsBtn").onclick = saveSettings;
  // primary nav tabs
  $$("#nav .nav-btn").forEach(b => b.onclick = () => showView(b.dataset.view));
  // history filters
  $("#histSearch").oninput = () => openHistory();
  $("#histStatus").onchange = () => openHistory();
  $("#histRefresh").onclick = () => openHistory();
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
}

function autoLayout() {
  // Horizontal left-to-right flow: one column per dependency depth (x grows
  // rightward, matching Drawflow's left→right ports); siblings stack down.
  if (!wf || !wf.nodes.length) return;
  collectWorkflow();  // read the latest edges from the canvas
  const succ = {}, indeg = {};
  wf.nodes.forEach(n => { succ[n.id] = []; indeg[n.id] = 0; });
  wf.edges.forEach(e => { if (succ[e.source]) { succ[e.source].push(e.target); indeg[e.target]++; } });

  const depth = {};
  const roots = wf.nodes.filter(n => !indeg[n.id]).map(n => n.id);
  roots.forEach(id => (depth[id] = 0));
  const ind = { ...indeg }, q = [...roots];
  while (q.length) {
    const u = q.shift();
    for (const v of succ[u]) {
      depth[v] = Math.max(depth[v] || 0, (depth[u] || 0) + 1);
      if (--ind[v] === 0) q.push(v);
    }
  }

  const COL = 300, ROW = 150, MX = 60, MY = 60;
  const rowInCol = {};
  // count column heights first so we can vertically centre each column
  const colCount = {};
  wf.nodes.forEach(n => { const d = depth[n.id] || 0; colCount[d] = (colCount[d] || 0) + 1; });
  const maxCol = Math.max(...Object.values(colCount), 1);
  wf.nodes.forEach(n => {
    const d = depth[n.id] || 0;
    rowInCol[d] = rowInCol[d] || 0;
    const offset = (maxCol - colCount[d]) / 2;            // centre shorter columns
    const x = MX + d * COL;
    const y = MY + (rowInCol[d] + offset) * ROW;
    rowInCol[d]++;
    const df = cfToDf[n.id];
    if (df == null) return;
    const el = $(`#node-${df}`);
    if (el) { el.style.left = x + "px"; el.style.top = y + "px"; }
    const data = editor.drawflow.drawflow.Home.data[df];
    if (data) { data.pos_x = x; data.pos_y = y; }
    n.x = x; n.y = y;
    editor.updateConnectionNodes(`node-${df}`);          // redraw its edges in place
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  initEditor(); wire(); initConsoleResize(); setConsoleFont(consoleFont);
  try { await boot_load(); } catch (e) { alert("Failed to load app: " + e.message); }
});
