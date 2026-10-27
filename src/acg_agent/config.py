"""配置。

Key 只来自设置页（保存在本机的设置文件里）。程序**不读**系统环境变量和任何文件夹里的 `.env` 来找 Key：
用户不知道时悄悄用上别处的 Key（可能是付费的）是越权行为（你的决定，2026-10-26）。
只有开发用的评估工具会明确传入开发者自己的配置文件（`load_settings(DEV_ENV_FILE)`），网页和桌面版从不这样做。

密钥规则：repr 里不显示 Key，任何地方都不打印、不写日志。
"""

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

DEV_ENV_FILE = Path(".env")  # 开发者自己的配置文件：只有评估工具会读，不进仓库、不打进安装包
DEFAULT_USER_SETTINGS = Path("data/settings.json")  # 网页设置页保存的内容（G-03），不进仓库

# 关掉 Pydantic AI 启动时打印的宣传横幅（所有入口都会先导入本模块）
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@dataclass(frozen=True)
class Settings:
    """当前用哪家模型服务商（M-01）。各家的地址、候选型号在 acg_agent/llm.py 的 PROVIDERS 里。"""

    provider: str = "none"  # 默认“不用 AI（只用规则）”，用户在设置页选了服务商、填了 Key 才调用模型（你的决定）
    api_key: str | None = field(default=None, repr=False)  # repr=False：打印 Settings 时不显示 Key
    model: str = ""
    base_url: str | None = None  # 只有“自定义”服务商用
    llm_timeout_s: float | None = None  # 解析一句话的超时；None 用服务商的默认值
    log_dir: Path = Path("data/logs")
    env_keys: dict[str, str | None] = field(default_factory=dict, repr=False, compare=False)  # 开发配置文件里各家的 Key（只有评估工具用）

    @property
    def has_llm(self) -> bool:
        return bool(self.api_key)

    @property
    def parse_timeout_s(self) -> float:
        from acg_agent.llm import provider_info  # noqa: PLC0415  llm.py 依赖本模块，放在函数里避免循环导入

        return self.llm_timeout_s or provider_info(self.provider).parse_timeout_s


def _read_env_file(path: Path) -> dict[str, str]:
    """只支持最简单的 KEY=VALUE 格式；# 开头的行是注释。"""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env_file: Path | None = None) -> Settings:
    """不传文件（网页、桌面版）：没有任何 Key，只读程序自己设置的日志目录（ACG_LOG_DIR，桌面版启动时设的）。
    传了文件（只有开发用的评估工具这样做）：从这个文件里读各家的 Key 和 LLM_PROVIDER / LLM_MODEL 等。
    两种情况都不读系统环境变量里的 Key。"""
    log_dir = Path(os.environ.get("ACG_LOG_DIR") or Settings().log_dir)
    if env_file is None:
        return Settings(log_dir=log_dir)
    values = _read_env_file(env_file)

    def get(name: str) -> str | None:
        return values.get(name) or None

    from acg_agent.llm import PROVIDERS, provider_info  # noqa: PLC0415

    env_keys = {pid: get(info.env_key) for pid, info in PROVIDERS.items() if info.env_key}
    # LLM_PROVIDER 指定了就用它；否则用第一个填了 Key 的（按 PROVIDERS 的顺序，国内的在前）
    provider = get("LLM_PROVIDER") or next((pid for pid, key in env_keys.items() if key), "none")
    provider = provider if provider in PROVIDERS else "none"
    legacy_model = get("GEMINI_MODEL") if provider == "gemini" else None  # 旧版 .env 的写法
    model = get("LLM_MODEL") or legacy_model or (provider_info(provider).models or ("",))[0]
    return Settings(
        provider=provider,
        api_key=env_keys.get(provider),
        model=model,
        base_url=get("LLM_BASE_URL"),
        llm_timeout_s=float(get("LLM_TIMEOUT_S")) if get("LLM_TIMEOUT_S") else None,
        log_dir=log_dir,
        env_keys=env_keys,
    )


