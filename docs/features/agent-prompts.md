# Agent 输入与阶段契约

[功能专题](README.md) · [文档目录](../README.md)

Robert 为普通 Worker、原生 review/vote、监督模型和受限维护生成统一结构的输入。
每次输入说明当前阶段、任务目标、上下文入口、允许行为、交付产物和退出条件。
不同阶段使用各自的任务要求和结果协议。

## 生成入口与职责

[`context/prompts.py`](../../src/robert_agent/context/prompts.py) 是公共生成入口。
[`prompt_contract.py`](../../src/robert_agent/context/prompt_contract.py) 定义输入与输出值，
[`prompt_profiles.py`](../../src/robert_agent/context/prompt_profiles.py) 定义固定阶段及适用组合。
正文资源集中在 [`resources/prompts/`](../../src/robert_agent/resources/prompts/)：

- `common.md`：授权、材料可信度、证据和外部效果等公共原则。
- `stages/`：每个阶段的目标、工作要求和限制。
- `protocols/`：helper、原子文件及 stdout 交付方式。
- `guidance/`：按阶段启用的审查、验证、恢复和协作说明。

准备用例和协调器提供已授权的任务事实、上下文位置和输出契约。它们不另行拼接
阶段行为指令。路由、权限、租约、结果校验和发布仍由原领域负责；renderer 不访问
数据库或网络，也不从模型文本中推导授权。

## 固定的八个区块

| 区块 | 内容 |
| --- | --- |
| Stage | 当前流程/阶段、身份、首次执行或恢复 |
| Task | 当前阶段目标；原始请求与约束通过 Context 提供 |
| Boundaries | 可用能力、读写范围和禁止动作 |
| Context | 已内联、必读、条件读取、可选及缺失材料 |
| Work | 阶段专属处理要求 |
| Verification | 适用的验证政策、证据和覆盖限制 |
| Delivery | 产物、格式、目的地及消费方 |
| Ending | 完成、阻塞、失败和退出条件 |

阶段、来源和执行模式是不同字段。例如，Web/GitHub 表示输入来源，resume 表示
同任务恢复，而 review.evaluate 表示独立审查职责。恢复上下文不会把审查变成实现任务。

## 上下文材料

材料记录用途、来源、是否必需、正文或定位方式、适用版本，以及是否完整。
短请求可以内联；长请求可以引用本次输入归档。引用文件时必须提供可发现的入口，
并说明何时读取。控制器能读取某路径，不代表受限模型进程也能读取。

普通 GitHub/Web 输入保留原始事件及其身份。后到事件不修改已启动 Attempt 的输入，
结果只声明实际处理且属于本次输入的事件。历史摘要和前序模型结论是待核对的材料，
不能扩大用户授权或覆盖原始要求。

监督诊断和决策没有工具：上下文来自同一 JSON 包中的 evidence、候选、历史等字段。
必要输入不能依赖文件读取；缺失观察和裁剪范围须保持可见。受限维护也不能通过
Robert 控制器的文件路径补读历史，因此请求材料在其输入中自足。

原生 reviewer/voter 读取同一冻结材料，使用字节相同的 prompt；各自输出目录经
环境变量提供。独立阶段不读取其他模型的结果。汇总阶段只读取输入清单选中的封存
产物，并按阶段要求核验来源、findings 或程序计票。

## 阶段与交付

| 流程 | 阶段 | 产物 |
| --- | --- | --- |
| 普通任务 | 分类、实现、更新 PR、分析、单模型 review、反馈核验、提问、CI/冲突修复 | 现有 compact submission，通过 helper 提交 |
| 原生 review | prepare → evaluate → synthesize | context-summary.md/source-index.json → result.json → evaluation.json |
| 原生 vote | prepare → cast → synthesize | prepared-request.json → ballot.json → synthesis.json |
| 监督 | diagnose / decide | 各自既有 JSON 结果，stdout 返回 |
| 受限维护 | maintenance.repair | compact-v1 new_pr JSON，由 broker 绑定身份并提交 |

helper 的 recorded/deferred 表示持久交接成功，Worker 随后停止修改并退出；最终
审计和发布仍由控制器执行。原生阶段在规定目录原子写入结果文件，stdout/stderr
仅作诊断。stdout 阶段输出一个 JSON 对象后退出，不调用 helper 或等待其确认。

输入不足的处理也服从阶段结果契约：普通 Worker 可以提交 waiting_for_user；投票
准备可返回 needs_code/needs_clarification；监督按允许的诊断/决策值表达不足；受限
维护保留 new_pr 类型并如实填写 handoff 和验证结果。

## 模板与执行保证

renderer 输出模板来源、输入引用和内容摘要等元数据，便于检查本次生成的输入。
模型进程收到的实际文本、原始上下文和原生阶段产物仍通过现有 Attempt/round
路径管理。模板使用固定 profile，操作员配置不能注册新指令片段或扩大权限。

冻结原生轮次会校验当前模板、源码快照和登记产物。旧模板产物不通过当前校验时，
不得按新内容继续冒充原轮次；系统沿现有失败/attention 路径显式报告。新模板轮次
的部分失败重试仍可复用通过校验的成功产物。

本约定覆盖 Robert 交给模型进程的输入。CLI 自带的 system prompt、自行加载的
仓库规范/skills、工具返回和会话压缩仍由 CLI/harness 管理。输入文件存在或 hash
一致不证明模型已经阅读理解，也不提供操作系统级隔离。

开发时应同时验证所有阶段的生成内容、示例结果、协议冲突、快照校验和已安装 wheel
资源。命令见[开发指南](../development.md)。
