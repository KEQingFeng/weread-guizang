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
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import selftest  # noqa: E402
import book_layout  # noqa: E402

# 笔记/阅读器套件点名的那本书：10 章、正文够长（能划出不重复的一串字）、标记为已取全。
GAP_BOOK = "GAPBOOK1"
GAP_CHAPTERS = 10

# 书架排法要有内容才看得出叠卡的扇形和瀑布的行列，至少给 5 本。
# 1.0.1 起三格各摆各的：叠卡那套要数到「甩出画外的那两张」（距离 > 3 才存在），
# 微信读书这一格得有 8 本以上才摊得开 —— 以前合并展示时随手就凑够的数，拆开后会不够。
SHELF_BOOKS = [
    ("SE_ALLOCATION", "内存分配为什么慢", "陈七", 6),
    ("SE_COMPILER", "编译原理十二讲", "林二", 9),
    ("SE_LINGUISTICS", "语言学纲要札记", "周午", 4),
    ("SE_NETWORK", "网络协议速通", "吴九", 7),
    ("SE_STATISTICS", "统计学习方法笔记", "郑十", 5),
    ("SE_DATABASE", "数据库系统内幕", "钱六", 5),
    ("SE_OS", "操作系统实战四讲", "孙八", 8),
    ("SE_DEBUGGING", "调试的艺术", "赵一", 3),
]

# 导入书（imp_ 前缀）与剪藏书（clip_ 前缀）各留一本：
# 这两类在书架上走的是「自己的书」那条分支，按钮和真书不一样，套件要能点到。
IMPORT_BOOK = ("imp_SE_RECIPE", "我的菜谱草稿")
CLIP_BOOK = ("clip_SE_POST", "一篇剪藏下来的文章")

