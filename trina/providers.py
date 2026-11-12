"""供应商预设与仓库外的本地连接配置；本模块不发起网络请求。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    name: str
    base_url: str
    models: tuple[str, ...]
    api_key_env: str
    docs: str
    api: str = "chat"
    group: str = "官方"
    note: str = ""


PRESETS = (
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", ("gpt-4.1", "gpt-4.1-mini"), "OPENAI_API_KEY", "https://platform.openai.com/docs/api-reference/chat"),
    ProviderPreset("anthropic", "Anthropic / Claude", "https://api.anthropic.com/v1", ("claude-sonnet-5-5", "claude-haiku-5-5"), "ANTHROPIC_API_KEY", "https://platform.claude.com/docs/en/api/overview", api="anthropic"),
    ProviderPreset("google", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", ("gemini-2.5-flash", "gemini-2.5-pro"), "GEMINI_API_KEY", "https://ai.google.dev/gemini-api/docs/openai"),
    ProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", ("deepseek-chat", "deepseek-reasoner"), "DEEPSEEK_API_KEY", "https://api-docs.deepseek.com/"),
    ProviderPreset("xai", "xAI / Grok", "https://api.x.ai/v1", (), "XAI_API_KEY", "https://docs.x.ai/docs/api-reference"),
    ProviderPreset("mistral", "Mistral AI", "https://api.mistral.ai/v1", ("mistral-small-latest", "mistral-large-latest"), "MISTRAL_API_KEY", "https://docs.mistral.ai/api/"),
    ProviderPreset("qwen", "阿里百炼 / 通义千问（北京）", "https://dashscope.aliyuncs.com/compatible-mode/v1", ("qwen-plus", "qwen-turbo", "qwen-max"), "DASHSCOPE_API_KEY", "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope"),
    ProviderPreset("qwen-intl", "阿里百炼（新加坡）", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", ("qwen-plus", "qwen-turbo"), "DASHSCOPE_INTL_API_KEY", "https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope"),
    ProviderPreset("ark", "火山方舟 / 豆包", "https://ark.cn-beijing.volces.com/api/v3", (), "ARK_API_KEY", "https://www.volcengine.com/docs/82379/1330626", note="模型可填写控制台的推理接入点 ID"),
    ProviderPreset("baidu", "百度千帆 / 文心", "https://qianfan.baidubce.com/v2", (), "QIANFAN_API_KEY", "https://cloud.baidu.com/doc/qianfan-api/s/3m7of64lb"),
    ProviderPreset("zhipu", "智谱 BigModel", "https://open.bigmodel.cn/api/paas/v4", ("glm-5", "glm-4-flash"), "ZHIPU_API_KEY", "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction"),
    ProviderPreset("zai", "Z.ai（国际）", "https://api.z.ai/api/paas/v4", (), "ZAI_API_KEY", "https://docs.z.ai/guides/develop/openai/introduction"),
    ProviderPreset("moonshot", "月之暗面 / Kimi", "https://api.moonshot.cn/v1", ("kimi-k2.5", "moonshot-v1-32k"), "MOONSHOT_API_KEY", "https://platform.moonshot.cn/docs/intro"),
    ProviderPreset("minimax", "MiniMax（国内）", "https://api.minimaxi.com/v1", (), "MINIMAX_API_KEY", "https://platform.minimaxi.com/docs/api-reference/text-openai-api"),
    ProviderPreset("minimax-intl", "MiniMax（国际）", "https://api.minimax.io/v1", (), "MINIMAX_INTL_API_KEY", "https://platform.minimax.io/docs/api-reference/text-openai-api"),
    ProviderPreset("stepfun", "阶跃星辰", "https://api.stepfun.com/v1", ("step-3.5-flash",), "STEP_API_KEY", "https://platform.stepfun.com/"),
    ProviderPreset("hunyuan", "腾讯混元（原平台）", "https://api.hunyuan.cloud.tencent.com/v1", ("hunyuan-turbos-latest",), "HUNYUAN_API_KEY", "https://cloud.tencent.com/document/product/1729/111007", note="已有账户；新开通服务使用 TokenHub"),
    ProviderPreset("tokenhub", "腾讯 TokenHub", "https://tokenhub.tencentmaas.com/v1", (), "TOKENHUB_API_KEY", "https://cloud.tencent.com/document/product/1772"),
    ProviderPreset("groq", "Groq", "https://api.groq.com/openai/v1", ("llama-3.3-70b-versatile",), "GROQ_API_KEY", "https://console.groq.com/docs/openai"),
    ProviderPreset("cerebras", "Cerebras", "https://api.cerebras.ai/v1", (), "CEREBRAS_API_KEY", "https://inference-docs.cerebras.ai/api-reference/chat-completions"),
    ProviderPreset("together", "Together AI", "https://api.together.xyz/v1", (), "TOGETHER_API_KEY", "https://docs.together.ai/docs/openai-api-compatibility"),
    ProviderPreset("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1", (), "FIREWORKS_API_KEY", "https://docs.fireworks.ai/tools-sdks/openai-compatibility"),
    ProviderPreset("nvidia", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1", (), "NVIDIA_API_KEY", "https://docs.api.nvidia.com/nim/reference/llm-apis"),
    ProviderPreset("perplexity", "Perplexity", "https://api.perplexity.ai", ("sonar", "sonar-pro"), "PERPLEXITY_API_KEY", "https://docs.perplexity.ai/guides/chat-completions-guide"),
    ProviderPreset("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", (), "SILICONFLOW_API_KEY", "https://docs.siliconflow.cn/cn/api-reference/chat-completions/chat-completions", group="模型平台"),
    ProviderPreset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", (), "OPENROUTER_API_KEY", "https://openrouter.ai/docs/quickstart", group="模型平台"),
    ProviderPreset("opencode-go", "OpenCode Go 订阅", "https://opencode.ai/inference/go/openai/v1", ("glm-5.3-flash", "deepseek-v4.1-flash", "kimi-k3.1-pro"), "OPENCODE_GO_API_KEY", "https://opencode.ai/v2/docs/console/go/", group="模型平台"),
    ProviderPreset("azure", "Azure OpenAI", "", (), "AZURE_OPENAI_API_KEY", "https://learn.microsoft.com/azure/ai-foundry/openai/how-to/switching-endpoints", note="填写资源地址 https://资源名.openai.azure.com/openai/v1 和部署名"),
    ProviderPreset("ollama", "Ollama 本地", "http://127.0.0.1:11434/v1", (), "", "https://docs.ollama.com/api/openai-compatibility", group="本地"),
    ProviderPreset("lmstudio", "LM Studio 本地", "http://127.0.0.1:1234/v1", (), "", "https://lmstudio.ai/docs/developer/openai-compat", group="本地"),
)
PRESET_BY_ID = {preset.id: preset for preset in PRESETS}


@dataclass
class ProviderProfile:
    """供应商是配置和凭据的管理单位；repr 不包含密钥。"""
    id: str
    provider: str
    base_url: str
    model: str
    api_key_env: str = "TRINA_LLM_API_KEY"
    api: str = "chat"
    api_key: str = field(default="", repr=False)
    reasoning_effort: str = "default"
    note: str = ""

    def validate(self) -> None:
        if not self.id.strip() or self.id != self.id.strip() or len(self.id) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in self.id):
            raise ValueError("供应商名称不能为空、包含控制字符或超过 64 字")
        validate_address(self.base_url)
        if not self.model.strip():
            raise ValueError("模型 ID 不能为空")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.model):
            raise ValueError("模型 ID 不能包含控制字符")
        if self.api not in {"chat", "anthropic", "responses"}:
            raise ValueError("接口协议必须为 chat、anthropic 或 responses")
        from .llm.reasoning import LEVEL_LABELS
        if self.reasoning_effort not in LEVEL_LABELS:
            raise ValueError("思考档位无效")
        if not isinstance(self.api_key, str) or any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise ValueError("API Key 不能包含换行或控制字符")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.note):
            raise ValueError("备注不能包含换行或控制字符")


def validate_address(value: str) -> None:
    """请求地址校验独立于模型，允许在填写模型前测试连通性。"""
    parts = urlsplit(value)
    if any(c.isspace() or ord(c) < 32 for c in value) or parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("请求地址必须是无账号、查询串或片段的 HTTP(S) 地址")


def normalize_endpoint(value: str, api: str) -> tuple[str, str]:
    """完整模型接口地址与 API 根地址统一保存，避免重复拼接路径。"""
    value = value.strip().rstrip("/")
    for suffix, protocol in (("/chat/completions", "chat"), ("/responses", "responses"), ("/messages", "anthropic")):
        if value.endswith(suffix):
            return value[:-len(suffix)], protocol
    return value, api


class ProfileStore:
    """原子保存当前供应商格式。"""
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("TRINA_PROVIDERS_FILE") or Path.home() / ".trina" / "providers.json")
        self.profiles: dict[str, ProviderProfile] = {}
        self.active = ""
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != 2:
                raise ValueError("供应商配置版本不受支持")
            rows = raw.get("profiles", [])
            for data in rows:
                data = dict(data)
                item = ProviderProfile(**data)
                item.validate()
                if item.id in self.profiles:
                    raise ValueError("供应商连接名称重复")
                self.profiles[item.id] = item
            self.active = raw.get("active", "")
            if self.active and self.active not in self.profiles:
                raise ValueError("当前供应商连接不存在")

    def save(self) -> None:
        from dataclasses import asdict
        if self.active and self.active not in self.profiles:
            raise ValueError("当前供应商连接不存在")
        for profile in self.profiles.values():
            profile.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".providers-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 2, "active": self.active,
                           "profiles": [asdict(p) for p in self.profiles.values()]}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def apply(self, llm, name: str = "") -> None:
        item = self.profiles[name or self.active]
        llm.backend, llm.profile = "openai", item.id
        llm.base_url, llm.model = item.base_url, item.model
        llm.api_key_env, llm.api = item.api_key_env, item.api
        llm.reasoning_effort = item.reasoning_effort

    def key(self, name: str, env: str) -> str:
        item = self.profiles.get(name)
        if item and item.api_key:
            return item.api_key
        return os.environ.get(env, "") if env else ""
