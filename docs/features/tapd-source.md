# TAPD 来源配置

[功能专题](README.md)

Robert 可以校验 TAPD 的 v2 配置。当前版本要求 `tapd.enabled: false`。
真实评论作者、标签和归档证据的接口能力尚待验证。配置校验会拒绝开启实时集成，
包括使用 `--skip-external` 时。仅配置 GitHub 的部署可以省略整个 `tapd` 段。

```yaml
tapd:
  enabled: false
  host: tapd.example
  executable: /srv/robert/bin/tapd
  cwd: /srv/robert/tapd
  api_timezone: Asia/Shanghai
  bot_name: robot-wklken
  reply_name: review-robot
  model_worker: reviewer
  projects:
    - workspace_id: "001234"
      trusted_author: project-operator
      operators: []
      repositories: [your-org/your-repo]
      mentions:
        enabled: true
        activated_after: "2026-11-14T08:00:00+08:00"
      rules:
        - id: gateway-story
          assignee: gateway-owner
          label: robert-gateway
          repo: your-org/your-repo
          kinds: [story, bug]
          repair:
            ci: false
            conflict: false
```

`host` 是服务主机名，配置校验会规范为小写。`executable` 和 `cwd` 必须是绝对路径。
禁用时可以校验配置而无需安装 CLI。`api_timezone` 使用 IANA 时区名称，供解释 API 的无时区时间。
`workspace_id` 使用带引号的数字字符串，保留前导零。项目和规则使用各自的精确标识。

`bot_name` 为评论中不带 `@` 的提及名称，默认是 `robert-wklken`。
`reply_name` 为回评署名，默认跟随所选 `bot_name`，回评以 `reply_name：` 开头。
两项使用 ASCII 字母、数字、点、下划线或连字符，并以字母或数字开头和结束。
`robot-wklken` 和 `robert-wklken` 对应不同名称。已准备的回评记录各自的提及名称和署名，
使后续对账沿用当时的正文协议。

`trusted_author` 标识项目的可信评论人，`assignee` 参与精确标签规则匹配。
两者可分别设置。配置中的作者名字需要由已认证的 TAPD 协议事实核实。
`repositories` 显式列出该项目允许的候选仓库；规则只可绑定其中一个仓库。
候选仓库、`model_worker` 都引用已有配置。提及模式需要含时区的首个启用时间，
校验结果保存为 UTC。模型 Worker 的配置名仅用于选择工作进程，配置校验不验证其工具隔离能力。

本地来源模型阶段当前只接受直接调用 Claude 的配置。命令须为指向原生 `claude` 可执行文件的
单个绝对路径，支持指向原生二进制的符号链接，额外参数和包装脚本会被拒绝。控制器构造空工具列表、
空 MCP 配置，关闭自定义设置来源、斜杠命令、浏览器和会话持久化。自定义模型端点、Provider 切换和
代理配置暂不支持，检测到这些设置时拒绝启动。该限制只适用于来源识别和汇总模型。真实 CLI 的
工具关闭行为仍待验收。

规则默认开启 CI 和冲突修复，也可分别关闭。开启 CI 修复时，必须在绑定仓库的 `pr_automation.ci.check_allowlist` 配置可信检查。
规则使用该仓库的 CI 时间、token、费用限额及冲突修复时间限额，共用固定三次修复上限。
仓库自动化开关继续管理其 GitHub 来源。

`operators` 填写 Web 或 OpenClaw 已认证的精确操作员身份，默认空列表。
OpenClaw 身份形如 `openclaw:telegram:personal:42`。可在 `messaging.tapd_projects`
配置显式的 `{host, workspace_id}` 项目范围。项目范围和 `messaging.repos` 各自独立，
Issue 派单仍需 GitHub 仓库范围。TAPD 来源消息收发尚未启用。

TAPD 凭据由控制器的认证环境管理。配置没有凭据字段，也不允许 Worker、监督诊断或
决策模型在环境白名单中引入 TAPD 凭据。

控制器为 TAPD 扫描和来源模型分别使用独立子进程。扫描在上次结束后等待 30 秒，
单次观测预算为 120 秒。模型协调先检查数据库中的到期记录。GitHub 轮询周期、限流和
普通 Worker 槽位各自管理。发布使用一次项目范围的 TAPD 批次，包含尚未确定仓库的结果，
与 GitHub 动作共用本次发布额度。GitHub 限流不会阻止该批次和已有回执的本地收尾。
单次运行先处理这批发布，再推进仓库工作，最后运行有时限的扫描和来源协调。
禁用配置只处理已有来源的本地撤销和取消。dry-run 不执行来源操作；跳过外部访问时，
只处理已记录的来源取消和原有 TAPD 来源已有完整回执的本地收尾。
混合 GitHub 触发的轮次还需要重新读取 TAPD 与全部原始 GitHub 触发评论；跳过外部访问或只对账模式下保持等待。
允许读取时，零发布额度仍可完成已有完整回执的本地收尾，不重复评论或推送。
控制子进程退出后，仍须独立证明模型或 Worker 进程组已经退出。
