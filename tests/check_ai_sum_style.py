# -*- coding: utf-8 -*-
"""AI 小结的「学习理解 / 归纳」这两颗模式钮，点得动、不偷改阅读偏好。

用户报的口径是「切到学习理解没反应」。根因不在 ai_sum.py（两种模式的提示词、缓存名、
落盘路径都对，`check_ai_sum.py` 一直是绿的），而在界面这**一次点击的归属**：

    wireReader 的点击委托原来这样认设置面板 ——

        const seg = e.target.closest('.rdseg button');
        if (seg && p.contains(seg)) { rdPickCfg(seg); return; }   // ← 先到先得

    `.rdseg` 是「分段控件长什么样」的**外观类**，不是「这一行是阅读偏好」的记号；
    小结的模式条 `#aiSumStyle` 为了长得像也挂着它，却没有 `data-rd`。于是点
    「学习理解」先落进 rdPickCfg：

        const key = segs.dataset.rd;   // undefined —— 模式钮用的是 data-k
        rdCfg[undefined] = ...;        // 账本里凭空多一个 'undefined' 键
        rdSaveCfg(); rdApplyCfg();     // 还顺手按阅读偏好重贴了一遍
        return;                        // ← 小结那一侧永远收不到这一下

    表现就是「按钮按下去没动静」：模式没换（提示词仍是归纳那套）、learn 那份不落盘、
    重开读的还是归纳。真机套件 `check_ai_summary_ui.py` 里红的那 6 条全是这一条的后果。
    `rdApplyCfg()` 那一下还顺手把模式钮的 `.on` 抹掉（它同步的是 `b.dataset.v`，
    模式钮用的是 `data-k`）—— 于是点完谁的亮都没了，看着更像坏了。

顺带修掉同一个类名误用的第二面：`rdApplyCfg` 原来也按裸 `.rdseg` 一把抓，
    星号同步每一段里的 `.on`。就算第一次点击被修好，只要用户在小结开着的时候改一次
    字号，模式条上那颗高亮也会被抹掉。

这一套不用浏览器也跑得动，钉住四件事：

  A 结构：阅读偏好那五行是 `[data-rd]`；模式条 `#aiSumStyle` 不挂这个记号。
  B 接线：点击委托与 rdApplyCfg 认的是 `.rdseg[data-rd]`，不是裸 `.rdseg`。
  C 纯逻辑（node）：把 ui.html 里**真的**那份 rdPickCfg 抽出来，喂一个没有 data-rd 的
    分段 —— 账本一个键不许动、rdSaveCfg / rdApplyCfg 一次都不许叫；换成真设置行
    （data-rd="size"）照旧要改得动。
  D 对照：改之前那一份（rdCfg[key] 前没有 !key 那道闸）拿同一个脏分段跑一遍，必须真
    在账本上留下 'undefined' 键、真叫了那两支后手 —— 免得这道闸被拿掉之后这条还在空转。
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "ui.html").read_text(encoding="utf-8")

PASSED = 0
FAIL = []


def chk(name, ok, extra=""):
    global PASSED
    if ok:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + ("  <- " + str(extra)[:300]))


def body(sig):
    """从 ui.html 里抠出某个函数**产品里那一份**的源码（到行首的 } 为止）。"""
    m = re.search(re.escape(sig) + r"\s*\{.*?\n\}", SRC, re.S)
    return m.group(0) if m else ""


# ── A 结构：谁挂 data-rd，谁不挂 ──────────────────────────────
print("── A 摘要：阅读偏好五行带 data-rd，模式条不挂 ──")
cfg_def = re.search(r"const RD_CFG_DEF\s*=\s*\{([^}]*)\}", SRC)
chk("A1 找得到 RD_CFG_DEF（阅读偏好键的唯一真相）", bool(cfg_def), cfg_def)
def_keys = set(re.findall(r"(\w+)\s*:", cfg_def.group(1))) if cfg_def else set()

rd_keys = re.findall(r'class="rdseg"\s+data-rd="(\w+)"', SRC)
chk("A2 阅读偏好那五行（字号/行距/栏宽/字体/底色）都挂 data-rd",
    set(rd_keys) == def_keys and len(rd_keys) == len(def_keys),
    "挂记号的 %s / 配置里的 %s" % (sorted(rd_keys), sorted(def_keys)))
tag = re.search(r'<div[^>]*id="aiSumStyle"[^>]*>', SRC)
chk("A3 借样子的模式条不挂 data-rd（它就是被误抓的那一条）",
    bool(tag) and 'class="rdseg rdseg-fit"' in tag.group(0)
    and "data-rd" not in tag.group(0),
    tag.group(0) if tag else "找不到 #aiSumStyle")

# ── B 接线：认的是 [data-rd]，不是裸类名 ─────────────────────
print()
print("── B 摘要：委托与贴配置都认 [data-rd] ──")
pick = body("function rdPickCfg(seg)")
apply_ = body("function rdApplyCfg()")
chk("B1 点击委托认的是 .rdseg[data-rd] button（点「学习理解」不再被设置那一支吞掉）",
    ".rdseg[data-rd] button" in SRC and "closest('.rdseg button')" not in SRC)
chk("B2 rdPickCfg 自己挡没带 data-rd 的分段（值不许写进 rdCfg 的 undefined 键）",
    "dataset.rd" in pick and "if (!key) return;" in pick and
    pick.index("if (!key) return;") < pick.index("rdCfg[key]"), pick)
chk("B3 rdApplyCfg 只同步 [data-rd] 那几行的选中态"
    "（不然改一次字号就把模式条的 on 抹了）",
    "querySelectorAll('.rdseg[data-rd]')" in apply_ and
    "querySelectorAll('.rdseg')" not in apply_,
    apply_[:240])
selectors = sorted(set(re.findall(r"['\"](\.rdseg[^'\"]*)['\"]", SRC)))
chk("B4 裸 .rdseg 选择器只剩 rdPickCfg 里读数据集那一处（就那么一处，还带着 B2 的闸）",
    [s for s in selectors if "[data-rd]" not in s] == [".rdseg"] and
    pick.count("closest('.rdseg')") == 1 and
    ".rdseg[data-rd][" not in SRC, selectors)

# ── C/D node：抽产品那一份 rdPickCfg 跑一遍 ──────────────────
print()
print("── C 摘要：脏分段不动账，真设置行照旧动 ──")
node = shutil.which("node")
if not node:
    print("SKIP  纯逻辑那一段：本机没有 node（这条不能算过，装了再跑）")
elif not pick:
    chk("C1 能从 ui.html 里抽到 rdPickCfg", False, "抽不到函式体")
else:
    harness = """
