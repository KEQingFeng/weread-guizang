#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""残留浏览器清理：POSIX 那半段也得按**字面**认 profile 路径。

`platform_compat.kill_stray_browsers` 要清掉仍占着 profile 锁的旧浏览器，Windows
那半段用的是 PowerShell 的 `.Contains`（字面匹配），POSIX 这半段却把绝对路径直接
丢给 `pkill -f` —— 而 `pkill -f` 的 pattern 是**扩展正则**，不是普通子串。于是：

  · 路径里带 `(2)`（把「归藏」复制了一份、文件夹叫「归藏 (2)」）时，`(2)` 在正则里
    是分组，等价于在找 `归藏 2/...`；子进程真实的 cmdline 是 `归藏 (2)/...`，谁也
    匹配不上。残留浏览器没被杀掉、profile 锁继续被占着，下一轮启动报的还是
    「浏览器起不来」—— 和没修过一模一样，属于这个模块最想消灭的「静默失败」。
  · 路径里带 `|` 时更糟：pattern 变成「或」，可能匹配到无关进程、误杀别人的程序。

这一条把「转义尺子只转正则元字符」「转义后确实匹配得上、没转义确实匹配不上（对照）」
「kill_stray_browsers 接的是转义后的 pattern」以及一条真机 `pgrep -f` 的来回都钉住，
全离线、不开浏览器。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import platform_compat as pc  # noqa: E402

FAIL = []
PASSED = 0
SKIPPED = []


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def skip(name, why):
    print("SKIP  %s   %s" % (name, why))
    SKIPPED.append(name)


# ── 1. 转义尺子：只动正则元字符，别的原样 ────────────────────────
chk("普通路径原样不动（斜杠、中文、数字都不动）",
    pc.pkill_pattern("/a/归藏12/browser_profile") == "/a/归藏12/browser_profile",
    pc.pkill_pattern("/a/归藏12/browser_profile"))
chk("正则元字符逐个转义",
    pc.pkill_pattern("/a/归藏 (2)/[x]+y|z$") == "/a/归藏 \\(2\\)/\\[x\\]\\+y\\|z\\$",
    pc.pkill_pattern("/a/归藏 (2)/[x]+y|z$"))
chk("空格 / 连字符不被多余转义（它们不是正则元字符）",
    pc.pkill_pattern("/a b-c") == "/a b-c", pc.pkill_pattern("/a b-c"))
chk("点号照转（`.` 匹配任意字符，不转就会多认别的路径）",
    pc.pkill_pattern("/a.b") == "/a\\.b", pc.pkill_pattern("/a.b"))
chk("非字符串不炸（None / 数字都能转）",
    pc.pkill_pattern(None) == "None" and pc.pkill_pattern(7) == "7")


# ── 2. 语义：转义后的只认它自己，没转义的认不出（对照） ──────────
D = "/tmp/归藏 (2)/cache/browser_profile"
CMD = "/tmp/chromium --user-data-dir=" + D + " --headless --remote-debugging-port=0"
chk("转义后能匹配真实 cmdline（修好之后的路）",
    re.search(pc.pkill_pattern(D), CMD) is not None, pc.pkill_pattern(D))
# 对照：把原路径当 pattern（旧代码就是这么传的），`(2)` 被当成分组，匹配不上。
# 没有这一条，上面那条绿着也说明不了「原来确实坏」。
chk("对照：没转义时匹配不上（bug 的现场）",
    re.search(D, CMD) is None, D)
chk("转义后也不会多认：别的目录不匹配",
    re.search(pc.pkill_pattern(D), CMD.replace("(2)", " (3)")) is None)


# ── 3. 接线：kill_stray_browsers 传的必须是转义后的 pattern ──────
def _argv_on_posix(profile):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return mock.Mock()

    with mock.patch.object(pc, "IS_WIN", False), \
            mock.patch.object(subprocess, "run", fake_run):
        pc.kill_stray_browsers(profile, wait=0)
    return seen.get("argv")


argv = _argv_on_posix("cache/归藏 (2)/browser_profile")
abs_profile = os.path.abspath("cache/归藏 (2)/browser_profile")
chk("仍是 pkill -f，且 pattern 还是那个绝对路径（没换成别的东西）",
    argv and argv[0] == "pkill" and argv[1] == "-f" and os.path.isabs(argv[2]),
    argv)
chk("传给 pkill 的 pattern 已经转义",
    argv and argv[2] == pc.pkill_pattern(abs_profile), argv)
chk("它认得出带元字符的真实 profile 命令行",
    argv and re.search(argv[2], "/tmp/chromium --user-data-dir=" + abs_profile) is not None,
    argv)

# 源码走查：别再退回「把绝对路径直接塞给 pkill」的写法。
src = Path(pc.__file__).read_text(encoding="utf-8")
chk("源码里没有裸的 [\"pkill\", \"-f\", target]",
    '["pkill", "-f", target]' not in src)
chk("源码里 pkill 那行接的是 pkill_pattern(...)",
    'subprocess.run(["pkill", "-f", pkill_pattern(target)]' in src)


# ── 4. 真机来回：pgrep -f 找得到那个带元字符的进程（有 pgrep 才跑） ──
if not shutil.which("pgrep"):
    skip("真机 pgrep 来回", "本机没有 pgrep，跳过（不算绿）")
else:
    tmp = tempfile.mkdtemp(prefix="gz pk (2) ")
    prof = os.path.join(tmp, "browser_profile")
    os.makedirs(prof, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", prof],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.6)

        def _pgrep(pat):
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True)
            return bool(r.stdout.strip())

        chk("真机 pgrep：转义后的 pattern 找得到进程",
            _pgrep(pc.pkill_pattern(prof)), prof)
        chk("真机 pgrep 对照：没转义时找不到（bug 现场）",
            not _pgrep(prof), prof)
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except Exception:
            child.kill()
        shutil.rmtree(tmp, ignore_errors=True)


print()
if FAIL:
    print("残留浏览器清理（pkill 转义）：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
extra = ("；跳过 %d 项" % len(SKIPPED)) if SKIPPED else ""
print("残留浏览器清理（pkill 转义）：%d 项通过，失败 0 项%s" % (PASSED, extra))
