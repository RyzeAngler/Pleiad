# Pleiad 0.3.0

2026-11-16 发布到[正式 PyPI](https://pypi.org/project/pleiad/0.3.0/)。本次发布包含持续 Agent 协作、Run 无上限预算维度、单次响应输出上限，以及图片历史压缩修复。

## 变更范围

- 新增 `CollaborationGroup`、成员 endpoint、宿主准入回调、协作工具、内存与 SQLite 存储。成员身份和 Session 跨工作轮次保留，支持定向消息、followup、中断、等待、关闭及独占恢复。
- `RunLimits` 和 `HostPolicy` 的执行时长、模型步数、累计输出预算支持 `None`。新增独立的 `max_response_output_tokens`，默认 8,192，随调用身份、父子策略和 checkpoint 传播。
- 图片历史压缩保留图片步骤，并将其间被省略的完整文本步骤纳入摘要。Session 更新跳过即将被替换字段的重复序列化，继续校验完整状态。
- SDK 元数据、运行时版本、两个锁文件、Weave 主依赖和全部 SDK extras、打包测试版本断言同步到 0.3.0。Weave Worker 创建、恢复和专项 Subagent 显式传递单次响应上限。

持续协作的接口和宿主责任见[交付说明](pleiad-agent-collaboration-handoff.md)。

## 发布前检查

以下命令从仓库根目录执行，最终均退出 0。运行环境为 macOS arm64、Python 3.12.13、uv 0.11.22。

| 命令 | 最终结果 |
| --- | --- |
| `uv sync --project packages/pleiad --locked --all-groups --all-extras` | SDK 环境与锁文件一致 |
| `uv sync --project apps/weave --locked --all-groups --all-extras` | 消费方使用本地 SDK 0.3.0 |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 261 files already formatted |
| `uv run --directory packages/pleiad --frozen ruff check .` | All checks passed |
| `uv run --directory packages/pleiad --frozen mypy` | 264 source files，无错误 |
| `uv run --directory packages/pleiad --frozen mypy examples/collaboration.py` | 1 source file，无错误 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 1017 passed，13 deselected，1 warning，42.70s |
| `uv run --directory apps/weave --frozen ruff format --check .` | 83 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | All checks passed |
| `uv run --directory apps/weave --frozen mypy` | 85 source files，无错误 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 277 passed，3 deselected，105.78s |
| `uv run --directory apps/weave --frozen pytest -q -m packaging tests/packaging` | 2 passed，30.39s，wheel 和 sdist 干净安装后执行协作示例 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 9 kept，0 broken |
| `uv run --project apps/weave --frozen pre-commit run --all-files --show-diff-on-failure` | 所有适用检查通过，包含锁文件、凭据检测、类型检查及 Web TypeScript |
| `uv run --directory packages/pleiad --frozen python examples/collaboration.py` | Root succeeded，3 个 terminal Work，3 个不同 Run |
| `git diff --cached --check` | 通过 |

初次 Weave 回归为 2 failed、275 passed。大输入 Router 超过 60 秒时限，性能分析定位到 Session 更新中被覆盖字段的重复序列化。取消用例发现新单次响应默认上限影响原产品预算约定，已在 Weave 的 Worker 和专项 Subagent 绑定处显式传递额度。两个原失败用例在修复后合并执行得到 2 passed，68.77s，随后完整回归通过。

SDK warning 来自已安装 DashScope 包的 Assistants API 弃用提示。SDK 的 13 项 deselected 按标记排除真实供应商及 OCI 检查，Weave 的 3 项 deselected 包含单独运行的 packaging 和 OCI 选集。pre-commit 的 broken symlinks 检查没有适用文件。本次未运行真实供应商、OCI、完整前端行为或吞吐压测。

## 构建与发布

产物和本机验证脚本位于 `/private/tmp/pleiad-0.3.0-release.4IljlZ/`。实际执行的构建和元数据检查命令如下。

```bash
uv build --project packages/pleiad --out-dir /private/tmp/pleiad-0.3.0-release.4IljlZ
uvx --from twine twine check /private/tmp/pleiad-0.3.0-release.4IljlZ/pleiad-0.3.0-py3-none-any.whl /private/tmp/pleiad-0.3.0-release.4IljlZ/pleiad-0.3.0.tar.gz
uv run --directory apps/weave --frozen python /private/tmp/pleiad-0.3.0-release.4IljlZ/verify_artifacts.py
uv run --directory packages/pleiad --frozen python /private/tmp/pleiad-0.3.0-release.4IljlZ/publish.py
uv run --directory packages/pleiad --frozen python /private/tmp/pleiad-0.3.0-release.4IljlZ/verify_remote.py
```

Twine 两项 PASSED。wheel 内全部 183 个 SDK 源文件与工作区逐字节一致，sdist 同样通过源码核对。工作区和两个发行包通过本机凭据扫描。

发布脚本从本机 `.pypirc` 读取凭据，仅经环境变量传给 `uv publish` 子进程，上传到 `https://upload.pypi.org/legacy/`。上传退出 0，PyPI JSON API 随后确认两个文件公开且哈希一致。首次公开查询返回 404，索引刷新后复查通过。

| 文件 | SHA256 | PyPI 上传时间 UTC |
| --- | --- | --- |
| `pleiad-0.3.0-py3-none-any.whl` | `aae81b39c6b101fb1bfdb84a62433c7a3530b2e061822e3b7591d04180508abd` | 2026-11-16 07:54:32 |
| `pleiad-0.3.0.tar.gz` | `528d7e1f87ef2cdf5a1869dfa21462261b532931aacfcce91714cc8e74de3bc8` | 2026-11-16 07:54:38 |

## 正式 PyPI 安装验证

在仓库外创建独立 Python 3.12 环境，并通过正式 PyPI 索引禁用本地下载缓存安装。

```bash
uv venv --python 3.12 /private/tmp/pleiad-0.3.0-release.4IljlZ/installed
uv pip install --python /private/tmp/pleiad-0.3.0-release.4IljlZ/installed/bin/python --index-url https://pypi.org/simple --no-cache 'pleiad==0.3.0'
uv run --no-project --python /private/tmp/pleiad-0.3.0-release.4IljlZ/installed/bin/python python -I /private/tmp/pleiad-0.3.0-release.4IljlZ/installed_smoke.py
```

上传刚结束时，索引暂未列出 0.3.0，首次安装解析失败。公开版本查询通过后重试安装成功，共安装 13 个包，包含 Pydantic 2.14.3。distribution 与运行时版本均为 0.3.0，协作模块来自该独立环境的 `site-packages`，Run 无上限维度及单次响应默认值验证通过。离线协作示例得到 Root succeeded、3 个 terminal Work 和 3 个不同 Run，进程退出 0。
