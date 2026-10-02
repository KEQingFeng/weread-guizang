**中文** | [English](README.en.md) | [日本語](README.ja.md)

# 归藏 · weread-guizang

把微信读书的阅读数据导出到本地，也把外部内容（公众号文章、知乎 / 小红书 / X 的帖子、RSS 订阅、视频）收进同一个本地书架：整本导出为 Markdown、本地多格式阅读器、书架与笔记管理，以及面向 AI Agent 的 MCP 适配器。全部在本机运行，服务只监听 `127.0.0.1`。

## 问题

微信读书把阅读数据封装成一套只服务 AI Agent 的接口（Agent Gateway）：以 `skill_version` 和一组 `api_name` 提供书架、书城搜索、书籍详情、划线、想法、阅读统计、推荐等数据，用形如 `wrk-…` 的 Key 鉴权。它有两个边界：

- **只读**：白名单里没有写接口，无法加书或修改书架。
- **只服务 Agent**：没有面向人的界面。

用户因此拿不到整本书的正文，也无法在不写代码的情况下使用这批数据。

## 方案

归藏把这套接口还原成可用的本地工具，并补齐它没有的部分：

- 官方 Gateway 提供的数据，做成本地网页界面：书架、书城搜索、书籍详情、阅读统计、推荐、全部划线与想法。
- Gateway 不提供的功能——正文导出、图片下载、把书加入书架——复用登录态直接调用网页端接口。
- 面向 Agent，除界面外另提供一套零依赖的 MCP 适配器（32 个工具）。

三项能力都在本机完成，服务只监听 `127.0.0.1`，不经第三方服务器。

## 功能

- **整本导出为 Markdown**：文字与插图按阅读顺序交错，插图 8 线程并发下载，逐章落盘，支持断点续传。
- **本地多格式阅读器**：取回的书直接在界面内阅读，左右分栏、目录随滚动高亮、键盘翻章；本章还有「大纲」（O 键）列出这一章的小标题，点一条就滚过去；重开一本书回到上次停下的那一行，不只是回到那一章；导入的 EPUB / TXT / PDF 以同一套交互阅读。
- **阅读中划句问 AI**：在正文里选中一段，就地弹出「复制 / 译成中文 / 问小助手」，可直接把这句话交给阅读器内的 Agent 追问。需在设置里自行填写 AI 模型的 API。
- **外文阅读优化**：长按选中任意段落即可译成中文或复制原文；英文正文里单击一个单词，直接弹窗给出释义与用法（仅英文支持单击取词，其他语言用长选）。
- **阅读时长双源记录**：分别记录微信读书与本机（归藏）的阅读时长，并自动合并为一个统一时长；两份账目分列展示，看得出合并从哪两段拼出来。
- **独立的本地书架**：与微信读书书架分开显示，集中列出已取回与自行导入的书；可一键定位到文件所在位置，也可导入自己的 Markdown / TXT / EPUB / PDF。
- **剪藏文章**：粘贴微信公众号推文链接即可解析正文、加入书架并阅读；解析结果走取书那套分章与目录，因此导出 EPUB / PDF、定位文件对剪藏的文章同样可用，读完还能点回原链接看配图版式。公众号之外，**知乎 / 小红书 / X（推特）**也有各自的专门解析：X 的推文能直接取到正文，知乎和小红书要先对方放行——未登录时它们常常只回「安全验证」页，这时如实说拿不到，不会把验证页当成正文存进书架。
- **RSS 订阅**：粘一个网址或订阅地址即可（只给站点首页也行，会自己从 `<link rel="alternate">` 里找订阅源）。订阅列表里能刷新、能逐条读，单条可一键入本地书架——全文够长的直接入库，只有摘要的才回源抓正文。同一条不重复入库。
- **视频转笔记**：贴 B 站 / YouTube 链接，先用 yt-dlp 取音频、用本地 Whisper（mlx-whisper / faster-whisper）或云端接口转成文字，再交给 AI 归纳成笔记与思维导图，最后当成一本书落进本地书架，之后与取回的书一样读、一样记。多 P 视频只转你点的那一 P，界面上写清是哪一 P。音频只落在临时目录，转完即清。
- **边读边记**：正文里选中一段就能高亮、加粗、划线、删除线或写批注；右栏笔记与正文互相跳转，读到哪条一点就回到原句。侧栏与目录都能收起，收起后只留正文。
- **两类笔记分开**：随手划的高亮配一句话想法（轻量、成批）与独立笔记条目（可以很长、可以引用书里别处的句子）分开存放。划词即记：选中后在浮层里直接写想法，保存时自动带上原文与位置。
- **高亮分色就是语义标签**：论点 / 疑问 / 可引用 / 待查 / 灵感各一色，颜色标的是「这句我打算怎么用它」。
- **模板、导图与导出**：可用官方读书笔记模板起手，也能把自己写的一篇存成模板；整篇笔记按大纲画成一张思维导图（SVG），并可导出 Markdown。笔记就写在这本书自己的目录里（notes.json / notes.md / mindmap.svg），不进数据库，拷走书等于拷走笔记。
- **书架两种摆法**：瀑布（羊皮卷逐张展开）与叠卡（放大成一本书，左右像折叠屏那样张合切换，方向键直接翻）。鼠标掠过卡片只有动效反馈，再点一下才出现「取书 / 查看详情」。
- **导出 EPUB / PDF**：把取回的书导成通用电子书格式，EPUB 保留插图与章节结构。
- **WebDAV / OneDrive 云同步**：把阅读时长、阅读记录、书籍文件同步到自己的网盘（坚果云、Nextcloud、OneDrive 均可），三项可分别勾选；凭据只留在本机。
- **界面内 AI Agent 助手**：右下角气泡随时呼出；选中正文即问，不必复制粘贴。
- **不打开微信读书即可查看数据**：书城搜索、书籍简介、作者与出版社、分类、阅读进度与最近阅读时间，都在本地显示。
- **搜索即抓取**：搜索结果直接转为导出任务；全部划线建索引后可全文检索。
- **划线回顾与 Anki 导出**：随机从全部划线中抽卡回顾；划线导出为 `.apkg`。
- **MCP 接入**：32 个工具，取书为长任务、不阻塞调用；服务未启动时适配器自行拉起。
- **划线迁移到 flomo**：多选划线批量转发，原文以「」包裹，附书名与标签。
- **配套 Skills**：仓库内 [`skills/`](skills/) 可直接安装；界面提供「一键建立 MCP」，把接入提示词复制给 Agent。

