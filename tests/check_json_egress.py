# -*- coding: utf-8 -*-
"""归藏门禁：回包里不许印出 `Infinity` / `NaN`（浏览器 `JSON.parse` 见到就整条作废）。

为什么单开这一份
    前面那几条（check_dirty_numeric / check_feed_overflow / check_video_time /
    check_notes_dirty_read …）管的都是**某个字段**的收口：一个越界时间戳回 0、
    一条坏记录当空。这一条管的是它们下面那道**最后的口**：

        `json.dumps` 默认 `allow_nan=True`，把 `inf` / `nan` 原样印成
        `Infinity` / `-Infinity` / `NaN` —— 那是 Python 的方言，不是 JSON。
        浏览器 `JSON.parse` 见到当场 SyntaxError，**整条回包作废**：用户看到的是
        「画板打不开」「这一屏整页空白」，而不是「某一个数字显示成空」。

    账本里这类值住得进来，而且是合法 JSON：手改、云同步落盘、旧版本写回，都会把
    一个 `Infinity` 写进 `boards/<id>.json` 的 canvas 坐标（画布那棵树我们完全不
    解析，原样透传）、`feed.json` 的时间戳之类的地方。字段级收口是逐条堵，堵得住
    今天列得出的十条；`_json` 是唯一那道**回包出口**，只认字段迟早漏第十一条。

    所以这一条把口收在 `ui_server._json_text`：干净的路照旧只 dump 一次；真逮到
    `Infinity` / `NaN` 那三个字面量时，把非有限数换成 `null`（前端拿到 unknown、
    界面照常铺开）重印一遍。**认得出的值（整数 / 小数 / 字符串 / 布尔）一个字都
    不许改口径**，调用方手里的对象也不许被就地改掉。与 `_encode_body` 收半个代理
    码点是同一路子：都是「一个坏值不许把整条回包带走」的最后一道口。

    B 段用一份手写进盘、真带 `Infinity` 字面量的画板档案走一遍产品级读路径；
    W 段直接调 `Handler._json`（拿一个只记 `_send` 的假壳），钉住的是**接线**，
    不是只测那个辅助函数。全程离线、不起服务、不联网、不碰真实 cache/ 与书库。
"""
import atexit
import json
import math
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA / BOOKS / SHOT 必须在 import ui_server 之前指到临时沙盒：
# ui_server 在导入时就把这几个路径定下来了。
SANDBOX = tempfile.mkdtemp(prefix="guizang-jsonegress-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SANDBOX, "books")
os.environ["GUIZANG_SHOT_DIR"] = os.path.join(SANDBOX, "shots")
for _d in (os.environ["GUIZANG_DATA"], os.environ["GUIZANG_BOOKS"], os.environ["GUIZANG_SHOT_DIR"]):
    os.makedirs(_d, exist_ok=True)

import board as board_mod  # noqa: E402
import ui_server as us  # noqa: E402

FAILED = []
COUNT = 0
INF = float("inf")
NAN = float("nan")


def chk(name, cond, extra=""):
    global COUNT
    COUNT += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)[:200]))
    if not cond:
        FAILED.append(name)


def strict(text):
    """按浏览器的尺子解析：`JSON.parse` 不认 `Infinity` / `NaN`，这里的
    `parse_constant` 就是那一关（Python 自带的 `json.loads` 反而照收）。"""
    return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError("浏览器会拒绝的字面量：" + c)))


class _Shell:
    """`Handler._json` 只用到 `self._send`，拿这个壳就能把那条回包路径整条跑一遍。"""

    def __init__(self):
        self.sent = []

    def _send(self, code, ctype, body, extra=None):
        # 真 `_send` 对 str 正文会先过 `_encode_body`（收半个代理码点），这里照抄
        # 那一步，W4 才是「两道口叠起来」的真样子。
        if isinstance(body, str):
            body = us._encode_body(body)
        self.sent.append((code, ctype, body, extra))


def body_of(obj, code=200):
    sh = _Shell()
    us.Handler._json(sh, obj, code)
    code_out, ctype, body, _extra = sh.sent[0]
    return code_out, ctype, body.decode("utf-8")


# ── 0. 前提：这三个字面量真是「进得来、出不去」 ──────────────────────
print()
print("── 0. 前提：非有限数是合法输入、非法回包 ──────────────────")
raw = json.dumps({"a": INF, "b": NAN, "c": -INF})
chk("P1 json.dumps 默认把非有限数印成 Infinity / NaN / -Infinity",
    "Infinity" in raw and "NaN" in raw and "-Infinity" in raw, raw)
chk("P2 这三个字面量 json.loads 照收（所以它们真住得进账本）",
    json.loads('{"v": Infinity}')["v"] == INF
    and json.loads('{"v": -Infinity}')["v"] == -INF
    and json.loads('{"v": NaN}')["v"] != json.loads('{"v": NaN}')["v"])
try:
    strict(raw)
    chk("P3 但浏览器的尺子（严格 JSON）拒绝它", False, raw)
except ValueError:
    chk("P3 但浏览器的尺子（严格 JSON）拒绝它", True)