let saved = 0, applied = 0;
const rdCfg = {size: 15, lh: 1.92, w: 70, font: 'sans', theme: 'auto'};
function rdSaveCfg() { saved++; }
function rdApplyCfg() { applied++; }
""" + pick + """
// 对照：改之前那一份（rdCfg[key] 前没有 !key 那道闸）
function oldPickCfg(seg) {
  const segs = seg.closest('.rdseg'); if (!segs) return;
  const key = segs.dataset.rd;
  let v = seg.dataset.v;
  if (key === 'size' || key === 'w' || key === 'lh') v = +v;
  rdCfg[key] = v;
  rdSaveCfg();
  rdApplyCfg();
}
const styleBtn = {dataset: {k: 'learn'}, closest: () => ({dataset: {}})};
const sizeBtn = {dataset: {v: '14'}, closest: () => ({dataset: {rd: 'size'}})};
const known = {size: 1, lh: 1, w: 1, font: 1, theme: 1};

// ① 对照先跑：老写法必须真在账本上留脏键、真叫那两支后手，否则这条套件在空转
oldPickCfg(styleBtn);
const controlJunk = Object.keys(rdCfg).filter(k => !(k in known));
const controlSaved = saved, controlApplied = applied;

// 重置成干净账
Object.keys(rdCfg).forEach(k => { delete rdCfg[k]; });
Object.assign(rdCfg, {size: 15, lh: 1.92, w: 70, font: 'sans', theme: 'auto'});
saved = 0; applied = 0;

// ② 产品那一份：借样子的分段一个键都不许动、一次都不许重贴
rdPickCfg(styleBtn);
const cleanKeys = Object.keys(rdCfg).sort().join(',');
const styleSaved = saved, styleApplied = applied;

// ③ 真设置行照旧要改得动
rdPickCfg(sizeBtn);
console.log(JSON.stringify({controlJunk, controlSaved, controlApplied, cleanKeys,
                            styleSaved, styleApplied, size: rdCfg.size, sizeSaved: saved}));
"""
    try:
        out = subprocess.run([node, "-e", harness], capture_output=True, text=True,
                             timeout=30)
        got = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        got = {"err": "%s: %s | %s" % (type(e).__name__, e,
                                       (locals().get("out") and out.stderr) or "")}

    if "err" in got:
        chk("C1 抽出来的 rdPickCfg 能在 node 里跑起来", False, got)
    else:
        chk("D1 对照：改之前那一份会凭空写出 'undefined' 键、并叫上两支后手",
            got.get("controlJunk") == ["undefined"] and got.get("controlSaved") == 1
            and got.get("controlApplied") == 1, got)
        chk("C1 脏分段进 rdPickCfg：账本键一个不多、一个不少",
            got.get("cleanKeys") == "font,lh,size,theme,w", got)
        chk("C2 脏分段进 rdPickCfg：不许落盘、不许按阅读偏好重贴一遍",
            got.get("styleSaved") == 0 and got.get("styleApplied") == 0, got)
        chk("C3 真设置行（data-rd=size）照旧改得动，值也照旧按数字收",
            got.get("size") == 14 and got.get("sizeSaved") == 1, got)

print()
if FAIL:
    print("AI 小结模式条门禁：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("AI 小结模式条：通过 %d 项，失败 0 项" % PASSED)
