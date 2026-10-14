# -*- coding: utf-8 -*-
"""云同步：OneDrive 换来的令牌有没有写回「调用方那份配置」。

为什么单独一份：上面那套 check_sync.py 量的是配置合并、URL 拼法与一次跑的收尾。
这一条只量一个容易悄悄坏、坏了很久都看不出来的点 —— **令牌回写**。

`_norm` 每次都新建一份 dict（这是对的，它要挡住畸形输入），`_ms_token` 换来的
access_token / refresh_token 也就只活在这次运行的副本里。而界面那半边是
`cfg = cloudsync._norm(...)` → `cloudsync.run(cfg, ...)` → `save_cfg(cfg)`：
不写回，跑完存回磁盘的就是**旧令牌**。后果有两个，都不是报错，是「越用越不对」：

  1. 每次同步都重新换一遍令牌 —— 白白多一次 POST，还拖慢启动；
  2. 微软会轮换 refresh_token，旧的那枚迟早失效，用户看到的是
     「明明授权过，过阵子又要重新授权」。

假 `_http` 记账顶掉真实请求，所以「有没有换令牌、换来的值有没有回到原配置」当场
断言得出来，全程不联网。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync as S  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


ORIG_HTTP = S._http


class FakeResponse:
    def __init__(self, data=b""):
        self._data = data

    def read(self, *_a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class Transport:
    """记账的假 HTTP：不联网，记下每次 (方法, URL)，可预置远端文件与错误。"""

    def __init__(self):
        self.calls = []
        self.remote = {}
        self.reply = {}          # url -> bytes（POST 回一段 JSON 用它）
        self.raise_on = {}       # url -> (code, body)

    def __call__(self, method, url, data=None, headers=None, timeout=60):
        self.calls.append((method, url, headers or {}))
        if url in self.raise_on:
            code, body = self.raise_on[url]
            raise urllib.error.HTTPError(url, code, "err", None, io.BytesIO(body or b""))
        if url in self.reply:
            return FakeResponse(self.reply[url])
        if method == "PUT":
            self.remote[url] = data
            return FakeResponse()
        if method == "GET":
            if url in self.remote:
                return FakeResponse(self.remote[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b""))
        return FakeResponse()

    def count(self, method, url=None):
        return len([1 for m, u, _h in self.calls
                    if m == method and (url is None or u == url)])

    def clear(self):
        self.calls = []


TMP = tempfile.mkdtemp(prefix="guizang-sync-tok-")
CACHE = os.path.join(TMP, "cache")
BOOKS = os.path.join(TMP, "books")
os.makedirs(CACHE)
os.makedirs(BOOKS)
READSTAT = os.path.join(CACHE, "readstat.json")
with open(READSTAT, "w", encoding="utf-8") as f:
    f.write("{}")

TOKEN_BODY = json.dumps({"access_token": "at-new", "refresh_token": "rt-new",
                         "expires_in": 3600}).encode()


def od_cfg(access_exp, refresh_token="rt-old", access_token="at-old"):
    """界面那份配置：开着 OneDrive、只同步阅读时长。"""
    return {"enabled": True, "provider": "onedrive",
            "what": {"time": True, "records": False, "books": False},
            "onedrive": {"client_id": "cid", "access_token": access_token,
                         "refresh_token": refresh_token, "access_exp": access_exp}}


# ── 基线：_norm 不碰调用方那份（这正是问题成立的前提） ──────────────
raw = od_cfg(S._now() + 3600)
snap = json.loads(json.dumps(raw))
S._norm(raw)
chk("前提：_norm 只读不改，调用方那份配置原封不动", raw == snap, raw)


# ── 1. 令牌过期 → 换新的，且写回调用方那份配置 ─────────────────────
tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = TOKEN_BODY
cfg = od_cfg(S._now() - 10)          # 已经过期
ok, msg = S.run(cfg, CACHE, BOOKS)
chk("过期令牌：真的发了一次换令牌请求",
    tr.count("POST", S.MS_TOKEN) == 1, tr.calls)
chk("过期令牌：access_token 写回了原配置",
    cfg["onedrive"]["access_token"] == "at-new", cfg["onedrive"])
chk("过期令牌：轮换后的 refresh_token 也写回了（否则旧的那枚迟早失效）",
    cfg["onedrive"]["refresh_token"] == "rt-new", cfg["onedrive"])
chk("过期令牌：access_exp 写回的是未来时间（下次不用再换）",
    isinstance(cfg["onedrive"]["access_exp"], int)
    and cfg["onedrive"]["access_exp"] > S._now() + 60, cfg["onedrive"])

# 最要紧的一条行为：拿写回后的配置再跑一次，不该再换令牌。
tr2 = Transport()
S._http = tr2
tr2.reply[S.MS_TOKEN] = TOKEN_BODY
ok2, msg2 = S.run(cfg, CACHE, BOOKS)
chk("写回之后第二次同步用缓存令牌，不再打令牌端点",
    ok2 and tr2.count("POST", S.MS_TOKEN) == 0, (ok2, msg2, tr2.calls))
chk("第二次同步仍然把文件传上去了（不是靠跳过整个流程换来「没换令牌」）",
    tr2.count("PUT") == 1, tr2.calls)


# ── 2. 令牌没过期 → 一个字都不许动调用方那份 ───────────────────────
tr = Transport()
S._http = tr
cfg = od_cfg(S._now() + 3600)
snap = json.loads(json.dumps(cfg))
S.run(cfg, CACHE, BOOKS)
chk("令牌有效时：不换令牌（0 次 POST）", tr.count("POST") == 0, tr.calls)
chk("令牌有效时：原配置一字未动", cfg == snap, cfg)


# ── 3. 换令牌失败 → 旧的 refresh_token 不许被抹掉 ─────────────────
tr = Transport()
S._http = tr
tr.raise_on[S.MS_TOKEN] = (400, b'{"error":"invalid_grant"}')
cfg = od_cfg(S._now() - 10)
ok, msg = S.run(cfg, CACHE, BOOKS)
chk("换令牌失败：这次同步算失败，且汇总里带上原因", not ok and "失败" in msg, (ok, msg))
chk("换令牌失败：旧的 refresh_token 还在（不许被清空逼用户重授权）",
    cfg["onedrive"]["refresh_token"] == "rt-old", cfg["onedrive"])
chk("换令牌失败：也没凭空塞一个假的 access_token",
    cfg["onedrive"]["access_token"] == "at-old", cfg["onedrive"])


# ── 4. 先换成了、后面传文件才失败 → 换来的令牌照样要保住 ───────────
tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = TOKEN_BODY
put_url = ("%s/me/drive/special/approot:/%s:/content"
           % (S.GRAPH, "guizang/cache/readstat.json"))
tr.raise_on[put_url] = (500, b"")
cfg = od_cfg(S._now() - 10)
ok, msg = S.run(cfg, CACHE, BOOKS)
chk("上传失败但令牌换成了：新令牌仍然写回（不因后半段失败而丢）",
    not ok and cfg["onedrive"]["access_token"] == "at-new"
    and cfg["onedrive"]["refresh_token"] == "rt-new", (ok, msg, cfg["onedrive"]))


# ── 5. test() 这条「测一下」的路也一样 ─────────────────────────────
tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = TOKEN_BODY
cfg = od_cfg(S._now() - 10)
S.test(cfg)
chk("test()：测一下顺手换的令牌也写回", cfg["onedrive"]["access_token"] == "at-new"
    and cfg["onedrive"]["refresh_token"] == "rt-new", cfg["onedrive"])


# ── 6. 兜底与边界 ─────────────────────────────────────────────────
cfg = {"enabled": True, "provider": "webdav",
       "webdav": {"url": "https://dav.example.com/x", "user": "u", "pass": "p"},
       "what": {"time": True, "records": False, "books": False}}
snap = json.loads(json.dumps(cfg))
tr = Transport()
S._http = tr
S.run(cfg, CACHE, BOOKS)
chk("WebDAV 一路：不许被塞进一个空的 onedrive 块", cfg == snap, cfg)

S._absorb_tokens(None, {"onedrive": {"access_token": "x"}})
S._absorb_tokens({"a": 1}, None)
S._absorb_tokens({"a": 1}, {"onedrive": "坏形状"})
chk("_absorb_tokens：非 dict 的任一侧都不炸", True)

src = {"onedrive": {"access_token": "", "refresh_token": "", "access_exp": 0}}
dst = {"a": 1}
S._absorb_tokens(dst, src)
chk("_absorb_tokens：没有可写的令牌时不留空壳", dst == {"a": 1}, dst)

# 界面上存的是归一化那份配置 —— 端到端把 ui_server 那条路模拟一遍：
# 归一化 → run → 存回磁盘 → 重新读出来，令牌必须是新的。
tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = TOKEN_BODY
disk = S._norm(od_cfg(S._now() - 10))        # 界面 load_cfg + _norm 的结果
S.run(disk, CACHE, BOOKS)                    # 界面 sync_run_now
disk["last"] = {"at": S._now(), "msg": "上传 1"}   # 界面跑完记一笔
saved = json.dumps(disk, ensure_ascii=False)       # 界面 save_cfg 落到磁盘
back = S._norm(json.loads(saved))                  # 下次启动读回来
tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = TOKEN_BODY
S.run(back, CACHE, BOOKS)
chk("端到端：存盘再读回来，令牌仍是新的，不用重新授权",
    back["onedrive"]["refresh_token"] == "rt-new"
    and tr.count("POST", S.MS_TOKEN) == 0, (back["onedrive"], tr.calls))

chk("令牌只在配置里流转，state() 回给界面的仍是「填没填」而不是值",
    "at-new" not in json.dumps(S.state(back), ensure_ascii=False)
    and S.state(back)["onedrive"]["authed"] is True, S.state(back))

S._http = ORIG_HTTP
shutil.rmtree(TMP, ignore_errors=True)
print()
print("云同步令牌回写体检：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
