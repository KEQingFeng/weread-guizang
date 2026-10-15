# -*- coding: utf-8 -*-
"""封面落盘的原子性：同一张封面被两处同时写时不许扑空，也不许留半张图 / 半个 .tmp。

为什么单列一条：`ui_server._write_json` 上面那段注释早就写死了「固定的 `xxx.tmp`
在多线程下会有一个 `os.replace` 扑空、直接抛错」，可封面这一档没跟上那条口径 ——
`cache_cover` 与「便签收进书架」用 `cover_path(id) + ".tmp"`，`ensure_cover` 更是
直接 `open(path, "wb")` 写目标文件。而封面恰恰是这个后端并发最高的读取口：一进
书架界面就同时发几十个 `/api/cover`，后台 `warm_covers` 那条线还在补同一批书，
详情页大图又会再敲一次。两处撞上同一本书时：

  · 固定临时名：先 `os.replace` 的把临时件搬走，后一个 replace 抛
    FileNotFoundError，下载明明成功了封面却没落上，旁边还留一个没人认领的 `.tmp`；
  · 直接写目标文件：中途进程被杀就留半张图，而读口只按 `getsize > 800` 认
    「有封面」，那张截断图会被一直当成真封面用下去，且再也不重取。

三段，全在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰用户真实书库：
  A 对照：固定 `.tmp` 名下第二个 replace 确实扑空（证明这条门禁不是空转）。
  B `_write_bytes`：并发写不抛、落下的永远是完整一份、不留 `.tmp`；写失败不毁旧档。
  C 接线：封面三个写点都走 `_write_bytes`，源码里再没有固定 `.tmp` / 直写目标文件。
  D 真跑：`cache_cover` 与 `ensure_cover` 两条路各落一次，字节与「不留 .tmp」一起验。
"""
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-cover-atomic-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0

