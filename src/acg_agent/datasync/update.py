"""准备和更新本地数据（S-01）：acg-setup、acg-update、网页设置里的“立即更新”都走这里。

顺序（后面的步骤要用前面的结果）：
1. bangumi/common 的代码对照表（固定版本）
2. Bangumi Archive → data/acg.db（动画）
3. bangumi-data → data/acg.db 的正版平台
4. VNDB → data/galgame.db（用到第 2、3 步的文件补中文名、对应动画）
每一步都先看本地是不是已经最新，是就跳过；导入沿用原来的三个导入程序（失败时旧库保留不动）。
项目不发布任何数据：都从官方来源下载到使用者自己的电脑上。
"""

import shutil
import sqlite3
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from acg_agent.datasync.download import Checksum, DownloadError, download, fetch_json, file_hash, head
from acg_agent.galgame.db import DEFAULT_GALGAME_DB
from acg_agent.galgame.vndb import run_vndb_import
from acg_agent.importer.common import find_imported, utc_now
from acg_agent.importer.pipeline import run_import
from acg_agent.importer.platforms import run_platform_import

BANGUMI_LATEST = "https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json"
BANGUMI_DATA_LATEST = "https://registry.npmjs.org/bangumi-data/latest"
COMMON_COMMIT = "2ee87b9995353f77cf02e390e8635bf97dbc1fa2"  # 固定版本（2026-10-23 核对过）
COMMON_URL = "https://raw.githubusercontent.com/bangumi/common/{commit}/{name}"
COMMON_FILES = ("subject_relations.yml", "subject_staffs.yml", "subject_platforms.yml")
VNDB_DUMP = "https://dl.vndb.org/dump/vndb-db-latest.tar.zst"
NEED_FREE_BYTES_FIRST = 2_500_000_000  # 第一次：数据包约 650 MB + 两个库约 300 MB + 导入时的临时库
NEED_FREE_BYTES_UPDATE = 1_500_000_000

StepStatus = Literal["updated", "current", "failed"]
Report = Callable[[str, int, int | None], None]  # (正在做什么, 已完成字节, 总字节或 None)


@dataclass
class DataPaths:
    data_dir: Path = Path("data")
    anime_db: Path = Path("data/acg.db")
    galgame_db: Path = DEFAULT_GALGAME_DB

    @property
    def codes_dir(self) -> Path:
        return self.data_dir / "bangumi-common"

    @property
    def bangumi_data_dir(self) -> Path:
        return self.data_dir / "bangumi-data"

    @property
    def vndb_dir(self) -> Path:
        return self.data_dir / "vndb"

    @property
    def errors_dir(self) -> Path:
        return self.data_dir / "import-errors"


@dataclass
class StepResult:
    name: str
    status: StepStatus
    detail: str = ""


@dataclass
class Sources:
    """所有下载地址集中在这里；测试时换成本机的临时服务器。"""

    bangumi_latest: str = BANGUMI_LATEST
    bangumi_data_latest: str = BANGUMI_DATA_LATEST
    common_url: str = COMMON_URL
    vndb_dump: str = VNDB_DUMP


@dataclass
class _State:
    bangumi_zip: Path | None = None
    bangumi_data_json: Path | None = None
    upstream_changed: bool = False  # 动画数据有更新时，galgame 也要重新导入（中文名、对应动画用到它们）
    results: list[StepResult] = field(default_factory=list)


def _say(report: Report | None, text: str, done: int = 0, total: int | None = None) -> None:
    if report:
        report(text, done, total)


def check_disk(paths: DataPaths) -> None:
    need = NEED_FREE_BYTES_FIRST if not paths.anime_db.exists() else NEED_FREE_BYTES_UPDATE
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(paths.data_dir).free
    if free < need:
        raise DownloadError(f"硬盘空间不够：需要约 {need / 1e9:.1f} GB，现在只剩 {free / 1e9:.1f} GB")


