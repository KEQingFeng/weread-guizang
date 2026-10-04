# -*- coding: utf-8 -*-
"""三格独立 + 各格自己的分类账 + 每页右上角 ⋯ —— 这一轮改动的主验收集。

为什么要真机：这轮的 bug 全长在「看不出来」的地方。前端传过去的模块名和这本书
真正住在哪一格不是一回事（刷新慢半拍就会拿旧的那份），类名和函数名撞车会静默
改到别处去，芯片条重画一次就是一次「页面自己闪一下」。这些都只有在浏览器里
点一遍才现形，所以一半是 HTTP 契约、一半是 Playwright 真点。

验的东西（每条都对应界面上真能点、或后端真会答的一件事）：
  · /api/state 的 libs / modules 六格齐、每本书带 module、tags 记在自己那一格；
  · 夹子与标签跨格不可见：剪藏的「待读」不会冒到本地书架那一格；
  · 把本地书架的书往剪藏的夹子里归 → 后端必须拒（不是静默归错）；
  · 六格表头各有一颗 ⋯，点开有菜单、Esc 与点别处都收得掉；
  · 剪藏那一格：⋯ 建夹子 → 翻开卡片 → ⋯ 归入分类 → 卡片背面写「分类 · 待读」
    → 芯片筛得出篇数、空夹子筛出「这一筛没筛到东西」→ ⋯ 贴标签 → 标签药丸能筛；
  · 筛选各记各的：剪藏筛完切去本地书架不留残渣，切回来还在；
  · 剪藏那篇不出现在别的三格，导进来的那本只出现在本地书架；
  · 全程零 console 报错。

跑之前先 `python tests/seed.py` 铺好书，或者由 run_all.sh 代劳。
收尾把这一轮建的夹子和贴的标签清干净，重跑一遍不会因为上一次的残留而变味。
"""
import atexit
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
URL = BASE + "/"
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
FAIL = []

