#!/usr/bin/env python3
"""归藏真机验证：写作平台（工具条 / 书 → 章那棵树 / 坞里的六件事）。

分三段。第一段只打接口 —— 建稿、存稿、版本号对不上、改名、快照、回退、导出，
以及书那一层的账（建书、加章、排顺序、出册、整本预览与整本 Markdown）；
第二段开浏览器，验这一屏真的点得动：上面那一排工具条改得动画面上的字
（标题、加粗、两个颜色、表格、待办、分隔 —— 用户不必自己写 Markdown）、
左栏那棵树建得出书、长得出章、合得上翻得开，六颗动作钮各开一次坞再收起来，
第一句落下就把稿子建出来（不点「新建」也不该丢字），以及「素材」那一栏那句
「原文只进提示词」的承诺真的写在界面上；第三段回源码，认函数不重名、emoji 清零。

别的套件验的是「界面照后端说的画」；这一套还要验两件只有写作才有的规矩：
存盘带版本号，对不上就当面停下问，不悄悄覆盖（编辑器丢稿的最后一道闸）；
还有屏幕上看见的字必须真的进了稿子 —— contenteditable 里那一份是 DOM，
只有序列化出来的 Markdown 才是账，两边对不上就是丢稿。

前提：有一个指向沙盒书库的服务（`bash tests/run_all.sh` 会起那一个）。
服务地址走命令行第一个参数或 GUIZANG_TEST_URL；没给就退出，不拿 8770 赌。
"""
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright

BASE = selftest.need_base(1)
selftest.SHOTS.mkdir(parents=True, exist_ok=True)
SHOTS = [str(selftest.SHOTS / (n + ".png")) for n in ("writer-edit", "writer-export",
                                                    "writer-material", "writer-book")]

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def get(path, raw=False):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        body = r.read()
        return body if raw else json.loads(body.decode("utf8"))


def post(path, payload, timeout=30):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf8"))


def http_status(path):
    """只问「回什么码」，不回 200 就算预期里的一种（导出没装铬内核时就是这样）。"""
    try:
        with urllib.request.urlopen(BASE + path, timeout=90) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_head(path):
    """只要响应头。导出预览必须带 no-store，得从头上看。"""
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        r.read()
        return r.status, dict(r.headers)


def base_name(p):
    s = str(p or "")
    return s[max(s.rfind("/"), s.rfind("\\")) + 1:]


# ── 一、接口契约 ────────────────────────────────────────────────
LIST = get("/api/writer")
chk("清单：回的是稿列 + 三张表（档位 / 改法 / 排版）",
    isinstance(LIST.get("drafts"), list) and LIST.get("templates")
    and LIST.get("lengths") and LIST.get("tones"),
    {k: LIST.get(k) for k in ("count", "words")})
chk("清单：续写档位是四个（一句 / 一段 / 三个要点 / 写完这一节），不是写死的",
    len(LIST.get("lengths") or []) == 4, LIST.get("lengths"))
chk("清单：排版模板四种（黑白简约 / 商务蓝 / 青翠 / 米白书简）",
    [t.get("id") for t in (LIST.get("templates") or [])] == ["plain", "business", "verdant", "letter"],
    LIST.get("templates"))

TEXT1 = "# 试笔\n\n第一段先说结论。第二句补一句为什么。\n\n- 一条\n- 两条\n"
NEW = post("/api/writer", {"do": "create", "title": "套件试笔", "text": ""})
chk("建稿：回 id 与整篇", NEW.get("ok") and NEW.get("id"),
    {k: NEW.get(k) for k in ("ok", "msg")})
WID = NEW.get("id") or ""
REV0 = int(((NEW.get("draft") or {}).get("meta") or {}).get("rev") or 0)
chk("建稿：新稿 rev 是 1", REV0 == 1, REV0)

S1 = post("/api/writer", {"do": "save", "id": WID, "text": TEXT1, "rev": REV0})
chk("存稿：字数按后端口径算（中文按字、英文按词，剥掉行首记号）",
    S1.get("ok") and S1.get("words") == 22, {k: S1.get(k) for k in ("ok", "words", "rev")})
chk("存稿：rev 往前走了", int(S1.get("rev") or 0) == REV0 + 1, S1.get("rev"))

S1b = post("/api/writer", {"do": "save", "id": WID, "text": TEXT1, "rev": int(S1.get("rev") or 0)})
chk("存稿：正文没变就不动 rev（免得每 1.4 秒自动存一次就把快照挤光）",
    S1b.get("ok") and int(S1b.get("rev") or 0) == int(S1.get("rev") or 0),
    {k: S1b.get(k) for k in ("ok", "rev", "words")})

BAD = post("/api/writer", {"do": "save", "id": WID, "text": "别的地方改的", "rev": REV0})
chk("版本号对不上：回 ok:false + conflict，而且一个字都没落盘",
    BAD.get("ok") is False and isinstance(BAD.get("conflict"), dict)
    and BAD.get("conflict", {}).get("rev") is not None, BAD)
AFTER = post("/api/writer", {"do": "save", "id": WID, "text": TEXT1,
                             "rev": int(S1.get("rev") or 0)})
chk("版本号对不上之后：原文还在（没被那句「别的地方改的」覆盖）",
    AFTER.get("ok") and int(AFTER.get("rev") or 0) == int(S1.get("rev") or 0),
    {k: AFTER.get(k) for k in ("ok", "rev")})

NOSAVE = post("/api/writer", {"do": "save", "id": WID, "rev": int(AFTER.get("rev") or 0)})
chk("保存时没带正文：挡下来，不许把稿子清成空白",
    NOSAVE.get("ok") is False, NOSAVE.get("msg"))

RN = post("/api/writer", {"do": "rename", "id": WID, "title": "套件试笔（改名）",
                          "rev": int(AFTER.get("rev") or 0)})
RD = get("/api/writer?id=" + WID)
chk("改名：标题落上了，正文一个字没动",
    RN.get("ok") and RD.get("meta", {}).get("title") == "套件试笔（改名）"
    and RD.get("text") == TEXT1, RD.get("meta", {}).get("title"))

