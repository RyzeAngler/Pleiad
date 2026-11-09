# Weave Web 使用指南

## 启动

在仓库根目录安装所需模型依赖，填写宿主 `.env`，构建静态 UI，再启动唯一 HTTP Owner。

```bash
uv sync --project apps/weave --extra deepseek
npm --prefix apps/weave/web ci
npm --prefix apps/weave/web run build
uv run --project apps/weave --frozen pleiad serve --host 127.0.0.1 --port 8000
```

模型配置使用 `PLEIAD_LLM_PROVIDER`、`PLEIAD_LLM_PROTOCOL`、`PLEIAD_LLM_MODEL`、`PLEIAD_LLM_API_KEY` 和可选 base URL。视觉与语音角色由对应宿主变量配置。前端源码构建需要 Node.js 22.12+。

`PLEIAD_SANDBOX_IMAGE` 必须填写 Docker 或 Podman 中实际可用的完整镜像 digest。工作区必须位于容器引擎允许挂载的目录。先通过对应引擎的 `info` 命令检查连通性。

浏览器打开 `http://127.0.0.1:8000/`。`/healthz` 检查 HTTP 存活，`/readyz` 检查工作区 Owner 就绪，`/docs` 提供 OpenAPI。终端 `Ctrl+C` 触发服务清理。

这是本地单所有者部署。服务检查本地 Host、同源 Origin 和写请求的 CSRF token。CLI 和 TUI 连接同一个 Owner，可使用 `PLEIAD_SERVER_URL` 配置地址。

## 永久主聊天

没有有效 URL 路由时，应用默认进入居中的聊天视图。打开 Space、Execution、资源或内容后，切换为导航、左侧主聊天和右侧工作区的分栏布局。两种视图共用同一消息历史与唯一输入框。

点击 Weave 标识或主聊天图标会切换到聊天视图、隐藏工作区，并聚焦主输入框。聊天阅读位置与未读提示保留；回到最新消息使用“有新消息”或“回到底部”入口。隐藏的工作区继续保留当前对象、标签、展开内容、筛选和滚动状态，返回后继续查看。导航高亮由当前路由决定，浏览器前进与后退恢复对应视图。切换视图保留草稿、附件及关联标签。

普通消息由 Router 判断是直接回复、派发新工作，还是补充已有工作。点击 Execution 的补充操作，只在输入框上方添加可关闭的“补充：任务名称”标签并聚焦正文，保留已有草稿。长名称省略显示，完整名称可查看。点击“取消补充”只解除该目标关联，保留正文、附件与其他目标；添加和取消补充都不自动发送消息。只有明确点击发送或按 Enter 才提交。composer 不提供取消任务按钮，任务取消统一在工作详情操作。引用 Space、产物或 Graph 节点添加只读上下文。附件先上传，正式发送时与 Message 原子关联。

正式报告显示来源工作和实际产物卡。点击产物在右侧查看元信息与受控内容，主输入框保留当前草稿。

输入框在聊天区域底部居中，聊天视图最大 480px；分栏工作区中宽度约为聊天栏的 74%，最大 360px。默认一行，附件、正文和发送按钮同排；正文增长时只增加高度，清空后恢复单行。不显示功能备注、快捷键说明或常驻计数。输入框支持 Enter 发送、Shift+Enter 换行和中文输入法组合。尚未确认的请求保留稳定客户端键，重试返回原收据。连接恢复时先刷新 snapshot，再核验当前目标、附件和引用。

正文按服务公布的 UTF-8 字节上限校验，达到上限的 90% 时显示接近上限提示，超限后提示删减并禁用发送。已添加附件显示为文件卡片。

每张 Attention 卡标明所属 Space 和真实工作目标，并提供来源工作的详情入口。同文问题按各自的持久身份分别处理。Attention 的回答进入同一个主输入框。发送通过 resolve 接口保存一条用户 Message 和回答，审批使用明确的批准或拒绝操作，并绑定当前 action digest。暂停中的 Execution 收到回答后仍由当前控制意图决定何时继续。

## Space 与 Execution

Space 保存名称、用途、长期指令、模型角色和资源选择。设置保存使用 expected revision，编辑期间显示未保存状态。归档限制新工作，恢复后可重新派发。删除先查看服务端预览，确认后显示清理进度与阻塞原因。

