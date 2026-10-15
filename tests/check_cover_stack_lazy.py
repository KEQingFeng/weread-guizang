#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""叠卡书架也只点亮扇形里看得见的封面，不是把整批一起砸出去。

`check_cover_lazy.py` 修的是瀑布那一面：封面不在游离节点上赋 src，改由 `paintCovers()`
按「滚到视口附近」择时点亮。但叠卡那一面当时留了一句未经核对的假设 ——
「叠卡一次摊 7 张，都在视口里，所以这里直接把这一屏的封面都点亮」，于是写成了：

    paintCovers(box.querySelector('.sstage'), Infinity);

这句注释的前半句是错的。`renderShelfStack()` 拿到的 `shown` 是**整批**（`shelfShown`
起步就是 `SHELF_BATCH = 60`），`fillGrid()` 把这 60 张全塞进 `.sstage`，`applyStack()`
只是给离得远的那几张贴上 `opacity:0` 的 `saway` / `sgone` —— 它们仍留在文档里，
`.sstage>.wcard` 又是 `position:absolute`，所以 `offsetParent` 不为 null。margin 传
`Infinity` 等于「谁都不用量」，60 张封面一个不落地全点亮。

2026-10-16 用 Playwright + 60 本假书实测（拦 `/api/cover` 记数，1x1 真 PNG 回状）：

    修前  叠卡：60 个 /api/cover 一次发出    瀑布：25 个（视口 ± 一屏）
    修后  叠卡：4 个（光标在最前，扇形 = s0..s3）  翻 4 下 +4（每次翻进来一张）

也就是说：同一条「不预取看不见的那一批」的规矩，在叠卡这一面被漏掉了，用户进一次叠卡
书架照样是 60 个并发。修法是新增 `paintStackCovers()`：只遍历 `stage.children`，跳过
`applyStack()` 自己打上 `saway` / `sgone` 的那几张 —— 判据只留一处，容器查询把 ±3 收窄
成 ±2 / ±1 时也不会分叉；`shelfStep()` 翻完再点一次，新进扇形的那几张就赶上了。

这一套不依赖浏览器，分两半：

  ① 静态段：只读 `ui.html` 源码，钉住接线（与 `check_cover_lazy.py` 同一路子）。
  ② 真调用段：把 `paintStackCovers` / `coverField` 两段源码抽出来，配一副假 DOM
     在 node 里真调一遍 —— 60 张假卡（7 张在扇形、53 张 saway / sgone），
     数它到底点亮了几张、翻页补没补上新进来的那张。假卡故意不实现
     `getBoundingClientRect`，谁把「按视口判」那套塞回来，调用当场就抛。

真浏览器上那份条数实测（60 → 4）仍写在上面，只作旁证；常驻的是这 19 条。
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


stack = fn_body("renderShelfStack")
step = fn_body("shelfStep")
stackpaint = fn_body("paintStackCovers")

# ── ① 前提：叠卡舞台里坐着的是整批，不是 7 张 ────────────────────────
chk("renderShelfStack 把整批 shown 交给 fillGrid（「一次只摊 7 张」那句注释是错的）",
    re.search(r"fillGrid\(\s*box\.querySelector\('\.sstage'\)\s*,\s*shown\s*\)", stack),
    stack[:300])
chk("applyStack 把画外那几张仍留在文档里（opacity:0 的 saway / sgone，不是 display:none）",
    ".sstage>.wcard{--sw:" in SRC and ".saway{opacity:0" in SRC
    and ".sgone{opacity:0" in SRC, "叠卡画外的收法变了")

# ── ② 那个「整批全点亮」的口子必须堵上 ──────────────────────────────
chk("没有任何 paintCovers(...) 调用再把 margin 传成 Infinity（那就是整批全点亮）",
    not re.search(r"paintCovers\([^)]*Infinity", SRC), "还能找到 paintCovers(…, Infinity)")
chk("renderShelfStack 不再用 paintCovers 点叠卡的封面，改用受限的 paintStackCovers",
    "paintStackCovers" in stack and "paintCovers" not in stack, stack[:300])

# ── ③ 受限的点亮器：跳过画出扇形的那几张 ───────────────────────────
chk("paintStackCovers 存在（叠卡封面按扇形点亮的总开关）", bool(stackpaint), "找不到")
chk("paintStackCovers 跳过 saway / sgone（判据复用 applyStack 打的类，不另算一遍距离）",
    "saway" in stackpaint and "sgone" in stackpaint and "continue" in stackpaint,
    stackpaint[:300])
chk("paintStackCovers 只走 stage.children，逐张卡取封面（不对整墙做一次大查询）",
    "stage.children" in stackpaint and "c.querySelectorAll('img[data-cover]')" in stackpaint,
    stackpaint[:300])
chk("paintStackCovers 走的是同一个 coverField（与瀑布共用一处赋 src，不另开一条路）",
    "coverField(" in stackpaint, stackpaint[:300])

# ── ④ 翻页时新进扇形的那几张要赶上 ─────────────────────────────────
chk("shelfStep 翻完（applyStack 之后）补一次 paintStackCovers",
    "applyStack(stage)" in step and "paintStackCovers(stage)" in step,
    "翻进来的卡片会停在没封面的空卡上")
