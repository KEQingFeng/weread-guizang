#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加书架回包形状收口（shelf_add.py）：合法 JSON 但不是对象，不许甩 AttributeError。

`/mp/shelf/addToShelf` 的回包形状不由我们定：接口正常时是 `{"succ":…}`，但中间
可能夹一层反向代理 / 门户认证页，或对面改版 —— `json.loads` 能解出一段**合法
JSON 却不是对象**的东西：`[]` / `"x"` / `123` / `null`。旧读法在 `post_add` 里
只兜了「解不出 JSON」那一种：

    json.loads("[]").get("succ")   -> AttributeError: 'list' object has no attribute 'get'

`main` 只把「接口回包看不懂」这句话留给 body=None，于是 JSON 数组一路穿到顶层
except，用户看到的是一句英文类型错误（`AttributeError: 'list' object has no
attribute 'get'`），而不是「接口回包看不懂（HTTP …）」。sync.py 的 `_json_obj`
修的是同一类形状（那边还会遇上 JSON 数组 / HTML 门户页）。

同一个回包里另一个不设防的字段是错误码：接口用 errCode / errcode 两种拼法，值
理应是整数。旧读法 `body.get("errCode", body.get("errcode", 0)) or 0` 之后直接
拿它比大小 —— 写成字符串 `"-2012"` 时 `== -2012` 判不出（于是不续期），紧接着
`err < 0` 当场 TypeError。

这一条离线钉住：
  · reply_obj：对象照收；数组 / 字符串 / 数字 / null / 非 JSON / 空串都回 None，不抛；
  · err_code：两种拼法都认；字符串数字照认；布尔 / 对象 / 越界浮点 / 非对象当 0；
  · 对照：裸 json.loads(...).get 对 JSON 数组确实抛 AttributeError，证明护栏不空转；
  · 接线：post_add 走 reply_obj、main 走 err_code，别在别处又抄一份裸读法。

要 import shelf_add（它 import playwright），没装时明确标 SKIP，不把「没测」装成「通过」。

跑法：.venv/bin/python tests/check_shelf_add_reply.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

FAIL = []
SKIPS = []


def chk(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   " + repr(extra)[:220]))
    if not ok:
        FAIL.append(name)


def skip(name, why):
    SKIPS.append(name)
    print(("SKIP  " + name + "   " + why)[:260])


try:
    import shelf_add as sa  # noqa: E402
except Exception as e:            # 多半是没装 playwright
    skip("加书架回包形状", "import shelf_add 失败（%s: %s），这条起不来"
         % (type(e).__name__, e))
    print()
    print("加书架回包形状收口：跳过 %d 项" % len(SKIPS))
    sys.exit(0)


# ── 1. 回包形状：是对象才收，别的一律 None ─────────────────────────
GOOD = '{"succ": 1, "errCode": 0}'
chk("真回包（对象）照收", sa.reply_obj(GOOD) == {"succ": 1, "errCode": 0},
    sa.reply_obj(GOOD))

# 合法 JSON、不是对象：这几种 json.loads 都不抛，旧读法在后面 .get 才炸。
for name, text in [
    ("空数组", "[]"),
    ("有内容的数组", '["付款", "成功"]'),
    ("字符串", '"登录"'),
    ("数字", "123"),
    ("null", "null"),
    ("真/假", "true"),
    ("嵌套数组", "[[{\"succ\": 1}]]"),
]:
    chk("合法 JSON 但不是对象（%s）回 None，不抛" % name, sa.reply_obj(text) is None,
        sa.reply_obj(text))

# 压根不是 JSON 的：门户页 / HTML / 空串，旧读法靠 try 兜住，这里沿用同一个结果。
for name, text in [("空串", ""), ("空白", "   "), ("HTML 门户页", "<html>登录过期</html>"),
                   ("半截 JSON", '{"succ":'), ("Python 字面量", "None")]:
    chk("不是 JSON（%s）回 None，不抛" % name, sa.reply_obj(text) is None, sa.reply_obj(text))

# JSON 里能写 Infinity：json.loads 收得下，形状仍是对象，照收（越界数字由别的尺子管）。
chk("对象里带 Infinity 仍算对象（不在这里拦）",
    isinstance(sa.reply_obj('{"succ": 1e400}'), dict))

# 对照：证明「不抛」不是白说的 —— 旧读法对上面的数组确实抛 AttributeError。
crashed = None
try:
    json.loads("[]").get("succ")
except Exception as e:
    crashed = e
chk("对照：旧读法 json.loads('[]').get(...) 确实抛 AttributeError",
    isinstance(crashed, AttributeError), crashed)

# 第二处对照：错误码被写成字符串时，旧读法拿它比大小确实抛 TypeError（"0" truthy）。
crashed2 = None
try:
    "0" < 0
except Exception as e:
    crashed2 = e
chk("对照：旧读法拿字符串错误码比大小确实抛 TypeError",
    isinstance(crashed2, TypeError), crashed2)


# ── 2. 错误码：两种拼法 / 字符串数字 / 怪形状 ──────────────────────
chk("errCode 整数照认", sa.err_code({"errCode": -2012}) == -2012)
chk("errcode（小写拼法）也认", sa.err_code({"errcode": -2012}) == -2012)
chk("字符串数字照认（旧写法 == -2012 判不出、< 0 还抛 TypeError）",
    sa.err_code({"errCode": "-2012"}) == -2012)
chk("字符串 '0' 是 0（旧写法它 truthy，随后 err < 0 抛 TypeError）",
    sa.err_code({"errCode": "0"}) == 0)
chk("浮点整数值照认", sa.err_code({"errCode": 3.0}) == 3)
chk("布尔不算错误码（True 不是 1）", sa.err_code({"errCode": True}) == 0)
chk("认不出的字符串当 0，不拿去比大小",
    sa.err_code({"errCode": "abc"}) == 0)
chk("对象 / 数组当 0", sa.err_code({"errCode": {"a": 1}}) == 0
    and sa.err_code({"errCode": [1]}) == 0)
chk("越界浮点（1e400 = inf）当 0，不抛 OverflowError",
    sa.err_code({"errCode": float("inf")}) == 0
    and sa.err_code({"errCode": float("nan")}) == 0)
chk("errCode 在但为 null 时当 0（与旧写法 `or 0` 一致）",
    sa.err_code({"errCode": None, "errcode": -1}) == 0)
chk("整个 body 不是对象也回 0，不抛", sa.err_code([]) == 0
    and sa.err_code(None) == 0 and sa.err_code("x") == 0)
chk("回包里没有错误码字段 = 0", sa.err_code({"succ": 1}) == 0)


# ── 3. 接线：真入口用的是这两把尺子，不是又抄一份裸读法 ─────────────
src = (ROOT / "shelf_add.py").read_text(encoding="utf-8")
chk("post_add 走 reply_obj（不是裸 json.loads 之后交给调用方）",
    "return resp.status, reply_obj(text), text" in src)
chk("main 两处错误码都走 err_code",
    src.count("err = err_code(body)") == 2, src.count("err = err_code(body)"))
chk("不再留裸读错误码那句（err = body.get(…)）",
    "err = body.get(" not in src)
chk("reply_obj 定义在仓库里，且不是空实现",
    "def reply_obj(text)" in src and "isinstance(body, dict)" in src)
chk("err_code 定义在仓库里，且认两种拼法",
    "def err_code(body)" in src and '"errcode"' in src)

print()
if SKIPS:
    print("加书架回包形状收口：跳过 %d 项（%s）" % (len(SKIPS), "、".join(SKIPS)))
print("加书架回包形状收口：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
