# -*- coding: utf-8 -*-
"""flomo 便签这一格的后端全套离线自测：解析 → 去重 → 附件 → 筛 → 收成书 → 记忆画像。

为什么单独一份、并且不起服务：flomo_notes.py 只碰磁盘不碰网络，喂它一段 HTML 就
该给一批条目，所以这些规矩都能在没浏览器、没服务的情况下钉死。真机那一屏归
tests/check_flomo_ui.py。

夹具在 tests/flomo_fixture.py（结构对着 flomo 官方导出的形状，真机那一屏也用它）：
**里面每一条正文都是现编的**，绝不把用户真实的笔记内容写进仓库 —— 这条线一旦松了，GitHub 上就挂着某人的
私人日记。所以这里也专门钉一条：记忆画像里不许出现任何一条笔记原文。

盯的都是会真出事的地方：
  · 解析器跟着 div 深度走（正则切块会在一处嵌套就错位、啃掉后面所有条目）；
  · 重复导同一份不许翻倍、按标签导的那份要能把图补给已有条目；
  · 包内路径与 HTML 里 src 差一层目录（「图存下来了但笔记里看不见」那个 bug）；
  · 标签前缀只往下算（选「SOP」得能看见「SOP/家务」，选叶子不许把只打了根标签的拽进来）；
  · 账本坏了要当空的读，不许把「重新导入」这条路挡死。
"""
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # 夹具与套件同目录
sys.path.insert(0, str(HERE.parent))   # 被测模块在仓库根
import flomo_notes as fn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 夹具：见 tests/flomo_fixture.py（两份套件共用，正文全是现编的）──
from flomo_fixture import (IMG_A_IN_HTML, IMG_A_IN_ZIP, N_MEMOS, PNG_A,  # noqa: E402
                           export_html, export_zip, memo_html)


ROOT = tempfile.mkdtemp(prefix="gz-flomo-")
D = os.path.join(ROOT, "flomo")          # 相当于 ui_server 里的 cache/flomo/


# ── A：解析 ──────────────────────────────────────────────────────
parsed = fn.parse_html(export_html())
chk("A1 一条不少、顺序原样（%d 条）" % N_MEMOS, len(parsed) == N_MEMOS, len(parsed))
chk("A2 日期与时分秒分开存（时间字段完整）",
    parsed[0]["date"] == "2026-08-29" and parsed[0]["clock"] == "07:12:03", parsed[0])
chk("A3 同一天的三条 ts 互不相等（列表按天分组、卡片要精确到秒）",
    len({fn.parse_html(memo_html(i))[0]["ts"] for i in (0, 1, 2)}) == 3)
chk("A4 正文分段之间空一行", parsed[0]["md"].count("\n\n") == 1, parsed[0]["md"])
chk("A5 列表项之间只换行、不空行（否则两条各成一个列表，行距宽一倍）",
    "- 买米\n- 换滤芯" in parsed[1]["md"], parsed[1]["md"])
chk("A6 加粗保留、mark 折成加粗、code 用反引号",
    "**复利**" in parsed[2]["md"] and "**高亮**" in parsed[2]["md"]
    and "`代码`" in parsed[2]["md"], parsed[2]["md"])
chk("A7 br 折成换行、blockquote 带前缀",
    "第二行\n第三行" in parsed[4]["md"] and "\n> " in parsed[4]["md"], parsed[4]["md"])
chk("A8 正文里套标签不吞掉后面的条目",
    len(fn.parse_html(memo_html(5) + memo_html(6))) == 2
    and "里面这句不该被吞掉" in parsed[5]["md"], parsed[5]["md"])
chk("A9 标签抽出并保留层级",
    parsed[0]["tags"] == ["灵感/写作"] and parsed[4]["tags"] == ["SOP/flomo"],
    (parsed[0]["tags"], parsed[4]["tags"]))
