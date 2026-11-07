# Robert 最终源码组织重构：完整执行计划

日期：2026-11-07。状态：全部实现及本地验收完成；PR 检查和合并状态以关联 PR 为准。
基线：`main@b097248ee6c20400206018716449f748af43d463`。
工作树：`/Users/wklken/.codex/worktrees/source-architecture/Robert`。
分支：`codex/source-architecture-final`。原用户工作区的修改保持不动。

## 目标与纠偏

原计划 U01–U11 本来就覆盖最终结构。PR #87 的实施修订主动缩小成基础边界，
不是原计划已完成。本计划替代其未完成的执行部分，以合并后的实际源码为基线，
逐步完成同一个最终交付。步骤是依赖和验证单元，不是可以停止交付的阶段。
不得把剩余工作改写为下一份路线图；出现阻塞须保留未完成状态并说明证据。

用户已明确选择“计划后直接执行全部重构”。既有任务授权包含 subagent、PR 交付，
以及全部检查通过后合并；不包含发布、真实部署、生产数据修改或验收 canary 写入。
一次最多一个有明确文件所有权的 worker；最终独立审查不与实现 worker 并行写入。

## 最终结构与依赖

```text
robert_agent/
  __init__.py, paths.py, resource_files.py
  <有明确兼容合同的薄脚本入口>
  foundation/             # 命令执行/deadline、基础值转换等稳定能力
  configuration/          # v2 加载、规范化、校验和固定路由资源
  work/                   # 工作项/事件/归属的事实、纯策略和持久化
  execution/              # attempt、执行控制、进程证据、容量、结果、共享阶段机制
  delivery/               # 提交合同、纯政策、发布锁和持久化事实
  supervision/            # 监督记录、规则、诊断与 lineage
  work/application/, execution/application/, delivery/application/
  supervision/application/, context/application/ # 领域内部用例与事务
  application/            # 仅跨领域 repository-cycle 组合
  workflows/              # review/vote/PR remediation/maintenance 的具体业务变化
  context/                # prompt、skills、项目记忆和运行知识
  readmodels/             # 只读查询、看板聚合、状态及历史
  adapters/               # GitHub/Git/进程/SQLite/通知/服务外部边界
  runtime/                # once/loop/daemon/scan/paused monitor 调用和调度
  interfaces/web/         # HTTP、CSRF、输入校验、响应和服务启动
  cli/, worker/, integrations/ # 已公开的命令与 Worker/OpenClaw 接口命名空间
  operations/             # 初始化、doctor、诊断、保留策略
  acceptance/             # 受控/只读/live 验收工具，保留独立授权边界
  resources/, web_assets/ # 已安装包的资源合同
```

纯规则和机制不依赖 application、runtime、interfaces 或具体 workflow。
各领域内部的 application 明确组合能力，拥有转换和事务；外层 application 只组合跨领域流程。
workflow 依赖执行机制；共享执行机制通过窄 collaborator 接口工作。
readmodels 不从 HTTP server 借查询；外部 adapter 不从应用层借业务规则。
runtime/HTTP 不实现业务状态转换或业务 SQL，只调用所属用例/查询。
所有显式 import（含函数内 import）无环；禁止靠动态 import、模块别名注册或
共享 globals 字典隐藏依赖。只为真实资源所有权、合同或依赖反转引入抽象。

## 不变合同和验收基线

- 保持 config v2、默认值/覆盖顺序、9 条路由、作者白名单和普通/模型容量区别。
- 保持数据库表、迁移 checksum/顺序、结果协议、状态语义和公共 CLI 参数。
- 保持任务/工作流/attempt/round/lineage 身份、fencing、幂等、授权和审计。
- 结果到达不等于进程退出；未知身份保留 attention；暂停仍监督已有执行。
- 不移动外部 I/O 进入长写事务，不改变既有 commit/savepoint 的持久化语义。
- 保留持久化 prompt 中 result/snapshot/heartbeat/status 脚本及 daemon 子进程入口。
- command-matrix 中已公开的脚本保留只调用新 main 的薄入口；内部消费者直接导入
  新 owner。旧私有 Python import 不做兼容。入口清单必须逐条说明消费者并受测试约束。
