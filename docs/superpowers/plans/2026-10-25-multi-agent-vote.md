# Robert 多 Agent 投票功能方案

日期：2026-10-25。实施启动：2026-10-26。状态：已获准实现并提交 PR。

## 1. 目标、基线与本次边界

用户在 GitHub Issue 或 PR 中明确 `@Agent` 请求方案对比或投票后，Robert 将请求分类为“投票”，由主 Agent 准备问题和上下文，多个模型独立选择，最后由主 Agent 解释分歧并汇总回帖。报告必须逐个展示投票 Agent、模型、选择和理由。

本方案根据以下现状制定：

- 当前工作区：`/Volumes/ExtSSD/workspace/Robert`；用户路径 `/Users/wklken/workspace/tx/wklken/Robert` 指向同一目录。分析快照为分支 `codex/native-multi-model-review` 的 `d479724b58db8dfdb03fe93bfc71ea7d9c79f98f`，开始时工作区干净。
- 已在线核对 [PR #45](https://github.com/wklken/Robert/pull/45)：已合并，PR head 与上述本地快照一致，GitHub merge commit 为 `7051b462a4cc8228ceedf933eb95d794ab0e84e9`。本地没有为本次规划切分支或更新 main；实施前需刷新实际基线。
- 已在线核对 [Issue #40](https://github.com/wklken/Robert/issues/40)：现有需求是“主 Agent 准备上下文和选项 → 多模型独立表决 → 主 Agent 汇总 → GitHub 回帖”。
- 规划轮仅制定方案并保存文档；后续用户已授权实现、提交、push 和创建 PR，详见末尾实施检查点。未授权真实付费模型调用或外部投票测试回帖。

以下第一版行为是本方案的推荐，尚未成为已有产品能力。暂不实现排序投票、加权评分、多轮辩论、自动复投、投票后自动改代码或合并、投票专用 Web 控件。

## 2. 用户交互和分类

新增独立分类及路由：

| 层级 | 建议标识 | 作用 |
| --- | --- | --- |
| 请求意图 | `intent: vote` | 显示为“投票” |
| 执行路由 | `route_id: vote` | 进入内建投票协调器 |
| 正常最终结果 | `output_type: multi_agent_vote_report` | 审计投票报告及其唯一回帖 |
| 分类任务推荐路由 | `recommended_route: vote` | 低置信度分类完成后，创建同样受配置和授权约束的投票任务 |
| 允许的 GitHub 动作 | `comment` | 在触发请求所在 Issue / PR 的 Conversation 回帖 |

典型触发：

```text
@robert-bot 投票：方案 A 使用轮询，方案 B 使用 webhook。
请按可靠性、维护成本和迁移成本比较，并选择一个。

@robert-bot 对比上面讨论的 A、B 两种方案，组织多个模型给出推荐。

@robert-bot compare proposal A and B and vote for one.
```

机器人账号以配置为准。明确要求对比多个候选并给出取舍的请求，也归为“投票”；不强制用户记住英文命令或写结构化选项。

识别规则：

1. 先确认当前事件确实提及配置的机器人，沿用事件授权；第一版仅 `trusted_actors` 的明确请求可启动多模型投票。已有 workstream 的普通参与者权限不因此升级。
2. 分类优先读取本次请求正文和明确指向的讨论，不能因为 Issue 标题、引用代码或历史评论里出现“投票”“失败”“方案”，就覆盖当前意图。显式投票/选型请求应先于通用 bug 和 analysis 关键词识别，也先于已有 PR 的默认更新路由。
3. 普通“分析这个问题”“解释这段代码”仍沿用原路由；`review` 请求审查一段投票功能代码，仍属于 review。否定表达如“先不要投票”不能触发。
4. 难以确定是否要求多模型决策时，走原轻量分类任务；允许它返回 `recommended_route: vote`，但创建子任务时重新应用显式请求、可信触发者、功能开关、worker 和 action scope 校验。分类结果不能自行扩大权限。
5. “如何在本项目实现投票功能”属于需求分析或实现请求，不等于立即组织投票；“投票并自动实现胜出方案”等复合指令超出第一版执行能力，应明确说明边界并澄清执行部分。
6. 第一版不从 assignment、review request、CI 失败或自动分析中主动发起投票。Web 本地任务保持原有禁止隐式 GitHub 写入的约束。

当前 `discover.infer_intent` 先检查 bug 关键词，再检查 analysis；`route.route_task` 对已有 PR 有默认更新分支。两处都需要接入新意图，不能只修改 classifier 文档。

## 3. 推荐流程

```text
可信 @ 请求
  → 分类 vote / 低置信度分类后推荐 vote
  → 排队并登记独立 round
  → 主 Agent 准备问题、选项、标准和上下文
      → 关键歧义未解：waiting_for_user，向原线程提出具体问题
      → 信息足够：校验并冻结 ballot-spec、context、prompt
  → 多个 voter 在额度内独立执行
  → Robert 校验并封存每份 ballot
  → 程序统计原始票数
  → 主 Agent 读取全部封存票据，解释分歧和建议
  → Robert 固定渲染报告
  → 审计、快照检查、GitHub 普通评论发布及远端对账
```

准备、每次 voter 调用和汇总都使用新模型会话。第一位配置 worker 担任主 Agent，也可以在独立 voter 会话中投一票；准备和汇总身份本身不产生额外选票，也没有平票裁决权。

### 主 Agent 的准备产物

准备阶段写出 `prepared-request.json`，状态为 `ready` 或 `needs_clarification`。后者包含具体缺失问题，进入现有 `waiting_for_user` / `operator_question` 协议；不能拿空选项继续调用多个模型。

`ready` 必须包含：

- 一个明确的决策问题和决策范围。
- 至少两个可区分、可比较的候选项，赋予稳定 `option_id`，例如 A / B / C。
- 每个选项的中性描述、来源链接，以及“用户给出”或“主 Agent 从讨论整理”的来源说明。
- 用户指定的比较标准和硬约束。未指定时可从现有问题归纳，并在报告列为主 Agent 采用的标准；不自行添加权重。
- 背景事实、关键代码/文档索引、已知限制、未知项和适用假设。
- 本轮来源清单：触发评论、实际采用的 Issue/PR 正文与讨论、引用材料的版本/hash，以及涉及代码时的固定 Git revision。

如果用户给定完整选项，必须保留其范围和含义。允许从被明确引用的讨论中归纳候选；不能私自增删实质选项。缺少决策目标、A/B 无法定位或关键约束互相矛盾时先澄清。普通资料不足可明确记为未知，不要求用户重复确认已清楚的请求。

上下文保留用户已表达的偏好并标注来源；主 Agent 不在共享 voter prompt 中预先写自己的胜出推荐。历史上明确被引用的比较可以作为有来源的材料，新一轮的 voter 不能读取其他 voter 的输出。

### 冻结后的输入

Robert 验证准备产物并生成：

- `ballot-spec.json`：问题、固定选项、比较标准、约束、投票方式、参与者名单。
- `manifest.json`：round、上下文快照身份、来源、文件 hash、代码 revision 和配置快照。
- `voter-prompt.md`：同轮所有 voter 读取同一个文件；启动前后验证 hash。

冻结后不可改问题、选项、标准、参与者或 prompt。同轮失败重试仍使用这些文件。不同 CLI 的内置系统提示不属于共享 prompt 的字节一致保证。

PR 请求绑定 head/base/目标分支；Issue 纯讨论不强制建立代码 worktree，使用独立只读 context 目录及独立运行目录即可。Issue 需要源码时获取指定 revision，或记录准备时默认分支的固定 SHA，并建立独立只读 worktree。禁止读到后来被其他轮次重置的工作区。

## 4. 每个模型的投票契约

第一版采用**单选、等权、一参与者一票、允许弃权**。不增加模型角色分工，所有 voter 比较相同问题。

每个 voter 读取相同选项和上下文，只写自己的输出目录；通过进程专属 `ROBERT_VOTE_OUTPUT_DIR` 获取路径。先写临时文件，关闭后原子重命名为 `ballot.json`。stdout/stderr 仅作诊断，不能当作选票。

建议票据示例：

```json
{
  "schema_version": 1,
  "round_id": "vote-example",
  "context_snapshot_id": "snapshot-example",
  "ballot_spec_sha256": "<Robert 提供的实际 SHA256>",
  "choice_id": "B",
  "reason_summary": "webhook 更符合当前及时性要求，现有队列可承担重试。",
  "tradeoffs": ["需要处理重复投递，并保留一次低频对账。"],
  "evidence_refs": ["source-2", "source-5"],
  "uncertainties": ["尚未测得事件高峰期的实际延迟。"]
}
```

弃权使用 `choice_id: null`，并提供 `abstain_reason` 和非空 `reason_summary`，说明信息不足、候选均不可接受或其他具体原因。技术失败不是弃权；重试或 fallback 不增加一票。要求的是可公开的决策依据，不要求输出模型内部推理过程。

Robert 校验：JSON/schema、大小限制、当前 round/快照/spec hash、选项存在、理由非空、引用来源存在、交接文件为规定目录内普通文件，及共享输入完整性。校验不通过进入该 voter 的失败重试。

身份由调度器持久化的 `worker_id`、adapter、配置 model、本次实际选择的 model、run ID 绑定；不接受模型在输出里自报另一个身份。CLI 的 `model: default` 只能显示“CLI 默认，实际模型未验证”，不能推测型号。

确认受监督进程退出后，将选票复制到 voter 不可写的 sealed 目录，校验并登记 hash。恢复和汇总只读取封存副本。主 Agent 对一张形式有效但论证不足的选票可以指出问题，不能因为不同意它而删除、改写或转投其选择。

## 5. 统计、汇总与报告

### 票数由代码计算

`vote_tally.py` 根据每个参与者恰好一份选定的有效封存 ballot，生成 `tally.json`。voter 名单和所选 run/ballot hash 必须完整覆盖，重试前的旧 run 不重复计票。

设冻结名单人数为 N，各选项票数为 `count[option_id]`。明确区分：

- `majority`：一个选项获得 `> N/2` 票，即全体参与者的严格多数；弃权不缩小此门槛。
- `plurality_only`：唯一最高票，但未达到严格多数。例如四人投 A=2、B=1、C=1，只能说 A 领先。
- `tie`：最高票并列。报告保留平票，无自动加权或主 Agent 加票。
- `all_abstain`：所有参与者弃权，不产生推荐胜出项。

第一版正常汇总要求所有配置 voter 都返回有效 ballot，包括明确弃权。超时、进程失败和无效输出单独显示为失败，不能算弃权或悄悄从分母删除；存在未恢复失败时不发布完整投票结论。沿用现有有限重试和人工 Retry/Cancel，不无限等候。

### 主 Agent 的汇总权限

主 Agent 读取固定的 `synthesis/inputs.json`，其中列出所有选中 ballot 的路径/hash、机器生成的 tally 及快照身份。写出 `synthesis.json`，包含比较结论、共同观点、关键分歧、待核实事项和可选建议。

其建议可以不同于最高票选项，但必须明确分为“票数结果”和“主 Agent 建议”，解释差异并引用选票/资料。不得改票、捏造没有返回的模型意见、把平票写成共识，或把相同底层模型的多个 Agent 宣称为独立模型证据。

Robert 固定渲染报告。每个模型的选择和 `reason_summary` 直接来自封存 ballot；主 Agent 的总结另列，避免摘要覆盖原始理由。程序验证参与者覆盖和所有数字，票据中的取舍、引用和未知项放入逐模型明细。报告超出发布限制时应明确失败，不能静默省略某些模型。

建议回帖形态如下，模型名称和结论均为示例：

```text
多 Agent 投票：采用轮询还是 webhook？

问题 / 约束 / A、B 选项说明 / 本轮来源与代码 revision

结果：B 获得 2/3 票，达到严格多数；A 1 票，弃权 0 票。

Agent       模型                     选择   理由
codex       配置的 Codex 模型        B      延迟更低，已有队列可承担重试。
claude      配置的 Claude 模型       A      当前事件量低，轮询更易运维。
third       配置的第三个模型         B      更符合后续多仓库扩展，但需幂等。

逐模型明细：主要取舍、证据来源、未知项；失败/fallback/默认型号说明。
主 Agent 汇总：分歧主要是当前维护成本与未来扩展收益的权重。
主 Agent 建议：优先 B，但先验证 webhook 可靠性与对账方案。
待决定事项：是否接受额外的事件去重复杂度。

round / 快照标识 / 触发评论 / 参与者完成情况
```

投票提供决策建议，不构成实施、push、合并、关闭 Issue 或其他外部操作的授权。

## 6. 配置及与 PR #45 的复用方式

建议新增独立路由配置，继续引用现有 `workers`。示例中的 worker 必须预先配置：

```yaml
model_max_concurrency: 4

routes:
  vote:
    worker: codex_voter
    multi_agent_vote:
      enabled: true
      workers: [codex_voter, claude_voter, third_voter]
```

- 默认关闭；至少两个不同的 worker ID，所有 ID 必须存在；第一个与 `routes.vote.worker` 一致，承担准备和汇总。通常配置三个，两个也允许并可正常显示平票。
- repository 可以覆盖 `routes.vote.multi_agent_vote` 整个设置对象，规则与现有 review 路由一致。
- 关闭、未配置或名单不足时明确返回未启用/配置错误，不悄悄把用户的“多模型投票”改成单模型意见。
- 不继承 `multi_agent_review.author_allowlist`：它是 PR review 的作者灰度规则；投票同时支持 Issue 和 PR，依据可信请求者的明确请求。review 原作者规则保持不变。
- 相同模型的多个 worker/fallback 仍按参与者记录，但报告展示重合。`default` 不视作已确认模型多样性。

架构推荐：**独立投票业务协调器 + 从现有 review 提取有明确双消费者的执行基础能力**。不把选票伪装为 review finding，也不复制整份 review 协调器。

可共用：适配器、阶段进程启动/身份登记、任务锁、全局模型额度、timeout/fallback、沙箱、受控输出封存、恢复和取消。review 的 finding schema、结论复核和 PR Review COMMENT 发布仍由原模块负责；投票单独实现问题准备、ballot schema、统计和普通 comment 报告。

现有实现的实际耦合包括：`review_runs.stage` 仅允许 prepare/review/synthesis，`review_sessions` 必须是 PR，沙箱要求 `worktree_path` 且固定 `ROBERT_REVIEW_OUTPUT_DIR`，进程池只统计 `review_runs`。因此不能声称直接改一个 prompt 就能复用。

建议局部提取 `model_execution.py` 和 `model_pool.py`：执行器处理阶段生命周期，业务协调器提供阶段文件、来源校验和结果校验；pool 在同一 SQLite 写事务内统计 review/vote 两类占用并认领对应 run。保持 `review_sessions` / `review_runs` 原记录和含义，新增 `vote_sessions` / `vote_runs`，不重建 review 历史表。

将全局配置名推广为 `model_max_concurrency`，默认 4，同时保留 `review_max_concurrency` 为兼容别名：只配置旧字段时使用其值；同时配置且不同则明确报错，相同则接受。仍禁止 repository/route 覆盖。两种多模型工作流共用一个限额，避免各开一个池使真实模型数翻倍；旧单模型路径的并发行为保持原样。

存储层可保留现有 `review_process_pool` 表名，由公共 pool 操作，避免为名称移动存量数据。只读 status 在旧库未迁移时保持“未知”，不能写库或伪造零占用。机械提取必须先通过现有 review 测试，再接入 vote。

沙箱改为接受明确的只读输入目录集合和输出目录环境变量；已有 review 调用继续给出原参数，Issue 纯讨论不再伪造 worktree。缺少沙箱工具不能自动降为不受限执行；沿用现有 macOS/Linux 支持及认证目录约束，不新增 runtime 依赖。

## 7. 轮次、恢复、过期与发布

### 轮次与排队

- 轮次身份绑定 `repo + source_type + number + trigger_fingerprint`，不能只绑定 workstream。关联的 Issue 和 PR 可以共享工作流，但回帖目标必须是实际触发源。
- 同一事件重复发现不重建 round；新可信投票评论创建新 round，即使代码 SHA、问题和选项相同。
- 当前 round 运行时的新投票事件分别排队，不能被当前结果的 consumed events 一并消费，也不能作为当前 voter 的新输入。现有 pending-event 聚合路径需按请求边界处理，而非合并所有待处理评论。
- 澄清回复仅在授权且能唯一关联尚未冻结的等待任务时恢复准备阶段；否则要求明确所指请求。冻结前回复可补齐当前请求，冻结后修改问题则需要新 round。
- 第一版编辑原评论不隐式重投；若本轮已冻结，引用评论被编辑/删除会使其失效，需要用户发新请求。不同轮次不复用 ballot。

### 状态与恢复

round 主链为 `queued → preparing → voting → synthesizing → ready → published`，并映射已有父任务生命周期。澄清时进入现有 `waiting_for_user`；执行失败、身份不明、取消和过期分别保留 failed/attention/canceled/superseded 原因。

- 同轮只补跑失败阶段，复用 hash 仍有效的成功选票；fallback 的实际模型启动前落库，最多仍计一票。
- 汇总失败只重跑汇总；发布失败只重试发布与对账，不重新投票。
- 汇总输入清单变化后，旧 synthesis 先终止/收尾，其输出不能用于新输入清单。
- 文件封存后、SQLite 登记前中断可重验接纳；进程仍运行时不能重复启动。
- 未知启动保留模型额度，进入需处理状态；取消不能声称停止身份未知的进程。沿用现有人工核对残留进程和确认放弃 run 的边界，扩展到 vote run。
- 每轮占一个现有任务并发槽；prepare、各 voter 和 synthesis 各占一个共享模型额度。等待额度不消耗模型执行 timeout，父任务时限仍生效。
- Status/Board 显示“投票”、round、准备/投票/汇总/发布进度，各 worker/model、失败原因、有效票及 Retry/Cancel；不泄露文件路径、认证信息或原始日志。

### 过期与发布

发布前检查授权、当前 task/attempt、冻结输入完整性，以及实际采用的来源是否仍有效：

- PR 的关闭/合并、head/base/目标分支变化：标记过期，不发布旧结论。
- 实际采用的请求或上下文正文被编辑/删除：标记过期。只校验本轮使用的材料版本，不能因为 Robert 自己发评论导致目标 `updated_at` 改变就误判过期。
- 未采用的新评论保留为后续事件；不默默混入当前 snapshot。显式固定 revision 的材料按固定版本解释；以当前分支为比较对象的代码同时核对其分支指针。

成功报告复用 `comment` action，但新增 vote 专属审计元数据和 `vote_publish.py` 校验：精确触发源、round、snapshot、封存报告路径/hash。`multi_agent_vote_report` 正常结果恰好一个 comment；合法澄清仍走已有 waiting 输出。发布系统读取注册报告，不能接受模型自由拼接的替代正文或外部 URL。

同时保留现有 `robert-comment` marker，增加稳定 `robert-vote round=...` marker。投票发布幂等身份为 repository/source type/number/round，不随 attempt/action ID 改变。模糊响应先分页查询目标评论，核对发布账号、marker 和完整正文/hash；已存在即接纳，无法确认则停止重复 POST。PR 投票也发 Conversation 普通评论，不提交 APPROVE/REQUEST_CHANGES，也不复用 review 的 findings 标题校验。

## 8. 实施单元、接口与验证

按顺序实施，不要求并行修改。以下新增模块名是建议名，实施时可以按周边结构微调，不能遗漏接口约束。

| 单元 | 文件归属与主要工作 | 生产/消费接口与验收 |
| --- | --- | --- |
| 1. 共享阶段执行与额度 | 新 `model_execution.py`、`model_pool.py`；调整 `review_coordinator.py`、`review_store.py`、`review_sandbox.py`、配置校验和 schema | 先保持 review 原行为，保留历史记录和旧配置；支持可选源码目录、业务输出变量和双类 run 的原子额度；通过现有 review lifecycle/parallel/sandbox/recovery 回归 |
| 2. 分类与路由 | `discover.py`、`route.py`、`route_config.py`、`render_prompt.py`、`audit_result.py`、`resources/routes.yml`、`classifier.md`、相关 route/result/config schema | 正文识别出 vote；强规则和分类子任务都能进入投票；开关、可信请求者和 action scope 两条入口一致；保持其他路由行为 |
| 3. 投票存储和文件协议 | 新 `vote_store.py`、`vote_workspace.py`、`vote_files.py`、投票 schema/提示词资源；接入现有 SQLite 初始化/迁移 | 新 vote sessions/runs；冻结 spec/context/prompt；正常、弃权、非法选项、跨轮、缺少理由、symlink/FIFO、输入篡改、封存和恢复均有行为覆盖 |
| 4. 编排与事件边界 | 新 `vote_coordinator.py`；接入 `run_once.py`、任务恢复/控制及所需 execution 模块 | prepare → voters → synthesis；每个请求单独排队；澄清能恢复；新请求不被旧任务消费；同轮重试只执行缺失部分，取消/恢复无重复模型 |
| 5. 统计、汇总、审计和发布 | 新 `vote_tally.py`、`vote_publish.py`；接入 `worker/result.py`、`audit_result.py`、`publish.py`、`publication_reconcile.py` | 程序负责计票，报告逐模型保留原票和理由；精确来源回帖；响应丢失远端对账只产生一条评论；过期不会发布 |
| 6. 可见性和集成验收 | `status.py`、`board_execution.py`、实际消费状态的 Web 组件；`docs/multi-agent-vote.md`、配置/结果/worker 协议说明和 fixtures | 用户可区分准备、各模型、汇总、失败和等待；文档与打包资源完整；review、普通分析和单模型路径无回归 |

实施前使用支持的项目 Python 环境，不因存在环境文件就修改运行时。重点检查/扩展现有 `test_routing.py`、`test_child_route_policy.py`、`test_discovery_authorization.py`、`test_workstreams.py`、`test_config_and_schema.py`、`test_review_parallel.py`、`test_review_recovery.py`、`test_review_sandbox.py` 和发布恢复测试；新投票测试按行为分组，不按函数机械建测试。

必须覆盖的集成场景：

1. Issue 明确 A/B 投票 → 三个假模型返回选择与理由 → 主 Agent 汇总 → 一条普通 comment。PR 同样完成，目标保持为触发 PR。
2. 中文方案对比、英文 vote、包含“修复失败”背景的显式投票、低置信度分类后推荐 vote；普通 analysis/review、引用投票、否定请求不误触发。
3. 已知/未知 workstream、已有 Robert PR、关联 Issue/PR、非 trusted 请求者、功能关闭、worker 缺失和默认型号都遵守相同边界。
4. 主 Agent 整理选项、关键歧义等待澄清、回复恢复；冻结后不接受增删选项或改标准。
5. 严格多数、仅相对领先、平票、全弃权；理由缺失、技术失败、重复 run 均不制造额外票或多数。
6. 同轮 voter prompt 字节一致、独立输出、看不到其他选票；fallback 和恢复仍绑定固定名单、snapshot 和 spec hash。
7. 模型部分失败后只补跑该模型；汇总/发布失败分别只重试自身；新评论排成下一轮，重复轮询不重复执行。
8. review 与 vote 同时竞争共享额度，任务上限为 1 时一轮内部 voter 仍可并行；等待不消耗模型 timeout，进程组未退出不释放额度。
9. SIGTERM/崩溃、未知启动、取消、人工放弃、迟到结果，以及 seal/数据库登记之间中断。
10. PR 更新、请求编辑/删除、代码来源变化导致过期；无关新评论和机器人回帖不会误判。发布成功但响应丢失时先对账，最终只有一条报告。
11. 主 Agent 的建议与票数不同仍如实呈现；模型理由无效证据可被指出，但形式有效的选择不被悄悄删改。
12. 纯讨论 Issue 不要求 worktree；使用代码的 Issue 和 PR 使用各自固定快照；旧 review/single-model 路径、旧配置和旧数据库可继续读取与恢复。

允许先用真实临时 Git 仓库、适配器启动的假模型进程和假 GitHub API 完成端到端验收；这不构成真实模型调用或真实 GitHub 发布已验证。

实现后的门禁来自仓库 AGENTS.md 和当前 CI：

```bash
python3 -B -m unittest discover -s tests
python3 -m ruff check src tests
python3 -m mypy src/robert_agent/cli src/robert_agent/paths.py src/robert_agent/route_config.py src/robert_agent/skills.py src/robert_agent/service.py src/robert_agent/migrate.py
python3 -m compileall -q src
python3 -m build
git diff --check
```

新配置、schema、提示词等打包资源需确认出现在 wheel/sdist，按现有 distribution 测试验证。若有移动/提取，扫描旧新符号、动态入口、转发残留及所需兼容包装。出现失败先确认基线和因果，不把实现后的失败自动归因于本次改动。

本次只有方案文档，验证范围是来源/接口核对、文档差异与格式检查，不运行应用测试或模型调用。

## 9. 兼容、上线和回退

投票默认关闭，先配置测试仓库及可信请求者。存储采用加法扩展，不删除 review 历史，不引入 DBOS 或额外 agent 框架。实施前先刷新 main 并检查工作区，不沿用本方案旧快照作最终交付证据。

关闭开关阻止新投票；已启动轮次继续按其冻结配置完成，或由操作者显式取消。关闭开关不能当作已终止子进程的证据。回退到旧二进制前必须停止并确认新 vote 协调器/子进程退出，否则旧代码不识别 vote 占用，可能超额启动 review；保留新表和产物以便恢复，不在回退时删库。

后续用户已授权实现并提 PR；真实模型/测试线程验证、合并和版本发布不在本轮授权范围。


## 实施检查点（2026-10-26）

- 用户已明确授权按本方案实现并提 PR；不包含合并、发布版本或运行真实付费模型。
- 当前分支 `codex/multi-agent-vote`，基线 `origin/main` 的 `7051b462a4cc8228ceedf933eb95d794ab0e84e9`。原工作区只有本任务方案文档，已保留。
- 已接入 vote 分类/配置、共享阶段执行与额度、独立 vote 存储、Issue/PR 快照、准备/澄清/投票/汇总、固定报告、审计发布、取消恢复及状态展示。操作说明见 `docs/multi-agent-vote.md`。
- 干净基线独立导出后运行完整测试：1019 项，145.417 秒，OK，平台跳过 1 项。集中投票测试、真实假模型子进程并发/取消及 review 提取回归已验证；最终完整门禁与交付证据由 PR 描述记录。
- 现有 unknown-run API 名 `abandon_review_run_ids` 为兼容保留，同时覆盖 vote run；底层共享表名 `review_process_pool` 保留。未实现排序、权重、自动复投或自动实施。
- 独立最终复核已完成，四项引入问题均已修复并通过回归；最终完整门禁和交付证据由 PR 描述记录。验收使用假模型和假 GitHub API，不代表真实认证和外部投票回帖已实测。


### 独立复核整改记录

复核基线 `7051b46`，初次审查树 `683a80a2a686f39b35c03ed2fc798619333ed663`。以下均为 Correct / introduced / in scope，已先建立失败回归再修正：

| ID | 状态 | 修正及证据 |
| --- | --- | --- |
| VOTE-REV-1 | fixed | 澄清入口检查已消费指纹、当前轮回复列表和当前等待问题时间；重复/历史回复不能新建准备任务。集成测试覆盖再次澄清后重复发现旧回复。 |
| VOTE-REV-2 | fixed | 已审计分类任务推荐 vote 时沿用其决定，仍检查 @、可信用户和配置。集成测试覆盖启发式未命中的语义请求创建 vote 子任务。 |
| VOTE-REV-3 | fixed | 捕获 submitted reviews 并核对 review 触发正文，发布时查询对应 review endpoint。覆盖来源被编辑阻止继续及发布端正确查询。 |
| VOTE-REV-4 | fixed | 共享执行器枚举 review/vote 两类已登记 run，通过其所属 store 按版本释放已确认退出的额度，未知启动继续保留。覆盖跨工作流回收和未知占用保留。 |

最终集中检查含 36 项投票测试，覆盖假二进制适配器、文件、恢复、分类与发布；Ruff 和 CI 范围 Mypy 通过。机械提取保留 review 的公开执行入口和文件协议；移除了本轮引入的未使用 Board 转发及重复额度配置 helper，原有控制协议已完整保留。
