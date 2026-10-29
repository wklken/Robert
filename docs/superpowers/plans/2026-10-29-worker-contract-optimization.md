# Worker 协议优化方案（review 后修订）

状态：方案已按“直接升级新协议，不兼容旧协议”修订，尚未实施。2026-10-29。

本次仅更新方案；没有修改产品代码、提交基线、调用真实模型或部署。此前逐项判定与复现结果见 [review 验证记录](../../reviews/2026-10-29-worker-contract-review.md)；其中旧协议兼容建议已被用户本次指令替代。

## 1. 目标、基线与职责

Robert 维护任务身份、权限和生命周期，负责确定性字段装配、持久化、审核与发布；worker 理解请求、完成代码工作、提供业务判断与证据。首先减少可纠正的提交错误造成整次任务失败，其次减少协议字段搬运和重复读取。

- 仓库：`/Users/wklken/workspace/tx/wklken/Robert`，实际 `/Volumes/ExtSSD/workspace/Robert`。
- 核查 HEAD：`b86f32754d450f67539f902de4c1eb914452b9ca`，加当前未提交的三项 prompt 优化。
- tracked diff：10 个文件、296 行新增/316 行删除；另有 2 个新测试文件及 prompt 对照资料。验证记录保留关键文件 hash。
- 普通评论分类仍缺真实任务效果证据；上一轮 1,336 项测试不能代替后续验证。
- 不迁移工作流框架，不改模型配置，不重写调度器，不以新表、完整 manifest/revision 系统为前提。
- 运行时只接受新协议：不保留旧解析器、字段别名、缺版本回退、旧 spool 解码和专用兼容入口。prompt、helper、controller、publisher 及所有调用方同步升级。
- 原生多模型 review/vote 的领域结果与执行流程不扩展；若调用公共提交入口，必须同步适配新提交合同，不能成为旧协议旁路。

| 职责 | 负责人 | 合同 |
| --- | --- | --- |
| 事实与状态 | controller、DB、Git/命令实际记录 | 模型结果是待验证声明；DB 记录身份、授权与生命周期。 |
| 数据传递 | helper、现有 spool、controller | 校验、持久化、去重、恢复由程序完成。 |
| 模型上下文 | 输入装配器 | 当前请求与必要约束优先可见，完整历史可定位。 |
| 业务判断 | worker | 需求含义、代码工作、验证选择、review 判定、完成范围与阻塞原因。 |

DB、JSON、Markdown 分别承载运行状态、机器交换/存档、阅读视图；派生表示由程序生成，不要求模型维护同步。

## 2. 对原方案的取舍

1. 先实现共享的提交时预审核，再接入紧凑输入标准化；二者属于同一新协议，不为中间开发步骤维护两套生产协议。
2. 保留 `consumed_event_fingerprints` 和 Web 的 `consumed_work_item_event_ids`，保持当前完整预填、显式提交以及 subset 合法的行为。不引入 E1、逐项选择、自动全选或 `unhandled_events` 新协议。
3. 禁止提交时从 task 最新事件集合自动计算“已处理”；这可能吞掉 worker 运行期间的新 pending。消费默认值/排除语义另作生命周期设计。
4. 保留 `planned_github_actions/type/push_existing_pr` 等准确的现有词汇，以减少无价值的概念替换；这不构成兼容承诺。合同标识由程序登记，用于校验和去重，模型不填写，也不用于选择旧解析器。
5. 复用现有 action scope；GitHub context 按 attempt 存放，不增加独立 context revision。
6. 命令证据独立交付，先补记录与输出。本轮不依赖 command_ref/tree hash 才能缩小结果协议。

## 3. 实施顺序与交付边界

```text
0 基线和数据
  → 1 共享预审核与可修正提交
  → 2 紧凑输出标准化、全部调用方接入 → 新协议整体切换
  → 3 输入内联和按 attempt 存 context
  → 4 命令记录与输出（按需求决定是否继续）
```

阶段 1、2 可分别开发、测试和审查，新协议生产切换以二者和全部调用方完成为边界，不部署半套合同。阶段 3、4 仍可独立交付；阶段 2 不依赖阶段 3 的文件布局，阶段 4 不依赖新的结果字段。基线数据用于评价优先级与收益，不再决定是否保留旧协议。

### 阶段 0：稳定基线与失败原因数据

