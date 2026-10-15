#!/usr/bin/env bash
# 归藏一键门禁：改完代码跑这一个脚本，绿了再谈提交。
#
#   bash tests/run_all.sh              # 全套（静态检查 + 真机浏览器套件）
#   bash tests/run_all.sh --fast       # 只跑不依赖浏览器的那几项，秒级
#
# 沙盒：所有套件都写进一个临时目录（书库 / cache / 截图），绝不碰用户真实的
# cache/、output/ 和 ~/Documents/归藏。服务是一次性的，脚本退出就关掉。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || exit 1

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

SANDBOX="${GUIZANG_SELFTEST_DIR:-${TMPDIR:-/tmp}/guizang-selftest}"
export GUIZANG_SELFTEST_DIR="$SANDBOX"
export GUIZANG_DATA="$SANDBOX/cache"
export GUIZANG_BOOKS="$SANDBOX/books"
export GUIZANG_SHOT_DIR="$SANDBOX/shots"
# 门禁里不弹安娜的档案真窗口：那会在用户屏幕上开一个 Chromium、还占住唯一任务槽。
# 打开 UI_SERVER 那一侧的这条开关后，/api/anna 的 search 只写口令不起窗口；
# 界面自己手动点搜索仍会照常开窗（这条只影响测试用的那份服务）。
export GUIZANG_ANNA_NO_WINDOW=1
mkdir -p "$GUIZANG_DATA" "$GUIZANG_BOOKS" "$GUIZANG_SHOT_DIR"

FAILED=()
PASSED=0
run() {  # run <名字> <命令…>
  local name="$1"; shift
  echo
  echo "── $name ──────────────────────────────────────────"
  if "$@"; then
    PASSED=$((PASSED + 1))
  else
    echo "✗ $name 未通过"
    FAILED+=("$name")
  fi
}

