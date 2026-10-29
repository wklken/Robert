# Worker 优化方案 review 验证记录

后续决定：用户已明确不需要旧协议兼容，直接升级新协议。本文保留当时的核查事实与处置记录；涉及旧协议/spool 兼容的实施建议，以[最新方案](../superpowers/plans/2026-10-29-worker-contract-optimization.md)为准，不再执行双协议设计。

日期：2026-10-29。源报告：本聊天用户提供的“主要问题”3 项及“设计细节上的缺口”8 项，按原顺序保留为合成 ID `MAIN-1..3`、`DETAIL-1..8`。原报告未给 severity，本文不推断严重级别。

目标：HEAD `b86f32754d450f67539f902de4c1eb914452b9ca` 加未提交工作树。非 PR 审查，所有 Introduced 均为 `Not applicable`，不将现有产品行为归因到本次方案。

原方案 SHA256：`f1bd851e049106e77b5549734ec2595296af7d05b3286fc4a681ff31acc736ca`。本次仅改方案和新增本记录。读代码及临时探针使用当前工作树；未访问生产 DB、调用真实模型或写 GitHub。

## 判定表

共 11 项：6 项 Correct，5 项 Partially correct。所有条目 Scope=`in scope`（更新方案）、Disposition=`Current change`、Resolution=`open`（产品实施尚未发生）；“方案已纳入”与“运行缺陷已修复”分开。

| ID | 原主张及 reviewer 建议 | Verdict | 核实与方案处置 |
| --- | --- | --- | --- |
| MAIN-1 | 最终审核太晚；waiting 缺 comment 后不可重提；建议 spool 前预审核，收益最大 | Partially correct | 失败链和无法修正已复现。helper 也做 owner/生命周期检查，并非仅形状检查；收益排名缺生产数据。预审核列首个产品阶段，共享确定性规则，最终 attestation 不前移。 |
| MAIN-2 | 显式选择 consumed 会触发生命周期变化；建议默认全量、选填未处理原因，取消 E1 | Partially correct | subset 当前合法；未消费只是后续候选，须重新授权。移除 E1/逐项选择改造，保留原预填与显式提交；不新增自动全量/排除协议，避免扩大范围。 |
| MAIN-3 | 未提交基线、阶段耦合、分类未真实验证；建议本地 commit 与独立交付顺序 | Partially correct | 未提交/未真实验证属实；tracked 实为 296 新增/316 删除，另有新文件，非约 300 行总变更。采用独立交付与基线要求；本次仅方案授权，不将引用中的 commit 建议当作立即提交指令。 |
| DETAIL-1 | scope 已有 repo/base/path/branch/remote/sources，应复用，不先造表/manifest | Correct | 来源已经存在，但函数还做 Git/文件 I/O，不是纯映射。计划共享提取、分离最终观察，避免循环依赖及写锁中 I/O。 |
| DETAIL-2 | 不换 publication/kind 等词汇；版本不由模型填写；补逐 route schema | Correct | 现有词汇覆盖需求。修订保留原动作名、controller 版本选择，补各 route 最小输入/标准化表；未覆盖调用方继续旧协议。 |
| DETAIL-3 | 同 attempt 同目标多 comment 使用同 marker 会误去重；加 action 序号并检查正则 | Correct | 模拟远端探针复现第二动作命中第一动作。附 action_index 可被 publisher/reconcile 正则完整识别，查重不再命中旧标记。纳入稳定动作序号和旧动作兼容。 |
| DETAIL-4 | 标准化输出指纹会受 normalizer 升级影响；建议语义输入+绑定版本去重，保存完整结果重放 | Correct | 现 hash 为完整 payload，仅派生 marker 变化即改变摘要。新协议采用含 task/attempt 的语义键、首次有效结果保留及新旧 spool 版本分派。 |
| DETAIL-5 | journal 无 argv/cwd/Git；建议临时 index tree 强校验、原样输出、避免旧条目绕过 | Partially correct | 缺字段/尾部输出属实。tree 只能表示部分输入，原样无限输出也可能增加成本。命令阶段先补记录、来源和可选 tee；暂不做 command_ref 强准入，tree 留独立实验。 |
| DETAIL-6 | 环境白名单阻止新绑定变量；普通 dispatch 不传 trusted_environment；建议显式路径参数 | Correct | 源码和 env probe 均证实。当前阶段保留已有显式参数；后续如简化可用 --binding 定位并查 DB，不依赖 env，也不因此要求完整 manifest。 |
| DETAIL-7 | 阶段 0 缺失败原因分布；DB 无法替代生产统计 | Correct | 原方案未列该原因矩阵，现 metrics 主要聚合时间。计划补 DB/日志/spool/通知/重试联合统计；部分 helper 失败没有 DB 记录。默认本地 config/data_dir 均不存在，生产频率仍未知。 |
| DETAIL-8 | GitHub task 级 context 被覆盖；Web 已按 attempt，建议仿照，不造 revision | Partially correct | 路径覆盖已复现，按 attempt 方案成立；同 attempt prepared refresh 仍需明确更新规则，单改文件名不保存全部准备历史。修订规定更新 prepared artifact 元数据、启动后不覆盖、测试 dispatch 竞态。 |

