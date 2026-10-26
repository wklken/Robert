# 多模型 PR Review

Robert 内建多模型审查流程：准备上下文、独立审查、验证汇总、发布 PR Review。
不需要安装 github-pr-monitor 或额外审查 skill。默认保持现有单模型行为。

在现有配置中声明两个 worker，再覆盖 `review-pr` 路由：

```yaml
review_max_concurrency: 4

workers:
  codex_reviewer:
    adapter: codex
    command: codex
    model: default
    effort: default
  claude_reviewer:
    adapter: claude
    command: claude
    model: default
    effort: default

routes:
  review-pr:
    worker: codex_reviewer
    multi_agent_review:
      enabled: true
      workers: [codex_reviewer, claude_reviewer]
      author_allowlist: [your-github-login]
```

`workers[0]` 必须与该路由的 `worker` 相同。也可在单个 repository 的 `routes.review-pr`
覆盖 `multi_agent_review`；覆盖整个设置对象。作者 login 不区分大小写。
仅开关开启且 **PR 作者**在白名单时使用多模型；空白名单、未命中或开关关闭均使用原单模型路径。
触发者、commit author 和 committer 不参与白名单判断；`trusted_actors` 的触发授权继续生效。
每轮 review 共占用原有一个任务并发槽。准备和汇总使用第一个 worker，各 reviewer 在独立的全局子进程额度内并行执行。

## 并发额度

`model_max_concurrency` 是共享模型额度的新名称，`review_max_concurrency` 保留为兼容别名；同时配置时必须相同。
开启 [多 Agent 投票](multi-agent-vote.md) 后，两种内建流程共享该额度，避免各自启动一个独立池。

`max_concurrency` 继续限制任务数，一轮多模型 review 从准备到汇总仍算一个任务。
顶层 `review_max_concurrency` 单独限制实际运行的 review 模型子进程，默认 `4`，必须是正整数，
不支持 repository 或 route 覆盖。同一 Robert 数据库/数据目录下的所有仓库和轮次共享这个上限；
旧单模型路径继续使用原有任务并发，不进入这个子进程池。

准备、每个 reviewer 和汇总分别占一个子进程额度，协调器本身不占子进程额度。
准备完成并冻结上下文后，同轮 reviewer 在额度足够时全部并行执行；额度不足的 reviewer 等待，
已完成的模型确认进程组退出后释放额度。全部模型结果有效后，汇总再申请一个额度执行。
配置顺序决定结果和报告中的排列，完成顺序可以不同。

额度与 run 状态保存在 SQLite，启动前原子认领，避免不同协调器同时超额启动。
等待额度不计入 worker 的 `timeout_seconds`，父任务总时限仍然生效。
新的 `review_max_concurrency` 在下一次加载该配置的运行中应用到共享池；降低上限保留已运行的进程，
新进程等待占用降到新上限以下；提高上限后等待者继续申请。

同轮重试复用已验证成功的结果，只有需要执行的阶段重新申请额度。
取消会阻止尚在等待的阶段启动，并停止已登记的模型进程组；未确认退出或启动结果不明时保留额度并进入需处理状态。
恢复会核对既有进程和文件，继续等待活跃模型或接纳有效结果，不为同一活跃 run 重复启动进程。
协调器收到 SIGTERM 时先设置停止标记；正在进行的启动会完成身份登记，再停止和清理。
协调器异常清理先提交轮次状态，观察/终止所有模型后再写清理结果，避免等待进程退出时占用 SQLite 写锁。

### 未知启动的人工恢复

如果协调器被 SIGKILL 或在启动后、登记前崩溃，某个 run 可能停在 `starting` 且没有进程记录。
Robert 无法自动证明该进程已退出，因此保留额度并阻止该 PR 的后续轮次。普通取消仍会拒绝此状态。

1. 在任务执行历史查看异常阶段，并用 `robert task show <task_id> --output json` 获取对应 run ID 和输出目录，再检查目录内的日志。
2. 在运行 Robert 的主机上检查并停止对应模型和工具残留进程；仅停止协调器并不能证明模型已退出。
3. 打开该任务的取消界面，核对列出的未知 run ID，勾选“我已检查并停止……所有残留模型进程”，再确认取消。

确认只作用于界面展示的 run ID。服务端在任务锁内重新检查；如果记录已经有进程身份、所属任务不匹配，
或协调器不能安全停止，则拒绝操作。成功后标记这些 run 为 `abandoned`、释放额度并取消本轮；
操作者和 run ID 写入任务事件，旧进程迟到的结果不能被接纳或发布。该操作依赖操作者对残留进程的检查，
不会声称自动终止了身份未知的进程。新的可信评论可以发起下一轮 review。
API 使用现有取消命令的 `abandon_review_run_ids` 列表，仍要求仓库权限、CSRF、版本及幂等键。

## 文件交接

每轮 fetch 当前 PR head/base，验证 SHA 后建立独立 detached Git worktree。
完整 PR diff 从该轮 merge-base 到 head 计算。共享源码和上下文不被后续轮次 reset。
PR 信息、触发上下文、变更文件、diff/stat/行数、Python 运行时信息由 Robert 写入；
第一个 worker 以独立会话阅读源码、调用方、项目约定和测试，写出上下文摘要和源码索引。
没有验证到项目 Python 解释器时，运行时记录 `UNRESOLVED`，不能用系统 Python 作语法错误的证据。

