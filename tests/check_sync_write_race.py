#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云同步取回落盘：两只手同时取同一个文件时，不许互相搬走临时件（sync.py）。

为什么要单开一条
    `tests/check_sync_atomic.py` 钉的是「**一个**写者写到一半断掉」那一档：目标位置
    不许出现半截件、`.part` 不许被当成书上传。这一条钉的是另一档 —— **两个写者同时
    进来**。

    `/api/sync/now` 由 `ThreadingHTTPServer` 的请求线程直接处理，而
    `ui_server.sync_run_now()` 没有任何锁（界面只锁得住自己那颗按钮，锁不住另一扇
    窗）。两个窗口 / 两个标签页各点一次「立即同步」，两趟 `sync.run()` 就会同时走到
    `_write_atomic`。临时件名是固定的 `<目标>.part`（不是那一族 pid + 随机串），于是
    两个写者打开同一个件：

      · 先收尾的那个 `os.replace` 把它搬去目标，后一个 `os.replace` 当场
        FileNotFoundError —— 界面看到「失败 1」，其实那份文件已经好好取回来了；
      · 两只手都还开着这个件时，后到的写入落在已经改名的目标上，把刚取回的那份
        写成两份内容的混血。

    临时件名为什么仍是固定的 `<目标>.part`（不换成随机串）：`cleanup._residue_re`
    正是按 `<账本>.part` 这个形状认同步留下的半截账（见 `docs/交接说明.md` §2），
    改名会让「清除本地数据」那一刀当场瞎掉。所以收的是另一半：「写临时件 + 改名」
    这一小段串进 `sync._ATOMIC_LOCK`。

这一条钉住
  · **对照（不是空转）**：把旧写法（没有锁）在这里重抄一遍，同一把行为级间谍挂在
    它身上，同时开四个写者 —— 间谍量到「同时有 >1 个人在写 / 改名」，且至少一个
    写者 FileNotFoundError。旧写法确实会撞，这一条量得到；
  · **新写法**：同一把间谍挂到 `sync._write_atomic` 上，四个写者抢同一个目标，
    「同时在写」与「同时在改名」的最大值都必须是 1（锁把这一段串起来了），且四个
    写者全部返回、目标内容逐字节等于某一个写者的、不留 `.part`；
  · **真并发**：去掉间谍、连抢多轮，每轮都不抛、内容仍是某一个人的整份、不留
    `.part`。

全离线、只碰临时目录，不联网、不起服务，归静态段。
"""
import builtins
import os
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync as S  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


REAL_OPEN = builtins.open
REAL_REPLACE = os.replace
REAL_REMOVE = os.remove
REAL_MAKEDIRS = os.makedirs


def _spy():
    """一把行为级间谍：记「同时有几个人在写临时件 / 在改名」。

    `writers` 在打开 `.part` 那一刻 +1、在 `with` 退出（关文件）时 -1；`replacers`
    在 `os.replace` 进 / 出时各动一下，中间停 0.2 秒 —— 没有锁时第二个写者一定能挤
    进这段窗口，有锁时它连 `open` 都到不了。
    """
    state = {"writers": 0, "max_writers": 0, "replacers": 0, "max_replacers": 0}
    guard = threading.Lock()

    class File:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            return self._fh.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            try:
                self._fh.close()
            finally:
                with guard:
                    state["writers"] -= 1
            return False

    def spy_open(path, mode="r", *a, **kw):
        fh = REAL_OPEN(path, mode, *a, **kw)
        if "w" in str(mode) and str(path).endswith(".part"):
            with guard:
                state["writers"] += 1
                state["max_writers"] = max(state["max_writers"], state["writers"])
        return File(fh)

    def spy_replace(src, dst):
        with guard:
            state["replacers"] += 1
            state["max_replacers"] = max(state["max_replacers"], state["replacers"])
        time.sleep(0.2)
        try:
            return REAL_REPLACE(src, dst)
        finally:
            with guard:
                state["replacers"] -= 1

    return spy_open, spy_replace, state


def old_write_atomic(path, blob, opener, replacer):
    """旧写法（没有锁）的镜子：与 `sync._write_atomic` 改之前逐行同形。"""
    d = os.path.dirname(path)
    if d:
        REAL_MAKEDIRS(d, exist_ok=True)
    tmp = path + ".part"
    try:
        with opener(tmp, "wb") as f:
            f.write(blob)
        replacer(tmp, path)
    except BaseException:
        try:
            REAL_REMOVE(tmp)
        except OSError:
            pass
        raise


def drive(target, blobs, write_one):
    """同时放 len(blobs) 个写者抢同一个目标；回 (错误表, 目标字节)。"""
    errs = []
    barrier = threading.Barrier(len(blobs))

    def one(blob):
        try:
            barrier.wait(5)
            write_one(target, blob)
        except BaseException as e:
            errs.append(type(e).__name__)

    ts = [threading.Thread(target=one, args=(b,)) for b in blobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    data = REAL_OPEN(target, "rb").read() if os.path.exists(target) else b"<missing>"
    return errs, data


TMP = tempfile.mkdtemp(prefix="guizang-sync-race-")
try:
    BLOBS = [bytes([65 + i]) * 8000 for i in range(4)]

    # ── 1. 对照：同一把间谍挂在旧写法上，四个写者当场撞车 ────────────────
    spy_open, spy_replace, old_state = _spy()
    old_target = os.path.join(TMP, "old", "readstat.json")
    old_errs, old_data = drive(
        old_target, BLOBS,
        lambda p, b: old_write_atomic(p, b, spy_open, spy_replace))
    chk("对照：旧写法（没有锁）确实有不止一个写者同时在写临时件",
        old_state["max_writers"] >= 2, old_state)
    chk("对照：旧写法确实有不止一个写者同时在改名",
        old_state["max_replacers"] >= 2, old_state)
    chk("对照：旧写法至少一个写者当场 FileNotFoundError",
        "FileNotFoundError" in old_errs, old_errs)

    # ── 2. 新写法：同一把间谍，锁把「写 + 改名」串成一次一个人 ────────────
    spy_open, spy_replace, new_state = _spy()
    new_target = os.path.join(TMP, "new", "readstat.json")
    ORIG_OS = S.os
    S.os = types.SimpleNamespace(path=os.path, makedirs=REAL_MAKEDIRS,
                                 replace=spy_replace, remove=REAL_REMOVE)
    S.open = spy_open
    try:
        new_errs, new_data = drive(new_target, BLOBS, S._write_atomic)
    finally:
        S.os = ORIG_OS
        del S.open
    chk("新写法：四个写者全部返回（没有 FileNotFoundError）", new_errs == [], new_errs)
    chk("新写法：同时在写临时件的最多只有 1 个", new_state["max_writers"] == 1, new_state)
    chk("新写法：同时改名的最多只有 1 个", new_state["max_replacers"] == 1, new_state)
    chk("新写法：目标内容逐字节等于某一个写者的整份（不是混血）",
        new_data in BLOBS, len(new_data))
    chk("新写法：不留 .part",
        not os.path.exists(new_target + ".part"), None)

    # ── 3. 真并发：去掉间谍，连抢多轮 ─────────────────────────────────
    bad = []
    for r in range(15):
        t = os.path.join(TMP, "hammer", "h%02d.json" % r)
        errs, data = drive(t, BLOBS, S._write_atomic)
        if errs or data not in BLOBS or os.path.exists(t + ".part"):
            bad.append((r, errs, len(data), os.path.exists(t + ".part")))
    chk("真并发：15 轮 x 4 个写者都不抛、内容是整份、不留 .part", not bad, bad)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("云同步取回落盘并发体检：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
