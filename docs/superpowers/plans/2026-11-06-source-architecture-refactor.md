# Robert 源码组织与分层重构执行计划

日期：2026-11-06
状态：历史计划；PR #87 仅完成部分内容，未完成项由 [最终源码组织完整计划](2026-11-07-final-source-architecture.md) 接管。原阶段记录保留用于说明实际完成范围。
分析基线：`origin/main@2aff5524bde229735ec5a7278535a14d3d65bfa9`
实施基线：`origin/main@e5989c381c52680deb7ecf585c52a8c18f6f7b6d`（已合入 config v2）
源码检查工作树：`/tmp/robert-main-architecture-20261106`（detached）
计划存放在用户原工作区，**不意味着实施应以原工作区当前分支为基线**。

## 2026-11-07 实施修订与阶段记录

完整 U01–U11 同时变更任务状态、事务、在途恢复和产品入口，无法作为一个清晰的
行为保持补丁审查。当前 PR 先交付下列可独立验证的基础边界；原 U01–U11 保留为完整
路线图，其余项没有因建目录或通过 import 检查而被标记完成。

| 单元 | 本 PR 完成范围 | 仍待后续完成 |
| --- | --- | --- |
| U01 | 进程能力、安全文本、静态无环及底层依赖门禁 | 其他叶子模块按实际消费者继续归位 |
| U02 | 配置分层、严格 v2、纯监督设置、单一路由资源、即时路由授权 | 进一步收窄各用例配置类型、PolicyProvider |
| U03/U04 | 输入查询/归属、任务所有权、租约、通知和任务终结入口 | 任务准备/接续、派发/监督、完整写入所有权 |
| U05/U06 | 纯结果校验、Git 身份能力、发布不回调 run_once | 审计/发布的完整用例与存储端口 |
| U07 | StageStore/Workspace/Artifacts 显式能力、共享槽位注入、PR adapter | review/vote 目录迁移、native-round 读写职责细分 |
| U08–U11 | 当前路径说明、真实入口保留、无用途 facade 删除和完整门禁 | 监督维护/上下文/UI/runtime 迁移、测试按层归属 |

基线完整测试：Python 3.14.7，1,672 tests，365.927 s，OK。本轮将受控验收 Worker 改为使用当前 `sys.executable`，避免直接调用虚拟环境
解释器时子进程从 PATH 选中另一个缺依赖的 Python；有对应失败后通过的回归场景。

实施后全源码显式静态 import 图无多节点强连通集合；基线为 23、5、2 模块的三个集合。
这个结果仅说明 import 方向，不能代替剩余应用层分层、事务和状态所有权验收。

当前阶段验证：完整 1,681 tests（366.213 s）通过；Ruff、mypy（14 个源码文件）、
compileall、构建和 staged diff 检查通过。独立 wheel 环境通过 CLI、资源、四个保留
脚本入口与受控端到端发布；安装的 161 个 Python 文件与最终源码一致。
独立审查的三个问题（两处 scheduler 旧调用、验收 Worker 解释器选择）已修复并复查。
本地验证平台为 macOS/Python 3.14.7；其余平台矩阵由 CI 验证，真实部署不在本 PR 范围。

## 1. 目标、范围与授权

把当前围绕 run_once 生长的控制逻辑，调整为职责和依赖方向明确的模块化单体。降低新增 workflow、监督规则、Worker adapter 和 UI 需求对核心执行与交付机制的影响。

用户在完成分析与计划后授权“提个 PR 处理，允许使用 subagents”。实施包含本地重构、验证、提交、推送和 PR，不包含合并、发布或部署。原工作区的无关修改保留；实施在从最新 main 创建的独立工作树进行。

实施范围：

- 源码包边界、公开用例、领域契约、事务和状态写入所有权。
- 配套调用者、动态入口、资源定位、相关测试及架构说明。
- 消除本次重构造成的旧引用、死代码和无实际用途的转发包装。
- 对现有行为建立必要的特征/契约测试，逐步约束依赖方向。

明确不做：

- 不引入新业务功能、通用 Agent 框架、ORM、消息代理、事件溯源或通用工作流 DSL。
- 不改变数据库产品、表结构、schema migration 版本、结果协议版本、外部 JSON 状态含义或现有权限。
- 保持已合入 main 的 config v2，不追加新的配置格式变更、监督新能力、性能优化、历史 DBOS 数据清理或其他工作区的未提交修改。
- 不将所有 dict 机械替换成类；不以文件行数阈值或目录数作为成功标准。

## 2. 基线、证据与执行前依赖

分析基线有 131 个 Python 文件、112 个根目录文件，run_once.py 为 8,992 行；最近 50 个涉及源码的非合并提交中，它修改了 33 次。静态显式 import 图包含 23 模块的最大强连通集合；包含局部 import，不表示已经发生加载错误。

已核对的源码和契约：