# ── 静态：不需要浏览器，也不需要起服务 ──────────────────────────
run "Python 语法" "$PY" -m compileall -q -x '(/\.venv/|/dist/|/cache/|/output/|/node_modules/|/安装包/)' .
run "内联 JS 语法" "$PY" tests/check_inline_js.py
# 语法过得了不代表点得动：这一条查「调一个从没写出来的函数」，
# 那类 bug 在界面上的表现就是「按下去没反应」，浏览器不打开永远看不出来。
run "内联 JS 引用体检" "$PY" tests/check_js_refs.py
# MCP 适配器是一行一条 JSON-RPC 的 stdio 服务：宿主喂进来一条 `null`（合法 JSON、
# 不是请求对象），`const {…} = null` 当场抛，async 回调没人接就变成 unhandledRejection，
# 整个适配器进程退 1 —— 61 个工具一起消失，宿主只看到「MCP 服务掉了」。这条把这类
# 「合法 JSON 但不是请求对象」的形状全喂一遍，每喂一条跟一条 ping：进程不许退、必须回话。
# 要 node（和内联 JS 那两条同一档），指着一个没人听的端口，不联网、不起服务。
run "MCP 标准输入脏行（坏行不许带走适配器）" "$PY" tests/check_mcp_stdin.py
run "控件体检" "$PY" tests/audit_ui.py
run "个人信息扫描" "$PY" tests/check_privacy.py
run "跨平台口径" "$PY" tests/test_platform_compat.py
# 首次运行那一步（bootstrap.py）自己判断「Chromium 装没装」，答错一次就是重下 368MB。
# 它原先自己抄了一份平台目录表，抄丢了官方环境变量的 `~` 写法（glob 不认 `~`，于是永远
# 报没装）。这一条钉住「目录只有一处真相」（platform_compat）＋「~ 也认」＋「只认
# chromium 目录、同名普通文件不算」，另加「裸系统 Python 也起得来」那条硬约束。
run "首次运行环境判断（一把尺子 / ~ 写法 / 认哪些目录）" "$PY" tests/check_bootstrap.py
# macOS 的 XPC_FLAGS 0x2 位会让 Chromium 一 fork 就被系统收走（症状是「浏览器窗口弹不出来」
# 加一句看不懂的 Target page…closed）。2026-10-15 报的那组「微信读书连不上 / 一堆功能失效」
# 就出在这。这一条钉住「哪几位清、哪几位留、脏值不炸、真入口都过这把尺子」。
run "XPC 环境消毒（0x2 位该清 / 其它位该留 / 真入口接线）" "$PY" tests/check_xpc_env.py
# 清残留浏览器时，POSIX 那半段把 profile 绝对路径直接丢给 `pkill -f`，而那 pattern
# 是**扩展正则**不是子串：路径带 `(2)`（归藏复制了一份）时它等价于在找 `归藏 2/…`，
# 真实 cmdline 是 `归藏 (2)/…`，谁也匹配不上 —— 旧浏览器没杀掉、profile 锁继续被占，
# 下一轮还是「浏览器起不来」。Windows 那半段早用 `.Contains` 按字面匹配，这条把 POSIX
# 补齐并钉住。纯逻辑 + 一条真机 pgrep 来回，离线、不开浏览器。
run "残留浏览器清理（pkill 按字面认路径 / 元字符转义 / 真机来回）" "$PY" tests/check_pkill_escape.py
# 这一条是 2026-10-04 那三条「后端没启动」工单留下来的镜子：界面从磁盘现读、路由表却是
# 进程起来那一刻装进内存的，两边不是同一份时新功能一律 404。静态先对三样东西 ——
# 版本号成对、界面和适配器打的每个 /api/ 后端都得有、那句「本机服务没在跑」不许再当字符串
# 出现；顺带钉住「四个入口（两个启动脚本、MCP 适配器、Swift 壳）用的是同一把尺子」。
run "前后端对表（版本号 / 路由 / 话术 / 四个入口）" "$PY" tests/check_route_pair.py
# 打包清单（shell/build_macos.sh 里的 APP_FILES）是 mac .app / Windows zip / 源码 zip 共用的
# 唯一一份，三个打包器都有「根目录 .py 漏进清单就停下」的反向护栏 —— 但那要等到打包那一刻
# 才跑，而 --fast 一路绿。book_ids.py 就这么漏了：源码直接跑正常，装起来一开就哑。
# 这一条把打包器那把尺子搬进 --fast，漏了谁直接点名。
run "进包清单（根目录每个 .py 都得进 APP_FILES）" "$PY" tests/check_package_list.py
# 文档里说的东西也得真有：README / docs 的仓库内相对链接按**文档所在目录**解析，
# 而「README 里的包与图必须真实存在」这条规矩写在开发规范 §8，一直没人拦。
# docs/部署说明.md 就漂过：`[安装包/…](安装包/…)` 从 docs/ 出发指到了不存在的
# docs/安装包/…，文件明明在、GitHub 上点了却是 404。这一条顺带守住「安装包/ 只留最新
# 那一份」（最多一个 dmg + 一个 Windows zip，且版本号一致），并带两条自证会红的对照。
run "文档链接与安装包（相对链接 / 点名的包 / 只留最新一份）" "$PY" tests/check_doc_links.py
# 取书续传锚点是纯逻辑（目录 + 已落盘章节），不用起浏览器也验得了：
# 它守的是「卡住之后再点取书必须能接着往前读」这条 —— 卡死一次就别再来第二次。
run "取书续传锚点" "$PY" tests/check_resume.py
# 锚点管「从哪儿接着读」，这一条管「那份目录本身被改坏了怎么办」：_catalog.json
# 是 export_precise 自己写的，用户会手工编辑、旧版本也落过别的形状。旧读法只兜了
# json.load 那一下 —— `[1,2]` / `null` / 数字让 re.split 或 for 抛 TypeError，整趟
# 取书带着「读取目录」垮掉；`"目录"` / 字典不抛，却把单个字符或 key 当成目录项，
# 续传锚点钉到错位置。_stuck.json 同理（列表 / 字符串 / 数字让 .get / .items 抛）。
# 这一条把「脏形状当没目录 / 没记录」「好数据一个字不改」与一条「旧读法确实会抛」的
# 对照一起钉住。纯逻辑 + 临时目录，不联网、不开浏览器，归静态段。
run "取书目录账本脏形状（脏形状当没目录 / 好数据不改 / 对照）" "$PY" tests/check_export_catalog.py
# 目录账本收完了，引擎**自己**的另外三个读口还留着同样的旧读法，而且踩得更靠前：
#   _progress.json → write_progress 里 data.update 在 try 之外，`[]` / `"书名"` / 数字
#                    一句 AttributeError 就在取书开头（running=True）或收尾带走整趟任务；
#   meta.json      → main 开头 meta.get、收尾 meta.update，脏形状让「书名读不出来」，
#                    或书都取完了 done 却写不进去（界面永远显示没取完）；
#   raw/*.json     → 续传开头清点已抓图时 json.load(...).get + img["url"]，一条脏记录
#                    让这本书再也取不动，与 collect_tasks「只跳不牵连」相反。
# 这一条把三个读口各钉一圈（脏形状不抛 / 好数据不改 / 旧读法确实会抛的对照 / 接线走
# book_layout.read_meta 同一把尺子）。纯逻辑 + 临时目录，不联网、不开浏览器，归静态段。
run "取书引擎脏读口（进度 / meta / raw 页记录不带走整趟取书）" "$PY" tests/check_export_dirty_read.py
# 上一对管的是「脏账本读得开」，这一条管的是「账本压根别写脏」。引擎里三份账本
# （_catalog.json / _stuck.json / meta.json）原先都是 open(path, "w") 直写，而 "w"
# 一进去就把旧内容截断 —— 取书又是会被界面停止键强杀的长任务，恰好在写完前被杀就
# 只剩半截 JSON：目录丢锚点、meta 丢书名与 done（界面显示「这本没取完」），而
# meta.json 偏偏是在整趟收尾那一刻才写的。仓库里其它账本早走「先写 .tmp 再改名」，
# 这一条把引擎补齐：正常写一个字不改、写一半失败旧档完整、失败或中断不留 .tmp，
# 外加一条「老直写确实会把旧档砸坏」的对照，免得门禁空转。纯逻辑 + 临时目录，归静态段。
run "取书账本原子落盘（写一半不毁旧档 / 不留 .tmp / 三份账本同一条写口）" "$PY" tests/check_export_atomic.py
# 与续传锚点配成一对：锚点管「卡住后再取能从断点往前」，这一条管「卡住这件事本身」
# —— 引擎进程退了但孙子进程攥着 stdout、或读输出抛异常时，任务必须照样收尾，
# 不许把 TASK["running"] 永远钉在 True（界面「正在取」、再取被「已有任务在运行」顶回）。
run "任务收尾（进程退了就收 / 读出错也收）" "$PY" tests/check_task_reader.py
# 三条新线（订阅 / 视频 / 平台解析）都是离线自测：夹具跑在本机临时 http.server 上，
# 数据目录全指进各自沙盒，不联网、不碰用户真实书库。
run "RSS 订阅（发现 / 抓取 / 去重 / 入库）" "$PY" tests/check_feed.py
# 刷新合并只认非空的正文：有的源在两次抓取之间只在 title / link 之间摆动，也可能
# 只回半截内容。旧代码用 content_hash 判「文章被改过」，而 parse_feed 每条一定带
# summary / content_html（可以是空串），于是这一趟没给正文时，上一次存好的正文会
# 被静默覆盖成空。这一条把「空正文保留旧值 / 真有新正文照刷 / 用户已读状态不丢 /
# 新出现的空正文条目仍收」一起钉住，纯逻辑、离线，归静态段。
run "订阅刷新合并（空正文不抹旧正文 / 真改动仍刷新 / 状态保留）" "$PY" tests/check_feed_merge.py
# 条目里的时间字段也会被手改坏：`published_ts` / `fetched_at` 写成字符串时，列表里
# 只要有一条是数字、另一条是字符串，排序就是 `'<' not supported between 'str' and
# 'int'`，`entries(since=…)` / `mark_read_all(older_than=…)` / `prune(days=…)` 也一起
# 500。这一条把手改的脏 feed.json 喂进去，钉住「入库即整数、纯数字串不误判成 0、
# 认不出的当 0 且不丢条目、未知字段照旧保留」，纯逻辑、离线。
run "订阅条目时间字段脏值（排序 / since / 标已读 / 裁剪不抛）" "$PY" tests/check_feed_ts.py
# 同一把尺子的另一半：上一条管「被写成字符串」，这一条管「越界数字」。`json` 认
# `Infinity` / `1e400`（解析成 inf）且能原样落进 feed.json，而 `_as_ts` 的数字那支
# 是 `int(v) if v > 0 else 0` —— `int(inf)` 抛 OverflowError，这条异常在 `_load` 的
# json.load 之外，于是 published_ts / fetched_at / later 任一为 Infinity，`entries()` /
# `subs()` / `summary()` 整页打不开。这一条钉住「越界归 0、超大整数照认、坏一条不
# 带走整页、干净账本一个字不改、落盘后不再有 Infinity」，纯逻辑、离线。
run "订阅条目时间字段越界数字（Infinity / 1e400 不打抛、不带走整页）" "$PY" tests/check_feed_overflow.py
# 订阅正文的清洗是外站内容进成品的那道边界。原来那把尺子只量原始字节，浏览器却会
# 先解一层 HTML 实体、再把 URL 里的 TAB / 换行删掉 —— `&#106;avascript:` 与
# `java<TAB>script:` 在浏览器手里都是 javascript:；截断的 `<script>` 没有闭合标签，
# 也会被漏过去。这一条把它们连同「正常链接 / data:image 一个字不改」一起钉死，纯函数、离线。
run "订阅正文清洗（实体 / 拆字协议 / 斜杠分隔 / 落单 script / 回归）" "$PY" tests/check_feed_sanitize.py
# OPML 导入收的是「从别处整包拿来的订阅清单」，按不可信文件办。两道闸原来都有漏：
# 实体闸门只看开头 8192 字节（前面垫一段长注释就能把 DOCTYPE 顶出窗口），嵌套 outline
# 的递归没有层数上限（几千层就把栈撞穿，抛出的是 RecursionError 而不是人话）。
# 这一条把「垫到窗口外仍要拒 / 夹在两段注释中间仍要拒 / 33 层越界给人话 / 上限内照常
# 导入 / 正常 &amp; 不误伤」一起钉死，import_opml 走 dry_run，不联网。
run "OPML 导入安全边（全文实体闸门 / 嵌套层数上限）" "$PY" tests/check_opml_safety.py
# feed.json 不只 feed.py 在写：用户会手改、旧版本写过别的形状、云同步也会落半截。
# _load 原来只 setdefault 了三个键名，值坏掉就带出去 —— entries 写成列表 / 字符串时
# `.values()` 直接抛 AttributeError（这条异常在 _load 外面，连空账都回不去，界面「订阅」
# 整页打不开）；feeds 写成对象、seen 写成列表、源行缺 id、settings.groups 写成字符串
# 各有各的炸法。这一条钉住「坏形状收成空账、合法数据照留、公开读接口一个都不抛」，离线。
run "订阅账本脏形状（坏形状收空账 / 合法照留 / 读接口不抛）" "$PY" tests/check_feed_dirty.py
# 上一条管「形状坏掉」，这一条管「值里 / 键里夹着落单的半个代理码点」——同一个账本、
# 另一类坏输入：`"\ud800"` 这种 JSON 转义 json.loads 收得下、utf-8 编不出。外站的
# JSON Feed 在 title / id 里带它会等到算去重键 sha1(...).encode("utf-8") 才炸，整趟
# 刷新带着这个源一起垮；脏账本里留着它则任何一次写盘都在 ensure_ascii=False 的
# json.dump 上抛，界面「点了没反应」还留一个半截 .tmp。这一条钉住「读口与解析口
# 都洗干净、写盘不再抛、seen 键与条目 id 仍对得上、真 emoji / ZWJ 一个字不动、对照
# 证明旧读法确实会抛」，纯逻辑 + 临时沙盒，不联网、不开浏览器，归 --fast 静态段。
run "订阅落单代理码点（脏账本 / JSON Feed 不再打崩刷新与写盘）" "$PY" tests/check_feed_surrogate.py
run "平台解析（知乎 / 小红书 / X）" "$PY" tests/check_web_parse.py
# 上面那条管「正常回包解析对不对」，这一条管「回包字段形状不对时会不会把 Python 报错
# 弹到用户脸上」。三家的 JSON 都是别人家的：text 可能是数字、photos 可能是 5、
# 合法 JSON 也可能根本不是对象。全离线（夹具是一个本地临时服务）。
run "平台解析脏形状（数字当文本 / 数字当数组 / 回包不是对象都不弹 Python 报错）" "$PY" tests/check_web_parse_dirty.py
# 上一条管「字节是什么编码」，这一条管「正文怎么切章」。Markdown 的 `#` 在围栏代码块里
# 是注释不是章节，可旧切法对全篇一视同仁 —— 导入一本带代码示例的 md（`# 安装依赖`
# 这种），代码注释会被当成章标题切走，目录里多出一批不存在的章节、正文被剪碎。
# 围栏口径按 CommonMark 钉住（同种字符收尾 / 不短于开栏 / 反引号 info 里不许再有反引号 /
# 没收尾吃到文末），并验正常 md、`##` 小节不受影响。纯函数、离线。
run "导入书切章（围栏代码块里的 # 不算章节）" "$PY" tests/check_import_fence.py
# 安娜的档案这一栏不是「接口取书」，是「弹一个看得见的窗口，用户在里头点下载，
# 归藏接住文件转成书」。所以这一条不联网、不开窗口，验的是不带窗口就判得掉的四件事：
# 账本（cache/anna.json）只存名字与数字、下载夹的路径拼法不许被人用 ../ 穿出沙盒、
# 同一份文件按字节摘要不重复入库、认不出的格式留着原文件并写清原因。全离线。
run "安娜的档案（账本 / 防穿越 / 去重 / 入库 / 域名）" "$PY" tests/check_anna.py
# 接住下载这一步只做「原样落盘 + 排队」，但最容易出的事故是同一秒里连下几份同名
# 文件互相覆盖（用户下了四本、目录里只剩两本，队列里四条还指向同一个路径）。它不该
# 靠真开一个窗口来验：假 download 对象 + 钉死在同一秒，离线把「谁也没被谁盖掉」钉住。
run "安娜接住下载（同名不覆盖 / 路径护栏）" "$PY" tests/check_anna_catch.py
# 接住下载的第一步是「把远端给的文件名变成能落盘的名字」。目录项上限是 255 字节，
# 而中文一个字占三个字节 —— 按「120 个字符」截出来的名字看着没超，open() 却当场
# ENAMETOOLONG（用户看到的是「这份没接住」）。这一条把两条上限、真建一次文件、
# 不切半个字、幂等、追加时间戳后仍在 255 内，全部离线钉住。
run "安娜落盘文件名（按字节截断 / 真建得出来 / 幂等）" "$PY" tests/check_anna_name.py
run "ffmpeg 按需下载（离线）" "$PY" tests/check_ffmpeg_tool.py
run "视频转笔记（下载 / 转写 / 总结 / 导图）" "$PY" tests/check_video_note.py
# 上面那一套走的是正常输入（章节起点递增、每段都带 id）。这一条专门打两种「归属」错：
# LLM 漏给章节起点（清洗层把每章都填成 0）时正文会全堆进最后一章、前几章只剩空标题；
# 编辑器新插的段落带着新 id，却按位置认领了旧段落的说话人 / 置信度。两条都在纯函数与
# 临时沙盒里当场钉住，不联网、不起浏览器。
run "视频转笔记归属（章节起点不可用 / 新段不继承旧段字段）" "$PY" tests/check_video_chapters.py
# 「重建」读的是磁盘上那份 summary.json（用户改得动），不是模型刚给的那一份：一章被
# 改成字符串、或少了 title，`_chapter_bodies` 原先就 AttributeError / KeyError，而
# /api/video 只接 ValueError —— 异常穿出去就是连接被甩（界面上是「点了没反应」）。
# 这条把读盘归一化与纯函数的容错都钉住：坏账本不抛、转写一个字不少、好账一模不改。
# 纯逻辑加临时目录里的一本假书，不起服务、不联网、不开浏览器，故归静态段。
run "视频书重建脏账本（手改 summary 不甩连接 / 正文不丢 / 好账不改）" "$PY" tests/check_video_rebuild.py
# 导图与画板两条新线也是纯逻辑自测：排版差分、环检测、存读导删全在系统临时目录的
# 沙盒里跑，不起服务、不开浏览器、不截图 —— 所以归静态段，--fast 也得把它们带上。
run "思维导图（清洗 / 上限 / 环检测 / 四形态坐标 / SVG）" "$PY" tests/check_mindmap.py
# 思维导图存盘的那条边：节点 / 备注 / 标题里落单的半个代理码点写不进 utf-8，
# 旧写法整张图都存不下去、还在书目录里留一个半截 .tmp。这条与画板那条同一口径：
# 写之前摘掉落单的那半个（成对的表情符号一个字不动），失败时把 .tmp 收掉。
run "思维导图存盘收尾（坏字符 / 不留 .tmp / 幂等没破）" "$PY" tests/check_mindmap_store.py
# 笔记那一侧的导图是另一份实现（book_notes.build_mindmap_svg），此前没有门禁。它出的
# SVG 会直接落进书目录，最怕的不是排得丑、而是根本不合 XML：标题是「先转义、后截断」，
# 带 & / < / " 的书名在第 60 个字正好被切进实体中间（`&amp;` → `&am`），文件照写不误，
# 打开却是一张破图。这一条把边界截断与正文转义都按成会失败的断言，真用 XML 解析器验。
run "笔记思维导图（标题截断不切实体 / 标签转义 / 真解析）" "$PY" tests/check_notes_mindmap.py
run "画板（存得下 / 读得出 / 导得走 / 删得掉 / 不越界）" "$PY" tests/check_board.py
# 画板存盘的两条边：标题里落单的半个代理码点写不进 utf-8，不许把整块板连累成 500；
# 删板 purge 必须把只有磁盘看得见的 .prev / .tmp 一起收掉，不然它们永远占盘又删不掉。
run "画板存盘收尾（坏字符 / 不留 .tmp / purge 清 .prev）" "$PY" tests/check_board_store.py
# 书库从「全挤一个文件夹」改成一个模块一个文件夹，账本也跟着分格。这一条管三件事：
# 老书归置会不会搬丢/搬坏（绝不覆盖）、按模块列清单会不会串门（剪藏不许出现在微信读书）、
# 账本分格后建夹贴标签只动自己那一格（空文件夹不许被清理那一刀悄悄削掉）。全在临时沙盒里跑。
run "书库目录结构（分模块 / 归置 / 账本分格）" "$PY" tests/check_book_layout.py
# 上一条验「清单分不分得开」，这一条验「清单会不会被一本书带崩」。meta.json 与
# _progress.json 被手改成 `[]` / `"书名"` / `123` 时都是合法 JSON，json.load 照收，
# 可接着的 meta.get(...) 会当场 AttributeError —— 首页 /api/state 整份 500，
# 用户看到的是「书全没了」。这条把脏形状、好书不被连累、好形状不被误伤一起钉住。
run "书架清单脏账本（meta / 进度不是对象时不整份抛）" "$PY" tests/check_shelf_dirty.py
# 上一条只钉住书架清单这一条路，可 meta.json 不止它在读：合并稿 / 打包 / 导出 / 目录 /
# 改书名 / 清残稿各自 json.load 一遍再 .get / .update，脏形状（`[]` / `"书名"` / `123` /
# `null`）一处 AttributeError 就是「导出没反应 / 保存失败」。
# 现在六条路统一走 ui_server._meta_dict()，而它只复用书库那边已有的
# book_layout.read_meta() —— 同一条规矩只有一份实现，不给「抄两遍、日后只改一处」留机会。
# 这一条把「脏形状当没 meta、好数据一个字不改、六条路一个都不抛」和一条「旧读法确实会抛」
# 的对照一起钉住，纯函数 + 临时沙盒，不联网、不开浏览器，归 --fast 静态段。
run "书目录 meta 脏形状（合并稿 / 打包 / 导出 / 目录 / 改书名 / 清残稿不抛）" "$PY" tests/check_meta_shape.py
# 上一条管的是「meta 被写成不是对象」；这一条管「meta 是对象，可字符串值里夹着落单的
# 半个代理码点」—— `"\ud800"` 这种转义 JSON 收得下，utf-8 却编不出来：一本书的
# title 带上它，`GET /api/zip` 会在 `zipfile.writestr` 里抛穿 do_GET、把连接直接甩掉
# （用户只看见「下载点了没反应」），`/api/epub`、`/api/pdf` 是 500，`content_disp`
# 拼下载头时自己也会抛。收口放在读 meta 的唯一入口 `book_layout.read_meta()`，
# 下载头那道 HTTP 边界另兜一层。纯函数 + 临时沙盒 + 一个进程内真服务，不联网、
# 不开浏览器，归 --fast 静态段。
run "书目录 meta 半个代理码点（打包 / 导出 / 下载头不甩连接）" "$PY" tests/check_meta_surrogate.py
# 上一条管「干净的老账本折得对不对」，这一条管「脏的老账本会不会把整份书架打崩」。
# cache/library.json 会被手工编辑、被云同步从别处落盘、旧版本也写过别的形状：顶层
# `"assign": ["书号"]`、`"order": 5`、`"folders": 5`，或 modules 里 folders 的元素
# 是数字 / 缺 id —— 这些全是合法 JSON，旧代码直接 .items() / 直接迭代就抛，
# load_lib 是 /api/state 的必经之路，用户看到的是「我的书全没了」。逐样喂一遍，
# 要求不抛、合法的那部分不连带丢、文件夹接口那几行仍能跑。纯逻辑 + 临时沙盒。
run "书架账本脏形状（顶层五样 / folders 元素 / 不连带丢数据）" "$PY" tests/check_shelf_ledger.py
# 导入书（Markdown / TXT / EPUB / PDF）第一关是「这份字节是什么编码」。
# 「编码认错」与「格式认错」都钉住，再走一遍 GBK TXT 真落盘，看章标题与目录是不是原字。
# 另半段守 PDF 那趟硬换行：抽出来的每一行都是独立一行，拼回一段时拉丁词之间要补空格
# （老实现一律直加，`quick brown` + `fox` 拼成 `brownfox`），中文与全角标点之间反而不许补。
# 纯逻辑 + 临时沙盒，不联网、不开浏览器，所以归静态段。
run "导入书（编码识别 / 格式兜底 / PDF 硬换行 / GBK 落到章标题）" "$PY" tests/check_book_import.py
# 上一条管的是「字节怎么解、章怎么切」，这一条管 EPUB 的配图归属。落盘名按「包内
# 路径」去重（a/pic.png → pic.png、b/pic.png → pic_2.png），而正文引用原先只看
# basename —— 两篇文档各引一张 pic.png 时正文全写 images/pic.png，第二章显示的是
# 第一章的图，不报任何错。两文档同名图、../ 相对路径、根相对路径、百分号编码的中文
# 图名、缺图兜底各钉一条，真建一次 EPUB 走 import_book。纯内存 zip，不联网。
run "导入书 EPUB 配图（同名不串门 / 相对路径 / 缺图兜底）" "$PY" tests/check_book_import_epub.py
# 上一条管好包；这一条管**坏包**。`.epub` 这个扩展名一给定，guess_format 就不再回头看
# 内容，所以「这份文件其实不是个压缩包」只有 read_epub 那一层挡得住。旧代码里
# zipfile.BadZipFile 会穿过 import_book 到 /api/import 的兜底 except：界面拿到 500 加
# 一句英文 `File is not a zip file`（安娜那条路账本上记的是 `BadZipFile` 这个类名）。
# 加密条目（RuntimeError）同理。这一条把「压缩包坏法 → 人话 ValueError」钉死，并守住
# 好包的章节、OPF 元信息与落盘正文一个字不变。纯内存 zip，不联网、不起服务。
run "导入坏 EPUB（坏包 / 加密包翻成人话，好包零改动）" "$PY" tests/check_import_broken_epub.py
# 上面两条管的是「文件本身坏」；这一条管的是「文件名 / 标题 / 作者这几个字段坏」。
# POST 来的 JSON 里 `"\ud800"` 这种转义能被 json.load 读成一个落单的半个代理码点，
# 旧代码把它写进 ensure_ascii=False 的 meta.json 时抛 UnicodeEncodeError —— 接口回 500
# 加一句 codec 原文，而书目录已经建出来了，书架上留一本只有半截章节、没有 meta 的幽灵书。
# 剪藏那条路（封面 og:image 地址）同理。收口复用 book_layout 那把共用清洗尺，离线钉住。
run "导入 / 剪藏坏字符（半个代理码点不许打成失败、不留半截目录）" "$PY" tests/check_import_surrogate.py
# 写作那一格从「一叠散稿」升级成「书 → 第一章 / 第二章 …」之后，多出的一份风险在账本上：
# `_books.json` 记的是「哪几章、按什么顺序、属于哪本书」，章本身还是原来那篇稿子
# （目录 / rev / 快照 / MCP 一字未改）。账本一坏，界面上就是「我的章不见了」这种最要命的
# 错觉，所以建书 / 归章 / 一章只属一本 / 排顺序 / 坏账本兜底 / 合稿补标题 / 摊成
# chapters/0001.md 全在临时沙盒里当场钉住，不联网、不起浏览器。
run "写作书籍账本（建书 / 归章 / 排顺序 / 坏账本 / 合稿 / 摊成一本）" "$PY" tests/check_writer_book.py
# 写作页的结构两件事（大纲 / 标题兜底）都是纯函数，可最容易出错的不是「认不认标题」，
# 而是把代码块里的井号也认了：粘一段带 `# 注释` 的代码，大纲里就多出并不存在的章节，
# 稿子以代码开头时标题还会变成 ```bash 那一串反引号。导出的 md_to_html 早就懂围栏，
# 这一条把两把尺子对齐，连同「未收尾的围栏吃到文末」的 CommonMark 口径一起离线钉住。
run "写作大纲（围栏代码块 / 行号 / 标题兜底 / 与导出口径对齐）" "$PY" tests/check_writer_outline.py
# 写作页那三张「往下写的方向」卡是 `parse_sparks` 收出来的，喂模型的素材是
# `material_block` 拼出来的，两处都只会「悄悄少一块」而不报错：旧正则把任何数字
# 开头的方向啃掉一截（「2024 年的…」→「年的…」），素材头一条自身超限时又整段丢成空串。
# 纯函数、离线，把「标记照摘 / 数字照留 / 超限夹一段」钉成会失败的断言。
run "写作方向卡与素材夹取（数字不当编号 / 超限不整段丢）" "$PY" tests/check_writer_sparks.py
# 写作页换成富文本工具条之后，稿子里会出现取书正文从来没有过的语法：内联颜色、底色、
# 下划线、删除线、待办勾、表格。界面预览用的是另一套渲染器，所以「预览看着对」不代表
# 成品里也在 —— md_to_html 没学过的语法会被原样转义，导出的 PDF / 电子书里就是一行
# 看得见的 <span style=...>。白名单放行什么、挡住什么、三份样式表有没有各自的画法，
# 连真打一个 EPUB 验包里带没带上，全部离线钉住。
run "富文本导出口径（白名单 / 越权标签 / 待办与表格 / 三份样式 / 真打一个 EPUB）" "$PY" tests/check_rich_export.py
# 上一套管的是「标记进不进成品」，这一套管的是「进去之后会不会从属性里逃出来」。
# 图片地址、链接地址、alt 都是外部内容（别人网页里的图、导入书里写的链接），而
# `_esc` 只转 &<> 、双引号原样留着 —— 一个引号就能关掉 `src="…"` 再挂一个
# onerror=… ，导出的 HTML 与交给铬内核打印的 PDF 里就是能跑的脚本。三种写法各钉
# 一条，并真打一个 EPUB、解析包内那页，确认成品里也没有多出来的事件属性。
run "导出口径的属性逃逸（图 / 链接 / alt 不许注出事件属性）" "$PY" tests/check_export_safety.py
# 同一道闸还有一处：围栏代码块 ``` 后面那串语言名，取自导入书 / 剪藏原文，落点
# 却是 `class="lang-…"` 这个属性 —— 那一处仍在用 `_esc`（quote=False），一个双引号
# 就能从属性里逃出去挂 onmouseover=…。这里把「语言串带引号 / 尖括号 / &」三种写法
# 各钉一条，并真打一个 EPUB 解析包内那页，确认成品里也没有多出来的事件属性。
run "导出口径的属性逃逸（围栏语言串不许注出事件属性）" "$PY" tests/check_export_fence.py
# 上面两条管的是「正文里的地址带引号会不会从属性里逃出去」，都在 md_to_html 那一层。
# 成品里还有另一处属性值直接来自磁盘：images/ 下的文件名（下载器只挡路径分隔符与
# 控制符，`& < > "` 都留着）。EPUB 把文件名拼进 `href="img/…"` 时用的是 `_esc`
# （不动双引号），一个 `a"b.jpg` 就让 content.opf / cover.xhtml 不再是合法 XML；
# PDF 的 `_abs_images` 又拿实体编码过的属性值去磁盘上找文件（`c&amp;d.jpg`），
# 不解码就永远「文件不存在」、那张图在 PDF 里静默消失。这里真打一个脏名字的 EPUB
# 逐份解析、再把 `_abs_images` 的替换结果还原成路径核对。纯离线、不起浏览器。
run "成品图片文件名（带引号 / & / < 不把 EPUB 打坏、也不从 PDF 里消失）" "$PY" tests/check_export_names.py
# 上一条管「正文里的地址」和「磁盘上的文件名」，这一条管「书的身份」——书名 / 作者。
# 它们来自 meta.json（手改 / 云同步 / 导入都写过它），而 json 连 `"\u000c"`（换页符）
# 这种转义都照收：XML 1.0 里根本没有这个码点，正文那条路早在 md_to_html 入口洗过，
# 元数据却没有 —— 一个换页符就让 content.opf 与 cover.xhtml「不是合法 XML」，
# 阅读器把整本书拒收，而导出返回成功、界面说「导出好了」。落单的半个代理码点更早：
# zipfile.writestr 在 encode 那一步就 UnicodeEncodeError。这里真打一个 EPUB，
# 用 ElementTree 逐份解析，并留一条「旧写法确实解不动」的对照免得空转。纯离线。
run "导出元数据 XML 字符（控制符 / 半个代理码点不打坏 EPUB，正常字一个不改）" "$PY" tests/check_export_xml_chars.py
# 跟上面那条并排：那条管「进成品的内容能不能逃出去」，这条管「成品本身是什么字节」。
# export_precise 写出去的章节 md / raw json / 合并稿原来不写 encoding，靠系统默认编码 ——
# macOS 恰好是 UTF-8 所以一直没露馅，Windows 的 GBK 代码页上就变成写 GBK、读 UTF-8，
# 正文里有个生僻字或 emoji 还会当场 UnicodeEncodeError。静态扫一遍 open()，
# 再把默认编码按到 ASCII 真跑一遍 save_chapter → merge_chapters。纯离线。
run "导出落盘编码（文本 open 都要 encoding / ASCII 环境下中文照样写得出）" "$PY" tests/check_export_encoding.py
# 紧挨着上一条：那条管「写出去的字节是什么编码」，这条管「读回来的章节坏一个字节会不会
# 带走整本书」。chapters/*.md 是用户能直接编辑的（也会被云同步 / 旧版本写回来），
# `read_chapters` 原来严格按 UTF-8 读 —— 一个另存成 GBK 的章节就让 EPUB 与 PDF 一起抛
# UnicodeDecodeError，另外几章正文也无从导出。现在按字节读、坏字节落成 U+FFFD：坏章照样
# 进成品、好章一字不差、干净的书一个字符都不动。真打一个 EPUB 验，纯离线。
run "导出章节读取容错（坏字节不带走整本书 / 不丢章 / 干净书零改动）" "$PY" tests/check_export_tolerant.py
# 上面那条收的是导出（EPUB / PDF）那一侧；读同一批 chapters/*.md 的还有界面这一侧 ——
# 阅读页取某一章、合并稿、单本 / 多本打包、剪藏 / 订阅 / 视频的「整篇小结」，原先各自
# 又 `open(..., encoding="utf-8")` 读一遍：一个另存成 GBK 的章节在导出那边早就只坏几个字，
# 在这边却让 `chapter_text` 回 None（`/api/read` 404「没有这一章」，文件明明在）与
# `merged_markdown` 抛穿 `do_GET`（`/api/zip` 甩连接、整篇小结报错）。现在四处都走
# `book_export.read_chapter_file` 这一把尺子。纯函数 + 进程内真服务，不联网，归静态段。
run "阅读 / 打包读章节容错（坏字节不回 404 / 不甩连接 / 好章零改动）" "$PY" tests/check_reader_tolerant.py
# 上一条管「坏字节不许带走整本书」，管不到「正文里的控制字节撞上渲染器的占位记号」。
# md_to_html 内部拿 \x00 / \x01 当行内代码与转义的占位，而正文是外部内容（导入的书、
# 剪藏原文、手改或云同步写回的章节），0x00 / 0x01 都是合法 UTF-8、read_chapters 原样
# 读进来 —— 正文里凑巧一段「控制字节 + 数字 + 控制字节」就会被当成占位：序号越界是
# IndexError 把整趟导出打抛，落在范围内则会把一段正文悄悄换成别的转义字符。C0 控制符
# 本来也不合法于 XHTML / EPUB。这条把崩点、不悄悄替换、干净稿回归、\t 与换行保留、
# 以及真打一个 EPUB 验成品里零控制字节，五面一起钉住。纯离线。
run "导出占位记号收口（正文里的控制字节不撞渲染占位 / 不越界 / 成品零控制符）" "$PY" tests/check_export_placeholder.py
# flomo 那条路（导入 → 一条笔记收成书 → 记忆画像）全是本地文件活：解析那份导出包
# （HTML 里套 div、图在 files/ 下）、同一条不重复入库、筛与标签用的是界面那同一把尺、
# 画像只由数字与标签算出（它是唯一会被反复喂给 Agent 的东西，带原文就是漏）。
# 笔记内容全部现编在 tests/flomo_fixture.py 里，用户的真实笔记一个字不进仓库。
run "flomo 便签后端（解析 / 去重 / 附件 / 收成书 / 画像）" "$PY" tests/check_flomo_notes.py
# 上面那条管「账本坏了当空读」，管不到「账本里某个字段写脏」。notes.json 用户会手工
# 编辑、旧版本也落过别的形状：words 写成 "abc" 让 stats 与画像抛 ValueError，ts 写成
# "oops" 让列表页排序抛穿请求，tags 写成裸字符串会被逐字拆成「读」「书」两个假标签。
# 这条把坏形状逐样喂进 load，要求列表 / 标签 / 画像 / 收成书 / 再导入都不抛，且认得出
# 的数字照认、条目不丢。纯离线，全在临时目录里跑。
run "flomo 脏账本兜底（脏字段不翻车 / 认得出照认 / 条目不丢）" "$PY" tests/check_flomo_dirty.py
# 脏的形状之外还有脏的**码点**：便签正文来自 POST 请求体的 JSON（`fetch` 送来的字符串
# 里可以有落单的代理转义），账本却是 ensure_ascii=False 落盘的 —— 一个落单的代理码点
# 就能让手记在 memo_of 算 id 的 encode 上抛，还会让盘上已经躺着它的账本从此只读：
# 打标签 / 忘一条 / 清空全在 json.dump 上抛穿，连 CSV 导出也跟着废。这条拿
# drop_lone_surrogates 一把尺子钉住出生 / 读回 / 落盘 / CSV 四个口子，并真发一次
# POST /api/flomo/notes。纯离线，沙盒在系统临时目录。
run "flomo 便签半个代理码点（手记 / 脏账本 / 清空 / CSV 不整屏卡住）" "$PY" tests/check_flomo_surrogate.py
# 写作那一格是同一个故事的另一本账：meta.json（rev / words / created / updated / 灵感角度）
# 与 _books.json（每本书的 created / updated / chapters）同样会被手工编辑、被旧版本写成别的
# 形状，而读它们的地方原来一律 `int(v or 0)`、`list(v or [])` 直读直用：一个 "很多" 让写作屏
# 左栏整栏加载不出来，"abc" 让 save() 第一行就抛 —— 用户敲完点保存，回的是「存不下」，一个字
# 没落；裸字符串 sparks 还会被逐字拆成「灵」「感」「角」「度」再写回去。这条要求列表 / 单篇 /
# 书树 / 存盘 / 快照 / 挪章都不抛、认得出的数字照认、裸字符串收成一个元素、条目不丢，并把
# rev 冲突协议与「存一次盘账本自己修好」一起钉住。纯离线，全在临时沙盒书库里跑。
run "写作脏账本兜底（脏字段不翻车 / 存得进 / 裸字符串不拆字）" "$PY" tests/check_writer_dirty.py
# 写作导出这条路上，scale 是地址来的字符串（`?scale=abc` / `1e400` / `nan`），
# 原来直接 `int(opts.get("scale") or 2)`：脏值当场抛，路由的兜底把它接住之后回的是
# 500 加一句 Python 报错原文（「invalid literal for int() with base 10: 'abc'」）。
# 界面上只有 1/2/3 三档，脏值该当没传：回缺省 2、再夹在 1..3 —— 与 _qint 同一把尺子。
# 离线的写法是在 writer_export() 这一层下探针（那条 HTTP 路前面有 chromium_ready()
# 前置拦，门禁里到不了 scale 这一段），不起浏览器、不碰真实书库。
run "写作导出参数（脏 scale 当没传 / 越界夹住 / 一处接线）" "$PY" tests/check_writer_export_args.py
# 上一条管「脏字段折不成数」，这一条管「脏字符写不进 utf-8」。写作这一格的正文按
# utf-8 写、meta.json 与 _books.json 按 ensure_ascii=False 写，而浏览器把 JS 字符串
# 拦腰截断时会留下落单的半个代理码点（json.loads 收得下 "\ud83d"）：老代码在
# `f.write(body)` / `json.dump(meta, ...)` 那一步当场 UnicodeEncodeError ——
# 用户写了一大段、一按保存就没了，回执本身也发不出去。mindmap / board / person /
# anna 早都按同一口径收了口，这一条把写作这一格补齐。纯离线，临时沙盒书库。
run "写作坏字符（半个代理码点不把保存打成失败 / emoji 不动 / 旧账自愈）" "$PY" tests/check_writer_utf8.py
# 转写这条线 1.0.8 整体云化：本地那套（mlx-whisper / faster-whisper、权重缓存、镜像回退）
# 全砍了，只留云端一路。条约是「引擎恒为 cloud、老配置里的 mlx / faster 静默归正不报错、
# 地址与 Key 存得下也读得回、回显只给末四位不许吐明文」，外加「本地模型那一问必须明说移除
# 并指路」和跨文件接线（video_note / bootstrap / build 脚本 / 壳 / 界面都别留本地残渣）。
# 纯逻辑 + 源码走查，配置读写全在临时沙盒，不联网。归静态段，--fast 也得带上。
run "转写云端化（引擎挑选 / 配置存取 / 地址补齐 / 接线）" "$PY" tests/check_asr_cloud.py
# 划线笔记导 CSV 是给 flomo 喂的：字节格式必须一比一对上官方模板（UTF-8 BOM + content,
# created_at 表头 + CRLF + YYYY-MM-DD HH:MM:SS），文件名与落点走「设置里选的导出文件夹、
# 没选就退回 downloads」，重名加序号绝不覆盖，外加「后端只有一条写盘路、前端勾选才放行」
# 的接线。纯函数 + 源码走查，不联网。归静态段。
run "划线笔记导出 CSV（字节格式 / 落盘位置 / 重名不覆盖 / 接线）" "$PY" tests/check_notes_csv.py
# 账本里的数字收口原先只写 `except (TypeError, ValueError)`，漏了 `OverflowError`。而
# `json` 认 `Infinity` / `-Infinity` / `NaN`，也认 `1e400`（inf）与一长串数字（大整数）——
# 这些都是合法 JSON，会落进本机账本：一条 `"ts": Infinity` 就让便签 `load()` 整份抛，
# `notes_csv` 的越界 `at` 让整份导出写不出来，安娜账本被当空、视频栏整栏 500。这条把
# 同一组越界值喂进每个收口函数与每条产品级读路径，要求不抛、坏值回安全默认、认得出的
# 值口径不改、坏一条不带走整份。纯离线，全在临时目录里跑。
run "越界数字收口（Infinity / 1e400 / 大整数不再穿出去）" "$PY" tests/check_dirty_numeric.py
# 导出 Markdown 是「笔记条目 → 一份能直接进阅读器 / Git / flomo 的稿子」那条路的唯一排版处，
# 它最会出的毛病是排版时把标点吃掉：引用行那句 `.rstrip("（）")` 会把刚拼上的右括号一起啃掉，
# 目录里标题为空时又只剩一个孤零零的左括号，两种都不报错、只是文件里悄悄缺一角。
# 三种标题（正常 / 对不上 / 空串）各看一遍引用行，外加空引用与坏引用不越位、划线段照旧、
# 落盘那份与内存里一字不差。纯函数 + 临时书目录，不 import 服务、不联网。归静态段。
run "划线笔记导出 Markdown（引用行括号 / 空引用 / 划线段 / 落盘）" "$PY" tests/check_notes_export.py
# 上面那条管「导出的排版」，这一条管「存得进、读得回」的那个口。它最容易出的不是
# 「没保存」，而是**一个坏字段把整份笔记带走**：`load_notes` 没挡 `"marks": null`
# （键在值不在的半截档，一打开就 500），`_normalize` 拿 `int()` 硬转外部写进来的
# 时间戳（"昨天" / "1728-10-14" 抛 ValueError，被路由兜成「存不下」，这一轮改的
# 别的笔记一起丢）。模板库满 200 条时旧代码 `items[:200]` 又把刚存的那条切在门外，
# 接口却回「模板存好了」。四类各钉一条，纯函数 + 临时目录，不 import 服务、不联网。
run "书本地笔记存盘（坏档读得开 / 坏时间戳不丢整份 / 坏形状当空 / 模板顶最旧）" "$PY" tests/check_book_notes.py
# 上面那条钉的是「容器形状」（marks 是不是列表、有没有 id）。这一条补的是**字段级**：
# notes.json 里一条划线的 created_at 是「昨天」、ch 是数组、color 是数字时，读盘这一步
# 照旧过，脏值却穿到下游才炸 —— export_markdown 的排序在 `int < str` 上抛（导出报
# 「导出没做成」），`by_ch.setdefault(list)` / `note_tree(cut="chapter")` 抛 unhashable。
# 现在 load_notes 与 _normalize 共用 _mark_row / _entry_row 一把尺子：只收形状，不丢
# 条目、不补 id、认不出的时间戳回 0（不拿「现在」编）。另钉顶层 `1e400` 不许变成
# JSON 里的 `Infinity`（那会让界面 JSON.parse 整份失败）。纯函数 + 临时目录，全离线。
run "笔记读盘脏字段（混排时间戳 / 数组 ch / 越界数字不带走导出与导图）" "$PY" tests/check_notes_dirty_read.py
# 上面两条管的都是「盘上那份 JSON 的字段形状」。这一条管**写盘那一步的坏字符**：
# 前半条划线的正文里带一个落单的代理码点（界面把 emoji 拦腰截断就会剩它），
# `json.dump(..., ensure_ascii=False)` 把它交给 utf-8 编码器时当场 UnicodeEncodeError，
# /api/mynotes 兜成「存不下」—— 用户这一轮改的整份笔记没落盘、还留一个 .tmp。
# 现在 `_clip` 这个字符串字段的唯一入口统一洗掉半个码点（成对 emoji 不动、长度口径不动），
# 读 / 存 / 导出 / 导图 / 模板四路都过。纯函数 + 临时目录，全离线。
run "笔记坏字符存盘（半个代理码点不把整份笔记打成「存不下」）" "$PY" tests/check_notes_surrogate.py
# 维护那一套（卸载组件 / 清除数据）最要紧的不是「删得掉」而是「删不掉不该删的」：
# 书库、别人家的模型、别人家的浏览器、数据目录本身 —— 四条红线各钉一条。
# 全程在临时沙盒里跑，不碰真实的 cache / 书库 / HF 缓存 / playwright 缓存。
run "维护（白名单清理 / 四条红线 / 试算不删）" "$PY" tests/check_cleanup.py
# 封面这一轮的规矩是「剪藏用文章首图、视频用视频封面」，而它最容易出的事故不是没图，
# 是拿错图（页头 logo、站长头像当封面，满书架一张脸）和不该下的图去下（别人网页里写的
# og:image 指到内网，等于替用户去敲内网的门）。网络那层用记账的假 urlopen 顶掉，
# 于是「一次请求都没发出去」也断言得出来。全程临时沙盒，不联网。
run "封面规则（首图挑选 / 内网拦截 / 落盘与补取）" "$PY" tests/check_covers.py
# 云同步是唯一一条「把用户文件写到本机之外」的通路：配置怎么合、密码会不会回显、
# 路径怎么拼（WebDAV 建目录曾经拼出 …/files/u//guizang，和 PUT 的落点不是同一处）、
# 传输失败怎么收尾。真实请求由记账的假 _http 顶掉，一次网络都不出，归静态段。
run "云同步（配置合并 / 密钥不回显 / 路径拼法 / 上传取回收尾）" "$PY" tests/check_sync.py
# 上面那条量的是配置与路径；这一条单独量「换来的令牌有没有写回调用方那份配置」。
# `_norm` 每次新建一份 dict，而界面跑完是把归一化那份存回磁盘 —— 不写回就等于把
# 旧令牌盖回去：每次同步都白换一遍令牌，微软轮换 refresh_token 之后旧的那枚迟早
# 失效，用户看到的是「明明授权过，过阵子又要重新授权」。假 _http 记账，全离线。
run "云同步令牌回写（刷新落盘 / 未刷新不动 / 失败不毁旧令牌）" "$PY" tests/check_sync_tokens.py
# 上面两条管配置、路径与令牌，这一条单独管「取回的字节怎么落到盘上」。云同步的
# 规则是「本地有就上传、本地没有才去取」，所以一次写到一半的取回最致命：目标位置
# 留下一个长度不对却「存在」的文件，下一趟同步就把它传回云端，把好的那份盖掉。
# 这里钉两件事：取回必须「先写 .part 再原子改名」，且 .part / .tmp 这类还没写完的
# 文件一律不进同步清单（断线的残留、写作稿正在写的中间产物都不许上云）。假 _http。
run "云同步取回落盘（半截不落目标 / .part 不上传 / 断线可恢复）" "$PY" tests/check_sync_atomic.py
# 上面三条都在讲「我们自己发的请求对不对」，这一条管「对方回的东西是什么形状」。
# 设备码 / 令牌 / 上传会话这几段 JSON 全都来自微软那一头，中间还可能夹一层反向代理
# 或门户认证页（回 HTML）；旧读法 `json.loads(...).get(...)` 遇到 HTML 抛
# JSONDecodeError、遇到 JSON 数组抛 AttributeError，而 /api/sync/onedrive/start 与
# /poll 两条 POST 没有任何兜底 —— 异常穿出 do_POST 就是连接被掐，界面「点了授权没反应」。
# 收口是 sync._json_obj，四条读口逐个喂坏响应：不抛、坏响应不当成功、好响应一个字不改。
run "云同步网络 JSON 形状（HTML / 数组 / 脏数字不甩连接，也不当成功）" "$PY" tests/check_sync_json.py
# 图片下载这一条最容易出的不是「下不来」，是「下了一半还被当成下好了」：旧代码
# `open(fp,"wb")` 先清空再写，中断留下的半截图只要过 1KB 就被跳过，重下按钮再点
# 也不会碰它 —— 一张裂图钉死。现在落盘走「同目录 .part + os.replace」、跳过判断
# 还要认文件头，raw/*.json 的坏记录也只跳过不炸。导入本模块不该再改全局 socket。
# 剪藏正文里的表格是最容易「悄悄抄坏」的一块：格子里带链接时锚文字记两遍、带脚本时
# 脚本原文被当正文抄进来、只剩一行的表格整块丢掉、格子里字面量恰好撞上内部占位符
# 「@@T1@@」被后一轮替换再吃一遍。这一条把 HTML 喂进 clip_article 真那条转换路，只读
# 内存、不联网、不开浏览器，四类毛病各钉一条，外加一整页的端到端。归静态段。
run "剪藏表格（格内链接不重复 / 脚本不抄入 / 单行表不丢 / 占位符不串味）" "$PY" tests/check_clip_md.py
# AI 小结这一轮的口径是「一个入口、两种模式、三种内容」，规矩全在 ai_sum.py：
# 哪一路算哪种、读书一次只吃当前这一章、超长怎么掐、模型回得不像导图时怎么兜、
# 一个模式一份缓存互不覆盖。再加一条这轮新钉的：上游半路拔线必须和「答完了」分得开
# —— 分不清就会把半截当成品存盘，把上一次那份好的覆盖掉。全程离线，假流直接喂字节行。
run "AI 小结（范围 / 提示词 / 导图兜底 / 缓存 / 断流）" "$PY" tests/check_ai_sum.py
# 上一条管「兜底图长得对不对」，这一条管「模型真给了图时别把它扔掉」：模型爱在 JSON
# 前写一句带花括号的话（`好的，先看 {主线}，结果如下：`），旧抠取只认第一对配平的花
# 括号，解不动就直接放弃，后面那段完好的导图被判成「模型没给出导图」。这一条把
# 「逐段试到第一段解得开的对象」钉死，并守住半截不闭合不硬猜、字符串里的花括号不捣乱。
run "AI 小结 JSON 抠取（花括号开场白不吃掉后面的真导图）" "$PY" tests/check_ai_sum_json.py
# 上一条管「模型给的 JSON 抠不抠得出来」，这一条管「抠出来的正文存不存得下去」：
# 模型那一边是外站 JSON，`"\ud800"` 这种转义 `json.loads` 收得下、utf-8 却编不出去，
# 而 save_result 的规矩是「写不动也不抛、回空串」—— 于是屏幕上那段小结好好的（响应
# 编码那道口子会顺手摘掉坏字符），落进 _ai/ 的却一个字都没有，重开这一页就没了。
# 这一条把「落盘前先摘掉落单的半个代理码点、真 emoji 与 ZWJ 一个字不动」钉住，并带
# 一条「不清就写当场 UnicodeEncodeError」的对照。纯函数 + 临时沙盒，归静态段。
run "AI 小结坏字符（半个代理码点不再让小结悄悄存不下去）" "$PY" tests/check_ai_sum_surrogate.py
# Markdown 里 `# 注释` 和 `# 标题` 只差「在不在围栏代码块里」，凡按 `^#+ ` 认结构的地方
# 漏了这一层就会把代码当章节讲。这一条把尺子（两串围栏 / 收尾同种且不短于开头 / 未收尾
# 吃到文末 / 行号仍按原文）与两处接线（ai_sum 的兜底导图、book_notes 的条目子分支）一起钉住。
run "围栏代码块（# 注释不当 # 标题 / 一把尺子两处接线）" "$PY" tests/check_md_fence.py
# 上一条钉住「哪些行在围栏里」，这一条钉住「围栏这件事只有一份实现」。原先 writer 与
# book_import 各抄了一份状态机，其中 md_fence 漏了 CommonMark 的一条：**反引号起栏的
# info string 里不许再有反引号**（` ``` a`b ` 只是普通正文）。于是同一行在「导入切章」
# 眼里是正文、在「大纲 / 导图」眼里却成了开栏，把后面真正的标题一路吞进代码块 ——
# 大纲里那一节凭空消失，且不报错。这一条把四处接线钉成逐行等价，并留一面「旧尺子确实
# 会吞标题」的镜子。纯逻辑、不联网、不开浏览器，归静态段。
run "围栏尺子一致性（info string 里的反引号 / 四处接线逐行等价 / 对照与回归）" "$PY" tests/check_fence_rulers.py
# 小 Agent 这一轮查的是「形状」而不是「配置」：兼容 OpenAI 的称呼听着统一，各家回话却
# 有好几副面孔（收了 stream:true 仍整段 JSON、SSE 给 message 不给 delta、stream:false
# 却按 SSE 回来）。这三种在旧代码里分别被当成「断线」「一个字都没回」「Expecting value」，
# 满屏好好的答案被判成断线。这一条按形状逐条钉死，配置读写与三个问答口接线也一并守住。
run "小 Agent（地址补全 / 流式形状 / 非流式抠字 / 配置 / 接线）" "$PY" tests/check_agent.py
# 两张热力图上那些数字的规矩全在 activity.py：一次心跳夹 300 秒（浏览器冻结、合盖睡醒会
# 报一个巨大间隔，不夹就是一根通天柱）、单日封顶 12 小时、缺的日子补 0 而不是跳过（跳一格
# 整张图就错位）、连续天数对「今天还没开始」宽容。纯函数不算盘不碰 HTTP，所以归静态段。
run "时长账（夹 / 封顶 / 补零 / 连续 / 色阶 / 归档）" "$PY" tests/check_activity.py
# 本机阅读时长这本老账原先直读直用：days 里一个 "abc"、books 里一个字符串 seconds，
# 就能让 /api/readstat 与阅读时每十几秒一次的 /api/readstat/tick 抛异常（同步会把远端
# 那份原样落盘，用户也可能手工编辑）。activity 早就按「认不出的当空账」洗过，这一条把
# 对齐：脏值丢掉、能认的数字照样认。只 import 服务模块读那几个纯函数、配置全指进临时
# 沙盒，不起服务、不开浏览器、不联网，所以归静态段。
run "本机阅读时长（脏账不崩 / 好账不改 / 归档 / 清零）" "$PY" tests/check_readstat.py
# 阅读计划那一套也是纯逻辑：页数按正文字数折算（跟字号、窗口宽窄无关）、进度只增不减
# （翻回去查个词不该把「今天读了 30 页」缩成 8 页）、跨天 / 跨周记账、读完把当时的
# 目标与起止留进档案（改新计划也冲不掉）。时间由调用方传，所以不用起浏览器也验得实。
run "阅读计划（页数折算 / 只增不减 / 跨天记账 / 读完留档）" "$PY" tests/check_plan.py
# 上一条管的是「算得对不对」，这一条管的是「脏读数进来会不会把线弄断」：界面按心跳
# 上报读到哪，`at` 可能是字符串 / NaN / 半个对象；`pages_at` 原来只兜了 `frac` 没兜
# `at`，路由那层也没有 try，于是一发脏值就是 500 级别的「连接被甩」——计划停在原地，
# 前端什么都收不到。纯函数段钉「认不出的章序号一律当第 0 章、不许当读完」，真机段拿
# `at: "abc"` 打一次 /api/plan，要回 200 + JSON 且书没被记成读完。归静态段。
run "阅读计划脏读数（认不出的章序号当第 0 章 / 真机不断线）" "$PY" tests/check_plan_input.py
# 文案这一条守的是「界面上不再有 AI 味」：语气词、波浪号尾巴（「好的~」）、感叹号、emoji
# 一律不许出现在散文里 —— 唯一放行的是剪藏正文的清洗正则（它得把别人的 emoji 滤掉）。
# 顺带钉住「设置那一段的每条提示都写完整句子、以句号收」，以及个人主界面那颗「设置」还在。
run "界面文案（零语气词 / 零俏皮号 / 零 emoji / 提示成句）" "$PY" tests/check_copy.py
# 「文字规格」这一类毛病里，有一半不用起浏览器也能钉死：同一样东西写在好几处、数值还对不上
# （毛玻璃模糊值 CSS 写 24、设置弹窗却写死 26，JS 一跑滑杆就从 26 跳到 24）。这一条只读
# ui.html 的源码，把「外观缺省值只有一处真相」和「按钮字一律 nowrap、只有章节标题开回来」
# 两条规矩按成会失败的断言 —— 真机上量得出几何的另有一半，见 check_overflow.py。
run "文字与外观规格（缺省值一处真相 / 折行规矩）" "$PY" tests/check_spec.py
# 1.1.0：外观从「固定色晕」放开成「预设 + 自定义」。这一条守默认色没被顺手换、
# 自定义真有「两三个色点 + 角度」、配置落盘在同一条外观设置里、梯度本体只在 CSS。
run "氛围渐变（默认仍老色 / 预设四套 / 自定义色点与角度 / 一处落盘）" "$PY" tests/check_ambient.py
# 推荐这一屏用户只提了一条：只出现六本书，六本之上不许再横任何字。两个落点都能
# 静态钉死 —— 数据那趟的早退必须真的切到 6（官方接口偶尔回十几本），渲染那趟
# 必须传空页头（原来那行「为我推荐 / 一次 N 本」就横在封面上）。只读 ui.html。
run "推荐页（只六本 / 不铺页头 / 骨架同数）" "$PY" tests/check_pick.py
# 内置浏览器是「替用户去访问外站」的第二条通路（第一条是剪藏）。这一条钉的重点不是
# 「取不取得到」，而是「不该取的坚决不取」：本机 / 内网 / file: / javascript: 一律拒绝，
# 页面补 <base> 与点链接回调脚本、非网页原样透传、失败铺自己那页人话。取与改拆成纯函数，
# 不联网也验得实。
run "内置浏览器（内网拦截 / base 与跳转脚本 / 透传 / 接线）" "$PY" tests/check_web_browse.py
# 上面那两条管的是「取什么」，这一条管的是「取回来怎么解」。`charset=…` 是对方站点声明的，
# 一个手滑的名字（实测有页面写 `charset=x-unknown`）或一个压根不是文本编码的名字会让
# bytes.decode 抛 LookupError —— `errors="replace"` 管字节不管名字，于是剪藏冒出运行期错误、
# 内置浏览器那一屏直接 500。两条取网页的路现在都过 clip_article.safe_charset 收口；假 urlopen /
# 假 opener 顶掉网络，坏名字、好名字、伪编码与响应头注入形状逐条钉住。全离线。
run "取网页的字符集口径（坏 charset 不抛 / 真编码照旧 / 回显只带验过的名字）" "$PY" tests/check_page_charset.py
# 上一条收的是剪藏与内置浏览器两条路；平台解析取 X 的 syndication JSON 走的是自己的
# `fetch_json`（绕开了 clip_article.fetch 那条「必须有 <」的检查），charset 那句一直没被
# 尺子量过 —— 一份假 urlopen 顶掉网络，坏名字 / 没声明 / 真 GBK / 解不出的字节逐条钉住，
# 外加 X 端到端与 extract_or_none 不被异常穿透。全离线。
run "平台解析的字符集口径（X 的 JSON 坏 charset 不抛 / 真编码照旧 / 端到端）" "$PY" tests/check_charset_json.py
# 上面那条钉的是「内网地址不许碰」这件事本身，这一条钉的是「换身写法也不许溜进来」：
# 2130706433 / 0x7f000001 / 0177.0.0.1 这些老式 IPv4 写法，以及 [::ffff:127.0.0.1] /
# [::] / [fc00::1] 这些 IPv6 段，浏览器和 getaddrinfo 都认、旧正则全不认。四条替用户
# 发请求的路（剪藏 / 封面 / 内置浏览器 / 平台解析）走的是同一道门，逐条钉住。
run "地址护栏（老式 IP 写法 / IPv6 内网段 / 跳转）" "$PY" tests/check_url_guard.py
# 上面这条钉的是「该拒的地址有没有拒掉」，这一条钉的是另一面：护栏收到根本没打算认的
# 输入（负数段的 `-1.0.0.1`、超长数字、下划线分隔……）也只许回放行 / 拒绝，绝不许抛。
# 根子是 `_legacy_ipv4` 用 `int()` 收段，负数收得下、拼出来是负的，交给 `ipaddress`
# 当场抛 —— 内置浏览器那条路只兜 Blocked，于是敲这种地址不是回人话而是把连接甩掉；
# 页面里写死的 `og:image` 带这种主机名会把整次剪藏带崩。纯函数 + 四条路入口，不联网。
run "地址护栏反面输入（认不出的数值主机名不把护栏打抛）" "$PY" tests/check_url_guard_neg.py
# 上一条管「护栏收到认不出的主机名不抛」，这一条管「地址本身就解不出来」：`urlparse` 碰到
# 半个 IPv6 方括号（`http://[::1`、`https://[`）自己会抛 ValueError，而内置浏览器那条路
# 只兜 Blocked、`/api/browse` 也没有第二道 try —— 异常穿到 BaseHTTPRequestHandler 手上
# 就是把连接关掉。用户在地址栏敲错一个方括号、或外站页面里的链接拼成半个方括号，看到的
# 不是「这一页没取回来」那张自带错误页，而是 iframe 一片空白。剪藏那份早收口了
# （clip_article.url_block_reason 的 except ValueError），这一条把内置浏览器补齐，三层都钉：
# 纯函数（Blocked 带人话）/ 对照 + 源码接线 + 跳转守卫 / in-process 真 Handler 裸 HTTP GET。
run "内置浏览器地址收口（半个 IPv6 方括号回一页人话，不是空响应）" "$PY" tests/check_browse_url.py
# 上一条把内置浏览器补齐了，注释里写「剪藏那份早收口了」—— 收口的只有 url_block_reason
# 一个函数，真正拿地址的 clip_article.fetch / first_image 与 web_parse.platform_of / _guard
# 各自又裸 urlparse 了一遍：粘一个 `http://[::1` 进来回的是 `Invalid IPv6 URL` 这句英文
# 运行期错误；一页正文里只要夹一张拼错的图（`<img src="http://a[b">`），挑封面那一步就把
# 整次剪藏带走。这一条按同一处收口（clip_article.split_url 解不出来回 None）逐条钉住：
# 要拒绝的拒绝、要跳过的跳过、要不认识的回不认识，正常地址与正常挑图规则一个不动机。
run "剪藏地址收口（半个 IPv6 方括号不把取页 / 首图 / 认站打抛）" "$PY" tests/check_clip_url_guard.py
# 上一条钉的是「粘进来的地址」，内置浏览器那一跳也自带检查；但剪藏 / 平台解析 / 订阅 /
# 插图 / 封面这五条取页路都是裸 urlopen —— 默认 opener 跟着 Location 走、不再问一次新地址
# 能不能去，于是外站 302 到 127.0.0.2 就把内网正文当文章取回来了（实测内网真被敲到）。
# 这一条起两台本机临时服务（外面 / 内网），看内网那台到底被敲了没有，并钉住「公网跳转照走」。
run "跳转护栏（302 到内网不算数 / 五条取页路都过闸 / 公网照走）" "$PY" tests/check_url_redirect.py
# 用户报「前几个页面正常，突然出现一个左右变动极大的」：根子是按页换 main 的宽度
# （书架 1120 / 宽页 1440）＋ 有 / 无滚动条差一条宽。这一条静态钉死「全站只有一块舞台」：
# --main-w 一处定义、main 的 max-width 只认它、任何按页给 main 另开宽度的口子都判失败，
# 顺带守住滚动条常驻槽位与「视图常驻、只挪 hidden」这两条不闪的前提。
run "切页一致性（一块舞台 / 无横跳 / 常驻视图）" "$PY" tests/check_pane_uniform.py
# 叠卡快速切书那条 bug（R4-4）：常驻 will-change 堆合成层、applyStack 无条件重刷整台卡、
# 批尾每翻一步同步重铺 —— 这三条都随书量放大成卡顿，且都能在源码里一眼钉死。只读 ui.html。
run "叠卡性能（只在翻动提层 / 位置没变不碰卡 / 批尾补卡去抖）" "$PY" tests/check_stack_perf.py
# 划线笔记那一屏改成书籍卡片瀑布流，并把「取书」时在本机划的线也并进来（R4-6）。
# 前端读源码钉瀑布流版式与两个来源的合并、后端直接落一份 notes.json 再读回来，全离线。
run "划线笔记瀑布流（书卡版式 / 微信 + 取书合并 / 同一套功能）" "$PY" tests/check_notebooks.py
# 随机漫步也要能抽本机（取书）的读书笔记，而不只是微信读书索引（R4-7）。
# 后端收本机 notes.json / 三个路由认 local_count 与 src，前端三档 + 分色，全离线。
run "随机漫步（微信 + 取书两路 / src 档位 / 没索引也能玩）" "$PY" tests/check_random_local.py
# 上面两条都吃 cache/notes_index.json。这份账本整份重建与单本增量合并交替落盘，
# 用户也会手工编辑、旧版本落过别的形状，而 load 原先只验顶层 items 是不是表 ——
# 索引里混进一条裸字符串 / null，划线搜索、卡片回顾、MCP 摘要就在 x.get(...) 上当场抛。
# 这一条把脏条目逐样喂进去（外加半截 JSON / books 写成对象），要求认得出的照留、
# 认不出的整条丢，四条下游用法都不抛；再把重建 / 合并两条落盘路径走一遍，确认
# 写出来的是好 JSON、不留半截临时件、写失败时旧索引一个字不动。源码走查一把钉住
# 「索引落盘只有 _write_json 一条路」。全离线、全在临时沙盒里跑。
run "划线索引脏账本（脏条目不翻车 / 重建与合并走原子写 / 不丢旧账）" "$PY" tests/check_notes_index_dirty.py
# 控件质感与交互反馈（R4-5）：动效令牌、通用按压、键盘焦点、落点涟漪、卡片入场、
# 成功回弹，外加「减少动效」与「反馈不许动版面」两条硬规矩。只读 ui.html，全离线。
run "控件质感与交互反馈（令牌 / 按压 / 涟漪 / 入场 / 回弹）" "$PY" tests/check_ui_polish.py
# 个人资料（头像 / 名字 / 简介）此前一条测试都没有，而它守的是两件容易悄悄坏的事：
# 头像「按魔数认类型」返回的那几个字符串要与 ui_server 的放行表成对，否则界面上
# 就是一张打不开的破图；名字与简介要在存的时候就洗干净 —— 从别处粘来的零宽空格、
# BOM、双向文本控制符会让名字显示成空的、甚至整行倒排，但 ZWNJ / ZWJ 是 emoji 的
# 组成笔画，必须留着（洗过头会把 🧑‍💻 拆成两个人）。纯函数 + 源码走查，全离线，归静态段。
run "个人资料（头像魔数 / 名字清洗 / 一处真相）" "$PY" tests/check_profile.py
# 上面那条管到「零宽 / 双向控制符」为止，但还有一类脏字符它拦不住：浏览器把 JS 字符串
# 拦腰截断时留下的**落单代理码点**（U+D800–U+DFFF）。json.loads 收得下它，config.json
# 却是 ensure_ascii=False 写的 —— json.dump 一碰到就抛 UnicodeEncodeError，用户看到的是
# 「改个名字没保存上」，cache/ 里还多一个半截 .tmp。这一条把「洗得掉、真 emoji 不误伤、
# 洗完的值真能落盘」三面都钉住（含一条「没洗的坏值确实写不进」的对照，防门禁空转）。
run "个人资料里的坏字符（半个代理码点不许把保存打成失败）" "$PY" tests/check_profile_utf8.py
# 插图并发下载（download_images.py）是界面「补图」按钮背后真正跑的脚本，此前一条测试都没有。
# 它读磁盘上的 raw/*.json（可能是半截的、被手改过的），所以这一条钉的是「坏清单只跳过不牵连」
# 「文件名带 ../ 一律拒收，一条字节都不许落到书库外面」「先写 .part 再改名，不留半截图」，
# 外加「下载时认多大、下次判已下好就用同一把尺子」——原来两处一个 >500 一个 >1000，
# 700 字节的小图每补一次图就被重新敲一遍微信图床。网络层用记账的假 urlopen 顶掉，全离线。
# 同理导入本模块不该再改全局 socket（force_ipv4 挪进 main()），raw/*.json 的坏记录只跳过不炸。
run "插图并发下载（坏清单 / 防穿越 / 原子落盘 / 判重口径 / 接线）" "$PY" tests/check_download_images.py
# 上面那条钉住了「../ 穿越」，但脏文件名还有第二类：NUL / 别的控制字符 / 半个代理码点
# （raw/*.json 是扒来的外部数据，`file` 字段什么都可能是）。文件系统对这三种名字抛的是
# ValueError / UnicodeEncodeError，不是 OSError —— 而 is_complete 只 except OSError、
# download_one 又在 try 之外调它，主流程再在 fut.result() 把异常取回来，于是**一条**脏
# 记录带走整批图。这一条把 safe_target 的收窄、is_complete 的兜底、collect_tasks 的
# 「只跳一条」和 download_one 的「坏路径只回 fail」一起钉住，含「这些名字确实过不了
# 文件系统」的对照，防护栏空转。全离线，假 urlopen 顶掉网络。
run "插图文件名脏值（NUL / 控制符 / 半个代理码点只跳一条，不带走整批）" "$PY" tests/check_download_dirty.py
# 加书架（shelf_add.py）是少数几个真去改微信那边状态的动作，此前也没有测试。这一条
# 不联网、不开浏览器，钉的是两件离线也验得实的事：书号尺子四个入口必须是同一把
# （界面 / 本地服务 / MCP 都写 5–20 位，shelf_add.py 原来写 5–12，且 Python 的 \d 连
# 全角数字都算数字），以及接口回的成败数不许把 1 本当 3 本（`1 == True` 那个老坑）。
# playwright 只在缺它时用占位模块顶上，纯函数照验。
# 另半段是「只剩一把尺子」：界面（ui.html / MCP 适配器 / ui_server.py）曾经放到 20 位、
# 子进程 shelf_add.py 只到 12 位，13–20 位的书在界面上看着能加、点下去被引擎判成
# 「没有可用的书籍编号」。现在 Python 这侧只留 book_ids.py 一份，界面那两处是 JS / HTML
# 接不过来，就按同一组数字逐处比对。纯逻辑 + 源码走查，全离线。
run "加书架口径（书号四处同尺 / 成功数不虚报 / 13–20 位 / 接线）" "$PY" tests/check_shelf_add.py
# 上一条比的是「几处写的都是不是 5–20」这组数字，比不出「什么算数字」：Python 的
# `\d` 默认连全角（１２３４５）与阿拉伯-印度（١٢٣٤٥）数字都算，而界面那几处是 JS 的
# /^\d{5,20}$/（ECMAScript 的 `\d` 只认 0–9）。少了 re.ASCII，Python 这把尺子就比界面
# 宽一档：全角串在界面上被拒、从 /api/shelf/add 或 shelf_add.py 进来却被当短 id 发给
# 加书架接口，换一个 succ:0 回来 —— 用户看到的又是「点了没反应」。这一条把「只认 ASCII
# 的 0–9」按成会失败的断言，并留一条「不带旗子的同一 pattern 确实会收」的对照。
run "书城短 id 的 ASCII 口径（全角 / 阿拉伯-印度数字不算数，与界面同一把尺）" "$PY" tests/check_book_ids.py
# 登录态账本（cache/login_state.json）此前一条测试都没有，而它有三处读者：设置页、
# 壳、MCP 适配器，三处都按「logged_in 是布尔、checked_at 是秒数」直接算时间 ——
# 文件被手改过 / 是半截 / 是老形状的时候，它们印出来的就是「未连接 NaN月NaN日
# NaN:NaN」，看着像后端坏了。这一条钉住形状只有一处真相（脏值一律不猜）、落盘改成
# 原子替换（写失败不毁旧档、不留 .tmp），外加界面与 MCP 都兜住了「没有秒数」这一档。
# 全离线、不联网、不开浏览器、不需要 playwright。归静态段，--fast 也得带上。
run "登录态账本（形状一处真相 / 原子落盘 / 半截档 / 三处读者兜底）" "$PY" tests/check_login.py
# 只听回环不等于「只有归藏自己的页面打得进来」：别的网页发的简单请求，浏览器照样
# 从用户这台机器送到后端，而删书 / 清数据 / 起任务「做成了」就是攻击的全部。这一条
# 前半段打纯函数边界表（谁算同源、谁算不是浏览器），后半段自己起一个服务、用裸 HTTP
# 把浏览器那几种发法各来一遍，专验「跨站回了 403、账本也一个字没动」。不起浏览器，
# 归静态段，--fast 也得带上。
run "跨站写操作（判定边界 / 接线 / 真机拦得住且不误伤）" "$PY" tests/check_csrf.py
# 上一条的前提是「攻击者的页面」和归藏是两个源。DNS rebinding 拆掉这个前提：让外站域名
# 先指自己、再改指 127.0.0.1，浏览器就认为页面与后端同源（Origin 与 Host 都是那个域名、
# Sec-Fetch-Site: same-origin），cross_site 一路放行，跨源读取限制也不再管用 —— 账本读得走、
# 写操作也做得成。堵它只能看 Host 是不是本机那几个写法，与 shell/main.swift 的 isOutside()
# 同一把尺子。这一条钉 ①纯函数边界表 ②两个入口的接线 ③in-process 真 Handler 上按 rebinding
# 形状发的 GET / POST（外站 Host 403、账本一个字不动，回环 Host 照常 200）④两把尺子对表。
# 不起浏览器、不联网，归静态段，--fast 也得带上。
run "Host 头收口（rebinding 借不到 Host / 像本机的子域名也拒 / 与外壳一把尺子）" "$PY" tests/check_host_guard.py
# 查询串里的数字被手改坏（?tail=abc / ?n=abc）时，裸 int() 会穿出 do_GET 把连接掐断，
# 前端只看见 RemoteDisconnected —— 又是「点了没反应」那一种。这一条钉住 _qint 的收口
# 口径与四个调用点：纯函数 + 只读源码，不依赖浏览器，归静态段。
run "查询参数脏值（不崩 / 不越界 / 四个调用点收口）" "$PY" tests/check_query_args.py
# 查询串收完了，config.json 的**内层字段**还没收：`load_cfg()` 原先只保证顶层是对象，
# `{"export_dir": 5}` / `{"nav_order": 5}` 会一路穿到 `.strip()` 与 `for x in` 去。
# 读取点里最重的是 /api/state —— 界面每 2.6s 轮一次，坏一个字段就把响应抛穿、整页失联
# （书架 / 任务 / 设置一起没），不是某一格报错。这一条把每个已知键逐一写脏再全写脏，
# 要求当没设、八个读取者都不抛、干净配置一个字不改。纯函数 + 临时假配置，归静态段。
run "配置脏字段（内层形状不对不再让 /api/state 整页失联）" "$PY" tests/check_config_dirty.py
# 上一条把查询串里的数字收了，这一条收的是同一类脏值的另一半：请求体长度。`do_POST`
# 原来一行 `int(self.headers.get("Content-Length") or 0)`，正常浏览器永远发非负整数，
# 所以一直没露馅；坏客户端发 `abc` 时当场抛、连接被掐（客户端收到的是空响应），发 `-1`
# 时 `read(-1)` 会一直读到客户端关连接、把这条连接的线程挂在原地。收口是 ui_server._body_len：
# 认不出回 None，do_POST 回 400 并主动断连，绝不带着猜出来的长度往下走。纯函数 + 源码接线
# + in-process 真 Handler 的裸 HTTP 三面都钉，不联网、不碰真实数据，归静态段。
run "请求体长度脏值（abc / 负数不再断连或挂住，脏值回 400）" "$PY" tests/check_post_length.py
# 上面那三条把「顶层是畸形体」收了口：body 解析出来不是对象就统一当 `{}`。可字段级
# 还漏着一档 —— `{"books": 123}` / `{"items": 123}` 是合法 JSON，顶层是对象，类型却
# 不对。`/api/book_state` `/api/delete_many` `/api/flomo` `/api/notes_csv` 这四条没有
# 兜底 try/except，落到 `for b in ids:` / `items[:200]` 就是 TypeError，异常穿出
# `do_POST` 后连接被直接掐断，前端只看见 RemoteDisconnected。收口是 ui_server._body_list
# （与查询串的 _qint 同一把尺子：认不出当没传）。纯函数 + 源码接线 + 真机四个口裸 HTTP
# 打脏形状三面都钉，不起浏览器、不联网、不碰真实数据，归静态段。
run "请求体字段形状（books/items 写脏不再断连，正常列表照旧）" "$PY" tests/check_post_body_shape.py
# 还有一处同类脏值在更前面：请求行本身。`do_GET` / `do_POST` 第一行都是
# `urlparse(self.path)`，绝对形式的请求目标（代理就是这么发的）里带半个 IPv6 方括号
# （`http://[::1`）或 NFKC 认不出的字符，urlparse 会当场抛 ValueError；这行没有第二道
# try，异常穿到 BaseHTTPRequestHandler 就是把连接关掉，客户端收到的是空响应 —— 与上一条
# 的 `Content-Length: abc` 同一个症状。注意 http.server 只折裸 `//[::1`，绝对形式它不碰。
# 收口是 ui_server._req_target：解不出回 None，两个入口回 400 人话。纯函数 + 源码接线 +
# in-process 真 Handler 的裸 HTTP 三面都钉，不联网、不碰真实数据，归静态段。
run "请求行目标地址收口（绝对形式的坏方括号回 400，不是空响应）" "$PY" tests/check_request_target.py
# 账本被记事本 / 云同步「另存」过时文件头会多三个字节 BOM，原先按 utf-8 读会整份
# 解不动、被每个读取者兜底成空账 —— 然后下一次保存把空账写回去，手改前的数据就没了。
# 这一条给每本账做「干净 → 加 BOM → 再读」的往返：夹具本身得先读得出（不许两边都是空
# 蒙过去），加 BOM 后必须与干净那份逐字段相同。纯临时目录，不 import 服务，归静态段。
run "账本 BOM（带 BOM 的手改账本不再被当成空账）" "$PY" tests/check_ledger_bom.py
# 上面那条 check_query_args.py 只扫 do_GET 的方法体，而订阅 / 便签这两屏的翻页参数
# （limit / offset）是在 do_GET 之外的两个视图函数里裸 int(...) 收的。它们外面各有一个
# except Exception，所以症状不是断连而是「整页从列表变成 ok:false 的一条报错」——
# `?limit=abc` 会让这两屏一条都铺不出来，本该像 `?tail=abc` 那样当没传、回默认。
# 这一条把两处的 _qint 收口钉住：纯行为（脏值回默认 / 好值与夹取边界不变）+ 源码走查
# （全文件不许再有裸 int(q.get(...)) / int(one(...))）+ in-process 真机裸 HTTP。
run "订阅 / 便签翻页参数收口（脏 limit / offset 不再把整页读成错误）" "$PY" tests/check_view_paging.py
# 请求体里「本该是字符串」的字段还有一档没收：`((body or {}).get("state") or "").strip()`
# 里那句 `or ""` 只换得掉假值，数字 / 列表 / 对象照样穿过去、在 `.strip()` 当场抛
# AttributeError。这几个口（book_state 的 state、agent/chat 的 q、agent/quick 的 mode、
# config 的两个地址）都没有兜底 try/except，异常穿出 do_POST 后连接被直接关掉，前端
# 收到 RemoteDisconnected，表现又是「按下去没反应」。收口是 ui_server._body_str：只放行
# 真字符串，认不出当没写。纯函数 + 源码接线 + in-process 真 Handler 的裸 HTTP 三面都钉，
# 不联网、不碰真实数据，归静态段。
run "请求体字符串字段脏形状（state / q / mode / Key 写脏不再断连）" "$PY" tests/check_post_body_str.py
# 字符串 / 列表都收口了，但「小 Agent 对话」这两个口还剩半截：`/api/agent/chat` 的
# `history` 是老写法直接 `(history or [])[-8:]` 再 `.get(...)`（数字在切片处 TypeError，
# 数组混数字在 `.get` 处 AttributeError，字符串按字符逐条炸），`text` / `ctx` 则原样递给
# `agent_quick` / `agent_payload` 让里面的 `.strip()` 抛 —— 这几处都没有兜底 try/except，
# 异常穿出 do_POST 后连接被直接关掉，前端又是「按下去没反应」。收口是 ui_server._body_msgs
# （只放行 `[{"role","content(str)"}]`，脏单条丢掉、不是列表当没写）+ 既有的 `_body_str`。
# 纯函数 + 源码接线 + in-process 真 Handler（上游 agent_open 换成假的）三面都钉，
# 不连外网、不碰真实数据，归静态段。
run "小 Agent 请求体字段脏形状（history / text / ctx 写脏不再断连）" "$PY" tests/check_agent_body_shape.py
# 请求体、查询串、请求行都收过口了，还剩响应这一头：账本里的字符串可能带着落单的
# 半个代理码点（手改 / 云同步落盘 / 旧版本写回），`json.dumps(ensure_ascii=False)`
# 原样留在正文里，`_send` 的 `body.encode("utf-8")` 当场抛。异常穿出 do_GET /
# do_POST，BaseHTTPRequestHandler 不回 500、只是把连接关掉 —— 便签那一屏整个打不开，
# 而不是「某一条显示不全」。收口是 ui_server._encode_body：干净的路照旧 encode，
# 编不动时才按书库那把尺子摘掉落单代理码点，`_send` / `_chunk`（SSE）两个出口都走它。
# 纯函数 + 源码接线 + in-process 真 Handler 的裸 HTTP 三面都钉，不联网、不碰真实数据。
run "响应正文坏字符（半个代理码点不再把整屏甩成空响应）" "$PY" tests/check_response_utf8.py
# 上一条钉的是响应那一侧的编码口；这一条钉同一类坏字符落盘的这一侧。`_write_json`
# 是书架账 / config.json / 阅读时长 / 阅读计划 / 划线索引这些小账本**唯一**的写出口，
# 而账本不只本进程在写（手改 / 云同步落盘 / 旧版本写回）：config.json 的 `export_dir`
# 里一个落单的半个代理码点，旧写法在 `json.dump(ensure_ascii=False)` 当场 UnicodeEncodeError
# —— `/api/config` 点保存等不到回包（异常穿出 do_POST，连接被关），账本旁边还留一个
# 没人认领的 `.tmp`。收口与 `_encode_body` 同一把尺子：干净的路照旧 dump，编不动时才按
# book_layout.drop_lone_surrogates 摘掉落单坏码点重编；任何写失败先收 .tmp 再抛，旧档不动。
# 纯函数 + save_cfg 全链 + 源码接线，全在临时沙盒，不联网、不起服务，归静态段。
run "账本落盘坏字符（半个代理码点不再让保存等不到回包 / 不留 .tmp）" "$PY" tests/check_ledger_write.py
# 字段级脏形状还剩最后一档：数字。`_body_str` / `_body_list` 之外的订阅整理参数一直是
# 裸的 `int(body["index"])`。它们有 `feed_do` 的兜底 catch 接着，所以不是断连，而是
# `str(e)` 把 `invalid literal for int() with base 10: 'abc'` 原样弹给用户 ——
# 「点了挪顺序，弹一句 Python 报错」。收口是 ui_server._body_int（直接调 _qint，
# 与查询串同一把尺子：认不出当没写 / 越界夹回边界）；reorder 的 index 认不出时当面
# 回「没挪」，不落进「挪到末尾」。纯函数 + 源码 AST 走查 + in-process 真 Handler 三面
# 都钉，不联网、不碰真实数据，归静态段。
run "请求体数字字段脏形状（订阅整理的 index / limit / days 写脏不再回 Python 报错）" "$PY" tests/check_post_body_int.py
# /api/action 那条分派链（_dispatch）是上面「请求体字段收口」三条的盲区：源码体检只扫了
# do_POST 的方法体，分派器里的裸 `.strip()` / 裸 `module` / 裸 `add`、`ids` 一直在。脏形状
# 被 _action 的兜底接住后，递到界面上的是 `AttributeError: 'int' object has no attribute
# 'strip'` 这类解释器原文。这一条补上那段盲区 + 真机回话体检，静态段跑。
run "动作分派字段脏形状（/api/action 的 name / module / add / ids 写脏不再回 Python 报错）" "$PY" tests/check_action_dirty.py
# 上面收的都是「服务怎么应答脏输入」；这一条管的是**干活的人自己**：tools/pr_triage.py
# 是合 PR 之前用来摊开「哪些会撞、哪些疑似同一件事做了两遍」的预扫描。它平时不在提交流程
# 里跑，坏了没人知道 —— 直到某次漏掉该裁的重复、或把干净的一批报成一堆假冲突。判定全靠
# 三个纯函数（切 hunk / 区间相交 / 相似度），离线喂输入就能测；再拿本仓库的历史分支当夹具，
# 认一遍「已知的重复」与「已知的只是相邻」。
run "PR 预扫描（撞车预演 / 重复识别 / 区间判定）" "$PY" tests/check_pr_triage.py

