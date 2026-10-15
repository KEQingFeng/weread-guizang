# -*- coding: utf-8 -*-
"""订阅条目时间字段的**越界数字**门禁：一条 Infinity 不许把整页订阅带走。

为什么单开这一份（而不是并进 check_feed_ts.py）
    上一份管的是「时间字段被写成字符串」（`"1759914000"` 混在整数里让排序
    `'<' not supported`）。这里管的是同一把尺子的另一半：`json` 认
    `Infinity` / `-Infinity` / `NaN`，也认 `1e400` 与一长串数字，`1e400` 解析
    成 `inf`。**它们是合法 JSON**，会原样落进 `feed.json`，再被读回来。

    `feed._as_ts` 的数字那一支写的是 `int(v) if v > 0 else 0`：`nan > 0` 为假
    走 0，可 `int(inf)` 抛的是 OverflowError。`_norm_entry` → `_normalize` 是在
    `_load` 的 `json.load` **try 之外**跑的，于是：

      · 任一 `published_ts: Infinity`
      · 任一 `fetched_at: Infinity`
      · 任一 `later: Infinity`（`later` 存的是标记那一刻的 epoch，`_entry_view`
        里同样有一处 `int(later)`）

    都会让 `entries()` / `subs()` / `summary()` 整条抛 OverflowError —— 界面
    「订阅」那一屏整页打不开，而且连空账都回不去（异常在 `_load` 外面）。

这份用例把「越界值不抛、坏值回 0、**认得出的值一点不许改口径**、坏一条不许带走
整页」钉住。全程离线：GUIZANG_DATA 指向临时沙盒，手写一份带 `Infinity` 字面量的
feed.json 后只调纯逻辑，不联网、不起服务、不碰用户真实的 cache/。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 STORE 定下来了。
SANDBOX = tempfile.mkdtemp(prefix="guizang-feedoverflow-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAILED = []
COUNT = 0
INF = float("inf")


def chk(name, cond, extra=""):
    global COUNT
    COUNT += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)[:200]))
    if not cond:
        FAILED.append(name)


def write_store(entries, feeds=None):
    """把沙盒里的 feed.json 写成「手工改坏」的样子。

    逐字节手写而不是 json.dumps：要的就是**磁盘上真有一行 `Infinity`**，
    证明这条输入是 `json.load` 收得下的合法 JSON，而不是测试自己造出来的对象。
    """
    feed_row = (feeds if feeds is not None else
                [{"id": "f1", "url": "http://x.test/feed.xml", "title": "源",
                  "group": "", "order": 0}])
    doc = {"feeds": feed_row, "entries": {"f1": entries}, "seen": {}, "settings": {}}
    # json.dumps 把 float('inf') 写成裸的 `Infinity`（Python 默认允许），磁盘上
    # 因此真的有一行 `"published_ts": Infinity` —— 正是 json.load 收得下的那种。
    with open(feed.STORE, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False))
    return doc


def read_all():
    """把三条产品级读路径都走一遍，返回 (是否没抛, 结果或异常人话)。"""
    try:
        return True, {
            "entries": feed.entries(limit=0),
            "subs": feed.subs(),
            "summary": feed.summary(),
        }
    except Exception as e:                                    # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


# ── 0. 前提：这组越界值真的是 json 解析得出来的 ──────────────────────
chk("J1 Infinity 是合法 JSON，解析成 inf",
    json.loads('{"v": Infinity}')["v"] == INF)
chk("J2 1e400 解析成 inf", json.loads("1e400") == INF)
try:
    int(INF)
    raised = False
except OverflowError:
    raised = True
chk("J3 int(inf) 抛的正是 OverflowError（漏接它整份账本读不出来）", raised)

# ── 1. 尺子本身：越界值不抛，回安全默认 ─────────────────────────────
_safe = True
for v in (INF, -INF, float("nan"), "Infinity", "1e400"):
    try:
        got = feed._as_ts(v)
    except Exception as e:                                    # noqa: BLE001
        got, _safe = "%s: %s" % (type(e).__name__, e), False
    if got != 0:
        _safe = False
chk("U1 _as_ts 对 inf / -inf / nan / 'Infinity' / '1e400' 一律回 0 不抛", _safe)

chk("U2 认得出的值口径一点不许改：整数 / 数字串 / 日期串照认",
    feed._as_ts(1759914000) == 1759914000
    and feed._as_ts("1759914000") == 1759914000
    and feed._as_ts("2025-10-07T00:00:00Z") > 0,
    (feed._as_ts(1759914000), feed._as_ts("1759914000"),
     feed._as_ts("2025-10-07T00:00:00Z")))

# 超大整数是「认得出的整数」：int() 装得下就该照认（与 flomo._as_int 同一口径）
chk("U3 超大整数的口径分得清：inf 归 0，10**400 照认",
    feed._as_ts(10 ** 400) == 10 ** 400 and feed._as_ts(INF) == 0,
    feed._as_ts(10 ** 400))

# ── 2. 账本：越界时间字段不许把整页带走 ────────────────────────────
write_store([
    {"id": "ok1", "title": "好的一条", "published_ts": 1759914000,
     "fetched_at": 1759914000, "later": 1759913000, "extra_note": "别丢"},
    {"id": "bad1", "title": "发布越界", "published_ts": INF},
    {"id": "bad2", "title": "抓取越界", "fetched_at": INF},
    {"id": "bad3", "title": "稍后读越界", "later": INF},
])
ok, got = read_all()
chk("L1 一条 published_ts/fetched_at/later 是 Infinity，三条读路径都不抛", ok, got)
rows = got["entries"] if ok else []
by_id = {r.get("id"): r for r in rows}
chk("L2 坏的是值不是条目：四条一条都不丢", ok and len(rows) == 4, [r.get("id") for r in rows])
chk("L3 坏值一律归 0，且落成 int",
    ok and all(isinstance(by_id[i]["published_ts"], int)
               and isinstance(by_id[i]["fetched_at"], int)
               and isinstance(by_id[i]["later_at"], int)
               for i in by_id if i in by_id),
    {i: (by_id[i]["published_ts"], by_id[i]["later_at"]) for i in by_id})
chk("L4 好的一条字段一点没动（发布时间 / 稍后读时刻都是原值）",
    ok and by_id.get("ok1", {}).get("published_ts") == 1759914000
    and by_id.get("ok1", {}).get("later_at") == 1759913000,
    by_id.get("ok1"))
chk("L5 越界归 0 后照常排得动序：有时间的在前，坏的三条沉到最后",
    ok and [r["id"] for r in rows] == ["ok1", "bad1", "bad2", "bad3"],
    [r["id"] for r in rows])

# ── 3. 稍后读字段的其他脏写法：字符串 / 越界串都不许打抛 ──────────────
write_store([
    {"id": "s1", "title": "稍后读是脏串", "later": "不是时间"},
    {"id": "s2", "title": "稍后读是数字串", "later": "1759914000"},
    {"id": "s3", "title": "稍后读越界", "later": "1e400"},
])
ok, got = read_all()
rows = got["entries"] if ok else []
by_id = {r.get("id"): r for r in rows}
chk("S1 later 写脏（非时间串 / 数字串 / 越界串）三条读路径都不抛", ok, got)
chk("S2 later 的数字串照认、认不出的归 0，且一律 int",
    ok and by_id.get("s2", {}).get("later_at") == 1759914000
    and by_id.get("s1", {}).get("later_at") == 0
    and by_id.get("s3", {}).get("later_at") == 0
    and all(isinstance(v.get("later_at"), int) for v in by_id.values()),
    {i: by_id[i].get("later_at") for i in by_id})

# ── 4. 干净账本一个字不改（加固不许把好数据洗坏）────────────────────
write_store([
    {"id": "g1", "title": "新", "published_ts": 1759914000, "fetched_at": 1759914000,
     "later": 1759913000, "read": True, "tags": ["a"], "custom": {"x": 1}},
    {"id": "g2", "title": "旧", "published_ts": 1759910000, "fetched_at": 1759910000},
])
ok, got = read_all()
rows = got["entries"] if ok else []
chk("R1 干净账本：读得动、排序仍是新的在前",
    ok and [r["id"] for r in rows] == ["g1", "g2"], [r.get("id") for r in rows])
chk("R2 干净账本：时间 / 已读 / 标签一个不改",
    ok and rows[0]["published_ts"] == 1759914000 and rows[0]["later_at"] == 1759913000
    and rows[0]["read"] is True and rows[0]["tags"] == ["a"],
    rows[0] if rows else None)

# 归一化多加了一道（later 也过尺子），必须证明它只碰时间、别的字段原样写回。
feed.mark_read(["g1"])
with open(feed.STORE, encoding="utf-8") as f:
    back = json.load(f)["entries"]["f1"][0]
chk("R3 写回后未知字段与自定义结构一个不丢",
    back.get("custom") == {"x": 1} and back.get("tags") == ["a"]
    and back.get("read") is True, back)

# ── 5. 线上那一手：写回一次盘，Infinity 不再被原样带下去 ─────────────
write_store([{"id": "w1", "title": "越界", "published_ts": INF, "fetched_at": INF}])
feed.mark_read(["w1"])
with open(feed.STORE, encoding="utf-8") as f:
    saved = json.load(f)["entries"]["f1"][0]
chk("W1 落盘后时间字段是整数，磁盘上再没有 Infinity",
    isinstance(saved.get("published_ts"), int) and isinstance(saved.get("fetched_at"), int)
    and saved.get("published_ts") == 0 and saved.get("fetched_at") == 0, saved)

print()
print("订阅时间字段越界数字门禁：%d 项通过，失败 %d 项" % (COUNT - len(FAILED), len(FAILED)))
for name in FAILED:
    print("  ✗ " + name)
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAILED else 0)
