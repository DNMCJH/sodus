"""Decision helpers shared by routing and triage."""

from __future__ import annotations

from ... import config, decision
from ..client import call

TRIAGE_ADVICE = {0: "重跑或换 agent", 1: "需要人看", 2: "抽查关键文件", 3: "可接受"}


def choice(result: dict, key: str) -> tuple:
    ans = (result.get("answers") or {}).get(key) or {}
    return ans.get("choice"), ans.get("confidence", 0.0), ans.get("probabilities", {})


def score(result: dict, key: str) -> tuple:
    ans = (result.get("answers") or {}).get(key) or {}
    return ans.get("score"), ans.get("confidence", 0.0)


def add(sub) -> None:
    p = sub.add_parser("route", help="ask Jev which agent + effort")
    p.add_argument("task")
    p.set_defaults(func=route)

    p = sub.add_parser("triage", help="ask Jev how much attention a run needs")
    p.add_argument("run")
    p.set_defaults(func=triage)


def route(args) -> int:
    task_obj = call("GET", f"/tasks/{args.task}")
    ready = [a["name"] for a in call("GET", "/agents") if a["ready"]]
    result = decision.route_task(task_obj, ready)
    if not result.get("ok"):
        print(f"unavailable: {result.get('unavailable')}")
        return 1
    picked, conf, probs = choice(result, "route")
    if picked is None:
        picked, conf, probs = choice(result, "agent")
    effort, econf, _ = choice(result, "effort")
    print(f"{picked}  confidence={conf:.2f}  model={result.get('model')}")
    if effort:
        print(f"effort {effort}  confidence={econf:.2f}")
    for k, v in sorted((probs or {}).items(), key=lambda kv: -kv[1]):
        print(f"  {k:<8} {v:.2f}")
    return 0


def triage(args) -> int:
    run_obj = call("GET", f"/runs/{args.run}")
    task_obj = call("GET", f"/tasks/{run_obj['task_id']}")
    arts = call("GET", f"/runs/{args.run}/artifacts")
    final_path = config.HOME / "runs" / args.run / "final.md"
    final = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    result = decision.triage_run(run_obj, task_obj, final, arts)
    if not result.get("ok"):
        print(f"unavailable: {result.get('unavailable')}")
        return 1
    value, conf = score(result, "completion")
    advice = TRIAGE_ADVICE.get(round(value or 0), "?")
    usage = result.get("usage") or {}
    print(f"score {value}/3  confidence={conf:.2f}  → {advice}")
    print(
        f"model={result.get('model')}  {result.get('latency_ms')}ms  "
        f"in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}"
    )
    return 0
