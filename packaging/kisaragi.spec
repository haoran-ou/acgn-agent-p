# PyInstaller 打包配置（C-02）：uv run pyinstaller packaging/kisaragi.spec --noconfirm
# macOS：dist/如月寮.app（文件夹形式的应用包）；Windows：dist/如月寮.exe（单个文件，双击就能运行）。
# 数据不打进程序：第一次打开时在窗口里下载（见 acg_agent/datasync）。
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP = "如月寮"

# 程序里用到、但 PyInstaller 从 import 语句里看不出来的东西：
datas = collect_data_files("acg_agent", excludes=["**/.DS_Store"])  # 网页文件、两个 schema.sql
datas += collect_data_files("opencc")  # 繁简转换的词典
# 有些库启动时读自己的版本信息（importlib.metadata），打包时要把包信息一起带上（TS-049）
datas += copy_metadata("pydantic-ai-slim", recursive=True)
datas += copy_metadata("acg-agent")
hiddenimports = ["acg_agent.api.app"]  # uvicorn 按字符串 "acg_agent.api.app:create_app" 加载
hiddenimports += collect_submodules("uvicorn")  # uvicorn 按名字挑选事件循环、协议实现
hiddenimports += collect_submodules("pydantic_ai.models")
hiddenimports += collect_submodules("pydantic_ai.providers")

a = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP, console=False)
    coll = COLLECT(exe, a.binaries, a.datas, name=APP)
    app = BUNDLE(
        coll,
        name=f"{APP}.app",
        bundle_identifier="io.github.haoran-ou.kisaragi",
        info_plist={"CFBundleDisplayName": APP, "CFBundleShortVersionString": "0.1.0",
                    "NSHighResolutionCapable": True},
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=APP, console=False)  # 单个 .exe
