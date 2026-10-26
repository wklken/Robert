# Robert 生命周期、耗时与存储：合并核验报告

核验日期：2026-10-26。核验方式：`fast-verify-review-point`；父 agent 与一位只读 `finding-verifier` 分工，使用固定源码、隔离 SQLite、mock 外部边界及真实本地进程。没有修复生产代码、接触生产数据库、调用 GitHub/模型服务或发布内容。

## 1. 锁定范围与原始输入

- 仓库：`.`；分支：`codex/multi-agent-vote`；checked HEAD：`764c5e3771565899b4b7dcf45044a229a31fb591`。
- PR/base：`Not applicable`。本任务是整个项目机制核验，所有输入的 Introduced 都为 `Not applicable`，不推断哪个 PR 引入。
- 来源 A：本聊天上一份报告，原审查版本 `1b23d3b635d3b319e02f3f73029ff5676f30eb26`；原编号 1–9，以及按原文分组补充的两个 PERFORMANCE 和一个 VALIDATION synthetic ID，共12项。
- 来源 B：用户附件，原始版本未给出，记录为 `Unverified`，不根据行号猜测版本；一节11项原编号、二节9项按分组派生的 LATENCY synthetic ID、三节6项原编号，共26项。
- 共38个输入ID全部保留原顺序。`A/1`、`B/I/1` 的数字仍是原编号，前缀只用于跨报告消歧；Rxx 是合并主题的第二视图，不替换、重编号或删除任何原条目。
- 审查路径：`src/robert_agent`、相关测试、timeout/multi-model文档及历史可靠性报告。排除生产遥测、实际部署资源占比、外部调用端到端压测和自动修复。
- 上轮后工作区产生的 vote 修复已经成为 `764c5e3`，本轮核验包括该提交。对源码未变的旧隔离测量，通过文件和fixture逐字节比较后复用；证据中明确区分旧测量与本轮重跑。
- [来源A结构化摘录](sources/a-previous-review.md)、[来源B原文](sources/b-additional-review.txt)、[机器可读完整合同](findings.json)。来源B SHA-256：`1db7dbf48221d6f133773f19119dc71e615fb6b571852d46fc82e01e92b59823`。

## 2. 合并结论

38项输入的主张判定为 **21 Correct、16 Partially correct、1 Incorrect**。Correct 只表示行为主张成立，不代表它一定是 bug；同源顺序推进等明确契约依然保留，并拒绝当成无效工作删除。

按独立修复主题合并为29组：15个应处理的缺陷主题（含1个有明确条件的spool公平性缺陷）、7个已证成本或保留策略优化、4组策略/延迟说明、1项被反证的说法、1项待补证风险、1项测试竞态。原严重度保留在台账；合并优先级是基于本轮证据的单独评估。

优先修复 R01/R02 的进程清理与所有权、R03 的长写事务、R06 的超时遗留；再处理 R07/R08/R09 的额外等待、空转和清理能力，以及 R10/R11/R12 的调度条件不一致。R04 是长锁竞争对模型轮次的直接放大效应。

两份报告都没有生产表级体积、活跃任务轨迹和耗时采样，**不能据此断言2.05GB主要由当前轮询造成，或把历史P95直接作为今天的性能基线**。文件系统快照/日志增长与SQLite页增长也必须分别统计。

## 3. 必须纠正的说法与修复边界

1. 全局45分钟是已声明的独立限制；改为worker优先会改变策略，可以先显示effective timeout并协调配置。
2. 已死进程不需要等20/45分钟才发现；每轮已有身份检查，活着但无心跳是另一种状态。
3. 强杀后的repo lease等待是剩余TTL，不是从强杀时重新等满九分钟；health指标存在不能证明线上事故已经发生。
4. retention的100是每类候选批次数；选中的run及其步骤会一起删。不能推导固定八万run的稳态，更不能据此解释2GB。
5. leader已死但后代身份未知时，attention保留容量是安全边界。不能只凭旧PGID直接killpg或到时释放。
6. 锁冲突不能简单跳过全部check_active；旧锁文件也不能在有writer/waiter时在线unlink；事实、幂等、审计记录不能统一按年龄删。
7. classification提交后再做Git避免大事务，这个边界应保留；需要补durable handoff和本地恢复，不是把Git移回事务。

## 4. 去重后的主题总表

