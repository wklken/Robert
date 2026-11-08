# OpenClaw Issue 派单

[文档目录](../README.md) · [功能专题](README.md)

在与 Robert 共用主机的 OpenClaw 会话中发起需求，Robert 先创建 GitHub Issue，
再自动分类和执行。调研或分析以原始 Issue 的结论回复交付；开发以关联 PR 和
原始 Issue 的结果回复交付。PR 等待人工合并，Robert 不自动合并或关闭 Issue。

## 启用

先配置并验证[操作员消息](operator-messaging.md)，再开启：

```yaml
messaging:
  enabled: true
  dispatch_enabled: true
  provider: openclaw
  channel: telegram
  account: personal
  target: "123456789"
  conversation: "telegram:123456789"
  thread: ""
  allowed_senders: ["123456789"]
  repos: ["your-org/your-repo"]
```

`dispatch_enabled` 默认关闭，且要求 `messaging.enabled: true`。仓库必须在 Robert
的 `repos` 和消息仓库列表中。OpenClaw 插件与 Robert daemon 使用同一配置和数据库。
服务账号的 `gh` 登录身份必须与仓库的 `github_account` 一致，并有创建 Issue 的权限；
凭据仍由 `gh` 管理。已有数据库通过 Robert 正常启动的 schema 初始化增加派单表。

更新生成的插件并重启服务：

```bash
robert openclaw install --force
robert openclaw status
```

保持 Robert daemon 运行。网络建单在独立发布通道进行；本地调度不等待 GitHub 扫描，
拿到确认的 Issue 后即进入首次分类。`run-once --dry-run` 不创建远程 Issue。

## 派单和查看

```text
/robert-inbox new your-org/your-repo 调研启动慢的原因，给出证据和建议
```

命令返回待派单、请求编号及完整提交命令。检查仓库和需求后发送返回的命令：

```text
/robert-inbox submit dispatch-0123456789abcdef0123456789abcdef
/robert-inbox status dispatch-0123456789abcdef0123456789abcdef
```

OpenClaw 命令上下文没有可依赖的每消息唯一编号。因此 `new` 只保存草稿；重复发送
`new` 会产生不同草稿。对同一请求重复 `submit` 返回既有状态，不重复创建 Issue。
编号绑定原始发送者、会话和仓库，不能通过请求正文指定另一身份。

分类器根据需求选择分析、开发或澄清。分析发现缺陷不自动获得修改代码的授权。
需要澄清时，Robert 在 Issue 提问，并将问题发回配置的会话；使用消息中给出的
`/robert-respond` 命令回复。回复沿用既有版本、工作项和 GitHub 上下文刷新流程。
分类完成、进入等待或 worker 提交结果都不等于交付完成。

状态包含请求阶段、Issue 链接、当前子任务和最终交付链接：

| 阶段 | 含义 |
| --- | --- |
| `draft` | 已保存，等待提交 |
| `accepted` | 已受理，等待远程建单 |
| `creating` | 建单已开始，或正在核对上次结果 |
| `issue_created` | 已确认 Issue，等待本地调度容量或该 Issue 上已有任务结束 |
| `admitted` | 已进入任务流程；查看 `task.lifecycle` 和 `/robert-inbox` 中的问题或失败通知 |
| `delivered` | 最终结果的全部动作有发布回执，且原始 Issue 收到回复；开发还需 PR 回执 |
| `failed` | 发送建单请求前失败，可修复后重试 |
| `attention` | 远程结果无法确认，需核查后重试对账 |

建单和完成通知包含 Issue、结果或 PR 链接。聊天投递沿用消息通道的有界重试，可能
重复发送通知；同一请求的完成记录只生成一次。配置撤销发送者、会话、仓库或派单开关
后，不再启动或发布该派单的工作。已发生的外部写入不会回滚。

## 失败与恢复

```text
/robert-inbox retry dispatch-0123456789abcdef0123456789abcdef
```

`failed` 重试再次核验身份后创建。`attention` 重试只按持久编号、机器人作者和原始
内容查找 Issue，不再执行第二次创建。创建请求超时、进程崩溃或 GitHub 查询结果为空
均不能证明 Issue 不存在。找不到唯一匹配时继续显示 `attention`，需在 GitHub 和本地
诊断中确认结果；不要把反复重试当成重新建单。确认未创建后可另建一个草稿并提交。
执行阶段的问题、取消和发布重试使用 `/robert-inbox` 提供的既有操作命令。

## 实现边界

`work/application/dispatch_requests.py` 接收命令并持久化远程建单状态；
`adapters/github/issues.py` 负责 `gh` 外部协议；`dispatch_requests` 表由
`models/work.py` 映射并通过 `persistence` 的命名查询和写入访问。
`application/publication_cycle.py` 在独立发布通道组合建单，
`work/application/dispatch_admission.py` 把确认的 Issue 接入分类和工作流，
`work/application/dispatch_delivery.py` 按最终结果回执记录交付。

机器人创建的 Issue 不伪装成可信 GitHub 用户事件。`operator_dispatch` 绑定本地
受认证请求、Issue 和原始正文；执行、接续与发布前重新检查当前配置。GitHub 扫描仍遵守
原有作者信任规则。通知按持久会话身份过滤后再分页，避免旧授权记录阻塞当前会话。

自动化测试使用临时数据库、生成插件和模拟 GitHub/消息传输。部署时还需验证真实
OpenClaw 命令上下文、服务账号的 GitHub 权限和回传渠道。
