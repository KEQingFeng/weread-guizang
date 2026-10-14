# -*- coding: utf-8 -*-
"""订阅这条线落单的半个代理码点（U+D800–U+DFFF）的离线验收。

半个代理码点不是一个「奇怪的字符」，是一个 utf-8 编不出来的坏值：JSON 里写
`"\\ud800"` 能被 json.loads 收成这个码点，可 `.encode("utf-8")` 与
`json.dump(..., ensure_ascii=False)` 碰到它当场 UnicodeEncodeError。订阅这一格
有两条路会撞上它，两条都是真实可复现的：

  A 外站的 JSON Feed 在 title / id 里带这种转义。解析（json.loads）不抛，等到算
    去重键 `_entry_key`（退回 sha1(link+title) 那一步 `.encode("utf-8")`）或入库算
    content_hash 时才炸 —— 整趟刷新带着这个源一起垮，用户看到「刷新失败」。
  B feed.json 被手改 / 云同步落盘 / 旧版本写过，值里（或 seen 表的**键**里）留着
    它。之后任何一次写盘（标已读 / 打标签 / 删除 / 加源）都在 ensure_ascii=False
    的 json.dump 上抛，界面看到「点了没反应」，cache 里还留一个半截 feed.json.tmp。

这一份把两条路连同三条底线钉住：

  C 只摘落单的那半个：真 emoji（😀）与 ZWJ 连字（🧑‍💻）一个字不动，洗完是幂等的。
  D 干净的账本一个字不改：加固不许把好数据洗坏，公开读接口照常。
  E 洗过之后经公开写路径落盘，再读仍是同一形状，且不留 .tmp。

全程离线、不联网、不开浏览器、不写用户真实数据目录（GUIZANG_DATA 指向临时沙盒）。
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就定下 STORE 的路径。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-surrogate-")
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FID = "a" * 12
EID = "e" * 12
HI = "\ud800"                       # 落单的高位代理码点
LO = "\udfff"                       # 落单的低位代理码点
HALF = "\ud83d"                     # 一个 emoji 被栏腰截断后剩下的那半个
EMOJI = "\U0001F600"                # 😀 真字符，必须原样留着
ZWJ = "\U0001F9D1\u200D\U0001F4BB"  # 🧑‍💻 ZWJ 连字，必须原样留着

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
    """把对象写进 feed.json。

    ensure_ascii=True（默认）是关键：落单的代理码点会被写成 `\\ud800` 转义，写得进去，
    读出来仍是那个坏码点 —— 正是「手改过 / 云同步落下」的那份账本的形状。
    """
    with open(feed.STORE, "w", encoding="utf-8") as f:
        json.dump(doc, f)          # 故意不写 ensure_ascii=False


def no_raise(name, fn, *args, **kw):
    """公开读接口不许因为脏账本抛异常；返回 (是否没抛, 结果或异常)。"""
    try:
        return True, fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def has_surrogate(v):
    """整棵树（含字典键）里还有没有落单的代理码点。"""
    if isinstance(v, str):
        return any(0xD800 <= ord(c) <= 0xDFFF for c in v)
    if isinstance(v, dict):
        return any(has_surrogate(k) or has_surrogate(x) for k, x in v.items())
    if isinstance(v, list):
        return any(has_surrogate(x) for x in v)
    return False


def dirty_doc():
    """一份手改/云同步能落下的脏账本：值与 seen 的**键**里都夹着半个码点。"""
    return {
        "feeds": [{"id": FID + HI, "url": "https://e/x", "title": "源" + HI + "名",
                   "site": "站" + HALF, "group": "组" + LO, "folder": "组" + LO}],
        "entries": {FID + HI: [{"id": EID + HI, "title": "标" + HALF + "题",
                                "link": "https://e/1", "summary": "摘" + LO + "要",
                                "content_html": "<p>正" + HALF + "文</p>",
                                "content_md": "", "tags": ["标" + HI + "签"],
                                "published_ts": 1, "read": False, "starred": False,
                                "later": 0, "pushed": ""}]},
        "seen": {FID + HI: {EID + HI: 1}},
        "settings": {"groups": ["夹" + LO + "子"]},
    }


def store_text():
    with open(feed.STORE, encoding="utf-8") as f:
        return f.read()


def tmp_left():
    return [p for p in os.listdir(os.environ["GUIZANG_DATA"]) if p.endswith(".tmp")]


try:
    # ── A 读口：脏账本里的值与键都洗干净，公开读接口一个都不抛 ──────────
    write_store(dirty_doc())
    ok, data = no_raise("A _load", feed._load)
    chk("A1 脏账本 _load 不抛", ok, data)
    chk("A2 读出来整棵树（含键）不再有落单的代理码点", not has_surrogate(data), data)
    chk("A3 源 id 洗干净（键与值用同一把尺子，entries/seen 对得上）",
        data["feeds"][0]["id"] == FID and FID in data["entries"] and FID in data["seen"],
        data["feeds"][0]["id"])
    chk("A4 条目 id 洗干净、值里的半个码点摘掉（'源'+'名' 直接接上）",
        data["entries"][FID][0]["id"] == EID
        and data["feeds"][0]["title"] == "源名"
        and data["entries"][FID][0]["title"] == "标题", data["entries"][FID][0])
    chk("A5 标签 / seen 键 / 设置组名都洗干净",
        data["entries"][FID][0]["tags"] == ["标签"]
        and data["seen"] == {FID: {EID: 1}}
        and data["settings"]["groups"] == ["夹子"], data["settings"])

    for fn_name, fn, kw in (("subs", feed.subs, {}),
                            ("entries", feed.entries, {}),
                            ("summary", feed.summary, {}),
                            ("tag_list", feed.tag_list, {}),
                            ("settings", feed.settings, {})):
        ok2, got = no_raise(fn_name, fn, **kw)
        chk("A6 %s() 不抛且回干净数据" % fn_name, ok2 and not has_surrogate(got), got)

    # ── B 写口：公开写路径照常落盘、不留半截 .tmp ────────────────────
    ok3, res = no_raise("mark_read_all", feed.mark_read_all, feed_id=FID, undo=False)
    chk("B1 脏账本上「整源标已读」不抛且真的改了", ok3 and res.get("n") == 1, res)
    text = store_text()
    chk("B2 落盘的 feed.json 里没有落单码点的转义", "\\ud" not in text.lower(), text[:200])
    chk("B3 落盘的那份 json 仍读得回来且已干净",
        not has_surrogate(json.loads(text)), text[:200])
    chk("B4 写盘成功不留 .tmp", tmp_left() == [], tmp_left())

    # _save 是最后一道闸：数据没经过 _load（例如抓完外站直接并进内存）也要兜住
    ok4, _ = no_raise("_save", feed._save,
                      {"feeds": [], "entries": {}, "seen": {}, "settings": {},
                       "note": "x" + HI + "y"})
    chk("B5 _save 直接收脏数据也不抛", ok4)
    chk("B6 _save 落盘的内容已洗干净、无 .tmp",
        "\\ud" not in store_text().lower() and tmp_left() == [],
        (store_text()[:120], tmp_left()))

    # ── C 外站 JSON Feed：解析出来就已经干净，算去重键不再炸 ──────────
    blob = ('{"title":"\\ud800源","items":['
            '{"title":"A \\ud83d B","url":"https://e/1","id":"i\\ud800d"}]}').encode("utf-8")
    ok5, pf = no_raise("parse_feed", feed.parse_feed, blob)
    chk("C1 带落单码点的 JSON Feed 解析得出来", ok5, pf)
    chk("C2 解析结果整棵树（含 id）已干净", ok5 and not has_surrogate(pf), pf)
    if ok5:
        item = pf["entries"][0]
        chk("C3 条目 id 里的半个码点摘掉（'i'+'d' 接上）", item["id"] == "id", item["id"])
        ok6, key = no_raise("_entry_key", feed._entry_key, item)
        chk("C4 算去重键不再 UnicodeEncodeError、键里也没有坏码点",
            ok6 and key and not has_surrogate(key), key)

    # 对照：不洗的话，同一个 id 直接 .encode("utf-8") 确实会抛 —— 这一条证明输入
    # 里真的带着坏码点，用例不是空转。
    raw = json.loads(blob.decode("utf-8"))
    try:
        raw["items"][0]["id"].encode("utf-8")
        legacy_raises = False
    except UnicodeEncodeError:
        legacy_raises = True
    chk("C5 对照：旧读法拿同一个 id 去 encode 确实抛 UnicodeEncodeError", legacy_raises)

    # ── D 只摘坏的那半个：真 emoji / ZWJ 连字一个字不动 ──────────────
    clean = {"t": EMOJI + ZWJ, "list": [EMOJI, "顶" + HI + "点"], "n": 7,
             ZWJ: {"深层": [EMOJI + HALF]}}
    washed = feed._drop_surrogates(clean)
    chk("D1 成对的 emoji 与 ZWJ 连字原样留着",
        washed["t"] == EMOJI + ZWJ and washed["list"][0] == EMOJI, washed)
    chk("D2 半个码点摘掉、其余字符不动", washed["list"][1] == "顶点"
        and washed["n"] == 7, washed)
    chk("D3 字典键也洗（ZWJ 键原样、深层项洗掉半个）",
        ZWJ in washed and washed[ZWJ]["深层"] == [EMOJI], washed)
    chk("D4 幂等：洗过再洗结果不变", feed._drop_surrogates(washed) == washed)

    # 干净账本经公开写路径落盘，再读仍是同一形状（好数据不被洗坏）
    clean_doc = {
        "feeds": [{"id": FID, "url": "https://e/x", "title": "书" + EMOJI, "site": "",
                   "group": "组", "folder": "组"}],
        "entries": {FID: [{"id": EID, "title": "标" + ZWJ, "link": "https://e/1",
                           "summary": "要", "content_html": "<p>x</p>",
                           "content_md": "", "tags": ["标" + EMOJI],
                           "published_ts": 1, "read": False, "starred": True,
                           "later": 0, "pushed": ""}]},
        "seen": {FID: {EID: 3}},
        "settings": {"groups": ["夹" + EMOJI], "fulltext": True},
    }
    write_store(clean_doc)
    before = feed._load()
    chk("D5 干净账本里的 emoji / ZWJ / 收藏都不动",
        before["feeds"][0]["title"] == "书" + EMOJI
        and before["entries"][FID][0]["title"] == "标" + ZWJ
        and before["entries"][FID][0]["starred"] is True
        and before["settings"]["groups"] == ["夹" + EMOJI], before)
    feed.set_settings(fulltext=False)
    again = feed._load()
    chk("D6 经公开写路径落盘再读，仍是同一形状",
        again["feeds"] == before["feeds"] and again["entries"] == before["entries"]
        and again["settings"]["groups"] == before["settings"]["groups"], again)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("订阅落单代理码点：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  外站 JSON Feed 与脏账本里的半个码点不再把刷新 / 写盘打崩，真 emoji 一个字不动")
