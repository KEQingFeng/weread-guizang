"""视口回归：瀑布、叠卡、详情页、阅读器在几档视口下都不许溢出、不许冒 emoji，
叠卡的扇形在窄窗也得整张看得见，书架样式要跨刷新活下来。只读，不动数据。

跑之前先 `python tests/seed.py` 铺好书，或者由 run_all.sh 代劳。
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

URL = selftest.need_base(1) + "/"
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
FAIL = []
# 桌面窗最小 480×620（shell/main.swift），所以最窄取 480，不给它测做不到的事。
VIEWPORTS = [(1440, 900), (1024, 760), (760, 700), (480, 640)]
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def flush():
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


def set_mode(m):
    page.evaluate("""() => { if (typeof setShelfMode === 'function') setShelfMode(%r); }""" % m)
    page.wait_for_timeout(700)
    flush()


SWEEP = """() => {
  const win = document.documentElement.clientWidth + 1;
  // 藏在收起的抽屉里、或被某个 overflow 祖先剪掉的，都不算「戳出视口」——
  // 用户根本看不到它，滚动条也不会因为它出现（整页 scrollWidth 那条已经在管这个）。
  const shown = (e) => {
    for (let p = e; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (p.hidden || cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < .05)
        return false;
      if (p !== e && /(hidden|clip|auto|scroll)/.test(cs.overflowX)) {
        const r = p.getBoundingClientRect();
        if (r.right <= win && r.left >= 0) return false;
      }
    }
    return true;
  };
  const over = [];
  // .sstage 故意不收：扇形要越过舞台铺进两边的空地才好看，管它的是「别越窗口」。
  for (const el of document.querySelectorAll('.sgrid, .dacts, .rdfoot, .rdbar, .ntt'))
    if (el.scrollWidth > el.clientWidth + 1)
      over.push(el.className + ' ' + el.scrollWidth + '>' + el.clientWidth);
  const off = [...document.querySelectorAll('*')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && (r.right > win || r.left < -1) &&
           getComputedStyle(e).position !== 'fixed' && shown(e);
  }).map(e => ((e.className || '') + ' ' + e.tagName).toString().trim().slice(0, 30));
  // 扇形：只要还看得见（opacity>0），就必须整张在窗口里 —— 切一半等于骗人去点。
  const cut = [...document.querySelectorAll('.sstage > .wcard')]
    .filter(c => +getComputedStyle(c).opacity > .05)
    .map(c => {
      const r = c.getBoundingClientRect();
      return {k: [...c.classList].find(x => /^s\\d$/.test(x)) || c.className,
              l: Math.round(r.left), rr: Math.round(r.right), win: win - 1};
    }).filter(c => c.rr > win || c.l < 0);
  return {doc: document.documentElement.scrollWidth, win, over: over.slice(0, 6),
          off: [...new Set(off)].slice(0, 6), cut};
}"""


def sweep(tag):
    t = page.evaluate(SWEEP)
    chk(f"{tag}：整页没有横向溢出", t["doc"] <= t["win"] + 1, t)
    chk(f"{tag}：关键容器不溢出", not t["over"], t["over"])
    chk(f"{tag}：没有元素戳出视口", not t["off"], t["off"])
    chk(f"{tag}：看得见的一张都没被窗口切到", not t["cut"], t["cut"])
    txt = page.evaluate("() => document.body.innerText")
    hits = sorted(set(EMOJI.findall(txt)))
    chk(f"{tag}：屏幕上一个 emoji 也没有", not hits, hits)


def shelf_mode():
    return page.evaluate("""() => {
      const b = [...document.querySelectorAll('#sMode button')];
      return (b.find(x => x.classList.contains('on')) || {}).dataset?.m || '';
    }""")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    for (w, h) in VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(900)
        set_mode("grid")                      # 样式存本机，上一轮留的是叠卡，得摆明测哪面
        tag = f"{w}×{h} 瀑布"
        chk(f"{tag}：网格铺出来了", page.locator(".sgrid > .wcard").count() >= 1)
        sweep(tag)

        set_mode("stack")
        chk(f"{w}×{h} 叠卡：舞台里有卡", page.locator(".sstage > .wcard").count() >= 1)
        chk(f"{w}×{h} 叠卡：正面那张看得见", page.evaluate("""() => {
          const c = document.querySelector('.sstage > .wcard.s0');
          if (!c) return false;
          const r = c.getBoundingClientRect();
          return +getComputedStyle(c).opacity > .9 && r.width > 60 &&
                 r.left >= -1 && r.right <= document.documentElement.clientWidth + 1;
        }"""))
        chk(f"{w}×{h} 叠卡：能点的都看得见", page.evaluate("""() => {
          const win = document.documentElement.clientWidth;
          return [...document.querySelectorAll('.sstage > .wcard')]
            .filter(c => getComputedStyle(c).pointerEvents === 'auto')
            .every(c => {
              const r = c.getBoundingClientRect();
              return +getComputedStyle(c).opacity > .05 && r.right <= win + 1 && r.left >= -1;
            });
        }"""))
        sweep(f"{w}×{h} 叠卡")

        got = page.evaluate("""() => {
          const c = document.querySelector('.sstage > .wcard.s0, .sgrid > .wcard');
          const b = c && [...c.querySelectorAll('.strip button, .wact button')]
              .find(x => x.textContent.trim() === '查看详情');
          if (b) b.click();
          return !!b;
        }""")
        page.wait_for_timeout(700)
        flush()
        chk(f"{w}×{h} 详情：进得去详情页", got)
        sweep(f"{w}×{h} 详情")
        page.screenshot(path=str(selftest.SHOTS / ("viewport-%d.png" % w)))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

    # ── 跨刷新：书架样式存在 localStorage，重载后得站得住 ──────────
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.click("#sMode button[data-m='stack']")
    page.wait_for_timeout(700)
    flush()
    chk("叠卡：写进了本机", page.evaluate(
        "() => localStorage.getItem('guizang_shelf_mode_v1')") == "stack")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1100)
    flush()
    chk("重载后还是叠卡", shelf_mode() == "stack", shelf_mode())
    chk("重载后叠卡舞台直接可用", page.locator(".sstage > .wcard.s0").count() == 1)
    page.click("#sMode button[data-m='grid']")
    page.wait_for_timeout(700)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(1100)
    flush()
    chk("重载后还是瀑布", shelf_mode() == "grid", shelf_mode())
    chk("重载后瀑布网格回来了", page.locator(".sgrid > .wcard").count() >= 1)
    chk("全程没有报错", not errors, errors[:6])

    browser.close()

print()
print(f"回归：{'全部通过' if not FAIL else str(len(FAIL)) + ' 项失败 -> ' + ' | '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
