# Robert 配置 v2 升级指南

本文适用于配置 v2 版本。升级必须成对切换程序与配置；旧版本程序不能读取以下新格式。

实施方案：[配置 v2 重构](superpowers/plans/2026-11-06-config-v2-refactor.md)。

## 1. 升级约定

- 新版本只接受 `version: 2`，不兼容 v1、无版本或混合新旧字段的配置。
- 手动依据本文转换配置；不提供双格式读取、自动迁移和旧字段别名。
- 将 YAML 的 `<<` 合并键展开为显式字段。重复键会报错；账号、模型名等字符串若类似 YAML 布尔值或数字，请加引号。
- DBOS 执行路径已经移除。删除整个 `workflow_engine`，无需配置引擎或 PostgreSQL 环境变量。本指南用于现有 SQLite 部署，不执行任何数据库引擎迁移。
- 配置升级不变更 SQLite 数据格式，也不清空历史、运行状态、待处理结果或发布记录。
- `config.yml` 和 `config.yaml` 都可作为文件名。默认路径仍为 `~/.config/robert/config.yml`；现有 `--config`、`ROBERT_CONFIG` 优先级不变。

## 2. 调整后的完整 config.yaml 示例

下面列出主要模块及所有可选配置种类。运行参数采用当前默认值；仓库路径、账号、worker 和技能必须替换为部署实际值。这个示例不是对任何现有部署的调参建议。

```yaml
version: 2

data_dir: ~/.local/share/robert
database: robert.sqlite3
# 可选高级设置；默认使用运行 Robert 的解释器，通常应省略。
# python_bin: /srv/robert/.venv/bin/python3

# 必填名称引用，不再隐含使用 workers 第一项。
default_worker: coder

github:
  account: robert-bot
  polling:
    # 常驻启动/扫描失败回退；同时用于同步路径的正常间隔。
    # 扫描成功后仍遵循服务端提示和扫描返回值。
    fallback_seconds: 300
    # 以下仅用于 daemon --once 同步诊断路径。
    synchronous:
      full_capacity_seconds: 600
      rate_limit_cache_seconds: 300
  rate_limit:
    min_search_remaining: 10
    min_core_remaining: 500

scheduler:
  max_concurrency: 3
  # 原生 review/vote 共享模型池；每轮仍占一个任务槽位。
  model_max_concurrency: 4
  local_poll_seconds: 5
  lease_ttl_seconds: 540

supervision:
  # Independent observation service; recovery and diagnosis remain opt-in.
  mode: observe
  poll_seconds: 60
  batch_size: 100
  stall_seconds: 300
  diagnosis: null
  recovery: null
  repair: null
  build_manifest: null
  heartbeat_stale_seconds: 1200
  hard_timeout_seconds: 2700
  startup_grace_seconds: 300

daemon:
  enabled: true
  scan_on_start: false
  control_timeouts:
    github_scan_seconds: 300
    # 同时限制本地 drain 和常驻 publisher 控制子进程。
    local_cycle_seconds: 180

maintenance:
  enabled: true
  interval_seconds: 300
  batch_size: 100
  retention:
    runs_days: 7
    failed_runs_days: 30
    daemon_events_days: 7

workers:
  coder:
    adapter: codex
    command: codex
    model: default
    effort: default
    # 可选；如已有配置应保留实际值，不要照抄未验证模型名。
    # fallback_model: YOUR_SUPPORTED_FALLBACK_MODEL
    timeout_seconds: 5400
    environment_allowlist: []
  reviewer:
    adapter: claude
    command: claude
    model: default
    effort: default
    timeout_seconds: 5400
    environment_allowlist: []

skills:
  search_paths: []

routes:
  new-pr:
    worker: coder
    # 可选，覆盖普通自动执行；省略时继承所选 worker。
    model: default
    effort: default
    required_skills: []
    recommended_skills: []
  review-pr:
    worker: coder
    multi_agent_review:
      enabled: false
      workers: [coder, reviewer]
      author_allowlist: []
  vote:
    worker: coder
    multi_agent_vote:
      enabled: false
      workers: [coder, reviewer]

repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/worktrees/backend
    default_branch: main
    # 可选，只覆盖监听账号，不切换 gh 登录身份。
    github_account: robert-bot
    trusted_actors: [maintainer]
    max_concurrency: 2
    routes:
      new-pr:
        worker: coder
        required_skills: []
        recommended_skills: []
    pr_automation:
      conflict:
        enabled: false
        max_attempts: 2
        max_wall_minutes: 60
      ci:
        enabled: false
        check_allowlist: []
        max_attempts: 2
        max_wall_minutes: 120
        max_total_tokens: 200000
        max_cost_usd: 10.0
        max_failure_summary_chars: 12000
```

