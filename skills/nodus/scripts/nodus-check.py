#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nodus-check.py — 一条命令自检 Nodus 环境能不能用。

三项硬检查，任何一项不通过就退 1：

    token    <home>/token 存在、可读、长度像 token
    daemon   http://127.0.0.1:<port>/health 返回 ok=true
    routes   每条已配置路由 ready=true（本机基线 6 条）

只用标准库，不 import nodus —— agent 的环境里不一定有 PYTHONPATH。
daemon 端口优先读 <home>/daemon.json，读不到用 4199。

用法：
    python nodus-check.py
    python nodus-check.py --json
    python nodus-check.py --port 4199
    python nodus-check.py --home D:\\nodus-state

退出码：0 全通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 4199
EXPECTED_ROUTES = 6
TIMEOUT = 3.0

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


def _harden_stdout() -> None:
    """Always write UTF-8.

    On Windows a piped stdout defaults to the ANSI code page (gbk here), which
    turns Chinese output into mojibake for whoever reads the pipe. Forcing
    utf-8 with errors=replace keeps the text readable and keeps an unencodable
    character from becoming an exit-code-1 crash.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def nodus_home(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("NODUS_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".nodus"


def read_token(home: Path) -> tuple[str | None, str]:
    path = home / "token"
    if not path.is_file():
        return None, f"{path} 不存在（跑任意一个 nodus 子命令会生成）"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"{path} 读不了：{exc}"
    if len(value) < 32:
        return None, f"{path} 只有 {len(value)} 字符，不像 token"
    return value, f"{path} ({len(value)} chars)"


def resolve_port(home: Path, override: int | None) -> tuple[int, str]:
    if override:
        return override, "命令行 --port"
    path = home / "daemon.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_PORT, f"{path} 读不到，用默认 {DEFAULT_PORT}"
    port = data.get("port")
    if isinstance(port, int) and 0 < port < 65536:
        return port, f"{path}"
    return DEFAULT_PORT, f"{path} 里没有有效 port，用默认 {DEFAULT_PORT}"


def http_json(url: str, token: str | None = None) -> tuple[object | None, str]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = exc.reason
        return None, f"连不上（{reason}）"
    except (ValueError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def check_token(home: Path) -> dict:
    token, detail = read_token(home)
    return {"name": "token", "status": PASS if token else FAIL,
            "detail": detail, "token": token}


def check_daemon(port: int, port_source: str) -> dict:
    url = f"http://127.0.0.1:{port}/health"
    payload, error = http_json(url)
    if payload is None:
        return {"name": "daemon", "status": FAIL, "detail": f"{url} {error}",
                "url": url, "port": port}
    if isinstance(payload, dict) and payload.get("ok") is True:
        version = payload.get("version") or "?"
        return {"name": "daemon", "status": PASS,
                "detail": f"{url} ok=true version={version} ({port_source})",
                "url": url, "port": port}
    return {"name": "daemon", "status": FAIL,
            "detail": f"{url} 没有返回 ok=true：{payload}", "url": url, "port": port}


def check_routes(port: int, token: str | None, expected: int) -> dict:
    if not token:
        return {"name": "routes", "status": SKIP,
                "detail": "没有 token，无法读 /routes"}
    url = f"http://127.0.0.1:{port}/routes"
    payload, error = http_json(url, token)
    if not isinstance(payload, list):
        return {"name": "routes", "status": FAIL,
                "detail": f"{url} 读不到路由表：{error or payload}"}
    ready = [r for r in payload if isinstance(r, dict) and r.get("ready")]
    broken = [r for r in payload if isinstance(r, dict) and not r.get("ready")]
    names = " ".join(str(r.get("name") or "?") for r in payload)
    if not payload:
        return {"name": "routes", "status": FAIL, "detail": "路由表是空的"}
    if broken:
        why = "; ".join(
            f"{r.get('name')}: {', '.join(map(str, r.get('problems') or ['未就绪']))}"
            for r in broken
        )
        return {"name": "routes", "status": FAIL,
                "detail": f"{len(ready)}/{len(payload)} ready —— {why}", "routes": payload}
    status = PASS
    note = ""
    if len(payload) != expected:
        note = f"（基线 {expected} 条，现在是 {len(payload)} 条）"
    return {"name": "routes", "status": status,
            "detail": f"{len(ready)}/{len(payload)} ready  {names}{note}",
            "routes": payload}


def check_cli() -> dict:
    found = shutil.which("nodus") or shutil.which("nodus.exe")
    if found:
        return {"name": "cli", "status": INFO, "detail": found}
    return {"name": "cli", "status": INFO,
            "detail": "PATH 里没有 nodus（MCP 方式不需要；CLI 方式用 python -m nodus）"}


def main(argv: list[str] | None = None) -> int:
    _harden_stdout()
    parser = argparse.ArgumentParser(
        description="自检 Nodus 环境：daemon / 路由 / token")
    parser.add_argument("--home", help="Nodus 状态目录，默认 $NODUS_HOME 或 ~/.nodus")
    parser.add_argument("--port", type=int, help=f"daemon 端口，默认读 daemon.json，再退 {DEFAULT_PORT}")
    parser.add_argument("--expect-routes", type=int, default=EXPECTED_ROUTES,
                        help=f"路由条数基线（只提示，不判失败；默认 {EXPECTED_ROUTES}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON，给 agent 解析")
    args = parser.parse_args(argv)

    home = nodus_home(args.home)
    port, port_source = resolve_port(home, args.port)

    token_check = check_token(home)
    token = token_check.pop("token", None)
    checks = [token_check]
    checks.append(check_daemon(port, port_source))

    if checks[1]["status"] == PASS:
        checks.append(check_routes(port, token, args.expect_routes))
    else:
        checks.append({"name": "routes", "status": SKIP,
                       "detail": "daemon 没起来，先 nodus up"})
    checks.append(check_cli())

    failed = [c for c in checks if c["status"] == FAIL]
    ok = not failed
    passed = len([c for c in checks if c["status"] == PASS])
    judged = len([c for c in checks if c["status"] in (PASS, FAIL)])

    if args.json:
        print(json.dumps({"ok": ok, "home": str(home), "port": port,
                          "checks": checks}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    for check in checks:
        print(f"{check['status']:<5} {check['name']:<7} {check['detail']}")
    if ok:
        print(f"nodus-check: {passed}/{judged} 通过 —— 可以派活")
    else:
        names = ", ".join(c["name"] for c in failed)
        print(f"nodus-check: {len(failed)} 项失败（{names}）—— 先修这个再派活")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
