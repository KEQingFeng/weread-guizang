#!/usr/bin/env python3
"""归藏真机验证：阅读器续读定位 + 本章大纲 + 换章动效 + 统计数字落位。

第二十六轮加的四件事，都得「点一下才知道」，静态检查看不出来：
  · 重开一本书回到上次停下的那一行（原来只回到那一章的顶上）；
  · 「大纲」浮窗列出这一章的 h1~h3，点一条滚过去，滚到哪一节亮哪一节；
  · 换章是带方向的淡入（WAAPI 直接播），不再走「摘 class → 读 offsetWidth 逼重排」；
  · 统计那屏的数字冷启动从 0 滚到位，滚完要停在真值上（别卡在 0）。

前提：有一个指向沙盒书库的服务，且 seed 铺好了 GAPBOOK1（`python tests/seed.py`）。
服务地址走命令行第一个参数或 GUIZANG_TEST_URL，缺省 8770。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
BOOK = "GAPBOOK1"
CH3 = pathlib.Path(selftest.BOOKS) / BOOK / "chapters" / "0003.md"
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
SHOT = str(selftest.SHOTS / "reader-outline.png")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 给第 3 章铺一版「有小标题、够长能滚」的正文：seed 那版只有一个大标题，
# 大纲和续读都验不出来。只动这一章，别的不碰。
def write_long_chapter():
    parts = ["# 第3章 大纲试验章"]
    for i in range(1, 7):
        parts.append("## %d. 小节标题 %d" % (i, i))
        parts.append("这一段用来说明本小节的主线，配合下面的补白把章节撑到能滚动。" * 3)
        if i % 2 == 0:
            parts.append("### %d.1 再下一层 %d" % (i, i))
            parts.append("三层标题也得进大纲，而且缩进要跟层级走。" * 4)
    parts.append("补白。" * 600)
    CH3.write_text("\n\n".join(parts) + "\n", encoding="utf-8")


# 量正文区滚到哪、大纲里存了哪几节。RD 是脚本顶层的 const，不挂在 window 上，
# 所以这里用裸名字 + typeof 兜底。
OL_JS = """() => {
  const R = (typeof RD !== 'undefined') ? RD : {ol: null, at: -1};
  const p = document.querySelector('#view > .vpane:not([hidden])');
  const sc = p && p.querySelector('#rdScroll');
  const pop = p && p.querySelector('#rdOlPop');
  const list = p && p.querySelector('#rdOl');
  const btn = p && p.querySelector('#rdOlT');
  return {
    at: R.at,
    y: sc ? Math.round(sc.scrollTop) : -1,
    max: sc ? sc.scrollHeight - sc.clientHeight : -1,
    open: !!(pop && pop.classList.contains('show')),
    rows: list ? [...list.querySelectorAll('button')].map(b => ({
      i: +b.dataset.i, cls: b.className, text: b.textContent.trim(),
      y: ((R.ol || [])[+b.dataset.i] || {}).y
    })) : [],
    olBtnHidden: !!(btn && btn.hidden)
  };
}"""

# 点了某一条之后，那一节的标题应贴在正文区顶上（留了 scroll-margin，容一点差）。
HEAD_JS = """(id) => {
  const p = document.querySelector('#view > .vpane:not([hidden])');
  const sc = p && p.querySelector('#rdScroll');
  const h = document.getElementById(id);
  if (!sc || !h) return null;
  return Math.round(h.getBoundingClientRect().top - sc.getBoundingClientRect().top);
}"""

STATS_JS = """() => [...document.querySelectorAll('#view .vpane:not([hidden]) .statbox .v')]
  .map(v => v.textContent.trim())"""

CSS_JS = """() => [...document.styleSheets].flatMap(s => {
  try { return [...s.cssRules].map(r => r.cssText); } catch (e) { return []; }
}).join('')"""


def open_book(pg):
    pg.evaluate("() => openReader('%s', '大纲试验本', 'shelf')" % BOOK)
    pg.wait_for_selector("#rdBody", timeout=9000)
    pg.wait_for_timeout(900)


def main():
    write_long_chapter()
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--no-sandbox"])
        pg = br.new_page(viewport={"width": 1440, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.evaluate("() => localStorage.clear()")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)

        # ── 开书，翻到第 3 章 ──
        pg.evaluate("() => setView('shelf')")
        pg.wait_for_timeout(400)
        open_book(pg)
        pg.evaluate("() => rdGo(2)")
        pg.wait_for_timeout(700)
        st = pg.evaluate(OL_JS)
        chk("翻到第 3 章", st["at"] == 2, st)
        chk("这一章长到能滚", st["max"] > 900, st)

        # ── 大纲按钮：这一章有标题就该露出来 ──
        heads = pg.evaluate("() => document.querySelectorAll('#rdBody h1,#rdBody h2,#rdBody h3').length")
        chk("有 h1~h3，「大纲」钮露出", not st["olBtnHidden"], st)
        chk("大纲条数等于标题数、每条都有文字",
            len(st["rows"]) == min(heads, 60) and all(r["text"] for r in st["rows"]),
            {"heads": heads, "rows": st["rows"][:3]})
        chk("大纲按层级缩进（h2、h3 各靠右一档）",
            any("l2" in r["cls"] for r in st["rows"]) and any("l3" in r["cls"] for r in st["rows"]),
            [r["cls"] for r in st["rows"]])

        # ── 点开浮窗：各节页内偏移量好了、当前节亮着 ──
        pg.evaluate("() => document.querySelector('#rdOlT').click()")
        pg.wait_for_timeout(320)
        st = pg.evaluate(OL_JS)
        chk("大纲浮窗打开", st["open"], st)
        chk("打开时把每节的页内偏移量好了（单调不减）",
            all(b["y"] >= a["y"] for a, b in zip(st["rows"], st["rows"][1:])),
            [r["y"] for r in st["rows"]])
        chk("停在顶上时第一节亮着", st["rows"] and "on" in st["rows"][0]["cls"], st["rows"][:2])

        # ── 点最后一条：滚过去 + 浮窗收起 + 高亮跟着走 ──
        last = st["rows"][-1]
        pg.evaluate("(i) => document.querySelector('#rdOl button[data-i=\"' + i + '\"]').click()",
                    last["i"])
        off = None
        for _ in range(30):
            pg.wait_for_timeout(100)
            off = pg.evaluate(HEAD_JS, "rdh-%d" % last["i"])
            if off is not None and abs(off) < 40:
                break
        st = pg.evaluate(OL_JS)
        chk("点了大纲就滚到那一节（离顶 %s px）" % off, off is not None and abs(off) < 40, st)
        chk("跳完浮窗自动收起", not st["open"], st)
        chk("跳完「在读」亮在最后那节", "on" in st["rows"][-1]["cls"], st["rows"][-1:])
        pg.screenshot(path=SHOT)

        # ── 滚到半途停手：「章 + 页内位置」要落进本机 ──
        pg.evaluate("() => { const s = document.querySelector('#rdScroll'); s.scrollTop = 900;"
                    " s.dispatchEvent(new Event('scroll')); }")
        pg.wait_for_timeout(1300)
        saved = json.loads(pg.evaluate("() => localStorage.getItem('guizang-readpos-%s')" % BOOK)
                           or "{}")
        chk("停手后记下「章 + 页内位置」",
            saved.get("c") == 2 and 800 <= saved.get("y", 0) <= 1000, saved)

        # ── 重开这本书：回到上次那一行 ──
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        open_book(pg)
        pg.evaluate("() => rdGo(2)")   # 开书本来就落在这一章；再点一次确认不会被拽走
        pg.wait_for_timeout(600)
        st = pg.evaluate(OL_JS)
        chk("重开自动回到第 3 章", st["at"] == 2, st)
        chk("重开回到上次那一行（±80px）", abs(st["y"] - saved["y"]) <= 80,
            {"now": st["y"], "want": saved["y"]})

        # 那一笔偏移只用一次：翻到别的章不该被它再拽回半腰
        pg.evaluate("() => rdGo(3)")
        pg.wait_for_timeout(700)
        st = pg.evaluate(OL_JS)
        chk("翻到下一章回到章首", st["y"] < 20, st)
        chk("只有一级标题的章，大纲仍给出一条", len(st["rows"]) >= 1, st)

        # ── 换章动效：WAAPI 在播，且样式表里不再有 .fade/rdfade 那套 ──
        # 第 4 章上一步刚读过，已进缓存 → 翻回去是同步铺的，动画当场就该在播。
        anim = pg.evaluate("""() => {
          const bg = document.querySelector('#rdBody');
          document.querySelector('#rdPrev').click();
          return {n: bg.getAnimations().length, cls: bg.className};
        }""")
        chk("翻章起了动效（WAAPI 动画在播）", anim["n"] >= 1, anim)
        chk("正文不再挂 .fade 那套靠重排重启的 class", "fade" not in anim["cls"], anim)
        chk("样式表里没有 rdfade 残留", "rdfade" not in pg.evaluate(CSS_JS))

        # ── 统计：数字滚到位后停住，不许卡在 0 ──
        # 先单测这段滚动本身：沙盒里没读够 30 秒时真实数据全是 0，
        # 「滚到非零」那条验不出名堂，就直接喂一组已知数字看它落不落对。
        roll = pg.evaluate("""() => {
          const d = document.createElement('div');
          d.className = 'statbox';
          d.innerHTML = '<div class="v">42<small>小时</small>7<small>分</small></div>';
          document.body.appendChild(d);
          rollNums(d, 160);
          const start = d.querySelector('.v').textContent;
          return new Promise(r => setTimeout(() => {
            const end = d.querySelector('.v').textContent;
            d.remove(); r({start, end});
          }, 700));
        }""")
        chk("滚动起步时先归零、收尾落回真值",
            roll["start"].startswith("0") and "42" in roll["end"] and "7" in roll["end"], roll)
        pg.evaluate("() => setView('stats')")
        pg.wait_for_timeout(400)
        mid = pg.evaluate(STATS_JS)      # 还在滚：可能是 0，也可能是半路的数，不拿它当真值
        pg.wait_for_timeout(1600)
        late = pg.evaluate(STATS_JS)
        pg.wait_for_timeout(700)
        settled = pg.evaluate(STATS_JS)
        chk("统计这一屏有格子", bool(late), late)
        chk("滚完不再变（不会卡在 0）", late == settled, {"mid": mid, "late": late,
                                                        "settled": settled})
        # 第二次进来不滚（cold 只认这一份缓存还没建的那一次），拿它当「真值」对照：
        # 滚到位之后必须和直接铺真值一模一样，不然「滚完」是句空话。
        pg.evaluate("() => setView('shelf')")
        pg.wait_for_timeout(300)
        pg.evaluate("() => setView('stats')")
        pg.wait_for_timeout(500)
        warm = pg.evaluate(STATS_JS)
        chk("滚完停在真值上（和不滚那次一致）", late == warm, {"late": late, "warm": warm})

        br.close()

    chk("没有页面报错", not errs, errs[:3])
    bad = [n for ok, n, _ in checks if not ok]
    print("\n通过 %d / %d" % (len(checks) - len(bad), len(checks)))
    if bad:
        print("失败：" + "、".join(bad))
    print("截图：" + SHOT)
    sys.exit(1 if bad else 0)


main()
