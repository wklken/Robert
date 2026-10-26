# PR #47 后续 review 修复

确认并修复原报告的三项问题；原 ID、原始优先级和判定保持独立记录。

基线：`4dd582724f8505b4cd037c9de1963fbab2027de2`（已合并 PR #47 的 `main`）；来源 PR head：`e7da717`，base：`d5ce073`。检查范围限于三项反馈及其调用链。原始来源见 [source-report.md](source-report.md)，完整合同和验证记录见 [remediation.json](remediation.json)。

| Finding ID | 判定 / 引入 | Status | 原证据 | 变更 / 测试 | 原因或下一步 |
| --- | --- | --- | --- | --- | --- |
| 1 · High | Partially correct / Yes | fixed | [原报告 1](source-report.md#finding-1) | common.py、run_once.py；test_followup_timeout_isolation.py | 普通 command cap 超时按 attempt/result/event 隔离；共享预算才产生 execution_deadline。 |
| 2 · Medium | Correct / Yes | fixed | [原报告 2](source-report.md#finding-2) | daemon.py；FollowupDaemonTests | ps 观察失败保留真实 child 结果和所有权，确认停止才回收 lease。 |
| 3 · Medium | Correct / Yes | fixed | [原报告 3](source-report.md#finding-3) | discover.py；FollowupIntentTests | 仅恢复 generic fallback 的 bug 优先；明确分析、vote/review、英文边界和否定规则保留。 |

## 行为变化与边界

1. [`ControlDeadlineExceeded`](../../../src/robert_agent/common.py) 区分共享预算耗尽与单条命令超时。未传 timeout、也没有控制 deadline 的调用恢复原本不限时的行为；显式 5 秒检查和 60 秒 publication cap 保留。运行中真正耗尽共享预算仍中止本仓库控制执行。
2. [`run_once.py`](../../../src/robert_agent/run_once.py) 的恢复观察、结果 scope 收集、分类后继和事件路由在本条工作上处理普通超时。未完成的观察保持待重试，避免把未知信息当成无进展或不合法 scope；回写仍核对最新 attempt/结果/执行控制。监督从前部阻断 audit/publish/discovery 的说法成立，路由发生在这些阶段之后，不能追溯阻断已经完成的阶段。
3. [`daemon.py`](../../../src/robert_agent/daemon.py) 只在进程组观察边界保守处理 OSError/TimeoutExpired。真实 Popen 失败仍可见，成功 child payload 不再被 ps 超时替换；unknown stop proof 保留 agent run/lease。共享进程检查函数的异常语义保持。
4. [`discover.py`](../../../src/robert_agent/discover.py) 继续优先匹配明确指令，再以有英文边界及否定保护的 bug fallback 处理混合 bug/analysis 文本。

三个问题分别有独立因果和修复；不更改其他 retention、并发、审计和完整性策略，不新增依赖。

## 验证

- 先补回归并观察失败：第 1 项五条 baseline regressions 均失败；第 2、3 项真实本地 child/故障注入和混合意图回归失败。修复后定向检查通过。
- 全套：`python3 -B -m unittest discover -s tests`：**1145 tests，195.254 秒，OK，1 skipped**（macOS 跳过 Linux bubblewrap 用例）。
- Ruff、CI 配置的 Mypy（9 个 source files）、compileall、wheel/sdist build、`git diff --check` 通过。
- [独立复审](review-summary.json) 未验证出新增问题，额外四项检查覆盖分类交接重试、共享预算、并发状态重校验和失败超时恢复；代码/测试内容身份为 `ddfad1871a9e929e934685171e58b47813327a2479eb4efd4c4ce79920921f85`。
- 首次全套中现有快速 worker 测试出现一次通知数 `1 != 0`。随后 20 次不改代码的独立重复和最终完整重跑均通过。未捕获该次通知内容，未将它标为已修复，也未证明它必然为既有缺陷。日志仍有 SQLite/Popen ResourceWarning。
- ps 故障在真实子进程边界注入，未实测 BusyBox；未调用真实模型或实际 GitHub 业务流程。证据仅支持本地隔离验证与明确的代码调用链。

## 变更边界检查

新增的 ControlDeadlineExceeded、_defer_attempt_inspection 和 daemon-local _process_group_stopped 均有生产调用者；无入口删除或兼容转发残留。逐项检查了标准 TimeoutExpired 与其子类的 catch 顺序、所有五条事件路由 handler、三条结果 finalize loop，以及共享 deadline 的传播。完整源校验、未知身份容量保留和既有 retry quota 继续生效。

回归代码：[超时隔离测试](../../../tests/test_followup_timeout_isolation.py)、[daemon 与意图测试](../../../tests/test_followup_daemon_intent.py)。
