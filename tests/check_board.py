# -*- coding: utf-8 -*-
"""board 模块的离线自测：一行一条结论，失败就非零退出。

只测「存得下、读得出、导得走、删得掉、不越界」这五件事，全程不联网、不起服务、
不用浏览器：画布内容用假 JSON 表示，PNG / SVG 用魔数与前缀拼出来，落盘全部进
系统临时目录下的一个沙盒（atexit 扫干净）—— 按开发规范第 7 节，自己造的夹具
崩溃也要删，否则残留的测试书会毒死后面的套件。

跑法：.venv/bin/python tests/check_board.py
"""
import atexit
import base64
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-board-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))

import board  # noqa: E402

FAIL = []
TOTAL = 0


def chk(name, cond, extra=""):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def no_throw(fn, *args, **kw):
    """调一次，返回 (有没有抛, 返回值)：契约要求这些函数「不往外抛」，抛了就算不合格。"""
    try:
        return False, fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return True, repr(e)


def book_dir(label):
    """开一本测试书：临时根目录下的一层目录，充当 safe_book_dir() 已经验过的书目录。"""
    d = os.path.join(SANDBOX, label)
    os.makedirs(d, exist_ok=True)
    return d


def stamp(path, ts):
    """改盘上那份信封的 updated：列表排序读的就是磁盘上的值，光改内存里的字典不算。"""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    doc["updated"] = ts
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def files_in(folder):
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def has_emoji(text):
    return [c for c in text if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]


PNG_BODY = board.PNG_MAGIC + b"\x0d\x0a\x1a\x0a" + b"fake-png-payload-for-tests" * 4
PNG_B64 = base64.b64encode(PNG_BODY).decode("ascii")
PNG_URL = "data:image/png;base64," + PNG_B64
# 浏览器和命令行工具常把 base64 按 76 列折行，这是真实形态之一，不是我为测试造的怪东西。
PNG_FOLDED = "\n".join(PNG_B64[i:i + 76] for i in range(0, len(PNG_B64), 76))
SVG_TEXT = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
            '<rect x="1" y="1" width="10" height="10"/></svg>')
SVG_URL = "data:image/svg+xml;base64," + base64.b64encode(SVG_TEXT.encode("utf-8")).decode("ascii")
CANVAS = {"version": "5.3.0", "objects": [{"type": "path", "path": ["M 0 0", "L 10 10"],
                                            "源中文键": "第 3 章的受力分析"}],
          "background": "#fffdf8"}


# ── 1. slug：路径纪律的第一道门 ──────────────────────────────────
esc = board.slug("../../escape")
chk("slug：../ 进不去（无斜杠、无点号）", "/" not in esc and "\\" not in esc and ".." not in esc
    and "." not in esc, esc)
chk("slug：../ 滤完还剩可读的名字", esc == "escape", esc)
chk("slug：%2e%2e 变不出点号", "." not in board.slug("%2e%2e%2f"), board.slug("%2e%2e%2f"))
chk("slug：中文名被滤但非空", board.slug("手绘涂鸦图") != "", board.slug("手绘涂鸦图"))
chk("slug：全是非法字符时自己兜一个名", re.fullmatch(r"[A-Za-z0-9_-]{1,40}", board.slug("！！！"))
    is not None, board.slug("！！！"))
chk("slug：空格与井号不进名字", board.slug("a b#c") == "abc", board.slug("a b#c"))
chk("slug：超长裁到 40", len(board.slug("x" * 300)) == 40, len(board.slug("x" * 300)))
chk("slug：合法 id 原样保留", board.slug("b8f2c1a4") == "b8f2c1a4", board.slug("b8f2c1a4"))
chk("slug：None 与空串都给非空兜底", bool(board.slug(None)) and bool(board.slug("")), None)
chk("slug：只有点号时不会退回 '.'", board.slug("..") not in (".", "..", ""), board.slug(".."))
chk("任务书里的 _slug() 与导出的 slug() 是同一个实现", board._slug is board.slug, None)

# ── 2. normalize_env：幂等 + canvas 原样 ─────────────────────────
env1 = board.normalize_env({"id": "b8f2c1a4", "title": "  第 3 章 ", "paper": "grid",
                            "canvas": CANVAS, "caption": "说明"})
env2 = board.normalize_env(env1)
chk("normalize_env：幂等（洗两次一模一样）", env1 == env2, [env1, env2])
chk("normalize_env：canvas 一字不改地留着（含嵌套数组与中文键）",
    env1["canvas"] == CANVAS, env1["canvas"])
chk("normalize_env：canvas 是同一个对象，没被深拷贝改写", env1["canvas"] is CANVAS, None)
chk("normalize_env：缺 id 会补一个能用的", re.fullmatch(r"[A-Za-z0-9_-]{1,40}", env1["id"])
    is not None if "id" in env1 else False, env1["id"])
