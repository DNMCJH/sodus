结论：0.7.0 不作日常主版本；限值守、关闭自动恢复试用。7370a7b。

Q1 整体性：MCP/plan归daemon，状态/计费集中。但plan仍创建占位task，import直写DB；state依赖board私有函数且另算asks状态。统一查询/状态投影，board/state/pet只渲染；明确ask答复、plan提案。

Q2 安全：威胁：恶意提示/输出、同机进程/网页。已有回环/token/限长、路径防逃逸、写锁/worktree，但无沙箱：worker可写任意路径、读凭据/记忆并外传；agents.py/build_env复制父环境，须凭据白名单。plan.approve只认human字符串，递归头可绕过；须隔离权限、人工批准、服务端限额。propose.merge有锁/回滚；git/验收命令按任意代码防护。server无Host校验，公开首页嵌token，须防DNS重绑定，校验Host禁远程。

Q3 鲁棒性：SQLite有WAL/事务迁移；非零退出不冒充成功。review复用_wait_run，已有默认1800秒超时，仍占线程；重启planning置failed。CTRL_C/exit=1未明，须留进程树/日志复现。

Q4 高效性：提效未证。指标：隐藏测试/verify成功率、耗时、全链成本（plan/review/重试）、介入、返工及操作数。opus规划/sol审查/写锁增开销。预注册24个分层任务、每题5对，等模型/预算、随机顺序、隔离快照/记忆盲测；任务聚类计算配对差及区间。120对起步，先导重算80%功效，禁n=1/3推断。

Q5 其它：多供应商降低锁定；CLI/DSH有漂移；账本可观测，缺隐私保留/脱敏证据。预算仅挡新run；临时200须恢复。发布PASS据报告；回滚须在线备份/恢复演练，勿照搬reset --hard。物化/replan未交付。

Q6 风险/阻塞（路径均在nodus/，P1高/P2中）：
- P1 daemon.py:start/stop、server.serve：无互斥/PID核验；bind前reconcile误清活跃plan。须独占/核身份/持久化恢复开关。
- P1 plan.py:_wait_run：超时不停止worker；须杀进程树。
- P1 agents.py:build_env：继承环境/只读无隔离。
- P2 cli/commands/mcp.py:_merge/install：丢env/非原子；保留未知键、原子替换。
- P2 plan.py:validate_plan/approve：不核HEAD/AGENTS哈希；批准前重验并绑定计划hash。

checklist（隔离留证）：
□ 并发up×10：Get-NetTCPConnection查唯一PID，匹配daemon.json；活跃plan不被误清。
□ 注入短超时/杀daemon：无遗留worker、锁或静默重试；CTRL_C复现后独立verify退出0。
□ 恶意只读任务不能写仓库/读token；假Host首页拒绝；测试凭据不进worker环境。
□ 配置加env再install须保留；中断写入仍可解析；三宿主--check及重启工具调用通过。
□ HEAD/AGENTS改变后approve拒绝；revise仍不可批准。
□ release_check.ps1退出0；备份恢复校验完整性/行数；完成上述配对实验。

SUMMARY：定向测试退出0，实机单监听0.7.0；未跑全量门禁，未改代码。先修P1再启用。
