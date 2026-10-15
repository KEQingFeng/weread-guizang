#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态账本的**越界整数**：一串 400 位的 checked_at 不许把整份登录态带走。

为什么单开一条
    `check_login.py` 已经钉住「NaN / inf 不算秒数」，可它量的是 **float** 那一档的
    越界。账本里住得进来的还有第二种越界：`json` 把一长串数字解成一个 Python 大整数
    （`{"checked_at": 10**400}` 就是），而 `login._seconds` 只写了

        v = float(v)          # ← 大整数到这一步抛 OverflowError

    `math.isfinite` 还没轮到 —— 异常先穿出 `normalize_state`，被 `read_state` 那把
    宽 except 接住，于是**整份登录态回 None**：`logged_in: true` 连同 url / note
    一起没了，设置页与 MCP 把「已登录」显示成「未检测」。这与 `web_parse._fmt_epoch`
    是同一个坑（它专门 catch 了 OverflowError，注释写明「整数大到 float 装不下」），
    登录这本账漏了。

    口径与 `check_login.py` 一致：认不出的时间给 None（界面据此不印那半句），
    **其余字段一个字不改**；能装进 float 的值（含小数）一点不许改口径。

全程离线、不起服务、不联网、不开浏览器、不需要 playwright。
"""
import json
import math
import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先把数据目录指进临时沙盒再 import ui_server：免得它真去动用户真实的 cache/。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-login-overflow-"))
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import login as lg      # noqa: E402
import ui_server as us  # noqa: E402

FAIL = []
PASSED = 0
BIG = 10 ** 400


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 0. 前提：这确实是「float 装不下」，不是 inf ─────────────────────────
try:
    float(BIG)
    chk("P0 前提：400 位整数 float() 会抛（不然这条门禁空转）", False)
except OverflowError:
    chk("P0 前提：400 位整数 float() 就是抛 OverflowError 的那种越界", True)


def _old_seconds(v):
    """收口前的写法：先把值转 float，再判有限性。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)                     # ← 大整数在这里抛
    if not math.isfinite(v) or v <= 0:
        return None
    return v


try:
    _old_seconds(BIG)
    chk("P1 对照：旧写法在越界整数上确实抛（防门禁空转）", False)
except OverflowError:
    chk("P1 对照：旧写法在越界整数上确实抛（防门禁空转）", True)


# ── 1. normalize_state：不抛、时间回 None、其余字段一个字不改 ──────────
GOOD_URL = "https://weread.qq.com/web/shelf"
try:
    got = lg.normalize_state({"logged_in": True, "checked_at": BIG,
                              "url": GOOD_URL, "note": "会话有效"})
    chk("H1 大整数 checked_at 不再抛 OverflowError", True)
except Exception as e:      # noqa: BLE001  —— 这里就是要逮住任何穿出来的异常
    chk("H1 大整数 checked_at 不再抛 OverflowError", False, "%s: %s" % (type(e).__name__, e))
    got = None

chk("H2 认不出的时间收成 None", isinstance(got, dict) and got["checked_at"] is None, got)
chk("H3 logged_in / url / note 一个字不改（坏时间不许带走整份）",
    got == {"logged_in": True, "checked_at": None, "url": GOOD_URL, "note": "会话有效"}, got)
chk("H4 负向越界（-10**400）同样回 None",
    lg.normalize_state({"checked_at": -(10 ** 400)})["checked_at"] is None)


# ── 2. read_state：整份不许变成 None ───────────────────────────────────
STATE = SB / "cache" / "login_state.json"
STATE.parent.mkdir(parents=True, exist_ok=True)


def write_raw(blob):
    STATE.write_text(blob, encoding="utf-8")


write_raw('{"logged_in": true, "checked_at": %d, "url": "u", "note": "n"}' % BIG)
got = lg.read_state(STATE)
chk("H5 读到带越界整数的账本：回的是洗过的对象，不是 None",
    isinstance(got, dict) and got["logged_in"] is True, got)
chk("H6 越界时间在 read_state 里也收成 None",
    isinstance(got, dict) and got["checked_at"] is None, got)
chk("H7 url / note 也没被连累", isinstance(got, dict) and got["url"] == "u"
    and got["note"] == "n", got)

# 前一条浮点那一档的越界（json 把 1e400 解成 inf）走同一条读路径，口径照旧。
write_raw('{"logged_in": true, "checked_at": 1e400}')
got = lg.read_state(STATE)
chk("H8 账本里是 1e400（json 解成 inf）也照旧：logged_in 留住、时间收 None",
    isinstance(got, dict) and got["logged_in"] is True and got["checked_at"] is None, got)


# ── 3. 口径没被放宽：能认的值一个不许动 ───────────────────────────────
chk("H9 正常小数秒数照收",
    lg.normalize_state({"checked_at": 1700000000.25})["checked_at"] == 1700000000.25)
chk("H10 能装进 float 的大整数口径不变（10**18 照收成 1e18）",
    lg.normalize_state({"checked_at": 10 ** 18})["checked_at"] == float(10 ** 18))
chk("H11 inf / NaN / 布尔 / 字符串仍是 None（原有口径没被放宽）",
    lg.normalize_state({"checked_at": float("inf")})["checked_at"] is None
    and lg.normalize_state({"checked_at": float("nan")})["checked_at"] is None
    and lg.normalize_state({"checked_at": True})["checked_at"] is None
    and lg.normalize_state({"checked_at": "1700000000"})["checked_at"] is None)


# ── 4. 接线：/api/state 的 login 那一格也留得住 ─────────────────────────
write_raw('{"logged_in": true, "checked_at": %d, "url": "u", "note": "n"}' % BIG)
_old_state_path = us.LOGIN_STATE
us.LOGIN_STATE = str(STATE)
try:
    got = us.read_login_state()
    chk("H12 ui_server 读到的也是对象（不是 None），logged_in 没丢",
        isinstance(got, dict) and got["logged_in"] is True, got)
    chk("H13 ui_server 这一层的时间同样是 None",
        isinstance(got, dict) and got["checked_at"] is None, got)
finally:
    us.LOGIN_STATE = _old_state_path


# ── 5. 源码接线：float() 那道转换被 OverflowError 兜住 ─────────────────
src_login = (ROOT / "login.py").read_text(encoding="utf-8")
m = re.search(r"def _seconds\(v\):(.*?)\n\n\ndef ", src_login, re.S)
body = m.group(1) if m else ""
chk("H14 _seconds 里 float() 转换外面有 OverflowError 分支",
    "float(v)" in body and "except OverflowError" in body, body[:220])
chk("H15 认不出的时间仍统一回 None（不是塞 0 或假时间）",
    "return None" in body and "math.isfinite(v)" in body, body[:220])


shutil.rmtree(SB, ignore_errors=True)
print()
print("登录态越界整数体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  越界整数不再把整份登录态带走，能认的值口径不变")
