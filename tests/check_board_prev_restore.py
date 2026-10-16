# -*- coding: utf-8 -*-
"""画板落盘的收尾门禁（board.py）：旧档已挪去 `.prev`、新稿顶不上时把它搬回来。

只碰系统临时目录里的沙盒，不开浏览器、不起服务：board.py 是纯存储层，写口能直接
对着函数打。tests/check_board_store.py 管的是坏字符与删板副产物、check_board_atomic.py
管的是并发原子性，这一份专门管**落盘失败那半步**：

  · `_write_json` 是先 `os.replace(path, path + ".prev")` 再 `os.replace(tmp, path)`。
    第二步失败（磁盘满 / 只读、Windows 上杀毒软件钳住目标、路径那一刻成了目录）时，
    `<id>.json` 原先整个消失，而它其实好好躺在 `.prev` 里 —— `_board_files` 只认
    `<id>.json`，于是列表里看不见、`board_counts` 数成 0，用户画了半天的那张点进去
    就没了，旁边还留一个没人认领的随机名 `.tmp`。笔记（`#157`）与导图（`#169`）两格
    早就补上了「把旧档搬回来」这半件，画板是用户画出来的东西里最重的一档（画布上限
    6 MB），这一格却一直没跟上。
  · 同一处只兜 `except Exception`：`KeyboardInterrupt` / `SystemExit` 不继承 Exception，
    强杀 / 关窗那一下从写入或改名这一步穿出去，boards/ 里就多一个随机名 `.tmp`
    （正常路径本就该自己收掉，`_stray_tmps` 是删板时的兜底，不是它的替代）。

为什么单开一条而不是塞进 check_board_store：那一条钉的是「坏字符不许把请求打成 500」
与「删板不留隐形垃圾」，这一条钉的是失败路径的**数据存活**，两者的夹具（要注入一次
改名失败）与判据（旧档还在不在）都不重叠，合在一起会让前者的失败清单说不清是哪一类。

全程离线：沙盒是临时目录，注入的是进程内的 `os.replace` 桩，不联网、不开浏览器。
"""
import errno
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def mk():
    d = tempfile.mkdtemp(prefix="guizang-board-prev-")
    os.makedirs(os.path.join(d, "boards"), exist_ok=True)
    return d


def names(book_dir):
    return sorted(os.listdir(os.path.join(book_dir, "boards")))


def tmps(book_dir):
    return [n for n in names(book_dir) if n.endswith(".tmp")]


