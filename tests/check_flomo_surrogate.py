#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：便签账本里落单的半个代理码点，不许让「手记 / 打标签 / 忘一条 / 清空」写不下去。

为什么会有这种档
    `json` 收得下 `"\\ud800"` 这种转义 —— 读回来就是落单的半个代理码点
    （U+D800–U+DFFF，不成对）。便签的正文有两条进账：界面「手记」那支笔从
    `POST /api/flomo/notes` 的 JSON 体里拿（`fetch` 送来的字符串里可以有这种
    转义），导入 / 手改 / 云同步落下的那份账本里也可能夹着。而这个码点在 utf-8
    里编不出来，便签账本偏偏是 `ensure_ascii=False` 落盘的。

实测的故障（改之前）
      · `add_memo("好句子\\ud800收尾")` → UnicodeEncodeError：`memo_of` 算 id 时
        先 `.encode("utf-8")`，正文根本到不了落盘；界面上是
        「手记一条」回一句 `'utf-8' codec can't encode character '\\ud800' …`；
      · 账本里已经躺着一个坏码时，`set_book` / `forget` / `clear` / 打标签
        （`flomo_notes.save`）全部在 `json.dump` 上抛穿 —— 便签从此只读，
        连「清空」都清不掉；
      · 划线导出 CSV 的 `build_import_csv` 同理，`encode("utf-8")` 当场抛，
        整份导出全废。

收口
    一把尺子 `flomo_notes.drop_lone_surrogates`（与 `book_layout` 同名同口径），
    接在三个口子上：`memo_of`（一条笔记出生处，id 要先编码）、`_clean_memo`
    （账本读回来的唯一入口）、`save`（唯一的落盘出口）；CSV 导出口另过一遍。
    成对的代理码点在 `json.load` 那一步早合成一个真字符，只会摘到落单的坏码，
    emoji 不受影响。

跑法：纯函数 + 系统临时目录沙盒 + 一个进程内的真 HTTP 服务，不联网、不开浏览器、
不起外部服务，归 --fast 静态段。
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-flomo-srg-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import flomo_notes as F                                   # noqa: E402
import ui_server                                          # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def call(fn, *a, **k):
    """把入口调一次：抛了就把异常当结果交出去，别让整条套件倒在第一发上。"""
    try:
        return fn(*a, **k), None
    except Exception as e:                       # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def fresh(tag):
    d = os.path.join(ROOT, tag)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def wrote_clean_utf8(flomo_dir):
    """账本文件本身必须是能按 utf-8 解出来的字节，且里面没有裸的代理码点。"""
    with open(F.path_in(flomo_dir), "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8")                 # 编不出来就抛，交给调用方记失败
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


BAD = "坏\ud800码"                              # 落单的半个代理码点
EMOJI = "🧑‍💻"                                  # ZWJ 是 emoji 的组成笔画，绝不许被洗掉