- `src/robert_agent/run_once.py`：接收、准备、监督、派发、审计、终结及 handoff。
- `work_items.py`、`work_item_control.py`、`execution_operations.py`：人工控制、状态与执行所有权。
- `worker/result.py`、`result_contract.py`、`result_spool.py`、`publish.py`：提交到发布闭环。
- `model_execution.py`、review/vote 模块、supervision 模块：多模型及监督扩展。
- `AGENTS.md`、`pyproject.toml`、`.github/workflows/ci.yml`、`docs/development.md`：平台及检查要求。
- 下文聚焦测试以当前实施基线核对；新增边界与解释器回归测试随本 PR 提供。

原工作区仍有 prompt 和其他任务的未提交内容。本 PR 在独立工作树 `codex/source-architecture-refactor` 实施，不移动、覆盖、暂存或 reset 原工作区内容。

执行前确认结果：

1. main 已合入 config v2，实施基线更新到 e5989c3；保留严格 v2 规范与秒单位。
2. 在独立基线工作树运行完整测试：1,672 tests，OK。实施工作树按 docs/development.md 建立自己的虚拟环境，使用 Python 3.14.7、PyYAML 6.0.3。
3. 仓库内动态入口已枚举；外部部署消费无法仅凭源码穷尽。已证实的公共入口保留，旧内部 Python import 的迁移限制见架构文档。
4. 不包含实际部署升级；服务账户、旧安装保留和在途进程切换未作生产验证。

## 3. 已选定的结构及设计决策

### 3.1 模块职责

```text
src/robert_agent/
  bootstrap.py
  configuration/               # load / models / validate / resolve
  work/                        # 工作项、串行任务、输入归属和 lineage
    domain.py
    policies.py
    ports.py
    application/               # admission / commands / handoff
    adapters/sqlite.py
  execution/                   # Attempt、所有权、容量、进程、结果
    domain.py
    policies.py
    ports.py
    application/               # dispatch / monitor / results / recovery / stages
    adapters/                  # sqlite / spool
  delivery/                    # 审计、作用域、发布意图和回执
    domain.py
    policies.py
    ports.py
    application/               # audit / publish / reconcile
    adapters/sqlite.py
  supervision/
    domain.py
    rules/
    ports.py
    application/               # observe / diagnose / recover
    adapters/                  # sqlite / recovery gate
  workflows/
    routing.py
    classification.py
    code_change.py
    review/
    vote/
    pr_remediation/
    maintenance/
  context/                     # prompts / skills / project_memory / runtime_knowledge
  readmodels/                  # board / workbench / status / execution / supervision
  adapters/                    # github / git / processes / workers / sqlite /
                               # notifications / services
  runtime/                     # daemon / scan / drain / publisher / supervisor / paused monitor
  interfaces/                  # cli / web / worker / openclaw
  security/
  operations/
  acceptance/
  resources/
```

目录按实际迁移创建，不先生成整树空壳。领域规则使用普通纯函数和少量不可变类型。应用层拥有用例及事务。跨域 SQL 查询允许放在 readmodels；跨域状态写入通过所属模块公开操作协调。

### 3.2 路由政策与配置

固定政策使用已打包的 `resources/routes.yml` 为唯一可编辑来源，内部解析为类型化 RoutePolicy。取消 route.py 中独立手写的政策副本；过渡期 ROUTES 可由规范化结果生成，但不得成为第二个可编辑来源。

这是相对分析报告中“Python 政策作为单一源”建议的收窄：保留当前资源格式可减少生成/打包调整。已用目标 main 实际核对 9 条 YAML 政策与 9 条 Python 路由：YAML 中同名字段值一致；Python 另有 route_id、confidence 和补齐的 required_skills 等内部表示。迁移保留这些内部语义，不将缺省值误当删除。

配置外部保持实施基线 main 的严格 version 2；不恢复已删除的 version 1 兼容输入。内部引入规范化、按消费方缩小的配置类型，不修改覆盖顺序、默认值、worker 顺序含义和多模型白名单规则。效果边界的实时配置/撤权校验保留。

### 3.3 状态与事务

- WorkItem、Workstream、Task、Attempt、Lineage、Round、Intent、Incident 保持不同语义和原身份。
- 不创建复制现有 SQL 生命周期的第二套可写状态模型；纯规则判断和存储约束表达同一契约。
- 应用用例明确事务；存储函数使用调用者连接，不暗中 commit。
- 外部 I/O 前先持久化准备/claim，I/O 后重新核对身份和 generation 再落回执。不得为“统一事务”把网络、Git 或进程等待包进长写事务。
- 当前跨表原子性继续使用同一 SQLite 连接。不是每个模块单独事务，也不是拆成消息后异步补齐。
- 数据库连接参数先提供明确的 read-only / short-write / migration 等既有用途入口，不把所有 busy timeout 强制统一为一个值。

### 3.4 入口和兼容

