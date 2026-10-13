#!/usr/bin/env python3
"""归藏静态体检：文字与外观的「规格」只许有一处真相（1.0.6 · #209）。

用户报的原话是「修正文字规格显示不合适的地方」。这一类毛病分两种：
  ① 同一样东西写在好几处、数值还对不上 —— 毛玻璃模糊值就是：CSS `--blur:24px`、
     JS 里缺省 24、设置弹窗的滑杆却写死 `value="26"` / 读数 `26px`。JS 一跑滑杆就从 26
     跳到 24，用户看见的是「一样东西两个默认值」。
  ② 该横排的字被挤成竖排、该收着的字戳出边框 —— 这两条真机才量得准（见 check_overflow.py），
     但它们各自在源码里都有能一眼钉死的那一行，这里顺手也按住，免得有人改回去。

这一套只读 ui.html，起不了服务也跑得动（归静态段）。
"""
import pathlib
import re
import sys

SRC_PATH = pathlib.Path(__file__).resolve().parent.parent
SRC = (SRC_PATH / "ui.html").read_text(encoding="utf-8")
SERVER = (SRC_PATH / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── 一、外观缺省值只有一处真相 ──────────────────────────────────
css_blur = re.search(r"--blur:\s*(\d+(?:\.\d+)?)px", SRC)
js_def = re.search(r"const LOOK_DEF\s*=\s*\{([^}]*)\}", SRC)
chk("CSS 里能找到 --blur 的缺省值", bool(css_blur), css_blur)
chk("JS 里能找到 LOOK_DEF（外观缺省值的唯一真相）", bool(js_def), js_def)

if css_blur and js_def:
    def jsnum(key):
        m = re.search(key + r"\s*:\s*(\d+(?:\.\d+)?)", js_def.group(1))
        return float(m.group(1)) if m else None

    chk("模糊强度：CSS 的 --blur 与 JS 的 LOOK_DEF.blur 是同一个数",
        float(css_blur.group(1)) == jsnum("blur"),
        (css_blur.group(1), jsnum("blur")))
    # 磨砂浓度是特例：CSS 那份是「还没起 JS」时的兜底，还要分浅/深主题（.68/.62），
    # 所以它跟 JS 缺省（72%）本来就不是一个数 —— 这里只钉 JS 那份存在且是 0~100 的整数。
    f = jsnum("frost")
    chk("磨砂浓度：JS 的 LOOK_DEF.frost 是一个 0~100 的数", f is not None and 0 <= f <= 100, f)

# 滑杆与读数不许在 HTML 里写死值/文本，否则又变成第二处真相。
def tag_for(rid):
    m = re.search(r"<input[^>]*id=\"%s\"[^>]*>" % rid, SRC)
    return m.group(0) if m else ""

for rid, what in (("blurRange", "模糊强度"), ("frostRange", "磨砂浓度")):
    tag = tag_for(rid)
    chk("%s：滑杆在 HTML 里不写死 value（由 applyLook 现填）" % what,
        bool(tag) and "value=" not in tag, tag)

for rid, what in (("v-blur", "模糊强度"), ("v-frost", "磨砂浓度")):
    m = re.search(r"<span[^>]*id=\"%s\"[^>]*>([^<]*)</span>" % rid, SRC)
    chk("%s：读数在 HTML 里是空的（由 applyLook 现填）" % what,
        bool(m) and m.group(1).strip() == "", m.group(0) if m else None)

# applyLook 必须走 LOOK_DEF，不能又退回写死的字面量。
ap = re.search(r"function applyLook\(part\)\s*\{(.*?)\n\}", SRC, re.S)
body = ap.group(1) if ap else ""
chk("applyLook 取缺省值时走 LOOK_DEF，不写死数字",
    "LOOK_DEF.frost" in body and "LOOK_DEF.blur" in body
    and not re.search(r":\s*72\b", body) and not re.search(r":\s*24\b", body),
    body[:160])

# ── 二、横排 / 少折行的规矩 ─────────────────────────────────────
chk("按钮的字一律不折行（button{white-space:nowrap}）",
    re.search(r"\bbutton\s*\{\s*white-space:\s*nowrap", SRC))
# 全站要折两行的只有两处，且都列进白名单：
#   · `.rdch .tt` —— 章节标题。`button{nowrap}` 是全局规矩，它得单独开回来，这正是 #206 的要害。
#   · `.sstage>.wcard .mt .a` —— 堆叠卡片里那句作者/副题。它不是按钮、不受 nowrap 约束，
#     折行本来就对；列进白名单只是免得它把这条断言绊红。
# 断言 = `.rdch .tt` 必须在，且不许冒出白名单外的新折行规则。
WRAP_OK = {".rdch .tt", ".sstage>.wcard .mt .a"}
found = [s.strip() for s in re.findall(r"([^\n{}]*)\{[^\n{}]*white-space:\s*normal", SRC)]
chk("只有章节标题那一行把折行开回来（其余按钮都是 nowrap）",
    ".rdch .tt" in found and set(found) <= WRAP_OK, found)

# ── 三、长出整句的标签必须能收缩（否则戳出窄栏） ─────────────────
for sel in (".wrline .lab", ".wrbar .lab"):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", SRC)
    rule = m.group(1) if m else ""
    chk("%s：允许收缩并给了 min-width:0（不再 flex:none）" % sel,
        "flex:0 1 auto" in rule and "min-width:0" in rule and "flex:none" not in rule, rule)

# ── 四、导图缩放走手势（1.2.0）：加减号按钮拆掉，双指 / 触控板捏合接管 ──
# 用户报「新增双指触控缩放交互，替代固定的右上角加减号按钮」。这一条只读源码，
# 钉住三件事：那两个 id 真没了、捏合那条路真接了、mmwrap 真把浏览器自己的
# 双指页面缩放关掉了（不关的话两指落下被浏览器抢走，导图收不到事件）。
chk("导图：加减号按钮的 id 与引用都拆干净了",
    "mmZoomIn" not in SRC and "mmZoomOut" not in SRC)
chk("导图：缩放读数还在（手势改了比例得让人看得见）",
    'id="mmZoom"' in SRC and "mmZoomAt(" in SRC and "function mmZoomWire(" in SRC
    and "mmZoomWire();" in SRC)
chk("导图：触控板捏合（带 ctrl / ⌘ 的滚轮）接到了缩放上",
    re.search(r"addEventListener\('wheel'.*?ctrlKey", SRC, re.S)
    and "e.preventDefault()" in SRC)
chk("导图：真触屏两指捏合有 touchstart / touchmove 两条，且单指滚动不抢",
    re.search(r"touchstart.*?e\.touches\.length\s*!==\s*2", SRC, re.S)
    and re.search(r"touchmove.*?e\.touches\.length\s*!==\s*2", SRC, re.S))
mwrap = re.search(r"\.mmwrap\{([^}]*)\}", SRC)
chk("导图：mmwrap 关了浏览器的双指页面缩放（touch-action:pan-x pan-y）",
    bool(mwrap) and re.search(r"touch-action:\s*pan-x\s+pan-y", mwrap.group(1)),
    mwrap.group(1) if mwrap else None)

# ── 五、视频转笔记：进度条实时渲染 / 刷新后保留「转写中」/ 停止点击即停（1.2.0）──
# 用户报的两条：「进度条状态未更新」「刷新后条目直接从内容框架移除，要标成转写中」，
# 外加「停止按钮响应延迟」。三条都有能在源码里一眼钉死的落点，真机那条另见
# check_video_workbench.py 与 check_media_views.py，这里先守住最容易被改回去的地方。
vt = re.search(r"async function vidTick\(\)\s*\{(.*?)\n\}", SRC, re.S)
vt_body = vt.group(1) if vt else ""
chk("进度条：vidTick 读日志对象的 .s 字段（{t,s} 不能直接喂给正则）",
    bool(vt) and ".s" in vt_body and not re.search(r"exec\(lines\[i\]\)", vt_body),
    vt_body[:220])
chk("进度条：任务在跑但还没有 [阶段 N%] 时也先亮出「正在准备」的条子",
    "runningKind === 'video'" in vt_body and "vidShowProg(-1" in vt_body, vt_body[:220])
chk("进度条：只认这一趟任务开工后的进度行，不吃上一支留在缓冲里的收尾行",
    "runningAt" in vt_body and "runningAt = running" in SRC, vt_body[:220])
chk("进度条：切进视频这屏立刻拉一次进度，不等下一拍轮询",
    re.search(r"vidReloadCap\(\);\s*\n\s*vidTick\(\);", SRC))

chk("左栏：转写中的那一条挂在本机记忆上，刷新后能摆回来",
    "VID_PENDING_KEY" in SRC and "vidPendingSet(" in SRC and "vidPendingClear()" in SRC)
chk("左栏：转写中的条目标成「转写中」且不是一本已完成的书",
    "转写中" in SRC and "vtbook pending" in SRC)
vs = re.search(r"async function vidStart\(\)\s*\{(.*?)\n\}", SRC, re.S)
vf = re.search(r"function vidFinish\(r\)\s*\{(.*?)\n\}", SRC, re.S)
chk("左栏：起任务时记下这一支，任务收尾时把「转写中」撤掉",
    bool(vs) and "vidPendingSet(" in vs.group(1)
    and bool(vf) and "vidPendingClear()" in vf.group(1))

st = re.search(r"async function vidStop\(\)\s*\{(.*?)\n\}", SRC, re.S)
stop_body = st.group(1) if st else ""
chk("停止：点击当场锁住按钮并回话，不等那趟请求慢慢回",
    "vidStopping = true" in stop_body
    and stop_body.index("vidStopping = true") < stop_body.index("await videoPost"),
    stop_body[:220])
chk("停止：按钮文字会变成「正在停…」，任务翻面后自己复原",
    "正在停" in SRC and "vidStopping = false" in SRC)
chk("停止：后端发完信号立刻回，不再在请求里等满 10 秒",
    "for _ in range(40)" not in SERVER and "已请求停止" in SERVER
    and "threading.Thread(target=_finish_stop" in SERVER)

bad = [c for c in checks if not c[0]]
print("\n规格体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
