# Weave 默认工具与 Skill

仓库提供本地文件、命令和数据工具、八份工作区 Skill，以及十一项宿主 MCP 连接预设，
覆盖资料研究、旅游规划、数据分析、文档写作、开发和业务应用。MCP 工具通过 Pleiad
实际发现后才进入能力目录；Skill 提供任务方法，不自行建立连接或赋予账号权限。

## 默认内容

| 配置 | 能力 | 默认状态 | 需要准备 |
| --- | --- | --- | --- |
| `websearch` | 多引擎搜索、网页正文、GitHub README、技术文章 | 关闭 | 安装本仓库固定的 Node 依赖，无需 API Key |
| `amap` | 高德地点、路线、天气 | 关闭 | 高德开放平台 Key |
| `baidu_maps` | 百度地图地点、路线、天气 | 关闭 | 百度地图开放平台 AK |
| `xiaohongshu` | 查询攻略、帖子详情、用户公开资料 | 关闭；仅六项查询/登录工具 | 社区 MCP 服务及首次扫码登录 |
| `tuniu_hotel` | 酒店搜索和详情 | 关闭；不开放下单工具 | 途牛开放平台 API Key |
| `tuniu_ticket` | 景点门票查询 | 关闭；不开放下单工具 | 途牛开放平台 API Key |
| `firecrawl` | 搜索、读取网页与提取内容 | 关闭 | Firecrawl API Key |
| `context7` | 查找软件库、检索版本文档与示例 | 关闭 | Context7 API Key |
| `github` | 读取仓库、Issue、PR | 关闭；启用后仍为只读 | 具备目标仓库权限的 GitHub Token |
| `activepieces` | 连接业务应用、管理表格和自动化 | 关闭 | 平台生成的 MCP URL、OAuth 登录和应用连接 |
| `playwright` | 逐步操作网页、查看快照与截图 | 关闭 | 运行 Playwright MCP 的主机或服务器 |

默认关闭连接，不会在未准备账号、网络和浏览器服务时发起远程发现，也不会将
尚未发现的工具伪装成可用工具。启用后，发现失败会明确报错，不会静默忽略该连接。
宿主可以不提供 `mcp.json`，保持原来的本地能力加载行为。

本地工具包括 Pleiad 的 `read_file`、`write_file`、`command`，以及工作区的
`text_statistics` 和 `csv_profile`。后两项通过 OCI 沙箱运行标准库 Python 脚本；
`csv_profile` 统计各列空值和样例，仅对显式选择的数值列计算摘要，不自动覆盖原始类型。
文件读写无需外部账号，命令与脚本需要可用的 Docker/Podman 和宿主配置的固定镜像。

`workspace/capabilities.yaml` 登记这些 Skill：

- `task-analysis`：任务拆解、约束和验收条件。
- `data-audit`：用真实工具结果审计文本或 CSV 数据。
- `tool-development`：开发和验证工作区脚本工具。
- `web-research`：检索、读原文、交叉核对并附来源。
- `travel-planning`：组织攻略、地图路线、酒店和景点信息，形成带日期和依据的行程。
- `document-writing`：将实际资料整理成 Markdown 报告，并读回核对产物。
- `systematic-debugging`：收集失败证据、逐个验证根因假设。
- `verification-before-completion`：执行验收检查，再报告完成情况。

后两份是针对 Weave 的适配版，不安装上游宿主插件、钩子或子 Agent 流程。
每个包保留 `SOURCE.md`、固定上游提交、原始指令和许可证。它们提供工作方法，
由 Agent 检索后通过 Graph 节点的 `skill_codes` 选择，按需读取指令和参考文件；
登记到清单不意味着每次回复都会自动执行这些方法。

