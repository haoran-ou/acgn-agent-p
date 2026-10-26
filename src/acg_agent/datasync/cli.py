"""命令行：uv run acg-setup（第一次准备数据）、uv run acg-update（更新数据）。两者做的事一样，只是提示不同。"""

import argparse
import logging
import sys
import time
from pathlib import Path

from acg_agent.datasync.download import DownloadError
from acg_agent.datasync.update import DataPaths, update_all

STATUS_TEXT = {"updated": "已更新", "current": "已是最新", "failed": "失败"}


class _Printer:
    """进度：同一行刷新；每秒最多刷新几次，免得刷屏。"""

    def __init__(self) -> None:
        self.last = 0.0
        self.text = ""

    def __call__(self, text: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        if text == self.text and now - self.last < 0.3 and (total is None or done < total):
            return
        if text != self.text and self.text:
            print()
        self.text, self.last = text, now
        if total:
            print(f"\r{text}  {done / 1e6:.0f} / {total / 1e6:.0f} MB（{done / total:.0%}）", end="", flush=True)
        elif done:
            print(f"\r{text}  {done / 1e6:.0f} MB", end="", flush=True)
        else:
            print(f"\r{text}…", end="", flush=True)


def _run(intro: str, argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=intro)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    paths = DataPaths(data_dir=args.data_dir, anime_db=args.data_dir / "acg.db", galgame_db=args.data_dir / "galgame.db")
    print(intro)
    try:
        results = update_all(paths, _Printer())
    except DownloadError as e:
        print(f"\n失败：{e}", file=sys.stderr)
        return 1
    print("\n")
    for r in results:
        print(f"  {r.name}：{STATUS_TEXT[r.status]}  {r.detail}")
    if any(r.status == "failed" for r in results):
        print("\n有一步失败了，已经完成的部分不受影响。检查网络后再运行一次即可（会接着下载）。", file=sys.stderr)
        return 1
    print("\n完成。运行 uv run acg-serve 打开网页。")
    return 0


def main_setup(argv: list[str] | None = None) -> int:
    return _run("准备数据：从官方来源下载 Bangumi、bangumi-data、VNDB 的数据包并导入（第一次约 650 MB、几分钟）", argv)


def main_update(argv: list[str] | None = None) -> int:
    return _run("更新数据：检查各个数据包有没有新版本，有就下载并导入", argv)