| 主题 | 优先级/性质 | 结论 | 对应原ID |
| --- | --- | --- | --- |
| [R01](#r01) | P1 | 远端关闭只收拢父进程，独立模型可能失去监管 | A/1 |
| [R02](#r02) | P1 | 结果入库和进程退出被合并为同一个完成状态 | A/2 |
| [R03](#r03) | P1 | 若干外部 I/O 跨 SQLite 写事务 | A/3, B/I/4, B/I/8, B/LATENCY-7 |
| [R04](#r04) | P2 | 可重试的 heartbeat 写锁竞争会升级为整轮 review 失败 | B/I/4 |
| [R05](#r05) | P2 | stale 存活进程从实际 admission 容量中消失 | A/4, B/I/2 |
| [R06](#r06) | P1 | 执行器超时会强杀持久化中的执行，且缺少遗留 run 的自动收尾 | B/I/1, B/LATENCY-4, B/LATENCY-7 |
| [R07](#r07) | P2 | 心跳 sleep 给被包装命令增加退出检测延迟 | A/5 |
| [R08](#r08) | P2 | 完整流水线被用作无变化的进程监督 | A/6, B/III/1 |
| [R09](#r09) | P2 | 默认事件清理能力低于一部分持续活跃负载的事件产生速率 | A/7, B/III/1, B/III/6 |
| [R10](#r10) | P2 | 孤立 queued vote 没有进入本地 runnable 探测 | A/8 |
| [R11](#r11) | P2 | 分类后继创建失败的 durable handoff 不在本地探测中 | B/I/6 |
| [R12](#r12) | P2 | prepared 退避期限未进入 runnable 判定 | B/I/7 |
| [R13](#r13) | P2 | 子串启发式误判意图，且部分明确实现请求多一轮分类 | B/I/11, B/LATENCY-2 |
| [R14](#r14) | P2 | 外部 search 配额门槛被用于本地 publication | A/9 |
| [R15](#r15) | 优化 | 重复心跳永久追加到 phase 历史 | B/III/2 |
| [R16](#r16) | 优化 | native 模型的多个线程重复更新同一个父心跳 | B/III/3 |
| [R17](#r17) | 优化 | 历史规模放大进度统计、模型池与部分恢复查询 | A/PERFORMANCE-1, B/III/3, B/III/6 |
| [R18](#r18) | 优化 | 同轮完整性校验多次全仓读盘且大文件整体入内存 | A/PERFORMANCE-2, B/LATENCY-9, B/III/4 |
| [R19](#r19) | 优化 | round 工作区、refs、日志及证据缺少统一回收策略 | B/III/4, B/III/5 |
| [R20](#r20) | 条件性 P2 | rejected spool 记录可反复耗尽扫描预算，尾部 pending 缺少公平性 | B/III/5 |
| [R21](#r21) | 优化 | init_database 的热路径包含重复扫描和旧迁移入口 | B/I/9 |
| [R22](#r22) | 优化 | health/通知/历史保留的成本与压缩边界需要区分 | B/III/6 |
| [R23](#r23) | 策略说明 | 45 分钟是明确声明的全局上限，不是被 worker 配置意外覆盖 | B/I/3, B/LATENCY-3 |
| [R24](#r24) | 策略说明 | 未知后代和 attention 保留容量是安全边界，恢复体验可以改进 | B/I/5, B/LATENCY-6 |
| [R25](#r25) | 策略说明 | 外部轮询和串行远端采集是延迟来源，实际占比尚未测量 | B/LATENCY-1, B/LATENCY-7 |
| [R26](#r26) | 策略说明 | workstream 顺序推进和 review 三阶段是现有契约 | B/LATENCY-8, B/LATENCY-9 |
| [N01](#n01) | 拒绝 | “死进程必须等20/45分钟才被发现”不成立 | B/LATENCY-5 |
| [U01](#u01) | 待补证 | pending fact 的无限空转风险尚缺常规生产触发证据 | B/I/10 |
| [T01](#t01) | P2 测试 | vote recovery 测试只确认一个 unknown launch，存在竞态 | A/VALIDATION-1 |

## 5. 逐主题证据与最小建议

<a id="r01"></a>
### R01 · P1 · 远端关闭只收拢父进程，独立模型可能失去监管

控制面先在写事务中落 terminal fact，再等待 coordinator 停止。Coordinator 的异常清理先写 session；它被同一个 SQLite writer 阻塞，父进程约两秒后被强杀，独立模型进程组仍在运行。当前版本实进程复现得到 task/attempt=canceled、coordinator_exit=-9、model_alive=True，模型 run 仍为 running、slot_reserved=1。

这会留下无法继续执行其超时逻辑的模型。模型存活期间继续占槽；后续申请能回收已经确认退出的进程组，因此不能称为无条件永久占槽。最小修复是持久化取消意图后释放 writer，在事务外收拢顶层及所有 native 子进程组，确认停止后再关闭资源所有权；未能证明停止则保留可操作的 attention。

原ID：A/1。

控制代码：[src/robert_agent/run_once.py:1183](../../../src/robert_agent/run_once.py#L1183)；[src/robert_agent/review_coordinator.py:164](../../../src/robert_agent/review_coordinator.py#L164)。

隔离证据：[robert-verify-lifecycle-remote-close.out](evidence/robert-verify-lifecycle-remote-close.out)。

<a id="r02"></a>
### R02 · P1 · 结果入库和进程退出被合并为同一个完成状态

合法 record_result 会立即把 attempt 标成 completed。当前实进程复现中，结果已 recorded，worker 仍存活，派发容量却归零；随后 supervisor 数量为零。后续 task/audit/publish 可以推进，但原 worker 或后台命令不再经过常规超时和取消路径。

应分别记录 result received 与 process stopped。结果可以独立审计，但控制面必须保留进程清理责任；在确认对应进程组退出前，不能释放物理资源容量或允许会冲突的后继执行。保持现有结果幂等与迟到结果 fencing。

原ID：A/2。

控制代码：[src/robert_agent/worker/result.py:336](../../../src/robert_agent/worker/result.py#L336)；[src/robert_agent/run_once.py:869](../../../src/robert_agent/run_once.py#L869)。

隔离证据：[robert-verify-lifecycle-capacity.out](evidence/robert-verify-lifecycle-capacity.out)。

<a id="r03"></a>
### R03 · P1 · 若干外部 I/O 跨 SQLite 写事务

最直接的实证是 PR 健康采集：第一个 PR 的 episode/event 写入后，循环读取第二个 PR 时 writer 尚未释放。独立连接在第一次远端调用时可 BEGIN IMMEDIATE，第二次则返回 database is locked。supervise 的进程观察/终止和 audit 的部分 Git 检查也发生在已有写入之后。

B/I/8 的“全程一个事务、全部 gh 都持锁”过宽：代码在 publication、discovery 等位置已有显式 commit，首轮远端采集也可能没有写锁。实际缺陷是仍存在跨 I/O 的长写事务窗口。

应把远端事实读取、进程观察/终止移到短写事务之外；重新入事务时校验版本、owner、attempt 和相关快照。不能为了原子性把耗时外部操作重新塞入大事务。

原ID：A/3, B/I/4, B/I/8, B/LATENCY-7。

控制代码：[src/robert_agent/run_once.py:2239](../../../src/robert_agent/run_once.py#L2239)；[src/robert_agent/run_once.py:6179](../../../src/robert_agent/run_once.py#L6179)。

隔离证据：[robert-verify-network-lock.out](evidence/robert-verify-network-lock.out)。

<a id="r04"></a>
### R04 · P2 · 可重试的 heartbeat 写锁竞争会升级为整轮 review 失败

check_active 同时读取所有权和更新父 attempt 心跳，每个模型等待线程默认每秒执行，连接 timeout=10 秒。真实 writer 持锁 11 秒时，实际 Coordinator.run→_wait→check_active 抛出 OperationalError；session 随后 failed，同轮已注册模型收到 TERM，退出码 -15。

优先消除 R03 的长写事务，再把 best-effort heartbeat 写入与所有权/取消检查分开，控制重试与降频。B/I/4 原建议“锁冲突跳过整个 check_active”不安全：这里也承担当前 attempt 和取消状态的 fencing。是否继续等待必须有有界策略，不能在状态未知时继续派发。

原ID：B/I/4。

控制代码：[src/robert_agent/model_execution.py:136](../../../src/robert_agent/model_execution.py#L136)；[src/robert_agent/review_store.py:81](../../../src/robert_agent/review_store.py#L81)；[src/robert_agent/review_coordinator.py:114](../../../src/robert_agent/review_coordinator.py#L114)。

隔离证据：[robert-verify-agent-lifecycle-results.json](evidence/robert-verify-agent-lifecycle-results.json)。

<a id="r05"></a>
### R05 · P2 · stale 存活进程从实际 admission 容量中消失

daemon_state 把 running/stale 都计入容量，run_once 的实际派发计数只统计 running。stale 仅代表心跳旧，supervise 对其返回 terminate=False。实进程探针：before=1，变 stale 后仍活着，repo/global count 均为 0。

已有 test_stale_attempt_does_not_consume_dispatch_capacity 固化了该机制；这不是补一个与现实现相同的测试就能解决。按仍活着或尚未确认停止的 owner 计算物理容量，统一 daemon 与 dispatch 口径，更新对应行为测试。

原ID：A/4, B/I/2。

控制代码：[src/robert_agent/run_once.py:2407](../../../src/robert_agent/run_once.py#L2407)；[src/robert_agent/daemon_state.py:258](../../../src/robert_agent/daemon_state.py#L258)；[tests/test_run_once.py:7145](../../../tests/test_run_once.py#L7145)。

隔离证据：[robert-verify-lifecycle-capacity.out](evidence/robert-verify-lifecycle-capacity.out)。

<a id="r06"></a>
### R06 · P1 · 执行器超时会强杀持久化中的执行，且缺少遗留 run 的自动收尾

local drain 的内部 max_seconds 和外部 subprocess timeout 使用同一个值，内部只在 cycle 入口检查时间，不能约束已进入 cycle 的 Git/gh/I/O。实际 daemon.run_child_json 超时探针证明：finally 没有执行，已提交 run 保持 running、lease 保持 active，下次 acquisition 被拒绝；即使将 retention 时间推进到 2070 年，这类 run 仍不被清理。

需要修正三个原说法：第 170 秒启动的 cycle 只有执行超过剩余预算才被强杀；repo lease 的默认总 TTL 是从 acquisition 起 540 秒，强杀后等待余下部分，不是重新等九分钟；有 health 指标只能证明检测能力，不能证明线上已发生。dispatch_inflight 的未知结果进入 attention 是必要防重入边界，不能自动重启未知进程。

修复应传播剩余 deadline、约束外部命令、预留协作退出和结果落库时间，并在确认旧 owner 及后代停止后收尾 run/lease。固定减 60 秒不能单独解决阻塞调用；单纯先 TERM 也不能保证未注册处理器的 Python 进程执行 finally。不得启动时盲目把所有 running run 标 failed。

原ID：B/I/1, B/LATENCY-4, B/LATENCY-7。

控制代码：[src/robert_agent/daemon.py:432](../../../src/robert_agent/daemon.py#L432)；[src/robert_agent/daemon.py:101](../../../src/robert_agent/daemon.py#L101)；[src/robert_agent/loop_engine.py:255](../../../src/robert_agent/loop_engine.py#L255)；[src/robert_agent/run_once.py:808](../../../src/robert_agent/run_once.py#L808)。

隔离证据：[robert-verify-agent-lifecycle-results.json](evidence/robert-verify-agent-lifecycle-results.json)。

<a id="r07"></a>
### R07 · P2 · 心跳 sleep 给被包装命令增加退出检测延迟

wrapper 在 poll 后直接 sleep(min(heartbeat_interval, remaining))，默认间隔 60 秒。来源 A 的隔离测量为裸命令 0.078 秒、默认包装 60.025 秒；当前 heartbeat.py 与该测量版本逐字节一致，复用该证据。此结论只针对经 wrapper 执行的命令，不代表所有命令都被包装。

用 proc.wait(timeout=下一次心跳/截止期限) 等待退出，仅在到期仍未结束时记录 heartbeat。保留超时后的后代清理和 durable command record。

原ID：A/5。

控制代码：[src/robert_agent/worker/heartbeat.py:209](../../../src/robert_agent/worker/heartbeat.py#L209)。

隔离证据：[robert-review-probes.log](evidence/robert-review-probes.log)、[reused-evidence-code-hashes.json](evidence/reused-evidence-code-hashes.json)。

<a id="r08"></a>
### R08 · P2 · 完整流水线被用作无变化的进程监督

has_runnable_local_work 把任何可执行 binding 下的 running/stale attempt 视为 runnable，daemon 随即启动 loop_engine/run_once。当前隔离探针仅将进程状态固定为健康，真实执行完整本地流水线：10 次均 no_progress，任务/attempt 均未推进，却写入 10 agent_runs、130 run_steps、80 run_repo_steps、10 leases；daemon 路径另有开始/完成事件。

单仓库每轮是 23 条 run/step/lease 新行；按每五秒一轮、忽略运行耗时估算为 397,440 条/天，另有 34,560 条 daemon_events。它说明存在很高的诊断放大，不是实际生产速率，也未证明它是整个部署的最大耗时项。

拆出轻量进程观察，按状态改变、退出、到期或 durable wakeup 进入完整 drain；保留低频恢复扫描。无变化的监督不应伪装成一次新的业务 run。不能仅延长 sleep，代价会变成结果和取消延迟。

原ID：A/6, B/III/1。

控制代码：[src/robert_agent/loop_engine.py:144](../../../src/robert_agent/loop_engine.py#L144)；[src/robert_agent/daemon.py:581](../../../src/robert_agent/daemon.py#L581)；[src/robert_agent/run_once.py:7117](../../../src/robert_agent/run_once.py#L7117)。

隔离证据：[robert-verify-no-progress.out](evidence/robert-verify-no-progress.out)。

<a id="r09"></a>
### R09 · P2 · 默认事件清理能力低于一部分持续活跃负载的事件产生速率

按默认间隔计算，两条 drain 事件/五秒对应 34,560 条 daemon_events/天；100 条/300 秒的单批清理理论上限为 28,800 条/天。发生持续活跃且单轮足够快的负载时，过期事件可能追不完；锁竞争、运行时间和保留年龄还会降低清理吞吐。

B/III/1 把“每批 100”当成总删除行数不准确：retention 分别选择最多 100 个 run、100 个 event、100 个 lease，选中 run 的步骤还会在同批一并删除。不能用这些参数推导出固定八万 run 的稳态库存。成功 run 被 wakeup 引用等条件也会阻止清理。

先减少无变化记录，再允许 maintenance 在时间/批次预算内追赶积压，分表观测生成率、eligible 数量、清理率。保留关联审计与幂等证据，不做无限清理。

原ID：A/7, B/III/1, B/III/6。

控制代码：[src/robert_agent/retention.py:20](../../../src/robert_agent/retention.py#L20)；[src/robert_agent/retention.py:81](../../../src/robert_agent/retention.py#L81)；[src/robert_agent/daemon.py:403](../../../src/robert_agent/daemon.py#L403)。

隔离证据：[robert-verify-scheduler-storage.out](evidence/robert-verify-scheduler-storage.out)。

<a id="r10"></a>
### R10 · P2 · 孤立 queued vote 没有进入本地 runnable 探测

当前相同临时数据库状态：queued vote 返回 False，queued review 返回 True。实际流水线支持 _prepare_queued_reviews(...round_store=vote_store)，探测却只看 review_sessions。普通完整流水线可能在同轮准备 vote，缺口主要暴露于重启或只剩排队 vote 的状态。

在无其他本地工作时要等外部 poll，默认数分钟；限流会继续推迟。统一 native round 的 eligibility/attention/workstream 检查，覆盖空任务队列与重启恢复，不用额外空 wakeup 掩盖不一致。

原ID：A/8。

控制代码：[src/robert_agent/loop_engine.py:118](../../../src/robert_agent/loop_engine.py#L118)；[src/robert_agent/run_once.py:3657](../../../src/robert_agent/run_once.py#L3657)。

隔离证据：[robert-verify-scheduler-storage.out](evidence/robert-verify-scheduler-storage.out)。

<a id="r11"></a>
### R11 · P2 · 分类后继创建失败的 durable handoff 不在本地探测中

分类父结果先提交，再准备真实工作区是正确的短事务方向；但在两者之间 Git 失败后，当前实证为 failed_route、父 task completed、child=0、wakeup consumed、has_runnable=False。手动再执行一次 run_once 能恢复并派发 1 个 child，证明恢复器存在但缺本地调度入口。

补 durable handoff/recovery intent 或准确的 eligibility，使此状态能本地恢复并有有界退避与停机原因。不能修成持有 SQLite writer 执行 git，也不能以周期性重扫代替交接状态。

原ID：B/I/6。

控制代码：[src/robert_agent/run_once.py:5491](../../../src/robert_agent/run_once.py#L5491)；[src/robert_agent/run_once.py:5797](../../../src/robert_agent/run_once.py#L5797)；[src/robert_agent/loop_engine.py:95](../../../src/robert_agent/loop_engine.py#L95)。

隔离证据：[robert-verify-agent-lifecycle-results.json](evidence/robert-verify-agent-lifecycle-results.json)。

<a id="r12"></a>
### R12 · P2 · prepared 退避期限未进入 runnable 判定

对 resume.not_before_at 在五分钟后的 prepared attempt，当前 has_runnable=True；同一数据库中真实 _queue_prepared_attempts 返回 0。意味着退避期间仍可每五秒生成 R08 的完整无进展 run。传输恢复次数本身有上限，问题是期限前的空转，不是无限派发。

探测和派发共用期限判断，并把下一个到期时间交给等待机制；容量阻塞、人工关口和退避不应被一概当成可立即执行。

原ID：B/I/7。

控制代码：[src/robert_agent/loop_engine.py:177](../../../src/robert_agent/loop_engine.py#L177)；[src/robert_agent/run_once.py:4475](../../../src/robert_agent/run_once.py#L4475)。

隔离证据：[robert-verify-local.out](evidence/robert-verify-local.out)。

<a id="r13"></a>
### R13 · P2 · 子串启发式误判意图，且部分明确实现请求多一轮分类

当前实测 debug、prefix、fixture、suffix、“分析一下为什么失败”均 infer 为 bug_fix；对 issue 会 route 到 new-pr。“实现用户登录”“支持增量同步”“add a feature”“implement pagination”则为 unclear→classification-result。small_change/add_tests 仍可由 classifier/显式输入产生，但不是当前启发式的返回值。

“进入 new-pr 路线”不等于已经实际创建 PR：执行、验证、审计和发布权限仍有后续边界。不能把这些误判描述成自动绕过发布。应优先识别被明确要求的动作、使用词边界和否定/引用语境；无法确定时继续分类。避免用更宽的关键词集合换取表面速度。103/496 秒只来自旧 snapshot 分类阶段分位数。

原ID：B/I/11, B/LATENCY-2。

控制代码：[src/robert_agent/discover.py:188](../../../src/robert_agent/discover.py#L188)；[src/robert_agent/route.py:187](../../../src/robert_agent/route.py#L187)；[docs/reliability-followup-20261024.md:40](../../reliability-followup-20261024.md#L40)。

隔离证据：[robert-verify-local.out](evidence/robert-verify-local.out)。

<a id="r14"></a>
### R14 · P2 · 外部 search 配额门槛被用于本地 publication

当前探针 core=5000/search=0，local_drain 仍得到 --skip-publish。普通评论路径使用 gh api，未执行 gh search，因此 search 不足会挡住已有受审计结果的发布；默认限流判断还缓存 300 秒。

按具体操作和所需配额控制 discovery 与 publication，继续保留真实权限、API 限流、审计和远端状态校验。

原ID：A/9。

控制代码：[src/robert_agent/daemon.py:168](../../../src/robert_agent/daemon.py#L168)；[src/robert_agent/daemon.py:581](../../../src/robert_agent/daemon.py#L581)；[src/robert_agent/publish.py:180](../../../src/robert_agent/publish.py#L180)。

隔离证据：[robert-verify-rate-gate.out](evidence/robert-verify-rate-gate.out)。

<a id="r15"></a>
### R15 · 优化 · 重复心跳永久追加到 phase 历史

对同一 attempt/phase/status 连续记录三次得到三行 worker_phases；wrapper 默认每 60 秒调用 snapshot，自动 retention 不覆盖此表。三个持续活跃 wrapper 的条件速率约 4,320 行/天，不是当前部署计数。

区分进程活性、真实阶段/命令开始结束和诊断采样：常规活性更新 attempts.heartbeat_at，保留有意义的变化与必要的有界样本。不要只按 phase 字符串去重而丢掉同阶段不同命令的开始、失败和结束证据。

原ID：B/III/2。

控制代码：[src/robert_agent/worker/snapshot.py:27](../../../src/robert_agent/worker/snapshot.py#L27)；[src/robert_agent/retention.py:9](../../../src/robert_agent/retention.py#L9)。

隔离证据：[robert-verify-local.out](evidence/robert-verify-local.out)。

<a id="r16"></a>
### R16 · 优化 · native 模型的多个线程重复更新同一个父心跳

每个模型等待线程默认每秒调用 check_active，等槽循环还可能在申请锁前后各检查一次。它们更新同一父 attempt；这会产生写竞争与日志/页面写入成本，但不会每次新增一条 worker_phases，不能与 R15 的行增长混为一谈。

可以合并父协调器心跳并降低写频率；所有权和取消检查仍需及时，不能简单把整段 check_active 降为一分钟一次。

原ID：B/III/3。

控制代码：[src/robert_agent/model_execution.py:136](../../../src/robert_agent/model_execution.py#L136)；[src/robert_agent/model_execution.py:222](../../../src/robert_agent/model_execution.py#L222)；[src/robert_agent/model_execution.py:319](../../../src/robert_agent/model_execution.py#L319)。

<a id="r17"></a>
### R17 · 优化 · 历史规模放大进度统计、模型池与部分恢复查询

每个 cycle 前后运行 17 个历史 COUNT；模型池每个 waiter 默认每秒读取 reserved_runs 和全局计数。slot_reserved 无对应索引，原始 EXPLAIN 为 SCAN review_runs；每表 1,000/100,000 历史行的合成测量为 0.101/7.179 ms。当前 worker_phases 的 attempt 查找也为 SCAN，1,000/100,000 行平均约 0.028/4.080 ms。

这不是生产慢查询归因，且部分恢复/audit 查询有 repo/status 条件，不能称所有函数无条件读取全部业务历史。优先给活动子集/attempt 时间线加匹配索引，减少无进展轮次；保留事务内 admission 和逐组死亡确认。之后用部署规模的 EXPLAIN/测量决定是否引入计数缓存或集中回收者。

原ID：A/PERFORMANCE-1, B/III/3, B/III/6。

控制代码：[src/robert_agent/loop_engine.py:52](../../../src/robert_agent/loop_engine.py#L52)；[src/robert_agent/model_pool.py:6](../../../src/robert_agent/model_pool.py#L6)；[src/robert_agent/review_store.py:181](../../../src/robert_agent/review_store.py#L181)；[src/robert_agent/run_once.py:1133](../../../src/robert_agent/run_once.py#L1133)。

隔离证据：[robert-review-lifecycle-native-cost.out](evidence/robert-review-lifecycle-native-cost.out)、[robert-verify-scheduler-storage.out](evidence/robert-verify-scheduler-storage.out)。

<a id="r18"></a>
### R18 · 优化 · 同轮完整性校验多次全仓读盘且大文件整体入内存

每个 run_phase 前后 verify_snapshot 会重新枚举并 hash 所有 tracked files，sha256_file 使用 read_bytes。来源 A 的同版本合成负载：两个 reviewer，32.977 MiB / 1,002 tracked files，一轮 12 次扫描，累计读取 395.719 MiB，hash 约 1.037 秒，全轮约 3.654 秒。源码和 review fixture 未变，允许复用；没有测出当前生产的分钟级耗时。

先改为分块 hash，保持相同 digest。只有在明确证明所有 producer 不能修改源文件的阶段边界，才考虑合并重复验证；不能直接删除完整性校验或只相信 git status。

原ID：A/PERFORMANCE-2, B/LATENCY-9, B/III/4。

控制代码：[src/robert_agent/review_workspace.py:161](../../../src/robert_agent/review_workspace.py#L161)；[src/robert_agent/review_files.py:20](../../../src/robert_agent/review_files.py#L20)；[src/robert_agent/model_execution.py:358](../../../src/robert_agent/model_execution.py#L358)。

隔离证据：[robert-review-lifecycle-native-cost.out](evidence/robert-review-lifecycle-native-cost.out)、[reused-evidence-code-hashes.json](evidence/reused-evidence-code-hashes.json)。

<a id="r19"></a>
### R19 · 优化 · round 工作区、refs、日志及证据缺少统一回收策略

新 review round 会固定 head/base refs 并建立独立 detached worktree；同 round 重试可复用已有 snapshot，Git object storage 由 worktree 共享，不是每次完整 clone。运行时代码未找到对应 round refs/worktree/artifacts/logs 的统一 GC。stdout/stderr 的显示 tail 有限，不代表原始日志文件长度有界。

这是文件系统增长，与 SQLite 文件变大是不同来源。GC 需检查活跃/未知进程、slot、待发布动作、恢复与审计引用，先归档或压缩已终态证据，再有界回收。不能只按目录年龄删除 snapshot/refs，也不能把仍用于锁等待者的稳定 inode 在线删除。

原ID：B/III/4, B/III/5。

控制代码：[src/robert_agent/review_workspace.py:108](../../../src/robert_agent/review_workspace.py#L108)；[src/robert_agent/dispatch.py:122](../../../src/robert_agent/dispatch.py#L122)；[src/robert_agent/publication_lock.py:9](../../../src/robert_agent/publication_lock.py#L9)；[src/robert_agent/result_spool.py:26](../../../src/robert_agent/result_spool.py#L26)。

<a id="r20"></a>
### R20 · 条件性 P2 · rejected spool 记录可反复耗尽扫描预算，尾部 pending 缺少公平性

replay 对所有 *.json 排序，逐条读取后才跳过 status!=pending。rejected JSON 长期留在同一目录，健康结果可能一直排在它们后面。用确定性的慢读时钟模拟超过两秒的 rejected 前缀，连续两次 replay 都未调用 writer，尾部 pending 保持不动。这验证了条件缺陷，不是生产磁盘吞吐测量。

原文“每次读完全部 JSON”过宽：glob/排序覆盖文件名全集，内容读取受两秒/100个 pending 的预算限制；成功结果会删除，损坏文件会隔离为 .invalid。将 rejected 证据移出 pending 扫描目录或维护公平、有界的 pending 入口，并保留人工可查证据。稳定 .lock 的清理仅能在确认所有 writer/waiter 停止的维护边界设计，不能在线 unlink。

原ID：B/III/5。

控制代码：[src/robert_agent/result_spool.py:112](../../../src/robert_agent/result_spool.py#L112)；[src/robert_agent/result_spool.py:146](../../../src/robert_agent/result_spool.py#L146)。

隔离证据：[robert-verify-local.out](evidence/robert-verify-local.out)。

<a id="r21"></a>
### R21 · 优化 · init_database 的热路径包含重复扫描和旧迁移入口

loop config 加载与 run_once 各调用 init_database；未版本门控的 PR mainline 多表 JOIN、route skill 回填以及旧 control import 仍会执行。调用次数和潜在历史规模成本可由源码确认，但本轮未给迁移扫描做生产规模 profiling。

“每次都覆盖当前 workstream 生命周期”不能由此直接推出：迁移 WHERE 要求 t.workstream_id!=source_key，移动成功的 task 下次被排除，其他迁移有自己的版本/checksum 门。版本门控应逐迁移设计，覆盖确有需要的兼容回填，不能一刀切关闭所有初始化修复。未复现正常生产路径的反复状态破坏。

原ID：B/I/9。

控制代码：[src/robert_agent/storage.py:80](../../../src/robert_agent/storage.py#L80)；[src/robert_agent/storage.py:162](../../../src/robert_agent/storage.py#L162)；[src/robert_agent/storage.py:532](../../../src/robert_agent/storage.py#L532)；[src/robert_agent/loop_engine.py:27](../../../src/robert_agent/loop_engine.py#L27)。

<a id="r22"></a>
### R22 · 优化 · health/通知/历史保留的成本与压缩边界需要区分

reliability_health.collect 的 docstring 称 read-only，实际会 INSERT OR IGNORE execution_delayed 通知；ID 去重避免同一 task/reason 每次追加新通知。部分 notifications JSON 聚合和投递 eligibility 查询会随历史规模增长。wakeups/audit/github_events 等不在诊断 retention 内，包含业务事实与幂等依据，不能等同无用数据。

自动 prune 不执行 VACUUM，删页进入 freelist 可以被重用；文件不缩小本身不是持续泄漏。新建标准库 SQLite 数据库默认 journal_mode=delete，当前初始化未切 WAL，因此缺 wal_checkpoint(TRUNCATE) 不能证明 WAL 膨胀。压缩应在停止/备份副本维护中做，不应每轮执行；历史报告已经提供 VACUUM INTO 副本流程。应分开监控 live bytes、free/reusable bytes、DB/WAL/文件系统证据大小。

原ID：B/III/6。

控制代码：[src/robert_agent/reliability_health.py:10](../../../src/robert_agent/reliability_health.py#L10)；[src/robert_agent/notify.py:78](../../../src/robert_agent/notify.py#L78)；[src/robert_agent/retention.py:81](../../../src/robert_agent/retention.py#L81)；[docs/reliability-followup-20261024.md:57](../../reliability-followup-20261024.md#L57)。

<a id="r23"></a>
### R23 · 策略说明 · 45 分钟是明确声明的全局上限，不是被 worker 配置意外覆盖

supervise 的 min(global, worker) 可复现：45 分钟且心跳新鲜、worker timeout=5400 时仍 failed_timeout。reference 明确把 hard_timeout_minutes 定义为独立 supervision wall-time limit，并说明只增加 worker timeout 不提高全局上限；多模型文档明确等槽仍算父总时限。

因此 B/I/3 的行为观察成立，“以 attempt 自己的 timeout 为准”不是等价修复，而是策略变更。可以显示 effective timeout、协调默认值或增加配置诊断。历史 new-pr P95=5015.754 秒不证明今天仍会大量重做；恢复受证据/配额约束且可以保留工作区，不能简单把旧分位数与当前2700秒规则相减作损失估计。

原ID：B/I/3, B/LATENCY-3。

控制代码：[src/robert_agent/supervise.py:34](../../../src/robert_agent/supervise.py#L34)；[docs/reference.md:197](../../reference.md#L197)；[docs/multi-model-review.md:54](../../multi-model-review.md#L54)。

隔离证据：[robert-verify-agent-lifecycle-results.json](evidence/robert-verify-agent-lifecycle-results.json)。

<a id="r24"></a>
### R24 · 策略说明 · 未知后代和 attention 保留容量是安全边界，恢复体验可以改进

leader dead 且其 PGID 仍有成员时，_dispatch_process_status 返回 unknown，supervise 把 binding 置 attention；随后 runnable_sql 和 active claim 阻止普通继续推进。不能称为每轮无限普通 supervise 重写。同一未知执行仍保留容量，后续任务确会受阻，但这避免了错误重入和未经证明的资源释放。

model_execution._group_finished 的 killpg(pgid,0) 仅是存在性探测，不是可以对未知 PGID 发终止信号的依据。已有三个真实进程终止测试通过，明确要求拒绝 unverified orphans。改善后代的可验证身份记录、停止确认、人工 inspection/abandonment 和明确恢复入口；不能仅凭 pgid==旧pid 就杀组，也不能到时改 failed 并释放仍未知的容量。

原ID：B/I/5, B/LATENCY-6。

控制代码：[src/robert_agent/run_once.py:1410](../../../src/robert_agent/run_once.py#L1410)；[src/robert_agent/run_once.py:937](../../../src/robert_agent/run_once.py#L937)；[src/robert_agent/worker_identity.py:325](../../../src/robert_agent/worker_identity.py#L325)；[src/robert_agent/execution_store.py:297](../../../src/robert_agent/execution_store.py#L297)。

隔离证据：[robert-verify-agent-process-termination.out](evidence/robert-verify-agent-process-termination.out)。

<a id="r25"></a>
### R25 · 策略说明 · 外部轮询和串行远端采集是延迟来源，实际占比尚未测量

默认外部 poll 为300秒、容量满时600秒；若事件均匀随机到达，平均等待才约为150/300秒，这不是实测平均数。已有 worker result 会通过 durable wakeup/本地通知推进，不必等待 GitHub poll，不能把所有内部阶段也加上一轮外部周期。

live poll 对通知/source/PR/CI 串行采集，daemon 同步等待 child 返回，期间本地 listener 不能并发执行 drain，慢批次会增加已完成结果的推进延迟并放大 R06。单轮是否接近300秒需要仓库数、open PR 数和API延迟的部署数据。先做单轮预算、批次与热点测量，再评估有界并发或增量读取；权限/远端状态校验仍要保留。

原ID：B/LATENCY-1, B/LATENCY-7。

控制代码：[src/robert_agent/validate_config.py:246](../../../src/robert_agent/validate_config.py#L246)；[src/robert_agent/daemon.py:573](../../../src/robert_agent/daemon.py#L573)；[src/robert_agent/discover.py:739](../../../src/robert_agent/discover.py#L739)；[src/robert_agent/run_once.py:2050](../../../src/robert_agent/run_once.py#L2050)。

<a id="r26"></a>
### R26 · 策略说明 · workstream 顺序推进和 review 三阶段是现有契约

同一 workstream 只允许一个活跃 task，保证同一源上的修改/跟进顺序及所有权。review 的 prepare→并行review→synthesis分别承担上下文准备、独立审查、统一审核报告。两者都消耗墙钟时间，但不能因“串行”直接归类为 bug 或没做工作。

可以分别测量排队/等槽/模型执行/完整性验证/发布阶段的时间，优化重复准备和 R18 的读盘成本；放宽同源并发或删阶段需要明确产品及正确性契约变更。

原ID：B/LATENCY-8, B/LATENCY-9。

控制代码：[src/robert_agent/resources/db/schema.sql:107](../../../src/robert_agent/resources/db/schema.sql#L107)；[src/robert_agent/review_coordinator.py:164](../../../src/robert_agent/review_coordinator.py#L164)；[docs/multi-model-review.md:48](../../multi-model-review.md#L48)。

<a id="n01"></a>
### N01 · 拒绝 · “死进程必须等20/45分钟才被发现”不成立

当前 supervisor 在每轮对 running/stale 做进程身份检查，dead 和 unknown 有独立路径；真正死去的进程不必等待心跳变 stale。20/45 分钟针对心跳陈旧或总运行时间，不能与进程死亡检查混淆。native coordinator 还会每秒更新父 heartbeat，所以“心跳只有snapshot和wrapper两种来源”也不完整。

长时间仍活着但没有可观察进展是否需要更细的 idle policy，可以另行定义；不能用误述的死亡检测窗口证明该需求。

原ID：B/LATENCY-5。

控制代码：[src/robert_agent/run_once.py:936](../../../src/robert_agent/run_once.py#L936)；[src/robert_agent/run_once.py:963](../../../src/robert_agent/run_once.py#L963)；[src/robert_agent/model_execution.py:136](../../../src/robert_agent/model_execution.py#L136)。

<a id="u01"></a>
### U01 · 待补证 · pending fact 的无限空转风险尚缺常规生产触发证据

execution_pending_facts 没有通用 TTL 或 retry_count；replay 中存在不消费的分支。若合法持久化事实长期无法产生域效应而仍被探测为 runnable，理论上可引发空转。

但正常 offline migration 只为存在且归属匹配的 result/event 建映射，不能把手工插入“缺失result”的记录当成正常生产证据。本轮普通 waiting_for_user context 探针被实际消费成功，不能支持“凡追加失败都会永久空转”的泛化。未复现经过正常迁移/生命周期转换可达且长期不消费的输入，故保留原ID与条件风险，暂不进入修复承诺。

后续需要正常迁移后的具体 fact、binding、domain 状态和多轮不消费轨迹。即便证明问题，也不能用 TTL 静默丢事实；可行方向是识别阻塞原因、停止调度并保留可恢复证据。

原ID：B/I/10。

控制代码：[src/robert_agent/execution_replay.py:12](../../../src/robert_agent/execution_replay.py#L12)；[src/robert_agent/sqlite_migration.py:265](../../../src/robert_agent/sqlite_migration.py#L265)；[src/robert_agent/loop_engine.py:133](../../../src/robert_agent/loop_engine.py#L133)。

隔离证据：[robert-verify-local.out](evidence/robert-verify-local.out)。

<a id="t01"></a>
### T01 · P2 测试 · vote recovery 测试只确认一个 unknown launch，存在竞态

两个 voter 可以同时进入 starting 且没有 dispatch identity，测试却用 next(...) 只选一个 run_id 作为确认。未被确认的另一个未知启动仍存在，cancel 正确抛 WorkItemConflictError。当前版本独立重复10次为9 pass/1 error；同一版本全量1069项的另一次运行通过，两者并不矛盾。

若测试只验证单个未知启动，应固定单个 worker；若覆盖多个并行启动，应确认全部未知 run，并明确验证漏确认会被拒绝。不要放松生产取消校验来“修好”测试。

原ID：A/VALIDATION-1。

控制代码：[tests/test_vote_recovery.py:64](../../../tests/test_vote_recovery.py#L64)；[tests/test_review_recovery.py:25](../../../tests/test_review_recovery.py#L25)。

隔离证据：[robert-verify-vote-flake.out](evidence/robert-verify-vote-flake.out)、[robert-verify-764c5e3-unittest.log](evidence/robert-verify-764c5e3-unittest.log)。

## 6. 完整原始 finding ledger

每个输入ID恰好一行。原claim与原建议分别保存在来源报告和`findings.json`的`sources`中；表中只保留可证伪摘要与证据位置。所有行的`Resolution=open`表示本轮未实施修复；`Reject`不要求修复，不意味着它等待后续改代码。

| Finding ID | 原严重度 | 可证伪摘要 | Verdict | Introduced | Scope | Evidence | Disposition | Resolution |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A/1 | P1 | 远端关闭会取消父任务而留下独立模型进程。 | Correct | Not applicable | in scope | [R01](#r01) | Follow-up | open |
| A/2 | P1 | 结果提交将活着的worker移出容量统计与supervision。 | Correct | Not applicable | in scope | [R02](#r02) | Follow-up | open |
| A/3 | P1 | PR健康采集在第二个远端请求期间仍持SQLite写锁。 | Correct | Not applicable | in scope | [R03](#r03) | Follow-up | open |
| A/4 | P2 | stale但存活的worker未计入实际派发容量。 | Correct | Not applicable | in scope | [R05](#r05) | Follow-up | open |
| A/5 | P2 | 默认心跳等待可能把命令退出检测延迟近60秒。 | Correct | Not applicable | in scope | [R07](#r07) | Follow-up | open |
| A/6 | P2 | 无进展supervision仍产生完整run和步骤记录。 | Correct | Not applicable | in scope | [R08](#r08) | Follow-up | open |
| A/7 | P2 | 持续活跃时默认daemon_events清理吞吐可落后于生成。 | Correct | Not applicable | in scope | [R09](#r09) | Follow-up | open |
| A/8 | P2 | 孤立queued vote不被本地调度探测发现。 | Correct | Not applicable | in scope | [R10](#r10) | Follow-up | open |
| A/9 | P2 | Search配额不足会禁止无需search的本地发布。 | Correct | Not applicable | in scope | [R14](#r14) | Follow-up | open |
| A/PERFORMANCE-1 | Not supplied | 模型池活动槽位查询随全部历史run增长。 | Correct | Not applicable | in scope | [R17](#r17) | Follow-up | open |
| A/PERFORMANCE-2 | Not supplied | 同轮各阶段重复全仓hash且单文件整体载入内存。 | Correct | Not applicable | in scope | [R18](#r18) | Follow-up | open |
| A/VALIDATION-1 | Not supplied | 未知vote启动的恢复测试在两个starting并存时会竞态失败。 | Correct | Not applicable | in scope | [T01](#t01) | Follow-up | open |
| B/I/1 | 高 | 内外同预算可强杀子执行器并遗留run和lease。 | Partially correct | Not applicable | in scope | [R06](#r06) | Follow-up | open |
| B/I/2 | 高 | stale worker被两套容量统计以不同方式处理。 | Correct | Not applicable | in scope | [R05](#r05) | Follow-up | open |
| B/I/3 | 高，语义冲突 | 全局45分钟会限制更长的worker配置。 | Partially correct | Not applicable | in scope | [R23](#r23) | Follow-up | open |
| B/I/4 | 中高 | 超过10秒的writer竞争可使整轮review失败并终止同轮模型。 | Correct | Not applicable | in scope | [R04](#r04), [R03](#r03) | Follow-up | open |
| B/I/5 | 中 | 无法验证的后代存活会保留容量并进入attention。 | Partially correct | Not applicable | in scope | [R24](#r24) | Follow-up | open |
| B/I/6 | 中 | classification完成提交后Git失败会留下调度遗漏的恢复状态。 | Correct | Not applicable | in scope | [R11](#r11) | Follow-up | open |
| B/I/7 | 中 | prepared退避时间未进入runnable探测条件。 | Correct | Not applicable | in scope | [R12](#r12) | Follow-up | open |
| B/I/8 | 中 | 若干supervise/audit/reconcile外部I/O跨SQLite写事务。 | Partially correct | Not applicable | in scope | [R03](#r03) | Follow-up | open |
| B/I/9 | 中，疑似风险 | init_database重复执行未版本门控的扫描和迁移入口。 | Partially correct | Not applicable | in scope | [R21](#r21) | Follow-up | open |
| B/I/10 | 低，迁移遗留的边缘情况 | 无法消费的pending fact缺少通用停止预算。 | Partially correct | Not applicable | in scope | [U01](#u01) | Need evidence | open |
| B/I/11 | 低到中 | 子串启发式会误路由部分非修复请求并增加分类阶段。 | Partially correct | Not applicable | in scope | [R13](#r13) | Follow-up | open |
| B/LATENCY-1 | Not supplied | 外部事件发现受300/600秒轮询周期限制。 | Partially correct | Not applicable | in scope | [R25](#r25) | Follow-up | open |
| B/LATENCY-2 | Not supplied | unclear请求额外执行classification worker。 | Partially correct | Not applicable | in scope | [R13](#r13) | Follow-up | open |
| B/LATENCY-3 | Not supplied | 全局超时可能触发恢复并延长处理。 | Partially correct | Not applicable | in scope | [R23](#r23) | Follow-up | open |
| B/LATENCY-4 | Not supplied | 被强杀执行器会留下repository lease等待。 | Partially correct | Not applicable | in scope | [R06](#r06) | Follow-up | open |
| B/LATENCY-5 | Not supplied | 死进程必须等20分钟stale或45分钟timeout才被发现。 | Incorrect | Not applicable | in scope | [N01](#n01) | Reject | open |
| B/LATENCY-6 | Not supplied | attention进程所有权保留可能阻塞后续容量。 | Partially correct | Not applicable | in scope | [R24](#r24) | Follow-up | open |
| B/LATENCY-7 | Not supplied | live poll串行读取source与PR/CI，长批次会阻塞本地推进。 | Correct | Not applicable | in scope | [R25](#r25), [R06](#r06), [R03](#r03) | Follow-up | open |
| B/LATENCY-8 | Not supplied | 同一workstream只推进一个活跃task。 | Correct | Not applicable | in scope | [R26](#r26) | Reject | open |
| B/LATENCY-9 | Not supplied | 多模型review存在prepare、并行review、synthesis三阶段。 | Correct | Not applicable | in scope | [R26](#r26), [R18](#r18) | Follow-up | open |
| B/III/1 | 最严重 | 用完整流水线监督运行进程制造大量无进展记录。 | Partially correct | Not applicable | in scope | [R08](#r08), [R09](#r09) | Follow-up | open |
| B/III/2 | Not supplied | 长命令每次心跳追加worker_phases且不在自动retention内。 | Correct | Not applicable | in scope | [R15](#r15) | Follow-up | open |
| B/III/3 | Not supplied | native模型等待和等槽周期反复更新父attempt心跳。 | Correct | Not applicable | in scope | [R16](#r16), [R17](#r17) | Follow-up | open |
| B/III/4 | Not supplied | 不同review round保留worktree/refs并反复全仓hash。 | Partially correct | Not applicable | in scope | [R18](#r18), [R19](#r19) | Follow-up | open |
| B/III/5 | Not supplied | 若干证据文件缺少GC，rejected spool能挤占扫描预算。 | Partially correct | Not applicable | in scope | [R19](#r19), [R20](#r20) | Follow-up | open |
| B/III/6 | Not supplied | 若干历史查询与诊断写入持续存在，删除不自动缩小文件。 | Partially correct | Not applicable | in scope | [R17](#r17), [R22](#r22), [R09](#r09) | Follow-up | open |

### 各原ID的判定理由

- **A/1 — Correct**：当前版本实进程复现：父退出-9，模型仍活着，run仍running+slot_reserved1；模型退出后可由后续申请回收槽位，不能写成无条件永久占槽。
- **A/2 — Correct**：当前版本实际record_result后attempt=completed，进程仍活着，supervision数量0。
- **A/3 — Correct**：当前版本双PR探针中第二次请求期间独立writer返回database is locked。
- **A/4 — Correct**：当前版本实进程复现count和global_count归零；daemon_state另有包含stale的口径。
- **A/5 — Correct**：heartbeat.py相对来源A版本逐字节一致；复用原始0.078/60.025秒隔离测量，检查当前控制流仍相同。
- **A/6 — Correct**：当前版本10次drain均no_progress，却新增230条run/step/lease记录。每日数字是默认间隔下的容量估算，不是生产观测。
- **A/7 — Correct**：单事件批100/300秒=28800每天；2事件/5秒=34560每天，包含运行耗时、锁和保留年龄的限制。
- **A/8 — Correct**：当前版本相同状态queued vote=False、queued review=True。
- **A/9 — Correct**：当前版本core5000/search0仍向本地drain传--skip-publish。
- **A/PERFORMANCE-1 — Correct**：关键源文件和测量fixture不变；原始EXPLAIN为SCAN，1k/100k历史测量0.101/7.179ms是合成负载。
- **A/PERFORMANCE-2 — Correct**：关键源文件和review fixture不变；复用33MiB×12次的合成测量，非生产耗时。
- **A/VALIDATION-1 — Correct**：当前版本重复10次为9pass/1error；全量1069项在另一轮通过不否定该竞态。
- **B/I/1 — Partially correct**：强杀残留已实证；170秒起步并非必杀，等待是剩余TTL，health指标不证明线上发生。
- **B/I/2 — Correct**：与A/4同一机制，保留两条原ID。
- **B/I/3 — Partially correct**：行为真实且明确写入reference与多模型文档；不是实现违背配置。历史P95不能证明当前部署反复重做，改优先级属于策略变更。
- **B/I/4 — Correct**：真实11秒writer竞争已复现；不能以跳过全部check_active修复，否则丢失取消与ownership fencing。
- **B/I/5 — Partially correct**：当前首先返回unknown并进入attention，之后被runnable fence排除；不是无限普通supervise。只凭旧PGID杀组不安全。
- **B/I/6 — Correct**：实证父completed、child0、wakeup consumed、runnable=False；手动run_once可恢复。
- **B/I/7 — Correct**：未来5分钟的prepared在has_runnable=True，但真实_queue_prepared_attempts=0。
- **B/I/8 — Partially correct**：PR第二轮持锁已复现，supervise/audit也有持锁窗口；“全程一个事务/全部gh持锁”过度概括，源码已有多个显式commit，首轮远端读取可无锁。
- **B/I/9 — Partially correct**：重复入口可证；已迁移task移到source_key后被WHERE排除，未复现运行中反复破坏lifecycle。
- **B/I/10 — Partially correct**：表确无TTL/重试上限；但正常迁移只映射存在的result/event，缺result分支不能作为正常生产触发。普通等待中的context探针实际被成功消费；缺少合法迁移后持续空转的复现。
- **B/I/11 — Partially correct**：当前实测debug/prefix/fixture/suffix和“分析一下为什么失败”→bug_fix/new-pr；实现/支持/add/implement→unclear。进入route不等于自动获得发布权限；旧P50/P95不是当前测量。
- **B/LATENCY-1 — Partially correct**：默认周期可证；2.5/5分钟平均值只在到达近似均匀等假设下成立，未测生产事件分布。
- **B/LATENCY-2 — Partially correct**：路径真实；大量请求比例和当前分类耗时未测，历史103/496秒只属旧snapshot。
- **B/LATENCY-3 — Partially correct**：全局cap真实，恢复有配额和证据门槛，也可复用worktree；不能称所有长任务都会被杀并大量重做。
- **B/LATENCY-4 — Partially correct**：等待余下的540秒TTL，非从SIGKILL重新计满9分钟。
- **B/LATENCY-5 — Incorrect**：supervisor每次对running/stale检查进程身份，dead/unknown有独立分支；native coordinator另有每秒heartbeat更新。活着但无心跳与已死是不同状态。
- **B/LATENCY-6 — Partially correct**：仍未证明停止的进程确会占容量，这是防重入/未知进程的安全边界，不能用到时释放作为通用修复。
- **B/LATENCY-7 — Correct**：顺序调用和父进程同步等待可证；是否实际接近300秒取决于部署规模及API延迟，未量化生产。
- **B/LATENCY-8 — Correct**：这是现有所有权与顺序契约，本条按行为保留，作为bug或无效工作应拒绝。
- **B/LATENCY-9 — Correct**：这是审查上下文/独立结果/统一报告契约；阶段成本真实，不能直接删阶段。
- **B/III/1 — Partially correct**：主机制及10轮230条已证；默认retention的100是run候选数而非总行数，事件另有100批次。固定8万run/解释2.05GB均未获当前表级数据支持，现有summary引用也避免了部分重复。
- **B/III/2 — Correct**：当前对同一attempt/phase/status调用3次得到3行；4300/天只对应3个持续活跃wrapper的条件负载。
- **B/III/3 — Correct**：默认1秒循环以及多线程重复写同一attempt可证；这些是UPDATE，不应当作新增worker_phases或DB行数增长。
- **B/III/4 — Partially correct**：每个新round单独checkout真实；同round重试复用且Git objects共享。无runtime GC可证，但分钟级生产开销未测；不得删除完整性检查。
- **B/III/5 — Partially correct**：rejected文件和稳定lock保留真实；成功spool会unlink，读取受2秒/100项边界而非每次读完全部。模拟慢读可使尾部pending连续不处理，尚无生产规模测量；在线删lock会破坏互斥。
- **B/III/6 — Partially correct**：health会去重INSERT通知，JSON聚合及历史COUNT成本真实；部分业务查询有repo/status过滤，daemon摘要已收敛，freelist可复用。默认journal_mode为delete，不能把无wal_checkpoint当WAL泄漏；业务历史/幂等记录不能一并按年龄删。

## 7. 验证记录与可复用边界

运行时：仓库已有 `.venv`，Python 3.14.7 / PyYAML 6.0.3。本轮所有行为探针导入固定 `764c5e3` 快照，临时DB和本地进程不连接生产；真实进程由探针清理。

| 验证 | 结果 |
| --- | --- |
| `python3 -B -m unittest discover -s tests`，固定新快照 | 1,069 tests，178.315秒，OK，skipped=1；存在ResourceWarning，未将其当作通过之外的额外保证 |
| 真实进程生命周期/容量 | 远端关闭残留模型、提交结果后失去监管、stale容量归零均重跑确认 |
| SQLite事务与恢复 | 双PR写锁、11秒writer导致整轮失败、强杀run/lease残留、classification交接缺口已复现 |
| 调度与意图 | future prepared runnable但queue0；queued vote漏检；search-only门槛阻止publish；子串误路由均确认 |
| 同步heartbeat/历史 | 同phase三次产生三行；模型池与phase查询计划、历史规模合成测量见证据 |
| rejected spool公平性 | 确定性慢读时钟，两轮尾部pending均未处理；不是实际磁盘吞吐测试 |
| targeted vote recovery重复10次 | 9 pass / 1 error；失败是未确认全部unknown launch的fixture竞态 |
| 进程终止安全测试 | 3项实进程测试通过，保留未知身份拒绝行为 |
| 旧测量复用 | heartbeat退出等待与33MiB全仓hash测量所涉源码/fixture相同，原始版本与hash记录在证据中 |
| 文档交付 | 检查38个ID唯一、源顺序、合同字段枚举、29主题引用、源码行号、证据路径/sha和diff空白；未执行代码修复、build或外部发布 |

主要命令模板（精确执行脚本以`evidence/*.py.txt`保存为不可执行证据，原始临时路径见scope）：

```bash
cd <TEMP>/robert-verify-764c5e3-d_pwg86h
source ./.venv/bin/activate
PYTHONPATH=<TEMP>/robert-verify-764c5e3-d_pwg86h/src python3 -B -m unittest discover -s tests
PYTHONPATH=<TEMP>/robert-verify-764c5e3-d_pwg86h/src:<TEMP>/robert-verify-764c5e3-d_pwg86h python3 -B <TEMP>/robert-verify-agent-lifecycle.py
```

历史报告的169 tasks、614 attempts、约2.05GB和分类/new-pr P50/P95，只是[仓库历史报告](../../../docs/reliability-followup-20261024.md)中明确标为pre-removal snapshot的记录。本轮未拿到该DB做表级占比和因果分析，未将其提升为当前生产事实。

## 8. 后续修复交接

本请求仅核验与合并，停在报告。可由`fast-review-remediation`直接消费`findings.json`的同一checked head；若源码、配置、diff或运行时改变，先刷新受影响条目，不重跑无关分析。

建议按以下独立批次推进，每项bug先建立失败行为测试；以下为修复顺序建议，不构成已获修复或发布授权：

1. 生命周期：R01/R02/R03/R04/R05/R06；保留进程身份、未知结果、取消、审计、幂等和信任边界。
2. 调度与耗时：R07/R08/R10/R11/R12/R13/R14；以本地推进延迟、无进展run率、错误路由率验证。
3. 存储与查询：R09/R15/R16/R17/R18/R19/R20/R21/R22；先测table/file分布、eligible积压与查询计划，再设计保留和维护。
4. T01单独修测试；R23/R24/R25/R26保留策略与测量需求；N01拒绝；U01先补正常迁移路径证据。

回滚与兼容重点：不把未确认停止的owner释放，不重启未知dispatch，不删除未消费事实或去重凭据，不使snapshot producer获得可写源，不把远端I/O放回长事务。