PR automation 的 `max_wall_minutes` 是业务 episode 预算，保留其现有字段与分钟单位；统一秒针对调度/监督/进程时间配置，不把业务预算也混入运行超时重命名。PR 自动化配置及其默认禁用行为保持不变。

示例中的新路由技能列表只演示语法。迁移时应保留“省略”和“显式空列表”的区别，不能随手增加空列表覆盖 packaged 推荐技能。

最小配置只需要根级必填项、GitHub 监听账号、一个命名 worker 和至少一个真实仓库；其他模块省略时使用上述默认值，`routes` 默认为空映射、`skills.search_paths` 默认为空列表，所有 PR 自动化默认关闭。

## 3. 旧字段到新字段的完整映射

### 3.1 根字段

| 旧字段 | 新字段/处理 | 升级规则 |
| --- | --- | --- |
| `version: 1` 或缺少版本 | `version: 2` | 仅在完成全部转换后设置；不能只改版本号 |
| `data_dir` | 原样保留 | 必须指向原运行目录 |
| `database` | 原样保留 | 仍是目录内文件名，不能改成新空数据库 |
| `python_bin` | 原样保留 | 可省略；不将旧环境解释器路径替换成当前 shell 的路径 |
| 隐含使用 workers 第一项 | `default_worker` | 填入原第一个 worker 的名称 |
| `max_concurrency` | `scheduler.max_concurrency` | 原值，缺省 3 |
| `model_max_concurrency` | `scheduler.model_max_concurrency` | 原值，缺省 4 |
| `review_max_concurrency` | `scheduler.model_max_concurrency` | 只有旧别名时取其值；两者同时存在必须先确认一致，不任意选一个 |
| `stale_after_minutes` | `supervision.heartbeat_stale_seconds` | 乘以 60；缺省 20 → 1200 |
| `hard_timeout_minutes` | `supervision.hard_timeout_seconds` | 乘以 60；缺省 45 → 2700 |
| `worker_startup_grace_seconds` | `supervision.startup_grace_seconds` | 原值，缺省 300 |
| `lease_ttl_minutes` | `scheduler.lease_ttl_seconds` | 乘以 60；缺省 9 → 540 |
| `workflow_engine.mode` | 删除 | SQLite 是唯一执行路径 |
| `workflow_engine.system_database_url_env` | 删除 | 不转移到任何新字段 |
| `route_worker_models` | 删除，按 3.4 展开到 routes | 不保留空占位对象 |
| `default_max_retries` | 删除 | 旧解析器未使用此字段；不改动已有任务/episode 重试预算 |

### 3.2 GitHub、daemon 与维护

| 旧字段 | 新字段/处理 | 默认/说明 |
| --- | --- | --- |
| `github.account` | 原样保留 | 监听账号 |
| `github.poll_seconds` | 删除 | 旧字段没有控制扫描；不能将它误当作有效周期 |
| `daemon.enabled` | 原样保留 | true |
| `daemon.run_on_start` | `daemon.scan_on_start` | false |
| `daemon.local_poll_seconds` | `scheduler.local_poll_seconds` | 5 |
| `daemon.github_poll_seconds` | `github.polling.fallback_seconds` | 300 |
| `daemon.github_poll_when_full_seconds` | `github.polling.synchronous.full_capacity_seconds` | 600；同步路径专用 |
| `daemon.rate_limit_cache_seconds` | `github.polling.synchronous.rate_limit_cache_seconds` | 300；同步路径专用 |
| `daemon.min_search_remaining` | `github.rate_limit.min_search_remaining` | 10 |
| `daemon.min_core_remaining` | `github.rate_limit.min_core_remaining` | 500 |
| `daemon.live_run_timeout_seconds` | `daemon.control_timeouts.github_scan_seconds` | 300 |
| `daemon.local_drain_timeout_seconds` | `daemon.control_timeouts.local_cycle_seconds` | 180；保留本地 drain/publisher 共享上限 |
| `daemon.maintenance_enabled` | `maintenance.enabled` | true；只控制自动维护 |
| `daemon.maintenance_interval_seconds` | `maintenance.interval_seconds` | 300 |
| `daemon.maintenance_batch_size` | `maintenance.batch_size` | 100，最大 500 |
| `daemon.run_retention_days` | `maintenance.retention.runs_days` | 7 |
| `daemon.failed_run_retention_days` | `maintenance.retention.failed_runs_days` | 30 |
| `daemon.event_retention_days` | `maintenance.retention.daemon_events_days` | 7；不是所有业务事件的统一 TTL |