chk("读整篇：正文 / 大纲 / 字数 / 快照列表四样都在",
    RD.get("text") == TEXT1 and isinstance(RD.get("outline"), list)
    and (RD.get("count") or {}).get("total") == 22 and isinstance(RD.get("versions"), list),
    {k: (RD.get(k) if k != "text" else "<%d 字>" % len(RD.get("text") or "")) for k in
     ("outline", "count", "versions")})

SN = post("/api/writer", {"do": "snapshot", "id": WID})
chk("手动快照：拍了就有一份，回列表不为空",
    SN.get("ok") and len(SN.get("versions") or []) >= 1, SN.get("name"))

TEXT2 = "# 试笔\n\n换了一版正文。\n"
S2 = post("/api/writer", {"do": "save", "id": WID, "text": TEXT2,
                          "rev": int(RD.get("meta", {}).get("rev") or 0)})
chk("再存一版", S2.get("ok"), S2)
RS = post("/api/writer", {"do": "restore", "id": WID, "name": (SN.get("versions") or [{}])[0].get("name")})
chk("回退：回到快照那一版（回退的是改名之前那一稿）",
    RS.get("ok") and ((RS.get("draft") or {}).get("text") or "").strip().endswith("两条"),
    (RS.get("draft") or {}).get("count"))

BADNAME = post("/api/writer", {"do": "restore", "id": WID, "name": "../../etc/passwd"})
chk("回退：拿 ../ 当快照名读不到任何东西（这是拼路径的地方）",
    BADNAME.get("ok") is False, BADNAME.get("msg"))

chk("没说要干什么：说人话，不抛异常",
    post("/api/writer", {"do": "wat"}).get("ok") is False)

# 预览与导出走同一条拼装路径：换模板该换出不同的 HTML，署名该在。
PLAIN = get("/api/writer?mode=preview&id=%s&tpl=plain&brand=1&date=1&lead=1" % WID, raw=True).decode("utf8")
BIZ = get("/api/writer?mode=preview&id=%s&tpl=business&brand=1&date=1&lead=1" % WID, raw=True).decode("utf8")
NLEAD = get("/api/writer?mode=preview&id=%s&tpl=plain&brand=0&date=0&lead=0" % WID, raw=True).decode("utf8")
chk("预览：两种排版的 HTML 不是同一份（换模板真的换排版）", PLAIN != BIZ)
chk("预览：带标注时有署名「归藏」，关掉就没有（三样标注真的管得住）",
    "归藏" in PLAIN and "归藏" not in NLEAD)
_st, _hd = http_head("/api/writer?mode=preview&id=%s&tpl=plain&brand=1" % WID)
chk("预览：带 no-store（导出那一刻必须重拼，不能拿浏览器缓存里上一版的）",
    _hd.get("Cache-Control") == "no-store", _hd.get("Cache-Control"))

# 四种导出：md 一定落地；png / pdf 看铬内核在不在，两条路都只认「说清了」或「真出了文件」。
MD = get("/api/writer/export?kind=md&id=%s&tpl=plain&brand=1&date=1&lead=1&json=1" % WID)
chk("导出 Markdown：落进导出文件夹，正文与稿子一致",
    MD.get("ok") and pathlib.Path(MD.get("path") or "").is_file()
    and pathlib.Path(MD.get("path")).read_text(encoding="utf8") == RS.get("draft", {}).get("text"),
    {"file": base_name(MD.get("path")), "size": MD.get("size"), "msg": MD.get("msg")})

for kind in ("png", "pdf"):
    code, body = http_status("/api/writer/export?kind=%s&id=%s&tpl=business&brand=1&date=1&lead=1&json=1"
                             % (kind, WID))
    if code == 200:
        j = json.loads(body.decode("utf8"))
        chk("导出 %s：出文件了且不是空壳" % kind,
            j.get("ok") and pathlib.Path(j.get("path") or "").is_file()
            and (j.get("size") or 0) > 1000,
            {"file": base_name(j.get("path")), "size": j.get("size")})
    else:
        # 铬内核没装好时只许说这一句（不许抛、不许含糊），这也是导出的前置拦。
        chk("导出 %s：铬内核没装好时说的是一句人话" % kind,
            code == 400 and "铬内核" in body.decode("utf8"), (code, body[:120]))

chk("导出：没有这一种格式时说没有，不抛", http_status("/api/writer/export?kind=wat&id=" + WID)[0] == 400)

# 素材那一栏的两处口径：用的是离线索引（不联网、不点外部）
IDX = get("/api/notes_index")
chk("素材：某本书的划线用的是离线索引（界面那一栏的选项就从这儿来）",
    IDX.get("ok") is True and isinstance(IDX.get("books"), list)
    and all("bookId" in b and "title" in b for b in (IDX.get("books") or [])),
    {"books": len(IDX.get("books") or [])})

AI = post("/api/writer/ai", {"mode": "spark", "id": WID, "text": TEXT1})
chk("AI：没配接口时回一句配置提示（而且是普通 JSON，不是半截流）",
    AI.get("ok") is False and isinstance(AI.get("msg"), str) and AI.get("msg"),
    AI.get("msg"))
chk("AI：润色没带选中时挡下来，一个字都不往外发",
    post("/api/writer/ai", {"mode": "polish", "id": WID, "sel": ""}).get("ok") is False)

# ── 一之二、书这一层的路由（左栏那棵树读的就是这几条）────────────
BK = post("/api/writer", {"do": "book.create", "title": "套件试的一本"})
BID = ((BK.get("book") or {}).get("id")) or ""
chk("建书：书号是 bk_ 开头，账上带着书名与空册",
    BK.get("ok") and BID.startswith("bk_") and (BK.get("book") or {}).get("title") == "套件试的一本",
    {k: BK.get(k) for k in ("ok", "msg", "book")})
CH1 = post("/api/writer", {"do": "book.add", "book": BID})
CH2 = post("/api/writer", {"do": "book.add", "book": BID})
chk("加章：不给稿号就顺手起一篇，界面上直接就能写",
    CH1.get("ok") and CH2.get("ok") and CH1.get("id") != CH2.get("id")
    and len(((CH2.get("book") or {}).get("chapters")) or []) == 2,
    {k: CH2.get(k) for k in ("ok", "msg")})
