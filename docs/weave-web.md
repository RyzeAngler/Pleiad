# Weave 网页版：启动与操作

Weave Web 与 HTTP API 由同一个 `pleiad serve` 进程提供。网页中的空间、任务、消息、Graph、节点结果与审批都读取真实后端；没有演示数据或浏览器内的模拟执行器。

## 1. 环境准备

从仓库根目录执行后续命令，避免 `.env` 和相对工作区路径发生变化。

需要：

- Python 3.12 与仓库固定的 uv 版本（见 `.uv-version`）；
- Node.js 22.12 或更新版本，以及 npm；仅从源码构建前端时需要；
- 一个已配置的模型接口；
- Docker 或 Podman，以及本地已有、使用固定 digest 的沙箱镜像。执行工作区脚本和命令时需要运行中的容器引擎。

安装 Python 与前端依赖并构建：

```bash
uv sync --project apps/weave --locked --all-groups --all-extras
npm --prefix apps/weave/web ci
npm --prefix apps/weave/web run build
```

构建结果写入 `apps/weave/src/web/assets/`，会随 Weave wheel 和 sdist 一起发行。只修改网页样式、字体或组件后，重新执行 `npm --prefix apps/weave/web run build` 并刷新页面；如果运行的是严格 editable 安装，新增的静态文件还需通过 `uv sync --project apps/weave --locked --all-groups --all-extras` 更新安装清单，并重启服务。修改 Python 服务或启动配置后，也需要在原终端正常退出并重新启动 `pleiad serve`。刷新浏览器不会重新加载 Python 服务代码。已安装的发行包自带静态资源，不要求使用者再安装 Node.js。

