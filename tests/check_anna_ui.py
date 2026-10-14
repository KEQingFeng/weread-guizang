# -*- coding: utf-8 -*-
"""「安娜的档案」这一栏 —— 搜索口令契约 / 砍到只剩搜索 / 下载接进本地书架 —— 真机主验收集。

为什么要真机：这一栏的风险一半长在「看得见」的那一侧 —— 界面这一屏 1.2.1 起只剩搜索框与
搜索键，点一下、按一下回车都得真把搜索口令发出去；另一半长在「原件真能转成书」上 ——
原始文件真的转成一本书、真的出现在本地书架里，这一步只有点一遍才知道成不成。

窗口本身不在这里开：门禁里弹一个真浏览器窗口会占住任务槽、还会在用户屏幕上乱开页面，
所以测试用的那份服务开着 GUIZANG_ANNA_NO_WINDOW=1（search 只写口令不起窗口），前端那半条
用请求截获验「发出去的确实是 mode=search」（真开窗归 tests/ 之外的人工实测，见 docs/交接说明.md）。

验的东西（每条都对应界面上真能点、或后端真会答的一件事）：
  · /api/state 带着这一栏的账，字段齐、窗口缺省是没开；名册十三格、最后一格是它；
  · search 写关键词、空关键词挡下来（门禁关掉真开窗，回执要说清「口令记下了」）；
  · domain 把 https/路径洗成干净的 host（接口还在，界面上的入口砍了）；
  · 往下载夹放一份 Markdown + 一份 .mobi → collect → Markdown 真进本地书架
    （磁盘上有那本书、/api/state.books 里 module=local），mobi 写明「格式不认」、原文件留着；
  · 界面这一屏只剩搜索框与搜索键：点搜索 / 按回车都发 mode=search，窗口状态 / 镜像域名 /
    补收 / 清空 / 三份记录整块不在；
  · 四档视口无横向溢出、这一屏零 emoji、零 console 报错；
  · 搜书页回到「只有微信读书一栏」，整屏不再出现 Z-Library。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
地址走命令行第一参数或 GUIZANG_TEST_URL；没给就退出。收尾把这一路造的账、
下的样本文件与入库的那本书一并抹掉，下一个套件不会看见残留。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import selftest          # noqa: E402
import anna_state        # noqa: E402
import book_layout       # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
URL = BASE + "/"
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
SHOT = str(selftest.SHOTS / "anna-pane.png")
FAIL = []
PASSED = [0]

# 被测服务读写的是哪两格：跟它自己那一份 env 对齐（run_all.sh 导好了）。
DATA = os.environ.get("GUIZANG_DATA") or str(selftest.CACHE)
BOOKS = os.environ.get("GUIZANG_BOOKS") or str(selftest.BOOKS)
SANDBOX = os.path.realpath(str(selftest.SANDBOX))
for label, path in (("数据目录", DATA), ("书库", BOOKS)):
    real = os.path.realpath(path)
    if not (real == SANDBOX or real.startswith(SANDBOX + os.sep)):
        sys.exit("这一套要往磁盘写样本文件、往书架转书，只准落在沙盒里：现在这一格的"
                 "%s 不在 %s 之下。要挂到别的实例上跑，先 export GUIZANG_DATA 与 "
                 "GUIZANG_BOOKS（bash tests/run_all.sh 会一并导好）。" % (label, SANDBOX))

INCOMING = anna_state.incoming_dir(DATA, create=True)
SHELF = book_layout.book_dir(BOOKS, "local")

MD_NAME = "安娜示范书.md"
MOBI_NAME = "扫件样本.mobi"
MD_TEXT = ("# 第一章 起首\n\n这一章是现编的，用来验「窗口里点下载 → 归藏接住 → 转成书」"
           "这条链路在界面上点得动。正文写得够长，好让它分得出章。\n\n"
           "# 第二章 收束\n\n第二段也是编的。两章加起来够入库器切出章节，"
           "于是书架上那本卡片的章数不是零。\n")

# 现场快照：域名与关键词是用户能改的两项，跑完按原样贴回去。
ORIG = anna_state.read_state(DATA)
MADE = {"md": os.path.join(INCOMING, MD_NAME),
        "mobi": os.path.join(INCOMING, MOBI_NAME),
        "book": ""}


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)


def api(path, body=None):
    """直连后端。不用 curl —— 本机没有按名字找得到的它，也不该用。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def anna(mode, **body):
    return api("/api/anna", dict({"mode": mode}, **body))


