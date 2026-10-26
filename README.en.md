# Kisaragi Dormitory 如月寮 (ACG Agent)

[中文](README.md) | **English**

Describe the anime you want to watch or the galgame (visual novel) you want to play in one sentence, and get matching works back. Every condition comes with the evidence used to judge it (satisfied / likely satisfied / unknown / conflict).

The interface is in Chinese, and requests work best in Chinese.

- **Anime**: data from Bangumi (番组计划), with covers, synopses, Bangumi links and, where recorded, the official streaming platforms the show has been released on
- **Galgame**: data from VNDB, with covers and links to VNDB and ErogameScape (批評空間). Only available after confirming you are 18 or older in Settings
- **Agent**: requests that tags can't express, such as "something like CLANNAD" or "the leads save each other", go to an AI agent that looks things up before recommending. Anime and galgame each have their own agent. Supported providers: Zhipu GLM, SiliconFlow, Qwen (Alibaba Cloud Bailian), DeepSeek, Kimi, Gemini and any OpenAI-compatible API. The free Zhipu GLM is the default recommendation
- **Cross-links**: galgame cards show their anime adaptations, and anime cards show related games
- **Appearance**: dark / light theme, accent color, your own background image, blur, dimming, panel opacity and font size

Everything runs on your own computer. Data dumps are downloaded from the official sources straight to your computer and never pass through any third-party server.

## Download the app (recommended, no terminal needed)

Download from **Releases** on the right side of the GitHub page:

- **Windows**: download `如月寮.exe` and double-click it (**not yet tested on Windows**)
- **macOS**: download `如月寮-macOS.zip`, double-click to unzip it, and drag "如月寮" into Applications (Apple silicon Macs only for now, i.e. M1 and later)

On first launch the window shows a **开始准备数据** (Prepare data) button. Click it and the app downloads about 650 MB of data from the official sources and imports it (a few minutes). Then you can add an API key under **设置** (Settings) in the top right (optional, see step 6 below). Data is stored in your user data folder (macOS: `~/Library/Application Support/如月寮`, Windows: `%APPDATA%\如月寮`). To uninstall, delete that folder.

**System warning on first launch**: the app is not code-signed (signing costs a yearly fee), so your system will say it can't verify the developer. This is expected:

- **macOS**: in Applications, **right-click** "如月寮" → **Open**, then click **Open** again in the dialog. You only need to do this once. If there is no "Open" button, go to System Settings → Privacy & Security and click "Open Anyway" at the bottom
- **Windows**: when "Windows protected your PC" appears, click **More info** → **Run anyway**. If the window doesn't open (older Windows), install Microsoft's [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) first

The rest of this page explains how to run from source, for people comfortable with a terminal.

## Requirements

- macOS or Linux (Windows steps are included but **untested**)
- About 3 GB of disk space and an internet connection (about 650 MB is downloaded the first time)
- Optional: an API key from an AI provider (the free Zhipu GLM is recommended). Without one the app still works, but sentences are understood by rules only and the agent is unavailable

## Run from source: install and start (first time)

**1. Install uv** (a Python package manager that also installs the right Python version for you)

Open Terminal, paste this line and press Enter:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (untested): open PowerShell and run

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

When it finishes, **close the terminal and open it again** so the `uv` command is available.

**2. Download this project**

On the GitHub page click the green **Code → Download ZIP** button, and unzip it somewhere you can find it (for example your Documents folder).

**3. Go to the folder in the terminal**

Type `cd ` (note the space after cd), drag the unzipped folder into the terminal window, and press Enter.

**4. Prepare the data** (only once, takes a few minutes)

```bash
uv run acg-setup
```

It downloads the Bangumi, bangumi-data and VNDB dumps, verifies them and imports them, showing progress as it goes. If the connection drops, just run it again and it resumes where it stopped.

**5. Start**

```bash
uv run acg-serve
```

Your browser opens `http://127.0.0.1:8000/` automatically. Press `Ctrl + C` in the terminal to stop. Next time, only steps 3 and 5 are needed.

**6. Choose an AI provider and add a key (optional, recommended)**

The app works without one, but sentences are understood by rules only and the agent is unavailable. **Zhipu GLM** is recommended: it is reachable from mainland China, and GLM-4.7-Flash is free.

1. Open the [Zhipu Open Platform](https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys), sign up and log in, click "添加新的 API Key" (Add API key), and copy the generated key
2. Click **设置** (Settings) in the top right of the app, choose **智谱 GLM** as the provider, keep the default model `glm-4.7-flash`, paste your key, and click **保存并测试连接** (Save and test connection). When you see "✓ 可以用" (works), you're done