保留 `robert` 命令及 `robert_agent.cli.main:main` 入口，暂不要求改 pyproject 的 console script。CLI 实现可移到 interfaces，原入口保留有用途的薄转发。

保留已生成 prompt 使用的 worker_result.py、worker_snapshot.py、worker_heartbeat.py、status.py 文件入口，以及仓库/服务证实使用的 python -m 模块入口。迁移后验证它们在已安装 wheel 中能工作。其他内部包装必须随最后调用者迁移而删除，不能全量保留“保险兼容”。

不承诺任意旧绝对安装路径在包被卸载后仍然可用；实际升级时需在旧进程/在途 prompt 仍需要旧安装时保留对应安装，或先安全排空。这个部署约束单独验证，不能靠源码同名包装声称解决。

## 4. 生产者、消费者和接口契约

以下是**拟议语义接口**，不是对当前 API 的声明。方法最终命名随实施单元确定；数据和权限契约必须遵守本表。

| 契约 | 所有者/生产者 | 消费者 | 数据与一致性要求 |
| --- | --- | --- | --- |
| Admission / InputReceipt / Lineage | work | GitHub/local admission、workflows、supervision | received 与输入接收同事务；disposition 与 Task/Round/pending 归属同事务；不从任意父任务推断新请求身份 |
| TaskCommands / TaskSnapshot | work | CLI/HTTP、runtime、workflows | 保留 actor、repo scope、expected_version、idempotency key；不绕过原授权 |
| AttemptSnapshot / DispatchDecision | execution | runtime、workflow、readmodels | task/attempt、版本、进程身份和容量证明完整；未知身份继续占位 |
| ProcessRunner / Workspace | execution 声明，具体 adapters 实现 | execution/application | 保留普通和隔离工程修复的不同能力；策略来自控制面 |
| SubmissionEnvelope / ResultReceipt | execution | Worker 入口、重放、delivery | 保留 compact-v1、normalizer version、fingerprint、提交绑定、产物和原序列化 |
| ActionScope / AuditDecision | delivery | result ingestion、workflow、publisher | 纯规则接收控制面证据；Worker 不指定授权目标，不把预审当最终接受 |
| PublicationIntent / Receipt | delivery | publisher、task finalization、supervision | 保留唯一动作身份、remote evidence、unknown/executing 语义、二次权限检查 |
| RecoveryGate | execution/delivery 按消费方定义 | 原业务消费者 | 注入 supervision adapter；claim 在已有锁内持久化，效果前复核，finish 与业务回执同事务 |
| PolicyProvider | 需要实时授权的应用层定义 | publish/recovery 边界 | 提供新鲜有效设置及配置身份；加载失败 fail closed，不把启动快照当永久授权 |
| Observation / Incident / RecoveryRequest | supervision | supervisor、readmodels、maintenance workflow | 只拥有异常/恢复记录；恢复完成需要新的正向业务证据 |
| StageRunner / StageStore | execution | review/vote workflow | 共享容量、监督、封存和局部重试机制；不理解 PR review 或 vote 业务判定 |
| ReadModel DTO | readmodels | Web/CLI/OpenClaw | 只读、脱敏；展示列状态不写回执行生命周期 |

接口落地必须显式表达“未观察到”“不允许”“待对账”和“已证实完成”，不能统一成 bool 成功。内部类型转换不得改变已有外部状态字符串。跨模块只 import 公开 domain/ports/use case，不能读取别的模块私有函数。

## 5. 顺序、所有权与执行单元

执行单元共 11 个，顺序为 U01 → U02 → … → U11。下面“所有权”是变更责任范围，不是子代理分工。run_once、publish、配置和核心测试会被多单元顺序触及，不允许并行改这些文件。

每个单元完成后保持可运行；不先完成一半全仓搬迁再统一修 import。共享检查见第 6 节，单元中仅列最相关的现有测试入口。

### U01：稳定叶子能力、边界门禁和兼容清单

**所有权**：common.py、worker_identity.py、control_lane.py、daemon.py 的进程辅助函数、board_execution.py 的 safe_text、redaction.py、paths.py、resource_files.py；对应 adapters/processes、security 及拟新增 tests/test_architecture_boundaries.py。

**操作**：

1. 从调用者中抽出进程状态/终止、控制命令 deadline、纯脱敏等能力；命名归属按职责，不建立巨型 common。
2. 将 run_once._dispatch_process_status / _terminate_attempt_process 的适用机制下沉；work_item_control、native_rounds、result_cutover、worker_supervision 改用公开能力。
3. control_lane 不再借用 daemon 私有进程函数；work_item_control 不再借用 board_execution 的安全文本。
4. 建立公共入口清单和依赖检查，包含函数内 import。新迁移模块禁止 import runtime/interfaces；既有未迁移代码记录精确例外及删除单元。
5. 使用现有行为测试约束边界，不为每个包装函数新增镜像测试。

