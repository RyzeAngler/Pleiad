# 持续协作架构方案阅读附件

权威正文为 [架构设计方案书](../../pleiad-agent-collaboration-architecture.md)。本目录保存其阅读与图表附件，不包含 SDK 实现。

当前为 1.1 源码校准评审稿；关联 [Codex 源码调研](../../pleiad-codex-subagent-source-research.md)及其独立 6 张机制图。

- `architecture.html`：离线阅读版，内嵌 16 张 SVG；可通过目录定位章节，无需网络。
- `diagrams/D01.mmd` 至 `D16.mmd`：从正文提取的 Mermaid 图源。
- `diagrams/D01.svg` 至 `D16.svg`：对应静态矢量图。
- `diagram-validation.json`：图表渲染器、数量、尺寸和文件大小。
- `document-validation.json`：正文摘要、图源一致性、本地链接与阅读版校验结果。

图表使用 Mermaid 11.12.1 与 Chromium 渲染，未向项目添加依赖。图中类型和接口均为设计提案。修改时先更新权威正文，再重新提取和渲染，避免阅读版与正文失配。校验结果只涉及文档和图表，不代表设计中的运行机制已经实现或通过验收。
