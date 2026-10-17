/* 封装 Agent 控制台前端。纯原生 JS，2 秒轮询 /api/state。
   身份全部来自 version/<版本>/identity.md + 同目录的 config.json / mods.md / 方块目录.md 等。 */
const $ = (id) => document.getElementById(id);
let state = null, preset = null;

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  return r.json();
}

function light(id, on, text) {
  const el = $(id);
  el.className = "light " + (on === true ? "on" : on === "warn" ? "warn" : "off");
  el.textContent = text;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderClients() {
  $("clientSel").innerHTML = state.client.list
    .map((c) => `<option value="${c.id}" ${c.id === state.client.active ? "selected" : ""}>${c.id}</option>`)
    .join("");
}

function renderPreset(p) {
  $("p-ver").textContent = p.id;
  $("p-name").textContent = state.client.name;
  $("bePreset").innerHTML = Object.entries(state._backends)
    .map(([k, v]) => `<option value="${k}" ${k === (p.backend["模板"] || p.backend.preset) ? "selected" : ""}>${v.label}</option>`)
    .join("");
  $("beEndpoint").value = p.backend.endpoint || "";
  $("beModel").value = p.backend.model || "";
  $("beKeyEnv").value = p.backend.key_env || "MCROBOT_LLM_KEY";
  $("keyHint").textContent = state._backends[p.backend["模板"] || p.backend.preset]?.key_hint || "";
  $("p-meta").value = Object.entries(p.meta || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  $("p-persona").value = p.persona || "";
  $("p-facts").value = (p.world_facts || []).join("\n");

  const a = p._auto || {};
  const mods = a.mods || {};
  const cats = (a.catalog || []).map((c) => `  ${c.label}/${c.modid}：${c.count} 块`).join("\n");
  $("auto").textContent =
    `mods.md：客户端 ${mods.count || 0} 个 mod\n` +
    `方块目录.md：\n${cats || "  （无）"}\n\n` +
    `边界与纪律：\n  ${(a.boundaries || []).join("\n  ")}\n\n` +
    `占位符：${JSON.stringify(a.context || {})}`;
}

function renderFeed() {
  const q = state.queue;
  $("queueHint").textContent = `inbox ${q.inbox_lines} 行 / 未读 ${q.unread}`;
  const ev = [];
  q.inbox.forEach((r) => ev.push({ t: r.ts || "", cls: "in", who: r.from || "?",
                                   text: r.text || "", tag: r.summon ? "" : "（旁听）" }));
  q.outbox.forEach((r) => ev.push({ t: r.ts || "", cls: r.cmd ? "cmd" : "out",
                                    who: "bot", text: r.say || r.cmd || "" }));
  ev.sort((a, b) => String(a.t).localeCompare(String(b.t)));
  $("feed").innerHTML = ev.slice(-60).map((e) =>
    `<div class="msg ${e.cls}"><span class="ts">${(e.t || "").slice(11, 19)}</span>` +
    `<span class="who">${e.who}</span>${e.tag || ""} ${escapeHtml(e.text)}</div>`).join("")
    || '<div class="hint">还没有消息</div>';
  $("feed").scrollTop = $("feed").scrollHeight;
}

function renderStatus() {
  const s = state.status, b = state.backend;
  light("l-body", s.body, "身体");
  light("l-game", s.game.in_game ? true : "off",
        s.game.in_game ? `游戏 ${(s.game.player || {}).dim || ""}` : "游戏离线");
  light("l-brain", s.console_brain === "running" ? true : s.bridge_watch ? "warn" : "off",
        s.console_brain === "running" ? `L1 大脑 ${s.pid}` : s.bridge_watch ? "bridge 在跑" : "大脑未启动");
  light("l-api", b.probe.startsWith("ok") ? true : b.probe.startsWith("不通") ? "off" : "warn", "后端");
  $("beProbe").textContent = `${b.endpoint || "(未填)"} → ${b.probe}`;
  $("keyState").textContent = b.key_set ? `已填：${b.key_masked}` : "未填（本机服务可留空）";
  $("btnBrain").textContent = s.console_brain === "running" ? "停止 L1 大脑" : "启动 L1 大脑";
}

function renderChain() {
  const c = state.chain || {};
  const st = c.running ? "跑着呢…"
    : c.ok === true ? "全绿 ✓"
    : c.ok === false ? "卡住：" + (c.error || "?")
    : "还没跑过";
  $("chainHint").textContent = st;
  $("btnChain").disabled = !!c.running;
  $("btnChain").textContent = c.running ? "全链路进行中…" : "一键启动全链路";
  const mark = { ok: "✓", skipped: "·", failed: "✗", running: "…", pending: "○" };
  $("chainSteps").innerHTML = (c.steps || []).map((s) =>
    `<div class="cstep ${s.state}"><b>${mark[s.state] || "?"}</b>` +
    `<span class="lbl">${s.label}</span><span class="det">${escapeHtml(s.detail || "")}` +
    `${s.secs != null ? ` (${s.secs}s)` : ""}</span></div>`).join("")
    || '<div class="hint">点上面那个按钮，一条龙把链路拉起来</div>';
}

async function refresh() {
  try {
    state = await api("/api/state");
    preset = await api("/api/preset");
    state._backends = preset.backends;
    renderClients();
    renderPreset(preset.preset);
    renderStatus();
    renderChain();
    renderFeed();
  } catch (e) {
    $("queueHint").textContent = "控制台未连通";
  }
}

/* ---- 交互 ---- */
$("btnChain").onclick = async () => {
  const r = await api("/api/chain/start", {});
  if (!r.ok) alert(r.msg);
  refresh();
};
$("btnChainStop").onclick = async () => {
  const r = await api("/api/chain/stop", {});
  if (!r.ok) alert(r.msg);
  refresh();
};
$("btnUse").onclick = async () => {
  await api("/api/client/use", { id: $("clientSel").value });
  refresh();
};
$("btnKey").onclick = async () => {
  const r = await api("/api/key", { value: $("keyInput").value, env: $("beKeyEnv").value });
  $("keyState").textContent = r.set ? `已填：${r.masked}` : "已清空";
  $("keyInput").value = "";
};
$("btnSave").onclick = async () => {
  const backend = ["模板: " + $("bePreset").value,
                   "endpoint: " + $("beEndpoint").value,
                   "model: " + $("beModel").value,
                   "key_env: " + $("beKeyEnv").value,
                   "timeout_seconds: " + (preset.preset.backend.timeout_seconds || 90)].join("\n");
  await api("/api/preset", {
    meta: $("p-meta").value, persona: $("p-persona").value,
    world_facts: $("p-facts").value, backend,
  });
  $("saveHint").textContent = "已保存（按分节原地改，说明表格不受影响）";
  setTimeout(() => ($("saveHint").textContent = ""), 2500);
};
$("btnSay").onclick = async () => {
  if (!$("sayInput").value) return;
  await api("/api/say", { text: $("sayInput").value });
  $("sayInput").value = ""; refresh();
};
$("btnCmd").onclick = async () => {
  if (!$("cmdInput").value) return;
  await api("/api/cmd", { command: $("cmdInput").value });
  $("cmdInput").value = ""; refresh();
};
$("btnAsk").onclick = async () => {
  const t = $("askInput").value;
  if (!t) return;
  $("askOut").textContent = "问 AI 中…";
  const r = await api("/api/ask", { text: t, send: $("askSend").checked });
  $("askOut").textContent = r.ok ? r.reply : "失败：" + (r.err || "?");
};
$("btnBrain").onclick = async () => {
  const running = state.status.console_brain === "running";
  const r = await api("/api/brain/" + (running ? "stop" : "start"));
  alert(r.ok ? r.msg : "失败：" + r.msg);
  refresh();
};
$("btnPreview").onclick = async () => {
  const r = await api("/api/preview", { text: $("pvText").value });
  $("preview").textContent = (r.messages || [])
    .map((m) => `── ${m.role} ──\n${m.content}`).join("\n\n");
};
$("btnBlk").onclick = async () => {
  const q = $("blkInput").value.trim();
  if (!q) return;
  const r = await api("/api/blocks?q=" + encodeURIComponent(q) + "&limit=15");
  const hits = r.hits || [];
  $("blkOut").textContent = hits.length
    ? hits.map((h) => `${h.id}\t${h.name}`).join("\n")
    : "没找到。方块目录里显示名是英文，试试 desk / chair / lamp / rubbish_bin";
};
$("btnLog").onclick = async () => {
  const r = await api("/api/logs?name=" + $("logSel").value);
  $("logs").textContent = (r.lines || []).join("\n");
};

refresh();
setInterval(refresh, 2000);
setInterval(() => $("btnLog").onclick(), 5000);
