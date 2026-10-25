# Robert 多模型 PR Review 实现方案

日期：2026-10-25。分析基线：`0e0201a834d1c9ffc6cd54a21836ce738b4b1c6a`（已执行 `git pull --ff-only`，本地 main 与拉取后的 origin/main 一致）。
当前授权包含本地实现、完整测试、subagents、提交并 push 到现有 PR #45，以及更新该 PR 描述。不包含运行真实付费模型、在真实测试 PR 上执行 review 发布、合并或发布版本。

## 本轮并发修改检查点

- 修改基线：`ea38417107e58f2a5fb298ea3e99dffcd7277253`，分支 `codex/native-multi-model-review`，已有 PR #45。
- 每轮 review 仍占一个原有任务并发槽；准备、reviewer、汇总各模型进程使用独立的全局子进程池。同轮 reviewer 在额度允许时并行执行。
- 顶层 `review_max_concurrency` 默认 4，严格正整数，不允许 repository 或 route 覆盖；同一数据库/数据目录下所有仓库共享上限。原单模型路径不使用新池。
- SQLite 持久化全局上限和额度认领。等待额度不计入 worker timeout，父任务总时限保留。降低上限不终止已运行模型，提高上限后等待者继续申请。
- 重试只为需要执行的阶段重新申请额度；取消阻止等待者启动，确认模型进程组退出后释放额度；未知启动或未确认退出保留额度。恢复核对已登记进程和文件，避免重复启动。
- 本轮增加并发、全局上限、重试、取消、恢复及单模型兼容验收，完成后跑完整测试和打包门禁；结果由交付说明记录。

## 实现检查点

- 基线：934 项完整测试通过，Python 3.14.7。
- 使用内建 `review_coordinator` 作为原有 attempt 的受监督进程，SQLite 保存 round/run。每轮占用原有一个任务并发槽，reviewer 使用独立全局子进程池并行执行。准备、独立审查和汇总均为新进程；重试仅重新执行未通过文件校验的阶段。
- 原有 `pr_review_comment` 路径保留。匹配作者白名单才切换到 `multi_agent_review_report` / `submit_pr_review`。
- 每个 reviewer 的输出目录由配置名的 hash 派生，避免任意 worker 名称作为路径。
- macOS 使用 sandbox-exec；Linux 使用 bubblewrap。缺少隔离工具时失败并显示原因，不自动降低隔离。
- 文件协议、真实临时 Git 多轮 worktree、适配器 stdin、模型失败重试、文件先落盘恢复、沙箱和 PR Review API 对账已有集中测试。最终完整检查结果见交付说明。
- 交接文件以非阻塞方式打开并验证为普通文件，再复制到 controller 独占的 sealed 目录；完整 seal 可跨数据库登记前崩溃恢复，生产者晚写不会改变已接纳内容。
- 备用模型沿用连续两次超时规则，计数限定在同轮、同阶段、同一 worker；实际 model 在启动前持久化，恢复及报告使用该值，reviewer prompt 保持相同。
- 端到端验收使用真实临时 Git、两种适配器启动的假二进制和假 GitHub API，覆盖指派首轮与代码更新后评论二轮各自完成发布。真实模型认证与 GitHub 写入没有执行。
- 保留每轮工作区和产物供诊断，当前不自动删除历史 worktree。


## 目标与已确定的规则

- 把 github-pr-monitor 的准备上下文、独立 review、复核汇总、报告生成、发布与恢复流程实现为 Robert 内建能力，不创建或依赖该 skill。
- 多个 reviewer 使用同一份 review prompt、同一代码快照、同一上下文；不保留按模型区分的审查重点。
- 主 agent 准备并冻结上下文后，各 reviewer 在全局子进程额度内并行；全部结果有效后再汇总。配置顺序决定报告排列，不要求按完成顺序生成报告。
- 文件是各阶段的交接协议：每轮独立 git worktree；reviewer 自行读取准备好的上下文与源码，写入自己的结果文件；主 agent 读取经校验的结果文件复核汇总。SQLite 保存状态、文件索引、hash 和执行证据。
- 开关开启且白名单命中时运行多个 reviewer；其余情况只运行第一个 reviewer，保留现有单模型路径。
- 白名单仅判断 PR 作者的 GitHub login；Git commit author、committer、Co-authored-by、评论者和请求者均不参与白名单判断，不增加 commit 身份查询。
- 白名单只决定模型数量，不替代 Robert 既有 trusted_actors、事件授权、workstream 和 publication scope 检查。
- 同一个 PR 支持多轮 review：首次可以由可信 PR 指派/review request 或评论激活；作者修改代码后，新的有效 review 评论创建新轮次，重新获取代码和上下文。
- 新轮次与失败重试是不同操作。新轮次不复用上一轮的审查结果，即使 head SHA 未变；故障重试仅允许复用同轮、同快照的有效结果。

## 实施前代码与实现边界

