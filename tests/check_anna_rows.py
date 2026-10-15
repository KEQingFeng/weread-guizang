#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安娜账本 `books` / `pending` 行里的坏数字不许把 `/api/state` 整份打废。

为什么单开一条
    `anna_state.normalize()` 立下的契约是「把任意一份读出来的表洗成字段齐全的状态，
    脏值退回默认，不抛异常」—— 顶层字段（domain / window / 计数 / seen / note）都按
    这把尺子收口了，可 `books` / `pending` 里的**每一条**原先是「是对象就原样收下」：

        out.append(r)          # 旧写法：行里的字段一个都不量

    而 `cache/anna.json` 不只是本进程在写（用户会手工编辑、云同步会落盘、旧版本写回），
    行里的 `bytes` / `at` / `chapters` 被改成越界数字时，`json` 读回来就是 inf：

        {"books": [{"name": "x.epub", "bytes": 1e400, "at": 1e400}]}

实测的故障（改之前）
    这一份状态整份进 `/api/state`，而界面的轮询是每 2.6 秒一次。Python 的
    `json.dumps` 默认 `allow_nan=True`，照写不误：

        HTTP 200  {"...": "...", "books": [{"bytes": Infinity, "at": Infinity}]}

    `Infinity` 不是 JSON 规范里的值，浏览器 `JSON.parse` 碰到一个就**整份拒收** ——
    用户看到的不是「安娜那一栏的记录显示不对」，而是整个界面什么都读不出来
    （书架、任务、设置一起没）。同一个回包 MCP 适配器那一侧 `r.json()` 也一样死。

收口
    行里认得的那几个字段与顶层计数走同一把 `_as_int`（能认的照认、越界与形状不对
    归 0），文本走 `_note`（压空白、摘落单代理码点、夹长度）；整条最后再过一遍
    `_finite`（非有限浮点一律 0），手改落到**不认识的那个键**上的坏数字也一起收，
    而不认识的键本身照旧保留（与便签账那条口径一致）。

