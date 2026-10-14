# -*- coding: utf-8 -*-
"""地址护栏的「反面输入」：认不出的数值主机名不许把护栏本身打抛。

上一条 `check_url_guard.py` 钉的是「换身写法的内网地址不许溜进来」，这一条钉的是
另一面 —— 护栏对**根本没打算认**的输入也得只回放行 / 拒绝两个值，绝不能抛。

为什么会漏
    `_legacy_ipv4()` 把 `-1.0.0.1` 这种主机名的每一段交给 `int()`：负数照样收得下，
    按位拼出来的也是个负数（`-1.0.0.1` -> `-16777215`），最后原样交给
    `ipaddress.ip_address()` —— 那个函数只认 0 .. 2**32-1，于是当场抛 `ValueError`。

漏在哪儿
    `host_blocked()` 是四条替用户发请求的路共用的一道门（剪藏 fetch、封面
    safe_image_url、内置浏览器 normalize、平台解析 _guard）。它一抛，调用方的
    `except Blocked` / `except ValueError` 就接不住：

      · `web_browse.browse()` 只兜 `Blocked`，于是内置浏览器里敲一个 `http://-1.0.0.1/`
        不是回一页人话，而是把连接甩掉（前端拿到 RemoteDisconnected）；
      · `pick_cover()` 对页面里写死的 `og:image` 直接调 `safe_image_url()`，一篇正文里
        带这种图地址的文章会把整次剪藏带崩。

这一套不联网：只打纯函数与四条路的入口，`--fast` 也得带上。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-urlguard-neg-")
os.environ.setdefault("GUIZANG_DATA", os.path.join(ROOT, "cache"))
os.environ.setdefault("GUIZANG_BOOKS", os.path.join(ROOT, "books"))

import clip_article
import web_browse
import web_parse  # 只 import，不触发任何网络

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 认不出的数值主机名：负号段、连写、超长数字、走不通的进制。这些都不是 inet_aton
# 认的 IPv4，`_legacy_ipv4` 该回 None，而不是回一个负数让后面炸开。
BAD_NUMERIC = [
    ("-1.0.0.1", "回环加负号"),
    ("1.-2.3.4", "中间一段是负的"),
    ("1.0.0.-1", "尾段是负的"),
    ("-5", "单段负数"),
    ("-0x7f000001", "负号的十六进制"),
    ("--1", "两个负号"),
    ("-", "只有一个负号"),
    ("-0177.0.0.1", "负号的八进制回环"),
    ("1.0.0.-4294967295", "尾段负的大数"),
    ("9" * 5000, "超长数字串（撞 int 位数上限）"),
    ("1_0.0.0.1", "下划线当分隔"),
]

for host, why in BAD_NUMERIC:
    try:
        got = clip_article.host_blocked(host)
    except Exception as e:                       # noqa: BLE001 —— 就是来找它抛不抛
        chk("host_blocked(%r) 不抛（%s）" % (host if len(host) < 24 else host[:21] + "…", why),
            False, "%s: %s" % (type(e).__name__, e))
        continue
    chk("host_blocked(%r) 只回布尔（%s）" % (host if len(host) < 24 else host[:21] + "…", why),
        isinstance(got, bool), repr(got))

# 根因那一条：负号段本来就不该被当成老式 IPv4 收下。
chk("_legacy_ipv4 不把负数段当老式 IPv4",
    clip_article._legacy_ipv4("-1.0.0.1") is None
    and clip_article._legacy_ipv4("1.0.0.-1") is None
    and clip_article._legacy_ipv4("-5") is None,
    [clip_article._legacy_ipv4(x) for x in ("-1.0.0.1", "1.0.0.-1", "-5")])

# 收口没把尺子改松：内网的换身写法照旧拒，公网照旧放。
REGRESS_BAD = ["127.0.0.1", "10.0.0.9", "192.168.1.1", "2130706433",
               "0x7f000001", "0177.0.0.1", "::1", "[::ffff:127.0.0.1]", "localhost."]
for host in REGRESS_BAD:
    chk("内网换身写法照旧拒 %r" % host, clip_article.host_blocked(host) is True)
for host in ["example.com", "1.1.1.1", "8.8.8.8", "172.15.0.1"]:
    chk("公网照旧放行 %r" % host, clip_article.host_blocked(host) is False)

# 四条路真的都过这道门，而不是各自留一份抄写的判断。
try:
    got = clip_article.safe_image_url("http://-1.0.0.1/a.jpg")
    chk("封面 safe_image_url 对负数主机名只回布尔、不抛", isinstance(got, bool), repr(got))
except Exception as e:                           # noqa: BLE001
    chk("封面 safe_image_url 对负数主机名只回布尔、不抛", False,
        "%s: %s" % (type(e).__name__, e))

# 内置浏览器：这条路只兜 Blocked。负数主机名不是内网、不会被拦，所以该照常收成一条
# 能发的 URL；退一步说，就算将来决定拦，也得是 Blocked（人话），不能是裸 ValueError。
try:
    out = web_browse.normalize("http://-1.0.0.1/")
    chk("内置浏览器 normalize 对负数主机名不抛裸异常", isinstance(out, str), repr(out))
except web_browse.Blocked as e:
    chk("内置浏览器 normalize 拦成 Blocked 也算对（%s）" % str(e)[:30], True)
except Exception as e:                           # noqa: BLE001
    chk("内置浏览器 normalize 对负数主机名不抛裸异常", False,
        "%s: %s" % (type(e).__name__, e))

# 平台解析：负数主机名不是内网，_guard 该放它过（继续往下走由解析层失败兜底），
# 而不是抛一个 ipaddress 的 ValueError 出来。
try:
    web_parse._guard("http://-1.0.0.1/")
    chk("平台解析 _guard 对负数主机名不抛", True)
except Exception as e:                           # noqa: BLE001
    chk("平台解析 _guard 对负数主机名不抛", False, "%s: %s" % (type(e).__name__, e))

# 封面挑选：页面里的 og:image 写出这种地址时，整次剪藏不能被它带走。
try:
    clip_article.pick_cover("http://-1.0.0.1/cover.jpg", "正文", "http://example.com/a")
    chk("pick_cover 对负数 og:image 不抛", True)
except Exception as e:                           # noqa: BLE001
    chk("pick_cover 对负数 og:image 不抛", False, "%s: %s" % (type(e).__name__, e))

bad = [c for c in checks if not c[0]]
print("\n地址护栏反面输入体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
