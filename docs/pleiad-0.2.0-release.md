# Pleiad 0.2.0

2026-11-12发布到[正式PyPI](https://pypi.org/project/pleiad/0.2.0/)。新增图片输入、历史图片重新授权、图片预算与上下文保留，以及原生图片生成/参考编辑客户端。适配范围、模型限制和升级约束见[多模态架构](pleiad-multimodal-design.md)。

SDK元数据和`pleiad.version.__version__`均为0.2.0；Weave主依赖、全部SDK extras及两个锁文件已同步。Weave自身版本保持0.1.0，本次只发布Pleiad。

## 发布前检查

以下命令从仓库根目录执行，均退出0。

| 命令 | 结果 |
| --- | --- |
| `uv sync --project packages/pleiad --locked --all-groups --all-extras` | SDK环境与锁文件一致 |
| `uv sync --project apps/weave --locked --all-groups --all-extras` | Weave环境使用本地0.2.0 |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 245 files already formatted |
| `uv run --directory packages/pleiad --frozen ruff check .` | All checks passed |
| `uv run --directory packages/pleiad --frozen mypy` | 249 source files，无错误 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 955 passed，13 deselected，1既有DashScope弃用warning，19.68s |
| `uv run --directory apps/weave --frozen ruff format --check .` | 83 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | All checks passed |
| `uv run --directory apps/weave --frozen mypy` | 85 source files，无错误 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 277 passed，3 deselected，86.41s |
| `uv run --directory apps/weave --frozen pytest -q -m packaging tests/packaging` | 2 passed，27.35s；wheel/sdist干净安装、版本及依赖元数据通过 |
| `uv run --project apps/weave --frozen pre-commit run --all-files --show-diff-on-failure` | 所有适用检查通过，包含锁文件、凭据检测及Web TypeScript；broken symlinks无适用文件跳过 |
| `git diff --cached --check` | 通过 |

同日版本升级前的真实火山测试已通过：2次看图对话、1次生成、1次参考编辑，三个provider_e2e用例各1 passed。版本升级只更改版本、依赖和文档，没有重新发起收费调用。精确命令和产物见[真实验证记录](pleiad-live-image-tests.md#2026-11-12-真实验证记录)。其他厂商真实服务、OCI及完整前端行为测试未在本次发布阶段运行。

## 构建和上传

发布产物位于本机`/private/tmp/pleiad-0.2.0-release.LQ5s02/`。实际执行：

```bash
uv build --project packages/pleiad --out-dir /private/tmp/pleiad-0.2.0-release.LQ5s02
uvx --from twine twine check /private/tmp/pleiad-0.2.0-release.LQ5s02/pleiad-0.2.0-py3-none-any.whl /private/tmp/pleiad-0.2.0-release.LQ5s02/pleiad-0.2.0.tar.gz
uv publish --no-config --trusted-publishing never --publish-url https://upload.pypi.org/legacy/ /private/tmp/pleiad-0.2.0-release.LQ5s02/pleiad-0.2.0-py3-none-any.whl /private/tmp/pleiad-0.2.0-release.LQ5s02/pleiad-0.2.0.tar.gz
```

构建成功，Twine两项PASSED，上传退出0。凭据由本机`.pypirc`传给发布子进程，未写入仓库、发行包或日志。wheel内全部SDK文件与源码逐字节一致；wheel的175个文件、sdist的182个文件均通过实际凭据扫描，不包含本地运行配置。

PyPI JSON API确认版本公开，两个远端文件SHA256与本地产物一致：

| 文件 | SHA256 |
| --- | --- |
| `pleiad-0.2.0-py3-none-any.whl` | `785b651e6986d74959451481b523361a3e38ce6f78bf050d8f8610a5e2756635` |
| `pleiad-0.2.0.tar.gz` | `ad3708b90564b67c1db1893713eae04b70c3c2e3c123f813988b88e310f04a1b` |

## PyPI安装验证

在仓库外创建独立环境，从正式PyPI下载0.2.0，未使用本地源码路径：

```bash
uv venv --python 3.12 /private/tmp/pleiad-0.2.0-release.LQ5s02/installed
uv pip install --python /private/tmp/pleiad-0.2.0-release.LQ5s02/installed/bin/python --index-url https://pypi.org/simple 'pleiad[volcengine]==0.2.0'
/private/tmp/pleiad-0.2.0-release.LQ5s02/installed/bin/python -I /private/tmp/pleiad-0.2.0-release.LQ5s02/smoke.py
```

安装25个包，运行时与distribution版本均为0.2.0。图片消息序列化往返、火山Chat绑定、原生图片生成和相同操作去重通过。该发布包验证只模拟外部HTTP，不计为新的真实provider_e2e。