1. `worker_adapters/` 已有 codex、claude、tcodex、tclaude 等适配器；`workers` 已能配置不同命令、模型、effort、timeout。缺口是同一 review 的编排与结果协议。
2. `route_config.py` 目前只允许 worker、required_skills、recommended_skills 覆盖；`run_once._dispatch_queued` 每次选择一个 worker。
3. `worktree.plan_review_worktree` 已获取 PR head 和目标分支，但还没有冻结并生成完整 review context bundle 的接口。
4. `render_prompt.py` 的 `pr_review_comment` 指导 worker 使用 fast-review-github-pr；现有结果要求一个带作者 mention 的普通 comment。新内建多模型路径必须有独立结果契约，不能伪造 used_skills 或沿用不适用的 mention 校验。
5. `publish.py` 的 comment 发布至 issue comments；skill 的正式结果使用 PR Review COMMENT，两者不是同一种动作。
6. 数据库限制一个 workstream 只能有一个活跃 task；一个 task/attempt 只能有一个 worker_result。不能让多个 reviewer 并发提交同一个父 attempt 的结果，也不能把模型参与者当作整任务重试 attempts。
7. 最新代码已删除 DBOS runtime、durable_workflows 和 workflow_driver，执行链路统一为 SQLite。接入 `run_once.py`、`loop_engine.py`、`execution_store.py` 和 `execution_operations.py`，复用 repository lease、task dispatch/publication lock、execution subject 控制和恢复限制。
8. `discover.py` 已提取已知 PR 的评论，`route.py` 已支持可信的 `@bot review ...` 路由；需要补齐 PR 指派、再次 review 的意图识别，以及新请求与上一轮上下文的分离。普通作者反馈仍遵守 review participant 的现有权限。
9. `workstream.plan_event` 会把活跃 PR 的新事件挂入 pending；`_create_child_task_for_pending_events` 当前按首个事件创建一个后续任务。不能直接假定它已经具备逐轮 review 语义。`_prepare_github_retrigger_tasks` 处理的是失败任务的人工重触发，并不等同于评论区再次 review。
10. `dispatch._worker_environment` 目前仅从父进程复制允许的变量，日志写在 prompt 父目录；新增文件协议需要可信的逐 run 环境变量注入和独立日志目录。不能通过修改全局 os.environ 或把结果目录拼进各模型 prompt 实现。

## 配置建议

保持现有 workers 定义；在 review-pr 路由上增加 multi_agent_review 块，功能名为 multi-agent-review。实现后的使用说明见 [多模型 Review](../../multi-model-review.md)。

```yaml
review_max_concurrency: 4

workers:
  reviewer:
    adapter: codex
    command: codex
    model: default
    effort: default
    timeout_seconds: 840
  claude_reviewer:
    adapter: claude
    command: claude
    model: default
    effort: default
    timeout_seconds: 840

routes:
  review-pr:
    worker: reviewer
    multi_agent_review:
      enabled: true
      workers: [reviewer, claude_reviewer]
      author_allowlist: [your-github-login]
```

- `workers` 是有序的已命名 worker 引用，可以继续增加第三个模型，也允许同一 adapter 的不同 model。
- `review_max_concurrency` 是独立于任务 `max_concurrency` 的全局模型子进程上限；默认 4，不允许 repository/route 覆盖。准备和汇总也计算在内，协调器不计算在内。
- `worker` 同时作为主 agent 配置和默认单模型配置；要求列表第一个元素与它相同，避免不命中白名单时选错模型。
- 省略整个配置块时保持旧行为；enabled 默认 false；空白名单不启用多模型。
- 开启时要求至少两个唯一 worker 名称且均存在；配置错误在校验阶段报告，不静默删掉参与者。
- 账号去除输入中的首尾空白和可选 @ 前缀，并按 GitHub login 不区分大小写匹配；只与 PR 元信息中的 author.login 比较。
- PR 作者缺失或查询失败时不启用多模型，记录无法确认作者的原因。后续执行仍须满足原有上下文和授权要求，不能绕过缺失必要元信息的失败处理。
- 支持现有全局和仓库级 route 覆盖；仓库级 multi_agent_review 块整体替换全局块，数组不合并。
- 每轮开始重新判断 PR 作者和当时的 worker/route 配置，随后固化选择结果、作者 login、命中原因与配置指纹。worker/route 修改只影响下一轮；停止当前轮使用既有取消操作。全局子进程上限在下次加载配置时应用到共享池，降低上限不终止已有模型，只限制新增启动。

判定公式：`multi_model = enabled and normalize(pr.author.login) in allowlist`。

## 主流程

```text
可信 review 事件 → 按事件去重并登记轮次 → 等待该 PR 的执行名额
  → 白名单判断 → 重新 fetch 并固化本轮配置与 PR 快照
  ├─ 单模型：现有单 reviewer 路径
  └─ 多模型：Robert 创建本轮 worktree → 主 agent 准备上下文文件 → 冻结共享输入
                → reviewer 1 / reviewer 2 / ... 自行读文件并写独立 result.json
                → Robert 校验所有结果文件 → 主 agent 读文件复核并写 evaluation.json
                → Robert 校验并渲染报告 → 审计 → 发布 PR Review
                → 回查发布结果 → 完成本轮并清理临时 workspace
```