PNG_HEAD = b"\x89PNG\r\n\x1a\n"


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
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。

    只看源码文本的断言用它比 `assert 'xxx' in SRC` 可靠：同一个词在别的函数里
    出现不该算数。够用就行，不做真正的语法分析。
    """
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def tmp_left(d):
    try:
        return [n for n in os.listdir(d) if n.endswith(".tmp")]
    except OSError:
        return []


# ── A、对照：固定 `.tmp` 名到底怎么坏的 ────────────────────────────
A = tempfile.mkdtemp(prefix="guizang-atomic-a-")
fixed_target = os.path.join(A, "cover.jpg")
fixed_tmp = fixed_target + ".tmp"

with open(fixed_tmp, "wb") as f:                              # 写者一
    f.write(b"A" * 1000)
with open(fixed_tmp, "wb") as f:                              # 写者二（同一个临时名）
    f.write(b"B" * 1000)
os.replace(fixed_tmp, fixed_target)                           # 写者一先顶替
race_err = None
try:
    os.replace(fixed_tmp, fixed_target)                       # 写者二：临时件已被搬走
except Exception as e:                                        # noqa: BLE001 — 就要抓原样这个
    race_err = e
chk("对照：固定 `.tmp` 名下第二个 os.replace 会扑空（FileNotFoundError）",
    isinstance(race_err, FileNotFoundError), race_err)
chk("对照：扑空的那一下没有留下 `.tmp`，失败看上去「什么也没发生」（最难查的一种）",
    not tmp_left(A) and os.path.isfile(fixed_target), os.listdir(A))
# 另一条更重的：直写目标文件（ensure_cover 的老写法）被杀在中途，会留一张半截图，
# 而读口只按 `getsize > 800` 认封面 —— 这张破图会被一直当成真封面用下去。
direct = os.path.join(A, "direct.jpg")
with open(direct, "wb") as f:
    f.write(PNG_HEAD + b"p" * 900)                            # 写到一半进程就没了
chk("对照：直写目标文件的半张图 >800 字节，读口会认它是「有封面」（再也不会重取）",
    os.path.getsize(direct) > 800)

# ── B、_write_bytes：并发不抛、落下必是完整一份、不留 .tmp ────────
B = tempfile.mkdtemp(prefix="guizang-atomic-b-")
btarget = os.path.join(B, "sub", "cover.jpg")                  # 连目录都还不存在
blobs = [bytes([65 + i]) * 1500 for i in range(6)]
berrs = []


def _writer(blob):
    try:
        ui_server._write_bytes(btarget, blob)
    except Exception as e:                                    # noqa: BLE001
        berrs.append(e)


threads = [threading.Thread(target=_writer, args=(b,)) for b in blobs]
for t in threads:
    t.start()
for t in threads:
    t.join(10)
got = open(btarget, "rb").read()
chk("并发：6 个写者一个都不抛（固定的 `.tmp` 名才会扑空）", not berrs, berrs)
chk("并发：父目录不存在也会被建出来（三个写点不必各自记得 mkdir）",
    os.path.isfile(btarget))
chk("并发：落下的永远是某一份完整内容（不是两份拼起来的半截）",
    got in blobs and len(got) == 1500, (len(got), got[:4]))
chk("并发：目录里没有留下没人认领的 .tmp（临时名带 pid + 随机串）",
    not tmp_left(os.path.dirname(btarget)), os.listdir(os.path.dirname(btarget)))

# 写失败：旧档不许被动；临时件要收干净（否则 cache/covers 会慢慢堆垃圾）
ui_server._write_bytes(btarget, b"good" * 300)
fail_err = None
try:
    ui_server._write_bytes(btarget, "这不是字节")             # f.write(str) → TypeError
except Exception as e:                                        # noqa: BLE001
    fail_err = e
chk("写失败：原样往上抛（调用方各自那层兜底去接，不许悄悄当成功）",
    fail_err is not None, fail_err)
chk("写失败：旧档一字不动（不是写坏一半）",
    open(btarget, "rb").read() == b"good" * 300)
chk("写失败：临时件收干净了，旁边不留 .tmp",
    not tmp_left(os.path.dirname(btarget)), os.listdir(os.path.dirname(btarget)))
# 反过来钉住上面那条对照：原子写失败时目标文件根本不该出现，读口就没机会认半截图。
fresh = os.path.join(B, "never.jpg")
try:
    ui_server._write_bytes(fresh, "还是不是字节")
except Exception:                                             # noqa: BLE001
    pass
chk("原子写失败：目标文件根本没出现（旧写法会在读口留下一张半截破图）",
    not os.path.exists(fresh), os.listdir(B))

# ── C、接线：三个封面写点都必须走这条出口 ────────────────────────
SRC = read_repo("ui_server.py")
wb = body_of(SRC, "_write_bytes")
chk("接线：_write_bytes 的临时名带 pid + 随机串（固定名正是扑空的根）",
    'os.getpid()' in wb and 'uuid.uuid4().hex' in wb, wb[:200])
chk("接线：_write_bytes 用 os.replace 原子顶替，失败把临时件删掉再抛",
    "os.replace(" in wb and "os.remove(tmp)" in wb, wb)

ec = body_of(SRC, "ensure_cover")
chk("接线：ensure_cover 改走 _write_bytes（原来是直接 open(...,\"wb\") 写目标文件）",
    "_write_bytes(" in ec, ec[:200])
chk("接线：ensure_cover 里再没有直接写封面目标文件那一段",
    not re.search(r'open\(\s*p\s*,\s*"wb"\)', ec), ec[-300:])

cc = body_of(SRC, "cache_cover")
chk("接线：cache_cover 改走 _write_bytes（原来用 cover_path + \".tmp\" 固定临时名）",
    "_write_bytes(" in cc, cc[:200])
chk("接线：cache_cover 里再没有固定临时名那一段",
    not re.search(r'p\s*\+\s*"\.tmp"', cc), cc[-300:])

chk("接线：便签「收进书架」拷封面也走 _write_bytes",
    '_write_bytes(cover_path(info["id"]), raw)' in SRC,
    "便签收书那条路还在用 cover_path(...)+'.tmp'")
chk("接线：源码里再没有 `cover_path(...) + \".tmp\"` 这种固定临时名",
    not re.search(r'cover_path\([^\n]*\)\s*\+\s*"\.tmp"', SRC))
chk("接线：源码里再没有直接往 cover_path(...) 目标文件写的写法",
    not re.search(r'open\(\s*cover_path\([^\n]*\)\s*,\s*"wb"\)', SRC))
chk("接线：渲染封面那条读口仍按字节下限认「有封面」（半张图不许被当真）",
    "os.path.getsize(p) > 800" in SRC, "getsize > 800 那道尺子被拆了")

# ── D、真跑两条路：字节对得上，且不留 .tmp ───────────────────────
PNG = PNG_HEAD + b"z" * 2000


class _Resp:
    def __init__(self, blob):
        self.blob = blob

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *_n):
        return self.blob


class fake_net:
    """顶替 urlopen，记账被叫到的地址；退出时原样还回去。"""

    def __init__(self, blob=PNG):
        self.blob = blob
        self.urls = []

    def __enter__(self):
        self._real = urllib.request.urlopen
        urllib.request.urlopen = self
        return self

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _Resp(self.blob)

    def __exit__(self, *a):
        urllib.request.urlopen = self._real
        return False


HERO = "https://img.first/atomic.jpg"
_real_safe = ui_server.clip_article.safe_image_url
_real_guard = ui_server.clip_article.ensure_redirect_guard
ui_server.clip_article.safe_image_url = lambda u: True
ui_server.clip_article.ensure_redirect_guard = lambda: None
try:
    cov_dir = os.path.dirname(ui_server.cover_path("video_ATOMIC1"))
    with fake_net() as net:
        ok = ui_server.cache_cover("video_ATOMIC1", HERO)
    p1 = ui_server.cover_path("video_ATOMIC1")
    chk("真跑·cache_cover：落下的就是取回来的那份字节",
        ok is True and os.path.isfile(p1) and open(p1, "rb").read() == PNG, net.urls)
    chk("真跑·cache_cover：落完之后 cache/covers 里没有残留 .tmp",
        not tmp_left(cov_dir), os.listdir(cov_dir))

    # ensure_cover：走微信读书官方 /book/info 拿地址那一条（原来的直写口）
    _real_key = ui_server.weread_key
    _real_call = ui_server.weread_call
    ui_server.weread_key = lambda: "k"
    ui_server.weread_call = lambda api, params=None, timeout=None: {"cover": HERO}
    try:
        with fake_net() as net2:
            p2 = ui_server.ensure_cover("video_ATOMIC2")
        chk("真跑·ensure_cover：官方封面地址取回来落到本地（不再是直写目标文件）",
            p2 == ui_server.cover_path("video_ATOMIC2")
            and os.path.isfile(p2) and open(p2, "rb").read() == PNG, net2.urls)
        chk("真跑·ensure_cover：落完之后 cache/covers 里没有残留 .tmp",
            not tmp_left(cov_dir), os.listdir(cov_dir))
        # 已经有封面（> 800 字节）就不再问一遍 —— 这道短路别被这次改动带坏
        ui_server.weread_call = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("本地已有封面不该再问微信读书"))
        with fake_net() as net3:
            again = ui_server.ensure_cover("video_ATOMIC2")
        chk("真跑·ensure_cover：本地已有封面就直接返回，不再敲一次官方接口",
            again == ui_server.cover_path("video_ATOMIC2") and net3.urls == [])
    finally:
        ui_server.weread_key = _real_key
        ui_server.weread_call = _real_call
finally:
    ui_server.clip_article.safe_image_url = _real_safe
    ui_server.clip_article.ensure_redirect_guard = _real_guard
    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.rmtree(A, ignore_errors=True)
    shutil.rmtree(B, ignore_errors=True)

print()
print("封面原子落盘取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