if [ "$FAST" = "1" ]; then
  echo; echo "静态门禁：通过 $PASSED 项，失败 ${#FAILED[@]} 项"
  for f in "${FAILED[@]:-}"; do [ -n "$f" ] && echo "  ✗ $f"; done
  [ ${#FAILED[@]} -eq 0 ] || exit 1
  exit 0
fi

# ── 一次性沙盒服务 ──────────────────────────────────────────────
"$PY" tests/seed.py || { echo "书架铺不上，真机套件没法跑"; exit 1; }
PORT=$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
LOG="$SANDBOX/server.log"
"$PY" ui_server.py --port "$PORT" >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

# 服务自己会往后找空闲端口，所以地址要从它自己打印的那行里取，别赌。
ACTUAL=""
for _ in $(seq 1 120); do
  ACTUAL=$(sed -n 's|.*http://127\.0\.0\.1:\([0-9]*\).*|\1|p' "$LOG" | tail -1)
  if [ -n "$ACTUAL" ]; then
    GUIZANG_PROBE_PORT="$ACTUAL" "$PY" - <<'PY' && break
import json, os, sys, urllib.request
try:
    json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%s/api/state" % os.environ["GUIZANG_PROBE_PORT"], timeout=1).read())
except Exception:
    sys.exit(1)
PY
  fi
  kill -0 $SRV 2>/dev/null || { echo "服务进程已退出，日志："; cat "$LOG"; exit 1; }
  sleep 0.25
done

if [ -z "${ACTUAL:-}" ]; then
  echo "沙盒服务没起来，日志："; cat "$LOG"; exit 1
fi
BASE="http://127.0.0.1:$ACTUAL"
export GUIZANG_TEST_URL="$BASE"
echo "沙盒服务：$BASE   书库：$GUIZANG_BOOKS"

# ── 真机：Playwright + Chromium ─────────────────────────────────
# 每套件跑之前都重铺一次书架：套件之间不许互相看数据。
# 上一轮就栽在这儿 —— 取证书留了一本空书在书架上，下一套件的「第一张卡」点到它，
# 报出「没有可打包的内容」，看着像后端的错，其实是测试互相踩了。
fresh_shelf() { "$PY" tests/seed.py --force >/dev/null || exit 1; }

fresh_shelf; run "布局校验" "$PY" tests/ui_check.py "$BASE/"
# 悬停会不会把版面顶动：先把 CSSOM 里所有改「在流内」布局的 hover 规则逮出来，再真机
# hover 视频转写行与章节行、比对整屏坐标。上一轮的病根是 `.nacts` 用 max-height 从 0 撑开，
# 展开的是自己那一行、被顶下去的是下面所有行，鼠标顺着列表一划整栏都在跳。
fresh_shelf; run "悬停位移（hover 不许顶动版面）" "$PY" tests/check_hover_shift.py "$BASE"
# 切日间 / 夜间不许闪：全站几十个组件都带着给悬停用的补间，主题一变它们各按各的时长
# 从旧色补到新色，颜色对不齐就是那一下闪。这一条掐两个中途采样点，钉死「换肤那一刻
# 没有任何元素停在中间色上」，浅转深、深转浅各走一遍。
fresh_shelf; run "主题切换（换肤不许闪）" "$PY" tests/check_theme.py "$BASE"
# 文字不许戳出它所在的框：写作屏左栏那句长说明被 `flex:none` 顶着 max-content 宽度、
# 在稿子列里戳出 82px 压进正文。同一类写法（定宽标签不给 min-width:0）全站都
# 可能再犯，所以这一条把十三格入口 + 个人主界面 + 设置弹窗在三档宽度下都扫一遍。
fresh_shelf; run "文字溢出体检（文字不许戳出边框）" "$PY" tests/check_overflow.py "$BASE"
# 「顺」和「稳」也要能失败：这一套把当下的基线钉住 —— 页签图标在（否则浏览器撞 /favicon.ico
# 404）、十三格 + 个人主界面 + 设置弹窗走两遍零报错、暖态切屏主线程没有超过 50ms 的长活、
# 连点 30 下不许有一屏卡在半透明。以后谁往渲染里塞了重活，先被这一条拦下。
fresh_shelf; run "流畅与稳定（零报错 / 无长任务 / 连点不卡）" "$PY" tests/check_fluency.py "$BASE"
fresh_shelf; run "重新取书取证" "$PY" tests/check_refetch.py "$BASE"
fresh_shelf; run "视口回归" "$PY" tests/check_viewports.py "$BASE"
fresh_shelf; run "书架交互" "$PY" tests/check_shelf.py "$BASE"
# 剪藏 / 本地书架 / 视频这三格从「合并展示」改成各过各的：夹子和标签一格一份账，
# 书住在哪一格由磁盘说了算。这类改动最容易留「看不出来的错」（前端传的模块名是旧的、
# 芯片条重画一次就闪一下、同名函数把别处的渲染顶掉），所以一半 HTTP 契约一半真机点。
fresh_shelf; run "三格独立与分类账" "$PY" tests/check_module_shelf.py "$BASE"
# 导航这一排从「页面里手抄一份」改成由 /api/state 生成之后，唯一真相只剩后端那张表：
# 页面加一颗图标而后端漏一行、或后端加了入口而页面没画，都会让用户看见一个点不动的格子。
# 这一套一半对着源码比（图标表 == 名册）、一半真机点（长按拖拽换序、坞里勾显隐、
# 侧边栏开/边缘两种模式、指针靠左缘弹出），顺序与显隐改完都还原。
fresh_shelf; run "导航与侧边栏（名册对表 / 拖拽换序 / 入口显隐 / 边缘弹出）" "$PY" tests/check_nav.py "$BASE"
fresh_shelf; run "笔记编辑器" "$PY" tests/check_notes_editor.py "$BASE"
fresh_shelf; run "脑图与画板" "$PY" tests/check_board_map_ui.py "$BASE"
fresh_shelf; run "阅读器续读与大纲" "$PY" tests/check_reader_flow.py "$BASE"
# AI 小结这一层只有浏览器里才验得全：流式一段段蹦字、两种模式换芯片、画完导图把那层
# 收掉、存为笔记、复制、上游回 500、上游半路拔线。这一套自己起一个假的 completions
# 服务（不联网、不烧 token），四种收尾各演一遍。它替产品挡掉的三类真事故都写在
# docs/交接说明.md 的踩坑清单里：浮层盖住按钮、前端嗅错流类型、换书时旧浮层不收。
fresh_shelf; run "AI 小结真机（一个入口两种模式三种内容）" "$PY" tests/check_ai_summary_ui.py "$BASE"
fresh_shelf; run "订阅与视频两屏" "$PY" tests/check_media_views.py "$BASE"
# 搜索这一屏 1.0.6 起是两栏（左微信读书取书 / 右 Z-Library 下载），1.0.7 把右边那一栏
# 连着整个模块一起砍了 —— 这一套钉的就是「砍干净了」：开屏只给一句指路的提示、搜一次
# 只出一栏、整屏不再出现 Z-Library 字样、没 Key 时的说法仍指向「设置」。
# 同一套里还验「安娜的档案」那一栏：十三格导航、窗口没开/开着/已经不在了/没起来四种
# 状态各自的文案与按钮、下载夹补收端到端（样本落磁盘 → 收进书架 → 界面看得见 →
# 「看这本」跳到本地书架那张卡片）、清空记录只清账不动书、四档视口不溢出、零 console 报错。
fresh_shelf; run "搜书一栏与安娜的档案（状态 / 按钮 / 记录列表 / 窄窗）" "$PY" tests/check_anna_ui.py "$BASE"
# 上面那套是「文件先躺在下载夹里、再点补收」，跳过了浏览器那一段。中间那条线 —— download
# 事件、save_as 落盘、同名加时间戳不覆盖、泵一轮转成书 —— 只有真下一次文件才量得到，
# 所以这一套自己起一个本机夹具服务，让 Chromium 真点三下（一份 Markdown、一份不认的 .mobi、
# 同一个名字再来一次）。这里可以无头：不联网、不碰对方站点，验的是归藏自己那半条管道；
# 产品里那个窗口必须看得见是另一件事（站点在 TLS 层挡机器），归人工实测。
fresh_shelf; run "安娜的档案（真下载流：接住 / 转书 / 不覆盖 / 不重复入库）" "$PY" tests/check_anna_download.py
# 阅读计划一半是纯逻辑（见静态段的 check_plan.py），另一半只有真机点得出来：详情页那块
# 定 / 改 / 结束都在原地重画、阅读器底栏那条「还剩 N 页」跟着读到的位置走、读完变成
# 「已读完全书」并把往期记录留档。这一套自己清旧账、自己推尾声，不看别的套件脸色。
fresh_shelf; run "阅读计划（详情页定 / 改 / 结束 · 底栏实时读数 · 读完留档）" "$PY" tests/check_plan_ui.py "$BASE"
# 写作平台最要紧的不是「存得下」而是「不许悄悄覆盖」：存盘带版本号，对不上就当面停下问。
# 这一套前一半打接口（建/存/冲突/改名/快照/回退/四种导出），后一半开浏览器验这一屏点得动
# —— 六颗动作钮各开一次坞、第一句落下就把稿子建出来（不点「新建」也不该丢字）、
# 导出那屏的模板芯片没被图标钮那套 27px 压扁、素材那栏那句「原文只进提示词」真写在界面上。
fresh_shelf; run "写作平台（稿列 / 正文 / 六件事 / 导出模板）" "$PY" tests/check_writer_ui.py "$BASE"
# 转写工作台那一屏最容易出的事故是「戴着筛子保存」—— 界面只看得见筛出来的那几段，
# 写盘用的却是整本。这条只有真机点得出来，所以这一套一半是 HTTP 契约、一半是 Playwright，
# 两边都去磁盘上数段落。
fresh_shelf; run "转写工作台（筛 / 存 / 重建 / 导出 / 真机）" "$PY" tests/check_video_workbench.py "$BASE"
# MCP 那一层没有界面上的护栏：agent 只会「只发自己改的那几段」，而后端三条写口全是整本覆盖。
# 这一套把 56 个工具的清单对齐、以及「改一段不许丢整本 / 不带 canvas 不许抹平笔画」这两条
# 护栏钉成可失败的检查 —— 它要 node，所以归真机段（本机没 node 时明确 SKIP，不装绿）。
fresh_shelf; run "MCP 工具清单与三条新线" "$PY" tests/check_mcp_tools.py "$BASE"
# 上面那套挂在活服务上，验不到「后台没人、第一次点」—— 那是用户装完只开 agent 的默认处境。
# 这一套自己不起服务，让适配器去拉：解释器找错、可选包拖死服务、端口挪窝找不着、
# 跳过拉起直接抛 fetch failed，四个坑各钉一条，全都得在冷启动下跑通。
fresh_shelf; run "MCP 冷启动（服务没起时自己拉起来）" "$PY" tests/check_mcp_boot.py
# 订阅那一屏自己起服务、自己铺夹具源（订源要真的订、真的抓），所以不吃上面那份沙盒。
run "订阅阅读器全流程" "$PY" tests/check_feed_ui.py
# 便签那一屏同理，而且比订阅更不能共用沙盒：它改的是真账本 cache/flomo/notes.json，
# 导入、收成书、忘掉、清空一路走到底，共用那份一定互相踩（自己起服务、自己临时目录，
# 跑完整包删掉）。
run "便签全流程（导入 / 时间线 / 收成书 / 画像 / 清空）" "$PY" tests/check_flomo_ui.py
run "首启页" "$PY" tests/check_onboarding.py
# 认指纹那一套得挂在活服务上才验得实：后端报不报得出 code、Node 与 Python 算的是不是同
# 一把哈希、闲着的旧后端能不能接过来、正在跑任务的动不动它、别人的端口碰不碰 —— 五样都在
# 真端口上跑（旧后端是临时目录里现编的假 ui_server.py，起的服务跑完整套收干净，
# 门禁那份共享沙盒只读不打扰，用户机器上的实例更是一概不碰）。外加一键换班 /api/restart：
# 老人退出、接班人绑回同一个端口、全程只有一个监听者。
fresh_shelf; run "认指纹（旧后端接管 / 不打断 / 不碰别人 / 一键换班）" "$PY" tests/test_backend_identity.py "$BASE"
# 上一条管的是「后端换得掉」，这一条管的是「用户看得见什么」：界面新、后端旧时页顶那条
# 横幅。这一轮三条 bug 报的都是「后端没启动」，而页面当时只会说一句假话把人带去查一件
# 本来没事的事 —— 所以横幅得认得出旧后端、报得出两版号、一键换得掉、换不掉时给的是后端
# 那句原因（而且不许被下一次轮询盖回去）、点了「先不管」就别再烦人。只改后端答的话，
# 真换班归上一条。自己起服务、自己临时目录，跑完删掉。
run "换新后端横幅（认旧后端 / 三种失败各说各的话 / 一键换班后自己刷新）" "$PY" tests/check_swapbar.py
# 上面两套都在盯「一屏」或「一条横幅」，这一套摊开的是接口层那张网：界面上打得出的每一条
# /api/ 都真打一次，专找三类「看着像后端没启动」的毛病 —— 没回声（连接被甩）、500、
# 以及最坏的一种「回了 ok 却没干活」。用户这轮报的三条（画板 / 导图 / flomo 导入）逐条走到底，
# 坏输入与路径穿越也必须只说一句「没有」而不是把线断掉。自己起服务、自己临时目录，跑完整包删掉。
run "接口冒烟（每条路由都有回声 / 三条被点名的功能走到底 / 坏输入不断线）" "$PY" tests/smoke_api.py
fresh_shelf; run "异常兜底" "$PY" tests/test_server_fallback.py

echo
echo "剪藏真机走查默认跳过：公众号链接会过期，不入库。要跑得先备好链接表："
echo "  GUIZANG_CLIP_LINKS=/path/to/links.txt \"$PY\" tests/check_clip_live.py \"$BASE\""

kill $SRV 2>/dev/null
echo
echo "════════════════════════════════════════════════════════"
echo "通过 $PASSED 项，失败 ${#FAILED[@]} 项"
for f in "${FAILED[@]:-}"; do [ -n "$f" ] && echo "  ✗ $f"; done
echo "截图在 $GUIZANG_SHOT_DIR"
[ ${#FAILED[@]} -eq 0 ] || exit 1
