# 模型、供应商与 API Key

## 在控制台使用

输入 `/model` 打开方框面板，首屏显示当前供应商、请求地址、模型、思考档位和遮挡的 Key。主菜单以供应商为单位提供添加、编辑、删除和切换，同时保留模型选择与思考强度。不再单独提供添加 Key、删除 Key 或命名 Key 切换菜单。

选择「添加供应商」，先搜索官方预设或选择「自定义请求地址」，随后进入一张表单，统一填写供应商名称、请求地址、API Key、模型与可选备注。预设填好地址、协议和示例模型；同一服务可以按不同账户、地区或 Key 添加成多条供应商配置。请求地址可以填 API 根地址或完整的 `/chat/completions`、`/responses`、`/messages` 地址；完整地址会识别协议并去除末尾接口路径，避免请求时重复拼接。

Tab / Shift+Tab 切换字段与按钮，Ctrl+S 保存，Esc / Ctrl+C 取消。Key 默认密码显示，通过「显示 Key」或 F2 在表单内切换显隐，退出后恢复控制台。编辑时保留原 Key，可直接替换；清空后使用该预设的环境变量，本地免密服务可保持空值。内容不进入普通输出或命令历史。

添加保存后启用新供应商。编辑当前供应商时，名称、地址、Key 与模型一起保存并立即生效；编辑其他供应商只更新其配置，不切换当前选择。取消和测试草稿都不写入存储。删除供应商同时移除该条配置的地址、Key、模型与备注；删除当前项后切到剩余第一条配置，没有剩余配置时回到未配置的标准 API 状态。

模型字段可输入完整模型 ID；已有供应商的模型选择页继续支持刷新远端目录及手动 ID。动态目录以服务端返回为准，示例模型不代表账户权限。Trina 不再读取其他 Agent 的模型数据库或鉴权文件。

## 表单内测试

| 按钮 | 实际检查 | 结果含义 |
|---|---|---|
| 连通测试 / F5 | 对草稿请求地址发送不带 Key 的 GET，获取 HTTP 状态和耗时，超时最多 10 秒 | 任意 HTTP 响应表示地址可达；包括 401、403、404、5xx，均不代表 Key 或模型可用 |
| 模型验证 / F6 | 使用草稿中的地址、Key、协议和模型发送一次最多 128 输出 Token 的短请求 | 返回有效模型响应才算验证通过；HTTP 鉴权失败、模型错误、空响应等显示失败原因 |

连通测试不需要先填模型，也不调用生成接口。模型验证会产生一次短模型请求，可能有少量 Token 用量。两种测试都在后台执行，结果显示在当前表单；测试中仍可修改字段，修改后旧结果失效，需要重新测试。测试使用草稿，不保存、不切换供应商，也不改变当前对话用量台账。

| 命令 | 行为 |
|---|---|
| `/model` | 供应商配置与模型管理首页 |
| `/model add [预设ID]` | 添加完整供应商配置 |
| `/model edit [供应商名称]` | 编辑名称、地址、Key、模型和备注 |
| `/model remove [供应商名称]` | 删除完整供应商配置 |
| `/model test [供应商名称]` | 测试指定或当前供应商地址的连通性 |
| `/model use <供应商名称>` | 切换并保存当前供应商 |
| `/model list` | 查看供应商预设模型目录 |
| `/model <已保存连接>` | 切换该连接 |
| `/model <模型ID>` | 修改模型；已有连接自动保存，旧会话设置用 `/save` 保存 |

## 存储和配置顺序

供应商配置保存在用户目录 `~/.trina/providers.json`，格式为 `version=2`，每条配置有一个 `api_key`，使用原子替换保存，POSIX 权限为 0600。文件是本机 JSON，不作加密存储；`TRINA_PROVIDERS_FILE` 可以指定独立文件，默认位置在仓库外。旧 `version=1` 文件不再自动迁移，载入时会明确报“供应商配置版本不受支持”；需要的连接应在 `/model` 中按供应商整条重新添加。

默认启动顺序为项目 YAML → 已选择的本地连接 → `TRINA_LLM_*` 环境变量 → 命令行 `--set`。显式 `--config` 文件仅在指定 `llm.profile` 时读取连接。`TRINA_API`、`TRINA_MODEL` 和启动脚本的旧选择参数不再参与装配。

配置有非空 `api_key` 时使用保存值，否则读取其 `api_key_env`。当前供应商的编辑、切换会重建后端，下一次请求立即生效，并保留聊天历史；运行中拒绝模型管理命令。旧 `/model keys` 仅作为进入供应商编辑页的兼容入口，不再维护独立 Key 对象。

## 思考强度（2026-11-05）

思考设置保存为 `llm.reasoning_effort`。默认值 `default` 不发送额外思考参数；其他值通过模型原生 API 参数生效，不向系统提示词添加「多思考」之类的文字。不同连接各自保存档位。切换到不支持原档位的模型时恢复「默认」。