post("/api/writer", {"do": "save", "id": CH1.get("id"), "text": "第一章的正文在此。\n", "rev": 1})
BKLIST = get("/api/writer?book=" + BID)
bkdetail = BKLIST.get("book") or {}
chk("整本清档：章号是册上的顺序（1、2），标题与字数都带着",
    BKLIST.get("ok") and [c.get("no") for c in (bkdetail.get("chapters") or [])] == [1, 2],
    bkdetail.get("chapters"))
BPREV = get("/api/writer?mode=preview&book=%s&tpl=plain" % BID, raw=True).decode("utf8")
chk("整本预览：合稿出来的 HTML 带着第一章那句正文",
    "第一章的正文在此" in BPREV, BPREV[-160:])
BMD = get("/api/writer/export?kind=md&book=%s&tpl=plain&json=1" % BID)
BMDTXT = ""
if BMD.get("ok") and pathlib.Path(BMD.get("path") or "").is_file():
    BMDTXT = pathlib.Path(BMD["path"]).read_text(encoding="utf8")
chk("整本导出 Markdown：出的是一本书（两章都在，章与章之间分开）",
    BMD.get("ok") and "第一章的正文在此" in BMDTXT and "新的一章" in BMDTXT,
    {"file": base_name(BMD.get("path")), "size": BMD.get("size")})
ORD = post("/api/writer", {"do": "book.order", "book": BID, "chapter": CH2.get("id"), "delta": -1})
chk("挪顺序：第二章换到前面（界面上那对上下钮走的也是这条）",
    ORD.get("ok") and ORD.get("moved") is True
    and [c.get("id") for c in ((ORD.get("book") or {}).get("chapters") or [])]
    == [CH2.get("id"), CH1.get("id")], ORD)
OUT = post("/api/writer", {"do": "book.remove", "chapter": CH2.get("id")})
chk("出册：章从书上摘下来，稿子本身还在（摘登记不是删稿）",
    OUT.get("ok") and get("/api/writer?id=" + CH2.get("id")).get("ok") is True, OUT)
post("/api/writer", {"do": "book.delete", "book": BID})

# ── 二、真机：这一屏点得动 ─────────────────────────────────────
SRC = (selftest.REPO / "ui.html").read_text(encoding="utf8")


def md(page):
    """稿子里那一份 Markdown —— 存盘、导出、快照、MCP 读的都是它。
    屏幕上的字是 DOM，只有这一份才是账：两边对不上就是丢稿。"""
    return page.evaluate("() => wrGet()")


def LAST_STYLE(page, sel):
    """正文最后一块里剩多少个某种样式节点。
    数整屏不行：前面几步留下的粗体、颜色还在稿子里，「清干净」只该管选中的那一块。"""
    return page.evaluate("""(s) => {
      const ed = document.querySelector('#wrTextIn');
      const last = ed.lastElementChild;
      return last ? last.querySelectorAll(s).length : -1;
    }""", sel)


