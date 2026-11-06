# Weave 网页交互与验收

日期：2026-11-04。当前范围：工作区首页、顶栏导航、对话创建空间、空间任务对话、Graph 画布、独立空间设置、全局资源库和执行控制。

## 当前操作路径

**工作区 → 创建或选择空间 → 新任务 → 对话 → 按需展开 Graph。**

工作区管理可复用空间，空间内的每个任务使用独立 conversation；每次消息提交仍对应真实 Execution。普通问答不要求生成 Graph。草稿阶段整理的是长期配置，只有点击「创建并进入」才保存空间。

旧侧栏和表单式创建入口已替换为顶栏、空间切换面板和 `SpaceCreator`。同一状态下不重复提供新任务或画布控制；所有执行状态均来自后端记录。

## 控件与状态契约

| 区域 | 控件 | 可观察行为 |
| --- | --- | --- |
| 顶栏 | Weave 标识 | 返回工作区首页。 |
| 空间操作区 | 当前空间名称 | 打开只含空间名称的切换面板；已有任务名称单独显示，不放进空间选择按钮。 |
| 顶栏 | 搜索 | 默认搜索全部空间任务，按任务标题和空间名称匹配；结果标明空间，选中后切换到正确会话。 |
| 搜索 | 清除、方向键、Enter | 清除后恢复完整列表；方向键移动任务焦点，输入框 Enter 打开首个匹配任务。 |
| 搜索 | 加载、空结果、错误与重试 | 分别显示，读取失败不会伪装成空目录。 |
| 空间操作区 | 空间设置 | 独立打开当前空间设置，不放进空间切换菜单。 |
| 空间操作区 | 新任务 | 仅在已有任务中显示；回到所属空间的输入页，发送前不创建空会话。 |
| 顶栏 | 资源库 | 打开全局目录，不附带空间配置；从设置进入时返回设置，从任务进入时返回原任务并保留对话输入。 |
| 导航面板 | 关闭、外部点击、Esc、Tab | 关闭恢复入口焦点；打开期间约束键盘焦点。 |
| 工作区 | 空间卡片、最近任务 | 分别进入空间新任务页或直接恢复对应任务；卡片显示任务概况，不展示默认指令。 |
| 创建空间 | 描述用途、发送 | 调用 `/v1/spaces/draft`，展示模型回复和可编辑预览；不会写入空间。 |
| 创建空间 | 名称、默认指令、调整资源偏好 | 默认指令可留空；修改当前草稿后继续对话会携带这些修改。偏好只来自已配置目录。 |
| 创建空间 | 创建并进入 | 确认四字段配置并调用 `/v1/spaces`；有未发送补充内容或正在生成草稿时不可创建。 |
| 创建空间 | 关闭、外部点击、Esc | 放弃未保存草稿，取消正在生成的请求；保存提交期间禁用关闭和重复操作。 |
| 创建空间 | 错误与重试 | 失败保留输入与已成功生成的草稿，不伪造配置、不自动保存。 |
| 新任务 | 输入、发送、Enter | 创建 conversation 并提交 Execution。Shift+Enter 换行，输入法 Enter 不误提交。 |
| 对话 | 提交失败后重试 | 保留输入；已创建会话继续复用，不增加空会话。 |
| 无 Graph 对话 | 消息区域、底部输入 | 中央阅读布局，不显示空画布、版本或缩放按钮。 |
| 有 Graph 对话 | 展开对话、收起对话 | 底部浮层与完整历史切换，保留输入内容。 |
| 对话 | 查看执行记录 | 切换指定 Execution 的结果、Graph 和错误；不把不同执行的状态混合。 |
| 对话 | 停止当前执行 | 只在存在可取消执行时显示，提交对应 execution_id；完成或取消后移除。 |
| 对话 | 工具调用与工具结果 | 原生详情展开查看实际参数和内容，不新增替代执行操作。 |
| 审批 | 参数、批准执行、拒绝 | 显示真实待审批动作并提交对应 interruption 的决定；冲突显示错误。 |
| Graph | 横向节点、曲线、配置资源、依赖步骤 | 展示当前版本的任务、绑定资源和入边；资源绑定与实际调用分开。 |
| Graph | 版本与保存空间编排 | 保存精确版本引用；切换版本不借用其他版本的节点结果。 |
| Graph | 节点点击、Enter、空格、Esc | 打开或关闭节点详情，查看输入、输出、指令、结构和错误。 |
| Graph | 询问节点详情 | 回到可见对话并填入问题，发送前不调用模型。 |
| Graph | 放大、缩小、适配视口、收起 | 调整视图；收起后可从对话展开编排。 |
| Graph | 编排、产物、过程 | 显示对应定义、服务器产物和事件记录，可下载真实 JSON 结果。 |
| 空间设置 | 名称、默认指令、保存 | 名称必填，默认指令可空；无有效修改时禁止保存，保存中锁定编辑，失败保留草稿。 |
| 空间设置 | 选择资源、搜索、移除已选标签 | 在真实 Tools／Skills 目录中选择优先资源；切换搜索保留选择，保存后作用于后续提交。 |
| 空间设置 | 查看已保存编排 | 调用 `/v1/spaces/{space_id}/saved-graph` 预览保存的精确版本，不提交 Execution、不运行节点。 |
| 空间设置 | 取消默认编排 | 修改配置引用，保存后作用于后续任务；不删除历史 Graph。 |
| 空间设置 | 返回、离开确认 | 回到原空间或任务并保留对话输入；有未保存修改时确认离开，取消离开保留草稿。关闭页面也有未保存提醒。 |
| 空间设置 | 保存期间的全局导航 | 暂时阻止离开并显示保存中提示；保存失败后错误可见，草稿可继续编辑和重试。 |
| 保存编排预览 | 读取失败、过期引用 | 一般读取失败可重新加载；引用已过期时提示关闭并刷新设置，不反复重试旧引用。 |
| 资源库 | 模型／Tools／Skills、搜索、详情 | 按类别读取真实目录并筛选资源；目录加载、空结果和读取失败分开显示，失败可重试。 |
| 资源库 | 配置位置、复制路径 | 展示真实工作区和能力清单路径；复制成功与失败均给出反馈，不在线修改配置。 |
| 错误 | 诊断详情、重新加载 | 保留错误上下文，允许重读服务状态。 |

