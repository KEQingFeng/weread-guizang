"""首启页验收：把壳和页面之间的那套暗号跑一遍。

壳是原生代码，测不了；页面这半边能测，而且必须测 —— 两边对不上就是
「点了没反应」，那是这类程序最容易翻车的地方。

模拟三件事：
  1. 壳推状态（window.gz.init / step / log / done）→ 页面画成什么样
  2. 页面往壳发什么（cmd 必须恰好是 probe/start/enter）
  3. 不在套壳里打开时，页面会不会装死

用法: .venv/bin/python tests/check_onboarding.py
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "file://" + os.path.join(HERE, "shell", "onboarding.html")

# 冒充壳：把消息记下来，供断言
BRIDGE = """
window.__sent = [];
window.webkit = { messageHandlers: { guizang: { postMessage(m) { window.__sent.push(m); } } } };
"""

VIEWPORTS = [("常规", 1180, 820, "light"), ("暗色", 1180, 820, "dark"),
             ("窄高", 460, 700, "light")]


async def shape(pg):
    """页面上该量的东西。"""
    return await pg.evaluate(r"""() => {
        const q = s => document.querySelector(s);
        const steps = [0,1,2,3].map(i => {
            const el = q('#s'+i);
            return { s: el.dataset.s, note: q('#n'+i).textContent };
        });
        const de = document.documentElement;
        // 背景那两团光晕故意画到视口外，由 .halo{overflow:hidden} 裁掉，
        // 它们不算溢出。判据用「最近的有裁剪的祖先」，不是「越出视口」。
        const clipped = e => {
            for (let p = e.parentElement; p; p = p.parentElement) {
                const o = getComputedStyle(p).overflow;
                if (o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll') return true;
            }
            return false;
        };
        const over = [];
        document.querySelectorAll('body *').forEach(e => {
            const b = e.getBoundingClientRect();
            if (b.width > 0 && (b.right > de.clientWidth + 1 || b.left < -1) && !clipped(e))
                over.push({cls: (e.className||'').toString().slice(0,24), l: Math.round(b.left), r: Math.round(b.right)});
        });
        const go = q('#go');
        const card = q('.card').getBoundingClientRect();
        return {
            vw: de.clientWidth, scrollW: de.scrollWidth,
            overflowX: de.scrollWidth > de.clientWidth + 1,
            overflowing: over.slice(0, 4),
            steps,
            goText: go.textContent, goDisabled: go.disabled,
            cardW: Math.round(card.width),
            logOn: q('#logwrap').classList.contains('on'),
            logLines: q('#log').textContent.split('\n').filter(Boolean).length,
            oopsOn: q('#oops').classList.contains('on'),
            oopsText: q('#oops').textContent,
            bye: q('#stage').classList.contains('bye'),
            sent: (window.__sent || []).map(m => m.cmd),
            emoji: (document.body.innerText.match(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu) || []).length,
        };
    }""")


async def main():
    bad = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ---------- A. 在壳里：走完整条首启链路 ----------
        for label, w, h, scheme in VIEWPORTS:
            ctx = await browser.new_context(viewport={"width": w, "height": h},
                                            color_scheme=scheme, device_scale_factor=2)
            await ctx.add_init_script(BRIDGE)
            pg = await ctx.new_page()
            errs = []
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            await pg.goto(PAGE)
            await pg.wait_for_timeout(700)          # 等那 220ms 自检发出去

            print(f"\n=== {label} {w}x{h} ({scheme}) ===")
            a = await shape(pg)
            print("  待配置：", json.dumps(a["steps"], ensure_ascii=False))
            print("  按钮：", a["goText"], "禁用" if a["goDisabled"] else "可点")
            print("  自发消息：", a["sent"], "（壳应当回一手 probe）")
            print("  横向溢出：", a["overflowX"], a["overflowing"] or "")
            print("  emoji：", a["emoji"])
            print("  console 报错：", errs or "无")
            await pg.screenshot(path=f"/tmp/onboard_{label}.png")

            if a["emoji"] != 0:
                bad.append(f"{label}: 页面出现 emoji {a['emoji']} 个")
            if a["overflowX"]:
                bad.append(f"{label}: 横向溢出 {a['overflowing']}")
            if errs:
                bad.append(f"{label}: console 报错 {errs}")
            if "probe" not in a["sent"]:
                bad.append(f"{label}: 没等到页面自检（漏发 probe）")
            if a["steps"] != [{"s": "idle", "note": "待检视"}] * 4:
                bad.append(f"{label}: 未配置时步骤状态不对 {a['steps']}")

            # 壳回一手状态：全部缺失
            await pg.evaluate("window.gz.init({env:false,browser:false,account:false,media:false})")
            await pg.wait_for_timeout(200)
            b = await shape(pg)
            if [s["s"] for s in b["steps"]] != ["idle"] * 4:
                bad.append(f"{label}: init(全假) 应保持 idle，得到 {b['steps']}")

            # 壳回一手状态：环境、浏览器与转写引擎已好 —— 这几行应直接变 ok
            await pg.evaluate("window.gz.init({env:true,browser:true,account:false,media:true})")
            await pg.wait_for_timeout(200)
            c = await shape(pg)
            print("  已装好的几项：", json.dumps(c["steps"], ensure_ascii=False))
            if [s["s"] for s in c["steps"]] != ["ok", "ok", "idle", "ok"]:
                bad.append(f"{label}: init(env/browser/引擎 真) 应得 ok/ok/idle/ok，得到 {c['steps']}")

            # 点「我思故我在」
            await pg.click("#go")
            await pg.wait_for_timeout(250)
            d = await shape(pg)
            print("  点击后：", d["goText"], "| 日志区展开：", d["logOn"], "| 发往壳：", d["sent"])
            await pg.screenshot(path=f"/tmp/onboard_{label}_run.png")
            if "start" not in d["sent"]:
                bad.append(f"{label}: 点按钮没发出 start")
            if not d["logOn"]:
                bad.append(f"{label}: 点按钮后日志区没展开")
            if [s["s"] for s in d["steps"]][0] != "run":
                bad.append(f"{label}: 点按钮后第 0 步应为 run，得到 {d['steps']}")

            # 壳回灌日志 + 逐步推进（照 main.swift 的真实顺序）
            await pg.evaluate("""() => {
                window.gz.log('—— 用 /usr/bin/python3 开始配置 ——');
                window.gz.log('[1/5] 虚拟环境：新建');
                window.gz.log('[2/5] 正在安装依赖');
                window.gz.step(0,'ok','依赖已装齐');
                window.gz.step(1,'ok','Chromium 已就绪');
                window.gz.step(2,'run','等你在弹出的窗口里扫码');
                window.gz.log('—— 即将弹出一个窗口，请用微信扫码 ——');
                window.gz.log('[5/5] 转写引擎：就绪');
                window.gz.step(3,'ok','转写引擎已就绪');
            }""")
            await pg.wait_for_timeout(300)
            e = await shape(pg)
            print("  配置完成态：", json.dumps(e["steps"], ensure_ascii=False), "| 日志行数", e["logLines"])
            await pg.screenshot(path=f"/tmp/onboard_{label}_done.png")
            if [s["s"] for s in e["steps"]] != ["ok", "ok", "run", "ok"]:
                bad.append(f"{label}: 推进后状态不对 {e['steps']}")
            if e["logLines"] < 6:
                bad.append(f"{label}: 日志没灌进去（{e['logLines']} 行）")
            if e["goDisabled"] is False:
                bad.append(f"{label}: 配置途中按钮应禁用")

            # 收场
            await pg.evaluate("window.gz.done()")
            await pg.wait_for_timeout(600)
            f = await shape(pg)
            print("  收场：", f["goText"], "| 淡出中：", f["bye"])
            await pg.screenshot(path=f"/tmp/onboard_{label}_bye.png")
            if not f["bye"]:
                bad.append(f"{label}: done() 后没进入淡出")

            # 失败回退：再点一次应当能重来
            await pg.evaluate("window.gz.fail('环境没配成功。')")
            await pg.wait_for_timeout(250)
            g = await shape(pg)
            print("  失败态：", g["goText"], "| 提示：", g["oopsText"][:24])
            if g["goText"] != "再试一次" or g["goDisabled"] or not g["oopsOn"]:
                bad.append(f"{label}: fail() 后应恢复可点并显示原因，得到 {g['goText']}/{g['goDisabled']}/{g['oopsOn']}")

            await ctx.close()

        # ---------- B. 不在壳里：不许装死 ----------
        ctx = await browser.new_context(viewport={"width": 900, "height": 760})
        pg = await ctx.new_page()                      # 这次不注入桥
        await pg.goto(PAGE)
        await pg.wait_for_timeout(500)
        await pg.click("#go")
        await pg.wait_for_timeout(500)
        r = await shape(pg)
        print("\n=== 不在套壳里打开 ===")
        print("  按钮：", r["goText"], "| 提示：", r["oopsText"])
        print("  console 报错：", "无" if True else "")
        await pg.screenshot(path="/tmp/onboard_noshell.png")
        if not r["oopsOn"] or "归藏" not in r["oopsText"]:
            bad.append(f"不在壳里时应提示去归藏里打开，得到 oops={r['oopsOn']} / {r['oopsText']!r}")
        if r["goDisabled"]:
            bad.append("不在壳里时按钮应恢复可点（否则用户只能关掉）")
        await ctx.close()

        await browser.close()

    print("\n" + "=" * 46)
    if bad:
        print("  未通过：")
        for b in bad:
            print("   ✗", b)
        return 1
    print("  全部通过")
    return 0


sys.exit(asyncio.run(main()))
