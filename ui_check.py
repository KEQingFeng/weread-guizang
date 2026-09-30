"""前端布局校验：真实视口下量几何 + 抓 console 报错。
用法: .venv/bin/python ui_check.py
"""
import asyncio
import json

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8770/"
VIEWPORTS = [("桌面", 1280, 900, "light"), ("暗色", 1280, 900, "dark"),
             ("窄屏", 430, 820, "light"), ("中屏", 900, 1000, "light")]


async def probe(pg):
    return await pg.evaluate(r"""() => {
        const q = s => document.querySelector(s);
        const box = s => { const e = q(s); if (!e) return null;
            const b = e.getBoundingClientRect();
            return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)}; };
        const de = document.documentElement;
        const over = [];
        document.querySelectorAll('body *').forEach(e => {
            const b = e.getBoundingClientRect();
            if (b.width > 0 && (b.right > de.clientWidth + 1 || b.left < -1))
                over.push({cls: (e.className || '').toString().slice(0, 28), l: Math.round(b.left), r: Math.round(b.right)});
        });
        const cs = s => { const e = q(s); return e ? getComputedStyle(e) : null; };
        const cso = s => cs(s) || {};                       // 选择器落空也不炸，只是量不到
        const val = (s, k) => (cso(s)[k] || '');
        const shown = '.vpane:not([hidden]) ';
        return {
            vw: de.clientWidth, scrollW: de.scrollWidth,
            overflowX: de.scrollWidth > de.clientWidth + 1,
            overflowing: over.slice(0, 5),
            side: box('.side'), main: box('main'), card: box(shown + '.wcard'),
            chead: box(shown + '.chead'), dock: box('#dock'), views: box('#views'),
            cards: document.querySelectorAll(shown + '.wcard').length,
            panesShown: document.querySelectorAll('.vpane:not([hidden])').length,
            bodyBg: cs('body') ? cs('body').backgroundColor : null,
            headTitle: val(shown + '.chead .t', 'fontSize'),
            cardRadius: val(shown + '.wcard', 'borderRadius'),
            cardShadow: (val(shown + '.wcard', 'boxShadow') || '').slice(0, 42),
            deedRadius: val(shown + '.deed', 'borderRadius'),
            // 界面文案里不许出现 emoji：这一列必须是 0
            emojiLeft: (document.body.innerText.match(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu) || []).length,
        };
    }""")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for label, w, h, scheme in VIEWPORTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h},
                                            color_scheme=scheme, device_scale_factor=2)
            pg = await ctx.new_page()
            errs = []
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            await pg.goto(URL, wait_until="networkidle")
            await pg.wait_for_timeout(1000)
            r = await probe(pg)
            print(f"\n=== {label} {w}x{h} ({scheme}) ===")
            print(json.dumps(r, ensure_ascii=False, indent=1))
            print("  console 报错:", errs or "无")
            await pg.screenshot(path=f"/tmp/ui2_{label}.png", full_page=True)
            await ctx.close()
        await browser.close()


asyncio.run(main())
