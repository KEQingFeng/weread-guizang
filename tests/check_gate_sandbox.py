#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁自身的沙盒体检：两次运行不许共用一份书库 / cache。

为什么单开一份
    `tests/run_all.sh` 的沙盒原先写死 `${TMPDIR:-/tmp}/guizang-selftest`。一个
    固定路径本身没错，错在「同一台机器上两套门禁同时在跑」：每套件开头都会
    `fresh_shelf()` 重铺一次书架，`cache/anna.json` 这类账本也是共享的那一份 ——
    2026-10-16 真栽过一次，另一套正在重铺时这一套读到「搜书一栏没开过窗口」，
    报出一串看着像产品 bug 的假红（详见《交接说明》§2）。

    这类坏的形状是「没有任何症状」：单跑永远绿，只有并发时才偶尔假红，而假红
    会让人去查产品代码。所以这里把 run_all.sh 里那段**沙盒选择逻辑原文抽出来**
    真跑两遍 —— 不是照着再抄一份（抄的那份绿了，run_all.sh 里那份仍然可以是旧的）。

钉住三件事
  A 默认（没 export GUIZANG_SELFTEST_DIR）：两次运行各拿一个新的、存在的目录，
    且自动开的那一份在进程正常退出后自己收掉（不在 /tmp 里攒书库）。
  B 显式 export 的那一份：两次运行都用调用方给的同一个目录，且退出后**不动它**
    （并行验的时候正是这么用，删掉等于毁了调用方的现场）。
  C 对照：抽出来的是旧写法时两次拿到同一个路径 —— 这条门禁不是空转。

抽取靠 run_all.sh 里那两行标记注释（`# ── 沙盒目录：一次运行一份` / `# ── 沙盒目录结束`）。
纯文件走查 + 两次 `bash` 子进程，不联网、不起服务、不开浏览器，归 --fast。
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_ALL = ROOT / "tests" / "run_all.sh"

START = "# ── 沙盒目录：一次运行一份"
END = "# ── 沙盒目录结束"

FAIL = []
PASSED = 0

SCRATCH = Path(tempfile.mkdtemp(prefix="gz-gate-sandbox-"))
atexit.register(lambda: shutil.rmtree(str(SCRATCH), ignore_errors=True))


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if not extra else "   <- " + str(extra)[:260]))


def sandbox_block(src):
    """把 run_all.sh 里那段沙盒选择逻辑原文抽出来；找不到回 None。"""
    i = src.find(START)
    j = src.find(END, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return src[i:j + len(END)]


def run_block(block, env_extra, label):
    """把抽出来的那段塞进一个干净脚本跑一遍，回 (returncode, stdout, stderr)。"""
    script = SCRATCH / ("run_%s.sh" % label)
    script.write_text("set -uo pipefail\n" + block + "\nprintf '%s\\n' \"$SANDBOX\"\n",
                      encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("GUIZANG_SELFTEST_DIR", "GUIZANG_DATA", "GUIZANG_BOOKS")}
    env.update(env_extra)
    p = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


SRC = RUN_ALL.read_text(encoding="utf-8")
BLOCK = sandbox_block(SRC)

chk("run_all.sh 里找得到沙盒选择那一段（两行标记注释还在）", BLOCK is not None)
if BLOCK is None:
    print()
    print("门禁沙盒：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)

# ── 机制：默认不再是一个写死的路径 ─────────────────────────────────
chk("默认走 mktemp -d（每次调用一个新目录，不是写死的 guizang-selftest）",
    "mktemp -d" in BLOCK and "/guizang-selftest\"" not in BLOCK, BLOCK[:200])

# ── A：两次默认运行各拿一份，且用完自收 ─────────────────────────────
TMP_A = SCRATCH / "tmp_a"
TMP_B = SCRATCH / "tmp_b"
TMP_A.mkdir()
TMP_B.mkdir()
rc_a, path_a, err_a = run_block(BLOCK, {"TMPDIR": str(TMP_A)}, "a")
rc_b, path_b, err_b = run_block(BLOCK, {"TMPDIR": str(TMP_B)}, "b")
chk("A1 默认那次正常退出（rc=0）", rc_a == 0 and rc_b == 0, (rc_a, rc_b, err_a, err_b))
chk("A2 打印出来的是沙盒路径", bool(path_a) and bool(path_b), (path_a, path_b))
chk("A3 两次拿到的是两个不同的目录（不是共用的那个固定名）",
    path_a != path_b and path_a and path_b, (path_a, path_b))
chk("A4 目录落在本次的 TMPDIR 下", path_a.startswith(str(TMP_A)) and
    path_b.startswith(str(TMP_B)), (path_a, path_b))
chk("A5 自动开的那一份退出后自己收掉了（不留半份书库在 /tmp）",
    not Path(path_a).exists() and not Path(path_b).exists(), (path_a, path_b))

# ── B：显式 export 的那一份原样用、且不动它 ────────────────────────
GIVEN = SCRATCH / "given_sandbox"
GIVEN.mkdir()
marker = GIVEN / "keep.txt"
marker.write_text("这是调用方自己留的现场", encoding="utf-8")
rc_c, path_c, err_c = run_block(BLOCK, {"GUIZANG_SELFTEST_DIR": str(GIVEN)}, "c")
rc_d, path_d, err_d = run_block(BLOCK, {"GUIZANG_SELFTEST_DIR": str(GIVEN)}, "d")
chk("B1 显式给的那一份原样用", rc_c == 0 and rc_d == 0 and path_c == str(GIVEN)
    and path_d == str(GIVEN), (rc_c, rc_d, path_c, path_d, err_c, err_d))
chk("B2 退出后不删调用方给的目录，里面的现场还在",
    GIVEN.is_dir() and marker.is_file(), (GIVEN.exists(), marker.exists()))

# ── C：对照 —— 旧写法两次拿到同一个路径（证明这条门禁量的是真东西）──
OLD = ('SANDBOX="${GUIZANG_SELFTEST_DIR:-${TMPDIR:-/tmp}/guizang-selftest}"\n'
       'SANDBOX_OWNED=0\n'
       'cleanup_sandbox() { :; }\n'
       'trap cleanup_sandbox EXIT\n')
TMP_OLD = SCRATCH / "tmp_old"
TMP_OLD.mkdir()
_rc, old_a, _e = run_block(OLD, {"TMPDIR": str(TMP_OLD)}, "old_a")
_rc, old_b, _e = run_block(OLD, {"TMPDIR": str(TMP_OLD)}, "old_b")
chk("C 对照：旧写法两次是同一个固定路径（假红就是这么来的）",
    old_a == old_b == str(TMP_OLD / "guizang-selftest"), (old_a, old_b))

print()
if FAIL:
    print("门禁沙盒：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("门禁沙盒：通过 %d 项，失败 0 项" % PASSED)