**接口交付**：公开进程身份/退出/终止结果；安全文本能力；已证实入口清单。新的进程 API 不额外取得发起任务权限。

**验收**：原进程身份、leader 退出但子组仍存活、deadline、取消和脱敏行为保持；上述两条反向边消失。

**聚焦检查**：
`python3 -B -m unittest tests.test_worker_identity tests.test_process_termination tests.test_dispatch_exit_race tests.test_deadline_remediation tests.test_redaction tests.test_control_lane_recovery`。

### U02：配置和路由的单一来源

**所有权**：validate_config.py、route_config.py、route.py、init_config.py、supervision_config.py；supervision_diagnose/repair 中配置规范化部分；resources/routes.yml、config.example.yml、schemas/config.schema.json 的一致性；configuration、workflows/routing。

**依赖**：U01 的轻量资源/路径能力。

**操作**：

1. 分开加载、兼容输入规范化、类型校验、覆盖解析、外部环境检查；保留 skip_external 的含义。
2. 提取纯设置类型及 supervision 配置规范化，配置模块不 import 诊断/修复执行服务。
3. 从 YAML 产生唯一固定路由 catalog，保留现有全部有效字段及 9 条路由行为。
4. 拆 _effective_route：纯政策选择、配置解析、技能可用性检查分别有明确输入；PR 作者资格与触发信任仍分别校验。
5. 通过 PolicyProvider 适配现有 fresh_config / _authoritative_config，不取消发布前的重新读取与数据库身份核对。
6. 更新消费者所需窄设置，不在本阶段改变对外 version 2 格式。

**接口交付**：规范化配置快照、RoutePolicy catalog、实时 PolicyProvider。

**验收**：全局/仓库覆盖、手动 worker、review 第一 worker/作者白名单、vote 禁用行为完全保持；修改政策只需一个可编辑位置；配置 import 不加载业务执行实现。

**聚焦检查**：
`python3 -B -m unittest tests.test_route_config tests.test_routing tests.test_child_route_policy tests.test_multi_review_config tests.test_worker_adapter_config tests.test_config_and_schema tests.test_config_v2 tests.test_init_and_doctor tests.test_supervision_publication`。

### U03：work 的输入、命令与 handoff 所有权

**所有权**：authorize.py、workstream.py、work_items.py、work_item_control.py、github_operator.py、pending_handoff.py、supervision_lineage.py；run_once 中接收/归属/child 创建相关函数；work/domain、policies、application 和 sqlite adapter。

**依赖**：U01 进程能力、U02 路由/设置。

**操作**：

1. 将 WorkItem/Workstream/Task/Lineage 和输入回执契约归 work；保持原 supervision checkpoint 表与 JSON key。
2. 把 create/start/reply/approve/cancel/retry/retrigger/reopen 的规则和存储操作分开，保留乐观版本及幂等结果重放。
3. 将事件授权、归属、pending append、分类/答复接续移为公开用例；execution_replay 和 github_scan 迁移相关调用。
4. cancel/retry 调用执行公开能力，不回调 run_once 私有函数；迁移期间由装配层注入尚未抽出的能力，桥接必须在 U04/U05 清除。
5. 同一事务完成事件、归属、任务、回执及 wakeup；notify 仍在提交后执行，失败不回滚已成功命令。

**接口交付**：TaskCommands、Admission、TaskSnapshot、handoff 用例；不暴露任意表更新。

**验收**：一个逻辑请求保持正确 lineage；独立新请求不会错误继承预算；输入接收后失败不会静默丢失；取消、closed source、pending publish 等状态约束保持。

**聚焦检查**：
`python3 -B -m unittest tests.test_work_items tests.test_workstreams tests.test_discovery_authorization tests.test_pending_handoff_recovery tests.test_github_board_recovery tests.test_terminal_github_followup tests.test_supervision_lineage tests.test_execution_replay`。

### U04：execution 的准备、派发、容量与运行监督

**所有权**：dispatch.py、supervise.py、worker_supervision.py、worker/heartbeat.py、worker/snapshot.py；execution_store/operations 的执行所有权部分；run_once 中 lease、prepare、dispatch、monitor、timeout/recovery；worktree.py 与 Worker adapters 调用边界。

**依赖**：U03 的任务身份/命令与 U01/U02。

**操作**：

1. 抽取 Attempt、所有权、租约/容量和超时规则；SQL 占位与尝试变更归 execution。
2. 分开任务准备、工作空间、prompt 获取、持久派发意图、启动及启动身份登记。Git/文件准备不得悄然延长 DB 写事务。
3. 将暂停时运行监督接入同一个 monitor 用例，并保留 allow_recovery=False 等现有权限差异。
4. 为普通和工程修复执行准备明确的 profile/workspace 端口；本阶段保留已存在的安全约束，不统一成任意命令透传。
5. 清除 U03 对 run_once 执行能力的临时桥接。

