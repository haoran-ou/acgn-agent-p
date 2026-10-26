"""标签词表：自然语言解析时，标签类条件只能从这里选。

词表 = 同义词表里的概念 + 数据里最常见的标签（按带这个标签的作品数排序），再去掉不适合当标签条件的：
- 日期类标签（“2016”“2018年7月”“1990s”）：年份由 YearRange 条件处理
- 类型词（TV、TVA、剧场版、电影、OVA、WEB、MV）：由 FormatIn 条件处理
- 太宽泛的词（动画、日本动画、无……）
- 成人向标签：按数据判断，挂在的作品里超过一半是成人内容就排除。成人内容默认整体排除，不作为可选条件
"""

import re
import sqlite3

from acg_agent.verdict.concepts import TAG_SYNONYMS

DEFAULT_SIZE = 300

_EXCLUDED = {
    # 类型词
    "TV", "TVA", "剧场版", "电影", "OVA", "OAD", "独立ova", "WEB", "Short", "短片", "MV",
    # 太宽泛
    "动画", "动画片", "日本动画", "动画电影", "空", "无",
}
# 日期类：2016、2018年、2018年7月、1990s、2020-2029
_DATE_LIKE = re.compile(r"^\d{4}(年(\d{1,2}月)?|s|-\d{4})?$")
# 挂在的作品里成人内容占比超过这个值，就当作成人向标签
_MAX_NSFW_SHARE = 0.5

# 同义词里的非主名 → 主概念，例如“治愈系”→“治愈”
_VARIANT_TO_CONCEPT = {v: c for c, variants in TAG_SYNONYMS.items() for v in variants}


def load_vocabulary(conn: sqlite3.Connection, size: int = DEFAULT_SIZE) -> list[str]:
    concepts = list(TAG_SYNONYMS)
    seen = set(concepts)
    rows = conn.execute(
        "SELECT t.name, COUNT(DISTINCT t.work_id) AS n, AVG(w.nsfw) AS nsfw_share"
        " FROM work_tag AS t JOIN work AS w ON w.id = t.work_id"
        " GROUP BY t.name ORDER BY n DESC, t.name"
    )
    for name, _, nsfw_share in rows:
        if len(concepts) >= size:
            break
        name = _VARIANT_TO_CONCEPT.get(name, name)
        if (name in seen or name in _EXCLUDED or _DATE_LIKE.match(name)
                or nsfw_share > _MAX_NSFW_SHARE):
            continue
        seen.add(name)
        concepts.append(name)
    return concepts