You can also use the other providers (each provider's key is saved separately, so switching doesn't lose them):

| Provider | Reachable from mainland China | Cost (as of 2026-10; check the official site) |
|---|---|---|
| Zhipu GLM | Yes | GLM-4.7-Flash is free (with concurrency limits) |
| SiliconFlow | Yes | Some small models are free |
| Qwen (Alibaba Cloud Bailian) | Yes | Free quota for new users, then paid |
| DeepSeek | Yes | Paid, very cheap |
| Kimi (Moonshot AI) | Yes | Paid |
| Google Gemini | No | Free quota |
| Custom | — | Any OpenAI-compatible API, e.g. a local Ollama |

Keys are stored only on your computer (`data/settings.json`, readable only by you), and the page shows only the last 4 characters. Free models have rate limits: if you ask too quickly, the agent temporarily falls back to a normal search. Wait a moment and ask again.

**Window version (no browser)**: you can also run `uv run --extra desktop acg-desktop` to open a standalone "如月寮" window. On first launch the window shows the Prepare data button, so you don't need `acg-setup`. Data goes into your user data folder (macOS: `~/Library/Application Support/如月寮`).

## Everyday use

- **Updating data**: the top of the page shows how long ago the local data was updated. Click **立即更新** (Update now) under **设置 → 数据** (Settings → Data) to download and import new data in the background; you can keep searching meanwhile. You can also run `uv run acg-update` in the terminal
- **Galgame**: turn on the switch under **设置 → 成人内容** (Settings → Adult content) and confirm you are 18 or older; the Galgame tab then appears at the top
- **Agent**: in Settings you can turn the agent off (faster, uses less quota) or send every request to the agent
- **Appearance**: under **设置 → 外观** (Settings → Appearance) you can switch dark / light theme and accent color, choose your own background image (PNG / JPEG / WebP, up to 10 MB, stored only on your computer), and adjust background blur, dimming, panel opacity and font size. Changes preview live and take effect when you click Save

## FAQ

- **`address already in use`**: a previous instance is still running. Find that terminal window and press `Ctrl + C`, or use another port: `uv run acg-serve --port 8001`
- **Download failed**: check your connection and run `uv run acg-setup` again. Parts already downloaded won't be downloaded again
- **Not enough disk space**: about 2.5 GB of free space is needed
- **The agent says it couldn't finish**: usually the free model's rate limit. Wait a moment and ask again, or click "Save and test connection" in Settings to check your key and model
- **Connection test times out**: Gemini is not available in mainland China. Switch to one of the Chinese providers

## Data sources and attribution

This project does not distribute any data with the code. The app downloads the dumps from the official sources below to the user's own computer and imports them locally.

| Source | Used for | License |
|---|---|---|
| [Bangumi](https://bgm.tv/) ([Bangumi Archive](https://github.com/bangumi/archive)) | Anime entries, tags, synopses, covers (loaded on demand from the Bangumi API); some Chinese galgame titles; anime–game adaptation relations | Entry information under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), copyright belongs to the creators; use follows the [Bangumi developer terms](https://bangumi.tv/about/copyright) |
| [bangumi-data](https://github.com/bangumi-data/bangumi-data) | Official streaming platforms the anime has been released on (onair entries only); anime ID mapping | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [bangumi/common](https://github.com/bangumi/common) | Code tables for relation types, staff roles, etc. | Not stated |
| [VNDB](https://vndb.org/) ([database dumps](https://vndb.org/d14)) | Galgame entries, tag votes, covers, ErogameScape links, related anime | Database [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), contents DbCL 1.0; descriptions come from various sources; non-commercial use only |

For ErogameScape (エロゲー批評空間) the app only provides links; see ErogameScape itself for scores and reviews.

**Privacy**: the sentences you type are sent to the AI provider you chose in Settings so it can understand them (when a key is set). Cover images are loaded by your browser directly from Bangumi and VNDB.

## Open-source libraries used

FastAPI, Uvicorn, Pydantic, Pydantic AI (MIT), OpenAI Python SDK (Apache 2.0, used to connect to the providers' OpenAI-compatible APIs), PyYAML (MIT), opencc-python-reimplemented (Apache 2.0), networkx (BSD-3-Clause), zstandard (BSD-3-Clause), certifi (MPL 2.0). The window version also uses pywebview (BSD-3-Clause), platformdirs (MIT) and PyObjC (MIT, macOS only). The installers are built with PyInstaller (GPL-2.0 with an exception that allows bundling any software).

## License

The code is released under the [MIT License](LICENSE). Data is not included in this repository; each source follows its own license listed above.