- 不引入框架、ORM、状态机副本、通用注册器、新运行依赖或业务功能。

基线已有同源码/测试快照 CI run 202：13/13 通过；Linux/macOS × Python 3.10–3.14，
1,681 tests，包含 lint、类型、构建和审计。复用该固定快照证据，不宣称改动后仍通过。
迁移前记录全部 TestCase ID；迁移后按显式路径映射比较，任何减少必须逐项说明。

## 顺序步骤与完成条件

### S01 固定归属和可执行入口合同

所有权：本计划、源文件映射、公共入口清单、测试发现清单。
列出每个顶层文件的新 owner；识别脚本、-m、服务、prompt、资源、mock 查找位置。
校验迁移后的目录不是旧问题的另一个容器。下面的全量表约束机械迁移；后续函数拆分
另在执行记录中保存精确符号映射。完成条件：108 个顶层 Python 文件均有处理结果。

### S02 归位稳定能力和功能文件群

所有权：映射中的模块、直接调用者、测试 import/mock、CI 类型检查路径。
先移文件再更新 AST import 和真实字符串入口，保留原函数体及行为。
将 work admission/lifecycle 收口到 work/application；不让 work 基础能力依赖具体流程。
调整源文件相对路径为明确 package resource/root，避免移动后定位错目录。
完成条件：导入可加载、原测试可发现、编译/lint/架构导入图通过；没有万能转发模块。

### S03 拆解核心循环的叶子事实与持久化

所有权：原 run_once 的 clock/JSON/ID、run journal、attempt 证据、输入持久化、
结果事实、上下文 artifact、工作区准备和容量函数及其调用者。
按函数实际自由变量和调用图提取，不复制共享全局状态。run/step/busy 状态写入归
持久化 owner；保留原事务/保存点时间。公共 helper 有单一 owner。
完成条件：loop/paused monitor 从相应 owner 读取证据，不回调循环入口。

### S04 完成准备、派发与监督应用用例

所有权：任务创建/刷新/resume、ready preparation、派发队列及 dispatch、monitor。
抽取显式输入输出；monitor 依赖窄 resume 能力；paused monitor 继续禁用新恢复。
运行时只调用用例。普通 Worker 与 native 模型 slot 保持各自容量和取消合同。
完成条件：新增任务、补充输入、重试、撤权、取消、远端关闭、未知进程及暂停场景通过。

### S05 完成结果、handoff、审计与发布闭环

所有权：结果载入/审计事实、completion/handoff、分类孤儿恢复、发布/回执及 scope。
delivery/application 组合 workflow 特定审核/发布；delivery 下的纯合同与锁不反向依赖。
保留待发布结果、后续任务准备失败后的 durable handoff、已发布回执和撤权复核。
完成条件：受控 issue-to-PR、重复发布、分类接续、发布失败/恢复等已有合同全部通过。

### S06 完成具体工作流与基础执行解耦

所有权：review/vote、PR remediation、maintenance、native rounds、model pool。
把通用轮次查询/进程收尾/slot 对应关系与具体流程决策分开；核心 execution 不导入
review/vote 实现。工作流装配提供窄 store/workspace/artifacts 能力，表映射只有一个来源。
完成条件：单模型/多模型、失败 reviewer 重试、vote tally、取消及轮次恢复合同保持。

### S07 完成监督、配置和上下文所有权

所有权：supervision、maintenance workflow、configuration、context。
观察/恢复应用协调留在 application；稳定规则/lineage/持久化留在 supervision。
配置解析只在 configuration，边界即时重新授权不因缓存丢失。
prompt 只接收明确准备的受信上下文；保留渲染结果、技能选择和资源定位。
完成条件：诊断/恢复/预算/修复复核与 prompt scope 相关测试通过；无第二套配置源。

