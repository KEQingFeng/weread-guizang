# -*- coding: utf-8 -*-
"""插图并发下载（download_images.py）：坏清单不牵连、不乱写目录外、半截不下线。

download_images.py 是界面「补图」那颗按钮背后真正跑的脚本（ui_server.py action=images），
此前一条测试都没有。它读的是磁盘上的 raw/*.json —— 可能来自旧版本、可能被人手改过、
也可能是上次导出被中途杀掉时留下的半截文件 —— 所以这里钉的不是「下得下来」（那要联网），
而是几件离线也验得实的事：

  A 采集 collect_tasks —— 半截 JSON / 非字典清单 / 缺网址 / 文件名带目录，一律只跳过
    只计数，绝不带着调用栈把整个补图任务打死（一份坏清单不许连累其它几章的图）。
  B 落盘 —— 先写 .part 再 os.replace（断电/被杀时 images/ 里不会留一张看着存在、
    长度够、其实缺尾的破图，然后被下次补图当成「已下好」永远跳过）。
  C 判重 —— 下载时认多大，下次「已下好」就得用同一把尺子：700 字节的小图不许每次补图
    都被重新敲一遍微信图床（原来两处一个 >500、一个 >1000）。
  D 穿越 —— raw 里记的 `file` 是外部输入，`../../..` 这种绝不能把字节写到书库外面；
    「不该取的坚决不取」在这里的形态是「那条记录连请求都不发」。
  E 接线 / 命令行 —— ui_server 的补图任务仍然起这个脚本；不给参数时给用法而不是崩栈。

网络那一层用「记账的假 urlopen」顶替（记下每一次被叫到的地址），睡眠也换成假的 ——
于是「重试真的重试了 3 次」「这张图我们根本没去访问」都断言得出来，整套秒级跑完。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOTDIR = os.path.dirname(HERE)
sys.path.insert(0, ROOTDIR)

SANDBOX = tempfile.mkdtemp(prefix="guizang-dl-img-")
BOOKS = os.path.join(SANDBOX, "books")
# 脚本在 import 那一刻就把 OUTPUT_DIR 定下来了，所以要抢在 import 之前指到沙盒里。
os.environ["GUIZANG_OUTPUT"] = BOOKS
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "cache")

import download_images as di                                      # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write_bytes(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def write_json(path, obj):
    write_bytes(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ── 记账的假网络 ────────────────────────────────────────────────
CALLS = []
BODIES = {}
BROKEN = set()


class _Resp(object):
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def fake_urlopen(req, timeout=None):
    url = getattr(req, "full_url", None) or str(req)
    CALLS.append(url)
    if url in BROKEN:
        raise OSError("连接被重置")
    return _Resp(BODIES.get(url, b""))


urllib.request.urlopen = fake_urlopen
# 只替掉 download_images 命名空间里的 time：重试不再真的等 1.5 秒，也不影响别的模块。
di.time = types.SimpleNamespace(sleep=lambda *_: None, time=time.time)

# os.replace 的探针：钉住「先写 .part 再改名」这个写法本身。
_real_replace = os.replace
REPLACED = []


def spy_replace(src, dst):
    REPLACED.append((src, dst))
    return _real_replace(src, dst)


os.replace = spy_replace

try:
    # ── A、采集：坏清单只跳过，不牵连 ─────────────────────────────
    a_raw = os.path.join(SANDBOX, "a", "raw")
    a_img = os.path.join(SANDBOX, "a", "images")
    write_json(os.path.join(a_raw, "ch0001.json"), {
        "title": "第一章", "images": [
            {"url": "https://cdn/a.jpg", "file": "ch0001_img01.jpg"},
            {"url": "https://cdn/b.jpg", "file": "ch0001_img02.jpg"}]})
    # 导出被中途杀掉留下的半截 JSON —— 原来这一份就让整个任务带调用栈死掉。
    write_bytes(os.path.join(a_raw, "broken.json"),
                b'{"images": [{"url": "https://cdn/c.jpg", "fil')
    write_json(os.path.join(a_raw, "toplist.json"), [])           # 合法 JSON，但不是字典
    write_bytes(os.path.join(a_raw, "junk.json"), b"not json at all")
    write_json(os.path.join(a_raw, "badrec.json"), {
        "images": [
            {"file": "x.jpg"},                                    # 缺网址
            {"url": "https://cdn/d.jpg"},                         # 缺文件名
            {"url": "https://cdn/e.jpg", "file": "../../../evil.jpg"},
            {"url": "https://cdn/f.jpg", "file": "sub/dir.jpg"},
            {"url": "https://cdn/g.jpg", "file": ".."},
            {"url": "https://cdn/h.jpg", "file": ""},
            "notadict",
            {"url": "https://cdn/i.jpg", "file": "C:win.jpg"},    # Windows 盘符
        ]})

    tasks, bad = di.collect_tasks(a_raw, a_img)
    chk("采集：好清单照收，两条都在（坏文件不许把好文件一起吞掉）",
        [t[0] for t in tasks] == ["https://cdn/a.jpg", "https://cdn/b.jpg"], tasks)
    chk("采集：3 份坏清单（半截 / 非字典 / 非 JSON）+ 8 条坏记录 = 11，一条不漏地计数",
        bad == 11, bad)
    chk("采集：落盘路径全在 images/ 里",
        all(os.path.dirname(p) == os.path.abspath(a_img) for _, p in tasks), tasks)

    chk("穿越：正常名字给出 images/ 下的路径",
        di.safe_image_path(a_img, "ch0001_img01.jpg")
        == os.path.join(os.path.abspath(a_img), "ch0001_img01.jpg"))
    for badname in ["../../../evil.jpg", "sub/dir.jpg", "..", ".", "", None,
                    "C:win.jpg", " ../../x.jpg "]:
        chk("穿越：%r 一律拒收" % (badname,),
            di.safe_image_path(a_img, badname) is None,
            di.safe_image_path(a_img, badname))

    # ── B/C/D、真跑一遍 main ────────────────────────────────────
    BOOK = "weread-book-1"
    raw_dir = os.path.join(BOOKS, BOOK, "raw")
    img_dir = os.path.join(BOOKS, BOOK, "images")
    big, small = b"B" * 1200, b"S" * 700
    BODIES.update({"https://cdn/big.jpg": big,
                   "https://cdn/small.jpg": small,
                   "https://cdn/tiny.jpg": b"T" * 100})
    BROKEN.add("https://cdn/dead.jpg")
    write_json(os.path.join(raw_dir, "ch0001.json"), {
        "title": "第一章", "images": [
            {"url": "https://cdn/big.jpg", "file": "ch0001_img01.jpg"},
            {"url": "https://cdn/small.jpg", "file": "ch0001_img02.jpg"},
            {"url": "https://cdn/tiny.jpg", "file": "ch0001_img03.jpg"},
            {"url": "https://cdn/dead.jpg", "file": "ch0001_img04.jpg"},
            {"url": "https://cdn/evil.jpg", "file": "../../../evil.jpg"}]})

    out1 = io.StringIO()
    _stdout = sys.stdout
    sys.stdout = out1
    try:
        di.main(BOOK)
    finally:
        sys.stdout = _stdout
    log1 = out1.getvalue()

    chk("落盘：下下来的图字节一比一（大图）",
        open(os.path.join(img_dir, "ch0001_img01.jpg"), "rb").read() == big)
    chk("落盘：700 字节的小图也收（>= 判据就认，不按大小挑三拣四）",
        open(os.path.join(img_dir, "ch0001_img02.jpg"), "rb").read() == small)
    chk("落盘：太小的回包（100 字节）不写盘",
        not os.path.exists(os.path.join(img_dir, "ch0001_img03.jpg")))
    chk("落盘：连不上的不写盘",
        not os.path.exists(os.path.join(img_dir, "ch0001_img04.jpg")))
    chk("落盘：images/ 里不留 .part 残渣（改名要么成、要么没有）",
        [p for p in os.listdir(img_dir) if p.endswith(".part")] == [],
        os.listdir(img_dir))
    chk("落盘：成功那两张都走了 os.replace，且源是 .part 临时名",
        sorted(REPLACED) == sorted([
            (os.path.join(img_dir, "ch0001_img01.jpg") + ".part",
             os.path.join(img_dir, "ch0001_img01.jpg")),
            (os.path.join(img_dir, "ch0001_img02.jpg") + ".part",
             os.path.join(img_dir, "ch0001_img02.jpg"))]), REPLACED)

    chk("穿越：带 ../../.. 的那条记录连请求都不发（不该取的坚决不取）",
        "https://cdn/evil.jpg" not in CALLS, CALLS)
    leaked = [os.path.join(dp, fn)
              for dp, _, fns in os.walk(SANDBOX) for fn in fns if "evil" in fn]
    chk("穿越：书库外面一张字节都没落下", leaked == [], leaked)

    chk("重试：连不上的地址试满 3 次才认输",
        CALLS.count("https://cdn/dead.jpg") == 3, CALLS.count("https://cdn/dead.jpg"))
    chk("重试：回包太小也试满 3 次（不是一次就放弃）",
        CALLS.count("https://cdn/tiny.jpg") == 3, CALLS.count("https://cdn/tiny.jpg"))
    chk("汇总：ok=2 skip=0 fail=2", "✅ 完成: ok=2 skip=0 fail=2" in log1, log1[-200:])
    chk("汇总：坏记录有明说的条数，不闷着",
        "有 1 条图片记录用不了" in log1, log1[:200])

    # 第二趟：已经在盘上的必须靠「同一把尺子」判成 skip —— 700 字节那张是重点。
    CALLS[:] = []
    REPLACED[:] = []
    out2 = io.StringIO()
    sys.stdout = out2
    try:
        di.main(BOOK)
    finally:
        sys.stdout = _stdout
    log2 = out2.getvalue()
    chk("判重：第二趟不再去敲已经下好的大图", "https://cdn/big.jpg" not in CALLS, CALLS)
    chk("判重：700 字节的小图也判成「已下好」，不再反复重下（原来 >1000 才算数）",
        "https://cdn/small.jpg" not in CALLS, CALLS)
    chk("判重：仍然下不下来的那两张照旧重试 3 次",
        CALLS.count("https://cdn/dead.jpg") == 3
        and CALLS.count("https://cdn/tiny.jpg") == 3, CALLS)
    chk("判重：第二趟汇总 ok=0 skip=2 fail=2",
        "✅ 完成: ok=0 skip=2 fail=2" in log2, log2[-200:])
finally:
    os.replace = _real_replace

# ── E、接线与命令行 ─────────────────────────────────────────────
ui = io.open(os.path.join(ROOTDIR, "ui_server.py"), encoding="utf-8").read()
chk("接线：界面的补图任务仍然起 download_images.py",
    'action == "images"' in ui and '"download_images.py"' in ui, "ui_server 里没接上")

proc = subprocess.run([sys.executable, os.path.join(ROOTDIR, "download_images.py")],
                      capture_output=True, text=True, timeout=60)
chk("命令行：不给参数时给用法并退 2，而不是抛 IndexError 栈",
    proc.returncode == 2 and "用法" in (proc.stderr + proc.stdout),
    (proc.returncode, proc.stderr[-200:], proc.stdout[-200:]))

shutil.rmtree(SANDBOX, ignore_errors=True)
print()
print("插图下载取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