def state_anna():
    return api("/api/state")["anna"]


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def restore():
    """抹掉这一套造的东西：样本文件、入库那本书、这一栏的账。"""
    for path in (MADE["md"], MADE["mobi"]):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    if MADE["book"]:
        shutil.rmtree(os.path.join(SHELF, MADE["book"]), ignore_errors=True)
    try:
        anna_state.write_state(DATA, {"domain": ORIG["domain"], "keyword": "",
                                      "caught": 0, "imported": 0, "skipped": 0,
                                      "seen": [], "books": [], "pending": [],
                                      "note": "", "last_error": ""})
    except Exception:
        pass


atexit.register(restore)

# ── 一、状态契约：名册与那一格账本 ──────────────────────────────
st = api("/api/state")
a = st.get("anna")
chk("state：带着「安娜的档案」这一栏的账", isinstance(a, dict), type(a).__name__)
FIELDS = {"domain", "window", "keyword", "caught", "imported", "skipped",
          "seen", "books", "pending", "note", "last_error", "updated_at"}
chk("state：账本字段齐（缺一个界面就要防字段缺失）", FIELDS <= set(a or {}),
    sorted(FIELDS - set(a or {})))
chk("state：沙盒里没开过窗口，账本说的是没开", (a or {}).get("window") == "closed",
    (a or {}).get("window"))
chk("state：三个计数是整数而不是字符串",
    all(isinstance((a or {}).get(k), int) for k in ("caught", "imported", "skipped")),
    {k: (a or {}).get(k) for k in ("caught", "imported", "skipped")})
raw = json.dumps(a or {}, ensure_ascii=False)
chk("state：账本里只有名字与数字，没有绝对路径",
    "/Users/" not in raw and "/var/" not in raw and "/private/" not in raw, raw[:200])

items = (st.get("nav") or {}).get("items") or []
ids = [x["id"] for x in items]
chk("名册：一共十三格（Z-Library 那格砍了，新增「安娜的档案」）", len(items) == 13, ids)
chk("名册：这一栏排在最末（用户要的就是「最底部新增一栏」）", ids[-1] == "anna", ids[-3:])
chk("名册：名字叫「安娜的档案」、归在工具组",
    items[-1]["label"] == "安娜的档案" and items[-1]["group"] == "tool", items[-1])
chk("名册：Z-Library 那一格彻底没了",
    not [x for x in items if "zlib" in x["id"] or "Library" in x["label"]], ids)

# ── 二、口令与域名：两处「用户填一下」的口子 ────────────────────
empty = anna("search", q="   ")
chk("关键词空着时挡下来，并说清要写什么",
    empty.get("ok") is False and "关键词" in (empty.get("msg") or ""), empty)

term = "示范关键词"
got = anna("search", q=term)
chk("写关键词：账本记下这一条", (got.get("anna") or {}).get("keyword") == term,
    (got.get("anna") or {}).get("keyword"))
chk("写关键词：门禁里关掉了真开窗，回执说清「口令记下了」而不是谎报窗口已经在搜",
    got.get("ok") is False and "口令记下了" in (got.get("msg") or ""), got.get("msg"))
chk("写关键词：界面那格 placeholder 跟着改口（state 里能看见）",
    state_anna().get("keyword") == term, state_anna().get("keyword"))

dom = anna("domain", domain="  https://Annas-Archive.gd/search?term=x  ")
saved = (dom.get("anna") or {}).get("domain")
chk("存镜像域名：协议、路径、大小写与空格都洗掉", saved == "annas-archive.gd", saved)
chk("存镜像域名：回执说已保存", dom.get("ok") is True and "已保存" in (dom.get("msg") or ""), dom)
unknown = anna("nope")
chk("不认识的动作要说「不认识这个动作」，不是静默成功",
    unknown.get("ok") is False and "不认识" in (unknown.get("msg") or ""), unknown)

# ── 三、补收：下载夹里那份 Markdown 真转成本地书架的书 ──────────
write(MADE["md"], MD_TEXT)
write(MADE["mobi"], "这份是假的 mobi 头，压根不是书：这一路只收 EPUB/PDF/TXT/Markdown。")
listed = anna("listing")
names = {f["name"] for f in (listed.get("files") or [])}
chk("下载夹那一格列得出这两份样本", {MD_NAME, MOBI_NAME} <= names, sorted(names))
rows = {f["name"]: f for f in (listed.get("files") or [])}
chk("列出来的每一行带「收得下 / 格式不认」这个标志（界面据此给徽章）",
    rows.get(MD_NAME, {}).get("ok") is True and rows.get(MOBI_NAME, {}).get("ok") is False,
    {k: v.get("ok") for k, v in rows.items()})