chk("A10 纯数字那种序号不当标签（#3）", "3" not in parsed[2]["tags"], parsed[2]["tags"])
chk("A11 files 那层的图收进 imgs", parsed[3]["imgs"] == [IMG_A_IN_HTML], parsed[3]["imgs"])
chk("A12 字数按「去掉空白与 Markdown 记号」数（跟剪藏同一把尺）",
    fn.words_of("abc def") == 6 and fn.words_of("#读书 一句话") == 5
    and parsed[0]["words"] > 10, (fn.words_of("abc def"), fn.words_of("#读书 一句话")))
chk("A13 id 稳定：同时间同正文再解一次还是同一个（重复导入靠它认人）",
    [m["id"] for m in parsed] == [m["id"] for m in fn.parse_html(export_html())])
chk("A14 正文相同、时间不同 → 当成新的一条（不许悄悄覆盖）",
    parsed[0]["id"] != parsed[N_MEMOS - 1]["id"])
chk("A15 一条里重复打的标签去重",
    fn.extract_tags("同一句 #读书 又来了 #读书") == ["读书"], fn.extract_tags("同一句 #读书 又来了 #读书"))
chk("A16 超长正文被掐住（坏 HTML 把整页当一条时的闸）",
    len(fn.parse_html('<div class="memo"><div class="time">2026-01-01 00:00:00</div>'
                      '<div class="content"><p>%s</p></div></div>' % ("字" * (fn.MAX_MD + 900))
                      )[0]["md"]) <= fn.MAX_MD)
chk("A17 标签尾上的中文标点被摘掉（「#读书。」不该把句号带进标签）",
    fn.extract_tags("结尾打个标签 #读书。") == ["读书"], fn.extract_tags("结尾打个标签 #读书。"))
chk("A18 行首的 # 是标题不是标签（flomo 里也不当标签）",
    fn.extract_tags("# 一级标题") == [])

# ── B：认包 ──────────────────────────────────────────────────────
text, files = fn.read_export(export_zip(), "flomo-export.zip")
chk("B1 zip 里认到笔记那一页", '<div class="memo">' in text)
chk("B2 附件按包内路径收齐（去重前 3 条）", len(files) == 3, sorted(files))
t2, f2 = fn.read_export(export_html().encode("utf-8"), "notes.html")
chk("B3 单文件 HTML 也能读", '<div class="memo">' in t2 and f2 == {})


class _Info:
    def __init__(self, name, flag):
        self.filename, self.flag_bits = name, flag


mojibake = "flomo@demo/file/2026-09-01/笔记.png".encode("utf-8").decode("cp437")
chk("B4 没打 UTF-8 标记的条目名按 cp437 兜回来（中文目录名不裂）",
    fn._fix_zip_name(_Info(mojibake, 0)) == "flomo@demo/file/2026-09-01/笔记.png",
    fn._fix_zip_name(_Info(mojibake, 0)))
chk("B5 打了 UTF-8 标记的原样给",
    fn._fix_zip_name(_Info("flomo@demo/file/笔记.png", 0x800)) == "flomo@demo/file/笔记.png")
try:
    fn.read_export(b"PK\x03\x04not a zip at all", "bad.zip")
    chk("B6 打不开的包回一句人话", False, "没抛错")
except ValueError as e:
    chk("B6 打不开的包回一句人话", "zip" in str(e), str(e))
try:
    fn.read_export(b"\x89PNG\r\n\x1a\n", "pic.png")
    chk("B7 不认识的类型说清要什么", False, "没抛错")
except ValueError as e:
    chk("B7 不认识的类型说清要什么", "flomo" in str(e), str(e))
try:
    fn.read_export(b"", "x.zip")
    chk("B8 空内容直接说没收到", False, "没抛错")
except ValueError as e:
    chk("B8 空内容直接说没收到", "没有收到" in str(e), str(e))

