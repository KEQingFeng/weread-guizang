#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云同步取回的落盘口径（sync.py）：半截文件不许出现在书库里，也不许被当成书上传。

为什么单开一份：`tests/check_sync.py` 管的是配置、URL 拼法与「传/取/404/500」
四类收尾，唯独没管「字节写到一半突然断掉」这一档。而云同步的规则是
**本地有就上传**、本地没有才去取 —— 这恰好是最危险的那一种：

    本地没有 → 取回写盘写到一半（断网 / 盘满 / 进程被杀）
      → 目标位置留下一个长度不对却「存在」的文件
      → 下一次同步见本地有，就把它传回云端，把好的那份盖掉
      → 一次断线变成永久损坏

所以这里钉两件事，两件都不联网（记账的假 `_http` 顶掉请求）：

  1. 取回落盘必须是「先写 .part、再原子改名」；替换失败时目标位置一个字都不许
     出现，临时文件也不许留下垃圾。
  2. `.part` / `.tmp` 这类「还没写完」的文件一律不进同步清单 —— 不然上一次断线
     留下的半截件，或被杀进程打断的写作稿，会被当成成品传上去。
"""
import io
import os
import shutil
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync as S  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


ORIG_HTTP = S._http
BASE = "https://dav.example.com/remote.php/dav/files/u"


class FakeResponse:
    def __init__(self, data=b""):
        self._data = data

    def read(self, *_a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class Transport:
    """记账的假 HTTP：不联网，预置远端文件，记下每次 (方法, URL)。"""

    def __init__(self):
        self.calls = []
        self.remote = {}

    def __call__(self, method, url, data=None, headers=None, timeout=60):
        self.calls.append((method, url))
        if method == "MKCOL":
            return FakeResponse()
        if method == "PUT":
            self.remote[url] = data
            return FakeResponse()
        if method == "GET":
            if url in self.remote:
                return FakeResponse(self.remote[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b""))
        return FakeResponse()

    def urls(self, method=None):
        return [u for m, u in self.calls if method is None or m == method]


def dav_cfg(**what):
    w = {"time": True, "records": False, "books": False}
    w.update(what)
    return S._norm({"enabled": True, "provider": "webdav", "what": w,
                    "webdav": {"url": BASE, "user": "u", "pass": "p"}})


TMP = tempfile.mkdtemp(prefix="guizang-sync-atomic-")
CACHE = os.path.join(TMP, "cache")
BOOKS = os.path.join(TMP, "books")
os.makedirs(CACHE)
os.makedirs(os.path.join(BOOKS, "bk1"))

try:
    # ── 1. 中间产物不进清单 ────────────────────────────────────────
    chk("_finished_file：.part / .tmp 不算写完，大小写一样",
        S._finished_file("chapters.md") and S._finished_file("a.png")
        and not S._finished_file("chapters.md.part") and not S._finished_file("draft.md.tmp")
        and not S._finished_file("BOARD.TMP"), None)

    for name, blob in (("chapters.md", "正文"),
                       ("notes.json", "{}"),
                       ("draft.md.tmp", "写了一半"),
                       ("cover.png.part", "半截图"),
                       ("Board.TMP", "别的模块正在存")):
        with open(os.path.join(BOOKS, "bk1", name), "w", encoding="utf-8") as f:
            f.write(blob)

    pairs = dict(S._collect(CACHE, BOOKS, {"time": False, "records": False, "books": True}))
    chk("_collect：成品照旧列出来（两个真文件都在）",
        set(pairs) == {"guizang/books/bk1/chapters.md", "guizang/books/bk1/notes.json"},
        sorted(pairs))
    chk("_collect：.part / .tmp 一个都不进清单（断线的半截件不许被传上去）",
        not any(k.lower().endswith((".part", ".tmp")) for k in pairs), sorted(pairs))

    # ── 2. 取回落盘：原子替换 ──────────────────────────────────────
    readstat = os.path.join(CACHE, "readstat.json")
    cfg = dav_cfg()
    url = BASE + "/guizang/cache/readstat.json"

    tr = Transport()
    S._http = tr
    tr.remote[url] = b'{"from":"cloud"}'
    ok, msg = S.run(cfg, CACHE, BOOKS)
    chk("run：本地没有、远端有 → 取回来落盘",
        ok and "取回 1" in msg and open(readstat, "rb").read() == b'{"from":"cloud"}',
        (ok, msg))
    chk("run：取回成功后不留 .part", not os.path.exists(readstat + ".part"), readstat)

    # ── 3. 写盘中断：目标位置一个字都不许出现 ──────────────────────
    os.remove(readstat)
    real_replace = S.os.replace

    def replace_boom(*_a, **_kw):
        raise OSError("No space left on device")

    tr = Transport()
    S._http = tr
    tr.remote[url] = b'{"from":"cloud"}'
    S.os.replace = replace_boom
    try:
        ok, msg = S.run(cfg, CACHE, BOOKS)
    finally:
        S.os.replace = real_replace
    chk("run：改名失败算失败，不当成功糊过去",
        not ok and "失败 1" in msg, (ok, msg))
    chk("run：改名失败时目标位置不许留下半截文件（关键）",
        not os.path.exists(readstat), os.path.exists(readstat))
    chk("run：改名失败时 .part 也要收掉，不给书库留垃圾",
        not os.path.exists(readstat + ".part"), os.listdir(CACHE))

    # 断线之后能自己恢复：下一趟照常取，不会因为上一次的残留而卡住
    tr = Transport()
    S._http = tr
    tr.remote[url] = b'{"from":"cloud"}'
    ok, msg = S.run(cfg, CACHE, BOOKS)
    chk("run：中断过的这一趟，下一趟照样能取回来（不留后遗症）",
        ok and "取回 1" in msg and open(readstat, "rb").read() == b'{"from":"cloud"}',
        (ok, msg))

    # ── 4. 书库里的半截件绝不上传 ──────────────────────────────────
    # 上一趟同步断线留下的 .part，或写作稿正在写的 .tmp：本地「存在」但没写完。
    with open(os.path.join(BOOKS, "bk1", "chapters.md.part"), "w", encoding="utf-8") as f:
        f.write("半截")
    tr = Transport()
    S._http = tr
    ok, msg = S.run(dav_cfg(books=True), CACHE, BOOKS)
    puts = tr.urls("PUT")
    chk("run：书库里的 .part 不会被当成书上传",
        ok and not any(u.lower().endswith(".part") for u in puts), puts)
    chk("run：同一目录里的真文件照常上传（没被误伤）",
        any(u.endswith("/guizang/books/bk1/chapters.md") for u in puts), puts)

    # ── 5. 接线：取回这条路必须走原子落盘 ──────────────────────────
    src = open(os.path.join(ROOT, "sync.py"), encoding="utf-8").read()
    chk("接线：取回落盘不再直接 open(local, \"wb\")",
        'open(local, "wb")' not in src and "_write_atomic(local, got)" in src, None)
finally:
    S._http = ORIG_HTTP
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("云同步取回落盘体检：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