- 在后续明确的实施/本地提交授权范围内，将已有三项优化形成独立本地 commit，不混入新协议改动，不推送。本轮只记录 HEAD/diff/hash，没有执行 commit。
- 复用 [14 组 prompt 对照](../../reviews/2026-10-29-worker-prompts/README.md)，覆盖普通请求、review report、mixed、长讨论、等待用户、Web、CI/conflict 与 resume。
- 在可用运行环境中抽样验证已有分类策略。当前未定位到本机生产 DB/config，不宣称已取得生产统计或真实模型验证。
- 只读统计按 route/task_kind/input-policy/worker/时间窗口分组：最终 audit reason/safe_error、helper failed_validation、result_conflict、spool 拒绝、人工 retry/operator recovery、失败前已用时间，并记录样本分母和缺失率。
- helper 的部分错误只返回到 worker，DB 不一定保留。结合 DB、worker stdout/stderr、spool/rejected、通知与重试记录核对；没有日志不等于零发生。
- 复用 `execution_metrics.py` 的分段时间，只补必要诊断计数；缺失为 unknown，重叠区间不能相加。
- 没有生产数据时仍可按已复现的重试缺陷推进新协议，但不称其“收益最大”；分别记录预审核的正确性收益与字段精简的实际效果。

验收：基线与样本可复现，失败原因和数据缺口明确。真实 A/B 使用同代码/模型/effort/验证范围与可比缓存条件，在隔离工作目录中运行且不发布；无真实执行证据则保留“效果未验证”。

### 阶段 1：共享预审核与可修正提交

主要文件：`worker/result.py`、`audit_result.py`、`run_once.py`；必要时抽取共享 scope/审核上下文模块。以规范化后的完整结果为审核输入，不要求先引入 manifest。

现状：helper 接受并入库后，controller 要等 attempt completed、真实进程停止才做最终审核；waiting 缺 comment 会写 `result_validation`，同 attempt 修正后也被拒绝。

实现合同：

1. 共享 `_action_scope_for_result` 的来源和规则。分离稳定 DB 字段与当前 Git/文件观察；不在 SQLite 写事务中做 Git I/O，不新增第二套 scope 推导，也不形成 helper → run_once → helper 循环依赖。
2. 抽取共同的确定性规则：字段形状、动作权限与目标、字段完整性、顺序、公开文本脱敏、marker、issue linking、required skills、verification 声明、review policy、classification 和 waiting 交付条件。
3. remediation 身份/字段可预查；独立 attestation、最终 HEAD/merge graph、进程停止和发布前权限仍由 controller 完整重验。不能伪造 attestation 来提前调用通过完整 audit。
4. 在占用 final-result slot 前预查。可纠正问题返回字段路径、稳定 reason_code、安全错误及修正建议；不写 worker_results/actions、不占 pending final spool、不写永久 `result_validation`。
5. 当前活跃 attempt 修正后可重提。首次有效结果已持久化后，继续保持唯一最终结果、不同提交 conflict 的规则；预审核通过不等于最终 accepted。
6. 取消、owner 失效、跨 attempt、已占位的不同结果继续拒绝，不包装成可修正格式错误。既有历史失败状态不自动解锁。
7. DB/scope 读取失败或超时返回可重试的 validation_unavailable，不视为通过。未收到已记录/已持久化确认时 worker 不得声称提交完成；预查通过后，入库锁冲突仍由原 spool 保证防丢失。
8. 写入前用短事务复核当前 owner/attempt；最终审核处理预查后的状态变化。可纠正失败仅写不占结果位的诊断计数，不保存敏感原文。

验收：顺序错误、缺必要业务字段、waiting 缺 comment 等可纠正提交均能同 attempt 修正，首次拒绝不占最终槽位或永久拒绝。机械 marker/path 由阶段 2 补齐；程序补齐不全属于实现错误，不能要求模型手工补救。取消/owner 变化仍拒绝；最终 HEAD 变化仍被最后审核拦截。

测试：`tests/test_worker_protocol.py`、`tests/test_run_once.py`、`tests/test_review_input_workflow.py`，补锁库、并发提交、修正重提、提交后继续改代码、进程尚未停止等场景。prompt 同步说明可纠正错误的重提流程，交付 before/after。

### 阶段 2：输出标准化，沿用现有词汇

主要文件：`worker/result.py`、公共 scope/审核模块、`result_spool.py`、`render_prompt.py`、`run_once.py`、schema/合同文档。marker 生成与识别同步覆盖 `publish.py`、`publication_reconcile.py`。

紧凑输入是唯一 worker 提交合同，程序将其补齐为审核/发布所需的完整内部结果。controller 在 attempt metadata 登记当前合同标识，缺失或不支持的标识明确拒绝，不回退。模型不提供 protocol_version。命令可继续使用 `--db/--task-id/--attempt-id/--output-type` 定位当前任务；删除仅为旧调用方保留的别名、默认值和转发层。

