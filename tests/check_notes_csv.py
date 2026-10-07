#!/usr/bin/env python3
"""划线笔记「导出 CSV」的离线自测：CSV 字节格式 + 落盘位置 + 界面接线。

用户要的是「在编辑器里勾选范围 → 导出成 CSV，且必须符合 flomo 导入的文件格式」。
这条路最容易出事的地方有两处，都不在界面上，只能这样钉：

  1. **格式差一个字节，flomo 就整份不认** —— 少了 BOM，Excel 打开是乱码；少了 CRLF，
     有的解析器把两份笔记粘成一条；时间不是 `YYYY-MM-DD HH:MM:SS` 就整列丢掉。
     所以这里不是「大概对」，而是与官方模板逐项对齐（BOM / 表头 / CRLF / 时间列）。
  2. **正文里本来就有逗号、引号、换行**（用户的划线常带逗号），手拼字符串一定翻车，
     必须交给 csv 模块转义 —— 这里专挑这三样喂进去，再解析回来比对原文。

落盘位置也一并钉住：用户设过导出位置就落那儿（与设置里那句话对上），没设过落下载格；
同一本连导两次自动加序号、绝不覆盖。

配置与落盘全在临时沙盒里跑（先把 GUIZANG_DATA 指过去再 import ui_server），
不碰用户真实的 cache/ 与导出目录。
"""
import csv
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-csv-"))
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


# ── 1. 时间列：Unix 秒 → YYYY-MM-DD HH:MM:SS（本机时区）──────────
chk("T1 正常时间按本机时区排成 YYYY-MM-DD HH:MM:SS",
    fn.csv_time(1756864800) == "2025-09-03 10:00:00", fn.csv_time(1756864800))
for bad in (0, None, "", "x", -5):
    chk("T2 拿不到时间（%r）回空串而不是 1970" % (bad,), fn.csv_time(bad) == "", fn.csv_time(bad))
chk("T3 字符串数字也认（前端有时传字符串）", fn.csv_time("1756864800") == "2025-09-03 10:00:00")

# ── 2. CSV 字节格式：与官方模板逐项对齐 ────────────────────────
raw = fn.build_import_csv([("甲", 1756864800), ("乙", 0)])
chk("F1 带 UTF-8 BOM（Excel 不乱码、flomo 认中文）", raw[:3] == b"\xef\xbb\xbf", raw[:3])
chk("F2 表头是 content,created_at",
    raw[3:3 + len("content,created_at")] == b"content,created_at", raw[3:30])
chk("F3 每行 CRLF 收尾（没有裸 LF）",
    b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), raw)
back = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
chk("F4 解析回来：表头 + 两行数据，顺序原样",
    len(back) == 3 and back[0] == ["content", "created_at"]
    and [r[0] for r in back[1:]] == ["甲", "乙"], back)
chk("F5 时间列写对、空时间留空", back[1][1] == "2025-09-03 10:00:00" and back[2][1] == "",
    [r[1] for r in back[1:]])
chk("F6 返回的是 bytes（不是 str，落盘要二进制）", isinstance(raw, bytes), type(raw))

# ── 3. 转义：正文里本来就有逗号 / 引号 / 换行 ──────────────────
tricky = [
    ("带个逗号，还有半角,和引号\"x\"", 1756864800),
    ("第一行\n第二行\n第三行", 1756866600),
    ("尾随空格 和 制表\t符", 0),
]
back2 = list(csv.reader(io.StringIO(fn.build_import_csv(tricky).decode("utf-8-sig"))))


def _as_lf(s):
    """比较正文时按换行写法归一：导出的换行一律是 CRLF，读回来要还原成 \\n 再比。"""
    return s.replace("\r\n", "\n").replace("\r", "\n")


chk("F7 逗号 / 引号 / 换行 / 制表符全都原样转义回来（换行按 CRLF 归一算等价）",
    [_as_lf(r[0]) for r in back2[1:]] == [_as_lf(t) for t, _ in tricky],
    [r[0] for r in back2[1:]])
chk("F8 引号是双写转义（标准 CSV），不是丢掉",
    '"x"' in back2[1][0], back2[1][0])
chk("F9 多行正文没有被拆成多条记录", len(back2) == 4, len(back2))
# 正文自带 \n 时，csv 模块只会把它原样写进引号里（不按 lineterminator 转），
# 于是文件就混着 CRLF 与裸 LF。这一条盯住「字段内的换行也已归一成 CRLF」。
_tricky_raw = fn.build_import_csv(tricky)
chk("F9b 正文里带换行时，整份文件仍是通篇 CRLF（没漏出裸 LF）",
    b"\r\n" in _tricky_raw and b"\n" not in _tricky_raw.replace(b"\r\n", b""), _tricky_raw[:80])
chk("F10 空正文行也能落（不会因为空就少一行）",
    len(list(csv.reader(io.StringIO(fn.build_import_csv([("", 0)]).decode("utf-8-sig"))))) == 2)
chk("F11 一篇都没有时只给表头（前端已拦，这里兜底）",
    len(list(csv.reader(io.StringIO(fn.build_import_csv([]).decode("utf-8-sig"))))) == 1)

# ── 4. 文件名：洗一遍再拼 ─────────────────────────────────────
chk("N1 正常书名拼成 flomo导入-书名.csv", us.notes_csv_name("三体") == "flomo导入-三体.csv",
    us.notes_csv_name("三体"))