chk("normalize_env：缺 id 时生成的名字非空", bool(board.normalize_env({})["id"]), None)
chk("normalize_env：paper 不在 PAPERS 里回落 plain",
    board.normalize_env({"paper": "graph"})["paper"] == "plain", env1["paper"])
chk("normalize_env：合法 paper 认下来", env1["paper"] == "grid", env1["paper"])
chk("normalize_env：标题裁到 MAX_TITLE", len(board.normalize_env(
    {"title": "标" * 300})["title"]) == board.MAX_TITLE, None)
chk("normalize_env：说明裁到 MAX_CAPTION", len(board.normalize_env(
    {"caption": "字" * 5000})["caption"]) == board.MAX_CAPTION, None)
chk("normalize_env：bytes 等于画布序列化后的字节数", env1["bytes"] == len(
    json.dumps(CANVAS, ensure_ascii=False).encode("utf-8")), env1["bytes"])
chk("normalize_env：字段齐、schema 是 1",
    {"schema", "id", "title", "caption", "paper", "canvas", "bytes", "created", "updated"}
    <= set(env1) and env1["schema"] == 1, sorted(env1))
chk("normalize_env：时间缺失会补齐", env1["created"] and env1["updated"], None)
bad_doc = board.normalize_env({"canvas": "画布被写成了字符串"})
chk("normalize_env：canvas 不是对象时换成空画布", bad_doc["canvas"] == {"objects": []}, bad_doc)
thrown, weird = no_throw(board.normalize_env, None)
chk("normalize_env：整个信封是 None 也不抛", not thrown and isinstance(weird, dict), weird)
thrown, weird2 = no_throw(board.normalize_env, ["不是对象"])
chk("normalize_env：信封是数组也不抛", not thrown and isinstance(weird2, dict), weird2)

# ── 3. empty / 目录拼法 ──────────────────────────────────────────
e = board.empty("第 3 章的受力分析")
chk("empty：给的名字过一遍 slug", e["id"] == board.slug("第 3 章的受力分析"), e["id"])
chk("empty：能直接存（字段齐 + canvas 是空画布）",
    e["canvas"] == {"objects": []} and e["paper"] == "plain" and e["schema"] == 1, e)
chk("empty：不传 id 也拿得到一个名字", bool(board.empty()["id"]), None)
chk("empty：title 走的是同一个裁剪口径", board.empty(None, "标" * 300)["title"]
    == "标" * board.MAX_TITLE, None)
chk("dir_of 只拼路径不建目录", board.dir_of("/tmp/x") == "/tmp/x/boards"
    and not os.path.isdir("/tmp/x"), board.dir_of("/tmp/x"))
chk("BOARD_DIR / BOARD_FILE_EXT 与落盘约定一致",
    board.BOARD_DIR == "boards" and board.BOARD_FILE_EXT == ".json", None)
chk("_human_size：B / KB / MB 三档都说得清",
    board._human_size(999) == "999 B" and board._human_size(348160) == "348 KB"
    and board._human_size(1572864) == "1.6 MB",
    [board._human_size(999), board._human_size(348160), board._human_size(1572864)])
chk("_human_size：整数量不写小数点（跟上限常量的读法对得上）",
    board._human_size(board.MAX_JSON_BYTES) == "6 MB"
    and board._human_size(board.MAX_PNG_BYTES) == "25 MB"
    and board._human_size(board.MAX_SVG_BYTES) == "8 MB",
    [board._human_size(board.MAX_JSON_BYTES), board._human_size(board.MAX_PNG_BYTES)])

# ── 4. save_board → load_board 往返 ──────────────────────────────
bk = book_dir("book-roundtrip")
doc = {"id": "b8f2c1a4", "title": "第 3 章的受力分析", "caption": "画在纸上的草图说明",
       "paper": "dots", "canvas": CANVAS}
res = board.save_board(bk, doc)
path = os.path.join(bk, "boards", "b8f2c1a4.json")
chk("save_board：返回 ok 与契约字段",
    res["ok"] and set(res) == {"ok", "path", "id", "bytes", "msg"}, res)
chk("save_board：文件真在 boards/ 下（不信返回值，看盘）", os.path.exists(path), res["path"])
chk("save_board：path 指向的那个文件就是它", os.path.exists(res["path"])
    and res["path"] == path, res["path"])
chk("save_board：msg 是人话（中文，带大小）", "画板已存好" in res["msg"] and "B" in res["msg"],
    res["msg"])
back = board.load_board(bk, "b8f2c1a4")
chk("load_board：读得回来", isinstance(back, dict), back)
chk("往返：中文键的值一个字节都没变", back["canvas"] == CANVAS, back["canvas"])
chk("往返：标题、说明、纸张、字节数、created 都对得上",
    (back["title"], back["caption"], back["paper"], back["bytes"], back["created"]) ==
    (doc["title"], doc["caption"], "dots", res["bytes"], back["created"]), back)