提交顺序：解析并校验紧凑输入 → 校验当前绑定/查重 → 标准化机械字段 → 共享预审核 → 原子持久化规范化结果 → 进程停止后完整审核 → 发布。只有预审核通过才占最终结果位；已保存的相同语义提交直接返回原记录，不重新标准化。

#### 通用最小字段

- 必填 `planned_github_actions`、`used_skills`、`verification`、当前 origin 的 consumed 数组；空列表只在对应 route/policy 允许时合法。
- consumed 数组继续预填本次输入集合并显式提交，subset 与后续任务处理保持现状；不引入新的事件索引。
- handoff 用于完成范围与限制说明，mixed 必须非空；memory_delta、used_memory_ids 按新 schema 保留可选语义。
- verification 保留 command/status/purpose/required，执行过的检查提供 exit_code，skipped 提供 skipped_reason；required/allow_skipped 由既有 policy 决定，不生成成功证据。
- JSON 不再要求 task/attempt 身份；仍由 helper 命令参数绑定并校验 DB。

#### 每条 route 的最小紧凑 schema

下表在通用字段上定义增量。表内 actions 是 `planned_github_actions` 的简写，`[]` 表示空动作；`A → B` 表示相应动作出现时的顺序。所有支持中的 route/调用方都须完成映射和测试才能整体切换，不能以旧协议兜底或用默认值掩盖歧义。

| Route / 场景 | 模型填写 | 标准化及合同 |
| --- | --- | --- |
| classification-result | actions=[]；recommended_route（无后续则空）；new-pr 时 branch_slug；handoff 说明依据 | 校验既有枚举/slug，不制造发布动作。 |
| local-result | actions=[]；结论放 handoff | 不增加外部动作，verification 按 optional。 |
| comment-analysis | `{type:comment,body}` | 补唯一明确目标和 action marker；多目标歧义时要求现有 target_url。 |
| new-pr | `{type:push_existing_pr}` → `{type:open_pr,title,body}`；issue 来源填写 issue_resolution，partial 增 remaining_scope；其他获准 comment 可显式提交 | 补路径/分支/remote、repo/head/base、PR 元数据与 issue 关联句；不推断 complete；验证 required。 |
| update-existing-pr 普通请求 | comment；需要 push 时放在 comment 前；classify 下 review_input_kind=request | 补目标/marker，不生成 findings；push 按 required_for_push。 |
| update-existing-pr 原生 review / review_feedback | 同上，另填非空 review_point_evaluation | required 不可被 request 绕过，push 仍须可实施 finding。 |
| update-existing-pr mixed | 同上，review_input_kind=mixed、非空 evaluation 和 handoff | 保留独立请求支持 push 的既有规则，不批准错误 claim。 |
| review-comment | comment；classify 时 review_input_kind；required 或 review_feedback/mixed 时 evaluation | comment-only，不允许 push；逐点字段沿用现有合同。 |
| review-pr（通用） | 一个 comment，正文为实际审查结论 | 从可信源补 PR author/mention，缺来源则报错；完整正文继续审核。 |
| waiting-for-user（GitHub） | operator_question={kind,summary,...} 与 comment，无 push | kind 沿用现有枚举；缺可交付问题/评论可纠正重提。 |
| Web local-result / waiting | consumed_work_item_event_ids；waiting 提供 operator_question；actions=[] | 不继承 GitHub 评论权限，遵循现有 Web output/route。 |
| 其他 Web 输出 | 根据已登记 expected_output/allowed actions 逐项确定新 schema | 实施时枚举所有支持分支并补对等测试，缺一项则不满足切换条件；不将 GitHub schema 直接套用。 |
| ci_remediation / merge_conflict_remediation | 原 update 动作；remediation_evidence.resolution_summary；真实验证 | 从 episode 补 kind/episode_id/observed SHA/CI signature；最终 attestation 保留。 |
| resume | 原 route schema，handoff 说明恢复与复用 | 新 attempt 身份；不复用旧 attempt marker；验证复用仍需有效证据。 |
| 原生 multi_agent_review_report / vote | 保留专用领域报告字段，由 coordinator 按新公共提交合同提交 | 专用报告是当前协议的显式结果类型，不保留旧提交入口。模型内部审查/投票流程不扩展。 |

review_point_evaluation 每项仍含 summary/verdict/reasoning/action，枚举及 implement 条件复用现有合同。切换生成器前，补每条 route 的 schema、正例、缺字段/权限反例与标准化 golden 样本；上述表格不是省略实现验证的理由。

#### 确定性补齐规则