**接口交付**：AttemptSnapshot、ProcessRunner、Workspace、dispatch/monitor/recovery 用例。

**验收**：容量竞争不能重复 launch；未知启动/未知身份继续占位；成功提交不等于进程组退出；暂停不启动新任务；超时恢复预算及退避保持。

**聚焦检查**：
`python3 -B -m unittest tests.test_agent_lease_safety tests.test_sqlite_runtime tests.test_sqlite_phase_boundaries tests.test_dispatch_exit_race tests.test_paused_worker_supervision tests.test_followup_timeout_isolation tests.test_native_launch_slot_recovery tests.test_worktree_safety tests.test_workspace_resolution`。

### U05：结果提交、纯审计契约和恢复交接

**所有权**：worker/result.py、result_contract.py、result_validation.py、result_spool.py、result_cutover.py、action_scope.py、audit_result.py 的共用校验；run_once 中结果加载/预审/最终审计/接续；execution/results 与 delivery 的 domain/policies。

**依赖**：U03/U04。

**操作**：

1. Worker 入口只解析协议并调用用例；envelope 的 compact-v1、NORMALIZER_VERSION 和 fingerprint 算法保持。
2. 把纯验证移入契约/政策模块，消除 result_contract → audit_result → vote_* → result_contract 的循环。
3. 作用域证据读取通过端口实现，Git owner 解析使用 Git adapter，纯规则不 import publisher。
4. review/vote 专属报告校验经显式注入接入。保留预提交与最终审计两个时点，以及 head/dirty worktree、允许目标、已消费输入和 OperatorQuestion 约束。
5. result、actions、wakeup、recovery receipt 的原子性保持；封存/重放与冲突隔离保持。业务完成/交接通过 work 用例推进。
6. 将 publication intent 的类型和存储入口先归 delivery，为 U06 提供稳定契约。

**接口交付**：SubmissionEnvelope、ResultReceipt、AuditDecision、ActionScope、纯审计接口。

**验收**：提交被拒后可以在同 attempt 修正；重复提交/重放不增加第二份动作；不兼容结果不能偷偷按新协议解释；接受结果与后续交付分开。

**聚焦检查**：
`python3 -B -m unittest tests.test_compact_result tests.test_compact_callers tests.test_contract_matrix tests.test_pre_submit_audit tests.test_cutover_recovery tests.test_native_cutover_recovery tests.test_worker_protocol tests.test_supervision_consumers tests.test_analysis_completion`。

### U06：delivery 的发布、对账和终结通知

**所有权**：publish.py、publication_reconcile.py、publication_lock.py、review_publish.py、vote_publish.py；execution_store 的 intent 操作；run_once 中 publication/finalization；supervision_publication 的消费者桥接部分。

**依赖**：U05 的审计/作用域/intent，U03 的任务命令。

**操作**：

1. publisher 只处理已审计动作、intent、远端效果及回执；不回调 run_once._finalize_superseded_result。
2. 将既有 finalize 语义移入应用用例，通过显式事务协调交付事实与任务状态；不新增不可靠的“发通知后再完成”窗口。
3. Git/GitHub 调用归 adapter；审计、目标版本、task lock、去重/对账与重试规则保留。
4. delivery 声明其 RecoveryGate/PolicyProvider；supervision_publication 实现适配，不再借用 run_once 授权判断。
5. 普通 Git 和隔离工程修复 Git 保留两种受信实现，二者共享交付契约而非共享权限。

**接口交付**：发布/对账用例、PublicationReceipt、需要 task finalization 的明确结果。

**验收**：外部成功但事务回滚不重复发布；unknown effect 必须先对账；发布与取消竞争仍有相同锁/fence；撤权、revision 变化和未提交改动不能绕过；恢复 receipt 与发布结果一致。

**聚焦检查**：
`python3 -B -m unittest tests.test_publication_recovery tests.test_publication_reconcile tests.test_publication_lifecycle_followup tests.test_publish_dedupe_acceptance tests.test_review_publish tests.test_supervision_publication tests.test_board_control_integration`。

### U07：多模型阶段机制与具体 workflow 分离

**所有权**：model_execution.py、model_pool.py、native_rounds.py、review_*、vote_*、pr_health.py、remediation.py；execution/stages、workflows/review、vote、pr_remediation。

**依赖**：U03–U06 的公开能力。

**操作**：

1. StageRunner 不依赖 review_coordinator/store/workspace 的默认实现；PR 读取由 GitHub adapter 提供。
2. review 和 vote 各自提供阶段计划、产物规则及窄存储接口；共享身份、容量、进程清理、封存和阶段重试。
3. 原 review_runs/vote_runs 表及 pool 含义保持，不能趁抽象扩大成普通 Worker 共用的全局容量。
4. PR remediation 的 episode、head/base、CI allowlist、预算保持在自己的 workflow。
5. native_rounds 的查询与停止能力按执行和 workflow 归属分开，消除具体 coordinator 回调。

