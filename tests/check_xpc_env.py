#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：macOS 的 XPC_FLAGS 消毒（platform_compat.sanitize_browser_env）。

背景（2026-10-15 报的那组症状）：
「微信读书连不上、浏览器窗口弹不出来、一堆功能失效」。根因是宿主（Cursor、
被 launchd 托管的 .app）往环境里塞了 `XPC_FLAGS=0x2` —— 那是「本进程由
launchd/XPC 代管」的标记。带着它 `fork` 出来的 Chromium 会被系统立刻收走，
Playwright 只抛一句 `Target page, context or browser has been closed`，
从这句话根本看不出跟那个标记有关。实测：`0x2`/`0x3` 必失败，`0x0`/`0x1`/`0x4`/
不设都正常，所以只该清 `0x2` 这一位。

这一套把「哪几位该清、哪几位该留、边界怎么写不炸」钉住：
不起服务、不开浏览器、不联网，全在内存里算。
"""
import os
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_compat as pc  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


def sanitized(raw, is_mac=True):
    """在给定的 XPC_FLAGS 下问一次消毒结果（回改后的那份 dict）。"""
    env = {} if raw is None else {"XPC_FLAGS": raw}
    with mock.patch.object(pc, "IS_MAC", is_mac):
        return pc.sanitize_browser_env(env)


# ── 1. 该清的位：0x2 必须消失，其它位原样留着 ────────────────────
chk("0x2：清掉后是 0x0", sanitized("0x2").get("XPC_FLAGS") == "0x0",
    sanitized("0x2"))
chk("0x3（0x1|0x2）：只清 0x2，留下 0x1", sanitized("0x3").get("XPC_FLAGS") == "0x1",
    sanitized("0x3"))
chk("0x6（0x2|0x4）：只清 0x2，留下 0x4", sanitized("0x6").get("XPC_FLAGS") == "0x4",
    sanitized("0x6"))
chk("0x7：清成 0x5", sanitized("0x7").get("XPC_FLAGS") == "0x5",
    sanitized("0x7"))

# ── 2. 不该动的：没有 0x2、或压根没这个变量 ─────────────────────
chk("无 0x2：0x0 原样", sanitized("0x0").get("XPC_FLAGS") == "0x0")
chk("无 0x2：0x1 原样", sanitized("0x1").get("XPC_FLAGS") == "0x1")
chk("无 0x2：0x4 原样", sanitized("0x4").get("XPC_FLAGS") == "0x4")
chk("空串：值原样（不解析、不塞 0x0）", sanitized("").get("XPC_FLAGS") == "")
chk("变量不在：不塞新键", "XPC_FLAGS" not in sanitized(None))
chk("变量不在：返回的就是传进来的那份（不新建 dict）",
    (lambda e: pc.sanitize_browser_env(e) is e)({}))

# ── 3. 非 macOS：一律不动 ────────────────────────────────────────
chk("Linux/Windows：0x2 也不动", sanitized("0x2", is_mac=False).get("XPC_FLAGS") == "0x2",
    sanitized("0x2", is_mac=False))

# ── 4. 脏形状：认不出来就原样放过，绝不抛异常 ───────────────────
for bad in ("abc", "0xzz", "  ", "0x", "1 2", "x2"):
    try:
        out = sanitized(bad)
        ok = out.get("XPC_FLAGS") == bad
    except Exception as e:  # noqa: BLE001
        ok = False
        out = type(e).__name__
    chk("脏值 %r：原样放过、不抛" % bad, ok, out)

# 不带 0x 前缀的十进制写法也认（有些宿主直接写 "2"）
chk("十进制 '2'：同样清掉（认非 0x 写法）", sanitized("2").get("XPC_FLAGS") == "0x0",
    sanitized("2"))

# ── 5. 接线：真入口都过这把尺子 ─────────────────────────────────
def src(name):
    return (ROOT / name).read_text(encoding="utf-8")


SRC_PC = src("platform_compat.py")
SRC_LOGIN = src("login.py")
SRC_ANNA = src("anna_browser.py")

chk("接线：platform_compat 里定义了 sanitize_browser_env",
    "def sanitize_browser_env(" in SRC_PC)
chk("接线：platform_compat 导入时就地消毒一次（不等调用方记得调）",
    "\nsanitize_browser_env()" in SRC_PC)
chk("接线：login.py 导入 platform_compat（单独跑也消毒）",
    "import platform_compat" in SRC_LOGIN)
chk("接线：anna_browser.py 导入 platform_compat（单独跑也消毒）",
    "import platform_compat" in SRC_ANNA)

# ── 5b. 第二类同源故障：注入的 PLAYWRIGHT_BROWSERS_PATH 指向空目录 ──
# 光改 ms_playwright_dir 的回退不作数（Playwright 直接读环境变量），必须把变量本身纠回来。
chk("接线：platform_compat 里定义了 repair_browser_env",
    "def repair_browser_env(" in SRC_PC)
chk("接线：platform_compat 定义了 ensure_browser_env（消毒 + 修路径一把过）",
    "def ensure_browser_env(" in SRC_PC)
chk("接线：ms_playwright_dir 只读不判（保持纯 getter，无 import 副作用）",
    "_has_chromium(cur)" not in SRC_PC.split("def ms_playwright_dir", 1)[-1].split("def ", 1)[0])
chk("接线：不在 import 时自动改全局环境（避免顶掉调用方还没铺好的沙盒）",
    "\nrepair_browser_env()\n" not in SRC_PC)

SRC_UI = src("ui_server.py")
chk("接线：ui_server.main 起进程前先 ensure_browser_env（子进程继承干净环境）",
    "pc.ensure_browser_env()" in SRC_UI)
chk("接线：login.py 开浏览器前先 ensure_browser_env",
    "pc.ensure_browser_env()" in SRC_LOGIN)
chk("接线：anna_browser.py 开窗口前先 ensure_browser_env",
    "pc.ensure_browser_env()" in SRC_ANNA)


# 用临时目录造出「有浏览器 / 没浏览器」两种局面，别碰真实缓存。
import tempfile  # noqa: E402

_tmp_root = tempfile.mkdtemp(prefix="xpc-pw-")
_default = os.path.join(_tmp_root, "default")
_empty = os.path.join(_tmp_root, "empty")
_iso_inject = os.path.join(_tmp_root, "inject")
_iso_default = os.path.join(_tmp_root, "iso-default")
os.makedirs(os.path.join(_default, "chromium-1234"))
os.makedirs(os.path.join(_iso_inject, "chromium-9"))
os.makedirs(os.path.join(_iso_default, "chromium-9"))
os.makedirs(_empty)

# ── 血泪门禁（2026-10-15）─────────────────────────────────────
# ms_playwright_dir 一旦在环境变量已明确指向别处时还去回退到真实用户缓存，「卸载组件」
# 那条测试就会**真的删掉 ~/Library/Caches/ms-playwright 里的 Chromium** —— 已经真删过
# 一次（chromium-1243 被带走，只得重下 368MB）。这两条钉死 getter 的纯读语义：
with mock.patch.object(pc, "_platform_default_playwright_dir", return_value=_iso_default), \
        mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": _iso_inject}, clear=True):
    chk("安全：环境变量有效时只认它，绝不回退到默认/真实缓存",
        pc.ms_playwright_dir() == _iso_inject, pc.ms_playwright_dir())
with mock.patch.object(pc, "_platform_default_playwright_dir", return_value=_iso_default), \
        mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "/nope/nope"}, clear=True):
    chk("安全：getter 无副作用（不存在也原样返回，不偷偷换目录）",
        pc.ms_playwright_dir() == "/nope/nope", pc.ms_playwright_dir())


def repair_with(raw, default_dir):
    """在给定注入值 + 给定默认目录下跑一次 repair，返回改后的 inject 值。"""
    with mock.patch.object(pc, "_platform_default_playwright_dir", return_value=default_dir), \
            mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": raw}, clear=True):
        pc.repair_browser_env()
        return os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


# 注入路径不存在 + 默认位置有浏览器 → 纠回默认
chk("坏路径：纠回平台默认位置",
    repair_with("/definitely/missing", _default) == _default,
    repair_with("/definitely/missing", _default))
# 注入路径存在（空）+ 默认位置有 → 也纠回
chk("空目录：纠回平台默认位置",
    repair_with(_empty, _default) == _default)
# 注入路径真有浏览器（.app 打包场景）→ 一字不改
_bundled = os.path.join(_tmp_root, "appbundled")
os.makedirs(os.path.join(_bundled, "chromium-1234"))
chk("真目录（.app 场景）：原样保留，不被顶掉",
    repair_with(_bundled, _default) == _bundled)
# 坏路径 + 默认位置也没有浏览器 → 原样不动（不臆造）
chk("坏路径 + 默认也没有：原样不动（不臆造）",
    repair_with("/definitely/missing", os.path.join(_tmp_root, "nope")) == "/definitely/missing")
# 变量是 "0"（官方「不接管」语义）→ 不碰
chk("值为 0（官方不接管）：不碰", repair_with("0", _default) == "0")

# ensure_browser_env 是两者的合体：一次调用要把 XPC 和坏路径都收掉
with mock.patch.object(pc, "_platform_default_playwright_dir", return_value=_default), \
        mock.patch.dict(os.environ, {"XPC_FLAGS": "0x2",
                                     "PLAYWRIGHT_BROWSERS_PATH": "/definitely/missing"},
                        clear=True), \
        mock.patch.object(pc, "IS_MAC", True):
    pc.ensure_browser_env()
    chk("ensure_browser_env：XPC 与坏路径一次都修",
        os.environ.get("XPC_FLAGS") == "0x0"
        and os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == _default,
        (os.environ.get("XPC_FLAGS"), os.environ.get("PLAYWRIGHT_BROWSERS_PATH")))




# ── 6. import 副作用真的生效：起一个干净子解释器，塞 0x2 进去 ────
# 上面都是「读源码」，这一条是「真跑一遍」——只有它才证明 import 那一刻确实改了环境。
import subprocess  # noqa: E402

code = (
    "import os, sys; sys.path.insert(0, %r)\n"
    "os.environ['XPC_FLAGS'] = '0x2'\n"
    "import platform_compat\n"
    "print('AFTER', os.environ.get('XPC_FLAGS'))\n" % str(ROOT)
)
env = {"HOME": os.path.expanduser("~"),
       "PATH": "/usr/bin:/bin",
       "XPC_FLAGS": "0x2"}
try:
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=30)
    got = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("AFTER")]
    val = got[0].split(" ", 1)[1] if got else "(无输出)"
    if sys.platform == "darwin":
        chk("真跑：子解释器里 import 后 XPC_FLAGS 已变 0x0", val == "0x0", val)
    else:
        chk("真跑：非 macOS 上不改（0x2 原样）", val == "0x2", val)
except Exception as e:  # noqa: BLE001
    chk("真跑：子解释器能起来并输出", False, repr(e))

print("\nXPC 环境消毒门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
