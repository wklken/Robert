# 来源 A：上一轮聊天报告

原始审查版本：1b23d3b635d3b319e02f3f73029ff5676f30eb26。以下保留原编号、原严重度、主张和原建议；内容摘自本聊天上一份报告，不把新的核验结论写回原主张。PERFORMANCE、VALIDATION 为本轮按原文分组补充的 synthetic IDs。

<a id="1"></a>
## 1 [P1] 远端关闭任务，可能留下失去监管的模型进程。

关闭路径持有 SQLite 写锁，再等待顶层 coordinator 退出。Coordinator 收到 TERM 后先尝试更新数据库，被该锁阻塞，随后被强杀；独立进程组中的模型没有被收拢。实际复现：约 2 秒后 task、attempt 已是 canceled，coordinator 退出码为 -9，模型仍存活，模型槽位仍被占用。模型卡死时，可以长期消耗资源并阻塞后续轮次。

原建议：应在短事务之外显式停止并确认所有模型进程组，再关闭所有权。

<a id="2"></a>
## 2 [P1] 收到结果就认为进程完成，过早释放容量和监管责任。

record_result 立即将 attempt 标为 completed。后续 supervisor 不再扫描它，但此时 worker 或后台命令可能仍在运行。实际复现：合法结果提交成功后，进程仍存活，并发计数已经归零，下一次 supervision 处理数量为零。这会允许后继任务启动，同时旧进程继续运行，甚至在结果审计后继续改变工作区。

原建议：应区分“结果已收到”和“进程已停止”。

<a id="3"></a>
## 3 [P1] PR 健康采集会跨远端请求持有 SQLite 写锁。

_collect_pr_remediation_decisions 处理第一个 PR 时写入 episode，随后读取第二个 PR 的远端状态，期间没有释放写事务。双 PR 隔离复现中，第一个 API 调用期间其他 writer 可以写入；第二个调用期间，其他 writer 得到 database is locked。慢请求会阻塞心跳、结果入库和其他控制操作，进一步触发失败或恢复。

原建议：应拆成“读取远端事实—短事务保存”的批次。

<a id="4"></a>
## 4 [P2] 活着的 stale worker 不占派发容量，会突破并发上限。

stale 只代表心跳过期，supervisor 明确不终止进程；但 run_once 的 admission 只统计 running。真实进程复现中，变为 stale 后仍活着，仓库和全局计数都从 1 变成 0。daemon_state 却把 stale 算入容量，两处机制不一致。已有测试还明确固化了“不占容量”的行为。

原建议：需要修改机制及对应测试。

<a id="5"></a>
## 5 [P2] 心跳包装器会给命令结束额外增加接近 60 秒的等待。

它通过 sleep(heartbeat_interval) 等待，而没有等待进程退出事件。实际测量：裸命令 0.078 秒，默认包装后 60.025 秒。每个被包装的串行命令都可能累积这段等待。

原建议：应使用带 timeout 的进程等待，到心跳期限仍未结束时才写心跳。

<a id="6"></a>
## 6 [P2] 正常运行的 worker 会每 5 秒触发完整流水线，即使没有任何进展。

调度器把“存在运行中的 attempt”直接视为 runnable，然后启动完整 run_once。隔离复现连续 10 次 drain，全部返回 no_progress，任务和 attempt 没有推进，却新增 agent_runs=10、run_steps=130、run_repo_steps=80、leases=10。单仓库持续活跃、单轮足够快时，按默认间隔估算，诊断记录可达到约 40 万条/天，另有 daemon 事件。

原建议：应把轻量进程监管与完整业务 drain 分开，并合并无变化记录。

<a id="7"></a>
## 7 [P2] 默认清理吞吐可能低于上述事件生成速率。

每次 drain 写开始、结束两条 daemon 事件；每 5 秒一轮对应 34,560 条/天。默认 maintenance 每 300 秒只清理一个 100 条批次，上限为 28,800 条/天，还未扣除实际运行和锁等待。因此存在持续追不上的负载区间，“保留 7 天”不能保证诊断历史有界。

原建议：应先减少无变化事件，再允许 maintenance 在时间预算内追赶多个批次。

<a id="8"></a>
## 8 [P2] 本地 runnable 探测遗漏 queued vote，影响重启恢复。

探测包含 review_sessions，没有对应的 vote_sessions。隔离数据库中，只有 queued vote 时返回 False，换成 queued review 则返回 True。当崩溃恢复后只剩排队 vote，它可能要等下一次外部 poll，默认额外等待数分钟；限流时更久。

原建议：应让调度探测和实际队列准备使用同一套 eligibility 判断。

<a id="9"></a>
## 9 [P2] Search 配额不足会连带阻止已有结果发布。

本地 drain 复用了外部 discovery 的限流判断。复现中 core.remaining=5000、search.remaining=0，仍传入 --skip-publish。普通评论发布路径没有执行 search，却被一起暂停；默认判断缓存还会延长等待。

原建议：应按实际操作需要的配额分别控制 discovery 和 publication。

<a id="performance-1"></a>
## PERFORMANCE-1 模型池每个等待者每秒扫描历史 run，缺少 slot_reserved 索引。

每表历史从 1,000 增至 100,000 行，查询组合从 0.101 ms 增至 7.179 ms。

原建议：增加活动槽位的 partial index，保留事务内 admission。

<a id="performance-2"></a>
## PERFORMANCE-2 同一 review snapshot 被反复全仓 hash。

默认两个 reviewer，约 33 MiB 仓库扫描 12 次，累计读取约 396 MiB。

原建议：先改为分块 hash；合并验证须保留完整性保证。

<a id="validation-1"></a>
## VALIDATION-1 vote recovery 测试存在竞态。

固定快照的全量测试为 1,055 项，跳过 1 项，其余通过；未知启动的恢复测试重复 10 次有 1 次失败，原因是测试只确认了多个未知启动中的一个，取消逻辑正确拒绝了剩余未知进程。

原建议：未单列修复建议。
