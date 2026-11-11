
import json, os, random, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(r"A:\VScode\Code\Projects\sodus")
PY = r"A:\Anaconda\python.exe"
BENCH = Path(tempfile.mkdtemp(prefix="sodus-model-bench-"))
OUT = ROOT / "reports" / "bench-fable-opus-sol-20261110.jsonl"
OUT.parent.mkdir(exist_ok=True)

TASKS = {
    "lru": {
        "prompt": "在当前目录实现 lru.py，提供一个按最近最少使用淘汰的 LRUCache 类，使 test_lru.py 全部通过。不要修改测试文件。实现后运行 pytest 验证。",
        "test": '''from lru import LRUCache\n\ndef test_basic():\n    c = LRUCache(2)\n    c.put(1, 1)\n    c.put(2, 2)\n    assert c.get(1) == 1\n    c.put(3, 3)\n    assert c.get(2) is None\n    assert c.get(1) == 1\n\ndef test_update():\n    c = LRUCache(2)\n    c.put(1, 1)\n    c.put(2, 2)\n    c.put(1, 10)\n    assert c.get(2) == 2\n    c.put(3, 3)\n    assert c.get(1) == 10\n    assert c.get(2) is None\n''',
    },
    "merge": {
        "prompt": "在当前目录实现 merge_intervals.py，提供 merge(intervals) 合并所有重叠或相邻的闭区间，使 test_merge.py 全部通过。不要修改测试文件。实现后运行 pytest 验证。",
        "test": '''from merge_intervals import merge\n\ndef test_basic():\n    assert merge([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\n\ndef test_touch():\n    assert merge([[1,4],[4,5]]) == [[1,5]]\n\ndef test_empty():\n    assert merge([]) == []\n''',
    },
}

CONFIGS = [
    ("fable-high", "fable", "high"),
    ("fable-xhigh", "fable", "xhigh"),
    ("opus-max", "opus", "max"),
    ("sol", "sol", ""),
]
REPS = 2

def seed(task, d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "pyproject.toml").write_text("[project]\nname = 'bench'\nversion = '0.1.0'\n", encoding="utf-8")
    name = "test_lru.py" if task == "lru" else "test_merge.py"
    (d / name).write_text(TASKS[task]["test"], encoding="utf-8")

def sh(args, cwd=None, timeout=900):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(args, cwd=str(cwd or ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

def main():
    sys.path.insert(0, str(ROOT))
    from sodus import db
    records = []
    try:
        for rep in range(1, REPS + 1):
            for task in sorted(TASKS):
                order = list(CONFIGS)
                random.Random(20261110 + rep * 100 + hash(task) % 100).shuffle(order)
                for name, route, effort in order:
                    d = BENCH / f"{task}-{name}-r{rep}"
                    seed(task, d)
                    t0 = time.time()
                    rec = {"rep": rep, "task": task, "config": name, "route": route, "effort": effort, "cwd": str(d)}
                    try:
                        made = sh([PY, "-m", "sodus", "task", TASKS[task]["prompt"], "--title", f"bench-{task}-{name}-r{rep}"])
                        import re
                        m = re.search(r"task-[a-z0-9]+", made.stdout or "")
                        if not m:
                            rec.update(error="task create failed", output=((made.stdout or "") + (made.stderr or ""))[-1500:])
                            records.append(rec); continue
                        tid = m.group(0)
                        args = [PY, "-m", "sodus", "run", tid, "--route", route, "--cwd", str(d), "--allow-expensive"]
                        if effort:
                            args += ["--effort", effort]
                        run = sh(args, timeout=1200)
                        rm = re.search(r"run-[a-z0-9]+", run.stdout or "")
                        rec.update(wall_s=round(time.time() - t0, 2), exit=run.returncode)
                        if rm:
                            rid = rm.group(0)
                            row = db.get_run(rid)
                            u = json.loads(row.get("usage_json") or "{}")
                            rec.update(run_id=rid, status=row.get("status"), error=row.get("error"),
                                       usage={k: u.get(k) for k in ("input_tokens","output_tokens","cache_read","cache_write","cost","currency","cost_basis")},
                                       verify=db.verify_states([rid]).get(rid, "none"))
                        else:
                            rec.update(error="run id missing", output=((run.stdout or "") + (run.stderr or ""))[-1500:])
                    except Exception as e:
                        rec.update(error=type(e).__name__ + ": " + str(e)[:300], wall_s=round(time.time()-t0,2))
                    records.append(rec)
                    print(json.dumps(rec, ensure_ascii=False), flush=True)
    finally:
        with OUT.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("BENCH_DIR", BENCH, "OUT", OUT, "N", len(records), flush=True)

if __name__ == "__main__":
    main()