- 机械字段统一取共享 scope，新 worker schema 不接受 repo/path/branch/remote、隐藏 marker 等程序自有字段；即使与真实值相同也不按旧格式接收。必要的业务目标选择字段单独列入 schema 并校验，不与机械字段混淆。
- sources 是允许目标集合，不等于唯一发布意图。多目标时要求明确 target_url 并校验，不新增 target ref。
- 保持动作顺序，不悄悄重排/补做动作；顺序错误在预审核返回。
- issue_resolution、remaining_scope、正文结论、review 判定和验证结果归模型；程序生成关联句时拒绝正文中的冲突 closing/reference 声明。
- 作者必须来自可信源，缺失则报错。程序补齐后的完整正文仍接受 redaction/scope/idempotency 和最终审核。

#### 逐 action marker

- 保留 robert-comment 前缀，添加稳定 action_index。同 attempt、同目标的两条 comment 各自唯一。
- index 取首次有效提交的持久动作顺序；不随机生成，不在 replay 重排序。
- worker 正文不接受控制标记，由程序为每个 action 生成唯一 marker，避免优先匹配到 worker 提供的标记。
- 同时验证 publisher/reconcile 对新 marker 的提取、远端查重及 crash recovery；移除旧标记协议的执行分支。已发布的历史文本不改写，部署前需收敛旧的未决发布。

#### 语义去重与重放

- 新摘要取 task_id + attempt_id + 固定输入合同/绑定版本 + canonical 语义输入。canonical 规则固定，数组顺序和正文语义保留；normalizer 代码版本、生成时间不进入去重键。
- 首次有效提交保存语义摘要、标准化版本、规范化结果和必要身份，pending spool 与 DB 使用同一语义键。
- 同语义重提返回已保存结果，不用升级后的 normalizer 重建；最终位已占用后的不同语义仍 conflict。
- replay 使用保存的规范化结果，不从变化中的任务内容重新补字段；当前 owner/生命周期与最终审核仍生效。
- spool 只读写新格式。旧格式或未知合同显式拒绝并报告，不自动转换、丢弃或当作新结果重放；由切换前检查阻止其进入新运行期。

验收：双 comment 不碰撞、同一合同内 normalizer 更新后同语义重提不冲突、不同语义仍拒绝、权限不扩大、验证要求不降低。全部调用方使用唯一新提交合同，不依赖新 context 布局或 command_ref。

### 阶段 3：当前请求内联、context 按 attempt 存放

主要文件：`run_once.py`、`render_prompt.py` 和 artifact/恢复路径；与结果字段精简没有功能依赖，不增加另一种提交格式。

- GitHub 使用 `<attempt_id>.github-context.md/json`，沿用 Web 按 attempt 命名方式。resume 写新路径，旧 attempt artifact 保留实际执行输入。
- prepared refresh 只在未启动且仍拥有准备权时替换同 attempt 输入；原子替换文件，更新已有 prepared artifact 元数据，避免同路径多条伪历史。
- 不宣称保存每次尚未启动的准备版本；不新建 revision 体系。dispatch 取得输入后禁止刷新同 attempt，测试 refresh/dispatch 竞态与失败恢复。
- 当前触发请求和必要验收要求直接可见，长 issue/报告提供明确读取入口；不凭元数据猜验收要求。完整历史默认指向 Markdown，JSON 保留机器存档。
- 确定性装配，不增加 LLM 摘要调用；省略内容必须可定位，保留 operator reply、未解决问题和冲突要求。
- consumed 预填集合仍与本次 prompt 输入一致，布局变化不得改变事件消费/子任务合同。

验收：短请求减少起步读取；长上下文不丢失、不默认双读；resume 不覆盖旧输入；历史不可信/新 pending 仍按原授权逻辑处理。比较实际工具轮次，不能用字节缩减替代延迟证据。

### 阶段 4：命令记录与输出，独立评估

主要文件：`worker/heartbeat.py`、相关 CLI/文档；复用 supervision/journal/recovery。

- 先补实际 argv、cwd，与现有开始/结束、退出码、日志关联；处理参数敏感信息及日志可见范围。当前 journal 缺这些字段，不能声称已经具备。
- 统计截断导致的额外读取后，提供显式 stream/tee 模式：透传 stdout/stderr 并保存日志，机器结果写独立 journal，避免与输出 JSON 混淆。短输出完整可见，大输出保留 summary 模式，不默认把无限日志送入模型。
- 新 verification schema 明确区分 worker reported、runtime recorded、外部 CI；自报来源不能自动升级为 attested。每类来源的必需字段与证明能力明确，不设置旧格式回退。
- 本阶段不加入严格 command_ref 新鲜度门槛，也不用引用替代 verification；以后增加此门槛时对新 schema 的所有适用来源统一实施，避免通过删除引用绕过。
- 临时 index/tree hash 留作后续实验：能捕获非忽略的未跟踪文件且不改真实 index，但需考虑大仓库成本、filters、submodule、忽略文件、并发变化与环境依赖。前后 tree 等于最终 commit tree 只证明所表示的文件内容一致，不能独立证明测试覆盖、运行环境或执行中没有临时改动。
- 若以后要求强制来源/新鲜度审核，先单独定义覆盖所有本地验证条目的统一政策及 CI/人工证据路径，再决定 command_ref；不附带混入本次协议精简。