def _keep_only(directory: Path, pattern: str, keep: Path) -> None:
    """更新成功后删掉旧的数据包，只留最新的（你的决定：省空间；导入失败时旧库本来就保留）。"""
    for old in directory.glob(pattern):
        if old.resolve() != keep.resolve() and old.is_file():
            old.unlink()


def step_common(paths: DataPaths, src: Sources, report: Report | None) -> StepResult:
    if all((paths.codes_dir / n).exists() for n in COMMON_FILES):
        return StepResult("bangumi/common", "current", f"commit {COMMON_COMMIT[:7]}")
    for name in COMMON_FILES:
        _say(report, f"下载代码对照表 {name}")
        download(src.common_url.format(commit=COMMON_COMMIT, name=name), paths.codes_dir / name)
    (paths.codes_dir / "SOURCE.txt").write_text(
        f"repo: https://github.com/bangumi/common\ncommit: {COMMON_COMMIT}\ndownloaded_at: {utc_now()}\n"
        "license: none declared (checked 2026-10-23)\n", encoding="utf-8")
    return StepResult("bangumi/common", "updated", f"commit {COMMON_COMMIT[:7]}")


def step_bangumi(paths: DataPaths, src: Sources, state: _State, report: Report | None) -> StepResult:
    _say(report, "检查 Bangumi 数据包")
    latest = fetch_json(src.bangumi_latest)
    checksum = Checksum.from_digest(latest["digest"])
    dest = paths.data_dir / latest["name"]
    state.bangumi_zip = dest
    if find_imported(paths.anime_db, "bangumi_archive", checksum.value) is not None:
        return StepResult("Bangumi", "current", latest["name"])
    download(latest["browser_download_url"], dest, checksum,
             lambda d, t: _say(report, f"下载 Bangumi 数据包 {latest['name']}", d, t or latest.get("size")))
    _say(report, "导入 Bangumi（动画）数据，约 20 秒")
    run_import(dest, paths.anime_db, checksum.value, paths.codes_dir, paths.errors_dir)
    _keep_only(paths.data_dir, "dump-*.zip", dest)
    state.upstream_changed = True
    return StepResult("Bangumi", "updated", latest["name"])


def step_bangumi_data(paths: DataPaths, src: Sources, state: _State, report: Report | None) -> StepResult:
    _say(report, "检查 bangumi-data")
    meta = fetch_json(src.bangumi_data_latest)
    version, dist = meta["version"], meta["dist"]
    json_path = paths.bangumi_data_dir / f"data-{version}.json"
    state.bangumi_data_json = json_path
    if json_path.exists() and find_imported(paths.anime_db, "bangumi_data", file_hash(json_path, "sha256")) is not None:
        return StepResult("bangumi-data", "current", version)
    tgz = paths.bangumi_data_dir / f"bangumi-data-{version}.tgz"
    download(dist["tarball"], tgz, Checksum.from_integrity(dist["integrity"]),
             lambda d, t: _say(report, f"下载 bangumi-data {version}", d, t))
    with tarfile.open(tgz, "r:gz") as tar:
        member = tar.extractfile("package/dist/data.json")  # 只取这一个文件，不解压整个包
        if member is None:
            raise DownloadError("bangumi-data 包里没有 package/dist/data.json")
        json_path.write_bytes(member.read())
    sha = file_hash(json_path, "sha256")
    (paths.bangumi_data_dir / "SOURCE.txt").write_text(
        f"package: bangumi-data@{version} (npm)\ntarball_integrity: {dist['integrity']}\ndata_json_sha256: {sha}\n"
        f"license: CC-BY-4.0\ndownloaded_at: {utc_now()}\n", encoding="utf-8")
    _say(report, "导入正版放送平台")
    run_platform_import(json_path, paths.anime_db, sha, paths.errors_dir)
    _keep_only(paths.bangumi_data_dir, "data-*.json", json_path)
    _keep_only(paths.bangumi_data_dir, "bangumi-data-*.tgz", tgz)
    state.upstream_changed = True
    return StepResult("bangumi-data", "updated", version)


