# 0.8.0-a 实现报告

## 实现

- 新增 nodus/handoff.py 与本地 sodus handoff init/show/append/sync/check/list；needs_daemon=False，不自动启动 daemon。
- 项目根优先 git root，非 git 目录使用 cwd；配置 handoff.path 支持相对根和绝对路径。
- 模板含 frontmatter 与 11 个人工节；append 添加日期 bullet、刷新 snapshot_at；sync 只更新唯一 auto 标记对内的 HEAD/tag/branch、daemon version/pid、task/run/plan 简要元数据和预算族上限。
- 所有写入使用同目录临时文件、flush/fsync 与 os.replace；保留 auto 段外的原始换行，包括 Windows CRLF。
- check 检查文件存在、snapshot 可解析/默认 7 天、HEAD 一致、关键路径和 daemon 版本；失败退出码 1，daemon 不可用明确失败。
- list 从当前 cwd 与已有 workspace task/run cwd 去重；当前 task/plan 表无 cwd，通过 task_id 关联 run；sqlite mode=ro，既不建库也不迁移，不新增全局正文库或缓存。
- HTTP GET /handoff、/handoffs 和 MCP check 所需的 GET /handoff/check 均须 bearer token；限定到已知 project root，禁止相对配置/符号链接向外逃逸；绝对 handoff.path 是受信任的用户配置。无写端点。
- MCP 新增 nodus_handoff_show/list/check（readOnlyHint），总计 14 个工具名，均通过 daemon GET；旧 sodus debug handoff <run> 保留。
- 更新 README、docs/cli.md、docs/mcp.md 和 openapi.yaml；版本未 bump，CHANGELOG 未添加版本号，无 git commit/push/merge。

## 验证

- 目标测试：96 项通过，退出码 0，证据 reports/080a-target-tests.txt。
- 全量测试：1163 项通过，A:/Anaconda/python.exe -m pytest -q 退出码 0，证据 reports/080a-pytest-all.txt；发行检查也执行全量 pytest。
- 初轮全量发现 OpenAPI 路由集合旧断言未更新，已同步新增只读路由；初轮失败记录保留在 reports/080a-full-tests.txt。
- 手动命令：临时 git 仓库 sodus-080a-manual-dta026a6 中使用 A:/Anaconda/python.exe -m sodus handoff，init、show --json、append、sync、check --json、list --json 均 exit 0；check ok=true/errors=[]。完整输出 reports/080a-manual.txt。
- 发行检查：首轮隔离安装/MCP smoke 成功，内嵌全量测试 test_memory.py::test_http_adopt_requires_the_token 遇到 ConnectionAbortedError；证据 reports/080a-release-check.txt。无其他测试并跑的情况下重跑 PASS、退出码 0（构建 wheel/sdist、隔离安装、doctor、DB smoke、MCP 握手及全量 pytest），最终证据 reports/080a-release-check-final.txt。

## 范围与交接

不做内容聚合、自动 hook、B3/B4、plan 执行/物化，不重启现有 daemon，不改主仓库 HANDOFF.md。首次接入已有手工控制台时需人工添加 auto 标记对；sync 对缺失/重复/顺序错误的标记拒绝写入。下一位审阅本工作区 diff 和验收证据，再由 Lead 决定版本 bump 与提交；B3/B4 仍属后续阶段。

## SUMMARY

实现已完成；目标 96 项、独立全量 1163 项和六个手动命令均通过、退出码 0；发行检查重跑 PASS、退出码 0；没有遗留验收阻塞，无 git commit。
