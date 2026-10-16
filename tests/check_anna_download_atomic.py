# -*- coding: utf-8 -*-
"""安娜接住下载的**落盘**那一半（anna_browser.handle_download）：最终名上要么完整、
要么没有，绝不出现半截件；没写完的下载不进「补收」清单。

为什么单开这一条
    前一套离线门禁（`check_anna_catch.py`）守的是「接住了又被自己盖掉」——同一秒
    连下多份同名，四份都得在。这一条守的是另一半，那里量不到：**一份下载写到一半
    就断了**。

    `handle_download` 原先让 `download.save_as(target)` 直接往最终名上写。连接被
    站点掐断、用户关掉窗口、磁盘满、进程被收走，这一步都会在**最终名**上留下一个
    长度不对却「存在」的文件，而它看起来跟一份正常接住的下载一模一样：

      · 「补收」下一轮拿它去解析，用户等到的是一句「这个 zip 打不开」，不知道为什么；
      · 同名的下一份下载看到这个名字被占，只会加时间戳绕开 —— 那份半截件再也没人
        重下，一直躺在下载格里。它连 `incoming_listing` 都照列不误（只按「是普通
        文件」筛），界面上就是一份永远收不进来的条目。

    现在写的是同目录随机名 `.part`，下完整再一次 `os.replace` 改名过去；失败 /
    中止两条退出路径都把临时件收掉。最终名只可能「没有」或「完整」，同名的下一份
    因此走得通，不会被一个坏件顶去加戳。

    顺带把「补收」的口径补齐：没写完的下载（归藏的 `.part` / 旧版 `.tmp`、Chromium
    自己的 `.crdownload`）不再出现在 `incoming_listing` 里 —— 半截 EPUB / PDF 拿去
    解析只会多一句没用的失败。

全程离线：拿一个只会写字节的假 download 对象，在临时目录里逐条钉；不连浏览器、
不联网、不碰真实 cache/ 与书库。
"""
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_browser as ab      # noqa: E402
import anna_state as anna      # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