## 同一个 PR 的多轮 review

以下事件去重、轮次和代码快照规则同时适用于单模型与多模型。单模型沿用原审查实现、报告正文和 comment 动作，只补齐轮次归属、快照与发布去重范围；下文 R1/R2 的 PR Review 示例描述多模型路径。

### 请求、轮次、快照和重试的身份

- PR/workstream 是长期身份：`repo + PR number`。
- review 请求是一次明确的用户意图：使用现有可信事件的 fingerprint，绑定仓库和 PR。相同评论被轮询、通知、重放重复发现，只登记一次；不以评论正文或发现时间生成新身份。
- review 轮次使用 `round_id` 和 PR 内递增的 `round_no`，关联 `trigger_event_fingerprint`、触发者、触发时间、前一轮和本轮父 task。数据库约束保证同一个触发事件只能创建一轮。
- 一轮激活时捕获快照：`head_sha + base_ref + base_sha + merge_base_sha`，连同 context/prompt hash 固化。记录 snapshot_captured_at；排队期间读到的 head 只作观察信息，执行开始时重新 fetch，以当时冻结的代码为准。
- reviewer 的执行尝试属于 `round_id + snapshot_id + worker_id`；补跑增加 attempt 序号，不改变轮次或快照。
- 一条新的有效 review 评论创建新 round，即使与上一轮措辞相同或 head SHA 相同。普通 push、普通讨论、已读过的旧 assignment/review request 不自行创建新轮次。
- 评论编辑沿用现有事件身份，不当作新的 review 请求；再次执行通过新评论或新的 review request 表达。

### 触发与轮次串行推进

1. 首次入口覆盖可信用户向 Robert 发出的 PR assignment、review request，以及明确要求 Robert review 的评论。将属于 reviewer 的 PR 指派归入 `review-pr`；issue 指派和明确的代码修改请求保持原有语义。
2. 后续评论继续通过相同授权和意图识别，支持 `@bot review ...`、`@bot review again`、明确的重新审查表达；不能把“作者反馈已修复”、报告引用、提到其他 reviewer 或普通讨论当作新请求。
3. PR 作者命中白名单不授予触发权限；作者的普通参与者评论不升级成 authorized_trigger。用户在可信评论中再次请求 review 后，才开始下一轮。
4. 上一轮已发布且 PR 仍 open 时，为新请求创建新的 task/attempt/session，保留前一轮关联；不重开已完成 task，也不沿用它的 execution subject 或 publication intent。
5. 上一轮仍运行时，新请求单独登记为 queued session，暂不创建活跃 task，不改变正在执行的快照和 prompt。多个不同的有效请求按顺序排队；相同事件只排一次。
6. 上一轮结果只能消费登记给自己的 trigger/context 事件。后来到达的 review 请求不能被模型的 consumed_event_fingerprints 吞掉，也不能只附加为上下文；Robert 审计并强制维持事件到轮次的归属。
7. 前一轮结束后，在事务和现有 task 锁保护下激活下一个 queued session，保证一个 PR 同时只有一个活跃 task；普通讨论事件按既有逻辑处理。失败/取消仍保留停止状态，后续明确的新请求创建新任务，不隐式恢复旧任务。
8. `loop_engine.has_runnable_local_work` 和 wakeup/本地 drain 必须看见可激活轮次。上一轮发布、失败或取消后，即使没有新 GitHub 轮询，也不能让已经排队的请求停滞。

### 两轮之间的代码与报告隔离

```text
PR #42 / review request E1
  → round 1：fetch head=A，固定 base/merge-base
  → 准备 context-1 → 多模型 review → 发布 Review R1（commit_id=A）

作者修改并 push：head=B
可信用户发出新的 review 评论 E2
  → round 2：重新 fetch head=B，重新计算 base/merge-base
  → 准备 context-2 → 全部所选模型重新 review → 发布 Review R2（commit_id=B）

E2 被再次轮询：仍然是 round 2，不新增轮次或重复报告。
round 2 的 Claude 失败：仅重试 round 2 / snapshot B / Claude。
```

- artifact 及 worktree 路径包含 repo、PR、round_id/snapshot_id，禁止只用 `pr-42-review.md` 或可覆盖的 PR 固定 worktree 保存各轮数据。
- 新轮次重新生成全部 diff/context 和运行时信息；审查范围仍是本轮 merge-base 到 head 的完整 PR diff。可以把上一轮报告和修改摘要作为明确标记的历史参考交给所有模型，不能把上一轮报告当作本轮结果，或只审查 A 到 B 的增量。
- fetch 前后验证 PR 元信息与本地 SHA 一致；若准备期间发生更新，丢弃未封存材料并进行有界重试，不能组合 A 的源码和 B 的 PR 元信息。
- 已封存后 head/base/目标分支发生变化，则旧快照结果不能作为当前代码报告发布；本轮记为 superseded，并记录具体变化。新有效请求已排队时继续下一轮，否则明确提示需要重新触发；仅 push 不自动创建新一轮。
- 已发布的 round 1 报告保持历史身份。round 2 是独立的新 PR Review；即使两轮正文相同，也不能被正文 hash 或 PR 级去重吞掉。
- 上轮迟到的进程结果只归档到原 round/run，不能进入当前轮次或触发其发布。force-push、非祖先关系和 base 分支切换都按新快照处理，不假定 A 必然是 B 的祖先。