chk("listing 只给名字，不给路径（界面拿不到磁盘结构）",
    all("/" not in f["name"] for f in (listed.get("files") or [])),
    [f["name"] for f in (listed.get("files") or [])][:3])

before_books = [b["id"] for b in api("/api/state")["books"]]
col = anna("collect")
done = col.get("done") or []
skipped = col.get("skipped") or []
chk("补收：Markdown 那份收进去了（回执点名道姓）", MD_NAME in done, col)
chk("补收：mobi 那份挡在门外，回执里写了「没收进来」",
    not any(MOBI_NAME in x for x in done) and "没收进来" in (col.get("msg") or ""), col.get("msg"))

a = col.get("anna") or {}
book_rows = a.get("books") or []
chk("补收：账本里那本书带书名与书号",
    len(book_rows) >= 1 and book_rows[0].get("book") and book_rows[0].get("title"),
    book_rows[:1])
book_id = book_rows[0]["book"] if book_rows else ""
MADE["book"] = book_id
chk("补收：书号是 anna_ 前缀（本地书架里能分辨出处）", book_id.startswith("anna_"), book_id)
chk("补收：计数跟上（进书架 1 本、没收进来 1 份）",
    a.get("imported") == 1 and a.get("skipped") == 1,
    {k: a.get(k) for k in ("caught", "imported", "skipped")})
chk("补收：一句话回执说的是「进没进书架」这件事（两份样本一收一挡，收尾那句总得落在书架上）",
    "书架" in (a.get("note") or ""), a.get("note"))
pend = [p for p in (a.get("pending") or []) if p.get("name") == MOBI_NAME]
chk("补收：没收进来的那份留进「没收进来的」，原因写明格式不认",
    len(pend) == 1 and "格式不认" in (pend[0].get("reason") or ""), pend[:1])
chk("补收：原文件一个都不删（mobi 还在下载夹里）",
    os.path.isfile(MADE["mobi"]) and os.path.isfile(MADE["md"]), MOBI_NAME)

book_dir = os.path.join(SHELF, book_id) if book_id else ""
chk("磁盘：本地书架那一格里真的多了一个书文件夹", bool(book_id) and os.path.isdir(book_dir),
    book_id)
chapters = os.path.join(book_dir, "chapters")
chk("磁盘：那本书不是空壳（meta.json 与章节目录里的正文都在）",
    os.path.isfile(os.path.join(book_dir, "meta.json"))
    and os.path.isdir(chapters) and bool(os.listdir(chapters)) if book_dir else False,
    sorted(os.listdir(book_dir)) if os.path.isdir(book_dir) else book_id)

st2 = api("/api/state")
new = [b for b in st2["books"] if b["id"] not in before_books]
chk("state：这本书出现在书目里，且归在「本地书架」那一格",
    len(new) == 1 and new[0].get("module") == "local", new[:1])

again = anna("collect")
chk("补收：同一份再点一次不重复入库（按字节摘要认出收过）",
    MD_NAME in (again.get("duplicated") or [])
    and (again.get("anna") or {}).get("imported") == 1,
    {k: again.get(k) for k in ("done", "duplicated", "skipped")})

