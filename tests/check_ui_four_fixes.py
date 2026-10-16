#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面四收口（画板全屏无限画布 / 转写结果重排 / 订阅栏名排版 / 写作工具条图标化）
的离线体检 —— 2026-10-16 这一轮。

用户那一段话里最要紧的四件事，各自都栽过或者眼看着要栽，所以逐条钉住：

  A. 画板
     ① 打开是铺满视口的一层（不再是 min(1040px) 的居中卡片）—— 「尺寸过小」；
     ② 两栏：左栏常驻（书 + 板），右侧画布区；「不是无限画板」的根因是画布跟着窗口
        改尺寸，所以钉的是「画布逻辑尺寸写死、不再随窗口 setDimensions」；
     ③ 三种挪画布的方式都在同一个裁决点上：指针拖动 / 触控板双指 / 滚轮，
        并且画笔手里的拖动不许被平移抢走；
     ④ 工具条是图形不是字（用户原话「用图标表示，不用文字」），撤销/重做/清空同此；
     ⑤ 视口不许被存进画板 JSON：存的是「这块板」，不是「我此刻盯着哪儿」。

  B. 转写结果
     ① 段落是卡片不是表格行（旧样是四列网格 + 满屏横线，像旧后台）；
     ② 工具条分三簇，原来的七个控件没有平摊成一行。

  C. 订阅左栏栏名
     「看哪些」不再和四颗钮挤同一个 flex-wrap 里 —— 那正是「排版有偏移」的根因
     （26px 的钮撑起栏高，19px 的标题只能垂直居中，跟按钮里的字不在一条基线上）。
     钉的是：标题独占一区、与按钮分列、且那颗计数不许被折成竖排。

  D. 写作工具条
     ① 二十颗文字钮从左排到右、排不下折行 —— 用户明说不要；
     ② 粗体/删除线/代码/颜色这些改用图形；
     ③ 段落样式收成一颗（单选不该摊开四颗），低频插入收进一颗「+」。