## 环境要求

- Python 3.10+（使用打包好的 macOS 程序时为 3.9+）
- Node（仅 MCP 适配器需要，`node -v` 能输出版本号即可）
- **微信读书账号（可选）**：只有取微信读书的书、看划线笔记与统计时才需要，且要对目标书有阅读权限（无限卡或已购买）。剪藏、RSS 订阅、视频转笔记这三条不依赖它。
- **ffmpeg（仅视频转笔记需要）**：不必预先装好。界面上点「装 ffmpeg」会下载一份静态版放进归藏自己的数据目录，不动系统；系统里已有 ffmpeg 时直接复用。
- **转写引擎（仅视频转笔记需要，本地与云端任选）**：本地用 mlx-whisper（Apple 芯片，最快）或 faster-whisper（其他平台通用），也可在设置里填一个兼容 OpenAI 的转写接口走云端。本地引擎不含在默认依赖里，点「视频转笔记」时若没装，界面会写清该装哪一个。

## 安装

### 方式一：下载 macOS 程序

[**下载 归藏-0.9.9.dmg**](安装包/归藏-0.9.9.dmg)（约 2 MB，要求 macOS 13 以上，支持 Intel 与 Apple 芯片）

1. 双击 dmg，把 **归藏.app** 拖入「应用程序」。
2. 首次打开：若提示 **「归藏」已损坏，无法打开。你应该将它移到废纸篓。**，这是 macOS 对未签名应用的默认拦截，并非文件损坏。执行：

   ```bash
   xattr -dr com.apple.quarantine /Applications/归藏.app
   ```

   装在别的位置请替换路径；提示 `Operation not permitted` 时在命令前加 `sudo`。也可以在 **系统设置 → 隐私与安全性 → 安全性** 中找到被拦截的条目，点「仍要打开」。

3. 首次进入为配置页，点「我思故我在」开始初始化：创建虚拟环境、安装依赖、下载 Chromium（约 370 MB，需能访问外网；使用代理时请在代理软件中开启「系统代理」，程序会自动继承）。随后弹出浏览器完成扫码登录。此后每次打开直接进入界面。

   机器需已安装 Python 3.9+，可从 <https://www.python.org/downloads/> 安装；安装后无需重启。包内另附《首次打开必读.txt》。细节与替代做法见 [部署说明.md](docs/部署说明.md)。

### 方式二：从源码运行

#### macOS / Linux

```bash
git clone https://github.com/KEQingFeng/weread-guizang.git
cd weread-guizang
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # 约 368 MB
.venv/bin/pip install mlx-whisper                  # 可选：视频转笔记的本地转写引擎，Apple 芯片用这个
.venv/bin/python ui_server.py --port 8770
```

其他平台把 `mlx-whisper` 换成 `faster-whisper`（mlx 只在 Apple 芯片上跑）。两者都不想装也行，在设置的 AI 助手一栏把转写接口填上即可走云端。ffmpeg 不用先装：界面上点「装 ffmpeg」会自己下一份静态版。

也可双击 `启动归藏.command`：自动识别目录，缺虚拟环境则创建并安装依赖，服务已在运行时直接打开界面。

构建为可双击运行的 macOS 程序：

```bash
./shell/build_macos.sh          # 产出 dist/归藏.app
```

将 `dist/归藏.app` 拖入「应用程序」即可。外壳为 Swift + WKWebView 原生应用，界面仍使用 `ui.html`，未作改动。首次打开为配置页，初始化与扫码流程同上；之后每次打开直接进入。构建不需要完整 Xcode，安装 CommandLineTools 即可。

程序运行所需文件（虚拟环境、缓存、登录态）存放在 `~/Library/Application Support/归藏/`，应用包本身保持只读、可任意移动。取回的书与自行导入的书统一存放在 `~/Documents/归藏/`（安装后自动创建），可在「文档」中直接访问。

打包为分发给他人使用的 dmg：

```bash
./shell/make_dmg.sh             # 产出 ~/Desktop/归藏-<版本>.dmg
```

#### Windows

```bat
git clone https://github.com/KEQingFeng/weread-guizang.git
cd weread-guizang
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m pip install faster-whisper   :: 可选：视频转笔记的本地转写引擎
.venv\Scripts\python.exe ui_server.py --port 8770
```

也可双击 `启动归藏.bat`，等同于依次执行上述四步。

启动后打开 <http://127.0.0.1:8770>。

### 首次使用

