#!/usr/bin/env python3
"""归藏真机验证：阅读器续读定位 + 本章大纲 + 换章动效 + 统计数字落位。

第二十六轮加的四件事，都得「点一下才知道」，静态检查看不出来：
  · 重开一本书回到上次停下的那一行（原来只回到那一章的顶上）；
  · 「大纲」浮窗列出这一章的 h1~h3，点一条滚过去，滚到哪一节亮哪一节；
  · 换章是带方向的淡入（WAAPI 直接播），不再走「摘 class → 读 offsetWidth 逼重排」；
  · 统计那屏的数字冷启动从 0 滚到位，滚完要停在真值上（别卡在 0）。

第二十九轮补的是窄窗那一档：≤840 时 @media 把 .rdscroll 的 overflow 关了，滚的是整页，
而进度细线、续读位置、大纲「在读」三样都只读 #rdScroll —— 窄窗里三处一起哑、宽窗却全好，
所以必须真把窗口收窄才验得出来。连同划词小条在 480 宽下被顶出屏幕、图没随书带出来时
正文留了个透明的洞，这几样都是「不缩窄 / 不缺图就看不见」的。

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
# 章节文件名是 0 基的（产品口径：引擎 / book_import / book_notes 都从 0000.md 起头），
# 所以「翻到第 3 章」（rdGo(2)）改的那一份是 0002.md —— 里头人读的序号还是第3章，
# 别被名字骗了：写成 0003.md 会铺到第 4 章上，这一章就短得滚不起来。
CH3 = pathlib.Path(selftest.BOOKS) / BOOK / "chapters" / "0002.md"
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

        # ── 窄窗：正文不再自己滚，滚的是整页 ─────────────────────
        # @media (max-width:840px) 把 .rdscroll 的 overflow 关掉了（手机上顺着整页读到底
        # 更舒服），代价是 #rdScroll.scrollTop 从此钉死在 0。第二十九轮之前，进度细线、
        # 续读位置、大纲「在读」三样全读它 —— 窄窗里进度条永远空着、重开回到章首、
        # 大纲永远亮第一节，三处一起哑。现在改成先问「到底谁在滚」（rdScroller），
        # 判据读当前生效的 CSS，不抄 840 这个数。这条必须真把窗口收窄才验得出来。
        pg.set_viewport_size({"width": 760, "height": 820})
        pg.wait_for_timeout(500)
        pg.evaluate("() => setView('shelf')")
        pg.wait_for_timeout(400)
        open_book(pg)
        pg.evaluate("() => rdGo(2)")
        pg.wait_for_timeout(700)
        mode = pg.evaluate("""() => {
          const sc = document.querySelector('#rdScroll');
          return {ov: sc ? getComputedStyle(sc).overflowY : '?',
                  reading: document.body.classList.contains('reading'),
                  box: sc ? Math.round(sc.scrollTop) : -1};
        }""")
        chk("窄窗确实是整页滚（.rdscroll 不自己滚）",
            mode["ov"] == "visible" and mode["reading"], mode)

        pg.evaluate("() => window.scrollTo(0, 900)")
        pg.wait_for_timeout(1400)                 # 停手 800ms 才落盘，等它写完
        thin = pg.evaluate("""() => {
          let pos = null;
          try { pos = JSON.parse(localStorage.getItem('guizang-readpos-%s')); } catch (e) {}
          const a = document.querySelector('#rdProg'), m = document.querySelector('.rdmain');
          return {w: a ? a.style.width : '', box: document.querySelector('#rdScroll').scrollTop,
                  y: Math.round(window.scrollY), pos: pos,
                  lifted: !!(m && m.classList.contains('scrolled'))};
        }""" % BOOK)
        pg.evaluate("() => window.scrollTo(0, 2600)")
        pg.wait_for_timeout(400)
        deeper = pg.evaluate("() => document.querySelector('#rdProg').style.width")
        # 「不是 0」不够 —— 量错了盒子时那条会算成 100%（容器自己不滚，可滚距离是 0，
        # 代码把它当「读完了」），照样不是 0。所以要它落在中段，并且越滚越大。
        pct = float((thin["w"] or "0").rstrip("%"))
        chk("窄窗滚一页，进度细线走到中段（不是 0 也不是「读完了」）",
            2 < pct < 95, thin)
        chk("窄窗：越往下滚细线越长", float((deeper or "0").rstrip("%")) > pct + 3,
            {"pct": pct, "deeper": deeper})
        chk("窄窗里 #rdScroll 自己不滚（量的不是它）", thin["box"] == 0 and thin["y"] > 400, thin)
        chk("窄窗也把页底那道浮影收放了", thin["lifted"], thin)
        chk("窄窗也记下「章 + 页内位置」（不是只记章）",
            bool(thin["pos"]) and thin["pos"]["c"] == 2 and thin["pos"]["y"] > 400, thin)
        # 重开要真的重开（整页刷一遍）：手动 scroll 回顶再重开是假动作 ——
        # 回顶那一下自己就会把位置存成 0，「回到那一行」当场被自己抹掉。
        # 而且页面不刷的话，整页一直停在原地，那条也会白过。
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1200)
        open_book(pg)
        pg.wait_for_timeout(900)
        back = pg.evaluate("""() => ({
          at: (typeof RD !== 'undefined') ? RD.at : -1,
          y: Math.round(window.scrollY),
          w: (document.querySelector('#rdProg') || {style: {}}).style.width})""")
        chk("窄窗重开回到第 3 章那一行", back["at"] == 2 and back["y"] > 400, back)
        chk("窄窗重开进度条跟着回到中段",
            back["w"] and 2 < float(back["w"].rstrip("%")) < 95, back)

        # ── 更窄一档：划词小条的笔那一排不许被顶出屏幕 ───────────
        # 笔色是用户自己加的（NT.tags），默认五支时那排刚好塞得进 480，加到九支就顶出去了；
        # 顶出去的是右半边 —— 正好是「记一笔」「写条目」，最该留的两个。
        # 摆位只管把小条夹回左边，宽度它不管，所以这里要：宽度封顶 + 两排都能折行。
        pg.set_viewport_size({"width": 480, "height": 640})
        pg.wait_for_timeout(500)
        pg.evaluate("""() => {
          const cs = ['#e8b04b', '#d76a6a', '#6aa9d7', '#7bc08a', '#b07ad7',
                      '#d78a5a', '#5ab8b8', '#c4c45a', '#8a8ad7'];
          NT.tags = cs.map((c, i) => ({key: 't' + i, name: '标签' + (i + 1), color: c}));
        }""")
        spot = pg.evaluate("""() => {
          const vh = innerHeight;
          const p = [...document.querySelectorAll('#rdBody p')].find(x => {
            if ((x.textContent || '').trim().length < 30) return false;
            const r = x.getBoundingClientRect();
            return r.width > 120 && r.top > -vh && r.bottom < vh * 2;
          });
          if (!p) return null;
          // 先把要选的那一段滚到视口中间再量：鼠标坐标是视口系的，
          // 这一段在上一步刚续读落到哪儿都行，不滚过来就可能整段在屏幕外。
          p.scrollIntoView({block: 'center'});
          return 1;
        }""")
        pg.wait_for_timeout(500)
        bar = None
        seg = None
        if spot:
            seg = pg.evaluate("""() => {
              const p = [...document.querySelectorAll('#rdBody p')].find(x => {
                const r = x.getBoundingClientRect();
                return (x.textContent || '').trim().length > 30
                  && r.top > 70 && r.bottom < innerHeight - 70 && r.width > 120;
              });
              if (!p) return null;
              const r = p.getBoundingClientRect();
              return [Math.round(r.left + 8), Math.round(r.top + r.height / 2)];
            }""")
        if seg:
            pg.mouse.move(seg[0], seg[1])
            pg.mouse.down()
            pg.mouse.move(min(seg[0] + 200, 470), seg[1], steps=8)
            pg.mouse.up()
            pg.wait_for_timeout(500)
            bar = pg.evaluate("""() => {
              const el = document.getElementById('selpop');
              if (!el || !el.classList.contains('show')) return null;
              const bs = [...el.querySelectorAll('button')], vw = innerWidth, vh = innerHeight;
              const box = el.getBoundingClientRect();
              return {n: bs.length, w: Math.round(box.width), vw,
                      out: bs.filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.right > vw + 1 || r.left < -1 || r.bottom > vh + 1 || r.top < -1;
                      }).map(b => (b.textContent.trim() || b.title || '色片').slice(0, 8)),
                      keep: bs.filter(b => /写想法|写条目/.test(b.textContent)).length};
            }""")
        chk("窄窗：划词真能拉出小条（上面一排工具、下面一排笔）", bool(bar) and bar["n"] >= 8,
            {"bar": bar, "spot": spot, "seg": seg})
        chk("窄窗：小条整个在视口里", bool(bar) and bar["w"] <= bar["vw"], bar)
        chk("窄窗：笔那一排一个钮都没被顶出去", bool(bar) and not bar["out"], bar)
        chk("窄窗：折行后「写想法」「写条目」还在", bool(bar) and bar["keep"] == 2, bar)

        # 图没随书带出来时得说句话，别留一个透明的洞（.mfade 的 opacity:0 会把
        # 浏览器的碎图标一起藏掉，看着像这一页少了一段）。
        imgs = pg.evaluate("""async () => {
          const bg = document.querySelector('#rdBody');
          const html = bg.innerHTML;
          bg.innerHTML = '<p><img src="images/没有这张.png"></p>' + html;
          rdFixMedia(bg);
          await new Promise(r => setTimeout(r, 700));
          const ph = bg.querySelector('.imgmiss');
          const im = bg.querySelector('img');
          const out = {ph: ph ? ph.textContent : '', gone: !!(im && im.classList.contains('mgone')),
                       shown: ph ? getComputedStyle(ph).display !== 'none' : false};
          bg.innerHTML = html;
          return out;
        }""")
        chk("图没带出来：换成一句明说的话，不留空洞",
            bool(imgs["ph"]) and imgs["gone"] and imgs["shown"] and "图没带出来" in imgs["ph"],
            imgs)

        br.close()

    chk("没有页面报错", not errs, errs[:3])
    bad = [n for ok, n, _ in checks if not ok]
    print("\n通过 %d / %d" % (len(checks) - len(bad), len(checks)))
    if bad:
        print("失败：" + "、".join(bad))
    print("截图：" + SHOT)
    sys.exit(1 if bad else 0)


main()
