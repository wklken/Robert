# Robert 文档目录

这里记录当前代码提供的行为、配置和操作方式。文档中的命令与默认值以本仓库源码、
配置 schema 和 CLI 为依据；线上运行是否成功需要部署现场的证据。

## 从这里开始

- **第一次认识 Robert**：先读 [README](../README.md) 的使用场景，再看[第一个任务](first-task.md)。
- **准备部署**：[手工安装](human-install.md)或 [Agent 安装（English）](agent-install.md)，先选定代码与文档版本。
- **已经在运行**：[故障排查](troubleshooting.md)、[看板操作](features/web-lifecycle-board.md)，再查具体配置。
- **参与开源**：[贡献指南](../CONTRIBUTING.md)、[安全策略](../SECURITY.md)、[支持渠道](../SUPPORT.md)。

入门指南以中文为主；Agent 安装、操作与命令参考包含英文，命令和配置以同版本源码为准。
根目录保留安装、通用使用、架构和开发入口；具体功能的配置与实现机制集中在
[功能专题](features/README.md)，需要深入了解时再阅读。

## 安装与使用

| 文档 | 内容 |
| --- | --- |
| [人工安装](human-install.md) | 环境要求、初始化、检查与首次启动 |
| [让编码 Agent 安装](agent-install.md) | 安装时需要的输入、权限与验证步骤 |
| [第一个任务](first-task.md) | 触发分析、验证发布、创建 PR 与继续反馈 |
| [故障排查](troubleshooting.md) | 安装、认证、远端、服务、排队与发布问题 |
| [操作指引](guides.md) | 多仓库、Worker、Skills、服务、Web 与 OpenClaw |
| [配置与命令参考](reference.md) | 完整最小配置、默认值、CLI、环境变量、数据保留与恢复 |

## 概念与架构

| 文档 | 内容 |
| --- | --- |
| [核心概念](concepts.md) | 信任边界、workstream、路由和 Worker 协议 |
| [架构与运行机制](architecture.md) | 源码分层、组件图、时序图、状态与数据关系 |

## 功能专题

需要配置某项功能或理解其内部机制时，进入 [features/](features/README.md)：
轮询与本地推进、持续任务监督、多模型评审、多 Agent 投票、Agent 输入与阶段契约、生命周期看板和操作员消息。

## 开发与协议

| 文档或代码 | 内容 |
| --- | --- |
| [开发指南](development.md) | 开发环境、测试、架构门禁和打包发布流程 |
| [贡献指南](../CONTRIBUTING.md) | 开发入口、验证要求与 PR 提交 |
| [安全策略](../SECURITY.md) | 私密漏洞报告、支持版本与运行边界 |
| [支持渠道](../SUPPORT.md) | 求助、缺陷与文档反馈需要的信息 |
| [行为准则](../CODE_OF_CONDUCT.md) / [社区目录](../COMMUNITY.md) | 社区规则与治理入口 |
| [仓库约定](../AGENTS.md) | 源码职责、依赖、状态与验证边界 |
| [配置示例](../src/robert_agent/resources/config.example.yml) / [schema](../src/robert_agent/resources/schemas/config.schema.json) | 可配置字段、类型和默认值 |
| [固定路由](../src/robert_agent/resources/routes.yml) | 输出、动作权限、验证及 workspace 策略 |
| [Worker 协议](../src/robert_agent/resources/worker-protocol.md) / [结果契约](../src/robert_agent/resources/result-contract.md) | 进度、结果提交、审计与幂等 |
| [命令清单](../src/robert_agent/resources/command-matrix.md) / [入口清单](../src/robert_agent/resources/entrypoints.json) | 内部工具和可执行模块 |
| [看板控制 API](../src/robert_agent/resources/kanban-control.md) | 本地操作的版本、权限与幂等约束 |
| [事件模型](../src/robert_agent/resources/github-events.md) / [workstream](../src/robert_agent/resources/workstream-model.md) / [分类](../src/robert_agent/resources/classifier.md) | 外部输入与任务路由 |
| [信任模型](../src/robert_agent/resources/trust-model.md) / [脱敏](../src/robert_agent/resources/redaction.md) | 执行授权与公开文本边界 |
| [Agent 输入与阶段契约](features/agent-prompts.md) / [审查原则](../src/robert_agent/resources/prompts/guidance/review-policy.md) | 统一输入结构、阶段上下文与审查行为 |
| [失败模式](../src/robert_agent/resources/failure-modes.md) / [监督诊断指令](../src/robert_agent/resources/prompts/stages/supervision-diagnose.md) | 执行故障与诊断输入输出 |
| [验收矩阵](../src/robert_agent/resources/acceptance-matrix.md) | 行为测试与受控验收工具的范围 |

## 文档维护

修改行为时，同时校验对应配置、命令、源码链接和图示。通用入口文档加入本目录；
功能配置、实现机制与专项恢复说明放在 `features/`，加入[专题索引](features/README.md)。
实施计划、审查报告和修复记录保存在被 Git 忽略的 `docs/superpowers/`、`docs/reviews/`、
`docs/remediation/`。公开文档不保留历史实现对比、迁移步骤或一次性验证日志。
发布版本记录单独保存在 [CHANGELOG.md](../CHANGELOG.md)。
