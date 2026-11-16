# TAPD 来源

[功能专题](README.md)

可信 TAPD 用户在 Story 或 Bug 的评论中写 `@robert-wklken`，Robert 就把这条评论当作一个请求，
在标签对应的仓库里分析，并在原单据下回评。当前版本只处理问答与澄清：分类器只能选择
`comment-analysis` 或 `waiting-for-user`，要求改代码的请求会得到"尚未开放"的说明。

## 配置

```yaml
tapd:
  enabled: true
  executable: tapd                 # 可选，默认从 PATH 查找
  mention_name: robert-wklken      # 触发标记和自动评论前缀
  projects:
    - host: tapd.example.com
      workspace_id: "20001234"
      trusted_user: alice
      accept_after: "2026-11-20T09:00:00+08:00"
      label_rules:
        - label: robert-repo-a
          repository: owner/repo-a
```

- `accept_after` 必须带时区。只有在此之后创建的评论可以触发请求，更早的评论只作上下文。
- 每个项目内标签不能重复，`repository` 必须是已配置的仓库。标签只决定仓库，不代表授权。
- 认证由 TAPD CLI 自己管理（`tapd auth` 写入用户主目录的 `.tapd.json`，或使用 `TAPD_*` 环境变量）。
  凭据不写入 Robert 配置。Robert 在 `<data_dir>/tapd` 中运行 CLI，不读取工作目录里的 `.tapd.json`，
  只向 CLI 传递 `HOME` 和 `TAPD_*` 环境变量。

## 触发

扫描通道每轮按项目读取可信用户在水位之后的评论（水位存于 `tapd_scan_state`，回看 10 分钟）。
一条评论只有同时满足以下条件才产生请求：

- 作者是该项目的 `trusted_user`，创建时间不早于 `accept_after`；
- 正文中有可见的 `@robert-wklken`，不在引用或代码中，全角 ＠ 和零宽拆分不算；
- 不以自动评论前缀 `robert-wklken：` 开头，也不是 Robert 已记录的评论 ID；
- 第一次被观察到。之后编辑补上标记、编辑或删除已受理的评论，都不改变请求。

单据正文、其他人的评论和不带标记的评论只作上下文。请求的授权在第一次观察时固定；
受理、接续和发布前只按当前配置（启用、项目、可信用户、标签规则）和已保存的单据结束状态复核，
不访问网络。

## 仓库与受理

扫描读取单据的标签、状态和全部评论。恰好一个标签匹配时，在对应仓库写入来源行和请求；
没有或有多个匹配标签时不写来源行，对每条触发评论回复一次标签提示。调整标签后需要重新 @。

每张单据在每个仓库有自己的 workstream，对外关联 ID 是随机的 `rbt-…`。执行中新到的 @
排队，在当前任务交接后合并处理。读取失败只记诊断，不推进水位，也不推断单据已结束；
GitHub 扫描不受影响。

## 上下文

提示中区分三部分：本轮请求、单据讨论（正文和其他评论）以及 Robert 以前的回评。
此前回评按已保存的 TAPD 评论 ID 识别，包括已投递的通知。
讨论超过容量时保留最新的评论，并明确说明省略或截断的内容。

## 回评

结论以评论发在原单据下，正文以 `robert-wklken：` 开头，去掉其中的 @ 标记，并以可见的
`回执 <token>` 段落结尾。审核只允许评论当前请求所在的单据，不能评论 GitHub 或其他单据。

写入前先按回执查找已有评论，找到就视为已发布。写入结果未知时意图保持待对账，
下次按单据评论、自动前缀和回执确认是否已写入，确认缺失后才重发。读取失败按有限退避重查。
发布确认后记录评论 ID，之后的扫描把它当作 Robert 自己的评论。

## 澄清回复

Robert 提问后 workstream 进入等待。可信用户带标记的回复恢复执行；不带标记的回复只作上下文。
回复者按 TAPD 来源的可信用户判断，GitHub 上的同名账号不能代替。

## 单据结束

扫描跟踪仍有未完成工作的单据。单据进入工作流定义的结束状态后，来源行记为 `closed`，
仓库循环按 GitHub Issue 关闭的方式结束单据自己的任务：终止并确认 Worker 退出、
退役未开始的动作、完成工作项。排队的请求记为 `ignored_current_state_closed`，不再受理。

## 通知

标签提示保存为 `channel=tapd` 的 `notifications` 行，ID 由触发评论派生，重复观察不会产生第二行。
发布通道在运营者消息之后投递，沿用三次、10 分钟的重试上限，写入结果未知时可能重复一条。
单据已结束时跳过。标签提示投递前会读取单据状态，因为缺标签或标签冲突时没有来源记录。
通知只用固定模板，不带执行结果，也不构成授权。运营者消息通道的过滤和投递保持原有行为。

源码：[TAPD CLI 适配器](../../src/robert_agent/adapters/tapd/client.py)、
[评论与授权策略](../../src/robert_agent/work/tapd_policy.py)、
[扫描](../../src/robert_agent/work/application/tapd_scan.py)、
[受理](../../src/robert_agent/work/application/tapd_intake.py)、
[回评与对账](../../src/robert_agent/delivery/application/tapd_comments.py)、
[通知投递](../../src/robert_agent/delivery/application/tapd_notifications.py)、
[单据结束](../../src/robert_agent/work/application/source_lifecycle.py)、
[提示说明](../../src/robert_agent/resources/prompts/guidance/tapd.md)。