## 具体证据与建议边界

### MAIN-1：预审核与不可修正失败

- [最终审核选择条件](../../src/robert_agent/run_once.py)（核查时第 6203 行）：`a.status='completed'`，有 worker PID 时需 `process_stopped_at`；无 PID 的受控路径有例外。非 accepted 会更新动作状态并 `_finalize_failed_task`。
- [helper 入库与拒绝标志](../../src/robert_agent/worker/result.py)（核查时第 244 行）：owner/生命周期/重复结果也参与检查；已有 `result_validation` 会拒绝重提。waiting 缺可交付 comment 会设置该标志。
- 临时 DB probe：第一次缺 comment 返回 `failed_validation`；补齐 comment 后返回 `rejected_by_supervisor`；worker_results 仍为 0。
- [remediation 审核](../../src/robert_agent/audit_result.py)（核查时第 383 行）要求独立 attestation。不能直接调用全部 audit 并假造证明；拆共同确定性规则，最终 controller 重验。
- 读取 scope 失败与可修正格式失败分别返回；取消/owner 失效不能改成普通格式重试。预查不会替代进程停止和最终 HEAD 检查。

### MAIN-2：消费集合影响后续任务，但有过滤

- [模板](../../src/robert_agent/render_prompt.py)（核查时第 436 行）预填 renderer 本次 events，结果例子复制该数组；[audit](../../src/robert_agent/audit_result.py)（核查时第 591 行）只要求非空，没有要求全选。
- [pending 查询](../../src/robert_agent/run_once.py)（核查时第 5480 行）读取未消费的 context/pending；[child 创建](../../src/robert_agent/run_once.py)（核查时第 5667 行）重新 authorize，仅 `drives_execution` 生成后续任务。
- 两个既有测试分别验证“未消费 pending 建 child”和“不可信历史参与者不能建 child”，本轮运行通过。
- reviewer 的“默认全量+未处理排除”仍属于新的表达合同。此次保持原字段与行为，既不新增逐项选择，也不根据提交时的最新 DB 自动全选。

### MAIN-3、DETAIL-1、DETAIL-2：缩小基线与接口改造

- `git diff --stat`：10 tracked files，296 insertions/316 deletions；status 另列 2 个新测试、对照文档和计划。
- [action scope](../../src/robert_agent/run_once.py)（核查时第 5286 行）从 DB 推导 repo/base/worktree/branch/remote/sources，再观察 Git HEAD/remote 和特定 review/vote 信息；无 DB mutation，但包含可能耗时的 I/O。
- [目标审核](../../src/robert_agent/audit_result.py)（核查时第 210 行）使用该 scope；[route](../../src/robert_agent/route.py)（核查时第 69 行）与[资源配置](../../src/robert_agent/resources/routes.yml)已定义 outputs/actions。复用现有词汇可以减少兼容转换。
- 前一轮 README 明确普通评论分类尚未真实验证；本记录没有把单元测试当作真实分类效果。

### DETAIL-3、DETAIL-4：marker 与摘要

- [comment marker](../../src/robert_agent/render_prompt.py)（核查时第 44 行）没有 action 身份；[publisher](../../src/robert_agent/publish.py)（核查时第 180 行）按完整 marker 子串查远端 comment。
- [reconcile 正则](../../src/robert_agent/publication_reconcile.py)（核查时第 17 行）允许附加字段，[reconcile 查重](../../src/robert_agent/publication_reconcile.py)（核查时第 197 行）同样按 marker 匹配。
- 探针：远端仅有第一条 comment，第二动作相同 marker 返回 deduplicated；加 action_index 后，两处正则完整匹配，lookup 为 not_found。使用模拟 run_command，没有 GitHub 请求。
- [spool fingerprint](../../src/robert_agent/result_spool.py)（核查时第 15 行）对整个 payload 计算摘要；pending 与 DB 已有结果分别比较摘要。改变规范化 marker 即改变摘要，支持 reviewer 的升级风险判断。
- 新语义键必须含 attempt 身份；重放使用首个有效提交保存的规范化结果，不能按新 normalizer 重建，也不能重新算旧格式指纹。

### DETAIL-5：命令证据与 tree 的证明范围

- [实际 journal](../../src/robert_agent/worker/heartbeat.py)（核查时第 163 行）无 argv/cwd/tree。文件顶部 `build_command_record` 虽有 active_command，不代表真实运行 journal 已保存它。
- [wrapper 输出](../../src/robert_agent/worker/heartbeat.py)（核查时第 259 行）返回尾部与日志位置。临时执行 100 行输出，完整文件 100 行、返回 80 行且 truncated=true。
- 临时 Git 仓库实验：独立 GIT_INDEX_FILE + add -A/write-tree 能识别未跟踪且未忽略文件的变化，真实 index hash 不变；修改 `.gitignore` 排除的 local.env 后 tree 不变。故 tree 相等不是完整执行输入一致的证明。
- 记录 argv/cwd 和明示来源成立；原样输出采用显式 tee 模式，保留大输出摘要选择。严格新鲜度准入须覆盖旧新证据路径，当前方案先移除该承诺。