def whisper_text(page):
    """右下角那条提示此刻写的是什么。
    不能拿 inner_text 读：提示淡出后只是 visibility:hidden（元素还在，字也还在），
    inner_text 会把它当成「没内容」——于是一句本该说出来的话，测出来是空的。"""
    return page.evaluate("() => { const w = document.querySelector('.whisper');" \
                         " return w ? String(w.textContent || '') : ''; }")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.click('#nav button[data-v="write"]')
    page.wait_for_timeout(1200)

    chk("进得去：写作那一格真的在", page.locator('#view > .vpane[data-pane="write"]').count() == 1)
    chk("四块都在：书树 / 工具条 / 正文 / 坞那排动作钮",
        page.locator("#wrTree").count() == 1
        and page.locator("#wrTools").count() == 1
        and page.locator("#wrTextIn").count() == 1
        and page.locator("#wrBtnCont").count() == 1
        and page.locator("#wrBtnExp").count() == 1)
    chk("正文是一块直接点的板子（不再是让人手写记号的输入框）",
        page.get_attribute("#wrTextIn", "contenteditable") == "true",
        page.get_attribute("#wrTextIn", "contenteditable"))
    chk("坞是收着的（收起要真占不到地方，不是透明地还杵着）",
        page.locator("#wrRes").is_hidden())

    # 工具条存在的全部理由：不用自己写 Markdown。缺一种排版就等于把那一种送回手写。
    tb = page.eval_on_selector_all('#wrTools button[data-t]', "els => els.map(e => e.dataset.t)")
    need = ["p", "h1", "h2", "h3", "bold", "ital", "und", "strike", "code", "color", "back",
            "clear", "bull", "num", "task", "quote", "table", "link", "image", "hr", "md"]
    chk("工具条：标题 / 加粗 / 颜色 / 列表 / 待办 / 表格 / 引用 / 分隔 / 链接 / 图片 %d 种都在"
        % len(need), all(x in tb for x in need), [x for x in need if x not in tb])
    chk("工具条：一颗也没有暗桩（每颗都有提示语，认不出来的那几颗才有救）",
        page.eval_on_selector_all('#wrTools button[data-t]',
                                  "els => els.filter(e => !e.title).length") == 0)
    # 上面那一段接口契约已经在沙盒库里留下稿子了，进来会自动落到第一篇。
    # 这里要验的是「空板子」那一面，所以先把这一屏的账归零（不许动库里别人的稿）：
    # 清掉没存盘的、把当前这篇的身份抹掉，编辑器就回到「第一句还没落下」那个状态。
    page.evaluate("""() => {
      clearTimeout(WR.timer);
      WR.id = ''; WR.rev = 0; WR.title = ''; WR.text = ''; WR.words = 0;
      WR.dirty = false; WR.pending = false; WR.creating = false;
      const ti = document.querySelector('#wrTitleIn');
      if (ti) ti.value = '';
      wrSet('');
      paintWrMeta();
    }""")
    page.wait_for_timeout(300)

    # 提示语是 CSS 画的：屏幕上看得见，但既不在 DOM 里、也不在稿子里 ——
    # 否则每个新开稿子的人都白得一句「从这里开始写」，还会被当成正文存下去。
    # 用 innerText 量这一条量不出来：Chromium 把 ::before 的内容也算进 innerText。
    ph = page.evaluate("""() => getComputedStyle(document.querySelector('#wrTextIn'),
                                                '::before').content""")
    chk("空稿子的提示语看得见、又不在稿子里（不会混进正文被当成正文存下）",
        "wrbare" in (page.get_attribute("#wrTextIn", "class") or "")
        and "从这里开始写" in ph and "从这里开始写" not in md(page)
        and md(page) == "", (page.get_attribute("#wrTextIn", "class"), ph[:40]))
    # 空板子里那个待着的段落（wrMarkup 种下的）序列化必须等于「没有」：
    # 它要是落进稿子，每篇新稿开头都多一行空行，字数也白加一个。
    chk("空板子里待着的那个段落不落进稿子（种子段落不许算成正文）",
        page.evaluate("() => document.querySelector('#wrTextIn').children.length") == 1
        and md(page) == "")

    # 第一句落下就把稿子建出来：不点「新建」也不该丢字。
    page.click("#wrTextIn")
    page.type("#wrTextIn", "第一句先落在正文里。", delay=8)
    page.wait_for_timeout(2200)
    NEW_ID = page.evaluate("() => WR.id")
    chk("第一句落下就自动建稿：正文那一篇的身份真的建出来了",
        bool(NEW_ID) and page.locator('#wrTree [data-do="open"][data-id="%s"]' % NEW_ID).count() == 1,
        {"id": NEW_ID, "meta": page.inner_text("#wrMeta")})
    chk("第一句落下：表头认出了字数（字数在动就是稿子有了）",
        "字" in (page.inner_text("#wrMeta") or ""), page.inner_text("#wrMeta"))
    # contenteditable 最容易犯的一种丢稿：浏览器把直接打进去的字挂在正文那一块的根上
    # （没包成段落），字数照它算、序列化却不认它 —— 屏幕上有一行，稿子里是空的。
    chk("第一句真的进了稿子（不许只有字数在动、Markdown 里却是空的）",
        "第一句先落在正文里" in md(page), md(page)[:80])

    # ── 工具条动得了动画面上的字（这一节才是「不用写 Markdown」的证据）──
    def select_last():
        page.evaluate("""() => {
          const ed = document.querySelector('#wrTextIn');
          const last = ed.lastElementChild || ed;
          const r = document.createRange();
          r.selectNodeContents(last);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          wrSaveRange();
        }""")

    page.keyboard.press("Enter")
    page.type("#wrTextIn", "这一行要当标题", delay=8)
    page.wait_for_timeout(300)
    # 1.2.2 起段落样式收成一颗钮 + 一张弹层：先点开那颗「这段是什么」，
    # 再点弹层里的「标题一」。按钮本身还在（data-t 没变），只是默认不摊开。
    page.click("#wrStyleB")
    page.wait_for_selector("#wrTools .wstylepop.open", timeout=4000)
    page.click('.wstylepop button[data-t="h1"]')
    page.wait_for_timeout(300)
    chk("工具条：点「标题一」这一行真的成了标题（不用再自己数 #）",
        page.locator("#wrTextIn h1").count() == 1
        and "# 这一行要当标题" in md(page), md(page)[-90:])
    chk("工具条：哪一颗管着当前这一行，那颗就亮着（不亮就等于用户不知道现在是标题）",
        page.locator('#wrTools button[data-t="h1"].on, #wrStyleB.on').count() >= 1)

    page.keyboard.press("Enter")
    page.type("#wrTextIn", "加粗与两个颜色", delay=8)
    page.wait_for_timeout(300)
    select_last()
    page.click('#wrTools button[data-t="bold"]')
    page.wait_for_timeout(250)
    chk("工具条：加粗落在选中的那几个字上（稿子里是 ** 而不是满屏 style）",
        page.locator("#wrTextIn b, #wrTextIn strong").count() == 1
        and "**加粗与两个颜色**" in md(page), md(page)[-90:])
    select_last()
    page.click('#wrTools button[data-t="color"]')
    page.wait_for_timeout(250)
    dots = page.locator('#wrTools .edpop.open .dots button.dot')
    chk("工具条：「字色」点开的是那一片色板（十色 + 一颗不涂）",
        dots.count() == 10
        and page.locator('#wrTools .edpop.open .dots button.undo').count() == 1, dots.count())
    dots.nth(0).click()
    page.wait_for_timeout(250)
    chk("工具条：点一颗颜色，选中的字带上这个色，稿子里也留着这一笔",
        page.locator('#wrTextIn [style*="d81e06"]').count() == 1
        and 'style="color:#d81e06"' in md(page), md(page)[-110:])
    chk("工具条：涂完色浮层自己收起来（一张掀开的牌挡着下一行）",
        page.locator('#wrTools .edpop.open').count() == 0)
    select_last()
    page.click('#wrTools button[data-t="back"]')
    page.wait_for_timeout(250)
    chk("工具条：底色是另一套（浅七色），跟字色不共用那一排",
        page.locator('#wrTools .edpop.open .dots button.dot').count() == 7
        and page.locator('#wrTools .edpop.open .gh').inner_text().strip() == "底色")
    page.locator('#wrTools .edpop.open .dots button.dot').nth(0).click()
    page.wait_for_timeout(250)
    chk("工具条：铺了底色的那几个字进得了稿子（导出也带得走）",
        'style="background-color:#fff3a3"' in md(page), md(page)[-110:])

    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    page.click("#wrPlusB")
    page.wait_for_selector("#wrTools .winspop.open", timeout=4000)
    page.click('.winspop button[data-t="table"]')
    page.wait_for_timeout(250)
    cells = page.locator('#wrTools .edpop.open .tb i')
    chk("工具条：「表格」点开的是那张几乘几的矩阵（点几乘几就插几乘几）",
        cells.count() == 48
        and page.locator('#wrTools .edpop.open .cap').inner_text().strip() != "")
    cells.nth(9).click()
    page.wait_for_timeout(300)
    chk("工具条：点格子当场把表格插进正文（表头一行、数据一行都在）",
        page.locator("#wrTextIn table").count() == 1
        and page.locator("#wrTextIn thead th").count() == 2
        and page.locator("#wrTextIn tbody tr").count() == 1)
    chk("工具条：表格在稿子里就是那张竖线表（导出、MCP、快照读的都是这一份）",
        "| 列1 | 列2 |" in md(page) and "|---|---|" in md(page), md(page)[-160:])
    chk("工具条：表尾补了一个空段落（出了表还有地方打字，不用拿方向键摸出去）",
        page.evaluate("""() => {
          const t = document.querySelector('#wrTextIn table');
          return !!(t && t.nextElementSibling && /^(P|DIV)$/.test(t.nextElementSibling.tagName));
        }"""))
    # 插完表格，光标是故意停在第一格里的（等着人填字）。这时候随手点一下「待办」
    # 是最容易把整张表弄散的一下 —— 表格会被换成一个 li，几格的字挤成一行。
    chk("插完表格：光标就在第一格等着（不用先拿鼠标点进表里）",
        page.evaluate("""() => {
          const n = window.getSelection().anchorNode;
          const el = n && (n.nodeType === 1 ? n : n.parentNode);
          const cell = el && el.closest ? el.closest('td,th') : null;
          return !!(cell && document.querySelector('#wrTextIn').contains(cell));
        }"""))
    page.click("#wrStyleB")
    page.wait_for_selector("#wrTools .wstylepop.open", timeout=4000)
    page.click('.wstylepop button[data-t="task"]')
    page.wait_for_timeout(300)
    chk("表格里点「待办」：表格没被拍扁，只说一句（宁可按不动，不能把用户搭的结构弄散）",
        page.locator("#wrTextIn table").count() == 1
        and page.locator("#wrTextIn li").count() == 0
        and "结构" in whisper_text(page), md(page)[-90:])

    # 光标挪到表尾那个空段落再往下写：这一段是段落，不是表格里的字。
    tail = page.locator("#wrTextIn table + p, #wrTextIn table + div").first
    box = tail.bounding_box()
    page.mouse.click(box["x"] + 12, box["y"] + box["height"] / 2)
    page.wait_for_timeout(200)
    page.type("#wrTextIn", "买一台能写长的机器", delay=8)
    page.wait_for_timeout(300)
    page.click("#wrStyleB")
    page.wait_for_selector("#wrTools .wstylepop.open", timeout=4000)
    page.click('.wstylepop button[data-t="task"]')
    page.wait_for_timeout(300)
    chk("工具条：「待办」给这一行装上能勾的方框",
        page.locator("#wrTextIn li.task").count() == 1
        and "- [ ] 买一台能写长的机器" in md(page), md(page)[-90:])
    # 方框那一格按几何算，不写死像素：::before 占 li 左边 0~.85em，竖直从 .42em 起 .85em 高。
    # 写死的偏移一换字号（缩窗口、换主题）就点空，而这种「点不动」最难在测试里看出来。
    def task_point(frac):
        return page.evaluate("""(f) => {
          const li = document.querySelector('#wrTextIn li.task');
          const b = li.getBoundingClientRect();
          const em = parseFloat(getComputedStyle(li).fontSize) || 16;
          return {x: b.left + em * f, y: b.top + em * 0.85};
        }""", frac)

    hit = task_point(0.42)
    page.mouse.click(hit["x"], hit["y"])
    page.wait_for_timeout(1200)
    chk("待办：点左边那个方框就勾上（勾的是这一行，不是选中文字）",
        page.locator("#wrTextIn li.task.done").count() == 1 and "- [x]" in md(page),
        md(page)[-90:])
    # 方框画在 li 外侧时，那一片像素归外面那个 ul —— 点它拿不到 li，那颗框其实点不动。
    # 现在框挪进 li 自己那一格，命中区从 2 个像素宽成一整格，但字那一片必须还算「点这一行」：
    # 否则人想选字、改字，一下手先把勾打了。
    txt = task_point(2.2)
    page.mouse.click(txt["x"], txt["y"])
    page.wait_for_timeout(600)
    chk("待办：点在字上不是勾选（放宽命中区是为了好点，不是抢改字的手势）",
        page.locator("#wrTextIn li.task.done").count() == 1, md(page)[-90:])
    page.mouse.click(hit["x"], hit["y"])
    page.wait_for_timeout(1200)
    chk("待办：再点一下就取消（勾能打也能撤，点错了不用整行重打）",
        page.locator("#wrTextIn li.task.done").count() == 0 and "- [ ]" in md(page),
        md(page)[-90:])

    # 点框那一下把光标落在行首，直接回车会把这一行劈成两半 —— 先走到行尾再续写。
    fin = page.evaluate("""() => {
      const li = document.querySelector('#wrTextIn li.task');
      const b = li.getBoundingClientRect();
      return {x: b.right - 6, y: b.top + b.height / 2};
    }""")
    page.mouse.click(fin["x"], fin["y"])
    page.wait_for_timeout(300)
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    page.type("#wrTextIn", "这几个字要被清干净", delay=8)
    page.wait_for_timeout(300)
    select_last()
    page.click('#wrTools button[data-t="bold"]')
    page.wait_for_timeout(200)
    chk("工具条：「加粗」吃的是当前这一块（列表里的项也加得上，不是只有段落才行）",
        LAST_STYLE(page, "b,strong") >= 1 and "**这几个字要被清干净**" in md(page),
        md(page)[-90:])
    select_last()
    page.click("#wrPlusB")
    page.wait_for_selector("#wrTools .winspop.open", timeout=4000)
    page.click('.winspop button[data-t="clear"]')
    page.wait_for_timeout(250)
    chk("工具条：「清格式」只剥样式，字一个不动（清成空白是最怕的一种「帮忙」）",
        LAST_STYLE(page, "b,strong,span[style]") == 0
        and "这几个字要被清干净" in md(page), md(page)[-90:])

    page.keyboard.press("Enter")
    page.click("#wrPlusB")
    page.wait_for_selector("#wrTools .winspop.open", timeout=4000)
    page.click('.winspop button[data-t="hr"]')
    page.wait_for_timeout(300)
    chk("工具条：「分隔」落一条横线，稿子里是那三个减号",
        page.locator("#wrTextIn hr").count() == 1
        and "---" in md(page).split("\n"), repr(md(page)[-60:]))

    # 看 Markdown：这一屏存的就是这一份，只给看和复制，不给在那儿改。
    page.click('#wrTools button[data-t="md"]')
    page.wait_for_timeout(300)
    shown = page.evaluate("() => document.querySelector('#sheetBody').value")
    chk("工具条：「看 Markdown」摊开的正是稿子里那一份（两处能改就一定会互相覆盖）",
        page.inner_text("#sheetTitle").strip() == "稿子里存的那份 Markdown"
        and "第一句先落在正文里" in shown and "| 列1 | 列2 |" in shown, shown[:90])
    page.click('#sheetActs button:not(.sure)')
    page.wait_for_timeout(250)
    chk("工具条：看完关掉（弹层不许留在稿子上面挡住下一句）",
        page.locator(".veil.open").count() == 0
        or page.evaluate("() => !document.querySelector('.veil').classList.contains('open')"))
    page.screenshot(path=SHOTS[0])


    # 六颗动作钮各开一次坞，再点同一颗收起。
    for bid, title in (("wrBtnCont", "续写"), ("wrBtnSpark", "灵感"), ("wrBtnPolish", "润色"),
                       ("wrBtnMat", "用什么笔记"), ("wrBtnExp", "导出"), ("wrBtnVer", "历史快照")):
        page.click("#" + bid)
        page.wait_for_timeout(240)
        ok = (not page.locator("#wrRes").is_hidden()
              and (page.inner_text("#wrResTitle") or "").strip() == title
              and page.locator("#wrResBody").inner_text().strip() != "")
        chk("坞：点「%s」开出来的就是这一栏，而且有内容" % title, ok,
            (page.inner_text("#wrResTitle"), page.locator("#wrResBody").inner_text()[:60]))
        page.click("#" + bid)
        page.wait_for_timeout(200)
        chk("坞：再点一次「%s」收起来" % title, page.locator("#wrRes").is_hidden())

    page.click("#wrBtnCont")
    page.wait_for_timeout(240)
    chk("续写那一栏：四个档位都在（写多少由用户自己选，不是写死的）",
        page.locator('#wrResBody .seg button[data-act="len"]').count() == 4,
        page.locator('#wrResBody .seg button').count())

    page.click("#wrBtnExp")
    page.wait_for_timeout(240)
    chk("导出那一栏：四种排版都在（黑白简约 / 商务蓝 / 青翠 / 米白书简）",
        page.locator('#wrResBody .seg button[data-act="tpl"]').count() == 4)
    chk("导出那一栏：三样标注 + 四颗格式钮（图片 / PDF / 电子书 / Markdown）都在",
        page.locator('#wrResBody input[data-act="brand"]').count() == 1
        and page.locator('#wrResBody input[data-act="date"]').count() == 1
        and page.locator('#wrResBody input[data-act="lead"]').count() == 1
        and page.locator('#wrResBody button[data-act="png"]').count() == 1
        and page.locator('#wrResBody button[data-act="pdf"]').count() == 1
        and page.locator('#wrResBody button[data-act="epub"]').count() == 1
        and page.locator('#wrResBody button[data-act="md"]').count() == 1)
    # .seg 是给顶栏那种图标钮用的（写死 27 宽）。写作平台拿它当文字档位，
    # 一旦忘了放行宽度，「黑白简约」会被压成四行叠成一团 —— 这条盯住那颗 27。
    widths = page.eval_on_selector_all(
        '#wrResBody .seg button',
        "els => els.map(e => Math.round(e.getBoundingClientRect().width))")
    chk("导出那一栏：排版芯片按字数撑开（没被图标钮那套 27px 压扁）",
        bool(widths) and min(widths) >= 34, widths)
    page.screenshot(path=SHOTS[1])

    page.click("#wrBtnMat")
    page.wait_for_timeout(700)
    mat = page.locator("#wrResBody").inner_text()
    chk("素材那一栏：当场把「原文只进提示词」这条承诺写在界面上（不写日志、不进回执）",
        "不写日志" in mat and "不进回执" in mat, mat[-120:])
    page.screenshot(path=SHOTS[2])

    page.click("#wrResClose")
    page.wait_for_timeout(300)
    chk("坞上的「收起」也收得掉", page.locator("#wrRes").is_hidden())

    # ── 左栏那棵树：建得出书、长得出章、合得上翻得开 ────────────
    # 书是「章的顺序」这一层，界面上每一步都要能对上后端那本账：
    # 建一本 → 自动起第一章 → 写两句 → 加一章 → 挪顺序 → 合上翻开 → 出册 → 再归进去。
    BOOK = "套件真机书"
    page.evaluate("() => document.querySelector('#wrNewBook').click()")
    page.wait_for_timeout(400)
    chk("建书：弹层说清了「书不是另一份稿子」",
        page.inner_text("#sheetTitle").strip() == "新建一本书"
        and "每一章照旧是一篇独立的稿" in page.inner_text("#sheetText"),
        page.inner_text("#sheetText")[:70])
    page.fill("#sheetBody", BOOK)
    page.click("#sheetActs button.sure")
    page.wait_for_timeout(2600)
    BID_JS = page.evaluate("() => WR.book")
    CH1 = page.evaluate("() => WR.id")
    chk("建书：书名落进左栏，而且当场就有第一章（建完不用再多点一次「新建」）",
        bool(BID_JS) and page.locator('#wrTree .wrtome[data-b="%s"]' % BID_JS).count() == 1
        and page.locator('#wrTree .wrtome[data-b="%s"] .wrdoc.chap' % BID_JS).count() == 1
        and page.locator('#wrTree .wrtome[data-b="%s"] .bt' % BID_JS).inner_text().strip() == BOOK,
        {"book": BID_JS, "tomes": page.locator("#wrTree .wrtome").count()})
    chk("建书：新建那本自动摊开（合上了就找不到章在哪）",
        page.locator('#wrTree .wrtome[data-b="%s"].open' % BID_JS).count() == 1)
    chk("建书：直接落进第一章（写着「我建了一本书，然后点哪儿？」就算白建）",
        bool(CH1) and page.locator('#wrTree .wrdoc.chap.on[data-id="%s"]' % CH1).count() == 1,
        {"doc": CH1})

    page.click("#wrTextIn")
    page.type("#wrTextIn", "第一章的第一句。", delay=8)
    page.wait_for_timeout(2000)
    chk("写章：第一章的字存进了第一章自己那篇稿",
        "第一章的第一句" in (get("/api/writer?id=%s" % CH1).get("text") or ""),
        get("/api/writer?id=%s" % CH1).get("count"))

    page.evaluate("() => document.querySelector('#wrCrum button[data-do=\"add\"]').click()")
    page.wait_for_timeout(2600)
    CH2 = page.evaluate("() => WR.id")
    chk("加一章：面包屑那颗「加一章」真的长出一章，并把它切过去",
        bool(CH2) and CH2 != CH1
        and page.locator('#wrTree .wrtome[data-b="%s"] .wrdoc.chap' % BID_JS).count() == 2,
        {"c1": CH1, "c2": CH2})
    # 两章共用同一块板子（正文是同一块 contenteditable，换章只是重涂一遍），
    # 上一章的字要是跟着流进这一章，那就是最恶劣的一种串稿。
    chk("换章：第一章的字没有跟着流进第二章",
        "第一章的第一句" not in md(page) and md(page).strip() == "", md(page)[:60])

    page.click("#wrTextIn")
    page.type("#wrTextIn", "第二章开头写的是这个。", delay=8)
    page.wait_for_timeout(2000)
    chk("写章：这一章的字各归各的稿（两章都认得出自己那一句）",
        "第二章开头写的是这个" in (get("/api/writer?id=%s" % CH2).get("text") or "")
        and "第一章的第一句" in (get("/api/writer?id=%s" % CH1).get("text") or ""),
        get("/api/writer?id=%s" % CH2).get("count"))

    CRUM = page.inner_text("#wrCrum")
    chk("面包屑：写清了「第几章 · 章名」，书名也在（写着写着不知道写到哪一本是最常见的迷路）",
        BOOK in CRUM and "第 2 章" in CRUM, CRUM[:70])
    MOV = "(() => ['up','down'].map(d => { const b = document.querySelector('#wrCrum button[data-do=\"' + d + '\"]'); return d + ':' + (b && b.disabled ? 'x' : '-'); }).join(','))()"
    chk("面包屑：第二章能上移、不能再下移（挪不出边界，比报错更省心）",
        page.evaluate(MOV) == "up:-,down:x", page.evaluate(MOV))

    page.locator('#wrTree .wrtome[data-b="%s"] .wrdoc.chap' % BID_JS).nth(0).click()
    page.wait_for_timeout(2200)
    chk("树：点章那一行就切到那一章", page.evaluate("() => WR.id") == CH1,
        {"now": page.evaluate("() => WR.id")})
    chk("面包屑：切到第一章后「上移」点不动了（第一章没有上面）",
        page.evaluate(MOV) == "up:x,down:-", page.evaluate(MOV))

    page.locator('#wrTree .wrtome[data-b="%s"] .wrdoc.chap' % BID_JS).nth(1).click()
    page.wait_for_timeout(2200)
    page.evaluate("() => document.querySelector('#wrCrum button[data-do=\"up\"]').click()")
    page.wait_for_timeout(2400)
    NOW_FIRST = [c.get("id") for c in
                 ((get("/api/writer?book=%s" % BID_JS).get("book") or {}).get("chapters") or [])][:1]
    chk("挪顺序：「上移」真的把这一章提到第一位（界面上的号与后端那本账对得上）",
        NOW_FIRST == [CH2] and "第 1 章" in page.inner_text("#wrCrum"),
        {"first": NOW_FIRST, "crum": page.inner_text("#wrCrum")[:40]})

    page.evaluate("() => document.querySelector('#wrTree .wrtomeh[data-do=\"fold\"]').click()")
    page.wait_for_timeout(500)
    chk("树：点书名那一行就合上（章藏起来了，书名与几章几字还在）",
        page.locator('#wrTree .wrtome[data-b="%s"] .wrchaps' % BID_JS).count() == 0
        and page.locator('#wrTree .wrtome[data-b="%s"] .bt' % BID_JS).count() == 1
        and "章" in page.locator('#wrTree .wrtome[data-b="%s"] .bn' % BID_JS).inner_text(),
        page.locator('#wrTree .wrtome[data-b="%s"] .bn' % BID_JS).inner_text())
    page.evaluate("() => document.querySelector('#wrTree .wrtomeh[data-do=\"fold\"]').click()")
    page.wait_for_timeout(500)
    chk("树：再点一下又翻开（合得上翻得开，两章都回来了）",
        page.locator('#wrTree .wrtome[data-b="%s"] .wrdoc.chap' % BID_JS).count() == 2)

    # 整本导出：勾上以后问的是「这一本」，不勾问的是「这一章」——同一条接口，只换要哪一份。
    page.evaluate("() => document.querySelector('#wrCrum button[data-do=\"whole\"]').click()")
    page.wait_for_timeout(600)
    chk("整本导出：那颗钮把人带到导出那一栏，并把开关打开",
        not page.locator("#wrRes").is_hidden()
        and page.locator('#wrResBody input[data-act="whole"]').count() == 1
        and page.locator('#wrResBody input[data-act="whole"]').is_checked(),
        page.inner_text("#wrResTitle"))
    MERGE = post("/api/writer", {"do": "book.merge", "book": BID_JS})
    chk("整本导出：合稿带得走两章的字（一章一段，电子书的目录就是章）",
        MERGE.get("ok") and "第一章的第一句" in (MERGE.get("text") or "")
        and "第二章开头写的是这个" in (MERGE.get("text") or ""), MERGE.get("count"))
    WQ = page.evaluate("() => wrExpQuery([])")
    chk("整本导出：勾上之后问的是这本号（问错了就只导出一章，是最难发现的那种少）",
        ("book=%s" % BID_JS) in WQ and "id=" not in WQ, WQ)
    page.evaluate("""() => {
      const c = document.querySelector('#wrResBody input[data-act="whole"]');
      c.checked = false;
      c.dispatchEvent(new Event("change", {bubbles: true}));
    }""")
    page.wait_for_timeout(500)
    chk("整本导出：不勾就只导现在这一章（一份还是一整本，问的号要跟着换）",
        "id=" in page.evaluate("() => wrExpQuery([])"), page.evaluate("() => wrExpQuery([])"))
    page.screenshot(path=SHOTS[3])

    # 出册摘的是「屏上这一章」，上一步挪完顺序留在屏上的是另一章 —— 先把要摘的那一章选上，
    # 否则这条断言验的是「没摘掉的那一章还留着」，恰好把真会犯的错放过。
    page.evaluate("""() => {
      const row = document.querySelector('#wrTree .wrdoc.chap[data-id="%s"]');
      if (row) row.click();
    }""" % CH1)
    page.wait_for_timeout(2200)
    chk("出册之前：要摘的那一章先在屏上（面包屑跟着换成它，不然摘错人）",
        page.evaluate("() => WR.id") == CH1 and BOOK in page.inner_text("#wrCrum"),
        page.inner_text("#wrCrum")[:50])
    page.evaluate("() => document.querySelector('#wrCrum button[data-do=\"out\"]').click()")
    page.wait_for_timeout(400)
    chk("出册：先问一句（把章从书上摘下来是「这篇忽然不见了」那一类动作）",
        page.inner_text("#sheetTitle").strip() == "把这一章移出这本书"
        and "稿子一个字都不动" in page.inner_text("#sheetText"), page.inner_text("#sheetText")[:60])
    page.click("#sheetActs button.sure")
    page.wait_for_timeout(2600)
    chk("出册：这一章不再排在这本书里，稿子本身还在、也回到了「未归册」那一堆",
        page.locator('#wrTree .wrtome[data-b="%s"] [data-do="open"][data-id="%s"]' % (BID_JS, CH1))
        .count() == 0
        and get("/api/writer?id=%s" % CH1).get("ok") is True
        and page.locator('#wrTree .wrtome.loose [data-do="open"][data-id="%s"]' % CH1).count() == 1,
        {"rows": page.locator("#wrTree .wrdoc").count()})
    page.evaluate("() => document.querySelector('#wrCrum button[data-do=\"into\"]').click()")
    page.wait_for_timeout(400)
    chk("归册：列出的是机上现有那几本书，点一本就排到章尾",
        page.inner_text("#sheetTitle").strip() == "归进哪一本书"
        and BOOK in page.locator("#sheetActs").inner_text(),
        page.locator("#sheetActs").inner_text()[:80])
    page.evaluate("""() => {
      const b = [].slice.call(document.querySelectorAll('#sheetActs button'))
        .filter(x => (x.textContent || '').indexOf('""" + BOOK + """') >= 0)[0];
      if (b) b.click();
    }""")
    page.wait_for_timeout(2600)
    chk("归册：归回去了，面包屑重新报出书名（归错了再拿出来就行，字不会丢）",
        page.evaluate("() => WR.book") == BID_JS and BOOK in page.inner_text("#wrCrum"),
        page.inner_text("#wrCrum")[:50])
    post("/api/writer", {"do": "book.delete", "book": BID_JS})

    # 换到别的屏再回来：这一屏是常驻的，来回一趟控件不该少、正文不该丢。
    # 打字留在屏上的那几句还没落盘（自动存要等一会儿），切屏最怕的就是把它们洗掉。
    page.click("#wrTextIn")
    page.keyboard.press("Enter")
    page.type("#wrTextIn", "切屏之前刚打的字。", delay=8)
    page.wait_for_timeout(300)
    BEFORE = md(page)
    page.click('#nav button[data-v="shelf"]')
    page.wait_for_timeout(700)
    page.click('#nav button[data-v="write"]')
    page.wait_for_timeout(900)
    chk("来回切：工具条 / 正文 / 树一样也没少（这一屏不该被重铺一遍）",
        page.locator("#wrTools").count() == 1 and page.locator("#wrTextIn").count() == 1
        and page.locator("#wrTree").count() == 1)
    chk("来回切：正文还在（切屏不许把没存的字弄丢）",
        md(page) == BEFORE and "切屏之前刚打的字" in md(page), md(page)[-70:])

    # 屏幕上不许有 emoji：这一屏的字全是自己写的，没有一条是外来正文，一颗都不该漏。
    seen = page.inner_text("#view")
    dom_emo = [c for c in seen if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]
    chk("界面：写作这一屏上一个 emoji 也没有", not dom_emo, dom_emo[:8])
    chk("全程没有报错", not errors, errors[:6])

    browser.close()

