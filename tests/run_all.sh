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
run "控件体检" "$PY" tests/audit_ui.py
run "个人信息扫描" "$PY" tests/check_privacy.py
run "跨平台口径" "$PY" tests/test_platform_compat.py
# 这一条是 2026-10-04 那三条「后端没启动」工单留下来的镜子：界面从磁盘现读、路由表却是
# 进程起来那一刻装进内存的，两边不是同一份时新功能一律 404。静态先对三样东西 ——
# 版本号成对、界面和适配器打的每个 /api/ 后端都得有、那句「本机服务没在跑」不许再当字符串
# 出现；顺带钉住「四个入口（两个启动脚本、MCP 适配器、Swift 壳）用的是同一把尺子」。
run "前后端对表（版本号 / 路由 / 话术 / 四个入口）" "$PY" tests/check_route_pair.py
# 取书续传锚点是纯逻辑（目录 + 已落盘章节），不用起浏览器也验得了：
# 它守的是「卡住之后再点取书必须能接着往前读」这条 —— 卡死一次就别再来第二次。
run "取书续传锚点" "$PY" tests/check_resume.py
# 三条新线（订阅 / 视频 / 平台解析）都是离线自测：夹具跑在本机临时 http.server 上，
# 数据目录全指进各自沙盒，不联网、不碰用户真实书库。
run "RSS 订阅（发现 / 抓取 / 去重 / 入库）" "$PY" tests/check_feed.py
run "平台解析（知乎 / 小红书 / X）" "$PY" tests/check_web_parse.py
# 安娜的档案这一栏不是「接口取书」，是「弹一个看得见的窗口，用户在里头点下载，
# 归藏接住文件转成书」。所以这一条不联网、不开窗口，验的是不带窗口就判得掉的四件事：
# 账本（cache/anna.json）只存名字与数字、下载夹的路径拼法不许被人用 ../ 穿出沙盒、
# 同一份文件按字节摘要不重复入库、认不出的格式留着原文件并写清原因。全离线。
run "安娜的档案（账本 / 防穿越 / 去重 / 入库 / 域名）" "$PY" tests/check_anna.py
run "ffmpeg 按需下载（离线）" "$PY" tests/check_ffmpeg_tool.py
run "视频转笔记（下载 / 转写 / 总结 / 导图）" "$PY" tests/check_video_note.py
# 导图与画板两条新线也是纯逻辑自测：排版差分、环检测、存读导删全在系统临时目录的
# 沙盒里跑，不起服务、不开浏览器、不截图 —— 所以归静态段，--fast 也得把它们带上。
run "思维导图（清洗 / 上限 / 环检测 / 四形态坐标 / SVG）" "$PY" tests/check_mindmap.py
run "画板（存得下 / 读得出 / 导得走 / 删得掉 / 不越界）" "$PY" tests/check_board.py
# 书库从「全挤一个文件夹」改成一个模块一个文件夹，账本也跟着分格。这一条管三件事：
# 老书归置会不会搬丢/搬坏（绝不覆盖）、按模块列清单会不会串门（剪藏不许出现在微信读书）、
# 账本分格后建夹贴标签只动自己那一格（空文件夹不许被清理那一刀悄悄削掉）。全在临时沙盒里跑。
run "书库目录结构（分模块 / 归置 / 账本分格）" "$PY" tests/check_book_layout.py
# flomo 那条路（导入 → 一条笔记收成书 → 记忆画像）全是本地文件活：解析那份导出包
# （HTML 里套 div、图在 files/ 下）、同一条不重复入库、筛与标签用的是界面那同一把尺、
# 画像只由数字与标签算出（它是唯一会被反复喂给 Agent 的东西，带原文就是漏）。
# 笔记内容全部现编在 tests/flomo_fixture.py 里，用户的真实笔记一个字不进仓库。
run "flomo 便签后端（解析 / 去重 / 附件 / 收成书 / 画像）" "$PY" tests/check_flomo_notes.py
# 转写组件那一套也是纯逻辑 + 接线检查：挑引擎、算缓存目录、直连失败换镜像、空壳不算就绪、
# 自动准备默认不开（源码跑起来不该悄悄拉 1.6GB），外加「这两步不拦门 / 进包 / 壳里开开关」。
# 下载那一步用桩替掉，全程不联网。所以归静态段，--fast 也得带上。
run "转写组件（引擎挑选 / 模型缓存 / 镜像回退 / 接线）" "$PY" tests/check_media_setup.py
# 维护那一套（卸载组件 / 清除数据）最要紧的不是「删得掉」而是「删不掉不该删的」：
# 书库、别人家的模型、别人家的浏览器、数据目录本身 —— 四条红线各钉一条。
# 全程在临时沙盒里跑，不碰真实的 cache / 书库 / HF 缓存 / playwright 缓存。
run "维护（白名单清理 / 四条红线 / 试算不删）" "$PY" tests/check_cleanup.py
# 封面这一轮的规矩是「剪藏用文章首图、视频用视频封面」，而它最容易出的事故不是没图，
# 是拿错图（页头 logo、站长头像当封面，满书架一张脸）和不该下的图去下（别人网页里写的
# og:image 指到内网，等于替用户去敲内网的门）。网络那层用记账的假 urlopen 顶掉，
# 于是「一次请求都没发出去」也断言得出来。全程临时沙盒，不联网。
run "封面规则（首图挑选 / 内网拦截 / 落盘与补取）" "$PY" tests/check_covers.py
# AI 小结这一轮的口径是「一个入口、两种模式、三种内容」，规矩全在 ai_sum.py：
# 哪一路算哪种、读书一次只吃当前这一章、超长怎么掐、模型回得不像导图时怎么兜、
# 一个模式一份缓存互不覆盖。再加一条这轮新钉的：上游半路拔线必须和「答完了」分得开
# —— 分不清就会把半截当成品存盘，把上一次那份好的覆盖掉。全程离线，假流直接喂字节行。
run "AI 小结（范围 / 提示词 / 导图兜底 / 缓存 / 断流）" "$PY" tests/check_ai_sum.py
# 两张热力图上那些数字的规矩全在 activity.py：一次心跳夹 300 秒（浏览器冻结、合盖睡醒会
# 报一个巨大间隔，不夹就是一根通天柱）、单日封顶 12 小时、缺的日子补 0 而不是跳过（跳一格
# 整张图就错位）、连续天数对「今天还没开始」宽容。纯函数不算盘不碰 HTTP，所以归静态段。
run "时长账（夹 / 封顶 / 补零 / 连续 / 色阶 / 归档）" "$PY" tests/check_activity.py
# 阅读计划那一套也是纯逻辑：页数按正文字数折算（跟字号、窗口宽窄无关）、进度只增不减
# （翻回去查个词不该把「今天读了 30 页」缩成 8 页）、跨天 / 跨周记账、读完把当时的
# 目标与起止留进档案（改新计划也冲不掉）。时间由调用方传，所以不用起浏览器也验得实。
run "阅读计划（页数折算 / 只增不减 / 跨天记账 / 读完留档）" "$PY" tests/check_plan.py
# 文案这一条守的是「界面上不再有 AI 味」：语气词、波浪号尾巴（「好的~」）、感叹号、emoji
# 一律不许出现在散文里 —— 唯一放行的是剪藏正文的清洗正则（它得把别人的 emoji 滤掉）。
# 顺带钉住「设置那一段的每条提示都写完整句子、以句号收」，以及个人主界面那颗「设置」还在。
run "界面文案（零语气词 / 零俏皮号 / 零 emoji / 提示成句）" "$PY" tests/check_copy.py
# 「文字规格」这一类毛病里，有一半不用起浏览器也能钉死：同一样东西写在好几处、数值还对不上
# （毛玻璃模糊值 CSS 写 24、设置弹窗却写死 26，JS 一跑滑杆就从 26 跳到 24）。这一条只读
# ui.html 的源码，把「外观缺省值只有一处真相」和「按钮字一律 nowrap、只有章节标题开回来」
# 两条规矩按成会失败的断言 —— 真机上量得出几何的另有一半，见 check_overflow.py。
run "文字与外观规格（缺省值一处真相 / 折行规矩）" "$PY" tests/check_spec.py

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
# 在 214px 的稿子列里戳出 82px 压进正文。同一类写法（定宽标签不给 min-width:0）全站都
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
