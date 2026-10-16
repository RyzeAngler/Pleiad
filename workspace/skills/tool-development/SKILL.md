---
name: tool-development
description: 开发、设计、实现、校验并发布工作区中的自定义 Tool
---

# 自定义 Tool 开发

先确认输入、输出、授权文件范围、外部副作用和失败方式。内置 read_file、write_file、command 可以完成的工作直接使用现有工具。

自定义 Tool 在 tools/<name>.py 编写独立 Python 脚本，通过 JSON stdin 接收参数，通过 stdout 返回一个 JSON 对象。脚本不能依赖宿主 Python 环境；依赖必须预装在配置的容器镜像中。

在 tools/<name>.json 声明 script、input_schema 和 output_schema。能力目录 capabilities.yaml 只登记 name、code、description。工具名必须是 Python 标识符，脚本路径必须在 tools/ 内。

先用 command 在容器中验证正常输入、无效输入、路径边界和输出模式；不要在宿主导入工作区代码。通过 write_file 发布绑定和能力描述后，使用 search_capabilities 检索验证，新请求或图执行会读取最新目录。

Skill 使用小写连字符 code，目录名和 SKILL.md frontmatter name 一致。脚本存放于 skills/<code>/scripts/，使用 command 执行，无需另设脚本注册协议。