# ── 四、真机：这一屏只剩搜索，点它 / 回车都直接去搜 ──────────
PANE = '#view > .vpane[data-pane="anna"]'
SPANE = '#view > .vpane[data-pane="search"]'
OVERFLOW_JS = "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2190-\u27BF\u2B00-\u2BFF\uFE0F]")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 880})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1100)

    nav_ids = page.eval_on_selector_all('#nav button[data-v]', "els => els.map(e => e.dataset.v)")
    chk("真机：导航画了十三格，最后一格是「安娜的档案」",
        len(nav_ids) == 13 and nav_ids[-1] == "anna", nav_ids)

    # ── 四、真机：这一屏只剩搜索，点它 / 回车都直接去搜 ──────────
    page.click('#nav button[data-v="anna"]')
    page.wait_for_selector(PANE + " #anQ", timeout=6000)
    page.wait_for_timeout(300)

    def pane_text(sel):
        loc = page.locator(PANE + " " + sel)
        return loc.inner_text().strip() if loc.count() else ""

    chk("真机：搜索框与搜索键各一颗",
        page.locator(PANE + " #anQ").count() == 1
        and page.locator(PANE + " #anGo").count() == 1)
    gone = ["anWinDot", "anWinText", "anTip", "anOpen", "anStop", "anLog", "anCollect",
            "anClear", "anCnt", "anDom", "anDomSave", "anBooks", "anFiles", "anPending"]
    still = [i for i in gone if page.locator(PANE + " #" + i).count()]
    chk("真机：窗口状态 / 镜像域名 / 补收 / 清空 / 三份记录整块砍干净", not still, still)
    hero = pane_text(".vbody")
    chk("真机：页头说清「写书名 → 搜索或回车，窗口自动跳过去」",
        "回车" in hero and ("自动" in hero or "跳" in hero), hero[:200])

    # 门禁不弹真窗口：把 /api/anna 截下来，只验前端发出去的是不是这一条口令。
    sent = []

    def grab(route):
        try:
            sent.append(json.loads(route.request.post_data or "{}"))
        except Exception:
            sent.append({})
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "msg": "正在打开窗口去搜",
                                       "anna": {"keyword": "真机关键词"}}))

    page.route("**/api/anna", grab)
    page.fill(PANE + " #anQ", "真机关键词")
    page.click(PANE + " #anGo")
    page.wait_for_timeout(400)
    chk("真机点「搜索」：带上 mode=search 与书名去后端（后端据此把窗口领起来）",
        sent and sent[-1].get("mode") == "search" and sent[-1].get("q") == "真机关键词",
        sent[-1:])

    page.fill(PANE + " #anQ", "回车关键词")
    page.press(PANE + " #anQ", "Enter")
    page.wait_for_timeout(400)
    chk("真机在输入框按回车：与点搜索同一路（用户要的「回车直接跳」）",
        len(sent) >= 2 and sent[-1].get("q") == "回车关键词", sent[-2:])
    page.unroute("**/api/anna")

    page.screenshot(path=SHOT)

    # ── 八、视口与文案 ──────────────────────────────────────────
    for w in (1280, 940, 768, 375):
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(320)
        o = page.evaluate(OVERFLOW_JS)
        chk("真机（视口 %d）：这一栏没有横向溢出" % w, o["sw"] <= o["iw"] + 1,
            "scrollWidth %s > innerWidth %s" % (o["sw"], o["iw"]))

    page.set_viewport_size({"width": 1280, "height": 880})
    page.wait_for_timeout(300)
    pane_all = page.locator(PANE).inner_text()
    chk("真机：这一栏零 emoji", not EMOJI.search(pane_all), EMOJI.findall(pane_all)[:6])

    # ── 九、搜书页回到「只有微信读书一栏」 ──────────────────────
    page.click('#nav button[data-v="search"]')
    page.wait_for_selector(SPANE + ' .vbody', timeout=6000)
    page.wait_for_timeout(400)
    hint = page.locator(SPANE + ' .vbody').inner_text()
    chk("真机：搜书开屏先给提示，并把「要下载去哪儿」指到安娜的档案",
        "微信读书" in hint and "安娜的档案" in hint, hint[:200])
    chk("真机：没搜之前不摆空栏", page.locator(SPANE + ' .zcol').count() == 0,
        page.locator(SPANE + ' .zcol').count())

    page.fill(SPANE + ' #wrq', "示范关键词")
    page.click(SPANE + ' #wrgo')
    try:
        page.wait_for_selector(SPANE + ' .zcol', timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(600)
    cols = page.locator(SPANE + ' .zcol')
    chk("真机：搜一次只出一栏（Z-Library 那一栏随模块一起没了）", cols.count() == 1,
        cols.count())
    chk("真机：留下的那一栏是「微信读书」",
        page.locator(SPANE + ' .zcol .zhead .b').inner_text().strip() == "微信读书",
        page.locator(SPANE + ' .zcol .zhead .b').inner_text())
    chk("真机：那一栏的小字说的是「加入书架再整本取回」，不再提下载",
        "取回" in (page.locator(SPANE + ' .zcol .zhead .s').inner_text() or ""),
        page.locator(SPANE + ' .zcol .zhead .s').inner_text())
    sp_txt = page.locator(SPANE).inner_text()
    chk("真机：搜书页整屏不再出现 Z-Library 字样", "Z-Library" not in sp_txt, sp_txt[:200])

    # 没配 Key 时不许干等：这一屏要指路（Z-Library 没了以后更要说清去哪儿下载）。
    body = page.locator(SPANE + ' .zbody').inner_text()
    chk("真机：搜一次没 Key 时说清先去「设置」配 Key，不是空转",
        "Key" in body and "设置" in body, body[:200])

    chk("真机：全程零 console 报错", not errors, errors[:4])
    browser.close()

print()
print("通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(len(FAIL))
