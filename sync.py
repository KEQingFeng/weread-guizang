"""云同步 / 云储存 —— WebDAV 与 OneDrive 两条通道，共用一套「放上去 / 拿回来」。

设计取舍先说清楚：

- **只用标准库**。整包不引第三方依赖，同步也不例外：WebDAV 就是 HTTP 的
  PUT/GET/MKCOL（Basic 认证），OneDrive 走 Microsoft Graph 的 REST，用
  urllib 都够。代价是要自己写一点点 OAuth 和设备码流程，但换来的是
  「装上就能用、不额外装东西」。

- **同步三样，各自可勾**：本机阅读时间、阅读记录（书架状态与划线索引）、
  整本书库文件。前两样是小 JSON，随便传；书库可能很大，默认不开，由用户勾。

- **不删除**：无论哪一端，只做「补齐」不做「清理」。远端有本地没有的就拉下来，
  本地有远端没有的就推上去。同一个文件都有的，比大小与修改时间，取新的那份。
  这样最坏情况是留了多余文件，绝不会因为同步把谁的笔记删没了。

- **密钥不出本机**：WebDAV 密码、OneDrive 令牌都只存在本机 config.json，
  回给界面的状态里永远只带「有没有填」和尾巴，不带值。
"""

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 远端根目录：所有东西放在它下面，不污染用户云盘里已有的文件
REMOTE_ROOT = "guizang"

# OneDrive 设备码流程要的端点（common 让个人/工作账号都能用）
GRAPH = "https://graph.microsoft.com/v1.0"
MS_DEVICECODE = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MS_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
# AppFolder 权限：只碰本应用自己的文件夹，看不到用户云盘别的东西 —— 请求得多、风险小
MS_SCOPE = "Files.ReadWrite.AppFolder offline_access"

UA = "guizang-sync/1.0"


def _now():
    return int(time.time())


# ---------------------------------------------------------------- 配置

def _s(v):
    """只认字符串，别的类型一律当空。配置是从界面上收回来的，可能夹带数字/列表，
    这里统一兜住，免得后面 .strip() 一声不吭把连接搞崩。"""
    return v.strip() if isinstance(v, str) else ""


def _obj(v):
    """取子配置：不是 dict 就退回空 dict（同样为挡住畸形输入）。"""
    return v if isinstance(v, dict) else {}


def _int(v):
    try:
        return int(v)
    except Exception:
        return 0


