#!/usr/bin/env python3
"""归藏真机验证：界面要「顺」也要「稳」（1.0.6 · #210）。

「提升整体流畅度与稳定性」这句话要能落成会失败的检查，得先定住「什么样算顺、算稳」。
2026-10-05 在真机上量过一遍，当下的基线是：静置时 2.6 秒那一跳（drawState 轮询）约
十几毫秒；暖态切屏一次 8~15ms；整轮十二格走下来没有一帧主线程被占超过 50ms。既然这是
现状，就把它钉成不许退的门槛 —— 以后谁往渲染里塞了重活，这套先把人拦下。

这一套钉五样：
  ① 页签图标：`<link rel="icon">` 得在，且是内联 data URI。缺了，浏览器会自己去要
     /favicon.ico，后端没那条路由 → 回 404 → 控制台常年一条红字（这正是本轮修掉的）。
  ② 全程零报错：十二格 + 个人主界面 + 设置弹窗走一遍，console.error / pageerror /
     未处理的 promise 拒绝，一个都不许有。这类东西平时不出声，一旦出现就是「按下去
     没反应」那类事故的前兆。
  ③ 暖态切屏不许有长任务：先把十二格各进一次（冷启动会把常驻 pane 建出来，那一下
     本来就有活），再走第二遍 —— 第二遍才是「天天用的那一下」。这一遍里主线程任何
     一段活都不许超过 50ms（长任务 = 掉帧 = 用户嘴里的「卡一下」）。
  ④ 连点不许卡住：30 下飞快地切，落定后必须恰好一屏可见、且没有任何一屏滞留在
     半透明（那是交接动画被打断、停在中间的样子，用户看到的「闪一下没出来」）。
  ⑤ 观测器自检：上面 ③ 的「没有长任务」只有在观测器真活着时才算数。这一条先注入
     一段已知的长活，抓不到就当场失败 —— 否则观测器悄然失灵时，③ 会永远绿（2026-10-05
     就栽在这儿：init 脚本写成了裸箭头函数，函数体根本没跑）。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出，不拿 8770 赌。
"""
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

BASE = selftest.need_base(1)
SHOTS_DIR = selftest.SHOTS
SHOTS_DIR.mkdir(parents=True, exist_ok=True)
SHOT = str(SHOTS_DIR / "fluency.png")

# 长任务门槛：主线程一段活超过它就记一笔。50ms 是通用那条线（掉一帧约 16.7ms，
# 50ms 意味着一口气吃掉三帧）。
LONG_MS = 50

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def nav_targets():
    """侧边栏那几格从后端名册取 —— 页面加一格，这套就跟着覆盖一格；藏起来的不点。"""
    try:
        st = json.loads(urllib.request.urlopen(BASE + "/api/state", timeout=4).read())
        items = ((st.get("nav") or {}).get("items")) or []
        return [(i.get("label") or i.get("id"), i["id"]) for i in items if not i.get("hidden")]
    except Exception:
        # 取不到名册就退回当下这一版的口径，至少别让这套整体失败。
        return [(n, n) for n in ["shelf", "local", "clip", "feed", "video", "flomo",
                                 "write", "notes", "random", "stats", "pick", "search"]]


# 装机观测器：长任务 + 未处理拒绝，各收到一个数组里，之后读差分。
# 这里必须是「语句」，不能写成裸箭头函数 —— add_init_script 只是把这段当脚本跑一遍，
# 传一个 `() => {...}` 进去，函数体根本不会被调用（2026-10-05 就是栽在这儿：观测器
# 没装上，`window.__long` 一直是 undefined，「暖态没有长任务」那条断言于是永远绿）。
INSTALL_JS = r"""
  window.__long = [];
  window.__rej = [];
  try { new PerformanceObserver(l => { for (const e of l.getEntries())
      window.__long.push({dur: Math.round(e.duration), name: e.name}); })
      .observe({entryTypes: ['longtask']}); } catch (e) {}
  addEventListener('unhandledrejection', e => window.__rej.push(String(e.reason).slice(0, 160)));
"""