CLIP_ID = "clip_SE_POST"
LOCAL_ID = "imp_SE_RECIPE"
VIDEO_ID = "video_SE_LECTURE"
PANES = [("shelf", "weread"), ("local", "local"), ("clip", "clip"),
         ("feed", "feed"), ("video", "video"), ("flomo", "flomo")]


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def api(path, body=None):
    """直连后端。不用 curl —— 本机没有按名字找得到的它，也不该用。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def act(action, **body):
    return api("/api/action", dict({"action": action}, **body))


def folder_ids(st, mod):
    """这一格里那些夹子的「名字」（函数名是历史包袱，断言按名字比对的）。"""
    return [f["name"] for f in (st["libs"].get(mod) or {}).get("folders", [])]


def folder_oids(st, mod):
    """同上，但要的是 id —— 界面上点「新建文件夹」建的夹子，测试事先不知道它的 id，
    只能靠前后两次快照求差，才能收尾时把它一起删掉（不然下一次跑就撞同名药丸）。"""
    return {f["id"] for f in (st["libs"].get(mod) or {}).get("folders", [])}


def wait_new_folders(mod, before_ids, ms=5000):
    """等界面上那一下「建好」真的落进账本，再把新出现的 id 交出去。

    点下去只是发了个异步 POST：立刻求差常常是空的 —— 那颗夹子既没被断言看见，
    也没进收尾那本账，于是这一次跑成一颗「删不掉的新药丸」、下一次跑多一座同名坟。
    门禁里那条「只多这一颗」的失败就是这么来的，不是后端一次建了两颗。
    """
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        new = folder_oids(api("/api/state"), mod) - before_ids
        if new:
            return new
        time.sleep(0.1)
    return set()


def tags_of(st, mod, bid):
    return ((st["libs"].get(mod) or {}).get("tags") or {}).get(bid) or []


def book_of(st, bid):
    return next((b for b in st["books"] if b["id"] == bid), None)


# ── 一、接口契约：账本分格与跨格隔离（不起浏览器也能钉死）──────────
st = api("/api/state")
mods = [m["id"] for m in st.get("modules", [])]
chk("state：modules 报出六格",
    mods == ["weread", "local", "clip", "feed", "video", "flomo"], mods)
chk("state：libs 的六格齐、每格有 folders/states/tags",
    sorted(st.get("libs", {})) == sorted(mods)
    and all({"folders", "states", "tags"} <= set(st["libs"][m]) for m in mods),
    {m: sorted(st["libs"][m]) for m in mods})
chk("state：每一本都写清自己住哪一格",
    all(b.get("module") in mods for b in st["books"]),
    [b["id"] for b in st["books"] if b.get("module") not in mods])
chk("state：一格一本书只出现一次（不重复铺卡）",
    len({b["id"] for b in st["books"]}) == len(st["books"]),
    [b["id"] for b in st["books"]])
for bid, want, what in ((CLIP_ID, "clip", "剪藏那篇"), (LOCAL_ID, "local", "导进来的那本"),
                        (VIDEO_ID, "video", "转出来的视频")):
    b = book_of(st, bid)
    chk(f"state：{what}落在「{want}」这一格", bool(b) and b["module"] == want, b)

made_folders = []   # 待删的账：这一趟建过哪些夹子（模块, id）
made_log = []       # 建的时候后端怎么回执的；收尾没清干净时打印这个看


def new_folder(mod, name):
    """建一个夹子，同时记进收尾那本账 —— 忘了记的夹子会留到下一次跑。"""
    r = act("folder.new", module=mod, name=name)
    made_folders.append((mod, r.get("id")))
    made_log.append((mod, name, r.get("id"), r.get("ok")))
    return r


def clean_up():
    """把这一趟造的夹子、标签、归类全退回去，两段各扫一次。

    界面上点「新建文件夹」造出来的那一颗，测试事先不知道 id，所以建完必须顺手
    补进 made_folders（见真机那一段的前后快照求差）—— 漏了的话下一次跑会撞出两颗
    同名药丸，「归进哪一颗」变成看运气，芯片计数跟着一起失真。
    """
    st = api("/api/state")
    for bid in (CLIP_ID, LOCAL_ID):
        b = book_of(st, bid)
        if not b:
            continue
        for t in tags_of(st, b["module"], bid):
            act("tag.drop", module=b["module"], name=t)
        if b.get("folder"):
            act("book.move", book=bid, folder="")
    for mod, f in made_folders:
        if f:
            act("folder.drop", module=mod, id=f)
    del made_folders[:]


def _clean_at_exit():
    # 跑测脚本会在我们退出前把服务收掉；那时候清不清得动都不该把退出码搞脏。
    try:
        clean_up()
    except Exception as e:
        print("（退出时的收尾没做成：%s）" % e)


atexit.register(_clean_at_exit)
try:
    r = new_folder("clip", "待读")
    fid = r.get("id") or ""
    chk("folder.new：剪藏格里建得出夹子", r.get("ok") is True and bool(fid), r)
    st = api("/api/state")
    chk("folder.new：夹子记在剪藏自己那一格", "待读" in folder_ids(st, "clip"), folder_ids(st, "clip"))
    chk("folder.new：别的格子看不见它（本地书架 / 微信读书 / 订阅 / 视频）",
        all(not folder_ids(st, m) for m in ("local", "weread", "feed", "video")),
        {m: folder_ids(st, m) for m in ("local", "weread", "feed", "video")})

    bad = act("book.move", book=LOCAL_ID, folder=fid)
    chk("book.move：本地书架的书不许归进剪藏的夹子（要拒绝，不许静默归错）",
        bad.get("ok") is False and "文件夹" in (bad.get("msg") or ""), bad)
    good = act("book.move", book=CLIP_ID, folder=fid)
    chk("book.move：剪藏的书归进剪藏的夹子能成", good.get("ok") is True, good)
    st = api("/api/state")
    chk("book.move：归完之后书的 folder 就是那个夹子",
        (book_of(st, CLIP_ID) or {}).get("folder") == fid, book_of(st, CLIP_ID))
    chk("book.move：没动到的那本仍是空的",
        (book_of(st, LOCAL_ID) or {}).get("folder") == "", book_of(st, LOCAL_ID))

    # 标签：同名两张，各在一格。改名只准动自己那一格。
    act("book.tag", book=CLIP_ID, add=["待读"])
    act("book.tag", book=LOCAL_ID, add=["待读"])
    st = api("/api/state")
    chk("book.tag：标签落在书自己那一格的账上",
        tags_of(st, "clip", CLIP_ID) and tags_of(st, "local", LOCAL_ID)
        and "tags" not in json.dumps({m: tags_of(st, m, CLIP_ID)
                                      for m in ("weread", "feed", "video")}),
        {m: tags_of(st, m, CLIP_ID) for m in mods})
    rn = act("tag.rename", module="clip", **{"from": "待读", "to": "精读"})
    chk("tag.rename：只在剪藏格里改名", rn.get("ok") is True, rn)
    st = api("/api/state")
    chk("tag.rename：剪藏那篇变成「精读」",
        tags_of(st, "clip", CLIP_ID) == ["精读"], tags_of(st, "clip", CLIP_ID))
    chk("tag.rename：本地书架同名那张没被带走",
        tags_of(st, "local", LOCAL_ID) == ["待读"], tags_of(st, "local", LOCAL_ID))

    r2 = new_folder("clip", "待读")
    chk("folder.new：一格允许同名夹子存在，但各是一个 id（不覆盖别人的账）",
        r2.get("ok") is True and r2.get("id") != fid, r2)
    act("folder.rename", module="clip", id=r2["id"], name="改过名的")
    chk("folder.rename：改名改到自己那一格",
        "改过名的" in folder_ids(api("/api/state"), "clip"), folder_ids(api("/api/state"), "clip"))
    cross = act("folder.rename", module="local", id=fid, name="不该改成")
    chk("folder.rename：拿剪藏的 id 去本地书架格改名会被拒", cross.get("ok") is False, cross)
    drop = act("folder.drop", module="clip", id=r2["id"])
    chk("folder.drop：删得掉，且里面的书回到未归类",
        drop.get("ok") is True and "改过名的" not in folder_ids(api("/api/state"), "clip"), drop)
finally:
    clean_up()
    after = api("/api/state")
    chk("第一段收尾：接口这一段造的夹子与标签清干净了",
        not folder_ids(after, "clip") and not tags_of(after, "clip", CLIP_ID)
        and not tags_of(after, "local", LOCAL_ID),
        {"folders": folder_ids(after, "clip"), "built": made_log,
         "tags": [tags_of(after, "clip", CLIP_ID), tags_of(after, "local", LOCAL_ID)]})


# ── 二、真机：六格的 ⋯、芯片条、卡片只在本格 ─────────────────────
def flush():
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


def settle(expr, ms=3000):
    """等一个条件成立，等不到只回 False（不炸断后面几十条）。

    注意 expr 必须是一个「会自己算出真假」的箭头函数，直接交给 wait_for_function：
    先前在这里套了一层 `() => (expr)`，等到的是那个函数对象本身 —— 函数恒为真，
    于是所有 settle 当场假通过，芯片条那条明明还没补画，断言却说「出现了」。
    """
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def sheet_shown(title):
    """弹层真的开着、标题就是那一句。只读 #sheetBody.value 不算数：
    关掉弹层不清空，上一轮填的字还挂在那儿，会把「没开成」看成「开对了」。"""
    return page.evaluate("""(t) => {
      const v = document.querySelector('#veil');
      const t2 = document.querySelector('#sheetTitle');
      return !!(v && v.classList.contains('open') && t2
        && t2.textContent.includes(t) && !document.querySelector('#sheetBody').hidden);
    }""", title)


def sheet_type(txt):
    """往弹层那个框里填字。没开着就直接回 False，不硬填 ——
    page.fill 对着一份不可见的 textarea 会干等 30 秒超时，把后面几十条断言一起带走。"""
    if not page.evaluate("() => { const v = document.querySelector('#veil');"
                         " return !!(v && v.classList.contains('open')); }"):
        return False
    page.fill("#sheetBody", txt)
    return True


def sheet_press(label):
    """点弹层里那颗动作按钮；弹层没开着同样直接回 False。"""
    if not page.evaluate("() => { const v = document.querySelector('#veil');"
                         " return !!(v && v.classList.contains('open')); }"):
        return False
    page.locator("#sheetActs button", has_text=label).first.click()
    return True


def goto_pane(name):
    page.click('#nav button[data-v="%s"]' % name)
    page.wait_for_timeout(600)
    flush()


def chip_rows(host):
    """芯片条两排：第一排夹子、第二排标签。管理那几颗是 <button class="folder">，
    所以只数 span.folder —— 药丸和按钮同一个类名，混着数会点错东西。"""
    return page.evaluate("""(h) => [...document.querySelectorAll(h + ' .chiprow')].map(r =>
      [...r.querySelectorAll('span.folder')].map(c => ({
        name: (c.firstElementChild ? c.firstElementChild.textContent : '').trim(),
        n: c.querySelector('em') ? c.querySelector('em').textContent.trim() : '',
        on: c.classList.contains('on')})))""", host)


def click_chip(host, row, label):
    return page.evaluate("""([h, i, t]) => {
      const r = document.querySelectorAll(h + ' .chiprow')[i];
      if (!r) return false;
      const c = [...r.querySelectorAll('span.folder')].find(x =>
        ((x.firstElementChild ? x.firstElementChild.textContent : '').trim() === t));
      if (!c) return false;
      c.click(); return true;
    }""", [host, row, label])


def menu():
    return page.evaluate("""() => {
      const m = document.querySelector('.kmenu');
      if (!m) return null;
      const hd = m.querySelector('.hd');
      return {head: hd ? hd.textContent.trim() : '',
              items: [...m.querySelectorAll('button')].map(b => b.textContent.trim())};
    }""")


def click_menu(label):
    return page.evaluate("""(t) => {
      const m = document.querySelector('.kmenu');
      if (!m) return false;
      const b = [...m.querySelectorAll('button')].find(x => x.textContent.trim().startsWith(t));
      if (!b) return false;
      b.click(); return true;
    }""", label)


def open_card(sel):
    """点封面把这张卡翻开：背面那排动作条（分类 / 标签 / 定位 / 删除）才亮，
    右上角 ⋯ 也从「讲这一格」切换成「讲这一篇」。"""
    page.evaluate("""(s) => {
      const c = document.querySelector(s);
      if (c) c.querySelector('.cv').click();
    }""", sel)
    flush()


def click_strip(sel, label):
    return page.evaluate("""([s, t]) => {
      const c = document.querySelector(s);
      if (!c) return false;
      const b = [...c.querySelectorAll('.strip button')].find(x => x.textContent.trim() === t);
      if (!b) return false;
      b.click(); return true;
    }""", [sel, label])


def card_cats(sel):
    """卡片背面那行：分类 · 某某，加贴上过的标签。"""
    return page.evaluate("""(s) => {
      const c = document.querySelector(s);
      const box = c && c.querySelector('.mt .c');
      if (!box) return null;
      return {text: box.innerText.trim(),
              folders: [...box.querySelectorAll('i:not([title])')].map(x => x.textContent.trim()),
              tags: [...box.querySelectorAll('i[title]')].map(x => x.textContent.trim())};
    }""", sel)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []

    def note(t):
        # 「Failed to load resource」那类是封面图 404 之类，界面本身没坏；
        # 真要看的是脚本抛错与未接上的按钮。
        if "Failed to load resource" not in t:
            errors.append(t)

    page.on("console", lambda m: note(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: note(str(e)))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(900)

    # 上一段（HTTP 契约）建过的夹子与标签必须先清干净 —— 不清的话这一段「新建文件夹」
    # 会撞出两颗同名药丸，归进去的是哪一颗都说不清，计数断言就成了看运气。
    base = api("/api/state")
    chk("开局是干净的（上一段没留下夹子与标签）",
        not folder_ids(base, "clip") and not tags_of(base, "clip", CLIP_ID)
        and not tags_of(base, "local", LOCAL_ID),
        {"folders": folder_ids(base, "clip"),
         "tags": [tags_of(base, "clip", CLIP_ID), tags_of(base, "local", LOCAL_ID)]})

    # ── 1. 六格表头各有一颗 ⋯ ────────────────────────────────
    for pane, mod in PANES:
        goto_pane(pane)
        chk(f"「{pane}」pane 切得进去", page.evaluate(
            "() => { const p = document.querySelector('.vpane[data-pane=\"%s\"]');"
            " return !!p && !p.hidden; }" % pane))
        loc = page.locator('[data-pane="%s"] .chead .kbtn' % pane)
        chk(f"{mod}那一格表头有且只有一颗 ⋯", loc.count() == 1, loc.count())
        chk(f"⋯ 上挂的是这一格自己的模块名", page.evaluate(
            "() => (document.querySelector('[data-pane=\"%s\"] .kbtn') || {})"
            ".dataset?.mod" % pane) == mod)

    # ── 2. 卡片只在自己那一格 ────────────────────────────────
    goto_pane("clip")
    chk("剪藏格里看得见剪藏那篇", page.locator(
        '[data-pane="clip"] .wcard[data-id="%s"]' % CLIP_ID).count() == 1)
    for pane in ("local", "shelf", "video"):
        chk(f"剪藏那篇不出现在「{pane}」格里", page.locator(
            '[data-pane="%s"] [data-id="%s"]' % (pane, CLIP_ID)).count() == 0)
    chk("导进来的那本只出现在本地书架", page.locator(
        '[data-pane="local"] .wcard[data-id="%s"]' % LOCAL_ID).count() == 1)
    for pane in ("clip", "shelf", "video"):
        chk(f"本地那本不出现在「{pane}」格里", page.locator(
            '[data-pane="%s"] [data-id="%s"]' % (pane, LOCAL_ID)).count() == 0)
    chk("侧边栏「剪藏」徽章数的是这一格的书", page.evaluate("() => document.querySelector('#bgClip').textContent")
        == str(len([b for b in api("/api/state")["books"] if b["module"] == "clip"])),
        page.evaluate("() => document.querySelector('#bgClip').textContent"))

    # ── 3. ⋯ 菜单开合 ───────────────────────────────────────
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(250)
    m = menu()
    chk("点 ⋯ 弹出菜单", m is not None)
    chk("菜单讲的是这一格（不是「全部」）", m and m["head"] == "剪藏", m)
    chk("菜单里有新建文件夹 / 管理分类 / 管理标签",
        m and any(x.startswith("新建文件夹") for x in m["items"])
        and any(x.startswith("管理分类") for x in m["items"])
        and any(x.startswith("管理标签") for x in m["items"]), m)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    chk("按 Esc 菜单收掉", page.locator(".kmenu").count() == 0)
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(200)
    chk("Esc 之后再点 ⋯ 打得开", page.locator(".kmenu").count() == 1)
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(200)
    chk("再点同一颗 ⋯ 会收起（不是叠第二张菜单）", page.locator(".kmenu").count() == 0)
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(200)
    page.click('[data-pane="clip"] .chead .t')
    page.wait_for_timeout(200)
    chk("点菜单外别处也收得掉", page.locator(".kmenu").count() == 0)

    # ── 4. ⋯ 里建文件夹 → 芯片条出现，且只在这一格出现 ──────────
    goto_pane("clip")
    # 界面上建的那颗夹子，测试事先不知道 id：建之前拍一份快照，事后求差补进收尾的账
    before_ids = folder_oids(api("/api/state"), "clip")
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(200)
    chk("⋯ 里点「新建文件夹」开弹层", click_menu("新建文件夹"))
    page.wait_for_timeout(300)
    chk("弹层真的开着，标题说清是在剪藏格里建", sheet_shown("在剪藏里新建文件夹"),
        page.evaluate("() => document.querySelector('#sheetTitle').textContent"))
    chk("弹层里那个框打得进字（editable 真的解开了）", page.evaluate(
        "() => !document.querySelector('#sheetBody').readOnly"
        " && !document.querySelector('#sheetBody').hasAttribute('readonly')"))
    chk("填名字、点「建好」这一步走得动", sheet_type("待读"))
    sheet_press("建好")
    built = wait_new_folders("clip", before_ids)
    made_folders += [("clip", i) for i in built]
    chk("界面上建这一颗，后端账上就只多这一颗（不是一次建出两颗同名的）",
        len(built) == 1, built)
    chk("建好之后剪藏格的芯片条出现「待读」", settle(
        "() => [...document.querySelectorAll('#cChips .chiprow')[0]"
        ".querySelectorAll('span.folder')].some(c => c.firstElementChild.textContent === '待读')"))
    rows = chip_rows("#cChips")
    chk("新夹子先没书（计数 0）", any(c["name"] == "待读" and c["n"] == "0" for c in rows[0]), rows)
    goto_pane("local")
    chk("本地书架的芯片条看不见剪藏那个夹子",
        not any(c["name"] == "待读" for r in chip_rows("#lChips") for c in r), chip_rows("#lChips"))
    goto_pane("shelf")
    chk("微信读书那一格也看不见它",
        not any(c["name"] == "待读" for r in chip_rows("#sChips") for c in r), chip_rows("#sChips"))
    chk("三格各是一份独立的账（JS 侧同一口径）", page.evaluate(
        "() => modFolders('clip').some(f => f.name === '待读')"
        " && !modFolders('local').length && !modFolders('weread').length"))

    # ── 5. 翻开卡片 → ⋯ → 归入分类 → 背面写「分类 · 待读」 ────────
    goto_pane("clip")
    CLIP_CARD = '[data-pane="clip"] .wcard[data-id="%s"]' % CLIP_ID
    chk("⋯ 认得出翻开的那张卡（没翻开时讲整格）", page.evaluate(
        "() => openCardId('clip') === ''"))
    open_card(CLIP_CARD)
    chk("点封面把卡片翻开了", page.locator(CLIP_CARD + ".open").count() == 1)
    chk("翻开后 ⋯ 就改讲这一篇", page.evaluate(
        "() => openCardId('clip') === '%s'" % CLIP_ID))
    chk("卡片背面有「分类」这颗", page.evaluate(
        "() => [...document.querySelectorAll('%s .strip button')]"
        ".some(b => b.textContent.trim() === '分类')" % CLIP_CARD))
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(250)
    m = menu()
    chk("翻开卡片后菜单头是书名（截断到 18 字）",
        m and m["head"].startswith("一篇剪藏下来的文章"), m)
    chk("翻开卡片后菜单给的是这一篇的动作",
        m and any(x.startswith("归入分类") for x in m["items"])
        and any(x.startswith("为这一篇打标签") for x in m["items"]), m)
    click_menu("归入分类")
    page.wait_for_timeout(300)
    chk("归入弹层真的开着", sheet_shown("把这篇归进哪一类"),
        page.evaluate("() => document.querySelector('#sheetTitle').textContent"))
    # 只列这一格的夹子：本格那个「待读」在，别的格子一个都不许混进来
    chk("归入弹层只列这一格的夹子", page.evaluate(
        "() => { const b = document.querySelector('#sheetBody');"
        " const names = [...modFolders('local'), ...modFolders('weread')]"
        ".map(f => f.name).filter(n => n && n !== '待读');"
        " return b.value.includes('待读') && !names.some(n => b.value.includes(n)); }"),
        page.evaluate("() => document.querySelector('#sheetBody').value"))
    chk("填夹子名、点「归入」这一步走得动", sheet_type("待读"))
    sheet_press("归入")
    chk("归入之后卡片背面写着「分类 · 待读」", settle(
        "() => [...document.querySelectorAll('%s .mt .c i')]"
        ".some(x => x.textContent.trim() === '分类 · 待读')" % CLIP_CARD), 4000)
    chk("剪藏格徽章与芯片计数跟上（1 篇）", settle(
        "() => [...document.querySelectorAll('#cChips .chiprow')[0]"
        ".querySelectorAll('span.folder')].some(c => c.firstElementChild.textContent === '待读'"
        " && c.querySelector('em').textContent === '1')", 4000),
        chip_rows("#cChips"))
    chk("归完之后后端账上就是这个夹子", bool(book_of(api("/api/state"), CLIP_ID)["folder"]))

    # ── 6. 芯片筛选：筛得到、筛空了说实话 ─────────────────────
    click_chip("#cChips", 0, "待读")
    page.wait_for_timeout(400)
    chk("点「待读」筛出 1 篇", page.locator(
        '#cShelf .wcard').count() == 1 and page.evaluate(
            "() => (document.querySelector('#cGridCnt').textContent || '')"
            ".includes('筛出 1 篇')"), chip_rows("#cChips"))
    chk("那颗芯片亮着选中态", any(c["on"] for c in chip_rows("#cChips")[0] if c["name"] == "待读"))
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(250)
    chk("筛着的时候菜单里多一颗「清掉筛选」",
        any(x.startswith("清掉筛选") for x in (menu() or {}).get("items", [])), menu())
    click_menu("清掉筛选")
    page.wait_for_timeout(400)
    chk("清掉筛选回到整格", page.evaluate("() => !uiOf('clip').folder"))
    # 空夹子：筛没了不许说「这一格还是空的」，得说「这一筛没筛到东西」
    ok = new_folder("clip", "空夹子")
    goto_pane("local")
    goto_pane("clip")
    chk("新建的夹子由轮询补进芯片条（不重画整格）", settle(
        "() => [...document.querySelectorAll('#cChips .chiprow')[0]"
        ".querySelectorAll('span.folder')].some(c => c.firstElementChild.textContent === '空夹子')"), 5000)
    click_chip("#cChips", 0, "空夹子")
    chk("筛一个空夹子：卡片清空", settle("() => document.querySelectorAll('#cShelf .wcard').length === 0"))
    chk("筛空了说实话（不是「这一格还没有东西」）", page.evaluate(
        "() => (document.querySelector('#cShelf .emptyline').innerText || '')"
        ".includes('这一筛没筛到东西')"),
        page.evaluate("() => (document.querySelector('#cShelf').innerText || '')"))
    click_chip("#cChips", 0, "全部")
    page.wait_for_timeout(400)
    chk("点「全部」回到整格", page.locator('#cShelf .wcard').count() == 1)

    # ── 7. ⋯ 里贴标签 → 第二排药丸能筛 ───────────────────────
    open_card(CLIP_CARD)
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(250)
    click_menu("为这一篇打标签")
    page.wait_for_timeout(300)
    chk("贴标签的弹层真的开着", sheet_shown("给这一篇贴标签"),
        page.evaluate("() => document.querySelector('#sheetTitle').textContent"))
    chk("填标签、点「贴好」这一步走得动", sheet_type("精读"))
    sheet_press("贴好")
    chk("贴完标签，第二排出现「精读」药丸", settle(
        "() => { const r = document.querySelectorAll('#cChips .chiprow')[1];"
        " return !!r && [...r.querySelectorAll('span.folder')]"
        ".some(c => c.firstElementChild.textContent === '精读'); }"), 4000)
    chk("卡片背面也挂上那片标签", card_cats(CLIP_CARD)
        and "精读" in (card_cats(CLIP_CARD) or {}).get("text", ""), card_cats(CLIP_CARD))
    click_chip("#cChips", 1, "精读")
    page.wait_for_timeout(400)
    chk("按标签筛出这一篇", page.locator("#cShelf .wcard").count() == 1
        and page.evaluate("() => uiOf('clip').tag === '精读'"))

    # ── 8. 筛选各记各的：切格子不带残渣、切回来还在 ───────────
    goto_pane("local")
    chk("切去本地书架：那一格没有剪藏留下的筛选",
        page.evaluate("() => !uiOf('local').folder && !uiOf('local').tag && !uiOf('local').q"),
        page.evaluate("() => JSON.stringify(uiOf('local'))"))
    # 「全部」本来就一直亮着（它是没筛选时的默认态），要看的是别的格子有没有跟着亮
    chk("切去本地书架：剪藏那两颗药丸没在这格亮着", not any(
        c["on"] for r in chip_rows("#lChips") for c in r if c["name"] != "全部"),
        chip_rows("#lChips"))
    chk("本地书架格里仍是它自己那本书", page.locator(
        '[data-pane="local"] .wcard[data-id="%s"]' % LOCAL_ID).count() == 1)
    goto_pane("clip")
    chk("切回剪藏：刚才那一道筛选还在", page.evaluate(
        "() => uiOf('clip').tag === '精读'"))
    chk("切回来芯片仍是选中态", any(
        c["on"] for c in chip_rows("#cChips")[1] if c["name"] == "精读"), chip_rows("#cChips"))
    page.screenshot(path=str(selftest.SHOTS / "module-shelf-clip.png"))

    # ── 9. 视频格也独立（左栏按格取书，不认 source 字段） ───────
    goto_pane("video")
    chk("视频格里看得见转出来的那本", page.evaluate(
        "() => videoBooks().some(b => b.id === '%s')" % VIDEO_ID))
    chk("视频格里没有剪藏与本地那两本", page.evaluate("""() => {
      const ids = videoBooks().map(b => b.id);
      return !ids.includes('%s') && !ids.includes('%s');
    }""" % (CLIP_ID, LOCAL_ID)))
    chk("视频那一格表头也有一颗 ⋯，点开讲的是视频", page.evaluate("""() => {
      const b = document.querySelector('[data-pane="video"] .kbtn');
      if (!b) return 'no-btn';
      b.click();
      const hd = document.querySelector('.kmenu .hd');
      return hd ? hd.textContent.trim() : 'no-menu';
    }""") == "视频")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    chk("全程没有脚本报错", not errors, errors[:6])
    browser.close()

# 菜单是挂在 body 上的一份，关掉之后不该在 DOM 里留空壳
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(900)
    goto_pane("clip")
    page.click('[data-pane="clip"] .chead .kbtn')
    page.wait_for_timeout(250)
    n_open = page.locator(".kmenu").count()
    page.evaluate("() => closeKMenu()")
    page.wait_for_timeout(150)
    chk("菜单收起来之后 DOM 里不留空壳", n_open == 1 and page.locator(".kmenu").count() == 0)
    browser.close()

# 真机那一段建的夹子（点 ⋯ 建的那颗、空夹子）与贴的标签，最后一起退回去。
# 这一步不做好，下一次跑就是「两颗同名药丸 + 归进哪一颗看运气」。
clean_up()
end = api("/api/state")
chk("全跑完收尾：硬盘上不留这一趟造的夹子与标签",
    not folder_ids(end, "clip") and not tags_of(end, "clip", CLIP_ID)
    and not tags_of(end, "local", LOCAL_ID),
    {"folders": {m: folder_ids(end, m) for m in
                 ("weread", "local", "clip", "feed", "video", "flomo")},
     "built": made_log,
     "tags": [tags_of(end, "clip", CLIP_ID), tags_of(end, "local", LOCAL_ID)]})

print()
print(f"三格独立：{'全部通过' if not FAIL else str(len(FAIL)) + ' 项失败 -> ' + ' | '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