## Review 执行阶段

### 文件布局与交接责任

本轮代码目录遵守仓库的 worktrees 配置；产物保存在 Robert 的 data_dir 下，两者由 manifest 中的绝对路径关联。下面的路径是实现约定示例，不是已创建的目录：

```text
<repo.worktrees>/review/pr-42/<round_id>/<snapshot_id>/
└── ...                         # 真正的 git worktree，HEAD 固定为本轮 head_sha

<data_dir>/artifacts/reviews/<repo-key>/pr-42/<round_id>/<snapshot_id>/
├── context/
│   ├── manifest.json           # 轮次、SHA、worktree_path、文件索引及 hash
│   ├── pr-info.json
│   ├── github-context.json
│   ├── changed-files.txt
│   ├── diff.patch
│   ├── diff-stat.txt
│   ├── diff-linecount.txt
│   ├── runtime-info.json
│   ├── context-summary.md
│   └── source-index.json
├── reviewer-prompt.md           # 本轮所有 reviewer 共用的已封存文件
├── runs/
│   ├── reviewer/attempt-1/      # 使用配置中的 worker id，不使用模型自报名称
│   │   ├── result.json         # reviewer 写入的权威审查结果
│   │   ├── report.md           # Robert 从有效 result.json 渲染
│   │   ├── stdout.log
│   │   └── stderr.log
│   └── claude_reviewer/attempt-1/
│       └── ...                 # 相同文件协议，独立目录
└── synthesis/
    ├── inputs.json             # Robert 选择的各模型有效结果路径及 hash
    ├── attempt-1/
    │   ├── evaluation.json     # 主 agent 写入的逐项复核和汇总结果
    │   ├── stdout.log
    │   └── stderr.log
    └── merged-review.md        # Robert 从有效 evaluation.json 渲染并封存
```

| 产物 | 写入者 | 读取者与用途 |
|---|---|---|
| worktree、PR 元信息和 diff 文件 | Robert | 主 agent 准备上下文；reviewer 自行读源码和 diff；主 agent 复核 |
| context-summary.md、source-index.json | 准备阶段的主 agent；Robert 校验后封存 | 所有 reviewer 自行读取相同文件 |
| manifest.json、reviewer-prompt.md | Robert | 各 reviewer 读取共同的执行约定和输入索引 |
| runs/<worker>/attempt-N/result.json | 对应 reviewer | Robert 校验；汇总阶段的主 agent 读取 |
| report.md、synthesis/inputs.json | Robert | 主 agent 按显式清单读取本轮各模型结果；用户查看报告 |
| synthesis/attempt-N/evaluation.json | 汇总阶段的主 agent | Robert 校验完整性、来源与格式并渲染最终报告 |
| merged-review.md | Robert | 审计、publisher 和用户 |

### 1. Robert 获取材料，主 agent 准备上下文

保留主 agent 准备环节，同时把可确定执行的 Git/API 操作交给 Robert：

- 获取真实 base 分支、base SHA、head SHA 和 merge-base SHA，fetch 后验证本地 SHA 与捕获值一致；不硬编码 master、仓库或本机路径。
- 每轮使用独立的真正 git worktree，以 detached HEAD 固定到捕获的 head_sha；同轮多个 reviewer 的 cwd 都指向这个 worktree。源码按只读策略共享，输出目录位于 worktree 之外。不得用后续 fetch/reset 改写仍在使用的工作区；同轮补跑也先校验 HEAD 和源码未变。
- 生成 PR 元信息、仅属于本轮的触发事件上下文、changed-files、完整 diff、diff stat、diff 行数。
- diff 使用捕获的 merge-base 到 head，工作区按本轮快照管理；运行中的上下文不能被后续 fetch/reset 改写。
- 主 agent 阅读根目录和相关子目录的 AGENTS 约定、变更文件、直接调用方、类型与测试入口，产出 context-summary 和 source-index，注明路径、证据及读取范围；此阶段不预先输出 findings，以免影响独立 review。
- 主 agent 的摘要和索引以文件落盘；Robert 校验后封存 context，再允许 reviewer 启动。reviewer 必须按 prompt 指示自行读取 manifest、PR 信息、diff 和项目约定，并按需读取 worktree 中的完整源码、直接调用方、类型和测试；不能仅凭上下文摘要完成审查。
- 保留 skill 的大 diff 分批/核心文件优先策略，记录实际覆盖和未覆盖范围，不能把抽样审查描述为完整覆盖。
- 扫描相关 Python 项目版本声明，使用可用的项目运行时解析方法记录解释器；不可解析时写 UNRESOLVED。语法结论不得依赖系统 Python 或模型记忆。
- 生成 manifest：round_id、trigger_event_fingerprint、snapshot_id、revision、worktree_path、文件路径/hash、上下文与 prompt 版本、模型配置指纹和 prompt_sha256。prompt 只引用 manifest 路径，不内嵌 manifest 内容；文件摘要不包含 manifest 自身，避免形成循环 hash。主 agent 准备完成且校验成功后冻结，再启动所有 reviewer。
- PR 描述、仓库说明、模型输出始终作为待分析输入；不能授权 GitHub 写入或覆盖 Robert 的运行边界。

