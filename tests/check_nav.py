#!/usr/bin/env python3
"""归藏真机验证：侧边栏导航、个人主界面与两张热力图。

1.0.5 把左边那一列换了心脏：顺序与显隐只有一处真相（本机 config 里的 nav_order /
nav_hidden），页面不再抄一份；`ui_server.NAV_ITEMS` 那张表就是入口的总名册。
这一套盯三件事：

  · **页面与名册逐条对得上** —— 名册里每一个 id 在页面上都有一颗图标、都能切过去；
    页面上多出来的图标没有，名册里漏掉的也没有（少一颗就是「某一格凭空消失」）。
  · **接口守得住边界** —— 排序、显隐、侧边栏待法都是 POST 回来的；
    全关掉要挡住（不然主界面没东西可点）；个人简介里的尖括号要洗干净再落盘。
  · **真机上点得动** —— 侧边栏默认摊开、点头像进个人主界面、两张热力图各 371 格、
    长按拖拽能换顺序、靠左缘待法下指针靠边会「弹」出来再自动收回；切页只认点击
    （悬停即切换那条已按用户要求砍掉，指针扫过或停住都不许把整屏换走）。

真机那一段跑完会把改过的顺序与待法还原，不给后面的套件留脏状态。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出，不拿 8770 赌。
"""
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

BASE = selftest.need_base(1)
SHOTS_DIR = selftest.SHOTS
SHOTS_DIR.mkdir(parents=True, exist_ok=True)
SHOTS = [str(SHOTS_DIR / (n + ".png")) for n in ("nav-open", "nav-me")]

# ui_server 是「名册」的唯一真相：在进程里 import 一次，读它那张 NAV_ITEMS 表。
# 必须在 import 之前把沙盒数据目录指过去，免得它读到用户的真实 cache。
selftest.export_sandbox_env()
sys.path.insert(0, str(selftest.REPO))
import ui_server  # noqa: E402

