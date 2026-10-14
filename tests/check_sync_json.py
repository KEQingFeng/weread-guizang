# -*- coding: utf-8 -*-
"""云同步：**从网络那头收回来的 JSON 必须是对象** —— 不是就给人话，不许把连接甩掉。

sync.py 是唯一一条「替用户敲别人家门」的通路，它读的几段 JSON 全都不由我们定形状：
微软的设备码 / 令牌端点、Graph 的上传会话，中间还可能夹一层反向代理或门户认证页
（回的是 HTML，不是 JSON），接口报错时也可能回一段 JSON 数组。旧读法一律

    j = json.loads(r.read().decode("utf-8", "replace"))
    ... j.get("user_code") ...

于是两种最常见的坏响应各有各的炸法：

  · 回一段 HTML（代理页 / 网关错误页）→ `json.loads` 当场 JSONDecodeError；
  · 回一个 JSON 数组 / 字符串 / 数字 → `.get` 当场 AttributeError；
  · `interval` / `expires_in` 写脏（`"abc"`）→ `int(...)` 当场 ValueError。

`/api/sync/onedrive/start` 与 `/api/sync/onedrive/poll` 这两条 POST **没有任何兜底**，
异常穿出 `do_POST` 就是连接被掐，界面上是「点了授权没反应」——与 check_post_length /
check_query_args 立下的那条规矩同一个症状。

收口是 sync._json_obj：解不出 / 不是对象都回空 dict，调用方各自按「没拿到某个字段」
说人话。这条门禁把四段读口（device_start / device_poll / _ms_token / _ms_put_large）
逐一喂坏响应，要求：不抛、坏响应不当成功、正常响应一个字不改；顺带钉住
「没给访问令牌时不许清掉旧的 refresh_token」。全程用假的 `_http` 顶掉真实请求，
不联网、不碰用户真实 cache/ 与书库，所以归 --fast 静态段。
"""
import io
import json
import os
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync as S  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


class FakeResponse:
    def __init__(self, data=b""):
        self._data = data

    def read(self, *_a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class Net:
    """假的 `_http`：不联网，按预置的 body / 错误回一段响应，并记下每次调用。"""

    def __init__(self, reply=b"", error=None):
        self.reply = reply
        self.error = error          # (code, body)
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=60):
        self.calls.append({"method": method, "url": url, "data": data,
                           "headers": headers or {}})
        if self.error is not None:
            code, body = self.error
            raise urllib.error.HTTPError(url, code, "err", None, io.BytesIO(body or b""))
        return FakeResponse(self.reply)


def od_cfg(**od):
    base = {"client_id": "cid", "refresh_token": "old-rt",
            "access_token": "old-at", "access_exp": S._now() + 3600}
    base.update(od)
    return S._norm({"enabled": True, "provider": "onedrive", "onedrive": base})


