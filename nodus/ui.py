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
from `diagnosis` (进程被外部杀掉 / 卡住超时 / 测试红 / 证据缺失 / …), and the
cause line is a rule-based guess quoting the line it came from. The raw evidence —
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
<meta name="color-scheme" content="light dark">
<script>(function(){try{var t=localStorage.getItem("nodus-theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);}catch(e){}})();</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sodus</title>
<style>

  /* ===================== 油彩纸本 · Nodus board (2026-10-25 v4) =====================
     Gouache on canvas paper: warm cream ground with a woven grain, broad oil-paint
     washes (persimmon / ochre / prussian / sage) behind the overview, serif titles,
     one restrained accent. Light = sunlit studio, dark = night studio with embers.
     Spacing scale: 8 / 12 / 16 / 24 / 32 / 40 (4 only inside inline chips). */
  :root {
    color-scheme: light;
    --s1:8px; --s2:12px; --s3:16px; --s4:24px; --s5:32px; --s6:40px;
    --r-lg:16px; --r-md:12px; --r-sm:8px;
    --bg:#efe7d8; --surface:#fbf7ef; --surface-2:#f4ede0; --sunk:#ece3d3; --glass:rgba(251,247,239,.74);
    --ink:#1d1f27; --ink-2:#3d404d; --ink-3:#686974; --ink-4:#8f8c85; --faint:#bfb7a8;
    --line:rgba(29,31,39,.12); --line-soft:rgba(29,31,39,.065); --line-strong:rgba(29,31,39,.24);
    --accent:#c4492f; --accent-ink:#fff8ef; --accent-soft:rgba(196,73,47,.09);
    --blue:#2d4c7c; --blue-soft:rgba(45,76,124,.08);
    --ok:#4c7a53; --ok-soft:rgba(76,122,83,.12);
    --warn:#a06c17; --warn-soft:rgba(168,114,26,.11);
    --bad:#b0362a; --bad-soft:rgba(176,54,42,.08);
    --paint-1:rgba(218,104,60,.55); --paint-2:rgba(230,174,74,.55);
    --paint-3:rgba(50,86,146,.42); --paint-4:rgba(112,148,104,.40);
    --glow-1:rgba(218,104,60,.16); --glow-2:rgba(50,86,146,.10);
    --grain:rgba(70,48,22,.045);
    --shadow:0 1px 0 rgba(255,255,255,.7) inset, 0 20px 40px -32px rgba(70,46,20,.55);
    --shadow-lift:0 1px 0 rgba(255,255,255,.7) inset, 0 24px 48px -28px rgba(70,46,20,.6);
    --serif:"Source Han Serif SC","Noto Serif SC","Songti SC","STSong","SimSun",Georgia,serif;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI","PingFang SC","Microsoft YaHei UI","Microsoft YaHei",sans-serif;
    --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg:#121319; --surface:#1b1c24; --surface-2:#20212b; --sunk:#15161d; --glass:rgba(27,28,36,.70);
    --ink:#ede7db; --ink-2:#cbc5b9; --ink-3:#9b968d; --ink-4:#77736d; --faint:#4b4947;
    --line:rgba(237,231,219,.11); --line-soft:rgba(237,231,219,.06); --line-strong:rgba(237,231,219,.24);
    --accent:#ef7d5a; --accent-ink:#1c100b; --accent-soft:rgba(239,125,90,.13);
    --blue:#8fa9e0; --blue-soft:rgba(143,169,224,.10);
    --ok:#8dc090; --ok-soft:rgba(141,192,144,.12);
    --warn:#e3b45e; --warn-soft:rgba(227,180,94,.11);
    --bad:#f0846a; --bad-soft:rgba(240,132,106,.11);
    --paint-1:rgba(214,92,52,.42); --paint-2:rgba(196,138,48,.28);
    --paint-3:rgba(72,104,206,.36); --paint-4:rgba(86,140,118,.24);
    --glow-1:rgba(214,92,52,.12); --glow-2:rgba(72,104,206,.12);
    --grain:rgba(255,255,255,.028);
    --shadow:0 1px 0 rgba(255,255,255,.035) inset, 0 20px 40px -28px rgba(0,0,0,.8);
    --shadow-lift:0 1px 0 rgba(255,255,255,.05) inset, 0 24px 48px -24px rgba(0,0,0,.9);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg:#121319; --surface:#1b1c24; --surface-2:#20212b; --sunk:#15161d; --glass:rgba(27,28,36,.70);
      --ink:#ede7db; --ink-2:#cbc5b9; --ink-3:#9b968d; --ink-4:#77736d; --faint:#4b4947;
      --line:rgba(237,231,219,.11); --line-soft:rgba(237,231,219,.06); --line-strong:rgba(237,231,219,.24);
      --accent:#ef7d5a; --accent-ink:#1c100b; --accent-soft:rgba(239,125,90,.13);
      --blue:#8fa9e0; --blue-soft:rgba(143,169,224,.10);
      --ok:#8dc090; --ok-soft:rgba(141,192,144,.12);
      --warn:#e3b45e; --warn-soft:rgba(227,180,94,.11);
      --bad:#f0846a; --bad-soft:rgba(240,132,106,.11);
      --paint-1:rgba(214,92,52,.42); --paint-2:rgba(196,138,48,.28);
      --paint-3:rgba(72,104,206,.36); --paint-4:rgba(86,140,118,.24);
      --glow-1:rgba(214,92,52,.12); --glow-2:rgba(72,104,206,.12);
      --grain:rgba(255,255,255,.028);
      --shadow:0 1px 0 rgba(255,255,255,.035) inset, 0 20px 40px -28px rgba(0,0,0,.8);
      --shadow-lift:0 1px 0 rgba(255,255,255,.05) inset, 0 24px 48px -24px rgba(0,0,0,.9);
    }
  }

  /* ---------- ground: cream canvas + woven grain ---------- */
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  html, body { margin:0; }
  html { background:var(--bg); }
  body {
    position:relative; isolation:isolate; min-height:100vh; color:var(--ink);
    font:14px/1.65 var(--sans); -webkit-font-smoothing:antialiased;
    background:var(--bg);
    transition:background-color .25s ease, color .25s ease;
  }
  body::before { content:""; position:fixed; inset:0; z-index:-1; pointer-events:none;
    background:
      radial-gradient(1200px 700px at -10% -20%, var(--glow-1), transparent 60%),
      radial-gradient(1000px 640px at 110% 10%, var(--glow-2), transparent 60%),
      repeating-linear-gradient(45deg, var(--grain) 0 1px, transparent 1px 6px),
      repeating-linear-gradient(-45deg, var(--grain) 0 1px, transparent 1px 7px),
      repeating-radial-gradient(circle at 23% 37%, var(--grain) 0 1px, transparent 1px 3px); }
  ::selection { background:var(--accent-soft); }
  button:focus-visible, summary:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline:2px solid var(--accent); outline-offset:2px; }

  /* ---------- header: seal + title, tools at the top-right ---------- */
  header { position:sticky; top:0; z-index:10; display:flex; align-items:center; gap:var(--s3);
           height:56px; padding:0 var(--s5); border-bottom:1px solid var(--line);
           background:color-mix(in srgb, var(--bg) 82%, transparent);
           backdrop-filter:blur(14px) saturate(1.2); -webkit-backdrop-filter:blur(14px) saturate(1.2); }
  .brand { display:flex; align-items:center; gap:var(--s2); flex:none; }
  .brand .mark { width:28px; height:28px; flex:none; display:flex; align-items:center; justify-content:center;
                 border-radius:7px; transform:rotate(-5deg); color:var(--accent-ink);
                 background:linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 60%, var(--warn)));
                 font:600 14px/1 var(--serif); box-shadow:0 6px 14px -8px var(--accent); }
  h1 { margin:0; font:600 15px/1 var(--serif); letter-spacing:.32em; color:var(--ink); }
  .tagline { color:var(--ink-4); font-size:12px; letter-spacing:.06em; white-space:nowrap; }
  .hmeta { display:flex; align-items:center; gap:var(--s1); min-width:0; }
  .hday { color:var(--ink-2); font-size:12px; font-variant-numeric:tabular-nums; letter-spacing:.04em;
          padding:0 var(--s1); border:1px solid var(--line); border-radius:999px; line-height:22px; white-space:nowrap; }
  .hprofile { color:var(--ink-4); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .rule { margin-left:auto; color:var(--ink-3); font:italic 13px/1.4 var(--serif); letter-spacing:.04em; white-space:nowrap;
          overflow:hidden; text-overflow:ellipsis; min-width:0; }
  header button { flex:none; background:transparent; }
  #theme-toggle { width:32px; padding:0; display:inline-flex; align-items:center; justify-content:center; border-radius:999px; }
  #theme-toggle svg { width:16px; height:16px; fill:none; stroke:currentColor; stroke-width:1.7;
                      stroke-linecap:round; stroke-linejoin:round; }
  #theme-toggle .i-sun { display:none; }
  :root[data-theme="dark"] #theme-toggle .i-sun { display:block; }
  :root[data-theme="dark"] #theme-toggle .i-moon { display:none; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) #theme-toggle .i-sun { display:block; }
    :root:not([data-theme="light"]) #theme-toggle .i-moon { display:none; }
  }

  /* ---------- shell ---------- */
  main { max-width:1240px; margin:0 auto; padding:var(--s5) var(--s5) calc(var(--s6) * 2); }

  /* overview: an oil-wash panel carrying four glass tiles */
  .hero { position:relative; overflow:hidden; display:grid; grid-template-columns:repeat(4, minmax(0,1fr));
          gap:var(--s3); padding:var(--s4); border-radius:var(--r-lg); border:1px solid var(--line);
          box-shadow:var(--shadow);
          background:
            radial-gradient(60% 120% at 0% 0%, var(--paint-1), transparent 62%),
            radial-gradient(50% 110% at 38% 110%, var(--paint-2), transparent 64%),
            radial-gradient(55% 120% at 100% 0%, var(--paint-3), transparent 62%),
            radial-gradient(40% 90% at 78% 100%, var(--paint-4), transparent 66%),
            var(--surface-2); }
  .hero::before { content:""; position:absolute; inset:0; pointer-events:none;
    background:
      repeating-linear-gradient(100deg, var(--grain) 0 2px, transparent 2px 9px),
      repeating-linear-gradient(10deg, var(--grain) 0 1px, transparent 1px 5px); }
  .kpi { position:relative; min-width:0; display:flex; flex-direction:column;
         padding:var(--s3) var(--s4) var(--s4); border-radius:var(--r-md);
         background:var(--glass); border:1px solid var(--line);
         backdrop-filter:blur(10px) saturate(1.1); -webkit-backdrop-filter:blur(10px) saturate(1.1);
         transition:transform .2s ease, box-shadow .2s ease; }
  .kpi:hover { transform:translateY(-2px); box-shadow:var(--shadow-lift); }
  .kpi-top { display:flex; align-items:center; gap:var(--s1); color:var(--ink-3);
             font-size:12px; letter-spacing:.14em; white-space:nowrap; }
  .kpi-dot { width:8px; height:8px; border-radius:99px; background:var(--faint); flex:none; }
  .kpi.ok .kpi-dot { background:var(--ok); box-shadow:0 0 0 3px var(--ok-soft); }
  .kpi.warn .kpi-dot { background:var(--warn); box-shadow:0 0 0 3px var(--warn-soft); }
  .kpi.bad .kpi-dot { background:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .kpi-value { margin-top:var(--s2); font:600 clamp(28px, 3.1vw, 40px)/1.1 var(--serif); letter-spacing:-.01em;
               color:var(--ink); font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .kpi.bad .kpi-value { color:var(--accent); }
  .kpi-sub { margin-top:var(--s1); color:var(--ink-3); font-size:12px;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  .cols { display:grid; grid-template-columns:minmax(0,1fr) 344px; gap:var(--s6); align-items:start; margin-top:var(--s6); }
  .col-main { min-width:0; display:flex; flex-direction:column; gap:var(--s6); counter-reset:sec; }
  .col-side { min-width:0; display:flex; flex-direction:column; gap:var(--s4); }
  .sec { margin:0; min-width:0; }
  .col-side .sec { padding:var(--s4); border-radius:var(--r-lg); background:var(--surface);
                   border:1px solid var(--line); box-shadow:var(--shadow); }

  .sec-head { display:flex; align-items:flex-end; justify-content:space-between; gap:var(--s3);
              padding-bottom:var(--s2); margin:0 0 var(--s3); border-bottom:1px solid var(--line); }
  .sec-title { min-width:0; display:flex; flex-direction:column; gap:4px; }
  h2 { margin:0; font:600 21px/1.3 var(--serif); letter-spacing:.04em; color:var(--ink);
       display:flex; align-items:baseline; gap:var(--s2); white-space:nowrap; }
  .col-main h2::before, details#more h2::before { counter-increment:sec; content:counter(sec, decimal-leading-zero);
       font:italic 500 13px/1 var(--serif); color:var(--accent); letter-spacing:.06em; }
  details#more { counter-reset:sec 4; }
  .col-side h2 { font-size:17px; }
  .sec-note { color:var(--ink-4); font-size:12px; line-height:1.6; overflow-wrap:anywhere; }
  .count { flex:none; color:var(--ink-2); font-size:12px; font-variant-numeric:tabular-nums; letter-spacing:.04em;
           padding:0 var(--s2); line-height:24px; border-radius:999px; background:var(--sunk); white-space:nowrap; }
  .count:empty { display:none; }
  .count.bad { color:var(--accent-ink); background:var(--accent); }
  .stack { display:flex; flex-direction:column; gap:var(--s3); }
  .col-side .stack { gap:0; }
  .empty { color:var(--ink-4); font-size:13px; padding:var(--s3) 0; }
  .empty-card { display:flex; gap:var(--s3); align-items:flex-start; padding:var(--s4);
                border:1px dashed var(--line-strong); border-radius:var(--r-md);
                background:color-mix(in srgb, var(--surface) 55%, transparent); }
  .empty-icon { width:32px; height:32px; flex:none; display:flex; align-items:center; justify-content:center;
                font-size:15px; color:var(--ok); border-radius:999px; background:var(--ok-soft); }
  .empty-icon.wait { color:var(--warn); background:var(--warn-soft); }
  .empty-title { font:600 16px/1.4 var(--serif); color:var(--ink); letter-spacing:.02em; }
  .empty-sub { margin-top:4px; color:var(--ink-3); font-size:13px; line-height:1.7; }

  /* ---------- cards: paper sheets with a soft top wash per voice (no side bars) ---------- */
  .card { position:relative; padding:var(--s4); border-radius:var(--r-lg); border:1px solid var(--line);
          background:var(--surface); box-shadow:var(--shadow); transition:box-shadow .2s ease, border-color .2s ease; }
  .card:hover { box-shadow:var(--shadow-lift); border-color:var(--line-strong); }
  .card.decision { background:linear-gradient(180deg, var(--accent-soft), transparent 140px), var(--surface); }
  .card.handling { background:linear-gradient(180deg, var(--warn-soft), transparent 140px), var(--surface); }
  .card.stopped { background:linear-gradient(180deg, var(--bad-soft), transparent 140px), var(--surface); }
  .col-side .card, .col-side .card:hover { padding:0; border:0; border-radius:0; background:transparent; box-shadow:none; }
  .col-side .card + .card { margin-top:var(--s3); padding-top:var(--s3); border-top:1px solid var(--line-soft); }
  .col-side .q-line { grid-template-columns:40px minmax(0,1fr); gap:var(--s2); }
  .questions { display:grid; gap:var(--s2); }
  .q-line { display:grid; grid-template-columns:64px minmax(0,1fr); gap:var(--s3); font-size:13.5px; line-height:1.7; }
  .q-k { color:var(--ink-4); font-size:12px; letter-spacing:.08em; padding-top:2px; white-space:nowrap; }
  .q-v { color:var(--ink-2); min-width:0; overflow-wrap:anywhere; }
  .q-project { padding-bottom:var(--s2); border-bottom:1px solid var(--line-soft); }
  .q-project .q-v { display:flex; align-items:baseline; flex-wrap:wrap; gap:4px var(--s1); color:var(--ink); }
  .q-project .q-v b { font:600 16px/1.4 var(--serif); letter-spacing:.02em; }
  .q-project .d-when { margin-left:auto; }
  .q-ask .q-k { color:var(--accent); }
  .q-ask .q-v { font:600 15.5px/1.6 var(--serif); color:var(--ink); }
  .q-tag { display:inline-block; margin-right:var(--s1); padding:0 var(--s1); line-height:20px; border-radius:999px;
           background:var(--sunk); color:var(--ink-3); font-size:11.5px; letter-spacing:.04em; white-space:nowrap; }
  .q-tag.bad { color:var(--bad); background:var(--bad-soft); }
  .q-tag.na { color:var(--warn); background:var(--warn-soft); }
  .q-advice .q-v { color:var(--ink); }
  .d-when { color:var(--ink-4); font-size:12px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .d-path { color:var(--ink-4); font-size:12px; overflow-wrap:anywhere; }
  .d-act { display:flex; align-items:center; gap:var(--s1); flex-wrap:wrap; margin-top:var(--s3); }
  .d-act .hint { color:var(--ink-4); font-size:12px; margin-left:var(--s1); }
  .evidence { margin-top:var(--s2); display:grid; gap:var(--s2); }
  .ev-row { display:grid; grid-template-columns:56px minmax(0,1fr); gap:var(--s3); font-size:12.5px; line-height:1.65; }
  .ev-k { color:var(--ink-4); font-size:12px; letter-spacing:.06em; padding-top:1px; white-space:nowrap; }
  .ev-v { color:var(--ink-2); min-width:0; overflow-wrap:anywhere; }
  .ev-v.na { color:var(--warn); }
  .ev-src { color:var(--ink-4); font-size:11.5px; margin-top:4px; }
  .tests { display:grid; gap:4px; }
  .test { display:flex; gap:var(--s1); align-items:baseline; flex-wrap:wrap; }
  .test-name { font-family:var(--mono); font-size:11.5px; color:var(--bad); overflow-wrap:anywhere; }
  .test-assert { font-family:var(--mono); font-size:11.5px; color:var(--ink-2); }
  pre.tail { margin:var(--s1) 0 0; padding:var(--s2); background:var(--sunk); border-radius:var(--r-sm);
             color:var(--ink-2); white-space:pre-wrap; word-break:break-word;
             font:11.5px/1.6 var(--mono); max-height:176px; overflow:auto; }
  .attempts { display:flex; gap:var(--s1); flex-wrap:wrap; }
  .attempt { display:inline-flex; align-items:baseline; gap:4px; padding:0 var(--s1); line-height:22px; border-radius:999px;
             background:var(--sunk); color:var(--ink-3); font-size:11.5px; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .attempt b { font-weight:600; color:var(--ink); }
  .attempt.red { color:var(--bad); background:var(--bad-soft); }
  .attempt.warn { color:var(--warn); background:var(--warn-soft); }
  .attempt.ok { color:var(--ok); background:var(--ok-soft); }

  .live-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:var(--s3); align-items:center;
              padding:var(--s3) var(--s4); border-radius:var(--r-md); border:1px solid var(--line);
              background:var(--surface); box-shadow:var(--shadow); }
  .live-what { color:var(--ink); font-size:14px; overflow-wrap:anywhere; }
  .live-what b { font:600 15px/1.5 var(--serif); }
  .live-what .status-dot { margin-right:4px; vertical-align:middle; position:relative; top:-1px; }
  .live-sub { color:var(--ink-4); font-size:12px; margin-top:4px; overflow-wrap:anywhere; }
  .act { display:flex; gap:var(--s1); align-items:center; }
  .more-inline { display:flex; align-items:center; gap:var(--s2); flex-wrap:wrap;
                 color:var(--ink-3); font-size:13px; padding:var(--s1) 0 0; }
  .more-inline button { height:28px; }

  /* ---------- rail ---------- */
  .money-total { color:var(--ink-3); font-size:12.5px; padding:0 0 var(--s2); font-variant-numeric:tabular-nums;
                 overflow-wrap:anywhere; }
  .money-total b { font:600 28px/1.2 var(--serif); color:var(--ink); margin:0 4px; letter-spacing:0; }
  .money-row { display:grid; grid-template-columns:minmax(0,88px) minmax(24px,1fr); gap:4px var(--s2);
               align-items:center; padding:var(--s1) 0; border-top:1px solid var(--line-soft);
               font-variant-numeric:tabular-nums; }
  .money-name { color:var(--ink-2); font-size:12.5px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .money-track { height:6px; border-radius:99px; background:var(--sunk); overflow:hidden; }
  .money-track i { display:block; height:100%; border-radius:99px;
                   background:linear-gradient(90deg, var(--accent), var(--warn)); }
  .sec-usage .money-track i { background:linear-gradient(90deg, var(--blue), var(--ok)); }
  .money-row .amt { grid-column:1 / -1; color:var(--ink); font-size:12px; overflow-wrap:anywhere; }
  .foot { color:var(--ink-4); font-size:12px; padding-top:var(--s2); border-top:1px solid var(--line); margin-top:var(--s1); line-height:1.7; }
  /* ---------- terminals: the five harnesses, one row each, same shape ---------- */
  .term-row { padding:var(--s2) 0; border-top:1px solid var(--line-soft); }
  .term-row:first-child { border-top:0; padding-top:0; }
  .term-row:last-child { padding-bottom:0; }
  .term-top { display:flex; align-items:baseline; gap:var(--s1); min-width:0; }
  .term-name { font:600 13px/1.5 var(--serif); letter-spacing:.04em; color:var(--ink);
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .term-state { font-size:11px; letter-spacing:.04em; padding:0 var(--s1); line-height:18px;
                border-radius:999px; background:var(--sunk); color:var(--ink-4); white-space:nowrap; flex:none; }
  .term-state.ok { color:var(--ok); background:var(--ok-soft); }
  .term-state.missing { color:var(--ink-4); background:transparent; border:1px dashed var(--line-strong); }
  .term-when { margin-left:auto; color:var(--ink-4); font-size:12px; white-space:nowrap;
               font-variant-numeric:tabular-nums; }
  .term-sub { margin-top:1px; color:var(--ink-4); font-size:11.5px; line-height:1.7;
              overflow-wrap:anywhere; }
  .term-sub b { color:var(--ink-2); font-weight:500; font-variant-numeric:tabular-nums; }
  .mem-list { max-height:480px; overflow:auto; }
  .mem-head { position:sticky; top:0; z-index:1; display:flex; align-items:center; gap:var(--s2);
              padding:0 0 var(--s2); border-bottom:1px solid var(--line); background:var(--surface);
              color:var(--ink-4); font-size:12px; line-height:1.6; }
  .mem-head button { margin-left:auto; flex:none; height:28px; }
  .mem-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:var(--s2); align-items:center;
             padding:var(--s2) 0; border-bottom:1px solid var(--line-soft); }
  .mem-row:last-child { border-bottom:0; }
  .mem-text { color:var(--ink-2); font-size:13px; line-height:1.65; overflow-wrap:anywhere; }
  .mem-src { color:var(--ink-4); font-size:11.5px; margin-top:4px; }
  .mem-flag { color:var(--warn); background:var(--warn-soft); border-radius:999px; padding:0 var(--s1); line-height:20px;
              font-size:11.5px; white-space:nowrap; }
  .mem-row button { height:28px; }
  .unv-list { margin-top:var(--s3); padding-top:var(--s1); border-top:1px dashed var(--line-strong); max-height:336px; overflow:auto; }
  .unv-row { display:flex; gap:var(--s2); align-items:center; padding:var(--s1) 0; font-size:12.5px;
             border-bottom:1px solid var(--line-soft); }
  .unv-row:last-child { border-bottom:0; }
  .unv-title { color:var(--ink-2); min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .unv-row button { margin-left:auto; flex:none; height:28px; }
  .nowrap { white-space:nowrap; }

  /* ---------- folds ---------- */
  details summary { color:var(--ink-3); cursor:pointer; font-size:12.5px; list-style:none; }
  details summary::-webkit-details-marker { display:none; }
  details summary:hover { color:var(--accent); }
  details.more { margin-top:var(--s3); padding-top:var(--s2); border-top:1px dashed var(--line-strong); }
  details.more > summary { display:inline-flex; align-items:center; gap:var(--s1); }
  details.more > summary::before { content:""; width:6px; height:6px; border-right:1.5px solid currentColor;
                                   border-bottom:1.5px solid currentColor; transform:rotate(-45deg); transition:transform .2s ease; }
  details.more[open] > summary::before { transform:rotate(45deg); }
  .evidence details.raw { margin-top:var(--s1); }
  .evidence details.raw summary { font-size:12px; color:var(--ink-4); }
  .evidence details.raw summary::before { content:"+"; display:inline-block; width:12px; }
  .evidence details.raw[open] summary::before { content:"–"; }

  details#more { margin-top:var(--s6); }
  details#more > summary { display:inline-flex; align-items:center; gap:var(--s1); height:40px; padding:0 var(--s4);
                           border-radius:999px; border:1px solid var(--line-strong); background:var(--surface);
                           color:var(--ink-2); font-size:13px; letter-spacing:.04em; box-shadow:var(--shadow); }
  details#more > summary::before { content:"+"; color:var(--accent); font-size:16px; line-height:1; width:12px; text-align:center; }
  details#more[open] > summary::before { content:"−"; }
  details#more > section { margin-top:var(--s4); padding:var(--s4); border-radius:var(--r-lg);
                           background:var(--surface); border:1px solid var(--line); box-shadow:var(--shadow); }
  #runs, #proposals { overflow-x:auto; }
  #runs table, #proposals table { min-width:640px; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:var(--s2); border-bottom:1px solid var(--line-soft); vertical-align:top; }
  th { color:var(--ink-4); font-weight:500; font-size:11.5px; letter-spacing:.1em; padding-top:0; }
  td { font-size:12.5px; color:var(--ink-2); }
  tbody tr:last-child td { border-bottom:0; }
  td.nowrap, th.nowrap { white-space:nowrap; }
  tbody tr:hover td { background:var(--surface-2); }
  td button { height:28px; }
  #evidence { display:none; margin-top:var(--s3); padding:var(--s4); background:var(--surface-2);
              border:1px solid var(--line); border-radius:var(--r-md); }
  #evidence > * + * { margin-top:var(--s1); }
  #evidence h3 { margin:0 0 var(--s2); font:600 16px/1.4 var(--serif); letter-spacing:.03em; }
  #evidence pre { white-space:pre-wrap; word-break:break-word; max-height:320px; overflow:auto; margin:var(--s1) 0;
                  background:var(--sunk); border-radius:var(--r-sm); padding:var(--s2);
                  font:11.5px/1.6 var(--mono); color:var(--ink-2); }

  /* ---------- form ---------- */
  form { display:grid; gap:var(--s3); max-width:760px; }
  label { display:grid; gap:var(--s1); font-size:12.5px; color:var(--ink-3); }
  input, select, textarea { width:100%; background:var(--surface); color:var(--ink);
                            border:1px solid var(--line-strong); border-radius:var(--r-sm); padding:var(--s1) var(--s2);
                            font:inherit; font-size:13px; transition:border-color .16s, box-shadow .16s; }
  input, select { height:40px; }
  input::placeholder, textarea::placeholder { color:var(--ink-4); }
  textarea { min-height:88px; resize:vertical; line-height:1.6; }
  input:focus, select:focus, textarea:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:var(--s3); }
  label.check { display:flex; gap:var(--s1); align-items:flex-start; color:var(--ink-2); font-size:13px; line-height:1.6; }
  .check input { width:16px; height:16px; margin:3px 0 0; padding:0; flex:none; accent-color:var(--accent); }
  form > div:last-child { display:flex; align-items:center; gap:var(--s2); flex-wrap:wrap; }
  form button[type="submit"] { height:40px; padding:0 var(--s4); border-color:var(--accent); background:var(--accent); color:var(--accent-ink); }
  form button[type="submit"]:hover { background:color-mix(in srgb, var(--accent) 86%, #000); border-color:var(--accent); color:var(--accent-ink); }

  #flash { position:fixed; left:50%; bottom:var(--s4); transform:translateX(-50%); display:none;
           max-width:min(80vw, 560px); padding:var(--s2) var(--s4); z-index:20; font-size:13px;
           background:var(--surface); color:var(--ink); border:1px solid var(--line-strong); border-radius:var(--r-md);
           box-shadow:var(--shadow-lift); }
  .mono { font-family:var(--mono); font-size:11.5px; }
  .dim { color:var(--ink-3); }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  .status-dot { width:8px; height:8px; border-radius:99px; display:inline-block; background:var(--faint); flex:none; }
  .status-dot.ok { background:var(--ok); animation:breathe 2.4s ease-in-out infinite; }
  .status-dot.bad { background:var(--accent); } .status-dot.warn { background:var(--warn); }
  @keyframes breathe { 0%,100% { box-shadow:0 0 0 0 var(--ok-soft); } 50% { box-shadow:0 0 0 5px var(--ok-soft); } }
  .sep { color:var(--faint); margin:0 var(--s1); }

  /* ---------- controls ---------- */
  button { appearance:none; height:32px; padding:0 var(--s3); border-radius:var(--r-sm); border:1px solid var(--line-strong);
           background:var(--surface); color:var(--ink-2); font:inherit; font-size:13px; letter-spacing:.02em;
           cursor:pointer; white-space:nowrap; transition:background .16s, border-color .16s, color .16s, transform .16s; }
  button:hover { color:var(--ink); border-color:var(--ink-3); background:var(--surface-2); }
  button:active { transform:translateY(1px); }
  button:disabled { opacity:.6; cursor:progress; }
  button.primary { border-color:var(--accent); color:var(--accent-ink); background:var(--accent); }
  button.primary:hover { background:color-mix(in srgb, var(--accent) 86%, #000); color:var(--accent-ink); border-color:var(--accent); }
  button.danger { color:var(--bad); border-color:color-mix(in srgb, var(--bad) 40%, transparent); background:transparent; }
  button.danger:hover { background:var(--bad-soft); border-color:var(--bad); color:var(--bad); }

  /* ---------- scrollbars / motion ---------- */
  * { scrollbar-width:thin; scrollbar-color:var(--line-strong) transparent; }
  *::-webkit-scrollbar { width:8px; height:8px; }
  *::-webkit-scrollbar-thumb { background:var(--line-strong); border-radius:99px; border:2px solid transparent; background-clip:content-box; }
  *::-webkit-scrollbar-track { background:transparent; }
  @keyframes rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
  .hero, .col-main > .sec, .col-side > .sec, details#more { animation:rise .5s ease both; }
  .col-main > .sec:nth-child(2) { animation-delay:.05s; }
  .col-main > .sec:nth-child(3) { animation-delay:.1s; }
  .col-side > .sec:nth-child(n+2) { animation-delay:.08s; }
  details[open] > :not(summary) { animation:rise .3s ease both; }
  @media (prefers-reduced-motion: reduce) { * { animation:none !important; transition:none !important; } }

  /* ---------- responsive: two columns, then rail as a 2-up grid, then one column ---------- */
  @media (max-width:1100px) {
    .cols { grid-template-columns:minmax(0,1fr); }
    .col-side { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); align-items:start; }
    .hero { grid-template-columns:repeat(2, minmax(0,1fr)); }
  }
  @media (max-width:860px) {
    header { padding:0 var(--s3); gap:var(--s2); }
    .tagline, .hprofile, .rule { display:none; }
    #refresh { margin-left:auto; }
    main { padding:var(--s4) var(--s3) var(--s6); }
    .col-side { display:flex; }
    .cols { margin-top:var(--s5); gap:var(--s5); }
    .col-main { gap:var(--s5); }
    details#more { margin-top:var(--s5); }
  }
  @media (max-width:640px) {
    body { font-size:13.5px; }
    .hero { padding:var(--s3); gap:var(--s2); }
    .kpi { padding:var(--s2) var(--s3) var(--s3); }
    .kpi-value { font-size:28px; }
    .card, .col-side .sec, details#more > section { padding:var(--s3); }
    .col-side .card { padding:0; }
    .live-row { padding:var(--s2) var(--s3); grid-template-columns:minmax(0,1fr); gap:var(--s2); }
    .sec-head { flex-wrap:wrap; }
    h2 { white-space:normal; font-size:19px; }
    .row { grid-template-columns:1fr; }
    .q-line { grid-template-columns:52px minmax(0,1fr); gap:var(--s2); }
    .ev-row { grid-template-columns:48px minmax(0,1fr); gap:var(--s2); }
    .q-project .d-when { margin-left:0; }
    .q-project .sep { display:none; }
  }
  @media (max-width:380px) {
    .hero { grid-template-columns:minmax(0,1fr); }
  }

</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark">验</span><h1>SODUS</h1><span class="tagline">一纸验收 · 本机工作台</span></div>
  <div class="hmeta"><span class="hday" id="s-day">—</span><span class="hprofile" id="s-profile"></span></div>
  <div class="rule">结束 ≠ 通过 · 只有测试证据变绿才算验收</div>
  <button id="refresh" onclick="manualRefresh()" title="重新拉取 /board。页面不会自己刷新。">刷新</button>
  <button id="theme-toggle" onclick="toggleTheme()" title="切换亮/暗主题" aria-label="切换亮/暗主题"><svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/></svg><svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>
</header>
<main>
  <section class="hero" aria-label="概览">
    <article class="kpi" id="kpi-live">
      <div class="kpi-top"><span class="kpi-dot"></span>运行中</div>
      <div class="kpi-value" id="k-live">—</div>
      <div class="kpi-sub" id="k-live-sub">读状态…</div>
    </article>
    <article class="kpi" id="kpi-wait">
      <div class="kpi-top"><span class="kpi-dot"></span>等你决定</div>
      <div class="kpi-value" id="k-wait">—</div>
      <div class="kpi-sub" id="k-wait-sub">读状态…</div>
    </article>
    <article class="kpi" id="kpi-cost">
      <div class="kpi-top"><span class="kpi-dot"></span>今天花费</div>
      <div class="kpi-value" id="k-cost">—</div>
      <div class="kpi-sub" id="k-cost-sub">账单原值</div>
    </article>
    <article class="kpi" id="kpi-retry">
      <div class="kpi-top"><span class="kpi-dot"></span>失败在重试</div>
      <div class="kpi-value" id="k-retry">—</div>
      <div class="kpi-sub" id="k-retry-sub">机器自己处理</div>
    </article>
  </section>
  <div class="cols">
    <div class="col-main">
      <section class="sec sec-live">
        <div class="sec-head"><div class="sec-title"><h2>正在跑</h2></div><span class="count" id="live-n"></span></div>
    <div id="live" class="stack"><div class="empty">现在没有任务在跑</div></div>
  </section>
      <section class="sec sec-decision">
        <div class="sec-head"><div class="sec-title"><h2>需要你决定</h2><span class="sec-note">只有三件事要你定：合不合、花不花钱、往哪个方向走</span></div><span class="count" id="todo-n"></span></div>
    <div id="todo" class="stack"><div class="empty">正在读…</div></div>
  </section>
      <section id="handling-sec" class="sec sec-working">
    <div class="sec-head"><div class="sec-title"><h2>我在处理</h2><span class="sec-note">失败、重试、验收都是我这边的事，不用你动手</span></div><span class="count" id="handling-n"></span></div>
    <div id="handling" class="stack"><div class="empty">正在读…</div></div>
  </section>
      <section id="stopped-sec" class="sec sec-stopped" hidden>
    <div class="sec-head"><div class="sec-title"><h2>已停止</h2><span class="sec-note">试到上限我就停：做了什么、结论是什么、证据在哪</span></div><span class="count" id="stopped-n"></span></div>
    <div id="stopped" class="stack"></div>
  </section>
    </div>
    <aside class="col-side">
      <section id="unv-sec" class="sec sec-unv" hidden>
    <div class="sec-head"><div class="sec-title"><h2>不可验证</h2><span class="sec-note">跑完了但没有测试证据：不算通过，也不算失败</span></div><span class="count" id="unv-n"></span></div>
    <div id="unv" class="stack"></div>
  </section>
      <section id="memory-sec" class="sec sec-memory" hidden>
    <div class="sec-head"><div class="sec-title"><h2>记忆待审</h2><span class="sec-note">从 harness 记忆文件抽出来的指令，采纳后写进 AGENTS.md（带日期和来源）</span></div><span class="count" id="memory-n"></span></div>
    <div id="memory"></div>
  </section>
      <section class="sec sec-money">
        <div class="sec-head"><div class="sec-title"><h2>今天的钱</h2><span class="sec-note">账单原值，不换算汇率</span></div></div>
    <div id="money"></div>
  </section>
      <section class="sec sec-usage">
        <div class="sec-head"><div class="sec-title"><h2>用量</h2><span class="sec-note">token 与缓存命中，按路由</span></div></div>
    <div id="usage"></div>
  </section>
      <section class="sec sec-terminal">
        <div class="sec-head"><div class="sec-title"><h2>终端</h2><span class="sec-note">五个 harness 一样对待：装没装、在不在跑、今天干了多少</span></div><span class="count" id="terminal-n"></span></div>
    <div id="terminals"></div>
  </section>
    </aside>
  </div>
  <details id="more">
    <summary>更多：最近执行 · 提案 · 派活</summary>
    <section>
      <div class="sec-head"><div class="sec-title"><h2>最近执行</h2><span class="sec-note">点「证据」看测试命令、退出码和改了哪些文件</span></div></div>
      <div id="runs"></div>
      <div id="evidence"></div>
    </section>
    <section>
      <div class="sec-head"><div class="sec-title"><h2>提案（在分支上，主工作区没动）</h2></div></div>
      <div id="proposals"></div>
    </section>
    <section>
      <div class="sec-head"><div class="sec-title"><h2>派活</h2></div></div>
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
      t.includes("bad-request") || t.includes("model-missing") || t.includes("content-policy") ||
      t.includes("orphan") || t.includes("failed")) return "red";
  if (t.includes("timeout") || t.includes("network") || t.includes("rate") || t.includes("5xx")) return "warn";
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
  $("s-day").textContent = b.day || "—";
  $("s-profile").textContent = b.profile ? ("profile · " + b.profile) : "";
  $("k-live").textContent = live;
  $("k-wait").textContent = waiting;
  $("k-cost").textContent = cost;
  $("k-retry").textContent = retrying;
  $("k-live-sub").textContent = pipes ? pipes + " 条流水线在跑" : (live ? "任务进行中" : "现在没有任务在跑");
  $("k-wait-sub").textContent = waiting ? "合不合 · 花不花钱 · 往哪走" : "没有要你定的事";
  $("k-cost-sub").textContent = runs ? runs + " 单 · 账单原值" : "今天还没花钱";
  $("k-retry-sub").textContent = retrying ? "机器在重试，不用你动手" : "没有失败在重试";
  const tone = (id, cls) => { const el = $(id); if (el) el.className = "kpi" + (cls ? " " + cls : ""); };
  tone("kpi-live", live ? "ok" : "");
  tone("kpi-wait", waiting ? "bad" : "ok");
  tone("kpi-cost", "");
  tone("kpi-retry", retrying ? "warn" : "ok");
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
// Tone only: the words themselves come from `diagnosis` on the server. These
// are the labels for a red test, a request that must change, and a process
// death; a transient cause keeps the neutral tone.
const KIND_TONE = new Set([
  "测试红", "密钥无效或没有权限", "模型名不对", "请求被拒绝",
  "内容被安全策略拦截", "进程被外部杀掉", "进程自己退出",
]);
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
      <span class="sep">·</span>${esc(d.task_title || "（任务没写标题）")}
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
  const head = `<div class="mem-head"><span>采纳后写进对应 AGENTS.md，带日期和来源；重复的不会写第二遍。</span>
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
        const share = runs ? Math.max(2, Math.round(((a.runs || 0) / runs) * 100)) : 0;
        return `<div class="money-row"><span class="money-name">${esc(a.agent)}</span>
          <span class="money-track"><i style="width:${share}%"></i></span>
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
  const max = Math.max(1, ...rows.map(r => r.tokens || 0));
  let html = `<div class="money-total">今天 <b>${humanTokens(total)}</b> token` +
    `<span class="dim"> · 缓存命中 ${pct(t.cache_hit_rate)}</span></div>`;
  html += rows.map(r => {
    const share = Math.max(2, Math.round(((r.tokens || 0) / max) * 100));
    return `<div class="money-row"><span class="money-name">${esc(r.route === "-" ? "未记录" : r.route)}</span>
    <span class="money-track"><i style="width:${share}%"></i></span>
    <span class="amt">${humanTokens(r.tokens)} token<span class="dim"> · ${r.runs} 次 · 缓存 ${pct(r.cache_hit_rate)} · ${moneyParts(r.cost)}</span></span></div>`;
  }).join("");
  if (month.runs) {
    html += `<div class="foot">本月累计 ${humanTokens((month.tokens || {}).total)} token · ` +
      `${moneyParts(month.cost)} · 缓存命中 ${pct(month.cache_hit_rate)}</div>`;
  }
  $("usage").innerHTML = html;
}

