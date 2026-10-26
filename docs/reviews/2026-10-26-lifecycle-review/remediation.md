# 生命周期 review 修复报告

本轮可直接修复范围已验证；合并报告尚未全部关闭。

原始 38 个 finding ID 均保留：**26 fixed、11 pending、1 rejected**。`fixed` 只指报告已核实的修复子项；复合 finding 仍含待定子项时保留 `pending`。原始判定、优先级与证据不重写，见 [合并报告](report.md) 和 [原始 ledger](findings.json)。

## 范围与身份

- 基线：`764c5e3771565899b4b7dcf45044a229a31fb591`，分支 `codex/multi-agent-vote`。开始时 tracked tree 干净，原有 review 目录未跟踪且保留。
- 最终变更：36 个 code/test 路径，SHA256 `8ce32bc5b70789bd35a871d5a314bac0708d2b37d002de7784c85973f86c451e`。全部 code/test/CI 输入身份：`efb36f8a605c5ece80a01ef7905aa4967434918301d3bc7eb923362bd1caf997`。
- 只修改本地实现、测试与报告；修复和验证阶段未提交、推送、创建 PR、访问生产数据库或调用外部 GitHub/model 服务；随后按用户要求提交本轮内容为 PR。
- 1 个实现 subagent 分批承担 worker、scheduler、migration、deadline 修复；交接后再启用 1 个只读 code-reviewer。父线程负责整合和全套验证。

## 主要行为变化

1. **保住进程所有权。** 接收结果不会释放仍存活的 worker；stale worker 仍占仓库与全局容量。远端关闭会收拢 coordinator 与独立模型进程组。确认退出前不审计/发布；未知身份保持 attention。
2. **缩短写锁占用并处理竞态。** Git、远端读取、进程观察和终止放到域写事务外。回写前加锁复核最新 attempt、heartbeat、metadata、结果及 execution control。恢复命令证据先观察再回填。控制面超时不会变成“无进展”或“作用域不合法”的业务失败。
3. **控制面可中断收尾。** 命令使用剩余 deadline；daemon 精确记录 executor 身份，超时停止控制子进程组后收尾相应 run、steps 和 lease，重启核查遗留 executor。TERM/KILL 的进程退出竞态不会使 daemon 抛异常退出。默认 daemon lease TTL 从 510 秒增至 567 秒，以覆盖 rate query 和最坏清理预算。
4. **减少无效循环与写入。** 健康且无变化的 worker 不再触发完整流水线；相同 phase 合并，父 heartbeat 合并刷新，busy heartbeat 延后重试。queued vote、分类 handoff 与退避期限进入统一 runnable 判断。
5. **削减可避免的存储与扫描成本。** 活动模型、phase、待投递通知使用索引；诊断 retention 有界追赶积压；两个旧数据迁移按 name/checksum 只执行一次；rejected spool 移出 pending 扫描目录。SHA256 改为流式读取，完整性校验仍保留。
6. **修正路由及配额门槛。** 明确分析/实现/测试请求优先，英文 bug/fix 不再匹配子串；search 低配额不再禁止仅用 core 的本地发布。

## 相同合成负载的前后测量

这些是同一 Python 3.14.7/runtime、同一机器、相同输入的隔离测量；没有生产规模耗时或分钟级加速的结论。完整输入和输出见 [probe](remediation-evidence/robert-remediation-cost-probe.py)、[before](remediation-evidence/robert-remediation-cost-before.json)、[after](remediation-evidence/robert-remediation-cost-after.json)。

| 场景 | 修复前 | 修复后 | 保留的契约 |
| --- | ---: | ---: | --- |
| 20 ms 命令，heartbeat 间隔 1.5 s | 1.5226 s | 0.1060 s | 仍记录命令退出并监督超时 |
| 连续 3 次相同 phase 快照 | 3 行 | 1 行 | 每次 heartbeat 更新，真实转换保留 |
| 16 MiB 文件 SHA256 的 tracemalloc 峰值 | 16,778,061 B | 2,229,497 B | digest 相同；仍全量校验 |
| 100,000 条终态 review_runs 的活动查询，50 次均值 | 4.2271 ms / SCAN | 0.0012 ms / 覆盖索引 | 事务内容量准入保持 |
| warm init 的 mainline JOIN / route UPDATE | 1 / 1 | 0 / 0 | 旧数据首轮迁移和失败重试保持 |

