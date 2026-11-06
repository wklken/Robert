# Robert 配置 v2 重构方案

状态：已按本方案实施配置 v2；升级操作以配套升级指南为准。

设计基线：2026-11-06，工作区 HEAD `bd0b091`。实施基线：main `bb36090`，提交前整合 `2aff552`（PR #84）。本 PR 更新程序、测试、打包资源和文档，不修改实际部署配置或数据库。

配套交付：[完整配置示例、字段映射和升级操作](../../config-v2-upgrade.md)。

## 1. 明确决策

1. 直接切换到 `version: 2`。新程序只接受 v2，不接受缺少版本、v1、旧字段、旧别名或旧列表形式的 workers。不提供双格式读取、自动转换或兼容开关。
2. DBOS 执行路径已经移除。删除 `workflow_engine` 配置及其归一化、环境变量占位和模式兼容分支，不再设计 DBOS 配置迁移流程。当前升级起点是使用 SQLite 的 Robert。
3. 保留 `workers → routes → repos[].routes` 分层。worker 定义执行能力；route 定义任务选择；repository 定义仓库资源、信任和局部覆盖。
4. 将散落的并发、监督参数分别归入 `scheduler`、`supervision`。将 GitHub 轮询及配额参数移到 `github`；维护策略移到 `maintenance`。
5. 删除无效的 `github.poll_seconds`，以实际使用的 `daemon.github_poll_seconds` 为迁移来源。新字段使用不同路径，避免让历史无效配置突然生效。
6. 将 `route_worker_models` 的普通执行模型覆盖合入 `routes.<route>.model/effort`，消除第二个 worker 选择入口。
7. 新增必填的 `default_worker` 名称引用，取代隐含的“workers 第一项”。升级时填入旧配置的第一个 worker 名称，保持旧行为。
8. 调度、监督和进程的超时、间隔、宽限和租约统一用整数秒；保留周期用整数天。原有分钟值精确乘以 60，不调整有效时长。PR 自动化的业务 episode 预算 `max_wall_minutes` 保留原单位，不与运行超时混改。
9. 根和所有固定结构对象拒绝未知字段；动态名称只允许出现在 worker 名称和已知 route ID 对应的位置。Schema、运行时和生成示例使用同一份已审核的字段契约。
10. PR #84 的持续监督字段保留在 supervision 内，与本次移入的 worker 时限共存；其 observe 默认值及诊断/恢复/修复授权不变。
11. 不改变任务生命周期、信任、发布权限、重试预算、审计、脱敏、幂等和 SQLite 持久化协议。配置重构不包含任务监督新功能或数据库格式升级。

## 2. 目标模块与边界

| 模块 | 职责 | 不包含的职责 |
| --- | --- | --- |
| 根级 `version/data_dir/database/python_bin/default_worker` | 配置版本、存储位置、解释器覆盖、默认 worker 引用 | 数据库引擎选择、第二套模型配置 |
| `github.account` | 监听账号默认值 | 凭据、切换 gh 登录身份 |
| `github.polling` | 扫描启动/失败回退，以及同步诊断路径参数 | 任务调度频率 |
| `github.rate_limit` | Search/Core 配额阈值 | 模型请求限流 |
| `scheduler` | 全局任务槽位、原生多模型槽位、本地轮询、任务租约 | 心跳判定和 worker 超时 |
| `supervision` | 启动宽限、心跳陈旧、运行硬上限 | 数据保留、模型预算 |
| `daemon` | 是否启用、启动扫描行为、控制子进程执行上限 | 数据维护策略和 GitHub 配额 |
| `maintenance` | 自动维护开关、间隔、批次及保留周期 | 业务任务重试 |
| `workers` | adapter、command、model、fallback_model、effort、进程超时、环境白名单 | GitHub 授权 |
| `skills` | 技能搜索路径 | 路由选型 |
| `routes` | worker、普通执行模型覆盖、技能、多模型功能配置 | 用户可编辑的 GitHub action 权限 |
| `repos` | checkout/worktrees、分支、信任、仓库并发、路由覆盖、PR 自动化 | 全局模型进程上限 |

