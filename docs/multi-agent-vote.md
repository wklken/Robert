# 多 Agent 投票

在 Issue 或 PR 中明确 `@` Robert 请求投票、方案比较或多模型选型。主 Agent 整理问题和选项，多个模型独立投票，再汇总回帖到触发请求所在的 GitHub 线程。投票提供决策建议，不会自动改代码、push、合并或关闭 Issue。

## 配置

在现有 `workers` 中配置模型，再开启独立的 `vote` 路由：

```yaml
model_max_concurrency: 4

workers:
  codex_voter:
    adapter: codex
    command: codex
    model: default
    effort: default
  claude_voter:
    adapter: claude
    command: claude
    model: default
    effort: default

routes:
  vote:
    worker: codex_voter
    multi_agent_vote:
      enabled: true
      workers: [codex_voter, claude_voter]
```

默认不启用。至少配置两个不同的 worker ID，第一个必须与路由的 `worker` 相同，负责准备和汇总，也会以独立会话投一票。可以增加第三个模型；两个模型产生平票时会如实报告。

repository 可以覆盖整个 `routes.vote.multi_agent_vote` 设置对象。只接受配置的 `trusted_actors` 明确提及机器人的请求；已有讨论参与者权限不扩大。PR review 的作者白名单继续只控制 review，不应用于投票。未开启时会解释缺少的配置，不把多模型请求变成一次单模型投票。

全局 `model_max_concurrency` 默认 4，控制 review 和 vote 两种内建多模型流程的实际模型子进程总数。旧配置 `review_max_concurrency` 仍有效；两者同时配置时必须相同，且都是严格正整数，不允许 repository/route 覆盖。一轮投票仍占原有一个任务并发槽；准备、各 voter 和汇总分别申请一个模型额度，额度等待不消耗模型执行 timeout，父任务总时限仍生效。旧单模型路径保持原有并发规则。

## 触发与澄清

```text
@robert-bot 投票：A 使用轮询，B 使用 webhook。
按可靠性、延迟和维护成本比较，请每个模型选一个并说明原因。

@robert-bot 对比上面 A、B 两个方案，给出推荐。
```

账号以配置为准。分类新增 `vote`，不会被背景里的“bug/修复失败”或已有 PR 的默认更新路由覆盖。普通分析、review、讨论如何实现投票，以及否定投票的请求，不会启动多模型投票。低置信度分类可以返回结构化 `recommended_route: vote`；创建子任务时仍验证原请求、开关和授权。

主 Agent 从请求和讨论中整理中性的选项、来源、比较标准、硬约束和未知项；保留用户指定选项的含义，不提前向 voter 提供胜出推荐。关键问题不明确时，使用现有 `waiting_for_user` 提出具体问题。可信用户在同一目标线程 `@` 新回复后，尚未冻结的同一轮继续准备；已使用的回复或早于当前问题的历史评论不能再次恢复任务；新的明确投票请求始终另起一轮。

Issue 纯讨论使用独立 context 目录，不要求源码 worktree。准备阶段需要源码时先获取默认分支的固定 SHA，再继续准备。PR 请求始终绑定本轮 head/base/目标分支和独立 worktree，普通评论、inline review comment 和 submitted review 正文都会绑定所用版本。各轮代码与上下文不互相重置。

## 投票和报告

准备完成后冻结问题、选项、标准、参与者、上下文和 `voter-prompt.md`。所有 voter 读取字节相同的 prompt，使用独立模型会话，只能写自己的 `ROBERT_VOTE_OUTPUT_DIR`；不同 CLI 内置系统提示不属于字节一致保证。

每个 voter 原子写入 `ballot.json`，包含 round、snapshot、spec hash、`choice_id`、非空 `reason_summary`、主要取舍、证据引用和未知项。`choice_id: null` 是弃权，必须说明 `abstain_reason`。输出必须通过身份、选项、来源和文件校验，stdout/stderr 仅作诊断。进程组退出后，Robert 把普通交接文件复制到生产者不可写的封存目录，汇总只读封存副本。

第一版采用单选、等权、一参与者一票。重试和 fallback 不增加票数，准备和汇总本身也不产生选票。所有参与者都必须返回有效选票，包括明确弃权；技术失败不算弃权，缺票时不能发布完整结论。

票数由程序计算：获得全体参与者 `> N/2` 票才达到严格多数，弃权不缩小门槛；唯一最高票但未过半显示“唯一领先”；最高票并列显示平票；全弃权时没有胜出项。不自动加权或发起复投。

报告逐一列出 Agent、调度使用的模型/adapter、原始选择和理由，附取舍与证据。主 Agent 另列共同观点、分歧、建议与待核实事项，可以提出不同于最高票的建议，但不能改写原票。`model: default` 显示“CLI 默认，实际模型未验证”；相同模型或 fallback 重合不能被描述为已验证的模型多样性。

PR 和 Issue 都发布一条普通 Conversation comment，经原有审计、redaction 和 publisher；不会提交 APPROVE / REQUEST_CHANGES。目标只能是触发请求所在的对象，不能改到关联 Issue 或其他 PR。

## 多轮、故障和恢复

- 相同事件重复轮询不重复建轮；新的请求单独排队，即使 SHA 未变，也不复用上一轮选票。编辑原请求不自动重投，请发新评论。
- 同轮只补跑失败阶段，成功的有效封存选票可复用；连续两次超时后的 fallback 按本轮/阶段/worker 独立计算，并在启动前登记实际选择的模型。汇总失败只重跑汇总，发布失败只重试发布。
- 文件封存后、数据库登记前崩溃可重验恢复；已运行模型不重复拉起。身份未知、启动不明或进程组未退出时保留额度并进入需处理状态。
- 使用现有 Retry/Cancel。未知启动必须由操作者检查并停止残留模型进程后，在取消界面确认放弃列出的 run；现有兼容字段 `abandon_review_run_ids` 也接受 vote run。操作继续要求仓库权限、CSRF、版本和幂等键，记录操作者，迟到输出不可再接纳。
- 发布前核对固定文件、当前任务身份和所用来源。PR 关闭/合并或 head/base/目标分支变化、已采用的正文/评论变化、Issue 源码默认 HEAD 变化，都阻止旧结果发布。来源无法查询时明确失败；不因为新无关评论或机器人回帖改变 `updated_at` 而误判过期。
- 发布用 repository/source/number/round 稳定去重，保留 `robert-comment` 并添加 `robert-vote` marker。响应丢失后分页查询评论，核对账号、marker 和完整正文，再决定是否重试；未知状态不重复 POST。
- CLI status 与 Board 执行历史显示投票轮次、各阶段/模型、失败原因和共享额度。历史产物保留在 `data_dir/artifacts/votes`，源码 worktree 位于仓库配置的 worktrees 目录。

macOS 使用 `sandbox-exec`，Linux 使用 `bubblewrap`；沿用 [多模型 review 的沙箱与 Claude 配置要求](multi-model-review.md)。缺少隔离工具时明确失败，不降低为不受限执行。不增加 Python runtime 依赖，也不把模型/GitHub 凭据写入配置或产物。

关闭开关阻止新轮次，已登记轮次按冻结配置继续，或由操作者显式取消。回退旧二进制前必须确认 vote 协调器与模型已停止；旧版本不识别 vote 的运行额度，不能与其并行运行。保留数据库新表和产物，不在回退时删除历史。
