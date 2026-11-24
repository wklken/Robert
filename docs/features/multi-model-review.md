# 多模型 PR Review

[文档目录](../README.md) · [功能专题](README.md)

Robert 内建多模型审查流程：准备上下文、独立审查、验证汇总、发布 PR Review。
不需要安装 github-pr-monitor 或额外审查 skill。默认使用单模型。

审查中的询问和决策使用最初可信触发用户作为请求归属人。Robert 创建的 PR 沿同仓库的来源 workstream 继承归属人，后续请求者不覆盖它。归属人及来源证据进入冻结审查上下文；单模型审查评论由控制器添加归属人的 mention，并在发布前审计。PR 作者仍用于原有作者校验和模型路由。无法确认归属人时回退到 `@wklken`。

在现有配置中声明两个 worker，再覆盖 `review-pr` 路由：

```yaml
scheduler:
  model_max_concurrency: 4

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
仅开关开启且 **PR 作者**在白名单时使用多模型；空白名单、未命中或开关关闭均使用单模型路径。
触发者、commit author 和 committer 不参与白名单判断；`trusted_actors` 的触发授权继续生效。
每轮 review 共占用原有一个任务并发槽。Robert 直接生成共享准备资料，汇总使用第一个 worker，各 reviewer 在独立的全局子进程额度内并行执行。

同一 PR 的通用 `@Robert账号 review this PR` 评论与 GitHub review request，在当前
head/base、完整 PR 范围、reviewer 顺序和配置、验收材料及授权资格一致时，关联同一个审查执行。
扫描先在数据库写事务外读取当前 PR，再分别记录每个触发的授权和执行关联。
额外关注点、材料、明确独立重审或资格不同的请求独立执行；无法确认完整修订的请求不复用。
晚到的等价请求关联原任务，不改变冻结输入。报告发布后，每个触发分别保存原结果与发布回执，
等价 GitHub review request 复用同一报告，不再调用模型或重复发布。报告发布后新发的人工通用 review 评论独立执行并回应；同一事件重放仍幂等。已取消、过期或无法继续推进的执行不会消费新的授权请求，新的事件独立开轮；尚可恢复的原任务保留既有额度和归属。既有历史轮次和运行进程保留各自身份。

## 并发额度

`scheduler.model_max_concurrency` 控制共享模型额度。
开启 [多 Agent 投票](multi-agent-vote.md) 后，两种内建流程共享该额度，避免各自启动一个独立池。

`scheduler.max_concurrency` 继续限制任务数，一轮多模型 review 从准备到汇总仍算一个任务。
`scheduler.model_max_concurrency` 单独限制实际运行的 review 模型子进程，默认 `4`，必须是正整数，
不支持 repository 或 route 覆盖。同一 Robert 数据库/数据目录下的所有仓库和轮次共享这个上限；
单模型路径使用任务并发，不进入这个子进程池。

共享准备由协调器完成，不启动模型、不占模型子进程额度。每个 reviewer 和汇总分别占一个子进程额度。
准备完成并冻结上下文后，同轮 reviewer 在额度足够时全部并行执行；额度不足的 reviewer 等待，
已完成的模型确认进程组退出后释放额度。全部模型结果有效后，汇总再申请一个额度执行。
配置顺序决定结果和报告中的排列，完成顺序可以不同。

额度与 run 状态保存在 SQLite，启动前原子认领，避免不同协调器同时超额启动。
等待额度不计入 worker 的 `timeout_seconds`，父任务总时限仍然生效。
新的 `scheduler.model_max_concurrency` 在下一次加载该配置的运行中应用到共享池；降低上限保留已运行的进程，
新进程等待占用降到新上限以下；提高上限后等待者继续申请。

任务总时限和 worker 阶段时限默认均为一小时；任务总时限包括准备、并行审查、等待额度与汇总。显式配置仍可覆盖默认值。

同轮重试复用已验证成功的结果，只有需要执行的阶段重新申请额度。
PR head/base 或验收标题、正文在排队或准备后变化导致轮次过期时，保存结构化原因，监督提供 `retry_worker` 候选。`decide` 模式需要当前模型批准；
恢复在确认原模型进程停止后创建子任务和新快照，保留原轮次、请求链与已消耗的一次恢复额度。
PR 关闭、当前权限撤销或进程退出不明时阻止恢复。
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

源码快照仅放行下文列出的、被 Git 忽略的明确测试产物。已跟踪文件即使命中忽略规则也必须
保持不变；其他新文件、HEAD、源码哈希、上下文和 prompt 的变化仍会被拒绝。

每轮 fetch 当前 PR head/base，验证 SHA 后建立独立 detached Git worktree。
完整 PR diff 从该轮 merge-base 到 head 计算。共享源码和上下文不被后续轮次 reset。
PR 信息、触发上下文、变更文件、diff/stat/行数、Python 运行时信息由 Robert 写入；
Robert 根据固定版本的元数据和 Git 统计直接写出简短上下文摘要和源码索引，记录完整变更路径、增删行数、删除/链接状态、父目录规则和运行/检查配置候选。
含控制字符或反斜杠等不能作为报告定位的合法 Git 路径，仍以 JSON 转义形式保留在变更统计中，标记为 `unindexable-path`，不加入源码入口索引；仅删除此类文件也可完成准备。完整内容继续通过捕获的 diff 查阅。
运行时信息包括变更组件及其父目录的 Python 声明，支持声明位于子目录的 monorepo。
准备摘要保留完整请求的入口，并提供有限长度的脱敏预览。索引仅表示发现了这些路径，不声称已阅读源码、解释规则或执行测试。每个 reviewer 独立阅读完整 diff、相关源码、调用方和测试，核对解释器、依赖和支持的验证命令。
没有验证到项目 Python 解释器时，运行时记录 `UNRESOLVED`，不能用系统 Python 作语法错误的证据。
reviewer 定向读取 manifest 的身份与上下文路径；完整源码哈希清单由控制器验证。

Robert 冻结 manifest 和一次生成的 `reviewer-prompt.md`。所有 reviewer 的 stdin 读取同一个文件，
启动前后核对 SHA256；重试也不改变 prompt。不同 CLI 内置的系统提示不在此字节一致保证内。
部署更新 prompt 模板后，已登记的轮次和阶段继续使用原文件，并核对登记的哈希。
冻结 manifest 已落盘但尚未登记数据库时，恢复核对原快照身份、源码及所有已登记上下文哈希、冻结文件集合和提示词哈希，再按内建模板完整渲染比对提示词后接纳。
当前支持共享清单模板及模型准备模板 v1；已有 `prompt_version=1` 的冻结包可匹配这两种模板，新快照使用 `prompt_version=2`。恢复保留原提示词字节与哈希，不重新生成共享资料；未知版本、未知模板或校验失败会拒绝接纳。尚未写入冻结 manifest 的旧提示词不享有此恢复保证。
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
Robert 固定渲染中文报告，分别保存渲染、脱敏与封存检查点；登记的公开副本经现有审计链路
提交绑定 head SHA 的 GitHub PR Review `COMMENT`。检查点绑定原始 evaluation、每个 reviewer
的封存结果和报告、manifest 及实际渲染/脱敏代码版本；登记重试复用封存报告，源码或输入变化时拒绝旧检查点。
封存 hash 在公开报告登记前单独写入数据库。没有可信封存记录的缓存必须从原始输入重新渲染、脱敏，
并精确核对结果，缓存目录中的自报 hash 不作为可信依据。
公开汇总保留问题、复核结论和 Agent Agreement Summary 表格；表头只显示 reviewer 名称，模型信息在报告开头列出。
已审查路径、未审查路径及 `coverage.notes` 保留在各 reviewer 的结果和独立报告中，公开汇总不再展开覆盖记录。
汇总 prompt 要求表格后的合并理由最多三句中文，只说明建议、阻塞情况和重要验证缺口；必要的人工决策各用一条短说明，保留决策人、问题、选项和建议。
每项复核理由限制为一至两句，说明关键证据或反证，避免重复问题详情和检查记录。
reviewer 仍在 notes 中记录实际检查、重要的未验证行为和产品取舍。汇总核验这些记录后精简呈现；产品取舍本身不作为代码缺陷，合并建议不替代维护者的判断或授权。
各阶段 prompt 要求输出脱敏，包括代码示例和 URL。Robert 在登记报告文件和 hash 前归一化
精确的 `password=secret` 示例字面值、本机路径和内部地址；其他凭据和高风险 token 仍阻止提交。
报告因内容校验被拒绝时保留已封存的成功模型结果，进入需处理状态，停止重复自动恢复。
失败原因保留具体校验分类；脱敏失败显示公共字段、规则和行列，不回显命中的值。
修正原因后可显式重试，已验证的模型产物继续复用。

## 审查原则与人工决策

普通 PR review、review 意见核验、原生 reviewer 和汇总阶段共用
[`review-policy.md`](../../src/robert_agent/resources/prompts/guidance/review-policy.md)。每轮根据当前代码快照、
issue/PR 验收要求及源码和测试证据独立判断。历史结论、自己的实现和模型信心不能代替证据；
已确认的需求及仍有效的恢复证据继续保留。汇总阶段只复核已有 finding，不新增无来源的问题。

对抗性审查同时寻找问题及反证，没有必须找出问题的数量要求。安全审查必须覆盖真实信任边界，
结合可达性、概率、影响和已有保护判断。阻塞问题需要当前版本的具体触发条件、实质影响或受保护
契约被破坏的证据。可信的严重漏洞即使少见也可能阻塞；纯假设、非支持环境、极端组合及风格偏好
可以不阻塞，说明假设和剩余风险即可。修复建议保持最小必要范围，避免大量推测性防御代码。

同一证据反复争论、继续 review 不再增加证据，或重要取舍无法决策时，应停止重复审查，说明版本、
争议点、证据与反证、待决问题、可选方案及建议，mention PR 作者决策。作者与配置的 Robert GitHub
账号一致时（忽略大小写），改为 mention `@wklken`；身份无法确定时请 `@wklken` 确认决策人。
reviewer 使用 `coverage.notes` 记录决策请求，汇总将其核验后写入公开的 `merge_reason`，
即使没有 finding 也保留。已有 finding 的不确定性用 `needs_confirmation`，不能当作误报；
重要风险待决时不能给出 `ready to merge`。这是一项提示词规则，不新增自动轮数阈值或自动合并行为。
mention 不扩大发布或修改权限，各阶段继续使用原有 JSON schema 和审计发布链路。

## 多轮、恢复和运行约束

- 可信的 PR 指派、review request 或 `@robert-bot review ...` 评论可以触发；机器人名以配置为准。
- 新增要求、材料或明确独立重审的评论创建新轮次，即使 SHA 未变，并单独排队。执行期间的通用等价请求关联原执行，各自保留授权与完成回执；已发布后新发的人工 review 评论创建新轮次并发布回应，避免静默完成。相同事件重复轮询不会重复建轮，等价的 GitHub review request 仍可关联已发布回执。
- 作者更新代码后再触发，将重新 fetch、建 worktree、准备上下文并审查完整 PR diff。
- 仅某个模型失败时，同轮重试复用已验证的其他模型结果；汇总失败仅重跑汇总。产物已落盘但数据库登记前崩溃，可重新验证后接纳。
- 配置了 `fallback_model` 时，同轮同阶段同一 worker 连续两次超时，下一次重试使用备用模型；其他 worker 的失败不会计入。实际选择在启动前持久化，恢复、报告和 Board 使用该值，reviewer prompt 不变。CLI 的 `default` 表示未指定具体模型，不推测其实际名称；相同模型配置不宣称为不同模型。
- 原成功文件缺失或 hash 改变，不再作为有效结果使用。无法确认进程身份或启动结果时停止自动执行；未知启动按上面的人工恢复流程处理，不盲目拉起第二个进程。主进程退出但进程组仍存在时同样停止，不能把仍可能变化的文件登记为成功。
- 源码快照拒绝未跟踪的审查规则、源码和配置，即使 Git 忽略这些文件。仅放行被 Git 忽略的明确测试产物：根目录 `bkcore_unittest.sqlite3`、`__pycache__` 内的 `.pyc`，以及根目录 `.pytest_cache` 的 `.gitignore`、`CACHEDIR.TAG`、`README.md`、`nodeids` 和 `v/cache/{nodeids,lastfailed,stepwise}`。这些产物可以在测试后更新，但文件及其父目录不能是符号链接；已跟踪文件仍受原有状态和哈希校验约束。
- 使用现有任务 Retry/Cancel 和 publication retry。发布失败沿用原 action/result，仅重试发布和远端对账。
- 发布前再次核对 head、base、目标分支和 PR 是否仍打开；变化标记 `superseded`，需要新触发请求。确认旧进程已停止后，重新触发失败任务会创建新轮次并读取当前 PR；旧轮次及其产物保留。已有排队请求会继续执行。
- 发布按 repository、PR、round 去重；遇到模糊响应先分页查询 PR Reviews，核对 marker、commit 和完整正文，不能确认时不重复发布。
- `robert status` 任务详情和 Board 执行历史显示轮次、SHA、各阶段/模型状态及失败原因。

Robert 在 macOS 和 Linux 上直接启动配置的模型 CLI，无需 `sandbox-exec`、`bubblewrap` (`bwrap`)
或 namespace 权限。
CLI 沿用运行 Robert 的账户已有的登录、配置和文件权限，可以按该账户权限访问宿主机文件及其他轮次产物；
Robert 不以 OS 策略限制这些读写。每轮 worktree、context、阶段输出和 `.tmp` 目录仍分别建立，
prompt 约定模型只写本阶段输出，并禁止后台或脱离进程；输入完整性、结果身份和封存 hash 仍由 Robert 校验。

两种平台继续监督已登记的模型进程组，保留并发额度、心跳、超时、重试、取消和恢复机制。
`setsid` 等创建的独立后代不属于原进程组，不能依靠 namespace 保证其退出；无法确认归属或退出的 run
仍进入需处理状态，并按上面的人工恢复流程处理。

`CODEX_HOME` 和 `CLAUDE_CONFIG_DIR` 在设置时传给模型 CLI；未设置时沿用 CLI 自身的默认配置方式。
`CLAUDE_CONFIG_DIR` 在两种平台上都可选，Linux 的 `claude`/`tclaude` 不要求额外配置目录。
若显式设置，必须指向已存在的独立配置目录，不能与 Robert 数据、源码或上下文目录重叠；
应使用同一目录完成 Claude 登录。Robert 不管理 CLI 认证文件，也不把模型或 GitHub 凭据写入配置或产物。

产物保存在 `data_dir/artifacts/reviews`，代码放在 repository 配置的 worktrees 根目录下。
历史 worktree 和产物保留用于排查与审计；当前不自动清理。模型二进制与认证应在运行 Robert 的账户下配置好。
