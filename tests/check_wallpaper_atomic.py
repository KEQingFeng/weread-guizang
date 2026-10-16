#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""壁纸落盘的原子性：写到一半被杀 / 盘满不再把旧壁纸截成半张，两个上传各落各的。

为什么单开一条
    `POST /api/wallpaper` 是 `ThreadingHTTPServer` 上的一个请求线程口，
    `save_wallpaper` 原先直接 `with open(WALL_PATH, "wb") as f: f.write(raw)` 写
    **目标文件**，而读口（`GET /api/wallpaper` 与 `/api/state` 的
    `wallpaper.set`）只按 `os.path.isfile(WALL_PATH)` 认「有没有壁纸」，根本不看
    大小。这与封面那一格栽过的坑是同一形状（见 `check_cover_atomic.py`）：

      · 写到一半进程被杀 / 盘满：用户原来那张自定义壁纸被截成半张，界面从此挂一张
        永远打不开的破图，而「有壁纸」这个判断还是真的；
      · 两个标签页同时上传：两只手都用 `"wb"` 打开同一个文件，各按自己的偏移写，
        落下的是一份谁也不是的混合字节 —— 先写完的那份也保不住；
      · `cache/` 还没铺出来时（首次运行那一段），直写直接 `FileNotFoundError`，
        用户看到的是「壁纸保存失败」。

收口
    `save_wallpaper` 改走 `_write_bytes`（封面那一格已在用的二进制原子写口：
    临时名带 pid + 随机串、`os.replace` 顶替、失败收残件再抛、父目录不存在也建），
    并把 `_write_bytes` 的失败清理从 `except Exception` 提到 `except BaseException`
    （Ctrl-C / 关窗那一档也是「字节落了一半」的时点，与这一族其余写口一致）。
    `save_wallpaper` 里设 `wall_mime` 仍在写成功之后：写失败时配置与旧壁纸都不动。

这一条钉住
  A 正常写入：字节与 `wall_mime` 都对；
  B 对照：直写目标在写一半抛错时确实把旧壁纸截成半张（证明门禁不是空转）；
  C 原子：同样的失败下旧壁纸一字不动、旁边不留 `.tmp`、目标不出现；
  D 中止信号 `KeyboardInterrupt` 也收残件、也不毁旧档；
  E 并发：6 个上传一个都不抛，落下的必是某一份完整内容、目录不留 `.tmp`；
  F 父目录不存在也会被建出来（老写法 FileNotFoundError）；
  G 接线：源码里 `save_wallpaper` 不再直写 WALL_PATH、真走 `_write_bytes`，
    且 `_write_bytes` 的失败清理是 `BaseException`。

跑法：临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast 静态段。
"""
import builtins
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-wallpaper-atomic-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

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


def read_repo(fname):
    return io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                fname), encoding="utf-8").read()


def body_of(src, func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。够用就行。"""
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def strip_comments(src):
    """去掉整行注释：源码字面量断言不该被「讲解老写法的注释」带偏。"""
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def tmp_left(d):
    try:
        return [n for n in os.listdir(d) if n.endswith(".tmp")]
    except OSError:
        return []


def data_url(raw):
    import base64
    return "data:image/png;base64," + base64.b64encode(raw).decode()


PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 900
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 900
PNG_BLOBS = [b"\x89PNG\r\n\x1a\n" + bytes([65 + i]) * 1500 for i in range(6)]


class _BreakAfter:
    """把文件对象包一层：第一次 write 只写前 n 个字节，然后抛。"""

    def __init__(self, f, exc, n):
        self._f = f
        self._exc = exc
        self._n = n
        self._done = False

    def write(self, data):
        if not self._done:
            self._done = True
            self._f.write(data[:self._n])
            self._f.flush()
            raise self._exc
        return self._f.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._f.close()
        return False

    def __getattr__(self, name):
        return getattr(self._f, name)