1. 左上角齿轮 → **连接账号** → 在弹出的浏览器窗口中使用微信扫码。会话持久化于 `cache/browser_profile/`，之后自动复用。
2. 填写 **接口 Key**：微信读书网页版「设置 → 开放 API」，复制形如 `wrk-…` 的 Key 并保存。Key 存放在本机 `cache/config.json`（权限 600），界面仅回显末 4 位。书架、笔记、统计、推荐、书城搜索、书籍详情均依赖它；未填写时仅列出本地已导出的书。
3. 若需将划线导入 flomo，填写 **flomo** 栏（flomo 网页版 → 设置 → API，形如 `https://flomoapp.com/iwh/xxxx/`），需要 flomo PRO。
4. 若要使用**阅读划句翻译 / 单击查词 / 问 AI 助手**，在设置里填 **AI 助手** 一栏：一个兼容 OpenAI 的接口（地址写到 `/v1` 为止）、Key 与模型名。地址与 Key 只存在本机，界面仅回显是否已填；问了才会把选中的内容发给该接口。

## 命令行

以下命令可在不启动界面的情况下使用。

```bash
# macOS / Linux
.venv/bin/python export_precise.py <书籍链接或ID>          # 链接会自动提取 ID
.venv/bin/python export_precise.py <ID> --headed          # 显示翻页过程
EXPORT_DEBUG=1 .venv/bin/python export_precise.py <ID>    # 卡住时每 60 秒输出 Python 栈

# Windows
.venv\Scripts\python.exe export_precise.py <书籍链接或ID>
.venv\Scripts\python.exe export_precise.py <ID> --headed
set EXPORT_DEBUG=1 && .venv\Scripts\python.exe export_precise.py <ID>
```

登录与加书架也可单独运行：`login.py`（登录与登录态检测）、`shelf_add.py <书城 id>`。

剪藏、订阅、视频这几条也各有可以直接跑的命令，用于排查而不必开界面（macOS / Linux 用 `.venv/bin/python`，Windows 换成 `.venv\Scripts\python.exe`）：

```bash
.venv/bin/python clip_article.py <文章链接>          # 看这一篇能解析出什么（公众号 / 知乎 / 小红书 / X 自动选路）
.venv/bin/python web_parse.py <链接>                 # 看这条链接被归到哪个站，以及正文前 1200 字
.venv/bin/python feed.py <站点或订阅地址>             # 从任意网址里找出订阅源
.venv/bin/python feed.py --list                     # 现有订阅与各条目的抓取状态
.venv/bin/python feed.py --refresh                  # 刷新全部订阅
.venv/bin/python video_note.py <视频链接> [输出目录]   # 打印视频信息，再整条跑一遍转笔记
.venv/bin/python video_note.py --task <链接>         # 界面用的那条路：选项走 GUIZANG_VIDEO_OPTS、结果打 ##GUIZANG## 一行
.venv/bin/python ffmpeg_tool.py --status            # 看 ffmpeg 备好没有
.venv/bin/python ffmpeg_tool.py --ensure            # 下一份静态 ffmpeg 放进数据目录
```

## 接入 AI Agent（MCP）

仓库自带零依赖的 Node 适配器（`mcp/guizang-mcp.mjs`，stdio + JSON-RPC 2.0）。在 MCP 配置中加入一项，路径替换为实际的归藏目录：

```json
"guizang": {
  "type": "stdio",
  "command": "node",
  "args": ["<归藏目录>/mcp/guizang-mcp.mjs"],
  "timeout": 180000,
  "env": { "GUIZANG_REPO": "<归藏目录>" }
}
```

Qoder CN 写在设置文件的 `mcpServers`；ZCode 写在 `config.json` 的 `mcp.servers`。配置完成后重启 Agent（MCP 在启动时加载，不支持热加载）。

适配器按以下顺序查找项目目录：环境变量 `GUIZANG_REPO` → 依据「本文件位于项目 `mcp/` 下」推断 → 常见路径回退。解释器按平台选择（Windows 用 `.venv\Scripts\python.exe`，macOS / Linux 用 `.venv/bin/python`），均不存在时回退至系统 `python`。服务未启动时自行拉起。

共 **32 个工具**，分为七类：

| 分类 | 工具 |
| --- | --- |
| 书架与状态 | `shelf_list`、`app_status`、`task_log`、`book_files`、`folder_create`、`book_move` |
| 取书 | `book_fetch`、`task_stop`、`batch_fetch`、`account_connect` |
| 书与笔记 | `book_detail`、`search_books`、`notes_index`、`notes_search`、`notes_random`、`book_mark`、`shelf_add` |
| 剪藏 | `clip_url` |
| 订阅 | `feed_list`、`feed_discover`、`feed_add`、`feed_entries`、`feed_entry`、`feed_refresh`、`feed_to_shelf`、`feed_remove` |
| 视频 | `video_capability`、`video_plan`、`video_to_shelf` |
| 导出 | `apkg_export`、`zip_export`、`cache_delete` |

三条约束写入适配器的工具说明，Agent 可读取：

- 取书与视频转笔记都是分钟到小时级的长任务（视频还要先下音频、再转写），`book_fetch` / `batch_fetch` / `video_to_shelf` 会立即返回，进度通过 `app_status` / `task_log` 轮询。不要等待其执行完毕，否则必然超时。
- `shelf_add` 是唯一会改动**真实微信读书书架**的写操作；`cache_delete` 默认只列出，需带 `confirm=true` 才真正删除。`feed_add` / `feed_remove` / `feed_refresh` / `feed_to_shelf` 只动本机的订阅与本地书架。
- 视频这条路要先具备工具链：`video_capability` 报现在还缺什么（yt-dlp / ffmpeg / 本地转写引擎），缺了就先补齐再发起，不要盲发。

## 配套 Skills

