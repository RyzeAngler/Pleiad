# Pleiad 多模态架构方案

2026-11-10，基线 `6727c0c`，实施分支 `feat/sdk-multimo`。

## 目标与边界

本次交付图片输入、图片生成与参考图编辑，以及贯穿 Session、上下文预算、协议投影和恢复的图片契约。用户要求覆盖 SDK 原有七家厂商，并进一步指定新增火山方舟。Canvas 的 `pleiad-capabilities-handoff.md` 仅作为业务背景，不能以其中的阶段范围缩小用户要求。图片身份和通用调用机制归 Pleiad，资产归属、文件解码、版本、外部操作账本和收费授权归宿主。

Agent 继续绑定对话模型。图片操作客户端由宿主创建，通过现有 FunctionTool 和 ToolRuntime 调用。Handoff 沿用同一 Run、Execution 和 Session。独立 Worker 继续使用 child Execution。

## 数据契约

`pleiad.messages.images` 定义持久化 `ImageReference`、`ImageBlock`，以及内存中的 `ResolvedImage`、`ImageReadScope`、`ImageResolver`、`ImageLimits`。

- `ImageReference` 包含不透明 reference、64 位小写 sha256、MIME、正整数 width 和 height。支持 PNG、JPEG、WebP 和 GIF 的引用。
- `ImageBlock` 使用 `type=image` 判别。`UserMessage.content` 和 `InputEnvelope.content` 接受有序 TextBlock/ImageBlock 元组，支持纯图片输入。空元组明确失败。
- `InputEnvelope.payload_digest` 对完整有序结构计算摘要。文本和 ResultReference 沿用原有表示。多模态 envelope 投影为元数据文本块和原内容块，message_id、source、kind、causation_id 保留。
- `ResolvedImage(image, data)` 为 frozen/slotted dataclass，data 从 repr 排除。Session、checkpoint、trace 仅保存引用和摘要。

## 解析、授权与请求边界

宿主注入异步 `ImageResolver(ImageReference, ImageReadScope) -> ResolvedImage`。scope 明确包含 session_id 和 run_id，宿主依据当前会话和保留资产校验访问权。回调必须校验文件解码、方向、MIME 和尺寸。SDK 再次核对返回的完整元数据、实际字节 SHA-256、单图字节、总字节、数量和尺寸上限。

Runner 接收 image_resolver、image_limits 和可选 token_counter。AgentLoop 在上下文确定后、模型请求受理前解析图片。每次请求重新授权，恢复和后续纯文字输入同样会解析保留的历史图片。解析失败产生明确运行失败，模型请求次数保持为零。取消沿现有 asyncio 生命周期传播。

协议投影接收请求级 ResolvedImage 集合。OpenAI Chat Completions 使用 image_url/data URL，Anthropic Messages 使用 image/source/base64。同步请求与流式请求共用投影。临时编码仅存在于网络请求，ModelInput.digest 使用持久引用。原始 provider 协议客户端仍可接收其自身请求结构。

## 上下文与预算

ContextRenderer 将动态上下文加入独立文本块，原用户内容块按原顺序保留。字符串输入维持现有渲染形式。

ApproximateTokenCounter 对文本和图片分别估计。图片估计使用宿主按精确模型绑定提供的 `ImageTokenEstimator`。无规则时默认明确失败，宿主可以显式允许未知估计，BudgetStatus 同时暴露未估算图片数量和 image_tokens 的未知值。图片二进制不参与字符计数。物理大小限制独立于 token 预算。

压缩保留含图片的完整 ModelStep，再加入最近步骤，按原 sequence 排序，保留工具调用配对。摘要仅覆盖可移除的连续历史前缀，避免跨过固定图片步骤而遗漏后续摘要。图片历史和当前输入超出模型窗口时明确失败，由宿主开启新 Session 并选择需要继续保留的资产。宿主资产 GC 依据 Session 与未结算调用保留引用。

## 图片操作客户端

`pleiad.models.images.client.ImageClient(provider=..., model=..., api_key=...)` 提供 generate、edit、recover、aclose。统一使用已安装的异步 HTTPX 发送各厂商公开原生 HTTP 协议。五个适配器分别处理 OpenAI Images、Qwen DashScope 多模态生成、GLM Images、MiniMax image_generation 和火山方舟 Seedream；它们只负责参数校验、原生序列化、响应解析及业务错误，不各自维护重复的操作状态机。Anthropic、Kimi、DeepSeek 若无公开生图接口，明确拒绝创建生图绑定，图片理解仍由现有对话适配器负责。