没有单节点执行、拖线编辑、删除节点、上传素材、自动安装资源或自动发布等占位按钮。资源库也没有在线 MCP 连接或模型编辑入口；模型与服务密钥仍由宿主配置管理，资源偏好不是权限开关。

默认指令按每个 Execution 保存提交时的快照，后续设置修改不改变运行中任务。主 Agent 和所有 Graph Agent 节点以用户级上下文接收本次任务与该快照；节点业务输入和保存的 Graph 定义不被修改。空白默认指令不注入，明确的本次任务要求优先，默认指令不替代权限或审批策略。

## 回归用例索引

前端测试使用真实 React 组件，在 HTTP／EventSource 边界提供受控响应；后端使用真实应用服务、Pleiad Runner 与存储，仅替换外部模型提供商。组件测试和传输测试不等同于真实模型或 OCI 端到端验收。

| 行为 | 对应测试 |
| --- | --- |
| 任务中的跨空间搜索 | `searches across spaces from a selected task and opens a result in its owning space` |
| 空间切换与设置入口分离 | `keeps the space switcher limited to space names and opens settings independently` |
| 新任务不提前落库 | `starts a new task in the same space without creating a conversation until a message is sent` |
| 对话创建允许空默认指令 | `creates a space from a conversation draft with empty default instructions` |
| 草稿失败可重试 | `keeps a failed space description available for retry without creating records` |
| 无 Graph 维持对话视图 | `keeps a task without graphs in conversation view while switching its execution results` |
| 节点配置与依赖直接可见 | `shows configured resource names and upstream objectives on the graph itself` |
| 图版本与节点结果隔离 | `saves the selected graph revision without showing another revision's node output` |
| 草稿多轮修改、保存边界 | `test_space_draft_can_be_refined_without_creating_a_space_or_task` |
| 无效模型草稿拒绝保存 | `test_invalid_space_draft_never_becomes_a_saved_configuration` |
| 模型异常安全反馈 | `test_space_draft_provider_failure_has_a_safe_error_and_no_saved_space` |
| 草稿没有工具写入权限 | `test_drafting_cannot_write_files_even_when_the_model_requests_a_tool` |
| 断连取消模型请求 | `test_leaving_space_creation_cancels_the_running_model_request` |
| 超时不留下部分空间 | `test_space_draft_deadline_returns_timeout_without_a_partial_space` |
| 删除旧创建字段并校验资源 | `test_create_space_rejects_removed_fields_and_unconfigured_resource_preferences` |
| 无效偏好不覆盖已保存配置 | `test_invalid_resource_preferences_do_not_replace_existing_space_settings` |
| 空默认指令保存 | `saves empty default instructions with the observed revision` |
| 取消离开保留设置草稿 | `keeps unsaved space edits when leaving is cancelled and discards them only after confirmation` |
| 资源库返回原设置 | `returns from the global resource library to space settings when opened there` |
| 只读资源目录与详情 | `filters actual resource metadata and opens details without space preferences` |
| 配置路径与复制 | `shows actual configuration paths and copies the chosen path` |
| 所有节点接收提交时指令及本次任务 | `test_every_graph_node_receives_execution_defaults_without_model_input_copying` |
| 清空默认指令后不再注入 | `test_cleared_default_instructions_are_not_sent_with_the_next_task` |
| 空指令草稿可确认创建 | `test_space_draft_without_default_instructions_can_be_created` |
| 保存版本预览与复用均不改用最新图 | `test_new_space_task_reuses_saved_graph_revision_after_restart_and_later_graph_update` |
| 无空间或无保存图时预览返回 404 | `test_saved_graph_preview_returns_not_found_without_starting_a_task` |
| SDK 节点参考上下文与输入、记忆分离 | `test_agent_node_references_reach_model_without_changing_input_contract_or_stored_turn` |