def body_of(src, func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。"""
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def old_write_json(path, obj):
    """改之前 `_write_json` 的形状：备份成功之后那句 os.replace 失败就任它去。"""
    tmp = board._tmp_for(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if os.path.exists(path):
        try:
            os.replace(path, path + ".prev")
        except OSError:
            pass
    os.replace(tmp, path)


# ── 1. 对照：老写法把旧档挪进 .prev 之后就没再管它 ─────────────────
# 先证明「这道闸堵的是真洞」，再看新写法。
A = mk()
board.save_board(A, {"id": "b1", "title": "旧画", "canvas": {"objects": [1]}})
_ap = os.path.join(A, "boards", "b1.json")
_real_replace = os.replace


def _old_fail(src, dst):
    if str(src).endswith(".tmp") and dst == _ap:
        raise OSError(errno.ENOSPC, "No space left on device")
    return _real_replace(src, dst)


os.replace = _old_fail
try:
    try:
        old_write_json(_ap, {"title": "新画", "canvas": {"objects": []}})
    except OSError:
        pass
finally:
    os.replace = _real_replace
chk("对照：老写法下 b1.json 整个消失（用户那张点进去就没了）",
    not os.path.exists(_ap), names(A))
chk("对照：老写法把旧档留在 b1.json.prev 里（内容其实还在，只是没人认得）",
    os.path.exists(_ap + ".prev"), names(A))
chk("对照：老写法还留一个没人认领的随机名 .tmp",
    len(tmps(A)) == 1, names(A))
chk("对照：老写法下这块板从列表里消失（board_counts 数成 0）",
    board.board_counts(A) == 0, board.board_counts(A))

# ── 2. 新写法：新稿顶不上时把旧档搬回来 ────────────────────────────
B = mk()
board.save_board(B, {"id": "b1", "title": "旧画", "canvas": {"objects": [1]}})
_bp = os.path.join(B, "boards", "b1.json")


def _new_fail(src, dst):
    if str(src).endswith(".tmp") and dst == _bp:
        raise OSError(errno.ENOSPC, "No space left on device")
    return _real_replace(src, dst)


os.replace = _new_fail
_bp_raised = None
try:
    board._write_json(_bp, {"title": "新画", "canvas": {"objects": []}})
except Exception as e:  # noqa: BLE001
    _bp_raised = e
finally:
    os.replace = _real_replace
chk("_write_json：写不进去照旧往上报（不许被吞成「存好了」）",
    isinstance(_bp_raised, OSError), _bp_raised)
chk("_write_json：旧档被搬回原位，没有整个消失", os.path.exists(_bp), names(B))
chk("_write_json：旧档内容一字不差（不是空档也不是新稿的半截）",
    (board.load_board(B, "b1") or {}).get("title") == "旧画", board.load_board(B, "b1"))
chk("_write_json：不留随机名 .tmp 残件", tmps(B) == [], names(B))
chk("_write_json：搬回来的旧档不再占着 .prev（否则列表会把它当存档）",
    not os.path.exists(_bp + ".prev"), names(B))

# ── 3. 真产品路径：save_board 回人话，画板仍在列表里 ────────────────
C = mk()
board.save_board(C, {"id": "p1", "title": "别弄丢我", "canvas": {"objects": [1]}})
_cp = os.path.join(C, "boards", "p1.json")


def _save_fail(src, dst):
    if str(src).endswith(".tmp") and dst == _cp:
        raise OSError(errno.ENOSPC, "No space left on device")
    return _real_replace(src, dst)


os.replace = _save_fail
try:
    _r = board.save_board(C, {"id": "p1", "title": "改一笔", "canvas": {"objects": [1, 2]}})
finally:
    os.replace = _real_replace
chk("save_board：落盘失败回 ok=False 配人话（不是异常、不是假装成功）",
    _r.get("ok") is False and "磁盘" in _r.get("msg", ""), _r)
chk("save_board：失败后这块板仍在列表里、标题还是旧的那份",
    [r["id"] for r in board.list_boards(C)] == ["p1"]
    and board.list_boards(C)[0]["title"] == "别弄丢我", board.list_boards(C))
chk("save_board：board_counts 照旧数得出来（不占名额也不算丢）",
    board.board_counts(C) == 1, board.board_counts(C))

# ── 4. 中止信号：写入那一步被打断也不许留随机名 .tmp ────────────────
D = mk()
board.save_board(D, {"id": "d1", "title": "还在", "canvas": {"objects": []}})
_real_dump = board.json.dump


def _kb_dump(*a, **k):
    raise KeyboardInterrupt


os.replace = _real_replace
board.json.dump = _kb_dump
try:
    _kb = False
    try:
        board._write_json(os.path.join(D, "boards", "d1.json"),
                          {"title": "新", "canvas": {"objects": []}})
    except KeyboardInterrupt:
        _kb = True
finally:
    board.json.dump = _real_dump
chk("_write_json：中止信号照旧往上传（不许被吞成「存好了」）", _kb)
chk("_write_json：中止信号也把随机名 .tmp 收干净", tmps(D) == [], names(D))
chk("_write_json：中止信号下旧档一个字没动",
    (board.load_board(D, "d1") or {}).get("title") == "还在", names(D))

# ── 5. 导出物那条写口（_write_bytes）同样兜住中止信号 ────────────────
E = mk()
board.save_board(E, {"id": "e1", "canvas": {"objects": []}})
_ep = os.path.join(E, "boards", "e1.png")


def _kb_replace(src, dst):
    if str(src).endswith(".tmp"):
        raise KeyboardInterrupt
    return _real_replace(src, dst)


os.replace = _kb_replace
try:
    _kb2 = False
    try:
        board._write_bytes(_ep, b"\x89PNG")
    except KeyboardInterrupt:
        _kb2 = True
finally:
    os.replace = _real_replace
chk("_write_bytes：中止信号照旧往上传", _kb2)
chk("_write_bytes：中止信号把随机名 .tmp 收干净", tmps(E) == [], names(E))
chk("_write_bytes：导出物没留下半个（正式路径上不出现文件）",
    not os.path.exists(_ep), names(E))

# ── 6. 存档行为没被这次修改动过（正常二次保存仍留 .prev） ───────────
F = mk()
board.save_board(F, {"id": "f1", "title": "一", "canvas": {"objects": []}})
board.save_board(F, {"id": "f1", "title": "二", "canvas": {"objects": [2]}})
chk("存档：正常二次保存仍留一份 .prev（上一版完整躺在里面）",
    os.path.exists(os.path.join(F, "boards", "f1.json.prev"))
    and (board.load_board(F, "f1") or {}).get("title") == "二", names(F))
chk("存档：.prev 仍不算画板、不占名额", board.board_counts(F) == 1, board.board_counts(F))

# ── 7. 接线：两处写口都兜 BaseException，_write_json 多了搬回旧档那一手 ──
SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "board.py"), encoding="utf-8").read()
wj = body_of(SRC, "_write_json")
wb = body_of(SRC, "_write_bytes")
chk("接线：_write_json 整段兜 BaseException（不只是 Exception）",
    "except BaseException" in wj, wj[-260:])
chk("接线：_write_json 在失败路径上把 .prev 搬回原位",
    "os.replace(prev, path)" in wj and 'path + ".prev"' in wj, wj[-260:])
chk("接线：_write_bytes 也兜 BaseException", "except BaseException" in wb, wb[-200:])

for tmpdir in (A, B, C, D, E, F):
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("画板失败路径收尾门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