try:
    # ── 0. 前提：这个值真的是 JSON 读得出来、utf-8 编不出来的 ──────────
    chk("J1 转义 \\ud800 能被 json 读成半个代理码点",
        json.loads('"\\ud800"') == "\ud800")
    chk("J2 半个代理码点确实编不进 utf-8",
        call(lambda: "\ud800".encode("utf-8"))[1] is not None)

    # ── 1. 纯函数：drop_lone_surrogates ───────────────────────────────
    chk("F1 单个字符串：坏码摘掉，别的字一个不动",
        F.drop_lone_surrogates(BAD) == "坏码", F.drop_lone_surrogates(BAD))
    chk("F2 成对的 emoji 不许误伤", F.drop_lone_surrogates(EMOJI) == EMOJI)
    chk("F3 字典 / 列表逐层走，非字符串类型原样回",
        F.drop_lone_surrogates({"a": BAD, "b": [1, "x\udfffy"], "c": 5, "d": None})
        == {"a": "坏码", "b": [1, "xy"], "c": 5, "d": None},
        F.drop_lone_surrogates({"a": BAD, "b": [1, "x\udfffy"], "c": 5, "d": None}))
    chk("F4 干净输入一个字不改（幂等）",
        F.drop_lone_surrogates({"md": "好句子", "tags": ["读书"]})
        == {"md": "好句子", "tags": ["读书"]})

    # ── 2. memo_of：一条笔记出生处，算 id 前就得干净 ──────────────────
    m, err = call(F.memo_of, "2026-03-05 08:00:00", "正文" + BAD + " #读书")
    chk("M1 memo_of 不再抛，正文与标签都洗过",
        err is None and m["md"] == "正文坏码 #读书" and m["tags"] == ["读书"], (err, m))
    chk("M2 id 是干净正文算出来的哈希（同文同 id）",
        F.memo_of("2026-03-05 08:00:00", "正文坏码 #读书")["id"] == m["id"], m["id"])

    # ── 3. add_memo：界面「手记」那支笔的整条路 ───────────────────────
    d1 = fresh("hand")
    res, err = call(F.add_memo, d1, "好句子" + BAD + "收尾")
    chk("A1 带坏码的手记不再抛，回 ok",
        err is None and res.get("ok") is True, (err, res))
    chk("A2 存下来的正文本就是洗过的",
        err is None and res["memo"]["md"] == "好句子坏码收尾" and res["memo"]["words"] == 7,
        (res or {}).get("memo"))
    chk("A3 落盘文件是合法 utf-8、没有裸的坏码", wrote_clean_utf8(d1))
    back = F.load(d1)
    chk("A4 读回来还是干净的，条目没丢",
        len(back["memos"]) == 1 and back["memos"][0]["md"] == "好句子坏码收尾", back)

    d2 = fresh("emoji")
    res2, err2 = call(F.add_memo, d2, "拼字" + EMOJI + "正常")
    chk("A5 成对 emoji 原样存下（不被误伤）",
        err2 is None and res2["memo"]["md"] == "拼字" + EMOJI + "正常", (err2, res2))
    chk("A6 空正文仍旧不收", F.add_memo(fresh("empty"), "")["ok"] is False)

    # ── 4. 脏账本：坏码已在盘上，所有写口都不许废 ─────────────────────
    d3 = fresh("dirty")
    with open(F.path_in(d3), "w", encoding="utf-8") as f:
        # ensure_ascii=True：写出 `\ud800` 转义 —— 合法 JSON 字节，读回来就是半个坏码
        json.dump({"version": 1, "at": 0, "src": "",
                   "memos": [{"id": "m1", "md": "脏" + BAD + "正文", "tags": ["标" + BAD],
                              "imgs": [], "atts": [], "ts": 1792000000}]}, f,
                  ensure_ascii=True, indent=1)
    got = F.load(d3)
    chk("D1 脏账本读回来正文 / 标签都干净了（条目还在）",
        len(got["memos"]) == 1 and got["memos"][0]["md"] == "脏坏码正文"
        and got["memos"][0]["tags"] == ["标坏码"], got)
    _, e1 = call(F.set_book, d3, "m1", "bk_srg")
    chk("D2 set_book（收成书那条路）不再抛", e1 is None, e1)
    _, e2 = call(F.forget, d3, "m1")
    chk("D3 forget 不再抛", e2 is None, e2)
    _, e3 = call(F.clear, d3)
    chk("D4 clear 不再抛（坏码不会让「清空」也清不掉）", e3 is None, e3)
    chk("D5 重写后的账本仍是合法 utf-8", wrote_clean_utf8(d3))

    d4 = fresh("portrait")
    with open(F.path_in(d4), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "memos": [{"id": "m1", "md": "#读书 一句" + BAD,
                                            "tags": ["读书"], "ts": 1792000000}]},
                  f, ensure_ascii=True)
    _, e4 = call(F.write_portrait, d4)
    chk("D6 记忆画像（会写 Markdown）不再抛", e4 is None, e4)

    # ── 5. CSV 导出：正文同样来自请求体 ──────────────────────────────
    blob, err5 = call(F.build_import_csv, [("一句" + BAD, 1792000000)])
    chk("C1 带坏码的正文导得成 CSV（utf-8 字节）",
        err5 is None and isinstance(blob, bytes) and blob.decode("utf-8").startswith("\ufeffcontent"),
        err5)
    chk("C2 CSV 正文里没有裸的坏码", err5 is None and BAD not in blob.decode("utf-8"),
        (blob or b"")[:40])

    # ── 6. 真机：进程内起服务，真发一次 POST /api/flomo/notes ─────────
    # 纯函数绿了也可能在路由那层被别的口子挡死，所以非真发一次不可。
    ui_flomo = os.path.join(ui_server.CACHE_DIR, "flomo")
    shutil.rmtree(ui_flomo, ignore_errors=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/flomo/notes" % port,
            data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    try:
        st, body = post({"act": "add", "md": "真机手记" + BAD})
        chk("S1 POST /api/flomo/notes 手记回 200 且 ok",
            st == 200 and body.get("ok") is True, (st, body))
        chk("S2 回包里没有把 Python 报错递给界面",
            "codec can't encode" not in json.dumps(body, ensure_ascii=False), body)
        memo = body.get("memo") or {}
        chk("S3 回包里的正文是洗过的", memo.get("md") == "真机手记坏码", memo)
        # 账本里已经躺着一个坏码时，接着打标签 / 再记一条也不许废
        p = F.path_in(ui_server.FLOMO_DIR)
        data = json.load(open(p, encoding="utf-8"))
        data["memos"][0]["md"] = "旧的" + BAD
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True)
        st2, body2 = post({"act": "tag", "id": memo.get("id"), "tag": "新标签"})
        chk("S4 脏账本上打标签照样 200 且 ok",
            st2 == 200 and body2.get("ok") is True, (st2, body2))
        chk("S5 标签回包没夹 Python 报错",
            "codec can't encode" not in json.dumps(body2, ensure_ascii=False), body2)
        st3, body3 = post({"act": "add", "md": "再记一条" + BAD + "干净"})
        chk("S6 脏账本上再手记一条也成", st3 == 200 and body3.get("ok") is True, (st3, body3))
    except Exception as e:                       # noqa: BLE001
        chk("S1 POST /api/flomo/notes 手记回 200 且 ok", False,
            "%s: %s" % (type(e).__name__, e))
        for nm in ("S2 回包里没有把 Python 报错递给界面", "S3 回包里的正文是洗过的",
                   "S4 脏账本上打标签照样 200 且 ok", "S5 标签回包没夹 Python 报错",
                   "S6 脏账本上再手记一条也成"):
            chk(nm, False, "见 S1")
    finally:
        srv.shutdown()
        srv.server_close()
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("便签账本半个代理码点体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("  ✗ " + "、".join(FAIL))
sys.exit(1 if FAIL else 0)