# ── C：导入与附件 ────────────────────────────────────────────────
r1 = fn.import_notes(D, export_zip(), "flomo-export.zip")
chk("C1 首导：%d 条全进、0 条已存在" % N_MEMOS,
    r1["added"] == N_MEMOS and r1["existed"] == 0 and r1["total"] == N_MEMOS, r1)
chk("C2 图片按「真落了几张」报数（一张图占好几个键不算三张）", r1["atts"] == 2, r1)
stored = sorted(os.listdir(fn.att_dir(D)))
chk("C3 同一张图本机只落一份（内容哈希命名）", len(stored) == 2, stored)
data = fn.load(D)
m_img = fn.one(data, parsed[3]["id"])
chk("C4 包内路径与 HTML 里 src 差一层目录也接得上图（「存下来了但看不见」那个 bug）",
    len(m_img["atts"]) == 1 and m_img["atts"][0].endswith(".png"), m_img)
chk("C5 落盘文件名就是内容哈希（换条路径也认得回同一张图）",
    m_img["atts"][0] == fn.att_name(IMG_A_IN_HTML, PNG_A), m_img["atts"])
chk("C6 两种写法都能查到同一个本机名（att_keys 那层兼容）",
    fn.att_keys(IMG_A_IN_ZIP) and all(
        k in fn.att_keys(IMG_A_IN_ZIP) for k in [IMG_A_IN_HTML, "a.png"]),
    fn.att_keys(IMG_A_IN_ZIP))

r2 = fn.import_notes(D, export_zip(), "flomo-export.zip")
chk("C7 重复导同一份：一条不加、一条不改",
    r2["added"] == 0 and r2["existed"] == N_MEMOS and r2["total"] == N_MEMOS, r2)

r3 = fn.import_notes(D, export_html(with_images=False), "by-tag.html")
chk("C8 按标签导的那份（不含附件目录）：条目都在、正文不动",
    r3["added"] == 0 and r3["total"] == N_MEMOS, r3)
r4 = fn.import_notes(D, export_zip(), "full.zip")
chk("C9 图本来就在账上的：再导一次不重复贴、也不虚报「补了几张」",
    r4["patched"] == 0 and r4["added"] == 0 and len(fn.one(fn.load(D), m_img["id"])["atts"]) == 1,
    r4)

# 用户真实的顺序常常反过来：先按标签导一份（那份不含附件目录），过些天再导全量。
# 这时同一条笔记靠哈希认得出是自己人，图就该补上去，正文一个字不许动。
D2 = os.path.join(ROOT, "flomo2")
q1 = fn.import_notes(D2, export_html(with_images=False), "by-tag.html")
chk("C10 先导按标签那份：%d 条进账、一条都没带图" % N_MEMOS,
    q1["added"] == N_MEMOS and q1["atts"] == 0
    and all(not m.get("atts") for m in fn.load(D2)["memos"]), q1)
q2 = fn.import_notes(D2, export_zip(), "full.zip")
chk("C11 之后补导全量：两条带图的各补一张（patched 报的是补上的张数）",
    q2["patched"] == 2 and q2["added"] == 0 and q2["total"] == N_MEMOS, q2)
chk("C12 补图不重复贴（同一条同一张图只留一次）",
    len(fn.one(fn.load(D2), parsed[3]["id"])["atts"]) == 1,
    fn.one(fn.load(D2), parsed[3]["id"])["atts"])
chk("C13 补图不许改正文（用户可能已经给它贴过标签）",
    fn.one(fn.load(D2), parsed[3]["id"])["md"] == parsed[3]["md"])
chk("C14 再补一次还是零（不把所有图当成新补的报一遍）",
    fn.import_notes(D2, export_zip(), "full.zip")["patched"] == 0)
chk("C15 账里存的是本机哈希名，不是包内那个临时目录（下一次导出目录就变了）",
    all("/" not in a for m in fn.load(D)["memos"] for a in (m.get("atts") or [])))
chk("C16 这份包叫什么、什么时候导的，都记进账本",
    fn.load(D)["src"].endswith("full.zip") and fn.load(D)["at"] > 0, fn.load(D)["src"])
