#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机后端的第一道门：挡住「用户随手打开的网页」替他来点归藏的按钮。

为什么「只听 127.0.0.1」不够
    服务绑在回环上，外面的人连不进来；但用户自己打开的那个网页，请求是**从用户
    这台机器上**发出去的 —— 对浏览器来说 127.0.0.1 就是本机，一点都不「外面」。
    一个 POST 只要不是「需要预检」的形状（CORS 简单请求），浏览器就会替那个页面
    把请求原样送到后端；页面读不读得到回包并不重要，「删书 / 清数据 / 起任务」
    这些动作真做成了，本身就是攻击的全部目的。这是本机小服务这一类软件的经典
    漏洞，不是理论上的。

只靠 Content-Type 那道断言漏得掉什么
    `do_POST` 原来只认 `application/json`，它挡住了表单提交和带类型的 fetch。
    但有一种请求浏览器**根本不发 Content-Type**：`fetch(url, {method:'POST',
    body: new Blob([payload])})` —— Blob 不写 type 时没有类型可带。这种请求既
    不需要预检（简单请求）、也不会被那道断言拦下，实测就是 200 照做。这一条
    `tests/check_csrf.py` 里钉着。

所以再加一把尺子，认浏览器自己贴的标签，而不是请求体长什么样：
  · 只审「会改状态」的几个方法（本后端的写操作全在 POST 上）；
  · `Sec-Fetch-Site` 是 `same-origin` / `same-site` / `none` 的放行
    （`none` 是地址栏直接开、书签、Playwright 的 `goto` 这一类）；
  · `Sec-Fetch-Site: cross-site` 的一律拒；
  · 一个 `Sec-Fetch-*` 都没带的，当「不是浏览器」放行 —— curl、MCP 适配器、
    门禁脚本、Playwright 的 APIRequestContext 都跑在这台机器上，拦它们没有意义，
    只会把正经的自动化一起挡在门外（实测 Node 的 fetch 只发 `Sec-Fetch-Mode`、
    不发 `Sec-Fetch-Site`、也不发 `Origin`，所以不会被误伤）；
  · 不发 `Sec-Fetch-*` 的老浏览器（或客户端），那就看 `Origin`：它必须和这次
    请求的 `Host` 同源；对不上、或者干脆是 `Origin: null`（沙箱 iframe 的写法），
    一律拒。

同源怎么比
    把 `Origin` 里的 scheme / host / port 解出来，和请求的 `Host` 头比 host 与 port
    （port 缺省按 scheme 补 80 / 443），host 大小写与末尾点号都归一。比较用 `Host`
    而不是再解一次监听地址，是为了不管用户从 127.0.0.1 还是 localhost 打开界面，
    界面自己的请求都算同源 —— 换一种打开方式不该把自己人挡在门外。

这一层是纯函数：进出都是字符串与 headers，不碰套接字、不看时钟，所以能脱开服务单测。
"""

from urllib.parse import urlsplit

# 会改状态的方法。GET / HEAD / OPTIONS 一个都不在里面：跨源 GET 的回包页面读不到，
# 谈不上「替用户点按钮」；真要写状态的接口，一律走 POST。
MUTATING = ("POST", "PUT", "PATCH", "DELETE")

# 浏览器说「这是我自己的页面 / 用户自己打开的 / 兄弟站点」的三种。`same-site` 也放行：
# 归藏只服务一个源，同站=同源；把这条也拒掉只会在将来同时开两个端口时自己咬自己。
SAFE_SITES = ("same-origin", "same-site", "none")

_DEFAULT_PORT = {"http": 80, "https": 443}


def header(headers, name):
    """取一个头。`http.server` 那份本来就是大小写不敏感的，单元测试直接喂 dict
    时不是 —— 所以 dict 再自己找一遍。"""
    if headers is None:
        return None
    try:
        v = headers.get(name)
    except Exception:
        v = None
    if v is not None:
        return v
    try:
        items = headers.items()
    except Exception:
        return None
    low = str(name).lower()
    for k, val in items:
        if str(k).lower() == low:
            return val
    return None


def _norm_host(v):
    return str(v or "").rstrip(".").lower()


def same_origin(origin, host):
    """`Origin` 与这次请求的 `Host` 是不是同一个源。解不出来 / 对不上都回 False。"""
    try:
        o = urlsplit(str(origin or "").strip())
        scheme = (o.scheme or "").lower()
        if scheme not in _DEFAULT_PORT or not o.hostname:
            return False
        oport = o.port if o.port is not None else _DEFAULT_PORT[scheme]
        h = urlsplit("//" + str(host or "").strip())
        if not h.hostname:
            return False
        hport = h.port if h.port is not None else _DEFAULT_PORT[scheme]
    except ValueError:
        return False
    return _norm_host(o.hostname) == _norm_host(h.hostname) and int(oport) == int(hport)


def cross_site(method, headers, host):
    """这次请求该不该按「别的网站替用户点的」拒掉。回 True 就是拒。"""
    if str(method or "").upper() not in MUTATING:
        return False
    origin = header(headers, "Origin")
    site = str(header(headers, "Sec-Fetch-Site") or "").strip().lower()
    # 不是浏览器发的（没有 Origin、也没有 Site），或浏览器明说这是自己人。
    if origin is None and site in ("",) + SAFE_SITES:
        return False
    if origin is not None and same_origin(origin, host):
        return False
    return True
