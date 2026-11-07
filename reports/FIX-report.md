# FIX report

## SUMMARY

已修复 H1/H2、M1/M2/M3，并加固 release_check；未处理 M4-M6。

- H1: dream 输出解析支持 `json` 围栏，校验 candidates/text schema；非法输出将批次置 failed 并记录 error，允许同游标重新 plan。目标 dream/ask/memory 测试通过。
- H2: recovery 逻辑保留三种崩溃分支的幂等行为，相关 recovery、memory 测试通过。
- M1: ask.view 对 run 终态统一映射，失败 run 不再让 ask 永久 running。
- M2: memory board 合并 staged 与 harvest 候选并按文本去重。
- M3: candidates 默认 workspace 使用 config.workspace_id，evidence_count 使用 candidate_evidence 非暴露 lineage 计数。
- release_check: build/venv/pip/doctor/mcp/pytest 均检查退出码；版本从 pyproject.toml 读取；末尾执行全量 pytest。

验证：目标测试退出码 0；`A:\Anaconda\python.exe -m pytest -q` 退出码 0（全量通过）。release_check 需在具备 build 依赖和本机 0.5.1 数据库的发布环境运行完整 PASS 检查。

遗留：M4、M5、M6 及评审列出的低危项未修改。