网页统一使用本地 MiSans，正文、标签和标题分别使用 Regular、Medium 和 Semibold；代码保留等宽字体。字体文件位于 `apps/weave/web/src/fonts/misans/`，来自固定版本 `misans@5.0.0`（字体 4.009），按 Unicode 分片加载，不连接外部字体 CDN。构建会把字体、来源说明和小米许可一并打包；页面的 `rel="license"` 链接可访问完整声明。第三方字体不适用项目自身的 MIT 许可，原始许可见 [MiSans 官方下载页](https://hyperos.mi.com/font/zh/download/)。

2026-11-05 的字体改造、桌面与窄屏检查和测试结果见 [MiSans 验收记录](../apps/weave/web/font-qa.md)。

## 2. 配置模型与沙箱

如果已经有可用的 `.env`，继续使用，**不要覆盖原文件**。首次使用时复制模板：

```bash
cp .env.example .env
```

编辑仓库根目录的 `.env`。沿用当前模板的提供商时，至少填写：

```dotenv
PLEIAD_LLM_PROTOCOL=openai
PLEIAD_LLM_PROVIDER=deepseek
PLEIAD_LLM_MODEL=deepseek-v4-flash
PLEIAD_LLM_API_KEY=填写自己的密钥
PLEIAD_SANDBOX_RUNTIME=docker
PLEIAD_SANDBOX_IMAGE=填写本地真实的镜像名@sha256:digest
```

模型名称、协议和地址应与自己的提供商配置一致。不要把 `.env`、密钥或服务凭据放入默认指令或能力 YAML。模型设置在启动时读取；修改后重启服务。

### 启动容器引擎

安装 Docker 命令行工具并不代表容器引擎已经运行。macOS 上若已安装 Colima，并使用默认 Docker 运行时，先执行：

```bash
colima start
colima status
docker context ls
docker info
```

`colima status` 应显示运行中，`docker info` 应成功返回包含 Server 的引擎信息。确认这些检查通过后，再配置镜像。[Colima 启动说明](https://colima.run/docs/getting-started/)、[Docker 引擎排查](https://docs.docker.com/engine/daemon/troubleshoot/)。

如果 Colima 已运行，但 Docker 仍连接失败，检查 `docker context ls` 中带 `*` 的当前连接。若仍指向不可用的 `default`，且列表中已存在 `colima`，可切换后重试：

```bash
docker context inspect colima
docker context use colima
docker info
```

使用其他 Colima profile 时，以列表中的实际 context 名称为准。`DOCKER_HOST` 或 `DOCKER_CONTEXT` 环境变量也可能覆盖当前连接；应在启动 Weave 的同一个终端检查这些变量。连接目标应与运行中的容器引擎一致。[Docker context 说明](https://docs.docker.com/engine/manage-resources/contexts/)。

### 配置沙箱镜像

沙箱镜像必须是完整的 `name@sha256:<64位十六进制字符>`，不能只写可变标签，也不能使用示例中的占位值。以只需要 Python 标准库的工具为例，容器引擎可用后，拉取官方 Python 镜像并查看真实 digest：

```bash
docker pull python:3.12-slim
docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
```

把第二条命令的完整输出填入 `PLEIAD_SANDBOX_IMAGE`。使用 Podman 时替换命令并设置 `PLEIAD_SANDBOX_RUNTIME=podman`。工具若依赖额外的 Python 包，需要使用包含相应依赖的受控镜像；基础 Python 镜像不会自动安装这些包。

使用命令工具前先执行 `docker info` 或 `podman info`，确认引擎已启动。SDK 内置的 `command` 也依赖沙箱；Graph 没有直接绑定 Tools 不代表禁用了内置工具。

默认工作区为仓库的 `workspace/`。另用工作区时，应准备它自己的能力目录、Tools 和 Skills，并在 `.env` 中设置绝对路径：

```dotenv
PLEIAD_WORKSPACE=/absolute/path/to/workspace
```

使用 Colima 时，工作区还必须位于虚拟机已共享的目录中。本机已验证用户主目录下的工作区可用；macOS 的 `/private/var/folders/` 临时目录未被共享，挂载时会出现 `bind source path does not exist`。遇到该错误时检查共享路径，不要仅凭宿主机上目录存在就判断挂载可用。

## 3. 启动网页

```bash
uv run --project apps/weave --frozen pleiad serve --host 127.0.0.1 --port 8000
```

浏览器打开 **http://127.0.0.1:8000/**。

- 健康检查：http://127.0.0.1:8000/healthz
- 接口文档：http://127.0.0.1:8000/docs
- 退出：在启动服务的终端按 `Ctrl+C`。

这是本地单所有者应用，当前没有多用户登录与鉴权。默认只监听本机；不要直接把该端口暴露到公网。同一个工作区只能由一个运行服务拥有，不能同时启动占用同一工作区的 TUI 与 Web 服务。

## 4. 第一次使用

### 工作区、空间与任务

网页分为三个层级：**工作区 → 空间 → 任务**。任务根据实际内容显示纯对话，或展开 Graph 与执行结果；这两种视图属于同一任务。

- 工作区首页展示空间卡片和跨空间最近任务。卡片显示任务概况，不把默认指令当作空间简介。点击空间进入其新任务页，点击最近任务直接继续对应会话。
- 顶部 Weave 标识返回工作区。「搜索」默认查询全部空间的任务，按任务标题和所属空间名称匹配；结果显示所属空间。
- 进入空间后，空间名称打开切换菜单，菜单只列空间。已有任务的名称单独显示；「空间设置」位于当前空间的操作区，打开独立设置页。
- 顶栏「资源库」打开整个工作区的模型、Tools 和 Skills 目录。资源偏好在空间设置中选择，不混入全局资源库。
- 进入已有任务后，顶部「新任务」回到所属空间的空白输入页并聚焦输入框。工作区首页和已经处于新任务的页面不重复显示该按钮；发送第一条消息才创建任务会话。

搜索支持清空、空结果反馈、读取失败后重试、上下方向键和 Enter 选择任务。搜索与空间切换面板可用关闭按钮、外部点击或 `Esc` 收起；Tab 焦点留在打开的面板内，关闭后返回入口。

### 对话创建空间

在工作区首页点击「创建空间」，描述用途；有长期偏好时可以一起说明。例如：

> 建一个小红书写作空间。主要写旅行内容，引用资料保留来源，语气自然，定稿前由我确认。

点击发送或按 Enter 后，后端使用当前配置的文本模型整理空间草稿。右侧显示可编辑的名称、默认指令与资源偏好。默认指令可留空，不影响创建；草稿生成提示要求只保留用户明确提出的长期偏好，不把简单用途扩写成规则清单，也不把一次任务的要求保存为默认指令。可以直接修改预览，也可以继续对话补充；下一轮整理会携带当前手工修改的草稿。资源偏好只能选择目录中已经配置的 Tool 和 Skill，不会安装资源或授予执行权限。

确认后点击「创建并进入」，空间才会写入后端。草稿生成期间没有空间、任务或 Execution 记录；关闭弹窗会取消尚未完成的草稿模型调用，未保存的草稿不会成为空间。生成失败保留输入和已有草稿，显示错误供重试；不会截取描述充当生成结果，也不会悄悄换模型。生成超时上限为 120 秒，同时受当前模型更短的超时设置约束。

存在未发送的补充内容时，需要先发送后再确认创建，避免保存未更新的预览。创建提交期间禁用重复操作和关闭；其他时候可通过关闭按钮、外部点击或 `Esc` 退出。

空间保存名称、默认指令与偏好资源。它不是一次执行，也不是一条无限增长的会话。新空间没有初始 Graph；后续任务产生的编排可按精确版本保存到空间。

### 空间设置与默认指令

从空间操作区进入「空间设置」，修改名称、可选的默认指令及优先使用的资源。点击「选择资源」搜索目录并选择 Tools／Skills；取消选择或移除已选标签不会卸载资源。点击「保存」才提交修改；没有有效变更时按钮不可用，保存失败会保留编辑内容。

保存期间全局导航会暂时阻止离开并显示提示，保存失败后错误可见且可重试。编排预览的一般读取失败可重新加载；若保存引用已过期，应关闭预览并刷新设置，不对同一个过期引用反复重试。

默认指令是这个空间中反复使用的提示，例如“默认从杭州出发，优先公共交通”。本次对话说“这次从上海出发”时，按本次任务处理。每条消息提交后，后端将当时的空间配置保存为该 Execution 的快照；修改设置只影响后续提交，不改动正在执行或历史任务的快照。

这些指令以用户级上下文发送给主 Agent 和 Graph 节点。Graph 节点同时收到本次任务，不需要主 Agent 手动把默认指令复制进每个节点；上下文不会改写已保存的 Graph 或节点业务输入。空白指令不注入。默认指令提供任务偏好，不能代替权限配置或强制审批。

「已保存的编排」中的「查看」读取保存的精确版本，只预览，不创建 Execution 或运行节点。点击「取消默认编排」并保存后，后续任务不再自动带上这个引用；历史 Graph 和执行记录仍保留。

设置有未保存修改时，返回、导航或关闭页面会提醒确认。取消离开会保留编辑内容；确认离开才放弃未保存修改。从设置返回原空间或任务时，原先尚未发送的对话输入仍保留。从设置打开资源库后，资源库的返回入口回到设置。

### 删除任务与空间

最近任务列表中，每项右侧都有删除按钮；进入任务后，也可以使用顶栏的「删除任务」。在「空间设置」底部点击「删除空间」，会同时删除该空间下的任务。确认弹窗会显示目标名称与删除范围，默认聚焦「取消」。删除无法撤销；提交期间不能重复操作，失败时保留弹窗并显示原因。

删除任务会清理会话消息、执行与事件记录、Graph 运行和节点记录、关联产物及会话范围的记忆。没有其他引用的 Graph 版本也会清理；空间保存的编排和其他任务仍在使用的版本会保留。模型、Tools、Skills、工作区文件以及跨会话的用户记忆不随任务删除。

排队、运行、等待审批或尚未完成清理的任务不能删除。先停止执行，等清理完成后再删除；空间内任何任务未结束时，也会阻止删除整个空间。任务删除成功后返回所属空间，空间删除成功后返回工作区首页；刷新页面后不会重新出现。只保留已删除任务的 ID，防止旧标签页用原 ID 重新提交执行，不保留任务内容。

如果删除还需要清理 Graph 版本，而其他任务尚在运行，也会暂时阻止删除，以免移除正在被引用的编排。没有 Graph 需要清理的任务不受这个限制。

### 通过对话开始任务

进入空间后，页面中央是任务输入区，下方是该空间的最近任务。直接描述目标，例如：

> 规划一篇旅行攻略。先生成任务 Graph，并解释各节点会使用什么资源，暂时不要执行。

每个任务有独立的 conversation；同一任务内的连续消息会保留上下文。每次发送创建一个真实 Execution，按同一会话的顺序执行。新的任务使用独立会话，复用空间配置。

发送后，消息在中央阅读区域展开，输入区固定在底部。没有 Graph 时不显示空画布、编排版本或缩放按钮。简单问题可以一直保持对话视图；复杂目标由 Weave Agent 搜索能力、提交和执行 Graph。网页不会在浏览器里拼出虚构计划。

提交失败时保留输入内容供重试；如果会话已经创建成功，重试继续使用该会话。Shift+Enter 换行，输入法组合过程中的 Enter 不提交。

### 查看与调整编排

真实 Graph 出现后，主区域展开横向画布，对话移入底部浮层。「展开对话」查看完整历史，「收起对话」恢复浮层；切换视图保留当前输入。收起画布后可以继续使用完整对话，通过「展开编排」恢复。

节点直接呈现任务摘要、模型角色或实际模型、配置的 Tools／Skills 和上游依赖步骤。彩色标题区用于组织阅读，执行状态单独标注；配置的资源不等于本次实际调用记录。Graph 可平移、缩放、适配视口，节点详情显示输入、输出、指令、结构和错误。

节点可点击或用 Enter、空格打开详情，Esc 关闭详情。「询问节点详情」把问题填入并聚焦可见的对话输入框，需要发送后才执行。没有单节点播放、拖线编辑或删除按钮；编排调整仍通过对话完成。例如：

> 在文案生成后增加事实核查，保留之前的检索步骤，生成新版本后先说明变化。

Graph 版本与执行结果分开显示。切换旧版本不会套用当前版本的节点状态，也不会重新执行计划。

确认满意后，点击保存空间编排的按钮。空间保存这份 Graph 的精确版本引用；之后的新任务沿用默认指令和该编排，描述这次的新目标或输入即可。修改 Graph 后需要再次保存才会替换空间引用；历史执行保留原版本。「空间设置」可以查看或取消保存的引用。

### 跟踪执行与审阅结果

网页接收真实 SSE 事件，展示当前执行、Graph 节点状态和过程记录；连接中断时重新读取持久化状态。最终答案、节点产物和错误以服务器记录为准。只有存在可取消的真实执行时，才显示停止操作；取消通过后端 ExecutionService 完成。

结果可以继续通过对话修改。需要调用外部发布工具时，必须先在工作区配置真实工具和对应凭据；安装 Web 不会自动获得小红书发布或联网搜索能力。

### 处理审批

需要审批的调用会展示后端保存的工具名、参数、具体动作和截止时间。选择批准或拒绝后，服务继续或终止该动作；浏览器不提交替代工具结果。

空间里的“发布前确认”是任务要求，不能替代宿主的强制审批策略。若需要每次执行 Graph 前均审批，可在 `.env` 中显式配置：

```dotenv
PLEIAD_APPROVAL_TOOL_CODES=["weave.execute_graph"]
```

配置后重启服务。审批固定具体 Graph 版本；等待期间生成的新版本不会替换已批准的计划。当前 SDK 接入仅支持主 Run 的可恢复审批；Graph 节点包含需审批的工具时仍会明确拒绝执行，这个已有边界没有被 Web 绕过。

## 5. 模型、Tools 与 Skills

「资源库」使用「模型」「Tools」「Skills」分类展示后端实际加载的模型角色和能力目录。可以搜索、查看资源详情，点击「配置位置」查看并复制实际工作区和能力清单路径。资源库不显示当前空间的配置项。

当前资源库提供目录查询，没有在线编辑模型、连接 MCP、安装 Tool 或 Skill 的按钮。模型密钥、MCP 连接、Tool 注册和 Skill 包安装继续使用已有工作区配置方式；网页中的空间设置只保存默认指令、资源偏好和编排引用，不提供素材上传入口。偏好资源用于指导编排，**不代表权限隔离或强制白名单**。

| 配置 | 位置 | 生效时间 |
| --- | --- | --- |
| 模型、endpoint、密钥、沙箱、审批策略 | 仓库根目录 `.env` | 重启服务后 |
| 能力名称、代码、描述 | `workspace/capabilities.yaml` | 下一次能力读取或绑定 |
| Tool 脚本与 Schema | `workspace/tools/<name>.py` 与 `<name>.json` | 下一次能力读取或绑定 |
| Skill 内容与附属文件 | `workspace/skills/<code>/SKILL.md` 等 | 下一次能力读取或绑定 |
| 默认指令、偏好能力、默认编排引用 | 网页内空间设置 | 下一次提交请求 |

新能力必须满足已有 SDK loader 的校验。目录无效会明确报错；不会静默使用旧配置。正在执行的 Run 保留已选资源引用。详细格式见 [工作区说明](../workspace/README.md) 和 [仓库能力配置](../README.md#能力配置)。

资源搜索按名称、代码和描述筛选，显示匹配数量与无结果提示。切换分类时更新列表，目录读取失败可重试。复制路径成功和失败分别显示反馈；复制操作不修改配置文件。MCP 配置方式见 [默认能力接入指南](weave-capabilities.md)。

网页控件的逐项检查、修复和验证命令见 [交互验收记录](weave-web-interactions.md)。

## 6. 开发时运行前端

一个终端在仓库根目录运行后端：

```bash
uv run --project apps/weave --frozen pleiad serve
```

另一个终端运行 Vite：

```bash
npm --prefix apps/weave/web run dev
```

打开 Vite 输出的地址。开发服务器把 `/v1` 与 `/healthz` 代理到 `127.0.0.1:8000`。正式使用时直接打开后端的 8000 端口，不必再启动 Vite。

前端修改后验证并重新构建：

```bash
npm --prefix apps/weave/web run typecheck
npm --prefix apps/weave/web run format:check
npm --prefix apps/weave/web test
npm --prefix apps/weave/web run build
```

## 7. 常见启动问题

| 现象 | 检查与处理 |
| --- | --- |
| 首页返回 503，提示 Web assets missing | 执行 `npm ci` 和 `npm run build`，然后重启 `pleiad serve`；检查是否在仓库根目录启动 |
| 首次启动提示模型或沙箱配置无效 | 检查根目录 `.env`，尤其是密钥、协议和完整真实镜像 digest；不要把占位值当配置 |
| `workspace_in_use` | 另一个服务或 TUI 正拥有同一工作区；回到原终端正常退出，或使用不同工作区；不要直接删除锁文件 |
| 页面显示后端不可达 | 确认服务终端没有退出，访问 `/healthz`；开发模式确认后端仍在 8000 端口 |
| 模型返回认证、额度或超时错误 | 查看页面中的实际错误和 `workspace/runtime/logs/`；检查提供商配置与额度，修复后重新提交 |
| Graph 一直未出现 | 当前任务可能直接回答，或模型尚未提交计划；可明确要求先生成 Graph；不要把无图误判为前端断开 |
| `sandbox.runtime.unavailable` | OCI 命令行工具或引擎不可用；先运行 `docker info` / `podman info`，检查服务和连接目标，详见下方说明 |
| `sandbox.cleanup.failed` | 未能确认本次沙箱容器已清理；查看诊断详情及服务日志，确认引擎连接和容器状态，不能据此认定引擎未启动 |
| `sandbox.execution.failed` | 展开诊断信息，检查容器创建或状态查询的具体错误；若为 `bind source path does not exist`，确认工作区在容器虚拟机的共享范围内 |
| 审批过期或返回 409 | 刷新任务状态，确认请求是否已处理、取消或过期；不可重新批准已经失效的动作 |
| 空间保存返回版本冲突 | 刷新空间设置，再基于最新版本修改；服务不会覆盖其他页面刚保存的配置 |

### 沙箱连接失败与清理失败

`sandbox.runtime.unavailable` 表示选择的 OCI 运行时当前不可用。SDK 在创建容器前执行只读的 `docker info` 或 `podman info` 检查；命令不存在、引擎未启动、连接错误或检查超时，都会在创建容器前报错。此时按上方步骤恢复引擎连接，并确认配置的镜像已在该引擎中存在。Weave 不自动启动容器引擎、拉取镜像，也不回退到宿主执行。

`sandbox.cleanup.failed` 表示无法确认本次执行拥有的容器已经清理，可能发生在容器创建或执行之后。它不等同于“Docker 未启动”。展开页面中的诊断详情，结合 `workspace/runtime/logs/` 检查原始错误和清理错误；本次修复会保留两者，避免清理错误覆盖前面的失败原因。恢复引擎连接后，应先确认该次执行的容器状态，再重新提交任务；不要直接批量清理容器。

更新代码后重新构建前端，并重启 `pleiad serve`，使服务使用本次修复。模型和沙箱配置的修改也需要重启。确认 `docker info` 或 `podman info` 通过后，在网页重新发送请求；新请求会创建新的执行记录，原失败记录仍保留，不会自动恢复或改成成功。

## 8. 数据与接口

空间和会话元数据写入 `workspace/runtime/workspaces/workspaces.sqlite3`；执行与事件仍在 `runtime/executions/`，Graph、节点产物、SDK 消息历史继续使用原存储目录。刷新页面或正常重启不会把它们变成浏览器缓存中的临时数据。

网页接口：

| 接口 | 用途 |
| --- | --- |
| `GET /v1/spaces` | 列出空间 |
| `POST /v1/spaces/draft` | 通过模型整理或修改未保存的空间草稿 |
| `POST /v1/spaces` | 确认草稿并创建空间 |
| `PATCH /v1/spaces/{space_id}` | 按 `expected_revision` 保存空间配置 |
| `DELETE /v1/spaces/{space_id}` | 删除空间及其任务的持久化数据 |
| `GET /v1/spaces/{space_id}/saved-graph` | 只读返回保存引用的精确 GraphSpec，不执行编排 |
| `GET/POST /v1/spaces/{space_id}/conversations` | 列出、创建独立任务会话 |
| `GET /v1/resources` | 查询真实模型与能力元数据，不返回密钥 |
| `GET /v1/conversations/{conversation_id}/messages` | 读取 SDK 公开会话历史 |
| `DELETE /v1/conversations/{conversation_id}` | 删除单个任务及其持久化数据 |
| `GET /v1/executions?conversation_id=…` | 查询该会话的持久化执行记录 |
| `GET /v1/executions/{execution_id}/details` | 查询该执行的 Graph、节点尝试及产物 |
| `GET /v1/executions/{execution_id}/stream` | SSE 在线事件与持久事件回放 |

空间草稿请求示例：

```json
{
  "messages": [{"role": "user", "content": "建立旅行内容写作空间，保留引用来源。"}],
  "draft": null
}
```

返回 `{ "reply": "…", "draft": {…} }`。后续请求发送完整草稿对话和当前可编辑的 `draft`；消息角色只允许 `user`、`assistant`，最后一条必须是用户消息。草稿对话最多 40 条、累计 48000 字符，单条最多 16000 字符。该请求只生成配置，没有保存副作用，断连会取消模型调用。

确认创建的请求体为：

```json
{
  "name": "旅行内容写作",
  "instructions": "引用资料保留来源。定稿前由我确认。",
  "preferred_tool_codes": [],
  "preferred_skill_codes": []
}
```

`POST /v1/spaces` 已移除旧 `saved_graph` 字段，传入该字段（即使为 `null`）会返回 422，不保留兼容入口。`PATCH /v1/spaces/{space_id}` 继续要求 `expected_revision`，可保存来自该空间真实任务的 `saved_graph` 精确版本引用。创建和更新都会验证资源偏好是否存在于当前目录。

`instructions` 对应界面中的「默认指令」，可省略或使用空字符串。每次提交 Execution 都读取服务端空间配置，空白指令不进入任务上下文。`GET /v1/spaces/{space_id}/saved-graph` 不读取最新版本来替代保存的旧版；空间不存在、没有保存编排或引用版本不存在时返回 404，不创建任务或调用模型。

草稿输入校验失败返回 422，模型调用或输出校验失败返回 502，超时返回 504；错误正文不返回提供商密钥或原始内部异常。详细诊断保留在服务日志。

两个删除接口成功时返回 `{ "deleted": true }`，目标不存在返回 404；执行尚未结束，或存在涉及其他任务的关联执行时返回 409。删除与执行提交、任务创建、空间设置更新互斥，校验冲突后不会先删除一部分记录。存储故障仍会报错，可修复后重试；它不是跨多个存储文件的统一数据库事务。

提交、取消和审批继续使用已有 `/v1/executions` 接口，完整字段以运行服务的 `/docs` 为准。草稿断连取消只适用于未保存的配置生成；关闭任务页或其 SSE 连接不会取消持久化 Execution。

## 9. 当前实现与验证

当前前端按工作区、空间、任务组织页面，顶栏导航替代旧侧栏；空间创建使用 `SpaceCreator` 与 `/v1/spaces/draft`，Graph 使用横向节点与底部对话浮层。后端草稿流程位于 `application/space_drafts.py`，继续调用 Pleiad 的公共 Agent、Runner 和模型接口，不另建执行引擎。

### 删除任务与空间：2026-11-05

前端入口、确认与请求状态在 `web/src/App.tsx`、`DeleteConfirmation.tsx`，后端清理由 `application/service.py` 统一处理。会话历史通过 Pleiad 的 `ShortMemory.clear()` 清理，Weave 不直接修改 SDK 的表。

| 实际执行的命令 | 结果 |
| --- | --- |
| `npm --prefix apps/weave/web run typecheck` | 通过 |
| `npm --prefix apps/weave/web run format:check` | 通过 |
| `npm --prefix apps/weave/web test` | 91 passed，8 files |
| `npm --prefix apps/weave/web run build` | 通过，已更新随包网页资源 |
| `uv run --directory apps/weave --frozen ruff format --check .` | 84 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | 通过 |
| `uv run --directory apps/weave --frozen mypy` | 86 source files，通过 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 229 passed，2 deselected |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 210 files already formatted |
| `uv run --directory packages/pleiad --frozen ruff check .` | 通过 |
| `uv run --directory packages/pleiad --frozen mypy` | 214 source files，通过 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 378 passed，2 deselected；1 条既有 DashScope 弃用警告 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `UV_OFFLINE=1 uv run --directory apps/weave --frozen pytest tests/packaging -q` | 最终构建后的 wheel、sdist 与干净安装：2 passed |
| `git diff --check` | 通过 |

回归覆盖持久化删除与重启、跨任务数据隔离、保存与复用 Graph、活动执行与父子任务冲突、共享 Graph 尚未落盘的竞争窗口、提交被连续取消时的锁和调度，以及前端迟到请求不恢复已删除任务。SDK 清空测试也覆盖 SQLite 删除失败时事务回滚。

独立浏览器在临时工作区连接真实 HTTP 服务，模型边界使用脚本化响应。桌面与 390 × 844 窄屏完成任务删除取消、顶栏删除、列表删除入口、执行中返回 409、空间删除失败后重试、删除成功后的导航及刷新检查。重启临时服务后，已删空间仍不存在，另一空间、任务和两条消息仍可读取。两个预期的 409 网络错误均显示在确认框内。此轮没有运行真实模型、OCI 或外部发布服务。

验证结束后清理临时工作区、脚本、截图、日志和测试缓存，关闭独立测试浏览器；保留正式网页资源、操作文档和原有用户数据。

### 默认指令与空间设置改造：本轮验证

2026-11-04，本轮自动检查结果：

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

本轮已使用独立浏览器验收最终构建。实际 `127.0.0.1:8000` 服务重新启动后，确认空间切换菜单、独立设置和资源库导航可用；此部分只读，没有修改真实用户数据或调用模型。

在 `127.0.0.1:18081` 的隔离内存 HTTP fixture 中，完成清空默认指令后保存并重新读取、资源选择与移除后保存、资源库分类搜索与详情、配置位置、返回原设置，以及保存 Graph 的精确版本预览、节点指令查看和缩放适配。1487 × 1058 桌面与 390 × 844 窄屏均无横向溢出，窄屏保存按钮可滚动到达。实际服务与 fixture 页面控制台均为 0 errors、0 warnings。

这些检查验证页面与 HTTP 交互，未调用真实模型、OCI 或外部发布服务；历史联调不计入本轮验收。逐项结果和截图见 [本轮交互验收](weave-web-interactions.md#本轮浏览器验收)。

### 历史验证：上一次顶栏与空间草稿改造

以下结果与截图属于前一轮，不代替本轮重新验证。

2026-11-04，上一次顶栏与空间草稿改造执行过以下检查：

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

后端确定性测试使用真实 SDK Runner、应用服务与存储，只在模型提供商边界提供受控响应。覆盖草稿多轮修改、确认前不持久化、无工具写入权限、断连取消、超时、旧字段拒绝和资源目录校验。

当时使用独立 Playwright 控制真实 Chrome 验证了完整前端操作路径：创建草稿、编辑预览、确认进入新空间、发送首条消息、无 Graph 对话、搜索与键盘导航、节点详情和询问、展开／收起对话、结果与 JSON 下载、切换执行记录、保存空间编排。手动缩放从 100% 到 120% 后，即使后端详情轮询返回变化，画布仍保持用户视口。

390 × 844 窄屏下检查了工作区、新任务、创建空间、搜索、Graph 与节点详情；创建预览可编辑，底部确认按钮可点击，页面宽度为 390px，没有横向溢出。该验证是桌面 Chrome 的窄视口检查，不是手机实机验收。

上述浏览器任务、Graph、执行和审批数据由 `127.0.0.1:18081` 的隔离 HTTP fixture 提供，验证真实页面渲染、交互与 HTTP 请求，不涉及真实提供商、OCI 或外部发布。

另外，本机实际服务 `127.0.0.1:8000` 的 `POST /v1/spaces/draft` 已通过直接请求和当时的浏览器页面验证：返回 200，模型生成的名称和规则显示在可编辑预览中。关闭后仍保留原有两个空间，没有确认创建，也没有启动产品任务或 OCI。这验证了前端、草稿接口与实际模型的连接，不等同于新版任务执行的完整端到端验证。

浏览器步骤和截图见 [交互验收](weave-web-interactions.md)；最终视觉核对见 [design-qa.md](../apps/weave/web/design-qa.md)。当时没有重新验证真实 OCI 或外部发布服务。

## 10. 历史验证记录（本轮界面改造前）

以下命令结果、真实模型与 OCI 联调、截图属于前一版的历史验证，不代替本轮界面和空间草稿接口验收。


前端源码在 `apps/weave/web/`，静态资源随 `pleiad_weave.web` 发行；空间接口在 `api/routes/workspaces.py`，会话、执行详情与 SSE 在 `api/routes/workbench.py`。两者继续使用现有 ExecutionService 和 Pleiad Runner。

2026-11-04 在 macOS 上完成以下验证：

| 实际执行的命令 | 结果 |
| --- | --- |
| `npm --prefix apps/weave/web run format:check` | 通过 |
| `npm --prefix apps/weave/web run typecheck` | 通过 |
| `npm --prefix apps/weave/web test` | 14 passed |
| `npm --prefix apps/weave/web run build` | 通过，已生成随包资源 |
| `uv run --directory apps/weave --frozen ruff format --check .` | 82 files 通过 |
| `uv run --directory apps/weave --frozen ruff check .` | 通过 |
| `uv run --directory apps/weave --frozen mypy` | 84 files 通过 |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 186 passed，2 deselected |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 210 files 通过 |
| `uv run --directory packages/pleiad --frozen ruff check .` | 通过 |
| `uv run --directory packages/pleiad --frozen mypy` | 214 files 通过 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 371 passed，2 deselected；1 条既有 DashScope 弃用警告 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `uv run --directory apps/weave --frozen pytest -q -m packaging tests/packaging` | 2 passed；最后一次前端构建后重新检查 wheel、sdist 与干净安装 |
| `uv lock --project apps/weave --check` / `uv lock --project packages/pleiad --check` | 通过 |
| `uv run --project apps/weave --frozen pre-commit run --files <本次变更和新增文件> --show-diff-on-failure` | 所有适用的 pre-commit hooks 通过；无符号链接供 check-symlinks 检查 |
| `git diff --check` | 通过 |

真实浏览器验收使用独立临时工作区和本机配置的 DeepSeek 模型：创建空间 → 对话生成两节点 Graph → 保存版本 → 独立任务复用 → 对话修改节点指令 → 审批 → 两节点成功 → 查看真实输入输出 → 下载节点 JSON。下载内容与服务器持久产物一致；刷新和服务重启后空间、任务及历史仍可读取。审批以 `waiting_approval → resume_queued → resumed` 的实际记录核验，不以模型回复中的叙述作为审批事实。

联调修复了 DeepSeek Chat Completions 的结构化输出协议适配：发送 JSON mode 和 Schema 指令，Runtime 继续校验结构，不在请求失败后切换模型或伪造成功。协议依据见 [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)。

首次网页联调时，本机 Docker 未运行，涉及内置 `command` 的尝试失败；当时的成功链路使用文本节点。后续已恢复容器引擎并补充真实 OCI 和网页命令调用验证，见下节。**外部发布工具、移动设备实机、Podman 及其他模型提供商未验证。** 测试未调用发布服务，也未修改既有用户工作区数据或 `.env`。

下面是实际联调页面，不是静态演示数据：

![第一版 Weave Web 的对话、Graph 和节点输入输出](assets/weave-web-v1.jpg)

### 2026-11-04：沙箱异常与界面文案修复

本次排查确认：故障发生时 Docker CLI 已安装，但 Colima 的 `default` 实例处于 `Stopped`，Docker 当前连接的 socket 不存在。已执行 `colima start` 启动现有实例；Colima 自动将 Docker context 切换为 `colima`，`docker info` 返回 Server 29.5.2，既有固定镜像可用。

本次修改增加容器创建前的引擎连接检查，在执行与清理同时失败时保留原始原因；创建和状态查询错误附带最多 2 KiB 的运行时 stderr。执行预算耗尽后不会继续以无限超时启动命令，容器清理保留独立预算。网页显示中文操作建议和可展开的诊断信息。静态文案统一为空间、任务、编排、执行和审批等术语，保留具体参数和版本信息。

最终代码通过上表中的确定性测试、Ruff、严格 mypy、前端检查、导入边界及打包测试。另完成：

- 引擎停止时，真实 `OciSandbox.execute()` 返回 `sandbox.runtime.unavailable`，未进入容器创建与清理。
- 使用 Weave 虚拟环境的 Python 执行 `-m pytest -q -m oci packages/pleiad/tests/sandbox/test_oci_real.py -k docker --basetemp <用户主目录下新建的临时目录>/pytest`：**1 passed，1 deselected**。`PLEIAD_DOCKER_SANDBOX_IMAGE` 来自现有 `Settings.sandbox_image`。覆盖非 root 身份、只读工作区、可写临时目录、退出码、stdout/stderr 与清理。
- 初次真实 OCI 检查遇到未共享临时目录；改用用户主目录后，发现原测试的只读写入探针污染 stderr。现只屏蔽该探针的预期报错，其余断言和权限策略均保留，复测通过。
- 在独立工作区用真实 DeepSeek 模型完成“网页对话 → command → Docker → 工具结果 → 执行完成”，命令返回 `sandbox-ready`、退出码 0；加载最终代码重启服务后，历史仍可读取，新请求返回 `sandbox-ready-final`、退出码 0。容器清理后，SDK 所有权标签下未发现残留容器。

文案修订后的资源页：

![空间与资源页面](assets/weave-web-copy.jpg)

真实沙箱调用结果：

![网页对话中的 Docker 命令执行结果](assets/weave-web-sandbox-verified.jpg)
