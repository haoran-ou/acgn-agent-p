"""同系列识别：把第一季、第二季、剧场版、番外篇……归成一个系列，并找出“从哪部开始看”。

做法：作品是图的节点，“同一个故事”的关系是边，一个连通分量（connected component）就是一个系列。
图算法用现成的 networkx，不自己写。
"""

import sqlite3
from dataclasses import dataclass

import networkx as nx

# 算“同一个故事”的动画关系代码（Bangumi，目标是动画时的含义，见 source_code 表）：
# 2 前传、3 续集、4 总集篇、5 全集、6 番外篇、12 主线故事。
# 不算的：10 不同演绎（重制版，剧情可能差很多）、8 相同世界观（如《魔法禁书目录》和《超电磁炮》）、
# 7 角色出演、11 衍生、14 联动、99 其他
SAME_STORY_CODES = (2, 3, 4, 5, 6, 12)
# 番外篇、主线故事：只有一方不是连续剧（TV / WEB）时才合并。
# 实测：Bangumi 把《魔法禁书目录》标成《超电磁炮》的“主线故事”，但两者都是完整的 TV 动画，应该是两个系列；
# 而 OVA 番外、剧场版外传应该归入本篇系列
SIDE_STORY_CODES = (6, 12)
_SERIES_FORMATS = {"tv", "web"}
ANIME_TYPE = 2

# 类型优先级：选“从哪部开始看”时，TV / WEB 正篇优先于 OVA、剧场版。
# 实测：《日常》的 OAD 比 TV 版早几天发售，只按日期会把 OAD 当成入坑作
_FORMAT_PRIORITY = {"tv": 0, "web": 1, "movie": 2, "ova": 3, "motion_comic": 4, "other": 5}
_MAIN_PRIORITY = 1  # 优先级不超过它的（tv、web）算正篇


@dataclass(frozen=True)
class SeriesIndex:
    series_of: dict[int, int]  # 作品 → 系列 ID（系列 ID = 入坑作的作品 ID）
    members: dict[int, list[int]]  # 系列 ID → 成员，按“入坑优先级”排列（正篇在前，再按播出时间）
    story_key: dict[int, tuple]  # 作品 → (播出日期, 类型优先级, ID)，按播出时间排列用

    def series(self, work_id: int) -> int:
        return self.series_of.get(work_id, work_id)  # 没有任何关系的作品自成一个系列

    def size(self, work_id: int) -> int:
        return len(self.members.get(self.series(work_id), [work_id]))

    def entry(self, work_id: int) -> int:
        return self.series(work_id)

    def order(self, work_id: int) -> tuple:
        """按播出时间排列用。"""
        return self.story_key.get(work_id, ("9999-99-99", 9, work_id))

    def entry_order(self, work_id: int) -> tuple:
        """选“从哪部开始看”用：先正篇（TV / WEB），再按播出时间。"""
        date, priority, wid = self.order(work_id)
        return (priority > _MAIN_PRIORITY, date, priority, wid)


def build_series_index(conn: sqlite3.Connection) -> SeriesIndex:
    edges = conn.execute(
        "SELECT r.work_id, e.work_id, r.relation_code, wa.format, wb.format FROM work_relation AS r"
        " JOIN external_id AS e ON e.source = r.related_source AND e.source_id = r.related_source_id"
        " JOIN work AS wa ON wa.id = r.work_id JOIN work AS wb ON wb.id = e.work_id"
        f" WHERE r.related_source_type = ? AND r.relation_code IN ({', '.join('?' for _ in SAME_STORY_CODES)})",
        (ANIME_TYPE, *SAME_STORY_CODES),
    ).fetchall()
    graph = nx.Graph()
    for a, b, code, fmt_a, fmt_b in edges:
        if a == b:
            continue
        if code in SIDE_STORY_CODES and fmt_a in _SERIES_FORMATS and fmt_b in _SERIES_FORMATS:
            continue  # 两部都是连续剧的外传关系：各自成系列
        graph.add_edge(a, b)

    nodes = list(graph.nodes)
    story_key = {}
    for start in range(0, len(nodes), 900):
        batch = nodes[start : start + 900]
        for wid, date, fmt in conn.execute(
            f"SELECT id, release_date, format FROM work WHERE id IN ({', '.join('?' for _ in batch)})", batch
        ):
            # 没有日期的排在最后，不当入坑作
            story_key[wid] = (date or "9999-99-99", _FORMAT_PRIORITY.get(fmt, 9), wid)

    def entry_key(w):
        date, priority, wid = story_key.get(w, ("9999-99-99", 9, w))
        return (priority > _MAIN_PRIORITY, date, priority, wid)

    series_of, members = {}, {}
    for component in nx.connected_components(graph):
        ordered = sorted(component, key=entry_key)
        series_id = ordered[0]
        members[series_id] = ordered
        for wid in ordered:
            series_of[wid] = series_id
    return SeriesIndex(series_of, members, story_key)
