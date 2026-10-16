#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：全后端账本写出口的两条收口（ui_server._write_json / _write_bytes）。

为什么单开一条
    `_write_json` 是书架账 / config.json / 阅读时长 / 阅读计划 / 划线索引这些小账本
    **唯一**的落盘出口，`_write_bytes` 是封面这一档二进制的唯一出口。两条都是
    「先写同目录临时件、再 os.replace」那把尺子，可收尾还差两格：

    1. **权限不是随改名一起生效的**。`save_cfg` 传 `mode=0o600`（config.json 里存着
       API Key），而临时件是裸 `open(..., "w")` 建的 —— 权限由 umask 决定（常见 0644）。
       改名之后才补一句 `os.chmod(path, mode)`，于是有两段窗口：
         · 写临时件的那几百毫秒里，半截 config.json 对同机其他用户是可读的；
         · 恰好在 `os.replace` 与 `os.chmod` 之间被杀（停止键 / 关窗口 / 断电），
           正式那份 config.json 就**永久**停在 0644，里面的 Key 从此谁都能读。
       正确的做法是让临时件从创建那一刻起就带目标权限：改名搬的是 inode，
       权限跟着一起过去，rename 的原子性同时覆盖内容与权限。

    2. **中止信号会留残件**。两处都只兜 `except Exception`，而 Ctrl-C / 停止键走的是
       `KeyboardInterrupt` / `SystemExit`，它们不继承 `Exception` —— 从写入或改名那一步
       穿出去时，盘上留下一个随机名 `.tmp`（与 `#169` 给 mindmap 补的那一格同一类）。
       两处都该跟 writer / book_notes / feed 那些写口一样兜 `BaseException`：先收残件再抛。

这一条钉两面：权限在改名那一刻就已经收紧（含对照：老写法那一刻还是 0644），以及
两种中止信号都不留 `.tmp` 且旧档一个字不动。纯函数 + 系统临时目录沙盒，不联网、
不起服务、不开浏览器，归 --fast 静态段。