共享客户端负责操作身份、并发去重、授权解析、错误分类、结果下载和恢复。所有收费提交均只尝试一次，网络中断不会触发自动重试。厂商适配器在资产读取和请求发送之前拒绝不支持的参数。使用原生端点与参数，不通过替换 OpenAI base_url 假定兼容。

`ImageGenerationRequest` 保留 operation_id、prompt、count、size、quality、output_format、background。size 使用统一 `宽x高` 格式，由适配器按厂商要求转换；auto 和未指定的格式使用厂商默认值。`provider_options` 由每个适配器的 Pydantic 模型严格校验，不允许覆盖 model、prompt、参考图等通用契约。编辑包含有序 references、base_reference、可选 mask，以及 `reference_kind=general|character`。MiniMax 人物参考必须显式选择 character，不能作为商品通用编辑使用。base_reference 必须是第一张图，mask 与其尺寸一致。

能力状态使用 supported、unsupported、unverified。厂商协议不支持的能力不能被宿主声明升级；有接口的能力仍需以具体模型、端点、账户进行实测，默认 unverified。同步协议的 query_operation、cancel_operation、idempotent_submission 为 unsupported。客户端依赖 HTTPX，安装对应 vendor extra 即可，不增加运行时依赖。

返回真实 bytes、MIME、实际 revised_prompt、供应商 request_id、配置模型与实际 reported_model、可空用量及完成时间。零张图明确失败，部分返回保留实际数量。URL 结果在返回前下载，下载失败携带原响应引用供 `recover` 重读，恢复下载不再次生成。原响应引用属于宿主外部调用记录，应按敏感短期传输数据管理。

operation_id 是宿主的持久化操作身份。客户端不提供跨进程幂等账本。宿主必须在调用前登记请求摘要和配置版本，重试时返回已保存的结果，语义变化时报冲突。相同 operation_id 在一个客户端生命周期内也应拒绝语义冲突和重复付费提交。网络断开、超时、5xx 等受理不确定错误保留 unknown。取消传播 CancelledError，宿主将已登记的在途操作标记 unknown。原生图片客户端不提供自动重试选项，每次收费提交只发送一次。

错误区分 invalid_configuration、unsupported_capability、authentication、rate_limit、quota、invalid_response、download_failed 和 acceptance_unknown。错误保留 operation_id、可公开请求 ID 和异常 cause，公开错误消息不包含请求正文、密钥或临时 URL。

## 配置指纹和恢复

对话客户端和图片客户端提供 binding_fingerprint，包含协议、provider、规范化 base_url、model、非密钥请求参数和适配器版本。API Key、认证头、URL 认证信息和请求参数中的凭据字段从指纹输入排除，已识别凭据的重复值也脱敏。凭据识别与值收集逻辑由 SDK 提供，Weave 资源详情复用它。路由查询参数顺序及有语义差异的路径保持可区分。

Agent 恢复指纹纳入对话 binding_fingerprint。工具绑定图片客户端时，宿主将图片绑定摘要纳入 ToolDescriptor.code 及对应 ToolRef，利用现有工具身份参与恢复校验，并在外部调用账本固定凭据版本。新默认配置用于之后接受的操作，旧操作恢复使用原配置。已有纯文本 Session 数据可读，旧版本未包含完整模型绑定的在途 checkpoint 将拒绝跨版本恢复，宿主应先完成或结算旧任务。

ImageTokenEstimator 绑定到宿主选择的模型规则。一个 Runner 使用多个模型或 Handoff 时，宿主应提供覆盖这些绑定的计数规则。自定义 TokenCounter 负责完整计数，只有 ApproximateTokenCounter 提供图片细分估计。

## 实施与验收

先建立公共图片契约，再贯通消息、上下文和运行链路，随后实现图片操作客户端和故障恢复，最后更新公开使用文档并运行两个发行包的质量门禁。

| 行为 | 验证入口 |
| --- | --- |
| 混排、纯图片、无效元数据、幂等冲突 | UserMessage、InputEnvelope、Session |
| 两种协议真实图片结构、流式工具往返 | Runner 与供应商模拟 HTTP 传输 |
| 次轮纯文字、SQLite 重开、恢复授权 | Session、Runner.start/resume |
| 图片缺失、摘要变化、权限失败、物理限制 | 异步 resolver 与请求计数 |
| 动态上下文、压缩、未知预算 | ContextRenderer、BudgetGuard、ContextCompactor |
| 生成、编辑、mask、部分结果、缺失用量 | ImageClient、五家原生适配器与模拟 HTTP 传输 |
| 超时、响应丢失、下载恢复、并发重复提交 | 故障注入、屏障与供应商请求计数 |
| URL/模型/参数变化和密钥轮换 | binding_fingerprint 与 checkpoint 恢复 |

