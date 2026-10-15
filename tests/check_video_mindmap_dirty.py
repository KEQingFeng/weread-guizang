# -*- coding: utf-8 -*-
"""视频思维导图：一个坏字段不许带走整张图（离线验收）。

导图的输入是「别人家的 JSON」——模型回话、手改过的导图档。节点里本该是数组的
`children` 偶尔会写成标量（`"children": 1`，或整个对象）。原来的
`for c in (item.get("children") or [])` 在 int 上当场抛 TypeError：

  A 扁平节点表（`nodes` 是列表、`children` 存 id）走 `_normalize_nodes`：一个节点的
    坏 children 把**整个列表**带崩。
  B 嵌套树（根直接给 `children` 对象数组）走 `_flatten_tree`：一个叶子的坏 children
    把整棵树的摊平带崩。

两条都会穿出 `_normalize_mindmap`，被 `build_mindmap` 当成「模型两次都不配合」——
用户看到的是「导图没生成成」，落下来的是摘要兜底那张，可模型给的六个分支其实就
在手里。这一份把「坏的那个字段当空、好的那棵树照用」钉住：

  A1 `_normalize_nodes` 顶层给非列表不抛，当空表。
  A2 某个节点的 `children` 给标量不抛，节点照留、children 当空。
  A3 正常的 id 列表一个字不改；空白项仍按老规矩丢掉。
  B1 `_flatten_tree` 某个叶子的 `children` 给标量不抛，标签一个不少。
  C1 `_normalize_mindmap` 两条路（扁平 / 嵌套）都能在坏 children 下给出可用图。
  D1 `build_mindmap` 遇到这种回话**不再退成兜底**（warn 为空、一级分支够）。
  D2 好数据一个字不改：正常回话仍是 warn 为空、三个一级分支。

全程离线、不联网、不开浏览器，也不碰用户真实书库（数据目录指进临时沙盒）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 先指进临时沙盒再 import video_note：免得 TMP_ROOT 真去动用户真实缓存。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-vidmap-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import video_note as vn  # noqa: E402

PASSED = 0
FAILED = []


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))


def call(fn, *a, **kw):
    """跑一次；抛了就把异常文本还回来，好让失败信息可读。"""
    try:
        return fn(*a, **kw), None
    except Exception as e:  # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


SUMMARY = {"overview": "一句话总览", "bulletPoints": ["要点一", "要点二", "要点三"],
           "chapters": [{"title": "第一章", "start": 0, "summary": "一"},
                        {"title": "第二章", "start": 30, "summary": "二"},
                        {"title": "第三章", "start": 60, "summary": "三"}]}
CFG = {"url": "https://example.invalid/v1", "key": "k", "model": "fake-model"}


# ── A. _normalize_nodes：顶层 / 单个字段的脏形状都不许抛 ───────────────
for bad in (5, 1.5, True, "nodes", {"id": "x"}, None):
    got, err = call(vn._normalize_nodes, bad)
    chk("A1 _normalize_nodes(%r)：非列表当空表、不抛" % (bad,), err is None and got == [],
        err if err else got)

DIRTY_ITEM = [{"id": "a", "label": "甲", "children": 1},
              {"id": "b", "label": "乙", "children": {"x": 1}},
              {"id": "c", "label": "丙", "children": "xyz"}]
got, err = call(vn._normalize_nodes, DIRTY_ITEM)
chk("A2 节点 children 给标量：不抛、三个节点都在", err is None and len(got or []) == 3,
    err if err else got)
chk("A2b 坏 children 一律当空数组（不是把字符串拆成字符）",
    err is None and [n["children"] for n in (got or [])] == [[], [], []],
    err if err else [n["children"] for n in (got or [])])

GOOD_ITEM = [{"id": "r", "label": "根", "type": "root", "children": ["t1", "", None, "t2"]},
             {"id": "t1", "label": "一"}, {"id": "t2", "label": "二"},
             7, {"label": "缺 id"}]
got, err = call(vn._normalize_nodes, GOOD_ITEM)
chk("A3 好数据不改：id 列表原样、空白项与非字典项照旧丢掉",
    err is None and [n["id"] for n in (got or [])] == ["r", "t1", "t2"]
    and got[0]["children"] == ["t1", "t2"],
    err if err else got)

# ── B. _flatten_tree：嵌套树里一个叶子的坏 children 不许带崩整棵 ─────────
NESTED_DIRTY = {"label": "总", "children": [
    {"label": "概念", "children": [{"label": "概念一"}, {"label": "概念二", "children": 1}]},
    {"label": "方法", "children": [{"label": "方法一"}]},
    {"label": "结论", "children": 7},
]}
flat, err = call(vn._flatten_tree, NESTED_DIRTY, "示例视频总结")
labels = [n["label"] for n in (flat or [])]
chk("B1 嵌套树里 children 给标量：不抛、标签一个不少",
    err is None and set(labels) == {"总", "概念", "概念一", "概念二", "方法", "方法一", "结论"},
    err if err else labels)
chk("B1b 一级仍是三个，坏 children 的那个当叶子",
    err is None and vn._mindmap_top_branches(
        {"root": next(n["id"] for n in flat if n["type"] == "root"), "nodes": flat}) == 3,
    err if err else flat)

# ── C. _normalize_mindmap：两条路都能在坏 children 下给出可用图 ─────────
FLAT_DIRTY = {"version": 1, "title": "示例视频总结", "root": "root", "nodes": [
    {"id": "root", "label": "总", "type": "root", "children": ["t1", "t2", "t3"]},
    {"id": "t1", "label": "概念", "type": "theme", "children": 1},
    {"id": "t2", "label": "方法", "type": "theme", "children": []},
    {"id": "t3", "label": "结论", "type": "theme", "children": []}]}
mm, err = call(vn._normalize_mindmap, FLAT_DIRTY, "示例视频总结", SUMMARY)
chk("C1 扁平路：坏 children 不抛、一级分支仍是 3",
    err is None and vn._mindmap_top_branches(mm) == 3, err if err else mm)

NESTED_PAYLOAD = {"version": 1, "title": "示例视频总结", "root": NESTED_DIRTY}
mm2, err = call(vn._normalize_mindmap, NESTED_PAYLOAD, "示例视频总结", SUMMARY)
chk("C1b 嵌套路：坏 children 不抛、一级分支仍是 3",
    err is None and vn._mindmap_top_branches(mm2) == 3, err if err else mm2)


# ── D. build_mindmap：这种回话不该退成兜底，好数据一个字不改 ────────────
def llm_returning(payload):
    text = json.dumps(payload, ensure_ascii=False)
    return lambda cfg, messages, timeout=180: text


vn.llm_chat = llm_returning(NESTED_PAYLOAD)
dirty_map, err = call(vn.build_mindmap, CFG, "示例视频总结", SUMMARY, "笔记正文")
chk("D1 坏 children 的回话被照用，不再退成摘要兜底",
    err is None and isinstance(dirty_map, tuple) and dirty_map[1] == ""
    and vn._mindmap_top_branches(dirty_map[0]) == 3,
    err if err else dirty_map)

CLEAN_PAYLOAD = {"version": 1, "title": "示例视频总结", "root": "root", "nodes": [
    {"id": "root", "label": "总", "type": "root", "children": ["t1", "t2", "t3"]},
    {"id": "t1", "label": "概念", "type": "theme", "children": []},
    {"id": "t2", "label": "方法", "type": "theme", "children": []},
    {"id": "t3", "label": "结论", "type": "theme", "children": []}]}
vn.llm_chat = llm_returning(CLEAN_PAYLOAD)
good_map, err = call(vn.build_mindmap, CFG, "示例视频总结", SUMMARY, "笔记正文")
chk("D2 好数据不改：正常回话仍是 warn 为空、一级分支 3",
    err is None and isinstance(good_map, tuple) and good_map[1] == ""
    and vn._mindmap_top_branches(good_map[0]) == 3,
    err if err else good_map)

print()
print("视频导图脏字段：通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
if FAILED:
    for f in FAILED:
        print("  ✗ " + f)
    shutil.rmtree(str(SANDBOX), ignore_errors=True)
    sys.exit(1)
shutil.rmtree(str(SANDBOX), ignore_errors=True)
print("ok  一个坏 children 不再把整张模型导图判失败、退成摘要兜底")