Execution 是持久工作身份。概览展示状态、目标、结果、预算、待办与子任务。暂停、继续、取消和重试按钮由服务端 `available_actions` 决定。重试保存来源 Execution，新 Run 延续产品的明确输入与预算规则。

过程视图以时间线展示真实步骤，用“阅读任务要求”“执行命令”等动作名称组织内容。步骤按 `run_sequence` 从新到旧展示，最新步骤默认展开；其余按需展开后才加载所属结果。调用记录按 `attempt` 倒序，默认选择 API 返回的全局 `latest_run_id`，超过一页也能定位最新调用。最新调用尚未取得 SDK Run 时，该值为 null，显示等待状态，不回退到旧的已完成调用。

调用和步骤均先全局倒序再分页，以排他的 `before_sequence` 读取更早记录。首次读取最新步骤后固定当前上界，实时刷新保留当前范围、展开状态和阅读位置；有新增步骤时显示“查看最新进展”，点击后再切换到最新范围。“查看更早步骤”使用 `next_before_sequence`，新增步骤不会挤动历史页。GraphRun 与节点尝试仍有各自独立的选择和分页。

任务正文按 Markdown 排版，命令与输出保持原文，工具包装和运行时标识不进入普通页面。调用结果与后台最终结果仍归于各自实际调用，并独立分页。模型步骤完成和后台操作完成分别显示，未返回、取消及失败结果保留真实状态。Graph 画布使用真实边和节点状态；没有 Graph 时不显示空选择器。此展示顺序不改变 SDK 中消息和步骤的原始顺序。

材料与产物保留来源和归属。图片、PDF、文本与 Markdown 由右侧内容视图预览，下载经服务端摘要校验。文本预览具有大小上限。

资料标签展示工作说明、中文工具与技能名称、收到的要求及实际状态。送达、助手读取和处理责任是独立事实，分别更新。失败发送的错误和重新发送按钮留在原要求下面。配置版本、模型角色代码、读取位置、原始 ID、投递代次及功能介绍不在普通界面展示，也不另设技术详情折叠。

## 资源与配置

资源目录展示模型、Tool、Skill 和环境的摘要，所有类型均可点击打开详情。详情读取完整 `ResourceDetail`，按基本信息、配置、输入参数、返回内容、技能原文和文件列表展示实际存在的内容，并提供完整原始数据及复制入口。名称、代码、可用性、说明、配置位置和原始字段均保留；不添加功能介绍或缺失版本的占位说明。“已配置”仅表示存在配置，不代表已完成模型连接验证。

密钥、令牌、密码、认证头及 URL 内凭据在服务端生成响应前加掩码；详情正文、原始数据和复制结果共用脱敏后的数据，浏览器不会收到这些凭据明文。schema 的字段名称、类型及结构保留，敏感示例和默认值掩码。Space 设置只选择当前宿主允许的资源。模型密钥、安装、MCP 凭据和连接配置由宿主管理；查看详情与复制信息不修改授权。

`mcp.json` 提供 HTTP 和 stdio 连接选择、允许工具列表和凭据变量。OAuth 通过 `pleiad mcp-login SERVER_CODE` 在终端授权。接入步骤见 [能力指南](weave-capabilities.md)。

设置页展示只读宿主摘要和本地显示偏好。默认产品数据根目录是 `workspace/runtime-v2/`，本地事实保存在其 `facts/` 目录。Worker 的文件和命令工具使用 `workspace/work/{space_id}/{execution_id}/`，附件与产物通过受控 `execution_content` 入口读取。显式选择共享 workspace 资源后，可用 `workspace_resource` 只读查询并 materialize 资料。

可选本地目录观察源使用 `PLEIAD_OBSERVE_DIRECTORY` 配置。读取配置与 observation 分页相互独立，显式 observe 请求提交去重元数据。健康查询检查目录访问状态。

## HTTP 契约