**接口交付**：StageRunner/StageStore、review/vote/remediation 公开用例。

**验收**：同轮模型可并行；成功兄弟产物可复用；只重试失败模型；未知 launch 保留容量；PR 作者资格和普通单模型 fallback 保持。

**聚焦检查**：
`python3 -B -m unittest tests.test_review_parallel tests.test_review_recovery tests.test_review_coordinator tests.test_review_files tests.test_vote_coordinator tests.test_vote_recovery tests.test_vote_processes tests.test_pr_remediation_integration tests.test_remediation tests.test_native_admission_policy`。

### U08：supervision 与 maintenance workflow 分离

**所有权**：supervision_*、supervisor.py、runtime_identity.py；work_items.create_supervision_repair、run_once/publish/action_scope 中工程修复分支；supervision 与 workflows/maintenance 及隔离 Git/模型适配。

**依赖**：U03 的 lineage/admission，U04–U07 的执行、交付和 workflow 能力。

**操作**：

1. supervision 只拥有观察/异常/恢复请求/预算；查询使用公开事实投影，禁止独立维护 task 状态机。
2. RecoveryGate adapter 实现 execution/delivery 的接口；保留 begin 前原门禁、claim 先持久化、效果前 current/guard、finish 同事务及后续 verify。
3. 配置规范化已在 U02 迁移；剩余模型诊断、持久 claim、限流和隔离工作区归相应应用/adapter。
4. supervision_repair 的创建工作项、代码验证、合入核验、部署证明和原任务进展迁入 workflows/maintenance。
5. 将特殊修复执行/发布通过受信 profile 和 adapter 装配，清除普通执行器直接 import maintenance 实现的分支。
6. 原 checkpoint/table/key 和 lineage 不改；没有可证明 provenance 的旧任务继续 observation-only。

**接口交付**：Incident 查询、恢复计划/验证、维护修复 workflow、隔离执行 profile。

**验收**：观察不增加业务权限；恢复预算不因新 attempt/round 重置；未知动作不重复执行；诊断失败不阻塞确定性观察；修复必须分别证明代码、合入、部署和原任务进展。

**聚焦检查**：
`python3 -B -m unittest tests.test_supervision_rules tests.test_supervision_progress tests.test_supervision_actions tests.test_supervision_consumers tests.test_supervision_diagnose tests.test_supervision_repair tests.test_supervision_repair_isolation tests.test_supervision_repair_followup tests.test_supervision_repair_worker tests.test_supervision_delivery tests.test_model_rate_limit_scenarios`。

### U09：上下文、查询与产品入口归位

**所有权**：render_prompt.py、skills.py、project_memory.py、runtime_knowledge.py、memory_curator.py；board.py、board_execution.py、workbench.py、status.py、web.py、web_assets；context、readmodels、interfaces/web。

**依赖**：U03–U08 的稳定查询/命令契约。

**操作**：

1. prompt 组装保留输入 scope、技能、命令路径、前后文 artifact 和版本引用；检索与模板分开。
2. Board/Workbench/Status 构造脱敏 readmodel，不借用业务写用例推断 UI 状态。
3. HTTP 入口仅做参数、身份/CSRF、调用与状态码映射；知识写操作也走对应知识用例。
4. 将 render_dashboard_html 的静态内容移到 Web 资源，保留路由、DOM/API 和输出语义，不同时更换框架或 UI。
5. 保留 status.py 的已生成 Worker 脚本入口，其实现使用 readmodel。

**接口交付**：PromptContext、只读 DTO、HTTP/CLI 消费契约。

**验收**：prompt scope 和受信字段不变；UI 无法放宽操作权限；脱敏/路径/产物访问规则保持；看板更新不触发状态写入。

**聚焦检查**：
`python3 -B -m unittest tests.test_prompt_scope tests.test_source_context tests.test_project_memory tests.test_runtime_knowledge tests.test_board tests.test_board_lifecycle tests.test_workbench tests.test_web_security tests.test_web_review_remediation tests.test_status_defaults`。
Web 资源改动另运行已核实可执行的浏览器单元检查，见第 6 节；真实浏览器行为未验证时明确披露。

### U10：运行入口、存储初始化及运维收口

**所有权**：run_once.py、loop_engine.py、daemon.py、github_scan.py、github_read.py、discover.py、control_lane.py、daemon_state.py；storage.py、sqlite_migration.py；cli、service.py、integrations/openclaw.py；doctor/diagnostics/retention/migrate/init、acceptance 工具、资源定位及打包声明。

**依赖**：U01–U09。

**操作**：

