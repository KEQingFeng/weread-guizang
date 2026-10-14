# -*- coding: utf-8 -*-
"""画板存盘的收尾门禁（board.py）：坏字符不许把请求打成 500，删板不许留隐形垃圾。

只碰系统临时目录里的沙盒，不开浏览器、不起服务：board.py 是纯存储层，
存/读/删都能直接对着函数打。tests/check_board.py 管的是「正常存得下、读得出、
导得走」，这一份专门管那两条只在边上才踩到的路：

  · **写不进去的字符**：浏览器把 JS 字符串拦腰截断时会留下半个代理码点（U+D800 这一区），
    json 收得下、却写不进 utf-8。标题里带一个，整块画板就存不下去 —— 用户画了
    半天的东西，因为标题里一个坏字符白存。修法是写之前摘掉它，并且保证失败时
    不把半截的 .tmp 留在 boards/ 里。
  · **删板留的副产物**：覆盖保存会留一份 `<id>.json.prev`，它是「只有磁盘看得见」
    的（列表不认它、不占名额），所以 purge 删除必须把它一起收掉，否则它永远占盘、
    界面上还看不见、删不掉。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402

# 落单的半个代理码点：chr() 现造，免得源码里出现字面上的半个码点（那会让文件本身读不动）
HI = chr(0xD800)
LO = chr(0xDFFF)

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
    d = tempfile.mkdtemp(prefix="guizang-board-store-")
    os.makedirs(os.path.join(d, "boards"), exist_ok=True)
    return d


def names(book_dir):
    return sorted(os.listdir(os.path.join(book_dir, "boards")))


# ── 1. _text：摘掉落单的代理码点，真字符一个字不动 ──────────────────
chk("_text：落单的半个代理码点摘掉", board._text("前" + HI + "后", 80) == "前后",
    board._text("前" + HI + "后", 80))
chk("_text：低区代理码点也摘", board._text(LO + "提示", 80) == "提示")
chk("_text：普通中文 / 英文原样留着", board._text(" 归藏 board 画板 ", 80) == "归藏 board 画板")
chk("_text：成对的真字符（😀）不许误伤", board._text("画板😀好", 80) == "画板😀好",
    repr(board._text("画板😀好", 80)))
chk("_text：裁长度照旧", board._text("abcdef", 3) == "abc")
chk("_text：None 给空串", board._text(None, 80) == "")

# 整个信封过一遍：标题与说明里的坏字符都不在了
env = board.normalize_env({"id": "b1", "title": "标" + HI + "题",
                           "caption": "说" + LO + "明", "canvas": {"objects": []}})
chk("normalize_env：标题与说明都洗过", env["title"] == "标题" and env["caption"] == "说明",
    (env["title"], env["caption"]))

# ── 2. save_board：标题里半个代理码点仍要存得下，且不留 .tmp ────────
d = mk()
r = board.save_board(d, {"id": "b1", "title": "标" + HI + "题", "canvas": {"objects": []}})
chk("存板：标题有坏字符也回 ok=True（不许抛、不许 500）", r.get("ok") is True, r)
chk("存板：只落下一份 .json，没有 .tmp 垃圾", names(d) == ["b1.json"], names(d))
got = board.load_board(d, "b1")
chk("读回：标题是洗过的、画布还在", got and got["title"] == "标题" and got["canvas"] == {"objects": []}, got)

# 全是坏字符的标题也不能把这块板带下去
r2 = board.save_board(d, {"id": "b2", "title": HI + LO, "canvas": {"objects": [1]}})
chk("存板：标题整条都是坏字符也存得下", r2.get("ok") is True and board.load_board(d, "b2")["title"] == "",
    r2)
chk("存板：这块也没留 .tmp", not any(n.endswith(".tmp") for n in names(d)), names(d))

# 正常标题照旧存读一致（不因为这一改而漂）
ok = board.save_board(d, {"id": "b3", "title": "正常标题😀", "caption": "一句说明",
                          "canvas": {"objects": []}})
back = board.load_board(d, "b3")
chk("存板：正常标题 / 说明 / 真字符存读一致",
    ok.get("ok") and back["title"] == "正常标题😀" and back["caption"] == "一句说明", back)

# ── 3. save_board：画布里的坏内容给一句人话，不是异常 ───────────────
bad = board.save_board(d, {"id": "b4", "canvas": {"objects": [{"text": HI}]}})
chk("存板：画布里有写不进 JSON 的内容 → ok=False 配人话，不抛",
    bad.get("ok") is False and "存不进 JSON" in bad["msg"], bad)
chk("存板：画布被拒时不落文件、不留 .tmp",
    not os.path.exists(os.path.join(d, "boards", "b4.json"))
    and not os.path.exists(os.path.join(d, "boards", "b4.json.tmp")), names(d))

nan = board.save_board(d, {"id": "b5", "canvas": {"objects": [{"x": float("nan")}]}})
chk("存板：画布里有 NaN → ok=False 配人话（浏览器 JSON.parse 收不了）",
    nan.get("ok") is False and "存不进 JSON" in nan["msg"], nan)

# _write_json 自己写崩了也要把 .tmp 收掉
t = mk()
raised = False
try:
    board._write_json(os.path.join(t, "boards", "x.json"), {"bad": {1, 2}})
except TypeError:
    raised = True
chk("_write_json：写崩时抛出去，但 .tmp 收干净了",
    raised and not os.path.exists(os.path.join(t, "boards", "x.json.tmp")), names(t))
shutil.rmtree(t, ignore_errors=True)

# ── 4. delete_board：purge 连 .prev / .tmp 一起清 ───────────────────
d2 = mk()
board.save_board(d2, {"id": "keep", "canvas": {"objects": []}})
board.save_board(d2, {"id": "keep", "canvas": {"objects": [1]}})   # 二次保存 → keep.json.prev
board._write_bytes(os.path.join(d2, "boards", "keep.svg"), b"<svg/>")
board._write_bytes(os.path.join(d2, "boards", "keep.png"), b"\x89PNG")
# 模拟旧版本写崩留下的半截临时文件
with open(os.path.join(d2, "boards", "keep.json.tmp"), "w", encoding="utf-8") as f:
    f.write("半截")
chk("删前夹具：.prev / .tmp / svg / png / json 都在",
    set(names(d2)) == {"keep.json", "keep.json.prev", "keep.json.tmp", "keep.svg", "keep.png"},
    names(d2))
res = board.delete_board(d2, "keep", purge=True)
chk("删板 purge：回 ok=True", res.get("ok") is True, res)
chk("删板 purge：.prev / .tmp 这两个隐形副产物也被收走", names(d2) == [], names(d2))
chk("删板 purge：.prev 确实走了（不是被列表忽略）",
    not os.path.exists(os.path.join(d2, "boards", "keep.json.prev")))
chk("删板 purge：removed 如实报出被删的文件名（含 .prev / .tmp）",
    "keep.json.prev" in res.get("removed", []) and "keep.json.tmp" in res.get("removed", []),
    res.get("removed"))

# 没有 .prev 的普通板：purge 行为不变
d3 = mk()
board.save_board(d3, {"id": "plain", "canvas": {"objects": []}})
board._write_bytes(os.path.join(d3, "boards", "plain.png"), b"\x89PNG")
res3 = board.delete_board(d3, "plain", purge=True)
chk("删板 purge：没有副产物的板照旧清干净", res3.get("ok") and names(d3) == [], (res3, names(d3)))

# purge=False：只收画板，导出物留在原处
d4 = mk()
board.save_board(d4, {"id": "keepimg", "canvas": {"objects": []}})
board._write_bytes(os.path.join(d4, "boards", "keepimg.png"), b"\x89PNG")
res4 = board.delete_board(d4, "keepimg", purge=False)
chk("删板 purge=False：只收 json，png 留在原处",
    res4.get("removed") == ["keepimg.json"] and os.path.exists(os.path.join(d4, "boards", "keepimg.png")),
    (res4, names(d4)))
chk("删板 purge=False：msg 说清图留下了", "留在原处" in res4.get("msg", ""), res4.get("msg"))

# 越界 id 只会落在 boards/ 里的安全名上（沿用 slug 那道闸）
d5 = mk()
board.save_board(d5, {"id": "safe", "canvas": {"objects": []}})
board.delete_board(d5, "../../safe", purge=True)
chk("删板：越界 id 仍只删 boards/ 里的安全名", names(d5) == [], names(d5))

# ── 5. 二次保存的 .prev 存档行为没被这次修改动过 ─────────────────────
d6 = mk()
board.save_board(d6, {"id": "arch", "canvas": {"objects": []}})
board.save_board(d6, {"id": "arch", "canvas": {"objects": [2]}})
chk("存档：覆盖保存仍留一份 .prev", os.path.exists(os.path.join(d6, "boards", "arch.json.prev")))
chk("存档：.prev 仍不算画板、不占名额", board.board_counts(d6) == 1, board.board_counts(d6))

for tmpdir in (d, d2, d3, d4, d5, d6):
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("画板存盘收尾门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