### S08 完成读模型与 HTTP 分离

所有权：原 web.py、board/status/workbench/chat、interfaces/web、readmodels。
数据库查询和数据聚合移至 readmodels；HTTP 路由/CSRF/内容类型/参数/服务留在接口。
工作项写入由 work/application 处理，HTTP 无业务 SQL。
完成条件：原 JSON/页面/错误状态保持，readmodels 不导入 HTTP，Web 安全和浏览器检查通过。

### S09 完成 runtime、SQLite 装配、运维和入口收口

所有权：runtime、storage/migrations、operations、acceptance、CLI/服务入口。
移出 once/per-repo pipeline 的事件授权/准入/准备实现，runtime 只负责顺序/调度。
SQLite 迁移初始化不导入 workflow 实现以获得 schema；由明确持久化定义/装配提供。
保留 once 与 daemon 三 lane 的不同语义、离线迁移、root Worker 部署规则。
完成条件：runtime/HTTP 无业务 SQL；旧 root run_once 只有 CLI 转发；已安装包入口可运行。

### S10 清除旧引用并封闭架构规则

所有权：全仓引用、入口清单、tests/test_architecture_boundaries.py、公共扫描。
删除没有合同的旧模块/包装；禁止业务导入根入口；定义允许的顶层文件集合。
完善每类边界、只读/纯规则约束；禁止用整目录 allowlist 放宽安全扫描。
测试 fixture 按稳定责任复用，不通过恢复旧私有 API 维持 mock。
核对全部测试 ID，保持 unittest discover 可发现性；不为目录整齐机械搬全部测试。
完成条件：映射表所有旧路径均为删除或已解释的薄入口；没有暂存桥接和未解释反向依赖。

### S11 完整验证和独立复审

固定 combined diff 后执行完整 unittest、Ruff、CI 配置 Mypy、compileall、build、
浏览器检查、git diff --check；清洁 venv 安装 wheel，在源码树外跑资源/入口/受控 E2E。
独立 reviewer 检查所有权/事务/入口/在途恢复/测试遗漏，修复并按受影响范围复核。
不要求真实部署，不执行未经独立授权的 live canary。失败不能因阶段结束而转为遗留。

### S12 同步最终文档、PR、CI 和合并

AGENTS/architecture/development 仅描述实际最终结构；原计划标记被本计划替代。
历史证据链接固定到原提交，当前文档链接指向新 owner，不屏蔽链接门禁。
提交完整可审查结果，PR 描述最终行为与全部验证；新 head CI 全部通过后按既有授权合并。
只有 S01–S12 全部完成才称本次重构完成；不写“下一阶段再拆 run_once”等交付豁免。

## 验证与回退

每个稳定单元先跑涉及的现有测试；共享改动完成后运行仓库强制全套。
支持命令：`python3 -B -m unittest discover -s tests`、`python3 -m ruff check src tests`、
CI 配置的 Mypy、`python3 -B -m compileall -q src`、`python3 -m build`、`git diff --check`。
保持标准库/当前依赖，不在重构中升级。wheel 中资源和所有真实入口另作 smoke。
未合并可丢弃专用分支或回退本任务提交；合并后可 revert 本 PR，无数据库 downgrade。
不对用户原工作区 reset/clean。这里的代码回退不等同部署回滚，真实升级另行授权。

## 全量顶层文件归属

以下目标是迁移所有权，不表示按行机械切分已完成。职责混合文件还须满足上面的函数拆分。

