# Worker 协议优化交付记录

本次在 `codex/worker-contract` 实现用户批准的方案，运行时只接受
`compact-v1`，不保留旧协议兼容解析。基线三项优化单独提交为
`ff42f36d1fbe310077c9590af3c17cc5aa4303e0`，新协议及调用方作为完整变更交付。

## 逐阶段完整 prompt 对照

所有组使用同一份 [14 组输入](../2026-10-29-worker-prompts/inputs.json)。
这些是实际渲染快照，不是手写节选。旧阶段快照保留当时规则；最终输出以阶段 4 after 为准。
外层 Markdown 围栏已加长，避免内部 JSON 围栏截断展示；未改写 prompt 内容。

| 阶段 | Before | After | 变化 |
| --- | --- | --- | --- |
| 基线 | [完整输入](../2026-10-29-worker-prompts/before.md) | [完整输出](../2026-10-29-worker-prompts/after.md) | 条件 mutation testing、按触发/内容分流、精简共享规则 |
| 1 | [before](stage-1-before.md) | [after](stage-1-after.md) | 明确提交预检、可修正错误、持久化确认与退出要求 |
| 2 | [before](stage-2-before.md) | [after](stage-2-after.md) | 唯一紧凑提交；机械字段、目标绑定和 marker 交由 Robert |
| 3 | [before](stage-3-before.md) | [after](stage-3-after.md) | 当前请求内联；长输入必须定位阅读；context 按 attempt 存档 |
| 4 | [before](stage-4-before.md) | [after](stage-4-after.md) | 显式输出模式、命令日志与来源边界；修正仅评论路由的多余 open_pr 指引 |

快照中的运行路径保持样本原值；真实 attempt 文件命名由集成测试验证。
渲染脚本及冻结的 before renderer 只用于审查证据，不是运行时旧协议入口。
阶段 2/3 的脚本若使用最终 renderer 重跑 after，会得到最终规则，不能据此覆盖历史快照。
阶段证据中的字节数是当时文件大小；不等于 token 数，更不等于模型耗时。

## 实现边界

- 模型提交业务判断、正文、真实验证声明和显式已处理事件集合。Robert 从同一份可信 scope
  补身份、仓库/分支/remote、目标、作者 mention、issue 关联和逐 action 唯一 marker。
- 确定性审核在保存最终结果之前执行。可修正错误不占 spool/结果槽位，也不永久阻塞 attempt；
  取消、owner 失效和不同最终结果仍拒绝。首次有效提交保存语义指纹及规范化结果，重复提交与
  spool 重放使用第一次的结果，不受后续 normalizer 变化影响。
- controller 仍等待进程停止，检查提交时 HEAD 与最终 HEAD，并执行 remediation attestation。
  Git 读取无法核实时不视为通过。事件消费仍是输入集合的显式提交，允许 subset；后续事件不自动消费。
- context 按 attempt 保存；prepared refresh 与 dispatch 共用 task 锁，检查状态/owner，更新
  artifact 元数据，捕获文件/DB 异常后恢复。未证明断电/SIGKILL 下多文件和 SQLite 的跨资源原子性。
- heartbeat 记录脱敏 argv、cwd 和退出结果。默认 summary；显式 tee 原样转发独立 stdout/stderr，
  保留私有完整日志。输出端阻塞不会阻止超时；未能转发的剩余输出由 `tee_incomplete` 标识。
  来源字段及日志路径是声明，不会产生代码新鲜度、测试覆盖或独立 attestation。
- 旧活动 worker、prepared 输入、未决结果/发布及旧 spool 会阻止切换。先用旧版完成并核对副作用，
  或停止并 reconcile 后 Cancel，再在新版创建剩余范围的 replacement task。保留历史和证据。
  详见 [运行合同与切换步骤](../../../src/robert_agent/resources/result-contract.md)。

## 审查与验证

父任务审查实现并完成所有旧协议测试调用方的迁移。原生 review/vote、Web、GitHub、受控验收工具
均接入新提交入口；没有以兼容 fallback 使旧测试继续通过。

审查修正覆盖：verification 字段形状、关闭 issue 的显式决定、多目标选择、提交后 HEAD 改变、
cutover 的并发 spool 删除、直接/后台发布绕过检查、旧 completed task 仍占用 active workstream、两条 comment 的 marker 独立性、waiting 纠正重提、prepared refresh
与 dispatch 竞争，以及程序生成的验收 marker。旧私有 helper、CLI 参数与所有 production
`build_result`/`record_result` 消费者已搜索；冻结证据文件中的旧符号不是运行消费者。

独立只读 review 发现并复现后台 publication 绕过 cutover；修复公共入口及通知前检查后，原 reviewer 运行两条回归验证通过。父任务进一步复现旧 completed task 保留 active workstream 的未决 handoff，被误判为历史；修复后由 reviewer 独立验证通过。

全套回归出现快速 worker 身份采集失败。新增确定性测试复现首次进程检查与身份采集之间退出的竞态；修复后 31 项相关检查通过。仅在原始 `Popen` 确认已退出时复用原退出码处理，活进程身份失败仍清理并拒绝。该有界修复单独提交。最终全套 unittest：1,399 项通过，242.214 秒。Ruff、CI 范围 mypy（9 文件）、compileall、wheel/sdist 构建与 `git diff --check` 均通过。测试输出仍有资源清理警告，本次未宣称解决。本地运行时为 Python 3.14.7；CI 的其他
Python/操作系统组合以远端检查为准。分阶段结果见 [阶段 2](stage-2-evidence.md)、
[阶段 3](stage-3-evidence.md)、[阶段 4](stage-4-evidence.md)；其中待办是各阶段交接时的历史状态，
不代表最终交付仍缺这些适配。

未连接生产 DB、未运行真实模型 A/B、未部署。已验证的是本地正确性与控制流程，不承诺生产延迟
改善比例。普通评论的 request/review_feedback/mixed 语义分类准确率仍需真实任务测量。