# ── 1. 序列化口本身：非有限数换成 null，干净的值一个字不改 ───────────
print()
print("── 1. _json_text：坏值换 null，认得出的值口径不动 ──────────")
clean = {"i": 3, "f": 1.5, "s": "正文里恰好出现 Infinity 和 NaN 两个词",
         "b": True, "n": None, "arr": [1, 2.25], "obj": {"k": "v"}}
chk("C1 干净回包与 json.dumps 一字不差（快路照旧，口径不变）",
    us._json_text(clean) == json.dumps(clean, ensure_ascii=False))
chk("C2 正文里带 Infinity / NaN 这两个词不会被误改",
    us._json_text(clean) == json.dumps(clean, ensure_ascii=False))

dirty = {"a": INF, "b": [-INF, NAN, 2.5], "c": {"d": INF, "keep": 7}}
text = us._json_text(dirty)
chk("D1 非有限数不再是字面量", "Infinity" not in text and "NaN" not in text, text)
parsed = strict(text)
chk("D2 严格解析过得去，且坏值全成了 null",
    parsed["a"] is None and parsed["b"][0] is None and parsed["b"][1] is None
    and parsed["c"]["d"] is None, parsed)
chk("D3 同一个容器里认得出的值一个不动（2.5 / 7 照旧）",
    parsed["b"][2] == 2.5 and parsed["c"]["keep"] == 7, parsed)
chk("D4 调用方手里的对象没有被就地改掉（原样还是 inf / nan）",
    dirty["a"] == INF and dirty["b"][0] == -INF and math.isnan(dirty["b"][1])
    and dirty["c"]["d"] == INF, dirty)

keyed = us._json_text({INF: 1, "normal": 2})
chk("K1 非有限数当键也不印出 Infinity（键收成 null 字符串）",
    "Infinity" not in keyed and "NaN" not in keyed and strict(keyed)["normal"] == 2, keyed)

tupled = us._json_text((INF, 1, [NAN]))
chk("T1 元组 / 嵌套列表里的非有限数一并收掉",
    "Infinity" not in tupled and "NaN" not in tupled and strict(tupled) == [None, 1, [None]],
    tupled)


# ── 2. 接线：走的是 Handler._json 那条回包路径，不是只测辅助函数 ────────
print()
print("── 2. 接线：Handler._json 出去的正文严格可解析 ─────────────")
code, ctype, body = body_of({"x": INF, "y": [NAN, 3]})
chk("W1 回包状态码与 Content-Type 没变",
    code == 200 and ctype == "application/json; charset=utf-8", (code, ctype))
chk("W2 回包正文严格可解析，坏值成 null、好值照旧",
    strict(body) == {"x": None, "y": [None, 3]}, body)

code, ctype, body = body_of(clean)
chk("W3 干净回包经 _json 出去仍与 json.dumps 一字不差",
    body == json.dumps(clean, ensure_ascii=False), body)

# 半个代理码点与非有限数同时出现：前者由 _encode_body 摘掉、后者由 _json_text 收掉，
# 两道口叠在一起才把这条回包完整发出去（少一道就是连接被甩 / 整条作废）。
code, ctype, body = body_of({"s": "\ud800", "n": INF})
chk("W4 半个代理码点 + 越界数字同时出现：回包仍编得出去且严格可解析",
    "\ud800" not in body and strict(body) == {"s": "", "n": None}, repr(body))


# ── 3. 产品级读路径：手写进盘的画板档案里真带一个 Infinity ─────────────
print()
print("── 3. 产品路径：脏画板档案不再让整条回包作废 ──────────────")


def write_board(board_id, canvas):
    d = os.path.join(SANDBOX, "books", "bktest")
    os.makedirs(os.path.join(d, "boards"), exist_ok=True)
    doc = {"schema": 1, "id": board_id, "title": "手改过的画板", "caption": "",
           "paper": "plain", "canvas": canvas, "bytes": 0,
           "created": "2026-01-01 00:00", "updated": "2026-01-01 00:00"}
    p = os.path.join(d, "boards", board_id + ".json")
    # 逐字节手写：要的就是盘上真有一行裸的 `Infinity`（json.dumps 默认允许它），
    # 证明这条输入是 json.load 收得下的合法 JSON，而不是测试自己造出来的对象。
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False))
    return d


d = write_board("b1", {"objects": [{"x": INF, "y": 12}]})
got = board_mod.load_board(d, "b1")
chk("B1 脏档案仍读得开（坏的是 canvas 里一个数，不是整份档案）",
    got and got["canvas"]["objects"][0]["x"] == INF, got)

code, ctype, body = body_of(got)
parsed = strict(body)
chk("B2 画板回包严格可解析：坏坐标成 null、另一个坐标与标题照旧",
    parsed["canvas"]["objects"][0]["x"] is None
    and parsed["canvas"]["objects"][0]["y"] == 12
    and parsed["title"] == "手改过的画板", parsed)

d2 = write_board("b2", {"objects": [{"x": 1.5, "y": 12}]})
good = board_mod.load_board(d2, "b2")
chk("B3 对照：干净画板的回包与 json.dumps 一字不差（没有为坏档案改口径）",
    us._json_text(good) == json.dumps(good, ensure_ascii=False))

print()
print("JSON 回包口门禁：%d 项通过，失败 %d 项" % (COUNT - len(FAILED), len(FAILED)))
for name in FAILED:
    print("  ✗ " + name)
sys.exit(1 if FAILED else 0)