| 原顶层模块 | 最终实现 owner |
| --- | --- |
| `acceptance.py` | `acceptance/readiness.py` |
| `acceptance_suite.py` | `acceptance/suite.py` |
| `action_scope.py` | `delivery/application/scope.py` |
| `audit_result.py` | `delivery/application/audit.py` |
| `authorize.py` | `security/authorization.py` |
| `board.py` | `readmodels/board.py` |
| `board_execution.py` | `readmodels/execution.py` |
| `chat_status.py` | `readmodels/chat.py` |
| `common.py` | `foundation/commands.py` |
| `control_lane.py` | `runtime/control.py` |
| `controlled_e2e_acceptance.py` | `acceptance/controlled_e2e.py` |
| `daemon.py` | `runtime/daemon.py` |
| `daemon_state.py` | `execution/runtime_state.py` |
| `diagnostics.py` | `operations/diagnostics.py` |
| `discover.py` | `adapters/github/discovery.py` |
| `dispatch.py` | `adapters/processes/worker.py` |
| `doctor.py` | `operations/doctor.py` |
| `execution_metrics.py` | `readmodels/execution_metrics.py` |
| `execution_operations.py` | `execution/operations.py` |
| `execution_replay.py` | `execution/application/replay.py` |
| `execution_store.py` | `adapters/sqlite/execution_control.py` |
| `github_api.py` | `adapters/github/api.py` |
| `github_operator.py` | `work/operator_queries.py` |
| `github_read.py` | `adapters/github/read.py` |
| `github_scan.py` | `runtime/scan.py` |
| `init_config.py` | `operations/initialize.py` |
| `live_discovery_acceptance.py` | `acceptance/discovery.py` |
| `live_worker_acceptance.py` | `acceptance/worker.py` |
| `live_worktree_acceptance.py` | `acceptance/worktree.py` |
| `loop_engine.py` | `runtime/loop.py` |
| `memory_curator.py` | `context/curation.py` |
| `model_environment.py` | `adapters/processes/environment.py` |
| `model_pool.py` | `adapters/sqlite/model_pool.py` |
| `native_rounds.py` | `execution/rounds.py` |
| `notify.py` | `delivery/application/notifications.py` |
| `pending_handoff.py` | `work/application/handoff.py` |
| `pr_health.py` | `workflows/pr_health.py` |
| `production_write_canary.py` | `acceptance/write_canary.py` |
| `project_memory.py` | `context/project.py` |
| `publication_lock.py` | `delivery/lock.py` |
| `publication_reconcile.py` | `delivery/application/reconcile.py` |
| `publish.py` | `delivery/application/publish.py` |
| `publish_dedupe_acceptance.py` | `acceptance/publish_dedupe.py` |
| `redaction.py` | `security/redaction.py` |
| `reliability_health.py` | `operations/health.py` |
| `remediation.py` | `workflows/pr_remediation.py` |
| `render_prompt.py` | `context/prompts.py` |
| `result_contract.py` | `delivery/contract.py` |
| `result_cutover.py` | `execution/application/cutover.py` |
| `result_spool.py` | `execution/spool.py` |
| `result_validation.py` | `delivery/application/validation.py` |
| `retention.py` | `operations/retention.py` |
| `retry_policy.py` | `execution/retry.py` |
| `review_coordinator.py` | `workflows/review/coordinator.py` |
| `review_files.py` | `workflows/review/files.py` |
| `review_policy.py` | `context/review_policy.py` |
| `review_publish.py` | `workflows/review/publish.py` |
| `review_store.py` | `adapters/sqlite/review_rounds.py` |
| `review_workspace.py` | `workflows/review/workspace.py` |
| `route.py` | `workflows/routes.py` |
| `run_history.py` | `readmodels/history.py` |
| `run_once.py` | `runtime/cycle.py` |
| `runtime_identity.py` | `adapters/processes/runtime_identity.py` |
| `runtime_knowledge.py` | `context/knowledge.py` |
| `service.py` | `adapters/services/native.py` |
| `skills.py` | `context/skills.py` |
| `sqlite_migration.py` | `operations/migration.py` |
| `status.py` | `readmodels/status.py` |
| `storage.py` | `adapters/sqlite/database.py` |
| `summarize.py` | `readmodels/summarize.py` |
| `supervise.py` | `execution/inspection.py` |
| `supervision_actions.py` | `supervision/actions.py` |
| `supervision_diagnose.py` | `supervision/diagnosis.py` |
| `supervision_git.py` | `supervision/git.py` |
| `supervision_lineage.py` | `supervision/lineage.py` |
| `supervision_observe.py` | `supervision/application/observation.py` |
| `supervision_publication.py` | `supervision/application/publication.py` |
| `supervision_repair.py` | `workflows/maintenance/repair.py` |
| `supervision_repair_followup.py` | `workflows/maintenance/followup.py` |
| `supervision_repair_worker.py` | `workflows/maintenance/worker.py` |
| `supervision_rules.py` | `supervision/rules.py` |
| `supervision_store.py` | `supervision/store.py` |
| `supervisor.py` | `runtime/supervisor.py` |
| `usage.py` | `readmodels/usage.py` |
| `validate_config.py` | `configuration/command.py` |
| `vote_coordinator.py` | `workflows/vote/coordinator.py` |
| `vote_files.py` | `workflows/vote/files.py` |
| `vote_publish.py` | `workflows/vote/publish.py` |
| `vote_store.py` | `adapters/sqlite/vote_rounds.py` |
| `vote_tally.py` | `workflows/vote/tally.py` |
| `vote_workspace.py` | `workflows/vote/workspace.py` |
| `wakeup.py` | `work/wakeup.py` |
| `wakeup_notifier.py` | `adapters/notifications/wakeup.py` |
| `web.py` | `interfaces/web/server.py` |
| `work_item_control.py` | `work/application/control.py` |
| `work_items.py` | `work/application/commands.py` |
| `workbench.py` | `readmodels/workbench.py` |
| `worker_identity.py` | `adapters/processes/identity.py` |
| `worker_supervision.py` | `runtime/paused_monitor.py` |
| `workflow_events.py` | `work/events.py` |
| `workstream.py` | `work/streams.py` |
| `worktree.py` | `adapters/git/workspace.py` |

