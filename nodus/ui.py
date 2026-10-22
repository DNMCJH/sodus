"""The web board: one HTML page served by the daemon.

No framework and no build step on purpose. A tool whose whole argument is
"acceptance needs executable evidence" should be inspectable by opening it.

The page ships with the API token embedded. That is safe *because* of where it
is served: the daemon binds 127.0.0.1, only the process that asked for the page
can read it, and any local process could read `~/.nodus/token` anyway.
Cross-origin reads are blocked by the same-origin policy, and cross-origin
writes cannot add the Authorization header.

The first screen answers the three questions that decide whether the operator
has to get up: what is running, what is waiting on a human, and what today has
cost so far. Everything below that (recent runs, proposals, dispatch) is one
click away, because a dashboard that needs scrolling before it says anything
is a log viewer.

The first screen is also written for someone who has never seen Nodus: each
row says what happened in one sentence, what it touched, and which button to
press. Run ids and the agents' raw English errors live behind "详情", because
they answer a debugging question, not the operator's "do I have to get up".
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
  header { display:flex; gap:20px; align-items:baseline; padding:13px 20px;
           border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:3; }
  header h1 { font-size:15px; margin:0; letter-spacing:.1em; }
  header .stat { color:var(--dim); font-variant-numeric:tabular-nums; }
  header .stat b { color:var(--text); font-weight:600; }
  header .rule { margin-left:auto; color:var(--warn); font-size:12.5px; }
  main { padding:14px 20px 40px; max-width:1180px; }
  section { margin-bottom:20px; }
  h2 { font-size:12px; letter-spacing:.14em; color:var(--dim); text-transform:uppercase;
       margin:0 0 6px; font-weight:600; }
  h2 .hint { letter-spacing:0; text-transform:none; color:var(--text); font-weight:500;
             margin-left:8px; font-variant-numeric:tabular-nums; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--dim); font-weight:500; font-size:12px; }
  td.nowrap, th.nowrap { white-space:nowrap; }
  tr:hover td { background:var(--panel); }
  .mono { font-family:ui-monospace, Consolas, monospace; font-size:12.5px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); } .dim { color:var(--dim); }
  .pill { display:inline-block; padding:1px 7px; border:1px solid var(--line); border-radius:999px; font-size:11.5px; }
  .pill.bad { border-color:var(--bad); }
  .pill.warn { border-color:var(--warn); }
  button { background:transparent; color:var(--accent); border:1px solid var(--line);
           border-radius:5px; padding:3px 10px; font:inherit; font-size:12.5px; cursor:pointer; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#0b0e12; font-weight:600; }
  button.primary:hover { background:#8bbcff; border-color:#8bbcff; }
  button.danger { color:var(--bad); } button.danger:hover { border-color:var(--bad); }
  .empty { color:var(--dim); padding:8px 12px; }
  .task { color:var(--dim); font-size:12.5px; }
  .scroll { max-height:320px; overflow:auto; }
  .total { padding:2px 12px 9px; font-size:13.5px; color:var(--dim); }
  .total b { color:var(--text); font-size:19px; font-weight:600; margin:0 5px;
             font-variant-numeric:tabular-nums; }
  .total.zero { font-size:13px; }
  .money-row { display:flex; justify-content:space-between; align-items:baseline;
               max-width:430px; padding:5px 12px; border-bottom:1px solid var(--line);
               font-variant-numeric:tabular-nums; }
  .money-row .amt { color:var(--text); }
  .foot { color:var(--dim); font-size:11.5px; padding:7px 12px 0; }
  .live-row, .ask { display:grid; grid-template-columns:1fr auto; gap:4px 16px;
                    align-items:start; padding:9px 12px; border-bottom:1px solid var(--line); }
  .live-row:hover, .ask:hover { background:var(--panel); }
  .live-what, .ask-what { font-size:14.5px; color:var(--text); }
  .live-what b { font-weight:600; }
  .live-sub, .ask-about { color:var(--dim); font-size:12.5px; margin-top:1px; }
  .ask-about .when { white-space:nowrap; }
  .live-row > .act, .ask > .act { display:flex; gap:6px; align-items:center; padding-top:1px; }
  .ask details.meta { border:0; margin:4px 0 0; padding:0; }
  .ask details.meta summary { font-size:11.5px; }
  .ask details.meta div { font-family:ui-monospace, Consolas, monospace; font-size:11.5px;
                          color:var(--dim); margin-top:2px; word-break:break-all; }
  .more-inline { color:var(--dim); font-size:12.5px; padding:8px 12px;
                 border-bottom:1px solid var(--line); }
  .more-inline button { margin-left:4px; }
  .unv-list { border-bottom:1px solid var(--line); background:#171a1f;
              padding:2px 12px 8px 26px; max-height:340px; overflow:auto; }
  .unv-row { display:flex; gap:8px; align-items:center; padding:4px 0; font-size:12.5px;
             border-top:1px solid rgba(42,47,56,.6); }
  .unv-row:first-child { border-top:0; }
  .unv-row .unv-title { color:var(--text); overflow:hidden; text-overflow:ellipsis;
                        white-space:nowrap; }
  .unv-row button { margin-left:auto; flex:none; }
  #evidence { background:var(--panel); border:1px solid var(--line); border-radius:7px;
              padding:12px 14px; margin-top:10px; display:none; }
  #evidence h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  #evidence pre { white-space:pre-wrap; word-break:break-all; max-height:320px; overflow:auto;
                  background:var(--bg); border:1px solid var(--line); border-radius:5px; padding:10px; font-size:12px; }
  details { border-top:1px solid var(--line); margin-top:6px; padding-top:10px; }
  details summary { color:var(--dim); cursor:pointer; font-size:12.5px; letter-spacing:.04em; }
  details summary:hover { color:var(--text); }
  details section { margin-top:18px; }
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
  <span class="stat" id="s-cost">—</span>
  <span class="stat" id="s-live">—</span>
  <span class="rule">结束 ≠ 通过：只有测试证据变绿才算验收</span>
</header>
<main>
  <section>
    <h2>正在跑 <span class="hint" id="live-n"></span></h2>
    <div id="live"><div class="empty">现在没有任务在跑</div></div>
  </section>
  <section>
    <h2>需要你决定 <span class="hint" id="todo-n"></span></h2>
    <div id="todo"><div class="empty">正在读…</div></div>
  </section>
  <section>
    <h2>今天的钱</h2>
    <div id="money"></div>
  </section>
  <details id="more">
    <summary>更多：最近执行 · 提案 · 派活</summary>
    <section>
      <h2>最近执行 <span class="dim" style="text-transform:none;letter-spacing:0">（点「证据」看测试命令、退出码和改了哪些文件）</span></h2>
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
  </details>
</main>
<div id="flash"></div>
<script>
const TOKEN = "__TOKEN__";
const H = { Authorization: "Bearer " + TOKEN, "Content-Type": "application/json" };
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
const clip = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n) + "…" : s; };
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

// ---- words, not ids ----
const whoName = a => ({ claude: "Claude", codex: "Codex" }[a] || a || "agent");
const clock = ts => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "?";
// A cwd is on screen to say where the work happened, not to reprint a temp
// hierarchy. Keep the last two meaningful segments, drop noise like Temp /
// AppData, and leave the full path to the `title` tooltip.
const NOISE_DIRS = new Set(["temp", "tmp", "appdata", "local", "roaming", "users",
                            "home", "worktrees", "var", "private"]);
function tailPath(p) {
  const parts = String(p || "").replace(/\\\\/g, "/").split("/").filter(Boolean);
  if (!parts.length) return "";
  const keep = [];
  for (let i = parts.length - 1; i >= 0 && keep.length < 2; i -= 1) {
    if (NOISE_DIRS.has(parts[i].toLowerCase())) break;
    keep.unshift(parts[i]);
  }
  if (!keep.length) keep.push(parts[parts.length - 1]);
  return (keep.length < parts.length ? "…/" : "") + keep.join("/");
}
function ago(ts) {
  if (!ts) return "";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return "刚刚";
  if (d < 3600) return Math.round(d / 60) + " 分钟前";
  if (d < 86400) return Math.round(d / 3600) + " 小时前";
  if (d < 86400 * 30) return Math.round(d / 86400) + " 天前";
  return new Date(ts * 1000).toLocaleDateString("zh-CN");
}
function span(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "";
  seconds = Math.round(seconds);
  if (seconds < 60) return seconds + " 秒";
  if (seconds < 3600) return Math.floor(seconds / 60) + " 分钟";
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60);
    return h + " 小时" + (m ? " " + m + " 分" : "");
  }
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
  return d + " 天" + (h ? " " + h + " 小时" : "");
}

const SYMBOL = { CNY: "¥", USD: "$" };
const money = (currency, value) =>
  (SYMBOL[currency] || currency + " ") + Number(value).toFixed(currency === "CNY" ? 2 : 4);
const moneyParts = costs =>
  Object.entries(costs || {}).sort().map(([c, v]) => money(c, v)).join(" + ") || "—";
const runMoney = u => (u && typeof u.cost === "number" ? money(u.currency || "USD", u.cost) : "-");
const VERIFY = { ok: ["通过", "ok"], red: ["测试红", "bad"], none: ["没验证", "warn"] };

function whyFailed(err) {
  const e = String(err || "");
  if (/^orphaned/i.test(e)) return "跑到一半，启动它的进程退出了（不是模型出错）";
  if (/timeout|timed out|超时/i.test(e)) return "跑太久，超时了";
  if (/error|fail|denied|refused|invalid|quota|limit|429|5\\d\\d/i.test(e)) return "模型报错，任务中断";
  return "任务中断了，没跑完";
}
function aboutRun(run, who) {
  const note = String(run.note || "").split("\\n")[0].trim();
  const task = (state.tasks || []).find(t => t.id && t.id === run.task_id) || {};
  const label = String(task.title || task.goal || "").split("\\n")[0].trim();
  const bits = [];
  if (who) bits.push(who);
  // The task label beats a directory: "opencode smoke" says what ran,
  // "…/nodus-oc-smoke" only says where. The full path stays in 详情.
  if (note) bits.push(clip(note, 44));
  else if (label) bits.push(clip(label, 44));
  else if (run.cwd) bits.push("目录 " + tailPath(run.cwd));
  return bits.join(" · ") || "任务记录没写内容";
}
function fileChange(d) {
  const files = String(d.note || "").split(",").map(s => s.trim()).filter(Boolean);
  const m = /\\+(\\d+)\\s*[−-]\\s*(\\d+)/.exec(d.detail || "");
  const plus = m ? Number(m[1]) : null, minus = m ? Number(m[2]) : null;
  const size = plus == null ? "" : (minus ? `（+${plus} −${minus} 行）` : `（+${plus} 行）`);
  if (files.length === 1) return "改了 " + files[0] + size;
  if (files.length > 1) return "改了 " + files.length + " 个文件" + size;
  return d.detail ? "改动：" + d.detail : "改动在分支上，主工作区没动";
}
function liveWhat(r) {
  const who = whoName(r.agent || r.route);
  const note = String(r.note || "").split("\\n")[0].trim();
  const m = /[\\w.-]+\\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|css|html|json|md|txt|yml|yaml|toml|sh)/i.exec(note);
  let doing = "正在干活";
  if (m) doing = "正在改 " + m[0];
  else if (note) doing = "正在做：" + (note.length > 38 ? note.slice(0, 38) + "…" : note);
  return `<b>${esc(who)}</b> ${esc(doing)}`;
}

let state = { board: null, runs: [], proposals: [], pipelines: [], routes: [], tasks: [] };
let sseLive = false, timer = 0, inflight = false;
let unvOpen = false, moreFailedOpen = false, moreProposalOpen = false;
const count = k => ((state.board || {}).decision_counts || {})[k] || 0;

function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table>`;
}

function renderHeader() {
  const b = state.board || {};
  const t = b.today || {};
  const pipes = state.pipelines.filter(p => p.status === "running").length;
  const n = (b.running || []).length + pipes;
  const runs = t.runs || 0;
  const cost = Object.keys(t.totals || {}).length ? moneyParts(t.totals)
    : (runs ? "待计价" : money("CNY", 0));
  $("s-live").innerHTML = "<b>" + n + "</b> 个任务在跑";
  $("s-cost").innerHTML = "今天 <b>" + cost + "</b>（" + runs + " 单）";
}

function renderLive() {
  const b = state.board || {};
  const live = b.running || [];
  const pipes = state.pipelines.filter(p => p.status === "running");
  const n = live.length + pipes.length;
  $("live-n").textContent = n ? n + " 个" : "";
  const now = Date.now() / 1000;
  const rows = live.map(r => {
    const sub = [];
    if (r.cwd) sub.push(`<span title="${esc(r.cwd)}">目录 ${esc(tailPath(r.cwd))}</span>`);
    if (r.partial_cost == null) sub.push("跑完才知道花了多少");
    else sub.push("约 " + money(r.partial_currency || "USD", r.partial_cost) + "（还没结算）");
    return `<div class="live-row">
      <div>
        <div class="live-what">${liveWhat(r)} <span class="dim">· 已跑 <span data-start="${r.started_at || 0}">${span(now - (r.started_at || now))}</span></span></div>
        <div class="live-sub">${sub.join(" · ")}</div>
      </div>
      <div class="act"><button class="danger" onclick="stopRun('${r.id}')">停止</button></div>
    </div>`;
  });
  for (const p of pipes) {
    const start = p.started_at || p.created_at || 0;
    rows.push(`<div class="live-row">
      <div>
        <div class="live-what"><b>流水线</b> ${esc(p.kind)} 正在跑 <span class="dim">· 已跑 <span data-start="${start}">${span(now - (start || now))}</span></span></div>
        <div class="live-sub">${esc(p.label || "后台批次")} · 不计入花费</div>
      </div>
      <div class="act"><button class="danger" onclick="stopPipe('${p.id}')">停止</button></div>
    </div>`);
  }
  $("live").innerHTML = rows.length ? rows.join("") : `<div class="empty">现在没有任务在跑</div>`;
}

function decisionRow(d) {
  const run = state.runs.find(r => r.id && r.id === d.run) || {};
  const who = whoName(run.agent || run.route || "");
  const when = d.at ? "· " + ago(d.at) : "";
  let what = "", about = "", act = "", meta = [];

  if (d.kind === "proposal") {
    what = (who !== "agent" && who ? who + " 改完了，等你验收" : "有改动，等你验收");
    about = fileChange(d);
    act = `<button class="primary" onclick="merge('${d.id}')">合并</button>
           <button class="danger" onclick="reject('${d.id}')">丢弃</button>`;
    const p = state.proposals.find(p => p.id === d.id) || {};
    meta.push("提案 " + d.id + (p.branch ? " · 分支 " + p.branch : ""));
    if (d.run) meta.push("来源 run " + d.run);
    if (run.note) meta.push("任务：" + run.note);
  } else if (d.kind === "failed") {
    what = whyFailed(d.detail || run.error);
    about = aboutRun(run, who);
    act = `<button class="primary" onclick="rerun('${d.run}')">重跑</button>
           <button onclick="showLog('${d.run}')">看日志</button>`;
    meta.push("run " + d.id + (run.task_id ? " · 任务 " + run.task_id : ""));
    if (run.cwd) meta.push("目录 " + run.cwd);
    meta.push("原始报错：" + (d.detail || run.error || "（没有记录）"));
  } else if (d.kind === "red") {
    what = (who !== "agent" && who ? who + " 改完了，但测试没通过" : "改完了，但测试没通过");
    about = aboutRun(run, "");
    act = `<button class="primary" onclick="showEvidence('${d.run}')">看日志</button>
           <button onclick="rerun('${d.run}')">重跑</button>`;
    meta.push("run " + d.id + (run.task_id ? " · 任务 " + run.task_id : ""));
    if (run.cwd) meta.push("目录 " + run.cwd);
    meta.push("验证结果：测试红");
  } else if (d.kind === "unverified") {
    const list = (state.board.unverified_runs || []).slice(0, 50);
    const n = count("unverified") || list.length || (/(\\d+)/.exec(d.title || "") || [])[1] || "";
    what = n + " 次跑完了，但没有测试证据，所以不算通过";
    const last = list[0] || {};
    about = last.id
      ? "最近一次：" + (ago(last.ended_at) || "刚才") + " · " + whoName(last.agent) +
        (last.title ? " · " + clip(last.title, 32) : "")
      : "这些改动都没跑过测试";
    act = `<button onclick="toggleUnv()">${unvOpen ? "收起" : "看看是哪 " + n + " 次"}</button>` +
      (last.id ? `<button class="primary" onclick="rerunOne('${last.id}')">重跑最近一次</button>` : "");
    if (last.id) meta.push("最近一次 run " + last.id + (last.title ? " · 任务：" + last.title : ""));
    meta.push("没有测试证据时，进程退出码 0 只说明它退出了，不说明改对了。");
    meta.push("「重跑最近一次」只重跑上面那一条，不是这 " + n + " 次。");
  } else {
    what = d.title || "有一件事等你处理";
    about = d.detail || "";
    act = d.run ? `<button onclick="showLog('${d.run}')">看日志</button>` : "";
    meta.push("类型：" + (d.kind || "未知"));
  }

  // the unverified row already says when the last one ran; do not repeat it
  const stamp = d.kind === "unverified" ? "" : when;
  const ask = `<div class="ask">
    <div>
      <div class="ask-what">${esc(what)}</div>
      <div class="ask-about">${esc(about)}${stamp ? ` <span class="when">${esc(stamp)}</span>` : ""}</div>
      <details class="meta"><summary>详情</summary>${meta.map(m => `<div>${esc(m)}</div>`).join("")}</details>
    </div>
    <div class="act">${act}</div>
  </div>`;
  if (d.kind !== "unverified" || !unvOpen) return ask;

  // one line per run: when, who, what, and a button that reruns exactly that
  // run. No single button stands in for all of them.
  const rows = list.map(u => `<div class="unv-row">
      <span class="dim nowrap">${esc(ago(u.ended_at) || "时间没记")}</span>
      <span class="nowrap">${esc(whoName(u.agent))}</span>
      <span class="unv-title" title="${esc(u.title || "")}">${esc(u.title || "（任务没写标题）")}</span>
      <button onclick="rerunOne('${u.id}')">重跑</button>
    </div>`).join("");
  return ask + `<div class="unv-list">${rows || '<div class="empty">没有可展开的记录</div>'}</div>`;
}

function moreBlock(kind, n) {
  const failed = kind === "failed";
  const open = failed ? moreFailedOpen : moreProposalOpen;
  return `<div class="more-inline">还有 ${n} ${failed ? "个失败" : "个提案"}
    <button onclick="${failed ? "toggleMoreFailed()" : "toggleMoreProposal()"}">${open ? "收起" : "展开"}</button></div>`;
}

// The first screen shows the newest 3 failures and 5 proposals; the rest were
// only counted and pointed at a collapsed section at the bottom. These build
// the same row shape for the ones behind the count, so they can open in place.
function extraDecisions(kind, items, counts) {
  const shown = items.filter(d => d.kind === kind);
  const want = Math.max(0, (counts[kind] || 0) - shown.length);
  if (!want) return [];
  if (kind === "failed") {
    const seen = new Set(shown.map(d => d.run || d.id));
    return state.runs
      .filter(r => r.status === "failed" && !seen.has(r.id))
      .slice(0, want)
      .map(r => ({
        kind: "failed", id: r.id, run: r.id, title: "执行失败，要不要重跑由你定",
        detail: String(r.error || "").slice(0, 160), note: "",
        at: r.ended_at || r.started_at,
      }));
  }
  const seen = new Set(shown.map(d => d.id));
  return state.proposals
    .filter(p => p.status === "open" && !seen.has(p.id))
    .slice(0, want)
    .map(p => ({
      kind: "proposal", id: p.id, run: p.run_id || "",
      title: "提案在分支上，等你合入或丢弃", detail: p.stat || "",
      note: (p.files || []).map(f => f.path).filter(Boolean).join(", "),
      at: p.created_at,
    }));
}

function renderTodo() {
  const b = state.board || {};
  const items = b.decisions || [];
  const counts = b.decision_counts || {};
  const parts = [];
  if (counts.proposal) parts.push(counts.proposal + " 个等你验收");
  if (counts.red) parts.push(counts.red + " 个测试没过");
  if (counts.failed) parts.push(counts.failed + " 个失败");
  if (counts.unverified) parts.push(counts.unverified + " 次没验证");
  $("todo-n").textContent = parts.join(" · ");
  if (!items.length) {
    $("todo").innerHTML = `<div class="empty">现在没有需要你决定的事。</div>`;
    return;
  }

  const extraFailed = extraDecisions("failed", items, counts);
  const extraProposals = extraDecisions("proposal", items, counts);
  const last = kind => items.map(d => d.kind).lastIndexOf(kind);
  const lastFailed = last("failed"), lastProposal = last("proposal");
  const blocks = [];
  items.forEach((d, i) => {
    blocks.push(decisionRow(d));
    if (i === lastFailed && extraFailed.length) {
      blocks.push(moreBlock("failed", extraFailed.length));
      if (moreFailedOpen) blocks.push(...extraFailed.map(decisionRow));
    }
    if (i === lastProposal && extraProposals.length) {
      blocks.push(moreBlock("proposal", extraProposals.length));
      if (moreProposalOpen) blocks.push(...extraProposals.map(decisionRow));
    }
  });
  $("todo").innerHTML = blocks.join("");
}

function renderMoney(t) {
  t = t || {};
  const runs = t.runs || 0;
  const agents = t.by_agent || [];
  let html = "";
  if (!runs) {
    html = `<div class="total zero">今天还没有花钱</div>`;
  } else if (!Object.keys(t.totals || {}).length) {
    html = `<div class="total">今天 ${runs} 单，还没有记到金额</div>`;
  } else {
    html = `<div class="total">今天 ${runs} 单，共 <b>${moneyParts(t.totals)}</b></div>` +
      agents.map(a => {
        const amount = Object.keys(a.costs || {}).length ? moneyParts(a.costs) : "还没计价";
        return `<div class="money-row"><span>${esc(a.agent)}</span>
          <span class="amt">${esc(amount)}<span class="dim">（${a.runs} 单）</span></span></div>`;
      }).join("") +
      `<div class="foot">金额取自账单原值，不换算汇率</div>`;
  }
  $("money").innerHTML = html;
}

function renderRuns() {
  const rows = state.runs.slice(0, 12).map(r => {
    const u = r.usage || {};
    const [label, cls] = VERIFY[r.verify] || ["-", "dim"];
    const status = r.status === "completed" ? '<span class="ok">完成</span>'
      : r.status === "failed" ? '<span class="bad">失败</span>'
      : r.status === "running" ? '<span class="pill">在跑</span>' : esc(r.status);
    return `<tr>
      <td class="mono dim">${esc((r.id || "").slice(4, 14))}</td>
      <td class="nowrap">${status}</td>
      <td class="nowrap"><span class="${cls}">${label}</span></td>
      <td class="nowrap">${esc(r.route || r.agent)}<span class="dim"> → ${esc(r.provider || "-")}</span></td>
      <td class="nowrap">${runMoney(u)}</td>
      <td class="task">${esc((r.title || r.note || r.task_id || "").slice(0, 50))}</td>
      <td class="dim nowrap">${ago(r.ended_at || r.started_at)}</td>
      <td class="nowrap"><button onclick="showEvidence('${r.id}')">证据</button></td>
    </tr>`;
  });
  $("runs").innerHTML = table(["run", "状态", "验证", "路由", "成本", "任务", "何时", ""], rows, "还没有执行记录。");
}

function renderProposals() {
  const PSTATE = { open: "等你决定", merged: "已合并", rejected: "已丢弃" };
  const rows = state.proposals.map(p => `<tr>
      <td class="mono">${esc(p.id)}</td>
      <td><span class="pill">${esc(PSTATE[p.status] || p.status)}</span></td>
      <td>${esc(String(p.stat || "").replace(/(\\d+) file\\(s\\)/, "$1 个文件"))}</td>
      <td class="task mono">${esc((p.branch || "").slice(0, 36))}</td></tr>`);
  $("proposals").innerHTML = table(["提案", "状态", "改动", "分支"], rows, "没有提案。");
}

function reveal(box) {
  $("more").open = true;
  box.style.display = "block";
}

async function showEvidence(rid) {
  const box = $("evidence");
  reveal(box);
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
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function showLog(rid) {
  const box = $("evidence");
  reveal(box);
  box.innerHTML = `<h3>日志 <span class="mono dim">${esc(rid)}</span></h3><div class="dim">读取中…</div>`;
  try {
    const [run, events] = await Promise.all([api("/runs/" + rid), api("/runs/" + rid + "/events")]);
    const lines = events.slice(-18).map(e =>
      clock(e.ts) + "  " + e.type + "  " + JSON.stringify(e.payload || {}));
    box.innerHTML = `<h3>日志 <span class="mono dim">${esc(rid)}</span></h3>
      <div class="bad">${esc(whyFailed(run.error))}</div>
      <div class="task">原始报错：<span class="mono">${esc(run.error || "（没有记录）")}</span></div>
      <div class="task">谁干：${esc(run.agent || "-")} · 目录：<span class="mono">${esc(run.cwd || "-")}</span>
        · 开始：${esc(clock(run.started_at))}</div>
      ${lines.length ? `<pre>${esc(lines.join("\\n"))}</pre>` : ""}
      <div class="task">共 ${events.length} 条事件，这里显示最后 ${lines.length} 条。</div>`;
  } catch (e) {
    box.innerHTML = `<h3>日志</h3><div class="bad">读不到日志：${esc(e.message)}</div>`;
  }
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function stopRun(id) {
  const r = state.runs.find(x => x.id === id) || state.board.running.find(x => x.id === id) || {};
  if (!confirm("停止 " + whoName(r.agent || r.route) + " 这个任务？已经改过的文件不会回滚。")) return;
  try { await api("/runs/" + id + "/stop", "POST"); flash("已请求停止，几秒后刷新"); }
  catch (e) { flash("停止失败：" + e.message, true); }
  scheduleRefresh(300);
}
async function stopPipe(id) {
  if (!confirm("停止这个流水线？")) return;
  try { await api("/pipelines/" + id + "/stop", "POST"); flash("已请求停止，几秒后刷新"); }
  catch (e) { flash("停止失败：" + e.message, true); }
  scheduleRefresh(300);
}
async function merge(id) {
  const p = state.proposals.find(x => x.id === id) || {};
  const run = state.runs.find(r => r.id === p.run_id) || {};
  const risky = run.verify && run.verify !== "ok";
  const files = (p.files || []).map(f => f.path).filter(Boolean);
  let text = "把这次改动合进主工作区？\\n\\n" + (files.length ? files.join("、") : (p.stat || ""));
  if (risky) text += "\\n\\n注意：这个改动" +
    (run.verify === "red" ? "测试是红的" : "没有验证记录") + "。";
  if (!confirm(text)) return;
  try { const r = await api("/proposals/" + id + "/merge", "POST");
    flash(r.ok ? "已合入主工作区" + (r.commit ? "：" + String(r.commit).slice(0, 8) : "") : "合并失败：" + r.reason, !r.ok); }
  catch (e) { flash("合并失败：" + e.message, true); }
  scheduleRefresh(300);
}
async function reject(id) {
  if (!confirm("丢弃这次改动？分支和工作区都会删掉，改动不会保留。")) return;
  try { await api("/proposals/" + id + "/reject", "POST"); flash("已丢弃"); }
  catch (e) { flash("丢弃失败：" + e.message, true); }
  scheduleRefresh(300);
}
async function rerun(rid, lead) {
  const r = state.runs.find(x => x.id === rid);
  if (!r) return flash("找不到这个任务的记录，刷新后再试", true);
  const goal = String(r.note || "").split("\\n")[0];
  if (!confirm((lead || "重跑一次？") + "\\n\\n谁干：" + (r.agent || r.route || "-") +
      "\\n目录：" + (r.cwd || "默认") +
      "\\n任务：" + (goal || "（没写）") +
      "\\n\\n会消耗额度，也会直接改动这个目录里的文件；跑完会自动跑测试，过了才算通过。")) return;
  try {
    const run = await api("/runs", "POST", {
      task_id: r.task_id, route: r.route || r.agent, cwd: r.cwd || null,
      note: r.note || "", verify: true,
    });
    flash("已重新派活，跑完会跑测试");
    if (run && run.id) state.runs.unshift({ ...run, agent: r.agent, route: r.route, cwd: r.cwd });
    scheduleRefresh(300);
  } catch (e) { flash("重跑失败：" + e.message, true); }
}
function rerunRecent() {
  const r = (state.board.unverified_runs || [])[0];
  if (!r) return flash("找不到没验证的任务了，刷新看看", true);
  rerun(r.id, "重跑最近一次？");
}
function rerunOne(rid) {
  rerun(rid, "重跑这一次？");
}
function toggleUnv() {
  unvOpen = !unvOpen;
  renderTodo();
}
function toggleMoreFailed() {
  moreFailedOpen = !moreFailedOpen;
  renderTodo();
}
function toggleMoreProposal() {
  moreProposalOpen = !moreProposalOpen;
  renderTodo();
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
    flash("已派活（" + route + "）");
    $("f-goal").value = ""; $("f-accept").value = "";
    scheduleRefresh(300);
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

function scheduleRefresh(ms) {
  clearTimeout(timer);
  timer = setTimeout(refresh, ms);
}

async function refresh() {
  if (inflight) return;
  inflight = true;
  try {
    // /board is the one source for the first screen: runs still going,
    // everything waiting on a human, and today's cost straight from sqlite.
    // /tasks is only the run → task title join the first screen needs so a
    // failed run can be named by what it was asked to do, not by its cwd.
    const [health, board, runs, proposals, pipelines, tasks] = await Promise.all([
      api("/health"), api("/board"), api("/runs"), api("/proposals"), api("/pipelines"),
      api("/tasks")]);
    state.board = { ...board, profile: board.profile || health.profile };
    state.runs = runs.map(r => ({ ...r, usage: safe(r.usage_json) }));
    state.proposals = proposals;
    state.pipelines = pipelines;
    state.tasks = tasks;
    renderHeader(); renderLive(); renderTodo(); renderMoney(board.today);
    renderRuns(); renderProposals();
  } catch (e) {
    $("todo").innerHTML = '<div class="empty bad">连不上 daemon：' + esc(e.message) + "</div>";
    $("live").innerHTML = "";
    $("money").innerHTML = "";
  } finally {
    inflight = false;
  }
}

function startStream() {
  if (!window.EventSource) return;
  // SSE carries this daemon's own runs (runner.BUS is in-process). A run
  // started from a terminal in another process never appears here, so the
  // stream is an accelerator and the timers below stay as the floor.
  try {
    const es = new EventSource("/events?token=" + encodeURIComponent(TOKEN));
    es.onopen = () => { sseLive = true; };
    es.onmessage = () => scheduleRefresh(250);
    es.onerror = () => { sseLive = false; };
  } catch (e) { sseLive = false; }
}

function tick() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-start]").forEach(el => {
    const start = Number(el.dataset.start);
    if (start) el.textContent = span(now - start);
  });
}

$("dispatch").addEventListener("submit", dispatch);
loadRoutes(); refresh(); startStream();
setInterval(tick, 1000);
setInterval(refresh, 15000);
setInterval(() => { if (!sseLive) refresh(); }, 5000);
</script>
</body>
</html>
"""