chk("往返：updated 是服务端盖的时间戳形状",
    re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", back["updated"]) is not None,
    back["updated"])
chk("盘上那份 JSON 的字段顺序就是信封约定",
    list(json.load(open(path, encoding="utf-8"))) ==
    ["schema", "id", "title", "caption", "paper", "canvas", "bytes", "created", "updated"],
    list(json.load(open(path, encoding="utf-8"))))
chk("save_board：同一块板再存一次不新增文件",
    board.save_board(bk, back)["ok"] and board.board_counts(bk) == 1,
    board.board_counts(bk))
again = board.load_board(bk, "b8f2c1a4")
chk("再存一次：created 不变（是同一块板）", again["created"] == back["created"], again)
chk("覆盖前留了一份 .prev 存档", os.path.exists(path + ".prev"), files_in(bk + "/boards"))
chk(".prev 不算画板、不占名额", board.board_counts(bk) == 1, board.board_counts(bk))
chk("load_board：不存在的板回 None 不抛", board.load_board(bk, "nope") is None, None)
chk("load_board：目录还没建出来也不抛",
    board.load_board(os.path.join(SANDBOX, "book-none"), "a") is None, None)
thrown, _ = no_throw(board.load_board, os.path.join(SANDBOX, "book-none"), "a")
chk("load_board：坏 id（../）不抛、也只认安全名",
    not thrown and board.load_board(bk, "../../escape") is None, None)

# 超大画布：拒绝，且磁盘上一个字都不留
big = book_dir("book-big")
thrown, over = no_throw(board.save_board, big, {"id": "huge",
                                               "canvas": {"objects": [{"d": "x" * 7_000_000}]}})
chk("save_board：超 MAX_JSON_BYTES 被拒", over["ok"] is False and "超过上限" in over["msg"], over)
chk("save_board：超上限说的是「超了多少」", "6 MB" in over["msg"] and "超了" in over["msg"],
    over["msg"])
chk("save_board：超上限没留下文件", not os.path.exists(os.path.join(big, "boards", "huge.json"))
    and board.board_counts(big) == 0, files_in(os.path.join(big, "boards")))
chk("save_board：超上限也没留下半个临时文件",
    [f for f in files_in(board.dir_of(big)) if f.endswith(".tmp")] == [],
    files_in(board.dir_of(big)))
chk("save_board：坏 JSON 内容（不可序列化）被拒且不抛",
    no_throw(board.save_board, big, {"id": "nan", "canvas": {"o": float("nan")}})[0] is False
    and board.load_board(big, "nan") is None, None)

# ── 5. 路径越界：这是安全门，不是装饰 ────────────────────────────
iso = os.path.join(SANDBOX, "isolation")
os.makedirs(iso, exist_ok=True)
sealed = os.path.join(iso, "book")
os.makedirs(sealed, exist_ok=True)
before = files_in(iso)
esc_res = board.save_board(sealed, {"id": "../../escape", "title": "越界试试", "canvas": CANVAS})
chk("越界：存这一手是成功的（改了名，不是失败）", esc_res["ok"] is True, esc_res)
chk("越界：产物落在书目录的 boards/ 里",
    os.path.exists(os.path.join(sealed, "boards", "escape.json")), esc_res["path"])
chk("越界：书目录的父目录里没多出任何东西", files_in(iso) == before, [before, files_in(iso)])
chk("越界：书目录本身也没多出散落文件", files_in(sealed) == ["boards"], files_in(sealed))
chk("越界：全盘找不到 escape.json 以外的越界产物",
    not os.path.exists(os.path.join(iso, "escape.json"))
    and not os.path.exists(os.path.join(SANDBOX, "escape.json")), None)
chk("越界：导出的 PNG 也被摁在 boards/ 里",
    board.save_export(sealed, "../../escape", "png", PNG_URL)["ok"]
    and files_in(board.dir_of(sealed)) == ["escape.json", "escape.png"],
    files_in(board.dir_of(sealed)))
chk("_file_of：手工构造的越界名字直接回空串",
    board._file_of(sealed, "../evil.json") == "", board._file_of(sealed, "../evil.json"))

# ── 6. list_boards / board_counts ────────────────────────────────
empty_book = os.path.join(SANDBOX, "book-nodir")
chk("list_boards：boards/ 还不存在时回空列表不抛",
    board.list_boards(empty_book) == [] and board.board_counts(empty_book) == 0, None)
thrown, _ = no_throw(board.list_boards, empty_book)
chk("list_boards：目录不存在时确实没抛", not thrown, None)

lb = book_dir("book-list")
for sid in ("aaa", "bbb", "ccc"):
    board.save_board(lb, {"id": sid, "title": "板" + sid, "canvas": {"objects": []}})