确定性验证覆盖可复现的 SDK 契约。真实 provider_e2e 需要宿主提供精确模型和可用服务配置，独立记录看图、工具往返、生成、参考编辑和 mask 的实测结果。OCI 验证单独列出。

## 协议依据与厂商覆盖

2026-11-10 联网核查 SDK 已有七家厂商，随后按用户明确指定新增火山方舟，看图和图片生成分别适配。当前生成适配层覆盖五家。文档存在不代表具体账户、地区和型号已通过真实服务验收。

| 厂商 | 图片理解 | 图片生成层 | 参考编辑 / mask |
| --- | --- | --- | --- |
| OpenAI | Chat Completions image_url | 原生 `/v1/images/generations` | `/v1/images/edits` multipart，通用参考和mask |
| Qwen | 原生 DashScope 多模态消息 | `/api/v1/services/aigc/multimodal-generation/generation` | 同一路径有序image/text块；无显式mask |
| GLM | 原生Zhipu SDK对话 | `/api/paas/v4/images/generations` | 同步生成文档无参考图和mask字段，提交前拒绝 |
| MiniMax | 视觉型号Chat图片输入 | `/v1/image_generation` | `subject_reference`，仅显式人物参考；无通用商品编辑和mask |
| 火山方舟 | 公开兼容Chat API，经OpenAI SDK调用 | `/api/v3/images/generations` | 同一路径JSON image字段，Seedream 5.0 Flash/Pro单图参考编辑；本适配不暴露mask/图层拆分 |
| Anthropic | Messages图片输入 | 当前模型文档为文本输出；构造图片生成客户端时拒绝 | 未查到公开图片生成/编辑API |
| Kimi | Chat图片输入 | 已查公开API为对话/Responses/Messages；构造图片生成客户端时拒绝 | 未查到公开图片生成/编辑API |
| DeepSeek | 指定视觉型号图片输入 | 已查公开API描述图片输入、文本响应；构造图片生成客户端时拒绝 | 未查到公开图片生成/编辑API |

### 原生参数与模型范围

- OpenAI适配GPT Image栅格生成和编辑。数量1–10，PNG/JPEG/WebP，auto/low/medium/high质量，背景选项。可使用provider_options中的output_compression、编辑input_fidelity、生成moderation及user。DALL-E旧协议未混入此契约。
- Qwen统一同步路由支持2.0、2.1、3.0系列及旧版edit/edit-plus/edit-max；已知旧版qwen-image/plus/max异步生成标识明确拒绝。当前型号最多6张输出，基础qwen-image-edit为1张。2.1参考上限10张，其余已接入系列为3张。原生参数有seed、watermark、negative_prompt和按型号校验的提示词扩展/thinking。尺寸转换为宽*高。仅PNG输出；显式background和mask不支持不等于模型不能根据提示生成透明素材。
- GLM支持glm-image、cogview-4-250304、cogview-4、cogview-3-flash，一次一张URL结果。quality、watermark_enabled、user_id是原生选项，不能直接使用OpenAI的quality含义。GLM-Image使用32倍数尺寸，CogView为16倍数，各自校验像素预算。厂商另有异步任务接口，本客户端不提交该接口。
- MiniMax支持image-01生成，以及image-01/image-01-live人物参考。数量1–9；人物参考须一张JPEG/PNG，显式reference_kind=character。原生aspect_ratio、response_format、seed、prompt_optimizer严格校验。没有编码格式、quality和background参数，不能默默忽略。width/height仅用于image-01，范围512–2048、8的倍数，并且不能同时指定aspect_ratio。
- 火山方舟新增`volcengine`绑定：Chat复用现有OpenAI兼容传输实现，默认地址为`https://ark.cn-beijing.volces.com/api/v3`。选定LTS视觉模型`doubao-seed-2-0-lite-260428`并在测试配置显式关闭thinking。图片适配接入`doubao-seedream-5-0-flash-260915`及`doubao-seedream-5-0-pro-260628`标准单图生成/参考编辑，最多10张授权参考图；独立序列化JSON image数组、尺寸、编码、watermark及原生响应。真实usage.generated_images映射image_count，缺失字段保留未知。只新增已有OpenAI/HTTPX依赖的optional extra，无额外第三方依赖。

