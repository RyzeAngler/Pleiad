# Weave Web 验收指南

本指南用于检查当前界面的交互和视觉表现。使用说明见 [Web 使用指南](../../../docs/weave-web.md)，操作语义见 [Web 交互说明](../../../docs/weave-web-interactions.md)。

## 自动化检查

从仓库根目录执行：

```bash
npm --prefix apps/weave/web run format:check
npm --prefix apps/weave/web run typecheck
npm --prefix apps/weave/web test
npm --prefix apps/weave/web run build

uv run --directory apps/weave --frozen --no-sync ruff format --check .
uv run --directory apps/weave --frozen --no-sync ruff check .
uv run --directory apps/weave --frozen --no-sync mypy
uv run --project apps/weave --frozen --no-sync lint-imports --config .importlinter
uv run --directory apps/weave --frozen --no-sync pytest -q -m 'not packaging and not oci and not provider_e2e'
uv run --directory apps/weave --frozen --no-sync pytest -q -m packaging tests/packaging
```

打包检查在最后一次前端构建后运行。严格 editable 安装需要重新安装才能包含新增模块或更新的静态资源，安装期间不要同时运行测试。

## 浏览器检查

使用独立浏览器和隔离的演示数据；检查真实工作区时，不发送测试消息或恢复、取消用户任务。临时状态只能用于界面验证，不能当作后端执行成功的证据。

| 场景 | 检查内容 |
| --- | --- |
| 聊天与工作区往返 | 聊天入口隐藏右侧工作区；导航只高亮当前页面；草稿、附件、消息与工作区阅读位置保留 |
| 长目标补充 | 目标只占单行标签，正文可编辑；添加和移除目标均不发送消息或取消任务 |
| 输入与键盘 | 默认窄单行，多行仅增高；Enter、Shift+Enter、输入法组合和附件操作正确 |
| 过程分页 | 最新步骤在上；超过一页仍从最新开始；新步骤到达不移动旧阅读范围，主动查看最新后才切换 |
| 步骤与结果 | 工具结果显示在所属调用内；错误和未完成状态真实；展开状态与各自结果分页保留 |
| 资源详情 | 四类资源均可打开；原始字段完整，敏感值脱敏；复制与展示一致，关闭后焦点恢复 |
| 返回来源 | 从聊天产物或附件返回后，焦点回原入口，草稿不变；切回聊天时弹窗不残留 |
| 空间设置 | 修改配置后切换页面保留草稿；离开需要丢弃时提示，显式取消后继续编辑 |
| 桌面布局 | 1200、1440、1920 宽度无横向溢出；弹窗限制高度且可滚动；长路径和代码不撑破布局 |
| 网络和权限变化 | 断线后明确显示连接状态；失败不伪造成功，重新同步不会覆盖新草稿 |

检查控制台和失败的网络请求。对于演示响应不支持的路径，修正演示数据后重测，不把错误截入文档。

## README 截图

README 展示当前应用构建，由独立浏览器直接截图，使用本地演示数据，不包含真实聊天、私有路径或凭据。

- [主聊天](../../../docs/assets/weave-web-chat.png)：聊天入口、完整消息、结果入口与紧凑输入框。
- [执行过程](../../../docs/assets/weave-web-process.png)：分栏布局、倒序步骤和展开的调用结果。
- [资源详情](../../../docs/assets/weave-web-resources.png)：完整配置、掩码凭据和原始数据入口。

三张图采用相同的 1440×1000 CSS 视口，不拼接或修改截图。更新后核对两份 README 的图片引用，每个文件小于仓库 1 MiB 限制；删除已无引用的旧图。演示脚本、浏览器日志、临时截图和个人验证记录不进入提交。

## 清理与交付

关闭本次启动的测试浏览器与服务，清理属于本次验证的临时脚本和缓存。保留用户工作区数据、凭据、依赖环境和正在使用的严格 editable 构建目录。分别报告确定性测试、打包、真实模型、MCP、OCI 与实机验证的执行范围。