前端测试位于 `apps/weave/web/src/`；空间后端测试位于 `apps/weave/tests/test_web_workspace.py`。SDK 既有事件顺序回归位于 `packages/pleiad/tests/orchestration/graph/test_graph.py`。

## 本轮验证

2026-11-04，默认指令与空间设置改造的自动检查：

| 命令 | 结果 |
| --- | --- |
| `uv run --directory apps/weave --frozen ruff format --check .` | 84 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | 通过 |
| `uv run --directory apps/weave --frozen mypy` | 86 source files，通过 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 222 passed，2 deselected |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 210 files already formatted |
| `uv run --directory packages/pleiad --frozen ruff check .` | 通过 |
| `uv run --directory packages/pleiad --frozen mypy` | 214 source files，通过 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 375 passed，2 deselected；1 条既有 DashScope 弃用警告 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `npm --prefix apps/weave/web run typecheck` | 通过 |
| `npm --prefix apps/weave/web run format:check` | 通过 |
| `npm --prefix apps/weave/web test` | 87 passed，8 files |
| `npm --prefix apps/weave/web run build` | 通过，已生成静态资源 |
| `uv run --directory apps/weave --frozen pytest tests/packaging -q` | 最后一次前端构建后运行：2 passed in 14.24s |

### 本轮浏览器验收

临时验收截图已在验证后清理。

使用独立浏览器检查最终构建。实际 `127.0.0.1:8000` 服务已经重新启动，确认加载新版页面；在真实用户工作区仅检查空间菜单、独立设置及资源库的只读导航，没有修改用户数据或调用模型。

编辑与保存操作使用 `127.0.0.1:18081` 的隔离内存 HTTP fixture，验证真实前端和 HTTP 交互：

| 路径 | 已观察结果 |
| --- | --- |
| 默认指令 | 清空后可保存，重新读取仍为空。 |
| 空间资源偏好 | 可选择、移除并保存资源。 |
| 全局资源库 | 模型／Tools／Skills 分类、搜索、详情与配置位置可用；返回入口回到原设置页。 |
| 保存编排 | 预览保存的精确 Graph 版本，可查看节点指令、缩放和适配视口。 |
| 桌面与窄屏 | 1487 × 1058 与 390 × 844 均无横向溢出；窄屏保存按钮可滚动到达。 |
| 控制台 | 实际服务页面和 fixture 页面各为 0 errors、0 warnings。 |

| 页面 | 检查视口 |
| --- | --- |
| 空间首页 | 桌面 |
| 空间设置 | 桌面 · 窄屏 |
| 资源库 | 桌面 · 窄屏 |
| 保存编排预览 | 桌面 · 窄屏 |

窄屏检查使用桌面浏览器视口，不是手机实机验收。本轮没有运行真实模型、OCI 或外部发布服务；以下历史联调结果不计入本轮验收。