try:
    fn.import_notes(D, export_html().replace('class="memo"', 'class="x"'), "empty.html")
    chk("C17 一条都没读出来时说清楚（别只回一句「失败」）", False, "没抛错")
except ValueError as e:
    chk("C17 一条都没读出来时说清楚（别只回一句「失败」）", "一条笔记都没" in str(e), str(e))

# ── D：筛 / 翻页 / 字段 ──────────────────────────────────────────
data = fn.load(D)
chk("D1 默认倒序：最新那条在最上面",
    fn.pick(data)["memos"][0]["date"] == "2026-09-01", fn.pick(data)["memos"][0]["date"])
chk("D2 顺序能翻过来（asc 时最老的在上）",
    fn.pick(data, order="asc")["memos"][0]["date"] == "2026-08-29")
chk("D3 标签前缀匹配向下：选「SOP」看得见「SOP/家务」「SOP/flomo」",
    fn.pick(data, tag="SOP")["total"] == 2, fn.pick(data, tag="SOP")["total"])
chk("D4 只往下不往上：选叶子不许把只打了根标签的那几条拽进来",
    fn.pick(data, tag="灵感/写作")["total"] == 2
    and fn.pick(data, tag="灵感")["total"] == 3,
    (fn.pick(data, tag="灵感/写作")["total"], fn.pick(data, tag="灵感")["total"]))
chk("D5 关键字搜正文", fn.pick(data, q="复利")["total"] == 1, fn.pick(data, q="复利")["total"])
chk("D6 关键字也搜标签（搜 flomo 命中打了那个标签的那条）",
    fn.pick(data, q="flomo")["total"] >= 1)
chk("D7 搜不到就给空，不许把全表倒出来",
    fn.pick(data, q="这句一定不存在xyzzy")["total"] == 0)
pg1 = fn.pick(data, limit=4, offset=0)
pg2 = fn.pick(data, limit=4, offset=4)
chk("D8 分页：limit/offset 切片、total 始终是总数、两页不重叠",
    pg1["count"] == 4 and pg1["total"] == N_MEMOS and pg2["offset"] == 4
    and {m["id"] for m in pg1["memos"]}.isdisjoint({m["id"] for m in pg2["memos"]}),
    (pg1["count"], pg2["count"], pg1["total"]))
chk("D9 越界偏移只给空不报错", fn.pick(data, offset=9999)["count"] == 0)
pub = fn.pick(data)["memos"][0]
chk("D10 交给界面的字段齐全（时间 / 标签 / 字数 / 图 / 书 / 摘干净的正文）",
    set(pub) >= {"id", "date", "clock", "ts", "md", "plain", "tags", "words", "atts", "book"},
    sorted(pub))
chk("D11 内部键不外漏（原始图片路径 imgs 不在交付的那份里）", "imgs" not in pub)
# plain 是给「界面以外的人」准备的那一份。前端自己会摘标签（它拿不到 Python），MCP 和
# 任何别的外部读者只有这一份账 —— 真源必须在这儿出一份，否则每个读者各摘各的，
# Agent 读到的正文就比用户在屏幕上看到的多一排 #标签（正是上一轮按掉的那个毛病）。
allpub = fn.pick(data, limit=0)["memos"]
withtag = [m for m in allpub if m["tags"]]
chk("D11a 每一条交付的 plain 都不再带自己账上的标签（%d 条有标签的都干净）" % len(withtag),
    len(withtag) >= 3 and all("#" + t not in m["plain"]
                              for m in withtag for t in m["tags"]),
    [(m["id"], m["tags"], m["plain"][-36:]) for m in withtag
     if any("#" + t in m["plain"] for t in m["tags"])])
chk("D11b md 一个字都不动（id 是拿原始 md 算的，摘只发生在交付这一层）",
    all(m["md"] == fn.one(data, m["id"])["md"] for m in allpub))