不增加只有两个字段的 `storage` 层。`python_bin` 是可选高级覆盖，省略时继续使用运行 Robert 的解释器；无需新增一个只包裹它的 runtime 模块。

## 3. 行为契约

### 3.1 默认值和时间

升级文档的完整示例列出运行参数默认值。手动升级必须复制部署的实际值；不能把完整示例当成覆盖原文件的默认模板。

- 全局任务上限 3，原生 review/vote 共享模型进程上限 4，保持独立。
- 心跳陈旧 1200 秒；运行硬上限 2700 秒；启动宽限 300 秒；任务租约 540 秒。
- 普通 worker 有效运行上限仍取监督硬上限与 worker 上限的较小值。不改变原生多模型协调器现有监督方式。
- `scheduler.model_max_concurrency` 只描述现有原生 review/vote 模型池。不得趁重命名扩展成所有普通 worker 共用的新全局池。
- 不新增 heartbeat/stale/lease 之间的数值大小限制；当前不同机制不构成简单的大小关系。
- `daemon.control_timeouts.local_cycle_seconds` 继续同时供本地 drain 和常驻 publisher 控制子进程使用。名称调整不拆出一个新的独立预算。

### 3.2 GitHub 轮询

`github.polling.fallback_seconds` 继承旧 `daemon.github_poll_seconds` 的有效值：

1. 常驻服务非立即扫描时的初始等待。
2. 扫描未返回有效间隔时的错误回退。
3. `daemon --once` 同步路径的正常轮询间隔。

常驻扫描成功后的间隔仍遵循现有服务端提示及扫描结果；不把 fallback 变成固定频率，不改变现有 60 秒回退规则。

`github.polling.synchronous.full_capacity_seconds` 和 `rate_limit_cache_seconds` 仅用于同步诊断路径。常驻扫描仍不因 worker 满载降频。保留独立的 `synchronous` 子层，以便用户从结构上看出适用入口。

`daemon.scan_on_start` 仅表示是否立即触发 GitHub 扫描，不承诺立即运行 worker，也不绕过授权或容量限制。

### 3.3 路由覆盖与 worker 选择

固定选择顺序：显式手动选 worker；否则仓库路由 worker；否则全局路由 worker；否则 `default_worker`。

手动选择继续使用所选 worker 自己的 model/effort，不套用 route.model/effort。fallback_model 的触发条件和预算不变。

自动普通 worker 的 model/effort 分别按以下顺序解析：

1. 仓库路由显式值。
2. 全局路由显式值。
3. 最终选中 worker 的 model/effort。

字段逐项覆盖；技能列表整体替换。`multi_agent_review`、`multi_agent_vote` 子对象继续整体替换，不新增深度合并。省略字段继承，`null` 不表示清空，空技能列表仍是合法的显式覆盖。现有 packaged route 必需技能的运行约束继续保留。

同一份 route override schema 用于全局和仓库，因此 v2 仓库路由也可以显式设置 model/effort。这是统一配置入口带来的明确能力；迁移时不自动增添不同的仓库模型策略。

### 3.4 普通执行与原生多模型

| 入口 | worker 来源 | 模型/effort 来源 |
| --- | --- | --- |
| 普通自动任务、未进入多模型的 PR review | 有效路由 worker | 路由覆盖，其次所选 worker |
| 手动指定 worker | 手动选择 | 所选 worker |
| 原生多模型 review 的 prepare/review/synthesis | 有效 `multi_agent_review.workers` | 每个命名 worker 自身配置；prepare/synthesis 使用首项 |
| 原生 vote 的 prepare/vote/synthesis | 有效 `multi_agent_vote.workers` | 每个命名 worker 自身配置；prepare/synthesis 使用首项 |

`route.model/effort` 明确只覆盖普通自动执行。不要将一个 adapter 的模型名广播给所有多模型参与者。需要多模型专用配置时定义单独命名 worker，允许同一 adapter/command 有不同模型配置。

