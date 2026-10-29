# Worker prompt 优化对照

基线：`b86f32754d450f67539f902de4c1eb914452b9ca`。以下均为相同输入下的完整 prompt 渲染；不是手写的节选或生产任务日志。

- [Before：修改前完整 prompts](before.md)
- [After：修改后完整 prompts](after.md)
- [逐段 unified diff](changes.diff)
- [同输入样本](inputs.json)

三项均已实现：mutation testing 条件化、按触发类型/内容选择工作流、按 route 精简并明确上下文入口。
原生 review/行内评论强制评估；普通 PR comment 由现有 worker 标记 request/review_feedback/mixed，再走对应分支。不增加模型调用。未知触发和历史任务保留严格评估。
任务政策由 controller 存入任务 metadata 并用于 audit；worker 不能覆盖政策。Mixed 仍需逐点评估，并在 handoff 说明独立请求。
Native review/vote 的 prepare、reviewer/voter 和 synthesis 不在本轮修改范围。

## 核心变化

1. mutation testing 仅在任务明确请求或适用仓库规则要求时执行。其他情况直接省略，不查工具，也不解释为何跳过。保留受影响测试和仓库强制检查。
2. GitHub context 先读 Markdown；只有需要结构字段时才查 JSON。仅有 JSON 时读 JSON；无 artifact 的独立调用内联完整请求。保留完整源上下文。
3. 分类仅展示分类结果字段；普通代码任务不展示分类字段。发布规则按 allowed actions 展示；空 memory/knowledge 不渲染。保留一条 status task 查询和 help 入口。
4. 普通修改请求不再强制逐点核查或编造 review_point_evaluation。评论中的审查报告（包括无问题报告）仍须核查；原生 review 不能自报 request 来跳过核查。
5. 保留 db_path、事件身份、结果提交、授权、脱敏、发布幂等、必要验证和恢复证据约束。

## 每阶段大小

单位为 UTF-8 字节，包含本次固定样本；不是 tokenizer 统计，不含 CLI 内置系统提示、技能内容、后续工具输出。缩减比例不是耗时收益。

| 阶段/场景 | Before | After | 缩减 |
| --- | ---: | ---: | ---: |
| classification-result | 8,904 | 5,225 | 41.3% |
| new-pr | 10,366 | 7,630 | 26.4% |
| update-existing-pr | 11,092 | 8,811 | 20.6% |
| review-comment | 9,434 | 7,106 | 24.7% |
| review-pr | 9,409 | 6,491 | 31.0% |
| comment-analysis | 8,494 | 5,574 | 34.4% |
| waiting-for-user | 8,461 | 5,370 | 36.5% |
| local-result | 7,827 | 3,946 | 49.6% |
| ci_remediation | 11,220 | 8,339 | 25.7% |
| merge_conflict_remediation | 11,216 | 8,331 | 25.7% |
| resume-update | 11,760 | 9,481 | 19.4% |
| new-pr-with-memory | 10,947 | 8,563 | 21.8% |
| native-review-feedback-update | 11,099 | 7,813 | 29.6% |
| native-review-feedback-comment | 9,440 | 6,294 | 33.3% |

`native-review-feedback-update/comment` 为原生审查意见的强制核查分支；普通 `update-existing-pr/review-comment` 展示内容判断分支。

`new-pr` 为普通实现；`update-existing-pr` 为已有 PR 更新；`review-comment` 为评估评论；`review-pr` 为 PR 源码审查；`ci_remediation` 和 `merge_conflict_remediation` 保留系统任务专用约束。`resume-update` 验证恢复说明，`new-pr-with-memory` 验证非空知识仍保留。

## 证据边界

未调用真实模型或改动部署配置；没有测量生产延迟。本地验证使用已有 `.venv`，Python 3.14.7。

- `python3 -B -m unittest discover -s tests`：1,336 tests，239.948 秒，OK，exit 0。
- `python -m ruff check src tests`：通过。
- CI 范围的 `python -m mypy`：9 个源文件通过。
- `python -m compileall -q src`：通过。
- `python3 -m build --outdir /tmp/robert-prompt-change/dist-final`：sdist 和 wheel 构建成功。
- `git diff --check`：通过。
- 14 组 before 与基线 renderer、after 与当前 renderer 逐字一致；结果示例均可解析为 JSON。
- 补充控制实验：结果 CLI 保留分类字段；resume 保留 classify 策略；prepared refresh 可加强为 required，后续普通评论不降级。
- 两轮只读独立 review 未发现可行动缺陷；旧私有 helper 的消费者、导出和动态入口扫描未发现遗留引用。

测试输出包含资源清理 `ResourceWarning`，未影响测试结果；结束后检查警告中提到的 subprocess PID 已不存在。没有据此宣称所有资源警告已修复。

普通评论的内容分类准确率仍需真实任务验证。分类由当前 worker 做出，审计能校验政策、字段和行动约束，不能独立证明每次语义分类都正确。
