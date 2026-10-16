#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""装组件下到一半被按停时，`cache/tools/` 里那份半截 `.part` 也得收走 (ffmpeg_tool.py)。

为什么要单开这一条
    `ffmpeg_tool._download()` 把「下到一半的临时件」写成 `<目标>.part`，失败时自己
    删掉再抛 —— 仓库里封面 / 账本 / 章节 / 运行时回执这一族都是同一条尺子。可这一处
    只兜了 `Exception`：

        except Exception:
            os.remove(tmp)

    而用户按 Ctrl-C、关窗口、应用退出时的 `KeyboardInterrupt` / `SystemExit` **不继承**
    `Exception`，那一句收残件根本轮不到。于是「装组件」这个 30MB 级的下载被中途按停，
    `cache/tools/.ffmpeg.zip.part`（带点的隐藏文件）就留在盘上：

      · 它不在 `cleanup.py` 的白名单里（那把刀只按点名过的账本 `<名>.tmp` 去收），
        `RESIDUE_FILES` 也没点它 —— 没有任何一把清理刀够得到它；
      · 下一次安装不会复用它，只会重新下一份，旧的这份纯占盘。

    改完与仓库里已收的那几处同形：`except BaseException` 收残件再抛（`sync.py:492`、
    `platform_compat.py:441`、`clip_article.py:935` 都是这一手）。本模块全在离线沙盒里
    跑，`_open` 用记账桩顶掉，不联网、不碰用户真实的 `cache/tools/`，故归 --fast 静态段。

这一条钉住
  · 正常下完：目标文件长出来、明文一字不差、旁边不留 `.part`；
  · 中途普通异常（网络断）：`.part` 收掉、异常照抛（不是回归）；
  · **中途 Ctrl-C（BaseException）**：`.part` 收掉、`KeyboardInterrupt` 照抛、目标没被顶成半件；
  · 对照：老写法（`except Exception`）在同一个 Ctrl-C 下确实留下 `.part` —— 这道收口不是空转。

跑法：.venv/bin/python tests/check_ffmpeg_abort_part.py
"""
import atexit
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# GUIZANG_DATA 指进沙盒：tools_dir() 就落到这里，绝不碰用户真实的 cache/。
SANDBOX = tempfile.mkdtemp(prefix="gz-ffmpeg-abort-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

import ffmpeg_tool as F  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if extra == "" else "   <- " + repr(extra)[:220]))


class Resp:
    """按剧本回字节的假响应：先吐完 blocks，再按 boom 抛一次（只抛一次）。"""

    headers = {"Content-Length": "4096"}

    def __init__(self, blocks=(), boom=None):
        self._blocks = list(blocks)
        self._boom = boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._blocks:
            return self._blocks.pop(0)
        if self._boom is not None:
            boom, self._boom = self._boom, None
            raise boom
        return b""


def with_open(resp, fn):
    """把 `_open` 换成「不管什么地址都回 resp」，跑完还原。"""
    real = F._open
    F._open = lambda url: resp
    try:
        return fn()
    finally:
        F._open = real


def call_download(dst, resp):
    """跑一次 _download，返回 (结果, 抛出来的 BaseException)。"""
    raised = None
    result = None
    try:
        result = with_open(resp, lambda: F._download("https://example.invalid/x.zip", dst))
    except BaseException as e:  # noqa: BLE001  这里就是要连 KeyboardInterrupt 一起接住
        raised = e
    return result, raised


# ── 1. 正常下完：目标长出来、不留 .part ─────────────────────────────
ok_dst = os.path.join(SANDBOX, "ok.zip")
BLOB = b"PK\x03\x04" + b"z" * 2048
result, raised = call_download(ok_dst, Resp([BLOB]))
chk("正常下完：_download 回目标路径", result == ok_dst and raised is None, (result, raised))
chk("正常下完：盘上的内容一字不差",
    os.path.exists(ok_dst) and open(ok_dst, "rb").read() == BLOB)
chk("正常下完：旁边不留 .part", not os.path.exists(ok_dst + ".part"))

# ── 2. 中途普通异常：残件收掉、异常照抛（不是回归） ─────────────────
err_dst = os.path.join(SANDBOX, "err.zip")
result, raised = call_download(err_dst, Resp([b"a" * 1024], OSError("network dropped")))
chk("中途网络断：异常照抛", isinstance(raised, OSError), raised)
chk("中途网络断：不留 .part", not os.path.exists(err_dst + ".part"))
chk("中途网络断：半个目标文件都没顶上去", not os.path.exists(err_dst))

# ── 3. 中途 Ctrl-C（BaseException）：这正是本轮补上的那一格 ─────────
abort_dst = os.path.join(SANDBOX, "abort.zip")
result, raised = call_download(abort_dst, Resp([b"b" * 1024], KeyboardInterrupt("用户按了停止")))
chk("Ctrl-C：KeyboardInterrupt 穿出去（不算成功）", isinstance(raised, KeyboardInterrupt), raised)
chk("Ctrl-C：半截 .part 收掉了", not os.path.exists(abort_dst + ".part"))
chk("Ctrl-C：目标没有被顶成半件", not os.path.exists(abort_dst))

# SystemExit 是同一档：应用退出路径上也留不得残件。
exit_dst = os.path.join(SANDBOX, "exit.zip")
result, raised = call_download(exit_dst, Resp([b"c" * 1024], SystemExit(0)))
chk("SystemExit：残件同样收掉", not os.path.exists(exit_dst + ".part"), raised)

# ── 4. 对照：老写法（只兜 Exception）在同一个 Ctrl-C 下确实留 .part ──
def old_download(dst, resp):
    """改之前 `_download` 的形状：`except Exception` 收残件。"""
    tmp = dst + ".part"
    try:
        with resp as r:
            with open(tmp, "wb") as fh:
                while True:
                    block = r.read(65536)
                    if not block:
                        break
                    fh.write(block)
        os.replace(tmp, dst)
        return dst
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


old_dst = os.path.join(SANDBOX, "old.zip")
old_raised = None
try:
    old_download(old_dst, Resp([b"d" * 1024], KeyboardInterrupt("用户按了停止")))
except BaseException as e:  # noqa: BLE001
    old_raised = e
chk("对照：老写法同一个 Ctrl-C 下确实把 .part 落在盘上",
    isinstance(old_raised, KeyboardInterrupt)
    and os.path.exists(old_dst + ".part")
    and os.path.getsize(old_dst + ".part") == 1024)
os.remove(old_dst + ".part")

# ── 5. 接线：收残件那句兜的是 BaseException ────────────────────────
src = (ROOT / "ffmpeg_tool.py").read_text(encoding="utf-8")
start = src.find("def _download(")
seg = src[start:src.find("def _install_zip(", start)]
chk("_download 段里收残件兜的是 BaseException",
    "except BaseException:" in seg and "except Exception:\n        try:" not in seg, seg[:0])
chk("收残件仍然是「删 tmp 再抛」（不是吞掉）",
    "os.remove(tmp)" in seg and seg.rstrip().endswith("raise"), seg[-120:])

print()
print("装组件中止残件门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
