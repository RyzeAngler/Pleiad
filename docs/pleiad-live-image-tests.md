# 多模态真实服务验证

SDK 0.2.0的测试入口使用本地源码。看图与生成各自在TOML中填写厂商、模型、`base_url`和`api_key`，不需要设置环境变量。默认测试不访问厂商；传入`--live-config`才启用所选真实测试。2026-11-12已使用用户授权的火山凭据通过下述三项真实测试。

## 准备配置与真实图片

从仓库根目录执行：

```bash
cd /Users/w/Documents/Projects/g-agent
uv sync --project packages/pleiad --frozen
mkdir -p .pleiad-workspace
cp -n packages/pleiad/tests/models/live.example.toml .pleiad-workspace/multimodal.toml
```

`.pleiad-workspace` 已被 Git 忽略。当前模板和本机配置均选择火山方舟：看图使用`doubao-seed-2-0-lite-260428`（LTS），生成/编辑使用`doubao-seedream-5-0-flash-260915`。两项地址均为`https://ark.cn-beijing.volces.com/api/v3`，可在两处填写同一个有相应模型权限的方舟API Key。[官方模型列表](https://console.volcengine.com/ark/region:cn-beijing/docs/model-list)

本机已准备`.pleiad-workspace/fixtures/volcengine-card.png`，测试问题要求读取图中编号，期望为`4827`，编号不在发送给模型的prompt中。该本地测试卡无需外部服务生成。也可替换为真实商品图并同步修改`expected_text`。

看图的`provider_options.extra_body.thinking.type`设置为`disabled`，适用于这组直接读取图片的测试，不涉及加密思考历史回传。Seedream单图生成和参考编辑都使用原生JSON `images/generations`，并非OpenAI的multipart编辑协议。[Chat API](https://docs.volcengine.com/docs/ark/chat-api?lang=zh)、[图片生成API](https://console.volcengine.com/ark/region:cn-beijing/docs/image-generation-api)

编辑配置时填写：

| 字段 | 内容 |
| --- | --- |
| `image_path` | 本机 PNG/JPEG/WebP 静态图片绝对路径，10MB以内、每边不超过8192像素；仅生成时无需读取 |
| `vision.provider` / `protocol` | 当前填`volcengine` / `openai`，表示通过兼容Chat协议调用火山方舟；SDK另支持原有七家厂商 |
| `vision.model` | 账户可调用的视觉型号精确ID |
| `vision.base_url` | 对应地区的对话API基地址；Qwen原生填`https://dashscope.aliyuncs.com/api/v1`，不要用`compatible-mode/v1` |
| `vision.api_key` | 看图服务的真实API Key，直接填写字符串 |
| `vision.prompt` / `expected_text` | 问图中实际编号，期望值填真实编号；不要把答案放进prompt |
| `images.provider` / `model` | 下列原生图片厂商和可调用的图片型号 |
| `images.base_url` | 可省略使用适配器默认值；需换地区时填写原生API前缀，不带具体操作路径 |
| `images.api_key` | 图片生成服务的真实API Key，可与看图服务不同 |
| `images.size` | `auto`或`宽x高`，按厂商约束选择 |
| `images.provider_options` | 厂商专有参数，严格校验，未知字段明确失败 |
| `output_dir` | 产物目录，相对路径以配置文件位置为基准 |

Pillow实际解码输入，自动获得MIME、尺寸与SHA-256，原文件保持不变。下面第一段为当前火山配置，其后是其他厂商的可替换配置；不同厂商分别测试时可保存多份TOML并通过`--live-config`选择文件。模型可用性仍以账户及地区为准。

```toml
[images]
provider = "volcengine"
model = "doubao-seedream-5-0-flash-260915"
base_url = "https://ark.cn-beijing.volces.com/api/v3"
api_key = "YOUR_IMAGE_API_KEY"
size = "1536x1536"
output_format = "png"
provider_options = { response_format = "url", watermark = true }
```

```toml
[images]
provider = "openai"
model = "YOUR_GPT_IMAGE_MODEL"
base_url = "https://api.openai.com/v1"
api_key = "YOUR_IMAGE_API_KEY"
size = "1024x1024"
quality = "low"
output_format = "png"
```

```toml
[images]
provider = "qwen"
model = "qwen-image-2.1-pro"
base_url = "https://dashscope.aliyuncs.com/api/v1"
api_key = "YOUR_IMAGE_API_KEY"
size = "1024x1024"
provider_options = { watermark = true }
```

```toml
[images]
provider = "glm"
model = "glm-image"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key = "YOUR_IMAGE_API_KEY"
size = "1280x1280"
provider_options = { quality = "hd", watermark_enabled = true }
```

```toml
[images]
provider = "minimax"
model = "image-01"
base_url = "https://api.minimax.io/v1"
api_key = "YOUR_IMAGE_API_KEY"
provider_options = { aspect_ratio = "1:1", response_format = "url" }
reference_kind = "character"
edit_prompt = "Place this person in a sunlit garden, preserving their appearance."
```

GLM目前只接入同步生成，不支持参考编辑。MiniMax编辑是人物参考生成，输入须为单张人物JPEG/PNG，不是商品通用编辑。Qwen与OpenAI支持参考编辑，但Qwen不支持mask；mask仅在OpenAI编辑用例中使用。Anthropic、Kimi、DeepSeek没有已接通的公开生图API，不能把其视觉模型填成生成模型。

## 在文件中填写密钥

将示例的`YOUR_VISION_API_KEY`和`YOUR_IMAGE_API_KEY`替换为各自服务的真实Key。只测一种能力时，可以删除不使用的整个`[vision]`或`[images]`配置节。保留的节必须提供非空`api_key`。

本地文件位于已被Git忽略的`.pleiad-workspace/`。加载后密钥使用SecretStr保存，不写入测试报告；配置校验错误会隐藏输入值。旧的`api_key_env`字段已移除。

## 分别执行

命令使用`--live-config=路径`的等号形式，避免pytest在加载tests/conftest.py之前将配置路径误识别为测试收集路径。

看图用例发送两次对话请求：首轮携带真实图片，重开SQLite Session后仅发文字追问。两次都检查真实图片内容，并验证历史图片重新授权。

```bash
uv run --directory packages/pleiad --frozen pytest -q -s --tb=short \
  --live-config=/Users/w/Documents/Projects/g-agent/.pleiad-workspace/multimodal.toml \
  -m provider_e2e tests/models/test_live_images.py::test_live_vision
```

生成一张图，调用所选厂商的原生接口一次：

```bash
uv run --directory packages/pleiad --frozen pytest -q -s --tb=short \
  --live-config=/Users/w/Documents/Projects/g-agent/.pleiad-workspace/multimodal.toml \
  -m provider_e2e 'tests/models/test_live_images.py::test_live_images[generate]'
```

参考编辑，仅对支持的厂商执行：

```bash
uv run --directory packages/pleiad --frozen pytest -q -s --tb=short \
  --live-config=/Users/w/Documents/Projects/g-agent/.pleiad-workspace/multimodal.toml \
  -m provider_e2e 'tests/models/test_live_images.py::test_live_images[edit]'
```

OpenAI若需蒙版，在TOML顶层设置 `mask_path`。必须为带alpha的PNG，尺寸与基础图相同；透明区域允许修改。每次运行创建新的操作ID，可能计费；客户端不会自动重试收费提交。

## 判断是否生效

终端 `Artifacts: ...` 显示结果目录，模板默认位于 `.pleiad-workspace/provider-results/`。

| 用例 | 自动检查 | 人工验收 |
| --- | --- | --- |
| 看图 | 两次Run成功，答案包含expected_text，图片两次重新授权 | 查看turn-1.json/turn-2.json，换不同编号图片再验证 |
| 生成 | 一张真实可解码图片，MIME正确，记录真实用量/请求ID（若厂商返回） | 打开output-1图片，确认符合prompt |
| 编辑 | 输入完整性校验、输出解码、真实请求及结果记录 | 通用编辑确认主体保留；MiniMax确认人物相似性；mask确认修改范围 |

`submission.json`记录厂商、模型和绑定；`request.json`记录请求及引用；`result.json`记录实际图片信息、用量、完成时间，视觉验收始终标记`pending_manual_review`。自动通过不能证明画面质量。

`error.json`记录安全错误类别。`authentication`先核对Key/地区；`invalid_request`核对型号和专有参数；`unsupported_capability`说明所选操作不受支持。`acceptance_unknown`意味着原请求可能已被受理，应先向厂商核对，不能直接重跑当作同一操作。SDK的`recover`仅恢复已取得原始结果后的下载，不重新生成；测试入口不自动重提。

## 验证边界

本地MockTransport契约测试验证真实适配器的路径、请求、响应、错误及下载恢复，不等于真实服务验收。provider_e2e需由上述显式配置执行；OCI不属于本次图片服务验证。官方接口来源、支持范围和本轮本地命令结果见[架构方案](pleiad-multimodal-design.md)。

## 2026-11-12 真实验证记录

使用本地TOML中的火山北京区地址和用户提供的Key，依次执行上文三条命令，均退出0。调用经过实际SDK、Runner/Session或ImageClient及火山接口，没有替换HTTP传输。

| 用例 | pytest结果 | 实际结果 |
| --- | --- | --- |
| `test_live_vision` | 1 passed，5.08s | 首轮答案4827；SQLite重开后答案包含PLEIAD、4827、IMAGE TEST CARD，历史图重新授权验证通过 |
| `test_live_images[generate]` | 1 passed，10.73s | 1536×1536 PNG，白桌上的红色陶瓷杯；返回模型与配置一致 |
| `test_live_images[edit]` | 1 passed，12.37s | 1536×1536 PNG，测试卡背景变浅蓝，编号和文字保留；返回模型与配置一致 |

实际请求共4次：2次对话、1次生成、1次参考编辑。对话分别报告2302和2376 total_tokens；图片分别报告9216 output_tokens及1张输出，未报告input_tokens。未换算费用。助理已打开两张输出核对上述可见内容；输出记录仍保留`pending_manual_review`供用户最终验收。

本机产物在`.pleiad-workspace/provider-results/`：

- `vision-3b0fc510e14e4b7aa6505f11033fed3e/`：两轮答案、用量和SQLite Session。
- `generate-4443b6280b64498a9b18c08639257ff5/`：生成请求、请求ID、模型、用量及output-1.png。
- `edit-c5c7a744508840b7a55d8de955f3ddce/`：参考编辑请求、请求ID、模型、用量及output-1.png。
- `2026-11-12-{vision,generate,edit}.log`：脱敏后的测试输出。

首个使用空格形式参数的命令在pytest解析阶段退出4，未发送请求；随后等号形式的collect-only收集3项真实用例、排除8项本地用例，才执行上述真实测试。只修改本地凭据和文档命令/记录，没有改动SDK或测试代码，因此未重跑完整确定性、打包或OCI门禁。其他厂商、型号、mask、真实工具调用和复杂商品编辑仍未获得本轮真实验收。
