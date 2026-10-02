# -*- coding: utf-8 -*-
"""续传锚点的取证：卡住之后再点一次取书，必须能接着往前读，而不是回到同一个死点。

为什么单独一份：实测《你身体里的奥秘》卡在 84/223 —— main() 一遇到「本轮没有写出
新章节」就直接收工，而续传位置又是阅读器自己的「上次读到」；它停在一个再也翻不出
内容的地方（方向键按 12 次一个字都抓不到），重跑照样落在同一点，于是永久卡死。

修法：一轮 0 章之后，改用目录把落点钉到**最后一个已写出项的下一项**（见
resume_catalog_index），再用 jump_to_catalog_item 跳过去。

这里只验不依赖浏览器的那部分判据：
  A resume_catalog_index 的语义（前沿 = 最后写出项的下一项，取不到时该收尾）
  B 目录顺序照旧（load_catalog_ordered 不排序，下标才对得上 DOM）
  C 接口还在（run_session 收了 resume_at；jump_to_first_item 仍委托给通用版）
"""
import inspect
import io
import json
import os
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


TMP = tempfile.mkdtemp(prefix="guizang-resume-")


def book(items, written_idx, with_catalog=True):
    """造一本「目录 N 项、其中若干项已写出」的书，返回 (catalog_path, md_dir)。"""
    d = tempfile.mkdtemp(dir=TMP)
    cat = os.path.join(d, "_catalog.json")
    if with_catalog:
        io.open(cat, "w", encoding="utf-8").write(json.dumps(items, ensure_ascii=False))
    md = os.path.join(d, "chapters")
    os.makedirs(md)
    for i in written_idx:
        # 正文里的标题是干净的；「当前读到 xx%」只挂在目录项上，不落到章文件里。
        io.open(os.path.join(md, "%04d.md" % (i + 1)), "w", encoding="utf-8").write(
            "# " + items[i].split("当前读到")[0].strip() + "\n\n正文\n")
    return cat, md


# ── A. 前沿语义 ──────────────────────────────────────────
ITEMS = ["版权信息", "文前", "序言", "一 思维模式", "二 情绪健康", "结语"]

cat, md = book(ITEMS, [])
chk("一章都没写出时从目录第一项开始", E.resume_catalog_index(cat, md) == 0)

cat, md = book(ITEMS, [0, 1])
chk("写出前两项 → 续第 3 项（下标 2）", E.resume_catalog_index(cat, md) == 2)

# 缺口散在中间（《你身体里的奥秘》的实况）：不能回第一个缺项重读，
# 只能从最后一个写出项之后接着读，否则已写出的几章会被整段重出来。
cat, md = book(ITEMS, [0, 2, 4])
chk("中间有缺口时不回填，仍取最后写出项的下一项",
    E.resume_catalog_index(cat, md) == 5, E.resume_catalog_index(cat, md))

cat, md = book(ITEMS, list(range(len(ITEMS))))
chk("目录末项都写出来了 → 返回 None（该收尾，别再往下读）",
    E.resume_catalog_index(cat, md) is None)

cat, md = book(ITEMS, [0], with_catalog=False)
chk("没有目录文件时返回 None（没有位置可以钉）",
    E.resume_catalog_index(cat, md) is None)

# 「当前读到 42%」是目录项自带的进度尾巴，不能参与下标计算
cat, md = book(["版权信息 当前读到 3%", "序言 当前读到 9%", "结语"], [0])
chk("目录项的「当前读到 xx%」被剥掉再算",
    E.resume_catalog_index(cat, md) == 1)

# ── B. 目录顺序 ──────────────────────────────────────────
cat, md = book(["短", "很长很长很长的一个标题啊"], [])
ordered = E.load_catalog_ordered(cat)
chk("load_catalog_ordered 保持目录原序（不按长度排）",
    [o for _n, o in ordered] == ["短", "很长很长很长的一个标题啊"], ordered)
by_len = E.load_catalog_titles(cat)
chk("load_catalog_titles 仍按长度降序（分章匹配要这个）",
    by_len and by_len[0][1] == "很长很长很长的一个标题啊", by_len)

# ── C. 接口 ──────────────────────────────────────────────
sig = inspect.signature(E.run_session)
chk("run_session 收了 resume_at（续传重定位的入口）", "resume_at" in sig.parameters,
    list(sig.parameters))
chk("jump_to_catalog_item 是协程", inspect.iscoroutinefunction(E.jump_to_catalog_item))
chk("jump_to_first_item 也还在（老调用点不用改）",
    inspect.iscoroutinefunction(E.jump_to_first_item))
src = inspect.getsource(E.jump_to_first_item)
chk("jump_to_first_item 委托给通用版，只有一份实现",
    "jump_to_catalog_item(" in src, src)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("续传锚点取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