# 视频转笔记那一屏要有真转写才点得动：一本带时间戳的视频书，段里故意留了两处
# 「听错的词」（内村 / 进成），套件的转写工作台就照着这两处改、存、导。
VIDEO_BOOK = ("video_SE_LECTURE", "操作系统导论·第 3 讲", "某个讲师")
VIDEO_SEGS = [
    (0.0, 6.4, "大家好，今天这一讲说的是内存管理。", ""),
    (6.4, 14.2, "上一讲我们把进程的生命周期过了一遍。", ""),
    (14.2, 23.8, "这一讲先看内村分配的三个基本问题。", "讲师"),
    (23.8, 33.1, "第一个是碎片，第二个是进成速度。", "讲师"),
    (33.1, 42.6, "同学可以把这三点自己在纸上写一遍。", ""),
    (42.6, 53.9, "然后我们看伙伴系统怎么把空闲块串起来。", ""),
    (53.9, 66.3, "这里的关键词是合并，也就是 coalescing。", ""),
    (66.3, 78.0, "下一讲接着说虚拟内存和页表。", ""),
]



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
        # 章节文件名照产品的口径来：引擎、book_import、book_notes 认的都是 0000.md 起头
        # （0 基）。铺成 0001 起头的话，右栏那份「章节标题表」会整体错一格，测出来的
        # 「标题对不上」跟产品没关系，纯粹是货架自己埋的坑。人的序号还在标题里（第 1 章）。
        (d / "chapters" / ("%04d.md" % i)).write_text(
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


def _write_video_book(root, bid, title, uploader):
    """一本「转完了」的视频书：transcript.json 带时间戳，章节和摘要都在。

    转写工作台要能改字、能存、能导，靠的就是一份真形状的 transcript.json —— 手搓的
    假形状会让套件测出一条真产品里不存在的路，那就白测了。字段照 video_note 的
    _transcript_doc 摆，段落故意留两处听错的词。
    """
    d = root / bid
    shutil.rmtree(d, ignore_errors=True)
    (d / "chapters").mkdir(parents=True)
    text = "\n".join(s[2] for s in VIDEO_SEGS)
    segs = [{"start": a, "end": b, "text": t, "id": "s%05d" % i}
            for i, (a, b, t, _sp) in enumerate(VIDEO_SEGS)]
    # 两段带说话人：工作台那一栏要看得出「这一段是谁说的」，也测得到筛说话人这条路。
    for i, (_a, _b, _t, sp) in enumerate(VIDEO_SEGS):
        if sp:
            segs[i]["speaker"] = sp
    (d / "transcript.json").write_text(json.dumps(
        {"schema": 1, "engine": "fake-whisper", "language": "zh", "duration": 78.0,
         "title": title, "vid": "SEFAKE03", "page": 3, "segments": segs,
         "generated_at": "2026-10-01 09:00:00"}, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "transcript.txt").write_text(text + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(
        {"title": title, "overview": "这一讲讲内存管理的三个基本问题。",
         "bulletPoints": ["碎片", "分配速度", "伙伴系统"],
         "chapters": [{"title": "三个基本问题", "start": 0, "summary": "碎片与速度"},
                      {"title": "伙伴系统", "start": 42.6, "summary": "空闲块合并"}]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for i, ch in enumerate(["三个基本问题", "伙伴系统", "下一讲"]):
        (d / "chapters" / ("%04d.md" % i)).write_text(
            "# %s\n\n%s\n" % (ch, text if i == 0 else "（这一节的正文来自转写。）"),
            encoding="utf-8")
    (d / "merged.md").write_text("# " + title + "\n\n" + text + "\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"title": title, "page_title": title, "author": uploader, "uploader": uploader,
         "site": "bilibili", "url": "https://www.bilibili.com/video/SEFAKE03?p=3",
         "source": "video", "done": True, "duration": 78, "words": len(text),
         "chars": len(text), "chapters": 3, "segments": len(segs), "page": 3, "pages": 12,
         "asr_engine": "fake-whisper", "language": "zh", "transcript_edited": False,
         "video_at": "2026-10-01 09:00:00", "updated_at": int(time.time())},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def module_root(books_root, module):
    """书库里那一格的文件夹：1.0.1 起一本书得住在自己模块的文件夹里，
    货架跟着产品走，不然真机套件测的就只是一条兜底的旧路径。"""
    return pathlib.Path(book_layout.book_dir(str(books_root), module))


def _reset_ledger():
    """把沙盒那本分类账（夹子 / 归类 / 标签 / 排序）一起清掉。

    书架清了账没清，界面上就留下上一轮跑测建的夹子：同名药丸长出两颗，
    「归进哪一颗」变成看运气，计数断言跟着失真，收尾又把它们当成新造的删不掉 ——
    真机套件报的「夹子删了又复活」就是这么来的，不是接口漏删。

    路径问 ui_server 要（文件名只有一个地方说了算，改了名这里跟着走），
    但只删落在沙盒里的那一份：没设沙盒环境变量时它指到用户自己的数据目录，
    那种情况下直接不动 —— 测试脚本不许碰真人的书架分类。
    """
    try:
        import ui_server
        path = pathlib.Path(ui_server.LIB_PATH)
    except Exception:
        path = selftest.CACHE / "library.json"
    if selftest.SANDBOX not in path.parents:
        return
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def seed(books_dir=None, force=False):
    """铺书架，返回书库路径。force=True 时先删掉整个沙盒书库与分类账再铺。"""
    root = pathlib.Path(books_dir or selftest.BOOKS)
    if force and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    if force:
        _reset_ledger()
    root.mkdir(parents=True, exist_ok=True)
    mine = module_root(root, "weread")

    _write_book(mine, GAP_BOOK, "缺口试验这本", "某人", GAP_CHAPTERS)
    for bid, title, author, n in SHELF_BOOKS:
        _write_book(mine, bid, title, author, n)
    _write_book(module_root(root, "local"), IMPORT_BOOK[0], IMPORT_BOOK[1], "我自己",
                3, source="local", prefix="imp")
    _write_book(module_root(root, "clip"), CLIP_BOOK[0], CLIP_BOOK[1], "某个公众号",
                1, source="clip", prefix="clip")
    # 视频那一屏的工作台要有带时间戳的转写才点得动（改字 / 保存 / 导字幕全在这本上跑）
    _write_video_book(module_root(root, "video"), VIDEO_BOOK[0], VIDEO_BOOK[1], VIDEO_BOOK[2])

    # 一本「取了一半」的书：卡片上应该给「续取」而不是「正文」，这是 0.9.8 那两道锁的靶子。
    part = _write_book(mine, "SE_PARTIAL", "只取了一半的书", "某人", 2, done=False)
    (part / "_catalog.json").write_text(
        json.dumps([{"chapterTitle": "第%d章" % (i + 1)} for i in range(7)], ensure_ascii=False),
        encoding="utf-8")
    return root


def book_dir(bid):
    """这本书现在在哪儿：先按 id 去书库里找（模块文件夹 or 平铺的旧位置）。

    套件不该关心书库长什么样 —— 目录结构变了，只要还按 id 问，一样指得对。
    """
    root = pathlib.Path(os.environ.get("GUIZANG_BOOKS", str(selftest.BOOKS)))
    if not bid:
        return str(root)
    return book_layout.resolve(str(root), bid) or str(root / bid)


if __name__ == "__main__":
    force = "--force" in sys.argv
    where = seed(force=force)
    print("书架已铺好：" + str(where))
    print("书数：%d" % len(book_layout.walk(str(where))))