| 模型 / 接口 | 页面选项与请求行为 |
|---|---|
| 已识别的 GPT / o 系列 | 按模型提供强度选项；Chat 使用 `reasoning_effort`，Responses 使用 `reasoning.effort`；不向全部型号统一提供关闭选项 |
| DeepSeek | 默认、关闭、低、高、最高；Chat 的开关为 `thinking.type`，强度为 `reasoning_effort`；Responses / Messages 使用对应字段 |
| Claude 原生 Messages | 新型号使用 adaptive thinking 与 `output_config.effort`；旧扩展思考型号按低 / 中 / 高分配 1024 / 2048 / 8192 Token 预算，预算必须小于输出上限 |
| GLM 4.5–4.7 / 5、Kimi K2.5 | 仅提供默认、关闭、开启，通过 `thinking.type` 设置；不展示没有区分作用的中、高档位 |
| 可切换思考的 Qwen3 / Qwen Plus / Turbo | 默认、关闭、开启，通过 `enable_thinking` 设置；带 coder / thinking / instruct 的固定模式型号只提供默认 |
| 已识别的 Gemini 思考型号 | 默认、低、中、高，使用官方 OpenAI 兼容接口的 `reasoning_effort` |
| OpenRouter | 已识别模型沿用对应选项，请求转换为网关的 `reasoning` 对象 |
| 未识别型号 | 只显示默认，不猜测自定义网关能力 |

Claude 预算型模型的短输出健康检查不足 1024 Token 时关闭本次思考，常规请求仍使用已保存档位。界面按已知模型家族识别能力，网关的别名和部署名可能无法识别；模型权限、支持范围和参数最终以服务端为准。各协议已验证本地请求格式，未使用所有付费账户在线调用。