`config show` 必须展示上述作用范围：普通自动执行的有效 worker/model/effort，以及多模型成员各自的配置。用户不应只看到一个无法判断用途的 model 值。

本轮保留 `route.worker` 与多模型列表首项相等的约束。它们分别承担普通执行/非白名单退回和多模型成员顺序；不增加新的自动主 worker 推导规则。review/vote 的 enabled 和授权判断仍独立，尤其 vote 禁用不能退化成单模型投票。

### 3.5 校验规则

- 必填：`version: 2`、`data_dir`、`database`、`github.account`、`default_worker`、非空 workers、非空 repos。
- `default_worker`、全部 route.worker、多模型成员必须引用存在的 worker；调整 workers 顺序不能改变执行选择。
- `workers` 只接受映射，worker 必填字段仍是 adapter/command/model/effort；保留 command adapter 必须使用 argv 列表的约束。
- 版本、并发、秒数、天数和批次必须是相应整数，拒绝 bool、数字字符串和小数。开关必须是 bool；不再依赖宽松的 int()/str() 转换吞掉类型错误。
- 正整数与原有上界保持一致，包括维护批次最多 500、仓库任务并发不超过全局值。
- 固定结构 unknown key 和未知 route ID 报完整路径，例如 `routes.reveiw-pr`，而不是被忽略。
- YAML 重复键报错；避免后写值静默覆盖前写值。
- 保留仓库路径、非空 trusted_actors、PR CI 开启必须有 check_allowlist、命令及敏感环境变量现有边界。
- 不增加用户配置 GitHub 凭据、允许动作或信任继承的入口。
- `workflow_engine`、`route_worker_models`、旧根字段、旧 daemon 字段、`prompt_transport` 等在 v2 一律报错。错误可以指向升级文档，但不解析或修正这些旧格式。

## 4. 输入格式与内部结构

唯一输入链路：YAML → v2 结构/类型校验 → 引用和跨字段校验 → 运行时归一化。

内部归一化对象不是第二种受支持的配置文件格式。运行时可以保留有实际调用价值的派生字段，例如 `db_path`、worker command_argv、解析后的 default_worker 对象和仓库路径；不得再把这些字段写回文件供加载器读取。

- 删除所有依 version=1 才启用公共路由或工作区策略的分支；v2 的 route 和工作区约束始终生效，不能因版本升级绕过它们。
- 移除 `route_worker_models` 内部传递和分派分支，使用已解析的有效路由值。
- 统一模型容量内部名称为 model_max_concurrency，不再生产 review_max_concurrency 别名。
- 移除无消费用途的 workflow_engine 内部输出。doctor 可以报告实际 SQLite 状态，但不能再暗示它来自一个用户可选引擎。
- 删除仅为接受历史格式存在的 workers 列表、worker_agent/worker_command 根字段、daemon_*、旧仓库字段解析。内部使用 agent/default_model 等名字本身不等于接受旧配置；不要为消灭字符串而无边界改动持久化快照。
- 不改写已经保存的 review/vote 请求、worker 启动元数据或模型运行记录。它们是运行事实，不是新 YAML 输入。

时间单位更新必须覆盖监督和租约实际消费者。不要将任意秒数整除 60 后交给旧分钟接口，造成精度丢失；可更新内部参数为秒，并测试非 60 倍数。

默认配置文件位置继续为 `~/.config/robert/config.yml`，`ROBERT_CONFIG` 与 `--config` 行为不变；`.yaml` 与 `.yml` 后缀不改变解析语义。

## 5. 实施顺序

以下阶段是一个版本发布中的顺序检查点，不在中间阶段发布半成品。用户当前只授权方案；实施需后续指令。

### 阶段一：固定契约与回归基线

涉及现有文件：`validate_config.py`、`route_config.py`、`run_once.py`、`daemon.py`、`supervise.py`、`loop_engine.py` 及对应测试。