stamp(os.path.join(lb, "boards", "aaa.json"), "2026-10-01T09:00:00")
stamp(os.path.join(lb, "boards", "bbb.json"), "2026-10-03T23:59:59")
stamp(os.path.join(lb, "boards", "ccc.json"), "2026-10-02T12:00:00")
board.save_export(lb, "bbb", "svg", SVG_TEXT)
board.save_export(lb, "bbb", "png", PNG_URL)
with open(os.path.join(lb, "boards", "readme.txt"), "w", encoding="utf-8") as f:
    f.write("随手放的说明，不是画板")
with open(os.path.join(lb, "boards", "中文名.json"), "w", encoding="utf-8") as f:
    f.write("{}")
with open(os.path.join(lb, "boards", "a.b.json"), "w", encoding="utf-8") as f:
    f.write("{}")
rows = board.list_boards(lb)
chk("list_boards：按 updated 新→旧", [r["id"] for r in rows[:3]] == ["bbb", "ccc", "aaa"],
    [r["id"] for r in rows])
chk("list_boards：readme.txt 不进列表", "readme.txt" not in [r["id"] for r in rows],
    [r["id"] for r in rows])
chk("list_boards：导出物、临时名、留档名都不占名额",
    board.board_counts(lb) == len(rows), [board.board_counts(lb), len(rows)])
chk("list_boards：has_png / has_svg 如实，名字给的是文件名",
    rows[0]["has_png"] and rows[0]["has_svg"] and rows[0]["png_name"] == "bbb.png"
    and rows[0]["svg_name"] == "bbb.svg", rows[0])
chk("list_boards：没有导出物时 has_ 为假、名字是空串",
    rows[1]["has_png"] is False and rows[1]["has_svg"] is False
    and rows[1]["png_name"] == "" and rows[1]["svg_name"] == "", rows[1])
chk("list_boards：每行字段就是约定的那十个",
    set(rows[0]) == {"id", "title", "caption", "paper", "updated", "bytes",
                     "has_svg", "has_png", "svg_name", "png_name"}, sorted(rows[0]))
chk("list_boards：不返回 canvas（列表只给元信息）", "canvas" not in rows[0], sorted(rows[0]))
chk("list_boards：bytes 是存进去时算好的画布大小",
    rows[0]["bytes"] == len(json.dumps({"objects": []}, ensure_ascii=False).encode("utf-8")),
    rows[0]["bytes"])
chk("list_boards：中文名野文件不算画板", "中文名" not in [r["id"] for r in rows],
    [r["id"] for r in rows])
chk("list_boards：名字带点的野文件不算画板（盘上还在，只是不认）",
    "a.b" not in [r["id"] for r in rows] and "a.b.json" in files_in(board.dir_of(lb)),
    [r["id"] for r in rows])

# ── 7. save_export：PNG ──────────────────────────────────────────
pb = book_dir("book-png")
ok_png = board.save_export(pb, "pic1", "png", PNG_URL)
chk("save_export png：正常 dataURL 收下", ok_png["ok"] is True, ok_png)
chk("save_export png：返回字段齐", set(ok_png) == {"ok", "path", "bytes", "fmt", "msg"}, ok_png)
chk("save_export png：文件名是 <id>.png 且在 boards/ 下",
    ok_png["path"] == os.path.join(pb, "boards", "pic1.png")
    and os.path.exists(ok_png["path"]), ok_png["path"])
chk("save_export png：落盘内容与解出来的字节一致",
    open(ok_png["path"], "rb").read() == PNG_BODY, None)
chk("save_export png：bytes 是真字节数", ok_png["bytes"] == len(PNG_BODY), ok_png["bytes"])
chk("save_export png：fmt 回填", ok_png["fmt"] == "png", ok_png["fmt"])
chk("save_export png：msg 是人话「已导出 PNG（…）」",
    ok_png["msg"].startswith("已导出 PNG（") and ok_png["msg"].endswith("）"), ok_png["msg"])
bare = board.save_export(pb, "pic2", "png", PNG_B64)
chk("save_export png：裸 base64 也收", bare["ok"] and os.path.exists(bare["path"]), bare)
folded = board.save_export(pb, "pic3", "png", PNG_FOLDED)
chk("save_export png：折过行的 base64 去掉空白照样收",
    folded["ok"] and open(folded["path"], "rb").read() == PNG_BODY, folded)
gif = board.save_export(pb, "gif1", "png", "data:image/png;base64,"
                      + base64.b64encode(b"GIF89a" + b"\x00" * 40).decode("ascii"))
chk("save_export png：GIF 冒充 PNG 被拒", gif["ok"] is False and "这不是 PNG" in gif["msg"], gif)
chk("save_export png：被拒的 GIF 没有留下文件",
    not os.path.exists(os.path.join(pb, "boards", "gif1.png")), files_in(board.dir_of(pb)))
