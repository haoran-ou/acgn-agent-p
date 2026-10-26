-- galgame 数据库结构（G-01）。和动画分开存在 data/galgame.db（你的决定，2026-10-25）。
-- 数据来自 VNDB 数据包：ODbL 1.0（数据库）+ DbCL 1.0（内容）；作品简介另有来源，许可证以 VNDB 说明为准。
-- 和动画库不同：VNDB 的 ID（如 'v17'）本身稳定，直接当主键；每次导入整库重建，
-- 因为 VNDB 要求再发布时删除已经不在数据包里的数据，重建最省事，也不需要墓碑记录。
-- 规则同动画库：全部 STRICT；缺失存 NULL，不用 0 或空字符串冒充。

CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,                      -- 'vndb_dump'
    file_name   TEXT    NOT NULL,
    sha256      TEXT    NOT NULL CHECK (length(sha256) = 64),
    size_bytes  INTEGER NOT NULL CHECK (size_bytes > 0),
    tag_cap     INTEGER,                               -- 和动画库同一结构；VNDB 标签没有数量上限，恒为 NULL
    dumped_at   TEXT,                                  -- 数据包里 TIMESTAMP 文件的时间
    bangumi_file TEXT,                                 -- 补中文名用的 Bangumi Archive 数据包（没用就是 NULL）
    bangumi_data_file TEXT,                            -- 对应动画用的 bangumi-data（没用就是 NULL）
    status      TEXT    NOT NULL CHECK (status IN ('importing', 'ready')),
    imported_at TEXT    NOT NULL,
    UNIQUE (source, sha256)
) STRICT;