1. 保存干净的实现基线和现有测试结果。当前已有未提交的 prompt 修改、监督方案和 uv.lock，不能纳入本任务；存在文件冲突时使用隔离工作树。
2. 为未知字段/route 被接受、两套 worker 来源混合补充有用的失败行为测试。新格式测试先证明当前拒绝或不能正确执行目标配置。
3. 固定迁移前行为样本：默认值、自定义时间、route_worker_models 不完整项、仓库 worker 覆盖、手动选择、多模型白名单、vote 禁用、满载扫描、维护。
4. 用冻结期望或现有版本离线生成的最小有效结果保存基线，不把旧解析器复制进新程序或测试助手。

验收：每个需要保持的行为有可比较结果，每个需要拒绝的输入有明确错误契约。

### 阶段二：唯一 v2 解析与 Schema

责任范围：`src/robert_agent/validate_config.py`、`resources/schemas/config.schema.json`、`tests/test_config_and_schema.py`、`tests/test_worker_adapter_config.py`。

1. 将解析器改成只接受 v2，建立各模块默认值、字段白名单和严格类型检查，删除所有旧输入分支。
2. Schema 完整声明字段、required/default/范围和 additionalProperties；声明 route.model/effort 的普通执行作用范围。
3. 内部只保留被调用方实际需要的归一化结果，不导出无效 poll、引擎占位和别名。
4. 使用现有 PyYAML 实现重复键检查，不添加运行时依赖。Schema 一致性测试覆盖字段集合、默认值、可选/必填及代表性非法输入；不要为方便加第二套隐藏的默认值来源。

验收：完整示例和最小配置均通过；旧格式、拼错字段和未知 route 均失败；省略可选值获得准确默认值。

### 阶段三：路由、调度、监督和维护接入

责任范围：`route_config.py`、`run_once.py`、`daemon.py`、`github_scan.py`、`loop_engine.py`、`supervise.py`、必要的租约调用、`review_store.py`、`cli/main.py` 的维护入口。

1. 使用一个有效路由解析结果选择普通 worker/model/effort；删除旧 route_worker_models 参数链和 v1 特判。
2. 将秒单位贯穿监督和租约；保留超时取最小值、stale 非终止、启动宽限现有语义。
3. 接入迁移后的轮询、配额、控制超时、维护参数；同步与常驻路径分别验证。
4. 全局/仓库任务容量与原生模型池仍独立。更新容量字段调用和错误文字，不更改池的算法。
5. 验证配置重排不改变扫描 context digest、信任判断或已保存请求；配置格式变化不触发 pending observation 丢弃、预算清零或重复发布。

验收：相同有效值下，分派身份、参数、超时判定、扫描时序和维护范围与旧基线一致；新增的仓库模型覆盖遵循明文优先级。

### 阶段四：更新全部配置生产者与用户入口

责任范围：`init_config.py`、`resources/config.example.yml`、`cli/main.py`、`doctor.py`、`diagnostics.py`、`migrate.py` 中旧配置导入入口、acceptance 配置生成器，以及相关测试和文档。

1. `robert init` 只生成最小 v2：必填项及必要仓库配置，不把所有可选默认值堆进新文件。完整示例和 reference 提供高级项。
2. `config validate/show` 使用同一校验路径。保留现有 normalized 输出性质，并补充有效路由说明；不得把 show 输出宣传为可直接回写的 YAML。
3. 更新 `live_worker_acceptance.py`、`live_worktree_acceptance.py`、`controlled_e2e_acceptance.py` 及测试助手：生成合法 v2，不再用无版本配置、workers 列表或 normalized repos 直接充当外部配置。
4. 现有 `robert migrate dd-github-agent` 会生成旧配置。本次撤下这个旧格式导入命令及只被它调用的旧配置转换逻辑；清理专属测试、帮助和文档。不要让它输出新程序无法读取的文件，也不另建 v1→v2 自动转换命令。数据库历史迁移 SQL、SQLite 状态迁移及独立 DBOS 离线迁移代码不因配置重构被随意删除。
5. 更新 README、guides、reference、multi-model-review、multi-agent-vote、github-polling 和维护说明中的活动示例。历史记录保留其历史语境并明确不适用于当前配置，避免篡改历史证据。
6. 对所有 fixtures、CLI 子进程、Web/服务/诊断读配置入口搜索复核；JSON 形式的配置输入同样只能使用 v2。

