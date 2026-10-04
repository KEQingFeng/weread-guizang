#!/usr/bin/env python3
"""归藏真机验证：写作平台（稿列 / 正文 / 坞里的六件事）。

分两段。前一段只打接口 —— 建稿、存稿、版本号对不上、改名、快照、回退、导出；
后一段开浏览器，验这一屏真的点得动：六颗动作钮各开一次坞、再点一次收起来、
第一句落下就把稿子建出来（不点「新建」也不该丢字）、导出那一屏的模板芯片、
以及「素材」那一栏那句「原文只进提示词」的承诺真的写在界面上。

别的套件验的是「界面照后端说的画」；这一套还要验一件只有写作才有的规矩：
存盘带版本号，对不上就当面停下问，不悄悄覆盖（编辑器丢稿的最后一道闸）。

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
                                                    "writer-material")]

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

# ── 二、真机：这一屏点得动 ─────────────────────────────────────
SRC = (selftest.REPO / "ui.html").read_text(encoding="utf8")

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
    chk("三块都在：稿列 / 正文 / 坞那排动作钮",
        page.locator("#wrDocList").count() == 1
        and page.locator("#wrTextIn").count() == 1
        and page.locator("#wrBtnCont").count() == 1
        and page.locator("#wrBtnExp").count() == 1)
    chk("坞是收着的（收起要真占不到地方，不是透明地还杵着）",
        page.locator("#wrRes").is_hidden())

    # 第一句落下就把稿子建出来：不点「新建」也不该丢字。
    page.click("#wrTextIn")
    page.type("#wrTextIn", "第一句先落在正文里。", delay=8)
    page.wait_for_timeout(2200)
    chk("第一句落下就自动建稿：稿列里多了一行、表头认出了字数",
        page.locator("#wrDocList .wrdoc").count() >= 1
        and "字" in (page.inner_text("#wrMeta") or ""),
        page.inner_text("#wrMeta"))

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

    page.click("#wrTextIn")
    page.type("#wrTextIn", "\n\n再补一段。", delay=8)
    page.wait_for_timeout(1800)
    page.screenshot(path=SHOTS[0])

    # 换到别的屏再回来：这一屏是常驻的，来回一趟控件不该少、正文不该丢。
    page.click('#nav button[data-v="shelf"]')
    page.wait_for_timeout(700)
    page.click('#nav button[data-v="write"]')
    page.wait_for_timeout(900)
    chk("来回切：正文还在（切屏不许把没存的字弄丢）",
        "第一句先落在正文里。" in (page.input_value("#wrTextIn") or ""),
        (page.input_value("#wrTextIn") or "")[:40])

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
          "wrResClick", "wrResChange", "wrResInput", "wrVersions", "wrRestore"]}
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
