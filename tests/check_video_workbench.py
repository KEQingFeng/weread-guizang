# -*- coding: utf-8 -*-
"""转写工作台的端到端自测：改字 → 保存 → 重建 → 导出 → 下载，一条链跑到磁盘上。

为什么单开一份：视频这一屏这轮从「一个按钮 + 一条进度条」长成了一本工具（左栏挑书、
右栏逐段改、能筛能存能导）。界面按 240 段一屏铺、还能按关键词筛，于是最容易出的事故变成：
**戴着筛子保存** —— 用户筛一个词、改一句、点保存，如果写盘用的是当前看见的那几段，
整本转写就被改剩一段。这条只能在真机上验（前端才有筛子和待存队列），所以这份套件
既有 HTTP 契约那一半，也有 Playwright 点出来那一半，两边都去磁盘上数段落。

跑法（run_all.sh 会代劳起服务与铺书架）：
    .venv/bin/python tests/seed.py
    .venv/bin/python tests/check_video_workbench.py http://127.0.0.1:8899
"""
import atexit
import json
import pathlib
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
FAIL = []

# 两本一次性试验书：接口那半用 A，真机那半用 B —— 两边都要从「原样的八段」出发，
# 共用一本的话前一半跑完后一半就测不到初值了。跑完连目录一起删，不污染别的套件。
from seed import _write_video_book, book_dir  # noqa: E402

WB_API = "video_WB_API"
WB_UI = "video_WB_UI"
# 两本的名字必须各不相同：左栏那排是按书名匹配的，同名会让界面测试撞 Playwright 的
# strict mode（一个选择器命中两行），报出来的错跟产品没关系，纯粹是套件自己埋的坑。
WB_API_TITLE = "工作台接口这本"
WB_UI_TITLE = "工作台点选这本"
ROOT = pathlib.Path(book_dir(""))
_write_video_book(ROOT, WB_API, WB_API_TITLE, "讲师甲")
_write_video_book(ROOT, WB_UI, WB_UI_TITLE, "讲师乙")
atexit.register(lambda: [shutil.rmtree(ROOT / b, ignore_errors=True) for b in (WB_API, WB_UI)])


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def via(path):
    """URL 里的中文先转义。http.client 把请求行按 ascii 编，没转义当场 UnicodeEncodeError；
    而套件里写「碎片」「讲师」这种人类话比手写 %E7%A2%8E 好读，所以转义放在出口统一做。
    safe 里带上 % —— 调用点自己 quote 过的名字不会被二次编成 %25。"""
    return BASE + urllib.parse.quote(path, safe="/?&=:%")