## 历史验证：上一次顶栏与空间草稿改造

以下命令和浏览器结果均属于前一轮，不作为本轮完成的证据。此前从仓库根目录实际执行：

| 命令 | 结果 |
| --- | --- |
| `npm --prefix apps/weave/web run typecheck` | 通过 |
| `npm --prefix apps/weave/web run format:check` | 通过 |
| `npm --prefix apps/weave/web test` | 73 passed，6 files |
| `npm --prefix apps/weave/web run build` | TypeScript／Vite 构建通过，已生成静态资源 |
| `uv run --directory apps/weave --frozen ruff format --check .` | 83 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | 通过 |
| `uv run --directory apps/weave --frozen mypy` | 85 source files，通过 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 202 passed，2 deselected |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `uv run --directory apps/weave --frozen pytest tests/packaging -q` | 最终静态资源构建后复跑：2 passed |

### 浏览器操作验证

当时使用独立 Playwright 控制真实 Chrome，加载上一轮最终前端。任务、Graph、执行及审批数据来自 `127.0.0.1:18081` 的隔离 HTTP fixture；这些验证覆盖当时的页面与 HTTP 交互，不调用真实模型、OCI 或发布服务。

| 路径 | 已观察结果 |
| --- | --- |
| 创建空间 | 描述用途后生成预览，可编辑名称和规则，确认后进入新空间。 |
| 开始任务 | 第一条消息创建会话；未生成 Graph 时保持纯对话视图。 |
| 全局搜索 | 空结果提示、清除搜索、上下方向键和 Enter 跳转可用，进入任务所属空间。 |
| Graph 详情与对话 | 节点详情可打开；询问将内容带回输入框；对话可展开和收起。 |
| 结果与历史 | 可查看结果和执行记录；下载 JSON 内容与 fixture 返回的产物一致；保存空间编排请求成功。 |
| 执行与资源 | 停止后状态为 cancelled；批准后状态为 succeeded；资源勾选保存后重新读取包含所选 Tool code。审批与执行均为内存 fixture，无外部发布。 |
| 视口稳定 | 手动缩放由 100% 到 120% 后，详情轮询返回更新仍保留用户缩放与平移状态。 |
| 390 × 844 | 首页、新任务、创建空间、搜索、Graph 和节点详情可使用；创建预览可编辑，底部按钮可点；页宽 390px，无横向溢出。 |

窄屏验证使用桌面 Chrome 视口，不代表移动设备实机验收。Graph、对话和窄屏检查使用隔离数据；桌面首页、新任务和创建预览另在实际服务检查，仍不能当作真实任务执行证据。

| 页面 | 检查视口 |
| --- | --- |
| 工作区 | 桌面 · 窄屏 |
| 新任务 | 桌面 · 窄屏 |
| 对话 | 桌面 |
| Graph | 桌面 · 窄屏 · 窄屏节点详情 |
| 创建空间 | 桌面 · 窄屏 |
| 搜索 | 窄屏 |

### 实际后端模型接口

当时另在实际服务 `127.0.0.1:8000` 的浏览器页面提交空间用途，真实 `POST /v1/spaces/draft` 返回 200，生成的名称和规则显示在可编辑预览中。关闭后仍为原有两个空间，未确认创建、未启动产品任务或 OCI。也读取了实际已有任务、消息和 Graph。该检查验证了当时页面、草稿接口与实际模型的连接，不代表本轮任务执行端到端验收。当时实际服务与 fixture 页面控制台均为 0 errors、0 warnings。

视觉对照报告见 [design-qa.md](../apps/weave/web/design-qa.md)，应以报告中的验收轮次为准。打包检查必须在最后一次前端构建后复跑；当时未重跑真实 OCI 或外部发布验收。

## 保留的历史修复

改造前已修复搜索无响应、新任务入口没有可见变化、重复历史入口、失败草稿丢失、资源保存竞态与节点键盘选择问题。新版保留这些操作边界，并用新的顶栏和草稿创建流程替换旧交互。

SDK 既有事件顺序修复保证开始事件先于节点执行，开始通知期间取消会退还未使用预算；该机制不因本次换布局而改变。改造前的真实模型、审批、下载、沙箱验证及截图在 [历史验证记录](weave-web.md#10-历史验证记录本轮界面改造前) 保留，仅作为历史证据，不表示当前界面已经重复完成这些外部验收。