验收：所有当前入口均生成/读取同一种格式；公开示例不依赖被删除字段；旧迁移命令不会留下孤立包装器。

### 阶段五：完整验证与发布交接

复用已有 `.venv`，以仓库 CI 为依据执行（这是未来实施命令，不是本次已执行结果）：

```bash
. .venv/bin/activate
python3 -m ruff check src tests
python3 -m mypy src/robert_agent/cli src/robert_agent/paths.py src/robert_agent/route_config.py src/robert_agent/skills.py src/robert_agent/service.py src/robert_agent/migrate.py
python3 -B -m unittest discover -s tests
python3 -m compileall -q src
python3 -m build
git diff --check
```

如旧迁移模块已完全删除，同步调整 CI 的显式 mypy 文件清单，不能继续运行不存在的路径。运行现有 wheel-smoke 和文档链接 CI；没有依赖变更不额外升级依赖。

先运行受影响的配置、路由、daemon、scan、retention、worker、多模型、CLI 测试，再执行上述完整门禁。已有通过结果在输入未变时复用。

最后扫描被删除字段、函数、导入、参数链、动态入口和导出；区分历史文档/持久化协议中的合法名字与残留的活动配置读取。报告没有独立 reviewer 时的审核范围；本方案不自动授权子代理、提交或发布。

## 6. 必须覆盖的验收场景

| 场景 | 成功标准 |
| --- | --- |
| v1、无版本、旧 worker 列表、旧别名 | 拒绝启动并定位错误；不自动转换 |
| 未知根字段、嵌套字段、路由 ID、重复键 | 验证失败，无静默忽略 |
| 重新排列 workers | default_worker 和路由执行结果不变 |
| 旧 model/effort 从另一 worker 取默认值 | 手动升级显式展开后，普通执行参数保持原样；不会换 adapter |
| 仓库只覆盖 worker | 路由已有显式模型值继续继承；不存在模型覆盖时使用新 worker 默认值 |
| 手动选择 worker | 不受 route.model/effort 覆盖 |
| review 白名单内/外、关闭功能 | 多模型成员自身配置与普通执行退回行为分别正确 |
| vote 禁用/未授权 | 保留现有不可执行说明，不转为单模型 |
| 60 倍数与非 60 倍数秒值 | 无截断；监督和租约边界准确 |
| 常驻扫描成功/失败/满载 | 分别遵循服务端间隔、fallback、不满载降频 |
| daemon --once | 同步正常/满载间隔与限流缓存仍生效 |
| 维护开关关闭 | 自动维护不跑；手动 prune 仍可使用同一保留策略 |
| 有 pending observations、未发布结果、保存的多模型 round | 重启后继续按原幂等/预算规则处理；不清空或隐式重建 |
| init、acceptance、CLI、Web、服务 | 全部使用 v2，未保留绕过校验的生产入口 |

## 7. 升级与回滚原则

升级步骤详见配套文档。先完成新文件、静态验证和配置行为比较，再停服务、确认控制子进程清理、备份数据库、成对切换程序与配置。

本次不改变 SQLite schema，不重置任务、租约、工作流、模型运行、结果或待发布记录。不得通过更换 data_dir/database 获得一个看似启动成功的空实例。

回滚必须将旧程序与旧配置成对恢复。新服务运行后可能已产生外部事实，不能随意恢复较早数据库备份而丢弃幂等记录；本次不改数据库格式，优先保留最新数据库。备份用于异常恢复，不是普通回滚必做步骤。

真实 worker 启动验证属于部署执行阶段，在授权环境内进行；config validate/doctor 和 mock 测试不能代替实际启动证明。
