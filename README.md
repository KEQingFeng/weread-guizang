**中文** | [English](README.en.md) | [日本語](README.ja.md)

# 归藏 · weread-guizang

把微信读书的阅读数据导出到本地：整本导出为 Markdown、本地多格式阅读器、书架与笔记管理，以及面向 AI Agent 的 MCP 适配器。全部在本机运行，服务只监听 `127.0.0.1`。

## 问题

微信读书把阅读数据封装成一套只服务 AI Agent 的接口（Agent Gateway）：以 `skill_version` 和一组 `api_name` 提供书架、书城搜索、书籍详情、划线、想法、阅读统计、推荐等数据，用形如 `wrk-…` 的 Key 鉴权。它有两个边界：

- **只读**：白名单里没有写接口，无法加书或修改书架。
- **只服务 Agent**：没有面向人的界面。

用户因此拿不到整本书的正文，也无法在不写代码的情况下使用这批数据。

## 方案

归藏把这套接口还原成可用的本地工具，并补齐它没有的部分：

- 官方 Gateway 提供的数据，做成本地网页界面：书架、书城搜索、书籍详情、阅读统计、推荐、全部划线与想法。
- Gateway 不提供的功能——正文导出、图片下载、把书加入书架——复用登录态直接调用网页端接口。
- 面向 Agent，除界面外另提供一套零依赖的 MCP 适配器（20 个工具）。

三项能力都在本机完成，服务只监听 `127.0.0.1`，不经第三方服务器。

## 功能

- **整本导出为 Markdown**：文字与插图按阅读顺序交错，插图 8 线程并发下载，逐章落盘，支持断点续传。
- **本地多格式阅读器**：取回的书直接在界面内阅读，左右分栏、目录随滚动高亮、键盘翻章；导入的 EPUB / TXT / PDF 以同一套交互阅读。
- **独立的本地书架**：与微信读书书架分开显示，集中列出已取回与自行导入的书；可一键定位到文件所在位置，也可导入自己的 Markdown / TXT / EPUB / PDF。
- **导出 EPUB / PDF**：把取回的书导成通用电子书格式，EPUB 保留插图与章节结构。
- **界面内 AI Agent 助手**：右下角气泡随时呼出。
- **不打开微信读书即可查看数据**：书城搜索、书籍简介、作者与出版社、分类、阅读进度与最近阅读时间，都在本地显示。
- **搜索即抓取**：搜索结果直接转为导出任务；全部划线建索引后可全文检索。
- **划线回顾与 Anki 导出**：随机从全部划线中抽卡回顾；划线导出为 `.apkg`。
- **MCP 接入**：20 个工具，取书为长任务、不阻塞调用；服务未启动时适配器自行拉起。
- **划线迁移到 flomo**：多选划线批量转发，原文以「」包裹，附书名与标签。
- **配套 Skills**：仓库内 [`skills/`](skills/) 可直接安装；界面提供「一键建立 MCP」，把接入提示词复制给 Agent。

## 环境要求

- Python 3.10+（使用打包好的 macOS 程序时为 3.9+）
- Node（仅 MCP 适配器需要，`node -v` 能输出版本号即可）
- 有效的微信读书账号，且对目标书有阅读权限（无限卡或已购买）

## 安装

### 方式一：下载 macOS 程序

[**下载 归藏-0.9.3.dmg**](安装包/归藏-0.9.3.dmg)（约 2 MB，要求 macOS 13 以上，支持 Intel 与 Apple 芯片）

1. 双击 dmg，把 **归藏.app** 拖入「应用程序」。
2. 首次打开：若提示 **「归藏」已损坏，无法打开。你应该将它移到废纸篓。**，这是 macOS 对未签名应用的默认拦截，并非文件损坏。执行：

   ```bash
   xattr -dr com.apple.quarantine /Applications/归藏.app
   ```

   装在别的位置请替换路径；提示 `Operation not permitted` 时在命令前加 `sudo`。也可以在 **系统设置 → 隐私与安全性 → 安全性** 中找到被拦截的条目，点「仍要打开」。

3. 首次进入为配置页，点「我思故我在」开始初始化：创建虚拟环境、安装依赖、下载 Chromium（约 370 MB，需能访问外网；使用代理时请在代理软件中开启「系统代理」，程序会自动继承）。随后弹出浏览器完成扫码登录。此后每次打开直接进入界面。

   机器需已安装 Python 3.9+，可从 <https://www.python.org/downloads/> 安装；安装后无需重启。包内另附《首次打开必读.txt》。细节与替代做法见 [部署说明.md](部署说明.md)。

### 方式二：从源码运行

#### macOS / Linux

```bash
git clone https://github.com/KEQingFeng/weread-guizang.git
cd weread-guizang
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # 约 368 MB
.venv/bin/python ui_server.py --port 8770
```

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
.venv\Scripts\python.exe ui_server.py --port 8770
```

也可双击 `启动归藏.bat`，等同于依次执行上述四步。

启动后打开 <http://127.0.0.1:8770>。

### 首次使用

1. 左上角齿轮 → **连接账号** → 在弹出的浏览器窗口中使用微信扫码。会话持久化于 `cache/browser_profile/`，之后自动复用。
2. 填写 **接口 Key**：微信读书网页版「设置 → 开放 API」，复制形如 `wrk-…` 的 Key 并保存。Key 存放在本机 `cache/config.json`（权限 600），界面仅回显末 4 位。书架、笔记、统计、推荐、书城搜索、书籍详情均依赖它；未填写时仅列出本地已导出的书。
3. 若需将划线导入 flomo，填写 **flomo** 栏（flomo 网页版 → 设置 → API，形如 `https://flomoapp.com/iwh/xxxx/`），需要 flomo PRO。

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

