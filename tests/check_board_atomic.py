# -*- coding: utf-8 -*-
"""画板落盘的并发原子性（board.py）：同一块板被两处同时写时，不许有一个写者扑空。

为什么单列一条：`ui_server` 的三条写口（`_write_text` / `_write_bytes` / `_write_json`）
早就把口径写死 —— 临时名带 pid + 随机串，因为固定的 `xxx.tmp` 在多线程下必有一条
`os.replace` 扑空。画板这一档一直没跟上：`_write_json` / `_write_bytes` 用的是
`path + ".tmp"`。而画板恰恰是这个后端最重的几次写（画布上限 6 MB，覆盖保存还要先把
旧档挪成 `.prev`），`save_board` / `save_export` 又直接挂在线程池化的 PUT / POST 上：
同一块板开着两个窗口、或「自动保存」和手点保存撞在一起，两个写者共用一个临时名 ——
先 replace 的把临时件搬走，后一个当场 FileNotFoundError，界面报「没存进去」，
可画其实已经落了一半，旁边还留一个没人认领的 `.tmp`。

四段，全在系统临时目录的沙盒里跑，不联网、不起服务、不开浏览器：
  A 对照：固定 `.tmp` 名下第二个 os.replace 确实扑空（证明这条门禁不是空转）。
  B 并发：`_write_json` / `_write_bytes` 各让 6 个写者抢同一份文件 —— 一个都不抛、
    落下的永远是某一份完整内容、`.prev` 也是完整的、目录里不留 `.tmp`；写失败旧档不动。
  C 残件：写到一半崩掉留下的 `<id>.json.<pid>.<hex>.tmp` 仍能被 delete_board 收走，
    别家板的临时件不许被误伤。
  D 接线：board.py 里两处写口都走 `_tmp_for`（pid + 随机串），源码里再没有
    `path + ".tmp"` 这种固定名。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read_repo(fname):
    with open(os.path.join(ROOT_DIR, fname), encoding="utf-8") as f:
        return f.read()


def body_of(src, func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。"""
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def tmp_left(d):
    try:
        return [n for n in os.listdir(d) if n.endswith(".tmp")]
    except OSError:
        return []


# ── A、对照：固定 `.tmp` 名到底怎么坏的 ────────────────────────────
A = tempfile.mkdtemp(prefix="guizang-board-atomic-a-")
fixed_target = os.path.join(A, "b1.json")
fixed_tmp = fixed_target + ".tmp"

with open(fixed_tmp, "w", encoding="utf-8") as f:            # 写者一
    f.write('{"who": "A"}')
with open(fixed_tmp, "w", encoding="utf-8") as f:            # 写者二（同一个临时名）
    f.write('{"who": "B"}')
os.replace(fixed_tmp, fixed_target)                          # 写者一先顶替
race_err = None
try:
    os.replace(fixed_tmp, fixed_target)                     # 写者二：临时件已被搬走
except Exception as e:                                       # noqa: BLE001 — 就要抓原样这个
    race_err = e
chk("对照：固定 `.tmp` 名下第二个 os.replace 会扑空（FileNotFoundError）",
    isinstance(race_err, FileNotFoundError), race_err)
chk("对照：扑空的那一下什么也没留下，失败看上去像「什么都没发生」（最难查的一种）",
    not tmp_left(A) and os.path.isfile(fixed_target), os.listdir(A))

# 反过来看新口径：同一个目标连要两个临时名，必须是两个互不相干的名字。
n1, n2 = board._tmp_for(fixed_target), board._tmp_for(fixed_target)
chk("_tmp_for：同一目标连要两个名字，不许撞名（pid + 随机串）",
    n1 != n2 and n1.startswith(fixed_target + ".") and n1.endswith(".tmp"), (n1, n2))

# ── B、并发：一个都不抛，落下的必是完整一份 ───────────────────────
B = tempfile.mkdtemp(prefix="guizang-board-atomic-b-")
json_target = os.path.join(B, "boards", "b1.json")
os.makedirs(os.path.dirname(json_target))
objs = [{"schema": 1, "id": "b1", "who": "w%d" % i, "canvas": {"objects": [i] * 400}}
        for i in range(6)]
berrs = []


def _json_writer(obj):
    try:
        board._write_json(json_target, obj)
    except Exception as e:                                   # noqa: BLE001
        berrs.append(e)


threads = [threading.Thread(target=_json_writer, args=(o,)) for o in objs]
for t in threads:
    t.start()
for t in threads:
    t.join(10)
chk("并发 _write_json：6 个写者一个都不抛（固定的 `.tmp` 名才会扑空）", not berrs, berrs)
try:
    with open(json_target, encoding="utf-8") as f:
        got = json.load(f)
except Exception as e:                                       # noqa: BLE001
    got, berrs = None, berrs + [e]
chk("并发 _write_json：落下的永远是某一份完整内容（不是两份拼起来的半截 JSON）",
    got in objs, got)
prev = json_target + ".prev"
try:
    with open(prev, encoding="utf-8") as f:
        got_prev = json.load(f)
