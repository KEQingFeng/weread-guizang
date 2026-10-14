# -*- coding: utf-8 -*-
"""目录账本与「钉不住」记录的脏形状收口。

为什么单独一份：取书主循环每一轮开头都要读这两份账本 ——
  _catalog.json  → load_catalog_ordered / load_catalog_titles / load_last_catalog_title
  _stuck.json    → load_stuck → stuck_skip / stuck_bump

它们是 export_precise 自己写出去的，但用户会手工编辑、旧版本也落过别的形状。
旧读法只兜住了 json.load 那一下，读出来之后「是什么形状」一概不问：

  * `[1, 2]`   → re.split 拿到 int，TypeError —— 整趟取书带着「读取目录」这句话垮掉
  * `null` / `5` → for 一个不可迭代对象，同样是 TypeError
  * `"目录"` / `{…}` → 不抛，却把单个字符 / 字典 key 当成目录项，续传锚点钉到错位置
  * `_stuck.json` 是列表 / 字符串 / 数字 → `data.get(...)` 抛 AttributeError
  * `_stuck.json` 的 strikes 是列表 → `.items()` 抛 AttributeError

口径与仓库其它账本一致：**认不出的当空账，能认的一个字不改**。目录项按原序保留、
只丢掉不是字符串的那几项；strikes 只收键值都转得成 int 的条目。
这一条把脏形状、好数据、以及「旧读法确实会抛」的对照一起钉住。纯离线、临时目录。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import export_precise as E  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-catalog-")


def put(name, obj):
    """把 obj 按 JSON 落盘（obj 是 {'raw': '...'} 时直接写原字节，方便造非 JSON）。"""
    p = os.path.join(TMP, name)
    with io.open(p, "w", encoding="utf-8") as f:
        if isinstance(obj, dict) and set(obj) == {"raw"}:
            f.write(obj["raw"])
        else:
            json.dump(obj, f, ensure_ascii=False)
    return p


def read_all(catalog_path):
    """四个目录读口各跑一遍，返回 (titles, ordered, last, first, resume)。"""
    return (E.load_catalog_titles(catalog_path),
            E.load_catalog_ordered(catalog_path),
            E.load_last_catalog_title(catalog_path),
            E.first_catalog_title(catalog_path),
            E.resume_catalog_index(catalog_path, os.path.join(TMP, "no-md")))


# ── A. 脏目录形状：四个读口都不许抛，一律当「没目录」 ──────────────────
DIRTY = {
    "整数列表": [1, 2, 3],
    "null": None,
    "数字": 5,
    "字符串": "目录标题",
    "字典": {"0": "第一章"},
    "嵌套列表": [["第一章"]],
    "布尔": True,
}
for label, shape in DIRTY.items():
    p = put("dirty-%s.json" % label, shape)
    try:
        titles, ordered, last, first, resume = read_all(p)
        ok = titles == [] and ordered == [] and last == "" and first == "" and resume is None
        chk("脏目录（%s）：四个读口不抛且当没目录" % label, ok,
            (titles, ordered, last, first, resume))
    except Exception as e:  # noqa: BLE001 —— 抛了就是这条门禁要抓的回归
        chk("脏目录（%s）：四个读口不抛且当没目录" % label, False,
            "%s: %s" % (type(e).__name__, e))

# 半截档（不是合法 JSON）也要回空，而不是抛
p = put("truncated.json", {"raw": '["第一章", "第二'})
try:
    titles, ordered, last, first, resume = read_all(p)
    chk("半截目录（非法 JSON）：四个读口不抛且当没目录",
        titles == [] and ordered == [] and last == "" and first == "" and resume is None,
        (titles, ordered, last, first, resume))
except Exception as e:  # noqa: BLE001
    chk("半截目录（非法 JSON）：四个读口不抛且当没目录", False, "%s: %s" % (type(e).__name__, e))

# ── B. 好数据 / 半脏数据：认得出的照认，顺序不动 ──────────────────────
GOOD = ["版权信息", "第一章 起点 当前读到 12%", "   ", "第二章 展开与深入", "结语"]
p = put("good.json", GOOD)
titles = E.load_catalog_titles(p)
ordered = E.load_catalog_ordered(p)
chk("好目录：load_catalog_ordered 保持原序、丢掉空标题",
    [t for _n, t in ordered] == ["版权信息", "第一章 起点", "第二章 展开与深入", "结语"],
    ordered)
chk("好目录：仍按标题长度降序（长标题优先匹配）",
    [len(n) for n, _t in titles] == sorted([len(n) for n, _t in titles], reverse=True),
    titles)
chk("好目录：首项 / 末项照旧（「当前读到」尾巴摘掉）",
    E.first_catalog_title(p) == "版权信息"
    and E.load_last_catalog_title(p) == "结语",
    (E.first_catalog_title(p), E.load_last_catalog_title(p)))

MIXED = ["第一章 起点", 7, None, "第二章 展开与深入", "   ", True, "第三章 收束"]
p = put("mixed.json", MIXED)
chk("半脏目录：不是字符串的项丢掉，字符串项原序保留",
    [t for _n, t in E.load_catalog_ordered(p)] == ["第一章 起点", "第二章 展开与深入", "第三章 收束"],
    E.load_catalog_ordered(p))
chk("半脏目录：首项跳过脏项取第一个字符串",
    E.first_catalog_title(p) == "第一章 起点", E.first_catalog_title(p))
chk("半脏目录：末项取最后一个字符串（不是 true）",
    E.load_last_catalog_title(p) == "第三章 收束", E.load_last_catalog_title(p))

# 续传锚点：脏项被丢掉之后，第一个没写出来的字符串项仍能正确定位
d = tempfile.mkdtemp(dir=TMP)
md = os.path.join(d, "chapters")
os.makedirs(md)
with io.open(os.path.join(md, "0001.md"), "w", encoding="utf-8") as f:
    f.write("# 第一章 起点\n\n正文\n")
cat = put("resume.json", ["第一章 起点", 7, "第二章 展开与深入", "第三章 收束"])
chk("半脏目录：续传锚点钉到第一个还没写出来的字符串项（第二章 → 下标 1）",
    E.resume_catalog_index(cat, md) == 1, E.resume_catalog_index(cat, md))

# ── C. 对照：同一份字节喂给旧读法确实会抛（防门禁空转） ───────────────
raised = False
try:
    re.split(r"当前读到", json.loads("[1, 2, 3]")[0])
except TypeError:
    raised = True
chk("对照：整数列表喂给旧读法（re.split 直吃）确实抛 TypeError", raised)

raised = False
try:
    for _t in json.loads("null"):
        pass
except TypeError:
    raised = True
chk("对照：null 喂给旧读法（直接 for）确实抛 TypeError", raised)

# ── D. _stuck.json 脏形状：当没记录，stuck_bump 还能自愈重记 ──────────
for label, shape in {"列表": [1, 2], "字符串": "x", "null": None,
                     "数字": 3, "字典但 strikes 是列表": {"catalog": 3, "strikes": [1, 2]}}.items():
    p = put("stuck-bad.json", shape)
    try:
        chk("脏 _stuck（%s）：load_stuck / stuck_skip 当没记录" % label,
            E.load_stuck(p, 3) == {} and E.stuck_skip(p, 3) == set(),
            (E.load_stuck(p, 3), E.stuck_skip(p, 3)))
    except Exception as e:  # noqa: BLE001
        chk("脏 _stuck（%s）：load_stuck / stuck_skip 当没记录" % label, False,
            "%s: %s" % (type(e).__name__, e))

p = put("stuck-bump.json", [1, 2])
chk("脏 _stuck：stuck_bump 第一轮不当场判跳过（只记一笔）",
    E.stuck_bump(p, 3, 7) is False and E.stuck_skip(p, 3) == set())
chk("脏 _stuck：第二轮攒够才跳过，且记录已自愈成好形状",
    E.stuck_bump(p, 3, 7) is True and E.stuck_skip(p, 3) == {7}
    and isinstance(json.loads(io.open(p, encoding="utf-8").read()).get("strikes"), dict))

# strikes 里逐条坏值只跳过该条，能认的照认
p = put("stuck-fields.json", {"catalog": 3,
                              "strikes": {"0": 1, "x": 2, "2": "oops", "3": 2.9, "4": None, "5": True}})
chk("strikes 坏键值只跳过该条：0→1、3→2、5→1，其余丢掉",
    E.load_stuck(p, 3) == {0: 1, 3: 2, 5: 1}, E.load_stuck(p, 3))

p = put("stuck-mismatch.json", {"catalog": 4, "strikes": {"0": 2}})
chk("strikes 记的目录项数对不上：整份作废（目录重 dump 过，下标全变了）",
    E.load_stuck(p, 3) == {} and E.stuck_skip(p, 3) == set())

p = put("stuck-list-ok.json", {"catalog": 3, "strikes": {"1": 2}})
chk("好 strikes：照旧认得出来（stuck_skip 取到 2 次的那项）",
    E.stuck_skip(p, 3) == {1}, E.stuck_skip(p, 3))

# ── E. 接线：所有目录读口都过同一把尺子 ─────────────────────────────
src = io.open(os.path.join(ROOT, "export_precise.py"), encoding="utf-8").read()
chk("接线：四个目录读口都走 _catalog_items（不再各自 json.load）",
    src.count("_catalog_items(catalog_path)") >= 4, src.count("_catalog_items(catalog_path)"))
chk("接线：load_stuck 对 data / strikes 各有一道 isinstance 闸",
    "isinstance(data, dict)" in src and "isinstance(strikes, dict)" in src)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("目录账本脏形状取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
