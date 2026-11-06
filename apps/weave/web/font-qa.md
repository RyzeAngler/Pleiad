# MiSans 字体与排版验收

日期：2026-11-05。仅调整网页字体资源与排版，不改接口、数据或执行逻辑。

final result: passed

## 实现

| Before | After | Why |
| --- | --- | --- |
| 只声明 Inter，实际依赖系统回退 | 本地 MiSans Regular / Medium / Semibold | 保证换电脑后字形一致，不依赖外部字体网络 |
| 首页、设置标题 44px / 650 / 负字距 | 38px / 600 / 中文字距 0 | 降低厚重感，改善中文间距 |
| 设置标签 21px / 600，输入 20px | 标签 16px / 500，输入 17px / 400 | 标签与正文分层 |
| 导航 17–18px，Graph 标题 20px | 导航 16px，Graph 标题 18px | 统一桌面阅读密度 |

源文件：`src/fonts/misans/`、`src/main.tsx`、`index.html` 和四份现有 CSS（styles、Navigation、Settings、Workbench）。代码及资源标识沿用原等宽字体。字体来源固定为 misans@5.0.0（MiSans 4.009），168 个未修改 WOFF2 文件，共 5,850,208 bytes；没有新增 npm 或 Python 依赖。上游 Semibold CSS 字重由 650 映射为 600，字形未修改。来源与变更说明及完整字体许可随应用打包。

## 浏览器验收

使用独立 Chrome / Playwright，最终构建在 1487×1058 桌面与 390×844 窄屏检查。测试页面屏蔽所有非本机请求后正常加载；字体请求全部同源。空间设置测试内容实际按需读取 19 个字体分片、529,244 bytes，并非首次加载全部字体。

通过 Chrome CSS.getPlatformFontsForNode 确认中文标题实际使用自托管 MiSans-Semibold，而不只是检查 CSS 的字体名称。实际 8000 服务的工作区标题、空间名称及英文 Weave 标识也检查了实际渲染字体。浏览器中的中文、英文和数字加载正常。

临时验收截图已在验证后清理。

空间设置与资源库无横向溢出；窄屏保存按钮可滚动到达。编排预览打开、Esc 关闭及资源库导航正常。真实服务只读查看，未修改用户空间、任务或发送模型请求；Graph 与完整设置样本来自隔离内存 HTTP fixture。此次未重新跑真实提供商或 OCI 流程，窄屏检查不等同于手机实机。

## 命令与结果

均从仓库根目录执行。

| 命令 | 结果 |
| --- | --- |
| `npm --prefix apps/weave/web run typecheck` | 通过 |
| `npm --prefix apps/weave/web run format:check` | 通过 |
| `npm --prefix apps/weave/web test` | 87 passed，8 files |
| `npm --prefix apps/weave/web run build` | 通过，包含本地字体、NOTICE 与许可 PDF |
| `uv run --directory apps/weave --frozen ruff format --check .` | 84 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | 通过 |
| `uv run --directory apps/weave --frozen mypy` | 86 source files，通过 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 222 passed，2 deselected |
| `uv run --directory apps/weave --frozen pytest tests/packaging -q` | 首次 1 passed、1 failed；wheel 安装依赖超时 120s，尚未进入资源断言 |
| `UV_OFFLINE=1 uv run --directory apps/weave --frozen pytest tests/packaging -q` | 2 passed in 15.71s；使用缓存执行 wheel 与 sdist 的完整构建、隔离安装和资源校验 |
| `git diff --check` | 通过 |

扩展既有 `test_distributions_install_without_the_checkout_and_ship_only_product_resources[wheel/sdist]`，检查安装后的 CSS 字体 URL 均为本地资源、所有字体具备 WOFF2 文件头，以及许可 PDF、MiSans/Xiaomi 来源声明完整随包分发。没有修改或放宽断言以通过重试。SDK 代码未改动，未重跑 SDK 套件或 import-linter。
