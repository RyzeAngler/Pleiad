# 默认工作区

```text
workspace/
├── capabilities.yaml       # Tool / Skill metadata
├── tools/                  # Python JSON stdin/stdout scripts and bindings
├── skills/                 # Skill packages with standard frontmatter
├── work/<space_id>/<execution_id>/  # Worker task files
└── runtime-v2/
    ├── product.sqlite3     # Product entities, transactions and event journal
    ├── sessions.sqlite3    # SDK inputs, Runs, ModelSteps and checkpoints
    ├── content/            # Digest-addressed attachment and artifact bytes
    ├── facts/              # Explicitly authorized Space and user facts
    └── logs/               # Rotating diagnostic logs
```

模型配置与固定 OCI 镜像位于宿主 `.env`，不进入能力 YAML。每个 Execution、能力搜索和图绑定都会读取当前能力目录，无需重启。运行中的 SDK Run 保持已选择的引用集合。

旧 `runtime/` 作为旧版本资料保留，新版本不读取或自动迁移其中的数据。

默认 MCP 连接位于仓库根目录的宿主 `mcp.json`，涵盖网页搜索、浏览器、小红书、地图、
酒店门票、开发资料和业务应用；初始全部关闭，安装依赖或配置账号后逐项启用。
工作区提供文本统计、CSV 分析工具，以及旅游规划、网页研究、文档写作、数据审计、
工具开发、任务分析、根因诊断和完成验证 Skill，供 Agent 按需选择。
能力清单统一使用 name、code、description，许可证和来源保留在 Skill 包内。
默认不登记依赖尚未启用浏览器服务的网页测试 Skill。凭据保存在宿主环境或工作区外的 OAuth 目录，
不要写入此目录。配置与验收步骤见 [默认能力接入指南](../docs/weave-capabilities.md)。

自定义 Tool 的脚本通过 JSON stdin/stdout 交互，`tools/<name>.json` 声明 script、input_schema 和 output_schema。Skill 脚本直接使用 Pleiad command 执行。示例见 [脚本](tools/text_statistics.py) 和 [JSON 绑定](tools/text_statistics.json)，能力的架构职责见 [Weave 架构设计](../docs/weave-architecture.md#4-组件视图c3)。

运行数据不会打包到 Weave 发行物。OpenTelemetry spans 由宿主显式提供的 exporter 导出，产品事件 journal 和 SDK Session 都不承担 Trace 存储职责。
