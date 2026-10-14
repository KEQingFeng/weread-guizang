#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：书目录 meta.json 里落单的半个代理码点，不许等到打包 / 导出那一刻才炸。

为什么会有这种档
    `json` 收得下 `"\\ud800"` 这种转义 —— 读回来就是落单的半个代理码点
    （U+D800–U+DFFF，不成对）。云同步从别处落一份 meta.json 下来、用户手工编辑、
    旧版本写过别的形状，都可能让一本书的身份里带上它。而这个码点在 utf-8 里编不
    出来：`zipfile.writestr` 会在 `data.encode("utf-8")` 上抛 UnicodeEncodeError，
    `urllib.parse.quote` 同样抛，书目录写盘同理。

实测的故障（改之前）
    把某本书 `meta.json` 的 `title` 写成 `"坏\\ud800书名"` 之后：

      · `GET /api/zip?book=…`  → 连接被甩（`RemoteDisconnected`）：`build_zip` 里
        `zipfile.writestr` 抛出，异常穿出 `do_GET`，浏览器只看见「下载点了没反应」；
      · `GET /api/epub` / `/api/pdf` → 500 `'utf-8' codec can't encode … surrogates
        not allowed`（导出用的标题 / 作者来自同一份 meta）；
      · `ui_server.content_disp(标题, "zip")` 自己也会抛 —— 而它是在发响应头的路上。

收口只有一处
    读一本书身份的唯一入口是 `book_layout.read_meta()`，所以文本值的清洗放这里
    （形状仍是老的规矩：不是对象回空表）。下游的打包 / 导出 / 下载头 / 书架清单
    因此都不用各防一遍；`content_disp` 作为 HTTP 头那道边界，另外自己兜一层
    「名字不是字符串 / 还夹着半个代理码点」也不再抛。

跑法：纯函数 + 系统临时目录沙盒 + 一个进程内的真 HTTP 服务，不联网、不开浏览器、
不起外部服务，归 --fast 静态段。
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-meta-srg-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_layout                                            # noqa: E402
import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def call(fn, *a, **k):
    """把入口调一次：抛了就把异常当结果交出去，别让整条套件倒在第一发上。"""
    try:
        return fn(*a, **k), None
    except Exception as e:                       # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


BAD_TITLE = "坏\ud800书名"
GOOD_TITLE = "正常书名"
EMOJI = "🧑‍💻"           # ZWJ 是 emoji 的组成笔画，不是落单码点，绝不许被洗掉