def _json_obj(blob):
    """网络那头回的一段 JSON，收成一个对象；不是 JSON / 不是对象就给空 dict，绝不抛。

    这些响应全都来自这台机器之外，形状不由我们定：微软的令牌与 Graph 接口、
    中间还可能夹一层反向代理或门户认证页（回的是 HTML，不是 JSON）；接口报错时
    也可能回一段 JSON 数组。原来的读法一律 `json.loads(...).get(...)`：
    前者当场 `JSONDecodeError`、后者 `AttributeError` —— 而 `/api/sync/onedrive/start`
    与 `/poll` 两条 POST 没有任何兜底，异常穿出 `do_POST` 的结果是连接被掐，
    界面上就是「点了授权没反应」。收口放在这一处，调用方各自按「没拿到某个字段」
    说人话（谁该报错、谁该退回默认，由那条路自己定）。
    """
    try:
        data = json.loads(blob.decode("utf-8", "replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _norm(cfg):
    """把外部传进来的配置补成完整结构，缺什么补默认，别让调用方到处判空。"""
    cfg = _obj(cfg)
    out = {
        "enabled": bool(cfg.get("enabled")),
        "provider": _s(cfg.get("provider")),
        "webdav": {},
        "onedrive": {},
        "what": {"time": True, "records": True, "books": False},
        "last": {"at": 0, "msg": ""},
    }
    wd = _obj(cfg.get("webdav"))
    out["webdav"] = {"url": _s(wd.get("url")).rstrip("/"),
                     "user": _s(wd.get("user")),
                     "pass": _s(wd.get("pass"))}
    od = _obj(cfg.get("onedrive"))
    out["onedrive"] = {"client_id": _s(od.get("client_id")),
                       "refresh_token": _s(od.get("refresh_token")),
                       "access_token": _s(od.get("access_token")),
                       "access_exp": _int(od.get("access_exp"))}
    what = _obj(cfg.get("what"))
    for k in ("time", "records", "books"):
        if k in what:
            out["what"][k] = bool(what[k])
    last = _obj(cfg.get("last"))
    out["last"] = {"at": _int(last.get("at")), "msg": str(last.get("msg") or "")[:200]}
    return out


def _absorb_tokens(dst, src):
    """把这次刷新出来的 OneDrive 令牌写回调用方那份配置。

    `_norm` 每次都新建一份 dict，于是 `_ms_token` 换来的 access_token /
    refresh_token 只活在这次运行的副本里。界面那半边的写法是「跑完把归一化的
    配置存回磁盘」（ui_server.sync_run_now），不写回就等于**把旧令牌原样盖回
    去**：下一次同步又得重新换一遍，而微软会轮换 refresh_token —— 旧的那枚
    迟早失效，用户看到的是「明明授权过，过阵子又要重新授权」。

    只有 src（归一化那份）里非空的令牌才覆盖 dst，所以没刷新时一个字都不动。
    """
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return
    s = src.get("onedrive")
    if not isinstance(s, dict):
        return
    # 只有真的换到令牌（access_token 非空）才动 dst：否则等于把 `_norm` 补的
    # 空壳写回去，本来没有 onedrive 那块的配置会凭空多出一个空块。
    fresh = {}
    if s.get("access_token"):
        fresh["access_token"] = s["access_token"]
        fresh["access_exp"] = s.get("access_exp") or 0
    if s.get("refresh_token"):
        fresh["refresh_token"] = s["refresh_token"]
    if not fresh:
        return
    d = dst.get("onedrive")
    if not isinstance(d, dict):
        d = {}
        dst["onedrive"] = d
    d.update(fresh)


def state(cfg):
    """给页面的那份状态。密码/令牌只回「填没填」，不回值。"""
    c = _norm(cfg)
    return {
        "enabled": c["enabled"],
        "provider": c["provider"],
        "webdav": {"url": c["webdav"]["url"], "user": c["webdav"]["user"],
                   "pass_set": bool(c["webdav"]["pass"])},
        "onedrive": {"client_set": bool(c["onedrive"]["client_id"]),
                     "authed": bool(c["onedrive"]["refresh_token"])},
        "what": c["what"],
        "last": c["last"],
    }


def save(old, incoming):
    """把界面上传的配置合进原来的。返回 (ok, 人话, 新配置)。

    空口令/空令牌 = 「不改」，与 Agent 那边的 Key 一个规矩：
    界面上那栏永远只有占位符，用户只改别的字段时不该被逼着重输密码。
    """
    c = _norm(old)
    inc = _obj(incoming)
    if "enabled" in inc:
        c["enabled"] = bool(inc["enabled"])
    if "provider" in inc:
        p = _s(inc.get("provider"))
        c["provider"] = p if p in ("webdav", "onedrive") else ""
    iwd = _obj(inc.get("webdav"))
    for k in ("url", "user"):
        if k in iwd:
            v = _s(iwd.get(k))
            c["webdav"][k] = v.rstrip("/") if k == "url" else v
    if _s(iwd.get("pass")):
        c["webdav"]["pass"] = _s(iwd.get("pass"))
    iod = _obj(inc.get("onedrive"))
    if "client_id" in iod:
        c["onedrive"]["client_id"] = _s(iod.get("client_id"))
    iwhat = _obj(inc.get("what"))
    for k in ("time", "records", "books"):
        if k in iwhat:
            c["what"][k] = bool(iwhat[k])

    if c["enabled"]:
        if not c["provider"]:
            return False, "先选一个云服务：WebDAV 或 OneDrive", c
        if c["provider"] == "webdav":
            if not c["webdav"]["url"]:
                return False, "WebDAV 还差一个地址", c
            if not re.match(r"^https?://", c["webdav"]["url"]):
                return False, "WebDAV 地址要以 http:// 或 https:// 开头", c
        if c["provider"] == "onedrive" and not c["onedrive"]["client_id"]:
            return False, "OneDrive 还差一个应用 ID（见下方说明）", c
    return True, "已保存", c


# ---------------------------------------------------------------- WebDAV 通道

def _basic(user, pwd):
    raw = ("%s:%s" % (user or "", pwd or "")).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _http(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    req.add_header("User-Agent", UA)
    return urllib.request.urlopen(req, timeout=timeout)


def _dav_headers(c):
    h = {"Authorization": _basic(c["webdav"]["user"], c["webdav"]["pass"])}
    return h


def _dav_mkdir(c, path):
    """逐级建目录。目录已存在会回 405，不是错，直接过。

    `cur` 不许带前导斜杠：地址本来就要在 base 后面补一个 `/`，再拼
    `/guizang` 就成了 `…/files/u//guizang`。双斜杠多数服务器会当另一个
    路径（404 / 409），而不是当同一个目录 —— 于是「测一下」第一下就报
    「建目录失败」，而按同一份配置 PUT 上来的文件落在 `…/files/u/guizang/…`
    （`_dav_put` 拼的是没有前导斜杠的完整路径），两边根本不是同一个位置。
    """
    parts = [p for p in path.split("/") if p]
    cur = ""
    for p in parts:
        cur = p if not cur else cur + "/" + p
        url = c["webdav"]["url"] + "/" + urllib.parse.quote(cur)
        try:
            _http("MKCOL", url, headers=_dav_headers(c), timeout=30).read()
        except urllib.error.HTTPError as e:
            if e.code in (405, 301):     # 已存在 / 已重定向到目录
                continue
            if e.code in (401, 403):
                raise RuntimeError("WebDAV 拒绝：用户名或密码不对")
            # 409 一般是父目录还没建好；上层目录本就在循环里先建，不该到这儿
            raise RuntimeError("WebDAV 建目录失败（%s）：%s" % (e.code, e.reason))


def _dav_put(c, path, blob):
    url = c["webdav"]["url"] + "/" + urllib.parse.quote(path)
    h = _dav_headers(c)
    h["Content-Type"] = "application/octet-stream"
    with _http("PUT", url, data=blob, headers=h, timeout=180) as r:
        r.read()


def _dav_get(c, path):
    url = c["webdav"]["url"] + "/" + urllib.parse.quote(path)
    try:
        with _http("GET", url, headers=_dav_headers(c), timeout=180) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _dav_test(c):
    _dav_mkdir(c, REMOTE_ROOT)
    return True, "WebDAV 通了"


# ---------------------------------------------------------------- OneDrive 通道

def _ms_token(c):
    """拿一个能用的 access_token：没过期就用缓存的，过期了用 refresh_token 换。"""
    od = c["onedrive"]
    if od["access_token"] and od["access_exp"] > _now() + 60:
        return od["access_token"]
    if not od["refresh_token"]:
        raise RuntimeError("OneDrive 还没授权（先在设置里点「授权」）")
    body = urllib.parse.urlencode({
        "client_id": od["client_id"],
        "grant_type": "refresh_token",
        "refresh_token": od["refresh_token"],
        "scope": MS_SCOPE,
    }).encode()
    try:
        with _http("POST", MS_TOKEN, data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   timeout=30) as r:
            j = _json_obj(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("OneDrive 令牌刷新失败：%s" % e.read().decode("utf-8", "replace")[:160])
    # 回了个对象却没带令牌（代理页面 / 半截响应）：以前会把 access_token 设成空串、
    # 照常返回来，下一次请求带着 "Bearer " 去撞 401，报错理由离现场很远。
    # 这里当场说清楚；旧 refresh_token 一个字不动（微软不一定每次都给新的）。
    if not j.get("access_token"):
        raise RuntimeError("OneDrive 令牌刷新失败：那边没给访问令牌")
    od["access_token"] = j["access_token"]
    od["access_exp"] = _now() + (_int(j.get("expires_in")) or 3600)
    if j.get("refresh_token"):
        od["refresh_token"] = j["refresh_token"]
    return od["access_token"]


def _ms_headers(c):
    return {"Authorization": "Bearer " + _ms_token(c)}


def _ms_put(c, path, blob):
    # AppFolder 根 = approot，直接把相对路径接在后面
    url = "%s/me/drive/special/approot:/%s:/content" % (GRAPH, urllib.parse.quote(path))
    h = _ms_headers(c)
    h["Content-Type"] = "application/octet-stream"
    # Graph 简单上传上限 4MB，超过要开上传会话；超了就交给会话那条路
    if len(blob) > 4 * 1024 * 1024:
        return _ms_put_large(c, path, blob)
    with _http("PUT", url, data=blob, headers=h, timeout=300) as r:
        r.read()


def _ms_put_large(c, path, blob):
    """大文件走上传会话，分块传。600 万字节一块，稳。"""
    sess_url = "%s/me/drive/special/approot:/%s:/createUploadSession" % (
        GRAPH, urllib.parse.quote(path))
    body = json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode()
    h = _ms_headers(c)
    h["Content-Type"] = "application/json"
    with _http("POST", sess_url, data=body, headers=h, timeout=60) as r:
        upload = _json_obj(r.read()).get("uploadUrl")
    if not upload:
        raise RuntimeError("OneDrive 没给上传地址")
    chunk = 6 * 1024 * 1024
    total = len(blob)
    pos = 0
    while pos < total:
        piece = blob[pos:pos + chunk]
        end = pos + len(piece) - 1
        hh = {"Content-Length": str(len(piece)),
              "Content-Range": "bytes %d-%d/%d" % (pos, end, total)}
        with _http("PUT", upload, data=piece, headers=hh, timeout=300) as r:
            r.read()
        pos += len(piece)


def _ms_get(c, path):
    url = "%s/me/drive/special/approot:/%s:/content" % (GRAPH, urllib.parse.quote(path))
    try:
        with _http("GET", url, headers=_ms_headers(c), timeout=300) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _ms_test(c):
    url = "%s/me/drive/special/approot" % GRAPH
    try:
        with _http("GET", url, headers=_ms_headers(c), timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # app 文件夹还没建过也算通 —— 首次写入会自动建
            return True, "OneDrive 通了"
        raise
    return True, "OneDrive 通了"


# ---------------------------------------------------------------- 设备码授权

def device_start(cfg):
    """第一步：跟微软要一个「设备码 + 用户码」，让用户去网页上输。"""
    c = _norm(cfg)
    if not c["onedrive"]["client_id"]:
        return False, "先填 OneDrive 应用 ID", None
    body = urllib.parse.urlencode({"client_id": c["onedrive"]["client_id"],
                                   "scope": MS_SCOPE}).encode()
    try:
        with _http("POST", MS_DEVICECODE, data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   timeout=30) as r:
            j = _json_obj(r.read())
    except urllib.error.HTTPError as e:
        return False, "申请设备码失败：%s" % e.read().decode("utf-8", "replace")[:200], None
    # 没拿到设备码就当申请失败：以前会照回 ok=True + device_code=None，界面存下这条
    # 「等待中的码」，用户输完代码点确定，后端只回一句「没在等授权」。
    if not j.get("device_code"):
        return False, ("申请设备码失败：%s"
                       % (j.get("error_description") or j.get("error")
                          or "那边没给设备码（可能被代理页面拦了）")), None
    return True, "去这个网址输入代码完成授权", {
        "user_code": j.get("user_code"),
        "verify_uri": j.get("verification_uri") or j.get("verification_uri_complete"),
        "device_code": j.get("device_code"),
        # 认不出的间隔 / 有效期退回默认：`int("abc")` 会当场抛（见 sync._int）
        "interval": _int(j.get("interval")) or 5,
        "expires_in": _int(j.get("expires_in")) or 900,
    }


def device_poll(cfg, device_code):
    """第二步：等用户在网页上点完。拿到 refresh_token 就算成。"""
    c = _norm(cfg)
    body = urllib.parse.urlencode({
        "client_id": c["onedrive"]["client_id"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
    }).encode()
    try:
        with _http("POST", MS_TOKEN, data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   timeout=30) as r:
            j = _json_obj(r.read())
    except urllib.error.HTTPError as e:
        j = _json_obj(e.read())
        err = j.get("error")
        if err == "authorization_pending":
            return "pending", "还没点确认…", None
        if err == "slow_down":
            return "pending", "慢一点…", None
        if err == "expired_token":
            return "expired", "代码过期了，重新申请", None
        return "error", j.get("error_description") or err or "授权失败", None
    # 回了个对象却没带访问令牌：代理页面 / 半截响应。绝不能当成功 —— 界面会清掉
    # 等待中的设备码，用户看到「授权成功」，下一次同步却还是没授权。
    if not j.get("access_token"):
        return "error", (j.get("error_description") or j.get("error")
                         or "那边没给访问令牌，再试一次"), None
    od = c["onedrive"]
    od["access_token"] = j["access_token"]
    od["access_exp"] = _now() + (_int(j.get("expires_in")) or 3600)
    # 微软不一定每次都回新的 refresh_token：没给就留着旧的那枚，别把它抹成空串
    if j.get("refresh_token"):
        od["refresh_token"] = j["refresh_token"]
    return "ok", "授权成功", c


# ---------------------------------------------------------------- 需要同步的文件

# 「还在写」的中间后缀。书库目录里会出现两种半截文件：别的模块正在写
# （写作稿 / 画板 / 导图一律先写 .tmp 再改名，插图下载先写 .part），
# 以及同步自己上一次取回断线留下的。两种都不是成品，一个字节都不该上云。
PART_SUFFIXES = (".part", ".tmp")


def _finished_file(name):
    """这个文件名算不算已经写完 —— 中间产物（.part / .tmp）不算。"""
    return not (name or "").lower().endswith(PART_SUFFIXES)


def _collect(cache_dir, books_dir, what):
    """按勾选，把「本地这边有哪些文件要同步」列出来：[(远端相对路径, 本地绝对路径)]。

    远端路径统一放在 guizang/ 下，分 cache/ 与 books/ 两支。
    """
    pairs = []
    if what.get("time"):
        pairs.append((REMOTE_ROOT + "/cache/readstat.json",
                      os.path.join(cache_dir, "readstat.json")))
    if what.get("records"):
        for name in ("library.json", "notes_index.json"):
            pairs.append((REMOTE_ROOT + "/cache/" + name,
                          os.path.join(cache_dir, name)))
    if what.get("books") and os.path.isdir(books_dir):
        for root, _dirs, files in os.walk(books_dir):
            for fn in files:
                if not _finished_file(fn):
                    continue
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, books_dir)
                pairs.append((REMOTE_ROOT + "/books/" + rel.replace(os.sep, "/"), fp))
    return pairs


# 同进程里两只手同时取同一个文件时，共用那把固定临时件的闸。
#
# 为什么够得着：`/api/sync/now` 由 `ThreadingHTTPServer` 的请求线程直接处理，
# 两个窗口 / 两个标签页各点一次「立即同步」就是两只手（界面那一次只锁住自己那颗
# 按钮，锁不住另一扇窗）。临时件名是固定的 `<目标>.part`，两个写者会打开同一个
# 件：先收尾的那个 `os.replace` 把它搬去目标，后一个当场 FileNotFoundError ——
# 界面看到「失败 1」，其实那份文件已经好好取回来了；更坏的是两只手都还开着这个
# 件时，后到的写入会落在已经改名的目标上，把刚取回的那份写成两份内容的混血。
#
# 临时件名为什么仍是固定的 `<目标>.part`、不换成那一族 pid + 随机串：`cleanup`
# 那一刀（`_residue_re`）正是按 `<账本>.part` 这个形状认同步留下的半截账，
# 改名会让「清除本地数据」当场瞎掉。所以这里收的是另一半 —— 用一把锁把
# 「写临时件 + 改名」串起来，名字与残件形状一个字不动。
_ATOMIC_LOCK = threading.Lock()


def _write_atomic(path, blob):
    """取回来的字节先写同目录 .part，写完再一次性改名到目标位置。

    为什么非这样不可：本地没有这份文件时同步才去远端取。若直接
    `open(path, "wb")` 写一半就断了（网卡、盘满、进程被杀），目标位置会留下一个
    长度不对却「存在」的文件；下一次同步的规则是「本地有就上传」，于是这个半截件
    被传回云端，把好的那份盖掉 —— 一次断线变成永久损坏。中间文件先落在目标旁边，
    改名是同一文件系统上的原子操作：要么旧内容完整、要么新内容完整，没有第三种。

    `_ATOMIC_LOCK` 罩住的是「写临时件 + 改名」这一小段：临时件名是固定的，两个
    写者同时进来会互相搬走对方的件（见上面那段注释）。锁只串行化落盘，同步的
    其余部分（取远端字节、上传）照旧并发。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".part"
    with _ATOMIC_LOCK:
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def _remote_dir(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


# ---------------------------------------------------------------- 跑一次同步

def run(cfg, cache_dir, books_dir, log=None):
    """跑一次同步。返回 (ok, 汇总人话)。

    规则：本地有、远端就传上去；本地没有、远端有就拉下来。只增不删。
    每个文件都包一层 try —— 一个文件失败不该让整趟同步断掉，
    最后汇总里会数清楚成功了几个、失败几个。
    """
    c = _norm(cfg)
    if not c["enabled"] or not c["provider"]:
        return False, "还没开云同步"
    say = log or (lambda *_: None)
    pairs = _collect(cache_dir, books_dir, c["what"])
    if not pairs:
        return False, "没有勾选任何要同步的内容"

    up = down = fail = 0
    first_err = ""
    made_dirs = set()
    for remote, local in pairs:
        try:
            blob = None
            if os.path.isfile(local):
                with open(local, "rb") as f:
                    blob = f.read()
            if c["provider"] == "webdav":
                rdir = _remote_dir(remote)
                if rdir not in made_dirs:
                    _dav_mkdir(c, rdir)
                    made_dirs.add(rdir)
                if blob is not None:
                    _dav_put(c, remote, blob)
                    up += 1
                    continue
                got = _dav_get(c, remote)
            else:
                if blob is not None:
                    _ms_put(c, remote, blob)
                    up += 1
                    continue
                got = _ms_get(c, remote)
            # 走到这儿＝本地没有、只能从远端拉
            if got is not None:
                _write_atomic(local, got)
                down += 1
        except Exception as e:
            fail += 1
            if not first_err:
                first_err = str(e)[:160]
            say("同步跳过 %s：%s" % (remote, str(e)[:120]))
    # 传文件时可能顺手换过令牌（`_ms_token` 在副本上刷新）：写回调用方那份，
    # 界面接着 save_cfg 才能把新令牌落盘，而不是把旧的盖回去。
    _absorb_tokens(cfg, c)
    parts = []
    if up:
        parts.append("上传 %d" % up)
    if down:
        parts.append("取回 %d" % down)
    if fail:
        parts.append("失败 %d" % fail)
    msg = ("、".join(parts) or "没有变化")
    if fail and first_err:
        msg += "（%s）" % first_err
    return (fail == 0), msg


def test(cfg):
    c = _norm(cfg)
    if not c["provider"]:
        return False, "先选一个云服务"
    try:
        if c["provider"] == "webdav":
            return _dav_test(c)
        return _ms_test(c)
    except urllib.error.HTTPError as e:
        return False, "连不上（%s %s）" % (e.code, e.reason)
    except Exception as e:
        return False, str(e)[:200]
    finally:
        # 测一下也可能顺手换过令牌，同样要写回调用方那份（否则下次又白换一遍）。
        _absorb_tokens(cfg, c)
