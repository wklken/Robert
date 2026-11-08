# 功能专题

[文档目录](../README.md)

这里按功能介绍具体配置、实现机制和恢复边界。首次部署从[安装指南](../human-install.md)
和[第一个任务](../first-task.md)开始；了解整体设计请读[核心概念](../concepts.md)和
[架构](../architecture.md)，参与开发请读[贡献指南](../../CONTRIBUTING.md)。

## 功能配置与操作

| 文档 | 内容 |
| --- | --- |
| [生命周期看板](web-lifecycle-board.md) | 状态含义、日常操作、异常恢复与交付证据 |
| [OpenClaw Issue 派单](issue-dispatch.md) | 两步提交、自动分类、Issue/PR 交付与建单恢复 |
| [操作员消息](operator-messaging.md) | OpenClaw 通知、提问、受认证回复与渠道接入边界 |
| [多模型 PR 评审](multi-model-review.md) | 作者白名单、并发、文件交接、审查与恢复 |
| [项目记忆与证据阶段](project-memory.md) | 技术提示、审计、发布回执与部署证明 |
| [多 Agent 投票](multi-agent-vote.md) | 显式触发、澄清、独立选票、汇总与发布 |
| [Agent 输入与阶段契约](agent-prompts.md) | 公共结构、阶段差异、上下文入口与交付协议 |

## 调度与监督机制

| 文档 | 内容 |
| --- | --- |
| [GitHub 轮询与本地推进](github-polling.md) | 扫描、缓存、持久观察、内部唤醒和受控发布 |
| [持续任务监督](task-supervision.md) | 独立观察、诊断、已知恢复、工程修复和验证 |

通用部署操作见[操作指引](../guides.md)，完整配置字段和命令见[配置与命令参考](../reference.md)。

- [Offline Compose lifecycle tests](compose-integration-tests.md)
