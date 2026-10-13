#!/usr/bin/env python3
"""写作那一格的「书 → 章」账本：建书、归章、排顺序、合稿、摊成一本真书。

为什么单独一份（而且是离线的）：这一层的全部风险都在**账本**上，不在界面上。
`_books.json` 记的是「哪几章、按什么顺序、属于哪本书」，而章本身还是原来那篇稿子
（目录 / rev / 快照 / MCP 一个字没改）。账本一坏，界面上表现为「章不见了」或
「同一章出现两次」，用户会以为稿子丢了 —— 那是最不能出错的一种错觉，所以要当场钉住：

  A 建 / 改名 / 删：删书只删「组」，章一篇都不许跟着没（重量不同的两件事）
  B 一章只属于一本书：从 A 书点到 B 书，A 那边自动腾出去
  C 排顺序：到顶到底不许越界，也不许把别的章挤乱
  D 账本自己坏了怎么办：文件不存在 / 是垃圾 / 章号指向空气 / 同一章列两次
  E 合稿与整本导出：每章前补它自己的标题（不补就切不出目录），
    自己已经写了 `#` 的章不重复补
  F 摊成 chapters/0001.md 那一坨：`book_layout` 得认它是一本完整的书

不联网、不起浏览器、不动真书库：全程跑在系统临时目录里造的那座沙盒书库上。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_layout          # noqa: E402
import writer               # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-books-")
ROOT_DIR = os.path.join(TMP, "library")
os.makedirs(ROOT_DIR)


def touch(text="正文", title="一章"):
    """造一篇稿子，只给药不关心书号。"""
    return writer.create(ROOT_DIR, title, text)[0]


# ── A. 建书 / 改名 / 删书 ─────────────────────────────────
b = writer.create_book(ROOT_DIR, "身体这本")
chk("建书：给的是 bk_ 开头、12 位的书号", writer.book_id_ok(b["id"]), b["id"])
chk("建书：一开始没有章", b["chapters"] == [], b)
chk("建书：书名进了账本", writer.load_books(ROOT_DIR)[0]["title"] == "身体这本")

bad = writer.rename_book(ROOT_DIR, b["id"], "   ")
chk("改名：空名字不落在盘上（界面上会出现一颗没有名的书）",
    bad[0] is False and writer.load_books(ROOT_DIR)[0]["title"] == "身体这本", bad)
chk("改名：认得的书号才改", writer.rename_book(ROOT_DIR, "bk_000000000000", "X")[0] is False)
chk("改名：不认得的书号格式直接判不是书号",
    not writer.book_id_ok("write_0123456789ab") and not writer.book_id_ok("../etc"))

ok, out = writer.delete_book(ROOT_DIR, b["id"])
chk("删书：报的是「不成书了、章节还在」这种话",
    ok and "章节还在" in (out or {}).get("msg", ""), out)
chk("删书：账本上这一本没了", writer.load_books(ROOT_DIR) == [])
chk("删书：不存在的书给一句人话", writer.delete_book(ROOT_DIR, "bk_000000000000")[0] is False)

# ── B. 归章：一章只属于一本书 ────────────────────────────
book = writer.create_book(ROOT_DIR, "第一本")
other = writer.create_book(ROOT_DIR, "第二本")
d1 = touch("血")
d2 = touch("汗")

ok, add = writer.add_chapter(ROOT_DIR, book["id"], d1)
chk("归章：已有的稿子能加进书里", ok and add["book"]["chapters"] == [d1], add)
ok2, add2 = writer.add_chapter(ROOT_DIR, book["id"])
chk("归章：不给稿号就顺手建一篇（点「加一章」不该还要先去哪建稿）",
    ok2 and writer.ok_id(add2["id"]) and os.path.isdir(writer.dir_of(ROOT_DIR, add2["id"])), add2)
new_id = add2["id"]
chk("归章：新建那章带着「新的一章」这个名字",
    (writer.load_meta(writer.dir_of(ROOT_DIR, new_id)) or {}).get("title") == "新的一章")

chk("归章：同一章再点一次不会列出两行",
    writer.add_chapter(ROOT_DIR, book["id"], d1)[1]["book"]["chapters"].count(d1) == 1)

writer.add_chapter(ROOT_DIR, other["id"], d1)
books = {x["id"]: x for x in writer.load_books(ROOT_DIR)}
chk("归章：从第二本加走之后，第一本里自动腾出去（一章只能属于一本书）",
    d1 not in books[book["id"]]["chapters"] and d1 in books[other["id"]]["chapters"],
    {k: v["chapters"] for k, v in books.items()})
chk("归章：稿子本身一个字没动",
    writer.read_text(writer.dir_of(ROOT_DIR, d1)) == "血")

chk("归章：不是写作那一格的稿号直接拒",
    writer.add_chapter(ROOT_DIR, other["id"], "31600081")[0] is False)
chk("归章：书不在就说书不在", writer.add_chapter(ROOT_DIR, "bk_000000000000", d2)[0] is False)
chk("归章：稿子已经不在了别说加成功",
    writer.add_chapter(ROOT_DIR, other["id"], "write_000000000000")[0] is False)

ok, out = writer.remove_chapter(ROOT_DIR, d1)
chk("出册：拿掉之后书里看不见了", ok and d1 not in writer.find_book(
    writer.load_books(ROOT_DIR), other["id"])["chapters"], out)
chk("出册：稿子还在盘上（移出书 ≠ 删掉稿）",
    os.path.isdir(writer.dir_of(ROOT_DIR, d1)))
chk("出册：本来就没归册的章给一句实话",
    writer.remove_chapter(ROOT_DIR, d2)[0] is False)

# ── C. 排顺序 ────────────────────────────────────────────
seq = writer.create_book(ROOT_DIR, "顺序这本")
ids = [touch("第%d章的正文" % i, "第%d章" % i) for i in range(1, 4)]
for did in ids:
    writer.add_chapter(ROOT_DIR, seq["id"], did)
cur = lambda: writer.find_book(writer.load_books(ROOT_DIR), seq["id"])["chapters"]
chk("排顺序：加进来的三章按先后排", cur() == ids, cur())
writer.order_chapter(ROOT_DIR, seq["id"], ids[2], -1)
chk("排顺序：往上挪一格", cur() == [ids[0], ids[2], ids[1]], cur())
chk("排顺序：到顶了再往上不越界（也不报错）",
    writer.order_chapter(ROOT_DIR, seq["id"], cur()[0], -1)[1]["moved"] is False)
writer.order_chapter(ROOT_DIR, seq["id"], ids[1], 99)
chk("排顺序：一次报多远的数都只到末尾", cur()[-1] == ids[1], cur())
chk("排顺序：挪动不许弄丢章", sorted(cur()) == sorted(ids), cur())
chk("排顺序：不在这本书里的章号说没有",
    writer.order_chapter(ROOT_DIR, seq["id"], "write_000000000000", 1)[0] is False)

# ── D. 账本坏掉的样子 ────────────────────────────────────
p = writer.books_path(ROOT_DIR)
books = writer.load_books(ROOT_DIR)
writer.save_books(ROOT_DIR, books)
chk("账本：写下去读回来一模一样", [x["id"] for x in writer.load_books(ROOT_DIR)]
    == [x["id"] for x in books])

# 章目录被删掉之后（界面上那颗「删掉这一章」走的是原有 book.delete），账本不该还留着它
shutil.rmtree(writer.dir_of(ROOT_DIR, ids[2]))
chk("账本：章被删了就从册上销名（不然界面上留一颗点了打不开的章）",
    ids[2] not in writer.find_book(writer.load_books(ROOT_DIR), seq["id"])["chapters"],
    writer.find_book(writer.load_books(ROOT_DIR), seq["id"]))

with open(p, "w", encoding="utf-8") as f:
    f.write("{ this is not json")
chk("账本：读坏了给空表（写作屏照常开，不许整页 500）", writer.load_books(ROOT_DIR) == [])
with open(p, "w", encoding="utf-8") as f:
    f.write(json.dumps({"books": [{"id": "bk_000000000000", "title": "X",
                                   "chapters": ["write_000000000000", ids[0], ids[0],
                                                "31600081", ""]},
                                 {"id": "write_0123456789ab", "title": "假书号"},
                                 "不是字典"]}, ensure_ascii=False))
fixed = writer.load_books(ROOT_DIR)
chk("账本：指向空气的章号被剔掉、重复的只留一次、乱来的书号与条目一概不认",
    len(fixed) == 1 and fixed[0]["chapters"] == [ids[0]], fixed)

# 上面两轮是把账本**写脏**来验读的兜底，验完清成空册，后面各节自己重建（互不牵连）
writer.save_books(ROOT_DIR, [])
chk("账本：空册读回来是空，不是报错", writer.load_books(ROOT_DIR) == [])

# ── E. 合稿 ──────────────────────────────────────────────
seq = writer.create_book(ROOT_DIR, "顺序这本")
writer.add_chapter(ROOT_DIR, seq["id"], ids[0])
writer.add_chapter(ROOT_DIR, seq["id"], ids[1])
m = writer.merge_text(ROOT_DIR, seq["id"])
body = m["text"]
chk("合稿：认得的书名带回来了", m["title"] == "顺序这本", m["title"])
chk("合稿：按册上的顺序接（第 3 章的稿子被删过，接出来就是剩下的两章）",
    body.find("第1章") < body.find("第2章") and "第3章" not in body, body[:200])
chk("合稿：每章前补一行它自己的标题（不补就切不出目录）",
    "## 第1章" in body and "## 第2章" in body, body)
chk("合稿：章与章之间有一条分隔", "---" in body, body[:120])
chk("合稿：不存在的书给 None", writer.merge_text(ROOT_DIR, "bk_000000000000") is None)

named = writer.create_book(ROOT_DIR, "自己写了标题的一本")
a = writer.create(ROOT_DIR, "甲", "## 我自己写的甲\n\n正文甲")[0]
bwrite = writer.create(ROOT_DIR, "乙", "# 乙\n\n正文乙")[0]
writer.add_chapter(ROOT_DIR, named["id"], a)
writer.add_chapter(ROOT_DIR, named["id"], bwrite)
txt = writer.merge_text(ROOT_DIR, named["id"])["text"]
chk("合稿：稿子里已经有 # 标题的，不重复补一行（用户写的算数）",
    txt.count("我自己写的甲") == 1 and txt.count("# 乙") == 1, txt)

# 空章：只有标题没有正文，不该在合稿里留下一个光秃秃的分隔
empt = writer.create(ROOT_DIR, "空的一章", "")[0]
writer.add_chapter(ROOT_DIR, named["id"], empt)
et = writer.merge_text(ROOT_DIR, named["id"])["text"]
chk("合稿：空章只留标题那一行，不掺进别的章的正文里",
    et.rstrip().endswith("空的一章"), et[-60:])

# ── F. 摊成一本真书（EPUB / PDF 那条现成的路）───────────
tmp2 = os.path.join(TMP, "spread")
os.makedirs(tmp2)
one = writer.as_book_dir_merged(ROOT_DIR, named["id"], tmp2)
chk("摊书：临时目录里有了 chapters/ 与 meta.json",
    one and os.path.isdir(os.path.join(one, "chapters"))
    and os.path.isfile(os.path.join(one, "meta.json")), one)
chk("摊书：book_layout 认这是一本完整的书", one and book_layout.is_book_dir(one))
files = sorted(os.listdir(os.path.join(one, "chapters"))) if one else []
chk("摊书：一章一个文件、按顺序编号（EPUB 的目录就是这么来的）",
    files == ["0001.md", "0002.md", "0003.md"], files)
chk("摊书：章文件里是那一章的正文，不是别人的",
    "正文乙" in open(os.path.join(one, "chapters", "0002.md"), encoding="utf-8").read())
chk("摊书：不存在的书给 None", writer.as_book_dir_merged(ROOT_DIR, "bk_000000000000", tmp2) is None)

meta = json.load(open(os.path.join(one, "meta.json"), encoding="utf-8")) if one else {}
chk("摊书：meta 里带的是书名，source 仍是写作那一格",
    meta.get("title") == "自己写了标题的一本" and meta.get("source") == "write", meta)

# ── 界面与后端都读的那份清单 ─────────────────────────────
listed = [x["id"] for x in writer.list_drafts(ROOT_DIR)]
chk("稿列：账本那个文件不许冒充一篇稿子出现在左栏里",
    listed and all(str(i or "").startswith("write_") for i in listed), listed)

det = writer.book_detail(ROOT_DIR, writer.find_book(writer.load_books(ROOT_DIR), named["id"]))
chk("清单：第几章是册上的顺序号（前端数的那份顺序不算数）",
    [c["no"] for c in det["chapters"]] == [1, 2, 3], det)
chk("清单：字数是各章相加，标题与 rev 都带着",
    det["words"] == sum(c["words"] for c in det["chapters"])
    and all("title" in c and "rev" in c for c in det["chapters"]), det)
chk("清单：默认不夹带正文（清单每秒刷一次，正文字数可能几万）",
    "text" not in det["chapters"][0])
chk("清单：要正文时才给正文",
    "text" in writer.book_detail(
        ROOT_DIR, writer.find_book(writer.load_books(ROOT_DIR), named["id"]),
        with_text=True)["chapters"][0])

shutil.rmtree(TMP, ignore_errors=True)
print()
print("书籍账本取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
