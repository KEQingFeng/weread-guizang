# -*- coding: utf-8 -*-
"""地址护栏：本机与内网地址一个都别想换身写法溜进来。

剪藏 / 封面 / 内置浏览器 / 平台解析这四条路都「替用户去发请求」，用的是同一道门
（`clip_article.host_blocked`）。这道门最早只按「点分十进制」的正则判主机名，于是
以下几种同样能发到内网的写法全都能绕过去 —— 浏览器和 getaddrinfo 认，正则不认：

    http://2130706433/          纯数字（= 127.0.0.1）
    http://0x7f000001/          十六进制
    http://0177.0.0.1/          八进制
    http://[::ffff:127.0.0.1]/  IPv4 映射的 IPv6
    http://[0:0:0:0:0:0:0:1]/   IPv6 回环的长写法
    http://[::]/                未指定地址
    http://[fc00::1]/           唯一本地地址（内网）

这一套不联网，把「放行 / 拒绝」这两边都钉死：上面这些与常规写法一律拒绝，公网域名
与公网 IP 一律放行，且四条路（fetch / safe_image_url / web_browse.normalize /
web_parse._guard）都真的走这道门，不是各自留一份抄写的正则。
"""
import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-urlguard-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import clip_article
import web_browse
import web_parse  # 只 import，不触发任何网络

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 换身写法也要认出来的那些地址。一种是老式 IPv4 数字写法，一种是 IPv6 的私有 / 特殊段。
BAD_HOSTS = [
    ("localhost", "名字"),
    ("localhost.", "带结尾点的名字"),
    ("127.0.0.1", "常规回环"),
    ("127.1", "少写两段的回环"),
    ("127.0.0.1.", "带结尾点的回环"),
    ("2130706433", "纯数字回环"),
    ("0x7f000001", "十六进制回环"),
    ("0x7f.1", "十六进制 + 少写"),
    ("0177.0.0.1", "八进制回环"),
    ("3232235777", "纯数字家用内网"),
    ("10.0.0.9", "公司内网"),
    ("192.168.1.1", "家用内网"),
    ("172.16.0.1", "内网段起点"),
    ("172.31.255.255", "内网段终点"),
    ("169.254.169.254", "云元数据"),
    ("0.0.0.0", "未指定 IPv4"),
    ("::1", "IPv6 回环"),
    ("[::1]", "方括号包着的 IPv6 回环"),
    ("0:0:0:0:0:0:0:1", "IPv6 回环长写法"),
    ("::ffff:127.0.0.1", "IPv4 映射回环"),
    ("[::ffff:192.168.1.1]", "IPv4 映射家用内网"),
    ("::", "未指定 IPv6"),
    ("[::]", "方括号未指定 IPv6"),
    ("fe80::1", "链路本地"),
    ("fc00::1", "唯一本地"),
    ("fd12:3456::1", "唯一本地"),
    ("100.64.0.1", "运营商级 NAT"),
    ("224.0.0.1", "组播"),
    ("240.0.0.1", "保留段"),
    ("198.51.100.1", "文档用保留段"),
    ("", "空主机名"),
]
GOOD_HOSTS = [
    "example.com", "www.google.com", "ai-bot.cn", "cdn.example.org",
    "1.1.1.1", "8.8.8.8", "172.15.0.1", "172.32.0.1", "11.0.0.1", "a.b.c.d",
]

for host, why in BAD_HOSTS:
    chk("拒掉 %r（%s）" % (host, why), clip_article.host_blocked(host) is True)
for host in GOOD_HOSTS:
    chk("放行公网主机 %s" % host, clip_article.host_blocked(host) is False)

# 名字那把老正则还在（没有为了认 IP 把 localhost 这条顺手弄丢）
chk("localhost 这条名字判据还在", clip_article.BLOCKED_HOSTS.match("localhost") is not None)

# ── 四条路都真的走这道门 ────────────────────────────────────────
for bad, why in [("http://2130706433/x", "剪藏 fetch"),
                 ("http://0x7f000001/", "剪藏 fetch"),
                 ("http://[::ffff:127.0.0.1]/", "剪藏 fetch")]:
    try:
        clip_article.fetch(bad)
        chk("%s 拒掉 %s" % (why, bad), False, "居然发请求了")
    except ValueError as e:
        chk("%s 拒掉 %s（%s）" % (why, bad, str(e)[:40]), True)

for bad in ("http://2130706433/a.jpg", "http://0177.0.0.1/a.jpg",
            "http://[::ffff:192.168.1.1]/a.jpg", "http://[fc00::1]/a.jpg"):
    chk("封面地址 safe_image_url 拒掉 %s" % bad,
        clip_article.safe_image_url(bad) is False)
chk("封面地址仍放行公网图",
    clip_article.safe_image_url("https://img.example.com/a.jpg") is True)

for bad in ("http://2130706433/", "http://[::ffff:127.0.0.1]/",
            "http://[0:0:0:0:0:0:0:1]/", "http://0177.0.0.1/"):
    try:
        out = web_browse.normalize(bad)
        chk("内置浏览器 normalize 拒掉 %s" % bad, False, "居然放行了：%s" % out)
    except web_browse.Blocked as e:
        chk("内置浏览器 normalize 拒掉 %s（%s）" % (bad, str(e)[:30]), True)
chk("内置浏览器仍放行公网地址",
    web_browse.normalize("ai-bot.cn") == "https://ai-bot.cn")

for bad in ("http://2130706433/", "http://[::ffff:10.0.0.1]/", "http://[::]/"):
    try:
        web_parse._guard(bad)
        chk("平台解析 _guard 拒掉 %s" % bad, False, "居然放行了")
    except ValueError as e:
        chk("平台解析 _guard 拒掉 %s（%s）" % (bad, str(e)[:30]), True)

# 跳转那道闸：外站 302 到内网的换身写法也要在发出去之前拦下
guard = web_browse._RedirectGuard()
for bad in ("http://2130706433/", "http://[::ffff:127.0.0.1]/"):
    try:
        guard.redirect_request(None, None, 302, "Found", {}, bad)
        chk("跳转护栏拒掉 %s" % bad, False, "居然跟着跳了")
    except urllib.error.HTTPError as e:
        chk("跳转护栏拒掉 %s（%s）" % (bad, str(e)[:30]), True)

bad = [c for c in checks if not c[0]]
print("\n地址护栏体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
