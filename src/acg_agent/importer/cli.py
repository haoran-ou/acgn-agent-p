"""命令行入口：uv run acg-import <zip> --expected-sha256 <值>"""

import argparse
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from acg_agent.galgame.db import DEFAULT_GALGAME_DB
from acg_agent.galgame.vndb import run_vndb_import
from acg_agent.importer.archive import ArchiveError
from acg_agent.importer.common import ImportFailed
from acg_agent.importer.pipeline import run_import
from acg_agent.importer.platforms import run_platform_import


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 Bangumi Archive 数据包导入本地 SQLite")
    parser.add_argument("zip", type=Path, help="数据包路径，例如 data/dump-2026-10-20.210332Z.zip")
    parser.add_argument(
        "--expected-sha256",
        required=True,
        help="latest.json 里的 digest（带不带 'sha256:' 前缀都可以）",
    )
    parser.add_argument("--db", type=Path, default=Path("data/acg.db"))
    parser.add_argument("--codes-dir", type=Path, default=Path("data/bangumi-common"))
    parser.add_argument("--errors-dir", type=Path, default=Path("data/import-errors"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        result = run_import(
            args.zip, args.db, args.expected_sha256, args.codes_dir, args.errors_dir
        )
    except (ArchiveError, ImportFailed, FileNotFoundError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1

    if result.status == "skipped":
        print(f"跳过：这个数据包已经导入过（snapshot {result.snapshot_id}）")
        return 0
    print(f"成功：snapshot {result.snapshot_id}")
    for key, value in asdict(result.stats).items():
        print(f"  {key}: {value}")
    if result.error_report:
        print(f"  错误报告：{result.error_report}")
    return 0


def main_platforms(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 bangumi-data 的正版放送平台（需要先运行 acg-import）")
    parser.add_argument("json", type=Path, help="bangumi-data 的 data.json，例如 data/bangumi-data/data-0.3.234.json")
    parser.add_argument("--expected-sha256", required=True, help="data.json 的 sha256（见 data/bangumi-data/SOURCE.txt）")
    parser.add_argument("--db", type=Path, default=Path("data/acg.db"))
    parser.add_argument("--errors-dir", type=Path, default=Path("data/import-errors"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        result = run_platform_import(args.json, args.db, args.expected_sha256, args.errors_dir)
    except (ArchiveError, ImportFailed, FileNotFoundError, ValueError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    if result.status == "skipped":
        print(f"跳过：这个文件已经导入过（snapshot {result.snapshot_id}）")
        return 0
    print(f"成功：snapshot {result.snapshot_id}")
    for key, value in asdict(result.stats).items():
        print(f"  {key}: {value}")
    if result.error_report:
        print(f"  报告：{result.error_report}")
    return 0


def main_vndb(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 VNDB 数据包导入 galgame 库（data/galgame.db，每次整库重建）")
    parser.add_argument("dump", type=Path, help="VNDB 数据包，例如 data/vndb/vndb-db-latest.tar.zst")
    parser.add_argument("--expected-sha256", default=None, help="可选：VNDB 不公布校验值，不传就只记录实际值")
    parser.add_argument("--db", type=Path, default=DEFAULT_GALGAME_DB)
    parser.add_argument("--errors-dir", type=Path, default=Path("data/import-errors"))
    parser.add_argument("--bangumi-zip", type=Path, default=None,
                        help="补中文名用的 Bangumi Archive 数据包；不传就自动用 data/ 下最新的 dump-*.zip")
    parser.add_argument("--no-bangumi", action="store_true", help="不用 Bangumi 补中文名和动画关联")
    parser.add_argument("--bangumi-data", type=Path, default=None,
                        help="对应动画用的 bangumi-data 的 data.json；不传就自动用 data/bangumi-data/ 下最新的")
    parser.add_argument("--force", action="store_true", help="同一个 VNDB 数据包也重新导入")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    bangumi_zip = None
    if not args.no_bangumi:
        bangumi_zip = args.bangumi_zip or max(Path("data").glob("dump-*.zip"), default=None)
        print(f"补中文名：{bangumi_zip}" if bangumi_zip else "没找到 Bangumi 数据包，不补中文名")
    bangumi_data = args.bangumi_data or max(Path("data/bangumi-data").glob("data-*.json"), default=None)
    print(f"对应动画：{bangumi_data}" if bangumi_data else "没找到 bangumi-data，只用 Bangumi 的改编关系")
    try:
        result = run_vndb_import(args.dump, args.db, args.expected_sha256, args.errors_dir,
                                 bangumi_zip=bangumi_zip, force=args.force, bangumi_data_json=bangumi_data)
    except (ArchiveError, ImportFailed, FileNotFoundError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    if result.status == "skipped":
        print(f"跳过：这个数据包已经导入过（snapshot {result.snapshot_id}）")
        return 0
    print(f"成功：snapshot {result.snapshot_id}")
    for key, value in asdict(result.stats).items():
        print(f"  {key}: {value}")
    if result.error_report:
        print(f"  错误报告：{result.error_report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
