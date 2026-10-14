#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""响应正文里落单的半个代理码点，不许把整屏甩成空响应。

为什么会有这种字符
    `\\ud800`（U+D800–U+DFFF 单独出现、不成对）在 Python 里是个正经的 str 字符：
    `json` 读得下 `"\\ud800"` 这种转义，`json.dumps(..., ensure_ascii=False)` 也
    原样把它留在 JSON 文本里。而账本不只本进程在写 —— 用户手工编辑、云同步从别处
    落盘、旧版本写回，都可能让它住进一条便签的正文、一条划线的文字里。

实测的故障（改之前）
    `cache/flomo/notes.json` 里一条 `md` 写成 `"good \\ud800 tail"` 之后：

        GET /api/flomo/notes?mode=list
          → 连接被甩（`RemoteDisconnected`）：`_json` 拼出来的正文在
            `_send` 的 `body.encode("utf-8")` 上抛 UnicodeEncodeError，异常穿出
            `do_GET`，`BaseHTTPRequestHandler` 不会替你回 500，它只是把连接关掉。
            用户看到的是便签这一屏整个打不开，不是「某一条显示不全」。

    同一份索引（`cache/notes_index.json`）里带这种字符时，`/api/notes_search` 一样
    在编码那一步断线。书目录 meta 那一轮只收了「一本书的身份」那条路，响应这道
    总口一直没收 —— 每条路由各防一遍是防不完的。

收口只有一处
    JSON / HTML / Markdown / SSE 每一行，所有响应正文都从 `_send` / `_chunk`
    出去。编码收进 `_encode_body`：干净的路走的还是原来那句 `encode("utf-8")`，
    编不动时才按书库那把同样的尺子摘掉落单代理码点（`book_layout.drop_lone_surrogates`）。
    成对的代理码点在 `json.load` 那一步早就合成真字符（emoji 不受影响），落单的
    本来就是垃圾。