# ── 三、源码口径 ────────────────────────────────────────────────
dupes = {n: SRC.count("function " + n + "(") for n in
         ["wrMarkup", "renderWriteView", "wireWrite", "wrSave", "wrConflict", "wrFlush",
          "wrContRun", "wrSparkRun", "wrPolishRun", "wrApplyBlocks", "wrExportDo",
          "wrResClick", "wrResChange", "wrResInput", "wrVersions", "wrRestore",
          # 序列化那一条链：屏幕上看的字与稿子里存的字只有这一个来路，
          # 定义了两次就是「后一份悄悄盖掉前一份」，丢的是用户正在写的东西。
          "wrGet", "wrBlock", "wrParse", "wrPlain", "wrTextMap", "wrSet", "wrPh",
          "wrStartDoc", "wrAfterEdit", "wrIn", "wrSpanStyle", "wrHex",
          # 工具条与浮层：一条命令两份定义，就会有「这一颗在这台机器上没反应」。
          "wrBarWire", "wrBarState", "wrPopBuild", "wrPopToggle", "wrPopHide",
          "wrTableDo", "wrColor", "wrWrap", "wrPeel", "wrBlocks", "wrCurBlock",
          "wrTurnable", "wrTurnList", "wrTurn", "wrNest", "wrHeading", "wrList",
          "wrTask", "wrQuote", "wrRule",
          # 书与章那一层：树、面包屑、册账三个出口，谁重铺一次都得同一份账。
          "paintWrTree", "wrBookHtml", "wrChapterHtml", "wrLooseHtml", "wrTreeClick",
          "paintWrCrum", "wrCrumClick", "wrNewBook", "wrAddChapter", "wrMoveChapter",
          "wrOutChapter", "wrIntoBook", "wrBookMenu", "wrRenameBook", "wrTakeBook",
          "wrTakeRows", "wrFoldOn", "wrFirstDoc", "wrOpen", "wrNewDoc",
          "wrWholeExport", "wrExpReady", "wrExpQuery", "wrExpHtml"]}
chk("代码：写作那一套关键函数没有重名覆盖（一份定义）",
    all(v == 1 for v in dupes.values()), {k: v for k, v in dupes.items() if v != 1})
# 源码里唯一允许出现 emoji 的地方是剪藏正文的清洗正则（STRIP），界面上一颗都不该有。
body = "\n".join(l for l in SRC.split("\n") if "STRIP = " not in l)
emo = [c for c in body if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]
chk("界面：源码里除清洗正则外零 emoji", not emo, emo[:10])

bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
print("截图：" + " ".join(SHOTS))
sys.exit(1 if bad else 0)
