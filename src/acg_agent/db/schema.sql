-- ACG Agent 数据库结构（V0-01）
-- 规则：
--   * 所有表用 STRICT：列类型写错直接报错，不做隐式转换
--   * 缺失就存 NULL，不用 0 或空字符串冒充（例如未评分的作品 score 为 NULL，而不是 0）
--   * 外键需要每个连接执行 PRAGMA foreign_keys = ON，见 connection.py

-- 每次导入的数据包。其他表通过 snapshot_id 追溯数据来自哪一期。
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,                      -- 'bangumi_archive' 或 'bangumi_data'
    file_name   TEXT    NOT NULL,                      -- 例：'dump-2026-10-20.210332Z.zip'
    sha256      TEXT    NOT NULL CHECK (length(sha256) = 64),
    size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
    tag_cap     INTEGER CHECK (tag_cap IS NULL OR tag_cap > 0),  -- Bangumi Archive 每部作品最多导出几个用户标签（实测 11）；其他来源为 NULL
    status      TEXT    NOT NULL CHECK (status IN ('importing', 'ready')),  -- 失败的导入连同临时库一起删除，不会留下记录
    imported_at TEXT    NOT NULL,                      -- ISO 8601 UTC
    UNIQUE (source, sha256)
) STRICT;

-- 作品。id 是我们自己的统一作品 ID（canonical work ID），与任何外部网站的 ID 无关。
CREATE TABLE IF NOT EXISTS work (
    id             INTEGER PRIMARY KEY,
    media_type     TEXT    NOT NULL CHECK (media_type IN
                       ('anime', 'manga', 'novel', 'book_other', 'game', 'music', 'live_action')),
    format         TEXT    CHECK (format IN ('tv', 'web', 'ova', 'movie', 'motion_comic', 'other')),
    title_original TEXT    NOT NULL CHECK (length(title_original) > 0),
    title_zh       TEXT    CHECK (title_zh IS NULL OR length(title_zh) > 0),
    release_date   TEXT    CHECK (release_date IS NULL OR
                       release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    nsfw           INTEGER NOT NULL CHECK (nsfw IN (0, 1)),
    score          REAL    CHECK (score IS NULL OR score BETWEEN 0 AND 10),
    rating_count   INTEGER NOT NULL CHECK (rating_count >= 0),
    summary        TEXT,
    snapshot_id    INTEGER NOT NULL REFERENCES snapshot (id) ON DELETE RESTRICT,
    removed_at     TEXT,                               -- 墓碑记录：新一期里消失的作品只打标记，不物理删除
    CHECK (rating_count > 0 OR score IS NULL)          -- 没人评分就不能有分数
) STRICT;

-- 外部 ID → 统一作品 ID。(source, source_id) 是主键，重复导入只能更新，不能插出第二条。
CREATE TABLE IF NOT EXISTS external_id (
    source    TEXT    NOT NULL,                        -- 例：'bangumi'
    source_id TEXT    NOT NULL,                        -- 用 TEXT：VNDB 等来源的 ID 不是纯数字（如 'v17'）
    work_id   INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    PRIMARY KEY (source, source_id)
) STRICT;
CREATE INDEX IF NOT EXISTS external_id_work ON external_id (work_id);

-- 作品的所有名字。同一个名字可以属于多部作品（同名不合并），但同一部作品内不重复。
CREATE TABLE IF NOT EXISTS alias (
    id         INTEGER PRIMARY KEY,
    work_id    INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    text       TEXT    NOT NULL CHECK (length(text) > 0),
    normalized TEXT    NOT NULL CHECK (length(normalized) > 0),
    search_key TEXT    NOT NULL CHECK (length(search_key) > 0),  -- 转成简体后再规范化，用于检索（“物語”→“物语”）
    kind       TEXT    NOT NULL CHECK (kind IN ('original', 'zh', 'alias')),
    UNIQUE (work_id, normalized)
) STRICT;
CREATE INDEX IF NOT EXISTS alias_normalized ON alias (normalized);

-- 别名的全文索引（FTS5，trigram 分词器）：按 3 个字符一组切分，支持任意位置的子串匹配，中文也适用。
-- 外部内容表（external content）：索引只存检索结构，文字本身仍在 alias 表里。
-- alias 变化后不会自动同步，导入完成时执行一次 'rebuild'（见 importer/repository.py）。
-- trigram 要求查询至少 3 个字符，更短的查询用 LIKE 扫描 alias.search_key。
CREATE VIRTUAL TABLE IF NOT EXISTS alias_fts USING fts5(
    search_key,
    content = 'alias',
    content_rowid = 'id',
    tokenize = 'trigram'
);

-- 标签。用户标签带打标人数和排名；维基人管理的公共标签（meta）两者都没有。
CREATE TABLE IF NOT EXISTS work_tag (
    work_id  INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    kind     TEXT    NOT NULL CHECK (kind IN ('user', 'meta')),
    name     TEXT    NOT NULL CHECK (length(name) > 0),
    count    INTEGER CHECK (count IS NULL OR count >= 1),
    position INTEGER CHECK (position IS NULL OR position >= 1),  -- 1 = 打标人数最多
    PRIMARY KEY (work_id, kind, name),
    CHECK ((kind = 'user' AND count IS NOT NULL AND position IS NOT NULL)
        OR (kind = 'meta' AND count IS NULL AND position IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS work_tag_name ON work_tag (name, kind);

-- 三态判断（three-state verdict）要用的标签证据。由 work_tag 现算，不单独存一份，避免两处数据不一致。
--   list_is_full = 1：用户标签达到了这一期的上限，没列出的标签打标人数 ≤ lowest_listed_count（上界证据）
--   list_is_full = 0：这部作品的用户标签已经全部导出，没列出的标签打标人数为 0，但标签本身少，证据弱
CREATE VIEW IF NOT EXISTS work_tag_evidence AS
SELECT
    w.id                                AS work_id,
    COUNT(t.name)                       AS user_tag_count,
    s.tag_cap                           AS tag_cap,
    COUNT(t.name) >= s.tag_cap          AS list_is_full,
    CASE WHEN COUNT(t.name) >= s.tag_cap THEN MIN(t.count) ELSE 0 END
                                        AS unlisted_tag_max_count
FROM work AS w
JOIN snapshot AS s ON s.id = w.snapshot_id
LEFT JOIN work_tag AS t ON t.work_id = w.id AND t.kind = 'user'
GROUP BY w.id;

-- 作品关系。目标作品可能还没导入（V0 只导入动画，而 65% 的关系指向漫画、小说、游戏、音乐），
-- 所以目标存来源 ID，不做外键。relation_code 的含义取决于目标作品的类型，见 source_code 表。
CREATE TABLE IF NOT EXISTS work_relation (
    work_id             INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    related_source      TEXT    NOT NULL,
    related_source_id   TEXT    NOT NULL,
    related_source_type INTEGER NOT NULL,              -- Bangumi 条目类型：1 书籍 2 动画 3 音乐 4 游戏 6 三次元
    relation_code       INTEGER NOT NULL,
    PRIMARY KEY (work_id, related_source, related_source_id, relation_code)
) STRICT;

-- 人物与公司。V0 不做跨库匹配，来源 ID 直接放在本表。
CREATE TABLE IF NOT EXISTS person (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    source_id   TEXT    NOT NULL,
    name        TEXT    NOT NULL CHECK (length(name) > 0),
    kind        TEXT    NOT NULL CHECK (kind IN ('individual', 'company', 'group', 'unknown')),
    snapshot_id INTEGER NOT NULL REFERENCES snapshot (id) ON DELETE RESTRICT,
    UNIQUE (source, source_id)
) STRICT;

CREATE TABLE IF NOT EXISTS work_person (
    work_id       INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    person_id     INTEGER NOT NULL REFERENCES person (id) ON DELETE CASCADE,
    position_code INTEGER NOT NULL,                    -- 职位代码，含义见 source_code 表
    PRIMARY KEY (work_id, person_id, position_code)
) STRICT;
CREATE INDEX IF NOT EXISTS work_person_person ON work_person (person_id, position_code);

-- 来源方的代码表（来自 bangumi/common 的 YAML）。
--   category = 'relation' 时，scope = 目标作品的条目类型；'staff' / 'platform' 时，scope = 作品本身的条目类型
CREATE TABLE IF NOT EXISTS source_code (
    source   TEXT    NOT NULL,
    category TEXT    NOT NULL CHECK (category IN ('relation', 'staff', 'platform')),
    scope    INTEGER NOT NULL,
    code     INTEGER NOT NULL,
    label_zh TEXT    NOT NULL,
    label_en TEXT,
    PRIMARY KEY (source, category, scope, code)
) STRICT;

-- 正版放送平台（来自 bangumi-data 的 onair 类站点；resource 类 BT 站点一律不导入）。
-- default_regions：平台默认的地区限制，JSON 数组，如 ["CN"]；NULL 表示来源没有声明（不等于全球可看）
CREATE TABLE IF NOT EXISTS platform (
    source          TEXT NOT NULL,
    site            TEXT NOT NULL,                     -- 例：'bilibili'
    title           TEXT NOT NULL CHECK (length(title) > 0),
    url_template    TEXT NOT NULL,
    default_regions TEXT CHECK (default_regions IS NULL OR json_valid(default_regions)),
    PRIMARY KEY (source, site)
) STRICT;

-- 作品曾在哪些平台上线。只说明“曾经上线”，不说明现在还能看。
CREATE TABLE IF NOT EXISTS work_platform (
    work_id     INTEGER NOT NULL REFERENCES work (id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,
    site        TEXT    NOT NULL,
    url         TEXT    NOT NULL CHECK (url LIKE 'https://%' OR url LIKE 'http://%'),
    regions     TEXT    CHECK (regions IS NULL OR json_valid(regions)),  -- 生效的地区：作品自己的设置优先，否则用平台默认
    begin       TEXT,                                   -- 在该平台开始放送的时间（ISO 8601），可能为空
    snapshot_id INTEGER NOT NULL REFERENCES snapshot (id) ON DELETE RESTRICT,
    PRIMARY KEY (work_id, source, site),
    FOREIGN KEY (source, site) REFERENCES platform (source, site)
) STRICT;
