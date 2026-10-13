# -*- coding: utf-8 -*-
"""氛围渐变（1.1.0）：外观层从「固定色晕」放开成「预设 + 自定义」。

这一套只读 ui.html，不起服务也不开浏览器（归静态段）。它守四件事：
  ① 默认那套必须还是原来那三个颜色 —— 没改过设置的人不该被这轮顺手换了皮肤；
  ② 自定义是「两三个色点 + 一个角度」，不是只给个颜色列表就完事；
  ③ 配置存在与磨砂 / 模糊同一个 LOOK_KEY 里（外观设置一处落盘，不另开一份）；
  ④ 数值缺省只有 CSS 变量一处真相，JS 不往 CSS 里塞第二份 `linear-gradient(...)` 字面量
     （那份还会被 check_js_refs 当成没定义的函数 gradient）。
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parent.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── 一、默认那套仍与老的固定色晕同色 ────────────────────────────
m = re.search(r"const AMB_DEF\s*=\s*\{([^}]*)\}", SRC)
chk("JS 里有 AMB_DEF（氛围缺省值的唯一真相）", bool(m), m)
if m:
    body = m.group(1)
    for hexv, label in (("#2f7ff0", "蓝"), ("#1f9e85", "绿"), ("#d68c60", "橙")):
        chk("默认色点还带老色晕里的%s色（%s）" % (label, hexv), hexv in body, body)
    chk("默认预设是 aurora（缺省不可省成空）", "preset: 'aurora'" in body, body)

# ── 二、多种预设 + 自定义 ───────────────────────────────────────
pm = re.search(r"const AMB_PRESETS\s*=\s*\[(.*?)\n\];", SRC, re.S)
chk("预设表存在（用户可选择多种渐变色，而不是只有一套）", bool(pm), pm)
if pm:
    ids = re.findall(r"\['([a-z]+)',\s*'[^']+'", pm.group(1))
    chk("预设至少四套且各不重名", len(ids) >= 4 and len(set(ids)) == len(ids), ids)
    chk("预设里没混进 'custom'（那是自定义的开关，不是一套色）", "custom" not in ids, ids)

chk("自定义三色点齐：设置里有三个取色器",
    all(('id="ambC%d"' % i) in SRC for i in (0, 1, 2)))
chk("自定义有「角度」这一档（只给颜色不给方向，谈不上渐变）",
    'id="ambAngle"' in SRC and re.search(r"ambSet\(\{angle:\s*\+?e\.target\.value", SRC))
chk("两色点有开关（题目里的『两三个色点』两种都要做得到）", 'id="ambTwo"' in SRC)

# ── 三、落盘与配置读取 ─────────────────────────────────────────
chk("氛围配置存在 LOOK_KEY 里（外观设置一处落盘，不另开一份）",
    re.search(r"l\.ambient\s*=\s*\{", SRC) and "localStorage.setItem(LOOK_KEY" in SRC)
chk("坏配置兜得住：非法的 preset / 颜色 / 角度都退回缺省，不抛",
    ": AMB_DEF.preset" in SRC and ": AMB_DEF.angle" in SRC
    and "Array.isArray(a.colors) && a.colors.length >= 2" in SRC)

# ── 四、CSS 变量一处真相 + 与 check_js_refs 不打架 ───────────────
amb_js = SRC.split("function applyAmbient", 1)[-1].split("ambReady = true", 1)[0]
chk("CSS 里用 --amb-1/2/3 驱动色点（JS 只写变量，不写第二份颜色）",
    all(("--amb-%d" % i) in SRC for i in (1, 2, 3)))
chk("梯度本体留在 CSS 的 linear-gradient 里，氛围那段 JS 不写这个字面量"
    "（写了 check_js_refs 会把它当成没定义的 gradient 函数报红）",
    "linear-gradient(" not in amb_js)
chk("角度也是变量（--amb-angle），不是 JS 拼进样式里",
    "--amb-angle" in SRC and "setProperty('--amb-angle'" in SRC)
chk("深浅主题的透明度由 JS 按当前主题算（原先那两段写死的深色覆盖已收拢）",
    "function ambDark()" in SRC)

bad = [c for c in checks if not c[0]]
print("\n氛围渐变体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  x " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