全部是纯文件走查 + 正则读常量，不起服务、不联网、不开浏览器，归 --fast 静态段。
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = (HERE.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def body_of(name, pat=None):
    """取某个顶层 function 的函数体（到下一个顶格 `}` 为止）。"""
    m = re.search(r"function %s\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else None


def css_of(sel):
    """从一个选择器起、到下一个 `}` 为止的那段 CSS（供这种一次性比对用）。"""
    m = re.search(re.escape(sel) + r"\{([^}]*)\}", SRC, re.S)
    return m.group(1) if m else None


# ══════════════════ A. 画板：全屏 + 无限画布 + 图标 ══════════════════
bd_layer = re.search(r'<div class="ntlay full" id="bdLayer">', SRC)
chk("画板：这一层加了 full，铺满视口而不是居中小卡", bool(bd_layer),
    "用户报「尺寸过小」：.ntlay>section 是 width:min(1040px,100%)")

bd_full_css = css_of(".ntlay.full>section") or css_of(".ntlay.full")
chk("画板：full 那一档把 section 撑到整屏（width:100% 且不带圆角）",
    bd_full_css and "width:100%" in bd_full_css and "border-radius:0" in bd_full_css,
    bd_full_css)

wrap_css = css_of(".bdwrap")
chk("画板：两栏结构在（左栏固定宽 + 右侧吃满）",
    wrap_css and "grid-template-columns:" in wrap_css and "minmax(0,1fr)" in wrap_css, wrap_css)

# 无限画布：尺寸是常量，不是窗口量出来的
# 两个尺寸写在同一个 const 里（const BD_PAPER_W = 3200, BD_PAPER_H = 2200;），
# 所以只查 BD_PAPER_W 带着 const，H 跟着同行出现 —— 两个都是字面量。
chk("画板：纸的逻辑尺寸写死成常量（不跟窗口走，这就是「无限」的前提）",
    bool(re.search(r"const BD_PAPER_W\s*=\s*\d+\s*,\s*BD_PAPER_H\s*=\s*\d+", SRC)),
    re.search(r"const BD_PAPER_W[^;]*", SRC))
mk = body_of("bdMakeCanvas")
chk("画板：画布按常量尺寸造出来，量的是 BD_PAPER_W/H 而不是 clientWidth",
    mk and "BD_PAPER_W" in mk and "BD_PAPER_H" in mk and "clientWidth" not in mk, mk)
res = body_of("bdClamp")
chk("画板：窗口变了只夹平移范围，不再 setDimensions（画的东西按原坐标留着）",
    res and "setViewportTransform" in res and "setDimensions" not in res, res)
chk("画板：旧的 bdResize 走开了（尺寸-视口那条路没了）",
    "function bdResize(" not in SRC)

pan = body_of("bdPanWire")
chk("画板：三种挪法都在同一个裁决点上（pointerdown / wheel / 右键）",
    pan and "pointerdown" in pan and "wheel" in pan and "contextmenu" in pan, pan)
chk("画板：选工具下拖空白才挪画布；画笔手里的拖动不让平移抢走",
    pan and "findTarget" in pan and "BD.tool !== 'select'" in pan, pan)
chk("画板：触控板双指走 wheel（deltaX / deltaY），⌘ 滚轮才是缩放",
    pan and "bdPanBy(-e.deltaX, -e.deltaY)" in pan and "ctrlKey || e.metaKey" in pan, pan)
chk("画板：滚轮在正文浮层上不抢（那一层自己滚）",
    pan and "bdRead" in pan, pan)
clamp = body_of("bdClamp")
chk("画板：平移夹在「纸还看得见」的范围内（不会把整张纸推出视野）",
    clamp and "vw - BD_PAPER_W" in clamp and "vh - BD_PAPER_H" in clamp, clamp)

# 工具条：图形不是字
tools = re.search(r"const BD_TOOLS = \[(.*?)\];", SRC, re.S)
chk("画板：BD_TOOLS 是对象数组（t / tip / svg），不再是有文字标签的老三元组",
    tools and '"t"' not in (tools.group(1)[:400])
    and tools and "svg:" in tools.group(1), tools)
bt = body_of("bdTools")
chk("画板：工具钮渲染的是 svg，不是中文标签",
    bt and "x.svg" in bt and 'class="svg' in bt, bt)
chk("画板：撤销 / 重做 / 清空 / 回原位 / 置中也都是图形",
    "function bdIconBtn(" in SRC and "const BD_ICON" in SRC
    and bt and "bdIconBtn('bdUndoB'" in bt
    and bt and "bdIconBtn('bdRedoB'" in bt and bt and "bdIconBtn('bdClearB'" in bt,
    "（「撤销」那种说明只许出现在 title 上，不许当按钮标签）")
chk("画板：纸面五档也是图形（不再是「白纸 / 方格 / 点阵 / 横线 / 黑纸」五个字）",
    "BD_PAPER_ICON" in SRC and "BD_PAPER_ICON[x[0]]" in SRC, "")

# 视口不进存档
save = body_of("bdSave")
chk("画板：落盘前先把视口复位，存的是这块板不是「我盯着哪儿」",
    save and "setViewportTransform([1, 0, 0, 1, 0, 0])" in save
    and "_text_env_canvas_" not in save, save)
snap = body_of("bdSnapJson")
chk("画板：撤销快照也不带视口（回退一步位置不许跟着跳）",
    snap and "setViewportTransform([1, 0, 0, 1, 0, 0])" in snap, snap)
pick = body_of("bdPick")
chk("画板：读回来贴回当前视口（旧档里写死的那份不许左右「现在看哪儿」）",
    pick and "bdClamp()" in pick, pick)

# 左侧常驻 + 正文浮层 + 换书
chk("画板：左栏有「这一本」与「换一本书」两组（左侧常驻书籍列表）",
    "这一本" in (body_of("bdPaintList") or "") and "换一本书" in (body_of("bdPaintList") or ""))
chk("画板：换书走书库清单，切前先把手上这块 flush 落盘",
    "data-swap=" in SRC and "if (BD.timer) await bdSave();" in (body_of("bdSwitchBook") or ""))
chk("画板：正文浮层在（边读边写），能收掉且不挡笔",
    'id="bdRead"' in SRC and "bdReadClose" in SRC and ".bdread{" in SRC)

# ══════════════════ B. 转写结果：表格行 → 卡片 ══════════════════
row_css = css_of(".vtrow")
chk("转写：段落是卡片（block + 圆角 + 间距），不再是四列网格的表格行",
    row_css and "display:block" in row_css and "grid-template-columns" not in row_css
    and "border-radius:" in row_css, row_css)
chk("转写：段落之间不再用横线切（border-bottom 那套没了，靠 margin 分开）",
    row_css and "border-bottom" not in row_css and "margin:" in row_css, row_css)
tb_css = css_of(".vttb")
chk("转写：工具条分成簇（vgrp），七个控件不再平摊一行",
    tb_css and ".vgrp" in SRC and SRC.count('class="vgrp"') >= 3, tb_css)
chk("转写：挑书 / 筛选 / 动作三簇各自成组（间距分组，不是一根横线串一串）",
    'class="sep"></span>' in SRC and '<span class="vgrp">' in SRC, "")
chk("转写：分区标题降噪成一句陈述，那三个动作说明挪到控件自己的 title 上",
    "点一段就能改" in SRC and 'id="vSecNote"' in SRC, "")
chk("转写：时间戳与说话人合成一行元信息（原来两列的空隙比内容还宽）",
    "vtmeta" in SRC and ".vtmeta{" in SRC, "")

# ══════════════════ C. 订阅左栏栏名排版 ══════════════════
hd_css = css_of(".fehd")
chk("订阅：栏头不再 flex-wrap（标题被按钮挤成一字一行的根因）",
    hd_css and "flex-wrap:wrap" not in hd_css, hd_css)
chk("订阅：标题独占一区 .fetit，与右侧按钮 .feact 分列",
    ".fehd .fetit" in SRC and ".fehd .feact" in SRC and hd_css
    and "align-items:stretch" in hd_css, hd_css)
tit_css = css_of(".fehd .fetit")
ftx_css = css_of(".fehd .ftx")
chk("订阅：标题区 flex:1 1 auto + min-width:0，文字那一层 nowrap（不许被压成竖排）",
    tit_css and "1 1 auto" in tit_css and "min-width:0" in tit_css
    and ftx_css and "nowrap" in ftx_css,
    (tit_css, ftx_css))
cnt_css = css_of(".fehd .fetit .cnt")
chk("订阅：栏名旁那颗计数不许折行（它一折，整行高度就飘）",
    cnt_css and "white-space:nowrap" in cnt_css and "flex:none" in cnt_css, cnt_css)
chk("订阅：「看哪些」在标题区里，不再和四颗钮挤同一行",
    '<span class="fetit"><span class="ftx">看哪些</span>' in SRC, "")
chk("订阅：视频那一栏的栏头同此（.fehd 两屏共用一套，别只改一半）",
    SRC.count('<span class="fetit">') >= 2
    and 'class="t">转出来的书' not in SRC, "")

# ══════════════════ D. 写作工具条：图标化 + 分三摊 ══════════════════
chk("写作：老的那张二十颗平铺清单 WR_TOOLS 不在了",
    "const WR_TOOLS = [" not in SRC, "")
chk("写作：换成三张表：段落样式 / 行内格式 / 低频插入",
    all(x in SRC for x in ("const WR_STYLES = [", "const WR_INLINE = [", "const WR_INS = [")), "")
chk("写作：粗体 / 斜体 / 下划线 / 删除线 / 代码都是图形，不是「粗 / 斜 / 下 / 删 / 码」",
    'html: "<b>粗</b>"' not in SRC and 'label: "码"' not in SRC
    and re.search(r'bold: .<svg viewBox', SRC), "")
wire = body_of("wrBarWire")
chk("写作：工具条分三摊铺（样式一颗 / 行内一组 / 插入藏进 +）",
    wire and "wstylepop" in wire and "wgrp" in wire and "winspop" in wire
    and "WR_ICON.plus" in wire, wire)
chk("写作：段落样式是单选，收成一颗钮 + 一张弹层（不再摊开四颗）",
    'sb.id = "wrStyleB"' in SRC and "wstylepop" in SRC, "")
chk("写作：低频插入收进一颗「+」（要用了才展开）",
    'plus.id = "wrPlusB"' in SRC and "winspop" in SRC, "")
chk("写作：段间不再用竖线分组（间距分组，一排小方块加竖线还是像旧系统）",
    css_of(".wrmain .edtools") and "gap:4px" in css_of(".wrmain .edtools")
    and css_of(".wrmain .edtools .sep") and "display:none" in css_of(".wrmain .edtools .sep"),
    css_of(".wrmain .edtools .sep"))
st = body_of("wrBarState")
chk("写作：当前段落那颗跟着光标换图标（不然引用里的光标配一颗正文钮，像坏了）",
    st and "wrStyleLabel" in st and "styleB.innerHTML = want.svg" in st, st)

bad = [c for c in checks if not c[0]]
print()
print("界面四收口：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, name, extra in bad:
    print("  ✗ " + name + ("   " + extra if extra else ""))
sys.exit(1 if bad else 0)