[`skills/`](skills/) 目录包含用于读书与学习的 Skill，将对应文件夹复制到本机 skills 目录即可（Qoder CN 为 `~/.qoder-cn/skills/`，其他平台使用各自的目录）：

| Skill | 用途 |
| --- | --- |
| [`skills/guizang/`](skills/guizang/) | 将归藏接入为读书助手：先查看状态再执行，长任务仅发起一次并轮询，写操作仅执行明确要求的那一个；包含找书→取书→划线检索→抽卡→导出 Anki 的完整动作序列与排障口径 |
| [`skills/book-speedrun/`](skills/book-speedrun/) | 将一本书一次讲透：前导地图 → 核心讲义 → 全书串讲 → 一页速记与分层行动清单，每部分末尾附一条可立即执行的动手项。含完整示例 |

分工判据：需要**内容**（把书讲透、读完即用）使用 `book-speedrun`；需要**数据**（把书取到本地、检索划线、导出 Anki）使用 `guizang`。两者可衔接：先用归藏取书，再用 `book-speedrun` 讲透。

> `book-speedrun` 中提到的 `grace-coach`、`learn-from-materials`、`learn-anything-skill` 属于同一生态的其他 Skill，**不在本仓库**，仅用于划分职责。

界面中的「**一键建立 MCP**」会把接入提示词复制到剪贴板，粘贴给 Agent 即可。该提示词不含任何本机路径、用户名或端口，可直接分享。

## 产物结构

书统一存放在 `~/Documents/归藏/` 下，结构一致，因此阅读器、EPUB/PDF 导出、定位文件、笔记等功能对它们一视同仁。命名前缀标明来源：

| 来源 | 目录名 | `meta.json` 的 `source` |
| --- | --- | --- |
| 微信读书取回 | `<书籍 id>` | 无（按书籍 id 命名） |
| 自行导入 | `imp_<名字片段>_<随机串>` | `local` |
| 剪藏文章 | `clip_<名字片段>_<随机串>` | `clip` |
| RSS 订阅 | `feed_<名字片段>_<随机串>` | `feed` |
| 视频转笔记 | `video_<名字片段>_<随机串>` | `video` |

```
~/Documents/归藏/
├── <书id>/                   # 微信读书取回的书
│   ├── chapters/NNNN.md      # 逐章正文，图文交错
│   ├── images/               # 插图 chXXXX_imgNN.jpg
│   ├── raw/NNNN.json         # 每章的图片 URL 与字数
│   ├── _catalog.json         # 目录章节标题（用于判定全书末尾）
│   ├── _progress.json        # 抓取进度（界面进度条的数据源）
│   └── meta.json             # 书名/作者/是否导完
├── imp_<名>_<串>/            # 自行导入的书（MD / TXT / EPUB / PDF），结构同上
├── clip_<名>_<串>/           # 剪藏的文章，meta 里多 url（一键回原文）
├── feed_<名>_<串>/           # 订阅条目，meta 里多 feed_id / feed_title / date
├── video_<名>_<串>/          # 视频转笔记，meta 里多 url / asr_engine / duration
│   └── notes.json / notes.md / mindmap.svg   # 笔记与导图，随书同目录
├── 书名.md                   # 合并稿（图片为相对路径，单独拷走会断图）
└── 书名.apkg                 # Anki 卡包（划线导出）
```

除微信读书取回的书之外，其余四种的 `meta.json` 都带 `format` 字段（`local` / `clip` / `feed` / `video`），界面据此在书架里分组显示来源。

合并稿中的图片是相对路径 `images/…`，单独拷走会导致断图；界面中的「完整包」ZIP 已将正文与图片平铺。

用 Typora / Obsidian 打开合并稿 `.md` 即可阅读图文完整的书。

## 工作原理

```mermaid
flowchart LR
  A[登录 profile<br/>cache/browser_profile] --> B[export_precise.py]
  B --> C[Playwright 驱动 Chromium<br/>打开网页版阅读器]
  C --> D[hook fillText<br/>收集每个字符的坐标]
  C --> E[取视口内的 img<br/>过滤预加载的下一页]
  D --> F[按 y 自适应聚类成行<br/>检测 y 重置点拆双页]
  E --> G[文字行与图片<br/>按 y 坐标排序交错]
  F --> G
  G --> H[按正文里出现的目录标题分章]
  H --> I[chapters/NNNN.md]
  E --> J[download_images.py<br/>强制 IPv4 · 8 线程并发]
  J --> K[images/]
```

```mermaid
flowchart TB
  UI[ui.html<br/>单文件前端 · 无构建步骤]
  S[ui_server.py<br/>标准库后端 · 只监听 127.0.0.1]
  E[export_precise.py<br/>抓取引擎 · 子进程]
  SA[shelf_add.py<br/>加书架 · 子进程]
  CL[clip_article.py + web_parse.py<br/>剪藏：公众号 · 知乎 · 小红书 · X]
  FS[feed.py<br/>RSS 发现 · 抓取 · 入库]
  VN[video_note.py<br/>视频转笔记 · 子进程]
  FF[ffmpeg_tool.py<br/>静态 ffmpeg 按需下载]
  YT[yt-dlp<br/>取音频 · 元信息]
  WG[微信读书官方 Agent Gateway<br/>wrk- Key · 16 个 api_name · 只读]
  WP[微信读书网页端 /mp/<br/>复用登录 cookie · 唯一的写路径]
  FL[flomo]
  PC[platform_compat.py<br/>解释器 / 建组 / 中止 / 结束进程树]
  M[mcp/guizang-mcp.mjs<br/>stdio JSON-RPC · 32 工具]
  AG[AI Agent]

  UI <-->|JSON| S
  S --> E
  S --> SA
  S --> CL
  S --> FS
  S --> VN
  S -->|HTTPS| WG
  SA -->|HTTPS| WP
  S -->|HTTPS| FL
  S --- PC
  E --- PC
  SA --- PC
  VN --- PC
  VN --> YT
  VN --> FF
  M <-->|HTTP| S
  AG <--> M
```