1. runtime/scan、drain、publisher、supervisor、paused_monitor 只编排公开用例；保留常驻三 lane 和同步 once 的不同语义。
2. scan 不再提供 publisher 的业务实现；运行入口只转到 delivery 用例。同步/缓存/归属状态保持分离。
3. SQLite 连接、迁移 registry/runner 集中；业务模块拥有表与转换；保留历史 checksum/ordinal、旧 controls 和失败回滚行为。
4. CLI/OpenClaw/服务安装实现归 interfaces/adapters/operations；保留现有命令参数、只读默认和安装目标。
5. 运维验收脚本归 acceptance，保留随 wheel 提供的命令，不根据文件名把它们删除。
6. 摘除 run_once 的全部业务/SQL 实现；只有经清单证明需要的薄入口继续保留。

**接口交付**：bootstrap 装配、薄 runtime/CLI、统一迁移执行入口。

**验收**：入口不被业务模块反向依赖；常驻扫描不受 worker 满载阻塞；暂停仍监督旧进程；旧 schema 初始化/离线迁移拒绝语义保持；已安装 wheel 资源和入口可用。

**聚焦检查**：
`python3 -B -m unittest tests.test_daemon tests.test_loop_engine tests.test_github_scan tests.test_github_read_cache tests.test_github_scan_remediation tests.test_storage_initialization_remediation tests.test_sqlite_migration tests.test_cli tests.test_service tests.test_openclaw tests.test_operational_commands tests.test_distribution tests.test_package_layout tests.test_acceptance_suite`。

### U11：测试归属、旧路径清理和整体验收

**所有权**：tests/support.py、tests/result_fixtures.py、跨 TestCase fixture；全部实际迁移调用者和旧入口清单；CI mypy 目标、docs/architecture.md、docs/development.md 及引用。

**依赖**：全部前置单元。

**操作**：

1. 将复用 fixture 抽成 builder，按 unit/contracts/integration/scenarios 归类；已有 tests/simulations 保留行为场景。分目录时保留 unittest discover 可发现性及包标记。
2. 更新 patch 目标和内部 import，而不是为测试继续暴露全部旧私有 API。
3. 全树搜索旧/新模块名、动态 import、python -m、脚本路径、资源路径、服务命令、文档链接和外部入口清单。
4. 对新包完整启用边界检查；清除临时桥接例外；已证实的兼容入口只允许向新实现单向转发。
5. 更新 PublicSurfaceTests 的路径白名单时逐条跟随实际代码迁移，禁止扩大为整目录豁免；保留安全扫描语义。
6. CI mypy 目标随模块迁移并覆盖新契约；不要顺手对整仓开启无关严格模式。
7. 完成离线 fixture 升级/恢复、wheel smoke 和完整门禁，更新架构文档。

**接口交付**：最终公开入口/兼容清单、无临时桥接的装配关系、测试路径映射和可复查的整体验收记录。

**验收**：

- 迁移后的生产模块显式静态 import 图无多节点强连通集合；动态 adapter 和入口另有 smoke。不是只删除 import 语句隐藏依赖。
- domain/policies 无 I/O/配置解析/入口依赖；work/execution/delivery/supervision 不 import 具体 workflow。
- 无未解释的跨模块私有调用、临时桥接或死 wrapper。
- 没有业务 SQL 遗留在 run_once/runtime/HTTP handler；明确的运行租约/调度存储通过其所属 adapter 管理。
- 无重复手写路由政策；测试发现数量/集合核对，避免因搬目录悄然少跑。
- 原场景完整保留；其余限制在验收记录中列明。

**聚焦检查**：
`python3 -B -m unittest tests.test_public_surface tests.test_distribution tests.test_package_layout tests.test_docs tests.test_controlled_e2e_acceptance tests.test_workflow_matrix tests.test_reliability_completion`。
上述名称是迁移前的已核实入口；若测试已迁移，必须在该单元记录对应新路径，不能执行旧命令后把“未发现测试”当通过。

## 6. 共享检查和证据要求

### 6.1 基线与每单元

实施前在目标工作树运行完整基线；记录 commit、dirty diff、解释器、依赖及退出状态。已有失败必须先归因，不能把后续出现的失败自动算给当前修改。

各单元先跑所列受影响测试，再运行仓库要求的完整代码门禁。不要每移动一个文件就重复全套；一个可验收单元的相关 diff 固定后复用结果，只有相关变更/失败才重跑。

仓库已支持的命令：

```sh
python3 -B -m unittest discover -s tests
python3 -m ruff check src tests
python3 -B -m compileall -q src
python3 -m build
git diff --check
```

当前 CI 明确的类型检查入口：

```sh
python3 -m mypy src/robert_agent/cli src/robert_agent/paths.py src/robert_agent/configuration src/robert_agent/execution/stages.py src/robert_agent/skills.py src/robert_agent/service.py
```

以上 Mypy 路径在迁移单元中同步更新，新增 domain/ports/config 类型进入检查。build 对资源和打包改动必跑；最终集成按 CI 也运行 build。仓库没有已核实的 make test/make lint，不使用假定命令。