Robert 冻结 manifest 和一次生成的 `reviewer-prompt.md`。所有 reviewer 的 stdin 读取同一个文件，
启动前后核对 SHA256；重试也不改变 prompt。不同 CLI 内置的系统提示不在此字节一致保证内。
每个子进程使用相同 cwd，通过独立环境变量 `ROBERT_REVIEW_OUTPUT_DIR` 得到自己的结果目录，
不将 worker 名称或各自输出路径插入 prompt，不修改宿主环境。

reviewer 自行阅读 manifest、完整 diff、摘要、源码索引及相关源码，写 `result.json.tmp`，
关闭后原子重命名为 `result.json`。stdout/stderr 只用于诊断。结果必须包含轮次、快照、head SHA、
稳定 finding id、位置、严重性、触发条件、影响、证据、建议、排除项、覆盖范围及合并建议。
Robert 确认已登记的进程组结束，将指定交接文件复制到独立的 `sealed` 目录，
校验路径、schema、身份和共享输入完整性，再登记封存副本的 hash。汇总读取封存副本，
生产者之后修改自己的原始输出不会改变已接纳的副本；恢复和使用时继续核对封存 hash。
这是应用层的文件交接校验，不会阻止同账户进程直接访问封存目录。封存完成但 SQLite 登记前崩溃也可恢复。

全部模型结果通过后，Robert 创建 `synthesis/inputs.json`，列出本轮选中的每个结果和派生报告的路径、hash。
主 agent 以新会话读取这些文件并逐项复核，写 `evaluation.json`。
若汇总失败后某个 reviewer 结果需要重建，会保留旧清单，生成带 hash 的新 inputs 和汇总 prompt；旧汇总进程先收尾，其结果不会用于新清单。每个 finding 必须恰好被处理一次，
保留来源；未报告与明确反驳分开，不按多数票决定问题是否成立，也不允许增加没有模型来源的问题。
Robert 固定渲染中文 `merged-review.md`，经现有审计链路提交绑定 head SHA 的 GitHub PR Review `COMMENT`。

## 多轮、恢复和运行约束

- 可信的 PR 指派、review request 或 `@robert-bot review ...` 评论可以触发；机器人名以配置为准。
- 新评论创建新轮次，即使 SHA 未变。运行中收到的新请求单独排队，不会被上一轮消费；相同事件重复轮询不会重复建轮。
- 作者更新代码后再触发，将重新 fetch、建 worktree、准备上下文并审查完整 PR diff。
- 仅某个模型失败时，同轮重试复用已验证的其他模型结果；汇总失败仅重跑汇总。产物已落盘但数据库登记前崩溃，可重新验证后接纳。
- 配置了 `fallback_model` 时，同轮同阶段同一 worker 连续两次超时，下一次重试使用备用模型；其他 worker 的失败不会计入。实际选择在启动前持久化，恢复、报告和 Board 使用该值，reviewer prompt 不变。CLI 的 `default` 表示未指定具体模型，不推测其实际名称；相同模型配置不宣称为不同模型。
- 原成功文件缺失或 hash 改变，不再作为有效结果使用。无法确认进程身份或启动结果时停止自动执行；未知启动按上面的人工恢复流程处理，不盲目拉起第二个进程。主进程退出但进程组仍存在时同样停止，不能把仍可能变化的文件登记为成功。
- 使用现有任务 Retry/Cancel 和 publication retry。发布失败沿用原 action/result，仅重试发布和远端对账。
- 发布前再次核对 head、base、目标分支和 PR 是否仍打开；变化标记 `superseded`，需要新触发请求。已有排队请求会继续执行。
- 发布按 repository、PR、round 去重；遇到模糊响应先分页查询 PR Reviews，核对 marker、commit 和完整正文，不能确认时不重复发布。
- `robert status` 任务详情和 Board 执行历史显示轮次、SHA、各阶段/模型状态及失败原因。

Robert 在 macOS 和 Linux 上直接启动配置的模型 CLI，无需 `sandbox-exec`、`bubblewrap` (`bwrap`)
或 namespace 权限。此执行模式取代历史设计文档中的 OS 沙箱要求，没有额外 Python runtime 依赖。
CLI 沿用运行 Robert 的账户已有的登录、配置和文件权限，可以按该账户权限访问宿主机文件及其他轮次产物；
Robert 不再以 OS 策略限制这些读写。每轮 worktree、context、阶段输出和 `.tmp` 目录仍分别建立，
prompt 约定模型只写本阶段输出，并禁止后台或脱离进程；输入完整性、结果身份和封存 hash 仍由 Robert 校验。

两种平台继续监督已登记的模型进程组，保留并发额度、心跳、超时、重试、取消和恢复机制。
`setsid` 等创建的独立后代不属于原进程组，不能依靠 namespace 保证其退出；无法确认归属或退出的 run
仍进入需处理状态，并按上面的人工恢复流程处理。

`CODEX_HOME` 和 `CLAUDE_CONFIG_DIR` 在设置时传给模型 CLI；未设置时沿用 CLI 自身的默认配置方式。
`CLAUDE_CONFIG_DIR` 在两种平台上都可选，Linux 的 `claude`/`tclaude` 不再要求额外配置目录。
若显式设置，必须指向已存在的独立配置目录，不能与 Robert 数据、源码或上下文目录重叠；
应使用同一目录完成 Claude 登录。Robert 不自动迁移认证文件，也不把模型或 GitHub 凭据写入配置或产物。

产物保存在 `data_dir/artifacts/reviews`，代码放在 repository 配置的 worktrees 根目录下。
历史 worktree 和产物保留用于排查与审计；当前不自动清理。模型二进制与认证应在运行 Robert 的账户下配置好。