抓取引擎中几个关键决定及其原因：

- **分章依据为「正文中实际绘制的目录标题」**，不使用顶栏标题。顶栏每翻一页都会变化，早期版本因此将整批内容归到同一标题下，其余小节只剩空壳；目录标题每个只出现一次，天然去重。
- **翻页只使用方向键，不点击正文中心**。点击会触发微信读书的「回到上次阅读位置」，而从目录跳到开头不会更新阅读记录，导致「点击 → 等待 → 跳回开头」无法收敛，表现为开头数章整片丢失。
- **每轮翻页设硬超时**。Playwright 的 `page.evaluate` 默认无超时，渲染进程卡死时调用永久挂起；`asyncio.wait_for` 对不响应取消的调用无效，因此使用 `asyncio.wait` 取得超时后直接返回，再终止浏览器回收连接。
- **图片下载强制 IPv4**。macOS 上 urllib 默认先尝试 IPv6，路由不通时每张图约卡 120 秒。
- **订阅源判定只认「根元素就是 feed」**。博客页脚常夹一块创作共用的 `<rdf:RDF>` 授权声明（还裹在 HTML 注释里），见着 `<rdf` 就当 RSS，会把整页 HTML 认成订阅源，反而把页面里 `<link alternate>` 指着的真源挡在后面用不上。判据改为：feed 的根元素之前不先出现 `<!doctype` / `<html`。
- **知乎 / 小红书 / X 各走各的取法，不进通用解析器**。X 的正文根本不在页面里（得问 syndication 那个公开接口），小红书把正文塞进 `<script>` 里的一坨 `window.__INITIAL_STATE__`，知乎对未登录读者时而给正文、时而给验证页。这些差异属于「同一个站一种取法」，塞进通用解析器会改坏所有站点的剪藏，因此独立成模块；哪一家改版失效，只动那一段，并在注释里写清现状，不留一个看着正常、其实永远抛错的空壳。
- **视频只转用户点的那一 P**。多 P 视频整条转可能几小时，而用户要的多半只是其中一集；认链接时先把分 P 列出来，只把选中那一 P 交给 yt-dlp。音频只在临时目录里存在，转完即删——用户要的是那本书，不是那段音轨。
- **转写引擎点名就点名**。用户指定 mlx 而机器上没装时直接说清原因，不偷偷降级成 faster——「我要用大模型」被静默换成小模型，比报错更让人火大。

## 仓库结构

```
仓库根      运行期代码：后端 ui_server.py、界面 ui.html、引擎与各功能模块
            （取书 export_precise.py、剪藏 clip_article.py / web_parse.py、
              订阅 feed.py、视频 video_note.py / ffmpeg_tool.py）
            —— 这一层刻意不分包：任务子进程以数据目录为工作目录，靠
               dirname(__file__) 找同伴模块，移动即断
tests/      验证门禁。一条命令跑全套：bash tests/run_all.sh（--fast 只跑静态检查）
tools/      只在开发机上跑：出图标、打源码包、窗口截图
docs/       四份文档：部署说明、架构与模块地图、开发规范、交接说明
shell/      macOS 原生壳与打包脚本：Swift 外壳 → dist/归藏.app → 安装包/*.dmg
mcp/        MCP stdio 适配器：把本地接口包成 32 个工具给 Agent 用
skills/     给 Agent 看的说明书（不含可执行逻辑）
vendor/     随包的三方前端库（markdown-it）及其 LICENSE，界面不连 CDN
安装包/     只保留最新那一个 dmg
```

| 文档 | 什么时候看 |
| --- | --- |
| [部署说明.md](docs/部署说明.md) | 装、跑、排障、接 MCP |
| [架构与模块地图.md](docs/架构与模块地图.md) | 动手改代码之前：谁调谁、数据落在哪、两套书籍 id、加一个功能的最短路径 |
| [开发规范.md](docs/开发规范.md) | 提改动之前：目录与路径契约、哪些事实只准写一处、界面零 emoji、隐私红线、验证门禁与发版流程 |
| [交接说明.md](docs/交接说明.md) | 接手这个项目时：0.9.9 这一轮加了什么、每个新模块的边界在哪、还差哪些没验过 |

## 更新记录

**0.9.9**

- 外部内容进同一个书架。这一轮打开三条取内容的通路，都落到 `~/Documents/归藏/` 下同一个书库，之后与取回的书一样读、一样记、一样导出：
  - **剪藏扩到知乎 / 小红书 / X**。公众号照旧；知乎、小红书、X 各有专门解析（X 走 syndication 公开接口，小红书读页面里那坨 `window.__INITIAL_STATE__`，知乎按未登录能读到的范围取）。这三家未登录时常常只回「安全验证」页，认出来就如实说拿不到，不把验证页当正文存进书架。
  - **RSS 订阅**。粘一个网址即可，只给站点首页也行——会自己从 `<link rel="alternate">` 里挑最好的那个源（RSS / Atom / JSON Feed 都认，GBK 编码的老站有兜底）。订阅列表可刷新、可逐条读，单条一键入本地书架：全文够长的直接入库，只有摘要的才回源抓正文；同一条不重复入库。
  - **视频转笔记**。贴 B 站 / YouTube 链接 → yt-dlp 取音频 → 本地 Whisper（mlx-whisper / faster-whisper）或云端接口转成文字 → AI 归纳成笔记与思维导图 → 当成一本书落进本地书架。多 P 视频只转你点的那一 P，界面上写清是哪一 P。音频只落临时目录，转完即清。AI 那段没配 LLM 时书照常可用，只存转写全文，并在任务结果里写明这次 AI 缺席的原因。
