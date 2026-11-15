# Robert 架构与运行机制

[文档目录](README.md)

Robert 是运行在本机的 Python 控制面。GitHub 和本地操作提供输入，编码 Worker
完成受限任务，控制面负责授权、调度、监督、审计和发布，SQLite 保存可恢复的事实。

Mermaid 图可直接在 GitHub 阅读。箭头的含义在各图前说明；省略通用日志和工具函数。
先看整体架构与模块表，再按问题进入机制图：

- [整体架构](#整体架构)与[模块关系](#模块关系)
- [事件授权、串行协作与路由](#事件授权串行协作与路由)
- [Issue 到 PR 的时序](#issue-到-pr-的时序)
- [结果提交、唤醒与内部推进](#结果提交唤醒与内部推进)
- [发布去重与崩溃恢复](#发布去重与崩溃恢复)
- [Worker 监督与失败恢复](#worker-监督与失败恢复)
- [独立任务监督](#独立任务监督)
- [多模型评审与投票](#多模型评审与投票)
- [Agent 输入生成](#agent-输入生成)
- [PR 自修复](#pr-自修复)
- [核心数据关系](#核心数据关系)

## 源码分层

源码以领域聚合，领域内部按稳定能力和应用用例分层，仍使用同一 SQLite 数据库。
下列目录与链接对应当前源码中的责任边界。

```text
robert_agent/
  work/                       # 输入/事件规则、查询、任务事实、扫描批次
    application/              # 准入、准备、命令、接续、终结、来源同步
  execution/                  # 所有权、容量、租约、进度/命令记录、阶段机制、轮次
    application/              # 派发、监督、恢复证据、结果回放、就绪判断
  delivery/                   # 合同、纯政策、结果/意图事实、回执相关持久化
    application/              # 结果提交、审计、作用域、外部效果、对账、发布和通知
  supervision/                # 规则、lineage、恢复记录、回执证明、健康事实
    application/              # 观察、诊断协调、发布恢复
  context/                    # 统一 prompt 合同/renderer/阶段输入、skills、项目和运行知识
    application/              # 知识人工审核事务
  workflows/                  # review、vote、PR remediation、maintenance
  configuration/              # 严格 v2、schema、路由、监督配置
  readmodels/                 # dashboard/board/status/workbench/history/usage
  models/                     # SQLAlchemy 2 mapped tables; existing column/storage contracts
  persistence/                # shared queries/stores/transactions/schema/dialects
  adapters/                   # github/git/processes/notifications/services
  application/                # 只保留跨领域的 repository-cycle 组合
  runtime/                    # once/loop/daemon/scan/supervisor/paused monitor
  interfaces/web/             # HTTP、输入/CSRF、模板、服务启动
  cli/, worker/, integrations/ # 稳定的 CLI、Worker 和 OpenClaw 接口命名空间
  foundation/                 # 小型共享值/命令/错误
    contracts/                # 按 work/execution/delivery/supervision 分组的纯值合同
  security/                   # 脱敏和安全文本能力
  operations/, acceptance/    # 数据库升级、运维命令和受控验收
  resources/, web_assets/     # 随 wheel 安装的资源
```

同一领域的概念在同一个包内复用：例如 `work.inputs`、`work.policies`、
`work.control_queries` 同时供工作项用例和读模型使用；展示层通过查询能力读取状态。
`delivery.results` 负责结果事实，`delivery.application.result_audit` 负责审计用例；
`execution.workspace` 负责工作区能力，监控用例通过组合层传入的恢复函数创建新尝试。
暂停监督不会隐式取得恢复权限。

跨业务层、持久层和协议使用的生命周期、状态和动作值定义在
[`foundation/contracts/`](../src/robert_agent/foundation/contracts/) 的领域模块中。
这些模块只包含 `StrEnum` 与不可变值集合，不依赖数据库、配置或业务实现。
任务、工作流、Attempt、模型轮次、发布意图与监督恢复分别持有自己的状态枚举；
监督模式、决策动作、决策状态、调用状态和模型命令结果也各自使用对应的值合同；
同名的 `running` 或 `failed` 不代表同一个状态机。调用方直接导入对应领域的值合同，
数据库写入和 JSON/YAML 等序列化边界使用 `.value`，保留原有字符串编码。
数据库 CHECK、模型映射与协议 Schema 继续声明各自边界约束，合同测试校验共享值的一致性。

[`foundation/timestamps.py`](../src/robert_agent/foundation/timestamps.py) 只使用标准库，
供配置校验、SQLite 方言函数与监督告警共用 UTC 时间解析和微秒比较。
它不执行 I/O，也不依赖领域或用例；通知的启用时间边界在各条投递路径保持一致。
[`foundation/tapd_identity.py`](../src/robert_agent/foundation/tapd_identity.py) 为配置、评论策略和 Worker 适配器
共享 TAPD 身份语法与控制器专用凭据变量名，依赖仅限标准库。

稳定领域能力、纯策略、适配器和读模型不依赖应用用例、具体 workflow、runtime 或接口。
领域 `application/` 与具体 workflow 属于应用层，按明确的模块依赖组合能力；跨领域
调度放在外层组合入口。公共函数/类型/常量归实际 owner；不靠延迟 import、动态查找、
模块别名注册或转发 facade 隐藏依赖。全源码显式 import 图包含函数内 import 并要求无环。

`runtime.cycle` 只负责循环组合，仓库处理流程调用明确的准入/准备/派发/监督/审计/
发布用例。所有业务调用方通过 persistence 的命名操作访问数据库，不直接执行 SQL。事务、savepoint、claim、fence、回执和
幂等记录由相应能力或应用用例处理；外部 I/O 在写事务外执行，回写时重新核对所有权与当前状态。

- `configuration` 不加载执行服务；`resources/routes.yml` 是固定路由的唯一可编辑来源。
- `execution.stages` 通过 `StageStore`、`StageWorkspace`、`StageArtifacts` 接收能力，
  不选择具体 review/vote 工作流。轮次持久化在 persistence/rounds，子进程机制在 execution。
- Worker 命令只解析输入。共享快照/命令机制在 `execution.progress`、`execution.command`，
  结果提交在 `delivery.application.submission`。models 和 persistence 不反向依赖领域或用例。
- `readmodels.dashboard` 负责只读查询与聚合；`interfaces.web.server` 负责 HTTP，
  工作项/知识写入调用相应领域的应用用例。
- 包根目录只保留 `__init__.py`、`paths.py` 和 `resource_files.py`。可执行入口位于所属模块，
  例如 `robert_agent.runtime.scan` 和 `robert_agent.worker.result`。安装检查使用
  [入口清单](../src/robert_agent/resources/entrypoints.json)。
- `readmodels.artifacts` 统一工件预览和可预览提示的路径约束，按目录描述符打开文件。
- workbench 列表读取当前任务的最新尝试、阶段和结果，在分页后补充看板生命周期。
  列表仍扫描轻量任务标识以保留全库搜索、计数和筛选项；完整尝试与工件历史由详情页读取。
- `context.memory_policy` 定义项目记忆的大小和脱敏约束，供提交、审计和入库共用。

[架构门禁](../tests/architecture/test_architecture_boundaries.py) 检查无环、层次方向、根目录合同和
全部业务调用方的 SQL 边界。 [pyproject.toml](../pyproject.toml) 统一配置 Ruff 和 import-linter，
后者约束入口/用例分层、稳定能力、适配器、配置和共享执行阶段；CI 必须通过这些合同。该门禁不能证明任意运行时调用或外部部署都正确；
真实子进程、暂停/恢复、回执/重复发布和已安装 wheel 另由行为场景验证。
目录约束、变更和验收规则见 [AGENTS.md](../AGENTS.md)。执行计划与审查记录只在本地保存，不发布到仓库。

### 数据模型与数据库操作

`models/` 使用 SQLAlchemy 2 声明现有表映射，保留 TEXT JSON、时间字符串、约束和索引。
它不建立连接，也不自动修改数据库。`persistence/` 统一持有查询、写入、批量操作、
条件更新和事务控制；调用方传入过滤值，取得物化记录或影响行数，不传 SQL 片段。
按持久化事实组织的 `queries/` 与 `stores/` 复用表操作；调度、进程占用、发布资格和
只读视图使用共享查询与谓词，避免各业务入口重复拼装相同条件。

应用用例继续拥有事务生命周期。传入连接的操作不自行提交、不换连接，保留
BEGIN IMMEDIATE、savepoint、checkpoint、回滚、超时和受影响行数语义。
连接上下文只管理提交与回滚，连接所有者负责关闭；使用 `closing` 管理其资源生命周期。
SQLite 方言细节集中在 `persistence/dialects/`，历史建表与升级集中在
`persistence/schema/`。SQLAlchemy 映射对应现有 schema，本次访问层切换无需搬迁数据，
不新增迁移版本；当前支持的运行后端仍为 SQLite。

## 整体架构

常驻 daemon 监督扫描和发布子进程，并调用有界的内部推进链路。实线表示主要调用
或数据交接，虚线表示共享持久状态；SQLite 节点概括各组件读写。

```mermaid
flowchart TB
    GitHub["GitHub<br/>Issue · PR · Review · Actions"]
    Human["本地操作员"]
    Config["配置与固定路由策略"]
    subgraph Host["Robert 运行主机"]
        Service["adapters/services/native.py<br/>Supervisor / systemd / launchd"]
        Daemon["runtime/daemon.py<br/>daemon lease · 子进程监督 · 调度"]
        Scan["扫描链路<br/>runtime/scan.py · adapters/github/read.py · adapters/github/discovery.py"]
        Local["内部推进链路<br/>runtime/loop.py → runtime/cycle.py"]
        Publisher["发布链路<br/>runtime/scan.py --publish → delivery/application/publish.py"]
        Supervisor["runtime/supervisor.py<br/>独立观察 · 诊断 · 已知恢复 · 工程修复"]
        UI["interfaces/web/server.py · readmodels/board.py · readmodels/workbench.py<br/>查询与受控操作"]
        Worker["Worker CLI / 内建协调器<br/>独立进程与任务产物"]
        Workspace["adapters/git/workspace.py<br/>仓库专属 Git worktree"]
        DB[("SQLite<br/>事实 · 状态 · 审计 · 发布回执")]
        Files["文件产物<br/>prompt · 日志 · result spool · 封存结果"]
    end
    Human --> UI
    Config --> Daemon
    Config --> UI
    Service --> Daemon
    Service --> UI
    Service --> Supervisor
    Supervisor -.-> DB
    Daemon --> Scan
    Daemon --> Local
    Daemon --> Publisher
    GitHub <-->|"gh 读取"| Scan
    Publisher -->|"审计通过后 gh / git 写入"| GitHub
    Local --> Workspace
    Local -->|"派发与监督"| Worker
    Workspace --> Worker
    Scan -.-> DB
    Local -.-> DB
    Publisher -.-> DB
    UI -.-> DB
    Worker -->|"结构化提交"| DB
    Worker --> Files
    Files -->|"spool 重放 / 产物校验"| Local
```

`robert daemon run` 使用三条链路；`robert daemon run --once` 和 `robert run once`
保留同步诊断路径。[轮询机制](features/github-polling.md)说明缓存、API 配额、观察批次和恢复。

Git worktree 负责工作目录与分支隔离。模型 CLI 以 Robert 服务账户的权限直接运行，
worktree 和文件 hash 校验不提供操作系统沙箱。Worker 按协议只提交计划动作，
对 GitHub 的实际写入由审计后的 publisher 执行。

源码：[常驻调度](../src/robert_agent/runtime/daemon.py)（`_background_decision`、`_resident_loop`）、
[控制子进程](../src/robert_agent/runtime/control.py)、[扫描与发布入口](../src/robert_agent/runtime/scan.py)、
[服务生成](../src/robert_agent/adapters/services/native.py)。

## 模块关系

箭头表示主要调用关系，节点按责任分组，不表示全部 Python import。
`runtime/cycle.py` 组合单次运行，`application/repository_cycle.py` 编排每个仓库的用例；
政策、资源和外部动作由各领域模块负责。

```mermaid
flowchart LR
    CLI["cli/main.py · runtime/daemon.py"] --> Loop["runtime/loop.py"]
    CLI --> Run["runtime/cycle.py"]
    Loop --> Run
    Run --> Repo["application/repository_cycle.py"]
    Repo --> Intake["adapters/github/discovery.py · security/authorization.py · work/streams.py"]
    Repo --> Routing["workflows/routes.py · configuration/routes.py · context/skills.py"]
    Repo --> Prepare["adapters/git/workspace.py · context/prompts.py"]
    Run --> Launch["adapters/processes/worker.py → worker_adapters/"]
    Repo --> Supervise["execution/application/monitor.py → execution/inspection.py · adapters/processes/identity.py"]
    Repo --> Audit["delivery/application/audit.py · delivery/application/scope.py · security/redaction.py"]
    Repo --> Publish["delivery/application/publish.py → delivery/application/reconcile.py"]
    Run --> State["persistence/schema/database.py · persistence/execution_control.py · execution/operations.py"]
    Run --> Rounds["workflows/review/coordinator.py / workflows/vote/coordinator.py"]
    Rounds --> Models["execution/stages.py → persistence/rounds/pool.py"]
```

| 责任 | 主要模块 | 关键契约 |
| --- | --- | --- |
| 外部发现 | [runtime/scan.py](../src/robert_agent/runtime/scan.py)、[adapters/github/read.py](../src/robert_agent/adapters/github/read.py)、[adapters/github/discovery.py](../src/robert_agent/adapters/github/discovery.py) | 完整观察批次先落盘；缓存命中仍需本地授权 |
| 授权与协作线程 | [security/authorization.py](../src/robert_agent/security/authorization.py)、[work/streams.py](../src/robert_agent/work/streams.py) | 正文不能扩大权限；Issue 与 PR 保留各自主线 |
| 路由与准备 | [workflows/routes.py](../src/robert_agent/workflows/routes.py)、[configuration/routes.py](../src/robert_agent/configuration/routes.py)、[context/skills.py](../src/robert_agent/context/skills.py)、[adapters/git/workspace.py](../src/robert_agent/adapters/git/workspace.py)、[context/prompts.py](../src/robert_agent/context/prompts.py) | 固定输出、动作权限与 workspace 策略；缺必需 Skill 时不启动 |
| 执行与监督 | [adapters/processes/worker.py](../src/robert_agent/adapters/processes/worker.py)、[worker_adapters/](../src/robert_agent/worker_adapters/)、[execution/inspection.py](../src/robert_agent/execution/inspection.py)、[adapters/processes/identity.py](../src/robert_agent/adapters/processes/identity.py) | 环境白名单、进程身份、心跳、超时与退出证明 |
| Worker 交接 | [worker/](../src/robert_agent/worker/)、[delivery/contract.py](../src/robert_agent/delivery/contract.py)、[execution/spool.py](../src/robert_agent/execution/spool.py) | 绑定任务与 Attempt；规范化计划动作；持久提交与冲突拒绝 |
| 审计与发布 | [delivery/application/audit.py](../src/robert_agent/delivery/application/audit.py)、[delivery/application/scope.py](../src/robert_agent/delivery/application/scope.py)、[security/redaction.py](../src/robert_agent/security/redaction.py)、[delivery/application/publish.py](../src/robert_agent/delivery/application/publish.py)、[delivery/application/reconcile.py](../src/robert_agent/delivery/application/reconcile.py) | 校验证据、权限与文本；发布锁、远端确认、去重与有限重试 |
| 状态与内部调度 | [persistence/schema/database.py](../src/robert_agent/persistence/schema/database.py)、[persistence/execution_control.py](../src/robert_agent/persistence/execution_control.py)、[execution/operations.py](../src/robert_agent/execution/operations.py)、[work/wakeup.py](../src/robert_agent/work/wakeup.py)、[adapters/notifications/wakeup.py](../src/robert_agent/adapters/notifications/wakeup.py) | SQLite 事务与事实回执；通知只是提示；恢复受主体状态约束 |
| 内建多模型流程 | [workflows/review/coordinator.py](../src/robert_agent/workflows/review/coordinator.py)、[workflows/vote/coordinator.py](../src/robert_agent/workflows/vote/coordinator.py)、[execution/stages.py](../src/robert_agent/execution/stages.py)、[persistence/rounds/pool.py](../src/robert_agent/persistence/rounds/pool.py) | 冻结输入、共享额度、封存输出、复用已校验阶段 |
| PR 自修复 | [workflows/pr_health.py](../src/robert_agent/workflows/pr_health.py)、[workflows/pr_remediation.py](../src/robert_agent/workflows/pr_remediation.py) | 仓库显式开启；证据、版本、次数与预算限制 |
| 操作与可观测性 | [interfaces/web/server.py](../src/robert_agent/interfaces/web/server.py)、[work/application/control.py](../src/robert_agent/work/application/control.py)、[readmodels/status.py](../src/robert_agent/readmodels/status.py)、[operations/diagnostics.py](../src/robert_agent/operations/diagnostics.py)、[delivery/application/notifications.py](../src/robert_agent/delivery/application/notifications.py) | 默认只读；写入需身份、CSRF 与状态校验；诊断导出脱敏 |
| 可选知识与查询 | [context/project.py](../src/robert_agent/context/project.py)、[context/knowledge.py](../src/robert_agent/context/knowledge.py)、[integrations/openclaw.py](../src/robert_agent/integrations/openclaw.py) | 项目记忆与运行知识可复用；OpenClaw 提供查询及显式启用的受认证消息入口 |

## 事件授权、串行协作与路由

这是新外部请求的决策图。系统生成的 PR 自修复请求走单独的策略入口。
可信身份使事件可以驱动工作，正文仍作为不可信输入处理。

```mermaid
flowchart TD
    Observe["扫描保存观察批次"] --> Normalize["本地规范化事件与 fingerprint"]
    Normalize --> Auth{"当前仓库规则允许驱动工作？"}
    Auth -->|"否 / 授权证据不足"| Context["保留上下文或忽略；不派发"]
    Auth -->|"是"| Seen{"事件已消费？"}
    Seen -->|"是"| Ignore["不重复创建任务"]
    Seen -->|"否"| Active{"workstream 有活动任务？"}
    Active -->|"有"| Pending["追加 pending context<br/>等待当前任务交接"]
    Active -->|"无 / 已获准恢复"| Task["创建或恢复 Task"]
    Task --> Route{"能确定意图？"}
    Route -->|"不能"| Classify["分类任务，经同样的执行门禁"]
    Classify --> Classified{"分类结果"}
    Classified -->|"有效推荐路由"| Policy
    Classified -->|"需要澄清"| Question["waiting_for_user，等待授权答复"]
    Route -->|"能"| Policy["解析固定契约与 Worker 配置"]
    Policy --> Skills{"必需 Skill 齐全？"}
    Skills -->|"否"| Block["阻止执行并保留原因"]
    Skills -->|"是"| Prepare["准备 workspace · prompt · Attempt"]
    Prepare --> Capacity{"主体可执行且并发有空位？"}
    Capacity -->|"否"| Queue["保持 prepared，等待后续推进"]
    Capacity -->|"是"| Dispatch["派发 Worker"]
```

指派事件核对指派者，review request 核对请求者，提及核对 actor；历史参与身份和
仓库角色本身不能启动 Worker。当前任务只消费显式报告已处理的事件身份，剩余事件
继续后续任务。`waiting_for_user` 必须收到授权答复才能恢复。

`route_task` 按来源、意图与已有 PR 选择分析、新 PR、更新 PR、源码评审、评审回复、
投票或分类。配置不能扩大固定路由的 `allowed_github_actions`、`expected_output`、
`verification_policy` 与 workspace 权限。

源码：[authorize_event](../src/robert_agent/security/authorization.py)、[plan_event](../src/robert_agent/work/streams.py)、
[route_task](../src/robert_agent/workflows/routes.py)、[仓库用例编排](../src/robert_agent/application/repository_cycle.py)（`run_repo_pipeline`）、
[事件消费与接续](../src/robert_agent/work/application/completion.py)
（`_mark_consumed_events`、`_create_child_task_for_pending_events`）。
行为见 [test_workstreams.py](../tests/work/test_workstreams.py) 和
[test_discovery.py](../tests/adapters/github/test_discovery.py)。

## Issue 到 PR 的时序

这是 `new-pr` 成功路径。扫描、监督、审计与发布可以分多轮推进；箭头表示消息和
持久交接，不表示所有步骤在同一进程或事务中同步完成。

```mermaid
sequenceDiagram
    actor Author as 可信请求者
    participant GH as GitHub
    participant Scan as Scanner
    participant Local as Local drain
    participant DB as SQLite
    participant Worker as Worker
    participant Pub as Publisher
    Author->>GH: 指派 / 提及 Issue
    Scan->>GH: gh 读取正文、讨论和授权证据
    Scan->>DB: 保存观察批次与 wakeup
    Local->>DB: 读取批次、当前 workstream 与政策
    Local->>Local: 授权、路由、Skill 与容量校验
    Local->>DB: 保存 Task、路由、prepared Attempt
    Local->>Worker: 在任务 worktree 启动，stdin 传入 prompt
    Worker->>Worker: 修改、验证、提交本地 Git 变更
    Worker->>DB: 经结果协议保存计划 push、open_pr 与验证证据
    DB-->>Worker: recorded 或 deferred 持久确认
    Worker-->>Local: 进程退出，由监督确认身份与进程组
    Local->>DB: 完成 Attempt，审计并接受计划动作
    Pub->>DB: 读取已审计动作，保存 publication intent
    Pub->>GH: 校验目标与分支；按序 push、open_pr
    Pub->>DB: 保存每个动作的发布回执
    Local->>DB: 完成当前 Task；建立派生 PR workstream
```

新 PR 先发布分支再创建 PR。派生 PR 记录 origin Issue，评审及后续修改归属
PR workstream。任务执行完成、动作发布成功、远端 PR 合并是三种独立事实。

源码：[工作目录策略](../src/robert_agent/adapters/git/workspace.py)、[启动适配器](../src/robert_agent/adapters/processes/worker.py)、
[结果入口](../src/robert_agent/delivery/application/submission.py)、[结果审计](../src/robert_agent/delivery/application/result_audit.py)（`audit_completed_results`）、
[最终完成与派生 PR](../src/robert_agent/work/application/completion.py)
（`finalize_accepted_result`、`_materialize_open_pr_workstream`）。
看板定义见 [web-lifecycle-board.md](features/web-lifecycle-board.md)。

## 结果提交、唤醒与内部推进

校验、规范化与预审计通过后，spool 原子保存最终提交。数据库事务将 result、
计划动作与 `worker_result_ready` 一起保存，提交后尽力发送 Unix datagram 通知。
通知丢失仍能依靠持久状态检查推进。

```mermaid
sequenceDiagram
    participant Worker as Worker
    participant Submit as worker/result.py
    participant Spool as execution/spool.py
    participant DB as SQLite
    participant Daemon as runtime/daemon.py
    participant Local as Local drain
    Worker->>Submit: 绑定 task / attempt 的 compact submission
    Submit->>Submit: 当前契约、scope、证据与预审计校验
    alt 校验不通过或状态暂不可读
        Submit-->>Worker: failed_validation / validation_unavailable
    else 校验通过
        Submit->>Spool: 原子保存规范化结果与 fingerprint
        Spool->>DB: 事务写 result、actions、wakeup
        alt 数据库暂时忙
            Spool-->>Daemon: spool 已保存，best-effort 提示重查
            Spool-->>Worker: deferred，文件已持久保存
            Daemon->>Spool: 有界重放 pending result
            Spool->>DB: 重试同一结果，按身份去重
        else 数据库提交成功
            Spool-->>Worker: recorded
        end
        Submit-->>Daemon: 提交后 best-effort 通知
        Daemon->>DB: 检查持久状态与待推进工作
        Daemon->>Local: 有界推进，常驻模式跳过直接发布
        Local->>DB: 监督退出后最终审计；保存动作接受状态
    end
```

`recorded` / `deferred` 表示持久交接成功，最终审计仍由 Robert 完成。已登记进程
必须确认进程组停止后才进入可发布的完成态。相同最终提交复用原结果，冲突提交拒绝；
迟到结果受当前 Attempt、主体及取消状态约束。

`runtime.loop.run_loop` 限制轮次、总时间、每轮派发数与累计发布数，无进展时停止。
通知只能让 daemon 尽快重查数据库，不能跳过 lease、审计和并发限制。

源码：[record_result 与重放](../src/robert_agent/delivery/application/submission.py)、[原子 spool](../src/robert_agent/execution/spool.py)、
[本地通知](../src/robert_agent/adapters/notifications/wakeup.py)、[有界循环](../src/robert_agent/runtime/loop.py)。
行为见 [test_compact_result.py](../tests/delivery/test_compact_result.py)、
[test_submission.py](../tests/delivery/test_submission.py) 和 [test_wakeup.py](../tests/work/test_wakeup.py)。

## 发布去重与崩溃恢复

publisher 在任务发布锁内核对已审计 scope，记录 intent 与预算，再调用外部命令。
SQLite 写事务不跨越远端副作用。三个远端判定是 `delivery.application.reconcile` 的返回值，
不表示 Task 状态。

```mermaid
flowchart TD
    Ready["已审计动作 + task publication lock"] --> Scope{"主体、scope 与当前目标有效？"}
    Scope -->|"否"| Stop["停止发布，记录 superseded / attention"]
    Scope -->|"是"| Intent["准备 / 读取唯一 publication intent"]
    Intent --> Uncertain{"存在未确认的远端副作用？"}
    Uncertain -->|"是"| Read["只读对账：marker · 正文 · owner · SHA"]
    Read --> Remote{"远端判定"}
    Remote -->|"confirmed_effect"| Receipt["回填 published 回执，不重复写入"]
    Remote -->|"unknown"| Attention["attention，保留证据等待处理"]
    Remote -->|"confirmed_absent"| Budget
    Uncertain -->|"否"| Budget{"重试预算与当前政策允许？"}
    Budget -->|"否"| Failed["失败 / 需处理；显式恢复入口"]
    Budget -->|"是"| Execute["提交 executing intent 与尝试次数"]
    Execute --> Write["调用 gh / git；不持有 SQLite 写事务"]
    Write --> Outcome{"调用结果"}
    Outcome -->|"成功"| Receipt
    Outcome -->|"模糊响应 / 执行后崩溃"| Read
    Outcome -->|"明确失败"| Retry["记录原因与有限退避"]
    Retry --> Ready
```

去重不等于网络请求保证恰好一次。`action_id`、`idempotency_key` 与请求 fingerprint
绑定本地 intent；远端 marker、正文、PR owner、分支与 SHA 证明实际效果。
无法证明存在或缺失时进入 `attention`，不能假设写入失败而重复发布。
发布前的远端校验独立于 discovery 缓存。自动重试受次数、时间与退避约束，
人工重试也须重新核对当前权限和版本。

源码：[publish_ready_actions](../src/robert_agent/delivery/application/publish.py)、
[classify_remote_state](../src/robert_agent/delivery/application/reconcile.py)、
[publication intent](../src/robert_agent/delivery/application/intents.py)、[发布锁](../src/robert_agent/delivery/lock.py)、
[execution/retry.py](../src/robert_agent/execution/retry.py)。
崩溃与重复写入行为见 [test_publication_reconcile.py](../tests/delivery/test_publication_reconcile.py)。

## Worker 监督与失败恢复

心跳、结果、进程身份和进程组退出共同决定下一步。图概括普通 Worker 路径，
多模型阶段和控制子进程有各自的身份记录与清理路径。

```mermaid
flowchart TD
    Check["监督当前 Attempt"] --> Identity{"进程身份与状态可确认？"}
    Identity -->|"否"| Attention["需处理：保留占用与证据"]
    Identity -->|"是"| Live{"进程组仍活跃？"}
    Live -->|"是"| Timeout{"达到硬超时？"}
    Timeout -->|"否"| Heartbeat["按心跳保持 running / 标记 stale"]
    Heartbeat --> Check
    Timeout -->|"是"| Term["终止已证明归属的进程组并确认停止"]
    Term --> Stopped{"停止已确认？"}
    Stopped -->|"否"| Attention
    Stopped -->|"是"| Failed["记录失败原因与恢复证据"]
    Live -->|"否"| Result{"存在有效的持久结果？"}
    Result -->|"是"| Audit["完成 Attempt，再审计结果"]
    Result -->|"否"| Failed
    Failed --> Recover{"当前主体、worktree 与恢复政策允许？"}
    Recover -->|"否"| Manual["失败 / attention，等待明确处理"]
    Recover -->|"是"| Next["保存后继 Attempt 与恢复上下文"]
    Next --> Model["按失败历史选择默认 / fallback model"]
    Model --> Queue["容量允许时重新派发"]
```

`stale` 不表示进程已退出。结果已收到但退出尚未证明时仍计入并发占用；未知身份、
未知启动和未确认清理不能释放额度或盲目拉起替代进程。普通 Worker 可配置
`fallback_model`：同一 workstream 连续两次 `failed_timeout` 后选择备用模型，
effort 保持不变；恢复路径也可能使用已保存的模型恢复信息。

主体恢复、Worker 重试、发布重试与 PR 自修复 Episode 预算是不同层次。
`execution_subjects` 记录 `active`、`terminal`、`attention` 与主体恢复计数。
取消或失效主体不能靠迟到结果继续发布。

结果兼容性门禁由 `execution.application.cutover` 检查：不支持的结果可按任务隔离，
未知 spool 或读取失败会暂停控制面。隔离不证明进程已停；恢复走显式 retrigger。

源码：[classify_attempt](../src/robert_agent/execution/inspection.py)、[进程身份](../src/robert_agent/adapters/processes/identity.py)、
[监督与恢复](../src/robert_agent/execution/application/monitor.py)
（`supervise_running_attempts`、`recover_failed_timeout_attempts`）、
[派发](../src/robert_agent/execution/application/dispatch.py)、
[主体控制](../src/robert_agent/execution/operations.py)、[结果兼容性门禁](../src/robert_agent/execution/application/cutover.py)。
边界见 [test_agent_lease_safety.py](../tests/execution/test_agent_lease_safety.py) 和
[test_cutover_recovery.py](../tests/execution/test_cutover_recovery.py)。

## 独立任务监督

`runtime/supervisor.py` 是独立于业务 daemon 的服务入口。默认 `observe` 只记录观察与
事件；心跳、进程存活或一次恢复函数返回都不能证明业务已取得进展。

显式 `decide` 模式以 Issue/PR 的根 workstream 聚合关联事实与历史，由模型选择下一步。
CI/冲突消费者和 Worker 恢复消费者先上报候选，再检查 `supervision.decisions` 的精确授权；
执行仍由原领域负责。`supervision/application/decision_loop.py` 调度无工具模型命令，
`adapters/processes/model_command.py` 管理有界输出和进程清理。事件变化和到期复查驱动后续决策。
决策、问题、回复及执行回执保存在命名 supervision checkpoint 中，历史决策单独留档。
`work/questions.py` 记录系统问题；已认证 Reply 更新监督上下文而不创建新执行。

```mermaid
flowchart LR
    Facts["目标、事件、当前状态、历史结果"] --> ModelDecision["模型选择下一步"]
    ModelDecision --> Guard["版本、权限、预算、幂等校验"]
    Guard --> Domain["原领域执行修复或恢复"]
    Domain --> Facts
    ModelDecision --> User["询问、通知或等待"]
    User --> Facts
```

`observe` / `recover_known` 保持原模式；以下图描述它们的观察、诊断与已知恢复流程。

```mermaid
flowchart TD
    Observe["读取持久事实与运行证据"] --> Rules["确定性规则：覆盖、停滞、发布与接续"]
    Rules --> Incident["保存 observation / incident"]
    Incident --> Diagnose{"配置无工具诊断 wrapper？"}
    Diagnose -->|"是"| Model["有界证据包 · 调用预算 · hash 校验"]
    Model --> Evidence["保存诊断证据"]
    Incident --> Policy{"recover_known 且仓库/动作允许？"}
    Policy -->|"是"| Claim["持久 claim 与 lineage/stage 预算"]
    Claim --> Consumer["领域消费者重新验证策略、所有权与审计"]
    Consumer --> Receipt["核对确切领域回执与正向进展"]
    Receipt --> Closed{"恢复证据满足？"}
    Closed -->|"是"| Resolve["关闭事件"]
    Closed -->|"未知 / 逾期"| Attention["保留范围、预算和待处理状态"]
    Evidence --> Repair{"产品缺陷且 repair 策略允许？"}
    Repair -->|"backlog"| Proposal["本地维护 WorkItem，等待处理"]
    Repair -->|"auto + 隔离验证通过"| Implement["受限维护 Worker → 审计 → PR"]
    Implement --> Verify["代码 · 主线 · 部署 · 原任务进展分别验证"]
```

已知恢复消费者不扩大原有重试权限，未确认的远端效果必须先对账。自动工程修复需要
显式命令 Worker、受信 launcher 和实际隔离验证；普通模型 CLI 的 worktree 隔离不满足
该条件。PR 合并、部署匹配和原任务进展分别留证。

源码：[观察用例](../src/robert_agent/supervision/application/observation.py)、
[规则](../src/robert_agent/supervision/rules.py)、[恢复事实](../src/robert_agent/supervision/store.py)、
[维护工作流](../src/robert_agent/workflows/maintenance/repair.py)。
配置、命令与限制见 [持续任务监督](features/task-supervision.md)。

## 多模型评审与投票

两种内建协调器共用 `StageCoordinator` 和 SQLite 模型进程池。一轮占一个任务并发槽，
各阶段模型再申请共享额度。图用两个模型说明阶段顺序，实际数量来自配置。

```mermaid
sequenceDiagram
    participant Local as Local drain
    participant C as Review / Vote Coordinator
    participant Pool as SQLite 模型额度
    participant A as 模型 A
    participant B as 模型 B
    participant Files as 冻结输入与封存产物
    Local->>C: 创建绑定触发事件与快照的轮次
    C->>Pool: 为 prepare 原子申请额度
    C->>A: 准备上下文 / 投票问题
    A-->>C: 交接文件，进程组退出
    C->>Pool: 释放 prepare 额度
    C->>Files: 校验、封存并冻结输入 / prompt
    par 模型 A 阶段
        C->>Pool: 申请额度；不足则等待
        C->>A: 冻结 prompt，独立输出目录
        A-->>C: result.json / ballot.json，进程组退出
        C->>Pool: 停止已确认后释放 A 额度
        C->>Files: 校验身份与 hash，封存 A
    and 模型 B 阶段
        C->>Pool: 申请额度；不足则等待
        C->>B: 相同冻结 prompt，独立输出目录
        B-->>C: result.json / ballot.json，进程组退出
        C->>Pool: 停止已确认后释放 B 额度
        C->>Files: 校验身份与 hash，封存 B
    end
    C->>Files: 生成绑定封存结果 hash 的 synthesis inputs
    C->>Pool: 为 synthesis 申请额度
    C->>A: 新会话汇总；逐项复核 / 解释计票
    A-->>C: evaluation.json / synthesis.json，进程组退出
    C->>Pool: 释放 synthesis 额度
    C->>C: 固定渲染报告并核对来源仍有效
    C->>Local: 结构化结果，进入现有审计和发布链路
```

| 机制 | 多模型评审 | 多 Agent 投票 |
| --- | --- | --- |
| 开启条件 | `review-pr` 显式启用且 PR 作者命中 allowlist | 配置启用、来源与投票意图满足路由 |
| 冻结输入 | 当前 head/base、完整 PR diff、源码索引、上下文与 prompt | 当前来源、投票问题、选项与规则；需要时准备源码 |
| 并行输出 | findings、证据与覆盖范围 | 结构化 ballot；`workflows/vote/tally.py` 确定性计票 |
| 汇总 | 逐项评估来源 finding，不用多数票认定缺陷 | 读取 tally，生成解释与分歧报告 |
| 发布 | 绑定 PR head 的 Review `COMMENT` | Issue / PR 评论 |

投票准备不充分时可请求澄清；只失败一个模型时可复用其他已验证成功的阶段结果。
`review_runs` / `vote_runs` 的 `slot_reserved` 计算共享占用；等待额度不计入单个
Worker 的运行超时，父任务期限仍有效。未知启动保留额度。封存是应用层交接校验，
同账户进程仍可能访问其他目录。

源码：[workflows/review/coordinator.py](../src/robert_agent/workflows/review/coordinator.py)、
[workflows/vote/coordinator.py](../src/robert_agent/workflows/vote/coordinator.py)、[StageCoordinator](../src/robert_agent/execution/stages.py)、
[persistence/rounds/pool.py](../src/robert_agent/persistence/rounds/pool.py)、[workflows/vote/tally.py](../src/robert_agent/workflows/vote/tally.py)。
配置与恢复见 [multi-model-review.md](features/multi-model-review.md) 和 [multi-agent-vote.md](features/multi-agent-vote.md)。

## Agent 输入生成

[`context.prompts`](../src/robert_agent/context/prompts.py) 统一生成普通 Worker、原生
review/vote、监督和受限维护的阶段指令。调用方提供结构化事实，公共原则、阶段要求和
交付协议来自 [`resources/prompts/`](../src/robert_agent/resources/prompts/)。
[`prompt_contract`](../src/robert_agent/context/prompt_contract.py) 定义输入和输出值；
[`prompt_profiles`](../src/robert_agent/context/prompt_profiles.py) 限定阶段及适用组合。

```mermaid
flowchart LR
    Facts["领域事实、授权范围、上下文引用"] --> Renderer["context.prompts"]
    Resources["公共约定 + 阶段要求 + 交付协议"] --> Renderer
    Renderer --> Text["八段结构的 Agent 输入"]
    Text --> Existing["既有 Worker / 原生阶段 / JSON 包"]
    Existing --> Result["原领域校验结果与外部效果"]
```

阶段选择和权限仍由流程 owner 决定；context 不导入具体 workflow 或监督执行用例。
helper、原子文件、stdout JSON 保持各自结果格式和完成条件。阶段、上下文和恢复
边界见 [Agent 输入与阶段契约](features/agent-prompts.md)。

## PR 自修复

CI 和冲突修复默认关闭，按仓库 `pr_automation` 显式开启；CI 还要求非空
`check_allowlist`。Episode 将系统请求限制在当前 PR 的版本、次数和预算内。

```mermaid
flowchart TD
    Health["读取 PR head/base 与健康证据"] --> Enabled{"仓库开启对应自动化？"}
    Enabled -->|"否"| Observe["不创建自动修复任务"]
    Enabled -->|"是"| Evidence{"允许的 CI 确实失败 / 存在冲突？"}
    Evidence -->|"等待、成功或证据不足"| Wait["保留观察；不推断失败"]
    Evidence -->|"是"| Episode["创建 / 更新绑定版本的 Episode"]
    Episode --> Budget{"次数、时间、成本与当前状态允许？"}
    Budget -->|"否"| Stop["exhausted / 需处理，保存通知"]
    Budget -->|"是"| Task["受控系统 Task → update-existing-pr"]
    Task --> Worker["当前 PR worktree 修复并验证"]
    Worker --> Audit["审计修复证据与结果版本"]
    Audit --> Fresh{"发布前版本、分支与 PR 状态仍有效？"}
    Fresh -->|"否"| Superseded["superseded，等待新的观察"]
    Fresh -->|"是"| Push["普通 push 与发布回执"]
    Push --> Signal["waiting_for_signal，等待后续健康证据"]
    Signal --> Health
```

push 成功不等于 CI 转绿。后续观察确认修复结果、继续等待或创建下一次有限尝试。
CI 等待、未知 mergeability、不可读取日志与真实失败保持区分；版本变化使旧证据失效。
Episode 的 `resolved`、`exhausted`、`superseded` 与 `canceled` 记录不同终结原因。

源码：[workflows/pr_health.py](../src/robert_agent/workflows/pr_health.py)、[Episode 与预算](../src/robert_agent/workflows/pr_remediation.py)
（`upsert_health_episodes`、`budget_status`、`next_system_decisions`）、
[修复发布校验](../src/robert_agent/delivery/application/publish.py)。行为见 [test_pr_health.py](../tests/workflows/test_pr_health.py)
和 [test_pr_remediation_integration.py](../tests/integration/test_pr_remediation_integration.py)。

## 核心数据关系

这是概念关系图，省略物理字段。实线是主要归属，虚线是关联或投影；并非每条边
都有 SQL 外键。`execution_subjects` 按主体类型与 ID 关联 Task 或 Episode。

```mermaid
flowchart LR
    Repo["repos"] --> WS["workstreams<br/>primary / origin 关系"]
    Source["sources · source_events"] -.-> WS
    WS --> Task["tasks<br/>一次受控工作"]
    WS --> Episode["pr_remediation_episodes<br/>系统修复范围与预算"]
    Task --> Attempt["attempts<br/>可重试的执行尝试"]
    Task --> Route["route_decisions<br/>冻结任务契约"]
    Attempt --> Result["worker_results"]
    Result --> Action["github_actions"]
    Action --> Intent["publication_intents<br/>外部效果与回执"]
    Attempt --> Artifact["artifacts · worker_phases"]
    Task -.-> Control["execution_subjects<br/>active / terminal / attention"]
    Episode -.-> Control
    Control --> Facts["execution_fact_receipts<br/>事实身份与去重"]
    WS -.-> Item["work_items · work_item_events<br/>本地入口与看板投影"]
    Result -.-> Wake["wakeups<br/>待推进提示"]
```

workstream 保留多轮 Task 历史，活动任务由部分唯一索引串行约束。Task 可有多个
Attempt，最终结果与已绑定的 Attempt 交接，发布动作归属 result。
本地和 GitHub 任务进入同一控制面，`work_items` 提供生命周期投影与操作入口。
`sources` 用 provider、host、project_key、source_type 和 external_id 标识外部来源。
GitHub 保留既有来源和事件 ID。TAPD Story 与 Bug 的来源和事件可先存入
`sources` 和 `source_events`，后续确定执行仓库。正式 workstream 仍归属一个仓库。
`ingress_requests` 保存授权证据、内容快照和请求阶段。`task_events` 保存任务的触发与上下文关联。
`request_events` 在任务或来源请求的范围内记录待处理与消费事实，一个范围的变化不会改写其他范围。

原生 TAPD 发布按 `source_required_actions` 的顺序验证各平台回执。GitHub 创建回执可恢复真实 PR 来源和派生
workstream，并保留原 TAPD WorkItem。派生流保存创建关联与审计固定的 base SHA，后续 PR 状态独立更新。
TAPD 准备正文加入已确认的 PR 链接与代码检查摘要。轮次收尾会从原始结果和同一回执重新构造正文，并核对
完整发布顺序。见 [PR 关联](../src/robert_agent/delivery/application/source_pr_lineage.py) 和
[来源收尾](../src/robert_agent/delivery/application/source_completion.py)。

分类和问答不依赖历史 PR 的可用性。开发流程由
[当前 PR 选择](../src/robert_agent/work/application/source_pr_selection.py) 根据创建回执和独立的 GitHub
观测决定新建、复用或暂停；`source_pr_selections` 按轮次和仓库保存选择证据。准备的任务保留原始分类建议、
控制器选择的路由及证据摘要，启动和新发布前重新核验，不改写已冻结的输入、Git 状态或创建回执。

[派生 PR 入口](../src/robert_agent/work/application/source_github_ingress.py) 根据已确认的创建回执核对 GitHub
PR 和原 TAPD 来源。关联 PR 的讨论在普通 GitHub 任务、评审和投票前截取。可信 @ 请求保留真实 GitHub
来源和事件，在 `request_events` 登记一次，并以等待状态保留独立 GitHub 回帖目标。`source_github_ingress`
保存首次和最新观察，缓存重放不替代最新观察，旧上下文编辑不创建新请求。当前入口仅登记请求和上下文。

[GitHub 当前观测](../src/robert_agent/work/application/source_github_observation.py) 在独立的有界读取中核对原 PR、
评论 ID、正文与作者／编辑者。`source_github_observations` 保存本次操作的证据，不承担请求消费。
读取前先使旧证据失效，读取后在写事务中复核来源认领、请求、配置和观测版本。GitHub 网络请求不持有
数据库写锁。已关闭 PR 的状态与评论提及的可信性分别记录。完整当前证据可用于混合轮次入队和执行授权。
[讨论读取适配器](../src/robert_agent/adapters/github/discussion.py) 和 TAPD CLI 复用
[有界进程传输](../src/robert_agent/adapters/processes/bounded.py)，TAPD 适配器保留自身的异常协议转换。

[来源成员查询](../src/robert_agent/persistence/queries/source_members.py) 统一核对 TAPD 与关联 GitHub 请求的
真实事件、冻结版本和创建回执关联。GitHub 的原始作者与创建时间来自不可变的首次记录，普通编辑只更新
最新正文。冻结轮次保留各自的平台身份，结果按实际处理的事件精确更新唯一消费账本，并保存相应 GitHub
回帖目标。活动混合轮次按原成员分批读取当前 GitHub 评论，再核对来源认领、观测版本和冻结输入。
启动前的正文编辑只替换原请求集合；新评论留到下一轮，启动后的提示词保持不变。
明确撤销会取消本轮；读取失败本身不构成撤销。TAPD 已明确失去标签授权时，即使正文暂不可用，
控制器也会暂停原 GitHub 开发请求并记录取消，消费账本和未确认退出的进程占位保持不变。
仓库 Worker 的准备和 PR 选择复核本轮的完整观测证据。启动前另行读取当前 TAPD 与所有 GitHub 触发评论，
将证据绑定到精确任务、attempt、冻结输入和本次观测时间；两次启动写事务都会重新核对。
准备阶段的证据不能替代启动证据，普通编辑导致拒绝旧提示词并交回协调器重新冻结。
启动成功不消费请求。混合轮次的模型使用独立的
[当前模型观测](../src/robert_agent/work/application/source_model_observation.py)，在启动前、身份登记后、
运行中和输出验收前读取两个来源；每次证据绑定具体模型、启动代际、冻结提示词和当前检查。
启动器在身份登记前的停止检查只认可原有精确启动授权，不假定进程已进入运行状态。
真实身份与退出证明独立记录；授权丢失禁止接纳输出，退出未确认仍保留占位。
当前混合请求限于关联 PR 的仓库，模型集成验证覆盖只读子任务后的汇总。
混合结果验收重新核对当前观测与冻结成员，在同一事务中追加控制器的必需 GitHub 回评动作，再消费请求。
回评以 `source_feedback` 保存，直接归属于结果；同一 PR 合并为一个动作，原始触发评论与创建回执关联完整保留。
[回评契约](../src/robert_agent/persistence/rounds/source_feedback.py) 只保存固定状态或代码／检查投影摘要的封闭配方，
使用不含内部身份的回执标记，不接受 Worker 私有正文。原有 Worker 动作顺序和协议限制保持不变。
已消费混合请求的 TAPD 回评通过独立的[交付观测](../src/robert_agent/delivery/application/source_delivery_observation.py)
复核当前 TAPD 与全部原始 GitHub 触发评论。证据绑定当前结果、动作、来源认领和观察版本，不能复用执行授权。
普通编辑不改写已接受输入或消费记录；明确撤销取消后续发布，读取未知则等待。已开始但结果未知的 TAPD
评论仍使用原始准备记录独立回读，确认回执不再发送一次。PR 关联只处理经过审计的代码动作，另行验证必需回评。
混合结果的 GitHub 代码动作在准备发送和最终写入前分别读取两个来源。最终证据绑定已启动的精确发布记录、
准备内容及原始 PR head/base；后一次读取发现的新变化不能被较早预检覆盖，原有推送租约也继续生效。
只允许排除本动作自身的未确认记录，其他未知效果或历史进程仍阻止写入。
专用 [GitHub 回评发布器](../src/robert_agent/delivery/application/source_feedback_publication.py) 使用当前双来源授权，
重建封闭代码／检查正文，验证前序回执后保存原始请求并发布。独立分页和评论读回确认作者、正文与关联 PR；
超时保留未知状态，只对账原请求，不重复发送。全局启用时，已启动请求可在项目配置移除后只读对账原目标。
回评与 Worker 代码动作使用不同回执类型。混合结果的[本地收尾](../src/robert_agent/delivery/application/source_completion.py)
要求完整回执及独立的当前双来源授权；该证明不能授权新的远程写入。最后回执保存后重启，后续发布扫描
可在零发布额度下重新核验并完成原轮次，未处理请求保持待处理。问题通知绑定完整有序回执，查询在分页前
校验各条回执和项目／仓库范围。没有外部观测能力或处于只对账模式时，混合来源收尾保持等待。
已接受的混合轮次仍由协调器复核原始触发。历史处置使用独立的原始结果、取消记录和暂停版本证明；
确认撤销后保留已确认回执，只取消从未开始的剩余动作。标签或处理人失效时，旧开发结果在进程与发布状态
可核验后暂停，允许新的独立只读请求进入队列。旧结果的后续处置不会覆盖新工作项；未知写入仍须先独立对账。
混合结果可在当前已发布选项和新可信回复下认领原交付，认领前后使用不同的双来源证明，
并重新核对原始提示词、Git 和 PR。认领不改变旧取消或消费记录。缺失的 TAPD 回复使用独立的当前发布证明和再次读取的完整单据证据，
在最终本地 Git／提示词核对后发送；已有回执不重复发送。混合结果的 GitHub 回评续发、代码动作续发
和恢复后的收尾尚未接通，历史处置或认领证明不能授权这些动作。

暂停授权的确认使用 `source_confirmation_questions` 和 `source_confirmation_answers`，分别保存当前问题的
版本、作用范围、真实发布回执和可信选项回复。原始与最新的作者自写正文必须匹配同一问题及选项，旧评论编辑
不创建确认，撤销或作用范围变化使确认失效。控制器在来源认领下按命名消费者领取一次确认；确认记录本身不
恢复授权、不派发任务，也不消费工作请求。规则选择复用入队策略，同仓库的多个匹配标签不会产生仓库冲突。
见 [确认策略](../src/robert_agent/work/source_confirmation.py) 和
[确认凭据](../src/robert_agent/work/application/source_confirmations.py)。

同规则恢复由 `source_resume_transitions` 保存已应用选项、原暂停绑定和仓库身份。
启动和发布仍复核该选项的当前证据；未知证据保持等待，明确撤销会取消依赖它的执行。
该转换仅恢复仍有效、尚未消费的请求，消费和取消历史保持不变。

已接受结果因标签或处理人失效暂停时，[交付暂停用例](../src/robert_agent/delivery/application/source_delivery_pause.py)
在同一事务保存 `source_delivery_pauses` 的版本化证据并结束旧执行占位。它核对原结果、冻结输入、
消费成员、动作目标以及已确认或确定未开始的效果；所有历史进程和未知效果仍阻止释放。
确定未开始的暂停动作可让出队列给独立只读调查，但不获得发布权限。
[交付恢复](../src/robert_agent/delivery/application/source_delivery_resume.py) 在当前明确选项下领取
`source_delivery_recoveries`，逐个补发原结果缺少的动作。它复核原提示词、仓库、已审计 Git 对象和
当前 PR 或新 PR 的远端 base，不重跑 Worker，也不重写消费或取消历史。已成功的动作保留原回执。
纯问答可使用执行前记录、结果验收时复核的只读 Git 快照；快照缺失或仓库变化时拒绝补发。
快照仅证明所记录的本地 Git 状态，不提供操作系统文件隔离。

恢复中再次失去同规则授权时，已确定的效果和未开始的剩余动作形成新暂停版本，旧恢复所有者标记
`suspended_at`，后续需要新版本的确认。未知效果仍占用来源，必须先对账。`completed_at` 只记录
所有必需回执得到确认后的本地收尾。WorkItem 保存实际创建子任务的轮次指针，旧结果收尾不会覆盖
后来实际工作拥有的投影。见 [暂停证据查询](../src/robert_agent/persistence/queries/source_delivery_pauses.py)。

原触发评论被明确删除或移除提及、单据进入终态后，
[交付撤销用例](../src/robert_agent/delivery/application/source_retirement.py) 保存
`source_result_retirements`。它保留已确认发布的部分，只跳过有证据证明从未开始的剩余动作；
准备过但未执行的意图记录为本地无效果失败，不伪装成远程回读。原结果、冻结输入、消费记录和取消历史
保持不变。协调器持续检查尚未收尾的历史结果；未知进程或未知发布结果仍会阻止收尾。
后续队列只认可重新核验过的撤销证据，标签或处理人失效仍走可恢复的交付暂停。

原生修复的[预算检查](../src/robert_agent/work/application/repair_cycles.py) 使用原始 PR 创建回执的目标分支，
并核对当前仓库和发布账号。首次观察到目标分支变化也不会扣除修复次数；同一目标分支的 head/base SHA
前进保留原预算。健康收尾检查整个来源的执行及发布占用，不能只检查上一次修复任务。

专用表保留各自边界：`github_scan_batches` / `github_scan_reads` 保存扫描重放输入；
`leases` / `github_control_children` 保存执行所有权；`review_sessions` / `review_runs`
和 `vote_sessions` / `vote_runs` 保存内建轮次；`execution_subjects` 是执行控制的唯一数据源；`execution_fact_receipts` 保存事实去重证据。
`supervision_checkpoints`、`supervision_incidents`、`supervision_recovery_actions`
保存独立监督的观察、事件和恢复记录。

源码：[主 schema](../src/robert_agent/resources/db/schema.sql)、[存储初始化](../src/robert_agent/persistence/schema/database.py)、
[执行主体与事实回执](../src/robert_agent/persistence/execution_control.py)、[persistence/rounds/review.py](../src/robert_agent/persistence/rounds/review.py)、
[persistence/rounds/vote.py](../src/robert_agent/persistence/rounds/vote.py)。基础概念见 [concepts.md](concepts.md)，
配置见 [reference.md](reference.md)。

## 维护图示

改变机制时，同时核对对应源码入口、schema 与行为测试，再更新节点和分支。
架构图表达责任与信任边界，时序图表达持久化与外部效果的先后，流程图保留失败、
等待和未知状态。规划目标、mock 验证或 Worker 声明不能替代当前实现证据。

## 操作员消息边界

[双向消息方案](features/operator-messaging.md)复用工作项事件、通知 outbox 和现有操作命令。
`work/messaging.py` 维护问题关联规则，`delivery/messages.py` 构建脱敏信封，
`adapters/notifications/messaging.py` 发送消息。`work/application/messaging.py`
校验发送者、会话和仓库授权后调用既有命令；OpenClaw 插件只做协议转换。
[Issue 派单](features/issue-dispatch.md) 由 `work/application/dispatch_requests.py`
保存受认证草稿和提交，独立发布通道通过 `application/publication_cycle.py` 创建或对账 Issue。
`dispatch_ingress.py` 将确认回执绑定到 GitHub 来源、原始 `operator_dispatch` 事件和入口，
`dispatch_admission.py` 在取得容量后接入分类器。最终结果审计和 `request_events`
消费证据在同一事务写入，`dispatch_delivery.py` 另行核对该结果的原始 Issue 发布回执。
`work/dispatch_requests.py` 持有派单绑定和当前授权规则，`models/work.py` 映射持久请求。
`persistence` 持有对应的命名查询和条件写入。
持久化统一调用 `persistence/queries/notifications.py`、`persistence/queries/work_item_events.py`
和 `persistence/stores/notifications.py`；问题和通知使用调用方事务原子提交，
收件箱、发送队列和就绪检测在限额前应用相同的未解决问题和仓库筛选。
通知确认不会改变 Supervisor 事件或执行所有权。未来渠道复用同一版本化协议。

原生 TAPD 通知复用[持久目标范围查询](../src/robert_agent/persistence/queries/notification_scope.py)，
从真实任务、轮次和原单据关联取得项目与仓库。发送需匹配显式项目目的地和仓库范围，收件箱及确认收悉
还须匹配已认证操作者的项目权限。这些筛选先于列表限额，并在读取信封和写入确认时复核。
派生 PR 上的历史 GitHub 任务保持原有范围；缺失原生关联不能回退成普通仓库权限。
协调器在保存进度的同一事务写入 [`source_attentions`](../src/robert_agent/persistence/queries/source_attention.py)
和通知 outbox。来源协调告警可以没有 WorkItem 或任务。仓库尚未选定时按单据项目授权，已有可验证的
仓库关联时还须匹配仓库权限。协调器记录相关阶段的进展或退出后结束告警，历史收悉回执仍保留。
普通问题在 TAPD 回执确认并完成来源收尾后，在同一事务写入 `source_question_attentions` 和通知。
Worker 问题要求其项目和仓库范围；仓库识别前的问题使用项目范围，汇总问题要求全部选中仓库范围。
通知按原结果、轮次和发布回执核验，不使用通知正文作为授权，关键确认问题也不会变成普通问答入口。
发布结果未知时，`source_publication_attentions` 将通知绑定到实际来源、结果、动作、发布意图和请求指纹。
通知和未知状态在同一事务落库。后续独立读回核实同一效果后解除对应通知；同一结果的多个效果分别记录。
没有执行轮次的固定控制通知按项目授权，不能借用其他任务的仓库范围。
恢复补发时发现原仓库或执行证据变化，`source_recovery_attentions` 将告警绑定到原已取消轮次、结果和
暂停版本。通知只提示检查及重新处理，不修改原结果、已消费请求或执行状态。原证据重新核实并获准继续，
或该恢复版本完成缺少的交付后，才解除相应告警。
当前原生通知支持确认收到，告警在实际问题解决前仍留在收件箱。收悉不会恢复任务、释放执行占用或增加修复预算。