svg_as_png = board.save_export(pb, "svg2", "png", SVG_URL)
chk("save_export png：SVG 的 dataURL 当成 PNG 给，也被魔数挡下",
    svg_as_png["ok"] is False and not os.path.exists(os.path.join(pb, "boards", "svg2.png")),
    svg_as_png)
thrown, broken = no_throw(board.save_export, pb, "bad1", "png", "这不是@@@base64")
chk("save_export png：坏 base64 不抛", not thrown, broken)
chk("save_export png：坏 base64 回 ok=False 说人话",
    broken["ok"] is False and "解不开" in broken["msg"], broken)
chk("save_export png：坏 base64 没留下文件",
    not os.path.exists(os.path.join(pb, "boards", "bad1.png")), files_in(board.dir_of(pb)))
thrown, pad = no_throw(board.save_export, pb, "pad1", "png", "AAAA=###")
chk("save_export png：填充位错的 base64 也归成拒绝", not thrown and pad["ok"] is False, pad)
thrown, huge = no_throw(board.save_export, pb, "huge1", "png", "data:image/png;base64,"
                       + base64.b64encode(board.PNG_MAGIC
                                          + b"z" * (board.MAX_PNG_BYTES - 8 + 5000)).decode())
chk("save_export png：超 MAX_PNG_BYTES 被拒", not thrown and huge["ok"] is False, huge)
chk("save_export png：超上限说清上限多少、超了多少",
    "上限 25 MB" in huge["msg"] and "超了 5 KB" in huge["msg"], huge["msg"])
chk("save_export png：超上限没写半个文件",
    not os.path.exists(os.path.join(pb, "boards", "huge1.png"))
    and not os.path.exists(os.path.join(pb, "boards", "huge1.png.tmp"))
    and not os.path.exists(os.path.join(pb, "boards", "huge1.png.prev")),
    files_in(board.dir_of(pb)))
chk("save_export png：空数据被拒且不抛",
    no_throw(board.save_export, pb, "nil", "png", "")[1]["ok"] is False
    and not os.path.exists(os.path.join(pb, "boards", "nil.png")), None)
chk("save_export png：dict 之类的怪载荷归成拒绝",
    board.save_export(pb, "dict1", "png", {"objects": []})["ok"] is False
    and not os.path.exists(os.path.join(pb, "boards", "dict1.png")), None)
chk("save_export：fmt 只认 svg / png",
    board.save_export(pb, "x", "jpg", PNG_URL)["ok"] is False
    and board.save_export(pb, "x", "", PNG_URL)["ok"] is False, None)
chk("save_export：目录里没留任何 .tmp",
    [f for f in files_in(board.dir_of(pb)) if f.endswith(".tmp")] == [],
    files_in(board.dir_of(pb)))
again = board.save_export(pb, "pic1", "png", PNG_URL)
chk("save_export：同一块板重复导出只覆盖不堆积",
    again["ok"] and files_in(board.dir_of(pb)).count("pic1.png") == 1, files_in(board.dir_of(pb)))

# ── 8. save_export：SVG ──────────────────────────────────────────
sb = book_dir("book-svg")
s1 = board.save_export(sb, "s1", "svg", "  <?xml version=\"1.0\" encoding=\"utf-8\"?>\n" + SVG_TEXT.split("\n", 1)[1])
chk("save_export svg：前面有空白和 XML 声明也认", s1["ok"] is True, s1)
chk("save_export svg：文件名 <id>.svg 在 boards/ 下",
    s1["path"] == os.path.join(sb, "boards", "s1.svg") and os.path.exists(s1["path"]), s1["path"])
chk("save_export svg：msg 是人话「已导出 SVG（…）」",
    s1["msg"].startswith("已导出 SVG（"), s1["msg"])
s2 = board.save_export(sb, "s2", "svg", SVG_TEXT)
s2t = open(s2["path"], encoding="utf-8").read() if s2["ok"] else ""
chk("save_export svg：带 XML 声明的也收，落盘那份从 <svg 开始（声明不留在盘上）",
    s2["ok"] and s2t.startswith("<svg") and '<rect x="1" y="1"' in s2t, (s2, s2t[:48]))
s3 = board.save_export(sb, "s3", "svg", SVG_URL)
s3t = open(s3["path"], encoding="utf-8").read() if s3["ok"] else ""
chk("save_export svg：dataURL 形式存的是 SVG 源码不是 dataURL",
    s3["ok"] and s3t.startswith("<svg") and "data:image" not in s3t, (s3, s3t[:48]))
# fabric.js 每一份 toSVG 都在这张图前面挂一条指向 w3.org 的 DOCTYPE：留着它，
# 严格点的解析器会去联网取 DTD，而这是一张本地图纸。收下来可以，落盘要干净。
sfabric = board.save_export(sb, "sfabric", "svg",
                            ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
                             '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
                             '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
                             + SVG_TEXT.split("\n", 1)[1]))