跑法：纯函数 + 源码接线 + 一个进程内真 Handler 的裸 HTTP，自带临时沙盒，
不联网、不开浏览器、不起外部服务。归 --fast 静态段。
"""
import atexit
import http.server
import json
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
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-resp-utf8-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"

import ui_server  # noqa: E402  —— 必须在环境变量设好之后 import

SERVER = (REPO / "ui_server.py").read_text(encoding="utf-8")
LONE = "\ud800"          # 落单的半个代理码点：utf-8 编不出来
PAIR_AS_JSON = '"\\ud83d\\ude00"'   # JSON 里成对的代理转义 → 一个真 emoji

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A 纯函数：干净的路一个字节不动，脏字符不抛 ─────────────────────
_enc = getattr(ui_server, "_encode_body", None)
chk("响应编码收口 _encode_body 存在（唯一入口）", callable(_enc))
chk("_encode_body 只有一处定义（一把尺子）",
    SERVER.count("def _encode_body(") == 1, SERVER.count("def _encode_body("))
if callable(_enc):
    clean = "普通中文 abc 😀 —— \n\t\"引号\""
    chk("干净正文与 text.encode('utf-8') 逐字节相同（不多走一步）",
        _enc(clean) == clean.encode("utf-8"))
    chk("空串仍是空串",
        _enc("") == b"")
    dirty = "good " + LONE + " tail"
    try:
        got = _enc(dirty)
    except Exception as e:                                # noqa: BLE001
        got = None
        chk("落单代理码点不再抛（改之前这里是 UnicodeEncodeError）", False,
            "%s: %s" % (type(e).__name__, e))
    if got is not None:
        text = got.decode("utf-8")                        # 必须是合法 utf-8，否则解不开
        chk("落单代理码点不再抛（改之前这里是 UnicodeEncodeError）", True)
        chk("坏字符摘掉、两边的正文一个字不改",
            text == "good  tail", repr(text))
        chk("坏字符没有换成 '?' 之类可见噪声",
            "?" not in text, repr(text))
        chk("结果里不再有任何半个代理码点",
            not any("\ud800" <= c <= "\udfff" for c in text), repr(text))
    # 真 emoji（U+1F600）是单个码点，不是代理对，绝不许被那把尺子误伤。
    chk("真 emoji 原样保留（它本来就不是代理对）",
        _enc("😀") == "😀".encode("utf-8"))
    # 佐证「摘掉代理码点不会吃掉 emoji」：JSON 里成对的转义，json.load 早就合成真字符，
    # 所以落单的那把尺子碰不到正常数据。
    combined = json.loads(PAIR_AS_JSON)
    chk("JSON 里成对的代理转义由 json.load 合成一个真字符（不是两个半码点）",
        combined == "\U0001F600" and len(combined) == 1, repr(combined))
    # 对照：老写法确实会抛 —— 这组断言不是空转。
    old_raised = False
    try:
        ("good " + LONE + " tail").encode("utf-8")
    except UnicodeEncodeError:
        old_raised = True
    chk("对照：旧的直接 encode('utf-8') 遇到落单代理码点确实 UnicodeEncodeError",
        old_raised)

# ── B 源码：两个出口都用它，坏字符的尺子与书库同一把 ───────────────
def _slice(start, end):
    """按标记取一段源码；标记不在（还没改 / 被改坏了）就回空串。

    回空串是让这一条当场变红，而不是让 `SERVER.index` 的 ValueError 把后面 C 层
    整个带走 —— 缺哪一样，读门禁的人要一眼看得见。
    """
    i = SERVER.find(start)
    if i < 0:
        return ""
    j = SERVER.find(end, i + len(start))
    return SERVER[i:j]


SEND = _slice("    def _send(self", "    def _json(self")
CHUNK = _slice("    def _chunk(self", "    def _chunk_end(self")
HELPER = _slice("def _encode_body(", "class Handler")
chk("_send 走 _encode_body（不再裸 body.encode）",
    "_encode_body(body)" in SEND and "body.encode(\"utf-8\")" not in SEND,
    [l.strip() for l in SEND.splitlines() if "encode" in l][:3])
chk("_chunk（SSE 每一行）也走 _encode_body",
    "_encode_body(text)" in CHUNK and "text.encode(\"utf-8\")" not in CHUNK,
    [l.strip() for l in CHUNK.splitlines() if "encode" in l][:3])
chk("坏字符的尺子复用书库那把 drop_lone_surrogates（不抄第二遍）",
    "book_layout.drop_lone_surrogates" in HELPER,
    HELPER[:120] or "找不到 _encode_body")

# ── C 真机：脏账本打过去要回完整 JSON，好数据原样 ─────────────────
# 便签账本：一条正文里带落单代理码点，另一条干干净净。
FLOMO = pathlib.Path(ui_server.FLOMO_DIR)
FLOMO.mkdir(parents=True, exist_ok=True)
GOOD_MD = "今天读完一章，记两笔。"
(FLOMO / "notes.json").write_text(json.dumps({
    "version": 2, "src": "test", "at": 0,
    "memos": [
        {"id": "m-bad", "date": "2026-01-01", "clock": "10:00", "ts": 1767225600,
         "md": "good " + LONE + " tail", "tags": ["读书"], "atts": [], "imgs": [],
         "book": "", "words": 3},
        {"id": "m-good", "date": "2026-01-02", "clock": "11:00", "ts": 1767312000,
         "md": GOOD_MD, "tags": ["读书"], "atts": [], "imgs": [],
         "book": "", "words": 10},
    ],
    # 不带 ensure_ascii=False：落单的半个代理码点写不成 utf-8 字节，只能以
    # `"\ud800"` 这种转义落在文件里 —— 手改 / 云同步落盘的账本正是这个形状，
    # json.load 读回来就是那个码点（也就是要验的那条路）。
}), encoding="utf-8")

# 划线索引：同样塞一条带落单代理码点的文字，走 /api/notes_search。
idx = pathlib.Path(ui_server.NOTES_IDX)
idx.parent.mkdir(parents=True, exist_ok=True)
idx.write_text(json.dumps({
    "built_at": 1,
    "items": [{"bookId": "bk_1", "title": "一本书", "author": "作者",
               "kind": "划线", "text": "keyword " + LONE + " x", "at": 1}],
    "books": [],
}, ensure_ascii=True), encoding="utf-8")

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT


def call(path, timeout=15):
    """→ (status, body_bytes)。status 为 'ERR' 表示连接层就失败了（服务断了）。"""
    try:
        r = urllib.request.urlopen(BASE + path, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                                # noqa: BLE001
        return "ERR", ("%s: %s" % (type(e).__name__, e)).encode()


def get_json(name, path, must_keys=("ok",)):
    st, body = call(path)
    bad = None
    obj = None
    if st == "ERR":
        bad = "连接被甩：%r" % body[:120]
    elif st != 200:
        bad = "HTTP %s：%r" % (st, body[:120])
    else:
        try:
            obj = json.loads(body or b"{}")
        except Exception as e:                            # noqa: BLE001
            bad = "回包不是完整 JSON（%s）：%r" % (e, body[:120])
        else:
            if any(k not in obj for k in must_keys):
                bad = "缺字段 %r：%r" % (list(must_keys), obj)
            elif any("\ud800" <= c <= "\udfff" for c in body.decode("utf-8", "replace")):
                bad = "回包里还留着半个代理码点"
    chk(name, bad is None, bad or "")
    return obj


obj = get_json("便签列表 mode=list：带坏字符的账本回 200 + 完整 JSON（不是 0 字节）",
               "/api/flomo/notes?mode=list", must_keys=("ok", "memos"))
if isinstance(obj, dict):
    memos = {m.get("id"): m for m in (obj.get("memos") or [])}
    bad_md = (memos.get("m-bad") or {}).get("md")
    chk("坏字符那条的正文：坏码点摘掉、好文字保留",
        bad_md == "good  tail", repr(bad_md))
    chk("干净那条的正文一个字不改",
        (memos.get("m-good") or {}).get("md") == GOOD_MD,
        repr((memos.get("m-good") or {}).get("md")))

obj = get_json("便签单条 mode=one：同一条脏账本回 200 + 完整 JSON",
               "/api/flomo/notes?mode=one&id=m-bad", must_keys=("ok", "memo"))
if isinstance(obj, dict) and obj.get("memo"):
    chk("单条里的坏码点也摘掉了",
        obj["memo"].get("md") == "good  tail", repr(obj["memo"].get("md")))

obj = get_json("划线搜索：索引里带坏字符也回 200 + 完整 JSON",
               "/api/notes_search?q=keyword", must_keys=("ok", "data"))
if isinstance(obj, dict) and obj.get("data"):
    chk("搜索命中的文字坏码点摘掉、关键词还在",
        obj["data"][0].get("text") == "keyword  x", repr(obj["data"][0].get("text")))

# 干净数据那条大路（/api/state）照旧 200 + JSON，且没有半个代理码点。
get_json("干净数据回归：/api/state 仍回 200 + 完整 JSON", "/api/state",
         must_keys=("books", "libs", "task"))

bad = [c for c in checks if not c[0]]
print("\n响应正文坏字符体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
