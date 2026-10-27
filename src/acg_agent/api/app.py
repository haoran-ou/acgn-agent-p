"""HTTP 接口。

运行：uv run acg-serve（默认读 data/acg.db，可用环境变量 ACG_DB 指定；模型服务商和 Key 在网页的设置页里填）
网页：启动后打开 http://127.0.0.1:8000/ ；接口文档：http://127.0.0.1:8000/docs
"""

import base64
import binascii
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from starlette.concurrency import run_in_threadpool

from acg_agent.agent.core import ChatOutcome, build_chat_agent, run_chat
from acg_agent.agent.router import Target, choose_route
from acg_agent.agent.tools import AgentDeps
from acg_agent.config import (
    DEFAULT_USER_SETTINGS,
    Appearance,
    Settings,
    UserSettings,
    effective_settings,
    load_settings,
    load_user_settings,
    save_user_settings,
)
from acg_agent.datasync.update import DataPaths, data_status, update_all
from acg_agent.galgame import search as gal
from acg_agent.galgame.concepts import CONCEPT_ALIASES, CONCEPT_TAGS, TagIndex, UnknownConcept
from acg_agent.llm import PROVIDERS, provider_info
from acg_agent.galgame.agent import GalDeps, build_galgame_agent, galgame_caveat, vn_visible
from acg_agent.galgame.db import DEFAULT_GALGAME_DB
from acg_agent.galgame.links import anime_for_vns, games_for_works
from acg_agent.galgame.verdict import UnsupportedCondition
from acg_agent.nlu.llm import build_agent
from acg_agent.nlu.schema import MAX_TEXT_CHARS, Intent, ParsedCondition
from acg_agent.nlu.service import ParseOutcome, log_call, parse_text
from acg_agent.nlu.vocab import load_vocabulary
from acg_agent.search.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SearchItem,
    SearchRequest,
    SearchResponse,
    items_for_works,
    search,
)
from acg_agent.search.series import build_series_index
from acg_agent.search.titles import DEFAULT_LIMIT, MAX_LIMIT, TitleMatch, lookup
from acg_agent.verdict.models import FormatIn, Mode, TagExcluded, TagRequired

DEFAULT_DB = Path("data/acg.db")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EXAMPLE = "“2010 年以后的 TV 动画，治愈系，不要后宫”"


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    mode: Mode = "strict"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    parser: Literal["auto", "rules"] = "auto"  # rules：强制只用规则解析（演示和排错用）


class ParseInfo(BaseModel):
    parser: Literal["llm", "rules"]
    fallback_reason: str | None
    latency_ms: int
    intent: Intent
    understood: list[ParsedCondition]  # 我理解成了哪些条件；inferred=True 的是模型推断的
    rule_overrides: list[str]  # 以规则为准覆盖了模型结果的条件类型（year / format）
    unmatched: list[str]  # 没能理解的部分


class AskResponse(BaseModel):
    parse: ParseInfo
    clarification: str | None = None  # 没理解时给用户的提示；此时不搜索
    search: SearchResponse | None = None


# 对话记录由网页保存、每次原样发回来（服务器不保存对话）。限制大小，防止被塞进超大的内容
MAX_HISTORY_MESSAGES = 60
MAX_HISTORY_BYTES = 200_000


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    history: list[dict] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    route: Literal["auto", "agent", "workflow"] = "auto"  # 强制走某一边（演示、排错和测试用）


class RouteInfo(BaseModel):
    target: Target  # 这句话交给了谁
    reason: str


class AgentInfo(BaseModel):
    used: bool  # False：代理人没完成，下面的 fallback 是固定流程的结果
    fallback_reason: str | None
    latency_ms: int
    usage: dict
    trace: list[dict]  # 调用了哪些工具、参数、结果概要
    dropped_ids: list[int | str]  # 模型给出、但被丢掉的作品 ID（不存在、已下架、成人内容）；galgame 是 'v17' 这样的字符串


class ChatResponse(BaseModel):
    reply: str  # 已经接上了 caveat
    caveat: str | None = None  # 代码补的说明（条件未知或冲突时），单独给出方便网页另外显示
    question: str | None = None  # 代理人反问用户的问题
    items: list[SearchItem] = Field(default_factory=list)  # 代理人推荐的作品
    route: RouteInfo
    agent: AgentInfo | None = None  # 交给代理人时才有
    history: list[dict] = Field(default_factory=list)  # 下一句话时原样发回来
    workflow: AskResponse | None = None  # 固定流程的结果：分流到固定流程，或代理人没完成时


class ProviderView(BaseModel):
    id: str
    name: str
    models: list[str]  # 候选型号（也可以手填别的）；第一个是默认的
    model: str  # 这一家现在用的型号：设置页选过的，否则默认的（切回这一家时显示它）
    note: str  # 费用、能不能国内直连
    key_url: str  # 去哪申请 Key
    has_key: bool  # 设置页里有没有这一家的 Key（只看设置页，不读环境变量和 .env）
    key_hint: str | None  # 例：“…a1b2”；完整的 Key 永远不离开服务器