- 视频那条线的工具链按需补齐：ffmpeg 不必预装，点「装 ffmpeg」会下一份静态版放进归藏自己的数据目录（不动系统，系统里已有就复用）；yt-dlp 与 feedparser 进了默认依赖；转写引擎因为体积与平台差异（mlx 只在 Apple 芯片上跑）留在可选，缺件时界面写清该装哪一个。
- MCP 适配器从 20 个工具扩到 32 个：新增剪藏 1 个（`clip_url`）、订阅 8 个（`feed_list` / `feed_discover` / `feed_add` / `feed_entries` / `feed_entry` / `feed_refresh` / `feed_to_shelf` / `feed_remove`）、视频 3 个（`video_capability` / `video_plan` / `video_to_shelf`）。工具说明里补了一条：视频转笔记与取书一样是长任务，`video_to_shelf` 立即返回，用 `app_status` / `task_log` 轮询。
- 界面新增「订阅」与「视频转笔记」两个视图，以及对应设置项（转写引擎、语言、ffmpeg 状态）；剪藏输入框写明四家平台的差别，哪家免登录能取、哪家要对方放行，一眼看得出。
- 修：订阅源判定误把整页 HTML 当 RSS。博客页脚常夹一块创作共用的 `<rdf:RDF>` 授权声明（还裹在 HTML 注释里），原来看见 `<rdf` 就认成 RSS，结果把页面本身当成订阅源，把 `<link alternate>` 指着的真源挡在后面用不上，刷新回来 0 条。判据改为「feed 的根元素之前不先出现 `<!doctype` / `<html`」，并补了回归用例。
- 修：视频认链接时那条「有几个分 P」的提示从来不显示——模块给的是 `pages` / `count`，界面读的是 `parts`，字段名对不上就永远取到空。现在按 `count` 报总数，并用 plan 回来的地址对出「这次转的是哪一 P」。
- 修：视频的「转写设置」按钮点开就关——那个按钮在弹层之外，被全局「点别处就收起弹层」的监听立刻关掉；补 `stopPropagation` 挡住，与齿轮按钮同一处理。
- 修：视频书架的「一本都没有」空态从不显示——空列表的指纹是空串，而初值也是空串，等于永远认为没变化；指纹里带上条数，才铺得出那句说明。
- 门禁：新增离线套件 `tests/check_web_parse.py`、`tests/check_feed.py`、`tests/check_ffmpeg_tool.py`、`tests/check_video_note.py`（后者的真跑链路要 `GUIZANG_VIDEO_LIVE=1` 另开）与浏览器套件 `tests/check_media_views.py`（订阅与视频两屏、元素溢出、分 P 卡片）。全仓 18 项门禁通过。
- 这些新增都尽量借现成项目，不另起炉灶：RSS 解析用 feedparser，视频下载用 yt-dlp，转写用 MLX Whisper / faster-whisper，思维导图与笔记沿用本仓库原有的那一套（`book_notes.py` / `notes.json` / `mindmap.svg`）。

**0.9.8**

