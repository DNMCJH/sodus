"""The web board: one HTML page served by the daemon.

No framework and no build step on purpose. A tool whose whole argument is
"acceptance needs executable evidence" should be inspectable by opening it.

The page ships with the API token embedded. That is safe *because* of where it
is served: the daemon binds 127.0.0.1, only the process that asked for the page
can read it, and any local process could read `~/.nodus/token` anyway.
Cross-origin reads are blocked by the same-origin policy, and cross-origin
writes cannot add the Authorization header.

A failure is never a question. The first screen splits by whose job it is:
"需要你决定" holds only the three things a person owns (merge a proposal, a
spend decision past the budget, an ambiguous requirement); "我在处理" is the
machine at work — the attempt number, the attempts already spent, the likely
cause and the plan, with no button but 看日志; "已停止" is the report for a
spent chain, a repeated error, a deterministic error or a red test nobody
owns, plus the older failures a retry moved past; "不可验证" is information
about runs that finished with no test evidence, and says why (usually no test
entry in that directory) and what would give the next one a green light.

Every card keeps the five answers — **哪个项目 / 什么样的失败 / 自己试过几次 /
可能原因 / 接下来怎么办** — but the labels change with the voice: 我试了 /
原因 / 我的判断 / 收尾 for a stopped row, 我在做 / 已试过 / 原因推测 / 计划
for one in flight. The project line carries the last two directories
(`nodus-removal（…/2026-10-survey）`), the failure line names the class
(进程退出 / 超时 / 测试红 / 模型报错 / 证据缺失 / 无法归类), and the cause
line is a rule-based guess quoting the line it came from. The raw evidence —
last tool call, failed tests, diff, error tail — is folded behind 详情 instead
of pushing the answers down the card. Missing evidence is printed as
"证据缺失：…"; nothing is paraphrased into "执行失败", and no failure copy
asks the reader whether the failure is theirs to handle.

Nothing here polls. `/board` is fetched on load, when the tab regains focus, and
when 刷新 is clicked; the numbers between those moments are a snapshot, and that
is the trade the page makes to cost no CPU while it sits open.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nodus</title>
<style>
  :root { color-scheme: dark;
          --bg:#0a0c0f; --panel:#11151a; --panel-2:#151a20; --panel-3:#0d1116;
          --line:#212730; --line-soft:#191e25;
          --text:#e7eaf0; --text-2:#b8c0cb; --dim:#88919e; --faint:#5f6875;
          --accent:#6f8cff; --accent-2:#8ba3ff; --accent-ink:#0a0d14;
          --ok:#4ec38a; --bad:#ef6b6b; --warn:#e0a340;
          --r:10px; }
  * { box-sizing:border-box; }
  html, body { margin:0; }
  body { background:var(--bg); color:var(--text); min-height:100vh;
         font:14px/1.55 ui-sans-serif, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         -webkit-font-smoothing:antialiased; }
  body::before { content:""; position:fixed; inset:0 0 auto 0; height:340px; pointer-events:none;
                 background:radial-gradient(900px 300px at 18% -60px, rgba(111,140,255,.07), transparent 70%); }
  ::selection { background:rgba(111,140,255,.28); }
  header { display:flex; align-items:center; gap:18px; padding:0 26px; height:58px;
           border-bottom:1px solid var(--line); position:sticky; top:0; z-index:4;
           background:rgba(10,12,15,.88); backdrop-filter:blur(8px); }
  .brand { display:flex; align-items:center; gap:10px; }
  .brand .mark { width:17px; height:17px; border-radius:5px; background:linear-gradient(145deg,#8ba3ff,#5a74e8);
                 box-shadow:0 0 0 1px rgba(139,163,255,.25) inset; }
  h1 { font-size:13px; letter-spacing:.2em; margin:0; font-weight:650; color:var(--text); }
  .hstats { display:flex; align-items:center; gap:18px; margin-left:6px; color:var(--dim);
            font-size:12.5px; font-variant-numeric:tabular-nums; }
  .hstats b { color:var(--text); font-weight:600; }
  .hstats .chip { display:flex; align-items:center; gap:6px; white-space:nowrap; }
  .status-dot { width:7px; height:7px; border-radius:99px; background:var(--faint); display:inline-block; }
  .status-dot.ok { background:var(--ok); }
  .status-dot.bad { background:var(--bad); }
  .status-dot.warn { background:var(--warn); }
  .rule { margin-left:auto; color:var(--faint); font-size:12px; white-space:nowrap; }
  main { max-width:1080px; margin:0 auto; padding:30px 26px 72px; }
  section { margin-bottom:32px; }
  .sec-head { display:flex; align-items:center; gap:10px; margin:0 0 12px; }
  h2 { font-size:13px; font-weight:650; color:var(--text-2); margin:0; letter-spacing:.02em; }
  .sec-note { color:var(--faint); font-size:12px; }
  .count { margin-left:auto; color:var(--dim); font-size:12px; font-variant-numeric:tabular-nums;
           border:1px solid var(--line); border-radius:999px; padding:1px 9px; }
  .count.bad { color:#f3a2a2; border-color:rgba(239,107,107,.32); background:rgba(239,107,107,.07); }
  .stack { display:flex; flex-direction:column; gap:10px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r); }
  .empty { color:var(--dim); padding:12px 2px; font-size:13px; }
  .empty-card { display:flex; gap:13px; align-items:flex-start; background:var(--panel);
                border:1px solid var(--line); border-radius:var(--r); padding:18px 18px 17px; }
  .empty-icon { width:22px; height:22px; border-radius:99px; border:1px solid rgba(78,195,138,.35);
                background:rgba(78,195,138,.08); color:var(--ok); display:flex; align-items:center;
                justify-content:center; font-size:12px; flex:none; margin-top:1px; }
  .empty-icon.wait { border-color:rgba(224,163,64,.35); background:rgba(224,163,64,.08);
                     color:var(--warn); }
  .empty-title { color:var(--text); font-size:14px; font-weight:600; }
  .empty-sub { color:var(--dim); font-size:12.5px; margin-top:3px; line-height:1.6; }
  button { appearance:none; background:transparent; color:var(--text-2); border:1px solid var(--line);
           border-radius:7px; height:30px; padding:0 12px; font:inherit; font-size:12.5px;
           cursor:pointer; white-space:nowrap; transition:background .12s, border-color .12s, color .12s; }
  button:hover { color:var(--text); border-color:#333c49; background:#171d24; }
  button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-ink);
                   font-weight:600; }
  button.primary:hover { background:var(--accent-2); border-color:var(--accent-2); }
  button.danger { color:var(--bad); }
  button.danger:hover { border-color:rgba(239,107,107,.4); background:rgba(239,107,107,.07); }
  button:focus-visible { outline:2px solid rgba(111,140,255,.5); outline-offset:1px; }
  #refresh { height:28px; padding:0 11px; font-size:12px; }
  #refresh[disabled] { opacity:.55; cursor:default; }
  .decision { padding:15px 17px 14px; }
  /* The two failure voices carry a border tone instead of a badge: the working
     block is warn, the stopped block is bad, and neither is a call to action. */
  .card.handling { border-color:rgba(224,163,64,.20); }
  .card.stopped { border-color:rgba(239,107,107,.15); }
  .card.unverifiable { border-style:dashed; }
  .q-ask .q-v { color:var(--text); font-size:13.5px; }
  .d-when { margin-left:auto; color:var(--faint); font-size:12px; white-space:nowrap;
            font-variant-numeric:tabular-nums; }
  /* The five questions every decision must answer, one line each. A reader
     skims these left labels; the values are the answer. */
  .questions { margin-top:9px; display:grid; gap:6px; }
  .q-line { display:grid; grid-template-columns:54px minmax(0,1fr); gap:10px;
            font-size:13px; line-height:1.5; }
  .q-k { color:var(--faint); font-size:11.5px; padding-top:2px; letter-spacing:.04em; }
  .q-v { color:var(--text-2); min-width:0; overflow-wrap:anywhere; }
  .q-project .q-v { display:flex; align-items:baseline; flex-wrap:wrap; color:var(--text); }
  .q-project .q-v b { font-weight:600; }
  .q-project .d-when { margin-left:auto; }
  .q-tag { display:inline-block; border:1px solid var(--line); border-radius:4px; padding:0 6px;
           margin-right:6px; color:var(--dim); font-size:11.5px; white-space:nowrap; }
  .q-tag.bad { border-color:rgba(239,107,107,.3); color:#eda2a2; }
  .q-tag.na { border-color:rgba(224,163,64,.32); color:#e2b877; }
  .q-advice .q-v { color:var(--text); }
  .d-path { color:var(--faint); font-size:11.5px; }
  .d-act { display:flex; align-items:center; gap:8px; margin-top:11px; flex-wrap:wrap; }
  .d-act .hint { color:var(--faint); font-size:12px; margin-left:2px; }
  .evidence { margin-top:13px; border-top:1px solid var(--line-soft); padding-top:11px;
              display:grid; gap:8px; }
  .ev-row { display:grid; grid-template-columns:52px minmax(0,1fr); gap:12px;
            font-size:12.5px; line-height:1.55; }
  .ev-k { color:var(--faint); font-size:12px; padding-top:1px; }
  .ev-v { color:var(--text-2); min-width:0; }
  .ev-v.na { color:var(--warn); opacity:.9; }
  .ev-src { color:var(--faint); font-size:11.5px; margin-top:2px; }
  .tests { display:grid; gap:3px; }
  .test { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
  .test-name { color:#f0b4b4; font-family:ui-monospace, Consolas, monospace; font-size:12px; }
  .test-assert { color:var(--text-2); font-family:ui-monospace, Consolas, monospace; font-size:11.5px; }
  pre.tail { margin:5px 0 0; padding:8px 10px; background:var(--panel-3); border:1px solid var(--line-soft);
             border-radius:6px; color:#c6cdd8; white-space:pre-wrap; word-break:break-word;
             font:11.5px/1.5 ui-monospace, Consolas, monospace; max-height:128px; overflow:auto; }
  .attempts { display:flex; gap:6px; flex-wrap:wrap; }
  .attempt { display:inline-flex; align-items:baseline; gap:5px; padding:2px 8px; border:1px solid var(--line);
             border-radius:999px; font-size:11.5px; color:var(--text-2); white-space:nowrap;
             font-variant-numeric:tabular-nums; }
  .attempt b { font-weight:600; color:var(--text); }
  .attempt.red { border-color:rgba(239,107,107,.3); color:#eda2a2; }
  .attempt.warn { border-color:rgba(224,163,64,.3); color:#e2b877; }
  .attempt.ok { border-color:rgba(78,195,138,.3); color:#8ed7b4; }
  .retry-row { display:grid; grid-template-columns:auto minmax(0,1fr) auto auto; gap:12px;
               align-items:center; background:var(--panel); border:1px solid var(--line);
               border-radius:9px; padding:11px 14px; }
  .retry-who { min-width:0; color:var(--text-2); font-size:13px; }
  .retry-who b { color:var(--text); font-weight:600; }
  .retry-who .sep { color:var(--faint); margin:0 6px; }
  .retry-last { color:var(--faint); font-size:11.5px; margin-top:2px; }
  .retry-progress { color:var(--dim); font-size:12px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .dots { display:inline-flex; gap:4px; margin-right:6px; vertical-align:1px; }
  .dots i { width:5px; height:5px; border-radius:99px; background:#2b323c; display:inline-block; }
  .dots i.on { background:var(--warn); }
  .when { color:var(--warn); font-size:12px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .live-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center;
              background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:11px 14px; }
  .live-what { color:var(--text); font-size:13.5px; }
  .live-what b { font-weight:600; }
  .live-sub { color:var(--faint); font-size:11.5px; margin-top:2px; }
  .live-sub .sep { margin:0 6px; color:#3a424e; }
  .money-total { color:var(--dim); font-size:13px; padding:2px 2px 10px; font-variant-numeric:tabular-nums; }
  .money-total b { color:var(--text); font-size:18px; font-weight:650; margin:0 4px; }
  .money-row { display:flex; justify-content:space-between; align-items:baseline; max-width:420px;
               padding:6px 2px; border-bottom:1px solid var(--line-soft); color:var(--text-2);
               font-size:13px; font-variant-numeric:tabular-nums; }
  .money-row:last-of-type { border-bottom:0; }
  .money-row .amt { color:var(--text); }
  .foot { color:var(--faint); font-size:11.5px; padding-top:8px; }
  .mono { font-family:ui-monospace, Consolas, monospace; font-size:12px; }
  .dim { color:var(--dim); }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line-soft); vertical-align:top; }
  th { color:var(--faint); font-weight:500; font-size:11.5px; }
  td.nowrap, th.nowrap { white-space:nowrap; }
  tr:hover td { background:rgba(255,255,255,.018); }
  .unv-list { margin-top:10px; border-top:1px solid var(--line-soft); padding-top:8px;
              max-height:320px; overflow:auto; }
  .unv-row { display:flex; gap:10px; align-items:center; padding:5px 0; font-size:12.5px;
             border-bottom:1px solid var(--line-soft); }
  .unv-row:last-child { border-bottom:0; }
  .unv-title { color:var(--text-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .unv-row button { margin-left:auto; flex:none; }
  .more-inline { color:var(--dim); font-size:12.5px; padding:10px 2px 0; }
  .more-inline button { margin-left:6px; height:26px; }
  /* memory review: one line per fact, the sources in small print, and nothing
     but an adopt button. The section stays hidden when there is no candidate. */
  .mem-list { background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
              max-height:440px; overflow:auto; }
  .mem-head { display:flex; align-items:center; gap:12px; padding:10px 14px;
              border-bottom:1px solid var(--line-soft); font-size:12.5px; color:var(--dim); }
  .mem-head button { margin-left:auto; height:26px; }
  .mem-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:12px;
             align-items:center; padding:9px 14px; border-bottom:1px solid var(--line-soft); }
  .mem-row:last-child { border-bottom:0; }
  .mem-text { color:var(--text-2); font-size:13px; line-height:1.45; }
  .mem-src { color:var(--faint); font-size:11.5px; margin-top:2px; }
  .mem-flag { color:#e8c07a; border:1px solid rgba(224,163,64,.32);
              background:rgba(224,163,64,.08); border-radius:999px; padding:1px 8px;
              font-size:11.5px; white-space:nowrap; }
  .mem-row button { height:26px; }
  #evidence { background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
              padding:14px 16px; margin-top:12px; display:none; }
  #evidence h3 { margin:0 0 8px; font-size:13px; font-weight:600; }
  #evidence pre { white-space:pre-wrap; word-break:break-word; max-height:320px; overflow:auto;
                  background:var(--panel-3); border:1px solid var(--line-soft); border-radius:6px;
                  padding:10px; font-size:11.5px; color:#c6cdd8; }
  .evidence details.raw { border-top:1px dashed var(--line-soft); margin-top:7px; padding-top:6px; }
  .evidence details.raw summary { font-size:11.5px; color:var(--faint); }
  .evidence details.raw summary::before { content:"+"; display:inline-block; width:10px; }
  .evidence details.raw[open] summary::before { content:"–"; }
  .evidence details.raw pre { margin-top:5px; }
  /* Evidence is folded: the five answers stay scannable, the raw output is
     one click down instead of pushing the answer off the card. */
  details.more { border-top:1px solid var(--line-soft); margin-top:11px; padding-top:9px; }
  details.more summary { font-size:12px; color:var(--faint); }
  details.more summary::before { content:"▸ "; }
  details.more[open] summary::before { content:"▾ "; }
  details.more .evidence { margin-top:9px; border-top:0; padding-top:0; }
  details { border-top:1px solid var(--line); margin-top:4px; padding-top:14px; }
  details summary { color:var(--dim); cursor:pointer; font-size:12.5px; letter-spacing:.02em;
                    list-style:none; }
  details summary::-webkit-details-marker { display:none; }
  details summary:hover { color:var(--text); }
  details section { margin-top:22px; }
  form { display:grid; gap:10px; max-width:760px; }
  label { display:grid; gap:5px; font-size:12.5px; color:var(--dim); }
  input, select, textarea { background:var(--panel-3); color:var(--text); border:1px solid var(--line);
                            border-radius:6px; padding:7px 9px; font:inherit; font-size:13px; }
  textarea { min-height:68px; resize:vertical; }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .check { display:flex; gap:8px; align-items:center; color:var(--text-2); font-size:12.5px; }
  #flash { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
           background:var(--panel-2); border:1px solid var(--line); border-radius:8px;
           padding:9px 15px; display:none; max-width:80vw; z-index:6; font-size:13px;
           box-shadow:0 10px 30px rgba(0,0,0,.35); }
  @media (max-width:720px) {
    header { padding:0 16px; gap:12px; }
    .rule { display:none; }
    main { padding:20px 16px 60px; }
    .ev-row { grid-template-columns:44px minmax(0,1fr); gap:8px; }
    .retry-row { grid-template-columns:auto minmax(0,1fr); }
    .retry-progress, .when { grid-column:2; }
  }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark"></span><h1>NODUS</h1></div>
  <div class="hstats">
    <span class="chip"><span class="status-dot" id="s-wait-dot"></span><span id="s-wait">读状态…</span></span>
    <span class="chip" id="s-live">—</span>
    <span class="chip" id="s-cost">—</span>
    <span class="chip" id="s-retry" hidden></span>
  </div>
  <div class="rule">结束 ≠ 通过 · 只有测试证据变绿才算验收</div>
  <button id="refresh" onclick="manualRefresh()" title="重新拉取 /board。页面不会自己刷新。">刷新</button>
</header>
<main>
  <section>
    <div class="sec-head"><h2>正在跑</h2><span class="sec-note" id="live-n"></span></div>
    <div id="live" class="stack"><div class="empty">现在没有任务在跑</div></div>
  </section>
  <section>
    <div class="sec-head"><h2>需要你决定</h2><span class="sec-note">只有三件事要你定：合不合、花不花钱、往哪个方向走</span><span class="count" id="todo-n"></span></div>
    <div id="todo" class="stack"><div class="empty">正在读…</div></div>
  </section>
  <section id="handling-sec">
    <div class="sec-head"><h2>我在处理</h2><span class="sec-note">失败、重试、验收都是我这边的事，不用你动手</span><span class="count" id="handling-n"></span></div>
    <div id="handling" class="stack"><div class="empty">正在读…</div></div>
  </section>
  <section id="stopped-sec" hidden>
    <div class="sec-head"><h2>已停止</h2><span class="sec-note">试到上限我就停：做了什么、结论是什么、证据在哪</span><span class="count" id="stopped-n"></span></div>
    <div id="stopped" class="stack"></div>
  </section>
  <section id="unv-sec" hidden>
    <div class="sec-head"><h2>不可验证</h2><span class="sec-note">跑完了但没有测试证据：不算通过，也不算失败</span><span class="count" id="unv-n"></span></div>
    <div id="unv" class="stack"></div>
  </section>
  <section id="memory-sec" hidden>
    <div class="sec-head"><h2>记忆待审</h2><span class="sec-note">从 CLAUDE.md / AGENTS.md 抽出来的指令，项目来源写进项目 AGENTS.md，全局来源写进 ~/.nodus/AGENTS.md</span><span class="count" id="memory-n"></span></div>
    <div id="memory"></div>
  </section>
  <section>
    <div class="sec-head"><h2>今天的钱</h2><span class="sec-note">账单原值，不换算汇率</span></div>
    <div id="money"></div>
  </section>
  <section>
    <div class="sec-head"><h2>用量</h2><span class="sec-note">token 与缓存命中，按路由</span></div>
    <div id="usage"></div>
  </section>
  <details id="more">
    <summary>更多：最近执行 · 提案 · 派活</summary>
    <section>
      <div class="sec-head"><h2>最近执行</h2><span class="sec-note">点「证据」看测试命令、退出码和改了哪些文件</span></div>
      <div id="runs"></div>
      <div id="evidence"></div>
    </section>
    <section>
      <div class="sec-head"><h2>提案（在分支上，主工作区没动）</h2></div>
      <div id="proposals"></div>
    </section>
    <section>
      <div class="sec-head"><h2>派活</h2></div>
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
          <span class="dim" id="f-hint"></span></div>
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
  currency === "-" ? "币种未记录 " + Number(value).toFixed(2)
  : (SYMBOL[currency] || currency + " ") + Number(value).toFixed(currency === "CNY" ? 2 : 4);
const moneyParts = costs =>
  Object.entries(costs || {}).sort().map(([c, v]) => money(c, v)).join(" + ") || "—";
const humanTokens = n => {
  n = Number(n) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
};
const pct = r => (r == null ? "—" : Math.round(r * 100) + "%");
const runMoney = u => (u && typeof u.cost === "number" ? money(u.currency || "USD", u.cost) : "-");
const VERIFY = { ok: ["通过", "ok"], red: ["测试红", "bad"], none: ["没验证", "warn"] };

function liveWhat(r) {
  const who = whoName(r.agent || r.route);
  const note = String(r.note || "").split("\\n")[0].trim();
  const retry = /^auto-retry (\\d+)\\/(\\d+)/.exec(note);
  const m = /[\\w.-]+\\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|css|html|json|md|txt|yml|yaml|toml|sh)/i.exec(note);
  let doing = "正在干活";
  if (retry) doing = `正在自动重试（第 ${retry[1]}/${retry[2]} 次）`;
  else if (m) doing = "正在改 " + m[0];
  else if (note) doing = "正在做：" + (note.length > 38 ? note.slice(0, 38) + "…" : note);
  return `<b>${esc(who)}</b> ${esc(doing)}`;
}

let state = { board: null, runs: [], proposals: [], pipelines: [], routes: [], tasks: [] };
let inflight = false;
let unvOpen = false, moreNeedsOpen = false;

function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table>`;
}

function outcomeTone(text) {
  const t = String(text || "").toLowerCase();
  if (t.includes("tests_red") || t.includes("exit") || t.includes("auth") ||
      t.includes("param") || t.includes("orphan") || t.includes("failed")) return "red";
  if (t.includes("timeout") || t.includes("network") || t.includes("rate") || t.includes("upstream")) return "warn";
  if (t.includes("ok") || t.includes("completed")) return "ok";
  return "";
}

function renderHeader() {
  const b = state.board || {};
  const t = b.today || {};
  const pipes = state.pipelines.filter(p => p.status === "running").length;
  const live = (b.running || []).length + pipes;
  const runs = t.runs || 0;
  const cost = Object.keys(t.totals || {}).length ? moneyParts(t.totals)
    : (runs ? "待计价" : money("CNY", 0));
  const waiting = (b.needs_you || []).length;
  const retrying = (b.handling || []).length;
  $("s-live").innerHTML = `<b>${live}</b> 个在跑`;
  $("s-cost").innerHTML = `今天 <b>${cost}</b><span class="dim"> · ${runs} 单</span>`;
  $("s-wait").textContent = waiting ? waiting + " 件要你定" : "没有要你定的事";
  $("s-wait-dot").className = "status-dot " + (waiting ? "bad" : "ok");
  $("s-retry").innerHTML = `<span class="status-dot warn"></span><b>${retrying}</b> 个失败在重试`;
  $("s-retry").hidden = !retrying;
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
        <div class="live-what"><span class="status-dot ok"></span> ${liveWhat(r)} <span class="dim">· 已跑 ${span(now - (r.started_at || now))}</span></div>
        <div class="live-sub">${sub.join('<span class="sep">·</span>')}</div>
      </div>
      <div class="act"><button class="danger" onclick="stopRun('${r.id}')">停止</button></div>
    </div>`;
  });
  for (const p of pipes) {
    const start = p.started_at || p.created_at || 0;
    rows.push(`<div class="live-row">
      <div>
        <div class="live-what"><span class="status-dot ok"></span> <b>流水线</b> ${esc(p.kind)} 正在跑 <span class="dim">· 已跑 ${span(now - (start || now))}</span></div>
        <div class="live-sub">${esc(p.label || "后台批次")}<span class="sep">·</span>不计入花费</div>
      </div>
      <div class="act"><button class="danger" onclick="stopPipe('${p.id}')">停止</button></div>
    </div>`);
  }
  $("live").innerHTML = rows.length ? rows.join("") : `<div class="empty">现在没有任务在跑</div>`;
}

// A failure is never a question. The board speaks in three voices:
//   需要你决定 — a value judgement (merge? spend? which direction?),
//   我在处理   — a statement about what the system is doing right now,
//   已停止     — a report: what I tried, why I stopped, where the evidence is.
// The five answers survive from the old decision card, relabelled per voice;
// the raw evidence (tool call / tests / diff / tail) stays behind 详情.
const KIND_TONE = new Set(["测试红", "模型报错", "进程退出"]);
const LINES = {
  handling: ["项目", "我在做", "已试过", "原因推测", "计划"],
  stopped: ["项目", "我试了", "原因", "我的判断", "收尾"],
};

// The server sends `cwd` next to the project name so two same-named checkouts
// stay apart. The project's own directory is already on the line as `project`,
// so the locator only needs the parent: nodus-removal（…/2026-10-survey）.
function parentPath(path, project) {
  const tail = project ? "/" + project : "";
  const parent = tail && path.endsWith(tail) ? path.slice(0, -tail.length) : path;
  return parent && parent !== project ? parent : "";
}

function projectLine(d, when) {
  const project = d.project || "未记录项目";
  const parent = parentPath(d.project_path || "", project);
  return `<div class="q-line q-project"><span class="q-k">项目</span><span class="q-v">
      <b>${esc(project)}</b>
      ${parent ? `<span class="d-path" title="${esc(d.cwd || "")}">（…/${esc(parent)}）</span>` : ""}
      <span class="sep" style="color:var(--faint);margin:0 5px">·</span>${esc(d.task_title || "（任务没写标题）")}
      ${when ? `<span class="d-when">${esc(when)}</span>` : ""}
    </span></div>`;
}

function retryWait(ts) {
  if (!ts) return "正在跑";
  const left = ts - Date.now() / 1000;
  return left <= 0 ? "马上" : "约 " + span(left) + "后";
}

function answerLines(d, when, voice) {
  const labels = LINES[voice];
  // The two failure voices answer the same five questions in different order:
  // a row in flight has a retry history worth a line, a stopped row has the
  // reason it stopped. Both name the failure class on the cause line.
  return `<div class="questions">
    ${projectLine(d, when)}
    <div class="q-line"><span class="q-k">${labels[1]}</span><span class="q-v">${doingText(d, voice)}</span></div>
    <div class="q-line"><span class="q-k">${labels[2]}</span><span class="q-v">${voice === "handling" ? esc(d.retry_line || "没有重试记录") : tagLine(d)}</span></div>
    <div class="q-line"><span class="q-k">${labels[3]}</span><span class="q-v">${voice === "handling" ? tagLine(d) : esc(d.why || "没留下停下来的原因")}</span></div>
    <div class="q-line q-advice"><span class="q-k">${labels[4]}</span><span class="q-v">${esc(d.plan || d.suggestion || "")}</span></div>
  </div>`;
}

// The failure class as a tag plus the rule-based cause, which quotes the line
// it matched and says what would have to change.
function tagLine(d) {
  const kind = d.failure_kind || "";
  const tone = d.evidence_missing ? "na" : KIND_TONE.has(kind) ? "bad" : "";
  const tag = kind ? `<span class="q-tag ${tone}">${esc(kind)}</span>` : "";
  return `${tag}${esc(d.cause_line || d.likely_cause || "推断不了：这条记录里没有能归因的报错原文")}`;
}

// "第 1/3 次重试" + when it starts. The server keeps the sentence free of a
// countdown so the page can refresh it without re-fetching.
function doingText(d, voice) {
  const text = esc(d.statement || "");
  if (voice !== "handling" || d.running || !d.next_retry_at) return text;
  return `${text}（${esc(retryWait(d.next_retry_at))}开始）`;
}

function retryChips(ev, d) {
  const attempts = ev.attempts || [];
  if (!attempts.length) return "";
  const chips = attempts.map(a => `<span class="attempt ${outcomeTone(a.outcome)}"><b>第 ${a.n} 次</b>${a.seconds == null ? "" : ` · ${a.seconds}s`}${a.outcome ? ` · ${esc(a.outcome)}` : ""}</span>`).join("");
  const how = d.retry && d.retry.mode ? (d.retry.mode === "resume" ? "续会话" : "从头跑") : "";
  return `<div class="ev-row"><span class="ev-k">试过什么</span><div class="ev-v"><div class="attempts">${chips}</div>
    ${how ? `<div class="ev-src">重试方式：${esc(how)}</div>` : ""}</div></div>`;
}

// Everything raw lives behind 详情: the answers stay readable, and the
// tool_call / diff / error tail are one click down instead of pushing them off
// the card.
function detailBlock(d) {
  const ev = d.evidence || {};
  const rows = [];
  if (ev.action) {
    rows.push(`<div class="ev-row"><span class="ev-k">哪一步</span><span class="ev-v">${esc(ev.action)}</span></div>`);
  } else {
    rows.push(`<div class="ev-row"><span class="ev-k">哪一步</span><span class="ev-v na">这类证据没有记录：event 表里没有 tool_call</span></div>`);
  }
  const tests = ev.tests || [];
  if (tests.length) {
    const list = tests.map(t => `<div class="test">
        <span class="test-name">${esc(t.name || t.test || "")}</span>
        ${t.assertion ? `<span class="test-assert">${esc(t.assertion)}</span>` : ""}</div>`).join("");
    rows.push(`<div class="ev-row"><span class="ev-k">什么错</span><div class="ev-v">
      <div class="tests">${list}</div>
      ${ev.error_tail ? `<details class="raw"><summary>原始输出（末尾 ${ev.error_tail.length} 字）</summary><pre class="tail">${esc(ev.error_tail)}</pre></details>` : ""}
      </div></div>`);
  } else if (ev.error_tail) {
    rows.push(`<div class="ev-row"><span class="ev-k">什么错</span><div class="ev-v">
      <pre class="tail">${esc(ev.error_tail)}</pre>
      <div class="ev-src">${esc(ev.error_source || "原始输出")} · 末尾 ${ev.error_tail.length} 字</div></div></div>`);
  } else {
    rows.push(`<div class="ev-row"><span class="ev-k">什么错</span><span class="ev-v na">这类证据没有记录：run.error 和事件流里都没有报错原文</span></div>`);
  }
  if (d.change) {
    rows.push(`<div class="ev-row"><span class="ev-k">改了哪</span><div class="ev-v">${esc(d.change)}
      ${ev.change && ev.change.stat ? `<div class="ev-src mono">${esc(ev.change.stat)}</div>` : ""}
      ${ev.change && ev.change.basis ? `<div class="ev-src">${esc(ev.change.basis)}</div>` : ""}</div></div>`);
  } else {
    rows.push(`<div class="ev-row"><span class="ev-k">改了哪</span><span class="ev-v na">这类证据没有记录：artifact 是空的</span></div>`);
  }
  const chips = retryChips(ev, d);
  if (chips) {
    rows.push(chips);
  } else {
    rows.push(`<div class="ev-row"><span class="ev-k">试过什么</span><span class="ev-v na">${esc(d.retry_line || "还没有自动重试过")}</span></div>`);
  }
  if (d.why) {
    rows.push(`<div class="ev-row"><span class="ev-k">为什么</span><span class="ev-v">${esc(d.why)}</span></div>`);
  }
  if ((ev.missing || []).length) {
    rows.push(`<div class="ev-row"><span class="ev-k">缺什么</span><span class="ev-v na">${ev.missing.map(esc).join("；")}</span></div>`);
  }
  return `<details class="more"><summary>详情（哪一步 / 改了哪 / 原始输出）</summary>
    <div class="evidence">${rows.join("")}</div></details>`;
}

// 需要你决定: a question with two answers, not a failure to triage. The proposal
// is a value judgement, the budget row is money, and both show the evidence
// behind 详情 because the answer usually depends on it.
function decisionCard(d) {
  const when = d.at ? ago(d.at) : "";
  const kind = d.kind || "proposal";
  let act = "";
  if (kind === "proposal") {
    act = `<button class="primary" onclick="merge('${d.id}')">合并</button>
           <button class="danger" onclick="reject('${d.id}')">丢弃</button>`;
  } else if (kind === "budget") {
    act = `<button class="primary" onclick="rerun('${d.run}', '再让这个任务试一次')">继续花</button>
           <button onclick="showLog('${d.run}')">不花，看日志</button>`;
  } else if (d.run) {
    act = `<button onclick="showLog('${d.run}')">看日志</button>`;
  }
  return `<article class="card decision ${kind}">
    <div class="questions">
      ${projectLine(d, when)}
      <div class="q-line q-ask"><span class="q-k">要决定</span><span class="q-v">${esc(d.question || "这件事要不要做？")}</span></div>
      <div class="q-line"><span class="q-k">依据</span><span class="q-v">${esc(d.basis || d.problem || "")}</span></div>
      ${d.suggestion ? `<div class="q-line"><span class="q-k"></span><span class="q-v dim">${esc(d.suggestion)}</span></div>` : ""}
    </div>
    <div class="d-act">${act}</div>
    ${d.evidence ? detailBlock(d) : ""}
  </article>`;
}

// 我在处理: the system is on it. The only button is 看日志, because there is
// nothing for a person to press: this block never asks whether to run again.
function handlingCard(d) {
  const when = d.at ? ago(d.at) : "";
  return `<article class="card handling">
    ${answerLines(d, when, "handling")}
    <div class="d-act">
      <button onclick="showLog('${d.run}')">看日志</button>
      <span class="hint">${d.running ? "正在跑，不用你点" : "到点我自己开跑，不用你点"}</span>
    </div>
    ${detailBlock(d)}
  </article>`;
}

// 已停止: the work report. A convenience retry button is allowed; the copy is
// a statement ("我试了 / 我的判断 / 收尾"), never a question back to the reader.
function stoppedCard(d) {
  const when = d.at ? ago(d.at) : "";
  const hint = d.settled ? "更早的失败，已经翻过去了" : "我停了，证据都在这";
  return `<article class="card stopped">
    ${answerLines(d, when, "stopped")}
    <div class="d-act">
      <button onclick="showLog('${d.run}')">看日志</button>
      ${d.run ? `<button onclick="rerun('${d.run}', '再试一次这个任务')">重试</button>` : ""}
      <span class="hint">${hint}</span>
    </div>
    ${detailBlock(d)}
  </article>`;
}

const unvList = () => ((state.board || {}).unverified_runs || []).slice(0, 50);

// 不可验证: information, not a decision and not a failure. It says why there is
// no green light and what would give the next run one.
function unverifiableCard(d) {
  const when = d.at ? ago(d.at) : "";
  const rows = unvList();
  const head = `<article class="card unverifiable">
    <div class="questions">
      ${projectLine(d, when)}
      <div class="q-line"><span class="q-k">情况</span><span class="q-v">${esc(d.headline || d.problem || "")}</span></div>
      <div class="q-line"><span class="q-k">原因</span><span class="q-v">${esc(d.reason || "")}</span></div>
      <div class="q-line q-advice"><span class="q-k">建议</span><span class="q-v">${esc(d.suggestion || "")}</span></div>
    </div>
    <div class="d-act"><button onclick="toggleUnv()">${unvOpen ? "收起" : "看看是哪 " + rows.length + " 次"}</button>
      <span class="hint">不可验证不等于通过，也不算失败</span></div>`;
  if (!unvOpen) return head + "</article>";
  const list = rows.map(u => `<div class="unv-row">
      <span class="dim nowrap">${esc(ago(u.ended_at) || "时间没记")}</span>
      <span class="nowrap">${esc(whoName(u.agent))}</span>
      <span class="unv-title" title="${esc(u.title || "")}">${esc(u.title || "（任务没写标题）")}</span>
      <button onclick="rerunOne('${u.id}')">重跑</button>
    </div>`).join("");
  return head + `<div class="unv-list">${list || '<div class="empty">没有可展开的记录</div>'}</div></article>`;
}

function renderTodo() {
  const needs = (state.board || {}).needs_you || [];
  $("todo-n").textContent = needs.length ? needs.length + " 件" : "";
  $("todo-n").className = "count" + (needs.length ? " bad" : "");
  if (!needs.length) {
    $("todo").innerHTML = `<div class="empty-card">
      <span class="empty-icon">✓</span>
      <div><div class="empty-title">没有需要你决定的事</div>
        <div class="empty-sub">失败、重试、验收都是我在管：在跑的在「我在处理」，停下来的在「已停止」；
          你只需要决定合不合、花不花钱、往哪个方向走。</div></div>
    </div>`;
    return;
  }
  // Nothing is hidden by default: a board that says "4 件" and shows 3 rows is
  // a board whose count cannot be trusted. Past 8 the tail folds, and the fold
  // says how many are behind it.
  const LIMIT = 8;
  const shown = needs.slice(0, LIMIT), rest = needs.slice(LIMIT);
  const blocks = shown.map(decisionCard);
  if (rest.length) {
    blocks.push(`<div class="more-inline">还有 ${rest.length} 条
      <button onclick="toggleMoreNeeds()">${moreNeedsOpen ? "收起" : "展开"}</button></div>`);
    if (moreNeedsOpen) blocks.push(...rest.map(decisionCard));
  }
  $("todo").innerHTML = blocks.join("");
}

function renderHandling() {
  const rows = (state.board || {}).handling || [];
  $("handling-n").textContent = rows.length ? rows.length + " 件" : "";
  if (!rows.length) {
    $("handling").innerHTML = `<div class="empty-card">
      <span class="empty-icon wait">·</span>
      <div><div class="empty-title">现在没有在处理的失败</div>
        <div class="empty-sub">有失败我会自己重试，到点自己开跑；试到上限都不行才停下，
          把结论和证据写进「已停止」。</div></div>
    </div>`;
    return;
  }
  $("handling").innerHTML = rows.map(handlingCard).join("");
}

function renderStopped() {
  const rows = (state.board || {}).stopped || [];
  const fresh = rows.filter(d => !d.settled);
  const older = rows.filter(d => d.settled);
  const sec = $("stopped-sec");
  if (!rows.length) { sec.hidden = true; $("stopped").innerHTML = ""; return; }
  sec.hidden = false;
  $("stopped-n").textContent = rows.length + " 件";
  let html = fresh.map(stoppedCard).join("");
  if (older.length) {
    html += `<div class="more-inline">另有 ${older.length} 条更早的失败（已经重试过或太旧）
      <button onclick="toggleHistory()">${historyOpen ? "收起" : "展开"}</button></div>`
      + (historyOpen ? older.map(stoppedCard).join("") : "");
  }
  $("stopped").innerHTML = html;
}

function renderUnverifiable() {
  const rows = (state.board || {}).unverifiable || [];
  const sec = $("unv-sec");
  if (!rows.length) { sec.hidden = true; $("unv").innerHTML = ""; return; }
  sec.hidden = false;
  $("unv-n").textContent = unvList().length + " 个 run";
  $("unv").innerHTML = rows.map(unverifiableCard).join("");
}

let historyOpen = false;
function toggleHistory() { historyOpen = !historyOpen; renderStopped(); }

// Facts are harvested from the harnesses' own files; adopting appends them to
// the project's AGENTS.md. An empty list hides the whole section: "no pending
// memory" is not a task, and a line saying so would just be furniture.
function srcLabel(s) {
  // The daemon already shortened the path against the project (`AGENTS.md`,
  // `~/.codex/AGENTS.md`); the harness name is in the label or implied by it.
  return s.label || tailPath(s.path || "");
}

function renderMemory() {
  const rows = ((state.board || {}).memory_candidates || []).filter(c => !c.adopted);
  const sec = $("memory-sec");
  if (!rows.length) { sec.hidden = true; $("memory").innerHTML = ""; return; }
  sec.hidden = false;
  const conflicts = rows.filter(c => c.conflict).length;
  $("memory-n").textContent = rows.length + " 条" + (conflicts ? " · " + conflicts + " 条冲突" : "");
  const head = `<div class="mem-head"><span>这些是 harness 记忆文件里像指令的条目。项目来源采纳进项目 AGENTS.md，全局来源进 ~/.nodus/AGENTS.md，带日期和来源。</span>
    <button class="primary" onclick="adoptMemory(null)">全部采纳</button></div>`;
  const body = rows.map(c => `<div class="mem-row">
      <div><div class="mem-text">${esc(c.text)}</div>
        <div class="mem-src">${(c.sources || []).map(s => esc(srcLabel(s))).join(" · ")}</div></div>
      <div>${c.conflict ? '<span class="mem-flag" title="同主题下另有一条相反的说法，采纳前先看两条">冲突</span>' : ""}</div>
      <div><button onclick="adoptMemory('${esc(c.id)}')">采纳</button></div>
    </div>`).join("");
  $("memory").innerHTML = `<div class="mem-list">${head}${body}</div>`;
}

async function adoptMemory(id) {
  const pending = ((state.board || {}).memory_candidates || []).filter(c => !c.adopted);
  const ids = id ? [id] : pending.map(c => c.id);
  if (!ids.length) return;
  const what = id ? "采纳这一条？" : `采纳全部 ${ids.length} 条？`;
  if (!confirm(what + "\\n\\n项目来源写进项目 AGENTS.md，全局来源写进 ~/.nodus/AGENTS.md；重复的不会写第二遍。")) return;
  try {
    const r = await api("/memory/adopt", "POST", { ids });
    // A second click is not a failure: the API answers `already` instead of
    // erroring, and the person who clicked needs to see that nothing broke.
    if (r.added) flash("已采纳 " + r.added + " 条 → " + r.path);
    else if (r.already) flash("已采纳（这 " + r.already + " 条之前就写过了，没有重复写）");
    else flash("没有可采纳的条目");
    refresh();
  } catch (e) { flash("采纳失败：" + e.message, true); }
}

function renderMoney(t) {  t = t || {};
  const runs = t.runs || 0;
  const agents = t.by_agent || [];
  let html = "";
  if (!runs) {
    html = `<div class="money-total">今天还没有花钱</div>`;
  } else if (!Object.keys(t.totals || {}).length) {
    html = `<div class="money-total">今天 ${runs} 单，还没有记到金额</div>`;
  } else {
    html = `<div class="money-total">今天 ${runs} 单，共 <b>${moneyParts(t.totals)}</b></div>` +
      agents.map(a => {
        const amount = Object.keys(a.costs || {}).length ? moneyParts(a.costs) : "还没计价";
        return `<div class="money-row"><span>${esc(a.agent)}</span>
          <span class="amt">${esc(amount)}<span class="dim">（${a.runs} 单）</span></span></div>`;
      }).join("");
  }
  $("money").innerHTML = html;
}

function renderUsage(t) {
  t = t || {};
  const total = (t.tokens || {}).total || 0;
  const rows = t.by_route || [];
  const month = t.month || {};
  if (!total && !rows.length) {
    $("usage").innerHTML = `<div class="money-total">今天还没有用量记录</div>`;
    return;
  }
  let html = `<div class="money-total">今天 <b>${humanTokens(total)}</b> token` +
    `<span class="dim"> · 缓存命中 ${pct(t.cache_hit_rate)}</span></div>`;
  html += rows.map(r => `<div class="money-row"><span>${esc(r.route === "-" ? "未记录" : r.route)}</span>
    <span class="amt">${humanTokens(r.tokens)} token<span class="dim"> · ${r.runs} 次 · 缓存 ${pct(r.cache_hit_rate)} · ${moneyParts(r.cost)}</span></span></div>`).join("");
  if (month.runs) {
    html += `<div class="foot">本月累计 ${humanTokens((month.tokens || {}).total)} token · ` +
      `${moneyParts(month.cost)} · 缓存命中 ${pct(month.cache_hit_rate)}</div>`;
  }
  $("usage").innerHTML = html;
}

function renderRuns() {
  const rows = state.runs.slice(0, 12).map(r => {
    const u = r.usage || {};
    const [label, cls] = VERIFY[r.verify] || ["-", "dim"];
    const status = r.status === "completed" ? '<span class="ok">完成</span>'
      : r.status === "failed" ? '<span class="bad">失败</span>'
      : r.status === "running" ? '<span class="dim">在跑</span>' : esc(r.status);
    return `<tr>
      <td class="mono dim">${esc((r.id || "").slice(4, 14))}</td>
      <td class="nowrap">${status}</td>
      <td class="nowrap"><span class="${cls}">${label}</span></td>
      <td class="nowrap">${esc(r.route || r.agent)}<span class="dim"> → ${esc(r.provider || "-")}</span></td>
      <td class="nowrap">${runMoney(u)}</td>
      <td class="dim">${esc((r.title || r.note || r.task_id || "").slice(0, 50))}</td>
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
      <td><span class="dim">${esc(PSTATE[p.status] || p.status)}</span></td>
      <td>${esc(String(p.stat || "").replace(/(\\d+) file\\(s\\)/, "$1 个文件"))}</td>
      <td class="dim mono">${esc((p.branch || "").slice(0, 36))}</td></tr>`);
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
    html += `<div class="dim" style="margin-top:8px">改动文件：${files.length ? files.map(esc).join(" · ") : "（无）"}</div>
      <div class="dim">原始记录：${esc("/runs/" + rid + "/handoff")}</div>`;
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
    const ev = run.evidence || {};
    const lines = events.slice(-18).map(e =>
      clock(e.ts) + "  " + e.type + "  " + JSON.stringify(e.payload || {}));
    let extra = "";
    if (ev.action) extra += `<div class="dim">${esc(ev.action)}</div>`;
    if ((ev.tests || []).length) extra += `<div class="bad mono">${ev.tests.map(t => esc(t.name + (t.assertion ? " — " + t.assertion : ""))).join("<br>")}</div>`;
    if (ev.error_tail) extra += `<pre>${esc(ev.error_tail)}</pre>`;
    if (ev.change && (ev.change.files || []).length) {
      extra += `<div class="dim">改动：${ev.change.files.map(esc).join(" · ")}${ev.change.added != null ? `（+${ev.change.added} −${ev.change.removed}）` : ""}</div>`;
    }
    if ((ev.attempts || []).length) {
      extra += `<div class="dim">重试：${ev.attempts.map(a => `第${a.n}次 ${a.seconds || "?"}s ${a.outcome}`).map(esc).join(" / ")}</div>`;
    }
    box.innerHTML = `<h3>日志 <span class="mono dim">${esc(rid)}</span></h3>
      <div class="bad">${esc(run.problem || "这个 run 失败了。")}</div>
      ${extra}
      <div class="dim">谁干：${esc(run.agent || "-")} · 目录：<span class="mono">${esc(run.cwd || "-")}</span>
        · 开始：${esc(clock(run.started_at))}</div>
      ${lines.length ? `<pre>${esc(lines.join("\\n"))}</pre>` : ""}
      <div class="dim">共 ${events.length} 条事件，这里显示最后 ${lines.length} 条。</div>`;
  } catch (e) {
    box.innerHTML = `<h3>日志</h3><div class="bad">读不到日志：${esc(e.message)}</div>`;
  }
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function stopRun(id) {
  const r = state.runs.find(x => x.id === id) || state.board.running.find(x => x.id === id) || {};
  if (!confirm("停止 " + whoName(r.agent || r.route) + " 这个任务？已经改过的文件不会回滚。")) return;
  try { await api("/runs/" + id + "/stop", "POST"); flash("已请求停止"); }
  catch (e) { flash("停止失败：" + e.message, true); }
  refresh();
}
async function stopPipe(id) {
  if (!confirm("停止这个流水线？")) return;
  try { await api("/pipelines/" + id + "/stop", "POST"); flash("已请求停止"); }
  catch (e) { flash("停止失败：" + e.message, true); }
  refresh();
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
  refresh();
}
async function reject(id) {
  if (!confirm("丢弃这次改动？分支和工作区都会删掉，改动不会保留。")) return;
  try { await api("/proposals/" + id + "/reject", "POST"); flash("已丢弃"); }
  catch (e) { flash("丢弃失败：" + e.message, true); }
  refresh();
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
    refresh();
  } catch (e) { flash("重跑失败：" + e.message, true); }
}
function rerunOne(rid) {
  rerun(rid, "重跑这一次？");
}
function toggleUnv() {
  unvOpen = !unvOpen;
  renderUnverifiable();
}
function toggleMoreNeeds() {
  moreNeedsOpen = !moreNeedsOpen;
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
    renderHeader(); renderLive(); renderTodo(); renderHandling(); renderStopped();
    renderUnverifiable(); renderMemory(); renderMoney(board.today); renderUsage(board.today);
    renderRuns(); renderProposals();
  } catch (e) {
    $("todo").innerHTML = '<div class="empty bad">连不上 daemon：' + esc(e.message) + "</div>";
    $("live").innerHTML = "";
    $("money").innerHTML = "";
    $("usage").innerHTML = "";
  } finally {
    inflight = false;
  }
}

async function manualRefresh() {
  const b = $("refresh");
  b.disabled = true; b.textContent = "刷新中…";
  try { await refresh(); } finally { b.disabled = false; b.textContent = "刷新"; }
}

$("dispatch").addEventListener("submit", dispatch);
// Three ways the board updates: load, tab focus, the button. No timer, so an
// idle board costs nothing — the numbers in between are a snapshot.
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
loadRoutes(); refresh();
</script>
</body>
</html>
"""
