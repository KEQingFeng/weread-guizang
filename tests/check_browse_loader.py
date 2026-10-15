#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置浏览器遮罩：切走再切回那一屏，网页中间不许永远压着「正在取回这一页」。

用户报的是「每次打开内置浏览器后，后面的页面中间会显示一行小字『正在取回这一页』」。
根子不在网络、也不在取页，而在 `bzLoad()` 里那两句的先后：

    if (load) load.classList.remove('off');          // 揭遮罩
    if (f && f.dataset.src !== url) { ... 换地址 ... } // ← 这个闸门只管 iframe

「同一地址不重发」这条闸门是上一轮为 `/api/state` 每 2.6 秒一轮加的：地址没变就不许
`f.src = …`，否则整页每几秒重刷一次。可遮罩的「揭」写在了闸门**外面** —— 于是每一次
重画这一屏（`setView('browse')` 切走再切回、或任何一次 `drawState(true)`）都会把遮罩
重新揭起来，而地址没变就不会再来一个 `load` 事件，`wireBrowse` 里那句合遮罩的
`classList.add('off')` 永远等不到。遮罩 `pointer-events:none`、只是 opacity 变 0，
揭着就是那行字实打实地压在网页中间，直到整个界面刷新。

修法：把「揭遮罩」挪进闸门里 —— 遮罩只属于「这一趟真发了请求」。

这一条不开浏览器、不联网，但也不是读源码形状：把 ui.html 里真实的 `bzLoad`（连同它
调用到的 `bzCur` / `bzPush` / `bzBar`）抽出来，在 node 里配一套假 DOM 原样跑，验四件事：
  ① 首次取页 / 换地址 → 遮罩揭起、iframe 真换地址；
  ② 同一地址重画（切走再切回 / drawState(true)）→ 遮罩保持合上、iframe 不重发请求；
  ③ 历史后退回到已加载过的页面（地址变了）→ 该揭还得揭；
  ④ 对照：把「揭遮罩」改回闸门外（复现旧写法）→ ②必须红，证明这条门禁不是空转。
再补两条静态不变量：`.bzload` 得 `pointer-events:none`（合上的遮罩不许吃点击），
`wireBrowse` 里得有那句合遮罩的 `add('off')`（有揭就得有合）。