healthy-worker 回归另验证 `run_loop.cycles == 0`、`agent_runs == 0`；不表示进程探测零成本。进度 COUNT 仍在实际业务轮次执行，父取消/ownership 仍每次检查。

## 验证

- 全套：`source .venv/bin/activate && python3 -B -m unittest discover -s tests`：**1132 tests，196.111 秒，OK，1 skipped**。见 [完整日志](remediation-evidence/robert-remediation-full-tests-final.log)。
- `python3 -m ruff check src tests` 通过；CI 配置的 Mypy 范围通过（9 个 source files）；`python3 -m compileall -q src`、`python3 -m build`、`git diff --check` 通过。构建产出 wheel 和 sdist；未发布。
- 新增有意义的失败回归覆盖结果/进程分离、写锁与并发提交、deadline、调度、锁竞争、spool、公平性、迁移及 hash 内存。先复现失败，再修复；关键 red/green 和初始兼容失败日志保存在 evidence 目录。
- 两轮独立复审发现的 `FINAL-01..04` 均修复，并针对最后固定快照复核。见 [最终独立审查](remediation-evidence/robert-remediation-final-review-final.json)、[身份清单](remediation-evidence/robert-remediation-review-manifest-final.json)、[边界审计](remediation-evidence/robert-remediation-refactor-audit.md)。
- 全套日志出现 ResourceWarning（未关闭 SQLite/Popen 句柄）；本轮未把它升级为已确认生产泄漏，也未宣称解决该警告。macOS 上跳过 1 个要求 Linux bubblewrap 的用例，未将其计为行为验证通过。

## 未关闭项与保留边界

- **R19 / B/III/4、B/III/5：自动回收策略待定。** worktree、refs、日志和审计证据没有启用自动删除。需要保留期限、归档/压缩与恢复要求；已向用户询问，尚未收到选择。rejected spool 公平性和 hash 内存子项已经修复，但复合原 ID 仍 pending。
- **R23–R26：保留现有策略。** 全局 timeout、未知身份占容量、轮询/串行采集、同 workstream 顺序和 review 三阶段没有擅自改变。相关 deadline/写锁子项已修；更改这些策略须另定契约并测量实际占比。
- **U01 / B/I/10：待补证。** 缺少常规生产路径触发 pending fact 无限空转的复现；保留幂等事实。
- **R18：保留重复完整性扫描。** 已降低读取峰值内存；尚无“producer 不能修改源文件”的证明，所以没有合并或删除扫描。
- 沿用原诊断保留天数，不自动 VACUUM，不删除业务事实、审计、wakeup 幂等证据或稳定锁 inode。删除后的 SQLite 页可重用，不保证文件立即缩小。
- 本轮是隔离 SQLite/本地真实进程验证；真实 daemon 部署、重启与模型/GitHub 生命周期仍需上线前验收。没有在运行中的生产状态上回滚或迁移。

## 按原始 ID 的最终账本

具体变更/测试按 R/T 主题关联，一处修复可以覆盖多个原 ID；重复项不丢失。可机读版本见 [remediation.json](remediation.json)。

