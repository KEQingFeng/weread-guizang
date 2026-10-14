# -*- coding: utf-8 -*-
"""安娜的档案「接住下载」这一步的离线自测（anna_browser.handle_download）。

这一环只做一件事：把浏览器交给我们的那份下载原样落到「原始下载」那一格，再把路径
丢进队列等主循环转成书。它最容易出的事故不是「没接住」，而是**接住了又被自己盖掉**：
同一批书目站方常常给同一个文件名（`download.epub`），而代码只拿「秒」当去重尾巴 ——
同一秒里连下三份，后两份会算出同一个名字，`save_as` 就把上一份刚接住的覆盖掉。
用户下了四本、目录里只剩两本，而且队列里四条指向同一个路径，看起来还像都成功了。

所以这一套不连浏览器、不联网：拿一个只会写字节的假 download 对象，把时间钉死在同一秒，
在临时目录里逐条钉「谁也没被谁盖掉」。
"""
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import anna_browser as ab  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


class FakeDownload:
    """只实现 handle_download 用到的两样：建议文件名与 save_as。"""

    def __init__(self, suggested, data=b"x"):
        self.suggested_filename = suggested
        self.data = data

    def save_as(self, path):
        with open(path, "wb") as f:
            f.write(self.data)


class Clock:
    """把秒钉死：不钉的话「同一秒里连下三份」这条前提本身就会飘。"""

    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


_QUIET = ab.log
ab.log = lambda *a, **k: None          # 接住每份都打一行，测试输出不必跟着刷

TMP = tempfile.mkdtemp(prefix="guizang-anna-catch-")
INCOMING = os.path.join(TMP, "cache", "downloads", "anna")


def saved():
    return sorted(os.listdir(INCOMING))


def payload(name):
    with open(os.path.join(INCOMING, name), "rb") as f:
        return f.read()


_real_time = ab.time
ab.time = Clock(1791974809)
try:
    # ── 1 同一秒里四份同名：四份都得在，谁也不能盖掉谁 ─────────────
    q = []
    for i in range(4):
        ab.handle_download(TMP, q, FakeDownload("book.epub", b"content-%d" % i))

    files = saved()
    chk("同一秒连下四份同名：目录里四份都在", len(files) == 4, files)
    chk("四份的字节各不相同（没有互相覆盖）",
        sorted(payload(f) for f in files) == [b"content-0", b"content-1", b"content-2", b"content-3"],
        [(f, payload(f)) for f in files])
    chk("队列四条指向四个互不相同、真实存在的路径",
        len({p for p, _ in q}) == 4 and all(os.path.exists(p) for p, _ in q),
        [(p, n) for p, n in q])

    # ── 2 上一趟留下的带戳文件占着第一候选名：这次不能盖它 ─────────
    shutil.rmtree(INCOMING, ignore_errors=True)
    os.makedirs(INCOMING)
    with open(os.path.join(INCOMING, "book-1791974809.epub"), "wb") as f:
        f.write(b"old-run")
    q2 = []
    ab.handle_download(TMP, q2, FakeDownload("book.epub", b"new-run"))
    chk("预置的同名带戳文件还在，且内容没被动", payload("book-1791974809.epub") == b"old-run")
    chk("新下载落成一个新名字，不覆盖旧文件", len(saved()) == 2, saved())
    chk("新文件的内容是这一次的", payload(os.path.basename(q2[0][1])) == b"new-run"
        if q2 else False, q2)

    # ── 3 没有扩展名的同名：尾巴要接得对（别拼成 notes.123） ────────
    shutil.rmtree(INCOMING, ignore_errors=True)
    q3 = []
    ab.handle_download(TMP, q3, FakeDownload("notes", b"a"))
    ab.handle_download(TMP, q3, FakeDownload("notes", b"b"))
    chk("无扩展名的同名也留两份", len(saved()) == 2, saved())
    chk("无扩展名的两份内容各自对得上",
        sorted(payload(f) for f in saved()) == [b"a", b"b"], sorted(payload(f) for f in saved()))
    chk("无扩展名不会凭空长出个点号", all("." not in f for f in saved()), saved())

    # ── 4 名字里的路径不许带出原始下载格（safe_name 仍在守） ────────
    shutil.rmtree(INCOMING, ignore_errors=True)
    q4 = []
    ab.handle_download(TMP, q4, FakeDownload("../../evil.epub", b"z"))
    name4 = os.path.basename(q4[0][1])
    chk("带 ../ 的建议名只取最后一截", "/" not in name4 and "evil.epub" in name4, name4)
    chk("../ 没把文件写到下载格外面",
        os.path.exists(os.path.join(INCOMING, name4)) and not os.path.exists(os.path.join(TMP, "evil.epub")),
        saved())

    # ── 5 不同名不加戳：不动原本的名字 ─────────────────────────────
    shutil.rmtree(INCOMING, ignore_errors=True)
    q5 = []
    ab.handle_download(TMP, q5, FakeDownload("first.epub", b"1"))
    ab.handle_download(TMP, q5, FakeDownload("second.epub", b"2"))
    chk("不同名字不加时间戳，原样落盘", saved() == ["first.epub", "second.epub"], saved())
finally:
    ab.time = _real_time
    ab.log = _QUIET
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAIL:
    print("安娜接住下载这一环有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("安娜接住下载：同一秒连下多份同名也不互相覆盖，路径护栏仍守得住。")
sys.exit(len(FAIL))
