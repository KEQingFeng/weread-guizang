#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：首次运行那一步的环境判断（bootstrap.py）。

这个脚本是用户第一次点「我思故我在」时跑的东西，它要先回答一个问题再决定做不做：
**Chromium 到底装了没。** 答错成「没装」，用户每点一次就重下 368MB；答错成「装了」，
进界面后取书全废。所以这里的判据必须和另外三处问的是同一个地方：

    · 后端       ui_server.chromium_ready()  —— 走 platform_compat.ms_playwright_dir()
    · 维护       cleanup.component_items()   —— 走 platform_compat.ms_playwright_dir()
    · macOS 壳   shell/main.swift            —— 认 PLAYWRIGHT_BROWSERS_PATH 并展开 `~`

bootstrap 原先自己抄了一份平台目录表，抄丢了一个细节：官方那个环境变量可以是
`~/...` 的写法。壳和 platform_compat 都会 expanduser，只有抄出来的那份不会 ——
`glob` 不认 `~`，于是浏览器明明在展开后的目录里，这里却按字面量 `~` 去找，永远报
「没装」，每点一次重下一遍。这一套把「一把尺子」和「认得出装好的那几种目录」都钉住，
不起服务、不开浏览器、不联网，全在临时目录里跑。
"""
import ast
import os
import pathlib
import sys
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先指进沙盒再 import：这两个模块本身不落盘，但万一以后加了导入期读写，
# 也不该碰用户真实的 cache/ 与书库。
_SB = tempfile.mkdtemp(prefix="guizang-bootstrap-")
os.environ["GUIZANG_DATA"] = os.path.join(_SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(_SB, "books")

import bootstrap  # noqa: E402
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


def src(name):
    return (ROOT / name).read_text(encoding="utf-8")


SRC_BOOT = src("bootstrap.py")
SRC_UI = src("ui_server.py")
SRC_CLEANUP = src("cleanup.py")
SRC_SWIFT = src("shell/main.swift")

# ── 1. 一把尺子：目录只有一处真相 ───────────────────────────────
chk("接线：bootstrap 的浏览器目录问的是 platform_compat",
    "pc.ms_playwright_dir()" in SRC_BOOT)
chk("接线：ui_server 的浏览器目录问的是 platform_compat",
    "pc.ms_playwright_dir()" in SRC_UI)
chk("接线：cleanup 的浏览器目录问的是 platform_compat",
    "pc.ms_playwright_dir()" in SRC_CLEANUP)
chk("接线：macOS 壳那份认官方环境变量并展开 ~",
    "PLAYWRIGHT_BROWSERS_PATH" in SRC_SWIFT and "expandingTildeInPath" in SRC_SWIFT)
chk("一把尺子：bootstrap 里不再自己抄平台目录表（没有 ms-playwright 字面量）",
    "ms-playwright" not in SRC_BOOT)
chk("一套判断：两处认的 Chromium 目录名一样",
    '("chromium-*", "chromium_headless_shell-*")' in SRC_BOOT
    and '("chromium-*", "chromium_headless_shell-*")' in SRC_UI)

# ── 2. 首次运行的硬约束：只用标准库 + platform_compat ───────────
# 第一次点它的时候项目里什么都还没装，所以这个脚本在裸系统 Python 上也得起得来。
# 函数体里那次 import ffmpeg_tool 是懒加载（第 4 步才走），不算。
_mods = set()
for _node in ast.parse(SRC_BOOT).body:
    if isinstance(_node, ast.Import):
        _mods |= {a.name.split(".")[0] for a in _node.names}
    elif isinstance(_node, ast.ImportFrom) and not _node.level:
        _mods.add((_node.module or "").split(".")[0])
_req = {ln.split("#")[0].split(">=")[0].split("==")[0].split("[")[0].strip().replace("-", "_")
        for ln in src("requirements.txt").splitlines()}
_req = {r for r in _req if r}
chk("裸机可跑：顶层不 import requirements 里那几个包（那时候还没装上）",
    not (_mods & _req), sorted(_mods & _req))
# 3.10+ 能报出标准库名册，就顺带把「第三方一个都不许有」钉死；3.9 上退化上一条。
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
if _STDLIB:
    _allowed = _STDLIB | {"platform_compat"}
    chk("裸机可跑：顶层 import 只有标准库 + platform_compat",
        _mods <= _allowed, sorted(_mods - _allowed))

# ── 3. 默认位置：三个平台各一处，且都是绝对路径 ─────────────────
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": ""}), \
        mock.patch.object(pc, "IS_WIN", False), mock.patch.object(pc, "IS_MAC", True):
    mac = pc.ms_playwright_dir()
    chk("默认（macOS）：~/Library/Caches 下", mac.endswith(
        os.path.join("Library", "Caches", "ms-playwright")), mac)
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": ""}), \
        mock.patch.object(pc, "IS_WIN", False), mock.patch.object(pc, "IS_MAC", False):
    lin = pc.ms_playwright_dir()
    chk("默认（Linux）：~/.cache 下", lin == os.path.expanduser("~/.cache/ms-playwright"), lin)
    chk("默认：不是字面量 ~ 开头（已经展开过）", not lin.startswith("~"), lin)
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "",
                                  "LOCALAPPDATA": os.path.join("C:", "L")}), \
        mock.patch.object(pc, "IS_WIN", True):
    win = pc.ms_playwright_dir()
    chk("默认（Windows）：LOCALAPPDATA 下", win == os.path.join("C:", "L", "ms-playwright"), win)

# ── 4. 官方环境变量：占先、`0` 表示不接管、`~` 要展开 ───────────
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": os.path.join(_SB, "mine")}), \
        mock.patch.object(pc, "IS_WIN", True), \
        mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join("C:", "L")}):
    chk("环境变量：指哪儿用哪儿，不被平台默认顶掉",
        pc.ms_playwright_dir() == os.path.join(_SB, "mine"))
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0",
                                  "LOCALAPPDATA": os.path.join("C:", "L")}), \
        mock.patch.object(pc, "IS_WIN", True):
    chk("环境变量：0 = 不接管，退回平台默认",
        pc.ms_playwright_dir() == os.path.join("C:", "L", "ms-playwright"))

# 这一条就是本次修的那个坑：`~` 写法必须展开成真实目录。
_fake_home = tempfile.mkdtemp(prefix="guizang-home-")
os.makedirs(os.path.join(_fake_home, "pw", "chromium-1234"))
with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "~/pw",
                                  "HOME": _fake_home, "USERPROFILE": _fake_home}):
    chk("~ 写法：解析成展开后的绝对路径",
        pc.ms_playwright_dir() == os.path.join(_fake_home, "pw"),
        pc.ms_playwright_dir())
    chk("~ 写法：字面量 ~ 底下那个目录并不存在（老写法正因此永远报没装）",
        not os.path.isdir(os.path.join("~/pw", "chromium-1234")))
    chk("~ 写法：装好的 Chromium 认得出（不重下 368MB）",
        bootstrap.browser_ready() is True)

# ── 5. 认得出 / 认不出的那几种目录 ───────────────────────────────
def ready_in(layout):
    """按给定的目录布局问一次 browser_ready。"""
    d = tempfile.mkdtemp(prefix="guizang-pw-")
    for name in layout:
        p = os.path.join(d, name)
        if name.endswith("/"):                  # 以 / 收尾表示建目录，否则建普通文件
            os.makedirs(p)
        else:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("x")
    with mock.patch.object(pc, "ms_playwright_dir", return_value=d):
        return bootstrap.browser_ready()


chk("识别：chromium-1234 目录算装好了", ready_in(["chromium-1234/"]) is True)
chk("识别：chromium_headless_shell-1234 目录也算",
    ready_in(["chromium_headless_shell-1234/"]) is True)
chk("识别：别的浏览器（firefox-1）不算", ready_in(["firefox-1/"]) is False)
chk("识别：同名的普通文件不算（必须是目录）", ready_in(["chromium-1234"]) is False)
chk("识别：目录空的 / 不存在都算没装", ready_in([]) is False)
chk("识别：一堆里有一个装好就算装好",
    ready_in(["firefox-1/", "chromium-2/", "chromium-3/"]) is True)

print("\n首次运行环境判断门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