例如旧 `github.poll_seconds: 30`、`daemon.github_poll_seconds: 300` 同时存在时，新 fallback 应为 **300**。若旧配置只有前者、没有后者，新 fallback 仍为旧有效默认值 **300**；将其改成 30 是另一次调参，不能夹带在迁移里。

### 3.3 workers 与 skills

| 旧字段 | 新字段/处理 | 说明 |
| --- | --- | --- |
| `workers.<name>.adapter` | 原样保留 | 不根据命令名重新推断 |
| `workers.<name>.command` | 原样保留 | 保留 argv、env 前缀、路径、参数和类型 |
| `workers.<name>.model` | 原样保留 | 不升级模型或重设 default |
| `workers.<name>.fallback_model` | 原样保留 | 可省略；不写 null 占位 |
| `workers.<name>.effort` | 原样保留 | 不顺便调节推理强度 |
| `workers.<name>.timeout_seconds` | 原样保留 | 缺省 5400 |
| `workers.<name>.environment_allowlist` | 原样保留 | 缺省 []；继续禁止 GitHub 凭据 |
| `workers.<name>.prompt_transport` | 删除 | 传输固定使用 stdin，无需用户选择 |
| `skills.search_paths` | 原样保留 | 保留路径和顺序 |

若旧 prompt_transport 不是 stdin，先确认真实使用的 adapter；v2 不提供其他传输方式，不能声称无损转换了一个不同协议。

### 3.4 routes 与模型覆盖

| 旧字段 | 新字段/处理 |
| --- | --- |
| `routes.<id>.worker` | 原样保留；不得被 route_worker_models.worker 替换 |
| `route_worker_models.<id>.model` | `routes.<id>.model` |
| `route_worker_models.<id>.effort` | `routes.<id>.effort` |
| `route_worker_models.<id>.worker` | 展开旧 model/effort 默认值后删除，不保留第二个 worker 引用 |
| `routes.<id>.required_skills` | 原样保留 |
| `routes.<id>.recommended_skills` | 原样保留 |
| `routes.<id>.multi_agent_review.enabled/workers/author_allowlist` | 逐字段原样保留 |
| `routes.<id>.multi_agent_vote.enabled/workers` | 逐字段原样保留 |
| `repos[].routes` | 保留原有字段及逐字段覆盖关系；v2 也支持 model/effort |

**关键转换规则：只要旧 route_worker_models 存在某个路由项，就先计算它原来归一化后的 model 和 effort，并将两者都显式写入新路由。**

- 该项有 worker：缺失的 model/effort 从该 worker 取。
- 该项没有 worker：缺失的 model/effort 从旧第一个 worker 取，不是从 routes.worker 取。
- 即使该项是空映射，也不能直接丢掉；它原来会给普通执行写入第一个 worker 的默认模型和 effort。
- 原本根本没有该路由项：新 route 不添加 model/effort，继续随最终选中 worker 默认值。
- 仓库覆盖了 worker 时，全局原有显式模型覆盖仍然生效；不能在转换时误改为仓库 worker 默认模型。
- 新格式发现未知路由 ID 时必须由操作者核对意图，不能把旧静默忽略的项自动改成一个可执行路由。

例：旧配置如下（仅为局部对照，不是独立可运行配置）：

```yaml
workers:
  a:
    adapter: codex
    command: codex
    model: default
    effort: high
  b:
    adapter: codex
    command: /opt/tools/codex
    model: default
    effort: low
routes:
  new-pr:
    worker: b
route_worker_models:
  new-pr:
    worker: a
```

