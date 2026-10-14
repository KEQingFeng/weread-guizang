# -*- coding: utf-8 -*-
"""思维导图存盘的收尾门禁（mindmap.py）：写不进 utf-8 的坏字符不许让整张图存不下去。

只碰系统临时目录里的沙盒，不开浏览器、不起服务：mindmap.py 的清洗与读写都是纯函数 /
文件操作，能直接对着打。tests/check_mindmap.py 管的是「正常存得下、读得出、布局与 SVG
对得上」，这一份专门管边上那一类：

  · **写不进去的字符**：浏览器把 JS 字符串拦腰截断时会留下半个代理码点（U+D800 这一区），
    JSON 收得下、却写不进 utf-8。标题或节点里带一个，整张图就存不下去 —— 用户画了半天的
    图，因为一个坏字符白存，还在书目录里留一个半截 .tmp。修法跟画板（check_board_store）
    同一口径：写之前摘掉落单的那半个，成对的表情符号一个字不动。

沙盒是临时目录，不碰用户真实书库。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mindmap as mm  # noqa: E402

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
    return tempfile.mkdtemp(prefix="guizang-mindmap-store-")


def names(book_dir):
    return sorted(os.listdir(book_dir))


# ── 1. 字符串清洗：摘掉落单的半个，真字符一个字不动 ──────────────────
chk("_clip：落单的半个代理码点摘掉", mm._clip("前" + HI + "后", 80) == "前后",
    repr(mm._clip("前" + HI + "后", 80)))
chk("_clip：低区代理码点也摘", mm._clip(LO + "提示", 80) == "提示")
chk("_clip：成对的真字符（😀）不许误伤", mm._clip("图😀好", 80) == "图😀好",
    repr(mm._clip("图😀好", 80)))
chk("_clip：裁长度照旧", mm._clip("abcdef", 3) == "abc")

st = {"dropped": 0}
chk("_trunc：备注里的坏字符摘掉、长度照裁", mm._trunc("备" + HI + "注", 80, st) == "备注")
chk("_trunc：摘字符不算「被裁」（dropped 不涨）", st["dropped"] == 0, st)
mm._trunc("x" * 5, 3, st)
chk("_trunc：真被裁时照旧报数", st["dropped"] == 1, st)

chk("_stamp：时间戳里的坏字符摘掉", mm._stamp("2026-10-15" + HI) == "2026-10-15")
chk("_stamp：正常时间戳原样留着", mm._stamp("2026-10-15T00:43:09") == "2026-10-15T00:43:09")

# ── 2. normalize：信封里每一处文本都洗过，真字符与幂等不受影响 ────────
doc = {"title": "标" + HI + "题",
       "nodes": [{"id": "a", "text": "节" + LO + "点", "note": HI + "备注",
                  "children": [{"id": "b", "text": "子", "children": []}]}],
       "links": [{"id": "l1", "from": "a", "to": "b", "text": "关" + HI + "系"}]}
n = mm.normalize(doc)
chk("normalize：标题洗过", n["title"] == "标题", repr(n["title"]))
chk("normalize：节点正文与备注洗过",
    n["nodes"][0]["text"] == "节点" and n["nodes"][0]["note"] == "备注",
    (n["nodes"][0]["text"], n["nodes"][0]["note"]))
chk("normalize：连线文字也洗过", n["links"][0]["text"] == "关系", n["links"][0]["text"])
chk("normalize：正常 emoji 与中文原样留着",
    mm.normalize({"title": "图😀好", "nodes": [{"id": "n", "text": "表情🧑‍💻在",
                                                "children": []}]})["nodes"][0]["text"]
    == "表情🧑‍💻在")
chk("normalize：洗过之后再洗一次结果完全相等（幂等没破）", mm.normalize(n) == n)

# 手改过的文件里，转义形式的半个代理码点也要在读进来后被洗掉

# ── 3. save_map：坏字符仍要存得下，且不留 .tmp ──────────────────────
d = mk()
r = mm.save_map(d, doc)
chk("存图：标题 / 节点 / 连线里有坏字符也回 ok=True（不许抛）", r.get("ok") is True, r)
chk("存图：落盘字节大于零、节点与连线是实况", r.get("bytes", 0) > 0 and r["nodes"] == 2
    and r["links"] == 1, r)
chk("存图：书目录里只有 mindmap.json，没有 .tmp 垃圾", names(d) == ["mindmap.json"], names(d))
back = mm.load_map(d)
chk("读回：坏字符都没了，正常字还在",
    back["title"] == "标题" and back["nodes"][0]["text"] == "节点"
    and back["links"][0]["text"] == "关系", back)

# 整条文本都是坏字符也不能把这张图带下去
r2 = mm.save_map(d, {"title": HI + LO, "nodes": [{"id": "x", "text": HI, "children": []}]})
chk("存图：整条都是坏字符也存得下（存成空串）",
    r2.get("ok") is True and mm.load_map(d)["title"] == "", r2)
chk("存图：这块也没留 .tmp", not any(x.endswith(".tmp") for x in names(d)), names(d))

# 从一份「手改坏」的磁盘文件读进来再存回去：读不抛、存得下。
# 这里用默认的 ensure_ascii=True 写成转义形式（"\ud83d"），正是用户手改 JSON 时最可能留下的形状；
# 直接写孤立的代理码点连夹具自己都写不出去。
d2 = mk()
with open(mm.path_of(d2), "w", encoding="utf-8") as f:
    json.dump({"title": "手" + HI + "改", "nodes": [{"id": "a", "text": "x", "children": []}]}, f)
chk("夹具：磁盘上写下的确是转义后的半个代理码点",
    "\\ud800" in open(mm.path_of(d2), encoding="utf-8").read(),
    open(mm.path_of(d2), encoding="utf-8").read())
loaded = mm.load_map(d2)
chk("load_map：手改文件里的坏字符读进来就被洗掉（不抛）", loaded["title"] == "手改",
    repr(loaded["title"]))
chk("load_map 后再存回去：ok 且不留 .tmp",
    mm.save_map(d2, loaded).get("ok") is True
    and not any(x.endswith(".tmp") for x in names(d2)), names(d2))

# ── 4. _write_json：写崩时抛出去，但 .tmp 收干净 ─────────────────────
t = mk()
raised = False
try:
    mm._write_json(os.path.join(t, "mindmap.json"), "坏" + HI)
except UnicodeEncodeError:
    raised = True
chk("_write_json：写不进 utf-8 时抛出去，但 .tmp 收干净了",
    raised and not os.path.exists(os.path.join(t, "mindmap.json.tmp")), names(t))

# ── 5. 二次保存的 .prev 存档行为没被这次修改动过 ─────────────────────
d3 = mk()
mm.save_map(d3, {"title": "一", "nodes": [{"id": "a", "text": "x", "children": []}]})
mm.save_map(d3, {"title": "二", "nodes": [{"id": "a", "text": "y", "children": []}]})
chk("存档：覆盖保存仍留一份 .prev", os.path.exists(os.path.join(d3, "mindmap.json.prev")),
    names(d3))
chk("存档：.prev 不是 .json，不会被当成本图重复读", mm.load_map(d3)["title"] == "二",
    mm.load_map(d3)["title"])

for tmpdir in (d, d2, d3, t):
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("思维导图存盘收尾门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