class SettingsView(BaseModel):
    """给网页看的设置。"""

    provider: str  # 当前用的服务商
    model: str
    base_url: str | None  # 自定义服务商的地址
    providers: list[ProviderView]
    has_key: bool  # 当前服务商有没有 Key
    key_hint: str | None
    using_ai: bool  # 现在会不会调用模型：选了服务商、有 Key（网页据此显示“正在使用：…”）
    appearance: Appearance
    agent_enabled: bool
    agent_always: bool
    adult_enabled: bool
    show_all_covers: bool


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None  # 换服务商
    api_key: str | None = Field(default=None, max_length=300)  # 这一家（provider，或者当前的）的 Key；不传：不改；空字符串：清除
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:/\-]{1,100}$")  # 也可以手填候选以外的型号
    base_url: str | None = Field(default=None, max_length=300)  # 自定义服务商的地址；空字符串：清除
    appearance: dict | None = None  # 外观（U-01）：只传要改的项，例如 {"accent": "#ff6fa8"}
    agent_enabled: bool | None = None
    agent_always: bool | None = None
    adult_enabled: bool | None = None
    confirm_age: bool = False  # 打开成人内容开关时必须为 true（网页上勾选“我已满 18 岁”）
    show_all_covers: bool | None = None


class BackgroundUpload(BaseModel):
    """自定义背景图（U-01）：网页把选中的图片读成 data URL 发过来，服务器复制到数据文件夹，不上传到任何地方。"""

    model_config = ConfigDict(extra="forbid")

    data_url: str = Field(max_length=15_000_000)  # 约 10 MB 的图片


MAX_BACKGROUND_BYTES = 10 * 1024 * 1024
# 按文件开头的字节判断格式，不信任文件名和网页给的类型
_IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}


def _image_type(data: bytes) -> str | None:
    for magic, ext in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


# 设置会写本机文件，只接受本机页面发来的请求：防止别的网站在浏览器里偷偷改设置（CSRF），
# 也防止 DNS rebinding（用别的域名指向 127.0.0.1）
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


def _host_of(value: str) -> str:
    value = value.split("://", 1)[-1].split("/", 1)[0]
    return value.rsplit(":", 1)[0] if not value.endswith("]") else value


def _local_only(request: Request) -> None:
    host = _host_of(request.headers.get("host", ""))
    origin = request.headers.get("origin")
    if host not in _LOCAL_HOSTS or (origin and _host_of(origin) not in _LOCAL_HOSTS):
        raise HTTPException(status_code=403, detail="设置只能在本机的页面上修改")


GALGAME_VOCABULARY = list(CONCEPT_TAGS) + list(CONCEPT_ALIASES)
# 只是在说“找 galgame”，不是条件；解析器可能把它们列进“没理解的部分”
_MEDIA_WORDS = {"gal", "galgame", "gal游戏", "游戏", "黄油", "视觉小说", "avg", "美少女游戏", "エロゲ", "eroge", "vn"}


class GalgameAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    mode: Mode = "strict"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=gal.DEFAULT_PAGE_SIZE, ge=1, le=gal.MAX_PAGE_SIZE)
    adult_confirmed: bool = False
    show_all_covers: bool = False


class GalgameAskResponse(BaseModel):
    parse: ParseInfo
    clarification: str | None = None
    search: gal.GalgameSearchResponse | None = None


class GalgameChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    history: list[dict] = Field(default_factory=list, max_length=MAX_HISTORY_MESSAGES)
    route: Literal["auto", "agent", "workflow"] = "auto"
    adult_confirmed: bool = False
    show_all_covers: bool = False


class GalgameChatResponse(BaseModel):
    reply: str
    caveat: str | None = None
    question: str | None = None
    items: list[gal.GalgameItem] = Field(default_factory=list)
    route: RouteInfo
    agent: AgentInfo | None = None
    history: list[dict] = Field(default_factory=list)
    workflow: GalgameAskResponse | None = None


class UpdateJob(BaseModel):
    """设置页“立即更新”的后台任务状态。网页每秒问一次，显示进度。"""

    running: bool = False
    stage: str = ""
    done: int = 0
    total: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    results: list[dict] = Field(default_factory=list)  # 每一步：name、status（updated / current / failed）、detail