NAV_IDS = [i for i, _l, _g in ui_server.NAV_ITEMS]
SRC = (selftest.REPO / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf8"))


def post(path, payload, timeout=30):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf8"))


def http_status(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def nav_of(st):
    return (st.get("nav") or {})


# ── 一、名册与页面：逐条对得上 ──────────────────────────────────
chk("名册：一共十二个入口", len(NAV_IDS) == 12, NAV_IDS)
chk("名册：顺序里没有重复", len(set(NAV_IDS)) == len(NAV_IDS), NAV_IDS)

block = re.search(r"const NAV_ICONS = \{(.*?)\n\};", SRC, re.S)
icons = re.findall(r"^\s*([A-Za-z0-9_-]+):\s*'", block.group(1), re.M) if block else []
chk("页面上的图标表能读到", bool(icons), len(icons))
chk("名册里每个入口都有一颗图标（缺一颗就是某一格没图）",
    set(NAV_IDS) - set(icons) == set(), sorted(set(NAV_IDS) - set(icons)))
chk("页面上没有名册之外的图标（多一颗就是没人认领的孤儿）",
    set(icons) - set(NAV_IDS) == set(), sorted(set(icons) - set(NAV_IDS)))

chk("名册：分组只有「空间」与「工具」两种",
    set(ui_server.NAV_GROUP[i] for i in NAV_IDS) <= {"space", "tool"})
chk("名册：每个入口都有中文名", all(ui_server.NAV_LABEL[i].strip() for i in NAV_IDS))
chk("侧边栏待法只有摊开与靠左缘两种", tuple(ui_server.SIDEBAR_MODES) == ("open", "edge"),
    ui_server.SIDEBAR_MODES)

# ── 二、接口契约（改完就还原） ──────────────────────────────────
ST = get("/api/state")
NAV = nav_of(ST)
items = NAV.get("items") or []

chk("状态包：导航项十二颗、带 id / 名字 / 分组 / 显隐",
    len(items) == 12 and all(set(("id", "label", "group", "hidden")) <= set(x) for x in items),
    len(items))
chk("状态包：默认顺序就是名册的顺序", [x["id"] for x in items] == NAV_IDS,
    [x["id"] for x in items])
chk("状态包：待法给的是合法档，导航里不再有悬停开关这一项",
    NAV.get("sidebar") in ui_server.SIDEBAR_MODES and "hover" not in NAV,
    (NAV.get("sidebar"), sorted(NAV.keys())))
chk("状态包：重启后落点是第一格可见入口（默认就是书架）", NAV.get("home") == "shelf",
    NAV.get("home"))

PROF = ST.get("profile") or {}
chk("状态包：个人资料带头像标记 / 名字 / 简介 / 长度上限这四样",
    all(k in PROF for k in ("name", "bio", "avatar", "limits")) and isinstance(PROF.get("avatar"), bool),
    {k: PROF.get(k) for k in ("name", "bio", "avatar")})
chk("状态包：名字与简介的上限是 40 / 200", PROF.get("limits") == {"name": 40, "bio": 200},
    PROF.get("limits"))

# 全关掉要挡住：主界面不能没有能点的东西。
ALL_OFF = post("/api/nav", {"hidden": list(NAV_IDS)})
chk("显隐：想全关掉时挡下来，并且说清为什么",
    ALL_OFF.get("ok") is False and isinstance(ALL_OFF.get("msg"), str) and "保留" in ALL_OFF["msg"],
    ALL_OFF.get("msg"))
AFTER_OFF = nav_of(get("/api/state"))
chk("显隐：挡住之后一个入口都没被关掉（没有半途写盘）",
    all(not x["hidden"] for x in (AFTER_OFF.get("items") or [])),
    [(x["id"], x["hidden"]) for x in (AFTER_OFF.get("items") or []) if x["hidden"]])

# 关两个：接口要认，话要能落。
HIDE = post("/api/nav", {"hidden": ["video", "write"]})
HIDDEN = {x["id"] for x in (nav_of(get("/api/state")).get("items") or []) if x["hidden"]}
chk("显隐：关掉「视频」与「写作」这两个，状态包如实反映",
    HIDE.get("ok") and HIDDEN == {"video", "write"}, sorted(HIDDEN))
chk("显隐：关到只剩工具页时，落点顺势挪到第一格可见入口",
    nav_of(get("/api/state")).get("home") == "shelf", nav_of(get("/api/state")).get("home"))
post("/api/nav", {"hidden": []})
RESTORED = {x["id"] for x in (nav_of(get("/api/state")).get("items") or []) if x["hidden"]}
chk("显隐：还原之后又都露出来了", RESTORED == set(), sorted(RESTORED))

# 排序：反着来一遍，接口要照做。
REV = list(reversed(NAV_IDS))
post("/api/nav", {"order": REV})
ORDERED = [x["id"] for x in (nav_of(get("/api/state")).get("items") or [])]
chk("排序：反着存一遍，回来的顺序就是反的", ORDERED == REV, ORDERED)
post("/api/nav", {"order": NAV_IDS})
chk("排序：还原成名册的顺序",
    [x["id"] for x in (nav_of(get("/api/state")).get("items") or [])] == NAV_IDS)

# 待法与悬停开关。
post("/api/nav", {"sidebar": "edge"})
chk("待法：切到「靠左缘滑出」后状态包认这个档",
    nav_of(get("/api/state")).get("sidebar") == "edge")
post("/api/nav", {"sidebar": "open"})
chk("待法：切回「常驻」", nav_of(get("/api/state")).get("sidebar") == "open")
# 悬停切换已下架：再把 hover 递上去，接口不该记它、状态包里也不许再冒出来。
post("/api/nav", {"hover": False})
chk("悬停开关已下架：递上去也不认，状态包里不会有 hover",
    "hover" not in nav_of(get("/api/state")))

# 头像：沙盒里没有，就该是 404 而不是一张破图。
chk("头像：没上传时接口回 404（不是一个空壳 200）", http_status("/api/avatar") == 404)

# 个人资料：尖括号要在落盘前洗掉（简介会被拼进 HTML）。
post("/api/profile", {"name": "<b>甲</b>", "bio": "x<script>"})
P2 = get("/api/state").get("profile") or {}
chk("个人资料：名字里的尖括号被换成书名号，原文进不了 HTML",
    "<" not in (P2.get("name") or "") and "〈" in (P2.get("name") or ""), P2.get("name"))
post("/api/profile", {"name": PROF.get("name") or "", "bio": PROF.get("bio") or ""})
chk("个人资料：还原成进来时的样子",
    (get("/api/state").get("profile") or {}).get("name") == (PROF.get("name") or ""))

# 时长账：心跳回的是「真正计入的秒数」。
T1 = post("/api/activity/tick", {"kind": "study", "seconds": 120})
chk("时长心跳：记 120 秒就回 120", T1.get("ok") and T1.get("added") == 120, T1)
T2 = post("/api/activity/tick", {"kind": "study", "seconds": 9999})
chk("时长心跳：一次报 9999 秒也只计 300（单次夹）",
    T2.get("added") == 300, T2)
T3 = post("/api/activity/tick", {"kind": "nope", "seconds": 60})
chk("时长心跳：不认识的那本账不记", T3.get("ok") is False and T3.get("added") == 0, T3)

ACT = get("/api/activity")
chk("热力图整包：两本账都在，各带日历格子与两道夹的参数",
    {"study", "write"} <= set(ACT) and ACT.get("cap") == {"tick": 300, "day": 43200},
    ACT.get("cap"))
chk("热力图整包：学习那一本铺满一年（53 周 × 7 = 371 格）",
    len((ACT.get("study") or {}).get("cells") or []) == 371,
    len((ACT.get("study") or {}).get("cells") or []))
chk("热力图整包：写作那一本同样 371 格",
    len((ACT.get("write") or {}).get("cells") or []) == 371,
    len((ACT.get("write") or {}).get("cells") or []))

# ── 三、真机：这一列点得动 ──────────────────────────────────────
from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(1500)

    dom = page.eval_on_selector_all("#nav button[data-v]", "els => els.map(e => e.dataset.v)")
    chk("真机：侧边栏铺出来的就是名册那十二颗，顺序一致", dom == NAV_IDS, dom)

    body_cls = page.eval_on_selector("body", "b => b.className")
    chk("真机：默认是「摊开」待法（body.side-open）",
        "side-open" in body_cls and "side-edge" not in body_cls, body_cls)

    # 拖拽排序：按住第一颗满 0.4 秒再挪到第三颗下半，松手该换位。
    sb = page.locator('#nav button[data-v="shelf"]').bounding_box()
    cb = page.locator('#nav button[data-v="clip"]').bounding_box()
    if sb and cb:
        page.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2)
        page.mouse.down()
        page.wait_for_timeout(520)                      # 过 400ms 的拖拽门槛
        page.mouse.move(cb["x"] + cb["width"] / 2, cb["y"] + cb["height"], steps=12)
        page.wait_for_timeout(140)
        page.mouse.up()
        page.wait_for_timeout(800)
        dragged = page.eval_on_selector_all("#nav button[data-v]", "els => els.map(e => e.dataset.v)")
        chk("真机：长按拖拽真的换了位（书架被拖到了别处）",
            dragged != NAV_IDS and dragged.index("shelf") > 0, dragged)
        chk("真机：拖拽没有弄丢入口（还是那十二个）", sorted(dragged) == sorted(NAV_IDS), dragged)
        post("/api/nav", {"order": NAV_IDS})            # 还原
        page.wait_for_timeout(3300)                     # 等一轮状态轮询把新顺序铺回来
        back = page.eval_on_selector_all("#nav button[data-v]", "els => els.map(e => e.dataset.v)")
        chk("真机：还原之后顺序回到名册的样子（下一轮轮询铺回来）", back == NAV_IDS, back)
    else:
        chk("真机：能拿到两颗粒子的位置（拖拽可测）", False, (sb, cb))

    # 切页只认「点」：悬停即切换那条已按用户要求砍掉。指针停在一格上再多也不许跳。
    page.hover('#nav button[data-v="notes"]')
    page.wait_for_timeout(500)
    chk("真机：指针停在一格上不再切页（悬停即切换已砍掉）",
        not page.eval_on_selector('#nav button[data-v="notes"]', "b => b.classList.contains('on')"),
        page.eval_on_selector('#nav button.on', "b => b.dataset.v"))

    # 浏览器补的 pointerover 同样不算「真点」。改窗口大小、窄屏断点重排会让另一格滑到
    # 原地不动的指针底下，浏览器这时会补一个 pointerover —— 那不是用户把指针挪过去。
    # 无论悬停切不切换，这条都得纹丝不动：直接合成那个事件来钉。
    page.evaluate("""() => {
      const b = document.querySelector('#nav button[data-v="pick"]');
      const r = b.getBoundingClientRect();
      b.dispatchEvent(new PointerEvent('pointerover', {bubbles: true, cancelable: true,
        clientX: r.x + r.width / 2, clientY: r.y + r.height / 2}));
    }""")
    page.wait_for_timeout(400)
    chk("真机：只补一个 pointerover（指针没真的动）不许切页",
        not page.eval_on_selector('#nav button[data-v="pick"]', "b => b.classList.contains('on')"),
        page.eval_on_selector('#nav button[data-v="pick"]', "b => b.className"))

    # 真点一下才是切页：点「笔记」这一格，页面要过去。
    page.click('#nav button[data-v="notes"]')
    page.wait_for_timeout(700)
    chk("真机：点一下目标才切页（点「笔记」就切过去了）",
        page.eval_on_selector('#nav button[data-v="notes"]', "b => b.classList.contains('on')"))
    page.click('#nav button[data-v="shelf"]')       # 还原到书架，别给后面的步骤留脏
    page.wait_for_timeout(700)

    # 点头像进个人主界面。
    page.click("#mebtn")
    page.wait_for_timeout(900)
    me = page.locator('#view > .vpane[data-pane="me"]')
    chk("真机：左上角头像点开就是个人主界面", me.count() == 1 and me.is_visible())
    chk("真机：个人主界面有两张热力图 + 头像 / 名字 / 简介",
        page.locator("#heat-study").count() == 1 and page.locator("#heat-write").count() == 1
        and page.locator("#mePick").count() == 1 and page.locator("#meNameIn").count() == 1
        and page.locator("#meBioIn").count() == 1)
    chk("真机：学习那张热力图铺满 371 格",
        page.locator("#heat-study .heatbody .heatcell").count() == 371,
        page.locator("#heat-study .heatbody .heatcell").count())
    chk("真机：写作那张也铺满 371 格",
        page.locator("#heat-write .heatbody .heatcell").count() == 371,
        page.locator("#heat-write .heatbody .heatcell").count())
    chk("真机：每张热力图上方的读数有六格（今天 / 本周 / 日均 / 连续 / 有记录天 / 累计）",
        page.locator("#heat-study .heatstat .s").count() == 6)
    chk("真机：原「设置」入口挪到了个人主界面最下面那一颗",
        page.locator("#meSet").count() == 1 and page.locator("#meSet").is_visible())
    page.screenshot(path=SHOTS[1])

    # 设置里那一段导航控件。
    page.evaluate("setPop(true, 'nav')")
    page.wait_for_timeout(400)
    chk("真机：设置里入口显隐的勾选框正好十二个",
        page.locator("#navList input[data-nav]").count() == 12,
        page.locator("#navList input[data-nav]").count())
    chk("真机：侧边栏待法两颗（常驻 / 靠左缘滑出）都在",
        page.locator('#navSideSeg button[data-s="open"]').count() == 1
        and page.locator('#navSideSeg button[data-s="edge"]').count() == 1)
    chk("真机：设置里不再有悬停切换那颗开关（那条交互已下架）",
        page.locator("#navHover").count() == 0,
        page.locator("#navHover").count())

    # 靠左缘待法：指针靠到窗口左缘要「弹」出来，移回内容区再自动收回。
    # 设置弹窗那张遮罩是铺满全屏的（z-index 54），压着左缘感应带 —— 要测就得先把它关掉，
    # 不然指针进不去那条 16px 的带子。
    page.click('#navSideSeg button[data-s="edge"]')
    page.wait_for_timeout(500)
    chk("真机：切到「靠左缘滑出」后，侧边栏让出了左列",
        "side-edge" in page.eval_on_selector("body", "b => b.className"))
    page.evaluate("setPop(false)")
    page.wait_for_timeout(400)
    page.mouse.move(6, 420)                    # 靠到左缘 16px 那条感应带里
    page.wait_for_timeout(350)
    chk("真机：指针靠到左缘，侧边栏弹出来了（body.side-peek）",
        "side-peek" in page.eval_on_selector("body", "b => b.className"))
    page.mouse.move(760, 420)                  # 移回内容区
    page.wait_for_timeout(600)
    chk("真机：指针移回内容区，侧边栏自己收回去了",
        "side-peek" not in page.eval_on_selector("body", "b => b.className"))
    page.evaluate("setPop(true, 'nav')")       # 还原：切回常驻
    page.wait_for_timeout(400)
    page.click('#navSideSeg button[data-s="open"]')
    page.wait_for_timeout(400)
    page.evaluate("setPop(false)")
    page.wait_for_timeout(300)
    chk("真机：切回「常驻」后左列回来了（还原干净）",
        "side-open" in page.eval_on_selector("body", "b => b.className"))

    page.screenshot(path=SHOTS[0])
    chk("真机：全程没有报错", not errors, errors[:6])
    browser.close()

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
print("截图：" + " ".join(SHOTS))
sys.exit(1 if bad else 0)