新配置对应部分应为：

```yaml
default_worker: a
routes:
  new-pr:
    worker: b
    model: default
    effort: high
```

workers 定义原样保留；仍执行 b 的命令，但保留旧配置从 a 取到的模型与 high effort。如果认为旧组合本身不合理，可另外调参，不把纠正意图与结构升级混为一步。

原生多模型 review/vote 始终使用各成员 worker 自身 model/effort；route 的两个覆盖字段只影响普通自动执行。手动选择 worker 也使用该 worker 自身配置。

### 3.5 repositories 与 PR 自动化

| 旧字段 | 新字段/处理 |
| --- | --- |
| `repos[].full_name` | 原样保留 |
| `repos[].checkout` | 原样保留 |
| `repos[].worktrees` | 原样保留 |
| `repos[].default_branch` | 原样保留 |
| `repos[].github_account` | 原样保留；省略时继承 github.account |
| `repos[].trusted_actors` | 原样保留，不提升为全局信任列表 |
| `repos[].max_concurrency` | 原样保留；省略时继承 scheduler.max_concurrency |
| `repos[].pr_automation.conflict.enabled/max_attempts/max_wall_minutes` | 逐字段原样保留 |
| `repos[].pr_automation.ci.enabled/check_allowlist/max_attempts/max_wall_minutes` | 逐字段原样保留 |
| `repos[].pr_automation.ci.max_total_tokens/max_cost_usd/max_failure_summary_chars` | 逐字段原样保留 |

PR 自动化的 max_wall_minutes 保留分钟；它约束整个业务 episode，不能替换 supervision.hard_timeout_seconds 或 worker timeout_seconds。

### 3.6 持续监督配置（PR #84）

若部署已经使用持续监督服务，原有 `supervision.mode/poll_seconds/batch_size/stall_seconds`、
`supervision.diagnosis/recovery/repair/build_manifest` 原样保留。将本次从根级移入的
heartbeat_stale_seconds、hard_timeout_seconds 和 startup_grace_seconds 加到同一个
supervision 对象中，不重复定义 YAML 键，也不覆盖已有诊断、恢复或修复授权。

持续监督默认仍为 observe，每 60 秒观察最多 100 项，停滞阈值 300 秒；
diagnosis、recovery、repair 和 build_manifest 默认均为 null。其可选子字段与授权限制见
[持续监督配置说明](task-supervision.md)，本次升级不启用额外恢复或修复能力。

### 3.7 更早的无版本配置：仅供手动查表

以下映射是升级文档，不代表新程序接受这些输入。

| 旧写法 | 手动转换 |
| --- | --- |
| 根 `github_account` | `github.account` |
| 根 `worker_agent/worker_command` | 创建命名 worker，填写 adapter/command，并显式填 model/effort/default_worker |
| workers 列表的 `name` | 改为映射键 |
| 列表 worker 的 `agent/default_model/default_effort` | `adapter/model/effort`；其他 worker 参数按 3.3 保留 |
| 根 `daemon_<字段>` | 按 3.2 对应旧 daemon 子字段转换 |
| `repos[].repo_root/worktree_root/default_base_branch` | `checkout/worktrees/default_branch` |
| `db_path/default_worker 对象/worker_agent_config/skip_external` 等归一化输出 | 不复制到配置；其中 default_worker 在 v2 必须是名称字符串 |

无版本配置的 route_worker_models.worker 可能实际参与 worker 选择，与 v1 不同。必须把旧有效 worker 写入相应 route.worker，再展开模型/effort；不能机械套用 v1“只删除该 worker 引用”的规则。旧根 worker 模式的隐含模型、effort 和命令应从旧程序的有效配置中确认，不从新默认值猜测。

遇到本文未列出的字段时停止转换该项并检查其真实来源，不丢弃未知字段后宣称迁移成功。

## 4. 实际升级步骤

以下流程使用包含配置 v2 支持的新版本制品执行。

### 第一步：记录旧配置和实际生效值

1. 记录当前程序版本、配置路径、启动命令、服务账号、data_dir/database、worker 命令和实际模型配置。
2. 用旧程序执行 `robert config show --config <旧配置路径> --output json`，在受控本地目录保存结果用于比较，不把归一化输出当成新 YAML。
3. 根据映射手动生成单独的 `config.v2.yaml`，保留原配置。展开路由隐式模型值，指定 default_worker，转换时间单位。
4. 复查信任名单、PR 自动化开关和预算、技能覆盖、worker 环境白名单，确保没有误启用功能。

