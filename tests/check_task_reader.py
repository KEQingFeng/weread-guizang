# -*- coding: utf-8 -*-
"""任务收尾的取证：取书任务不许因为「读不到管道 EOF / 读输出报错」永远钉在运行中。

用户报的「取书卡住」在后端有两种形态，这里各钉一条：

  A 引擎进程已经退了，但它拉起的浏览器 / playwright 驱动子进程继承了 stdout 写端、
    还攥着不放 —— `for line in proc.stdout` 等不到 EOF，`TASK["running"]` 永远是 True。
    界面一直「正在取」，再点取书只会被「已有任务在运行」顶回来，只能重启归藏。
  B 读输出的循环自己抛异常（管道被并发关掉、解码坏掉）—— 原实现里异常一路冒到线程外，
    下面那段「running=False」根本轮不到执行，TASK 同样永久卡住。

两条都要求 `_reader` 在秒级内把 running 翻成 False 并记下 exit_code。不联网、不起浏览器。
"""
import io
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))    # 进程内 import ui_server 验后端
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def reset_task(**fields):
    ui_server.TASK.update({"running": True, "kind": "login", "book": None,
                           "exit_code": None, "result": None})
    ui_server.TASK.update(fields)


class OrphanProc:
    """引擎进程退了，但 stdout 写端被孙子进程攥着：readline 永远不返回。"""

    def __init__(self, code=0):
        self.returncode = code
        self._polls = 0
        outer = self

        class _S:
            @staticmethod
            def readline():
                time.sleep(30)          # 等不到 EOF
                return ""

        self.stdout = _S()

    def poll(self):
        # 第一次问「还在吗」说在（模拟进程刚退的一瞬间），之后如实说退了
        self._polls += 1
        return None if self._polls <= 1 else self.returncode

    def wait(self):
        return self.returncode


# ── A. 孤儿管道：进程退了就收尾，不被 EOF 拖住 ──────────────────
_orig_grace = ui_server.PIPE_DRAIN_GRACE
ui_server.PIPE_DRAIN_GRACE = 0.2            # 门禁里别真等 2 秒
try:
    reset_task()
    proc = OrphanProc(code=0)
    th = threading.Thread(target=ui_server._reader, args=(proc,), daemon=True)
    th.start()
    th.join(6)
    chk("孤儿管道：引擎进程退了就收尾，不被 EOF 拖住", not th.is_alive())
    chk("孤儿管道：running 翻成 False（界面不再永远「正在取」）",
        ui_server.TASK["running"] is False, ui_server.TASK)
    chk("孤儿管道：exit_code 如实记下", ui_server.TASK["exit_code"] == 0, ui_server.TASK)
finally:
    ui_server.PIPE_DRAIN_GRACE = _orig_grace


# ── B. 读输出抛异常：也要收尾，不留僵尸任务 ─────────────────────
class DeadProc:
    def __init__(self, code=-9):
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


_orig_iter = ui_server._iter_lines


def _boom(_proc):
    raise OSError("管道被关了")


reset_task()
ui_server._iter_lines = _boom
try:
    ui_server._reader(DeadProc(code=255))       # 线程内直接跑，异常不许冒出去
except Exception as e:                          # noqa: BLE001
    chk("读输出抛错：_reader 自己兜住异常，不冒到线程外", False, repr(e))
else:
    chk("读输出抛错：_reader 自己兜住异常，不冒到线程外", True)
finally:
    ui_server._iter_lines = _orig_iter
chk("读输出抛错：running 仍被翻成 False（原实现的僵尸任务）",
    ui_server.TASK["running"] is False, ui_server.TASK)
chk("读输出抛错：exit_code 如实记下", ui_server.TASK["exit_code"] == 255, ui_server.TASK)


# ── C. 正常路径没被 watchdog 改坏 ───────────────────────────────
class LineProc:
    def __init__(self, lines, code):
        self.lines = [x + "\n" for x in lines]
        self.returncode = code
        outer = self

        def readline():
            return outer.lines.pop(0) if outer.lines else ""

        self.stdout = type("S", (), {"readline": staticmethod(readline)})()

    def poll(self):
        return None if self.lines else self.returncode

    def wait(self):
        return self.returncode


reset_task(kind="login")
ui_server._reader(LineProc(["📖 正常路径 — 某人", "##GUIZANG## {\"book_id\": \"TP_X\"}"], 0))
chk("正常路径：跑完 running=False", ui_server.TASK["running"] is False, ui_server.TASK)
chk("正常路径：结构化结果照旧收进 TASK",
    (ui_server.TASK.get("result") or {}).get("book_id") == "TP_X", ui_server.TASK)

print()
if FAIL:
    print("未通过 %d 项：%s" % (len(FAIL), " / ".join(FAIL)))
    sys.exit(1)
print("全部通过")
