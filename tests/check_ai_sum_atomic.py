#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 小结落盘的原子性（ai_sum.py）：写到一半被强杀，不许把上次那份小结毁掉。

为什么单独一份：`_ai/` 里那份是「这本书 / 这一章已经总结过、不必再问一次模型」的
唯一凭据（GET /api/ai_summary 直接 load_result 回给界面）。而 `save_result` 原来是
`open(path, "w")` 直写 —— `"w"` 一进去就把旧小结截断了，恰好在这一步被强杀
（界面关窗口、换书重开、断电），盘上只剩半截 Markdown；下一次 load_result 认它就是
上次那份，用户看到一段断在句子中间、重开也长不回去的小结，还以为模型就答成这样。
这与 download_images / 封面 / 取书账本踩的是同一个坑：仓库里其它账本早就是
「先写 .tmp 再 os.replace」，只有 ai_sum 这一份内容缓存还是直写。

这一条把原子写口钉住：
  · 正常写：内容一个字不改、改名完成、不留 .tmp；
  · 覆盖：旧稿整份换成新稿；
  · 写一半失败 / KeyboardInterrupt / 改名失败：旧稿必须完整、.tmp 必须收掉；
  · 对照：老写法（open w + 写一半）确实会把旧稿砸坏，证明这道收口不是空转；
  · 接线：save_result 走的是 .tmp + os.replace，不是直写；
  · 同一份小结被两处同时保存：临时件各写各的，两次都成功、盘上是完整的一份，
    且两次用的临时件不同名；
  · 对照：固定临时名下后一个改名扑空（那一次保存白做、回空串）。

