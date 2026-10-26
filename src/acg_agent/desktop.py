"""桌面客户端（C-01）：双击打开一个独立窗口，不用碰终端。uv run --extra desktop acg-desktop

做法：在后台线程里启动和 acg-serve 一样的服务（只在 127.0.0.1 的一个空闲端口上），等它准备好，
再用 pywebview（BSD 许可）打开一个原生窗口显示页面；关掉窗口，服务一起停。界面就是现有的网页，不用重写。

数据不放在程序旁边（打包后的程序不能往自己的安装目录里写），而是放进系统的用户数据文件夹：
  macOS：~/Library/Application Support/如月寮    Windows：%APPDATA%\\如月寮
第一次打开时页面会引导“准备数据”（下载和导入，见 datasync/）。
"""

import html
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "如月寮"
WINDOW_SIZE = (1100, 820)
READY_TIMEOUT_S = 20.0


def data_dir() -> Path:
    """环境变量 ACG_DATA_DIR 优先（测试和高级用户用），否则用系统的用户数据文件夹（platformdirs，MIT 许可）。"""
    if os.environ.get("ACG_DATA_DIR"):
        return Path(os.environ["ACG_DATA_DIR"])
    from platformdirs import user_data_dir  # noqa: PLC0415  只有桌面版需要这个库

    return Path(user_data_dir(APP_NAME, appauthor=False))


def use_data_dir(path: Path) -> None:
    """让服务的所有文件都落在这个文件夹里：两个库、设置、日志（app.py 和 config.py 读这些环境变量）。"""
    path.mkdir(parents=True, exist_ok=True)
    os.environ["ACG_DB"] = str(path / "acg.db")
    os.environ["ACG_GALGAME_DB"] = str(path / "galgame.db")
    os.environ["ACG_SETTINGS"] = str(path / "settings.json")
    os.environ["ACG_LOG_DIR"] = str(path / "logs")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))  # 让系统挑一个没人用的端口：不会和用户自己开的 acg-serve 冲突
        return s.getsockname()[1]


class BackgroundServer:
    """在后台线程里跑 uvicorn；stop() 让它正常退出（处理完手上的请求）。"""

    def __init__(self, port: int):
        import uvicorn  # noqa: PLC0415

        config = uvicorn.Config("acg_agent.api.app:create_app", factory=True, host="127.0.0.1", port=port,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{port}/"

    def start(self, timeout_s: float = READY_TIMEOUT_S) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout_s
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("服务没能启动")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def log_to_file(folder: Path) -> Path:
    """打包成窗口程序后没有控制台：Windows 上 sys.stdout / sys.stderr 是 None，uvicorn 一写日志就崩（TS-049）。
    把它们都接到数据文件夹里的 logs/desktop.log，出问题时也能查。"""
    log = folder / "logs" / "desktop.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        stream = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
        logging.basicConfig(stream=stream, level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    return log


def main() -> int:
    import webview  # noqa: PLC0415  pywebview：macOS 用系统自带的 WebKit，Windows 用 Edge WebView2

    folder = data_dir()
    use_data_dir(folder)
    log = log_to_file(folder)
    server = BackgroundServer(free_port())
    try:
        server.start()
    except Exception as e:  # noqa: BLE001  窗口程序没有控制台：失败原因显示在窗口里
        logging.exception("启动失败")
        message = f"<h2>如月寮启动失败</h2><p>{html.escape(str(e))}</p><p>详细记录：{html.escape(str(log))}</p>"
        webview.create_window(APP_NAME, html=message, width=520, height=260)
        webview.start()
        return 1
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # VNDB、Bangumi 等外部链接在系统浏览器里打开
    webview.create_window(APP_NAME, server.url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=(400, 500),
                          text_select=True)  # 允许选中文字（复制作品名）
    try:
        webview.start()  # 一直到窗口关掉才返回
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
