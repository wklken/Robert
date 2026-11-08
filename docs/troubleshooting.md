# 故障排查与常见问题

[文档目录](README.md) · [安装](human-install.md) · [支持渠道](../SUPPORT.md)

先确认正在检查的是哪套配置、哪个系统账号和哪种服务管理方式。
命令中的配置路径必须与运行中的 daemon 和 Web 相同。

```bash
robert --version
robert config path
robert config validate --config ~/.config/robert/config.yml --output json
robert doctor --config ~/.config/robert/config.yml --output json
robert status --config ~/.config/robert/config.yml --output json
```

## 安装或启动失败

| 症状 | 检查与处理 |
| --- | --- |
| 找不到 `robert` | 用 `pipx list` 确认安装来源；执行 `pipx ensurepath` 后重开终端，或用已解析的绝对路径 |
| 文档命令不存在 / 配置字段不被接受 | 对照 `robert --help`、安装来源和提交 SHA；使用[同版本文档](human-install.md#choose-version)，不要用旧包读取新配置 |
| 找不到 `supervisord` / `supervisorctl` | 新安装用 `--include-deps`；现有安装从 `pipx list` 定位环境的 `bin/`，无需为 PATH 问题覆盖安装 |
| 配置说 checkout 无效 | 指向真正的本地 Git checkout，确保服务账号可访问；不只是一个同名空目录 |
| `upstream` 或默认分支不存在 | 按[仓库准备步骤](human-install.md)检查远端和真实默认分支；`init` 不会自动配置 Git 远端 |
| 终端可用，后台找不到 Worker | Supervisor 不加载交互式 shell 配置；检查启动它的 PATH、真实可执行文件及所用系统账号 |
| doctor 通过但模型启动失败 | doctor 不执行模型任务；检查 Worker 登录、配额、adapter、模型名和环境白名单 |
| Claude 以 root 启动被拒绝 | 先确认运行账号和容器隔离；仅在符合条件时按[容器指南](guides.md#claude-workers-as-root-in-a-restricted-container)处理 |
| Web 端口已占用 | 检查是否已有实例；不要为绕过错误启动同一配置的第二套 daemon |
| 服务一直等待 lease | 排查旧实例和已记录进程，参照[进程恢复](features/github-polling.md#inspecting-and-recovering-attention)；不要直接删锁或数据库 |

## 看板正常，但没有执行或回复

Web 只显示状态并接受已获授权的操作，实际工作由 daemon 推进。
手工 Supervisor 部署使用：

```bash
supervisorctl -c ~/.config/robert/supervisord.conf status
supervisorctl -c ~/.config/robert/supervisord.conf tail robert-daemon stderr
supervisorctl -c ~/.config/robert/supervisord.conf tail robert-web stderr
```

使用 `robert service install` 的原生服务时，改查 `robert service status --output json`，
不要把 systemd/launchd 的实例误当作手工 Supervisor 实例。

1. 在 `/operations` 查看最近扫描、后台活动和错误。初始扫描默认可等待 300 秒；轮询不是实时推送。
2. 核对仓库、`github.account`、评论者的 `trusted_actors`；不要用机器人账号给自己发任务。
3. 确认提及在正文中，且请求明确。未受信任用户的内容可以作为上下文，但不能独立授权工作。
4. 已排队时查看容量、同一协作记录的活动任务，以及“需要处理”的原因。
5. 执行结束仍无回复时查看审核与发布记录。审核失败和发布失败是不同环节。

不要直接把不受信任的用户加入名单，只为让一条任务通过。

## 如何重试或暂停

先修复失败原因。已启用写入模式时，根据任务当前状态使用看板提供的重试执行、重试发布或取消。
若远端结果未知，先核实已有评论、分支或 PR，避免重复发布。
操作含义见[看板指南](features/web-lifecycle-board.md)。

手工 Supervisor 实例需要完全停止时：

```bash
supervisorctl -c ~/.config/robert/supervisord.conf shutdown
```

这会停止该配置管理的进程。若另装了独立任务监督服务，也要按其服务管理方式单独处理。
重启方式及配置重新加载见[服务操作](guides.md#start-and-operate-both-processes)。

## 常见问题

**需要 GitHub App 或公网回调吗？** 不需要。当前通过已认证 `gh` 轮询；本地 Web 可通过 SSH 转发访问。

**Robert 自带模型吗？** 不带。你提供已安装的 Agent 和模型账号，费用及限额由供应商决定。
不同 Worker 可配置不同模型，先让单 Worker 完成一个真实任务再扩大配置。

**必需安装额外 Skills 吗？** 以有效路由配置为准。缺少必需 Skill 会阻止任务；
缺少推荐 Skill 会产生诊断但不阻止执行。Robert 不自动安装或更新外部 Skills。

**可以只开看板吗？** 可以查看已有数据，但没有 daemon 就不会推进新任务。

**Supervisor 和 `robert supervisor` 是一个东西吗？** 前者是外部进程管理器，负责重启 daemon/Web；
后者是 Robert 的独立任务监督进程，检查停滞和交付证据。详见[持续监督](features/task-supervision.md)。

**数据在哪里？可以删掉重新来吗？** 默认配置在 `~/.config/robert/config.yml`，
状态、任务和产物在 `~/.local/share/robert/`。不要把删除数据库作为重试方法：它包含授权、任务关联与发布去重证据。
保留策略见[配置参考](reference.md#diagnostic-retention)。

## 仍然需要帮助

按 [SUPPORT.md](../SUPPORT.md)选择渠道。可以生成诊断包供自己检查：

```bash
robert diagnostics export --output robert-diagnostics.zip
```

脱敏不是公开上传的保证。先审查内容，只分享最小必要片段，删除私有代码、内部地址、路径与身份信息。
安全问题走 [SECURITY.md](../SECURITY.md) 的私密报告渠道。