- 新增剪藏文章：粘贴微信公众号推文链接即可解析正文、入书架、直接读。解析后走的是取书那套分章与目录，所以导出 EPUB / PDF、定位文件、阅读器里的全部交互对剪藏进来的文章同样成立。抓取只取正文：脚本与样式整块丢弃，`javascript:` / `data:` / `vbscript:` 这类链接一律丢掉，本机与内网地址一律拒绝，反爬提示页认出来说人话而不是当成正文存进书库。
- 阅读器右栏笔记系统：选中正文一段即可高亮、加粗、划线、删除线或写批注；右栏与正文互相跳转，点一条笔记回到原句，读的位置不丢。侧栏与目录都能收起，收起后只剩正文。
- 两类笔记分开：随手划的高亮 + 一句话想法（轻量、成批）与独立笔记条目（可长、可引用书里别处的句子）分别存放；划词即记——选中后在浮层里写想法，保存时自动带上原文与所在章节位置。
- 高亮分色 = 语义标签：论点 / 疑问 / 可引用 / 待查 / 灵感五种颜色，颜色表达「这句打算怎么用」，不是装饰。
- 笔记支持 Markdown：一到五级标题、加粗、删除线、表格（选几乘几），长选直接出浮窗工具条。可套官方模板或自建模板起手，整篇按大纲画成思维导图（SVG），可导出 Markdown。数据写在这本书自己的目录里（notes.json / notes.md / mindmap.svg），不进数据库。
- 书架新增显示样式切换：瀑布（羊皮卷逐张展开）与叠卡（放大成一本书，左右以折叠屏式的张合切换，方向键可翻）。鼠标掠过卡片只给动效反馈，再次点击才出现「取书 / 查看详情」。
- 详情页瘦身：去掉完整包与正文 / EPUB / PDF / 目录四颗按钮，这些操作统一交给阅读器，一个地方做完。
- 修复：中止取书之后仍显示「已抓取」，且无法重新取。完成标记改为只有章节文件确实全部落盘时才置位；中止或失败后卡片给「续取」，详情页与任务记录里给「清掉重取」——后者先把上一次留下的残稿与浏览器临时分片删干净，再从头取一遍。
- 细节：阅读器底栏按栏宽分三档降级，窗口最小尺寸上调到 480×620；叠卡的扇形按舞台宽度收拢，窄窗口下不再出现「提示叫你去点、实际却越出窗口」的卡片。
- 切屏不再空一帧：第一次进的屏先铺与真实结构同形的骨架（统计是格子、书架是卡、划线是一行行），数据到了就地换上，不把整屏重画。按 600ms 的网关往返量过：统计页的空白从 714ms 到 3ms，为我推荐从 1637ms 到 1ms（顺手把排着队的两个请求并起来，内容到齐从 1640ms 到 708ms），划线笔记 20ms 到 1ms，搜索 7ms 到 2ms；再看过的屏 0~2ms。
- 悬停即预备：鼠标在导航或卡片上停住（90ms 起算），那一屏的数据就先取回来，10 秒内不重复取。点下去多半是直接用缓存，不再站在那里等接口。
- 不透明纸面：Markdown 写作与笔记编辑器的背景改为实心纸（浅色 `#fff` / `#f7f8fc`，深色 `#1e2229` / `#242935`），后面正文的字不再透上来。这一层刻意不跟「磨砂」滑杆走。
- 更省的一层玻璃：收着的浮层改用 `visibility` 收掉，不再只调透明度——每个 `backdrop-filter` 层都是一块离屏缓冲。同一次量法下带模糊的节点从 103 到 37，真正参与合成的从 56 到 8，每帧涂抹面积从 284 万像素到 218 万。
- 换章那一下更省：淡入换成直接播放，去掉「逼一次同步布局」的写法。一轮翻八章的布局次数从 39 到 32，脚本从 56ms 到 50ms，主线程任务从 748ms 到 685ms。多出来的账也写明白：每章约 3ms，用在记读到哪一行和铺本章大纲上。
- 阅读器续读回到上次停下的那一行，不再只回到那一章的顶上；新增本章大纲（`O`），随滚动指出在读的一节，点一条跳过去，下次打开还接着亮。
- 统计数字进场时滚到位（ease-out，只算一次），尊重系统的「减少动效」；数值到齐才开始滚，滚完不再变。
- 本轮新增门禁 `tests/check_reader_flow.py`（续读位置、本章大纲、数字滚落共 24 项），全仓 13 项门禁通过。
- 借鉴自成熟项目：不透明内容色 token 走 VitePress 的路子（内容底色不吃半透明变量），骨架微光取自 Element Plus 的 skeleton，章内定位的思路来自 VS Code 大纲面板与 markdown-it-anchor。

**0.9.7**

- 阅读器正文重排：段落收束孤字、中文标点悬挂出格，标题不再孤零零落在段尾，长句里夹英文/链接时断词更稳。
- 换章读取由静态的「正在读取…」换成同宽字形骨架，数据到达直接换上去，正文高度不再先塌再撑。
- 滚动更顺：进度计算按帧节流，正文往下滚时顶/底两条浮起淡影；正文、代码块、目录三处滚动条统一为同款细样式。
- 图片加载化入（淡入而非「啪」地裂开一块）；正文里的宽表格改为横向滚动，窄屏与专注模式下不再顶破版心。
- 键盘补充 PageUp / PageDown 翻屏，与空格、Shift+空格、Home/End 统一。

**0.9.6**

- 阅读中划句问 AI：选中正文一段，就地弹出「复制 / 译成中文 / 问小助手」，直接交给阅读器内的 Agent 追问。
- 外文阅读优化：长选可复制或译成中文；英文正文单击单词直接弹窗给释义（其他语言用长选）。
- 阅读时长双源记录：微信读书与本机时长分别统计，自动合并为一个统一时长，分列展示来源。
- 云同步：新增 WebDAV 与 OneDrive，可分别勾选同步阅读时长 / 阅读记录 / 书籍文件，凭据只留在本机。
- 阅读器界面进一步打磨：翻页、选中、弹窗的动效与流畅度优化。
- 稳定性修复：并发写本地账本时的竞态与丢更新、异常请求体导致连接中断等问题（冒火测试 48 项全过）。

**0.9.3**

- 新增多格式内置阅读器：Markdown 原生日读，导入的 EPUB / TXT / PDF 一并支持，目录随滚动高亮、键盘翻章、左右分栏。
- 新增独立的本地书架：与微信读书分开显示，集中列出本地已有的书（含自行导入的），支持一键定位到文件所在位置，支持导入 Markdown / TXT / EPUB / PDF。
- 新增导出 EPUB / PDF：取回的书可导成通用电子书格式，EPUB 保留插图与章节结构。
- 新增应用文件夹：安装后自动创建 `~/Documents/归藏`，取回的书自动存入，不再散落。
- 阅读器交互升级：翻章、滚动、悬停反馈更流畅。
- Markdown 阅读体验优化：分栏切换、字号与行距调整。

**0.9.2 及更早**

- 内置 Markdown 阅读器（0.9.2）：取回的书直接在界面内阅读。
- 界面内 AI Agent 助手（0.9.1）：右下角气泡随时呼出。
- 视觉与动效体系、设置弹窗化等界面打磨。

## 常见问题

**点击「连接账号」或某个按钮没有反应。**
查看启动服务的窗口，它会打印实际的「解释器」与「浏览器」路径，这两行是排查起点。真实出错时后端不会掐断连接，而是将异常与最后几行栈写入界面的「进展」栏，页面同时显示红字。

