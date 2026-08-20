# 默认工作区

默认工作区只保存能力目录、静态技能和运行时数据：

```text
workspace/
├── capabilities.yaml       # Tool 和 Skill 的 name/code/description
├── tools/                  # 自定义 BaseTool 实现和固定注册入口
├── skills/                 # 静态 SKILL.md 内容，包含 Tool 开发流程
└── runtime/
    ├── logs/               # 按日期保存的日志，单文件 10 MB，最多保留 7 天
    ├── traces/             # 全链路追踪
    ├── executions/         # 执行摘要
    ├── graphs/             # 任务图版本
    ├── graph-runs/         # 任务图运行记录
    ├── node-runs/          # 节点运行记录
    ├── artifacts/          # 节点结构化输出
    └── memory/             # 会话上下文
```

全局模型配置位于仓库根目录的 `.env`，不写入能力 YAML。数据库地址、接口地址、凭据和连接状态属于 Tool 实现，也不写入能力 YAML。Skill 只提供静态提示词内容，不支持运行中连接配置。

自定义 Tool 的实现放在 `tools/`，由 `tools/__init__.py` 中的 `register_tools` 建立稳定 `code` 与 Tool 工厂的绑定，再在 `capabilities.yaml` 中发布同一 `code` 的三字段元数据。Runtime 会在每轮请求前检查变化并热加载最新的有效目录。完整流程见 [自定义 Tool 开发指南](../docs/custom-tools.md)。

`runtime/` 中的内容由本地运行自动生成，Git 只保留各目录的 `.gitkeep` 占位文件。