step_paint_at = step.find("paintStackCovers(stage)")
step_stack_at = step.find("applyStack(stage)")
chk("这次补点亮排在 applyStack 之后（先换类，再按新扇形点亮）",
    0 <= step_stack_at < step_paint_at, "顺序反了：会按旧扇形点亮")

# ── ⑤ 瀑布那一面不能被动到 ─────────────────────────────────────────
chk("瀑布那面照旧：renderShelfGrid 仍用 paintCovers(grid) 做视口预留",
    "paintCovers(grid)" in fn_body("renderShelfGrid"), "瀑布的视口预留被误伤")
ext = fn_body("extCoverImg")
chk("extCoverImg 仍然只暂存 data-cover，不在游离节点上赋 src",
    "dataset.cover" in ext and not re.search(r"^\s*im\.src\s*=", ext, re.M), ext[:240])

# ── ⑥ 假 DOM 里真调一遍：拿 ui.html 里那两段源码跑 ───────────────────
def node_checks():
    """把 `paintStackCovers` / `coverField` 抽出来，配一副假 DOM 在 node 里真调一遍。

    ⑤ 之前那一整段只能钉住「源码长什么样」；这一段钉住「真给它 60 张卡时点亮了几张」——
    扇形里那几张亮、`saway` / `sgone` 那几张一个请求都不发、翻页只补新进扇形的那一张。
    假卡**故意不实现 `getBoundingClientRect`**：一旦有人把「按视口判」那套写法塞回这里，
    调用当场就抛，静态检查看不出来的那种回退也溜不过去。
    """
    paint = fn_src("paintStackCovers")
    field = fn_src("coverField")
    if not (paint and field):
        return [("叠卡点亮器抽得出来（跑真调用要用它）", False, (bool(paint), bool(field)))]
    node = shutil.which("node")
    if not node:
        return [("node 在不在（这套要用它跑界面源码）", False, "找不到 node")]
    harness = r"""
'use strict';
const FETCHED = [];
function mkImg(url){
  return {
    dataset: {cover: url},
    _src: null,
    getAttribute(n){ return n === 'src' ? (this._src || null) : null; },
    get src(){ return this._src; },
    set src(v){ this._src = v; FETCHED.push(v); }
  };
}
function mkCard(id, kind){
  const cls = new Set(kind ? [kind] : []);
  const imgs = [];
  return {
    id: id,
    classList: {
      contains(k){ return cls.has(k); },
      add(k){ cls.add(k); },
      remove(k){ cls.delete(k); }
    },
    querySelectorAll(sel){
      if (sel !== 'img[data-cover]') return [];
      return imgs.filter(im => 'cover' in im.dataset);
    },
    addCover(url){ imgs.push(mkImg(url)); }
  };
}
__COVER__
__PAINT__
// 60 本一批：光标在最前，前 7 张（s0..s3 / b1..b3）在扇形里，其余被甩出画外
const cards = [];
for (let i = 0; i < 60; i++){
  cards.push(mkCard('b' + i, i < 7 ? '' : (i < 34 ? 'saway' : 'sgone')));
}
cards.forEach((c, i) => c.addCover('/cover/' + i + '.jpg'));
const stage = {children: cards};
const results = [];
function step(name, ok, extra){ results.push([name, !!ok, extra == null ? '' : String(extra)]); }
const want = Array.from({length: 7}, (_, i) => '/cover/' + i + '.jpg').join(',');

const lit = paintStackCovers(stage);
step('60 张卡里只点亮扇形里那 7 张（不是整批全砸）', lit === 7 && FETCHED.length === 7,
     'lit=' + lit + ' fetched=' + FETCHED.length);
step('点亮的正是扇形那 7 张（saway / sgone 一张都没发）',
     FETCHED.join(',') === want, FETCHED.join(','));
paintStackCovers(stage);
step('再调一次不重发（src 已在的跳过）', FETCHED.length === 7, 'fetched=' + FETCHED.length);
// 翻一下：第 5 张被甩出去，第 8 张翻进扇形
cards[4].classList.remove('b1'); cards[4].classList.add('sgone');
cards[7].classList.remove('saway');
paintStackCovers(stage);
step('翻页只补新进扇形的那一张', FETCHED.length === 8, 'fetched=' + FETCHED.length);
step('补的确实是新进来的第 8 张', FETCHED[7] === '/cover/7.jpg', String(FETCHED[7]));
step('stage 传 null 不抛、回 0', paintStackCovers(null) === 0);
step('空舞台回 0', paintStackCovers({children: []}) === 0);
console.log('GZRESULT ' + JSON.stringify(results));
"""
    harness = harness.replace("__COVER__", field).replace("__PAINT__", paint)
    with tempfile.TemporaryDirectory(prefix="guizang-stackcover-") as tmp:
        js = pathlib.Path(tmp) / "stackcover.js"
        js.write_text(harness, encoding="utf-8")
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
    line = [x for x in r.stdout.splitlines() if x.startswith("GZRESULT ")]
    if r.returncode or not line:
        return [("假 DOM 里跑得起来", False,
                 (r.stderr or r.stdout).strip()[-300:])]
    out = []
    for name, ok, extra in json.loads(line[-1][len("GZRESULT "):]):
        out.append(("真调用：%s" % name, bool(ok), "" if ok else extra))
    return out


for name, ok, extra in node_checks():
    chk(name, ok, extra)

bad = [c for c in checks if not c[0]]
print("\n叠卡封面按需加载体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