跑法：.venv/bin/python tests/check_ledger_perm.py
"""
import atexit
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带临时沙盒：绝不碰用户真实的 cache/，也不和别的套件共用那一份。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-ledger-perm-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")

import ui_server as U  # noqa: E402 —— 必须在环境变量设好之后 import

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def tmp_left(path):
    folder = os.path.dirname(path)
    base = os.path.basename(path)
    return sorted(n for n in os.listdir(folder) if n.startswith(base) and n.endswith(".tmp"))


# umask 固定成 0o022（常见的默认值）：不固定的话「临时件权限」这一条在 umask 077 的
# 机器上会碰巧通过，门禁就空转了。跑完还原，不污染同进程的别的套件。
_OLD_UMASK = os.umask(0o022)
_real_replace = os.replace


# ── 1. 权限随改名一起生效 ────────────────────────────────────────────
D = str(SB / "perm")
P = os.path.join(D, "config.json")
seen = {}


def _spy_replace(src, dst):
    """记下改名那一刻临时件的权限，再真的改名。"""
    try:
        seen["mode"] = mode_of(src)
    except OSError:
        pass
    return _real_replace(src, dst)


U.os.replace = _spy_replace
try:
    U._write_json(P, {"weread_key": "wrk-secret", "export_dir": "x"}, 0o600)
finally:
    U.os.replace = _real_replace

chk("A1 传了 mode：改名那一刻临时件已经是 0600", seen.get("mode") == 0o600, oct(seen.get("mode", -1)))
chk("A2 落盘后正式文件是 0600", mode_of(P) == 0o600, oct(mode_of(P)))
with open(P, encoding="utf-8") as f:
    chk("A3 内容照旧写得下、解得动", json.load(f).get("weread_key") == "wrk-secret")

# 不带 mode 的账本（书架 / 时长 / 计划…）不该被顺手改权限：仍是 umask 决定的 0644。
P0 = os.path.join(D, "plain.json")
U._write_json(P0, {"a": 1})
chk("A4 没传 mode：临时件仍是 umask 决定的 0644（不误伤别的账本）",
    mode_of(P0) == 0o644, oct(mode_of(P0)))

# 对照：老写法（裸 open 建临时件 → replace → chmod）在改名那一刻是 0644，
# 证明上面那条不是空转 —— 窗口真实存在。
LEGACY = os.path.join(D, "legacy.json")
legacy_tmp = LEGACY + ".tmp"
legacy_mode = None
legacy_seen = {}


def _spy_legacy(src, dst):
    try:
        legacy_seen["mode"] = mode_of(src)
    except OSError:
        pass
    return _real_replace(src, dst)


U.os.replace = _spy_legacy
try:
    with open(legacy_tmp, "w", encoding="utf-8") as f:
        f.write("{}")
    os.replace(legacy_tmp, LEGACY)          # 走 _spy_legacy，记下这一刻的权限
    os.chmod(LEGACY, 0o600)
finally:
    U.os.replace = _real_replace
legacy_mode = legacy_seen.get("mode")
chk("A5 对照：老写法改名那一刻是 0644（窗口真实存在，门禁不空转）",
    legacy_mode == 0o644, oct(legacy_mode if legacy_mode is not None else -1))

# ── 2. 中止信号不留残件 ──────────────────────────────────────────────
D2 = str(SB / "abort")
P1 = os.path.join(D2, "keep.json")
U._write_json(P1, {"good": "旧的一份"})
before = open(P1, "rb").read()

_real_dump = U.json.dump


def _boom(*_a, **_k):
    raise KeyboardInterrupt("用户按了停止")


raised = None
U.json.dump = _boom
try:
    try:
        U._write_json(P1, {"good": "新的一份"})
    except KeyboardInterrupt:
        raised = "KeyboardInterrupt"
    except BaseException as e:                      # noqa: BLE001
        raised = type(e).__name__
finally:
    U.json.dump = _real_dump

chk("B1 写这一步抛 KeyboardInterrupt 时照旧穿出（不吞）", raised == "KeyboardInterrupt", raised)
chk("B2 中止信号不留 .tmp", tmp_left(P1) == [], tmp_left(P1))
chk("B3 中止不毁旧档（逐字节还在）", open(P1, "rb").read() == before)

U._write_json(P1, {"good": "新的一份"})
chk("B4 中止之后同一个口子照旧可用", json.load(open(P1, encoding="utf-8")) == {"good": "新的一份"})

# 二进制那一档：改名这一步抛 SystemExit（停止键的另一张脸）。
P2 = os.path.join(D2, "shot.bin")
U._write_bytes(P2, b"\x89PNG\r\n\x1a\nold")
before2 = open(P2, "rb").read()


def _boom_replace(*_a, **_k):
    raise SystemExit("收摊")


raised2 = None
U.os.replace = _boom_replace
try:
    try:
        U._write_bytes(P2, b"\x89PNG\r\n\x1a\nnew")
    except SystemExit:
        raised2 = "SystemExit"
    except BaseException as e:                      # noqa: BLE001
        raised2 = type(e).__name__
finally:
    U.os.replace = _real_replace

chk("B5 改名抛 SystemExit 时照旧穿出", raised2 == "SystemExit", raised2)
chk("B6 二进制这一档也不留 .tmp", tmp_left(P2) == [], tmp_left(P2))
chk("B7 二进制旧档没被毁", open(P2, "rb").read() == before2)

# 对照：同一情形交给「只兜 Exception」的老写法，盘上会留一个残件。
def _legacy_write_bytes(path, raw):
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        _boom_replace(tmp, path)
    except Exception:                               # noqa: BLE001 —— 老写法就在这儿漏
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


P3 = os.path.join(D2, "legacy.bin")
left3 = None
try:
    _legacy_write_bytes(P3, b"abc")
except SystemExit:
    left3 = tmp_left(P3)
chk("B8 对照：只兜 Exception 的老写法确实留下 .tmp（门禁不空转）", left3 == ["legacy.bin.tmp"], left3)

# ── 3. 源码形状：两条收口长在这两个写口里 ────────────────────────────
src = (REPO / "ui_server.py").read_text(encoding="utf-8")
body_json = src.split("def _write_json(", 1)[1].split("\ndef ", 1)[0]
body_bytes = src.split("def _write_bytes(", 1)[1].split("\ndef ", 1)[0]
chk("C1 _write_json 兜住了中止信号（BaseException）", "except BaseException" in body_json)
chk("C2 _write_bytes 兜住了中止信号（BaseException）", "except BaseException" in body_bytes)
chk("C3 _write_json 的临时件按目标权限创建（os.open + mode）",
    "os.open(" in body_json and "os.fdopen(" in body_json)
chk("C4 save_cfg 仍传 0o600（API Key 那一份）", "_write_json(CFG_PATH, d, 0o600)" in src)

os.umask(_OLD_UMASK)

print()
print("账本写口权限与中止收口体检：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
