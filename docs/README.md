# Robert 文档目录

这里记录当前代码提供的行为、配置和操作方式。文档中的命令与默认值以本仓库源码、
配置 schema 和 CLI 为依据；线上运行是否成功需要部署现场的证据。

## 安装与使用

| 文档 | 内容 |
| --- | --- |
| [人工安装](human-install.md) | 环境要求、初始化、检查与首次启动 |
| [让编码 Agent 安装](agent-install.md) | 安装时需要的输入、权限与验证步骤 |
| [操作指引](guides.md) | 多仓库、Worker、Skills、服务、Web 与 OpenClaw |
| [配置与命令参考](reference.md) | 完整最小配置、默认值、CLI、环境变量、数据保留与恢复 |
| [生命周期看板](web-lifecycle-board.md) | 状态含义、日常操作、异常恢复与交付证据 |

## 运行机制

| 文档 | 内容 |
| --- | --- |
| [核心概念](concepts.md) | 信任边界、workstream、路由和 Worker 协议 |
| [架构与运行机制](architecture.md) | 源码分层、组件图、时序图、状态与数据关系 |
| [GitHub 轮询与本地推进](github-polling.md) | 扫描、缓存、持久观察、内部唤醒和受控发布 |
| [持续任务监督](task-supervision.md) | 独立观察、诊断、已知恢复、工程修复和验证 |
| [多模型 PR 评审](multi-model-review.md) | 作者白名单、并发、文件交接、审查与恢复 |
| [多 Agent 投票](multi-agent-vote.md) | 显式触发、澄清、独立选票、汇总与发布 |

## 开发与协议

| 文档或代码 | 内容 |
| --- | --- |
| [开发指南](development.md) | 开发环境、测试、架构门禁和打包发布流程 |
| [贡献与治理](../COMMUNITY.md) | 贡献、安全反馈和社区规则 |
| [仓库约定](../AGENTS.md) | 源码职责、依赖、状态与验证边界 |
| [配置示例](../src/robert_agent/resources/config.example.yml) / [schema](../src/robert_agent/resources/schemas/config.schema.json) | 可配置字段、类型和默认值 |
| [固定路由](../src/robert_agent/resources/routes.yml) | 输出、动作权限、验证及 workspace 策略 |
| [Worker 协议](../src/robert_agent/resources/worker-protocol.md) / [结果契约](../src/robert_agent/resources/result-contract.md) | 进度、结果提交、审计与幂等 |
| [命令清单](../src/robert_agent/resources/command-matrix.md) / [入口清单](../src/robert_agent/resources/entrypoints.json) | 内部工具和可执行模块 |
| [看板控制 API](../src/robert_agent/resources/kanban-control.md) | 本地操作的版本、权限与幂等约束 |
| [事件模型](../src/robert_agent/resources/github-events.md) / [workstream](../src/robert_agent/resources/workstream-model.md) / [分类](../src/robert_agent/resources/classifier.md) | 外部输入与任务路由 |
| [信任模型](../src/robert_agent/resources/trust-model.md) / [脱敏](../src/robert_agent/resources/redaction.md) | 执行授权与公开文本边界 |
| [Worker 提示词契约](../src/robert_agent/resources/worker-prompt.md) / [审查原则](../src/robert_agent/resources/review-policy.md) | Worker 上下文与审查行为 |
| [失败模式](../src/robert_agent/resources/failure-modes.md) / [监督诊断契约](../src/robert_agent/resources/supervision-diagnosis.md) | 执行故障与诊断输入输出 |
| [验收矩阵](../src/robert_agent/resources/acceptance-matrix.md) | 行为测试与受控验收工具的范围 |

## 文档维护

修改行为时，同时校验对应配置、命令、源码链接和图示。新增公开文档必须加入本目录。
实施计划、审查报告和修复记录保存在被 Git 忽略的 `docs/superpowers/`、`docs/reviews/`、
`docs/remediation/`。公开文档不保留历史实现对比、迁移步骤或一次性验证日志。
发布版本记录单独保存在 [CHANGELOG.md](../CHANGELOG.md)。
