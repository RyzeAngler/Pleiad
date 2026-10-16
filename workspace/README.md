# 默认工作区

```text
workspace/
├── capabilities.yaml       # Tool / Skill 的 name、code、description
├── tools/                  # Python JSON stdin/stdout 脚本与同名 JSON 绑定
├── skills/                 # 带标准 frontmatter 的 Skill 包
└── runtime/
    ├── executions/         # Execution 与顺序事件的 SQLite journal
    ├── graphs/             # 不可变图版本
    ├── graph-runs/         # 图执行产品记录
    ├── node-runs/          # 节点尝试记录与 SDK Run 关联
    ├── artifacts/          # 节点 JSON 结果
    ├── memory/             # SDK ShortMemory SQLite 与授权事实 JSONL
    ├── scratch/            # 容器命令和 Skill 生成的临时产物
    └── logs/               # 按大小轮转的程序日志
```

模型配置与固定 OCI 镜像位于宿主 `.env`，不进入能力 YAML。每个 Execution、能力搜索和图绑定都会读取当前能力目录，无需重启。运行中的 SDK Run 保持已选择的引用集合。

自定义 Tool 的脚本通过 JSON stdin/stdout 交互，`tools/<name>.json` 声明 script、input_schema 和 output_schema。Skill 脚本直接使用 Pleiad command 执行。示例见 [脚本](tools/text_statistics.py) 和 [JSON 绑定](tools/text_statistics.json)，能力的架构职责见 [Weave 架构设计](../docs/weave-architecture.md#4-组件视图c3)。

运行数据不会打包到 Weave 发行物。OpenTelemetry spans 由宿主显式提供的 exporter 导出，事件 journal 和 SDK ShortMemory 都不承担 Trace 存储职责。
