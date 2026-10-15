#!/usr/bin/env python3
"""叠卡的几何：左右翻的时候，卡的下缘不许压到页码那一排（1.2.1 · R5）。

用户报的是「叠卡布局下 左右切换页数展示出现底部和书籍重叠」。舞台 `.sstage` 定高，卡片是
绝对定位挂在里面，s1/s2/s3 与 b1/b2/b3 又各自往下错开 10~30px —— 原来卡宽只跟**横向**
（容器宽度 cqw）受限，竖方向没人管：窗口一矮、或书名长到两行，卡的下缘就越过舞台底边，
正好搭在 `.snav` 那排页码上；翻到一半时两张卡一起压过去，看着就是「书和页码重叠」。

这一轮把竖向也变成一次算清的代数式（全部在 CSS 里，不引解析器）：

  封面是 2:3 → 卡高 ≈ 卡宽 × 1.5 + 文字那一块（--sm）
  所以卡宽不能超过 (舞台高 --sh − 竖排余量 --sm) / 1.5

这一套用读源码的方式把这条式子和几条压在后面的余量钉死，`--fast` 也跑：

  A 舞台自己：--sh / --sm 只写一次，height 就吃 --sh。
  B 卡宽：横向 33cqw 与竖向 (sh−sm)/1.5 取小的，上下还有 clamp 夹着。
  C 竖向算式对得上封面的 2:3（改成 1.4 就红 —— 那是给自己留了个假余量）。
  D 最坏情况不越界：sw 下限 168 时 1.5×sw + sm 仍 ≤ sh 下限 430。
  E 横向也不溢出舞台：最外那张 f3 + sw/2 ≤ 50cqw。
  F 往下错开的那些位移（含 hover 那一档）都落在 --sm 里。
  G 页码那一排压得住：.snav 的 z-index 高于任何一张卡。
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parent.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def rule(sel):
    m = re.search(re.escape(sel) + r"\{([^}]*)\}", SRC)
    return m.group(1) if m else ""


def num(text, pat, default=None):
    m = re.search(pat, text)
    return float(m.group(1)) if m else default


stage = rule(".sstage")
card = rule(".sstage>.wcard")
chk("舞台还在（找得到 .sstage{…}）", bool(stage), stage)
chk("竖向两个数各只写一次：--sh（舞台高）与 --sm（文字 + 错开的余量）",
    "--sh:clamp(" in stage and "--sm:" in stage, stage)
chk("舞台的高吃的就是 --sh（同一个数，不在两处各写一遍）",
    re.search(r"height:var\(--sh\)", stage) is not None, stage)
chk("卡片按容器自己算（cqw 的基准还在）",
    "container-type:inline-size" in stage and "container-name:sst" in stage, stage)

chk("卡宽由竖方向夹住：(var(--sh) - var(--sm)) / 1.5",
    "(var(--sh) - var(--sm)) / 1.5" in card, card[:260])
chk("横竖两边取小的（33cqw 与竖向上限）", "min(33cqw, (var(--sh) - var(--sm)) / 1.5)" in card,
    card[:260])
chk("上下有 clamp 夹着（不让它瘦成一条、也不放任涨破 300）",
    "--sw:clamp(168px," in card and "300px)" in card, card[:260])

# 封面 2:3 → 高 = 宽 × 1.5；式子里那个除数必须还是 1.5。
chk("封面仍是 2:3（1.5 这个系数是照它来的）", "aspect-ratio:2/3" in rule(".wcard .cv"),
    rule(".wcard .cv")[:160])
chk("除数写的是 1.5 而不是随手一个 1.4（改了就是假余量）",
    re.search(r"\(var\(--sh\) - var\(--sm\)\) / 1\.5\)", card) is not None, card[:200])

sm = num(stage, r"--sm:\s*(\d+)px", 0)
sh_lo = num(stage, r"--sh:clamp\(\s*(\d+)px", 0)
sh_hi = num(stage, r"--sh:clamp\([^,]+,\s*\d+vh,\s*(\d+)px", 0)
sw_lo = num(card, r"--sw:clamp\(\s*(\d+)px", 0)
sw_hi = num(card, r",\s*(\d+)px\)", 0)
chk("把这几个数都读出来了（读不出就别假装算过）",
    sm and sh_lo and sh_hi and sw_lo and sw_hi,
    {"sm": sm, "sh": (sh_lo, sh_hi), "sw": (sw_lo, sw_hi)})
chk("最坏情况不越界：卡宽取下限时 1.5×宽 + 竖排余量 仍 ≤ 舞台最矮",
    sw_lo * 1.5 + sm <= sh_lo, {"1.5*%s+%s" % (sw_lo, sm): sw_lo * 1.5 + sm, "sh_lo": sh_lo})
chk("卡宽取上限时也不越界（最高的那一档舞台也得容得下）",
    sw_hi * 1.5 + sm <= sh_hi, {"sw_hi": sw_hi, "需要": sw_hi * 1.5 + sm, "sh_hi": sh_hi})

# E：横向最外那张 + 半张卡宽 ≤ 半个舞台（50cqw）。
f1 = num(card, r"--f1:(\d+(?:\.\d+)?)cqw", 0)
f2 = num(card, r"--f2:(\d+(?:\.\d+)?)cqw", 0)
f3 = num(card, r"--f3:(\d+(?:\.\d+)?)cqw", 0)
sw_cqw = num(card, r"min\((\d+(?:\.\d+)?)cqw", 0)
chk("三档偏移是递增的（f1<f2<f3）", f1 < f2 < f3, {"f": (f1, f2, f3)})
chk("最外那张的远缘不越过舞台边（f3 + 半张卡宽 ≤ 50cqw）",
    f3 + sw_cqw / 2 <= 50, {"edge": f3 + sw_cqw / 2})

# F：s/b 六张各自往下挪的那些像素，加上卡本身的高度都留在 --sm 里说的就是这些。
drops = [float(x) for x in re.findall(
    r"\.sstage>\.wcard\.[sb][123](?::hover)?\{transform:translate3d\([^,]+,\s*(\d+)px", SRC)]
chk("六张的向下错位都读到了", len(drops) >= 6, drops)
chk("错位最大那一下仍小于竖排余量（不会把页码盖住）",
    bool(drops) and max(drops) < sm, {"max": max(drops) if drops else None, "sm": sm})

# G：页码那一排压得住任何一张卡。
nav = rule(".snav")
nav_z = num(nav, r"z-index:\s*(\d+)", -1)
card_z = [float(x) for x in re.findall(r"\.sstage>\.wcard\.[\w.]+\{[^}]*?z-index:\s*(\d+)", SRC)]
chk("页码那一排是定位的（z-index 才生效）", "position:relative" in nav, nav[:200])
chk("卡的 z-index 都在 16~20 这一档（读得出基准）", bool(card_z) and max(card_z) == 20, card_z)
chk("页码永远压在卡之上（卡片再翻也不许盖住它）",
    nav_z > max(card_z or [0]), {"nav_z": nav_z, "card_max": max(card_z or [0])})
chk("页码那排的注释讲清了这两件事（下缘不越界 + 万一越了也看得见）",
    SRC.count("页码") >= 2, SRC.count("页码"))

bad = [c for c in checks if not c[0]]
print("\n叠卡几何体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
