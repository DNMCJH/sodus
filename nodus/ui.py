"""The web board: one HTML page served by the daemon.

No framework and no build step on purpose. A tool whose whole argument is
"acceptance needs executable evidence" should be inspectable by opening it.

The page ships with the API token embedded. That is safe *because* of where it
is served: the daemon binds 127.0.0.1, only the process that asked for the page
can read it, and any local process could read `~/.nodus/token` anyway.
Cross-origin reads are blocked by the same-origin policy, and cross-origin
writes cannot add the Authorization header.

The layout follows what two stronger models converged on when asked what the
first screen should be (opus and gpt-6.1-sol, run through Nodus itself):
status, evidence, dispatch — in that order, with "finished is not passed"
stated on the screen rather than in a doc.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nodus</title>
<style>
  :root { color-scheme: dark; --bg:#14161a; --panel:#1b1e24; --line:#2a2f38;
          --dim:#8b93a1; --text:#e6e9ef; --ok:#5ac48a; --bad:#e06c75; --warn:#d9a441; --accent:#6aa9ff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.55 ui-sans-serif, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display:flex; gap:18px; align-items:baseline; padding:13px 20px;
           border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:3; }
  header h1 { font-size:15px; margin:0; letter-spacing:.1em; }
  header .stat { color:var(--dim); font-variant-numeric:tabular-nums; }
  header .stat b { color:var(--text); font-weight:600; }
  header .rule { margin-left:auto; color:var(--warn); font-size:12.5px; }
  main { padding:16px 20px 120px; max-width:1180px; }
  section { margin-bottom:26px; }
  h2 { font-size:12px; letter-spacing:.14em; color:var(--dim); text-transform:uppercase;
       margin:0 0 8px; font-weight:600; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:12px; }
  tr:hover td { background:var(--panel); }
  td.nowrap, th.nowrap { white-space:nowrap; }
  .mono { font-family:ui-monospace, Consolas, monospace; font-size:12.5px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); } .dim { color:var(--dim); }
  .pill { display:inline-block; padding:1px 7px; border:1px solid var(--line); border-radius:999px; font-size:11.5px; }
  button { background:transparent; color:var(--accent); border:1px solid var(--line);
           border-radius:5px; padding:2px 9px; font:inherit; font-size:12px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.danger { color:var(--bad); } button.danger:hover { border-color:var(--bad); }
  .empty { color:var(--dim); padding:8px 10px; }
  .task { color:var(--dim); font-size:12.5px; }
  #evidence { background:var(--panel); border:1px solid var(--line); border-radius:7px;
              padding:12px 14px; margin-top:10px; display:none; }
  #evidence h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  #evidence pre { white-space:pre-wrap; word-break:break-all; max-height:320px; overflow:auto;
                  background:var(--bg); border:1px solid var(--line); border-radius:5px; padding:10px; font-size:12px; }
  form { display:grid; gap:9px; max-width:760px; }
  label { display:grid; gap:4px; font-size:12.5px; color:var(--dim); }
  input, select, textarea { background:var(--bg); color:var(--text); border:1px solid var(--line);
                            border-radius:5px; padding:7px 9px; font:inherit; }
  textarea { min-height:70px; resize:vertical; }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
  .check { display:flex; gap:8px; align-items:center; color:var(--text); font-size:13px; }
  #flash { position:fixed; bottom:16px; left:50%; transform:translateX(-50%);
           background:var(--panel); border:1px solid var(--line); border-radius:6px;
           padding:8px 14px; display:none; max-width:80vw; z-index:5; }
</style>
</head>
<body>
<header>
  <h1>NODUS</h1>
  <span class="stat" id="s-profile">—</span>
  <span class="stat" id="s-live">—</span>
  <span class="stat" id="s-cost">—</span>
  <span class="rule">结束 ≠ 通过：只有测试证据变绿才算验收</span>
</header>
<main>
  <section>
    <h2>需要你决定</h2>
    <div id="todo"></div>
  </section>
  <section>
    <h2>正在跑</h2>
    <div id="live"></div>
  </section>
  <section>
    <h2>最近执行 <span class="dim" style="text-transform:none;letter-spacing:0">（点"证据"看测试命令、退出码和改动文件）</span></h2>
    <div id="runs"></div>
    <div id="evidence"></div>
  </section>
  <section>
    <h2>提案（在分支上，主工作区没动）</h2>
    <div id="proposals"></div>
  </section>
  <section>
    <h2>派活</h2>
    <form id="dispatch">
      <label>目标（要做什么，一句话说清）
        <textarea id="f-goal" required placeholder="例：修好 calc.py 里的 add 函数，让 test_calc.py 通过"></textarea>
      </label>
      <label>验收标准（怎么算做完了；能写成命令最好）
        <input id="f-accept" placeholder="例：python -m pytest 全绿">
      </label>
      <div class="row">
        <label>工作目录
          <input id="f-cwd" />
        </label>
        <label>谁去干
          <select id="f-route"></select>
        </label>
      </div>
      <label class="check">
        <input type="checkbox" id="f-propose" checked>
        在独立分支上干（提案模式）——主工作区不会被改动，之后你再决定合不合
      </label>
      <label class="check">
        <input type="checkbox" id="f-verify" checked>
        结束时自动跑测试（改过文件才跑，检测不到测试命令就不跑）
      </label>
      <div><button type="submit">派活</button>
        <span class="task" id="f-hint"></span></div>
    </form>
  </section>
</main>
<div id="flash"></div>
<script>
const TOKEN = "__TOKEN__";
const H = { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" };
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
const safe = s => { try { return JSON.parse(s || "{}"); } catch { return {}; } };

async function api(path, method = "GET", body) {
  const r = await fetch(path, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 300) }; }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
function flash(msg, bad) {
  const el = $("flash");
  el.textContent = msg; el.style.display = "block";
  el.style.borderColor = bad ? "var(--bad)" : "var(--line)";
  clearTimeout(el._t); el._t = setTimeout(() => el.style.display = "none", 5000);
}
const ago = ts => {
  if (!ts) return "-";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return Math.round(d) + "s";
  if (d < 3600) return Math.round(d / 60) + "m";
  if (d < 86400) return Math.round(d / 3600) + "h";
  return Math.round(d / 86400) + "d";
};
const money = u => (u.cost == null ? "-" :
  (u.currency === "CNY" ? "¥" : (u.currency || "") + " ") + Number(u.cost).toFixed(4));
const VERIFY = { ok: ["通过", "ok"], red: ["测试红", "bad"], none: ["没验证", "warn"] };

let state = { runs: [], proposals: [], pipelines: [], routes: [] };

function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table>`;
}

function renderTodo() {
  const items = [];
  for (const p of state.proposals.filter(p => p.status === "open")) {
    items.push(`<tr><td><span class="pill">提案</span></td>
      <td class="mono">${esc(p.id)}</td>
      <td>${esc(p.stat || "")}<div class="task">${(p.files||[]).map(f=>esc(f.path)).join(", ")}</div></td>
      <td style="white-space:nowrap">
        <button onclick="merge('${p.id}')">合并</button>
        <button class="danger" onclick="reject('${p.id}')">丢弃</button></td></tr>`);
  }
  for (const r of state.runs.filter(r => r.verify === "red").slice(0, 5)) {
    items.push(`<tr><td><span class="pill bad">测试红</span></td><td class="mono">${esc(r.id)}</td>
      <td>跑完了但测试没通过，改动不该算完成</td>
      <td><button onclick="showEvidence('${r.id}')">看证据</button></td></tr>`);
  }
  const failed = state.runs.filter(r => r.status === "failed");
  for (const r of failed.slice(0, 3)) {
    items.push(`<tr><td><span class="pill bad">失败</span></td><td class="mono">${esc(r.id)}</td>
      <td>${esc((r.error || "").slice(0, 150))}</td>
      <td><button onclick="showEvidence('${r.id}')">看证据</button></td></tr>`);
  }
  if (failed.length > 3) {
    items.push(`<tr><td><span class="pill bad">失败</span></td>
      <td class="mono">还有 ${failed.length - 3} 个</td>
      <td>更早的失败记录，列表里能看到</td><td></td></tr>`);
  }
  const unverified = state.runs.filter(r => r.status === "completed" && r.verify === "none");
  if (unverified.length) {
    items.push(`<tr><td><span class="pill warn">未验证</span></td>
      <td class="mono">${unverified.length} 个</td>
      <td>跑完了但没有测试证据 —— 状态只代表进程退出码</td><td></td></tr>`);
  }
  $("todo").innerHTML = table(["", "对象", "内容", ""], items, "没有需要你决定的事。");
}

function renderLive() {
  const live = state.runs.filter(r => r.status === "running" || r.status === "queued");
  const pipes = state.pipelines.filter(p => p.status === "running");
  const rows = live.map(r => `<tr><td class="mono">${esc(r.id)}</td>
      <td class="nowrap">${esc(r.route || r.agent)}<span class="dim"> → ${esc(r.provider || "-")}</span></td>
      <td class="dim">${esc((r.cwd || "").slice(-44))}</td>
      <td class="nowrap">${ago(r.started_at)}</td>
      <td><button class="danger" onclick="stopRun('${r.id}')">停止</button></td></tr>`);
  for (const p of pipes) rows.push(`<tr><td class="mono">${esc(p.id)}</td>
      <td>${esc(p.kind)}<span class="dim"> pipeline</span></td>
      <td class="dim">${esc(p.label || "")}</td>
      <td class="nowrap">${ago(p.started_at || p.created_at)}</td>
      <td><button class="danger" onclick="stopPipe('${p.id}')">停止</button></td></tr>`);
  $("live").innerHTML = table(["对象", "谁在干", "目录", "开始", ""], rows, "现在没有在跑的。");
}

function renderRuns() {
  const rows = state.runs.slice(0, 12).map(r => {
    const u = r.usage || {};
    const [label, cls] = VERIFY[r.verify] || ["-", "dim"];
    const status = r.status === "completed" ? '<span class="ok">完成</span>'
      : r.status === "failed" ? '<span class="bad">失败</span>'
      : r.status === "running" ? '<span class="pill">跑着</span>' : esc(r.status);
    return `<tr>
      <td class="mono dim">${esc((r.id || "").slice(4, 14))}</td>
      <td class="nowrap">${status}</td>
      <td class="nowrap"><span class="${cls}">${label}</span></td>
      <td class="nowrap">${esc(r.route || r.agent)}<span class="dim"> → ${esc(r.provider || "-")}</span></td>
      <td class="nowrap">${money(u)}</td>
      <td class="task">${esc((r.title || r.note || r.task_id || "").slice(0, 50))}</td>
      <td class="dim nowrap">${ago(r.ended_at || r.started_at)}</td>
      <td class="nowrap"><button onclick="showEvidence('${r.id}')">证据</button></td>
    </tr>`;
  });
  $("runs").innerHTML = table(["run", "状态", "验证", "路由", "成本", "任务", "何时", ""], rows, "还没有执行记录。");
}

function renderProposals() {
  const rows = state.proposals.map(p => `<tr>
      <td class="mono">${esc(p.id)}</td>
      <td><span class="pill">${esc(p.status)}</span></td>
      <td>${esc(p.stat || "")}</td>
      <td class="task mono">${esc((p.branch || "").slice(0, 36))}</td></tr>`);
  $("proposals").innerHTML = table(["提案", "状态", "改动", "分支"], rows, "没有提案。");
}

function renderHeader() {
  $("s-profile").innerHTML = "profile <b>" + esc(state.profile || "default") + "</b>";
  $("s-live").innerHTML = "<b>" + state.runs.filter(r => r.status === "running").length + "</b> 在跑";
  const byCurrency = {};
  for (const r of state.runs) {
    const c = r.usage.cost, cur = r.usage.currency || "USD";
    if (typeof c === "number") byCurrency[cur] = (byCurrency[cur] || 0) + c;
  }
  const parts = Object.entries(byCurrency).map(([cur, v]) =>
    (cur === "CNY" ? "¥" : cur + " ") + v.toFixed(2));
  $("s-cost").innerHTML = "最近 <b>" + state.runs.length + "</b> 个 run <b>" +
    (parts.join(" + ") || "-") + "</b>";
}

async function showEvidence(rid) {
  const box = $("evidence");
  box.style.display = "block";
  box.innerHTML = `<h3>${esc(rid)}</h3><div class="dim">读取中…</div>`;
  try {
    const [events, artifacts] = await Promise.all([
      api("/runs/" + rid + "/events"), api("/runs/" + rid + "/artifacts")]);
    const verifies = events.filter(e => e.type.startsWith("verify."));
    const files = artifacts.filter(a => a.kind === "file").map(a => a.rel_path || a.path);
    let html = `<h3>${esc(rid)}</h3>`;
    if (!verifies.length) {
      html += `<div class="warn">这个 run 没有验证记录：状态只等于进程退出码，不代表做对了。</div>`;
    } else {
      for (const v of verifies) {
        const p = v.payload || {};
        html += `<div><span class="${p.ok ? "ok" : "bad"}">${p.ok ? "PASS" : "FAIL"}</span>
          <span class="mono">${esc((p.cmd || []).join(" "))}</span>
          <span class="dim">exit=${esc(p.exit)} ${esc(p.duration_ms)}ms</span></div>`;
        if (p.tail) html += `<pre>${esc(p.tail)}</pre>`;
      }
    }
    html += `<div class="task">改动文件：${files.length ? files.map(esc).join(" · ") : "（无）"}</div>
      <div class="task">原始记录：${esc("/runs/" + rid + "/handoff")}</div>`;
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<h3>${esc(rid)}</h3><div class="bad">读取失败：${esc(e.message)}</div>`;
  }
}

async function stopRun(id) {
  if (!confirm("停止 " + id + "？已经改过的文件不会回滚。")) return;
  try { await api("/runs/" + id + "/stop", "POST"); flash("已请求停止 " + id); }
  catch (e) { flash("停止失败：" + e.message, true); }
  refresh();
}
async function stopPipe(id) {
  if (!confirm("停止流水线 " + id + "？")) return;
  try { await api("/pipelines/" + id + "/stop", "POST"); flash("已请求停止 " + id); }
  catch (e) { flash("停止失败：" + e.message, true); }
  refresh();
}
async function merge(id) {
  const p = state.proposals.find(x => x.id === id) || {};
  const run = state.runs.find(r => r.id === p.run_id) || {};
  const risky = run.verify && run.verify !== "ok";
  let text = "把 " + id + " 合进主工作区？\\n\\n" + (p.stat || "");
  if (risky) text += "\\n\\n注意：这个提案的 run " +
    (run.verify === "red" ? "测试是红的" : "没有验证记录") + "。";
  if (!confirm(text)) return;
  try { const r = await api("/proposals/" + id + "/merge", "POST");
    flash(r.ok ? "已合入 " + (r.commit || "") : "合并失败：" + r.reason, !r.ok); }
  catch (e) { flash("合并失败：" + e.message, true); }
  refresh();
}
async function reject(id) {
  if (!confirm("丢弃 " + id + "？分支和工作区都会删掉，改动不会保留。")) return;
  try { await api("/proposals/" + id + "/reject", "POST"); flash("已丢弃 " + id); }
  catch (e) { flash("丢弃失败：" + e.message, true); }
  refresh();
}

async function dispatch(ev) {
  ev.preventDefault();
  const goal = $("f-goal").value.trim();
  const accept = $("f-accept").value.trim();
  const cwd = $("f-cwd").value.trim();
  const route = $("f-route").value;
  const proposeMode = $("f-propose").checked;
  const verify = $("f-verify").checked;
  if (!goal) return;
  const summary = "派活：\\n\\n" + goal + "\\n\\n谁干：" + route +
    "\\n目录：" + (cwd || "（默认）") +
    "\\n模式：" + (proposeMode ? "提案（独立分支，主工作区不动）" : "直接写主工作区") +
    "\\n结束自动跑测试：" + (verify ? "是" : "否");
  if (!confirm(summary + "\\n\\n会消耗额度并修改文件，确认？")) return;
  try {
    const task = await api("/tasks", "POST", { goal, acceptance: accept, title: goal.slice(0, 60) });
    const run = await api("/runs", "POST", {
      task_id: task.id, route, cwd: cwd || null, note: goal,
      propose: proposeMode, verify,
    });
    flash("已派活：" + run.id + "（" + route + "）");
    $("f-goal").value = ""; $("f-accept").value = "";
    refresh();
  } catch (e) { flash("派活失败：" + e.message, true); }
}

async function loadRoutes() {
  try {
    const routes = await api("/routes");
    state.routes = routes.filter(r => r.ready);
    $("f-route").innerHTML = state.routes
      .map(r => `<option value="${esc(r.name)}">${esc(r.name)} — ${esc((r.providers||[]).join(" → "))}</option>`)
      .join("");
    $("f-hint").textContent = state.routes.length ? "" : "没有可用的路由，先去配置端点。";
  } catch (e) { $("f-hint").textContent = "读不到路由：" + e.message; }
}

async function refresh() {
  try {
    const [health, runs, proposals, pipelines] = await Promise.all([
      api("/health"), api("/runs"), api("/proposals"), api("/pipelines")]);
    state.profile = health.profile;
    state.runs = runs.map(r => ({ ...r, usage: safe(r.usage_json) }));
    state.proposals = proposals;
    state.pipelines = pipelines;
    renderHeader(); renderTodo(); renderLive(); renderRuns(); renderProposals();
  } catch (e) {
    $("todo").innerHTML = '<div class="empty bad">连不上 daemon：' + esc(e.message) + "</div>";
  }
}

$("dispatch").addEventListener("submit", dispatch);
loadRoutes(); refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
