# 手工安装 Robert

[文档目录](README.md) · [Agent 安装指南（English）](agent-install.md)

本指南从一台尚未运行 Robert 的机器开始，连接一个 GitHub 仓库，完成检查后启动服务。
已有实例请先确认其配置、数据目录和服务管理方式，避免启动两个实例。

```mermaid
flowchart LR
    A[选择版本] --> B[准备账号和仓库]
    B --> C[检查配置]
    C --> D[离线预览]
    D --> E[启动服务]
    E --> F[验证真实任务]
```

## 1. 准备环境

- Linux 或 macOS；Windows 使用 WSL。主机需要在工作期间保持运行并能访问 GitHub 和模型服务。
- 当前分支支持 Python 3.11–3.15、Git、GitHub CLI（`gh`）。
- 至少一个已安装、已登录且能在终端完成任务的 Agent；本指南以 `codex` 为例。
- 使用同一个非 root 系统账号安装、登录和运行服务。模型订阅或 API 费用由自己的账号承担。

```bash
python3 --version
git --version
gh --version
codex --version
pipx --version
```

缺少 `pipx` 时，按其[官方安装指南](https://pipx.pypa.io/stable/how-to/install-pipx.html)安装，
运行 `pipx ensurepath` 后重新打开终端。Agent 的安装与模型登录需按对应工具的说明完成。
版本输出只证明命令可执行，不证明模型认证或真实任务可用。

<a id="choose-version"></a>

## 2. 选择代码版本并安装

**代码和文档必须来自同一版本。** `main` 上的文档可能领先于 PyPI，
`robert --version` 也不能区分两个版本号相同的开发提交。

| 目的 | 安装方式 | 应阅读的文档 |
| --- | --- | --- |
| 使用一个已发布版本 | 从 [Releases](https://github.com/wklken/Robert/releases) 选定版本，安装同版本 PyPI 包 | 该 tag 下的 README 和安装指南 |
| 体验本页描述的当前能力 | 克隆源码，记录提交，再从该目录安装 | 同一 checkout 下的文档 |

已发布版本的安装命令如下；将 `VERSION` 换成 Releases 中实际存在的包版本号（去掉 tag 的 `v`）：

```bash
pipx install robert-github-agent==VERSION --include-deps
```

**随后切换到该 tag 的安装指南，不要继续套用主分支的配置。**

体验当前分支时，选择一个尚不存在的目录（例如 `~/src/Robert`）：

```bash
mkdir -p ~/src
git clone https://github.com/wklken/Robert.git ~/src/Robert
cd ~/src/Robert
git rev-parse HEAD
pipx install . --include-deps
robert --version
robert --help
```

记录上面的提交 SHA；自动化安装应固定同一 SHA，而不是在不同步骤重新读取变化中的 `main`。
`--include-deps` 同时暴露 `supervisord`、`supervisorctl`。若之前已安装 Robert，先用
`pipx list` 确认安装来源；不要用 `--force` 覆盖现有环境或配置。

## 3. 准备账号与目标仓库

这里的目标仓库是 **Robert 将要处理的项目**，与上一步安装 Robert 的源码目录不同。

| 信息 | 示例 | 含义 |
| --- | --- | --- |
| GitHub 仓库 | `example/backend` | 要监听的仓库 |
| 本地 checkout | `/srv/repos/backend` | 专用且干净的 Git 工作目录 |
| Robert 的 GitHub 账号 | `robert-bot` | `gh` 登录账号；通常也是被提及、指派的账号 |
| 可信触发者 | `maintainer` | 获准给 Robert 下达工作的人的 GitHub 登录名 |
| 默认分支 | `main` 或实际名称 | 必须与目标仓库一致 |

不要混淆系统用户名、GitHub 登录名和 Git 提交作者。请使用独立的机器人与可信人类 GitHub 账号：
机器人接单，可信的人发出请求。`github.account` 配置监听对象，不会替你切换 `gh` 的登录账号。

```bash
gh auth status
# 尚未登录时，由你交互式完成：
gh auth login
gh api user --jq .login
gh repo view example/backend --json nameWithOwner,defaultBranchRef,viewerPermission
```

将所有 `example/backend` 替换成真实仓库。Robert 的账号需要能够读取该仓库、发布评论/评审，
创建 PR 时还需要向所用分支仓库 push 的权限。组织 SSO 和分支保护仍然适用。
不要把 Token 写进 Robert YAML。

克隆或选择专用 checkout 后检查：

```bash
git -C /srv/repos/backend status --short
git -C /srv/repos/backend remote -v
git -C /srv/repos/backend var GIT_AUTHOR_IDENT
```

若尚无 checkout，可先执行 `gh repo clone example/backend /srv/repos/backend`（目录须可写）。
若提交身份缺失，按自己的真实身份设置该 checkout 的 `user.name` 与 `user.email`。

**当前工作区实现要求名为 `upstream` 的远端。** 普通 clone 常只有 `origin`：

- `upstream` 指向配置中的目标仓库，用于读取默认分支和 PR。
- `origin` 指向可写的分支仓库。直接协作时可与 `upstream` 相同；fork 模式下指向机器人 fork。

仅在 `upstream` 不存在时添加，不要覆盖已有远端：

```bash
git -C /srv/repos/backend remote add upstream https://github.com/example/backend.git
git -C /srv/repos/backend fetch upstream main
```

将 URL 和 `main` 换成真实值。HTTPS 或 SSH 必须在运行服务的账号下可用；`gh auth status`
成功不代表 Git 的 SSH/HTTPS 认证也成功。使用 HTTPS 且希望 Git 复用 gh 登录时，可执行：

```bash
gh auth setup-git --hostname github.com
```

这会修改当前系统用户的 Git credential helper；已有企业凭证配置时先核对，SSH 远端则使用 SSH 密钥。
fetch 不发布代码，首次 PR 验收才证明 push 可用。

## 4. 生成并检查配置

新安装可交互式运行：

```bash
robert init --config ~/.config/robert/config.yml
```

按提示填写上表和 Worker 命令。已有配置时不会自动覆盖；不要为跳过检查加 `--force`。
默认数据目录为 `~/.local/share/robert/`，配置格式为 `version: 2`。

启动前打开生成的 YAML，逐项核对：

- `repos[].full_name`、`checkout`、`trusted_actors` 与预期一致。
- `repos[].default_branch` 初始值固定为 `main`；目标使用其他分支时必须修改。
- `repos[].worktrees` 是可写的任务工作区目录；可设到 checkout 外部，避免混入业务目录。
- `github.account` 是监听账号，`default_worker` 指向已定义 Worker。
- `workers.default.command` 是实际命令，`adapter` 与所用 Agent 相符。

`init --worker` 只会自动识别 `codex`、`tcodex` 和 `cbc`；其他命令（包括绝对路径）
需要检查适配器。例如使用 Claude Code 时，将生成的 Worker 段改为：

```yaml
workers:
  default:
    adapter: claude
    command: claude
    model: default
    effort: default
```

其他适配器、环境变量和 Skills 见[操作指引](guides.md#worker-adapters)，
完整最小配置见[配置参考](reference.md#configuration)。先保持 CI/冲突自动修复与远程 Web 关闭。
在已受限容器中有意以 root 运行 Claude 时，另读[容器说明](guides.md#claude-workers-as-root-in-a-restricted-container)。

## 5. 检查后再启动

```bash
robert config validate --config ~/.config/robert/config.yml --output json
robert doctor --config ~/.config/robert/config.yml --output json
robert run once --config ~/.config/robert/config.yml --dry-run --skip-external
```

前两项应返回 `ok: true`；doctor 检查配置、默认 Worker 可执行文件、GitHub 登录、仓库路径与 Skills。
它不执行真实模型任务，也不验证所有 Git 远端权限。预览不发现新的 GitHub 工作或发布结果，
但会写入本地运行记录；它不是端到端验收。失败时先按[故障排查](troubleshooting.md)定位原因。

## 6. 启动后台与看板

按[Supervisor 完整配置](guides.md#daemon-service)创建配置与日志目录，替换示例中的用户路径。
这个进程管理器与 `robert supervisor` 独立任务监督器是两回事；初次安装运行 daemon 和 Web 即可，
额外监督服务按[持续监督指南](task-supervision.md)配置。

启动 daemon 后，它可能接收已有请求、执行 Agent 并发布允许的 GitHub 动作。
确认仓库和可信用户正确，且没有旧实例后执行：

```bash
supervisord -c ~/.config/robert/supervisord.conf
supervisorctl -c ~/.config/robert/supervisord.conf status
```

等过配置中的 `startsecs`（示例为 5 秒），确认 `robert-daemon` 和 `robert-web` 都为 `RUNNING`。
打开 `http://127.0.0.1:8765/`，在 `/operations` 查看后台活动。默认看板只读。
云主机可通过 SSH 本地端口转发访问：

```bash
ssh -N -L 8765:127.0.0.1:8765 YOUR_SERVER
```

在自己的电脑浏览器打开上述本地地址；无需把 Web 端口开放到公网。
Supervisor 自动重启子进程，但主机重启后仍需由宿主服务管理器启动 Supervisor。
若需要原生开机服务，参考[自动服务后端](guides.md#automatic-service-backend-alternative)，
先查看 `robert service install --dry-run --output json`，再选择该流程中的 `robert service start`。
不要混用两套服务管理方式。

## 7. 验证一个真实任务

接着完成[第一个任务](first-task.md)：先请求一次分析评论，再按需要验证 PR 创建。
服务状态正常只是“已启动”；只有看到匹配的任务记录和实际 GitHub 结果，才能确认对应链路已跑通。
