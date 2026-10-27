# 如月寮（ACG Agent）

**中文** | [English](README.en.md)

用一句中文描述你想看的动画或想玩的 galgame，返回符合条件的作品，每个条件都附上判断依据（满足 / 很可能满足 / 未知 / 冲突）。

- **动画**：数据来自 Bangumi 番组计划，附封面、简介、Bangumi 条目链接，以及（有记录时）曾上线的正版平台
- **Galgame**：数据来自 VNDB，附封面、VNDB 和批評空間链接；需要在设置里确认已满 18 岁后才能使用
- **代理人**：“像 CLANNAD 那样的”“主角之间互相救赎”这类标签说不清的请求，交给 AI 查资料再推荐；动画和 galgame 各有一个
- **互相关联**：galgame 卡片显示改编动画，动画卡片显示相关游戏
- **外观**：深色 / 浅色、主题色、自定义背景图

整个程序在你自己的电脑上运行：数据从官方来源下载到你的电脑，不经过任何第三方服务器。

## 下载

在 [Releases](https://github.com/haoran-ou/acgn-agent-p/releases/latest) 里下载：

| 系统 | 下载 | 怎么打开 |
|---|---|---|
| Windows 10 / 11 | `Kisaragi-Windows.exe` | 双击打开，不用安装（**还没有在 Windows 上实测过**） |
| macOS（Apple 芯片，M1 及以后） | `Kisaragi-macOS.zip` | 双击解压得到“如月寮”，拖进“应用程序”文件夹 |

需要约 3 GB 硬盘空间，第一次使用要联网下载约 650 MB 的数据。

## 第一次打开

**1. 系统警告**：程序没有做代码签名（签名每年要付费），系统会提示“无法验证开发者”。这是正常的：

- **macOS**：在“应用程序”里**右键**点“如月寮” → **打开** → 在弹出的对话框里再点 **打开**。只有第一次需要这样做。如果没有“打开”按钮：打开“系统设置 → 隐私与安全性”，在最下面点“仍要打开”
- **Windows**：出现“Windows 已保护你的电脑”时，点 **更多信息** → **仍要运行**。如果窗口打不开（较老的 Windows），先安装微软的 [WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)

**2. 准备数据**：点窗口里的 **开始准备数据**，程序会从 Bangumi、bangumi-data、VNDB 的官方来源下载数据、校验并导入，需要几分钟。中途断网也没关系，再点一次会接着下载。

**3. 填 AI 的 Key（可选，推荐）**：见下一节。程序本身不带任何 AI 的 Key，刚装好时是 **不用 AI（只用规则）**：免费、最快，但理解不了复杂的说法，代理人不可用。页面上方会写明“正在使用：…”。

## 填 AI 的 Key（可选）

推荐 **智谱 GLM**：国内直连，GLM-4.7-Flash 免费。

1. 打开 [智谱开放平台](https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys)，注册登录后点“添加新的 API Key”，复制生成的 Key
2. 在如月寮右上角点 **设置**，“服务商”选 **智谱 GLM**，型号用默认的 `glm-4.7-flash`，把 Key 粘贴进去，点 **保存并测试连接**，看到“✓ 可以用”就好了

设置页里的“哪里有免费的 Key？”也列出了申请地址。也可以选其他几家（每家的 Key 分开保存，切换不会丢）：

| 服务商 | 国内直连 | 费用（2026-10 查到的，以官网为准） |
|---|---|---|
| 智谱 GLM | 能 | GLM-4.7-Flash 免费（有并发限制） |
| 硅基流动 | 能 | 有免费的小模型 |
| 通义千问（阿里云百炼） | 能 | 新用户有免费额度，之后付费 |
| DeepSeek | 能 | 付费，很便宜 |
| Kimi（月之暗面） | 能 | 付费 |
| Google Gemini | 不能 | 有免费额度 |
| 自定义 | — | 任意 OpenAI 兼容接口，例如本机的 Ollama |

Key 只保存在你电脑上，网页上只显示末 4 位；程序不会读取你电脑上别处的 Key（环境变量、`.env` 文件）。想回到不用 AI，在设置里把服务商选成 **不用 AI（只用规则）** 即可，各家的 Key 会保留。免费模型有调用频率限制；连续问得太快时，代理人会暂时退回普通搜索，稍等再问就好。

## 日常使用

- **更新数据**：页面顶部显示“本机更新于 X 天前”。在 **设置 → 数据** 里点 **立即更新**，会在后台下载并导入新数据，期间照常可以搜索
- **Galgame**：在 **设置 → 成人内容** 里打开开关并确认已满 18 岁，页面上方才会出现 Galgame 分区
- **代理人**：设置里可以关掉代理人（更快、更省额度），或者让所有请求都交给代理人
- **外观**：在 **设置 → 外观** 里可以换深色 / 浅色、主题色，选一张自己的背景图（PNG / JPEG / WebP，10 MB 以内，只保存在你的电脑上），调背景模糊、压暗、面板透明度和字号；调整时实时预览，点“保存”生效
- **数据和设置放在哪**：macOS 是 `~/Library/Application Support/如月寮`，Windows 是 `%APPDATA%\如月寮`。卸载时删掉程序和这个文件夹即可

## 常见问题

- **准备数据时下载失败**：检查网络后再点一次 **开始准备数据**，已经下好的部分不会重下。国内网络下载 GitHub 上的数据包有时很慢，可以换个时间再试
- **提示硬盘空间不够**：需要约 2.5 GB 空闲空间
- **代理人说“没能完成”**：多半是免费模型的调用频率限制，稍等再问；也可以在设置里点“保存并测试连接”看看 Key 和型号对不对
- **测试连接提示超时**：在中国大陆用不了 Gemini，换成国内的服务商
- **窗口出错或打不开**：数据文件夹里的 `logs/desktop.log` 记着原因，反馈问题时请附上

## 数据来源与署名

本项目不随程序发布任何数据：数据由程序从下面的官方来源下载到使用者自己的电脑，在本机导入。

| 来源 | 用途 | 许可 |
|---|---|---|
| [Bangumi 番组计划](https://bgm.tv/)（[Bangumi Archive](https://github.com/bangumi/archive)） | 动画条目、标签、简介、封面（按需从 Bangumi 接口加载）；部分 galgame 中文名；动画与游戏的改编关系 | 条目信息遵循 [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/deed.zh)，版权归创作者；使用遵守 [Bangumi 开发者平台使用协议](https://bangumi.tv/about/copyright) |
| [bangumi-data](https://github.com/bangumi-data/bangumi-data) | 动画曾上线的正版放送平台（只用 onair 类）；动画编号对照 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [bangumi/common](https://github.com/bangumi/common) | 关联类型、职位等代码对照表 | 未声明 |
| [VNDB](https://vndb.org/)（[数据包](https://vndb.org/d14)） | galgame 作品、标签投票、封面、批評空間链接、对应动画 | 数据库 [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)，内容 DbCL 1.0；作品简介另有来源；仅限非商业使用 |

批評空間（エロゲー批評空間）只提供页面链接，评分和评论请到批評空間查看。

**隐私**：你输入的句子会发给你在设置里选的 AI 服务商用来理解（填了 Key 时）；封面图片直接从 Bangumi、VNDB 加载。

## 用到的开源库

FastAPI、Uvicorn、Pydantic、Pydantic AI（MIT）、OpenAI Python SDK（Apache 2.0，用来连接国内各家的兼容接口）、PyYAML（MIT）、opencc-python-reimplemented（Apache 2.0）、networkx（BSD-3-Clause）、zstandard（BSD-3-Clause）、certifi（MPL 2.0）、pywebview（BSD-3-Clause）、platformdirs（MIT）、PyObjC（MIT，仅 macOS）；程序由 PyInstaller 打包（GPL-2.0，附允许打包任何软件的例外条款）。

## 许可证

代码使用 [MIT 许可证](LICENSE)。数据不在本仓库里，各自遵守上表中的许可。
