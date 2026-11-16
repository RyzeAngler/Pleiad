# Pleiad 0.3.1

2026-11-16 发布到[正式 PyPI](https://pypi.org/project/pleiad/0.3.1/)。本次补齐上下文压缩的公开生命周期事件，主 Run、专项 Subagent 和持续协作成员沿用同一持久化、订阅与重放链路。

## 变更范围

- 新增 `pleiad.events.context`，公开 `context.compaction.started/completed/failed/cancelled`。同一次压缩通过 `compaction_id` 关联，并携带当前 Run、Step、Agent 和真实父 Run 身份。
- 事件只在选定实际压缩动作后产生。普通预算检查、无实际缩减、单独复用摘要和常规超大工具结果引用化不产生压缩事件。
- 终态记录实际尝试与完成的策略、工作耗时、token 数及估算信息。完成事件包含摘要新增覆盖、历史保留与省略、工具结果引用及 ContextBlock 保留与裁剪范围；失败、取消没有最终请求时不补造结果数据。
- 压缩事件先持久化、再通知。终态通知阶段的取消不会产生第二个终态；协作投影保留类型化源事件和来源游标，实时消费与历史重放使用相同去重规则。
- 事件追加复用已经验证的历史对象，再校验日志不变量；SQLite 对仅追加一个事件的事务原子更新同一 JSON 日志，避免反复序列化整份历史。其他状态更新继续完整写入，原工具事件和 Session 历史保持原契约。
- SDK 元数据、运行时版本、两个锁文件、Weave 主依赖和 SDK extras、发行安装测试版本断言同步到 0.3.1。Weave 自身版本保持 0.1.0，本次只发布 SDK。

事件字段与接入示例见 [SDK README](../packages/pleiad/README.md)，职责与持久化边界见 [SDK 架构](pleiad-architecture.md)。

## 发布验证

以下命令从仓库根目录执行，最终均退出 0。运行环境为 macOS arm64、Python 3.12.13、uv 0.11.22。

| 命令 | 结果 |
| --- | --- |
| `uv lock --project packages/pleiad --offline` | 仅将本地 SDK 版本更新为 0.3.1 |
| `uv lock --project apps/weave --offline` | 仅将本地 SDK 版本更新为 0.3.1 |
| `uv sync --project packages/pleiad --locked --all-groups --all-extras` | SDK 环境与锁文件一致 |
| `uv sync --project apps/weave --locked --all-groups --all-extras` | 消费方使用本地 SDK 0.3.1 |
| `uv run --project apps/weave --frozen pre-commit run --all-files --show-diff-on-failure` | 锁文件、两份发行的 Ruff format/lint、strict mypy、9 条导入边界、Web TypeScript、凭据与文件检查均通过 |
| `uv run --project apps/weave --frozen pre-commit run --all-files --hook-stage pre-push --verbose` | SDK 1056 passed、13 deselected，38.84s；Weave 277 passed、3 deselected，96.21s；Web 74 passed |
| `uv run --directory apps/weave --frozen pytest -q -m packaging tests/packaging` | wheel 和 sdist 两种干净安装测试，2 passed，26.73s |
| `git diff --cached --check` | 通过 |

测试覆盖正常完成、摘要异常与空摘要、压缩后仍超窗、取消、截止时间取消、终态通知竞争、无需压缩、持久化后通知、订阅分离与续读、SQLite 重开重放、子 Run 关联与协作投影、图片导致的不连续摘要覆盖、未知图片 token 和观察者等待时间排除。

实现阶段的大输入压力回归曾超过原有 60 秒期限。事件日志追加优化后，原用例在不修改期限或断言的情况下单独运行得到 1 passed、52.99s；随后完整 Weave 回归及本次发布前回归通过。

SDK 有一条 DashScope Assistants API 弃用警告。pre-push 嵌套执行 SDK 命令时，uv 提示忽略与 SDK 目录不一致的父级 VIRTUAL_ENV，并使用正确的 SDK 环境。真实供应商与 OCI 检查按标记排除，broken symlinks 检查没有适用文件；这些结果不代表真实供应商或 OCI 端到端验证。

## 发行包与上传

发布产物与本机验证脚本保存在 `/private/tmp/pleiad-0.3.1-release.JL4rWY/`。实际执行：

```bash
uv build --project packages/pleiad --out-dir /private/tmp/pleiad-0.3.1-release.JL4rWY
uvx --from twine twine check /private/tmp/pleiad-0.3.1-release.JL4rWY/pleiad-0.3.1-py3-none-any.whl /private/tmp/pleiad-0.3.1-release.JL4rWY/pleiad-0.3.1.tar.gz
uv run --directory packages/pleiad --frozen python /private/tmp/pleiad-0.3.1-release.JL4rWY/verify_artifacts.py
uv run --directory packages/pleiad --frozen python /private/tmp/pleiad-0.3.1-release.JL4rWY/publish.py
uv run --directory packages/pleiad --frozen python /private/tmp/pleiad-0.3.1-release.JL4rWY/verify_remote.py
```

Twine 两项 PASSED。wheel 与 sdist 中的 185 个 SDK 源文件均与工作区逐字节一致。两个发行包及暂存的发布文件通过实际本机凭据扫描，共检查 413 个条目。发布脚本从本机 `.pypirc` 读取凭据，仅经环境变量传给 `uv publish` 子进程，上传到 `https://upload.pypi.org/legacy/`；凭据没有写入仓库、发行包或日志。

PyPI JSON API 的 SHA256 及从远端下载文件的 SHA256 均与本地产物一致：

| 文件 | SHA256 | PyPI 上传时间 UTC |
| --- | --- | --- |
| `pleiad-0.3.1-py3-none-any.whl` | `a921ae9907faa8d9cb765895678927be23c15e55a921aa0ef029ec93e1a7e98b` | 2026-11-16 11:35:23 |
| `pleiad-0.3.1.tar.gz` | `893c82a43b0739d10ef574160e95c399a7158c395454cd6df8ea6030ad35f7c1` | 2026-11-16 11:35:26 |

## 正式 PyPI 安装验证

本地 wheel 先在独立环境通过同一烟测。正式上传后，从 PyPI 禁用下载缓存安装，再以隔离模式执行：

```bash
uv venv --python 3.12 /private/tmp/pleiad-0.3.1-release.JL4rWY/installed
uv pip install --python /private/tmp/pleiad-0.3.1-release.JL4rWY/installed/bin/python --index-url https://pypi.org/simple --no-cache 'pleiad==0.3.1'
uv run --no-project --python /private/tmp/pleiad-0.3.1-release.JL4rWY/installed/bin/python python -I /private/tmp/pleiad-0.3.1-release.JL4rWY/installed_smoke.py
```

上传后首次解析尚未找到新版本；公开文件与哈希确认后重试安装成功，共安装 13 个包。distribution 与运行时版本均为 0.3.1，公开事件模块来自独立环境的 `site-packages`。烟测通过真实 Runner、Session 和预算计数器执行四次调用，验证同一压缩的 started/completed、输入 token 从 7 降到 3、摘要新增覆盖步骤 1 和 2、四个原始历史步骤完整保留，以及按游标重放终态；模型传输为确定性假实现，未调用真实供应商。
