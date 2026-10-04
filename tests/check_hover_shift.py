"""悬停会不会把版面顶动。

用户报的是「视频与章节模块：鼠标 hover 时布局发生偏移」——一行上的动作钮悬停才
显形，显形时若占的是正常流里的一格，它下面所有行都得跟着让位，鼠标顺着列表扫
一遍整栏都在跳。这一套就盯这一件事，两道：

一、从 CSSOM 里把所有含 :hover 的规则捞出来，凡是改「在流内」的布局属性
    （宽高 / 内外边距 / display / grid / flex / 字号 / 行高 / 间距 / 边框宽）
    又落在正常流元素上的，一律判失败。伪元素（::before / ::after）不算 ——
    它们是浮在面上的装饰，动了自己也不挪别人。改之前这条会逮住
    `.ntrow:hover .nacts{max-height:30px}`。
二、真机 hover 视频转写行与阅读器章节目录行，比对整屏元素坐标；位移超过
    容差的算失败。容差是给背景那层几乎看不见的呼吸动画留的（它自己就会飘 1px），
    真·布局偏移是一整颗按钮的高度（20px 以上）。

用法: .venv/bin/python tests/check_hover_shift.py [地址]
"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8770/"
TOL = 3          # 位移容差（px）：小于它的只当是背景动效在喘气

# 只挑「改了就一定在流内动别人」的属性。opacity / color / background / transform /
# box-shadow / filter 故意不在列 —— 它们不动别人的位置。
LAYOUT = ['width', 'height', 'max-height', 'min-height', 'max-width', 'min-width',
          'padding', 'padding-top', 'padding-left', 'padding-right', 'padding-bottom',
          'margin', 'margin-top', 'margin-left', 'margin-right', 'margin-bottom',
          'display', 'flex-basis', 'flex-grow', 'flex-shrink', 'grid-template-columns',
          'grid-template-rows', 'font-size', 'line-height', 'gap', 'row-gap', 'column-gap',
          'border-width', 'border-top-width', 'border-left-width', 'inset', 'top', 'left',
          'right', 'bottom', 'position', 'letter-spacing', 'white-space', 'overflow']

AUDIT_JS = "() => { const L = %s;" % str(LAYOUT).replace("'", '"') + r"""
  const hits = [];
  const walk = (list) => {
    for (const r of list) {
      // 样式规则自身也带 .cssRules（嵌套），必须靠 selectorText 先认出它，
      // 否则空规则组会被当成「还要往下走」，把这条规则本身漏掉。
      if (r.selectorText) {
        if (r.selectorText.indexOf(':hover') < 0) continue;
      } else if (r.cssRules && r.cssRules.length) { walk(r.cssRules); continue; }
      else continue;
      if (!r.style) continue;
      // 伪元素是浮面上的装饰，动自己不挪别人。
      if (r.selectorText.indexOf('::before') >= 0 || r.selectorText.indexOf('::after') >= 0) continue;
      const hit = [];
      for (const p of L) { const v = r.style.getPropertyValue(p); if (v) hit.push(p + ':' + v); }
      if (hit.length) hits.push(r.selectorText + ' {' + hit.join('; ') + '}');
    }
  };
  for (const ss of document.styleSheets) {
    let rules; try { rules = ss.cssRules; } catch (e) { continue; }
    if (rules) walk(rules);
  }
  return hits;
}"""

SIG_JS = r"""() => {
  const o = [];
  document.querySelectorAll('body *').forEach(e => {
    if (!e.offsetParent) return;
    const b = e.getBoundingClientRect();
    if (b.width < 2 || b.height < 2) return;
    o.push({k: (e.className || e.tagName).toString().slice(0, 48),
            x: Math.round(b.x), y: Math.round(b.y)});
  });
  return o;
}"""


def diff(before, after):
    bm, am = {}, {}
    for it in before:
        bm.setdefault(it["k"], []).append((it["x"], it["y"]))
    for it in after:
        am.setdefault(it["k"], []).append((it["x"], it["y"]))
    out = []
    for k, ys in am.items():
        if not bm.get(k):
            continue
        for a, b in zip(bm[k], ys):
            if abs(a[0] - b[0]) > TOL or abs(a[1] - b[1]) > TOL:
                out.append(f"{k}: {a} -> {b}")
    return out


async def hover_delta(pg, sel):
    """hover 这个选择器命中的第一个元素，回报整屏里位移超过容差的东西。"""
    box = await pg.evaluate(
        "(s)=>{const e=document.querySelector(s);if(!e)return null;"
        "const b=e.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2}}", sel)
    if not box:
        return None
    await pg.mouse.move(6, 260)
    await pg.wait_for_timeout(220)
    before = await pg.evaluate(SIG_JS)
    await pg.mouse.move(box["x"], box["y"])
    await pg.wait_for_timeout(360)
    after = await pg.evaluate(SIG_JS)
    return diff(before, after)


async def main():
    fails = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        pg = await b.new_page(viewport={"width": 1440, "height": 940})
        await pg.goto(URL, wait_until="networkidle")
        await pg.wait_for_timeout(1300)

        print("── 一、CSSOM：有没有 hover 规则改在流内的布局 ──")
        hits = await pg.evaluate(AUDIT_JS)
        if hits:
            for h in hits:
                print("  ✗", h)
                fails.append("hover 改了在流内的布局：" + h)
        else:
            print("  ✓ 没有一条 hover 规则改动在流内的布局")

        print("── 二、真机：hover 视频行 / 章节行，有没有把谁顶动 ──")
        # 视频转写行：先进「视频转笔记」，点开左栏那一本，右栏才有转写行
        await pg.click('#nav button[data-v="video"]')
        await pg.wait_for_timeout(1400)
        try:
            await pg.click("#vShelf .vtbook", timeout=2500)
            await pg.wait_for_timeout(1400)
        except Exception as e:
            print("  ! 没点开视频书（沙盒里可能没铺）：", str(e)[:60])
        for sel in ("#vShelf .vtbook", "#vRows .vtrow"):
            d = await hover_delta(pg, sel)
            if d is None:
                print(f"  - {sel}：这一屏没有，跳过")
            elif d:
                for x in d[:6]:
                    print(f"  ✗ hover {sel} 顶动了：{x}")
                fails.append(f"hover {sel} 顶动了 {len(d)} 个元素")
            else:
                print(f"  ✓ hover {sel} 谁也没动")

        # 章节目录行：从书架点开一本有正文的书，进阅读器左栏
        await pg.click('#nav button[data-v="shelf"]')
        await pg.wait_for_timeout(900)
        cards = await pg.evaluate(
            "()=>[...document.querySelectorAll('.wcard')].map(e=>{const b=e.getBoundingClientRect();"
            "return {x:b.x+b.width/2,y:b.y+b.height/2}})")
        opened = False
        for c in cards:
            await pg.mouse.move(c["x"], c["y"])
            await pg.wait_for_timeout(120)
            await pg.mouse.click(c["x"], c["y"])
            await pg.wait_for_timeout(260)
            labels = await pg.evaluate(
                "()=>[...document.querySelectorAll('.wcard.open .strip button')].map(x=>x.textContent)")
            if "正文" in labels:
                await pg.evaluate(
                    "()=>{const x=[...document.querySelectorAll('.wcard.open .strip button')]"
                    ".find(y=>y.textContent==='正文');x.click();}")
                opened = True
                break
            await pg.mouse.click(c["x"], c["y"])
        if opened:
            await pg.wait_for_timeout(1700)
            d = await hover_delta(pg, ".rdch")
            if d is None:
                print("  - .rdch：这本没有章节，跳过")
            elif d:
                for x in d[:6]:
                    print(f"  ✗ hover .rdch 顶动了：{x}")
                fails.append(f"hover .rdch 顶动了 {len(d)} 个元素")
            else:
                print("  ✓ hover .rdch 谁也没动")
        else:
            print("  ! 没找到能打开正文的书，章节行这一段跳过")

        await b.close()

    print()
    if fails:
        print(f"悬停位移检查未通过：{len(fails)} 条")
        for f in fails:
            print("  ✗", f)
        sys.exit(1)
    print("悬停位移检查通过")


asyncio.run(main())