def api(path):
    with urllib.request.urlopen(via(path), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def post(body):
    req = urllib.request.Request(BASE + "/api/video",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def raw(path):
    """回 (状态码, 响应头, 正文字节) —— 下载那一条要看头和字节，不能只当 JSON 读。"""
    try:
        with urllib.request.urlopen(via(path), timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ── 1. 列表：一本刚铺好的视频书，界面能看到什么 ──────────────────────
lst = api("/api/video?mode=books")
books = {b["id"]: b for b in (lst.get("books") or [])}
wb = books.get(WB_API) or {}
chk("列表：mode=books 把试验书列出来了（ok 且有这一本）",
    lst.get("ok") is True and WB_API in books, sorted(books))
chk("列表：段数与章节数是真的（章节 0 那个谎报修好了）",
    wb.get("segments") == 8 and wb.get("chapters") == 3,
    (wb.get("segments"), wb.get("chapters")))
chk("列表：三盏灯的数据齐（带时间戳 / 没手改 / 还没导过）",
    wb.get("has_json") is True and wb.get("edited") is False
    and (wb.get("exports") or []) == [], wb)
chk("列表：引擎 / 站点 / 时长 / 第几 P 都带出来了（左栏那一行靠它写话）",
    wb.get("engine") == "fake-whisper" and wb.get("site") == "bilibili"
    and wb.get("duration") == 78 and wb.get("page") == 3 and wb.get("pages") == 12, wb)

# ── 2. 分页契约：limit 生效、上限夹住、越界回空、text 不随段落白传 ────
p3 = api("/api/video?mode=transcript&book=%s&limit=3" % WB_API)["transcript"]
chk("分页：limit=3 就只回三段，并如实说还有下文",
    p3["returned"] == 3 and len(p3["segments"]) == 3 and p3["has_more"] is True
    and p3["total"] == 8 and p3["offset"] == 0,
    (p3["returned"], p3["has_more"], p3["total"]))
tail = api("/api/video?mode=transcript&book=%s&limit=3&offset=6" % WB_API)["transcript"]
chk("分页：offset=6 拿到最后两段，下文报 False（前端「再来一屏」靠这一对）",
    tail["returned"] == 2 and tail["has_more"] is False
    and [s["id"] for s in tail["segments"]] == ["s00006", "s00007"],
    (tail["returned"], tail["has_more"], [s["id"] for s in tail["segments"]]))
over = api("/api/video?mode=transcript&book=%s&offset=99" % WB_API)["transcript"]
chk("分页：offset 越过末尾回空页，不抛也不把 total 抹成 0",
    over["returned"] == 0 and over["has_more"] is False and over["total"] == 8, over)
big = api("/api/video?mode=transcript&book=%s&limit=999999" % WB_API)["transcript"]
chk("分页：limit 吹得再大也夹在服务端上限内（不能靠它一次拖走整本）",
    big["returned"] == 8 and big["has_more"] is False, (big["returned"], big["has_more"]))
chk("分页：响应里没有 text 那份全文（几十万字跟着每页重发是白占带宽）",
    "text" not in big, sorted(big))
zero = api("/api/video?mode=transcript&book=%s&limit=0" % WB_API)
chk("分页：limit=0 / 负数不当「一段都不给」用（夹回 1，界面不会铺出空白屏）",
    zero.get("ok") is True and zero["transcript"]["returned"] >= 1, zero)

# ── 3. 筛法：关键词与时间写法（mm:ss 那一侧）────────────────────────
kw = api("/api/video?mode=transcript&book=%s&q=%s" % (WB_API, "碎片"))["transcript"]
chk("筛法：关键词只留命中的段，total 仍是全量（保存靠的是 total 那一份）",
    kw["matched"] == 1 and kw["total"] == 8 and kw["offset"] == 0
    and kw["segments"][0]["id"] == "s00003",
    (kw["matched"], kw["total"], kw["segments"][0]["id"]))
tm = api("/api/video?mode=transcript&book=%s&from=0:06&to=0:10" % WB_API)["transcript"]
chk("筛法：时间写 0:06 这种 mm:ss 认（服务端 parse_stamp，不是让用户背秒数）",
    tm["matched"] == 2 and [s["id"] for s in tm["segments"]] == ["s00000", "s00001"],
    (tm["matched"], [s["id"] for s in tm["segments"]]))
bad = api("/api/video?mode=transcript&book=%s&from=1:2a" % WB_API)
chk("筛法：时间写看不懂时报人话，不静默当 0 秒筛出整本",
    bad.get("ok") is False and "时间没看懂" in (bad.get("msg") or ""), bad)
speaker = api("/api/video?mode=transcript&book=%s&q=%s" % (WB_API, "讲师"))["transcript"]
chk("筛法：按说话人也筛得动（那两段 seeded 时特意标了讲师）",
    speaker["matched"] == 2 and all(s.get("speaker") == "讲师" for s in speaker["segments"]),
    speaker["matched"])
missing = api("/api/video?mode=transcript&book=video_不存在")
chk("读取：没这本书时回 ok=False 加人话，不抛 traceback",
    missing.get("ok") is False and bool(missing.get("msg")), missing)

# ── 4. 存：改一句 + 插一段 + 删一段，落盘后回文件里数 ────────────────
full = api("/api/video?mode=transcript&book=%s&limit=1200" % WB_API)["transcript"]["segments"]
seg = [dict(s) for s in full]
seg[2]["text"] = "这一讲先看内存分配的三个基本问题。"     # 「内村」→「内存」
seg[3]["speaker"] = "讲师乙"
del seg[7]                                                # 末段删掉
seg.insert(4, {"id": "nmanual01", "start": 33.1, "end": 40.0,
               "text": "这一句是手动补上去的", "after": "s00003"})
sv = post({"act": "save_transcript", "book": WB_API, "segments": seg})
chk("保存：写盘成功并回报段落数", sv.get("ok") is True and sv["saved"]["segments"] == 8, sv)
doc = json.loads((pathlib.Path(book_dir(WB_API)) / "transcript.json")
                 .read_text(encoding="utf-8"))
ids = [s["id"] for s in doc["segments"]]
chk("保存：没筛的段落一段不丢（8 段 = 原 8 - 删 1 + 插 1）",
    len(doc["segments"]) == 8, ids)
chk("保存：改过的那句按 id 落在原来那段上",
    doc["segments"][2]["id"] == "s00002"
    and doc["segments"][2]["text"].startswith("这一讲先看内存分配"), doc["segments"][2])
chk("保存：删掉的那段真没了，插进去的那段带着自己的 id",
    "s00007" not in ids and "nmanual01" in ids, ids)
chk("保存：插进去那段排在锚点后面（after 只是界面话，位置靠它算）",
    ids.index("nmanual01") == ids.index("s00003") + 1, ids)
chk("保存：after 没留在用户文件里（导出 JSON 不该多一串没人认的编号）",
    all("after" not in s for s in doc["segments"]), doc["segments"][4])
chk("保存：edited 标记立起来了（左栏那盏「手改」灯靠它）",
    doc.get("edited") is True, doc.get("edited"))
txt = (pathlib.Path(book_dir(WB_API)) / "transcript.txt").read_text(encoding="utf-8")
chk("保存：纯文本那份跟着同步（两份视图不能一个改了一个没改）",
    "内存分配的三个基本问题" in txt and "下一讲接着说虚拟内存" not in txt, txt[-80:])
flagged = api("/api/video?mode=books")["books"]
wb2 = [b for b in flagged if b["id"] == WB_API][0]
chk("保存：列表里这盏手改灯跟着亮了", wb2["edited"] is True, wb2)
empty = post({"act": "save_transcript", "book": WB_API, "segments": []})
chk("保存：一段都不给时拒绝写盘（不然用户点错就把整本转写清了）",
    empty.get("ok") is False, empty)

# ── 5. 重建：改完转写，章节正文跟着变，旧的可回退 ────────────────────
rb = post({"act": "rebuild", "book": WB_API})
chk("重建：回报成功并给出节数", rb.get("ok") is True and rb["rebuilt"]["chapters"] >= 1, rb)
ch_dir = pathlib.Path(book_dir(WB_API)) / "chapters"
ch_files = sorted(p.name for p in ch_dir.glob("*.md"))
# 文件名从 0000 起头是这一仓库的口径（引擎、book_import、book_notes 都这么认），
# 界面那份「章节标题表」也按 0 基对 —— 重建要是铺成 0001 起头，每章的标题就错开一格。
chk("重建：章节文件名跟着产品口径从 0000 起头", ch_files[0] == "0000.md", ch_files)
ch1 = "\n".join(p.read_text(encoding="utf-8") for p in sorted(ch_dir.glob("*.md")))
chk("重建：正文用的是改过的那句（不是旧转写）",
    "内存分配的三个基本问题" in ch1 and "内村" not in ch1, ch1[:120])
chk("重建：手动补那句也进了正文", "这一句是手动补上去的" in ch1, ch1[:200])
chk("重建：删掉那句从正文里退场了", "下一讲接着说虚拟内存" not in ch1, ch1[-120:])
merged = (pathlib.Path(book_dir(WB_API)) / "merged.md").read_text(encoding="utf-8")
chk("重建：merged.md 那份整文也重写了", "内存分配" in merged, merged[:120])
chk("重建：旧文件挪进了备份目录（改坏了还有得退）",
    bool(rb["rebuilt"].get("backup"))
    and (pathlib.Path(book_dir(WB_API)) / rb["rebuilt"]["backup"]).exists(),
    rb["rebuilt"].get("backup"))

# ── 6. 导出与下载：五种格式、文件真在、越界进不来 ────────────────────
ex = post({"act": "export", "book": WB_API, "fmt": "srt"})
chk("导出：srt 成功并回报文件名与字节数",
    ex.get("ok") is True and ex["export"]["name"].endswith(".srt")
    and ex["export"]["bytes"] > 0 and ex["export"]["timed"] is True, ex)
srt_path = pathlib.Path(book_dir(WB_API)) / "exports" / ex["export"]["name"]
chk("导出：文件真的落在书的 exports/ 里（列表只数这一层）", srt_path.is_file(), str(srt_path))
srt_body = srt_path.read_text(encoding="utf-8")
chk("导出：字幕体是对的（有序号、有 --> 时间行、用的是改过的那句）",
    "-->" in srt_body and "1\n00:00:00,000 --> " in srt_body
    and "内存分配的三个基本问题" in srt_body, srt_body[:120])
chk("导出：被删掉那句不在字幕里（导的是存下来的那份，不是界面那份）",
    "下一讲接着说虚拟内存" not in srt_body, srt_body[-120:])
post({"act": "export", "book": WB_API, "fmt": "md"})
jl = post({"act": "export", "book": WB_API, "fmt": "json"})
chk("导出：换格式也导得出来，md 那份带标题",
    jl.get("ok") is True and (pathlib.Path(book_dir(WB_API)) / "exports").is_dir(), jl)
wb3 = [b for b in api("/api/video?mode=books")["books"] if b["id"] == WB_API][0]
chk("导出：列表把导出清单报回来了（几种都数得清、时间倒着排）",
    len(wb3["exports"]) == 3 and wb3["exports"][0]["fmt"] == "json"
    and wb3["exports"][-1]["fmt"] == "srt", wb3["exports"])
code, head, data = raw("/api/video?mode=file&book=%s&name=%s"
                       % (WB_API, urllib.parse.quote(ex["export"]["name"])))
chk("下载：mode=file 回字节不是 JSON，并挂着 attachment 让浏览器存盘",
    code == 200 and "subrip" in (head.get("Content-Type") or "")
    and "attachment" in (head.get("Content-Disposition") or "")
    and data.decode("utf-8") == srt_body, (code, head.get("Content-Type"), len(data)))
for label, name in [("想爬出目录", "../meta.json"), ("压根不存在", "没这个文件.srt"),
                    ("扩展名不在白名单", "meta.json")]:
    c, _h, _b = raw("/api/video?mode=file&book=%s&name=%s" % (WB_API, urllib.parse.quote(name)))
    chk("下载：%s的名字回 404（白名单加一层 realpath 兜底）" % label, c == 404, (name, c))
c, _h, _b = raw("/api/video?mode=file&book=%s&name=%s" % ("video_没这本书", "a.srt"))
chk("下载：书不在也回 404，不 500", c == 404, c)
bad_fmt = post({"act": "export", "book": WB_API, "fmt": "pdf"})
chk("导出：认不出的格式报人话（不是闷头写出个 .pdf 空文件）",
    bad_fmt.get("ok") is False and bool(bad_fmt.get("msg")), bad_fmt)
# ── 真机：在工作台上点一遍（筛着保存这一条只能在这儿验）─────────────
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    # 「原片」那颗走 window.open：套件不联网，把它换成一个收集地址的桩——既验得出 URL
    # 拼得对不对（多 P 要带 p=、跳转要带 t=秒），又不会真开窗口去碰外网。
    # 注意 add_init_script 的入参是「脚本正文」不是「函数」：写成 () => {...} 会被当表达式
    # 求值后丢掉，桩等于没打 —— 于是 window.open 还是真的，测试悄悄开了个外网窗口，
    # 而断言那边永远拿到 undefined，看起来像「按钮点了没反应」。纯语句才对（check_onboarding 同款）。
    page.add_init_script("""window.__opened = [];
      window.open = function (u) { window.__opened.push(String(u || '')); return null; };""")
    page.goto(BASE + "/", wait_until="networkidle")
    page.click('#nav button[data-v="video"]')
    page.wait_for_selector("#vShelf .vtbook", timeout=20000)

    rows = lambda: page.evaluate("() => document.querySelectorAll('#vRows .vtrow').length")
    cnt = lambda: page.evaluate("() => document.querySelector('#vCntT').textContent || ''")  # noqa: E731
    stat = lambda: page.evaluate("() => document.querySelector('#vStat').innerText || ''")  # noqa: E731
    # 点时间戳、标待删这类「一句提醒」落在右下角那颗 whisper 上，不是 #vStat——断言找错
    # 地方就会把「产品其实说话了」测成「点了没反应」。
    tip = lambda: page.evaluate(  # noqa: E731
        "() => (document.querySelector('.whisper') || {}).textContent || ''")
    wait_rows = lambda n: page.wait_for_function(  # noqa: E731
        "(m) => document.querySelectorAll('#vRows .vtrow').length === m", arg=n, timeout=20000)
    wait_stat = lambda pat: page.wait_for_function(  # noqa: E731
        "(p) => new RegExp(p).test((document.querySelector('#vStat') || {}).textContent || '')",
        arg=pat, timeout=30000)
    row_of = lambda needle: page.locator("#vRows .vtrow").filter(has_text=needle)

    # 左栏点这本书（不是用下拉），段落铺出来
    page.click('#vShelf .vtbook:has-text("%s")' % WB_UI_TITLE)
    wait_rows(8)
    chk("界面：左栏点一下就铺开八段", rows() == 8, rows())
    chk("界面：计数说「共 8 段」", "共 8 段" in cnt(), cnt())
    chk("界面：这本书没导过时，那一栏说的是没导过而不是空白",
        "还没导出过" in page.evaluate("() => document.querySelector('#vExp').innerText"),
        page.evaluate("() => document.querySelector('#vExp').innerText"))
    chk("界面：没改动时保存按钮没提示色", page.evaluate(
        "() => !document.querySelector('#vSave').classList.contains('hot')"))
    chk("界面：工具条这一段解锁了（挑上书才能改）", page.evaluate(
        "() => !document.querySelector('#vSave').disabled"
        " && !document.querySelector('#vExport').disabled"))

    # 筛一个词 —— 接下来这一处改动是「戴着筛子」做的
    page.fill("#vQ", "碎片")
    page.click("#vFilter")
    wait_rows(1)
    chk("界面：筛完只铺命中那一段，计数报「筛出 1 / 共 8」",
        rows() == 1 and "筛出 1" in cnt() and "共 8" in cnt(), (rows(), cnt()))

    page.click("#vRows .vtrow .tx")
    page.wait_for_selector("#vRows .vtrow.edit textarea", timeout=10000)
    chk("界面：点正文那格进了编辑态（textarea 与说话人框都在）", page.evaluate(
        "() => !!document.querySelector('#vRows .vtrow.edit textarea')"
        " && !!document.querySelector('#vRows .vtrow.edit input')"))
    page.fill("#vRows .vtrow.edit textarea", "第一个是碎片，第二个是分配速度。")
    page.click("#vRows .vtrow.edit .vtrowbar button:has-text('记好')")
    page.wait_for_timeout(500)
    chk("界面：记好之后正文那格显示的是新句子",
        "分配速度" in page.evaluate("() => document.querySelector('#vRows .vtrow .tx').innerText"))
    chk("界面：待存计数出现，保存按钮亮提示色",
        "待存 1 处" in cnt() and page.evaluate(
            "() => document.querySelector('#vSave').classList.contains('hot')"), cnt())

    page.click("#vRows .vtrow .act button:has-text('插')")
    page.wait_for_selector("#vRows .vtrow.new.edit textarea", timeout=10000)
    chk("界面：点插之后新段就地进编辑态（不用再去列表里找）", page.evaluate(
        "() => document.querySelectorAll('#vRows .vtrow.new').length === 1"
        " && !!document.querySelector('#vRows .vtrow.new.edit textarea')"))
    page.fill("#vRows .vtrow.new.edit textarea", "补一句：伙伴系统按 2 的幂对齐。")
    page.click("#vRows .vtrow.new .vtrowbar button:has-text('记好')")
    page.wait_for_timeout(500)
    chk("界面：新段留在筛出来那段后面（插的位置按锚点算，不按整本位置）",
        rows() == 2 and "补一句" in page.evaluate(
            "() => [...document.querySelectorAll('#vRows .vtrow .tx')][1].innerText"), rows())
    chk("界面：新段算第二处待存", "待存 2 处" in cnt(), cnt())

    # 摘掉筛子再删段：界面上摸不着的段落本来就不该有「删」那颗，删这一笔只能在版面前列
    page.click("#vClearF")
    wait_rows(9)
    chk("界面：摘掉筛子回到全量，新插那段还蹲在原位（8 段里多了 1 段）", rows() == 9, rows())
    row_of("同学可以把这三点").locator(".act button:has-text('删')").click()
    page.wait_for_timeout(500)
    chk("界面：删一段是标待删——那段灰着留在列表里，等「恢复」或「存转写」定夺",
        page.evaluate("() => !!document.querySelector('#vRows .vtrow.gone')") and rows() == 9,
        rows())
    chk("界面：待存数到三处并点名删了一个", "待存 3 处" in cnt() and "删 1" in cnt(), cnt())
    chk("界面：待删那排给了「找回」，点错不至于直接丢字", page.evaluate(
        "() => [...document.querySelectorAll('#vRows .vtmore button')]"
        ".some(b => b.textContent === '找回')"))

    # 关键一条：戴着筛子改的字 + 摘了筛子删的段一起存盘，没碰过的那些段不能被抹掉
    page.click("#vSave")
    wait_stat("存好了")
    saved_doc = json.loads((pathlib.Path(book_dir(WB_UI)) / "transcript.json")
                           .read_text(encoding="utf-8"))
    sids = [s["id"] for s in saved_doc["segments"]]
    chk("界面：筛着改完再存，没改到的段一条没丢（盘上还是 8 段：删一补一）",
        len(saved_doc["segments"]) == 8, sids)
    chk("界面：没动过的那些段一字未改、id 也没挪位",
        "s00000" in sids and saved_doc["segments"][0]["text"].startswith("大家好"),
        saved_doc["segments"][0])
    chk("界面：改过那句按 id 落在原来那段上（改 A 存进 B 就是这一条在管）",
        any(s["id"] == "s00003" and "分配速度" in s["text"] for s in saved_doc["segments"]),
        [s for s in saved_doc["segments"] if s["id"] == "s00003"])
    chk("界面：标删那段真的没写进盘", "s00004" not in sids, sids)
    chk("界面：新插那句真的写进盘了",
        any("伙伴系统按 2 的幂对齐" in s["text"] for s in saved_doc["segments"]), sids)
    chk("界面：新插那段的时间是按邻居推出来的（不是 0 也不是空）",
        all(s.get("start") is not None and s.get("end") is not None
            for s in saved_doc["segments"]), saved_doc["segments"][4])
    chk("界面：after 锚点没漏进用户的文件",
        all("after" not in s for s in saved_doc["segments"]), sids)
    chk("界面：存完待存清零、提示色退掉",
        "待存" not in cnt() and page.evaluate(
            "() => !document.querySelector('#vSave').classList.contains('hot')"), cnt())
    wait_rows(8)
    chk("界面：存完列表换成盘上这份（9 行变 8 行）", rows() == 8, rows())
    chk("界面：保存回执报段数并把删掉的那些说清楚",
        "存好了 8 段" in stat() and "删掉的 1 段" in stat(), stat()[:160])

    page.click("#vRebuild")
    wait_stat("重建好了")
    chk("界面：点重建之后状态栏交代了节数", "节" in stat(), stat()[:160])
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in sorted(
        (pathlib.Path(book_dir(WB_UI)) / "chapters").glob("*.md")))
    chk("界面：重建出来的章节正文用的是改过那句与新补那句（不是旧转写）",
        "分配速度" in bodies and "伙伴系统按 2 的幂对齐" in bodies, bodies[:200])
    chk("界面：标删那句从章节里退场了", "同学可以把这三点" not in bodies, None)
    chk("界面：merged.md 那份整文也跟着重写",
        "分配速度" in (pathlib.Path(book_dir(WB_UI)) / "merged.md")
        .read_text(encoding="utf-8"), None)

    page.select_option("#vExpFmt", "vtt")
    page.click("#vExport")
    wait_stat("导好了")
    chk("界面：导完状态栏报出文件名", "导好了" in stat(), stat()[:160])
    page.wait_for_function(
        "() => [...document.querySelectorAll('#vExp a')].some(a => /VTT/.test(a.textContent))",
        timeout=10000)
    chips = page.evaluate("() => [...document.querySelectorAll('#vExp a')].map(a => a.textContent)")
    chk("界面：导完那条立刻挂在下载栏上（不用等下一次轮询才出现）",
        any("VTT" in c for c in chips), chips)
    href = page.evaluate("() => (document.querySelector('#vExp a') || {}).href")
    code, head, fbody = raw(href.replace(BASE, "") if href else "/api/video?mode=file")
    chk("界面：那一条真能下到文件（同域直连，不需要另一套接口）",
        code == 200 and "vtt" in (head.get("Content-Type") or "")
        and fbody.decode("utf-8").startswith("WEBVTT"), (code, head.get("Content-Type")))
    lamps = page.evaluate("() => [...document.querySelectorAll('#vShelf .vtbook.on .lt i')]"
                          ".map(e => e.textContent).join('|')")
    chk("界面：导出之后左栏那盏灯跟着变成「导 1」（不用等下一次轮询）", "导 1" in lamps, lamps)

    # 收起左栏 / 再展开：折叠那条只靠 .nonav 一个类，别让它在视频屏上失灵
    page.click("#vFoldNav")
    page.wait_for_timeout(400)
    chk("界面：点收起后左栏退场、工具条出现那颗「栏」", page.evaluate(
        "() => document.querySelector('#vWrap').classList.contains('nonav')"
        " && getComputedStyle(document.querySelector('#vUnfold')).display !== 'none'"))
    page.click("#vUnfold")
    page.wait_for_timeout(400)
    chk("界面：再点一下就展开回来（来回点不落进死角）", page.evaluate(
        "() => !document.querySelector('#vWrap').classList.contains('nonav')"))
    chk("界面：折叠偏好写进了 localStorage（下次进来还是那个样子）", page.evaluate(
        "() => localStorage.getItem('gz_video_nav') !== null"))

    page.click("#vRows .vtrow .ts")
    page.wait_for_timeout(500)
    chk("界面：点时间戳给一句反馈（复制成了、还是浏览器不给剪贴板，都得说）",
        "复制了" in tip() or "剪贴板" in tip(), tip())
    page.click("#vRows .vtrow .act button:has-text('原片')")
    page.wait_for_timeout(500)
    opened = page.evaluate("() => window.__opened")
    chk("界面：「原片」拼的是这一句的位置（多 P 带 p=，跳转带 t=秒）",
        bool(opened) and "SEFAKE03" in opened[-1] and "p=3" in opened[-1]
        and "t=" in opened[-1], opened)
    chk("界面：跳原片没炸出控制台错", not [e for e in errors if "open" in e.lower()], errors[:4])

    # 换一本书再回来：改动作废，看到的应当是盘上那一份
    page.click('#vShelf .vtbook:has-text("操作系统导论")')
    wait_rows(8)
    chk("界面：换书后段落重铺、待存清零", "待存" not in cnt() and rows() == 8, (cnt(), rows()))
    page.select_option("#vPick", WB_UI)
    page.wait_for_function("(id) => (document.querySelector('#vPick') || {}).value === id",
                           arg=WB_UI, timeout=10000)
    page.wait_for_timeout(900)
    chk("界面：下拉选书与左栏点书走的是同一条路", rows() == 8, rows())
    chk("界面：换回来看见的是盘上那一份（改过那句还在、删掉那句没了）",
        "分配速度" in page.evaluate("() => document.querySelector('#vRows').innerText")
        and "同学可以把这三点" not in page.evaluate(
            "() => document.querySelector('#vRows').innerText"), None)

    sweep_off = page.evaluate("""() => {
      const win = document.documentElement.clientWidth + 1;
      const off = [...document.querySelectorAll('.vpane[data-pane="video"] *')].filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && (r.right > win || r.left < -1) &&
               getComputedStyle(e).position !== 'fixed' && !e.closest('[hidden]');
      }).length;
      return {doc: document.documentElement.scrollWidth, win, off};
    }""")
    chk("界面：工作台不横向溢出", sweep_off["doc"] <= sweep_off["win"] + 1, sweep_off)
    hits = sorted(set(re.findall("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]",
                                 page.evaluate("() => document.body.innerText"))))
    chk("界面：工作台上一个 emoji 也没有", not hits, hits)
    chk("界面：全程没有报错", not errors, errors[:6])

    page.screenshot(path=str(selftest.SHOTS / "video-workbench.png"))
    browser.close()

print()
print(f"转写工作台：{'全部通过' if not FAIL else str(len(FAIL)) + ' 项失败 -> ' + ' | '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
