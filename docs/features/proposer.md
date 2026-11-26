# Proposer

Proposer 把近期 GitHub Issue 和 TAPD 需求、缺陷聚合到 `/proposals`。页面展示处理范围、可行性、优先级、推荐理由、代码证据和最近分析时间。操作员可以先调查、正式处理、延期或放弃，在同一处决定下一项工作。

## 配置每日推荐

在 v2 配置里启用独立调度。以下片段复用已有仓库、TAPD 项目、Worker 和操作员消息配置。

```yaml
proposer:
  enabled: true
  publish_comments: true
  github_all_registered: true
  github_in_progress_labels: ["in progress", "in-progress"]
  tapd_projects: ["tapd.example.com/20001234"]
  tapd_assignees: ["kunliangwu"]
  tapd_todo_states:
    story: ["status_1"]
    bug: ["new"]
  tapd_started_states:
    story: "status_2"
    bug: "in_progress"
  gateway_metadata: /path/to/local-messages-gateway.json
  schedule:
    enabled: true
    analysis_time: "08:30"
    digest_time: "09:30"
    analysis_limit: 20
    worker: coder
    web_url: https://robert.example.internal
```

`tapd_projects` 引用一个已配置的网关 TAPD 项目，值由项目 host 和 workspace ID 组成。需求和缺陷各自配置待办、进行中的真实工作流状态 ID。待办状态可以有多个，进行中状态必须与待办状态不同。仓库标签映射仍由 `tapd.projects[].label_rules` 维护，详见 [TAPD 来源](tapd.md)。

`schedule.worker` 引用已配置的 Worker，其 `model` 应是本地 Messages gateway 可接受的模型名。Proposer 通过该 gateway 进行无工具分析，提交经过脱敏的单据、讨论和固定到仓库默认分支 commit 的代码片段。Worker 的模型配置决定分析模型。gateway metadata 文件由服务用户拥有，并且只有文件所有者可写。来源内容需要允许发送到配置的 gateway。

`schedule.web_url` 是操作员实际可访问的 Robert Web origin，可以使用内网域名和端口。它只包含协议、主机和可选端口。摘要附带 `/proposals` 入口。启用 Web 写操作后，页面上的决定遵循现有认证、仓库权限和 CSRF 检查，见 [Web 认证与远程部署](web-authentication.md)。

每日摘要使用现有的 [操作员消息](operator-messaging.md) 配置。选择私有操作员会话，并把允许查看的仓库加入 `messaging.repos`。摘要只包含当前消息范围可见的提案。无确定仓库的候选要求操作员具备其全部候选仓库的查看权限。

## 运行独立定时进程

保留正常 daemon 和发布流程，另行启动常驻进程。

```bash
robert cron --config /path/to/config.yml
```

调度时间使用 `Asia/Shanghai`。默认每天 08:30 开始分析，09:30 入队一条摘要。分析运行在独立子进程中，09:30 的摘要可以注明分析仍在进行，后续结果继续更新到 Web。进程重启时只补当前日期的工作。重复启动受进程锁保护，摘要按日期持久去重。部分失败的分析最多进行三次日内尝试，尝试之间至少间隔五分钟。

执行当前已到时间的工作并等待本次分析完成，可以使用下面的命令。

```bash
robert cron --config /path/to/config.yml --once
```

启用 `schedule` 后，daemon 的内嵌 Proposer 扫描由独立 cron 接管。daemon 继续受理 Web 决策、启动执行和发布通知。cron 负责入队摘要，外部发送沿用 daemon 的消息发布及重试机制。

## 来源和每日分析

GitHub 默认扫描已注册仓库中最近七天创建的 open Issue。明确带有进行中标签、关联开放 PR 或已被 Robert 正式受理的工作退出候选范围。仅有负责人仍可作为待办分析。关联 PR 读取失败时，本次来源显示部分刷新。

TAPD 扫描配置的单个项目内最近七天创建、处于待办状态的需求和缺陷。负责人需要在白名单内，或有明确的未分配元数据。部分 TAPD CLI 版本省略空负责人字段，此时 Robert 无法区分未分配与字段不可见，会跳过负责人未知的单据。

无仓库标签的 TAPD 单据也进入提案。Robert 从已配置标签映射中建议仓库，并提供理由。操作员在 Web 上选择仓库后，可以等待针对该仓库的证据分析。现有标签存在冲突时，提案保留待复核原因，需要先处理冲突。

最近七天是发现窗口。已经收录的待办持续保留，日常分析兼顾排名靠前、新收录、延期到期和久未分析的提案。来源关闭、归档或已经被接手时，提案记录其当前原因。分析失败会保留上次报告和分析时间，方便判断内容是否已刷新。

来源读取和当前状态刷新位于 `work/application/proposal_sources.py`，提供 `discover` 和 `refresh`。来源观察包含稳定身份、原文 URL、正文摘要、版本指纹和可选仓库映射。新的来源适配器通过这组事实进入分析和审批流程，并为拆分候选保留各自稳定身份及原文位置。GitHub 和 TAPD 单据沿用原始单据身份。

## 在 Web 决策

- **先调查**创建调查任务，结论评论到原单。调查期间提案显示调查中，调查结束后仍可进入待决策。原单保持待办。正式处理时重新读取原单和讨论，包括已发布的调查结论。
- **处理**批准当前提案版本。Robert 重新检查单据、讨论、代码证据、当前权限和正在执行的工作，再通过普通任务队列受理。成功后提案从待决策列表移到看板，历史及关联关系保留。
- **延期 N 天**记录恢复日期，到期后重新参与推荐。操作员也可以提前恢复。
- **放弃**保留历史，只有手动恢复后才再次进入推荐。

调查和正式执行的审批、任务身份各自保存。重复点击通过版本和幂等键合并，来源变化会要求重新复核。调查结论写回失败时，可以在提案和任务详情查看发布结果。

## 原单同步

`publish_comments: true` 启用原单回评。正式任务受理时评论说明等待执行，首次确认正式执行 Worker 启动后评论说明已经开工。TAPD 此时从配置的待办状态推进到进行中。后续澄清、失败、完成和 PR 里程碑沿用发布回执。调查只补充单据内容。

正式受理无标签 TAPD 单据时，Robert 会补写操作员确认的仓库标签。标签更新读取当前标签并合并，再写入和读回确认。状态同步也先检查当前状态和归属，跳过已推进或已转交的单据。未知写入结果先读回核对，再决定是否重试。页面分别展示执行与回写状态。

TAPD CLI 的标签和状态更新缺少原子条件写接口，检查与写入之间仍存在并发修改窗口。读回可以发现部分冲突，但不能证明这段窗口内的人工修改均被保留。需要严格条件更新的部署，应先扩展适配器对应的上游能力。

## 兼容内嵌扫描

现有配置可以继续使用 `github_repositories` 和 `github_assignees` 指定扫描范围，并通过 `model` 指定 gateway 模型。`schedule.enabled` 默认关闭，原有 daemon 扫描在上海时间 09:00 后的空闲机会运行，每轮最多评估六项。启用独立每日推荐时，使用前面的完整调度配置。
