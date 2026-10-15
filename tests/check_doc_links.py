# -*- coding: utf-8 -*-
"""文档里的相对链接、正文点名的安装包，都不许指着不存在的东西。

为什么这条值得单独一个离线门禁
    开发规范 §8 把「README 说谎比没有 README 更糟」写成硬规矩：安装段里那个
    `归藏-x.y.z.dmg` 必须是 `安装包/` 里真实存在的那一个，「不构建不等于可以宣称
    一个不存在的包」，引的展示图也得对得上当前界面。可这条全靠人记得，谁也没拦。
    `docs/部署说明.md` 就是这么漂的：它写
    `[安装包/归藏-1.0.9.dmg](安装包/归藏-1.0.9.dmg)`，而 markdown 的相对链接是按
    **那份文档自己所在的目录**解析的 —— GitHub 上这一点，就指到了不存在的
    `docs/安装包/…`，文件明明在仓库里、链接却是坏的。同一份文档引 README 时用的是
    `../README.md`，可见正确写法本来就在旁边。

这一条做三件事
    1. `README.md` 与 `docs/*.md` 里每条仓库内相对链接 / 图片都解析得开（按所在目录解析，
       `http(s):` / `mailto:` / `#锚点` / 绝对路径 / 带协议的外链都跳过）；
    2. 正文里点名的 `安装包/xxx.dmg|zip|exe` 也确实躺在仓库根下 —— 占位与通配写法
       （`<版本>`、`*.dmg`）跳过，它们本来就不该指某个具体文件；
    3. `安装包/` 只留最新那一份：最多一个 dmg、最多一个 Windows zip，且两者版本号一致
       （别一个 1.0.9 一个 1.0.8）。

另外带两条「门禁自己会红」的对照：拿一条**故意种出来的**不存在的链接 / 包名喂给同一把
尺子，它必须报出来；再拿真存在的喂一遍，不许误报。没有这两条，上面那些「绿」证明不了
尺子还在量东西。

纯文件走查，不起服务、不联网、不打包，归 --fast。
"""
import os
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "安装包")

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 相对链接：README + docs/*.md ───────────────────────────────
# 只认行内形态 `[文字](目标)` / `![说明](目标)`，跳过引用式链接（`[x]: url`）——
# 本仓库的文档不用那种写法，真用了也不该被这里误判成坏链。
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")


def relative_targets(text):
    """吐出这份文档里所有「该按仓库内路径解析」的目标（原样串 + 去掉锚点/查询串的路径）。"""
    for m in LINK.finditer(text):
        raw = m.group(1).strip()
        if not raw or raw.startswith("#"):
            continue
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or raw.startswith("/"):
            continue
        target = unquote(parts.path)
        # 省略号是文档里写「这类路径」的占位，本来就不指某个具体文件，别当坏链。
        if target and "…" not in target and "..." not in target:
            yield raw, target


def broken_links(doc_rel, text):
    base = os.path.dirname(os.path.join(ROOT, doc_rel))
    out = []
    for raw, target in relative_targets(text):
        if not os.path.exists(os.path.normpath(os.path.join(base, target))):
            out.append(raw)
    return out


# ── 正文点名的包：`安装包/xxx.dmg|zip|exe`，一律按仓库根解析 ─────────
PKG_MENTION = re.compile(r"安装包/([^\s`）)】、，。；;\"'<>*]+\.(?:dmg|zip|exe))")


def missing_pkg_mentions(text):
    out = []
    for m in PKG_MENTION.finditer(text):
        name = m.group(1)
        if "…" in name or "..." in name:                              # 省略号占位，同上
            continue
        if not os.path.exists(os.path.join(PKG_DIR, name)):
            out.append(name)
    return out


DOC_FILES = ["README.md"] + [
    "docs/" + n for n in sorted(os.listdir(os.path.join(ROOT, "docs"))) if n.endswith(".md")
]

texts = {}
for rel in DOC_FILES:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        texts[rel] = f.read()

for rel in DOC_FILES:
    bad = broken_links(rel, texts[rel])
    chk("%s 的仓库内相对链接都指得到（按文档所在目录解析）" % rel, not bad, bad)
    miss = missing_pkg_mentions(texts[rel])
    chk("%s 点名的安装包都在 安装包/ 里" % rel, not miss, miss)

n_targets = sum(len(list(relative_targets(t))) for t in texts.values())
chk("读写走的是同一把尺子且没退化成空表（读到的仓库内链接 > 20 条）",
    n_targets > 20, n_targets)

# ── 安装包目录：只留最新那一份 ─────────────────────────────────
try:
    pkg_names = sorted(os.listdir(PKG_DIR))
except OSError as e:                                                  # pragma: no cover
    pkg_names = []
    chk("安装包/ 目录在", False, str(e))

dmgs = [n for n in pkg_names if n.endswith(".dmg")]
wins = [n for n in pkg_names if n.startswith("归藏-Windows-") and n.endswith(".zip")]
chk("安装包/ 只留最新那一份 dmg", len(dmgs) <= 1, dmgs)
chk("安装包/ 只留最新那一份 Windows zip", len(wins) <= 1, wins)

VER = re.compile(r"(\d+\.\d+\.\d+)")
d_v = VER.search(dmgs[0]).group(1) if dmgs and VER.search(dmgs[0]) else None
z_v = VER.search(wins[0]).group(1) if wins and VER.search(wins[0]) else None
chk("dmg 与 Windows zip 是同一版（别一个 1.0.9 一个 1.0.8）",
    d_v is None or z_v is None or d_v == z_v, (dmgs, wins))

# ── 门禁自己会红：种一条坏的，再拿一条好的当对照 ─────────────────
planted = "[包](../../安装包/归藏-9.9.9.dmg)"
chk("门禁自己会红：种一条不存在的相对链接能被逮住",
    broken_links("docs/部署说明.md", planted) == ["../../安装包/归藏-9.9.9.dmg"],
    broken_links("docs/部署说明.md", planted))
chk("对照：真存在的相对链接不误报",
    broken_links("docs/部署说明.md", "[README](../README.md)") == [],
    broken_links("docs/部署说明.md", "[README](../README.md)"))
chk("门禁自己会红：正文点名一个不存在的包能被逮住",
    missing_pkg_mentions("见 `安装包/归藏-9.9.9.dmg`") == ["归藏-9.9.9.dmg"],
    missing_pkg_mentions("见 `安装包/归藏-9.9.9.dmg`"))
chk("占位 / 通配写法不误报（它们本来就不指某个具体文件）",
    missing_pkg_mentions("产出 安装包/归藏-Windows-<版本>.zip、安装包/*.dmg，"
                         "正文里那种 安装包/… 的写法") == [],
    missing_pkg_mentions("产出 安装包/归藏-Windows-<版本>.zip、安装包/*.dmg，"
                         "正文里那种 安装包/… 的写法"))
chk("对照：省略号占位的链接目标不当坏链",
    broken_links("docs/开发规范.md", "[这类路径](安装包/…)") == [],
    broken_links("docs/开发规范.md", "[这类路径](安装包/…)"))

print()
if FAIL:
    print("文档链接与安装包：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("文档链接与安装包：通过 %d 项，失败 0 项" % PASSED)