chk("N2 空书名有兜底名", us.notes_csv_name("") == "flomo导入-划线笔记.csv", us.notes_csv_name(""))
bad_name = us.notes_csv_name("a/b:c*d?e")
chk("N3 路径分隔符与通配符先洗掉（不许拼出到别处去的路径）",
    "/" not in bad_name and ":" not in bad_name and "*" not in bad_name and "?" not in bad_name,
    bad_name)
chk("N4 一定以 .csv 结尾", bad_name.endswith(".csv"), bad_name)

# ── 5. 落盘位置：设过就落那儿，没设落下载格 ────────────────────
DL = us.DOWNLOAD_DIR
chk("L1 没设过导出位置 → 落下载格", us.export_target_dir() == DL, us.export_target_dir())
pick = os.path.join(str(SB), "my-exports")
os.makedirs(pick, exist_ok=True)
ok, msg = us.set_export_dir(pick)
chk("L2 设一个能写的目录 → 存得进", ok is True, msg)
chk("L3 设过之后导出就落那儿了", us.export_target_dir() == os.path.abspath(pick),
    us.export_target_dir())
shutil.rmtree(pick)          # 目录被用户删掉（存的时候验过，过后没了）
chk("L4 设过的目录后来被删了 → 悄悄退回下载格（不让导出失败）",
    us.export_target_dir() == DL, us.export_target_dir())
ok, msg = us.set_export_dir("/no/such/dir/gz-xyz")
chk("L5 选一个不存在的目录 → 当场拒绝并说明", ok is False and "不存在" in msg, msg)
chk("L6 清空导出位置 → 回到下载格", us.set_export_dir("")[0] is True
    and us.export_target_dir() == DL)

# ── 6. 重名不覆盖：自动加序号 ─────────────────────────────────
d = os.path.join(str(SB), "uniq")
os.makedirs(d, exist_ok=True)
p1 = us.unique_dest(d, "x.csv")
open(p1, "wb").write(b"1")
chk("U1 空目录里就用原名", os.path.basename(p1) == "x.csv", p1)
p2 = us.unique_dest(d, "x.csv")
chk("U2 重了加 (2)", os.path.basename(p2) == "x (2).csv", p2)
open(p2, "wb").write(b"2")
chk("U3 再重加 (3)", os.path.basename(us.unique_dest(d, "x.csv")) == "x (3).csv")
chk("U4 加序号时不动扩展名", us.unique_dest(d, "x.csv").endswith(".csv"))
chk("U5 旧文件一个都没被盖（两份内容都还在）",
    open(p1, "rb").read() == b"1" and open(p2, "rb").read() == b"2")

# ── 7. 接线：后端有路由、前端有钮、两条路共用同一份正文 ────────
srv = (ROOT / "ui_server.py").read_text(encoding="utf-8")
chk("W1 后端有 /api/notes_csv 路由", 'u.path == "/api/notes_csv"' in srv)
chk("W2 路由用 build_import_csv 真拼字节（不是拼字符串）",
    "flomo_notes.build_import_csv" in srv)
chk("W3 落盘用 export_target_dir + unique_dest（与设置口径一致、不覆盖）",
    "export_target_dir()" in srv and "unique_dest(" in srv)
rte = srv[srv.index('u.path == "/api/notes_csv"'):]
rte = rte[:rte.index('u.path == "/api/clip"')]
chk("W4 这条路的日志只记条数与文件名，正文一个字不落",
    "len(rows)" in rte and "os.path.basename(dest)" in rte and "it.get(\"text\")" in rte
    and "rows[0]" not in rte, rte[-200:])
chk("W5 openexports 认一个可选 dir，但仍只放行用户自己设过的那个目录",
    'body.get("dir")' in srv and "os.path.abspath(want) == os.path.abspath(chosen)" in srv)

html = (ROOT / "ui.html").read_text(encoding="utf-8")
chk("W6 划线笔记工具条上有「导出 CSV」这颗钮", 'id="nbCsv"' in html and "导出 CSV" in html)
chk("W7 钮接上了 exportNotesCsv（不是摆着不动的）",
    "exportNotesCsv(nbTitle)" in html and "async function exportNotesCsv(" in html)
chk("W8 导入 flomo 那条路还在（两颗钮并存，不是替换）",
    'id="nbFlomo"' in html and "async function importToFlomo(" in html)
chk("W9 两条路共用同一份正文（nbFlomoText），导出的和直推的是一个样",
    html.count("nbFlomoText(") >= 3
    and "function nbFlomoText(" in html
    and "nbPicked().map(it => nbFlomoText(it, title, tag))" in html)
chk("W10 勾选顺序按列表原序（勾选顺序不重要，书的顺序才重要）",
    "nbPicked()" in html and "[...nbSel].sort((a, b) => a - b)" in html)
chk("W11 导出带原始时间戳给后端（CSV 时间列靠它）",
    "at: it.at || 0" in html and "at: x.createTime || 0" in html)
chk("W12 导出成功给「打开文件夹」那颗可点的钮",
    "whisperAct(" in html and "post('openexports', {dir: r.dir || ''})" in html)
chk("W13 一条没勾就出声拦住（不静默什么都不做）",
    "先勾选要导出的条目" in html)

shutil.rmtree(SB, ignore_errors=True)
print()
print("划线笔记导出 CSV：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  CSV 字节格式、文件名、落盘位置、重名不覆盖与两面接线全部验通过")