### DETAIL-6、DETAIL-7、DETAIL-8：环境、数据与文件

- [环境白名单](../../src/robert_agent/dispatch.py)（核查时第 33 行）剔除未允许变量；[普通 worker kwargs](../../src/robert_agent/run_once.py)（核查时第 5077 行）未传 trusted_environment。probe 中未列入白名单的 ROBERT_BINDING 被丢弃。
- [metrics](../../src/robert_agent/execution_metrics.py)（核查时第 96 行）按任务/route/生命周期聚合时间，没有所需错误原因分布。[helper](../../src/robert_agent/worker/result.py)（核查时第 185 行）部分失败直接返回；仅查 DB 会漏报。
- 按 `paths.default_config_path/default_data_dir` 检查，默认 config 和 data_dir 均不存在。只证明这两个入口不可用，不能断言其他机器/自定义目录没有生产 DB。
- [GitHub context writer](../../src/robert_agent/run_once.py)（核查时第 2910 行）重复使用 task 下同一路径；临时调用两次，旧路径内容被第二次覆盖。[prepared refresh](../../src/robert_agent/run_once.py)（核查时第 3026 行）限 queued/prepared；[resume](../../src/robert_agent/run_once.py)（核查时第 4335 行）也调用同一 writer。[Web](../../src/robert_agent/run_once.py)（核查时第 3592 行）已使用 attempt 文件名。

## 本轮验证命令与结果

现有 `.venv/bin/python -B -m unittest` 分批运行以下 10 个不同测试，全部通过：

```text
tests.test_worker_protocol.WorkerProtocolTests.test_update_existing_pr_response_comment_must_follow_push_action
tests.test_worker_protocol.WorkerProtocolTests.test_comment_action_without_idempotency_marker_fails_audit
tests.test_worker_protocol.WorkerProtocolTests.test_stale_attempt_result_is_recorded_for_audit
tests.test_run_once.RunOnceDryRunTests.test_unconsumed_pending_event_creates_child_task_after_parent_result
tests.test_run_once.RunOnceDryRunTests.test_legacy_participant_pending_events_cannot_spawn_child
tests.test_worker_protocol.WorkerProtocolTests.test_heartbeat_returns_only_tail_for_large_output
tests.test_worker_protocol.WorkerProtocolTests.test_heartbeat_preserves_command_result_when_snapshots_are_locked
tests.test_worker_protocol.WorkerProtocolTests.test_heartbeat_journals_completion_before_database_write
tests.test_worker_adapter_config.WorkerEnvironmentTests.test_worker_environment_is_allowlisted
tests.test_run_once.RunOnceDryRunTests.test_active_prepared_pr_task_is_rerouted_when_pr_followup_context_arrives
```

首次父侧批次有一条测试类名误写成 WorkerAdapterConfigTests，产生 unittest loader error；读取实际类名后单独重跑正确的 WorkerEnvironmentTests，通过。其余该批次 4 项均通过，不归因于产品失败。

另做临时 DB/模拟远端/Git 仓库探针，结果已记录在上文；临时数据未进入生产工作区。仅文档修改，未重跑全套应用测试，没有真实性能/生产频率结论。方案与本报告检查链接、引用路径、Markdown 和 diff；产品源文件 hash 首尾一致。

## 核查源码指纹（SHA256）

```text
src/robert_agent/run_once.py                 1c56060b08fe69859bf7148e45eefd9c1fbaad5d86d7e0613bf9cfeb014c15ce
src/robert_agent/worker/result.py            43551795d8df3db481f3182ac31385070d8bbb4e31841ae9f03e23bde557e3fc
src/robert_agent/audit_result.py             ef7efd1f2bfd7af82a327524d26dea582efa302fce7e353d2db0bced08141962
src/robert_agent/result_spool.py             cabb85494cd29eb1c54f90c97da3ebd8015cd8eea229c630098adee39971db27
src/robert_agent/publish.py                  a38f40ac42c1db0d48d4c52eb353604c376149207a848b2ab898fd701c78c2d5
src/robert_agent/publication_reconcile.py    7e73b3bc26902fadac354aa5230b2fec9c36bc606e93768243c87f8b069e6e3d
src/robert_agent/worker/heartbeat.py         2ef53b3f7bea51463d362306b11170f1d799834389f004c174035e10fea3ea70
src/robert_agent/dispatch.py                 2260a31e46347b0b81aceaf3609245d3090b29cc41b4099a06471b0163e5174d
src/robert_agent/render_prompt.py            15c856a746eebb6a6c1e75ff626c3d26f4610e7fd4fa2039ebd5f4e7db6d7d12
```

下一步以[修订方案](../superpowers/plans/2026-10-29-worker-contract-optimization.md)为入口；该链接不表示产品实施已获授权或已经完成。