sft = open(sfabric["path"], encoding="utf-8").read() if sfabric["ok"] else ""
chk("save_export svg：fabric 那条 DOCTYPE 不跟着落盘（图形内容一个不少）",
    sfabric["ok"] and sft.startswith("<svg") and "DOCTYPE" not in sft
    and '<rect x="1" y="1"' in sft, (sfabric, sft[:48]))
s4 = board.save_export(sb, "s4", "svg", "data:image/svg+xml;charset=utf-8,"
                      + "  " + SVG_TEXT.replace("<", "%3C").replace(">", "%3E"))
chk("save_export svg：百分号编码的 dataURL 也解得开", s4["ok"], s4)
s5 = board.save_export(sb, "s5", "svg", "<!-- 一句注释 -->\n" + SVG_TEXT.split("\n", 1)[1])
chk("save_export svg：前面是注释也能露出 <svg", s5["ok"], s5)
rej = board.save_export(sb, "rej", "svg", "<html><body>不是图</body></html>")
chk("save_export svg：<html> 被拒", rej["ok"] is False and "这不是 SVG" in rej["msg"], rej)
chk("save_export svg：被拒的没留下文件", not os.path.exists(os.path.join(sb, "boards", "rej.svg")),
    files_in(board.dir_of(sb)))
chk("save_export svg：空串被拒", board.save_export(sb, "nil2", "svg", "")["ok"] is False
    and not os.path.exists(os.path.join(sb, "boards", "nil2.svg")), None)
chk("save_export svg：PNG 的 dataURL 当成 SVG 给，被前缀挡下",
    board.save_export(sb, "p2s", "svg", PNG_URL)["ok"] is False
    and not os.path.exists(os.path.join(sb, "boards", "p2s.svg")), None)
over = board.save_export(sb, "over1", "svg",
                         "<svg " + "a" * (board.MAX_SVG_BYTES + 10) + "></svg>")
chk("save_export svg：超 MAX_SVG_BYTES 被拒", over["ok"] is False and "超过上限" in over["msg"], over)
chk("save_export svg：超上限没写半个文件",
    not os.path.exists(os.path.join(sb, "boards", "over1.svg"))
    and not os.path.exists(os.path.join(sb, "boards", "over1.svg.tmp")),
    files_in(board.dir_of(sb)))
chk("save_export svg：坏 base64 的 dataURL 归成拒绝不抛",
    no_throw(board.save_export, sb, "badsvg", "svg", "data:image/svg+xml;base64,@@@")[1]["ok"]
    is False, None)

# ── 9. delete_board ──────────────────────────────────────────────
db = book_dir("book-delete")
board.save_board(db, {"id": "d1", "canvas": {"objects": []}})
board.save_export(db, "d1", "svg", SVG_TEXT)
board.save_export(db, "d1", "png", PNG_URL)
chk("delete_board：删之前三个文件确实都在（夹具铺对了）",
    all(f in files_in(board.dir_of(db)) for f in ("d1.json", "d1.svg", "d1.png")),
    files_in(board.dir_of(db)))
d1 = board.delete_board(db, "d1", purge=True)
chk("delete_board：purge=True 三个文件一起清",
    files_in(board.dir_of(db)) == [], files_in(board.dir_of(db)))
chk("delete_board：purge=True 的 removed 报数对（含文件名不带路径）",
    d1["ok"] is True and sorted(d1["removed"]) == ["d1.json", "d1.png", "d1.svg"], d1)
chk("delete_board：purge=True 的 msg 说清连带删了几个",
    "连带 3 个文件" in d1["msg"], d1["msg"])
chk("delete_board：purge=True 之后 load 回 None、counts 归零",
    board.load_board(db, "d1") is None and board.board_counts(db) == 0, None)
gone = board.delete_board(db, "d1", purge=True)
chk("delete_board：再删一次是 ok=True 配一句人话，不是报错",
    gone["ok"] is True and "不在了" in gone["msg"] and gone["removed"] == [], gone)
chk("delete_board：从没出现过的 id 也回 ok=True",
    board.delete_board(db, "../../ghost", purge=True)["ok"] is True, None)
board.save_board(db, {"id": "d2", "canvas": {"objects": []}})
board.save_export(db, "d2", "svg", SVG_TEXT)
board.save_export(db, "d2", "png", PNG_URL)
d2 = board.delete_board(db, "d2", purge=False)
chk("delete_board：purge=False 只收画板", d2["ok"] is True and d2["removed"] == ["d2.json"], d2)
chk("delete_board：purge=False 之后图还在原处",
    os.path.exists(os.path.join(db, "boards", "d2.svg"))
    and os.path.exists(os.path.join(db, "boards", "d2.png")), files_in(board.dir_of(db)))