| 路由 | 用途 |
| --- | --- |
| `GET /v1/snapshot` | 一致快照、全局水位与浏览器写 token |
| `GET/POST /v1/chat/messages` | 永久主聊天分页和原子提交 |
| `GET /v1/events` | 全局序列 SSE 回放 |
| `GET /v1/spaces`、`GET /v1/spaces/{id}` | Space 列表与详情 |
| `PATCH /v1/spaces/{id}` | 带 revision 和 request ID 的设置修改 |
| `POST /v1/spaces/{id}/archive`、`restore` | Space 生命周期 |
| `GET /v1/spaces/{id}/deletion-preview`、`DELETE /v1/spaces/{id}` | 删除预览与确认 |
| `GET /v1/executions`、`GET /v1/executions/{id}` | 工作列表与详情 |
| `GET /v1/executions/{id}/tool-results/{result_id}` | `view=display` 先从完整结果提取可读内容，再按 UTF-8 字节分页；`view=raw` 保留诊断表示。两者执行同样的归属校验 |
| `GET /v1/executions/{id}/runs` | 按 attempt 倒序，使用 before_sequence 加载更早调用；latest_run_id 标识全局最新调用的 SDK Run，尚未启动时为 null |
| `GET /v1/executions/{id}/runs/{run_id}` | 所有权校验后的 Run 详情，步骤按 run_sequence 倒序且使用 before_sequence 分页；operations 提供当页实际后台执行状态，tool_results 提供真实结果引用 |
| `POST /v1/executions/{id}/pause`、`resume`、`cancel`、`retry` | 工作控制 |
| `GET /v1/attentions`、`GET /v1/attentions/{id}` | 用户待办 |
| `POST /v1/attentions/{id}/resolve` | 原子回答或审批 |
| `POST /v1/attachments` | 有界 multipart 上传 |
| `GET /v1/artifacts/{id}`、`/content` | 产物元数据与内容 |
| `GET /v1/resources`、`GET /v1/resources/{kind}/{code}` | 只读资源目录摘要与完整 ResourceDetail；详情字段在服务端统一脱敏 |
| `GET /v1/settings` | 只读宿主摘要 |
| `/v1/sources` | 显式配置的只读观察源 |

写请求从 snapshot 的 `capabilities.csrf_token` 取得 token，并放入 `X-Weave-CSRF`。SSE 使用 `event: product` 和全局 `id`，正式消息事件携带 MessageView，可按序直接合并正文、来源和产物元数据。`resync` 要求重新读取 snapshot。浏览器断开后，服务继续拥有 Execution。

Run 列表和步骤采用 `SequencePage`：`items`、`total`、`next_before_sequence`、`latest_sequence` 与 `read_sequence`。`before_sequence` 为排他上界，省略时读取最新页；`latest_sequence` 不受当前页限制，`next_before_sequence` 为 null 表示没有更早记录。两处不再使用 offset cursor。GraphRun、节点及其他资源继续遵循各自分页合同。`ResourceDetail` 在目录摘要字段外包含 `metadata`、`configuration`、`input_schema`、`output_schema`、`content` 和 `files`。

产品事件到达后，客户端在已加载的历史范围内，按有界消息页重读可能变化的责任、报告与产物投影。每条消息按产品读取序列合并，新缓存代次丢弃旧请求结果，历史消息的归属状态与清理标记随服务事实更新。重读保留聊天阅读锚点、草稿和右侧选择。

错误返回稳定 code、message、request_id 和 retryable，revision 冲突还返回 current_revision。客户端应保留当前输入并刷新对象，确认后再发起新操作。

## 开发与排查

```bash
npm --prefix apps/weave/web run dev
npm --prefix apps/weave/web run format:check
npm --prefix apps/weave/web run typecheck
npm --prefix apps/weave/web test
npm --prefix apps/weave/web run build
```

Vite 开发服务器将 `/v1`、`/healthz` 和 `/readyz` 代理到本地 HTTP Owner。安装分发包使用打包静态资源。构建产物位于 `apps/weave/src/web/assets/`。

页面提示静态资源缺失时重新运行 build 并重启服务。readyz 返回 503 时检查 Owner 锁、恢复日志与配置。工具失败时检查实际错误码、Docker/Podman 连通性、镜像 digest 和挂载目录。MCP 失败时检查明确启用的连接、宿主凭据变量和允许工具列表。

确定性测试、浏览器交互验收、真实模型、远程 MCP 和 OCI 引擎验收分别记录。

交互行为矩阵见 [Web 交互说明](weave-web-interactions.md)，浏览器验收和截图更新步骤见 [前端验收指南](../apps/weave/web/design-qa.md)。
