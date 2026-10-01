# -*- coding: utf-8 -*-
"""给真机套件铺一个一次性书架。

浏览器套件（书架两种排法、叠卡、详情页、阅读器、笔记）都得有书可点。以前这些书是
一轮一轮手工堆在临时目录里的，换台机器就空了，套件会「因为没书可点」而报假失败。
这里把书架固化下来：跑一次就有，跑多少次结果都一样。

用法：
    .venv/bin/python tests/seed.py            # 铺到默认沙盒，打印路径
    .venv/bin/python tests/seed.py --force    # 先清空再铺
也可以在套件里直接 `from seed import seed`。
"""
import json
import os
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

# 笔记/阅读器套件点名的那本书：10 章、正文够长（能划出不重复的一串字）、标记为已取全。
GAP_BOOK = "GAPBOOK1"
GAP_CHAPTERS = 10

# 书架排法要有内容才看得出叠卡的扇形和瀑布的行列，至少给 5 本。
SHELF_BOOKS = [
    ("SE_ALLOCATION", "内存分配为什么慢", "陈七", 6),
    ("SE_COMPILER", "编译原理十二讲", "林二", 9),
    ("SE_LINGUISTICS", "语言学纲要札记", "周午", 4),
    ("SE_NETWORK", "网络协议速通", "吴九", 7),
    ("SE_STATISTICS", "统计学习方法笔记", "郑十", 5),
]

# 导入书（imp_ 前缀）与剪藏书（clip_ 前缀）各留一本：
# 这两类在书架上走的是「自己的书」那条分支，按钮和真书不一样，套件要能点到。
IMPORT_BOOK = ("imp_SE_RECIPE", "我的菜谱草稿")
CLIP_BOOK = ("clip_SE_POST", "一篇剪藏下来的文章")


def _paragraph(i):
    """造一段有真实中文肌理的正文，避免套件划词时划到空白或全是同一个字。"""
    lines = [
        "第%d节里要说的是这一章的主线：把问题拆开之后，每一块都能单独验证。" % i,
        "读者在这里划一句，就能在右栏留下一条对应的笔记，两边互相跳转但不打断阅读。",
        "论点、疑问、可引用、待查，四种高亮颜色只是标签，不改变正文本身。",
        "表格、删除线、一到五级标题这些 Markdown 写法都应当原样渲染出来。",
        "导出时这份笔记会跟着书走，存在书自己的文件夹里，不落在某个人的家目录。",
    ]
    body = "\n\n".join(lines)
    return body + "\n\n" + ("正文补白，用来把章节撑到能滚动。" * 12) + "\n"


def _write_book(root, bid, title, author, chapters, done=True, source="weread",
                prefix="SE"):
    d = root / bid
    shutil.rmtree(d, ignore_errors=True)
    (d / "chapters").mkdir(parents=True)
    (d / "images").mkdir(parents=True)
    (d / "raw").mkdir(parents=True)
    for i in range(chapters):
        (d / "chapters" / ("%04d.md" % (i + 1))).write_text(
            "# 第%d章 %s\n\n%s\n" % (i + 1, title[:6], _paragraph(i + 1)), encoding="utf-8")
    catalog = [{"chapterTitle": "第%d章" % (i + 1)} for i in range(chapters)]
    (d / "_catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    meta = {"title": title, "author": author, "done": done, "source": source,
            "chars": sum(len(_paragraph(i)) for i in range(chapters))}
    if prefix == "imp":
        meta["format"] = "import"
    if prefix == "clip":
        meta.update({"format": "clip", "url": "https://example.com/a", "words": 800})
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "raw" / "note.txt").write_text("占位原始包\n", encoding="utf-8")
    return d


def seed(books_dir=None, force=False):
    """铺书架，返回书库路径。force=True 时先删掉整个沙盒书库再铺。"""
    root = pathlib.Path(books_dir or selftest.BOOKS)
    if force and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    _write_book(root, GAP_BOOK, "缺口试验这本", "某人", GAP_CHAPTERS)
    for bid, title, author, n in SHELF_BOOKS:
        _write_book(root, bid, title, author, n)
    _write_book(root, IMPORT_BOOK[0], IMPORT_BOOK[1], "我自己", 3, source="local", prefix="imp")
    _write_book(root, CLIP_BOOK[0], CLIP_BOOK[1], "某个公众号", 1, source="clip", prefix="clip")

    # 一本「取了一半」的书：卡片上应该给「续取」而不是「正文」，这是 0.9.8 那两道锁的靶子。
    part = _write_book(root, "SE_PARTIAL", "只取了一半的书", "某人", 2, done=False)
    (part / "_catalog.json").write_text(
        json.dumps([{"chapterTitle": "第%d章" % (i + 1)} for i in range(7)], ensure_ascii=False),
        encoding="utf-8")
    return root


def book_dir(bid):
    return pathlib.Path(os.environ.get("GUIZANG_BOOKS", str(selftest.BOOKS))) / bid


if __name__ == "__main__":
    force = "--force" in sys.argv
    where = seed(force=force)
    print("书架已铺好：" + str(where))
    print("书数：%d" % len([p for p in where.iterdir() if p.is_dir()]))