chk("delete_board：purge=False 的话 msg 说清图留下了", "留在原处" in d2["msg"], d2["msg"])
chk("delete_board：越界 id 只会删掉 boards/ 里的安全名",
    board.delete_board(db, "../../d2", purge=True)["ok"] is True
    and files_in(db) == ["boards"], files_in(db))

# ── 10. to_markdown ──────────────────────────────────────────────
mb = book_dir("book-md")
board.save_board(mb, {"id": "b8f2c1a4", "title": "第 3 章的受力分析",
                      "caption": "画在纸上的草图说明：「先画整体，再画隔离体」",
                      "canvas": CANVAS})
md = board.to_markdown(board.load_board(mb, "b8f2c1a4"), mb)
chk("to_markdown：标题行是「### 画板：…」", md.splitlines()[0] == "### 画板：第 3 章的受力分析",
    md)
chk("to_markdown：说明紧跟在标题下面，中文引号原样",
    "「先画整体，再画隔离体」" in md.splitlines()[1], md.splitlines())
chk("to_markdown：没有导出图片时给一句人话，不留坏链接",
    "（这块板还没有导出图片）" in md and "](" not in md, md)
board.save_export(mb, "b8f2c1a4", "png", PNG_URL)
md_png = board.to_markdown(board.load_board(mb, "b8f2c1a4"), mb)
chk("to_markdown：有 PNG 时链接指向 boards/<id>.png",
    "![第 3 章的受力分析](boards/b8f2c1a4.png)" in md_png, md_png)
chk("to_markdown：相对链接用 / 不用反斜杠", "\\" not in md_png and "boards/" in md_png, md_png)
chk("to_markdown：有图就不再写「还没有导出图片」", "还没有导出图片" not in md_png, md_png)
chk("to_markdown：顺序是 标题 / 说明 / 空行 / 链接",
    len(md_png.rstrip().splitlines()) == 4 and md_png.rstrip().splitlines()[2] == "",
    md_png.splitlines())
only_svg = book_dir("book-md-svg")
board.save_board(only_svg, {"id": "sv1", "title": "只有 SVG", "canvas": {"objects": []}})
board.save_export(only_svg, "sv1", "svg", SVG_TEXT)
md_svg = board.to_markdown(board.load_board(only_svg, "sv1"), only_svg)
chk("to_markdown：只有 SVG 时链接指向 .svg", "![只有 SVG](boards/sv1.svg)" in md_svg, md_svg)
chk("to_markdown：PNG 优先于 SVG（两个都在时只贴一个链接）",
    md_png.count("](") == 1 and md.count("](") == 0, [md_png, md])
untitled = board.to_markdown({"id": "noTitle", "title": "", "caption": ""}, None)
chk("to_markdown：空标题用「未命名画板」", "### 画板：未命名画板" in untitled, untitled)
chk("to_markdown：没 base_dir 时认 list_boards 送来的 has_png 标记",
    "boards/noTitle.png" in board.to_markdown({"id": "noTitle", "has_png": True}, None), None)
chk("to_markdown：没 base_dir 也没标记时不留链接",
    "](" not in board.to_markdown({"id": "noTitle"}, None), None)
bracket = board.to_markdown({"id": "b1", "title": "受力[分析]*号", "has_png": True}, None)
link_line = [ln for ln in bracket.splitlines() if ln.startswith("![")][0]
chk("to_markdown：标题里的方括号不弄坏链接（替代文字里不再有方括号）",
    link_line == "![受力分析*号](boards/b1.png)"
    and "[" not in link_line[2:].split("]")[0] and "](" in link_line, bracket)
chk("to_markdown：标题原样进小标题一行（方括号只在链接里才需要处理）",
    bracket.splitlines()[0] == "### 画板：受力[分析]*号", bracket)
chk("to_markdown：说明为空时不留一行空白",
    board.to_markdown({"id": "b2", "title": "没说明", "has_png": True}, None).count("\n\n") == 1,
    board.to_markdown({"id": "b2", "title": "没说明", "has_png": True}, None))
chk("to_markdown：喂进来的东西不是 dict 也不抛",
    no_throw(board.to_markdown, "不是对象")[0] is False, None)
chk("to_markdown：能直接吃 list_boards 的行（界面那条路走得通）",
    all("### 画板：" in board.to_markdown(r, mb) for r in board.list_boards(mb)), None)

# ── 11. MAX_BOARDS：第 201 张的处置 ──────────────────────────────
cap = book_dir("book-cap")
ids = ["cap%03d" % n for n in range(board.MAX_BOARDS)]
saved_all = all(board.save_board(cap, {"id": sid, "canvas": {"objects": []}})["ok"]
                for sid in ids)
