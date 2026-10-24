# Web 任务生命周期看板

首页 `/` 和 `/board` 提供任务看板；原 GitHub 工作列表保留在 `/work`，
运行与后台服务信息在 `/operations`。Web 默认只读；已有写入开关、仓库权限、
Origin、CSRF、版本和幂等校验继续生效。

## 日常操作

卡片只显示标题、仓库、当前状态、Agent 和最新更新时间。打开卡片后，先查看当前状态、
执行阶段、心跳、错误原因和可执行操作。需求描述、技术标识、执行历史和事件时间线默认折叠。执行历史包含每个 Task 的所有 Attempt、阶段上报、心跳、结束时间和脱敏失败原因。
执行阶段来自 Worker 的上报；未上报时明确显示等待信息，不估算完成百分比。

| 阶段 | 含义 | 下一步 |
| --- | --- | --- |
| 待整理 | 尚未激活的需求 | 编辑、开始或取消 |
| 已排队 | 已请求执行，等待后台调度 | 等待；需要停止时取消 |
| 执行中 | Worker 运行或已有结果正在发布 | 查看阶段和心跳；需要停止时取消 |
| 需要处理 | 执行失败、发布失败、问题、决策或清理失败 | 根据具体原因回复、批准、重试或取消 |
| 待评审 | 已产生 PR，需要评审或后续修改 | 打开 GitHub PR、要求修改或取消 |
| 已完成 | 结果已经满足完成条件 | 必要时重新打开 |
| 已取消 | 已停止的任务及历史记录 | 使用“已取消”视图查看，不再执行旧操作 |

看板和已打开的详情每 15 秒刷新。输入中的回复和修改草稿不会被轮询覆盖。
失败反馈保留在当前对话框中；网络超时会结束等待状态，重试相同请求复用幂等标识。
任务和事件采用游标分页，已加载的任务页在自动刷新时保留。

## 异常恢复

- **执行失败或连续失败**：查看最近一次执行的原因，使用“重试执行”。Web 任务可调整
  Worker；GitHub 来源的失败任务通过现有 retrigger 流程重新验证来源及授权。
  已排队或仍在运行时不重复创建 Worker。
- **发布失败**：使用“重试发布”，复用已经审核的结果。它不重新执行任务，也不会放宽
  发布审核和幂等规则。一次重试执行结束后，如果仍然失败，可以再次重试。
- **Issue / PR 关闭时 Worker 仍在运行**：原始任务和派生 PR 的执行信息一并展示。
  点击“停止并取消”会检查停止结果；未确认进程组停止时保留运行状态并给出原因。
  后台记录的清理失败也会进入“需要处理”。
- **取消与发布冲突**：仓库已有活跃执行周期时返回冲突，待周期结束后重试。
  取消成功会终止未完成任务、撤销准备中的尝试和待发布动作，并保留已有 GitHub 变更。
  发布与取消通过按任务的进程锁互斥，避免取消已成功却仍发送先前读取的待发布动作。
  GitHub / Git 发布命令超过 60 秒会记录可重试失败，外部调用期间不占用 SQLite 写锁。
- **长时间排队**：查看 `/operations` 的服务状态。Web 只提交请求；后台服务仍需运行，
  并具备可用 Worker、仓库配置和 GitHub 授权。

源 Issue / PR 的状态来自最近一次后台同步。关闭源不会授权 Web 自动重新打开 GitHub 对象。
缺少进程标识、权限不足或后台长时间占用时，页面会显示失败原因，不会伪报取消成功。

## 本次检查范围

重构基于 `origin/main` 的 `9e665db`，保留已有终态保护、GitHub 失败任务 retrigger、
PR 链接及发布去重逻辑。重点修正状态与操作不一致、详情不刷新、手机宽度下阶段入口被挤压、
错误反馈不明显、发布失败误显示为执行中，以及取消后的历史不可见。

复现和交互验证使用隔离 SQLite 数据库，没有停止真实 Worker、修改真实 Issue / PR 或发布内容。
Python 生命周期测试覆盖发布重试、关闭来源与派生 PR、停止失败、运行周期冲突、重复请求与恢复。
前端请求测试执行：

```bash
node --test tests/browser/*.test.cjs
```

`tests/browser/closed_pr_actions.js` 是手动 Playwright 场景；取消需要在详情中确认。
编辑或回复期间若版本或待处理事件发生变化，会保留草稿并要求核对最新内容。

## 与主工作区已有恢复逻辑的集成

daemon 与本地调度共用 SQLite 执行状态。Worker 身份、发布意图、恢复额度和取消
约束继续生效；已使用 DBOS 的部署须先按[升级指南](sqlite-state-machine-upgrade.md)
完成离线交接。只读界面可以查看旧记录，写入入口会拒绝未交接的执行状态。

- 手动重试创建新的 Task，旧失败任务保留终态与证据。
- 一次自动恢复额度耗尽后进入“需要处理”，不因升级重置。
- 发布结果未知时先核对远端，不能直接重新发布。
- 取消只有在确认 Worker 停止后才完成，迟到结果不会复活任务。

真实任务数据库的离线快照可以验证状态投影、历史和按钮可达性；真实 Worker 停止、再次执行及 GitHub 发布必须在拥有该数据库和进程的执行主机上验证。

## 真实数据与历史日志

- 未指定阶段时，窄窗口优先打开有任务的“需要处理”等阶段；显式选择的空阶段仍会保留。
- 看板执行历史按最近任务、最近尝试排序。每个 Task 先显示 10 次尝试，可继续加载全部记录，自动刷新保留已加载数量和展开状态。
- GitHub 工作详情按尝试折叠阶段和日志，自动刷新保留展开状态，并提供对应看板入口。已结束且来源关闭的工作流不再因历史失败或已跳过发布而留在当前待办；运行、待发布或修复中的记录仍保留关注。
- 日志链接使用具体 `artifact_id`，同时校验所属任务和产物类型，避免历史尝试误打开最新日志。文件缺失时明确显示“当前机器没有此文件”，不显示无法打开的预览入口。SQLite 快照仅含日志引用，查看原始 stdout/stderr 需要同时提供对应 artifacts 文件。
- 兼容历史 DD / Robert 数据库中已经占用的迁移版本号，保留旧记录，以迁移名称和 checksum 识别是否已应用。只读旧工作流详情不要求存在新看板表。

### Real snapshot walkthrough and GitHub-style presentation

The board uses a neutral shared shell, compact cards, underlined stage navigation,
status dots and horizontal Kanban columns above phone widths. Stage navigation
scrolls to the selected column; background refresh keeps the operator's position.
Historical work lists omit repeated execution-state badges and task UUIDs; the
execution detail still exposes task identifiers, attempts and failure evidence.

The detail's visible lifecycle joins task intake, classification/implementation,
execution counts, publication receipts and recorded PR-merge events. Publication
receipts expose only action type, audit/publication status, timestamps and a
redacted error. A closed PR alone never supplies a merge milestone. Event times
are observation times; worker completion and successful publication are separate
facts. Finished tasks fold their latest worker report, retaining per-attempt
phases and exact artifact links. Missing remote files are described once per
attempt rather than repeated as unusable links.

Snapshot walkthroughs used `wi-cfff835be757068c8a20` (Issue 3404, two successful
executions, PR 3405 published and merge recorded) and `wi-af5b769747247343e7a0`
(Issue 3020, 394 executions including 392 failures, PR 3021 published and merge
recorded). These are historical walkthroughs, not new worker runs. The supplied
snapshot lacks the referenced artifact files, so original stdout/stderr contents
remain unavailable locally. Mutating controls stay disabled in snapshot mode.