保留包元信息/基础定位：`__init__.py`、`paths.py`、`resource_files.py`。
保留已有 Worker 薄入口：`worker_result.py`、`worker_snapshot.py`、`worker_heartbeat.py`。
其他根脚本仅限 command-matrix 中已公开的 main 转发；不保留原实现或私有 re-export。

## 执行记录

- S01：完整 108 文件归属已核对；142 个 run_once 函数调用图无内部循环，可按依赖顺序提取。
- S02–S10：实现与引用迁移已覆盖；稳定能力/应用用例已按领域内部层次收口。
  142 个原核心函数按 owner 拆分；恢复创建改为显式 collaborator；runtime/HTTP 无 SQL。
  10 项架构门禁通过，含全图无环、适配器/Worker 反向依赖限制和有限根入口合同。
- S11：最终全套 1,688 项通过（360.778 秒）；原 1,681 项测试 ID 全部保留，新增 7 项。
  Ruff、mypy 的 15 个配置目标、compileall、29 项浏览器测试、wheel/sdist 构建通过。
  已安装 wheel 在源码目录外通过全部模块与 35 个 main 目标导入、资源/脚本/两个运维
  模块命令，以及受控端到端场景（模拟发布两次）。该模块/资源/恢复入口检查已加入 CI。
  文档本地链接存在性和 diff 格式检查通过；网络链接由远端 CI 检查。
- S12：独立审查提出 6 项问题，均已修复并独立复核关闭；复核的 15 项边界/布局测试通过。
  修复包括公开运维入口、绝对脚本启动、Worker 业务下沉、SQLite 容量归属，以及共享
  重鉴权/来源/维修标记查询。额外将 SQLite 控制存储归持久化 adapter，数据库升级和
  通知投递归运维/交付用例，adapter 不再反向导入领域。
  本提交进入同一个最终 PR；远端 Linux/macOS × Python 3.10–3.14、wheel、密钥扫描、
  依赖审计和文档链接全部通过后才允许合并。远端结果保留在该 PR 的检查记录中。
  未执行真实部署、发布或生产写入验收；这不属于本次授权。
