# 0.7.0-c1 执行报告

## 交付

- `docs/sodus-0.7-plan.md` 定义 plan.json v1、生命周期和 CLI/存储契约。
- `nodus/db.py` 增加事务内 additive schema v6：plan / plan_node；v5 升级、老数据保留、重复迁移均有覆盖。
- `nodus/plan.py` 提供结构化错误列表、文件加载、原子导入、读取和当前工作区列表。
- `sodus plan validate/import/show/list` 已注册，无需 daemon，不接模型，不执行 acceptance。

## 校验规则

检查 schema_version、目标、recon 结构及 agents_md 哈希格式；base_sha 必须为可解析的完整 commit SHA；
recon.files 和 file_scope 只允许当前目录内已存在路径；禁止绝对路径、上级逃逸和外部符号链接；
检查 node id 唯一、milestone/deps 引用、DAG、每个 milestone 有节点；检查可执行命令形式的非空 acceptance；
检查整数 difficulty 1–5、有限非负 budget_cny、非空 risks/review_points/rollback。
命令检查只识别形式，不承诺命令可用或测试通过；错误输入不派发任务、不创建 plan。

## 测试证据

- plan 专项：22 passed（`A:\Anaconda\python.exe -m pytest tests/test_plan.py --override-ini addopts= -q`）。
- CLI surface 与 dream 迁移回归：25 passed。
- 六类必测错误：不存在 SHA、虚构路径、依赖环、缺 acceptance、重复 node id、空 milestone；均测试拒绝导入。
- 合法计划通过 CLI validate/import/show/list，plan_json 原样 round-trip；另覆盖预算、难度、自然语言验收、缺依赖及工作区隔离。
- 首轮全量暴露三处旧断言（CLI 帮助大小、两处 schema v5），已随新接口更新，最终全量结果见下方 SUMMARY。

## 遗留与边界

c2 接模型、review/approve 及后续执行生命周期；c1 不新增 HTTP 端点，不改 task 状态机。
文件即 draft，只有合法计划持久化为 validated；不存储无效 draft。
agents_md 只验证 SHA256 格式，不进行内容漂移比较；命令不执行，路径存在性只代表校验时刻。
未执行 git commit/push/merge，原有未跟踪交接材料保留。

## SUMMARY

契约、schema v6、机械 validator、本地 CLI 和 22 项专项测试已完成。
最终执行 `A:\Anaconda\python.exe -m pytest -q`，1065 项全部通过（100%），退出码 0；日志 `reports/070c1-pytest.log`，收集清单 `reports/070c1-collection.log`。
六类错误、合法 import round-trip、迁移幂等与旧数据保留均已覆盖；`git diff --check` 退出码 0。下一位接 c2 模型及审核批准流程；未提交 git commit。
