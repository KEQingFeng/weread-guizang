#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地书架 / 剪藏两格也把封面地址暂存起来，等网格挂上文档再按「滚到眼前」点亮。

前两条门禁修的是书架那一面：`check_cover_lazy.py` 管瀑布的 `extCoverImg()` +
`paintCovers()`，`check_cover_stack_lazy.py` 管叠卡的 `paintStackCovers()`。可书库里
还有一处**自己造封面图**的地方 —— `localCard()`：

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.src = '/api/cover?book=' + encodeURIComponent(id);   // ← 游离节点上就赋了 src

`localCard()` 是「本地书架」和「已入库的剪藏」两格共用的卡片。这两格的网格
（`renderLocalGrid()` / `renderClipShelf()`）都是**一次把整格的书全铺进 `.sgrid`**，
没有书架那种分批：本地的书有多少本就铺多少张。而 img 是在卡片还没挂进文档时就赋了
src 的，Chromium 对游离元素没有位置可判，`loading="lazy"` 形同虚设 —— 进一次本地
书架就有多少个封面请求同时砸出去，和瀑布、叠卡当初是同一个病，只是漏了这两面。

修法与书架那面完全一致，不再另起炉灶：
  ① `localCard()` 改调 `extCoverImg()`（只把地址暂存在 `data-cover` 上）；
  ② `renderLocalGrid()` / `renderClipShelf()` 在网格挂进文档之后各补一次
     `paintCovers(grid)`。

安全前提是这两格在调 `paintCovers()` 时**已经显出来**了：`renderLocalView()` /
`renderClipView()` 都先 `viewPane(...)`，而 `viewPane()` → `paneEl()` 里面那句
`PANES[k].hidden = (k !== name)` 就是把这一格显出来的地方。就算将来切换时机改坏了，
`paintCovers()` 自己也会跳过「没有布局盒」的卡（`offsetParent === null`），不会把
藏着的整墙一次点亮，只是要等一次 resize / 切回来的补点亮。

这一套照旧不依赖浏览器，分两半：

  ① 静态段：只读 `ui.html` 源码，钉住上面那两处接线，以及「壁纸别的收口没被顺手拆掉」。
  ② 真调用段：把 `paintCovers` / `coverField` 两段源码抽出来，配一副假 DOM 在 node
     里真调一遍 —— 视口附近的亮、一屏开外的先不亮、藏着的 pane 一张都不亮，等它
     显出来再补点亮。这是「先 viewPane 再 paintCovers」这个前提的另一半保障。
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    """截出 `function <name>(...) { ... }` 的函数体（到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


def fn_src(name):
    """截出整个 `function <name>(...) { ... }`（含签名，到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{.*?\n\}" % re.escape(name), SRC, re.S)
    return m.group(0) if m else ""


def _lit_before(body, mark, call):
    """`call` 是不是排在 `mark` 之后（不相干时返回 False，好让断言直接红）。"""
    i, j = body.find(mark), body.find(call)
    return i >= 0 and j >= 0 and j > i


# ── ① localCard 不再在游离节点上给封面赋 src ──────────────────────
local = fn_body("localCard")
chk("localCard：封面改走 extCoverImg（只暂存 data-cover，不在这儿赋 src）",
    "extCoverImg(" in local and not re.search(r"^\s*img\.src\s*=", local, re.M), local[:300])
chk("localCard：不再有那句已失效的 `img.complete && img.naturalWidth`（没 src 时永远是假）",
    "img.complete" not in local, local[:300])
chk("localCard：封面挂了仍然换书脊兜底（onerror → ph 那条没被拿掉）",
    "img.onerror" in local and "replaceWith(ph())" in local, local[:300])
chk("localCard：到了再淡入（onload → .in）仍在",
    "img.onload" in local and "classList.add('in')" in local, local[:300])

