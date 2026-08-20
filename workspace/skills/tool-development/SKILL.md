# 自定义 Tool 开发

当用户希望新增能力、实现一个 Tool 或让 Pleiad 扩展自己的能力时，按照本流程完成开发、校验和发布。

## 能力边界

Tool 负责执行真实动作，Skill 负责提供开发方法。自定义 Tool 继承 `BaseTool`，实现输入 Schema、输出 Schema 和执行逻辑。能力目录只发布 `name`、`code` 和 `description`。

实现文件位于 `workspace/tools/`，固定注册入口是 `workspace/tools/__init__.py`，能力元数据位于 `workspace/capabilities.yaml`。

## 工作流程

### 1. 理解需求

先确认 Tool 的目标、输入、输出、文件范围、外部副作用、权限和失败方式。已有内置 Tool 可以完成的工作直接使用内置 Tool，新增 Tool 只覆盖缺少的能力。

### 2. 设计 Tool 契约

为 Tool 确定稳定的 `code`。输入和输出都使用 JSON 对象，并为字段定义清晰的 JSON Schema。Tool 的返回值必须满足输出 Schema，错误交给运行时统一转换。

涉及文件时，使用工作区相对路径并限制路径范围。涉及网络、数据库或其他服务时，从环境变量或专用配置读取地址和凭据，运行参数不写入能力 YAML。

### 3. 编写实现

将实现放到 `workspace/tools/<tool_name>.py`，继承 `BaseTool`。实现 `descriptor`、`input_schema`、`output_schema` 和异步 `execute`。

`descriptor` 使用注册入口传入的 YAML 元数据，不在 Python 文件中复制一份描述。执行逻辑保持小而明确，输入校验依靠 Schema，业务安全边界由 Tool 自己实现。

### 4. 注册实现

在 `workspace/tools/__init__.py` 的 `register_tools` 中建立 `code` 到 Tool 工厂的绑定。注册代码与能力 YAML 中的 `code` 完全一致。

工厂只负责根据 descriptor 创建 Tool。模块加载阶段完成注册，实际 Tool 实例在 Agent 选择该能力后创建。

### 5. 先校验实现

先使用内置 `bash` 执行：

```bash
python -m compileall workspace/tools
ruff check workspace/tools
```

再使用最小输入运行 Tool，检查输入 Schema、输出 Schema、工作区边界和错误处理。实现、注册入口和校验都通过后，再发布能力 YAML。

### 6. 发布能力

在 `workspace/capabilities.yaml` 的 `tools` 中增加：

```yaml
- name: tool_name
  code: example.tool_name
  description: 说明这个 Tool 能解决什么问题
```

能力 YAML 是 Tool 的发布入口。新增 YAML 条目后，Runtime 会在下一次循环边界扫描工作区并尝试热加载。候选目录校验通过后，当前运行中的 Pleiad、后续 Node 和能力搜索都会使用最新目录。

### 7. 验证发布结果

再次搜索能力目录，确认新 Tool 能按名称、代码或描述被找到。随后用一个最小目标实际调用 Tool，确认返回结构和执行效果。

如果热加载发现文件处于编写中的中间状态，系统会保留上一份有效能力目录，并在下一次工作区变化后重新尝试。修复实现或注册后继续执行即可。

## 完成标准

完成后应确认：

- Python 实现位于 `workspace/tools/`；
- Tool 继承 `BaseTool` 并提供完整输入输出 Schema；
- `workspace/tools/__init__.py` 已注册稳定 `code`；
- `workspace/capabilities.yaml` 只有 `name`、`code`、`description` 三个字段；
- `compileall`、代码检查和最小运行测试通过；
- 能力搜索可以找到新 Tool；
- 最小真实调用返回符合 Schema 的结果。
