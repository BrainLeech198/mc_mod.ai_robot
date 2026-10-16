// DeepSink 前端共用工具
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg) {
  let t = $(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.remove("show"), 1800);
}
function copy(t) {
  (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject())
    .then(() => toast("已复制")).catch(() => { window.prompt("手动复制：", t); });
}
function money(n, cur = "CNY", digits = 6) {
  const sym = cur === "CNY" ? "¥" : "";
  return sym + Number(n || 0).toFixed(digits);
}
function num(n) { return Number(n || 0).toLocaleString("en-US"); }
function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = x => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
async function apiGet(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || ("HTTP " + r.status));
  return j;
}
function badge(text, cls) { return `<span class="badge ${cls || ""}">${esc(text)}</span>`; }

/* ---------- 账户 / 会话 ---------- */
async function apiMe() {
  try {
    const r = await fetch("/api/me");
    if (!r.ok) return null;
    return (await r.json()).user || null;
  } catch (e) { return null; }
}
async function requireLogin() {
  const u = await apiMe();
  if (!u) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    return null;
  }
  return u;
}
async function doLogout() {
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
  location.href = "/";
}
/** 渲染右上角登录态。返回当前用户（或 null）。 */
async function navAuth(elId = "navauth") {
  const el = document.getElementById(elId);
  const u = await apiMe();
  if (el) {
    if (u) {
      el.innerHTML = `<span class="pill big"><b>${esc(u.name)}</b> · 余额 <b id="navbal">—</b></span>
        <button class="ghost" onclick="doLogout()">退出</button>`;
      apiGet("/api/account").then(d => {
        const b = document.getElementById("navbal");
        if (b) b.textContent = money(d.account.balance, d.account.currency, 2);
      }).catch(() => {});
    } else {
      el.innerHTML = `<a class="btn" href="/login">登录 / 注册</a>`;
    }
  }
  return u;
}

/* ---------- 高科技氛围：粒子星座背景 + 卡片光标聚光 ---------- */
(function initDsFx() {
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // 卡片光标聚光（把鼠标位置写进 CSS 变量 --mx/--my）
  document.addEventListener("pointermove", e => {
    const el = e.target && e.target.closest ? e.target.closest(".card") : null;
    if (!el) return;
    const r = el.getBoundingClientRect();
    el.style.setProperty("--mx", (e.clientX - r.left) + "px");
    el.style.setProperty("--my", (e.clientY - r.top) + "px");
  }, { passive: true });

  if (reduce) return;

  // 粒子星座：轻量 canvas，节点漂移 + 近邻连线 + 鼠标吸附
  const cv = document.createElement("canvas");
  cv.id = "deepsink-bg";
  document.body.appendChild(cv);
  const ctx = cv.getContext("2d");
  let w = 0, h = 0, dpr = 1, pts = [];
  const mouse = { x: -1e4, y: -1e4 };

  function build() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = cv.width = Math.floor(innerWidth * dpr);
    h = cv.height = Math.floor(innerHeight * dpr);
    cv.style.width = innerWidth + "px";
    cv.style.height = innerHeight + "px";
    const n = Math.max(26, Math.min(96, Math.floor(innerWidth * innerHeight / 26000)));
    pts = Array.from({ length: n }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      vx: (Math.random() - .5) * .22 * dpr, vy: (Math.random() - .5) * .22 * dpr,
      r: (Math.random() * 1.3 + .6) * dpr
    }));
  }

  function frame() {
    ctx.clearRect(0, 0, w, h);
    for (const p of pts) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > w) p.vx *= -1;
      if (p.y < 0 || p.y > h) p.vy *= -1;
      const dx = p.x - mouse.x, dy = p.y - mouse.y;
      const md = Math.hypot(dx, dy);
      if (md < 160 * dpr && md > 0.01) { p.x += dx / md * .7 * dpr; p.y += dy / md * .7 * dpr; }
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, 6.283);
      ctx.fillStyle = "rgba(40,226,255,.75)";
      ctx.fill();
    }
    const maxD = 128 * dpr;
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i], b = pts[j];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d < maxD) {
          const o = (1 - d / maxD) * .3;
          ctx.strokeStyle = `rgba(91,123,255,${o})`;
          ctx.lineWidth = dpr * .7;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
    }
    requestAnimationFrame(frame);
  }

  addEventListener("resize", build, { passive: true });
  addEventListener("pointermove", e => { mouse.x = e.clientX * dpr; mouse.y = e.clientY * dpr; }, { passive: true });
  addEventListener("pointerleave", () => { mouse.x = mouse.y = -1e4; });
  build();
  requestAnimationFrame(frame);
})();