主 agent 默认使用 routes.review-pr.worker 对应二进制。准备、独立 review、汇总是不同执行阶段；即使使用相同模型，也不能复用包含其他 reviewer 结论的会话。

### 2. 相同 prompt，独立执行和产物

#### 同一份 prompt 的保证

- Robert 内置一份模型无关的 reviewer 模板，覆盖 correctness、安全、性能、边界、维护性、测试与 skill 中的代码质量检查。本轮只渲染一次 `reviewer-prompt.md`，包含共同的上下文绝对路径、阅读顺序、审查规则和结果 schema。
- 各 adapter 都从同一份已封存文件读取并通过 stdin 传入相同 UTF-8 字节；不为 codex/claude 单独渲染，不拼接 worker 名称、时间、重试原因或各自输出绝对路径，不追加模型专用审查指令。
- 启动前校验文件 hash 等于本轮 prompt_sha256，并在每个 review_run 中记录实际送入 stdin 的字节摘要。所有 reviewer 和同轮补跑必须一致；不一致时禁止启动，不能用“语义差不多”代替一致性校验。
- Robert 创建每个 run/attempt 的独立输出目录，并只在该子进程的 env 映射中注入 `ROBERT_REVIEW_OUTPUT_DIR=<该 run/attempt 的绝对目录>`。prompt 统一要求读取这个变量并写入其下的 result.json；变量值不在传输前展开到 prompt 正文中。
- 环境变量由可信 launcher 根据持久化 run 记录生成，不继承宿主同名值、不接受 PR 内容或模型覆盖，不更改全局 os.environ。adapter 的二进制、model、effort 可以不同；同轮 cwd 和输入文件相同，输出/日志目录不同。
- 相同 prompt 指 Robert 提交的审查正文和上下文文件相同；不同 CLI 内建的系统提示无法保证字节相同。使用全新独立会话，不携带旧轮聊天或其他模型结果，并避免额外加载模型专属的项目审查指令；记录实际 CLI/model 配置供审计。
- 准备和汇总是主 agent 的独立阶段，可以使用各自的内置 prompt；“同一份 prompt”约束适用于所有独立 reviewer，包括同轮重试。

统一 reviewer prompt 的文件交接指令应明确表达：

```text
读取列出的 manifest、PR 元信息、diff、项目约定和源码索引；
按需自行读取当前 worktree 中的源码、调用方和测试。
从环境变量 ROBERT_REVIEW_OUTPUT_DIR 取得本次结果目录。
将符合约定 schema 的结果写入该目录的 result.json.tmp，关闭文件后
在同一目录原子重命名为 result.json，再结束本次执行。
仅写入自己的结果目录；不修改源码、context 或其他 reviewer 的产物。
```

#### reviewer 写结果文件，Robert 验证并登记

- reviewer 只独立读取输入、审查并写 `result.json`，没有 planned_github_actions，不读取其他 reviewer 的产物，不承担调度、汇总或发布。
- `result.json` 是权威审查结果，含 schema_version、从 manifest 读取的 round_id/snapshot_id/head_sha，以及 findings、dismissed_findings、coverage、merge_recommendation。每个 finding 有本报告内的稳定 id、位置、级别、触发条件、影响、证据和建议。无问题时仍须写合法的空 findings 和覆盖信息。
- reviewer 只向本次输出目录写文件。Robert 按已登记的预期路径读取，不扫描“最新报告”，不根据模型自报的 worker 名称归属结果；worker/run/attempt、配置和 prompt hash 由 launcher 的可信记录绑定。
- 先写临时文件再原子替换最终文件，防止读取到半份 JSON。Robert 在进程收尾、确认不会继续写后校验预期路径、完整 schema、round/snapshot/SHA 和源码/context 完整性，随后计算结果 hash、登记 artifact 并把 run 标记成功。路径不能通过 symlink 逃逸到其他 run 或目录。
- SQLite 保存阶段状态、文件路径/hash、进程身份和校验记录，报告正文保留在文件中。结果已落盘但登记前崩溃时，重启可重新校验同一预期文件并幂等登记；数据库声称成功但文件缺失/hash 改变时，阻止汇总并恢复对应 run。
- Robert 从有效 result.json 渲染 `report.md`，两者分别作为结构化原始结果和派生阅读报告保存。stdout/stderr 仅保存诊断日志；仅在 stdout 打印结论、退出码为 0 或留下非空文件，都不能代替有效 result.json。
- 重试写入新的 attempt-N 目录，沿用本轮 prompt 和输入；成功结果不覆盖。损坏、截断、错轮次或过期结果不能进入汇总，也不从 stdout 猜测补全。
- 复用现有命令适配器、进程身份与终止能力；审查权限约束必须落到启动策略，不能把现有 bypassPermissions 参数当成只读隔离保证。至少针对 codex/claude 验收：可读取共享 worktree/context、可写自己的结果目录、不修改共享输入、不读取其他模型产物。