seq = [m for m in allpub if "#3" in m["md"]]
chk("D11c 不在标签账上的那个「#3」在 plain 里原样留着（摘的是标签，不是井号）",
    seq and all("#3" in m["plain"] for m in seq),
    [(m["id"], m["tags"], m["plain"][-30:]) for m in seq])
tc = fn.tag_counts(data)
chk("D12 标签按用得多的排、带条数",
    bool(tc) and all(x["n"] >= y["n"] for x, y in zip(tc, tc[1:])) and tc[0]["n"] >= 1, tc[:3])
chk("D12b 上级路径也算一颗（打了 读书/神经科学 的那条，看 读书 时该在里面）",
    {x["tag"]: x["n"] for x in tc}.get("读书") == 2, tc)
# 这一条是「药丸不许说谎」：界面那颗写着几条，点下去就得筛出几条，两边共用一把尺。
chk("D12c 每一颗标签的条数 = 按它筛出的条数",
    all(x["n"] == fn.pick(data, tag=x["tag"])["total"] for x in tc),
    [(x["tag"], x["n"], fn.pick(data, tag=x["tag"])["total"]) for x in tc])
chk("D13 recent_tags 从最新往回捞且不超重",
    len(fn.recent_tags(data, 3)) <= 3)

# ── E：收成书 ────────────────────────────────────────────────────
m_short = parsed[0]
chk("E1 书名借第一行的前几个字（flomo 没有标题）",
    fn.memo_title(m_short).startswith("早上一句话的灵感"), fn.memo_title(m_short))
chk("E2 长第一行掐到 24 字并加省略号",
    fn.memo_title({"md": "字" * 60}) == "字" * 24 + "…", fn.memo_title({"md": "字" * 60}))
chk("E3 只有图没字时给一句兜底（不许叫「.md」）",
    fn.memo_title({"date": "2026-08-31", "md": "", "atts": ["x.png"]}) == "2026-08-31 的一张图")
md1 = fn.memo_markdown(m_img)
chk("E4 收成书那篇：标题 + 时间与标签那行 + 正文",
    md1.startswith("# 带图的一条") and "> 2026-08-30 06:30:10　·　#读书" in md1, md1[:140])
chk("E5 图片引用改成落盘后的 images/<本机名>",
    "images/%s" % m_img["atts"][0] in md1, md1[-120:])
# flomo 的导出把标签就写在正文末尾。收成书时开头那行已经列过一遍了，正文里再留一份，
# 一本书里同一个标签就出现三次（书名、那行时间戳、正文）—— 一眼就是没清干净。
chk("E5a 书名不带末尾那个标签（标签在那行时间戳里露一次就够）",
    fn.memo_title(m_img) == "带图的一条，图在 files 那层。", fn.memo_title(m_img))
chk("E5b 收成书那篇里「#读书」只出现一次", md1.count("#读书") == 1, md1)
chk("E5c 账里存的那份 md 照旧带着标签（摘只发生在展示与收成书时）",
    "#读书" in m_img["md"], m_img["md"])
chk("E5d 句末那个「#3」是序号，不在标签账上 —— 一个字不许动",
    "#3" in fn.strip_inline_tags(parsed[2]["md"], parsed[2]["tags"]), parsed[2]["md"])

BOOKS = os.path.join(ROOT, "books")
fake = {"calls": []}


def fake_import(out_root, filename, blob, title=None, author=None,
                book_id_prefix="", source=""):
    """假 import_book：只建目录、写文件，返回一本「书」。"""
    bid = "%s_demo0001" % (book_id_prefix or "flomo")
    d = os.path.join(out_root, bid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, filename), "wb") as f:
        f.write(blob)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"id": bid, "title": title, "author": author,
                   "dir": d, "chapters": 1}, f, ensure_ascii=False)
    fake["calls"].append({"filename": filename, "prefix": book_id_prefix, "bytes": len(blob)})
    return {"id": bid, "dir": d, "title": title}


