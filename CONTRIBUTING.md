# 贡献指南

感谢你参与 Pleiad。当前项目处于早期开发阶段，欢迎通过 Issue、设计讨论、文档改进、测试和代码贡献参与交流。

提交贡献即表示你有权提交相关内容，并同意贡献内容随本仓库一起按照 [MIT License](LICENSE) 提供。除非另有书面约定，贡献者保留其原有权利，同时授予项目使用、修改、分发和再许可该贡献所需的 MIT 许可。

## 开始开发

```bash
# Run these commands from the root of your local fork.
cd g-agent
uv sync --locked --all-groups
cp .env.example .env
```

运行测试不需要真实模型密钥。进行真实模型验证时，在本地 `.env` 中填写密钥；`.env` 和运行数据均已被 Git 忽略。

## 架构边界

Pleiad 的公开架构基线是 [Architecture Overview](docs/architecture-overview.md)。这是一个初始的非兼容重构版本，变更应保持其中定义的边界：

代码边界如下：

- `pleiad.llm` 只负责模型协议、提供商和标准响应；
- `pleiad.tools` 只负责工具契约、能力库、加载器和运行时执行；
- `pleiad.skills` 只负责静态提示词资产；
- `pleiad.agent` 只负责模型、提示词、工具和执行循环；
- `pleiad.graph` 只负责任务图定义、校验和版本；
- `pleiad.runtime` 负责调度和状态迁移；
- `pleiad.application` 和 `pleiad.entrypoints` 不实现底层业务规则。

新增能力使用稳定的 `code` 注册到加载器。能力 YAML 只包含 `name`、`code` 和 `description`。新的执行能力统一实现为工具，新的静态方法和领域知识统一实现为技能。

自定义 Tool 的目录、注册入口和示例见 [自定义 Tool 开发指南](docs/custom-tools.md)。新增 Tool 时应同时提交实现、固定 `code` 注册、能力 YAML 元数据和确定性测试。

## 提交内容

一个变更应包含清晰的问题背景、实现范围和验证结果。涉及公开行为或稳定架构边界的变更，需要同步更新公开架构总览或相应开发文档。

建议的提交范围：

- 一个明确的问题或能力；
- 与实现对应的测试；
- 对外行为变化对应的文档；
- 不包含本地日志、Trace、执行记录、会话数据和模型密钥。

## 代码规范

- Python 版本固定为 3.12；
- 使用完整类型标注并通过严格类型检查；
- 导入语句统一位于模块顶部；
- 错误通过统一错误目录和错误数据模型表达；
- 日志使用全局日志模块；
- 运行过程通过事件和 Trace 暴露给界面及诊断工具；
- 新的工具和技能使用稳定的能力代码并补充测试。

## 提交前验证

提交前执行：

```bash
uv sync --locked --all-groups
uv run ruff format --check
uv run ruff check src tests
uv run mypy src
uv run lint-imports
uv run pytest
```

真实模型或真实终端行为发生变化时，还应完成对应的手工验证，并在 Pull Request 中记录模型提供商、终端尺寸和验证结果。

## Pull Request

Pull Request 请说明：

- 解决的问题；
- 主要设计决定；
- 涉及的架构边界；
- 自动化测试结果；
- 手工验证结果；
- 后续仍需处理的事项。

维护者会优先检查行为契约、数据模型、错误处理、可观测性和测试，而不仅是代码能否运行。
