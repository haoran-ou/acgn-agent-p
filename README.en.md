# Kisaragi Dormitory 如月寮 (ACG Agent)

[中文](README.md) | **English**

Describe the anime you want to watch or the galgame (visual novel) you want to play in one sentence, and get matching works back. Every condition comes with the evidence used to judge it (satisfied / likely satisfied / unknown / conflict).

The interface is in Chinese, and requests work best in Chinese.

- **Anime**: data from Bangumi (番组计划), with covers, synopses, Bangumi links and, where recorded, the official streaming platforms the show has been released on
- **Galgame**: data from VNDB, with covers and links to VNDB and ErogameScape (批評空間). Only available after confirming you are 18 or older in Settings
- **Agent**: requests that tags can't express, such as "something like CLANNAD" or "the leads save each other", go to an AI agent that looks things up before recommending. Anime and galgame each have their own agent
- **Cross-links**: galgame cards show their anime adaptations, and anime cards show related games
- **Appearance**: dark / light theme, accent color, your own background image

Everything runs on your own computer. Data is downloaded from the official sources straight to your computer and never passes through any third-party server.

## Download

Download from [Releases](https://github.com/haoran-ou/acgn-agent-p/releases/latest):

| System | Download | How to open |
|---|---|---|
| Windows 10 / 11 | `Kisaragi-Windows.exe` | Double-click it, no installation needed (**not yet tested on Windows**) |
| macOS (Apple silicon, M1 and later) | `Kisaragi-macOS.zip` | Double-click to unzip it, then drag "如月寮" into Applications |

You need about 3 GB of disk space, and about 650 MB of data is downloaded on first use.

## First launch

**1. System warning**: the app is not code-signed (signing costs a yearly fee), so your system will say it can't verify the developer. This is expected:

- **macOS**: in Applications, **right-click** "如月寮" → **Open**, then click **Open** again in the dialog. You only need to do this once. If there is no "Open" button, go to System Settings → Privacy & Security and click "Open Anyway" at the bottom
- **Windows**: when "Windows protected your PC" appears, click **More info** → **Run anyway**. If the window doesn't open (older Windows), install Microsoft's [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) first

**2. Prepare the data**: click **开始准备数据** (Prepare data) in the window. The app downloads data from the official Bangumi, bangumi-data and VNDB sources, verifies it and imports it, which takes a few minutes. If the connection drops, click it again and it resumes where it stopped.

**3. Add an AI key (optional, recommended)**: see the next section. The app ships with no AI key, so right after installing it uses **不用 AI（只用规则）** (No AI, rules only): free and fastest, but it can't understand complex requests and the agent is unavailable. The top of the page shows what is currently in use.

## Adding an AI key (optional)

**Zhipu GLM** is recommended: it is reachable from mainland China, and GLM-4.7-Flash is free.

1. Open the [Zhipu Open Platform](https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys), sign up and log in, click "添加新的 API Key" (Add API key), and copy the generated key
2. Click **设置** (Settings) in the top right of the app, choose **智谱 GLM** as the provider, keep the default model `glm-4.7-flash`, paste your key, and click **保存并测试连接** (Save and test connection). When you see "✓ 可以用" (works), you're done

"哪里有免费的 Key？" (Where to get a free key?) in Settings also lists where to sign up. You can use the other providers too (each provider's key is saved separately, so switching doesn't lose them):

| Provider | Reachable from mainland China | Cost (as of 2026-10; check the official site) |
|---|---|---|
| Zhipu GLM | Yes | GLM-4.7-Flash is free (with concurrency limits) |
| SiliconFlow | Yes | Some small models are free |
| Qwen (Alibaba Cloud Bailian) | Yes | Free quota for new users, then paid |
| DeepSeek | Yes | Paid, very cheap |
| Kimi (Moonshot AI) | Yes | Paid |
| Google Gemini | No | Free quota |
| Custom | — | Any OpenAI-compatible API, e.g. a local Ollama |

Keys are stored only on your computer, and the page shows only the last 4 characters. The app never reads keys from elsewhere on your computer (environment variables, `.env` files). To go back to no AI, choose **不用 AI（只用规则）** (No AI) as the provider in Settings; your keys are kept. Free models have rate limits: if you ask too quickly, the agent temporarily falls back to a normal search. Wait a moment and ask again.

## Everyday use

- **Updating data**: the top of the page shows how long ago the local data was updated. Click **立即更新** (Update now) under **设置 → 数据** (Settings → Data) to download and import new data in the background; you can keep searching meanwhile
- **Galgame**: turn on the switch under **设置 → 成人内容** (Settings → Adult content) and confirm you are 18 or older; the Galgame tab then appears at the top
- **Agent**: in Settings you can turn the agent off (faster, uses less quota) or send every request to the agent
- **Appearance**: under **设置 → 外观** (Settings → Appearance) you can switch dark / light theme and accent color, choose your own background image (PNG / JPEG / WebP, up to 10 MB, stored only on your computer), and adjust background blur, dimming, panel opacity and font size. Changes preview live and take effect when you click Save
- **Where data and settings live**: `~/Library/Application Support/如月寮` on macOS, `%APPDATA%\如月寮` on Windows. To uninstall, delete the app and this folder

## FAQ

- **Download fails while preparing data**: check your connection and click **开始准备数据** (Prepare data) again. Parts already downloaded won't be downloaded again. From mainland China, downloads from GitHub are sometimes slow; try again later
- **Not enough disk space**: about 2.5 GB of free space is needed
- **The agent says it couldn't finish**: usually the free model's rate limit. Wait a moment and ask again, or click "Save and test connection" in Settings to check your key and model
- **Connection test times out**: Gemini is not available in mainland China. Switch to one of the Chinese providers
- **The window shows an error or won't open**: `logs/desktop.log` in the data folder records the cause; please attach it when reporting a problem

## Data sources and attribution

This project does not distribute any data with the app. The app downloads data from the official sources below to the user's own computer and imports it locally.

| Source | Used for | License |
|---|---|---|
| [Bangumi](https://bgm.tv/) ([Bangumi Archive](https://github.com/bangumi/archive)) | Anime entries, tags, synopses, covers (loaded on demand from the Bangumi API); some Chinese galgame titles; anime–game adaptation relations | Entry information under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), copyright belongs to the creators; use follows the [Bangumi developer terms](https://bangumi.tv/about/copyright) |
| [bangumi-data](https://github.com/bangumi-data/bangumi-data) | Official streaming platforms the anime has been released on (onair entries only); anime ID mapping | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [bangumi/common](https://github.com/bangumi/common) | Code tables for relation types, staff roles, etc. | Not stated |
| [VNDB](https://vndb.org/) ([database dumps](https://vndb.org/d14)) | Galgame entries, tag votes, covers, ErogameScape links, related anime | Database [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), contents DbCL 1.0; descriptions come from various sources; non-commercial use only |

For ErogameScape (エロゲー批評空間) the app only provides links; see ErogameScape itself for scores and reviews.

**Privacy**: the sentences you type are sent to the AI provider you chose in Settings so it can understand them (when a key is set). Cover images are loaded directly from Bangumi and VNDB.

## Open-source libraries used

FastAPI, Uvicorn, Pydantic, Pydantic AI (MIT), OpenAI Python SDK (Apache 2.0, used to connect to the providers' OpenAI-compatible APIs), PyYAML (MIT), opencc-python-reimplemented (Apache 2.0), networkx (BSD-3-Clause), zstandard (BSD-3-Clause), certifi (MPL 2.0), pywebview (BSD-3-Clause), platformdirs (MIT), PyObjC (MIT, macOS only). The app is built with PyInstaller (GPL-2.0 with an exception that allows bundling any software).

## License

The code is released under the [MIT License](LICENSE). Data is not included in this repository; each source follows its own license listed above.