chk("MAX_BOARDS：铺满 200 张都存下来了", saved_all and board.board_counts(cap) == 200,
    board.board_counts(cap))
over201 = board.save_board(cap, {"id": "cap201", "canvas": {"objects": []}})
chk("MAX_BOARDS：第 201 张被拒绝（实现的是「拒绝并存报人话」）",
    over201["ok"] is False and "上限" in over201["msg"], over201)
chk("MAX_BOARDS：msg 说清怎么办（先删几张）", "先删掉几张再存" in over201["msg"], over201["msg"])
chk("MAX_BOARDS：被拒的第 201 张没落盘",
    not os.path.exists(os.path.join(cap, "boards", "cap201.json"))
    and board.board_counts(cap) == board.MAX_BOARDS, board.board_counts(cap))
chk("MAX_BOARDS：满员时旧板还能改（不是把用户锁死）",
    board.save_board(cap, {"id": "cap000", "title": "改一笔",
                           "canvas": {"objects": []}})["ok"] is True, None)
chk("MAX_BOARDS：满员时删一张就能再存",
    board.delete_board(cap, "cap199")["ok"] is True
    and board.save_board(cap, {"id": "cap201", "canvas": {"objects": []}})["ok"] is True, None)
chk("MAX_BOARDS：删掉导出物不占名额（只数 .json）",
    board.save_export(cap, "cap000", "png", PNG_URL)["ok"]
    and board.board_counts(cap) == board.MAX_BOARDS, board.board_counts(cap))

# ── 12. 全模块的兜底与仓库规矩 ───────────────────────────────────
junk = book_dir("book-junk")
with open(os.path.join(junk, "boards"), "w", encoding="utf-8") as fp:
    fp.write("boards 这个名字被占成了一个文件")
chk("boards 位置上是个文件时：列目录回空、不抛",
    no_throw(board.list_boards, junk)[0] is False and board.list_boards(junk) == [], None)
chk("boards 位置上是个文件时：存板回人话而不是抛异常",
    no_throw(board.save_board, junk, {"id": "j1", "canvas": {"objects": []}})[0] is False
    and board.save_board(junk, {"id": "j1", "canvas": {"objects": []}})["ok"] is False, None)
broken = book_dir("book-broken")
board.save_board(broken, {"id": "br1", "title": "坏档", "canvas": {"objects": []}})
with open(os.path.join(broken, "boards", "br1.json"), "w", encoding="utf-8") as f:
    f.write('{"id": "br1", "canvas": {只有半截')
chk("坏 JSON：load_board 回 None 不抛", no_throw(board.load_board, broken, "br1")[0] is False
    and board.load_board(broken, "br1") is None, None)
chk("坏 JSON：list_boards 还是把它列出来（有 updated 才能排序）",
    len(board.list_boards(broken)) == 1 and bool(board.list_boards(broken)[0]["updated"]),
    board.list_boards(broken))
chk("坏 JSON：删得掉（用户能清掉它）",
    board.delete_board(broken, "br1")["ok"] is True and board.board_counts(broken) == 0, None)

src = (REPO / "board.py").read_text(encoding="utf-8")
own = Path(__file__).resolve().read_text(encoding="utf-8")
chk("board.py 源码零 emoji", not has_emoji(src), has_emoji(src)[:8])
chk("本套件源码零 emoji", not has_emoji(own), has_emoji(own)[:8])
chk("board.py 只用标准库",
    sorted({re.sub(r"\..*$", "", m.split()[1]) for m in re.findall(r"^import .*", src, re.M)})
    == ["base64", "binascii", "json", "os", "re", "time", "urllib", "uuid"],
    sorted({re.sub(r"\..*$", "", m.split()[1]) for m in re.findall(r"^import .*", src, re.M)}))
chk("board.py 不碰网络（没有 socket / http / urllib.request）",
    "urllib.request" not in src and "socket" not in src and "http.client" not in src, None)
chk("契约里的函数一个不缺", all(callable(getattr(board, n, None)) for n in
    ("slug", "dir_of", "empty", "normalize_env", "list_boards", "load_board", "save_board",
     "delete_board", "save_export", "to_markdown", "board_counts")), None)
chk("契约里的常量一个不改",
    board.BOARD_DIR == "boards" and board.PAPERS == ("plain", "grid", "dots", "lines", "dark")
    and board.MAX_JSON_BYTES == 6_000_000 and board.MAX_TITLE == 80
    and board.MAX_CAPTION == 2000 and board.MAX_BOARDS == 200
    and board.MAX_SVG_BYTES == 8_000_000 and board.MAX_PNG_BYTES == 25_000_000, None)

print()
if FAIL:
    print("失败 %d 项（共 %d 条断言）：%s" % (len(FAIL), TOTAL, "、".join(FAIL)))
    sys.exit(1)
print("全部通过（%d 条断言）" % TOTAL)
