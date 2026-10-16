#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""划线笔记「导出 CSV」的落盘原子性：直写会在盘满 / 强杀时留下半份清单。

`check_notes_csv.py` 量的是 CSV 的字节格式与落点口径；这一条只量**写**的那一下。
`/api/notes_csv` 原先 `open(dest, "wb")` 直接往目标文件里写，栽在两处：

  · 盘满 / 进程被杀发生在写完之前：`dest` 就是一份写了一半的清单。它是个新名字、
    没有旧档可毁，所以症状不是「覆盖」而是**静默少几条** —— 用户拿这份 CSV 去
    flomo 导入，缺的那些划线不会报错，只看得到条数少了；
  · 同一本连点两次导出：两处各自 `unique_dest` 时都还没落盘，会挑中同一个名字，
    于是两只手往同一个文件里织。

现在写这一步走 `_write_bytes`（同目录临时件 + `os.replace`，与封面同一个二进制
落盘口）：读口永远只见完整的一份，改名失败时临时件收干净、目标位置一个字节不留。

全程临时沙盒（先把 GUIZANG_DATA 指过去再 import ui_server），不起服务、不联网、
不碰用户真实的 cache/ 与导出目录。
"""
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-csv-atomic-"))
os.environ["GUIZANG_DATA"] = str(SB)
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import flomo_notes as fn   # noqa: E402
import ui_server as us     # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


ROWS = [("第一条划线", 1756864800), ("第二条，带逗号", 0)]
FULL = fn.build_import_csv(ROWS)

# ── A. 正常一趟：整份清单落盘，不留临时件 ─────────────────────
A = os.path.join(str(SB), "A")
os.makedirs(A)
dest = us.notes_csv_export(A, "三体", ROWS)
raw = open(dest, "rb").read()
chk("A1 落点就在这个目录、名字是 flomo导入-三体.csv",
    os.path.dirname(dest) == A and os.path.basename(dest) == "flomo导入-三体.csv", dest)
chk("A2 整份写完了（BOM + 表头 + 两行数据，与 build_import_csv 逐字节相同）",
    raw == FULL, raw[:48])
chk("A3 目录里只剩成品，没有 .tmp 残件",
    sorted(os.listdir(A)) == ["flomo导入-三体.csv"], os.listdir(A))

# ── B. 同名再导一次不覆盖（与 unique_dest 的承诺对上） ─────────
dest2 = us.notes_csv_export(A, "三体", [("换一版", 0)])
chk("B1 同名第二次落到 (2)，不盖第一份",
    os.path.basename(dest2) == "flomo导入-三体 (2).csv", dest2)
chk("B2 第一份内容一个字节没动", open(dest, "rb").read() == raw)
chk("B3 这一趟也没留 .tmp",
    sorted(os.listdir(A)) == ["flomo导入-三体 (2).csv", "flomo导入-三体.csv"], os.listdir(A))

# ── C. 写是「先临时件再改名」，不是直接写目标 ─────────────────
C = os.path.join(str(SB), "C")
os.makedirs(C)
seen = []
real_replace = os.replace


def spy_replace(src, dst):
    seen.append((str(src), str(dst)))
    return real_replace(src, dst)


us.os.replace = spy_replace
try:
    d3 = us.notes_csv_export(C, "原子", [("看得见吗", 0)])
finally:
    us.os.replace = real_replace
chk("C1 只有一次改名，源文件是同目录的 .tmp、目标才是落点",
    len(seen) == 1 and seen[0][1] == d3 and seen[0][0] != d3
    and seen[0][0].endswith(".tmp")
    and os.path.dirname(seen[0][0]) == os.path.dirname(seen[0][1]), seen)
chk("C2 改名之后 .tmp 已经不在了", not os.path.exists(seen[0][0]) if seen else False, seen)

# ── D. 改名失败（盘满）：异常往外抛，目标位置一个字节不留 ──────
D = os.path.join(str(SB), "D")
os.makedirs(D)


def boom_replace(src, dst):
    raise OSError(28, "No space left on device")


us.os.replace = boom_replace
raised = False
try:
    us.notes_csv_export(D, "盘满", ROWS)
except OSError:
    raised = True
finally:
    us.os.replace = real_replace
chk("D1 改名失败照旧往外抛（调用方才知道要报「写不出来」）", raised)
chk("D2 目标位置一个字节都没留（读口看不见半份清单）", os.listdir(D) == [], os.listdir(D))

# ── E. 对照：老写法的直写确实会留下半份清单 ───────────────────
E = os.path.join(str(SB), "E")
os.makedirs(E)
old_dest = os.path.join(E, "flomo导入-旧写法.csv")


def old_direct_write(path, payload):
    """旧 `/api/notes_csv` 的形状：open(dest,"wb") 直写，写到一半失败就停在原地。"""
    with open(path, "wb") as f:
        f.write(payload[:max(1, len(payload) // 3)])
        raise OSError(28, "No space left on device")


try:
    old_direct_write(old_dest, FULL)
except OSError:
    pass
chk("E1 对照：老的直写在失败后盘上留着一份不完整的 dest（不是空目录）",
    os.path.exists(old_dest) and 0 < os.path.getsize(old_dest) < len(FULL),
    os.path.getsize(old_dest) if os.path.exists(old_dest) else "缺文件")

# ── F. 接线：路由走这个函数，函数走仓库那把二进制落盘尺子 ──────
src = (ROOT / "ui_server.py").read_text(encoding="utf-8")
chk("F1 /api/notes_csv 的落盘改成调 notes_csv_export 了",
    "dest = notes_csv_export(" in src)
_start = src.index('u.path == "/api/notes_csv"')
route = src[_start:src.index('u.path == "/api/clip"', _start)]
chk("F2 那条路由里不再有裸 open（写口只有一处）",
    "with open(" not in route and 'open(dest, "wb")' not in route, route[-260:])
_j = src.index("def notes_csv_export(")
_body = src[_j:src.index("\n# ---------- 本机阅读时长", _j)]
chk("F3 notes_csv_export 的写口是 _write_bytes（同目录临时件 + 原子改名）",
    "_write_bytes(dest," in _body)
chk("F4 notes_csv_export 自己不再直接 open 目标文件",
    "with open(" not in _body, _body[-200:])
chk("F5 unique_dest 仍在（重名加序号、不覆盖的承诺不变）",
    "unique_dest(" in _body)

shutil.rmtree(str(SB), ignore_errors=True)
print()
print("划线笔记导出 CSV 原子落盘门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
