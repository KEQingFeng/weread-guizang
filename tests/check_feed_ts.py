# -*- coding: utf-8 -*-
"""订阅条目时间字段的脏值门禁：一个字符串不许把整页条目带走。

为什么要单开一份：feed.json 是用户手改得动的文本，老版本或手工编辑会把条目的
`published_ts` / `fetched_at` 写成字符串（"1759914000" 或 ISO 日期），而列表里
只要有一条是数字、另一条是字符串，`entries()` 的排序就是
`'<' not supported between instances of 'str' and 'int'`；`entries(since=…)`、
`mark_read_all(older_than=…)`、`prune(days=…)` 这些拿时间比大小的路径也一起 500。
它不报「哪一条坏了」，只把整页订阅打成异常 —— 所以这一条把「时间字段入库即整数」
钉死，同时确认纯数字串不会被误当成日期解成 0（那等于把有发布时间的条目降级成没有）。

全程离线：GUIZANG_DATA 指向临时沙盒，手写一份脏 feed.json 后只调纯逻辑，
不联网、不起服务、不碰用户真实的 cache/。
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
SANDBOX = tempfile.mkdtemp(prefix="guizang-feedts-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAILED = []
COUNT = 0


def chk(name, cond, extra=""):
    global COUNT
    COUNT += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAILED.append(name)


def write_store(entries, feeds=None):
    """把沙盒里的 feed.json 写成「手工改坏」的样子。"""
    dirty = {
        "feeds": feeds if feeds is not None else [
            {"id": "f1", "url": "http://x.test/feed.xml", "title": "源", "group": "",
             "order": 0},
        ],
        "entries": {"f1": entries},
        "seen": {},
        "settings": {},
    }
    with open(feed.STORE, "w", encoding="utf-8") as f:
        json.dump(dirty, f, ensure_ascii=False)
    return dirty


# ── 1. _as_ts：一把尺子认数字 / 数字串 / 日期串，认不出的算 0 ──────────
chk("_as_ts：整数原样", feed._as_ts(1759914000) == 1759914000, feed._as_ts(1759914000))
chk("_as_ts：纯数字串按 epoch 认（不是解成 0）",
    feed._as_ts("1759914000") == 1759914000, feed._as_ts("1759914000"))
chk("_as_ts：带空白的数字串也认",
    feed._as_ts(" 1759914000 ") == 1759914000, feed._as_ts(" 1759914000 "))
chk("_as_ts：ISO 日期串照旧",
    feed._as_ts("2025-10-07T00:00:00Z") > 0, feed._as_ts("2025-10-07T00:00:00Z"))
chk("_as_ts：RFC 822 照旧",
    feed._as_ts("Tue, 07 Oct 2025 10:00:00 GMT") > 0,
    feed._as_ts("Tue, 07 Oct 2025 10:00:00 GMT"))
chk("_as_ts：认不出的串算 0",
    feed._as_ts("不是时间") == 0 and feed._as_ts("nan") == 0 and feed._as_ts("") == 0,
    (feed._as_ts("不是时间"), feed._as_ts("nan")))
chk("_as_ts：布尔不当时间", feed._as_ts(True) == 0 and feed._as_ts(False) == 0, None)
chk("_as_ts：负数不算时间", feed._as_ts(-5) == 0 and feed._as_ts("-5") == 0, None)

# ── 2. 脏账本：字符串时间混在数字里，排序 / 过滤 / 标记 / 裁剪都不许抛 ──
write_store([
    {"id": "e1", "title": "数字串时间", "published_ts": "1759914000",
     "fetched_at": "1759914000", "extra_note": "别丢"},
    {"id": "e2", "title": "整数时间", "published_ts": 1759910000, "fetched_at": 1759910000},
    {"id": "e3", "title": "ISO 发布时间", "published": "2025-10-07T00:00:00Z"},
    {"id": "e4", "title": "时间认不出", "fetched_at": "不是时间"},
])

try:
    rows = feed.entries(limit=0)
    order_ok = [r["id"] for r in rows] == ["e1", "e2", "e3", "e4"]
except Exception as e:                                    # noqa: BLE001
    rows, order_ok = [], False
    print("  entries 抛了：" + type(e).__name__ + " " + str(e))
chk("混合类型的时间：entries 排得动序、最新的在前", order_ok,
    [r.get("id") for r in rows])

ts_all_int = all(isinstance(r["published_ts"], int) and isinstance(r["fetched_at"], int)
                 for r in rows) if rows else False
chk("视图里的 published_ts / fetched_at 一律是 int", ts_all_int,
    [(r["id"], type(r["published_ts"]).__name__, type(r["fetched_at"]).__name__)
     for r in rows])

chk("数字串时间没有被解成 0",
    rows and rows[0]["published_ts"] == 1759914000, rows[0] if rows else None)
chk("ISO 发布时间仍补得出来", any(r["id"] == "e3" and r["published_ts"] > 0 for r in rows),
    [r for r in rows if r["id"] == "e3"])
chk("认不出的时间当 0，不把这条丢掉",
    any(r["id"] == "e4" and r["published_ts"] == 0 for r in rows), rows)

try:
    since_hit = [r["id"] for r in feed.entries(since=1759912000, limit=0)]
except Exception as e:                                    # noqa: BLE001
    since_hit = ["抛了：%s" % type(e).__name__]
chk("entries(since=…)：只回过线的，且不抛", since_hit == ["e1"], since_hit)

try:
    marked = feed.mark_read_all(older_than=1759912000, undo=False)
    marked_n = marked["n"] if isinstance(marked, dict) else marked
except Exception as e:                                    # noqa: BLE001
    marked_n = "抛了：%s" % type(e).__name__
chk("mark_read_all(older_than=…)：按时间标已读不抛且数目对", marked_n == 3, marked_n)

write_store([
    {"id": "e1", "title": "数字串时间", "published_ts": "1759914000", "fetched_at": "1759914000"},
    {"id": "e2", "title": "整数时间", "published_ts": 1, "fetched_at": 1},
])
try:
    pruned = feed.prune(days=7, dry_run=True)
    pruned_n = pruned["removed"] if isinstance(pruned, dict) else pruned
except Exception as e:                                    # noqa: BLE001
    pruned_n = "抛了：%s" % type(e).__name__
chk("prune(days=…)：按时间裁旧条目不抛", pruned_n == 2, pruned_n)

# ── 3. 归一化只改时间，不动别的字段 ────────────────────────────────
write_store([
    {"id": "e1", "title": "数字串时间", "published_ts": "1759914000",
     "fetched_at": "1759914000", "extra_note": "别丢", "legacy_flag": 7},
])
feed.mark_read(["e1"])
with open(feed.STORE, encoding="utf-8") as f:
    raw = json.load(f)
saved = raw["entries"]["f1"][0]
chk("写回后未知字段一个不丢",
    saved.get("extra_note") == "别丢" and saved.get("legacy_flag") == 7, saved)
chk("写回后时间字段落成整数",
    isinstance(saved.get("published_ts"), int) and saved["published_ts"] == 1759914000,
    saved)

print()
print("订阅时间字段脏值门禁：%d 项通过，失败 %d 项" % (COUNT - len(FAILED), len(FAILED)))
for name in FAILED:
    print("  ✗ " + name)
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAILED else 0)