共 **20 个工具**，分为四类：

| 分类 | 工具 |
| --- | --- |
| 书架与状态 | `shelf_list`、`app_status`、`task_log`、`book_files`、`folder_create`、`book_move` |
| 取书 | `book_fetch`、`task_stop`、`batch_fetch`、`account_connect` |
| 书与笔记 | `book_detail`、`search_books`、`notes_index`、`notes_search`、`notes_random`、`book_mark`、`shelf_add` |
| 导出 | `apkg_export`、`zip_export`、`cache_delete` |

两条约束写入适配器的工具说明，Agent 可读取：

- 取书是分钟到小时级的长任务，`book_fetch` / `batch_fetch` 会立即返回，进度通过 `app_status` / `task_log` 轮询。不要等待其执行完毕，否则必然超时。
- `shelf_add` 是唯一的写操作，会修改真实的微信读书书架；`cache_delete` 默认只列出，需带 `confirm=true` 才真正删除。

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

书统一存放在 `~/Documents/归藏/` 下。微信读书取回的书以书籍 id 命名，自行导入的书以 `imp_<名字片段>_<随机串>` 命名，两者结构一致，因此阅读器、EPUB/PDF 导出、定位文件等功能对它们一视同仁。

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
│   └── meta.json             # 多 source:"local" 与 format 字段
├── 书名.md                   # 合并稿（图片为相对路径，单独拷走会断图）
└── 书名.apkg                 # Anki 卡包（划线导出）
```

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
  WG[微信读书官方 Agent Gateway<br/>wrk- Key · 16 个 api_name · 只读]
  WP[微信读书网页端 /mp/<br/>复用登录 cookie · 唯一的写路径]
  FL[flomo]
  PC[platform_compat.py<br/>解释器 / 建组 / 中止 / 结束进程树]
  M[mcp/guizang-mcp.mjs<br/>stdio JSON-RPC · 20 工具]
  AG[AI Agent]

  UI <-->|JSON| S
  S --> E
  S --> SA
  S -->|HTTPS| WG
  SA -->|HTTPS| WP
  S -->|HTTPS| FL
  S --- PC
  E --- PC
  SA --- PC
  M <-->|HTTP| S
  AG <--> M
```

抓取引擎中几个关键决定及其原因：

- **分章依据为「正文中实际绘制的目录标题」**，不使用顶栏标题。顶栏每翻一页都会变化，早期版本因此将整批内容归到同一标题下，其余小节只剩空壳；目录标题每个只出现一次，天然去重。
- **翻页只使用方向键，不点击正文中心**。点击会触发微信读书的「回到上次阅读位置」，而从目录跳到开头不会更新阅读记录，导致「点击 → 等待 → 跳回开头」无法收敛，表现为开头数章整片丢失。
- **每轮翻页设硬超时**。Playwright 的 `page.evaluate` 默认无超时，渲染进程卡死时调用永久挂起；`asyncio.wait_for` 对不响应取消的调用无效，因此使用 `asyncio.wait` 取得超时后直接返回，再终止浏览器回收连接。
- **图片下载强制 IPv4**。macOS 上 urllib 默认先尝试 IPv6，路由不通时每张图约卡 120 秒。

## 更新记录

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

- 需要有效的微信读书账号，且对目标书有阅读权限（无限卡或已购买）。
- 部分出版社限制网页端阅读（显示「去 App 阅读」），此类书无法导出。
- 抓取不保证 100%：阅读器会复用已绘制的缓存，部分页确实不触发 `fillText`；章节归属在两次导出之间可能略有差异（绘制批次不同），但正文总量稳定。
- 纯图廊章节图片密集时，图注与图的配对偶尔相差一位；正文章节中图片相对段落的位置准确。
- 导出速度约每页 1.1 秒（同一本书 14 页 A/B 实测：固定等待 2.12s/页 → 画完即走 1.08s/页，逐页正文 14/14 一致）。继续压缩需缩短「等待本屏画完」的判定，会开始丢字。
- Canvas 逐字抓取对跨行断字仍会丢失少量字符（如 `multi-agent` 被截为 `ulti`）。
- 官方 Gateway 不开放「书单」接口，面板中没有书单，最接近的是「推荐」。
- flomo 请求格式官方未提供示例，此处按通行约定发送 JSON，失败时回退为表单编码；**真实发送未经过验证**。
- 跨平台：Windows 分支已在单元测试中以 mock 平台标志运行，但**未在真实 Windows 机器上端到端验证**。

## 来源与许可

抓取引擎（`export_precise.py`、`download_images.py`）来自 [lbq110/weread-exporter](https://github.com/lbq110/weread-exporter)，在此之上做了二次开发。

**上游仓库未声明任何开源许可证。** 因此这里不替上游做授权决定：上述文件的著作权与授权状态以上游为准；如需再分发或商用，请先向上游确认。

归藏新增的部分——`ui_server.py`、`ui.html`、`mcp/guizang-mcp.mjs`、`platform_compat.py`、`shelf_add.py`、`login.py`、`skills/`、`tools/`、启动脚本与文档——按 **MIT** 使用。

仓库根目录**刻意不放 `LICENSE` 文件**：一份根 LICENSE 会覆盖整个仓库，而上游代码的授权不由此处决定。待上游明确授权后，再补充合适的许可证。

## 免责声明

仅供个人学习研究及备份**自己已购**的内容使用。请勿传播导出成果，勿用于商业用途，尊重著作权与平台服务条款。工具只监听本机回环地址，不会将账号、Key 或书籍内容发往任何第三方服务器。
