"""模型服务商（M-01）：解析一句话、动画代理人、galgame 代理人都从这里创建模型。

Gemini 用 Pydantic AI 的 GoogleModel；国内的几家都提供 OpenAI 兼容接口，统一用 OpenAIChatModel + 各自的地址
（Pydantic AI 自带，底层是 OpenAI 官方 SDK，Apache-2.0）。Google 的服务在中国大陆不能直接访问，所以默认推荐智谱。
价格、免费额度会变：写在 PLAN 附录 A（带核查日期），这里只放接入需要的东西。
"""

from dataclasses import dataclass, field

from pydantic_ai.settings import ModelSettings

from acg_agent.config import Settings


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    env_key: str  # .env 里放 Key 的变量名
    base_url: str | None  # OpenAI 兼容接口的地址；Gemini 为 None
    models: tuple[str, ...]  # 设置页里的候选（也可以手填别的型号）
    note: str  # 给用户看的一句话：费用、能不能国内直连
    key_url: str  # 去哪申请 Key
    parse_timeout_s: float = 8.0  # 解析一句话的超时；国内的免费模型一般比 Gemini flash-lite 慢
    extra_body: dict = field(default_factory=dict)  # 各家特有的参数


PROVIDERS: dict[str, ProviderInfo] = {p.id: p for p in [
    ProviderInfo("zhipu", "智谱 GLM", "ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/",
                 ("glm-4.7-flash", "glm-4-flash-250414"), "国内直连；GLM-4.7-Flash 免费（有并发限制）",
                 "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys",
                 extra_body={"thinking": {"type": "disabled"}}),  # 这一类模型默认会先“思考”，很慢；解析一句话不需要
    ProviderInfo("siliconflow", "硅基流动", "SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1",
                 ("Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct"), "国内直连；有免费的小模型",
                 "https://cloud.siliconflow.cn/account/ak", extra_body={"enable_thinking": False}),
    ProviderInfo("dashscope", "通义千问（阿里云百炼）", "DASHSCOPE_API_KEY",
                 "https://dashscope.aliyuncs.com/compatible-mode/v1", ("qwen-plus", "qwen-turbo", "qwen-max"),
                 "国内直连；新用户有免费额度，之后付费", "https://bailian.console.aliyun.com/?apiKey=1",
                 extra_body={"enable_thinking": False}),
    ProviderInfo("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", ("deepseek-chat",),
                 "国内直连；付费，很便宜", "https://platform.deepseek.com/api_keys"),
    ProviderInfo("moonshot", "Kimi（月之暗面）", "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1",
                 ("moonshot-v1-8k", "moonshot-v1-32k"), "国内直连；付费", "https://platform.moonshot.cn/console/api-keys"),
    ProviderInfo("gemini", "Google Gemini", "GEMINI_API_KEY", None, ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
                 "中国大陆不能直接访问；有免费额度", "https://aistudio.google.com/apikey", parse_timeout_s=5.0),
    ProviderInfo("custom", "自定义（OpenAI 兼容接口）", "CUSTOM_API_KEY", None, (),
                 "填任意 OpenAI 兼容接口的地址，例如本机的 Ollama", ""),
]}
DEFAULT_PROVIDER = "zhipu"  # 你的决定：免费、国内直连，新用户最容易上手


def provider_info(provider_id: str) -> ProviderInfo:
    return PROVIDERS.get(provider_id) or PROVIDERS[DEFAULT_PROVIDER]


def make_model(settings: Settings, timeout_s: float | None = None):
    """按设置创建 Pydantic AI 的模型对象。没有 Key 时抛 ValueError（调用方据此退回规则解析）。"""
    if not settings.api_key:
        raise ValueError(f"没有配置 {provider_info(settings.provider).name} 的 API Key")
    if settings.provider == "gemini":
        from pydantic_ai.models.google import GoogleModel  # noqa: PLC0415
        from pydantic_ai.providers.google import GoogleProvider  # noqa: PLC0415

        return GoogleModel(settings.model, provider=GoogleProvider(api_key=settings.api_key))
    from openai import AsyncOpenAI  # noqa: PLC0415
    from pydantic_ai.models.openai import OpenAIChatModel  # noqa: PLC0415
    from pydantic_ai.providers.openai import OpenAIProvider  # noqa: PLC0415

    base_url = settings.base_url if settings.provider == "custom" else provider_info(settings.provider).base_url
    if not base_url:
        raise ValueError("自定义接口需要填写地址")
    # SDK 自己的重试设成 1 次（和 Gemini 那边一致）；超时由调用方的 asyncio.wait_for 兜底
    client = AsyncOpenAI(base_url=base_url, api_key=settings.api_key, max_retries=1,
                         timeout=timeout_s or provider_info(settings.provider).parse_timeout_s * 3)
    return OpenAIChatModel(settings.model, provider=OpenAIProvider(openai_client=client))


def model_settings(settings: Settings, for_parser: bool = False) -> ModelSettings:
    """temperature 0：同一句话尽量得到同样的结果。

    解析器：失败就交给规则后备，不在这里重试（max_retries=0）；Gemini 用最低的思考等级。
    不给 Gemini 设 timeout：它会作为截止时间发给服务器，而 Gemini 要求至少 10 秒（实测 400）；超时由外层 wait_for 控制。
    """
    retries = 0 if for_parser else 1
    if settings.provider == "gemini":
        from google.genai.types import ThinkingLevel  # noqa: PLC0415
        from pydantic_ai.models.google import GoogleModelSettings  # noqa: PLC0415

        extra = {"google_thinking_config": {"thinking_level": ThinkingLevel.MINIMAL}} if for_parser else {}
        return GoogleModelSettings(temperature=0, max_retries=retries, **extra)
    info = provider_info(settings.provider)
    return ModelSettings(temperature=0, max_retries=retries, **({"extra_body": info.extra_body} if info.extra_body else {}))


def parse_timeout(settings: Settings) -> float:
    return provider_info(settings.provider).parse_timeout_s