def with_provider(base: Settings, provider: str, model: str | None = None) -> Settings:
    """换一家服务商（对比评估用）：Key 取开发配置文件里那一家的。"""
    from acg_agent.llm import provider_info  # noqa: PLC0415

    return replace(base, provider=provider, api_key=base.env_keys.get(provider),
                   model=model or (provider_info(provider).models or ("",))[0])


# ---------- 网页设置页（G-03）----------
# 只存在本机的 data/settings.json，文件权限 600（只有自己能读写）。
# 没有做“加密”：本机加密的钥匙也只能放在同一台电脑上，起不到真正的保护作用（PLAN 已如实写明）。
# Key 只来自设置页。

class Appearance(BaseModel):
    """外观（U-01）：配色和背景。风格参考 galgame《BALDR SKY》的电子空间 / HUD 感（你的决定），全部用 CSS 画，不用原作的素材。"""

    theme: Literal["dark", "light", "auto"] = "dark"  # auto：跟随系统
    accent: str = Field(default="#3fd0ff", pattern=r"^#[0-9a-fA-F]{6}$")  # 主题色（默认天青色）
    background: str | None = None  # 自定义背景图在数据文件夹 appearance/ 里的文件名；None 用默认的网格背景
    background_version: int = 0  # 换图后加一，让浏览器重新加载
    bg_blur: int = Field(default=4, ge=0, le=24)  # 背景模糊（像素）
    bg_dim: int = Field(default=55, ge=0, le=90)  # 背景压暗（%），保证文字看得清
    panel_opacity: int = Field(default=82, ge=40, le=100)  # 面板不透明度（%）
    font_scale: int = Field(default=100, ge=85, le=125)  # 字号（%）


class UserSettings(BaseModel):
    provider: str | None = None  # None：用 .env 推断的服务商
    api_keys: dict[str, str] = Field(default_factory=dict, repr=False)  # 每家的 Key 分开存：切换服务商不会丢
    models: dict[str, str] = Field(default_factory=dict)  # 每家选的型号
    base_url: str | None = None  # 自定义服务商的地址
    agent_enabled: bool = True  # 关掉后全部走固定流程
    agent_always: bool = False  # 所有请求都交给代理人（不分流）；更慢、更费额度
    adult_enabled: bool = False  # 已确认满 18 岁并打开成人内容开关：才能检索 galgame
    show_all_covers: bool = False  # 显示非安全级封面（受底线限制）
    appearance: Appearance = Field(default_factory=Appearance)

    @model_validator(mode="before")
    @classmethod
    def _migrate_g03(cls, data):
        """G-03 的设置文件只有 Gemini：gemini_api_key / gemini_model 搬进新的字段。"""
        if isinstance(data, dict) and ("gemini_api_key" in data or "gemini_model" in data):
            data = dict(data)
            key, model = data.pop("gemini_api_key", None), data.pop("gemini_model", None)
            if key:
                data.setdefault("api_keys", {})["gemini"] = key
                data.setdefault("provider", "gemini")
            if model:
                data.setdefault("models", {})["gemini"] = model
        return data


def load_user_settings(path: Path = DEFAULT_USER_SETTINGS) -> UserSettings:
    if not path.exists():
        return UserSettings()
    return UserSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_user_settings(user: UserSettings, path: Path = DEFAULT_USER_SETTINGS) -> None:
    """先写临时文件再改名（写到一半断电也不会留下坏文件）；权限 600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(user.model_dump_json(indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def effective_settings(base: Settings, user: UserSettings) -> Settings:
    """把设置页的内容叠加到 .env 的配置上：设置页有值就用设置页的（服务商、这一家的 Key、型号）。"""
    from acg_agent.llm import provider_info  # noqa: PLC0415

    provider = user.provider or base.provider
    if provider == "none":  # 不用 AI：不管哪里有 Key 都不调用模型
        return replace(base, provider="none", api_key=None, model="")
    same = provider == base.provider
    model = user.models.get(provider) or (base.model if same else (provider_info(provider).models or ("",))[0])
    # base.api_key 只在代码里直接传入 Settings 时才有（测试、评估）；网页和桌面版的 base 没有 Key
    return replace(base, provider=provider, api_key=user.api_keys.get(provider) or (base.api_key if same else None),
                   model=model, base_url=user.base_url or base.base_url)
