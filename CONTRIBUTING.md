# 贡献指南

感谢你参与 Pleiad。当前项目处于早期开发阶段，欢迎通过 Issue、设计讨论、文档改进、测试和代码贡献参与交流。

提交贡献即表示你有权提交相关内容，并同意贡献内容随本仓库一起按照 [MIT License](LICENSE) 提供。除非另有书面约定，贡献者保留其原有权利，同时授予项目使用、修改、分发和再许可该贡献所需的 MIT 许可。

## 开始开发

开发工具链固定使用 uv 0.11.22（见仓库根目录 `.uv-version`）。SDK 和 App 是两个独立项目，分别在 `packages/pleiad/`、`apps/weave/` 维护 `pyproject.toml` 与 `uv.lock`；仓库根目录不配置 Python 项目或共享依赖环境。

```bash
# Run from the repository root.
uv sync --project packages/pleiad --locked --all-groups --all-extras
uv sync --project apps/weave --locked --all-groups --all-extras
cp .env.example .env
```

运行测试不需要真实模型密钥。进行真实模型验证时，在本地 `.env` 中填写密钥；`.env` 和运行数据均已被 Git 忽略。

源码目录是 `packages/pleiad/src/` 和 `apps/weave/src/`，通过各自 setuptools `package-dir` 配置映射为 `pleiad` / `pleiad_weave`。不要重新添加同名内层目录，也不要改成 `import src`。App 的 uv 开发源通过本地路径引用 SDK；构建发行物声明 `pleiad[mcp]==0.1.2`，发布 App 前需先发布对应 SDK 版本。

新增、删除或移动源码/资源后，在受影响的项目目录重新运行 `uv sync --locked --all-groups --all-extras`。新增非 Python 资源时，同时更新对应成员的 `package-data` 和 `cache-keys`，并验证 wheel/sdist。

SDK 单元测试位于 `packages/pleiad/tests/`；Weave 测试包含应用与跨包链路，位于 `apps/weave/tests/`。每个项目的 `pytest`、Ruff 和 mypy 配置独立维护，SDK 测试不导入 Weave；共享架构导入边界由仓库根 `.importlinter` 检查。

## 架构边界

架构分别由 [Pleiad 架构设计](docs/pleiad-architecture.md) 和 [Weave 架构设计](docs/weave-architecture.md) 说明；发生冲突时遵守仓库根目录的 `AGENTS.md`。网页启动与操作维护在 [Web 操作文档](docs/weave-web.md)。架构图直接嵌入 Mermaid，独立图资源如有需要放在 `docs/assets/`。不保留旧 import/API 别名或双轨实现；修改接口时必须一起迁移调用方、测试和相关文档，不自动迁移或删除用户旧数据。

代码边界如下：

- `pleiad.models` 只负责模型协议、提供商和标准响应；
- `pleiad.tools` 只负责工具契约、能力库、加载器和运行时执行；
- `pleiad.skills` 负责 Skill 包、元数据和按需读取；脚本由 command Tool 执行，不授予执行权限；
- `pleiad.agent` 声明绑定具体模型客户端的 Agent、Tool/Skill refs 和输出契约，不导入 runtime；
- `pleiad.orchestration.graph` 负责通用 DAG 定义、校验和节点执行；Graph 节点通过 SDK Runner 或普通异步函数运行；
- `pleiad.runtime` 的公共 Runner 负责运行、权限、预算和生命周期，Weave 不绕过它调用 AgentLoop；
- `pleiad` 不依赖 `pleiad_weave`；Weave 的 `application`、`api`、`tui` 和 `cli` 不实现底层框架规则。
- 工作区 Python、Bash 和 Skill Script 只能通过 Sandbox backend 执行；不能在宿主进程动态 import 或静默回退到本地 subprocess。

新增能力使用稳定的 `code` 注册到加载器。能力 YAML 只包含 `name`、`code` 和 `description`。新的执行能力统一实现为工具，新的静态方法和领域知识统一实现为技能。

自定义 Tool 的示例见 [脚本](workspace/tools/text_statistics.py) 与 [JSON 绑定](workspace/tools/text_statistics.json)，能力的架构职责见 [Weave 架构设计](docs/weave-architecture.md#4-组件视图c3)。新增 Tool 时应同时提交实现、固定 `code` 注册、能力 YAML 元数据和确定性测试。

## 提交内容

一个变更应包含清晰的问题背景、实现范围和验证结果。涉及公开行为或稳定架构边界的变更，需要同步更新所属模块的架构文档。

建议的提交范围：

- 一个明确的问题或能力；
- 与实现对应的测试；
- 对外行为变化对应的文档；
- 不包含本地日志、Trace、执行记录、会话数据和模型密钥。

## 代码规范

- Python 版本固定为 3.12；
- 使用完整类型标注并通过严格类型检查；
- 导入语句统一位于模块顶部；
- 错误由所属 SDK 模块的 BusinessError/SystemFault 子类表达；Weave 使用 ErrorRecord 序列化产品错误；
- SDK 使用标准 logging 且不配置 handler；Weave 在统一组装点配置日志；
- 运行过程通过事件和 Trace 暴露给界面及诊断工具；
- 新的工具和技能使用稳定的能力代码并补充测试。

## 提交前验证

提交前执行：

```bash
uv sync --project packages/pleiad --locked --all-groups --all-extras
uv lock --project packages/pleiad --check
uv run --directory packages/pleiad --frozen ruff format --check .
uv run --directory packages/pleiad --frozen ruff check .
uv run --directory packages/pleiad --frozen mypy
uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'

uv sync --project apps/weave --locked --all-groups --all-extras
uv lock --project apps/weave --check
uv run --directory apps/weave --frozen ruff format --check .
uv run --directory apps/weave --frozen ruff check .
uv run --directory apps/weave --frozen mypy
uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'
uv run --project apps/weave --frozen lint-imports --config .importlinter
uv run --project apps/weave --frozen pre-commit run --all-files

npm --prefix apps/weave/web ci
npm --prefix apps/weave/web run format:check
npm --prefix apps/weave/web run typecheck
npm --prefix apps/weave/web test
npm --prefix apps/weave/web run build
```

pre-commit 默认只检查，不自动格式化或修补文件。除 Ruff、strict mypy、import-linter 和两个 lock 外，还检查 JSON/YAML/TOML、冲突标记、大小写冲突、损坏链接、私钥和大文件；文件检查采用固定版本的 [官方 pre-commit-hooks](https://github.com/pre-commit/pre-commit-hooks/tree/v6.0.0)。

`--all-files` 面向 Git 文件集合。新增文件尚未跟踪时，仍须直接运行上面的全目录检查，并用 `pre-commit run --files <路径...>` 验证新增文件，不能把未被收集当作通过。仅提交配置不会安装本机 Git hook；需要时由开发者自行执行 `uv run --project apps/weave --frozen pre-commit install`。

发行物验收单独执行 `uv run --directory apps/weave --frozen pytest -q -m packaging tests/packaging`：测试分别构建 SDK 和 App，检查 wheel 与 sdist 内容，再把两个 wheel 安装到干净环境，在仓库外验证。第三方依赖下载缓存可以复用，但不复用旧 dist 或 editable 安装；已有完整缓存时可加 `UV_OFFLINE=1`。缺依赖/网络属于基础设施失败，不是通过或自动 skip。

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
