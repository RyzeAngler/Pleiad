# 调研与架构文档校验记录

日期：2026-11-12。范围：文档、源码引用与图表；没有 SDK 实现变更。

实际执行命令（渲染和检查脚本保存在本次会话的临时目录，Node/Playwright 使用现有运行时；没有新增项目依赖）：

```bash
/Users/w/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node /tmp/pleiad-collaboration-architecture/render.mjs
/Users/w/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node /tmp/pleiad-collaboration-architecture/render-research.mjs
/Users/w/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node /tmp/pleiad-collaboration-architecture/verify.mjs
/Users/w/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node /tmp/pleiad-collaboration-architecture/verify-research.mjs
python3 /tmp/pleiad-collaboration-architecture/verify-sources.py
git diff --check
git status --short
```

结果：

- 架构稿 16 张、源码调研 6 张 Mermaid 全部解析并渲染为 SVG；图源与 Markdown 一致。
- 两份离线 HTML 的本地链接和目录锚点有效；1440px、820px 页面无水平溢出，无脚本错误。
- 抽看 R01、R04、D08 图的渲染，文字和连线可辨认；每图提供独立 SVG。
- 46 处源码引用全部使用固定提交，路径存在于源码缓存，行范围有效；61 处正文引用均可解析。
- `git diff --check` 无错误；其本身仅覆盖 Git 已跟踪差异，新 Markdown 的尾部空白和本地链接另由文档检查覆盖。
- Git 状态仅新增文档及附件；原任务书仍为原有未跟踪文件，SDK/Weave 源码和依赖没有修改。

结果文件：[架构文档检查](../agent-collaboration/document-validation.json)、[调研文档检查](document-validation.json)、[源码引用检查](source-validation.json)。

未运行：Codex Rust 编译/测试，Pleiad/Weave 实现质量门禁，真实供应商、OCI、打包和产品联调。源码中测试的断言仅作阅读证据，不报告为本次执行通过。
