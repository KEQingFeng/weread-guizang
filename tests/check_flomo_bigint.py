# -*- coding: utf-8 -*-
"""flomo 账本里「认得出但越界」的字数：一长串数字不许把记忆画像整页打不开。

背景（改之前实测）
    .venv/bin/python -c "import flomo_notes as F; d=F.load(D); F.build_portrait(d)"
      -> OverflowError: integer division result too large for a float

    `words` 这一格在 `_clean_memo` 里走的是 `_as_int`，它认得 `10**400` 这种
    合法 JSON 大整数（`json.load` 按 Python 大整数原样读回），于是一个手改过 /
    云同步回来 / 旧版本落下的越界字数就穿过了读口。真正炸的地方在记忆画像：
    `round(总字数 / 条数, 1)` 先做一次大整数转 float，`10**400` 转 float 当场
    OverflowError —— 画像那页（JSON 与 Markdown 两条路）整页打不开。

    这不是它第一次犯：隔壁 activity 的色阶（`int(hi * 0.25)`）与 web_parse 的推文
    编号（`int(tid) / 1e15`）是同一种病，两处都已收口。这里收在**读口**：
    `words` 是「这条正文有多少字」，正文字数由 `words_of` 唯一决定；认得出但对不上
    正文的（负数 / 大得不可能）跟「认不出」一视同仁，按正文重算。

这一条钉四面，全程离线（纯函数 + 临时目录，不起服务、不碰 HTTP，归 --fast）：
  1. 越界 / 负数的 words 读回来按正文重算，正常值一个字不动，条目不丢；
  2. 下游（stats / 画像 / Markdown / 落盘）全都不抛，画像里不再有越界数字；
  3. 对照证明「把脏值直接喂给 build_portrait」确实会抛 OverflowError，
     而旧算式 `round(10**400 / 1, 1)` 也确实抛 —— 免得门禁空转；
  4. 边界：MAX_WORDS 之内的值原样留着（收口只挡明显不可能是真的那些）。

跑法：.venv/bin/python tests/check_flomo_bigint.py
"""
import json
import math
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import flomo_notes as fn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


ROOT = tempfile.mkdtemp(prefix="gz-flomo-bigint-")
FN = os.path.join(ROOT, "flomo")          # 相当于 ui_server 里的 cache/flomo/


def put(memos, **top):
    os.makedirs(FN, exist_ok=True)
    d = {"memos": memos}
    d.update(top)
    with open(fn.path_in(FN), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


HUGE = 10 ** 400

# ── A：读口收口 —— 越界按正文重算 / 正常值不动 / 条目不丢 ────────
DIRTY = [
    {"id": "fm_huge", "date": "2026-10-01", "clock": "09:00:00", "ts": 1,
     "md": "#读书 这一条的字数是越界的大整数", "tags": ["读书"], "words": HUGE},
    {"id": "fm_neg", "date": "2026-10-02", "clock": "10:00:00", "ts": 2,
     "md": "负数的字数同样接不住", "tags": ["灵感"], "words": -5},
    {"id": "fm_num", "date": "2026-10-03", "clock": "11:00:00", "ts": 3,
     "md": "数字串要照认", "tags": [], "words": "42"},
]
put(DIRTY, at=0, src="")
d = fn.load(FN)

chk("A1 越界字数读回来按正文重算（不是 10**400，也不是 0）",
    d["memos"][0]["words"] == fn.words_of(d["memos"][0]["md"])
    and 0 < d["memos"][0]["words"] < fn.MAX_WORDS,
    d["memos"][0]["words"])
chk("A2 负数字数同样按正文重算",
    d["memos"][1]["words"] == fn.words_of(d["memos"][1]["md"]) and d["memos"][1]["words"] > 0,
    d["memos"][1]["words"])
chk("A3 正常值一个字不动（\"42\" 仍是 42）", d["memos"][2]["words"] == 42,
    d["memos"][2]["words"])
chk("A4 越界值不把整条丢掉（三条都在）", len(d["memos"]) == 3, len(d["memos"]))
chk("A5 边界：MAX_WORDS 之内的数原样留着（收口只挡明显不可能是真的）",
    fn._clean_memo({"id": "x", "md": "a", "words": fn.MAX_WORDS})["words"] == fn.MAX_WORDS
    and fn._clean_memo({"id": "x", "md": "a", "words": fn.MAX_WORDS + 1})["words"]
    == fn.words_of("a"),
    (fn._clean_memo({"id": "x", "md": "a", "words": fn.MAX_WORDS})["words"],
     fn._clean_memo({"id": "x", "md": "a", "words": fn.MAX_WORDS + 1})["words"]))

# ── B：下游全都不抛，画像里不再有越界数字 ────────────────────────
stats = fn.summary_stats(d)
chk("B1 骨架统计不抛、总字数仍是整数", isinstance(stats["words"], int), stats["words"])

p = fn.build_portrait(d)
chk("B2 记忆画像不抛，平均字数是个有限的数",
    isinstance(p["avg_words"], (int, float)) and math.isfinite(p["avg_words"]),
    p["avg_words"])
chk("B3 画像里的总字数不可能再越界（它由重算后的每条字数加出来）",
    p["words"] == sum(m["words"] for m in d["memos"]), (p["words"], stats["words"]))

pm = fn.portrait_markdown(p)
chk("B4 画像 Markdown 生成得出来（%.1f 不再撞越界整数）",
    pm.startswith("# 我的记忆画像") and "平均每天" in pm, pm[:60])
chk("B5 画像能被 json.dumps 收下（回包那一步不会炸）",
    json.loads(json.dumps(p, ensure_ascii=False))["avg_words"] == p["avg_words"])

wrote = fn.write_portrait(FN, d)
chk("B6 画像落盘两条路都成功（portrait.json / portrait.md）",
    os.path.isfile(wrote["json"]) and os.path.isfile(wrote["md"]), wrote)
landed = json.load(open(wrote["json"], encoding="utf-8"))
chk("B7 落盘的画像里平均字数不再是越界值",
    math.isfinite(landed["avg_words"]) and landed["avg_words"] == p["avg_words"],
    landed.get("avg_words"))

pub = fn.public(d["memos"][0])
chk("B8 交给界面的那一份字数仍是整数", isinstance(pub["words"], int), pub["words"])

# ── C：对照 —— 证明这道门不是空的 ────────────────────────────────
raw = {"memos": [{"id": "fm_huge", "md": "正文", "words": HUGE}], "at": 0, "src": ""}
try:
    fn.build_portrait(raw)
    c1 = False
except OverflowError:
    c1 = True
chk("C1 对照：把越界字数直接喂给 build_portrait 确实抛 OverflowError（所以读口要收）", c1)

try:
    round(HUGE / 1, 1)
    c2 = False
except OverflowError:
    c2 = True
chk("C2 对照：旧算式 round(大整数 / 1, 1) 确实抛（这就是画像打不开的那一句）", c2)

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("flomo 越界字数：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  一长串数字不再把记忆画像整页打不开")