except Exception:                                            # noqa: BLE001
    got_prev = None
chk("并发 _write_json：被顶掉的那一版挪进 .prev，也是一份完整内容",
    got_prev is None or got_prev in objs, got_prev)
chk("并发 _write_json：目录里没有留下没人认领的 .tmp（临时名各写各的）",
    not tmp_left(os.path.dirname(json_target)), os.listdir(os.path.dirname(json_target)))

# _write_bytes：两个写者抢同一张导出图，同理
btarget = os.path.join(B, "boards", "b1.png")
blobs = [bytes([65 + i]) * 3000 for i in range(6)]
bterrs = []


def _byte_writer(blob):
    try:
        board._write_bytes(btarget, blob)
    except Exception as e:                                   # noqa: BLE001
        bterrs.append(e)


threads = [threading.Thread(target=_byte_writer, args=(b,)) for b in blobs]
for t in threads:
    t.start()
for t in threads:
    t.join(10)
with open(btarget, "rb") as f:
    bgot = f.read()
chk("并发 _write_bytes：6 个写者一个都不抛", not bterrs, bterrs)
chk("并发 _write_bytes：落下的永远是某一份完整字节",
    bgot in blobs and len(bgot) == 3000, (len(bgot), bgot[:4]))
chk("并发 _write_bytes：目录里同样不留 .tmp",
    not tmp_left(os.path.dirname(btarget)), os.listdir(os.path.dirname(btarget)))

# 写失败：旧档一字不动，临时件收干净（否则 boards/ 会慢慢堆垃圾）
board._write_json(json_target, {"who": "keep"})
fail_err = None
try:
    board._write_json(json_target, {"who": "\ud800"})        # 落单半个代理码点 → UnicodeEncodeError
except Exception as e:                                       # noqa: BLE001
    fail_err = e
chk("写失败：原样往上抛（调用方各自那层兜底去接，不许悄悄当成功）",
    fail_err is not None, fail_err)
with open(json_target, encoding="utf-8") as f:
    keep = json.load(f)
chk("写失败：旧档一字不动（不是写坏一半）", keep == {"who": "keep"}, keep)
chk("写失败：临时件收干净了，旁边不留 .tmp",
    not tmp_left(os.path.dirname(json_target)), os.listdir(os.path.dirname(json_target)))

# ── C、残件：崩掉留下的随机临时名，删板仍能收走 ───────────────────
C = tempfile.mkdtemp(prefix="guizang-board-atomic-c-")
cfolder = os.path.join(C, "boards")
os.makedirs(cfolder)
for name in ("keep.json", "keep.json.prev", "keep.json.4242.deadbeef.tmp",
             "keep.svg.4242.deadbeef.tmp", "other.json.7.cafe.tmp",
             "keep.svg", "keep.png"):
    with open(os.path.join(cfolder, name), "w", encoding="utf-8") as f:
        f.write("x")
res = board.delete_board(C, "keep", purge=True)
left = set(os.listdir(cfolder))
chk("删板 purge：带随机串的 .tmp 残件也被收走（不再只认一个固定名）",
    not any(n.startswith("keep.") and n.endswith(".tmp") for n in left), left)
chk("删板 purge：removed 如实报出这些随机名残件",
    "keep.json.4242.deadbeef.tmp" in res.get("removed", [])
    and "keep.svg.4242.deadbeef.tmp" in res.get("removed", []), res)
chk("删板 purge：别家板的临时件不许被误伤",
    left == {"other.json.7.cafe.tmp"}, left)

# ── D、接线：两处写口都必须走 _tmp_for ────────────────────────────
SRC = read_repo("board.py")
wb = body_of(SRC, "_write_bytes")
wj = body_of(SRC, "_write_json")
tf = body_of(SRC, "_tmp_for")
chk("接线：_tmp_for 的临时名带 pid + 随机串（固定名正是扑空的根）",
    "os.getpid()" in tf and "uuid.uuid4().hex" in tf, tf[:200])
chk("接线：_write_json 走 _tmp_for，不再是固定临时名",
    "_tmp_for(path)" in wj and not re.search(r'path\s*\+\s*"\.tmp"', wj), wj[:200])
chk("接线：_write_bytes 走 _tmp_for，不再是固定临时名",
    "_tmp_for(path)" in wb and not re.search(r'path\s*\+\s*"\.tmp"', wb), wb[:200])
chk("接线：board.py 源码里再没有 `path + \".tmp\"` 这种固定临时名",
    not re.search(r'path\s*\+\s*"\.tmp"', SRC))
chk("接线：原子替换仍走 os.replace，写崩时仍把临时件删掉再抛",
    "os.replace(tmp," in wb and "os.remove(tmp)" in wb
    and "os.replace(tmp," in wj and "os.remove(tmp)" in wj, (wb[-200:], wj[-200:]))
chk("接线：删板仍会扫这一形状的残件（_stray_tmps 接在 delete_board 上）",
    "_stray_tmps(folder, sid)" in body_of(SRC, "delete_board"))

for tmpdir in (A, B, C):
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("画板落盘并发原子性门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