Python 兼容范围维持 3.10–3.14，CI 为 Ubuntu/macOS 矩阵。不能把本机单版本通过声称为整个矩阵通过。

CI 对一次性解释器权限有 maintenance 检查；本机遇到环境型失败时先诊断，不为通过测试直接修改共享 Python 权限。环境不足应使用隔离运行环境或披露验证限制。

### 6.2 崩溃/恢复必须保持的场景

| 场景 | 必须观察到的结果 | 主要责任单元 |
| --- | --- | --- |
| 输入接收后、建任务前失败 | 输入可见并可再次处置，不能误记已消费 | U03 |
| 同一 Workstream 并发输入 | 无并行重复 Task，pending 输入有明确接续 | U03/U04 |
| 启动后身份登记不完整 | 未知所有权占位，不能再次 launch | U04/U07 |
| result 已持久化但进程组仍活 | 不提前释放运行容量或宣称执行清理完成 | U04/U05 |
| SQLite 忙导致 spool | exact attempt 重放；不误消费其他文件 | U05 |
| 预审成功后 head/权限改变 | 最终审计/发布仍拒绝 | U05/U06 |
| 远端成功但本地回执丢失 | 对账识别既有效果，不重复发布 | U06 |
| cancel 与 publish 竞争 | 既有锁与 generation 检查成立 | U03/U06 |
| 一个模型失败、其他模型成功 | 只重试失败项，成功产物仍有效 | U07 |
| 恢复后仍无进展 | 原 lineage 预算继续消耗，不伪造关闭异常 | U08 |
| 暂停 admission | 仍监督旧 worker，不授予新的恢复/启动权限 | U04/U10 |
| 旧安装产物与新代码恢复 | ID、schema marker、spool、pending action 仍可识别 | U11 |

有覆盖则迁移保留，不重复堆积相同断言；缺口优先在原实现上增加能通过的特征/契约测试。若发现真实 bug，按仓库规则先建立失败行为测试，单独记录行为修复范围，不混入“行为不变”结论。

### 6.3 打包与入口

按 docs/development.md 在独立临时 venv 安装本次唯一确定的 wheel，执行 robert --version、robert --help；不要在 dist 存在多个历史 wheel 时使用含糊通配符选择结果。

增加并执行已安装 wheel 的 CLI、Worker/result/snapshot/heartbeat、status 脚本和 package resources 契约检查；所有数据库/配置/工作目录来自临时 fixture。live acceptance/canary 入口移动不授权真正外部写入，测试使用既有受控替身。

Web 测试先检查 tests/browser/*.test.cjs 的 node:test 入口和实际依赖，再执行对应命令；不把普通浏览器脚本 *.js 当成 Node 单元测试，不安装未声明浏览器依赖。真实 UI 验证另记结果。

CI 的依赖审计和 secret-scan 继续保留；本次不新增运行依赖。未运行的远端 CI/平台矩阵和真实服务验证均需明确标记。

## 7. 迁移与回滚

本计划首轮是代码组织迁移，持久化表示不变。因此同一基线的旧/新代码应能读取相同状态；用脱敏或合成 fixture 验证，不能拿生产数据库作为随意试验对象。

- 保持原表、索引约束、migration 名称/checksum/序号、metadata key、指纹与幂等算法。
- 保存迁移前已有版本生成的 prompt、result spool、native round sealed artifacts、pending publication fixture；用新入口继续处理，证明兼容。
- 回滚应恢复完整上一套代码/资源和入口，不能只撤销目录移动的一半。涉及服务版本切换时按部署范围安全停止/排空，禁止新旧控制面同时争抢相同业务所有权。
- 不因移动源码就生成数据 down migration。若实施中发现必须改变持久化格式或外部协议，该部分超出本计划；先明确兼容/迁移/回滚方案再继续其依赖工作。
- 保留 wrapper 只能解决已证实的模块/文件入口兼容，不能代替服务进程、安装路径和在途工作验证。
- 数据恢复问题和结构重构分开处理，不用清库、重置 lease 或删除未知 attempt 来让新结构“跑通”。

## 8. 文档与计划完成检查

本计划已：

- 固定 main revision，并将原工作区和实施基线区分。
- 记录 11 个有顺序、所有权、接口、行为验收和现存聚焦测试入口的单元。
- 明确配置、数据、权限、Worker 协议与运行时升级边界。
- 确认路由政策两份表示的同名字段一致，保留内部派生字段。
- 核对文档中的现存测试模块及主要路径。
- 规定过渡桥接的退出单元、最终依赖门禁和必要兼容入口。
- 保留其他工作区内容；本 PR 运行完整代码门禁并记录实际覆盖，不将后续应用层迁移标记完成。

后续执行记录按单元追加：目标 commit/diff、实际改动、命令和退出结果、发现的契约偏差、剩余桥接及限制。不得用“文件已经移动”替代单元验收。