# ── ② 两格网格铺完之后各补一次点亮 ────────────────────────────────
lgrid = fn_body("renderLocalGrid")
cgrid = fn_body("renderClipShelf")
chk("renderLocalGrid：网格挂进文档之后立刻 paintCovers(grid)（首屏不能等第一次滚动）",
    _lit_before(lgrid, "wrap.replaceChildren(grid)", "paintCovers(grid)"), lgrid[-400:])
chk("renderClipShelf：同一处同样补一次 paintCovers(grid)",
    _lit_before(cgrid, "wrap.replaceChildren(grid)", "paintCovers(grid)"), cgrid[-400:])
chk("renderLocalGrid / renderClipShelf 都在卡片造完之后才点亮（不是造之前）",
    lgrid.find("localCard(") < lgrid.find("paintCovers(grid)")
    and cgrid.find("localCard(") < cgrid.find("paintCovers(grid)"), "点亮排在了建卡前面")

# ── ③ 前提：调 paintCovers 时这一格已经显出来 ─────────────────────
lview = fn_body("renderLocalView")
cview = fn_body("renderClipView")
chk("renderLocalView：先 viewPane('local') 再 renderLocalGrid（paneEl 顺带把这一格 hidden=false）",
    _lit_before(lview, "viewPane('local'", "renderLocalGrid(body)"), lview[:300])
chk("renderClipView：先 viewPane('clip') 再 renderClipShelf",
    _lit_before(cview, "viewPane('clip'", "renderClipShelf()"), cview[:300])
pel = fn_body("paneEl")
chk("paneEl：切到这一格时把这一格 hidden=false（paintCovers 量得到布局盒）",
    re.search(r"PANES\[k\]\.hidden\s*=\s*\(k\s*!==\s*name\)", pel), pel[:300])
paint = fn_body("paintCovers")
chk("paintCovers：仍然跳过藏着的 pane（offsetParent === null），切换时机改坏也不会点亮整墙",
    "offsetParent === null" in paint, paint[:300])

# ── ④ 没有别处再把封面地址直接糊到 src 上 ─────────────────────────
chk("全文件再无「当场把字面 /api/cover 赋给 img.src」的写法",
    not re.search(r"\.src\s*=\s*['\"]\/api\/cover", SRC), "还有游离节点赋 src 的口子")

# ── ⑤ 与书架那两面不能互相拆台 ────────────────────────────────────
ext = fn_body("extCoverImg")
chk("extCoverImg 仍然只暂存 data-cover（本地书架这条路也吃它）",
    "dataset.cover" in ext and not re.search(r"^\s*im\.src\s*=", ext, re.M), ext[:240])
chk("瀑布那面没被误伤：renderShelfGrid 仍调 paintCovers(grid)",
    "paintCovers(grid)" in fn_body("renderShelfGrid"), "瀑布封面的接线被动了")
chk("叠卡那面没被误伤：paintStackCovers 仍在用",
    "paintStackCovers(" in fn_body("renderShelfStack")
    or "paintStackCovers(" in fn_body("shelfStep"), "叠卡封面的接线被动了")
chk("详情页/远端封面那条「立刻显示」的路没受影响（coverSrc 归一与 no-referrer 都在）",
    "esc(coverSrc(coverUrl))" in SRC and "im.referrerPolicy = 'no-referrer'" in ext,
    "详情大图或 Referer 收口被拆了")


