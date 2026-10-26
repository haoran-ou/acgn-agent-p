"""领域模型（domain models）。

每个模型对应 schema.sql 里的一张表，规则与数据库的 CHECK 约束一致。
两层都检查是有意为之（纵深防御，defense in depth）：
  - Pydantic 在导入时拦住坏数据，报错信息清楚，能指出是哪条记录、哪个字段
  - 数据库约束是最后一道防线，防止绕过 Python 代码直接写库
"""

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MediaType = Literal["anime", "manga", "novel", "book_other", "game", "music", "live_action"]
Format = Literal["tv", "web", "ova", "movie", "motion_comic", "other"]
SnapshotStatus = Literal["importing", "ready"]
AliasKind = Literal["original", "zh", "alias"]
TagKind = Literal["user", "meta"]
PersonKind = Literal["individual", "company", "group", "unknown"]


class _Model(BaseModel):
    # extra="forbid"：多出未知字段就报错，防止拼错字段名被悄悄忽略
    # frozen=True：创建后不可修改
    model_config = ConfigDict(extra="forbid", frozen=True)


class Snapshot(_Model):
    source: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    tag_cap: int | None = Field(default=None, gt=0)  # 只有 Bangumi Archive 有
    status: SnapshotStatus
    imported_at: str


class Work(_Model):
    media_type: MediaType
    format: Format | None = None
    title_original: str = Field(min_length=1)
    title_zh: str | None = Field(default=None, min_length=1)
    release_date: date | None = None
    nsfw: bool
    score: float | None = Field(default=None, ge=0, le=10)
    rating_count: int = Field(ge=0)
    summary: str | None = None

    @model_validator(mode="after")
    def _no_score_without_ratings(self) -> Self:
        if self.rating_count == 0 and self.score is not None:
            raise ValueError("rating_count 为 0 时 score 必须为 None（0 分不等于未评分）")
        return self


class ExternalId(_Model):
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)


class Alias(_Model):
    text: str = Field(min_length=1)
    normalized: str = Field(min_length=1)
    search_key: str = Field(min_length=1)
    kind: AliasKind


class WorkTag(_Model):
    kind: TagKind
    name: str = Field(min_length=1)
    count: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        has_stats = self.count is not None and self.position is not None
        no_stats = self.count is None and self.position is None
        if self.kind == "user" and not has_stats:
            raise ValueError("用户标签必须同时有 count 和 position")
        if self.kind == "meta" and not no_stats:
            raise ValueError("公共标签（meta）不能有 count 或 position")
        return self


class WorkRelation(_Model):
    related_source: str = Field(min_length=1)
    related_source_id: str = Field(min_length=1)
    related_source_type: int
    relation_code: int


class Person(_Model):
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: PersonKind
