# -*- coding: utf-8 -*-
"""「安娜的档案」这一栏 —— 状态契约 / 四颗按钮 / 三份记录 / 下载接进本地书架 —— 真机主验收集。

为什么要真机：这一栏的全部风险都长在「看得见」的那一侧。窗口那头是个独立进程，界面
只能靠一份小账本猜它的状态；账本说「开着」而进程早被杀了，就得显示「上一次说还开着，
其实已经不在了」，不能继续骗人说窗口在。四颗按钮各写各的字段（口令、域名、补收、清账），
少接一根线就是「按下去没反应」；而「补收下载夹」这条是这一路的价值所在 —— 原始文件
真的转成一本书、真的出现在本地书架里，这一步只有点一遍才知道成不成。

窗口本身不在这里开：门禁里弹一个真浏览器窗口会占住任务槽、还会在用户屏幕上乱开页面，
所以「窗口开着时长什么样」是把页面里那套状态函数拿来摆明面的状态演一遍（真开窗归
tests/ 之外的人工实测，见 docs/交接说明.md）。

验的东西（每条都对应界面上真能点、或后端真会答的一件事）：
  · /api/state 带着这一栏的账，字段齐、窗口缺省是没开；名册十三格、最后一格是它；
  · 「让窗口去搜」写关键词（窗口没开也写，回执要说清「口令记下了」）、空关键词挡下来；
  · 「存这个镜像域名」把 https/路径洗成干净的 host，输入框随后清空；
  · 往下载夹放一份 Markdown + 一份 .mobi → 点「补收下载夹」→ Markdown 真进本地书架
    （磁盘上有那本书、/api/state.books 里 module=local、界面上列进「最近收进本地书架」），
    mobi 进「没收进来的」并写明「格式不认」、原文件留着；同一份再补收不重复入库；
  · 「清空记录」只清账：书架上那本书与下载夹里的原文件一个都不动；
  · 窗口没开时「关掉窗口 / 查看详情」收起、状态点带 off；账本说谎时改口成「已经不在了」；
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
chk("写关键词：窗口没开时不谎报「已经让窗口去搜」，而是说口令记下了",
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

# ── 四、真机：这一屏点得动、三份列表摆得出、窗口状态不说谎 ──────
PANE = '#view > .vpane[data-pane="anna"]'
SPANE = '#view > .vpane[data-pane="search"]'
OVERFLOW_JS = "() => ({sw: document.documentElement.scrollWidth, iw: window.innerWidth})"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2190-\u27BF\u2B00-\u2BFF\uFE0F]")


def wait_text(page, sel, needle, ms=30000):
    """等这一栏里那一格的文字变成应有的样子。

    点「补收 / 清空」发出去的是异步 POST，界面要等回执才重画；立刻读常常读到上一轮
    的那一份 —— 于是「后端慢」会被报成「按钮没生效」。等不到不算失败，
    紧随其后的那条断言会给出真相。
    """
    try:
        page.wait_for_function(
            "(a) => { const el = document.querySelector(a[0]);"
            "  return !!el && el.textContent.indexOf(a[1]) >= 0; }",
            arg=[sel, needle], timeout=ms)
    except Exception:
        pass
    page.wait_for_timeout(150)


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

    page.click('#nav button[data-v="anna"]')
    page.wait_for_selector(PANE + ' #anWinDot', timeout=6000)
    page.wait_for_timeout(400)

    ids = ["anWinDot", "anWinText", "anTip", "anOpen", "anStop", "anLog", "anCollect",
           "anClear", "anCnt", "anQ", "anGo", "anDom", "anDomSave", "anBooks", "anFiles",
           "anPending"]
    missing = [i for i in ids if page.locator(PANE + " #" + i).count() != 1]
    chk("真机：这一屏该有的控件一个不缺（缺一颗就是按下去没反应）", not missing, missing)

    def pane_text(sel):
        loc = page.locator(PANE + " " + sel)
        return loc.inner_text().strip() if loc.count() else ""

    def pane_lines(sel):
        """同一种小标题有好几份时用它：Playwright 的严格模式不许一个选择器指多个元素。"""
        return [t.strip() for t in page.locator(PANE + " " + sel).all_inner_texts()]

    chk("真机：窗口没开时状态写「窗口没开」", pane_text("#anWinText") == "窗口没开",
        pane_text("#anWinText"))
    chk("真机：窗口没开时状态点是灭的（off）",
        "off" in (page.get_attribute(PANE + " #anWinDot", "class") or ""),
        page.get_attribute(PANE + " #anWinDot", "class"))
    chk("真机：窗口没开时「关掉窗口 / 查看详情」都收起",
        page.locator(PANE + " #anStop").is_hidden()
        and page.locator(PANE + " #anLog").is_hidden())
    chk("真机：窗口没开时那句话说的是这一路怎么用",
        "浏览器窗口" in pane_text("#anTip"), pane_text("#anTip")[:120])
    # 2026-10-07 在有头窗口里实测：详情页每一条下载链接都指向 /account，写着
    # 「Log in to access downloads」。这句不写在明处，用户只会觉得「点了没反应」，
    # 所以把它钉成断言 —— 谁把提示改没了，这里就红。
    chk("真机：那句话提醒「要先登录才给下载」",
        "登录" in pane_text("#anTip"), pane_text("#anTip")[:160])
    chk("真机：三块记录各有小标题，讲清各是什么",
        all(s in pane_text(".vbody") for s in ("最近收进本地书架", "下载夹里", "没收进来的")),
        page.locator(PANE + ' .ansec').all_inner_texts())
    chk("真机：计数条摆出「接住几份 · 进书架几本 · 没收进来几份」",
        "接住" in pane_text("#anCnt") and "进书架" in pane_text("#anCnt")
        and "没收进来" in pane_text("#anCnt"), pane_text("#anCnt"))
    chk("真机：接口那两份样本列在「下载夹里」那一格",
        MD_NAME in pane_text("#anFiles") and MOBI_NAME in pane_text("#anFiles"),
        pane_text("#anFiles")[:200])
    chk("真机：入库那本列在「最近收进本地书架」，并给了「看这本」",
        book_rows[0]["title"] in pane_text("#anBooks")
        and page.locator(PANE + ' #anBooks button:has-text("看这本")').count() >= 1,
        pane_text("#anBooks")[:200])
    chk("真机：mobi 那行显示「原文件留着」并把原因写在下面",
        MOBI_NAME in pane_text("#anPending") and "格式不认" in pane_text("#anPending"),
        pane_text("#anPending")[:200])

    # ── 五、四颗按钮各写各的字段（真点，不只看代码） ────────────
    page.fill(PANE + " #anQ", "真机关键词")
    page.click(PANE + " #anGo")
    page.wait_for_timeout(500)
    chk("真机点「让窗口去搜」：关键词落进账本",
        state_anna().get("keyword") == "真机关键词", state_anna().get("keyword"))
    chk("真机点「让窗口去搜」：搜索框占位改口成上一次搜的那条",
        "真机关键词" in (page.get_attribute(PANE + " #anQ", "placeholder") or ""),
        page.get_attribute(PANE + " #anQ", "placeholder"))

    page.fill(PANE + " #anDom", "annas-archive.se")
    page.click(PANE + " #anDomSave")
    page.wait_for_timeout(500)
    chk("真机点「存这个镜像域名」：域名写进账本",
        state_anna().get("domain") == "annas-archive.se", state_anna().get("domain"))
    chk("真机点「存这个镜像域名」：输入框当场清空（免得下次误以为还没存）",
        (page.input_value(PANE + " #anDom") or "") == "", page.input_value(PANE + " #anDom"))

    # 「窗口开着」那一层的界面：账本 + 任务槽一起说了算，所以拿页面的状态函数演一遍。
    page.evaluate("() => { running = true; runningKind = 'anna_browser';"
                  "paintAnna(Object.assign({}, annaState, {window: 'open'})); }")
    page.wait_for_timeout(150)
    chk("真机（窗口开着）：状态写「窗口开着」，那颗点变亮",
        "窗口开着" in pane_text("#anWinText")
        and "off" not in (page.get_attribute(PANE + " #anWinDot", "class") or ""),
        pane_text("#anWinText"))
    chk("真机（窗口开着）：「关掉窗口 / 查看详情」摆出来，「打开浏览器窗口」按住",
        page.locator(PANE + " #anStop").is_visible()
        and page.locator(PANE + " #anLog").is_visible()
        and page.locator(PANE + " #anOpen").is_disabled())
    chk("真机（窗口开着）：明说这期间发起的取书会排队（任务槽只有一个位置）",
        "排队" in pane_text("#anTip"), pane_text("#anTip")[:140])

    # 账本说谎（进程被强杀、来不及写 closed）：真源是任务槽，界面得改口。
    page.evaluate("() => { running = false; runningKind = '';"
                  "paintAnna(Object.assign({}, annaState, {window: 'open'})); }")
    page.wait_for_timeout(150)
    chk("真机（账本说开着、进程其实没了）：改口成「已经不在了」而不是继续谎报",
        "已经不在了" in pane_text("#anWinText"), pane_text("#anWinText"))
    page.evaluate("() => paintAnna(Object.assign({}, annaState,"
                  "{window: 'failed', last_error: '没能起浏览器'}))")
    page.wait_for_timeout(150)
    chk("真机（窗口没起来）：错误原因显示在明处、按钮改口「重新打开窗口」、状态点还是灭的",
        "没能起浏览器" in pane_text("#anTip") and "重新打开" in pane_text("#anOpen")
        and "off" in (page.get_attribute(PANE + " #anWinDot", "class") or ""),
        (pane_text("#anTip"), pane_text("#anOpen")))

    # ── 六、界面上补收一次（放一份新的进下载夹） ────────────────
    os.remove(MADE["md"])
    write(MADE["md"], MD_TEXT.replace("第一章 起首", "第一章 界面补收"))
    page.click(PANE + " #anCollect")
    wait_text(page, PANE + " #anCnt", "进书架 2 本")
    chk("真机点「补收下载夹」：那本书列进「最近收进本地书架」两本",
        page.locator(PANE + ' #anBooks .zrow').count() == 2,
        pane_text("#anBooks")[:200])
    chk("真机点「补收下载夹」：计数跟着涨（进书架 2 本）",
        "进书架 2 本" in pane_text("#anCnt"), pane_text("#anCnt"))

    page.click('#nav button[data-v="local"]')
    page.wait_for_selector('#view > .vpane[data-pane="local"]', timeout=6000)
    page.wait_for_timeout(700)
    local_text = page.locator('#view > .vpane[data-pane="local"]').inner_text()
    chk("真机（本地书架）：接口那条路收的书真的摆在书架上",
        book_rows[0]["title"] in local_text, local_text[:200])

    # ── 七、清空记录只清账 ──────────────────────────────────────
    page.click('#nav button[data-v="anna"]')
    page.wait_for_timeout(500)
    page.click(PANE + " #anClear")
    page.wait_for_selector('#veil.open', timeout=5000)
    chk("真机点「清空记录」：先问一句，说清只清账不动书",
        "一个都不动" in (page.locator("#sheetText").inner_text() or ""),
        page.locator("#sheetText").inner_text()[:160])
    page.click('#sheetActs button.sure')
    wait_text(page, PANE + " #anBooks", "还没有从窗口里接住的书")
    chk("真机点「确认清空」：三份记录回到空态",
        "还没有从窗口里接住的书" in pane_text("#anBooks")
        and "没有没收进来的文件" in pane_text("#anPending"),
        (pane_text("#anBooks"), pane_text("#anPending")))
    chk("真机点「确认清空」：计数归零",
        "接住 0 份" in pane_text("#anCnt"), pane_text("#anCnt"))
    a = state_anna()
    chk("真机点「确认清空」：账本清空，但书架上的两本书还在",
        a.get("imported") == 0 and a.get("books") == [] and len(MADE["book"]) > 0
        and os.path.isdir(os.path.join(SHELF, MADE["book"])),
        {k: a.get(k) for k in ("caught", "imported", "skipped")})
    chk("真机点「确认清空」：下载夹里的原文件也没动",
        os.path.isfile(MADE["mobi"]), MOBI_NAME)

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