try:
    # ── 0. 前提：这个值真的是 JSON 读得出来、utf-8 编不出来的 ──────────
    chk("J1 转义 \\ud800 能被 json 读成半个代理码点",
        json.loads('"\\ud800"') == "\ud800")
    chk("J2 半个代理码点确实编不进 utf-8",
        call(lambda: "\ud800".encode("utf-8"))[1] is not None)
    chk("J3 quote() 碰到它当场抛（这就是下载头那条路的病根）",
        call(lambda: __import__("urllib.parse", fromlist=["quote"]).quote(BAD_TITLE))[1]
        is not None)

    # ── 1. 纯函数：drop_lone_surrogates ───────────────────────────────
    chk("F1 单个字符串：半个代理码点摘掉，别的字一个不动",
        book_layout.drop_lone_surrogates(BAD_TITLE) == "坏书名",
        book_layout.drop_lone_surrogates(BAD_TITLE))
    chk("F2 成对的 emoji 不许误伤", book_layout.drop_lone_surrogates(EMOJI) == EMOJI)
    chk("F3 字典 / 列表逐层走，非字符串类型原样回",
        book_layout.drop_lone_surrogates(
            {"a": BAD_TITLE, "b": [1, "x\udfffy"], "c": 5, "d": None})
        == {"a": "坏书名", "b": [1, "xy"], "c": 5, "d": None})

    # ── 2. read_meta：一本书身份的唯一读口 ────────────────────────────
    book_id = "srg_test_book"
    bd = os.path.join(ui_server.OUT_DIR, book_id)
    os.makedirs(os.path.join(bd, "chapters"), exist_ok=True)
    with open(os.path.join(bd, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write("# %s\n\n正文一段。\n" % GOOD_TITLE)
    # ensure_ascii=True：写出 `\ud800` 转义 —— 合法 JSON 字节，读回来就是半个代理码点
    with open(os.path.join(bd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": BAD_TITLE, "author": "作者\ud800署", "source": "local",
                   "tags": ["标\ud800签", "好标签"]}, f, ensure_ascii=True, indent=2)
    m = book_layout.read_meta(bd)
    chk("R1 脏 meta：title / author / 列表里的字符串都洗干净了",
        m.get("title") == "坏书名" and m.get("author") == "作者署"
        and m.get("tags") == ["标签", "好标签"], m)
    chk("R2 脏 meta：认得出的字段一个不丢", m.get("source") == "local", m)

    gd = os.path.join(ui_server.OUT_DIR, "srg_good_book")
    os.makedirs(gd, exist_ok=True)
    with open(os.path.join(gd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": GOOD_TITLE + EMOJI, "author": "某某"}, f, ensure_ascii=False)
    chk("R3 干净 meta：一个字不改（含成对 emoji）",
        book_layout.read_meta(gd) == {"title": GOOD_TITLE + EMOJI, "author": "某某"},
        book_layout.read_meta(gd))
    for label, shape in (("数组", []), ("字符串", "书名"), ("数字", 123), ("null", None)):
        d = os.path.join(ui_server.OUT_DIR, "srg_shape")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(shape, f)
        chk("R4 脏形状「%s」仍回空表（老规矩不许回退）" % label,
            book_layout.read_meta(d) == {})
    chk("R5 meta 不在 / 目录是 None 都不抛",
        book_layout.read_meta(os.path.join(ROOT, "nope")) == {}
        and book_layout.read_meta(None) == {})

    # ── 3. content_disp：HTTP 头这道边界自己不许炸 ────────────────────
    cd, err = call(ui_server.content_disp, BAD_TITLE, "zip")
    chk("H1 半个代理码点的书名不再抛，且头是 latin-1 编得出来的",
        err is None and cd.encode("latin-1") is not None, (cd, err))
    chk("H2 filename* 里没有裸的代理码点",
        err is None and "\ud800" not in cd, cd)
    chk("H3 中文书名照旧走 RFC 5987（quote 后是百分号编码）",
        "%E6%AD%A3%E5%B8%B8%E4%B9%A6%E5%90%8D" in ui_server.content_disp(GOOD_TITLE, "epub"))
    for weird in (None, 0, 12345, ""):
        _, e = call(ui_server.content_disp, weird, "zip")
        chk("H4 非字符串名字 %r 也不抛" % (weird,), e is None, e)

    # ── 4. 产品读路径：合并稿 / 打包 / 导出不再带走整本书 ─────────────
    md, err = call(ui_server.merged_markdown, book_id)
    chk("P1 合并稿出得来、编得成 utf-8，开头是洗过的书名",
        err is None and md.encode("utf-8") and md.startswith("# 坏书名\n"), (err, (md or "")[:20]))

    zpath, err = call(ui_server.build_zip, book_id)
    ok_zip = False
    if err is None and zpath:
        try:
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                text = z.read(names[0]).decode("utf-8")
            ok_zip = (not any("\ud800" in n for n in names)
                      and text.startswith("# 坏书名"))
        except Exception as e:                   # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
    chk("P2 打包出得来、zip 里的文件名与正文都没有半个代理码点",
        err is None and ok_zip, (err, zpath))

    epath, err = call(ui_server.export_book, "epub", book_id)
    ok_epub = False
    if err is None and epath:
        try:
            with zipfile.ZipFile(epath) as z:
                names = z.namelist()
                ok_epub = "mimetype" in names and z.read("mimetype") == b"application/epub+zip"
        except Exception as e:                   # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
    chk("P3 EPUB 导得出来（mimetype 对），不是 500", err is None and ok_epub, (err, epath))

    # ── 5. 真机：进程内起一个服务，真发一次 /api/zip ──────────────────
    # 这一条钉的正是「改之前是 RemoteDisconnected」——纯函数绿了也可能在
    # do_GET 里照样把连接甩掉，所以非真发一次不可。
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.urlopen(
            "http://127.0.0.1:%d/api/zip?book=%s" % (port, book_id), timeout=15)
        body = req.read()
        disp = req.headers.get("Content-Disposition") or ""
        chk("S1 /api/zip 回 200 而不是甩连接（RemoteDisconnected）",
            req.status == 200 and len(body) > 0, (req.status, len(body)))
        chk("S2 响应头带着下载名，且是 latin-1 可编码的",
            "attachment;" in disp and disp.encode("latin-1") is not None, disp)
        chk("S3 下载头里没有裸的代理码点", "\ud800" not in disp, disp)
    except Exception as e:                       # noqa: BLE001
        chk("S1 /api/zip 回 200 而不是甩连接（RemoteDisconnected）", False,
            "%s: %s" % (type(e).__name__, e))
        chk("S2 响应头带着下载名，且是 latin-1 可编码的", False, "见 S1")
        chk("S3 下载头里没有裸的代理码点", False, "见 S1")
    finally:
        srv.shutdown()
        srv.server_close()
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("书目录 meta 半个代理码点体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("  ✗ " + "、".join(FAIL))
sys.exit(1 if FAIL else 0)
