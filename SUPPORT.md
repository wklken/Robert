# Getting help / 获取帮助

先阅读[手工安装](docs/human-install.md)、[第一个任务](docs/first-task.md)和
[故障排查](docs/troubleshooting.md)。The detailed command reference is
[here](docs/reference.md); the agent-led installation guide is [in English](docs/agent-install.md).
中文和英文反馈均可。

| Need / 问题 | Channel / 渠道 |
| --- | --- |
| 安装、配置、使用问题 / Usage questions | [GitHub Discussions](https://github.com/wklken/Robert/discussions) |
| 可复现缺陷 / Reproducible bug | [Bug report](https://github.com/wklken/Robert/issues/new?template=bug.yml) |
| 缺失或难懂的文档 / Documentation | [Documentation issue](https://github.com/wklken/Robert/issues/new?template=documentation.yml) |
| 新能力建议 / Feature proposal | [Feature request](https://github.com/wklken/Robert/issues/new?template=feature.yml) |
| 安全漏洞 / Vulnerability | [Private security reporting](SECURITY.md#reporting-a-vulnerability) |

## 提供哪些信息 / What to include

- `robert --version`、安装方式；源码安装附提交 SHA，避免同版本号的不同开发提交混淆。
- 操作系统、Python 版本、Worker/adapter 名称与版本。
- 最小重现步骤、预期结果、实际结果、相关文档章节。
- 服务管理方式（手工 Supervisor、systemd 或 launchd），daemon/Web 是否运行。
- 经检查的 `robert doctor --output json` 输出，以及相关任务的错误摘要。

A version string alone may not identify a development checkout. Include its
commit SHA and installation source. For a documentation-only problem, an OS,
Python version, or doctor result may be N/A; report the page and confusing step.

## 分享前检查 / Before sharing

不要公开 Token、API key、Cookie、私有代码、内部 URL、完整 Worker 日志或敏感本地路径。
诊断包即使经过自动脱敏，也应先自行审查，只摘取最小必要内容。
Never attach an unreviewed diagnostics archive to a public issue.

Check existing issues and discussions before opening another report. Support is
community-based; do not treat a successful model run as a guarantee of correctness.