def step_vndb(paths: DataPaths, src: Sources, state: _State, report: Report | None) -> StepResult:
    _say(report, "检查 VNDB 数据包")
    info = head(src.vndb_dump)
    remote = info.get("last-modified", "")
    dest = paths.vndb_dir / "vndb-db-latest.tar.zst"
    stamp = dest.with_name(dest.name + ".last-modified")  # VNDB 不公布校验值，按服务器的“最后修改时间”判断有没有新版
    have = stamp.read_text(encoding="utf-8").strip() if stamp.exists() and dest.exists() else None
    fresh_download = have != remote or not remote
    if not fresh_download and paths.galgame_db.exists() and not state.upstream_changed:
        return StepResult("VNDB", "current", remote)
    if fresh_download:
        total = int(info["content-length"]) if info.get("content-length") else None
        download(src.vndb_dump, dest, None, lambda d, t: _say(report, "下载 VNDB 数据包", d, t or total))
        stamp.write_text(remote, encoding="utf-8")
    _say(report, "导入 VNDB（galgame）数据，约 25 秒")
    run_vndb_import(dest, paths.galgame_db, None, paths.errors_dir, bangumi_zip=state.bangumi_zip,
                    bangumi_data_json=state.bangumi_data_json, force=True)
    return StepResult("VNDB", "updated", remote)


def update_all(paths: DataPaths = DataPaths(), report: Report | None = None, sources: Sources = Sources()) -> list[StepResult]:
    """依次准备每一个数据源。某一步失败就停下（后面的步骤要用它的结果），已经完成的步骤不受影响。"""
    check_disk(paths)
    state = _State()
    steps = [
        lambda: step_common(paths, sources, report),
        lambda: step_bangumi(paths, sources, state, report),
        lambda: step_bangumi_data(paths, sources, state, report),
        lambda: step_vndb(paths, sources, state, report),
    ]
    names = ["bangumi/common", "Bangumi", "bangumi-data", "VNDB"]
    for name, step in zip(names, steps, strict=True):
        try:
            state.results.append(step())
        except Exception as e:  # noqa: BLE001  任何一步失败都如实报告，不能让网页的后台任务悄悄挂掉
            state.results.append(StepResult(name, "failed", str(e)))
            break
    return state.results


# ---------- 数据状态：页面上显示“更新于 X 天前” ----------

def _latest_import(db: Path, source: str, package_column: str) -> tuple[str | None, str | None]:
    """（上次在本机导入的时间, 数据包本身的标识）：动画是数据包文件名（带日期），galgame 是数据包里的时间。"""
    if not db.exists():
        return None, None
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(f"SELECT imported_at, {package_column} FROM snapshot WHERE status = 'ready' AND source = ?"
                           " ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        return (row[0], row[1]) if row else (None, None)
    except sqlite3.OperationalError:
        return None, None
    finally:
        conn.close()


def _days_since(stamp: str | None, now: datetime | None = None) -> int | None:
    if not stamp:
        return None
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return max(0, ((now or datetime.now(UTC)) - when).days)


def data_status(paths: DataPaths = DataPaths(), now: datetime | None = None) -> dict:
    """页面上显示“更新于 X 天前”用：天数按上次在本机更新（导入）的时间算。"""
    anime, anime_pkg = _latest_import(paths.anime_db, "bangumi_archive", "file_name")
    galgame, galgame_pkg = _latest_import(paths.galgame_db, "vndb_dump", "dumped_at")
    return {
        "anime": {"imported_at": anime, "days": _days_since(anime, now), "package": anime_pkg},
        "galgame": {"imported_at": galgame, "days": _days_since(galgame, now), "package": galgame_pkg},
    }