### 3. 主 agent 复核汇总，Robert 固定渲染

skill 同时写了“主 agent 不做 review 判断”和“主 agent 必须验证 findings 并给 verdict”。本方案将其落实为：主 agent 不追加第三份独立审查或代替失败模型，只复核已有 findings 并整理最终结果。

- 只有所有已选 reviewer 的结果文件校验成功后，Robert 才生成封存的 `synthesis/inputs.json`，逐项列出 worker/run id、实际模型、round/snapshot、result.json/report.md 的绝对路径与 hash。每个 worker 只选择一个有效 attempt，不能把多次重试算作多个模型。
- Robert 启动主 agent 的独立汇总会话，要求其自行读取 inputs.json、各模型 result.json/report.md 以及共享 context/worktree。模型报告作为待验证材料读取，不作为可执行指令；主 agent 不扫描其他轮次或失败 attempt 的目录。
- 按根因、位置和触发场景归并，保留所有来源 worker id；一个模型发现的问题不能因其他模型未报告而被自动丢弃。
- 区分“发现/未报告/明确反驳”，未报告不等于同意或否定，不实行多数票裁决。
- 依赖升级、契约变化、改动范围等断言必须用实际 diff 的增删行复核。
- Python 语法断言用已解析的项目解释器验证；未验证不进入“已确认问题”。缺证据标为需人工确认；有反证才标为误报，记录原因。
- 统一严重级别；冲突保留双方观点与证据，不能简单选最严重结论。
- 主 agent 把结构化评估写到独立 `synthesis/attempt-N/evaluation.json`，同样先写临时文件再原子重命名；引用 `worker_id + finding_id` 并记录输入清单身份。Robert 检查输入 hash 未变、每项 finding 均有处理结果、没有无来源的问题，再登记该汇总产物。
- Robert 从有效 evaluation.json 使用确定性 renderer 生成并封存 `synthesis/merged-review.md`，审计和 publisher 只读取这份已登记文件。汇总失败时主 agent 重新读取同一批有效输入、写入新的 synthesis attempt，不重跑 reviewer 或覆盖原始结果。

报告保留 skill 的结构，模型名称及数量按本轮真实执行动态生成，签名改为 Robert：

```markdown
# PR #<N> Code Review 汇总报告

> 由 <模型 A> + <模型 B> 多模型 review，主 agent 汇总整理。

## Report

审查轮次：<round_no>；代码快照：<head_sha>；目标分支：<base_ref>；触发：<评论或请求链接>。

### Critical Issues
无 / 经复核确认的问题

### High Issues
无 / 经复核确认的问题

### Medium Issues
无 / 经复核确认的问题

### Low Issues
无 / 经复核确认的问题

### Dismissed Findings
无 / 驳回项与原因；证据不足项明确标为需人工确认

### Agent Agreement Summary
| Issue | <模型 A> | <模型 B> | Verdict |
|-------|----------|----------|---------|
| ... | 发现/未报告/反驳 | 发现/未报告/反驳 | 级别/误报/需人工确认 |

**合并建议**：ready to merge / merge after fixes / needs significant rework

---
*由 <真实参与模型> review，主 agent 汇总 | [from Robert]*
```

选取一个合并建议并附中文理由；无 findings 也保留所有标题与表头。第三个 reviewer 增加相应列。原始报告、复核记录、最终报告都进入 artifact 系统。

### 4. 审计和 GitHub 发布

- 为新流程新增明确的结果类型及 `submit_pr_review` 动作，限定 event=COMMENT；沿用旧单模型路径的 comment 契约。
- 新路径以可信的编排证据替代强制 skill 使用证明，used_skills 可以为空；不伪造 skill 执行记录。
- 模型不直接调用 gh 发布；只有 publisher 在完成来源、仓库、PR、SHA、报告齐全与格式审计后发布。
- 逻辑发布身份以 `repo + PR number + round_id + action_type` 唯一确定；snapshot_id 和最终 body hash 绑定该身份用于完整性校验。同轮发布重试沿用同一 intent/隐藏标记；新轮次使用新身份，即使正文相同也允许独立发布。
- 发布前重查 PR 状态与 head/base/目标分支；已关闭则停止，快照变化则把当前 session 标记 superseded，不以新 HEAD 重标旧报告，不跨轮重试旧 action。
- 发布明确绑定被审查的 head SHA；发布期间仍可能发生远端更新，因此报告与存档始终标明审查 revision。
- 发布响应不明确时先查询 PR Reviews；按本轮标记和所审查 SHA 认领已有结果。即使 PR 此时已更新，已成功发布的旧轮报告仍记为旧轮成功，不能再发一份。查无结果后必须重新通过本轮状态/SHA 校验才能 POST；查询失败保持未知并重试，不回退成普通 issue comment。
- 新报告不强加现有单模型路径的 @author 段落，保留 skill 的正文结构；内部幂等信息使用隐藏标记。
- 触发事件在登记轮次时持久化认领关系，在发布成功后记为完成；失败、取消、superseded 使用明确的独立处置记录，不能标成成功，也不能让旧事件在下一次轮询创建新轮次。cleanup 不删除报告、事件归属和恢复证据。