info = fn.to_shelf(D, m_img["id"], BOOKS, import_fn=fake_import)
chk("E6 收成书走 flomo 前缀（书库那一格靠它认门）",
    info["id"].startswith("flomo_") and fake["calls"][0]["prefix"] == "flomo", info)
with open(os.path.join(info["dir"], "meta.json"), encoding="utf-8") as f:
    meta = json.load(f)
chk("E7 书的 meta 补齐来源与字段（剪藏那一格能认出它是便签）",
    meta["source"] == "flomo" and meta["format"] == "clip" and meta["memo_id"] == m_img["id"]
    and meta["date"] == "2026-08-30" and meta["clock"] == "06:30:10"
    and meta["tags"] == ["读书"] and meta["words"] == m_img["words"], meta)
chk("E8 图片真的拷进书目录（阅读器按 images/ 相对路径找得到）",
    os.path.isfile(os.path.join(info["dir"], "images", m_img["atts"][0]))
    and meta["images"] == 1, sorted(os.listdir(info["dir"])))
chk("E9 收完记得这条已经收过（界面上那颗按钮该变成「去书库」）",
    fn.one(fn.load(D), m_img["id"])["book"] == info["id"])
again = fn.to_shelf(D, m_img["id"], BOOKS, import_fn=fake_import)
chk("E10 再点一次不许建出第二本",
    again["existed"] is True and again["id"] == info["id"] and len(fake["calls"]) == 1, again)


def weird_import(out_root, filename, blob, **kw):
    """返回一个「目录还没建」的书 —— 真实事故就是这个形状。"""
    return {"id": "flomo_gonedir", "dir": os.path.join(out_root, "flomo_gonedir"),
            "title": "目录不存在"}


chk("E11 目录不存在时也建得出书（宁可新建，不抛 FileNotFoundError）",
    fn.to_shelf(D, parsed[0]["id"], BOOKS, import_fn=weird_import)["id"] == "flomo_gonedir"
    and os.path.isdir(os.path.join(BOOKS, "flomo_gonedir")))
chk("E12 收一本没有图的：images 记 0，不报错", meta["images"] == 1
    and fn.one(fn.load(D), parsed[0]["id"])["atts"] == [])
try:
    fn.to_shelf(D, "fm_不存在", BOOKS, import_fn=fake_import)
    chk("E13 没这条笔记时说清楚", False, "没抛错")
except ValueError as e:
    chk("E13 没这条笔记时说清楚", "没有这条" in str(e), str(e))

# ── F：忘掉 / 清空 / 记忆画像 ────────────────────────────────────
n_before = len(fn.load(D)["memos"])
chk("F1 忘掉一条：本机账上少一条、返回 True",
    fn.forget(D, fn.load(D)["memos"][0]["id"]) and len(fn.load(D)["memos"]) == n_before - 1)
chk("F2 忘掉不存在的给 False（界面别报成功）", fn.forget(D, "fm_不存在") is False)
chk("F3 忘掉只是「本机忘掉」——附件文件还在（flomo 那边一个字没动）",
    len(os.listdir(fn.att_dir(D))) >= 1)
chk("F4 没有账本时读成空的（首启就是这个状态）",
    fn.load(os.path.join(ROOT, "nothing"))["memos"] == [])
with open(fn.path_in(D), "w", encoding="utf-8") as f:
    f.write("{这不是 JSON")
chk("F5 账本坏了也当空的读（不许把「重新导入」这条路挡死）", fn.load(D)["memos"] == [])
with open(fn.path_in(D), "w", encoding="utf-8") as f:
    json.dump({"memos": [{"id": "x", "md": 3}, "不是字典", {"md": "没 id"}]}, f)
chk("F6 账本里形状不对的条目被丢掉、能用的留下", len(fn.load(D)["memos"]) == 1)
fn.import_notes(D, export_zip(), "z.zip")

