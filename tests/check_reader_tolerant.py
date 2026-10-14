#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读 / 打包 / 整篇小结读章节文件时，一个坏字节不许把整条路打崩。

为什么单独一份
    `book_export.read_chapters` 早就把「章节文件是用户能手改的文本」这件事收了口
    （见 check_export_tolerant.py）：一个另存成 GBK 的章节、一次断电留下的半截字节，
    只把认不出的字节换成 U+FFFD，正文其余部分照旧。可读同一批 `chapters/*.md`
    的**还有界面这一侧** —— 阅读页取某一章、合并稿、单本 / 多本打包、剪藏 / 订阅
    / 视频这类「整篇小结」，各自再 `open(..., encoding="utf-8")` 读一遍，漏在外头。
    改之前实测（GBK 档一节）：

      · `ui_server.chapter_text()` 严格读失败被吞成 None，`GET /api/read?ch=…`
        回 404「没有这一章」—— 文件明明就在目录里，用户看到的是「这一章打不开」；
      · `ui_server.merged_markdown()` 的 UnicodeDecodeError 直接穿出 `do_GET`：
        `GET /api/zip` 连接被甩（「下载点了没反应」）、整篇 AI 小结报错。

收口只有一处
    新增 `book_export.read_chapter_file()`（按字节读、UTF-8 解、坏字节落替代符，
    读不到才回 None），`read_chapters`、`chapter_text`、`merged_markdown`、
    `book_outline` 四处都走它 —— 同一条规矩只留一份实现。

这一条钉五面
  A 对照：夹具确实按严格 UTF-8 解不开（防门禁空转）。
  B 单章：好章一字不差、坏章读得出且其余中文照在、读不到才回 None。
  C 合并稿 / 目录：不抛、坏章照样进合并稿、目录里的章名仍从正文首行取。
  D 真机：进程内起一个服务，`GET /api/read`（坏章回 200 而不是 404）与
    `GET /api/zip`（回 200 而不是甩连接）各打一次。
  E 回归：干净的书一个字符都不动、不许平白多出替代符。

跑法：python3 tests/check_reader_tolerant.py
不联网、不起浏览器、不开外部服务，归 --fast 静态段。
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-reader-tol-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_export                                            # noqa: E402
import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0
REPL = "\ufffd"

GOOD1 = "# 好的一章\n\n这是干净的中文正文 🧭。\n"
GOOD2 = "# 好的二章\n\n第二章也没有坏字节。\n"
# 章名是干净 UTF-8、只有尾巴上挂着两个认不出的字节：这样才分得清「坏字节只坏那几个字」
# 与「整章被丢弃 / 整章变成空串」。
BAD_TAIL = "# 坏章\n\n认得出的正文在这里。\n".encode("utf-8") + b"\xff\xfe not utf8"
# 另一种最常见的现场：整节另存成 GBK（'坏' 是 0xBB 0xB5，0xBB 在 UTF-8 里是续接字节）。
BAD_GBK = "# 坏章\n\n".encode("gbk") + "UTF-8 正文照留。\n".encode("utf-8")


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def call(fn, *a, **k):
    """把入口调一次：抛了就把异常当结果交出去，别让整条套件倒在第一发上。"""
    try:
        return fn(*a, **k), None
    except Exception as e:                                   # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def write(path, data):
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