# 落定后清点各 pane 的样子：可见的有几个、有没有卡在半透明的。
PANES_JS = r"""() => {
  const all = [...document.querySelectorAll('.vpane')];
  const vis = all.filter(p => !p.hidden).map(p => p.dataset.pane);
  const faded = all.filter(p => { const o = +getComputedStyle(p).opacity; return o > 0.02 && o < 0.98; })
                   .map(p => p.dataset.pane + ':' + getComputedStyle(p).opacity);
  const on = document.querySelector('#nav button.on') || document.querySelector('#nav button[aria-current]');
  return {vis, faded, active: on ? (on.dataset.v || '') : ''};
}"""

from playwright.sync_api import sync_playwright  # noqa: E402

TARGETS = nav_targets() + [("个人主界面", "__me__")]


def selector_for(name):
    return "#mebtn" if name == "__me__" else '#nav button[data-v="%s"]' % name


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append("console." + m.type + ": " + m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    page.add_init_script(INSTALL_JS)

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    # ① 页签图标：在，且是内联的 SVG data URI。
    icon = page.evaluate(
        "() => { const l = document.querySelector('link[rel=\"icon\"]'); return l ? l.href : ''; }")
    chk("页签图标在，且是内联 SVG（浏览器不会去要 /favicon.ico 撞 404）",
        isinstance(icon, str) and icon.startswith("data:image/svg+xml"),
        (icon or "")[:80] or "没有 <link rel=icon>")

    # ⑤ 先自证观测器是活的：往页面里排一段「已知的长活」，抓不到就说明上面那条是空枪 ——
    # 与其让 ③ 在观测器失灵时永远绿，不如在这里当场失败。（排在页面里的 setTimeout 才
    # 是正常任务队列上的一段活；这也正是 ③ 要盯的那类东西。）
    page.evaluate("window.__long = []")
    page.evaluate("() => setTimeout(() => { const t = performance.now();"
                  " while (performance.now() - t < 140) {} }, 0)")
    page.wait_for_timeout(500)
    probe_durs = [t["dur"] for t in (page.evaluate("window.__long") or [])]
    chk("长任务观测器自检：注入一段 140ms 忙活确实被抓到（否则 ③ 那条不算数）",
        any(d >= 100 for d in probe_durs), "抓到的：%s" % probe_durs)

    # 冷走一遍：把十二格 + 个人主界面的常驻 pane 都建出来（这一遍不进长任务统计）。
    for label, name in TARGETS:
        try:
            page.click(selector_for(name), timeout=4000)
        except Exception as ex:
            errors.append("冷走点不到 %s: %s" % (label, ex))
        page.wait_for_timeout(180)

    # ③ 暖态第二遍：这一遍才是日常那一下，主线程不许有长任务。
    page.evaluate("window.__long = []")
    switched = 0
    for label, name in TARGETS:
        page.click(selector_for(name), timeout=4000)
        page.wait_for_timeout(160)
        switched += 1
    page.wait_for_timeout(400)
    longtasks = page.evaluate("window.__long") or []
    chk("暖态逐屏切换：没有任何一段主线程活超过 %dms（不长卡）" % LONG_MS,
        len(longtasks) == 0,
        "最长几段：%s" % [t["dur"] for t in sorted(longtasks, key=lambda x: -x["dur"])[:4]])

    page.screenshot(path=SHOT)

    # ④ 连点 30 下：落定后必须恰好一屏可见、没有半透明卡住的。
    for i in range(30):
        page.click(selector_for(TARGETS[i % len(TARGETS)][1]), timeout=4000)
    page.wait_for_timeout(1400)
    p = page.evaluate(PANES_JS)
    chk("连点 30 下落定后：恰好一屏可见（没有两屏叠着 / 一屏没出来）",
        len(p["vis"]) == 1, "可见的 pane：%s" % p["vis"])
    chk("连点 30 下落定后：没有任何一屏滞留在半透明（交接没被打断在半路）",
        not p["faded"], "半透明的 pane：%s" % p["faded"])

    # ② 全程零报错（连点之后再看，把压测也覆盖进来）。
    rej = page.evaluate("window.__rej") or []
    chk("全程零 console 报错 / 页面异常", not errors, errors[:5])
    chk("全程零未处理的 promise 拒绝", not rej, rej[:5])

    browser.close()

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
print("截图：" + SHOT)
sys.exit(1 if bad else 0)