p = fn.build_portrait(fn.load(D))
pm = fn.portrait_markdown(p)
pj = json.dumps(p, ensure_ascii=False)
# 画像里带标签是设计如此（标签是用户自己打的分类），所以探针只取正文里那些句子——
# 拿标签串去比会假阳性（「读书/神经科学」本来就该在画像里）。
probes = ["复利", "买米", "换滤芯", "先写下来再改", "这一整块是引用", "凑够两百字以上"]
chk("F7 记忆画像不含任何一条笔记原文（它是唯一会被反复喂给 Agent 的东西）",
    not [x for x in probes if x in pm or x in pj],
    [x for x in probes if x in pm or x in pj])
chk("F8 画像字段齐（体量 / 节奏 / 长度分布 / 分类 / 深加工）",
    all(k in p for k in ("memos", "days", "per_day", "avg_words", "short", "mid", "long",
                         "shelved", "last7", "tags", "groups", "busiest", "hours")), sorted(p))
chk("F9 骨架数字对得上（条数 = 账上条数、日期跨度有头有尾）",
    p["memos"] == len(fn.load(D)["memos"]) and p["first"] == "2026-08-29"
    and p["last"] == "2026-09-01", (p["memos"], p["first"], p["last"]))
chk("F10 长度分布三档加起来就是全部",
    p["short"] + p["mid"] + p["long"] == p["memos"], (p["short"], p["mid"], p["long"]))
chk("F11 一级标签被当成「分类习惯」聚合（层级只在统计这一刀抹平）",
    any(t == "灵感" for t, _n in p["groups"]), p["groups"])
chk("F12 高峰时段按小时聚合（Agent 用它判断这人什么时候在想事情）",
    all(isinstance(h, int) for h, _n in p["hours"]) and sum(n for _h, n in p["hours"]) >= 1,
    p["hours"])
wrote = fn.write_portrait(D)
chk("F13 画像落在存储目录下的指定文件夹（cache/flomo/portrait/）",
    os.path.isfile(wrote["json"]) and os.path.isfile(wrote["md"])
    and wrote["md"].endswith(os.path.join("portrait", "portrait.md")), wrote)
chk("F14 读得回来；没写过时现算一份并落盘",
    fn.read_portrait(D)["memos"] == p["memos"])
chk("F15 画像末尾那句诚实声明在（不给用户「原文被上传」的错觉）",
    pm.rstrip().endswith("不含任何一条笔记原文。"))
chk("F16 统计口径：图按本机存的张数算（两条带图各一张）",
    fn.summary_stats(fn.load(D))["images"] == 2, fn.summary_stats(fn.load(D))["images"])
n_now = len(fn.load(D)["memos"])
chk("F17 清空报得出清了几条", fn.clear(D) == n_now and fn.load(D)["memos"] == [], n_now)
# 界面上那颗「清空」写的是「笔记、附件里的图片和记忆画像」—— 说到就得做到。
# 上一版只把账本清空，私人图片还摊在磁盘上，用户以为删了。
chk("F17a 清空把附件里的图一起删掉（界面那句话不许说谎）",
    os.listdir(fn.att_dir(D)) == [], os.listdir(fn.att_dir(D)))
chk("F17b 清空把记忆画像一起删掉",
    [x for x in os.listdir(fn.portrait_dir(D)) if not x.endswith(".tmp")] == [],
    os.listdir(fn.portrait_dir(D)))
chk("F17c 清空只删内容、不删账本本身与目录（数据目录是红线）",
    os.path.isfile(fn.path_in(D)) and os.path.isdir(D))
chk("F18 空账上算画像不炸（首启时就是这个状态）",
    fn.build_portrait(fn.load(D))["memos"] == 0
    and fn.portrait_markdown(fn.build_portrait(fn.load(D))).startswith("# 我的记忆画像"))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("flomo 便签后端：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  解析、去重、附件、筛、收成书与记忆画像全部验通过")
