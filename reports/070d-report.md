# 0.7.1-a 修复报告

## 修复

- `POST /plans/{id}/review` 启动后台 review 并返回 202；计划状态可见 `reviewing`、`reviewed` 或 `review_failed`。`GET /plans/{id}` 供 CLI 轮询。review 使用 `NODUS_REVIEW_TIMEOUT` / `review_timeout`，默认 900 秒；超时及其他后台错误统一标记 `review_failed` 并写入 `errors_json`。
- validator 对可解析但不同于目标仓库 HEAD 的 `base_sha`、`AGENTS.md` 哈希不匹配或文件缺失写入非阻断 `warnings_json`。`recon.agents_md_path` 可指定仓库内相对路径，`agents_md: null` 跳过哈希检查。acceptance 命令需以常用命令白名单或路径形式的 executable 开始；自然语言空话报错。
- MCP installer 保留 Codex/Claude/DSH sodus 项内已有字段和 env；未提供 DSH 路径时报告 skipped，不令安装检查失败。写入使用同目录临时文件和 `os.replace`，写入前保留备份。

## 测试证据

- `tests/test_plan.py`：fake planner 验证 review 异步状态转换、超时状态、base SHA 漂移、AGENTS 哈希不匹配/文件缺失及 acceptance 语义；覆盖 HTTP 202 和轮询。
- `tests/test_mcp_install.py`：验证宿主自定义字段/env 保留、DSH skipped、原子替换失败时原文件完整。
- `A:\Anaconda\python.exe -m pytest -q`：退出码 0。
- `A:\Anaconda\python.exe -m pytest tests/test_plan.py tests/test_mcp_install.py -q`：退出码 0。
- `A:\Anaconda\python.exe -m sodus mcp install --check`：Codex、Claude、DSH 均为 `configured`，退出码 0。
- `git diff --check`：通过。

## 遗留

- DSH 配置目前用 PyYAML 重序列化，注释格式可能无法保留；如需逐字保留 YAML 注释，列入 0.7.2。
- brief 指定的 `reports/HANDOFF-0.7.md` 和 `reports/opus-review-0.7.md` 在工作区不存在，本次依据 brief 和当前实现完成。

## SUMMARY

SUMMARY：异步 review/timeout、validator warnings 与 acceptance 白名单、installer 配置 merge/DSH skipped/原子写均已实现并通过测试；全量 pytest 与三宿主安装检查退出码均为 0。未做 git commit。
