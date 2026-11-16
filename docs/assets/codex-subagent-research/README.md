# Codex 子智能体源码调研附件

权威正文为 [源码调研报告](../../pleiad-codex-subagent-source-research.md)。本目录不包含 Codex 复制源码或 SDK 实现。

- `research.html`：离线阅读版，内嵌 6 张机制图。
- `diagrams/R01.mmd` 至 `R06.mmd`：正文提取的 Mermaid 图源。
- `diagrams/R01.svg` 至 `R06.svg`：对应矢量图。
- `source-manifest.json`：固定提交、66 个缓存源文件的路径、行数和 SHA-256；表示调研材料范围，不表示每行均审计。
- `diagram-validation.json`、`document-validation.json`：渲染与链接、图源一致性检查。
- `source-validation.json`：46 处固定提交源码引用的路径、行范围及引用解析检查。
- [本轮校验记录](verification.md)：实际命令、结果和未执行范围。

图表依据源码阅读归纳，不是 Codex 官方架构图。源码和测试只作阅读，未编译/运行 Rust 测试，也未验证供应商端到端行为。
