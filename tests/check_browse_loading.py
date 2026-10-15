#!/usr/bin/env python3
"""内置浏览器那行「正在取回这一页」只在该取的时候亮，取完 / 离开就收（issue #131）。

用户报的现象：打开过一次内置浏览器之后，后面的网页中间一直压着一行小字
（内置浏览器里那行「正在取回这一页」）。根因不在后端取页，而在这行提示的开关：

  `bzLoad()` 原来**无条件** `remove('off')`，可紧接着那句 `if (f.dataset.src !== url)`
  又拦住了同一地址的重发。于是「/api/state 每 2.6 秒一轮 / 重进这一屏」这类同地址
  调用会把提示重新掀开，却没有一次新请求、也就没有 iframe 的 `load` 事件来把它收掉 ——
  那一行就永久压在页面上。

这一条不联网、不起服务：从 ui.html 里把 `bzLoad` / `wireBrowse` / `bzBusy` 三段抽出来，
配一副假 DOM 在 node 里真调一遍，钉住三件事：

  A 「确实在取」才亮：换成新地址亮，取回（load）后收。
  B 「同一地址」不许再掀开，也不许真的重发 iframe（否则等于每几秒刷一次页面）。
  C 离开这一屏（paneEl）与刷新按钮也走同一个开关 —— 收尾只有一处。

另加几条静态接线检查，确认这三段还在、还连着。
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = (ROOT / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_src(name):
    """截出整个 `function <name>(...) { ... }`（到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{.*?\n\}" % re.escape(name), SRC, re.S)
    return m.group(0) if m else ""


def fn_body(name):
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


bz_load = fn_src("bzLoad")
wire = fn_src("wireBrowse")
busy = fn_src("bzBusy")
load_body = fn_body("bzLoad")
pane_body = fn_body("paneEl")
wire_body = fn_body("wireBrowse")

# ── 0. 这一屏还摆着那行提示，代码都抽得到 ────────────────────────
chk("界面里还有「正在取回这一页」那一行", 'id="bzLoad"' in SRC and "正在取回这一页" in SRC)
chk("三处开关都抽得出来（bzLoad / wireBrowse / bzBusy）",
    bool(bz_load) and bool(wire) and bool(busy),
    (bool(bz_load), bool(wire), bool(busy)))
chk("「正在取回」的开关收在 bzBusy 一处",
    "classList.toggle('off', !on)" in busy and "bzLoad" in busy)

# ── 1. 掀开提示只许发生在「真去取一个新地址」那一支里 ──────────────
guard = load_body.find("dataset.src !== url")
lift = load_body.find("bzBusy(true)")
chk("bzLoad 先判「地址变了没」，再决定亮不亮",
    0 <= guard < lift, (guard, lift))
chk("bzLoad 里不再有无条件的 classList.remove('off')（issue #131 的根因）",
    "classList.remove('off')" not in load_body, load_body)

# ── 2. 取回事件与离开这一屏，都把它收掉 ──────────────────────────
chk("iframe 的 load 事件收掉提示",
    "addEventListener('load'" in wire_body and "bzBusy(false)" in wire_body, wire_body)
chk("离开内置浏览器这一屏时也收掉（不留到别的页面）",
    "shownPane === 'browse'" in pane_body and "bzBusy(false)" in pane_body, pane_body)
chk("刷新按钮先亮提示再重取", "bzBusy(true)" in wire_body, wire_body)


# ── 3. 假 DOM 里真调一遍：拿 ui.html 里那两段源码跑 ───────────────
def node_checks():
    """把 ui.html 里的 bzLoad / wireBrowse / bzBusy 抽出来，在假 DOM 上跑一圈。"""
    if not (bz_load and wire):
        return []
    node = shutil.which("node")
    if not node:
        return [("node 在不在（这套要用它跑界面源码）", False, "找不到 node")]
    harness = """
'use strict';
const BZ_HOME = 'https://ai-bot.cn/';
const BZ = {hist: [], at: -1};
function bzCur(){ return BZ.hist[BZ.at] || ''; }
function bzPush(url){
  if (!url) return;
  if (bzCur() === url) return;
  BZ.hist = BZ.hist.slice(0, BZ.at + 1);
  BZ.hist.push(url);
  BZ.at = BZ.hist.length - 1;
}
function bzBar(){}
function mkLoad(){
  const off = new Set(['off']);
  return {off: off, classList: {
    remove(c){ off.delete(c); },
    add(c){ off.add(c); },
    toggle(c, on){ if (on) off.add(c); else off.delete(c); },
    contains(c){ return off.has(c); }
  }};
}
function mkFrame(){
  const listeners = {};
  const f = {dataset: {},
    addEventListener(ev, fn){ (listeners[ev] = listeners[ev] || []).push(fn); },
    fire(ev){ (listeners[ev] || []).forEach(fn => fn()); }};
  let src = '';
  Object.defineProperty(f, 'src', {get(){ return src; }, set(v){ src = v; }});
  return f;
}
function mkBtn(){ return {onclick: null, disabled: false, textContent: ''}; }
function mkInput(){ return {value: '', addEventListener(){}}; }
const frame = mkFrame(), load = mkLoad();
const els = {'#bzBack': mkBtn(), '#bzFwd': mkBtn(), '#bzReload': mkBtn(),
             '#bzHome': mkBtn(), '#bzGo': mkBtn(), '#bzUrl': mkInput(),
             '#bzFrame': frame, '#bzLoad': load};
const PANES = {browse: {querySelector(sel){ return els[sel] || null; }}};
__BUSY__
__BZLOAD__
__WIRE__
wireBrowse(PANES.browse);
const results = [];
function step(name, ok){ results.push([name, !!ok]); }
step('初始收着', load.classList.contains('off'));
bzLoad('https://a.cn/', true);
step('取新页时亮起', !load.classList.contains('off'));
step('iframe 真指到了 /api/browse',
    frame.src.indexOf('/api/browse?url=') === 0
    && frame.src.indexOf(encodeURIComponent('https://a.cn/')) >= 0);
frame.fire('load');
step('取回后收掉', load.classList.contains('off'));
const before = frame.src;
bzLoad('https://a.cn/', false);
step('同一地址重进不再掀开（issue #131）', load.classList.contains('off'));
step('同一地址不重发（src 没动）', frame.src === before);
bzLoad('https://b.cn/', true);
step('换一页时再亮起', !load.classList.contains('off'));
frame.fire('load');
step('换页取回后收掉', load.classList.contains('off'));
els['#bzReload'].onclick();
step('点刷新先亮提示', !load.classList.contains('off'));
frame.fire('load');
step('刷新取回后收掉', load.classList.contains('off'));
console.log('GZRESULT ' + JSON.stringify(results));
"""
    harness = (harness.replace("__BUSY__", busy)
               .replace("__BZLOAD__", bz_load)
               .replace("__WIRE__", wire))
    with tempfile.TemporaryDirectory(prefix="guizang-bzload-") as tmp:
        js = pathlib.Path(tmp) / "bzload.js"
        js.write_text(harness, encoding="utf-8")
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
    line = [x for x in r.stdout.splitlines() if x.startswith("GZRESULT ")]
    if r.returncode or not line:
        return [("假 DOM 里跑得起来", False,
                 (r.stderr or r.stdout).strip()[-300:])]
    out = []
    for name, ok in json.loads(line[-1][len("GZRESULT "):]):
        out.append(("真机：%s" % name, bool(ok), ""))
    return out


for name, ok, extra in node_checks():
    chk(name, ok, extra)

bad = [c for c in checks if not c[0]]
print("\n内置浏览器加载提示体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