# ── ⑥ 假 DOM 里真调一遍：藏着的 pane 一张不亮，显出来再补 ──────────
def node_checks():
    """把 `paintCovers` / `coverField` 抽出来，在 node 里配假 DOM 真调一遍。

    静态段只能钉住「源码长什么样」；这一段量的是「真给它几张卡时点亮了哪几张」——
    视口附近的亮、一屏开外的先不亮、藏着的 pane 一张都不亮，等它显出来再补点亮。
    这正是「renderLocalGrid 里先 viewPane 再 paintCovers」这个前提的另一半保障：
    万一前面那一步的时序将来改坏了，封面也不会丢，只是要等下一次补点亮。
    """
    pfn = fn_src("paintCovers")
    ffn = fn_src("coverField")
    if not (pfn and ffn):
        return [("封面点亮器抽得出来（跑真调用要用它）", False, (bool(pfn), bool(ffn)))]
    node = shutil.which("node")
    if not node:
        return [("node 在不在（这套要用它跑界面源码）", False, "找不到 node")]
    harness = r"""
'use strict';
const FETCHED = [];
const window = {innerHeight: 800};
const document = {documentElement: {clientHeight: 800}};
function mkImg(url){
  return {
    dataset: {cover: url},
    _src: null,
    getAttribute(n){ return n === 'src' ? (this._src || null) : null; },
    get src(){ return this._src; },
    set src(v){ this._src = v; FETCHED.push(v); }
  };
}
// 一张卡：offsetParent 为 null 就是「藏着的 pane」（paintCovers 的那把尺子）
function mkCard(id, top, bottom, hidden){
  const im = mkImg('/cover/' + id + '.jpg');
  const card = {
    dataset: {}, offsetParent: hidden ? null : {},
    getBoundingClientRect(){ return {top: top, bottom: bottom}; },
    querySelectorAll(sel){ return sel === 'img[data-cover]' ? [im] : []; }
  };
  im.closest = sel => sel === '.wcard' ? card : null;
  card.im = im;
  return card;
}
__FIELD__
__PAINT__
const near = mkCard('near', 100, 400, false);      // 视口里
const above = mkCard('above', -700, -100, false);  // 视口往上不到一屏（该亮）
const far = mkCard('far', 5000, 5300, false);      // 一屏开外（先不亮）
const hidden = mkCard('hidden', 100, 400, true);   // 藏着的格（一张都不许亮）
const grid = {querySelectorAll(sel){
  return sel === 'img[data-cover]' ? [near.im, above.im, far.im, hidden.im] : [];
}};
const results = [];
function step(name, ok, extra){ results.push([name, !!ok, extra == null ? '' : String(extra)]); }

const k = paintCovers(grid);
step('视口里 + 往上不到一屏的点亮，一屏开外与藏着的都不亮',
     k === 2 && FETCHED.length === 2, 'k=' + k + ' fetched=' + FETCHED.length);
step('点亮的正是 near / above 两张',
     FETCHED.join(',') === '/cover/near.jpg,/cover/above.jpg', FETCHED.join(','));
paintCovers(grid);
step('再调一次不重发（src 已在的跳过）', FETCHED.length === 2, 'fetched=' + FETCHED.length);
hidden.offsetParent = {};                          // 这一格显出来了
paintCovers(grid);
step('藏着的那张等它显出来才补点亮（封面不会丢）',
     FETCHED.length === 3 && FETCHED[2] === '/cover/hidden.jpg', FETCHED.join(','));
far.getBoundingClientRect = () => ({top: 100, bottom: 400});   // 滚到跟前了
paintCovers(grid);
step('滚动到跟前的那张也补上了', FETCHED.length === 4 && FETCHED[3] === '/cover/far.jpg',
     FETCHED.join(','));
step('传 null 不抛、回 0', paintCovers(null) === 0);
console.log('GZRESULT ' + JSON.stringify(results));
"""
    harness = harness.replace("__FIELD__", ffn).replace("__PAINT__", pfn)
    with tempfile.TemporaryDirectory(prefix="guizang-localcover-") as tmp:
        js = pathlib.Path(tmp) / "localcover.js"
        js.write_text(harness, encoding="utf-8")
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
    line = [x for x in r.stdout.splitlines() if x.startswith("GZRESULT ")]
    if r.returncode or not line:
        return [("假 DOM 里跑得起来", False, (r.stderr or r.stdout).strip()[-300:])]
    out = []
    for name, ok, extra in json.loads(line[-1][len("GZRESULT "):]):
        out.append(("真调用：%s" % name, bool(ok), "" if ok else extra))
    return out


for name, ok, extra in node_checks():
    chk(name, ok, extra)

bad = [c for c in checks if not c[0]]
print("\n本地书架封面按需加载体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