参数依据：[OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)、[OpenRouter reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。

## 协议与模型目录

内置协议为 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。Google Gemini 使用其官方 OpenAI 兼容入口。Claude 使用 Messages 的 system 字段、版本头和鉴权头；响应文本及 Token 用量转换为现有 LLM 后端契约。OpenCode Go 自动带稳定 `x-opencode-session` 和 Trina User-Agent。

「从供应商刷新模型列表」显式请求当前连接的 `/models`，不在启动或键盘补全时联网。OpenCode Go 使用 `/inference/go/v1/models`。不提供模型目录的服务仍可手动输入 ID；带命名空间的 ID（例如 `meta-llama/...`）保留完整内容。Azure 需要填写自己的资源地址和部署名。AWS Bedrock / Google Vertex 的 IAM 签名接入不属于本页的 API Key 协议，当前尚不支持。

## 内置供应商目录（2026-11-04）

以下预设提供连接入口与文档。接口地址可以在添加页面调整；模型清单与服务可用性以账户和官方平台为准。协议的本地请求 / 响应契约有自动化测试，未使用全部供应商的付费账户逐一验证。

| ID | 名称 | 默认 Base URL | 协议 | 官方文档 |
|---|---|---|---|---|
| `openai` | OpenAI | `https://api.openai.com/v1` | chat | [文档](https://platform.openai.com/docs/api-reference/chat) |
| `anthropic` | Anthropic / Claude | `https://api.anthropic.com/v1` | anthropic | [文档](https://platform.claude.com/docs/en/api/overview) |
| `google` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | chat | [文档](https://ai.google.dev/gemini-api/docs/openai) |
| `deepseek` | DeepSeek | `https://api.deepseek.com/v1` | chat | [文档](https://api-docs.deepseek.com/) |
| `xai` | xAI / Grok | `https://api.x.ai/v1` | chat | [文档](https://docs.x.ai/docs/api-reference) |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | chat | [文档](https://docs.mistral.ai/api/) |
| `qwen` | 阿里百炼 / 通义千问（北京） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | chat | [文档](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope) |
| `qwen-intl` | 阿里百炼（新加坡） | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | chat | [文档](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope) |
| `ark` | 火山方舟 / 豆包 | `https://ark.cn-beijing.volces.com/api/v3` | chat | [文档](https://www.volcengine.com/docs/82379/1330626) |
| `baidu` | 百度千帆 / 文心 | `https://qianfan.baidubce.com/v2` | chat | [文档](https://cloud.baidu.com/doc/qianfan-api/s/3m7of64lb) |
| `zhipu` | 智谱 BigModel | `https://open.bigmodel.cn/api/paas/v4` | chat | [文档](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) |
| `zai` | Z.ai（国际） | `https://api.z.ai/api/paas/v4` | chat | [文档](https://docs.z.ai/guides/develop/openai/introduction) |
| `moonshot` | 月之暗面 / Kimi | `https://api.moonshot.cn/v1` | chat | [文档](https://platform.moonshot.cn/docs/intro) |
| `minimax` | MiniMax（国内） | `https://api.minimaxi.com/v1` | chat | [文档](https://platform.minimaxi.com/docs/api-reference/text-openai-api) |
| `minimax-intl` | MiniMax（国际） | `https://api.minimax.io/v1` | chat | [文档](https://platform.minimax.io/docs/api-reference/text-openai-api) |
| `stepfun` | 阶跃星辰 | `https://api.stepfun.com/v1` | chat | [文档](https://platform.stepfun.com/) |
| `hunyuan` | 腾讯混元（原平台） | `https://api.hunyuan.cloud.tencent.com/v1` | chat | [文档](https://cloud.tencent.com/document/product/1729/111007) |
| `tokenhub` | 腾讯 TokenHub | `https://tokenhub.tencentmaas.com/v1` | chat | [文档](https://cloud.tencent.com/document/product/1772) |
| `groq` | Groq | `https://api.groq.com/openai/v1` | chat | [文档](https://console.groq.com/docs/openai) |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | chat | [文档](https://inference-docs.cerebras.ai/api-reference/chat-completions) |
| `together` | Together AI | `https://api.together.xyz/v1` | chat | [文档](https://docs.together.ai/docs/openai-api-compatibility) |
| `fireworks` | Fireworks AI | `https://api.fireworks.ai/inference/v1` | chat | [文档](https://docs.fireworks.ai/tools-sdks/openai-compatibility) |
| `nvidia` | NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | chat | [文档](https://docs.api.nvidia.com/nim/reference/llm-apis) |
| `perplexity` | Perplexity | `https://api.perplexity.ai` | chat | [文档](https://docs.perplexity.ai/guides/chat-completions-guide) |
| `siliconflow` | 硅基流动 | `https://api.siliconflow.cn/v1` | chat | [文档](https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions) |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | chat | [文档](https://openrouter.ai/docs/quickstart) |
| `opencode-go` | OpenCode Go 订阅 | `https://opencode.ai/inference/go/openai/v1` | chat | [文档](https://opencode.ai/v2/docs/console/go/) |
| `azure` | Azure OpenAI | 用户资源地址 | chat | [文档](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/switching-endpoints) |
| `ollama` | Ollama 本地 | `http://127.0.0.1:11434/v1` | chat | [文档](https://docs.ollama.com/api/openai-compatibility) |
| `lmstudio` | LM Studio 本地 | `http://127.0.0.1:1234/v1` | chat | [文档](https://lmstudio.ai/docs/developer/openai-compat) |

## 设计参考与验证

参考 [omp 的供应商与凭据设计](https://github.com/can1357/oh-my-pi/blob/main/docs/providers.md) 中的供应商命名空间、模型选择和凭据来源表达，以及 [omp 自动补全接口](https://github.com/can1357/oh-my-pi/blob/main/packages/tui/src/autocomplete.ts) 中的命令与参数目录。Trina 使用现有 Python / prompt_toolkit 实现，没有引入 omp 的 TypeScript 运行栈。

供应商整条表单参考 [CC Switch 添加供应商](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/2-providers/2.1-add.md)，地址测试与实际模型调用的区别参考其[连通性检查说明](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/en/4-proxy/4.5-model-test.md)。

验证入口为 `tests/test_providers.py`、`tests/test_provider_form.py`、`tests/test_reasoning.py`、`tests/test_llm_openai.py` 与 `tests/test_terminal_ui.py`。覆盖供应商增删编辑、拒绝旧格式、草稿隔离、测试语义、异步结果失效、实际按键及终端屏幕恢复。工具授权、审计与目标护栏继续由既有执行链处理。

## 流式传输与标准消息

在线请求统一由 `OpenAICompatBackend.complete_messages()` 发出，`backend` 仅保留 `openai` 与离线 `replay`。OMP 后端、`/backend` 命令、OMP 模型数据库读取与独立模型持久化均已删除；`.omp/skills` 和可选工具扩展仍作为项目技能与外部调用集成存在。

Chat Completions 发送标准 `messages` 数组；Anthropic Messages 将首条 system 放入 `system`，其余消息保留角色；Responses 将首条 system 放入 `instructions`，其余消息以结构化 `input` 数组发送。多轮历史保留 user/assistant 边界；原生工具调用保留 tool_calls 与匹配的 tool 结果。消息不通过 `USER:` 或 `ASSISTANT:` 标签字符串模拟。

系统提示词和初始运行合同只写一次，动态状态、补充与观察逐轮追加。缓存由供应商按其分词和前缀规则决定；稳定消息前缀为复用提供条件，实际命中只采用 API 报告值，不能用请求 JSON 的字节相同直接推定命中。

三个 API 方言分别解析 SSE，规范化为 start、text_delta、thinking_delta、done、error。增强终端原位更新正文卡片；思考增量只作为模型状态处理，JSON/DSML 动作在完成解析前隐藏。中断、错误或缺少终止事件时返回失败并撤回未完成卡片；非 TTY 保留一次完整结果。健康检查和表单模型验证继续使用短非流式请求。

当前按检查点维护增量状态；上下文接近本地预算时，先原子保存完整历史，再保留稳定指令、用户约束、事实、证据索引和最近执行链。`/context` 查看前缀及缓存诊断，`/compact` 在空闲聊天中手动创建继承检查点。供应商缓存参数、连接级能力声明和已知限制见 [缓存感知运行时](CACHE_RUNTIME.md)。测试入口：`python -X utf8 -m unittest tests.test_cache_runtime tests.test_llm_streaming tests.test_llm_openai -v`。
