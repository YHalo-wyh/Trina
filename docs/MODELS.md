# 模型、供应商与 API Key

## 在控制台使用

输入 `/model` 打开管理页。输入关键词搜索，↑↓ 选择，Enter 确认，Esc 返回。已保存连接显示名称、模型和当前密钥名称；「添加供应商连接」提供官方预设和「自定义供应商 / 兼容网关」。

添加连接依次填写连接名称、Base URL、API Key 和模型。官方供应商预填地址与协议，回车保留；同一供应商可以保存为多个连接。API Key 隐藏输入，留空时使用页面注明的环境变量。模型页可选择示例 ID、从供应商刷新目录，或手动输入完整模型 ID / 部署名。动态目录以供应商当前返回为准，示例模型不代表账户已获授权。

选择「管理 API Key」可添加命名密钥、切换已保存密钥、使用环境变量或删除密钥。密钥内容不出现在列表、成功提示或命令历史中。新增同名 Key 会被拒绝，原有内容不会被覆盖。选择页按 Esc 返回，字段录入按 Ctrl+C 取消；尚未完成的连接不落盘。

| 命令 | 行为 |
|---|---|
| `/model` | 供应商、模型与密钥管理首页 |
| `/model add` | 添加官方或自定义连接 |
| `/model use <连接名称>` | 切换并保存当前连接 |
| `/model keys [连接名称]` | 管理当前或指定连接的多把 Key |
| `/model list` | 查看本机 omp 模型与供应商预设目录 |
| `/model <已保存连接>` | 切换该连接 |
| `/model <模型ID>` | 修改模型；已有连接自动保存，旧会话设置用 `/save` 保存 |
| `/backend omp` | 本会话切换到 omp；管理页内的 omp 选项同时保存选择 |

## 存储和配置顺序

连接与命名密钥保存在用户目录 `~/.trina/providers.json`，文件使用原子替换保存，POSIX 权限为 0600。文件内容是本机 JSON 配置，不作加密存储。默认位置在仓库外；`TRINA_PROVIDERS_FILE` 可以指定独立配置文件。页面不复制或导入其他工具的凭据。

默认启动顺序为项目 YAML → 已选择的本地连接 → `TRINA_LLM_*` 环境变量 → 命令行 `--set`。显式 `--config` 文件仅在指定 `llm.profile` 时读取连接。旧 `~/.trina/env` 中的 `TRINA_API`、`TRINA_MODEL` 在没有已选连接时继续生效；`scripts/dev.sh --api ...` 是显式覆盖。

每个连接选择了命名 Key 时使用该 Key；选择「使用环境变量」后读取该连接的 `api_key_env`。切换 Key 后后端重建，下一次模型请求立即使用新 Key。模型切换保留原对话历史；正在运行的任务不能通过 `/model` 变更配置。

## 协议与模型目录

内置协议为 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。Google Gemini 使用其官方 OpenAI 兼容入口。Claude 使用 Messages 的 system 字段、版本头和鉴权头；响应文本及 Token 用量转换为现有 LLM 后端契约。OpenCode Go 自动带稳定 `x-opencode-session` 和 Trina User-Agent。

「从供应商刷新模型列表」显式请求当前连接的 `/models`，不在启动或键盘补全时联网。OpenCode Go 使用 `/inference/go/v1/models`。不提供模型目录的服务仍可手动输入 ID；带命名空间的 ID（例如 `meta-llama/...`）保留完整内容。Azure 需要填写自己的资源地址和部署名。AWS Bedrock / Google Vertex 的 IAM 签名接入不属于本页的 API Key 协议，可继续使用已有 omp 后端。

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

验证入口为 `tests/test_providers.py`、`tests/test_llm_openai.py` 与 `tests/test_terminal_ui.py`：覆盖重启载入、多把 Key 切换、取消录入、自定义命名空间、错误脱敏、协议请求和 `/h` 实际按键输入。工具授权、审计与目标护栏仍由既有执行链处理。