| Finding ID | Status | 原证据 | 变更/测试主题 | 原因或下一步 |
| --- | --- | --- | --- | --- |
| A/1 | fixed | [R01](report.md#r01) | [R01](#change-r01) | 已核实子项修复，回归及最终检查通过。 |
| A/2 | fixed | [R02](report.md#r02) | [R02](#change-r02) | 已核实子项修复，回归及最终检查通过。 |
| A/3 | fixed | [R03](report.md#r03) | [R03](#change-r03) | 已核实子项修复，回归及最终检查通过。 |
| A/4 | fixed | [R05](report.md#r05) | [R05](#change-r05) | 已核实子项修复，回归及最终检查通过。 |
| A/5 | fixed | [R07](report.md#r07) | [R07](#change-r07) | 已核实子项修复，回归及最终检查通过。 |
| A/6 | fixed | [R08](report.md#r08) | [R08](#change-r08) | 已核实子项修复，回归及最终检查通过。 |
| A/7 | fixed | [R09](report.md#r09) | [R09](#change-r09) | 已核实子项修复，回归及最终检查通过。 |
| A/8 | fixed | [R10](report.md#r10) | [R10](#change-r10) | 已核实子项修复，回归及最终检查通过。 |
| A/9 | fixed | [R14](report.md#r14) | [R14](#change-r14) | 已核实子项修复，回归及最终检查通过。 |
| A/PERFORMANCE-1 | fixed | [R17](report.md#r17) | [R17](#change-r17) | 已核实子项修复，回归及最终检查通过。 |
| A/PERFORMANCE-2 | fixed | [R18](report.md#r18) | [R18](#change-r18) | 已核实子项修复，回归及最终检查通过。 |
| A/VALIDATION-1 | fixed | [T01](report.md#t01) | [T01](#change-t01) | 已核实子项修复，回归及最终检查通过。 |
| B/I/1 | fixed | [R06](report.md#r06) | [R06](#change-r06) | 已核实子项修复，回归及最终检查通过。 |
| B/I/2 | fixed | [R05](report.md#r05) | [R05](#change-r05) | 已核实子项修复，回归及最终检查通过。 |
| B/I/3 | pending | [R23](report.md#r23) | [R23](#change-r23) | 全局 hard timeout 是现有配置契约；没有擅自改成长模型配置覆盖全局上限。 |
| B/I/4 | fixed | [R04](report.md#r04)、[R03](report.md#r03) | [R04](#change-r04)、[R03](#change-r03) | 已核实子项修复，回归及最终检查通过。 |
| B/I/5 | pending | [R24](report.md#r24) | [R24](#change-r24) | 未知进程/后代的 attention 和容量保留属于既有安全边界；没有取消身份核验或强制释放。 |
| B/I/6 | fixed | [R11](report.md#r11) | [R11](#change-r11) | 已核实子项修复，回归及最终检查通过。 |
| B/I/7 | fixed | [R12](report.md#r12) | [R12](#change-r12) | 已核实子项修复，回归及最终检查通过。 |
| B/I/8 | fixed | [R03](report.md#r03) | [R03](#change-r03) | 已核实子项修复，回归及最终检查通过。 |
| B/I/9 | fixed | [R21](report.md#r21) | [R21](#change-r21) | 已核实子项修复，回归及最终检查通过。 |
| B/I/10 | pending | [U01](report.md#u01) | [U01](#change-u01) | 缺少常规生产路径的 pending fact 反复触发复现；未改幂等事实或直接丢弃 pending 数据。 |
| B/I/11 | fixed | [R13](report.md#r13) | [R13](#change-r13) | 已核实子项修复，回归及最终检查通过。 |
| B/LATENCY-1 | pending | [R25](report.md#r25) | [R25](#change-r25) | 修复已确认 deadline/写锁子项；轮询周期与串行远端采集策略尚需实际负载数据，未擅自改变。 |
| B/LATENCY-2 | fixed | [R13](report.md#r13) | [R13](#change-r13) | 已核实子项修复，回归及最终检查通过。 |
| B/LATENCY-3 | pending | [R23](report.md#r23) | [R23](#change-r23) | 全局 hard timeout 是现有配置契约；没有擅自改成长模型配置覆盖全局上限。 |
| B/LATENCY-4 | fixed | [R06](report.md#r06) | [R06](#change-r06) | 已核实子项修复，回归及最终检查通过。 |
| B/LATENCY-5 | rejected | [N01](report.md#n01) | [N01](#change-n01) | 原说法被当前代码和隔离证据反驳，维持 Incorrect；不实施相反修复。 |
| B/LATENCY-6 | pending | [R24](report.md#r24) | [R24](#change-r24) | 未知进程/后代的 attention 和容量保留属于既有安全边界；没有取消身份核验或强制释放。 |
| B/LATENCY-7 | pending | [R25](report.md#r25)、[R06](report.md#r06)、[R03](report.md#r03) | [R25](#change-r25)、[R06](#change-r06)、[R03](#change-r03) | 修复已确认 deadline/写锁子项；轮询周期与串行远端采集策略尚需实际负载数据，未擅自改变。 |
| B/LATENCY-8 | pending | [R26](report.md#r26) | [R26](#change-r26) | 同 workstream 顺序和 prepare/review/synthesis 三阶段是现有契约；未删阶段或放开冲突任务并行。 |
| B/LATENCY-9 | pending | [R26](report.md#r26)、[R18](report.md#r18) | [R26](#change-r26)、[R18](#change-r18) | 同 workstream 顺序和 prepare/review/synthesis 三阶段是现有契约；未删阶段或放开冲突任务并行。 |
| B/III/1 | fixed | [R08](report.md#r08)、[R09](report.md#r09) | [R08](#change-r08)、[R09](#change-r09) | 已核实子项修复，回归及最终检查通过。 |
| B/III/2 | fixed | [R15](report.md#r15) | [R15](#change-r15) | 已核实子项修复，回归及最终检查通过。 |
| B/III/3 | fixed | [R16](report.md#r16)、[R17](report.md#r17) | [R16](#change-r16)、[R17](#change-r17) | 已核实子项修复，回归及最终检查通过。 |
| B/III/4 | pending | [R18](report.md#r18)、[R19](report.md#r19) | [R18](#change-r18)、[R19](#change-r19) | 自动删除 worktree/refs/日志/审计证据仍需保留期限、归档与恢复策略；等待用户答复。未在线删除稳定 lock inode。 |
| B/III/5 | pending | [R19](report.md#r19)、[R20](report.md#r20) | [R19](#change-r19)、[R20](#change-r20) | 自动删除 worktree/refs/日志/审计证据仍需保留期限、归档与恢复策略；等待用户答复。未在线删除稳定 lock inode。 |
| B/III/6 | fixed | [R17](report.md#r17)、[R22](report.md#r22)、[R09](report.md#r09) | [R17](#change-r17)、[R22](#change-r22)、[R09](#change-r09) | 已核实子项修复，回归及最终检查通过。 |

## 变更与回归定位

<a id="change-r01"></a>
### R01 · fixed

远端关闭先持久化 fence，在写锁外停止 coordinator 与注册的独立模型；未知进程保留 attention。

代码/测试：`run_once.py / lifecycle:remote_close_stops_registered_native_models`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/run_once.py](../../../src/robert_agent/run_once.py)、[tests/test_lifecycle_remediation.py](../../../tests/test_lifecycle_remediation.py)

<a id="change-r02"></a>
### R02 · fixed

结果接收保留实际进程所有权、容量及监督；确认进程组停止后才完成审计与发布。

代码/测试：`worker/result.py + supervise.py + retry_policy.py / lifecycle:received_result,fast_receipt,result_arriving; controlplane:publication_requires_recorded_worker_process_stop`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/worker/result.py](../../../src/robert_agent/worker/result.py)、[src/robert_agent/supervise.py](../../../src/robert_agent/supervise.py)、[src/robert_agent/retry_policy.py](../../../src/robert_agent/retry_policy.py)、[tests/test_lifecycle_remediation.py](../../../tests/test_lifecycle_remediation.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r03"></a>
### R03 · fixed

远端读取、Git、进程检查和停止移出域写事务；重新拿写锁后核对 attempt/heartbeat/metadata/result/控制权；命令 phase 回填安排在外部观察之后。Git 观察超时直接中止本轮，不能当成无进展或不合法 scope。

代码/测试：`run_once.py / lifecycle:health_reads,backfill_does_not_hold_writer,startup_command_inspection,result_arriving`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/run_once.py](../../../src/robert_agent/run_once.py)、[tests/test_lifecycle_remediation.py](../../../tests/test_lifecycle_remediation.py)

<a id="change-r04"></a>
### R04 · fixed

诊断 heartbeat 的 SQLITE_BUSY 短等待并延后重试；取消/ownership 每次检查，非锁错误仍上抛。

代码/测试：`model_execution.py / runtime:busy_heartbeat_write,non_contention_heartbeat_errors,cancellation_is_checked`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/model_execution.py](../../../src/robert_agent/model_execution.py)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r05"></a>
### R05 · fixed

仓库和全局 admission 共用 occupied 谓词；stale 及已收结果但未确认退出的进程都占容量。

代码/测试：`daemon_state.py + supervise.py / lifecycle:stale_live_worker_keeps_dispatch_capacity`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/daemon_state.py](../../../src/robert_agent/daemon_state.py)、[src/robert_agent/supervise.py](../../../src/robert_agent/supervise.py)、[tests/test_lifecycle_remediation.py](../../../tests/test_lifecycle_remediation.py)

<a id="change-r06"></a>
### R06 · fixed

Git/GitHub 命令继承剩余控制 deadline；外层 child 有独立 executor nonce、进程组清理和精确 run/lease 收尾；启动时核查遗留 executor；TERM/KILL 退出竞态容错；TTL 纳入 rate 与 cleanup 预算。

代码/测试：`daemon.py + common.py + worktree.py + publish.py + run_once.py / deadline:8 regressions; controlplane:timed_out_control_child,expired_control_deadline`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/common.py](../../../src/robert_agent/common.py)、[src/robert_agent/daemon.py](../../../src/robert_agent/daemon.py)、[src/robert_agent/worktree.py](../../../src/robert_agent/worktree.py)、[src/robert_agent/publish.py](../../../src/robert_agent/publish.py)、[tests/test_deadline_remediation.py](../../../tests/test_deadline_remediation.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r07"></a>
### R07 · fixed

用 Popen.wait(timeout) 等待下一个心跳或命令退出，命令结束立即返回。

代码/测试：`worker/heartbeat.py / runtime:command_completion_does_not_wait`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/worker/heartbeat.py](../../../src/robert_agent/worker/heartbeat.py)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r08"></a>
### R08 · fixed

健康且无状态转换的 worker 使用只读 liveness probe；死亡、超时、首次 stale、结果待完成才进入完整业务流水线。

代码/测试：`loop_engine.py / scheduler:healthy_worker_does_not_create_business_runs,observation_is_read_only,dead_worker_is_runnable`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/loop_engine.py](../../../src/robert_agent/loop_engine.py)、[tests/test_scheduler_remediation.py](../../../tests/test_scheduler_remediation.py)

<a id="change-r09"></a>
### R09 · fixed

沿用保留期，单次维护最多 3 批/1 秒，存在 backlog 时缩短再次维护间隔；health 保持独立正常周期。

代码/测试：`daemon.py + retention.py / controlplane:maintenance_catches_up_more_than_one_event_batch`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/daemon.py](../../../src/robert_agent/daemon.py)、[src/robert_agent/retention.py](../../../src/robert_agent/retention.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r10"></a>
### R10 · fixed

queued review/vote 使用统一有 repo/workstream/attention 约束的探测。

代码/测试：`loop_engine.py / scheduler:queued_vote_is_discovered,native_queue_scope`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/loop_engine.py](../../../src/robert_agent/loop_engine.py)、[tests/test_scheduler_remediation.py](../../../tests/test_scheduler_remediation.py)

<a id="change-r11"></a>
### R11 · fixed

Git 前持久化分类 handoff，失败有 3 次/10 分钟上限及 durable backoff，耗尽转 attention；本地探测与恢复共享查询。

代码/测试：`run_once.py + loop_engine.py / lifecycle:classification_handoff_survives_git_failure; scheduler:orphaned_classification`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/run_once.py](../../../src/robert_agent/run_once.py)、[tests/test_lifecycle_remediation.py](../../../tests/test_lifecycle_remediation.py)、[tests/test_scheduler_remediation.py](../../../tests/test_scheduler_remediation.py)

<a id="change-r12"></a>
### R12 · fixed

prepared.resume.not_before_at 未到时不触发 drain；真实超时配置传入 probe。

代码/测试：`loop_engine.py / scheduler:prepared_backoff_waits_until_due`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/loop_engine.py](../../../src/robert_agent/loop_engine.py)、[tests/test_scheduler_remediation.py](../../../tests/test_scheduler_remediation.py)

<a id="change-r13"></a>
### R13 · fixed

明确分析指令优先，英文 bug/fix 使用单词边界；明确实现和补测试请求直接进入对应 route。

代码/测试：`discover.py / controlplane:analysis_and_implementation_intents`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/discover.py](../../../src/robert_agent/discover.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r14"></a>
### R14 · fixed

search 低配额不再把仅用 core 的本地 publication 全部关闭；core 门槛与失败反馈保留。

代码/测试：`daemon.py / controlplane:search_floor_does_not_disable_core_only_publication`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/daemon.py](../../../src/robert_agent/daemon.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r15"></a>
### R15 · fixed

事务内按 attempt 合并连续相同 phase/status/summary/next_step；每次仍刷新 heartbeat，真实转换保留。

代码/测试：`worker/snapshot.py / runtime:identical_snapshots,duplicate_snapshot`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/worker/snapshot.py](../../../src/robert_agent/worker/snapshot.py)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r16"></a>
### R16 · fixed

多 reviewer 共享父 heartbeat 的 30 秒刷新预算；忙锁后 1 秒重试，取消探测频率不降低。

代码/测试：`model_execution.py / runtime:repeated_activity_checks,parent_heartbeat_refreshes`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/model_execution.py](../../../src/robert_agent/model_execution.py)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r17"></a>
### R17 · fixed

添加 reserved 活动子集、attempt phase 时间线及通知索引，减少无进展轮次；实际业务推进的进度统计仍保留。

代码/测试：`review_store.py + vote_store.py + schema.sql + loop_engine.py / runtime:activity_queries,legacy_model_tables; scheduler:healthy_worker`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/review_store.py](../../../src/robert_agent/review_store.py)、[src/robert_agent/vote_store.py](../../../src/robert_agent/vote_store.py)、[src/robert_agent/resources/db/schema.sql](../../../src/robert_agent/resources/db/schema.sql)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r18"></a>
### R18 · fixed

分块 SHA256 降低大文件峰值内存；所有原有全源完整性验证继续执行，未证明可安全合并的扫描不删除。

代码/测试：`review_files.py / runtime:file_hash_preserves_digest_with_bounded_memory`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/review_files.py](../../../src/robert_agent/review_files.py)、[tests/test_runtime_remediation.py](../../../tests/test_runtime_remediation.py)

<a id="change-r19"></a>
### R19 · pending

自动删除 worktree/refs/日志/审计证据仍需保留期限、归档与恢复策略；等待用户答复。未在线删除稳定 lock inode。

代码/测试：`report.md#r19`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-r20"></a>
### R20 · fixed

rejected 原子归档到独立子目录，旧 rejected 在持有稳定 attempt 锁后移出 pending glob；保留证据与锁 inode。

代码/测试：`result_spool.py / controlplane:rejected_spool_prefix`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/result_spool.py](../../../src/robert_agent/result_spool.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r21"></a>
### R21 · fixed

PR mainline 与 recommended-skills 数据迁移按独立 name/checksum 门控；失败不提前记录完成，既有迁移序号与必要兼容入口保留。

代码/测试：`storage.py / storage_initialization:5 regressions; test_migration + run_once:legacy_backfill`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/storage.py](../../../src/robert_agent/storage.py)、[tests/test_storage_initialization_remediation.py](../../../tests/test_storage_initialization_remediation.py)

<a id="change-r22"></a>
### R22 · fixed

health 与 backlog 清理分开节流，通知投递命中 pending 索引；修正 health 写通知的 docstring，保留业务事实，不自动 VACUUM。

代码/测试：`daemon.py + schema.sql + reliability_health.py / controlplane:notification_delivery_queries_only_pending_history,maintenance_catches_up`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[src/robert_agent/daemon.py](../../../src/robert_agent/daemon.py)、[src/robert_agent/resources/db/schema.sql](../../../src/robert_agent/resources/db/schema.sql)、[src/robert_agent/reliability_health.py](../../../src/robert_agent/reliability_health.py)、[tests/test_controlplane_remediation.py](../../../tests/test_controlplane_remediation.py)

<a id="change-r23"></a>
### R23 · pending

全局 hard timeout 是现有配置契约；没有擅自改成长模型配置覆盖全局上限。

代码/测试：`report.md#r23`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-r24"></a>
### R24 · pending

未知进程/后代的 attention 和容量保留属于既有安全边界；没有取消身份核验或强制释放。

代码/测试：`report.md#r24`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-r25"></a>
### R25 · pending

修复已确认 deadline/写锁子项；轮询周期与串行远端采集策略尚需实际负载数据，未擅自改变。

代码/测试：`report.md#r25`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-r26"></a>
### R26 · pending

同 workstream 顺序和 prepare/review/synthesis 三阶段是现有契约；未删阶段或放开冲突任务并行。

代码/测试：`report.md#r26`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-n01"></a>
### N01 · rejected

原说法被当前代码和隔离证据反驳，维持 Incorrect；不实施相反修复。

代码/测试：`report.md#n01`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-u01"></a>
### U01 · pending

缺少常规生产路径的 pending fact 反复触发复现；未改幂等事实或直接丢弃 pending 数据。

代码/测试：`report.md#u01`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

<a id="change-t01"></a>
### T01 · fixed

Barrier 确定两个 unknown launch 均已持久化；只确认一个拒绝释放，确认全部才回收，去掉测试竞态。

代码/测试：`tests/test_vote_recovery.py / dual unknown launch regression`。测试名为稳定的前缀定位，完整方法名见对应新增 `test_*_remediation.py`。

[tests/test_vote_recovery.py](../../../tests/test_vote_recovery.py)

## 可复用证据

- [验证记录与内容身份](verification.json)、[证据文件校验清单](remediation-evidence/evidence-index.json)。
- 原始 [report.md](report.md)、[findings.json](findings.json) 的 finding ID、判定与证据顺序保留；公开副本只转换本机路径和文件链接，原始哈希及转换记录见 [publication.json](publication.json)。
- 复用前应比较 base、全部 source/test 输入、runtime、命令和 scope；后续代码或相关输入变化会使本轮验证失效。
