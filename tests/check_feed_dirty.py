# -*- coding: utf-8 -*-
"""订阅账本（feed.json）脏形状的离线验收：坏的是账本，不该把整屏订阅带走。

feed.json 不只 feed.py 一个进程在写：用户会手工编辑、旧版本写下过别的形状、云同步
也可能落下一份半截的。feed._load 原来只 `setdefault` 了 feeds / entries / seen 三个
键名，**值坏掉就原样带出去**，于是：

  A `entries` 被写成列表 / 字符串：`_normalize` 里的 `.values()` 直接抛 AttributeError，
    而且这条异常在 _load 外面，连空账都回不去 —— 界面「订阅」整页打不开。
  B `feeds` 被写成对象 / 字符串：后面逐条 `f.get(...)` 同样炸。
  C `seen` 被写成列表：按源写回已读标记时 `dict[key]` 撞上列表索引，照样抛。
  D 源行没有 id：`entries()` 整页 KeyError（谁也点不到、也没有条目能对上）。
  E `settings.groups` 被写成字符串：`list("abc")` 拆成 a / b / c 三个假夹子。

这份用例把上面五条连同两条底线一起钉住：

  F 救得回来的照留：一份合法源 + 合法条目混在垃圾行里，读数仍认得出来、字段补齐。
  G 干净的账本一个字不改：加固不许把好数据洗坏。

全程离线、不开浏览器、不写用户真实数据目录（GUIZANG_DATA 指向临时沙盒）。
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就定下 STORE 的路径。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-dirty-")
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FID = "a" * 12
FAIL = []
PASSED = 0


def chk(name, ok, extra=""):
    global PASSED
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + repr(extra)))
    if ok:
        PASSED += 1
    else:
        FAIL.append(name)


def write_store(doc):
    """把任意一棵对象（含坏形状）原样写进 feed.json。"""
    with open(feed.STORE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def no_raise(name, fn, *args, **kw):
    """公开读接口不许因为脏账本抛异常；返回 (是否没抛, 结果或异常)。"""
    try:
        return True, fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


try:
    # ── A entries 值坏掉 ────────────────────────────────────────────
    for tag, val in (("A1 entries 写成列表", [1, 2]),
                     ("A2 entries 写成字符串", "坏账"),
                     ("A3 entries 写成数字", 7)):
        write_store({"feeds": [], "entries": val, "seen": {}, "settings": {}})
        ok, data = no_raise(tag, feed._load)
        chk(tag + "：_load 不抛、把 entries 收成空对象",
            ok and isinstance(data.get("entries"), dict) and data["entries"] == {}, data)
        for fn_name in ("subs", "entries", "summary", "tag_list", "settings"):
            ook, res = no_raise(tag + " → " + fn_name, getattr(feed, fn_name))
            chk("%s：%s() 不抛" % (tag, fn_name), ook, res)

    # ── B feeds 值坏掉 ─────────────────────────────────────────────
    for tag, val in (("B1 feeds 写成对象", {"x": 1}),
                     ("B2 feeds 写成字符串", "ab")):
        write_store({"feeds": val, "entries": {}, "seen": {}, "settings": {}})
        ok, data = no_raise(tag, feed._load)
        chk(tag + "：_load 不抛、把 feeds 收成空列表",
            ok and data.get("feeds") == [], data)
        ook, res = no_raise(tag + " → subs", feed.subs)
        chk(tag + "：subs() 不抛且回空表", ook and res == [], res)

    # ── C seen 值坏掉 ──────────────────────────────────────────────
    write_store({"feeds": [{"id": FID, "title": "t"}], "entries": {},
                 "seen": [1, 2], "settings": {}})
    ok, data = no_raise("C1 seen 写成列表", feed._load)
    chk("C1 seen 写成列表：_load 不抛、收成空对象",
        ok and data.get("seen") == {}, data)
    ook, res = no_raise("C1 seen 写成列表 → mark", feed.mark_read, [])
    chk("C1 seen 写成列表：标已读不抛", ook, res)

    # ── D 源行没有 id ──────────────────────────────────────────────
    write_store({"feeds": [{"title": "没有 id"}, 5, "x", {"id": FID, "title": "好的"}],
                 "entries": {FID: [{"id": "e1", "title": "ok"}]},
                 "seen": {FID: {"e1": 1}}, "settings": {}})
    ok, data = no_raise("D1 源行缺 id", feed._load)
    ids = [f.get("id") for f in data.get("feeds", [])]
    chk("D1 源行缺 id / 非对象：只留下带 id 的那条", ok and ids == [FID], data)
    for fn_name in ("subs", "entries", "summary", "count_entries"):
        ook, res = no_raise("D1 → " + fn_name, getattr(feed, fn_name))
        chk("D1 源行缺 id：%s() 不抛" % fn_name, ook, res)

    # ── E settings.groups 被写成字符串 ─────────────────────────────
    write_store({"feeds": [], "entries": {}, "seen": {},
                 "settings": {"groups": "abc", "fulltext": True}})
    ok, data = no_raise("E1 groups 写成字符串", feed._load)
    chk("E1 groups 写成字符串：收成空列表、不再拆出假夹子",
        ok and data["settings"]["groups"] == [], data)
    ook, res = no_raise("E1 → groups", feed.groups)
    chk("E1 groups 写成字符串：groups() 不抛",
        ook and [g["group"] for g in res if g.get("group")] == [], res)
    chk("E1 同一份 settings 里的 fulltext 仍照读", data["settings"]["fulltext"] is True, data)

    # 顶层整个不是对象（旧版本 / 同步截断）
    write_store([1, 2, 3])
    ok, data = no_raise("E2 顶层写成列表", feed._load)
    chk("E2 顶层写成列表：_load 不抛、四样都回到空形状",
        ok and data.get("feeds") == [] and data.get("entries") == {}
        and data.get("seen") == {} and isinstance(data.get("settings"), dict), data)

    # ── F 救得回来的照留 ───────────────────────────────────────────
    write_store({
        "feeds": [{"id": FID, "title": "真源", "url": "http://x.test/f.xml"},
                  {"title": "缺 id 的垃圾行"}, 42],
        "entries": {FID: [{"id": "e1", "title": "真条目", "read": True},
                          "坏条目", 9],
                    "orphan": {"not": "a list"}},
        "seen": {FID: {"e1": 123}, "orphan": [1]},
        "settings": {"policy": {"days": 7}},
    })
    data = feed._load()
    chk("F1 合法源留下来了（字段补齐）",
        len(data["feeds"]) == 1 and data["feeds"][0]["id"] == FID
        and data["feeds"][0]["kind"] == "rss", data["feeds"])
    chk("F2 合法条目留下来了，坏条目丢掉",
        [e["id"] for e in data["entries"].get(FID, [])] == ["e1"], data["entries"])
    chk("F3 合法条目的缺字段补齐（read 照原值、starred 补默认）",
        data["entries"][FID][0]["read"] is True
        and data["entries"][FID][0]["starred"] is False, data["entries"])
    chk("F4 值不是列表的 entries 分格丢掉", "orphan" not in data["entries"], data["entries"])
    chk("F5 值不是对象的 seen 分格丢掉", data["seen"] == {FID: {"e1": 123}}, data["seen"])
    s = feed.summary()
    chk("F6 读数认得出来：summary 源 1 / 条 1 / 已读 1",
        s["feeds"] == 1 and s["entries"] == 1 and s["unread"] == 0, s)
    chk("F7 policy 里的已知键照叠（days=7）", s["policy"]["days"] == 7, s["policy"])

    # ── G 干净的账本一个字不改 ─────────────────────────────────────
    clean = {
        "feeds": [{"id": FID, "url": "http://x.test/f.xml", "site": "http://x.test",
                   "title": "订阅", "kind": "rss", "group": "组", "folder": "组",
                   "order": 0, "error": "", "etag": "", "modified": "",
                   "last_fetched": 111}],
        "entries": {FID: [{"id": "e1", "title": "条", "link": "http://x.test/1",
                           "published_ts": 222, "read": False, "starred": True,
                           "later": 1, "tags": ["标签"], "content_md": "正文",
                           "content_html": "<p>正文</p>", "summary": "", "pushed": ""}]},
        "seen": {FID: {"e1": 333}},
        "settings": {"fulltext": True,
                     "policy": {"days": 0, "max_entries": 0, "keep_starred": True},
                     "groups": ["组"]},
    }
    write_store(clean)
    data = feed._load()
    chk("G1 源行原样留着", data["feeds"][0] == feed._norm_feed(dict(clean["feeds"][0])),
        data["feeds"][0])
    chk("G2 条目原样留着（标签 / 收藏 / 稍后读都不丢）",
        data["entries"][FID][0]["tags"] == ["标签"]
        and data["entries"][FID][0]["starred"] is True
        and data["entries"][FID][0]["later"] == 1, data["entries"])
    chk("G3 seen 原样留着", data["seen"] == {FID: {"e1": 333}}, data["seen"])
    chk("G4 settings 原样留着", data["settings"]["groups"] == ["组"]
        and data["settings"]["fulltext"] is True, data["settings"])

    # 洗过之后写回去，再读一次仍是同一个形状（幂等，不会越洗越少）
    feed.set_settings(fulltext=True)
    again = feed._load()
    chk("G5 脏账洗过之后经公开写路径落盘，再读仍是同一形状",
        again["feeds"] == data["feeds"] and again["entries"] == data["entries"]
        and again["seen"] == data["seen"], again)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("订阅账本脏形状：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  坏形状收成空账、合法数据照留，公开读接口一个都不抛")