## 持久化、恢复与资源控制

推荐新增两个小的领域表，复用 task/workstream/attempt/artifacts，而不是泛化成任意 DAG 平台：

- `review_sessions`：保存 PR/workstream、round_id/round_no、前一轮、唯一 trigger_event_fingerprint、触发与处置证据、快照、白名单决策、冻结配置、context/prompt hash、阶段和发布状态。queued session 暂不绑定活跃 task，激活时关联新的父 task/attempt。建立 PR 内轮次唯一约束和触发事件唯一约束。
- `review_runs`：关联 session 与 snapshot_id，保存 prepare/reviewer/synthesis 阶段、worker id、执行次数、实际 model、进程身份、启动意图、状态、时间、失败原因和产物 hash。

父 task 仍是唯一活跃任务。prepare/reviewer/synthesis 的结果进入 review_runs，最终只有聚合结果进入父 worker_result。父任务的完成、超时与失败判定必须识别这类编排任务，不能用“没有单个 worker PID”将它误判失败。

轮次状态独立于现有 task.lifecycle；例如 superseded 属于 review session，父 task 映射为现有终态并保存 reason_code，不直接向 task.lifecycle 写入新枚举。失败重试若按既有机制生成新 task/attempt，必须显式承接同一轮且记录绑定历史；新用户请求则始终创建新轮次。

- 同一 run 的调度通过 SQLite 事务、repository lease、dispatch-admission 和 task 锁认领，启动前记录意图。当前 execution subject 的单个 dispatch_inflight 需要适配为逐 run 的启动状态；不能在内存循环中绕过启动防重放保护。重启后核对进程和结果，不能重复拉起仍在运行的二进制。
- 已选定的 N 个 reviewer 必须全部成功。一个成功、一个失败时保留成功结果，只补跑失败方；不得降级发布单模型报告，也不得让主 agent 补写缺失报告。
- 复用要求 round、head/base/merge-base、context、prompt、worker 配置及结果 hash 均有效。显式 review-again 创建新一轮，即使 HEAD 不变也重跑全部模型；故障 retry 沿用同一轮。
- 聚合失败只重跑聚合；发布失败只重试/对账发布。任何阶段重试都不覆盖已经封存的成功产物。
- 每轮父任务占一个原有任务并发槽，模型进程使用独立的 `review_max_concurrency` 全局额度。prepare 和 synthesis 各计算一个模型进程，父控制器不占模型名额。额度为 1 时 reviewer 等待前一个模型退出；额度足够时同轮 reviewer 并行，不受任务并发为 1 的限制。启动前原子认领额度，未知启动/未确认退出继续保留额度；等待时间不计入模型 timeout，仍保留父任务总时限。
- 使用各 worker 的 timeout_seconds，保留任务总时限、有界重试和 execution subject 的恢复限制；耗尽后进入可见失败/attention，提供重试或取消，不能无期限运行。
- 取消父任务会停止所有活跃 run、冻结发布、保留诊断材料。只有确认进程退出后才释放并清理工作区。
- fallback 按失败的 reviewer 单独计算，不累计其他模型的失败；记录实际替代模型。两个参与者最终落到相同模型配置时，不得将其宣传成两个不同模型。
- status/board 展示 PR 下各轮次、触发链接、head SHA、当前阶段、N 个 reviewer 状态、耗时、失败原因和报告；每轮仍只有一张父任务卡片，不能让第二轮覆盖第一轮。
- review controller 接入当前单一 SQLite 执行链路，不增加 DBOS、PostgreSQL 或多引擎兼容分支。沿用 execution_subjects 的 active/attention/terminal、进程身份、取消与发布锁；外部 Git/API/模型操作不得长时间持有 SQLite 写事务。

## 分步实施与验收