验收：透传不破坏超时、取消、进程组、锁库恢复；来源描述准确；不将自报转换为程序证明。tree hash 不作为本轮交付门槛。

## 4. 单协议切换与运行正确性

- 当前环境白名单和普通 dispatch 不自动传新绑定变量。本方案保留现有参数，不依赖尚未接通的环境变量。
- 以后简化命令可用显式 `--binding <path>` 定位，helper 对照 DB 校验 task/attempt/owner；路径不授予权限，环境变量只是可选补充。不为短参数先造完整 manifest。
- prompt、helper、controller、publisher、coordinator 和资源 schema 同批切换；新运行期不允许旧 worker 或旧 prompt 继续提交，不自动解除历史 result_validation。
- 默认切换步骤：停止接收/派发新任务；在原运行版本下完成在途 worker、pending spool 和未决发布；核对进程均停止、无待重放/待发布结果，再启动新版本并生成新 prompt。prepared 未启动任务重新准备，不能直接执行旧 prompt。
- 若旧工作无法收敛，暂停切换，按明确的取消/人工对账流程处理。不能以“不兼容”为理由清空 DB、丢弃结果或盲目重跑可能已发布的动作。
- 已完成的历史任务仅保留为审计记录，不要求新 worker/helper 继续解析或恢复其旧提交协议。恢复/重试必须创建新 attempt 并生成新协议输入；明确未完成范围并核对已有发布，避免重复动作。
- 不设计新旧格式转换或跨协议自动回滚。需要回退部署时同样先停止派发、收敛/核对副作用；不能恢复旧 DB 后忘记已发生的远端发布。具体部署操作另在实施时记录。
- 以上是切换和幂等正确性要求，不是旧协议运行时兼容。审核、redaction、owner、恢复和唯一最终结果约束保持有效。

## 5. 验证与阶段交付

每步交付实际 diff、完整 prompt before/after、协议变化、切换条件及测试结果；阶段 1、2 合并满足整体切换验收。新增代码前为已确认缺陷建立失败回归，不能仅修改字符串断言。

重点行为矩阵：

- helper 拒绝 → 当前 worker 修正 → 同 attempt 成功；取消/owner 冲突仍拒绝。
- 预审核后继续改 HEAD/权限，最终审核仍等待停止且重新检查。
- 全量/子集 consumed、历史不可信事件、新 pending 的子任务结果保持合同。
- 双 comment、重复提交、normalizer 升级、锁库重放、发布后崩溃恢复。
- GitHub/Web、required/classify/mixed、waiting、CI/conflict、resume、原生 review/vote。
- context refresh/dispatch 竞态与旧 attempt 追溯；命令失败/超时/取消/大输出。
- 旧格式/缺合同标识显式拒绝；所有当前调用方均用新合同；旧 worker、pending spool 或未决发布存在时切换不能通过；prepared 重新生成输入。

主要测试：`tests/test_worker_protocol.py`、`tests/test_run_once.py`、`tests/test_review_input_workflow.py`、`tests/test_prompt_scope.py`、`tests/test_publication_recovery.py`、`tests/test_publish_dedupe_acceptance.py`、`tests/test_worker_adapter_config.py`。按实际改动选择 focused tests，完成时按仓库要求运行全套 unittest、资源 build，以及 CI 的 Ruff、mypy 范围、compileall 和 diff 检查。

```sh
python3 -B -m unittest discover -s tests
python3 -m ruff check src tests
python3 -m compileall -q src
python3 -m build
git diff --check
```

抽取共享 helper 时检查 imports、导出、CLI/动态入口、全部消费者和循环依赖；删除只为旧协议服务的解析、别名、转发及测试。保留行为合同测试，更新为新输入，另验证不支持格式被明确拒绝。真实模型效果与离线/mock 检查分别报告；缺生产频率时不承诺加速排名或百分比。

本轮交付仅为修订方案与验证记录。产品实现、本地 commit、真实模型实验、推送与部署均未执行。