class FakeDownload:
    """只实现 handle_download 用到的两样：建议文件名与 save_as。

    `mode="partial"` 模拟「写了一截、站点把连接断了」：先落一点字节再抛；
    `mode="abort"` 模拟中止信号（KeyboardInterrupt）落在写盘这一步。
    """

    def __init__(self, suggested, data=b"x", mode="ok"):
        self.suggested_filename = suggested
        self.data = data
        self.mode = mode
        self.paths = []

    def save_as(self, path):
        self.paths.append(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if self.mode == "partial":
            with open(path, "wb") as f:
                f.write((self.data or b"PK")[:4])
            raise RuntimeError("目标站点把连接断了")
        if self.mode == "abort":
            with open(path, "wb") as f:
                f.write(b"PK\x03\x04half")
            raise KeyboardInterrupt()
        with open(path, "wb") as f:
            f.write(self.data)


TMP = tempfile.mkdtemp(prefix="guizang-anna-part-")
INCOMING = anna.incoming_dir(TMP, create=True)
_QUIET = ab.log
ab.log = lambda *a, **k: None          # 失败路径会打一行，测试输出不必跟着刷


def fresh_incoming():
    shutil.rmtree(INCOMING, ignore_errors=True)
    os.makedirs(INCOMING)


def listing():
    return sorted(os.listdir(INCOMING))


def payload(name):
    with open(os.path.join(INCOMING, name), "rb") as f:
        return f.read()


try:
    # ── A 正常路径：最终名完整，写口只碰临时件，盘上不留 `.part` ────────
    fresh_incoming()
    q = []
    fd = FakeDownload("book.epub", b"FULL-CONTENT")
    ab.handle_download(TMP, q, fd)
    chk("正常路径：最终名就是洗过的建议名", len(q) == 1 and os.path.basename(q[0][1]) == "book.epub"
        and q[0][0] == os.path.join(INCOMING, "book.epub"), q)
    chk("正常路径：最终名上的字节是完整的那一份", payload("book.epub") == b"FULL-CONTENT")
    chk("正常路径：save_as 收到的是临时件，不是最终名",
        bool(fd.paths) and fd.paths[0] != q[0][0] and fd.paths[0].endswith(".part"),
        fd.paths)
    chk("正常路径：临时件名带进程号与随机串，一次一个名字",
        os.path.basename(fd.paths[0]).startswith("book.epub.") and
        len(os.path.basename(fd.paths[0]).split(".")) >= 4, fd.paths[0])
    chk("正常路径：下载格里只剩那一份正式文件（没有 .part 残件）",
        listing() == ["book.epub"], listing())

    # ── B 写一半就断：最终名上不留任何东西，账本如实说没接住 ────────────
    fresh_incoming()
    q = []
    fd = FakeDownload("half.epub", b"FULL-BYTE", mode="partial")
    ab.handle_download(TMP, q, fd)
    chk("写一半就断：最终名上一个字节都不许有",
        not os.path.exists(os.path.join(INCOMING, "half.epub")), listing())
    chk("写一半就断：下载格里不留 .part", listing() == [], listing())
    chk("写一半就断：不排队、不谎报接住了", q == [], q)
    chk("写一半就断：账本记的是「这份没接住」",
        "没接住" in (anna.read_state(TMP).get("note") or ""), anna.read_state(TMP))

    # ── C 对照：老写法（save_as 直接写最终名）确实会把半截件留在最终名上 ──
    old_dir = os.path.join(TMP, "old-behaviour")
    os.makedirs(old_dir, exist_ok=True)
    old_target = os.path.join(old_dir, "half.epub")
    fd_old = FakeDownload("half.epub", b"FULL-BYTE", mode="partial")
    try:
        fd_old.save_as(old_target)
    except RuntimeError:
        pass
    chk("对照：老写法把半截件留在最终名上（这一条证明门禁不是空转）",
        os.path.isfile(old_target) and os.path.getsize(old_target) > 0,
        os.path.getsize(old_target) if os.path.exists(old_target) else "缺失")

    # ── D 半截件不挡路：失败之后同名再来一次，不加戳、落完整那份 ────────
    q = []
    fd = FakeDownload("half.epub", b"SECOND-TRY-FULL")
    ab.handle_download(TMP, q, fd)
    chk("上一次没接住之后，同名这一份用回原名（没被半截件顶去加时间戳）",
        len(q) == 1 and os.path.basename(q[0][1]) == "half.epub", q)
    chk("这一份的内容是完整的", payload("half.epub") == b"SECOND-TRY-FULL")

    # ── E 中止信号：原样往上传，两处都不留，不装成「没接住」 ────────────
    fresh_incoming()
    note_before = anna.read_state(TMP).get("note") or ""
    q = []
    raised = False
    try:
        ab.handle_download(TMP, q, FakeDownload("abort.epub", mode="abort"))
    except KeyboardInterrupt:
        raised = True
    chk("中止信号：原样抛出去，不被吞成「没接住」", raised)
    chk("中止信号：最终名与临时件都不留", listing() == [], listing())
    chk("中止信号：不排队", q == [], q)
    chk("中止信号：账本那句话没被改写",
        (anna.read_state(TMP).get("note") or "") == note_before,
        (note_before, anna.read_state(TMP).get("note")))

    # ── F 补收清单跳过没写完的下载（含 Chromium 自己的 .crdownload） ─────
    fresh_incoming()
    half_names = ("novel.epub.1234.abcd1234.part", "x.crdownload", "y.tmp",
                  "UPPER.EPUB.PART", "old.md.tmp")
    for n in half_names:
        with open(os.path.join(INCOMING, n), "wb") as f:
            f.write(b"PK\x03\x04half")
    with open(os.path.join(INCOMING, "book.epub"), "wb") as f:
        f.write(b"PK\x03\x04")
    with open(os.path.join(INCOMING, "book.mobi"), "wb") as f:
        f.write(b"MOBI")
    rows = anna.incoming_listing(INCOMING)
    names = {r["name"] for r in rows}
    chk("清单：没写完的下载一份都不露面", not (names & set(half_names)), sorted(names))
    chk("清单：正常的书照旧在，收得下的标 ok、收不下的标 False",
        names == {"book.epub", "book.mobi"} and
        {r["name"]: r["ok"] for r in rows} == {"book.epub": True, "book.mobi": False},
        rows)
    # 对照：老口径只按「是普通文件」筛（`incoming_listing` 原先就是这样），三份
    # 半截件都会被列进去 —— 证明这条门禁量的是真变化，不是空转。
    loose = {n for n in os.listdir(INCOMING)
             if os.path.isfile(os.path.join(INCOMING, n))}
    chk("对照：老口径（只按普通文件筛）会把半截件一起列出来",
        set(half_names) <= loose and len(loose) > len(names),
        sorted(loose - names))

    # ── G 接线：源码里 handle_download 不再直接往最终名上写 ─────────────
    src = (REPO / "anna_browser.py").read_text(encoding="utf-8")
    chk("接线：写口指向临时件（源码里没有 `save_as(target)`）",
        "save_as(target)" not in src and "download.save_as(tmp)" in src)
finally:
    ab.log = _QUIET
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAIL:
    print("安娜接住下载的落盘这一半有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("安娜接住下载：最终名上要么完整要么没有，没写完的下载不进补收清单（%d 项）。"
          % PASSED[0])
sys.exit(len(FAIL))