以上是已实现的同步操作范围，不宣称覆盖每个厂商的所有历史型号、异步任务API、视频/动图/矢量输出或其他托管产品。原生HTTPX适配器复用一套操作状态、下载恢复和错误契约，没有替换base_url冒充原生适配，也没有未实现的占位成功分支。

### 官方来源

- [OpenAI Images生成和编辑](https://developers.openai.com/api/docs/guides/image-generation)、[图片理解](https://developers.openai.com/api/docs/guides/images-vision)
- [Qwen统一生成/编辑](https://help.aliyun.com/en/model-studio/qwen-image-generation-and-editing-api-reference)、[Qwen编辑](https://help.aliyun.com/en/model-studio/qwen-image-edit-api)、[官方旧版接口补充](https://raw.githubusercontent.com/QianWen-AI/qianwen-ai/main/skills/image/qianwen-image-generation/references/api-guide.md)
- [GLM生成API](https://docs.bigmodel.cn/api-reference/模型-api/图像生成)、[GLM-Image尺寸约束](https://docs.bigmodel.cn/cn/guide/models/image-generation/glm-image)、[GLM错误码](https://docs.bigmodel.cn/cn/api/api-code)
- [MiniMax生成](https://platform.minimax.io/docs/api-reference/image-generation-t2i)、[人物参考](https://platform.minimax.io/docs/api-reference/image-generation-i2i)、[单参考图限制](https://platform.minimax.io/docs/guides/image-generation)、[错误码](https://platform.minimax.io/docs/api-reference/errorcode)
- [Anthropic模型输入/输出](https://platform.claude.com/docs/en/models/overview)、[Vision](https://platform.claude.com/docs/en/build-with-claude/vision)
- [Kimi公开API入口](https://platform.kimi.com/docs/get-api-key)、[Chat接口](https://platform.kimi.com/docs/api/chat)、[Vision](https://platform.kimi.com/docs/guide/use-kimi-vision-model)
- [DeepSeek Chat接口](https://api-docs.deepseek.com/api/create-chat-completion/)、[Vision](https://api-docs.deepseek.com/guides/vision/)
- [火山模型列表](https://console.volcengine.com/ark/region:cn-beijing/docs/model-list)、[Chat API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh)、[图片生成API](https://console.volcengine.com/ark/region:cn-beijing/docs/image-generation-api)

## 实现与行为证据

现有看图适配保留真实Runner、Session、协议客户端链路。Qwen图片输入使用AioMultiModalConversation和原生image/text块，纯文字首轮需要多模态路由时由provider_options.multimodal显式指定。GLM/Qwen在适配器消费stream_options.include_usage，映射max_completion_tokens为max_tokens并拒绝冲突。GLM原生SDK响应补齐内部OpenAI协议判别字段，内容、模型和用量取真实响应。

图片生成层使用公开ImageClient和真实适配器，只在外部HTTP边界模拟。新增测试使用简短场景名，原OpenAI生命周期测试迁移到统一客户端。没有保留OpenAIImageClient旧别名。

| 行为 | 测试证据 |
| --- | --- |
| 原有四家原生生成端点与请求/结果 | test_generation_partial_result、test_qwen_native_generation、test_glm_native_generation、test_minimax_native_generation |
| 火山看图默认/自定义地址、complete/stream和真实Runner图片投影 | test_volcengine_chat、test_image_http_payload[volcengine] |
| Seedream原生单图生成、参考编辑、限制、异常及原结果恢复 | test_volcengine_generation、test_volcengine_edit、test_volcengine_rejects_request、test_volcengine_error、test_volcengine_recovery |
| 参考顺序、授权字节与mask | test_edit_reference_order_and_mask、test_qwen_reference_order、test_minimax_character_edit |
| 不支持的操作在读资产/发请求前失败 | test_qwen_rejects_edit、test_glm_rejects_edit、test_minimax_rejects_edit、test_no_generation_api |
| 厂商尺寸/数量/选项及边界 | test_qwen_rejects_request、test_qwen_rejects_options、test_glm_size_limits、test_glm_rejects_options、test_minimax_native_options、test_minimax_rejects_request |
| 原生业务错误、追踪ID和信息脱敏 | test_qwen_safe_errors、test_glm_business_error、test_minimax_business_error、test_provider_errors_are_sanitized |
| 受理未知后不重复收费提交 | test_response_loss_blocks_replay、test_native_response_loss、test_generation_deduplicates |
| 重启后下载原结果、不传厂商凭据 | test_download_recovers_after_restart、test_native_download_recovery |
| Session与工具往返 | test_image_tool_roundtrip、test_image_followup_after_reopen、test_image_resume_reauthorizes |
| 预算与压缩 | test_compaction_retains_image_steps、test_image_budget_partial_estimates |

## 验证记录

全部从仓库根目录执行，下列最终门禁退出码均为0。默认provider用例跳过表示隔离生效，不代表服务验收。

| 命令 | 实际结果 |
| --- | --- |
| `uv run --directory packages/pleiad --frozen ruff format --check .` | 245 files already formatted |
| `uv run --directory packages/pleiad --frozen ruff check .` | All checks passed |
| `uv run --directory packages/pleiad --frozen mypy` | 249 source files，无错误 |
| `uv run --directory packages/pleiad --frozen pytest -q -m 'not oci and not provider_e2e'` | 955 passed，13 deselected，1 warning，18.38s |
| `uv run --directory apps/weave --frozen ruff format --check .` | 83 files already formatted |
| `uv run --directory apps/weave --frozen ruff check .` | All checks passed |
| `uv run --directory apps/weave --frozen mypy` | 85 source files，无错误 |
| `uv run --project apps/weave --frozen lint-imports --config .importlinter` | 7 kept，0 broken |
| `uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'` | 277 passed，3 deselected，87.93s |
| `uv run --directory apps/weave --frozen pytest -q -m packaging` | 2 passed，278 deselected；wheel与sdist干净安装，包含volcengine模块与extra依赖元数据 |
| `uv run --directory packages/pleiad --frozen pytest -q -rs tests/models/test_live_images.py` | 8 passed，3 skipped；配置回归通过，未开启真实服务调用 |
| `uv lock --project packages/pleiad --check` | 121 packages，锁文件一致 |
| `uv lock --project apps/weave --check` | 130 packages，锁文件一致 |
| `git diff --check` | 通过 |

SDK warning来自已有DashScope Assistants弃用提示。原生适配阶段已用本地Python验证示例参数。随后按用户要求改为TOML直接填写api_key/base_url，使用--live-config选择文件并启用所选测试，删除环境变量读取路径。新增8项配置回归验证两家独立密钥/地址、路径解析、空缺密钥及错误脱敏；CLI collect-only成功收集3个真实用例（8个本地用例deselected），未执行模型调用。后续火山适配完成后重跑SDK全套955项确定性测试、Weave格式/lint/mypy、7条导入边界和两种distribution安装验证；Weave277项业务测试结果保留原有一轮记录。火山视觉模块7个新增参数场景及Seedream52个行为场景均由真实SDK/适配器加外部假HTTP传输验证。此前分模块修改阶段的测试已通过；最终完整门禁覆盖合并后的实现与简短测试名。Pillow仅为测试图片解码所需的开发依赖，未增加SDK运行时依赖。

以上为2026-11-10的确定性和打包验证记录。2026-11-12使用用户授权的火山凭据补跑真实provider_e2e：看图1 passed（5.08s）、生图1 passed（10.73s）、参考编辑1 passed（12.37s）。两次真实对话及两次Seedream图片请求均成功，图片已解码并由助理打开核对基本内容；用户最终视觉验收仍待确认。其他厂商、型号及OCI未做本轮真实验证。配置、精确命令和产物位置见[真实服务验证指南](pleiad-live-image-tests.md#2026-11-12-真实验证记录)。

宿主仍负责真实图片解码、资产授权、跨进程收费操作账本、凭据版本与模型token规则。旧文本Session可读，旧版在途checkpoint需使用原版本结算。图片历史超过窗口时，由宿主选择资产并开启新Session。2026-11-12已发布SDK 0.2.0，最终门禁、发行文件校验和PyPI安装验证见[发布记录](pleiad-0.2.0-release.md)。

本机`.pleiad-workspace/multimodal.toml`的两节均选择火山方舟。2026-11-10使用640×480编号测试卡完成离线校验；2026-11-12按用户请求在两节填入提供的API Key并完成上述真实请求，Key没有写入测试报告或Git文件，配置文件权限为0600。