try:
    # ── 夹具：一本两好两坏的书 + 一本全好的书 ─────────────────────
    books = os.environ["GUIZANG_BOOKS"]
    book = os.path.join(books, "bookA")
    ch = os.path.join(book, "chapters")
    os.makedirs(ch)
    write(os.path.join(ch, "0001.md"), GOOD1)
    write(os.path.join(ch, "0002.md"), BAD_TAIL)
    write(os.path.join(ch, "0003.md"), GOOD2)
    write(os.path.join(ch, "0005.md"), BAD_GBK)
    write(os.path.join(ch, "notes.txt"), "不是章节，不许被读进来")
    os.makedirs(os.path.join(ch, "0004.md"))          # 与 .md 同名的目录：只跳过不炸

    clean = os.path.join(books, "bookB")
    cch = os.path.join(clean, "chapters")
    os.makedirs(cch)
    write(os.path.join(cch, "0001.md"), GOOD1)
    write(os.path.join(cch, "0002.md"), GOOD2)

    # ── A 对照：这两个坏章节按严格 UTF-8 确实解不开 ────────────────
    strict_bad = 0
    for fn in ("0002.md", "0005.md"):
        try:
            with open(os.path.join(ch, fn), encoding="utf-8") as f:
                f.read()
        except UnicodeDecodeError:
            strict_bad += 1
    chk("A1 对照：坏章节按严格 UTF-8 确实解不开（夹具不是空的）", strict_bad == 2, strict_bad)

    # ── B 单章读法：坏字节不抛、其余照留、读不到才 None ─────────────
    ok1, err1 = call(book_export.read_chapter_file, os.path.join(ch, "0002.md"))
    chk("B1 尾挂坏字节：读得出来、不抛", err1 is None and isinstance(ok1, str), (err1, repr(ok1)[:60]))
    chk("B2 尾挂坏字节：章名与能认出的正文都在",
        isinstance(ok1, str) and ok1.startswith("# 坏章") and "认得出的正文在这里" in ok1,
        repr(ok1)[:80])
    chk("B3 尾挂坏字节：认不出的字节落成 U+FFFD 而不是被吞掉",
        isinstance(ok1, str) and REPL in ok1, repr(ok1)[:80])

    ok2, err2 = call(book_export.read_chapter_file, os.path.join(ch, "0005.md"))
    chk("B4 GBK 整节：读得出来、正文里的 UTF-8 照留",
        err2 is None and isinstance(ok2, str) and "UTF-8 正文照留" in ok2, (err2, repr(ok2)[:60]))
    chk("B5 GBK 整节：坏字节落成 U+FFFD", isinstance(ok2, str) and REPL in ok2, repr(ok2)[:60])

    chk("B6 好章节一字不差",
        book_export.read_chapter_file(os.path.join(ch, "0001.md")) == GOOD1)
    chk("B7 读不到（文件不存在）才回 None",
        book_export.read_chapter_file(os.path.join(ch, "0999.md")) is None)
    chk("B8 同名目录（不是文件）回 None 而不是抛",
        book_export.read_chapter_file(os.path.join(ch, "0004.md")) is None)

    got = book_export.read_chapters(book)
    names = [n for n, _ in got]
    chk("B9 不丢章：五个 .md 里读回四个真章节（同名目录只跳过）",
        names == ["0001.md", "0002.md", "0003.md", "0005.md"], names)
    chk("B10 不误收：.txt 不是章节", all(n.endswith(".md") for n in names), names)

    # ── C 产品读路径：单章正文 / 合并稿 / 目录 ─────────────────────
    ct, err = call(ui_server.chapter_text, "bookA", "0002.md")
    chk("C1 /api/read 取坏章：回得来正文（旧行为是 None → 404）",
        err is None and isinstance(ct, str) and "坏章" in ct, (err, repr(ct)[:60]))
    chk("C2 /api/read 取坏章：替代符在、认得出的中文在",
        isinstance(ct, str) and REPL in ct and "认得出的正文在这里" in ct, repr(ct)[:80])
    chk("C3 /api/read 取好章：一字不差", ui_server.chapter_text("bookA", "0001.md") == GOOD1)
    ct_gbk, err_gbk = call(ui_server.chapter_text, "bookA", "0005.md")
    chk("C4 /api/read 取 GBK 整节：也不回 None",
        err_gbk is None and isinstance(ct_gbk, str) and "UTF-8 正文照留" in ct_gbk,
        (err_gbk, repr(ct_gbk)[:60]))
    chk("C5 路径护栏没被放过：带 .. 的章名照旧回 None",
        ui_server.chapter_text("bookA", "../0001.md") is None)
    chk("C6 不存在的章回 None", ui_server.chapter_text("bookA", "0999.md") is None)

    md, err = call(ui_server.merged_markdown, "bookA")
    chk("C7 合并稿：不抛，坏章不再把整份合并稿打崩", err is None and isinstance(md, str),
        (err, repr(md)[:60]))
    ok_md = (isinstance(md, str) and GOOD1.strip() in md and GOOD2.strip() in md
             and "坏章" in md and REPL in md and "UTF-8 正文照留" in md)
    chk("C8 合并稿：两好两坏四章都在，好章原样、坏章带替代符", ok_md, repr(md)[:120])

    out, err = call(ui_server.book_outline, "bookA")
    titles = {c.get("file"): c.get("title") for c in (out or {}).get("chapters", [])}
    chk("C9 目录：不抛", err is None and isinstance(out, dict), (err, repr(out)[:60]))
    chk("C10 目录：坏章的章名仍从正文首行取（旧行为是空串 → 「第 N 章」）",
        titles.get("0002.md") == "坏章", titles)
    chk("C11 目录：好章的章名不受影响", titles.get("0001.md") == "好的一章", titles)

    # ── D 真机：进程内起一个服务，/api/read 与 /api/zip 各打一次 ────
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % port

        def get(path):
            r = urllib.request.urlopen(base + path, timeout=15)
            return r.status, r.read()

        try:
            st, body = get("/api/read?book=bookA&ch=0002.md")
            data = json.loads(body)
            chk("D1 /api/read 坏章回 200 + JSON（旧行为是 404「没有这一章」）",
                st == 200 and data.get("ok") is True, (st, body[:80]))
            chk("D2 /api/read 坏章回的是真正文（替代符 + 认得出的中文）",
                REPL in (data.get("md") or "") and "坏章" in (data.get("md") or ""),
                repr(data.get("md"))[:80])
        except Exception as e:                               # noqa: BLE001
            chk("D1 /api/read 坏章回 200 + JSON（旧行为是 404「没有这一章」）", False,
                "%s: %s" % (type(e).__name__, e))
            chk("D2 /api/read 坏章回的是真正文（替代符 + 认得出的中文）", False, "见 D1")

        try:
            st, body = get("/api/read?book=bookA&ch=0005.md")
            chk("D3 /api/read GBK 整节也回 200", st == 200 and json.loads(body)["ok"] is True,
                (st, body[:80]))
        except Exception as e:                               # noqa: BLE001
            chk("D3 /api/read GBK 整节也回 200", False, "%s: %s" % (type(e).__name__, e))

        try:
            st, body = get("/api/read?book=bookA&ch=0001.md")
            chk("D4 /api/read 好章回 200 且正文一字不差",
                st == 200 and json.loads(body).get("md") == GOOD1, st)
        except Exception as e:                               # noqa: BLE001
            chk("D4 /api/read 好章回 200 且正文一字不差", False, "%s: %s" % (type(e).__name__, e))

        try:
            st, body = get("/api/read?book=bookA&ch=../0001.md")
            chk("D5 /api/read 路径护栏：带 .. 的回 404", st == 404, st)
        except urllib.error.HTTPError as e:
            chk("D5 /api/read 路径护栏：带 .. 的回 404", e.code == 404, e.code)
        except Exception as e:                               # noqa: BLE001
            chk("D5 /api/read 路径护栏：带 .. 的回 404", False, "%s: %s" % (type(e).__name__, e))

        try:
            st, body = get("/api/zip?book=bookA")
            with zipfile.ZipFile(__import__("io").BytesIO(body)) as z:
                names = z.namelist()
                text = z.read(names[0]).decode("utf-8")
            chk("D6 /api/zip 回 200 而不是甩连接（合并稿不再抛）",
                st == 200 and len(names) >= 1, (st, len(body)))
            chk("D7 /api/zip 包内合并稿两好两坏都在、好章原样",
                GOOD1.strip() in text and GOOD2.strip() in text and "坏章" in text,
                repr(text)[:100])
        except Exception as e:                               # noqa: BLE001
            chk("D6 /api/zip 回 200 而不是甩连接（合并稿不再抛）", False,
                "%s: %s" % (type(e).__name__, e))
            chk("D7 /api/zip 包内合并稿两好两坏都在、好章原样", False, "见 D6")
    finally:
        srv.shutdown()
        srv.server_close()

    # ── E 回归：干净的书一个字符都不动、不多出替代符 ────────────────
    cmd, err = call(ui_server.merged_markdown, "bookB")
    chk("E1 回归：干净合并稿里两章原样、零替代符",
        err is None and GOOD1.strip() in cmd and GOOD2.strip() in cmd and REPL not in cmd,
        (err, repr(cmd)[:100]))
    chk("E2 回归：干净单章与源码字符串完全相同",
        ui_server.chapter_text("bookB", "0002.md") == GOOD2)
    chk("E3 回归：干净书目每章都有真章名",
        [c["title"] for c in (ui_server.book_outline("bookB") or {}).get("chapters", [])]
        == ["好的一章", "好的二章"])
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("阅读 / 打包读章节容错：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
print("通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
sys.exit(1 if FAIL else 0)