ORIG = S._http
try:
    # ── A. _json_obj：网络 JSON 的收口本身 ─────────────────────────
    chk("_json_obj：正常对象原样收下（一个字段不改）",
        S._json_obj(b'{"a": 1, "b": "x"}') == {"a": 1, "b": "x"})
    chk("_json_obj：JSON 数组 / 字符串 / 数字 / null 一律当空 dict",
        S._json_obj(b"[]") == {} and S._json_obj(b'"s"') == {}
        and S._json_obj(b"3") == {} and S._json_obj(b"null") == {})
    chk("_json_obj：HTML / 空 body / 半截 JSON 都不抛，回空 dict",
        S._json_obj(b"<html>proxy</html>") == {}
        and S._json_obj(b"") == {} and S._json_obj(b'{"a":') == {})
    chk("_json_obj：坏字节不抛（errors=replace）", S._json_obj(b'\xff\xfe{"a":1}') in ({}, {"a": 1}))

    # ── B. device_start：申请设备码 ────────────────────────────────
    cfg = od_cfg()
    S._http = Net(reply=b"<html>captive portal</html>")
    ok, msg, info = S.device_start(cfg)
    chk("device_start：回 HTML → 不抛，回人话 + 不给 info（原来 JSONDecodeError 甩连接）",
        ok is False and info is None and "失败" in msg, (ok, msg, info))

    S._http = Net(reply=b"[]")
    ok, msg, info = S.device_start(cfg)
    chk("device_start：回 JSON 数组 → 不抛（原来 .get 当场 AttributeError）",
        ok is False and info is None, (ok, msg, info))

    # 合法 JSON 但没有设备码：不能照回 ok=True（界面会存下一条永远等不到的码）
    S._http = Net(reply=json.dumps({"error": "invalid_client"}).encode())
    ok, msg, info = S.device_start(cfg)
    chk("device_start：有对象没设备码 → 不当成功，把对方的话带出来",
        ok is False and info is None and "invalid_client" in msg, (ok, msg))

    S._http = Net(reply=json.dumps(
        {"user_code": "ABCD", "device_code": "dev",
         "verification_uri": "https://microsoft.com/devicelogin",
         "interval": "abc", "expires_in": 1e400}).encode())
    ok, msg, info = S.device_start(cfg)
    chk("device_start：好响应照走，脏 interval / 越界 expires_in 退回默认",
        ok is True and info["device_code"] == "dev" and info["user_code"] == "ABCD"
        and info["interval"] == 5 and info["expires_in"] == 900, (ok, info))

    S._http = Net(reply=json.dumps(
        {"user_code": "ABCD", "device_code": "dev", "interval": 2}).encode())
    ok, msg, info = S.device_start(cfg)
    chk("device_start：正常的 interval / 缺省 expires_in 不被误伤",
        ok is True and info["interval"] == 2 and info["expires_in"] == 900, (ok, info))

    # ── C. device_poll：等用户点确认 ───────────────────────────────
    for body in (b"<html>bad gateway</html>", b"[]", b"{}"):
        S._http = Net(reply=body)
        status, msg, upd = S.device_poll(cfg, "dev")
        chk("device_poll：坏响应 %r → 不抛、不当成功" % body[:12],
            status != "ok" and upd is None, (status, msg, upd))

    S._http = Net(error=(400, b"<html>gateway</html>"))
    status, msg, upd = S.device_poll(cfg, "dev")
    chk("device_poll：报错体不是 JSON → 不抛，回 error + 人话（原来 JSONDecodeError）",
        status == "error" and upd is None and msg, (status, msg, upd))

    for err, expect in (("authorization_pending", "pending"),
                        ("slow_down", "pending"),
                        ("expired_token", "expired")):
        S._http = Net(error=(400, json.dumps({"error": err}).encode()))
        status, msg, upd = S.device_poll(cfg, "dev")
        chk("device_poll：%s → %s（回归，没点确认不该报错）" % (err, expect),
            status == expect and upd is None, (status, msg))

    S._http = Net(reply=json.dumps(
        {"access_token": "new-at", "refresh_token": "new-rt",
         "expires_in": 7200}).encode())
    status, msg, upd = S.device_poll(od_cfg(), "dev")
    chk("device_poll：拿到令牌算成，令牌与有效期都写回",
        status == "ok" and upd["onedrive"]["access_token"] == "new-at"
        and upd["onedrive"]["refresh_token"] == "new-rt"
        and upd["onedrive"]["access_exp"] > S._now() + 600, (status, upd["onedrive"]))

    # 微软不一定每次都给新的 refresh_token：没给就留着旧的那枚
    S._http = Net(reply=json.dumps({"access_token": "only-at"}).encode())
    status, msg, upd = S.device_poll(od_cfg(refresh_token="keep-me", access_exp=0), "dev")
    chk("device_poll：回包里没有 refresh_token 时，旧的那枚一个字不动",
        status == "ok" and upd["onedrive"]["refresh_token"] == "keep-me",
        (status, upd["onedrive"]))

    S._http = Net(reply=json.dumps(
        {"access_token": "a", "expires_in": "abc"}).encode())
    status, msg, upd = S.device_poll(od_cfg(), "dev")
    chk("device_poll：脏 expires_in 退回一小时默认（原来 int('abc') 当场抛）",
        status == "ok" and upd["onedrive"]["access_exp"] > S._now() + 3000,
        (status, upd["onedrive"]["access_exp"] - S._now()))

    # ── D. _ms_token：拿令牌 / 刷新令牌 ────────────────────────────
    for body in (b"<html>proxy</html>", b"[]", b"{}"):
        c = od_cfg(access_exp=0)
        S._http = Net(reply=body)
        try:
            S._ms_token(c)
            raised = None
        except Exception as e:                       # noqa: BLE001
            raised = e
        chk("_ms_token：坏响应 %r → 抛的是人话 RuntimeError，不是 JSON/属性异常" % body[:12],
            isinstance(raised, RuntimeError) and "令牌刷新失败" in str(raised),
            raised)

    c = od_cfg(access_exp=0, refresh_token="keep-me")
    S._http = Net(reply=json.dumps({"access_token": "fresh", "expires_in": 1e400}).encode())
    tok = S._ms_token(c)
    chk("_ms_token：好响应照走，脏/越界 expires_in 退回默认，refresh_token 不丢",
        tok == "fresh" and c["onedrive"]["refresh_token"] == "keep-me"
        and c["onedrive"]["access_exp"] > S._now() + 3000, (tok, c["onedrive"]))

    # ── E. _ms_put_large：大文件上传会话 ──────────────────────────
    c = od_cfg()
    for body in (b"<html>", b"[]", b'{"id": "x"}'):
        S._http = Net(reply=body)
        try:
            S._ms_put_large(c, "books/a.bin", b"x" * 16)
            raised = None
        except Exception as e:                       # noqa: BLE001
            raised = e
        chk("_ms_put_large：坏会话响应 %r → 人话 RuntimeError（原来 JSON/属性异常）" % body[:8],
            isinstance(raised, RuntimeError) and "上传地址" in str(raised), raised)

    c = od_cfg()
    S._http = Net(reply=json.dumps({"uploadUrl": "https://upload.example/up"}).encode())
    S._ms_put_large(c, "books/a.bin", b"x" * 16)
    puts = [x for x in S._http.calls if x["method"] == "PUT"]
    chk("_ms_put_large：有 uploadUrl 时照旧分块 PUT（别把好的挡回去）",
        len(puts) == 1 and puts[0]["url"] == "https://upload.example/up", puts)

    # ── F. 接线：那几段网络 JSON 都走 _json_obj，不再有裸 json.loads(...).get ──
    SRC = open(os.path.join(ROOT, "sync.py"), encoding="utf-8").read()
    chk("sync.py：四处网络 JSON 读口都走 _json_obj",
        SRC.count("_json_obj(") >= 5, SRC.count("_json_obj("))
    chk("sync.py：不再有「json.loads(...).get」那种裸读法",
        "json.loads(r.read()" not in SRC and "json.loads(e.read()" not in SRC)
    chk("sync.py：_json_obj 只有一处定义（一把尺子）",
        SRC.count("def _json_obj(") == 1)
finally:
    S._http = ORIG

print()
bad = list(FAIL)
print("云同步网络 JSON 收口体检：通过 %d 项，失败 %d 项" % (PASSED, len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