纯逻辑 + 系统临时目录里的沙盒书库，不起服务、不联网、不碰真实书库，归静态段，
`--fast` 也要带。
"""
import inspect
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import：免得这条路真去动用户真实书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-ai-sum-atomic-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import ai_sum  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read_text(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


BOOK = str(SANDBOX / "book")
OLD = "旧的小结：一、二、三。\n- 要点 A\n- 要点 B\n"
NEW = "新的小结：换了一整份。\n\n## 一句话核心\n这是新版。"

# ── 一、正常写：内容原样、改名完成、不留 .tmp ─────────────────────
p = ai_sum.save_result(BOOK, "whole", "learn", "", OLD)
chk("基本：回的是真路径", bool(p) and os.path.isfile(p), p)
chk("基本：内容原样落盘、原样读回", ai_sum.load_result(BOOK, "whole", "learn", "") == OLD)
chk("基本：不留 .tmp", not os.path.exists(p + ".tmp"))

# 覆盖：旧稿整份换成新稿，盘上只剩新的
p2 = ai_sum.save_result(BOOK, "whole", "learn", "", NEW)
chk("覆盖：旧内容整份换成新的", ai_sum.load_result(BOOK, "whole", "learn", "") == NEW,
    ai_sum.load_result(BOOK, "whole", "learn", ""))
chk("覆盖：不留 .tmp", not os.path.exists(p2 + ".tmp"))

# 干净正文（含中文 / emoji / ZWJ）一个字不改
CLEAN = "小结 \U0001f600 \U0001f9d1\u200d\U0001f4bb\n\n- 一条要点"
p3 = ai_sum.save_result(BOOK, "chapter", "brief", "0002.md", CLEAN)
chk("干净正文（含 emoji / ZWJ）原样落盘、原样读回",
    ai_sum.load_result(BOOK, "chapter", "brief", "0002.md") == CLEAN,
    ai_sum.load_result(BOOK, "chapter", "brief", "0002.md"))
chk("干净正文：不留 .tmp", not os.path.exists(p3 + ".tmp"))

# ── 二、写一半崩：旧档必须完整，.tmp 必须收掉 ──────────────────────
good = NEW
atomic_path = ai_sum.cache_path(BOOK, "whole", "learn", "")
real_open = open
real_os = ai_sum.os


class _HalfFile:
    """open(path,"w") 的替身：先写半截再按 caller 指定的异常炸掉。"""

    def __init__(self, real, exc):
        self._real = real
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._real.close()
        return False

    def write(self, s):
        self._real.write(str(s)[:4])          # 半截，正是「写到一半断电 / 被杀」
        raise self._exc


def _boom_open(exc):
    def opener(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        return _HalfFile(f, exc)
    return opener


def read_path(pth):
    try:
        return read_text(pth)
    except OSError:
        return None


# 二·a：普通异常（磁盘满 / 编码失败）—— save_result 吞成空串，旧档不许动
try:
    ai_sum.open = _boom_open(OSError("磁盘满"))
    r = ai_sum.save_result(BOOK, "whole", "learn", "", "新的这一份会被写到一半")
    chk("写一半（OSError）：确实回空串（不抛）", r == "", r)
finally:
    del ai_sum.open
chk("写一半（OSError）：旧档一个字没动", read_path(atomic_path) == good, read_path(atomic_path))
chk("写一半（OSError）：不留 .tmp", not os.path.exists(atomic_path + ".tmp"))

# 二·b：中止信号（KeyboardInterrupt）—— 照旧往外抛，但 .tmp 也得收干净
threw = None
try:
    ai_sum.open = _boom_open(KeyboardInterrupt())
    ai_sum.save_result(BOOK, "whole", "learn", "", "又一份写到一半")
except KeyboardInterrupt:
    threw = True
except Exception as e:                       # noqa: BLE001
    threw = e
finally:
    del ai_sum.open
chk("写一半（KeyboardInterrupt）：抛的是它自己那种异常", threw is True, threw)
chk("写一半（KeyboardInterrupt）：旧档一个字没动",
    read_path(atomic_path) == good, read_path(atomic_path))
chk("写一半（KeyboardInterrupt）：不留 .tmp", not os.path.exists(atomic_path + ".tmp"))

# 二·c：改名这一步失败 —— 新稿进不去，旧稿仍然是完整的
class _BoomOS:
    """只把 replace 换掉，别的（path / makedirs / remove）原样透传。"""

    def __getattr__(self, name):
        return getattr(real_os, name)

    def replace(self, *a, **k):
        raise OSError("改名失败")


r3 = None
try:
    ai_sum.os = _BoomOS()
    r3 = ai_sum.save_result(BOOK, "whole", "learn", "", "改不了名的新稿")
finally:
    ai_sum.os = real_os
chk("改名失败：回空串（不抛）", r3 == "", r3)
chk("改名失败：旧档一个字没动", read_path(atomic_path) == good, read_path(atomic_path))
chk("改名失败：不留 .tmp", not os.path.exists(atomic_path + ".tmp"))

# ── 三、对照：老写法（open path,"w" 直写）确实会把旧稿砸坏 ───────────
# 没有这一条，上面的断言可能只是因为「写坏了也没人看」。这里按老写法走一遍，
# 要求旧稿**真的**变成半截 —— 证明原子写不是多余的。
control = SANDBOX / "old-way.md"
control.write_text(good, encoding="utf-8")
try:
    with real_open(str(control), "w", encoding="utf-8") as f:
        f.write("新的这一份会被写到一半")
        raise KeyboardInterrupt()
except KeyboardInterrupt:
    pass
chk("对照：老写法写一半后旧稿已变成半截（原子写不是多余的）",
    read_text(str(control)) != good and read_text(str(control)) == "新的这一份会被写到一半",
    read_text(str(control)))

# ── 四、接线：走 .tmp + os.replace，且旧档在改名之前不被动过 ──────────
src = inspect.getsource(ai_sum.save_result)
chk("接线：先写临时件再改名", ".tmp" in src and "os.replace(" in src)
chk("接线：不再对目标路径直写（w 模式只落在临时件上）",
    'open(path, "w"' not in src and 'open(tmp, "w"' in src, src)
chk("接线：失败 / 中止路径把 .tmp 收掉", "os.remove(" in src and "finally" in src)
# 与 load_result 对得上：读口认的仍是那个 .md，临时件不是另一种名字
chk("接线：临时件是目标旁边加 .tmp，读口只认目标那份",
    ai_sum.cache_path(BOOK, "whole", "learn", "").endswith("learn-whole.md")
    and bool(p) and p.endswith("learn-whole.md"), p)
chk('接线：临时件名不是固定的 path + ".tmp"（带 pid 与随机串）',
    'tmp = path + ".tmp"' not in src and "getpid" in src and "uuid" in src, src)

# ── 五、并发 / 跨进程：同一份小结的临时件不许同名 ──────────────────
# `save_result` 有两个调用侧（同步那条与任务那条），后端又是多线程的，MCP 适配器
# 还是另一个进程；同一个 (书, scope, style, chapter) 被两处同时问出来时，固定临时名
# 让先 replace 的那个把件搬走、后一个改名扑空 —— 保存明明成功却回空串，界面照那句
# 「没存上」说，用户再点一次又是同一场。这里不靠线程调度碰运气：把那一次交错按顺序
# 摆出来（外层改名之前，先让「另一处保存」整趟跑完）。
CONC_A = "A 的那一份：完整。\n- 要点一"
CONC_B = "B 的那一份：完整。\n- 要点二"
conc_path = ai_sum.cache_path(BOOK, "conc", "brief", "")


class _InterleaveOS:
    """只截 replace：第一次改名之前先触发一次 hook，别的（path / makedirs / remove）原样透传。"""

    def __init__(self, real):
        self._real = real
        self.pending = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def replace(self, src, dst, *a, **k):
        hook = self.pending
        if hook is not None:
            self.pending = None          # 只插一次，内层不再触发自己
            hook()
        return self._real.replace(src, dst, *a, **k)


inter = _InterleaveOS(real_os)
second = []
inter.pending = lambda: second.append(ai_sum.save_result(BOOK, "conc", "brief", "", CONC_B))
outer = None
try:
    ai_sum.os = inter
    outer = ai_sum.save_result(BOOK, "conc", "brief", "", CONC_A)
finally:
    ai_sum.os = real_os
chk("并发：交错的那两处保存都回真路径（不扑空）",
    bool(outer) and len(second) == 1 and bool(second[0]), (outer, second))
final = read_path(conc_path)
chk("并发：盘上是完整的一份，不是两份混写", final in (CONC_A, CONC_B), final)
_leftover = [n for n in os.listdir(os.path.dirname(conc_path)) if n.endswith(".tmp")]
chk("并发：不留临时件", _leftover == [], _leftover)

# 两次保存用的临时件必须不同名 —— 这是上面那次交错能各写各的原因。
seen_tmp = []


def _record_open(pth, mode="r", *a, **k):
    if "w" in str(mode) and str(pth).endswith(".tmp"):
        seen_tmp.append(str(pth))
    return real_open(pth, mode, *a, **k)


try:
    ai_sum.open = _record_open
    ai_sum.save_result(BOOK, "conc", "brief", "", "one")
    ai_sum.save_result(BOOK, "conc", "brief", "", "two")
finally:
    del ai_sum.open
chk("接线：两次保存的临时件不同名（固定 path+.tmp 会互相截断）",
    len(seen_tmp) == 2 and seen_tmp[0] != seen_tmp[1], seen_tmp)
chk("接线：临时件名里带 pid（跨进程也不撞）",
    all(str(os.getpid()) in s for s in seen_tmp), seen_tmp)

# ── 六、对照：固定临时名下，那个「改名扑空」是真的 ──────────────────
old_path = str(SANDBOX / "old-concurrent.md")
old_tmp = old_path + ".tmp"
with real_open(old_tmp, "w", encoding="utf-8") as f:
    f.write(CONC_A)                      # A 先写完自己的临时件，还没改名
with real_open(old_tmp, "w", encoding="utf-8") as f:
    f.write(CONC_B)                      # B 用同一个名字写完并先改名走了
real_os.replace(old_tmp, old_path)
lost = None
try:
    real_os.replace(old_tmp, old_path)   # A 这才改名 —— 扑空
except OSError as e:
    lost = e
chk("对照：固定临时名下后一个改名扑空（那次保存白做）", isinstance(lost, FileNotFoundError), lost)
chk("对照：盘上只剩 B 一份，A 那份没了", read_path(old_path) == CONC_B, read_path(old_path))

shutil.rmtree(SANDBOX, ignore_errors=True)

print("\nAI 小结落盘原子性体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
