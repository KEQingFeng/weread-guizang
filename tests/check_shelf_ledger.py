# -*- coding: utf-8 -*-
"""书架账本 library.json 的脏形状兜底 —— 一个字段脏不该让整份书架 500。

`ui_server.load_lib()` 是 `/api/state` 的必经之路，而它读的 `cache/library.json`
是会被手工编辑、会被云同步从别的机器落盘、旧版本也写过别样形状的一份账：

  · 老格式（1.0.1 以前）把 folders / assign / order / state / tags 摊在顶层，
    读进来时按书号折进各模块那一格。可这五样的形状没人保证 ——
    `"assign": ["书号"]`、`"state": ["x"]`、`"order": 5`、`"folders": 5`
    都是合法 JSON，旧代码直接 `.items()` / 直接迭代，一个脏字段就抛
    AttributeError / TypeError，整份书架清单 500，用户看到的是「我的书全没了」。
  · `modules.<格>.folders` 只被验过「是不是列表」，列表里每个元素没人验：
    元素是数字、缺 id、id 不是字符串时，新建 / 改名 / 删除文件夹那几个接口
    会 `f["id"]` 直接抛，甚至 `{x.get("id") for x in ...}` 撞上不可哈希的值。

这一条把上面两类脏形状逐样喂进 load_lib，要求：不抛、账本仍然可用、
**同文件里合法的那部分数据不许被脏字段连带丢掉**，并顺手回归一次老账本折格子。
全在系统临时目录的沙盒里跑，不联网、不起服务、不开浏览器。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-shelf-ledger-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_layout                                            # noqa: E402
import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


LIB = ui_server.LIB_PATH


def write_lib(payload):
    os.makedirs(os.path.dirname(LIB), exist_ok=True)
    with open(LIB, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


# ── A：老格式摊在顶层，五样各自脏一遍 ─────────────────────────────
# 每一项单独写一次盘：一个脏字段不该让 load_lib 抛，也不该让别的格读不出来。
DIRTY_LEGACY = [
    ("assign 是列表", {"assign": ["clip_post"]}),
    ("assign 是字符串", {"assign": "clip_post"}),
    ("state 是列表", {"state": ["clip_post"]}),
    ("tags 是列表", {"tags": ["公众号"]}),
    ("order 是数字", {"order": 5}),
    ("folders 是数字", {"folders": 5}),
    ("folders 元素是数字", {"folders": [1, 2]}),
]
for label, payload in DIRTY_LEGACY:
    write_lib(payload)
    try:
        lib = ui_server.load_lib()
    except Exception as exc:                                  # noqa: BLE001
        chk("A 顶层 %s：load_lib 不抛" % label, False, "%s: %s" % (type(exc).__name__, exc))
        continue
    chk("A 顶层 %s：load_lib 不抛且七格齐" % label,
        set(lib.get("modules") or {}) == set(book_layout.ORDER))
    # 折格子那一步之后，每一格的形状都得是能直接用的
    s = ui_server.lib_scope(lib, "clip")
    chk("A 顶层 %s：折完仍是有序账（assign/order/tags 形状对）" % label,
        isinstance(s["assign"], dict) and isinstance(s["order"], list) and
        isinstance(s["tags"], dict) and isinstance(s["folders"], list))

# 脏字段旁边合法的那部分必须留着：顶层 assign 里混着一条好的，不能一起丢
write_lib({"assign": {"clip_post": "f1", "imp_mine": 5, "": "f2"},
           "state": {"clip_post": "在读", "imp_mine": ["坏"]},
           "tags": {"clip_post": ["公众号"], "imp_mine": "坏"},
           "order": ["clip_post", 7, None, ""]})
lib = ui_server.load_lib()
cs = ui_server.lib_scope(lib, "clip")
chk("A 脏 assign 里认得出的留下、认不出的丢",
    cs["assign"] == {"clip_post": "f1"}, cs["assign"])
chk("A 脏 state / tags 里认得出的留下",
    cs["state"] == {"clip_post": "在读"} and cs["tags"] == {"clip_post": ["公众号"]},
    (cs["state"], cs["tags"]))
chk("A 脏 order 里只有书号字符串留下",
    cs["order"] == ["clip_post"], cs["order"])

# 顶层脏 + modules 合法同时存在：modules 那份数据一个字不许动
write_lib({"modules": {"clip": {"assign": {"clip_post": "f9"}, "order": ["clip_post"],
                                "folders": [{"id": "f9", "name": "行业观察"}]}},
           "assign": ["坏"], "order": 5})
lib = ui_server.load_lib()
cs = ui_server.lib_scope(lib, "clip")
chk("A 顶层脏不连带丢掉 modules 里合法的账",
    cs["assign"] == {"clip_post": "f9"} and cs["order"] == ["clip_post"] and
    [f["name"] for f in cs["folders"]] == ["行业观察"], (cs["assign"], cs["order"]))

# ── B：modules 里 folders 的元素形状 ─────────────────────────────
# folders 是列表但元素是脏的：数字 / 缺 id / id 不是字符串。这些元素会让
# 「新建 / 改名 / 删除文件夹」那三个接口的 `f["id"]` 与集合推导当场抛。
write_lib({"modules": {"clip": {"folders": [1, {"name": "没 id"}, {"id": 9},
                                             {"id": "good", "name": "行业观察"}],
                                "assign": {"clip_post": "good"}}}})
lib = ui_server.load_lib()
cs = ui_server.lib_scope(lib, "clip")
chk("B 脏 folders 元素被摘掉，认得出的夹子留住",
    [f["name"] for f in cs["folders"]] == ["行业观察"], cs["folders"])

# 模拟那三个接口里按 id 查夹子的几行：形状收干净之后必须一行都不抛
try:
    for s in (lib.get("modules") or {}).values():
        any(f["id"] == "f9" for f in s["folders"])            # folder.rename 前置检查
        [f for f in s["folders"] if f["id"] != "f9"]          # folder.drop
        any(f["id"] == "good" for f in s["folders"])          # book.move 前置检查
    chk("B 收拾过的 folders 能直接喂给文件夹接口那几行", True)
except Exception as exc:                                      # noqa: BLE001
    chk("B 收拾过的 folders 能直接喂给文件夹接口那几行", False,
        "%s: %s" % (type(exc).__name__, exc))

# prune_lib 也读这一份：脏账本裁幽灵时不许抛，好夹子留着
try:
    gone = ui_server.prune_lib(ui_server.load_lib())
    gp = ui_server.lib_scope(gone, "clip")
    chk("B 脏账本过一遍 prune_lib 不抛，好夹子留住",
        any(f["id"] == "good" for f in gp["folders"]))
except Exception as exc:                                      # noqa: BLE001
    chk("B 脏账本过一遍 prune_lib 不抛，好夹子留住", False,
        "%s: %s" % (type(exc).__name__, exc))

# ── C：回归 —— 一份干净的老账本还是照旧折对格子 ───────────────────
write_lib({"folders": [{"id": "f1", "name": "随笔"}],
           "assign": {"clip_post": "f1", "36000000000000000b000c01": "f1"},
           "order": ["clip_post"],
           "state": {"clip_post": "在读"},
           "tags": {"clip_post": ["公众号"]}})
lib = ui_server.load_lib()
chk("C 干净老账本按书号折进各自那格",
    ui_server.lib_scope(lib, "clip")["assign"] == {"clip_post": "f1"} and
    ui_server.lib_scope(lib, "weread")["assign"] == {"36000000000000000b000c01": "f1"})
chk("C 标签与状态跟着书走",
    ui_server.lib_scope(lib, "clip")["tags"] == {"clip_post": ["公众号"]} and
    ui_server.lib_scope(lib, "clip")["state"] == {"clip_post": "在读"})
chk("C 被引用的夹子抄给引用方，顶层不再留副本",
    any(f["id"] == "f1" for f in ui_server.lib_scope(lib, "clip")["folders"]) and
    not any(k in lib for k in ui_server.LIB_KEYS))

# 脏账本落盘一次再读回来：仍是能用的形状（幂等），不会把坏东西留成永久状态
write_lib({"assign": ["坏"], "order": 5, "folders": [1, {"id": "f1"}]})
ui_server.save_lib(ui_server.load_lib())
again = ui_server.load_lib()
chk("C 脏账本存回去再读一遍仍是能用的形状",
    set(again.get("modules") or {}) == set(book_layout.ORDER) and
    any(f["id"] == "f1" for f in ui_server.lib_scope(again, "weread")["folders"]))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("书架账本脏形状：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  书架账本脏形状全部收干净，不抛、不连带丢数据、老账本照旧")