// 终端: the five harnesses that exist on this machine, one row each. Presence
// is each CLI's own session-log freshness plus a live Nodus run; nothing here
// starts a process or calls a CLI. 一视同仁 is the point -- DSH and Kimi get
// the same fields, the same words and the same width.
const TERM_STATE = {
  running: ["运行中", "ok"],
  idle: ["空闲", ""],
  missing: ["未安装", "missing"],
};
function renderTerminals(rows) {
  rows = rows || [];
  const busy = rows.filter(t => t.state === "running").length;
  $("terminal-n").textContent = busy ? busy + " 个在跑" : "";
  if (!rows.length) {
    $("terminals").innerHTML = '<div class="empty">没探测到终端</div>';
    return;
  }
  $("terminals").innerHTML = rows.map(t => {
    const pair = TERM_STATE[t.state] || TERM_STATE.idle;
    const today = t.today || {};
    const runs = today.runs || 0;
    const sessions = today.sessions || 0;
    const priced = Object.keys(today.cost || {}).length > 0;
    const cost = priced ? moneyParts(today.cost) : "—";
    let detail;
    if (t.state === "missing") detail = "没装这个 CLI";
    else if (runs) detail = '今日 <b>' + esc(cost) + '</b><span class="sep">·</span><b>' + runs + "</b> 单" +
      (sessions ? '<span class="sep">·</span><b>' + sessions + "</b> 会话" : "");
    else if (priced) detail = '今日 <b>' + esc(cost) + "</b>（只有聊天账单，不算工作）";
    else detail = t.state === "running" ? "正在跑，今天还没派活" : "今天还没动过";
    const when = t.last_active ? ago(t.last_active) : (t.state === "missing" ? "—" : "没有日志");
    const hint = "日志：" + (t.log_root || "未找到") +
      (t.binary ? "；CLI：" + t.binary : "") +
      (t.last_active ? "；最近写日志：" + clock(t.last_active) : "") +
      "；今日工作 " + runs + " 单 · 会话 " + sessions + " · 花费含闲聊";
    return '<div class="term-row" title="' + esc(hint) + '">' +
      '<div class="term-top"><span class="term-name">' + esc(t.name) + "</span>" +
        '<span class="term-state ' + pair[1] + '">' + pair[0] + "</span>" +
        '<span class="term-when">' + esc(when) + "</span></div>" +
      '<div class="term-sub">' + detail + "</div></div>";
  }).join("");
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
    renderTerminals(board.terminals);
    renderRuns(); renderProposals();
  } catch (e) {
    $("todo").innerHTML = '<div class="empty bad">连不上 daemon：' + esc(e.message) + "</div>";
    $("live").innerHTML = "";
    $("money").innerHTML = "";
    $("usage").innerHTML = "";
    $("terminals").innerHTML = "";
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
<script>
function toggleTheme() {
  var root = document.documentElement;
  var explicit = root.getAttribute("data-theme");
  var current = explicit || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  var next = current === "dark" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  try { localStorage.setItem("nodus-theme", next); } catch (e) {}
}
</script>
</body>
</html>
"""