-- 作品（VNDB 的 visual novel）。一部作品可以有多个发行版（release）：日文原版、汉化版、全年龄移植版……
CREATE TABLE IF NOT EXISTS vn (
    id             TEXT    PRIMARY KEY CHECK (id GLOB 'v[0-9]*'),
    title_original TEXT    NOT NULL CHECK (length(title_original) > 0),  -- 原语言的标题
    title_zh       TEXT    CHECK (title_zh IS NULL OR length(title_zh) > 0),  -- 简体优先，没有就用繁体
    title_zh_source TEXT   CHECK (title_zh_source IS NULL OR title_zh_source IN ('vndb', 'bangumi')),
    bangumi_id     TEXT,                               -- 中文名来自 Bangumi 时，对应的 Bangumi 游戏条目（CC BY-SA 3.0，要署名）
    title_ja       TEXT,
    title_latin    TEXT,                               -- 原标题的罗马字
    olang          TEXT    NOT NULL,                   -- 原语言，例：'ja'
    release_date   TEXT    CHECK (release_date IS NULL OR
                       release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    release_year   INTEGER CHECK (release_year IS NULL OR release_year BETWEEN 1970 AND 2100),  -- 只知道年份时也有
    min_age        INTEGER CHECK (min_age IS NULL OR min_age BETWEEN 0 AND 21),  -- 各发行版年龄分级的最大值
    has_ero        INTEGER NOT NULL CHECK (has_ero IN (0, 1)),  -- 任何一个发行版含色情内容
    has_all_ages   INTEGER NOT NULL CHECK (has_all_ages IN (0, 1)),  -- 有不含色情内容的发行版（全年龄版）
    rating         REAL    CHECK (rating IS NULL OR rating BETWEEN 1 AND 10),  -- VNDB 的贝叶斯评分，10 分制
    vote_count     INTEGER NOT NULL CHECK (vote_count >= 0),
    length_minutes INTEGER CHECK (length_minutes IS NULL OR length_minutes > 0),  -- 玩家投票的游玩时长中位数
    description    TEXT,                               -- 已去掉 BBCode 和剧透段落
    cover_id       TEXT    CHECK (cover_id IS NULL OR cover_id GLOB 'cv[0-9]*'),
    cover_sexual   REAL    CHECK (cover_sexual IS NULL OR cover_sexual BETWEEN 0 AND 2),  -- 0 安全 · 1 擦边 · 2 露骨（投票平均）
    cover_votes    INTEGER CHECK (cover_votes IS NULL OR cover_votes >= 0),
    tag_count      INTEGER NOT NULL CHECK (tag_count >= 0),  -- 平均分 > 0 的标签有几个；“排除某标签”判断时用来看标签是否齐全
    minor_sexual   INTEGER NOT NULL CHECK (minor_sexual IN (0, 1)),  -- 被标了“涉及未成年角色的性内容”（见 galgame/vndb.py）
    egs_id         TEXT,                               -- 批評空間的作品 ID（来自 VNDB 的外部链接）
    snapshot_id    INTEGER NOT NULL REFERENCES snapshot (id) ON DELETE RESTRICT,
    CHECK (vote_count > 0 OR rating IS NULL),
    CHECK ((title_zh IS NULL) = (title_zh_source IS NULL)),
    CHECK (bangumi_id IS NULL OR title_zh_source = 'bangumi')
) STRICT;

-- 作品的所有名字（各语言标题、罗马字、别名），检索用。和动画库的 alias 同一套规范化。
CREATE TABLE IF NOT EXISTS vn_alias (
    id         INTEGER PRIMARY KEY,
    vn_id      TEXT    NOT NULL REFERENCES vn (id) ON DELETE CASCADE,
    text       TEXT    NOT NULL CHECK (length(text) > 0),
    normalized TEXT    NOT NULL CHECK (length(normalized) > 0),
    search_key TEXT    NOT NULL CHECK (length(search_key) > 0),
    kind       TEXT    NOT NULL CHECK (kind IN ('original', 'zh', 'title', 'latin', 'alias')),
    UNIQUE (vn_id, normalized)
) STRICT;
CREATE INDEX IF NOT EXISTS vn_alias_normalized ON vn_alias (normalized);

CREATE VIRTUAL TABLE IF NOT EXISTS vn_alias_fts USING fts5(
    search_key,
    content = 'vn_alias',
    content_rowid = 'id',
    tokenize = 'trigram'
);

-- VNDB 的标签定义。cat：cont 内容 · ero 色情 · tech 技术。
CREATE TABLE IF NOT EXISTS tag (
    id         TEXT    PRIMARY KEY CHECK (id GLOB 'g[0-9]*'),
    name       TEXT    NOT NULL,
    cat        TEXT    NOT NULL CHECK (cat IN ('cont', 'ero', 'tech')),
    searchable INTEGER NOT NULL CHECK (searchable IN (0, 1)),
    applicable INTEGER NOT NULL CHECK (applicable IN (0, 1)),  -- 0：只是分组用的父标签，不能直接打
    aliases    TEXT    NOT NULL                        -- JSON 数组
) STRICT;

CREATE TABLE IF NOT EXISTS tag_parent (
    tag_id    TEXT NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
    parent_id TEXT NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
    PRIMARY KEY (tag_id, parent_id)
) STRICT;

-- 作品的标签，由 VNDB 的逐人投票汇总而来（不保存是谁投的票）。
-- 投票是 -3 到 3：正数表示“有这个要素”（数字越大越重要），负数表示“没有”。这比 Bangumi 的标签多了反面证据。
CREATE TABLE IF NOT EXISTS vn_tag (
    vn_id    TEXT    NOT NULL REFERENCES vn (id) ON DELETE CASCADE,
    tag_id   TEXT    NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
    votes    INTEGER NOT NULL CHECK (votes >= 1),     -- 有效投票数（不含被管理员忽略的票）
    positive INTEGER NOT NULL CHECK (positive >= 0),
    negative INTEGER NOT NULL CHECK (negative >= 0),
    score    REAL    NOT NULL CHECK (score BETWEEN -3 AND 3),  -- 投票平均；VNDB 网站上平均 > 0 才算有这个标签
    spoiler  REAL    CHECK (spoiler IS NULL OR spoiler BETWEEN 0 AND 2),  -- 剧透等级平均：0 无 · 1 轻微 · 2 严重
    PRIMARY KEY (vn_id, tag_id),
    CHECK (positive + negative <= votes)
) STRICT;
CREATE INDEX IF NOT EXISTS vn_tag_tag ON vn_tag (tag_id, score);

-- 作品之间的关系（续作、同一世界观等），以后做系列折叠用。
CREATE TABLE IF NOT EXISTS vn_relation (
    vn_id         TEXT    NOT NULL REFERENCES vn (id) ON DELETE CASCADE,
    related_vn_id TEXT    NOT NULL,                   -- 不做外键：关联作品可能被过滤掉
    relation      TEXT    NOT NULL,                   -- VNDB 的关系代码，例：'seq' 续作、'preq' 前作、'char' 共享角色
    official      INTEGER NOT NULL CHECK (official IN (0, 1)),
    PRIMARY KEY (vn_id, related_vn_id)
) STRICT;

-- 作品对应的动画（G-05）。bangumi_id 是动画在 Bangumi 的条目 ID，查动画库时用 external_id 换成作品 ID。
-- 两个来源（都在导入时算好）：
--   vndb_anidb：VNDB 记录了作品对应哪部 AniDB 动画（vn_anime 表），再用 bangumi-data 把 AniDB 编号换成 Bangumi 编号
--   bangumi_relation：Bangumi 的动画条目和游戏条目之间有“改编”关系，而游戏条目就是这部作品（vn.bangumi_id）
CREATE TABLE IF NOT EXISTS vn_anime (
    vn_id      TEXT NOT NULL REFERENCES vn (id) ON DELETE CASCADE,
    bangumi_id TEXT NOT NULL,
    via        TEXT NOT NULL CHECK (via IN ('vndb_anidb', 'bangumi_relation')),
    PRIMARY KEY (vn_id, bangumi_id)
) STRICT;
CREATE INDEX IF NOT EXISTS vn_anime_bangumi ON vn_anime (bangumi_id);