跑法: .venv/bin/python tests/check_browse_loader.py
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = (REPO / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def brace_end(src, start):
    """从 `{` 开始配平花括号，回闭合处之后的位置。字符串 / 注释里的大括号不参与配平。"""
    i = src.index("{", start)
    depth = 0
    quote = None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif src.startswith("//", i):
            i = src.index("\n", i)
            continue
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("花括号没配平")


def slice_region(src):
    """抽 `const BZ_HOME` 到 `function bzLoad` 结尾这一段 —— 自洽、不碰 DOM 之外的全局。"""
    start = src.index("const BZ_HOME")
    head = src.index("function bzLoad(", start)
    return src[start:brace_end(src, head)]


try:
    REGION = slice_region(UI)
except ValueError as e:
    print("  FAIL 抽不出 bzLoad 那一段：%s" % e)
    sys.exit(1)

# ── 静态不变量：有揭就得有合，合上的遮罩不许吃点击 ──────────────────
m = re.search(r"<style>(.*?)</style>", UI, re.S)
CSS = m.group(1) if m else ""
bzload = re.search(r"\.bzload\s*\{([^}]*)\}", CSS)
chk("`.bzload` 的样式还在", bool(bzload), CSS[:80])
chk("`.bzload` 是 pointer-events:none（合上时不许挡住 iframe 的点击 / 滚动）",
    bool(bzload) and "pointer-events:none" in bzload.group(1).replace(" ", ""),
    bzload.group(1) if bzload else "")
chk("`.bzload.off` 用 opacity 合上遮罩",
    bool(re.search(r"\.bzload\.off\s*\{[^}]*opacity\s*:\s*0", CSS)), "")
chk("wireBrowse 里那句合遮罩的 add('off') 还在（load 事件必须有主）",
    bool(re.search(r"addEventListener\(\s*'load'|addEventListener\(\s*\"load\"", UI))
    and "classList.add('off')" in UI, "")

# ── 真跑 NODE：假 DOM + ui.html 里真实的 bzLoad ────────────────────
DRIVER = r"""
// 只要 bzLoad 用得上的那几样：dataset / src / value / classList / disabled。
function fakeEl() {
  const cls = new Set();
  return { dataset: {}, src: '', value: '', disabled: false,
    _cls: cls,
    classList: { remove: c => cls.delete(c), add: c => cls.add(c), has: c => cls.has(c) } };
}
const frame = fakeEl(), inp = fakeEl(), load = fakeEl(), back = fakeEl(), fwd = fakeEl();
const SELS = {'#bzFrame': frame, '#bzUrl': inp, '#bzLoad': load,
              '#bzBack': back, '#bzFwd': fwd};
const PANES = { browse: { querySelector: s => SELS[s] || null } };
function setView() {}            // openBrowse 用得到；这里不会被调
function ask() { return load._cls.has('off'); }

let fails = 0;
function chk(name, ok, extra) {
  if (!ok) fails += 1;
  console.log((ok ? '  ok  ' : ' FAIL ') + name + (ok ? '' : '  <- ' + (extra || '')));
}
function url(u) { return '/api/browse?url=' + encodeURIComponent(u); }

const A = 'https://a.example/', B = 'https://b.example/';

// ① 首次取页：网页还没回来，遮罩该揭着
bzLoad(A, true);
chk('首次取页：遮罩揭起（去 off）', !ask(), Array.from(load._cls));
chk('首次取页：iframe 指向 /api/browse', frame.src === url(A), frame.src);
chk('首次取页：地址栏跟着走', inp.value === A, inp.value);

// ② 网页回来了（wireBrowse 的 load 监听做的就是这一句）
load.classList.add('off');

// ③ 同一屏被重画：地址没变 → 不许再把遮罩揭起来（否则没有第二个 load 来合它）
bzLoad(A, false);
chk('同一地址重画：遮罩保持合上（不许再揭）', ask(), Array.from(load._cls));
chk('同一地址重画：iframe 不重发请求', frame.src === url(A), frame.src);

// ④ 换地址：该揭还得揭
bzLoad(B, true);
chk('换地址：遮罩重新揭起', !ask(), Array.from(load._cls));
chk('换地址：iframe 真的换过去', frame.src === url(B), frame.src);

// ⑤ 历史后退回到已经加载过的 A（地址变了 → 照样揭）
load.classList.add('off');
bzNav(-1);
chk('后退到上一页：遮罩重新揭起', !ask(), Array.from(load._cls));
chk('后退到上一页：iframe 指回 A', frame.src === url(A), frame.src);

process.exit(fails ? 1 : 0);
"""


def run_node(region, driver):
    node = shutil.which("node")
    if not node:
        return None, "PATH 里没有 node（内联 JS 那两条门禁也要它）"
    src = ("// 由 tests/check_browse_loader.py 从 ui.html 抽出，原样跑\n"
           + region + driver)
    with tempfile.TemporaryDirectory(prefix="guizang-bz-") as tmp:
        p = pathlib.Path(tmp) / "bz.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True)
    return r, (r.stdout + r.stderr)


# 正向：真实代码必须全绿
r, out = run_node(REGION, DRIVER)
chk("node 跑得动抽取出来的代码", r is not None, out)
if r is not None:
    for line in out.splitlines():
        if line.strip():
            print("    " + line.rstrip())
    chk("真实 bzLoad：同一地址重画不许再揭遮罩，换地址才揭（行为全绿）", r.returncode == 0, out)

# 对照：把「揭遮罩」挪回闸门外（也就是这一轮修掉的那种写法），第二条必须红。
MUTANT = re.sub(r"[ \t]*if \(load\) load\.classList\.remove\('off'\);\n", "", REGION, count=1)
MUTANT = MUTANT.replace("if (inp) inp.value = url;",
                        "if (inp) inp.value = url;\n  if (load) load.classList.remove('off');", 1)
chk("对照夹具确实改动了（揭遮罩那行还在、位置挪了）",
    MUTANT != REGION and "classList.remove('off')" in MUTANT)
r2, out2 = run_node(MUTANT, DRIVER)
chk("对照：旧写法（揭遮罩在闸门外）会红 —— 说明这条门禁抓得住回退",
    r2 is not None and r2.returncode != 0, out2)

print()
bad = [n for ok, n in checks if not ok]
if bad:
    print("内置浏览器遮罩：失败 %d 项" % len(bad))
    for n in bad:
        print("  ✗ " + n)
    sys.exit(1)
print("内置浏览器遮罩：通过 %d 项，失败 0 项" % len(checks))
