#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入坏 EPUB 的离线门禁：坏包 / 加密包不许再变成 500 与一句英文。

为什么单开这一条：`.epub` 这个扩展名一旦定了格式，`guess_format` 就不再回头看内容，
所以「这份文件其实不是个压缩包」只有 `read_epub` 那一层挡得住。旧代码里
`zipfile.BadZipFile` 会穿过 `import_book` 一路到 `/api/import` 的兜底 except：
用户拿到的是 **500 + `导入失败：File is not a zip file`**（安娜那条路更糟，账本上
记的是 `BadZipFile` 这个类名）。加密的 EPUB 同理（`RuntimeError: ... password
required`）。这一条把「压缩包坏法 → 人话的 ValueError」钉死，并守住好包一个字不变。

只喂内存里现造的字节（zipfile 手搓），不联网、不起服务、不碰真实书库；
全程在系统临时目录里。
"""
import io
import os
import pathlib
import shutil
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-epubbad-"))
os.environ["GUIZANG_DATA"] = str(SB)
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import book_import as B   # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("  ✗  " + name + "   " + repr(extra)[:220])


# ── 现造包：一份最小但完整的好 EPUB ──────────────────────────────
def build_epub(title="坏包测试"):
    buf = io.BytesIO()
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>%s</dc:title><dc:creator>某人</dc:creator></metadata>'
           '<manifest><item id="c0" href="c0.xhtml" '
           'media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="c0"/></spine></package>' % title)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c0.xhtml",
                   "<html><body><h1>第一章</h1><p>正文一段。</p></body></html>")
    return buf.getvalue()


def mark_encrypted(data):
    """把中央目录里每个条目的「加密」标志位立起来。

    标准库写不出加密 zip，而 zipfile 只看中央目录里的 flag_bits 判「要不要密码」；
    把那一位置上，read() 就会抛 RuntimeError —— 与真实加密 EPUB 走的是同一条路。
    只动 flag 那一个字节，压缩数据一个字不碰。
    """
    b = bytearray(data)
    i = b.find(b"PK\x01\x02")
    while i != -1:
        b[i + 8] |= 0x1
        i = b.find(b"PK\x01\x02", i + 4)
    return bytes(b)


def import_err(data, name="x.epub"):
    """跑一遍真入口，回异常本身（不装作导入成功）。"""
    try:
        B.import_book(str(SB / "lib"), name, data)
    except Exception as e:      # 这里就是要看它抛的是什么
        return e
    return None


# ── A. 坏压缩包：必须是人话的 ValueError ────────────────────────
BAD = [
    ("整份不是 zip", b"this is not a zip file at all"),
    ("只有 PK 头就断了", b"PK\x03\x04" + b"\x00" * 40),
    ("只有两个字 PK", b"PK"),
]
for label, blob in BAD:
    e = import_err(blob)
    chk("A 坏包（%s）抛的是 ValueError 而不是 BadZipFile" % label,
        isinstance(e, ValueError) and not isinstance(e, zipfile.BadZipFile),
        type(e).__name__ if e else None)
    msg = str(e or "")
    chk("A 坏包（%s）说的是中文人话，不夹 zipfile 英文原文" % label,
        "打不开" in msg and "is not a zip file" not in msg
        and "BadZipFile" not in msg and "Traceback" not in msg, msg)
    chk("A 坏包（%s）能指出「没下全或不是压缩包」" % label,
        "没下全" in msg or "不是一个 EPUB" in msg, msg)

# 空字节在更前面就被「这份文件是空的」拦下，不属于压缩包这一类
e0 = import_err(b"")
chk("A4 空字节仍是原来那句「这份文件是空的」，不误报成坏包",
    isinstance(e0, ValueError) and "空的" in str(e0), str(e0))

# 真 zip、但不是 EPUB：这条是原有的「没读出正文」，别被上面的收口吃掉
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("hello.txt", "just a zip")
e2 = import_err(buf.getvalue())
chk("A5 真 zip 但不是 EPUB → 仍是「没读出正文」那句，不误报成坏包",
    isinstance(e2, ValueError) and "没读出正文" in str(e2), str(e2))

# ── B. 加密条目：RuntimeError 也要翻成人话 ─────────────────────
enc = mark_encrypted(build_epub())
try:
    with zipfile.ZipFile(io.BytesIO(enc)) as _z:
        _z.read("OEBPS/content.opf")
    _raw_runtime_error = None
except RuntimeError as _e:
    _raw_runtime_error = _e
except Exception as _e:                                    # noqa: BLE001
    _raw_runtime_error = _e
chk("B0 加密标志位真让 zipfile 抛 RuntimeError",
    isinstance(_raw_runtime_error, RuntimeError), repr(_raw_runtime_error))

e3 = import_err(enc)
chk("B1 加密 EPUB → ValueError（不是 RuntimeError）",
    isinstance(e3, ValueError) and not isinstance(e3, RuntimeError), type(e3).__name__)
chk("B2 加密 EPUB 说的是「加了密」，不夹 password 原文",
    "加了密" in str(e3) and "password" not in str(e3), str(e3))

# ── C. read_epub 直连这一层（绕过 import_book）──────────────────
for label, exc in [("BadZipFile", zipfile.BadZipFile("File is not a zip file")),
                   ("RuntimeError", RuntimeError("File 'x' is encrypted, password required")),
                   ("NotImplementedError", NotImplementedError("compression type 9"))]:
    human = B.epub_zip_error(exc)
    chk("C epub_zip_error(%s) 给的是中文、不泄原文" % label,
        bool(human) and human == human.strip()
        and "password" not in human and "compression type" not in human
        and not human.startswith("File "), human)

try:
    B.read_epub(b"definitely not a zip")
    _direct = None
except Exception as _e:                                   # noqa: BLE001
    _direct = _e
chk("C4 read_epub 直接喂坏字节 → ValueError",
    isinstance(_direct, ValueError) and not isinstance(_direct, zipfile.BadZipFile),
    type(_direct).__name__)

# ── D. 好包一个字不变（收口不许把正常路也堵了）─────────────────
good = build_epub(title="好好的书")
docs, images, cover, meta = B.read_epub(good)
chk("D1 好 EPUB 照旧拆得出章节", len(docs) == 1 and "第一章" in docs[0][1],
    [d[0] for d in docs])
chk("D2 好 EPUB 的 OPF 元信息照旧读得出",
    meta.get("title") == "好好的书" and meta.get("creator") == "某人", meta)

info = B.import_book(str(SB / "lib"), "好好的书.epub", good)
chk("D3 好 EPUB 照旧导得成一本书", (info.get("chapters") or 0) >= 1, info)
chk("D4 落盘的正文里那句还在",
    any("正文一段" in open(os.path.join(info["dir"], "chapters", f),
                            encoding="utf-8").read()
        for f in os.listdir(os.path.join(info["dir"], "chapters"))))

# ── E. 接线：坏包这一路不许再有英文原文穿到界面 ────────────────
src = (ROOT / "book_import.py").read_text(encoding="utf-8")
chk("E1 read_epub 是收口那一层，真正的拆包在 _read_epub",
    "def read_epub(data):" in src and "def _read_epub(data):" in src)
chk("E2 import_book 走的是收口后的 read_epub",
    "docs, images, cover_orig, opf_meta = read_epub(data)" in src)
chk("E3 在路由收口之前就把 BadZipFile / RuntimeError 接住了",
    "zipfile.BadZipFile, RuntimeError" in src)

shutil.rmtree(SB, ignore_errors=True)
print()
print("导入坏 EPUB：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  坏包 / 加密包都翻成人话，好包与元信息一个字没动")