能力清单统一使用 `name`、`code`、`description`：`code` 是 Pleiad 的绑定标识，
对应 `workspace/skills/<code>/`；`name` 和 `description` 与包内 `SKILL.md` 一致。
许可证保留在 `SKILL.md` 的可选 `license` 字段及 `LICENSE.txt` 中，不在能力清单重复声明。
`license` 是 [Agent Skills 标准](https://agentskills.io/specification) 的可选元数据，不授予工具权限。

默认不包含 `webapp-testing`：未启用 Playwright MCP 时，单独一份网页操作指引无法
执行浏览器测试。需要网页操作时，先按下文准备真实浏览器服务并验证工具连接。

## 连接配置

从仓库根目录运行命令。依赖已经加入 Weave 的发行依赖与锁文件：

```bash
uv sync --project apps/weave --frozen --all-extras
```

编辑宿主 `.env` 或导出进程环境变量，不要把密钥放进 `workspace/`：

```dotenv
FIRECRAWL_API_KEY=你的密钥
CONTEXT7_API_KEY=你的密钥
GITHUB_TOKEN=你的令牌
ACTIVEPIECES_MCP_URL=平台生成的地址
AMAP_MCP_URL=https://mcp.amap.com/mcp?key=你的高德Key
BAIDU_MAPS_MCP_URL=https://mcp.map.baidu.com/mcp?ak=你的百度AK
TUNIU_API_KEY=你的途牛Key
```

进程环境覆盖 `.env` 中的同名值；MCP 每次加载时重读 `.env`，不展开其中的变量引用。
然后将仓库根目录 `mcp.json` 中准备好的连接的 `enabled` 改成 `true`。
连接配置与凭据必须由宿主管理，不能根据模型返回的地址自动建立连接。
可通过 `PLEIAD_MCP_CONFIG_FILE` 指定其他宿主文件；该文件必须在工具工作区外，
避免模型通过修改配置替换目标地址或借用宿主凭据。

配置字段：

| 字段 | 含义 |
| --- | --- |
| `code` | 唯一的小写连接标识，用于工具命名空间 |
| `enabled` | 是否在能力加载时发现该连接的工具，默认 `false` |
| `url` / `url_env` / `command` | 固定 HTTP(S) 地址、地址环境变量名或 stdio 可执行程序，三选一 |
| `args` | stdio 参数数组；不经 shell 展开，仓库预设从仓库根目录启动 |
| `env` | stdio 非机密设置，如 `MODE=stdio` |
| `env_from` | stdio 子进程环境变量名 → 宿主环境变量名；只引用所需凭据 |
| `auth` | `none`、`bearer` 或 `oauth` |
| `token_env` | `bearer` 模式的密钥环境变量名；其他模式不填写 |
| `headers` | 静态非机密请求头，例如 GitHub 工具集和只读限制；禁止填写 Authorization |
| `header_env` | HTTP 请求头名 → 宿主环境变量名，如途牛的 `apiKey`；同样禁止 Authorization |
| `allowed_tools` | 允许注册的远端工具原名；省略表示全部，空数组表示不注册任何工具；未知名称报错 |
| `timeout_seconds` | 初始化、发现请求与工具执行的超时秒数，默认 60 |

工具保留远端输入契约，注册为 `mcp.<server-code>.<remote-tool-name>`，提供给模型的
名称为 `<server-code>__<remote-tool-name>`。发现后的工具会进入现有资源列表、能力
检索和 Graph 绑定流程，不需要在能力 YAML 中重复逐项登记。
配置在新的能力加载边界生效；运行中的 Run 保留其已绑定的工具。

stdio 是宿主安装的可信程序，不是工作区 Python 脚本的执行入口。每次发现和调用拥有独立
子进程，退出或取消时由 SDK 回收，互不终止其他调用；需要持久会话的浏览器使用下面的 HTTP 服务。
HTTP 和 stdio 配置字段不能混用，请求头名不区分大小写且禁止重复。凭据不会写入工具描述。

### 无账号搜索与网页读取

安装固定版本的宿主服务，随后启用 `mcp.json` 中的 `websearch`：

```bash
npm --prefix apps/weave/mcp ci --ignore-scripts
```

Weave 会按需启动 `open-websearch` 的 stdio 进程，提供搜索、通用网页正文和技术文章读取。
默认采用 HTTP 抓取；验证码、登录页、空搜索结果不能当成目标网页内容。动态页面可明确改用
Playwright。网络受限时按照上游说明，在该连接的 `env` / `env_from` 中配置 `USE_PROXY`、
`PROXY_URL`，不要关闭 TLS 验证。

版本固定在 `apps/weave/mcp/package-lock.json`。搜索包的 `zod` 依赖固定为修复版本 `4.6.6`，
升级时需重新验证 MCP schema 发现和真实查询。不要对这组宿主依赖执行未经检查的 `audit fix --force`。

- [Open-WebSearch 源码和使用说明](https://github.com/Aas-ee/open-webSearch)

### 旅游：攻略、地图、酒店和门票

`travel-planning` 会先检查实际可用工具。小红书帖子用于经验线索；开放时间和预约政策应核对
景区官网，路线要使用地图结果，酒店和票价要携带查询日期、入住/出游条件及币种。搜索摘要或
“起价”不等于指定日期的可订库存。

- **高德 / 百度地图**：将带 Key 的完整 MCP URL 放在宿主 `.env`，启用对应条目。
  预设采用官方 HTTP 接口，不需要安装额外地图服务。API 额度和高级权限取决于各自账号。
  [高德官方文档](https://developer.amap.com/api/mcp-server/gettingstarted)、
  [百度官方文档](https://lbsyun.baidu.com/docs/ai?title=mcpserver%2Fquickstart)、
  [百度开源实现](https://github.com/baidu-maps/mcp)。
- **小红书**：使用社区项目 [xiaohongshu-mcp v2.7.2](https://github.com/xpzouying/xiaohongshu-mcp/releases/tag/v2.7.2)
  对应平台的发布包，在工作区外保存二进制及 cookies，先运行登录程序完成扫码。
  macOS arm64 的服务启动命令为 `./xiaohongshu-mcp-darwin-arm64 -port 127.0.0.1:18060`。
  再启用 `xiaohongshu`；预设仅允许登录检查、二维码、搜索、详情、列表和公开资料，
  不开放发布、评论、点赞、收藏或会清除未读标记的通知工具。它不是小红书官方 API，登录态可能过期。
- **途牛**：在[开放平台](https://open.tuniu.com/mcp/)申请 Key，填写 `TUNIU_API_KEY` 后启用酒店/门票条目。
  凭据通过 `apiKey` 请求头传递。预设只注册酒店搜索、酒店详情和门票查询，不能创建订单。
  端点与工具名来自[官方开源 CLI](https://github.com/tuniucorp/tuniu-cli)；其云端服务需要账号，
  不代表后台或库存数据本身开源。
- **携程 / 去哪儿**：本仓库没有伪造专用 MCP。可用 Playwright 查看用户可访问的公开页面，
  遇到登录/验证码明确停在该步骤。未读取到目标网站时，不得把途牛或搜索结果标成携程/去哪儿报价。

地图、酒店、小红书需要各自 Key 或登录态；配置预设和协议测试不代表这些账号服务已经验收。

### Firecrawl 与 Context7

填写各自 API Key 后启用对应条目。Firecrawl 使用 API Key 端点，Context7 使用其
远程 MCP，均通过 Bearer 认证，不启动本地服务。

- [Firecrawl 官方接入说明](https://docs.firecrawl.dev/mcp-server)
- [Context7 官方接入说明](https://context7.com/docs/resources/all-clients)

### GitHub

为测试仓库准备 Token 后启用 `github`。预设仅开放 `repos,issues,pull_requests`
工具集，并发送 `X-MCP-Readonly: true`。如需写入，先明确目标仓库与授权范围，
再调整远端只读配置及宿主审批策略。空间资源偏好不构成权限授予。

- [GitHub 远程 MCP 文档](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)

### Activepieces 与 OAuth

1. 在平台连接需要的应用账号。
2. 在 Settings → MCP Server 启用 MCP，复制地址到宿主的 `ACTIVEPIECES_MCP_URL`。
3. 在终端执行以下命令并完成浏览器授权；此时配置条目仍可保持关闭。

```bash
uv run --project apps/weave --frozen pleiad mcp-login activepieces
```

4. 登录成功后启用 `activepieces`，并在平台限制所需工具类别。

登录命令只完成认证与工具列表检查，不调用模型、启动沙箱或执行业务动作。
OAuth 可自动刷新令牌；需要重新授权时，后台请求会失败并提示终端登录，不会弹出浏览器。
本版本的交互式登录回调在运行 CLI 的主机上，尚未实现网页内 OAuth 回调。

OAuth 状态默认位于宿主 `.pleiad-workspace/mcp/<workspace-hash>/<server-code>/`，
由私有目录保护并被 Git 忽略；它不在工具可读或容器挂载的工作区内。可通过
`PLEIAD_MCP_AUTH_DIRECTORY` 指向其他宿主目录，禁止设置在工作区之内。
令牌存储在该宿主目录的文件中，不会通过资源 API 返回。

- [Activepieces 官方接入说明](https://www.activepieces.com/docs/mcp/overview)

### Playwright

在浏览器运行主机准备 Node.js 和 Playwright 所需浏览器后，独立启动服务：

```bash
npm --prefix apps/weave/mcp ci --ignore-scripts
npm --prefix apps/weave/mcp run browser
```

Weave 默认连接 `http://127.0.0.1:8931/mcp`。如服务在另一台主机，修改配置中的地址，
并保证 Weave 到 MCP、浏览器到测试站点均可访问。脚本仅监听本机，使用无头、内存隔离配置；
需安装 Chrome，或按上游要求准备浏览器。额外 CLI 参数可在 `npm ... run browser -- ...` 后提供。
进程需要保持运行；结束后连接将不可用。预设不会自动启动该服务。

工具允许列表包含导航、页面快照、表单、点击、标签页、截图等常用操作；不登记宿主安装、
本机文件上传或任意浏览器脚本执行工具。服务和允许列表应一起升级。

`--shared-browser-context` 用于单所有者测试：Pleiad 的 MCP 调用使用独立连接，
共享浏览器上下文可避免每次调用丢失页面状态。不要将该共享上下文提供给不同信任方。
浏览器服务及浏览器状态由启动它的宿主管理，Weave 不接管其生命周期。

本仓库固定 `@playwright/mcp@0.0.68`：实测 `0.0.84` 在独立连接结束后会丢失页面，
不能仅凭同名共享参数判断兼容。升级必须验证“导航 → 断开连接 → 新连接快照/点击”完整步骤，
并检查输出仍包含可供下一步使用的页面内容。

- [Playwright MCP 文档](https://github.com/microsoft/playwright-mcp)

## 启动与验证

完成代码更新后重新启动已有 Weave 服务，之后连接文件和 Skill 变更在新请求中加载。
不要同时启动第二个占用相同工作区的服务。

```bash
uv run --project apps/weave --frozen pleiad serve
curl --fail http://127.0.0.1:8000/v1/resources
```

在网页“空间与资源”中查看真实工具与 Skill。推荐依次验证：

1. Context7：查询指定版本的库文档，检查是否先解析库再读取文档。
2. Firecrawl：搜索主题、阅读多个网页，输出附带真实来源的摘要。
3. GitHub：读取测试仓库的 Issue 和 PR，核对实际内容。
4. Playwright：使用实际发现的浏览器工具完成一次授权的表单交互并观察结果。
5. Activepieces：先读取测试表，再执行明确授权的写入，并读回结果。

这些是人工验收场景，不表示已经连接真实服务或通过端到端测试。云服务的账号、
额度、网络和费用由所选平台决定；MCP 源码开源不代表其云服务免费。

### 本次验证范围（2026-11-05）

已通过真实 Weave/Pleiad 调用验证 Open-WebSearch 搜索与正文读取、Playwright 跨连接表单
填写/点击/读回、CSV 工具的 Docker 执行。临时启用搜索和浏览器时实际加载 28 个工具和
8 个 Skill；正式配置仍保持默认关闭。小红书、地图、途牛及其他账号服务未进行真实业务查询，
也未进行真实模型端到端旅行规划。遇到空结果、广告或受限页面仍需检查内容质量。
