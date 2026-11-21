# 新 PR 发布前 Review

[功能专题](README.md) · [现有 PR 的多模型评审](multi-model-review.md)

在仓库的 `routes.new-pr` 下配置 `pre_pr_review.workers`，Robert 会在推分支前审查本地候选提交。未填写时沿用原有新 PR 流程。以下片段使用已在全局 `workers` 中配置的名称。

```yaml
repos:
  - full_name: example/service
    # 保留该仓库已有的 checkout、worktrees、default_branch 和 trusted_actors。
    routes:
      new-pr:
        pre_pr_review:
          workers: [codex_reviewer, claude_reviewer]
```

一个 reviewer 直接产出报告。多个 reviewer 使用同一冻结上下文独立审查，结果全数校验通过后，由列表中的第一个 reviewer 汇总。第一个 reviewer 也负责准备上下文。实现 Worker 在另一个本地 worktree 逐项处理报告，并验证最终提交。 必需验证必须通过。可选验证可以记录跳过原因，已执行且失败的验证会阻止发布。已启动的任务保留其审查计划和选定的 Worker。修改配置后，新任务使用更新后的计划。

PR 正文包含 Review 报告、已修复项、未处理项及原因，并注明候选和最终提交。Robert 保存各 reviewer 的原始结果和汇总输入。Reviewer、准备和汇总共享 `scheduler.model_max_concurrency`，一个新 PR 任务占用一个普通任务额度。

配置的 `checkout`、`worktrees` 和数据目录可以通过根目录或祖先目录的软链接访问。新候选审查快照记录真实路径；已有快照中的同一可信根目录别名仍可校验。可信根目录内部的文件和子目录软链接继续被拒绝，审查文件必须位于本轮登记的目录内。

## 失败与恢复

- 审查或处置未完成时，发布门禁保持关闭。进程状态不明时，先停掉残留进程，再用任务取消中的未知启动处置功能确认。
- 暂时性失败按阶段有界重试。汇总失败时，成功的独立 reviewer 结果保持封存。只有输入受损或快照变化时，相应 reviewer 才重跑。意见处理失败保留报告，重试从候选提交新建处理工作区。
- 任务详情和 Board 执行历史展示候选、最终提交、各阶段尝试和安全错误。手动恢复受现有任务权限、来源状态和进程所有权检查约束。GitHub Retrigger 和 Web Retry 沿用已冻结的审查要求。输入、仓库和分支相同且尚无正式结果时，会复用之前封存的审查轮次。
- 推送和创建 PR 使用已审计的同一结果。若已推分支而建 PR 前发现远端 head 或 base 变化，Robert 保留回执并停止建 PR。创建结果不明确时，对账会核对目标、版本和正文。
- 对启用 Review 的仓库，受限维护任务当前停在维护授权检查。仓库策略同样约束当前任务的后续发布。

模型 CLI 仍使用 Robert 服务账户的权限。生产环境需按现有 Worker 信任规则管理账户和凭据。