def create_app(
    db_path: Path | None = None,
    settings: Settings | None = None,
    agent: Agent | None = None,
    chat_model=None,  # 只给测试用：代理人用的假模型（FunctionModel）
    galgame_model=None,  # 只给测试用：galgame 代理人用的假模型
    updater=None,  # 只给测试用：代替 update_all 的函数
    galgame_db_path: Path | None = None,
    user_settings_path: Path | None = None,  # 不传时：没指定 settings 就用 data/settings.json；测试指定了 settings 就只放内存
) -> FastAPI:
    app = FastAPI(title="ACG Agent", version="0.1.1")
    app.state.db_path = db_path or Path(os.environ.get("ACG_DB", DEFAULT_DB))
    app.state.galgame_db_path = galgame_db_path or Path(os.environ.get("ACG_GALGAME_DB", DEFAULT_GALGAME_DB))
    app.state.tag_index = None  # galgame 的标签索引，第一次用到时加载
    app.state.base_settings = settings or load_settings()  # 不带任何 Key：Key 只来自设置页
    # 设置文件：环境变量 ACG_SETTINGS 可以指定（桌面客户端把它放进系统的用户数据文件夹）
    app.state.user_settings_path = user_settings_path or (
        Path(os.environ.get("ACG_SETTINGS", DEFAULT_USER_SETTINGS)) if settings is None else None)
    path = app.state.user_settings_path
    app.state.user = load_user_settings(path) if path else UserSettings()
    app.state.settings = effective_settings(app.state.base_settings, app.state.user)
    app.state.agent = agent if agent is not None else (
        build_agent(app.state.settings) if app.state.settings.has_llm else None
    )
    app.state.vocabulary = None  # 第一次用到时从数据库加载
    app.state.series_index = None  # 同上：系列索引（约 0.4 秒），重新导入数据后要重启服务
    app.state.chat_agent = None  # 代理人：需要词表，第一次 /chat 时创建
    app.state.galgame_agent = None  # galgame 的代理人，第一次 /galgame/chat 时创建
    app.state.update_job = UpdateJob()
    app.state.update_lock = threading.Lock()

    async def vocabulary(conn: sqlite3.Connection) -> list[str]:
        if app.state.vocabulary is None:
            app.state.vocabulary = await run_in_threadpool(load_vocabulary, conn)
        return app.state.vocabulary

    def chat_agent(vocab: list[str]):
        if app.state.chat_agent is None and (chat_model is not None or app.state.settings.has_llm):
            app.state.chat_agent = build_chat_agent(app.state.settings, vocab, model=chat_model)
        return app.state.chat_agent

    def galgame_agent():
        if app.state.galgame_agent is None and (galgame_model is not None or app.state.settings.has_llm):
            app.state.galgame_agent = build_galgame_agent(app.state.settings, model=galgame_model)
        return app.state.galgame_agent

    def preset_route(req_route: str, history: list) -> RouteInfo | None:
        """不用解析就能定下的分流（动画和 galgame 共用）；返回 None 表示要先解析，再按 agent/router.py 的规则分。"""
        if req_route == "agent":
            return RouteInfo(target="agent", reason="指定交给代理人")
        if req_route == "workflow":
            return RouteInfo(target="workflow", reason="指定走固定流程")
        if not app.state.user.agent_enabled:
            return RouteInfo(target="workflow", reason="代理人已在设置里关闭")
        if app.state.user.agent_always:
            return RouteInfo(target="agent", reason="设置为所有请求都交给代理人")
        if history:
            return RouteInfo(**vars(choose_route(None, has_history=True)))
        return None

    def apply_user_settings(user: UserSettings) -> None:
        """保存设置后立刻生效：重新组合配置，按新的 Key 和模型重建解析器和代理人。"""
        if app.state.user_settings_path:
            save_user_settings(user, app.state.user_settings_path)
        app.state.user = user
        app.state.settings = effective_settings(app.state.base_settings, user)
        if agent is None:  # 测试注入的假解析器不替换
            app.state.agent = build_agent(app.state.settings) if app.state.settings.has_llm else None
        app.state.chat_agent = None
        app.state.galgame_agent = None

    def key_of(pid: str) -> str | None:
        if pid == "none":
            return None
        if app.state.user.api_keys.get(pid):
            return app.state.user.api_keys[pid]
        base = app.state.base_settings  # 只有测试、评估在代码里直接传入的 Settings 才会带 Key
        return base.api_key if pid == base.provider else None

    def provider_view(pid: str) -> ProviderView:
        info = PROVIDERS[pid]
        key = key_of(pid)
        saved = app.state.settings.model if pid == app.state.settings.provider else app.state.user.models.get(pid)
        return ProviderView(id=pid, name=info.name, models=list(info.models), model=saved or (info.models or ("",))[0],
                            note=info.note, key_url=info.key_url,
                            has_key=bool(key), key_hint=f"…{key[-4:]}" if key else None)

    def settings_view() -> SettingsView:
        user, s = app.state.user, app.state.settings
        current = provider_view(s.provider)
        return SettingsView(provider=s.provider, model=s.model, base_url=s.base_url,
                            providers=[provider_view(pid) for pid in PROVIDERS],
                            has_key=current.has_key, key_hint=current.key_hint, using_ai=s.has_llm,
                            appearance=user.appearance,
                            agent_enabled=user.agent_enabled, agent_always=user.agent_always,
                            adult_enabled=user.adult_enabled,
                            show_all_covers=user.show_all_covers)

    def attach_games(conn: sqlite3.Connection, items: list[SearchItem]) -> list[SearchItem]:
        """动画卡片加上“原作游戏”：只在成人开关打开时（你的决定）。"""
        if app.state.user.adult_enabled and items:
            found = games_for_works(conn, app.state.galgame_db_path, [i.work_id for i in items])
            for item in items:
                item.original_games = found.get(item.work_id, [])
        return items

    def attach_anime(gconn: sqlite3.Connection, items: list[gal.GalgameItem]) -> list[gal.GalgameItem]:
        """galgame 卡片加上“改编动画”。"""
        if items:
            found = anime_for_vns(gconn, app.state.db_path, [i.vn_id for i in items])
            for item in items:
                item.anime = found.get(item.vn_id, [])
        return items

    def series_index(conn: sqlite3.Connection):
        if app.state.series_index is None:
            app.state.series_index = build_series_index(conn)
        return app.state.series_index
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.middleware("http")
    async def no_stale_pages(request: Request, call_next):
        """页面和脚本每次都先问服务器有没有新版本（no-cache 仍然可以用缓存，只是要先确认）。
        否则更新代码后，浏览器可能拿旧的页面配新的脚本，页面坏掉（TS-047）。"""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    def data_paths() -> DataPaths:
        return DataPaths(data_dir=app.state.db_path.parent, anime_db=app.state.db_path,
                         galgame_db=app.state.galgame_db_path)

    def reset_caches() -> None:
        """数据换成新的之后：词表、系列、标签索引、代理人（提示词里有词表）都重新加载。"""
        app.state.vocabulary = None
        app.state.series_index = None
        app.state.tag_index = None
        app.state.chat_agent = None
        app.state.galgame_agent = None

    def run_update() -> None:
        job = app.state.update_job

        def report(stage: str, done: int, total: int | None) -> None:
            job.stage, job.done, job.total = stage, done, total

        try:
            results = (updater or update_all)(data_paths(), report)
            job.results = [{"name": r.name, "status": r.status, "detail": r.detail} for r in results]
        except Exception as e:  # noqa: BLE001  后台任务出任何错都要报告给页面，不能悄悄挂掉
            job.results = [{"name": "更新", "status": "failed", "detail": str(e)}]
        finally:
            reset_caches()  # 导入是原子切换的：新连接读到的已经是新库
            job.running, job.stage = False, ""
            job.finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    @app.get("/data/status")
    def get_data_status() -> dict:
        return {**data_status(data_paths()), "job": app.state.update_job.model_dump()}

    @app.post("/data/update", dependencies=[Depends(_local_only)])
    def start_update() -> UpdateJob:
        """在后台检查、下载、导入，期间网站照常可用（你的决定）。同一时间只跑一个。"""
        with app.state.update_lock:
            if not app.state.update_job.running:
                app.state.update_job = UpdateJob(running=True, stage="准备中",
                                                 started_at=datetime.now(UTC).isoformat(timespec="seconds"))
                threading.Thread(target=run_update, daemon=True).start()
        return app.state.update_job

    @app.get("/settings")
    def get_settings() -> SettingsView:
        return settings_view()

    @app.post("/settings", dependencies=[Depends(_local_only)])
    def update_settings(req: SettingsUpdate) -> SettingsView:
        user = app.state.user.model_copy(deep=True)
        if req.provider is not None:
            if req.provider not in PROVIDERS:
                raise HTTPException(status_code=422, detail=f"不认识的服务商：{req.provider}")
            user.provider = req.provider
        target = req.provider or app.state.settings.provider
        if req.api_key is not None and req.api_key.strip() and target == "none":
            raise HTTPException(status_code=422, detail="“不用 AI”不需要 Key：先选一家服务商再填")
        if req.api_key is not None:
            key = req.api_key.strip()
            if key and (len(key) < 16 or any(ch.isspace() for ch in key)):
                raise HTTPException(status_code=422, detail="Key 的格式不对（太短或含空格）")
            if key:
                user.api_keys[target] = key
            else:
                user.api_keys.pop(target, None)
        if req.model is not None and target != "none":
            user.models[target] = req.model
        if req.base_url is not None:
            url = req.base_url.strip()
            local = url.startswith(("http://127.0.0.1", "http://localhost"))  # 本机的 Ollama 等可以用 http
            if url and not (url.startswith("https://") or local):
                raise HTTPException(status_code=422, detail="地址要以 https:// 开头（本机地址可以用 http://127.0.0.1）")
            user.base_url = url or None
        if req.appearance is not None:
            merged = user.appearance.model_dump() | req.appearance
            merged["background"] = user.appearance.background  # 背景图只能通过上传接口换，防止指向别的文件
            merged["background_version"] = user.appearance.background_version
            try:
                user.appearance = Appearance.model_validate(merged)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=f"外观设置不对：{e}") from e
        if req.agent_enabled is not None:
            user.agent_enabled = req.agent_enabled
        if req.agent_always is not None:
            user.agent_always = req.agent_always
        if req.adult_enabled is not None:
            if req.adult_enabled and not req.confirm_age:
                raise HTTPException(status_code=422, detail="打开成人内容开关前，需要确认已满 18 岁")
            user.adult_enabled = req.adult_enabled
            if not req.adult_enabled:
                user.show_all_covers = False  # 关掉成人开关时，封面开关一起关
        if req.show_all_covers is not None:
            if req.show_all_covers and not user.adult_enabled:
                raise HTTPException(status_code=422, detail="先打开成人内容开关，才能显示非安全级封面")
            user.show_all_covers = req.show_all_covers
        apply_user_settings(user)
        return settings_view()

    def appearance_dir() -> Path:
        return app.state.db_path.parent / "appearance"  # 和数据库在同一个数据文件夹里（桌面版在系统的用户数据文件夹）

    @app.post("/appearance/background", dependencies=[Depends(_local_only)])
    def upload_background(req: BackgroundUpload) -> SettingsView:
        header, _, payload = req.data_url.partition(",")
        if not header.startswith("data:") or ";base64" not in header:
            raise HTTPException(status_code=422, detail="图片格式不对")
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(status_code=422, detail="图片数据不对") from e
        if len(data) > MAX_BACKGROUND_BYTES:
            raise HTTPException(status_code=413, detail="图片太大：最大 10 MB")
        ext = _image_type(data)
        if ext is None:
            raise HTTPException(status_code=422, detail="只支持 PNG、JPG、WebP 图片")
        folder = appearance_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("background.*"):
            old.unlink()
        (folder / f"background.{ext}").write_bytes(data)
        user = app.state.user.model_copy(deep=True)
        user.appearance.background = f"background.{ext}"
        user.appearance.background_version += 1
        apply_user_settings(user)
        return settings_view()

    @app.delete("/appearance/background", dependencies=[Depends(_local_only)])
    def delete_background() -> SettingsView:
        for old in appearance_dir().glob("background.*"):
            old.unlink()
        user = app.state.user.model_copy(deep=True)
        user.appearance.background = None
        user.appearance.background_version += 1
        apply_user_settings(user)
        return settings_view()

    @app.get("/appearance/background", include_in_schema=False)
    def get_background() -> FileResponse:
        name = app.state.user.appearance.background
        path = appearance_dir() / name if name else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="没有自定义背景")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.post("/settings/test", dependencies=[Depends(_local_only)])
    async def test_connection() -> dict:
        """用一句很短的话试一下当前的服务商、Key、型号能不能用（解析一句话，约 1 次调用）。"""
        s = app.state.settings
        if s.provider == "none":
            return {"ok": False, "detail": "现在选的是“不用 AI（只用规则）”，不需要测试"}
        if app.state.agent is None:
            return {"ok": False, "detail": f"还没有填 {provider_info(s.provider).name} 的 Key"}
        outcome = await parse_text("2010年以后的治愈番", ["治愈", "日常"], s, app.state.agent)
        if outcome.parser == "llm":
            return {"ok": True, "detail": f"可以用：{provider_info(s.provider).name} · {s.model} · {outcome.latency_ms} 毫秒"}
        return {"ok": False, "detail": _connection_error_text(outcome.fallback_reason)}

    @app.get("/health")
    def health(conn: Annotated[sqlite3.Connection, Depends(_connection)]) -> dict:
        # 每个来源各自最新的快照（Bangumi 数据和平台数据是分开导入的）
        rows = conn.execute(
            "SELECT source, file_name, imported_at FROM snapshot WHERE id IN"
            " (SELECT MAX(id) FROM snapshot WHERE status = 'ready' GROUP BY source)"
        ).fetchall()
        return {
            "status": "ok",
            "snapshots": {r["source"]: {"file_name": r["file_name"], "imported_at": r["imported_at"]} for r in rows},
            "llm": f"{app.state.settings.provider}/{app.state.settings.model}" if app.state.agent else None,
        }

    @app.post("/search")
    def search_endpoint(
        req: SearchRequest, conn: Annotated[sqlite3.Connection, Depends(_connection)]
    ) -> SearchResponse:
        resp = search(conn, req, series=series_index(conn))
        attach_games(conn, resp.items)
        return resp

    @app.get("/works")
    def works_by_title(
        conn: Annotated[sqlite3.Connection, Depends(_connection)],
        q: Annotated[str, Query(min_length=1, max_length=100, description="作品名，任意写法，支持繁体")],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> list[TitleMatch]:
        return lookup(conn, q, limit)

    # ---------- galgame（G-02）：单独的库 data/galgame.db ----------

    def tag_index(conn: sqlite3.Connection) -> TagIndex:
        if app.state.tag_index is None:
            app.state.tag_index = TagIndex(conn)
        return app.state.tag_index

    def galgame_errors(fn):
        try:
            return fn()
        except gal.AdultNotConfirmed as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except (UnknownConcept, UnsupportedCondition) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/galgame/search")
    def galgame_search(
        req: gal.GalgameSearchRequest, conn: Annotated[sqlite3.Connection, Depends(_galgame_connection)]
    ) -> gal.GalgameSearchResponse:
        resp = galgame_errors(lambda: gal.search(conn, req, tag_index(conn)))
        attach_anime(conn, resp.items)
        return resp

    async def galgame_parse(text: str):
        """galgame 的解析：动画的解析器 + galgame 词表，再按 galgame_query 清理。"""
        outcome = await parse_text(text, GALGAME_VOCABULARY, app.state.settings, app.state.agent)
        await run_in_threadpool(log_call, app.state.settings.log_dir, text, outcome)
        return outcome, galgame_query(outcome.query)

    @app.post("/galgame/ask")
    async def galgame_ask(
        req: GalgameAskRequest, conn: Annotated[sqlite3.Connection, Depends(_galgame_connection)]
    ) -> GalgameAskResponse:
        """一句话搜索 galgame：复用动画的解析器，词表换成 galgame 的中文概念。"""
        galgame_errors(lambda: gal.require_adult(req.adult_confirmed))  # 先检查开关，没打开就不调用模型
        outcome, q = await galgame_parse(req.text)
        return await galgame_answer(outcome, q, req, conn)

    async def galgame_answer(outcome, q, req: GalgameAskRequest, conn: sqlite3.Connection) -> GalgameAskResponse:
        info = ParseInfo(parser=outcome.parser, fallback_reason=outcome.fallback_reason,
                         latency_ms=outcome.latency_ms, intent=q.intent, understood=q.conditions,
                         rule_overrides=outcome.rule_overrides, unmatched=q.unmatched)
        if q.intent != "search":
            return GalgameAskResponse(parse=info, clarification=_galgame_clarification(q.intent, q.unmatched))
        search_req = gal.GalgameSearchRequest(conditions=[pc.condition for pc in q.conditions], mode=req.mode,
                                              page=req.page, page_size=req.page_size,
                                              adult_confirmed=req.adult_confirmed,
                                              show_all_covers=req.show_all_covers)
        result = await run_in_threadpool(lambda: galgame_errors(lambda: gal.search(conn, search_req, tag_index(conn))))
        await run_in_threadpool(attach_anime, conn, result.items)
        return GalgameAskResponse(parse=info, search=result)

    @app.post("/galgame/chat")
    async def galgame_chat(
        req: GalgameChatRequest, conn: Annotated[sqlite3.Connection, Depends(_galgame_connection)]
    ) -> GalgameChatResponse:
        """galgame 的代理人：和动画的 /chat 一样先分流；失败时退回固定流程，复用已经做好的解析。"""
        galgame_errors(lambda: gal.require_adult(req.adult_confirmed))  # 没打开成人开关：不解析、不启动代理人
        history = _validated_history(req.history)
        outcome = q = None
        route = preset_route(req.route, history)
        if route is None:
            outcome, q = await galgame_parse(req.text)
            route = RouteInfo(**vars(choose_route(q)))
        ask_req = GalgameAskRequest(text=req.text, adult_confirmed=True, show_all_covers=req.show_all_covers)
        agent = galgame_agent() if route.target == "agent" else None
        if route.target == "agent" and agent is None:
            route = RouteInfo(target="workflow", reason="没有配置模型，代理人不可用")
        if route.target == "workflow":
            if q is None:
                outcome, q = await galgame_parse(req.text)
            resp = await galgame_answer(outcome, q, ask_req, conn)
            return GalgameChatResponse(reply=_galgame_workflow_reply(resp), route=route, workflow=resp,
                                       history=req.history)

        deps = GalDeps(conn=conn, index=tag_index(conn), adult_confirmed=True, anime_db=app.state.db_path)
        result = await run_chat(agent, deps, req.text, history, visible=vn_visible, caveat=galgame_caveat)
        await run_in_threadpool(_log_chat, app.state.settings.log_dir, req.text, result, "galgame")
        info = AgentInfo(used=result.answer is not None, fallback_reason=result.fallback_reason,
                         latency_ms=result.latency_ms, usage=result.usage, trace=deps.trace,
                         dropped_ids=result.dropped_ids)
        if result.answer is None:
            if q is None:
                outcome, q = await galgame_parse(req.text)
            resp = await galgame_answer(outcome, q, ask_req, conn)
            return GalgameChatResponse(reply="代理人这次没能完成，下面是按条件直接搜索的结果。", route=route, agent=info,
                                       workflow=resp, history=req.history)
        items = await run_in_threadpool(gal.items_for_vns, conn, result.work_ids, req.show_all_covers,
                                        deps.last_conditions, deps.index)
        await run_in_threadpool(attach_anime, conn, items)
        return GalgameChatResponse(
            reply=result.answer.reply + (f"\n{result.caveat}" if result.caveat else ""),
            caveat=result.caveat, question=result.answer.question, items=items, route=route, agent=info,
            history=ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
        )

    @app.get("/galgame/works")
    def galgame_works(
        conn: Annotated[sqlite3.Connection, Depends(_galgame_connection)],
        q: Annotated[str, Query(min_length=1, max_length=100, description="作品名：中文、日文、罗马字、别名")],
        adult: Annotated[bool, Query(description="已确认满 18 岁并打开成人内容开关")] = False,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> list[gal.GalgameTitleMatch]:
        return galgame_errors(lambda: gal.lookup(conn, q, adult, limit))

    @app.post("/ask")
    async def ask(req: AskRequest, conn: Annotated[sqlite3.Connection, Depends(_connection)]) -> AskResponse:
        return await run_ask(req, conn)

    @app.post("/chat")
    async def chat(req: ChatRequest, conn: Annotated[sqlite3.Connection, Depends(_connection)]) -> ChatResponse:
        """先分流：条件都理解了走固定流程（更准、更快、更省）；有没理解的部分才交给代理人。

        代理人失败时退回固定流程，并复用已经做好的解析，不再调用第二次模型。
        """
        history = _validated_history(req.history)

        parsed: ParseOutcome | None = None
        route = preset_route(req.route, history)
        if route is None:
            parsed = await parse_ask(req.text, conn)
            route = RouteInfo(**vars(choose_route(parsed.query)))

        vocab = await vocabulary(conn)
        agent = chat_agent(vocab) if route.target == "agent" else None
        if route.target == "agent" and agent is None:
            route = RouteInfo(target="workflow", reason="没有配置模型，代理人不可用")
        if route.target == "workflow":
            parsed = parsed or await parse_ask(req.text, conn)
            ask_resp = await answer_parsed(parsed, AskRequest(text=req.text), conn)
            return ChatResponse(reply=_workflow_reply(ask_resp), route=route, workflow=ask_resp, history=req.history)

        index = await run_in_threadpool(series_index, conn)
        deps = AgentDeps(conn=conn, series=index, vocabulary=vocab,
                         galgame_db=app.state.galgame_db_path if app.state.user.adult_enabled else None)
        outcome = await run_chat(agent, deps, req.text, history)
        await run_in_threadpool(_log_chat, app.state.settings.log_dir, req.text, outcome, "anime")
        info = AgentInfo(used=outcome.answer is not None, fallback_reason=outcome.fallback_reason,
                         latency_ms=outcome.latency_ms, usage=outcome.usage, trace=deps.trace,
                         dropped_ids=outcome.dropped_ids)
        if outcome.answer is None:
            parsed = parsed or await parse_ask(req.text, conn)
            ask_resp = await answer_parsed(parsed, AskRequest(text=req.text), conn)
            return ChatResponse(reply="代理人这次没能完成，下面是按条件直接搜索的结果。", route=route, agent=info,
                                workflow=ask_resp, history=req.history)
        items = await run_in_threadpool(
            items_for_works, conn, outcome.work_ids, deps.last_conditions, index)
        await run_in_threadpool(attach_games, conn, items)
        return ChatResponse(
            reply=outcome.answer.reply + (f"\n{outcome.caveat}" if outcome.caveat else ""),
            caveat=outcome.caveat, question=outcome.answer.question, items=items, route=route, agent=info,
            history=ModelMessagesTypeAdapter.dump_python(outcome.messages, mode="json"),
        )

    async def run_ask(req: AskRequest, conn: sqlite3.Connection) -> AskResponse:
        outcome = await parse_ask(req.text, conn, force_rules=req.parser == "rules")
        return await answer_parsed(outcome, req, conn)

    async def parse_ask(text: str, conn: sqlite3.Connection, force_rules: bool = False) -> ParseOutcome:
        vocab = await vocabulary(conn)
        outcome = await parse_text(text, vocab, app.state.settings, app.state.agent, force_rules=force_rules)
        await run_in_threadpool(log_call, app.state.settings.log_dir, text, outcome)
        return outcome

    async def answer_parsed(outcome: ParseOutcome, req: AskRequest, conn: sqlite3.Connection) -> AskResponse:
        q = outcome.query
        info = ParseInfo(parser=outcome.parser, fallback_reason=outcome.fallback_reason,
                         latency_ms=outcome.latency_ms, intent=q.intent,
                         understood=q.conditions, rule_overrides=outcome.rule_overrides,
                         unmatched=q.unmatched)
        if q.intent != "search":
            return AskResponse(parse=info, clarification=_clarification(q.intent, q.unmatched))
        search_req = SearchRequest(conditions=[pc.condition for pc in q.conditions], mode=req.mode,
                                   page=req.page, page_size=req.page_size)
        index = await run_in_threadpool(series_index, conn)
        result = await run_in_threadpool(lambda: search(conn, search_req, series=index))
        await run_in_threadpool(attach_games, conn, result.items)
        return AskResponse(parse=info, search=result)

    return app


def _validated_history(raw: list[dict]):
    """对话记录由网页保存、原样发回来：先限制大小，再按 Pydantic AI 的消息格式校验。"""
    if len(json.dumps(raw, ensure_ascii=False)) > MAX_HISTORY_BYTES:
        raise HTTPException(status_code=422, detail="对话记录太长，请开始新的对话")
    try:
        return ModelMessagesTypeAdapter.validate_python(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="对话记录格式不对，请开始新的对话") from e


def _galgame_workflow_reply(resp: "GalgameAskResponse") -> str:
    if resp.clarification:
        return resp.clarification
    return f"按条件直接搜索，找到 {resp.search.total} 部。" if resp.search else ""


def _log_chat(log_dir: Path, text: str, outcome: ChatOutcome, domain: str = "anime") -> None:
    """代理人的本地日志：只在 data/logs/ 下，不提交。记工具调用过程，不记 Key，也不记完整对话。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "domain": domain,  # anime 或 galgame
        "text": text,
        "used": outcome.answer is not None,
        "fallback_reason": outcome.fallback_reason,
        "latency_ms": outcome.latency_ms,
        "usage": outcome.usage,
        "trace": outcome.deps.trace,
        "work_ids": outcome.work_ids,
        "dropped_ids": outcome.dropped_ids,
        "question": outcome.answer.question if outcome.answer else None,
    }
    with (log_dir / "agent.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def galgame_query(q):
    """解析结果改成 galgame 能用的：别名换成标准概念；“类型”条件对 galgame 没意义，移进没理解的部分。"""
    conditions, unmatched = [], [u for u in q.unmatched if u.strip().casefold() not in _MEDIA_WORDS]
    for pc in q.conditions:
        c = pc.condition
        if isinstance(c, FormatIn):
            unmatched.append(pc.source_text or "类型")
            continue
        if isinstance(c, (TagRequired, TagExcluded)) and c.concept in CONCEPT_ALIASES:
            pc = pc.model_copy(update={"condition": c.model_copy(update={"concept": CONCEPT_ALIASES[c.concept]})})
        conditions.append(pc)
    intent = q.intent
    if intent == "search" and not conditions:
        intent = "unclear"
    return q.model_copy(update={"conditions": conditions, "unmatched": list(dict.fromkeys(unmatched))[:5],
                                "intent": intent})


GALGAME_EXAMPLE = "“2010 年以后的催泪 galgame，不要 NTR”"


def _galgame_clarification(intent: Intent, unmatched: list[str]) -> str:
    if intent == "off_topic":
        return f"我只能帮你找 galgame。可以这样说：{GALGAME_EXAMPLE}。"
    detail = f"（没理解的部分：{'、'.join(unmatched)}）" if unmatched else ""
    return f"没能理解你的要求{detail}。可以这样说：{GALGAME_EXAMPLE}。可用的标签：{'、'.join(CONCEPT_TAGS)}。"


def _connection_error_text(reason: str | None) -> str:
    reason = reason or ""
    if reason in ("http_401", "http_403"):
        return "Key 不对或没有权限（服务商返回 401/403）"
    if reason == "http_404":
        return "找不到这个型号或地址（404）：检查型号名称"
    if reason == "rate_limited":
        return "达到了这家的调用限制（429）：稍等再试，或者换一个型号"
    if reason == "timeout":
        return "连接超时：网络不通或服务商太慢（在中国大陆用不了 Gemini）"
    if reason == "invalid_output":
        return "能连上，但这个型号不能按要求的格式输出：换一个型号试试"
    return f"连接失败（{reason}）"


def _workflow_reply(resp: AskResponse) -> str:
    if resp.clarification:
        return resp.clarification
    return f"按条件直接搜索，找到 {resp.search.total} 个系列。" if resp.search else ""


def _clarification(intent: Intent, unmatched: list[str]) -> str:
    if intent == "off_topic":
        return f"我只能帮你找动画。可以这样说：{EXAMPLE}。"
    detail = f"（没理解的部分：{'、'.join(unmatched)}）" if unmatched else ""
    return f"没能理解你的要求{detail}。可以这样说：{EXAMPLE}。"


def _connection(request: Request) -> Iterator[sqlite3.Connection]:
    """每个请求一个只读连接。正式库只由导入程序的原子切换改变，接口绝不写库。"""
    path: Path = request.app.state.db_path
    if not path.exists():
        raise HTTPException(status_code=503, detail=f"数据库不存在：{path}，请先运行 acg-import")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _galgame_connection(request: Request) -> Iterator[sqlite3.Connection]:
    path: Path = request.app.state.galgame_db_path
    if not path.exists():
        raise HTTPException(status_code=503, detail=f"galgame 数据库不存在：{path}，请先运行 acg-import-vndb")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    """uv run acg-serve：启动服务，并自动打开浏览器（--no-browser 关掉）。"""
    import argparse
    import threading
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(description="启动如月寮（只在本机 127.0.0.1 上开放）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}/"
    if not Path(os.environ.get("ACG_DB", DEFAULT_DB)).exists():
        print("还没有数据：请先运行 uv run acg-setup")
    print(f"如月寮：{url}（按 Ctrl + C 停止）")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()  # 等服务起来再打开
    uvicorn.run("acg_agent.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)