| 顺序 | 修改单元与接口 | 可观察验收 |
|---|---|---|
| 1 | validate_config.py、route_config.py、配置 schema/example；新增仅 PR 作者的白名单选择器，输出固化 selection | 开关、PR 作者命中/未命中、大小写、空白名单、作者查询异常；commit/committer/触发者命中均不影响选择；旧配置兼容 |
| 2 | discover.py、route.py、workstream.py、run_once.py、loop_engine.py；新增 review_store.py，定义事件到轮次的唯一归属和排队接口 | PR 指派/评论首次触发、修改后的评论二次触发、同一评论重复投递、普通作者反馈、活跃轮次期间请求排队、不丢事件 |
| 3 | worktree.py、GitHub context 获取；新增 review_context.py 与内置 prompt 资源，输出按轮次隔离的 manifest/context bundle 和单一 reviewer-prompt.md | 每轮独立 detached worktree；reviewer 自行读文件；release 目标分支、force-push、fetch 竞态、AGENTS/源码索引、Python UNRESOLVED；本轮 prompt/context hash 相同 |
| 4 | schema.sql、storage.py；新增 review_controller.py，接入 run_once.py、execution_store.py、execution_operations.py、dispatch.py、worker_adapters 与进程监督 | 独立输出/日志目录；可信逐 run env 注入、stdin 字节一致、不污染全局 env；同轮单方重试、容量 1、取消、恢复配额、逐 run 启动意图与文件登记恢复 |
| 5 | 新增 review_report.py，定义 result.json、inputs.json、evaluation.json 文件协议和 renderer；调整 render_prompt.py 和 audit_result.py | 原子交接、损坏/错轮次/缺失文件、主 agent 按清单读文件、来源完整、分歧/误报/零 findings、固定模板与事件消费范围 |
| 6 | publish.py、publication_reconcile.py、结果/动作契约与相关 route 策略 | PR Review COMMENT；同轮幂等、跨轮分别发布、相同正文不同轮次、响应丢失对账、PR 更新/关闭、迟到旧结果、不降级发布 |
| 7 | status.py、board 展示、文档和受控端到端测试 | 完整展示两轮代码/报告和触发来源；每轮只发布一个报告；同轮失败重试只重跑失败者；旧单模型用例通过 |

每步先写有意义的行为测试，使用 fake binary 和 fake GitHub 建立可重复验证。重点不是只验证启动两个命令，而是覆盖完整的准备、分析、部分失败、恢复、汇总、发布与重入。

文件交接验收：fake reviewer 必须实际从 stdin 接收 prompt、读取 context/worktree，再向 env 指定目录写 result.json；fake 主 agent 必须按 inputs.json 读取这些结果后写 evaluation.json。断言 codex/claude 及同轮补跑接收的 prompt 字节/hash 相同，而输出目录各不相同。覆盖临时文件未提交、JSON 截断、只有 stdout 没有结果文件、错 snapshot、越界结果路径、结果落盘后登记前崩溃、登记后文件丢失、汇总期间输入 hash 变化，以及第二轮输入/输出不能覆盖第一轮。

并发验收：任务并发为 1、子进程额度大于等于 2 时，同轮 reviewer 确实重叠运行；多个轮次/仓库共享上限且不能超额。额度为 1 时等待执行，等待时间不消耗模型 timeout。覆盖额度升降、失败方重试、取消等待者不再启动、取消活跃模型后释放额度、启动结果不明时保留额度，以及协调器恢复后不重复启动仍活跃的 run。单模型和未命中白名单的既有行为继续通过。

必须增加一条跨轮端到端验收：PR 指派或评论 E1 → HEAD A → 多模型报告 R1 发布 → 修改并 push HEAD B → 新评论 E2 → 重新准备上下文并运行全部所选模型 → 报告 R2 发布。断言 R1/R2 绑定不同 round 和正确 SHA；重复投递 E1/E2 不新增执行；第二轮部分失败不取用第一轮报告；即使正文相同也存在两条各自幂等的 PR Review。额外覆盖 HEAD 未变但明确新请求、前轮运行中再请求、前轮失败/取消后新请求、PR 更新导致旧轮 superseded 以及 daemon 重启后的排队推进。

现有测试入口优先扩展 `tests/test_discovery_authorization.py`、`tests/test_routing.py`、`tests/test_workstreams.py`、`tests/test_run_once.py`、`tests/test_worktree_safety.py`、`tests/test_sqlite_runtime.py`、`tests/test_sqlite_phase_boundaries.py` 和发布恢复测试；新增多轮 review 集成测试，避免只通过伪造 fixture 跳过真实评论发现与意图识别。

实施后的仓库门禁包括 `python3 -B -m unittest discover -s tests`、CI 定义的 Ruff/Mypy 范围、compileall、资源/打包变更对应的 `python3 -m build` 和 `git diff --check`。真实 codex/claude 采用独立临时数据目录做不写 GitHub 的完整验收；GitHub 写入验收需使用明确授权的测试 PR。

新增 schema 采用加法迁移，保留现有数据；新版本关闭开关即可让后续任务回到单模型。回退旧程序前先完成或取消多模型在途任务，不能让旧程序接管未知的活跃 review session。

本次实现 review 流程和显式再次触发所需的事件/轮次衔接。工作日时间窗、cron 每次仅处理一个 PR、固定仓库路径、重复标题过滤和 skill 自己的 state JSON 不迁入 Robert；保留原有信任与通用调度规则。
