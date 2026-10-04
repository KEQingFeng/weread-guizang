#!/usr/bin/env python3
"""归藏真机验证：文字不许戳出它所在的框（1.0.6 · #207）。

用户报的原话是「文字需控制在边框内，不得超出，避免排版拥挤」。这一套盯的就是这一类：
**一个自带文字的元素，右边界越过它父容器的内容框** —— 也就是那句文字从自己的框里探出去，
压到旁边的列、或把窄栏顶出横向滚动条。

最先栽的是写作屏左栏那句「还没有稿子。在右边直接开始写，第一句落下就会建一篇。」：
`.wrline .lab` 写了 `flex:none`，既不长也不缩，整句就顶着 max-content 的宽度戳出 214px 的
稿子列 82px。同一类写法（`flex:none` / 不给 `min-width:0` 的定宽标签）全站都可能再犯，
所以这里不做单点断言，而是把十二格入口 + 个人主界面 + 设置弹窗都扫一遍。

判定口径（都在页面里算，量的是真实排版结果）：
  · 只看**自己带文字节点**的元素 —— 空壳容器不算，它有没有溢出由里面的文字决定；
  · 元素的 `position` 是 absolute / fixed 的不算（浮在行尾的动作钮、扇形卡片是有意叠出去的）；
  · 只比父容器的**内容框**右边界（扣掉 padding-right 与 border-right）；
  · 父容器 `overflow-x` 不是 visible 的不算（那本来就是个裁剪 / 滚动容器，文字没「探出去」）；
  · `.sstage` 的子项不算（叠卡扇形要越过舞台铺进两边空地，那条归视口回归管）。
容差 2px（亚像素取整）。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出，不拿 8770 赌。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
SHOTS_DIR = selftest.SHOTS
SHOTS_DIR.mkdir(parents=True, exist_ok=True)
SHOT = str(SHOTS_DIR / "overflow.png")

# 十二格入口一律走 setView（它就是侧边栏点击走的那条路）；「我的」不在名册里，点头像进。
NAVS = ["shelf", "local", "clip", "subs", "video", "notes", "write",
        "marks", "wander", "stats", "pick", "find"]
SIZES = [(1280, 900), (1024, 760), (760, 700)]

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:400]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:400]))


# 页面里这一份是唯一真相：量的是浏览器真排出来的几何，不是源码里猜的。
SWEEP = r"""() => {
  const leaks = [];
  const vis = e => {
    for (let p = e; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (p.hidden || cs.display === 'none' || cs.visibility === 'hidden') return false;
      const r = p.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return false;
    }
    return true;
  };
  const ownText = e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length);
  document.querySelectorAll('body *').forEach(el => {
    if (!vis(el) || !ownText(el)) return;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.position === 'absolute') return;
    const p = el.parentElement;
    if (!p) return;
    const pcls = (p.className || '').toString();
    if (/sstage/.test(pcls)) return;
    const pcs = getComputedStyle(p);
    if (!/visible/.test(pcs.overflowX)) return;
    const pr = p.getBoundingClientRect();
    const contentRight = pr.right - parseFloat(pcs.paddingRight) - parseFloat(pcs.borderRightWidth);
    const r = el.getBoundingClientRect();
    if (r.right > contentRight + 2) {
      leaks.push({el: (el.className || el.tagName).toString().slice(0, 26),
        parent: pcls.slice(0, 26), over: Math.round(r.right - contentRight),
        text: (el.textContent || '').trim().slice(0, 30)});
    }
  });
  return leaks;
}"""


def sweep(page, tag):
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(60)
    leaks = page.evaluate(SWEEP)
    chk("%s：文字都待在自己的框里" % tag, not leaks,
        json.dumps(leaks[:6], ensure_ascii=False))


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for (w, h) in SIZES:
        page = browser.new_page(viewport={"width": w, "height": h})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(1100)

        for nav in NAVS:
            page.evaluate("(v) => setView(v)", nav)
            page.wait_for_timeout(650)
            sweep(page, "%d×%d %s" % (w, h, nav))

        # 个人主界面（不在名册里，点头像进）：两张热力图 + 资料表单那一屏
        page.evaluate("() => { const m = document.querySelector('#mebtn'); if (m) m.click(); }")
        page.wait_for_timeout(700)
        sweep(page, "%d×%d me" % (w, h))

        # 设置弹窗：这一类「标签 + 控件 + 说明」最密的地方，最容易挤出去
        page.evaluate("() => setPop(true, 'nav')")
        page.wait_for_timeout(400)
        sweep(page, "%d×%d 设置·侧边栏" % (w, h))
        page.evaluate("() => setPop(true, 'agent')")
        page.wait_for_timeout(300)
        sweep(page, "%d×%d 设置·小助手" % (w, h))
        page.evaluate("() => setPop(false)")
        page.wait_for_timeout(200)

        chk("%d×%d：全程没有报错" % (w, h), not errors, errors[:5])
        if (w, h) == SIZES[0]:
            page.screenshot(path=SHOT)
        page.close()
    browser.close()

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
