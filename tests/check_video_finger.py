#!/usr/bin/env python3
"""视频转笔记：三步条换成一颗手指，一点即可（1.2.1 · R5）。

用户原话是「砍掉识别 将样式变为剪藏的手指样式 一点即可 无需点击转笔记确认」，接着又要求
「整体 UI 布局更加简约灵动先进」。这一屏原来是三步：贴链接 → 点「识别」→ 点「开始转笔记」，
而「识别」那一步本来就不需要人确认 —— `vidStart()` 里就写着「没有 plan 就先认一次」。
所以这一轮把它压回一个动作：和剪藏那一屏共用的 72px 手指，外圈那道环当进度。

只读 ui.html，起不了服务也跑得动（归静态段，`--fast` 也跑）：

  A 手指本体   —— #vStart 是 .clipfinger，里面一只 cf-ico 手 + 一圈 cf-ring。
  B 三步条没了 —— #vPlan / VIDEO.plan / .vtsteps 都不许再出现（改回去就红）。
  C 一点即可   —— 手指与 ⌘/Ctrl+Enter 都走 vidFinger；识别发生在 vidStart 里面。
  D 读不到就说 —— 剪贴板空 / 被拒时只 whisper 一句人话，不再弹一层要人填。
  E 环是真进度 —— vidRing 走日志里的百分比，busy / finish 各有收尾，常数只写一处。
  F 布局       —— 手指 + 一栏（链接行 / 那句话 / 停止）；窄窗叠起来； vbox 出场有动效。
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parent.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


def css_of(sel):
    m = re.search(re.escape(sel) + r"\{([^}]*)\}", SRC)
    return m.group(1) if m else ""


# ── A 手指本体：与剪藏同一颗，不另起一套 ────────────────────────
hero = re.search(r'<div class="vthero">.*?</div>\s*</div>', SRC, re.S)
chk("任务区那一块是 .vthero（手指 + 一栏），不是三步条", bool(hero), hero)
chk("那颗钮就是剪藏用的 .clipfinger（两屏一个手感）",
    bool(hero) and 'class="clipfinger" id="vStart"' in hero.group(0), hero.group(0)[:200])
chk("手在里面（cf-ico 那只手画得出来）", bool(hero) and 'class="cf-ico"' in hero.group(0))
chk("环在外面，且那根进度弧有 id 可写", bool(hero) and 'class="cf-ring"' in hero.group(0)
    and 'id="vRing"' in hero.group(0), hero.group(0)[:260])
chk("环的半径与 CLIP_RING_C 用的是同一个数（33）",
    bool(hero) and 'r="33"' in hero.group(0) and "CLIP_RING_C = 2 * Math.PI * 33" in SRC)
chk("手指上写着「一步到位」，不再教用户点第二颗",
    bool(hero) and "一步到位" in hero.group(0) and "再点第二次" in hero.group(0),
    hero.group(0)[:400])
chk("尺寸只在 .clipfinger 一处写死（视频这里只说 flex:none）",
    css_of(".vthero .clipfinger").strip() == "flex:none", css_of(".vthero .clipfinger"))

# ── B 三步条整块拆掉 ───────────────────────────────────────────
chk("「识别」那颗钮不再存在（#vPlan 摘干净）", 'id="vPlan"' not in SRC, SRC.count('id="vPlan"'))
chk("VIDEO 对象里也删了 plan 这一格（不留半截引用）", "plan:" not in fn_body("wireVideo")
    and "VIDEO.plan" not in SRC, "VIDEO.plan 还在")
chk("旧的三步条 .vtsteps 不再出现（CSS 与 DOM 都拆了）", ".vtsteps" not in SRC and "vtsteps" not in SRC)
chk("「开始转笔记」那颗确认钮也跟着没了（只剩手指这一颗）", 'id="vGo"' not in SRC)

# ── C 一点即可：识别发生在开跑那一步里面 ────────────────────────
wire = fn_body("wireVideo")
chk("手指点的就是 vidFinger", "VIDEO.start.onclick = vidFinger" in wire, wire[:260])
chk("⌘/Ctrl+Enter 也走 vidFinger（键盘不落第二套）",
    "if (!vidBusy) vidFinger();" in SRC, SRC[SRC.find("e.metaKey") - 60:SRC.find("e.metaKey") + 120])
start = fn_body("vidStart")
chk("vidStart 里自己会认一次（所以不必先点「识别」）",
    "if (!vidPlan) { await vidDoPlan()" in start, start[:220])
finger = fn_body("vidFinger")
chk("vidFinger：这行空着就去读剪贴板，不为难人", "navigator.clipboard.readText()" in finger,
    finger[:260])
chk("vidFinger：读到了就填进这行、作废上一支的 plan",
    "vidPlan = null; vidRenderPlan();" in finger and "clipGrow(VIDEO.ta)" in finger, finger[:300])
chk("vidFinger：最后一步才是 vidStart（认链接与开跑是同一下）",
    "await vidStart();" in finger, finger[-200:])

# ── D 读不到只说一句话，不许再弹一层 ────────────────────────────
chk("剪贴板空 / 被拒：whisper 一句人话就收（不弹粘贴浮层）",
    "whisper('剪贴板里没找到视频链接" in finger and "return;" in finger, finger[:300])
chk("视频这一路不调 clipPasteFallback（那是剪藏那屏的退路）",
    "clipPasteFallback" not in finger and "clipPasteFallback" not in fn_body("vidStart"))
chk("那句话说清了另一条路：也可以把链接贴进这行", "贴进这行" in finger, finger[:300])

# ── E 环画的是真进度，常数只写一处 ──────────────────────────────
ring = fn_body("vidRing")
chk("vidRing 存在：拿百分比写 strokeDashoffset",
    "CLIP_RING_C * (1 - p / 100)" in ring, ring[:220])
chk("拿不到百分比时不硬编一个数上去（停在原地等下一拍）",
    "if (pct == null || pct < 0) return;" in ring, ring[:200])
chk("进度那一拍把日志里的百分比喂给环", "vidRing(unk ? -1 : pct)" in fn_body("vidShowProg"),
    fn_body("vidShowProg")[:260])
busy = fn_body("vidRingBusy")
chk("开跑时挂 busy（环知道现在该转），收尾摘掉",
    "classList.toggle('busy'" in busy and "VIDEO.start" in busy, busy[:200])
fin = fn_body("vidRingFinish")
chk("成了就补满整圈并闪一下 .done，随后归零",
    "classList.add('done')" in fin and "strokeDashoffset = 0" in fin
    and "clearTimeout" in fin, fin[:260])
chk("vidFinish 收尾会叫这套（成功失败各说各的话）",
    "vidRingFinish(!!(r && r.ok))" in fn_body("vidFinish"), fn_body("vidFinish")[:200])
chk("上一支那一圈不许带到下一支：开跑前先归零再亮 busy",
    "vidRingBusy(true)" in start and "strokeDashoffset = CLIP_RING_C" in start, start[:300])
dash = re.findall(r"strokeDasharray\s*=\s*([^;\n]+)", SRC)
chk("环的长度只有 CLIP_RING_C 一个来源（不出现第二个硬写的常数）",
    all("CLIP_RING_C" in x or "getTotalLength" in x for x in dash), dash)

# ── F 这一行的话与布局 ─────────────────────────────────────────
tools = fn_body("vidTools")
for phrase in ("把视频链接贴进来", "链接在这儿，点手指就开始", "已认出：", "正在转，进度在下面",
               "有别的任务在跑"):
    chk("vidTools 会说这一句：%s" % phrase, phrase in tools, tools[:260])
chk("手指在跑 / 有任务时按不住（避免双开）",
    "VIDEO.start.disabled = vidBusy || running" in tools, tools[:200])
chk("停止那颗挪进了这一行（不再自成一条步骤）",
    'id="vStop"' in (hero.group(0) if hero else ""), (hero.group(0)[:200] if hero else ""))
chk(".vthero 是「手指 + 一栏」的横排", "display:flex" in css_of(".vthero"),
    css_of(".vthero"))
chk("右边那一栏会缩（长链接不顶宽）", "min-width:0" in css_of(".vthin"), css_of(".vthin"))
narrow = re.search(r"@media \(max-width:860px\)\{([^@]*?)\}\s*/\* 识别结果", SRC, re.S)
chk("窄窗里手指与那一栏叠起来（不再横挤一条）",
    bool(narrow) and "flex-direction:column" in narrow.group(1)
    and ".vthero .clipfinger{align-self:center}" in narrow.group(1),
    narrow.group(1)[:260] if narrow else "找不到 .vthero 的窄窗那档")
chk("认出来那张卡 / 进度那块冒出来时有入场动效",
    ".vbox.on{animation:vpop" in SRC and "@keyframes vpop" in SRC)
chk("动了晕的人有减动效开关（reduce 时关掉）",
    "prefers-reduced-motion" in SRC and ".vbox.on{animation:none}" in SRC)
chk("这一屏没有 emoji（项目硬规矩）",
    not re.search(r"[\U0001F000-\U0001FAFF☀-➿]", hero.group(0)) if hero else False)

bad = [c for c in checks if not c[0]]
print("\n视频手指体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