@contextlib.contextmanager
def break_writes(exc, n=50):
    """沙盒里任何以写模式打开的文件，第一次 write 都只落一半再抛 exc。"""
    real_open = builtins.open

    def patched(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        try:
            inside = os.path.realpath(str(path)).startswith(os.path.realpath(ROOT))
        except Exception:                                     # noqa: BLE001
            inside = False
        if inside and any(ch in str(mode) for ch in "wa"):
            return _BreakAfter(f, exc, n)
        return f

    builtins.open = patched
    try:
        yield
    finally:
        builtins.open = real_open


def old_save_wallpaper(raw):
    """改之前 `save_wallpaper` 的落盘形状：直写目标文件。"""
    with open(ui_server.WALL_PATH, "wb") as f:
        f.write(raw)


WALL_DIR = os.path.dirname(ui_server.WALL_PATH)

# ── A、正常写入 ────────────────────────────────────────────────────
n = ui_server.save_wallpaper(data_url(PNG_A))
chk("A1 正常上传：回的是字节数", n == len(PNG_A), n)
chk("A2 正常上传：盘上就是那一份", open(ui_server.WALL_PATH, "rb").read() == PNG_A)
chk("A3 正常上传：wall_mime 按魔数记成 image/png",
    (ui_server.load_cfg().get("wall_mime") or "") == "image/png",
    ui_server.load_cfg().get("wall_mime"))
chk("A4 正常上传：目录里不留 .tmp", tmp_left(WALL_DIR) == [], tmp_left(WALL_DIR))

# ── B、对照：直写目标确实把旧壁纸截成半张 ──────────────────────────
ctrl_err = None
try:
    with break_writes(OSError("simulated disk full")):
        old_save_wallpaper(PNG_B)
except OSError as e:                                          # noqa: BLE001
    ctrl_err = e
ctrl_size = os.path.getsize(ui_server.WALL_PATH)
chk("B1 对照：直写目标在写一半抛错（改之前就是这么写的）", ctrl_err is not None, ctrl_err)
chk("B2 对照：旧壁纸当场被截成半张（读口只按 isfile 认，界面从此挂破图）",
    ctrl_size < len(PNG_A), ctrl_size)

# 恢复到一张完整的好壁纸，供后面几条用。
ui_server.save_wallpaper(data_url(PNG_A))
chk("B3 恢复：又是完整一份", open(ui_server.WALL_PATH, "rb").read() == PNG_A)

# ── C、原子：同样的失败不毁旧档、不留残件 ──────────────────────────
atomic_err = None
try:
    with break_writes(OSError("simulated disk full")):
        ui_server.save_wallpaper(data_url(PNG_B))
except OSError as e:                                          # noqa: BLE001
    atomic_err = e
chk("C1 写失败：原样往上抛（调用方那层兜底去接，不许悄悄当成功）",
    atomic_err is not None, atomic_err)
chk("C2 写失败：旧壁纸一字不动（不是写坏一半、也不是被搬走）",
    open(ui_server.WALL_PATH, "rb").read() == PNG_A)
chk("C3 写失败：临时件收干净，旁边不留 .tmp",
    tmp_left(WALL_DIR) == [], tmp_left(WALL_DIR))
chk("C4 写失败：配置那份 wall_mime 没被改（写成功才轮到它）",
    (ui_server.load_cfg().get("wall_mime") or "") == "image/png")

# ── D、中止信号（Ctrl-C / 关窗那一档）也收干净 ─────────────────────
kb_err = None
try:
    with break_writes(KeyboardInterrupt()):
        ui_server.save_wallpaper(data_url(PNG_B))
except BaseException as e:                                    # noqa: BLE001
    kb_err = e
chk("D1 中止信号往上抛（不是被吞掉）", isinstance(kb_err, KeyboardInterrupt), kb_err)
chk("D2 中止信号：旧壁纸仍然完好", open(ui_server.WALL_PATH, "rb").read() == PNG_A)
chk("D3 中止信号：临时件也收干净（BaseException 那一档）",
    tmp_left(WALL_DIR) == [], tmp_left(WALL_DIR))

# ── E、并发：6 个上传各写各的，落下的必是完整一份 ──────────────────
errs = []
seen = []


def _upload(blob):
    try:
        seen.append(ui_server.save_wallpaper(data_url(blob)))
    except BaseException as e:                                # noqa: BLE001
        errs.append(e)


threads = [threading.Thread(target=_upload, args=(b,)) for b in PNG_BLOBS]
for t in threads:
    t.start()
for t in threads:
    t.join(10)
final = open(ui_server.WALL_PATH, "rb").read()
chk("E1 并发：6 个写者一个都不抛（老写法是同一个文件互相截断）", not errs, errs)
chk("E2 并发：落下的永远是某一份完整内容（不是两份拼起来的半截）",
    final in PNG_BLOBS and len(final) == len(PNG_BLOBS[0]), (len(final), final[:8]))
chk("E3 并发：回的都是自己那一份的长度",
    sorted(seen) == [len(PNG_BLOBS[0])] * 6, seen)
chk("E4 并发：目录里没有没人认领的 .tmp",
    tmp_left(WALL_DIR) == [], tmp_left(WALL_DIR))

# ── F、父目录还没铺出来时也写得进去 ────────────────────────────────
saved_wall = ui_server.WALL_PATH
try:
    ui_server.WALL_PATH = os.path.join(ROOT, "fresh", "nested", "wallpaper.bin")
    miss_err = None
    try:
        ui_server.save_wallpaper(data_url(PNG_A))
    except BaseException as e:                                # noqa: BLE001
        miss_err = e
    chk("F1 父目录不存在也能落（老写法直写这里会 FileNotFoundError）",
        miss_err is None, miss_err)
    chk("F2 落在深目录里的也是完整一份",
        os.path.isfile(ui_server.WALL_PATH)
        and open(ui_server.WALL_PATH, "rb").read() == PNG_A)
finally:
    ui_server.WALL_PATH = saved_wall

# ── G、接线 ────────────────────────────────────────────────────────
SRC = read_repo("ui_server.py")
CODE = strip_comments(SRC)
sw = body_of(CODE, "save_wallpaper")
chk("G1 接线：save_wallpaper 真走 _write_bytes", "_write_bytes(WALL_PATH" in sw, sw[-400:])
chk("G2 接线：save_wallpaper 里再没有直接 open(WALL_PATH, \"wb\") 写目标",
    not re.search(r'open\(\s*WALL_PATH\s*,\s*"wb"', sw), sw[-400:])
wb = body_of(CODE, "_write_bytes")
chk("G3 接线：_write_bytes 的失败清理是 BaseException（中止信号也收残件）",
    "except BaseException:" in wb, wb[:400])
chk("G4 接线：_write_bytes 仍用 os.replace 原子顶替、失败把临时件删掉再抛",
    "os.replace(" in wb and "os.remove(tmp)" in wb, wb[:400])
chk("G5 接线：全仓再没有直写 WALL_PATH 的地方（读口 open(WALL_PATH, \"rb\") 不算）",
    not re.search(r'open\(\s*WALL_PATH\s*,\s*"[wa]', CODE),
    re.findall(r'(?m)^.*open\(\s*WALL_PATH.*$', CODE))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("壁纸原子落盘门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