### 第二步：在启动新服务之前验证

使用可独立调用的新版本程序验证候选文件，避免运行中的子进程在升级中混用不同二进制。下列命令仍沿用现有 CLI 形状：

```bash
robert config validate --config /absolute/path/config.v2.yaml --output json
robert config show --config /absolute/path/config.v2.yaml --output json
```

这里的 `robert` 必须指向包含 v2 支持的新版本。比较的是有效行为，不是 JSON 文本完全相同：路径、默认 worker、每仓库每路由的执行者/模型/effort、技能、多模型成员、容量、时间、配额及维护策略。

校验失败就修正文档映射对应项。新程序拒绝旧格式，而不是以一个兼容选项继续启动。不要使用 `robert init --force` 覆盖配置，也不要运行不存在的 config migrate 命令。

### 第三步：停止写入并备份

1. 使用部署现有的 Supervisor/systemd/launchd 等管理方式停止 daemon、web 和独立的 supervisor 服务，防止服务管理器立即重启。
2. 尽量等正在运行的 worker/原生多模型轮次结束；否则按现有停止与恢复流程确认相关进程身份和退出状态，不能只看主 daemon PID。
3. 确认 scanner、publisher、supervisor 的诊断/修复子进程和其他数据库写入者已经退出。无法证明清理完成时，不启动另一个实例争用同一份状态。
4. 保存旧程序制品、原配置及运行目录的一致性备份。SQLite 使用可靠的备份方式，不能在写入中只复制主数据库文件并漏掉 WAL。

### 第四步：成对切换与启动

1. 部署新制品，将服务配置指向已验证的 v2 文件。保持 data_dir/database、仓库和 worktree 路径不变。
2. 按新服务账号执行 doctor，并确认所有配置 worker 的命令可用。
3. 在已有受限容器内以 root 运行 Claude/tclaude 时，保留经确认的 `env IS_SANDBOX=1` 命令前缀；不要给非受限宿主机自动加这个变量。
4. 先启动 daemon 完成其他版本可能需要的附加数据库迁移，再启动 web 和原来启用的 supervisor。使用 `robert supervisor check --config <v2配置路径>` 验证监督服务。通过 config show 确认 version 为 2，启动日志无旧字段错误；确认连接的是旧数据库及旧工作项，而非空目录。
5. 检查本地调度、GitHub 扫描、维护，以及已有待发布/待消费记录的恢复。保持原预算和幂等记录，不手动清表来“修复”启动。

### 第五步：运行验收

- 核对默认 worker、常用 route 与仓库 override 的实际启动命令和模型参数。
- 在已授权的测试仓库执行一次真实 worker 启动；有原生多模型部署时验证相应路径。doctor、版本检查和 mock launch 不是启动成功的证明。
- 确认普通任务和模型池各自遵循容量；检查一次扫描成功后的间隔与失败回退含义。
- 检查历史工作项、pending observations、结果和发布状态是否完整，是否出现重复处理。
- 检查维护策略加载正确；不为了验证而运行破坏性清理或修改保留周期。

未授权外部操作的环境可以先完成只读检查，但真实执行验收须标记为待完成，不能据此宣称部署验证全部通过。

## 5. 回滚

1. 停止新 daemon/web/supervisor，确认 worker、诊断/修复和控制子进程不再写入或执行外部操作。
2. 成对恢复旧程序与原配置，不能让旧程序读取 v2，也不能让新程序读取旧配置。
3. 本次没有数据库格式变更，正常回滚保留最新数据库与审计/幂等记录，不默认恢复升级前备份。该约定适用于数据库/worker 协议相同的两份程序；跨越其他版本的数据库或 worker 协议迁移时，还必须遵循对应迁移的回滚限制。
4. 若升级期间已经发布 GitHub 结果，恢复旧数据库可能丢失已发布事实并造成重复动作；只有在单独核对这些外部事实后，才考虑异常数据恢复。
5. 按旧程序已有恢复流程启动并核对队列。升级失败不授权删除任务、清空租约、重置预算或丢弃待发布记录。
