#!/usr/bin/env python3
"""归藏真机验证：搜索页 —— 微信读书与 Z-Library 左右分栏、各摆各的动作。

搜索这一屏自 1.0.6 起分成两栏：左栏微信读书（搜到 → 加入书架 → 整本取回），
右栏 Z-Library（搜到 → 直接下一份 EPUB / PDF，落进「本地书架」）。两条路的动作
根本不同，所以栏头各写各话、各给各的按钮，不揉成一条控件。

这一套钉五件事：
  · 开屏先给一句「两边同时搜」的提示（没搜之前不摆空栏）；搜一次就出两栏，
    各带索引（data-z），栏头是「微信读书 / Z-Library」，小字各说各的动作。
  · 宽窗并排（右栏在右、两栏等宽），窄窗收成上下排（右栏落到左栏下面）。上下排的断点
    必须与「取消定高」的断点一致 —— 不然下半栏会被 overflow:hidden 吃掉、还滚不到。
  · 没登录时右栏搜一次要就地摆出登录表单（不必先跑设置）。
  · 左栏没配 Key 时说清「先去配 Key」，不是干等。
  · 整屏不出现横向溢出（body 不横向滚）——「文字控制在边框内」的兜底。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

BASE = selftest.need_base(1)
SHOTS = selftest.SHOTS
SHOTS.mkdir(parents=True, exist_ok=True)
SHOT = str(SHOTS / "zlib-search.png")

PANE = '#view > .vpane[data-pane="search"]'

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def txt(page, sel):
    loc = page.locator(sel)
    return loc.inner_text().strip() if loc.count() else ""


# 搜索那一屏的实时样式：定高没定高、vbody 裁不裁溢出。
# 注意：Chrome 的 getComputedStyle(el).height 对 height:auto 也会回「用出来的像素值」，
# 所以「是不是定高」不能看它 == 'auto'，只能拿它跟「视口高 - 106」（定高的那个算式）
# 比 —— 贴得上就是定高，明显矮一截就是按内容撑的。
PANE_JS = """() => {
  const pane = document.querySelector('%s');
  const body = pane && pane.querySelector(':scope > .vbody');
  return {h: pane ? pane.getBoundingClientRect().height : -1,
          lock: window.innerHeight - 106,
          ov: body ? getComputedStyle(body).overflow : ''};
}""" % PANE

OVERFLOW_JS = """() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"""


from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 880})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1200)

    page.click('#nav button[data-v="search"]')
    page.wait_for_selector(PANE, timeout=6000)
    page.wait_for_timeout(400)

    # ── 开屏：先是一句提示，讲清「两边同时搜」 ──────────────
    bodytxt = txt(page, PANE + ' .vbody')
    chk("真机：开屏提示里同时点了「微信读书」与「Z-Library」",
        '微信读书' in bodytxt and 'Z-Library' in bodytxt, bodytxt[:140])
    chk("真机：没搜之前不摆空栏（先给提示）", page.locator('.zsplit').count() == 0)
    ph = page.get_attribute('#wrq', 'placeholder')
    chk("真机：搜索框占位是「书名 / 作者 / 关键词」", ph == '书名 / 作者 / 关键词', ph)
    chk("真机：书城那一格「在网页中搜」是露着的", page.locator('#wrweb').is_visible())

    # ── 搜一次：两栏出场，各摆各的 ─────────────────────
    page.fill('#wrq', '时间简史')
    page.click('#wrgo')
    try:
        page.wait_for_selector('.zsplit', timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(600)

    wr = page.locator('.zcol[data-z="wr"]')
    zl = page.locator('.zcol[data-z="zl"]')
    chk("真机：搜一次就出两栏（左微信读书 / 右 Z-Library）",
        wr.count() == 1 and zl.count() == 1, "wr=%s zl=%s" % (wr.count(), zl.count()))
    chk("真机：左栏头写着「微信读书」", txt(page, '.zcol[data-z="wr"] .zhead .b') == '微信读书',
        txt(page, '.zcol[data-z="wr"] .zhead .b'))
    chk("真机：右栏头写着「Z-Library」", txt(page, '.zcol[data-z="zl"] .zhead .b') == 'Z-Library',
        txt(page, '.zcol[data-z="zl"] .zhead .b'))

    # 右栏没登录：就地摆登录表单（不必先跑设置）
    chk("真机：右栏没登录时就地摆出登录表单", page.locator('#zbodyZl .zlogin').count() == 1)
    chk("真机：登录表单里有邮箱与密码两格",
        page.locator('#zbodyZl #zlEmail').count() == 1
        and page.locator('#zbodyZl #zlPass').count() == 1)
    zsub = txt(page, '.zcol[data-z="zl"] .zhead .s')
    chk("真机：右栏小字说的是「下载」那条路", '下载' in zsub, zsub)

    # 左栏没配 Key：说清要先配 Key，而不是空等
    wbody = txt(page, '#zbodyWr')
    chk("真机：左栏没配 Key 时说清要先配 Key", 'Key' in wbody, wbody[:140])
    chk("真机：左栏没拿 Key 去空搜（没摆出结果网格）",
        page.locator('#zbodyWr .sgrid').count() == 0)

    # ── 宽窗：并排、等宽、定高 ─────────────────────────
    bw, bl = wr.bounding_box(), zl.bounding_box()
    chk("真机（宽窗）：两栏并排，右栏在右",
        bw and bl and bl["x"] > bw["x"] + 40 and abs(bl["y"] - bw["y"]) < 30,
        "%s / %s" % (bw, bl))
    chk("真机（宽窗）：两栏等宽（各占一半）",
        bw and bl and abs(bw["width"] - bl["width"]) <= 2,
        "宽 %s vs %s" % (bw and bw["width"], bl and bl["width"]))
    st = page.evaluate(PANE_JS)
    chk("真机（宽窗）：搜索这一屏定到视口那么高（整页不动，栏内各自滚）",
        abs(st["h"] - st["lock"]) <= 4, st)
    chk("真机（宽窗）：vbody 收着溢出（滚的是栏里的 .zbody）", st["ov"] == "hidden", st)

    page.screenshot(path=SHOT)

    # ── 各视口都不许横向溢出 ──────────────────────────
    for w in (1280, 1024, 940, 760):
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(340)
        o = page.evaluate(OVERFLOW_JS)
        chk("真机（视口 %d）：没有横向溢出（文字都在边框里）" % w,
            o["sw"] <= o["iw"] + 1, "scrollWidth %s > innerWidth %s" % (o["sw"], o["iw"]))

    # ── 窄窗：收成上下排，且不再定高、不吃溢出 ────────────
    page.set_viewport_size({"width": 940, "height": 900})
    page.wait_for_timeout(450)
    bn_w, bn_l = wr.bounding_box(), zl.bounding_box()
    chk("真机（窄窗 ≤1000px）：两栏收成上下排（右栏落到左栏下面）",
        bn_w and bn_l and bn_l["y"] > bn_w["y"] + 40, "%s / %s" % (bn_w, bn_l))
    st2 = page.evaluate(PANE_JS)
    chk("真机（窄窗）：搜索这一屏不再定到视口那么高（回到整页滚）",
        st2["h"] < st2["lock"] - 40, st2)
    chk("真机（窄窗）：vbody 不再裁掉溢出（下面那栏滚得到）", st2["ov"] != "hidden", st2)

    page.set_viewport_size({"width": 1280, "height": 880})
    page.wait_for_timeout(300)

    # ── 切到「划线内容」：网页搜收起，提示换成划线那套 ─────
    page.click('#scopeTabs button[data-s="notes"]')
    page.wait_for_timeout(300)
    chk("真机：切到「划线内容」后「在网页中搜」收起", page.locator('#wrweb').is_hidden())
    chk("真机：切到「划线内容」后提示讲的是划线",
        '划线' in txt(page, PANE + ' .vbody'), txt(page, PANE + ' .vbody')[:140])
    chk("真机：切到「划线内容」后书城那两栏收走", page.locator('.zsplit').count() == 0)
    page.click('#scopeTabs button[data-s="store"]')
    page.wait_for_timeout(300)
    chk("真机：切回「书城」后又回到两边同时搜的提示",
        '微信读书' in txt(page, PANE + ' .vbody')
        and 'Z-Library' in txt(page, PANE + ' .vbody'), txt(page, PANE + ' .vbody')[:140])

    # ── 右栏有结果时的样子：一本来一行、按格式给不给下 ───────
    # /api/zlib 换成一份编好的假回声（书名都是编的，不是真书），把「登录 → 搜 → 下载」
    # 这条前端接线走完。这一段不碰网络，也不碰沙盒书库。
    def zlib_route(route):
        try:
            req = json.loads(route.request.post_data or "{}")
        except Exception:
            req = {}
        mode = req.get("mode")
        if mode == "login":
            body = {"ok": True, "msg": "已登录 Z-Library",
                    "zlib": {"logged_in": True, "email": "reader@example.com",
                             "domain": "1lib.sk"}}
        elif mode == "search":
            body = {"ok": True, "total": 3, "page": 1, "logged_in": True, "books": [
                {"id": "1", "hash": "a1", "title": "样书甲", "author": "张三",
                 "extension": "epub", "size": "1.2 MB", "year": "1988", "language": "中文",
                 "cover": "", "publisher": "", "readable": True},
                {"id": "2", "hash": "b2", "title": "Sample Book Beta", "author": "Li Si",
                 "extension": "pdf", "size": "3.4 MB", "year": "2001", "language": "English",
                 "cover": "", "publisher": "", "readable": True},
                {"id": "3", "hash": "c3", "title": "扫件丙", "author": "",
                 "extension": "djvu", "size": "8 MB", "year": "", "language": "",
                 "cover": "", "publisher": "", "readable": False},
            ]}
        elif mode == "download":
            body = {"ok": True, "msg": "已下载《样书甲》"}
        else:
            body = {"ok": False, "msg": "这一套没铺这个动作"}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body, ensure_ascii=False))

    page.route("**/api/zlib", zlib_route)
    page.fill('#wrq', '样书')
    page.click('#wrgo')
    try:
        page.wait_for_selector('#zbodyZl .zlogin', timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(300)
    page.fill('#zbodyZl #zlEmail', 'reader@example.com')
    page.fill('#zbodyZl #zlPass', 'not-a-real-password')
    page.click('#zbodyZl #zlGo')
    try:
        page.wait_for_selector('#zbodyZl .zrow', timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(400)
    chk("真机：就地登录后右栏头改口成「已登录 · 邮箱」",
        'reader@example.com' in txt(page, '.zcol[data-z="zl"] .zhead .s'),
        txt(page, '.zcol[data-z="zl"] .zhead .s'))
    rows = page.locator('#zbodyZl .zrow')
    chk("真机：右栏结果按行排（一本一行）", rows.count() == 3, rows.count())
    chk("真机：行里带书名", '样书甲' in txt(page, '#zbodyZl'), txt(page, '#zbodyZl')[:120])
    chk("真机：行里带作者", '张三' in txt(page, '#zbodyZl'))
    chk("真机：可读格式的格式牌是点亮的那种（.ok）",
        page.locator('#zbodyZl .zrow .zb span.ok').count() == 2,
        page.locator('#zbodyZl .zrow .zb span.ok').count())
    btns = page.locator('#zbodyZl .zrow .zn button')
    chk("真机：可读的两行是「下载」钮，且点得动",
        btns.nth(0).inner_text().strip() == '下载' and btns.nth(0).is_enabled()
        and btns.nth(1).inner_text().strip() == '下载' and btns.nth(1).is_enabled(),
        [btns.nth(i).inner_text().strip() for i in range(btns.count())])
    chk("真机：阅读器收不下的格式，钮禁用并写明「阅读器打不开」",
        btns.nth(2).inner_text().strip() == '阅读器打不开' and btns.nth(2).is_disabled(),
        btns.nth(2).inner_text().strip())
    page.screenshot(path=str(SHOTS / "zlib-search-rows.png"))

    btns.nth(0).click()
    page.wait_for_timeout(700)
    chk("真机：点下载后这颗钮改口成「已入架」",
        btns.nth(0).inner_text().strip() == '已入架', btns.nth(0).inner_text().strip())
    w = txt(page, 'body .whisper')
    chk("真机：下载这件事有一句回执浮上来", '样书甲' in w, w[:80])

    # ── 设置里的 Z-Library 那一栏 ──────────────────────
    page.unroute("**/api/zlib")
    page.wait_for_timeout(2900)          # 等一轮轮询把状态照回真值（沙盒里是未登录）
    page.click('#mebtn')
    page.wait_for_selector('#meSet', timeout=6000)
    page.click('#meSet')
    page.wait_for_selector('#pop.open', timeout=6000)
    page.click('#popnav .pcat[data-cat="zlib"]')
    page.wait_for_timeout(300)
    chk("真机：设置里有「Z-Library」这一类", page.locator('#popnav .pcat[data-cat="zlib"]').count() == 1)
    chk("真机：未登录时状态那一行写「未登录」", txt(page, '#v-zlib') == '未登录', txt(page, '#v-zlib'))
    chk("真机：未登录时邮箱与密码两行都露着（才填得进去）",
        page.locator('#zlibAcct').is_visible() and page.locator('#zlibAcct2').is_visible())
    chk("真机：没登录时「退出」是灰的", page.locator('#zlibOut').is_disabled())
    chk("真机：域名一格的占位是 1lib.sk",
        page.get_attribute('#zlibDomain', 'placeholder') == '1lib.sk',
        page.get_attribute('#zlibDomain', 'placeholder'))
    hint = txt(page, '#zlibhint')
    chk("真机：域名那段说明是一句完整的话（以句号收尾）",
        hint.endswith('。') and len(hint) > 20, hint[:70])

    chk("真机：全程没有报错", not errors, errors[:6])
    browser.close()

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
print("截图：" + SHOT)
sys.exit(1 if bad else 0)