**加书架失败，提示「登录超时」。**
网页端 `/mp/` 接口校验两个 cookie：`wr_vid`（长期身份）与 `wr_skey`（约 30 天会话）。`wr_skey` 过期时服务端返回 **HTTP 200**，内容为 `{"errcode":-2012,"errmsg":"登录超时"}`。用同一 profile 打开任意微信读书页面，服务端会重新下发 `wr_skey`；脚本也会自动续期一次后重发。请照抄服务端 `errmsg`，不要推测码值含义。

**取书中途停止响应。**
每轮翻页有 45 秒硬超时，超时后重开浏览器续传，不会整场报废。需要更多信息时，设 `EXPORT_DEBUG=1` 每 60 秒输出一次 Python 栈；日志每 10 页有一次心跳。会话切换时可能有数十秒无响应，属设计内行为（判定 12 页无新内容并重开浏览器）。

**导出的章节数少于目录。**
正文依赖 hook Canvas 取字，阅读器会复用已绘制的缓存，因此整本抓取不保证 100%。引擎已按目录标题分章并支持断点续传，再运行一次通常可补齐。

**书架为空。**
未填写接口 Key。填写后书架、笔记、统计、推荐都会出现；未填写时本地已导出的书仍会列出。

**在搜索引擎中搜不到本项目。**
仓库名为 `weread-guizang`，项目名为「归藏」。

## 已知限制

- 取微信读书的书需要有效账号，且对目标书有阅读权限（无限卡或已购买）；剪藏、RSS 订阅、视频转笔记三条不用登录微信读书。
- 部分出版社限制网页端阅读（显示「去 App 阅读」），此类书无法导出。
- 抓取不保证 100%：阅读器会复用已绘制的缓存，部分页确实不触发 `fillText`；章节归属在两次导出之间可能略有差异（绘制批次不同），但正文总量稳定。
- 纯图廊章节图片密集时，图注与图的配对偶尔相差一位；正文章节中图片相对段落的位置准确。
- 导出速度约每页 1.1 秒（同一本书 14 页 A/B 实测：固定等待 2.12s/页 → 画完即走 1.08s/页，逐页正文 14/14 一致）。继续压缩需缩短「等待本屏画完」的判定，会开始丢字。
- Canvas 逐字抓取对跨行断字仍会丢失少量字符（如 `multi-agent` 被截为 `ulti`）。
- 官方 Gateway 不开放「书单」接口，面板中没有书单，最接近的是「推荐」。
- flomo 请求格式官方未提供示例，此处按通行约定发送 JSON，失败时回退为表单编码；**真实发送未经过验证**。
- 阅读划句翻译 / 查词 / 问助手依赖你自己填的兼容 OpenAI 接口；本地按常见的 `/chat/completions` 返回结构解析，**未对具体厂商逐一联调**，返回格式特殊时可能失败。
- 知乎 / 小红书 / X 的解析依赖对方「现在」怎么发页面，随时可能失效。未登录时知乎与小红书多数情况只回验证页，这是平台限制，不是可以绕过的 bug——归藏选择如实报错，不做登录态绕过（也就没有你的 cookie 上传问题）。
- RSS 只做「订阅 → 读 → 入书架」这条最小闭环：不做已读/未读同步、不做双向同步、不在后台定时轮询（要新内容时点一下刷新）。
- 视频转笔记第一版只支持 B 站与 YouTube。转写用通用 Whisper 模型，专有名词、多人对话、强口音的准确度不保证；本地转写首次使用要下载模型（large-v3-turbo 约 1.5 GB），下过一次就常驻本机。
- yt-dlp 属于「随平台改版天天要更新」的工具：视频取不到时先 `.venv/bin/pip install -U yt-dlp` 再试一次。
- 视频的 AI 笔记与思维导图依赖你自己填的兼容 OpenAI 接口；没填时就只存转写全文——这是有意为之，不在你没配的情况下把音频或文字发给第三方。
- 云同步按 WebDAV 与 Microsoft Graph 的公开接口实现；**未在真实网盘账号上端到端验证**，首次同步建议先用一本小书试通，确认无误再同步全部。
- 跨平台：Windows 分支已在单元测试中以 mock 平台标志运行，但**未在真实 Windows 机器上端到端验证**。

## 来源与许可

抓取引擎（`export_precise.py`、`download_images.py`）来自 [lbq110/weread-exporter](https://github.com/lbq110/weread-exporter)，在此之上做了二次开发。

**上游仓库未声明任何开源许可证。** 因此这里不替上游做授权决定：上述文件的著作权与授权状态以上游为准；如需再分发或商用，请先向上游确认。

归藏新增的部分——`ui_server.py`、`ui.html`、`mcp/guizang-mcp.mjs`、`platform_compat.py`、`shelf_add.py`、`login.py`、`skills/`、`tools/`、`tests/`、`docs/`、启动脚本与文档——按 **MIT** 使用。

仓库根目录**刻意不放 `LICENSE` 文件**：一份根 LICENSE 会覆盖整个仓库，而上游代码的授权不由此处决定。待上游明确授权后，再补充合适的许可证。

## 免责声明

仅供个人学习研究及备份**自己已购**的内容使用。请勿传播导出成果，勿用于商业用途，尊重著作权与平台服务条款。工具只监听本机回环地址，不会把账号、Key 或书籍内容发往任何第三方服务器——**唯一的外发是你自己填的那些接口**：划句翻译 / 问助手会把你选中的那段发给你在设置里填的 AI 接口，视频转笔记在选了云端转写时会把音频发给你填的转写接口、在填了 AI 接口时会把转写文本交给它做总结。这些地址都由你自己填写，归藏不预置、不代传。