这条钉三面 —— 纯函数洗出来的东西能写成严格 JSON、源码里不再原样直收、真机上
`/api/state` 回 200 + 严格 JSON；另留一面「旧写法确实会写出 Infinity」的对照，
防门禁空转。跑法：纯函数 + 源码走查 + 一个进程内真 Handler 的裸 HTTP，自带临时
沙盒，不联网、不开浏览器、不起外部服务。归 --fast 静态段。
"""
import atexit
import http.server
import json
import math
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带一个沙盒，绝不碰用户真实的 cache/ 与书库，也不和别的套件共用那份共享沙盒。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-anna-rows-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"

import anna_state  # noqa: E402
import ui_server    # noqa: E402  —— 必须在环境变量设好之后 import

SRC = (REPO / "anna_state.py").read_text(encoding="utf-8")

checks = []


def _safe(s):
    """打印用：坏字符（落单代理码点）不许把门禁自己带崩，转义着显示出来。"""
    return str(s).encode("utf-8", "backslashreplace").decode("utf-8", "replace")[:300]


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + _safe(extra)))


def strict_json(text):
    """严格 JSON 解析：`Infinity` / `NaN` 这类 Python 放行、浏览器拒收的值当场判失败。"""
    def boom(const):
        raise ValueError("回包里出现 JSON 规范不收的 " + const)
    return json.loads(text, parse_constant=boom)


def no_bad_numbers(v, path="$"):
    """递归找非有限浮点；找到就回一条人话路径，干净回空串。"""
    if isinstance(v, float) and not math.isfinite(v):
        return "%s = %r" % (path, v)
    if isinstance(v, dict):
        for k, x in v.items():
            hit = no_bad_numbers(x, "%s.%s" % (path, k))
            if hit:
                return hit
    if isinstance(v, list):
        for i, x in enumerate(v):
            hit = no_bad_numbers(x, "%s[%d]" % (path, i))
            if hit:
                return hit
    return ""


# ── A 纯函数：行里的越界数字按同一把尺子归 0 ────────────────────────
# 1e400 / NaN 写成字面量而不是 json.dump —— 手改、云同步落盘的账本正是这个形状，
# json.load 读回来就是 inf / nan。
INF = float("inf")
NAN = float("nan")

st = anna_state.normalize({
    "books": [{"name": "坏数字.epub", "title": "旧账", "author": "a", "book": "anna_x",
               "bytes": 1e400, "chapters": INF, "at": NAN}],
    "pending": [{"name": "没进书架.mobi", "reason": "格式不认",
                 "bytes": 1e400, "at": 1e400}],
})
bk = (st.get("books") or [{}])[0]
pk = (st.get("pending") or [{}])[0]
chk("books 行的 bytes 越界归 0", bk.get("bytes") == 0, bk.get("bytes"))
chk("books 行的 chapters 越界归 0", bk.get("chapters") == 0, bk.get("chapters"))
chk("books 行的 at 是 NaN 也归 0", bk.get("at") == 0, bk.get("at"))
chk("pending 行的 bytes / at 越界归 0",
    pk.get("bytes") == 0 and pk.get("at") == 0, pk)
chk("归 0 的是普通 int（不是 bool / float / inf / nan）",
    all(isinstance(r.get(k), int) and not isinstance(r.get(k), bool)
        for r, ks in ((bk, ("bytes", "chapters", "at")), (pk, ("bytes", "at")))
        for k in ks),
    (bk, pk))
chk("整份 normalize 结果里没有非有限浮点", not no_bad_numbers(st), no_bad_numbers(st))

# 手改落到不认识的那个键上，一样要收（_finite 是递归的）；不认识的键本身照旧保留。
st2 = anna_state.normalize({
    "books": [{"name": "x.epub", "unknown_num": 1e400,
               "nested": {"deep": [{"v": NAN}]}}],
})
row2 = (st2.get("books") or [{}])[0]
chk("不认识的键照旧保留（不丢字段）", "unknown_num" in row2 and "nested" in row2, row2)
chk("不认识键上的越界数字也归 0",
    row2.get("unknown_num") == 0
    and row2.get("nested", {}).get("deep", [{}])[0].get("v") == 0, row2)

# 好数据：一个数都不许改，形状也不许动。
GOOD = {"name": "好.epub", "title": "示范这本", "author": "作者", "book": "anna_ok",
        "bytes": 4096, "chapters": 12, "at": 1700000000}
st3 = anna_state.normalize({"books": [dict(GOOD)]})
chk("好行原样收（逐字段相同）", st3.get("books") == [GOOD], st3.get("books"))
chk("数字字符串照认（\"42\" 就是 42）",
    anna_state.normalize({"books": [{"bytes": "42", "at": "1700000000"}]})
    ["books"][0].get("bytes") == 42)

# 文本字段与顶层同一把尺子：落单的半个代理码点摘掉、坏形状不抛。
HALF = "\ud83d"
st4 = anna_state.normalize({"books": [{"name": "书" + HALF + "名.md", "title": 123}],
                            "pending": [{"reason": "第一行\n第二行"}]})
r4 = (st4.get("books") or [{}])[0]
chk("行里文本的落单代理码点被摘掉", HALF not in (r4.get("name") or ""), r4.get("name"))
chk("行里文本的坏形状不抛、收成人话",
    isinstance(r4.get("title"), str) and r4.get("title") == "123", r4.get("title"))
chk("pending 的 reason 换行压成一句",
    "\n" not in (st4.get("pending") or [{}])[0].get("reason", ""),
    (st4.get("pending") or [{}])[0].get("reason"))

# 形状与上限照旧：非对象丢掉、每栏最多 12 条。
chk("非对象行照样丢掉、不认识的键原样留着",
    anna_state.normalize({"books": [{"n": 1}, "字符串", None]})["books"] == [{"n": 1}])
chk("books 仍最多留 12 条",
    len(anna_state.normalize({"books": [{"n": i} for i in range(40)]})["books"]) == 12)

# 整份真的写得成严格 JSON（这是这一条的契约）。
try:
    text = json.dumps(st, ensure_ascii=False, allow_nan=False)
    strict_json(text)
    chk("normalize 的结果能写成严格 JSON（没有 Infinity / NaN）", True)
except Exception as e:                                    # noqa: BLE001
    chk("normalize 的结果能写成严格 JSON（没有 Infinity / NaN）", False,
        "%s: %s" % (type(e).__name__, e))

# 对照：旧写法（行原样直收）确实会把 Infinity 写进回包 —— 这组断言不是空转。
old_text = json.dumps({"books": [{"bytes": 1e400, "at": 1e400}]}, ensure_ascii=False)
chk("对照：旧写法原样直收时 json.dumps 确实写出 Infinity",
    "Infinity" in old_text, old_text)

# ── B 源码：normalize 不再把行原样直收 ─────────────────────────────
chk("源码里不再有 `out.append(r)` 的直收行", "out.append(r)" not in SRC)
chk("行经 _clean_row 收口（认得的那几个字段 + _finite）",
    "out.append(_clean_row(r, nums, texts))" in SRC
    and "def _clean_row(" in SRC and "def _finite(" in SRC)
chk("行里的数字走 _as_int、文本走 _note",
    "_as_int(out[k])" in SRC and "_note(out[k])" in SRC)

# ── C 真机：/api/state 必须是严格 JSON ─────────────────────────────
cache = pathlib.Path(ui_server.CACHE_DIR)
cache.mkdir(parents=True, exist_ok=True)
(cache / "anna.json").write_text(json.dumps({
    "domain": "annas-archive.is", "window": "closed", "keyword": "x",
    "caught": 3, "imported": 2, "skipped": 1,
    "books": [{"name": "坏.epub", "title": "旧账", "author": "a", "book": "anna_x",
               "bytes": 1e400, "chapters": 3, "at": 1e400}],
    "pending": [{"name": "坏.mobi", "reason": "格式不认", "bytes": 1e400, "at": 1e400}],
    "note": "n", "updated_at": 1,
}), encoding="utf-8")

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT


def call(path, timeout=15):
    """→ (status, body_text)。status 为 'ERR' 表示连接层就失败了（服务断了）。"""
    try:
        r = urllib.request.urlopen(BASE + path, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                # noqa: BLE001
        return "ERR", "%s: %s" % (type(e).__name__, e)


st_code, body = call("/api/state")
if st_code == "ERR":
    chk("真机：GET /api/state 回 200（不是连接被甩）", False, body[:200])
elif st_code != 200:
    chk("真机：GET /api/state 回 200（不是连接被甩）", False,
        "HTTP %s：%r" % (st_code, body[:200]))
else:
    chk("真机：GET /api/state 回 200（不是连接被甩）", True)
    chk("真机：回包正文里没有 Infinity / NaN 字面量",
        "Infinity" not in body and "NaN" not in body,
        body[body.find("Infinity") - 80: body.find("Infinity") + 40] if "Infinity" in body else "")
    obj = None
    try:
        obj = strict_json(body)
        chk("真机：回包是严格 JSON（浏览器 JSON.parse 不会整份失败）", True)
    except Exception as e:                                # noqa: BLE001
        chk("真机：回包是严格 JSON（浏览器 JSON.parse 不会整份失败）", False,
            "%s：%r" % (e, body[:200]))
    if isinstance(obj, dict):
        anna = obj.get("anna") or {}
        rb = (anna.get("books") or [{}])[0]
        rp = (anna.get("pending") or [{}])[0]
        chk("真机：坏账里的两行都还在（没有被整条丢掉）",
            (anna.get("books") or []) and (anna.get("pending") or []), anna)
        chk("真机：books 行的 bytes / at 已是有限 int",
            isinstance(rb.get("bytes"), int) and isinstance(rb.get("at"), int)
            and math.isfinite(rb.get("bytes")) and math.isfinite(rb.get("at")), rb)
        chk("真机：pending 行的 bytes / at 已是有限 int",
            isinstance(rp.get("bytes"), int) and isinstance(rp.get("at"), int)
            and math.isfinite(rp.get("bytes")) and math.isfinite(rp.get("at")), rp)
        chk("真机：整份 anna 状态里没有非有限浮点", not no_bad_numbers(anna),
            no_bad_numbers(anna))

# 干净数据的回归：账本没坏时照旧 200 + 严格 JSON，读数一个不改。
(cache / "anna.json").write_text(json.dumps({
    "domain": "annas-archive.gd", "window": "open", "keyword": "归藏",
    "caught": 5, "imported": 4, "skipped": 1,
    "books": [{"name": "好.epub", "title": "示范这本", "author": "作者", "book": "anna_ok",
               "bytes": 4096, "chapters": 12, "at": 1700000000}],
    "pending": [{"name": "好.mobi", "reason": "格式不认", "bytes": 20, "at": 1700000001}],
    "note": "一切正常", "updated_at": 1700000002,
}), encoding="utf-8")
st_code, body = call("/api/state")
ok_clean = st_code == 200
clean = None
if ok_clean:
    try:
        clean = strict_json(body)
    except Exception:                                     # noqa: BLE001
        ok_clean = False
chk("干净回归：/api/state 仍回 200 + 严格 JSON", ok_clean,
    "HTTP %s：%r" % (st_code, body[:200]))
if isinstance(clean, dict):
    anna = clean.get("anna") or {}
    chk("干净回归：好账一个字段都不改",
        anna.get("books", [{}])[0].get("bytes") == 4096
        and anna.get("books", [{}])[0].get("title") == "示范这本"
        and anna.get("pending", [{}])[0].get("bytes") == 20,
        anna)

bad = [c for c in checks if not c[0]]
print("\n安娜账本行脏数字体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + _safe(x) if x else ""))
sys.exit(1 if bad else 0)
