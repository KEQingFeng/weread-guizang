#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转写那两个落盘口的中止信号（Ctrl-C / 停止键）也得把临时件收干净。

为什么单开一条门禁
    `video_note._write_json` / `_write_text` 已经收进「一次一个随机名字」那一族：
    名带进程号 + 随机串，写失败先把残件收掉再抛。可它们兜的是 `except Exception`，
    而**中止信号不继承 `Exception`**：

        KeyboardInterrupt（Ctrl-C / 停止键）/ SystemExit / GeneratorExit
        └─ BaseException
             └─ Exception ← 只兜这一层的清理分支，恰好漏掉退出路径上的每一件

    代价：用户点了停止（`ui_server.start_task` 起的 `video_note.py --task` 那条独立
    进程收 Ctrl-C），临时件已经 `open` 出来、正文还没写完，清理分支根本不会被调用，
    书目录里留下一个 `transcript.json.4242.1a2b3c4d.tmp` 半截件。它不进任何读口，
    `drop_stray_tmps` 也要等下一次清残稿才碰得到 —— 在那之前它只是占盘。
    《开发规范》§「落盘原子化」那条写的就是这一口径（`except BaseException`），
    笔记 / 便签 / 取书那几把早就是；视频这两把是这一族里最后两处没对齐的
    （导图那处在 `#169`，本仓未并）。

这一条钉四面：
  A. 正常写：json 与 text 两个口都写得下、目录里不留 `.tmp`（这一栏不空转）；
  B. 中止信号：`KeyboardInterrupt` 与 `SystemExit` 都照原样穿出（不被吞），
     且临时件被收干净、已存在的旧档逐字节不动 —— json 与 text 各一遍；
  C. 对照：只兜 `except Exception` 的老写法在中止信号下**确实会把 .tmp 留下**，
     证明这道闸不是空转；
  D. 接线：源码里这两个写口的清理分支就是 `except BaseException`，防止改回去。

跑法：纯函数 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import os
import pathlib
import re
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import video_note as vn  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("  ✗  " + name + ("  " + str(extra) if extra else ""))


def tmp_left(d):
    return sorted(os.listdir(d))


ROOT = tempfile.mkdtemp(prefix="gz-video-abort-")
try:
    # ── A 正常写：两个口都落得下、不留 .tmp ──────────────────────────────
    jp = os.path.join(ROOT, "transcript.json")
    tp = os.path.join(ROOT, "transcript.txt")
    vn._write_json(jp, {"segments": [{"t": "你好", "s": 0}]})
    vn._write_text(tp, "你好，世界")
    import json as _json
    chk("A1 json 正常写得下且解得动",
        _json.load(open(jp, encoding="utf-8")) == {"segments": [{"t": "你好", "s": 0}]})
    chk("A2 text 正常写得下", open(tp, encoding="utf-8").read() == "你好，世界")
    chk("A3 正常路径目录里不留 .tmp", tmp_left(ROOT) == ["transcript.json", "transcript.txt"],
        tmp_left(ROOT))

    # ── B1 json 写口撞上中止信号：照旧抛、残件收干净、旧档不动 ────────────
    old_bytes = open(jp, "rb").read()
    real_dump = vn.json.dump

    def dump_boom(*a, **k):
        raise KeyboardInterrupt("用户按了停止")

    vn.json.dump = dump_boom
    raised = None
    try:
        vn._write_json(jp, {"segments": [{"t": "新的一版"}]})
    except KeyboardInterrupt as e:
        raised = e
    finally:
        vn.json.dump = real_dump
    chk("B1 KeyboardInterrupt 照原样穿出（清理分支不许吞掉它）",
        isinstance(raised, KeyboardInterrupt), raised)
    chk("B2 中止之后不留 .tmp 残件", tmp_left(ROOT) == ["transcript.json", "transcript.txt"],
        tmp_left(ROOT))
    chk("B3 旧档逐字节不动", open(jp, "rb").read() == old_bytes)

    # ── B2 SystemExit 同一族：兜的必须是 BaseException 那一层 ─────────────
    def dump_exit(*a, **k):
        raise SystemExit(3)

    vn.json.dump = dump_exit
    raised = None
    try:
        vn._write_json(jp, {"segments": []})
    except SystemExit as e:
        raised = e
    finally:
        vn.json.dump = real_dump
    chk("B4 SystemExit 也照原样穿出", isinstance(raised, SystemExit), raised)
    chk("B5 SystemExit 之后同样不留 .tmp", tmp_left(ROOT) == ["transcript.json", "transcript.txt"],
        tmp_left(ROOT))

    # ── B3 text 写口撞上中止信号：同一把尺子 ────────────────────────────
    old_txt = open(tp, "rb").read()
    real_drop = vn._drop_surrogates

    def drop_boom(_x):
        raise KeyboardInterrupt("用户按了停止")

    vn._drop_surrogates = drop_boom
    raised = None
    try:
        vn._write_text(tp, "半截正文")
    except KeyboardInterrupt as e:
        raised = e
    finally:
        vn._drop_surrogates = real_drop
    chk("B6 text 口的中止信号照原样穿出", isinstance(raised, KeyboardInterrupt), raised)
    chk("B7 text 口中止之后不留 .tmp",
        tmp_left(ROOT) == ["transcript.json", "transcript.txt"], tmp_left(ROOT))
    chk("B8 text 口的旧档逐字节不动", open(tp, "rb").read() == old_txt)

    # ── C 对照：只兜 except Exception 的老写法确实会把 .tmp 留下 ──────────
    chk("C1 中止信号确实不继承 Exception（这就是根因）",
        not issubclass(KeyboardInterrupt, Exception) and issubclass(KeyboardInterrupt, BaseException))
    d2 = tempfile.mkdtemp(prefix="gz-video-old-")
    try:
        target = os.path.join(d2, "transcript.json")
        stale = target + ".tmp"

        def _old_shape():
            # 老写法：清理分支只兜 Exception，中止信号从它头顶穿出去
            try:
                with open(stale, "w", encoding="utf-8") as f:
                    f.write("半截")
                raise KeyboardInterrupt("stop")
            except Exception:                  # noqa: BLE001 —— 抓不住中止信号
                os.remove(stale)

        try:
            _old_shape()
        except KeyboardInterrupt:
            pass                               # 调用侧自己接住，不然这一条会把门禁带走
        chk("C2 对照：老写法（except Exception）把 .tmp 留在了盘上",
            os.path.exists(stale), os.listdir(d2))
    finally:
        shutil.rmtree(d2, ignore_errors=True)

    # ── D 接线：两个写口的清理分支就是 except BaseException ───────────────
    src = (REPO / "video_note.py").read_text(encoding="utf-8")

    def _seg(name):
        m = re.search(r"\ndef %s\(.*?(?=\ndef )" % re.escape(name), src, re.S)
        return m.group(0) if m else ""

    for fn in ("_write_json", "_write_text"):
        seg = _seg(fn)
        chk("D1 %s 的清理分支兜的是 BaseException" % fn, "except BaseException:" in seg)
        chk("D2 %s 里不再有兜 Exception 的清理分支" % fn, "except Exception:" not in seg)
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n视频转写中止信号落盘体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
