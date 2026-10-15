#!/usr/bin/env python3
"""油管在内置浏览器里得有一页能看的东西（1.2.1 · 用户报「打开无法显示 / 依旧是哔哩哔哩」）。

根因两段，都不在「取回来」这一步（首页 950KB 照样几秒就回来了）：

  ① youtube.com 是纯客户端应用 —— 回来的 HTML 只有 <ytd-app> 一副空壳，正文全在
     `var ytInitialData = {…}` 那坨 JSON 里，等站点自己的脚本现渲染。
  ② 我们的 iframe 故意不给 allow-same-origin（不让外站读到归藏的 localStorage），
     在浏览器看来就是不透明源：脚本一碰本地存储就抛 SecurityError，那段引导脚本当场
     断在这里 —— 壳也渲染不出来，于是整屏空白。

放宽 sandbox 是拿用户的界面换一页视频，不做。改成自己读那坨 JSON、铺自己的一页静态
目录：卡片、频道、时长、缩略图都是油管给的原文，「看列表 → 挑一支 → 把链接送去转笔记」
三件事全在，缺的只有播放器本身（流式媒体这一层从来也不代理）。

这一套全程离线：喂合成的 ytInitialData / ytInitialPlayerResponse，不起服务也不联网。

  A is_youtube    —— 只认油管那几台站，别的站一律走原来那条原样重送的路。
  B _json_after   —— 按配对扫 JSON：字符串里那半个花括号不许把它劈断。
  C _yt_cards     —— 新旧两代 renderer 都认（videoRenderer / lockupViewModel）。
  D 不进脏东西     —— id 要形状对、缩略图只认自家图床、文字全转义。
  E yt_static     —— watch 把主角置顶；认不出卡片也给一页能搜的壳，不留空白。
  F 「转成笔记」   —— 绝对地址 + data-gz-use，界面接得住（vidUseLink）。
  G shape         —— 油管这一路换成静态页；非油管不受影响。
  H 不残留上一页   —— 那层「正在取回」必须是不透明的一整块，隔着它看不见旧页面。
"""
import json
import pathlib
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-yt-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import web_browse as wb  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = (REPO / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:320]))


def page(payload, marker="ytInitialData", extra=""):
    """照油管的样子把一坨 JSON 塞进 <script> 里。"""
    return ("<html><body><script>var %s = %s;%s</script></body></html>"
            % (marker, json.dumps(payload, ensure_ascii=False), extra))


def card(vid, title, channel, length, when, thumb_w=(168, 336)):
    """一枚旧代 videoRenderer，缩略图故意「糊的在前、清的在后」（油管就这么排）。"""
    host = "https://i.ytimg.com/vi/%s" % vid
    return {"videoRenderer": {
        "videoId": vid,
        "title": {"runs": [{"text": title}]},
        "ownerText": {"runs": [{"text": channel}]},
        "lengthText": {"simpleText": length},
        "publishedTimeText": {"simpleText": when},
        "thumbnail": {"thumbnails": [
            {"url": "%s/hqdefault.jpg" % host, "width": thumb_w[0], "height": 94},
            {"url": "%s/mqdefault.jpg" % host, "width": thumb_w[1], "height": 188},
        ]}}}


RESULTS = "https://www.youtube.com/results?search_query=%E8%BD%AC%E5%86%99"
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# ── A 只认油管那几台 ────────────────────────────────────────────
for u in ("https://www.youtube.com/watch?v=x1", "https://youtu.be/x1",
          "https://m.youtube.com/", "https://music.youtube.com/",
          "https://www.youtube.com/results"):
    chk("认得 %s 是油管" % u, wb.is_youtube(u) is True)
for u in ("https://bilibili.com/", "https://you.tube/", "https://youtube.com.evil.cn/x",
          "", None):
    chk("不许把 %r 当油管（别的站走原路）" % (u,), wb.is_youtube(u) is False)
chk("非油管的页面原样重送：yt_static 直接回 None",
    wb.yt_static("<html><body>hi</body></html>", "https://bilibili.com/") is None)

# ── B 按配对扫 JSON ────────────────────────────────────────────
BRIDGY = page({"items": [{"title": {"runs": [{"text": '一支有 } 和 { 和 " 的标题'}]}},
                          card("abcdef12345", "第二支", "某频道", "8:12", "3 天前")],
                "after": {"ok": True}})
got = wb._json_after(BRIDGY, "ytInitialData")
chk("字符串里那半个花括号没把 JSON 劈断（贪婪正则会栽在这）",
    isinstance(got, dict) and got.get("after", {}).get("ok") is True, got)
chk("marker 不在时回 None，不抛", wb._json_after("<html>x</html>", "ytInitialData") is None)
chk("坏 JSON 不炸（回 None 走原路）",
    wb._json_after("var ytInitialData = {oops", "ytInitialData") is None)

# ── C 两代 renderer 都认 ───────────────────────────────────────
legacy = page({"section": {"contents": [card("AAA1112223", "第一支", "甲频道", "12:34", "2 周前"),
                                         card("BBB2223334", "第二支", "乙频道", "1:02:03", "昨天")]}})
cards = wb._yt_cards(wb._json_after(legacy, "ytInitialData"))
chk("旧代 videoRenderer 认得出两支", len(cards) == 2, cards)
chk("标题 / 频道 / 时长 / 多久前都收了进来",
    cards and cards[0]["title"] == "第一支" and cards[0]["channel"] == "甲频道"
    and cards[0]["length"] == "12:34" and cards[0]["when"] == "2 周前", cards[:1])
chk("缩略图挑最宽那张（照第一个取会拿到糊的）",
    cards and cards[0]["thumb"].endswith("mqdefault.jpg"), cards[:1])

def lockup(vid, title, channel, when, length, thumb):
    """一枚新代 lockupViewModel（watch 页的「相关」、频道页用的是这一代）。

    字段路径与旧代完全不同：标题在 metadata.lockupMetadataViewModel.title.content，
    频道与时间在 metadataRows 的那几排 parts 里，时长藏在封面图的 overlay 徽章里。
    """
    return {"lockupViewModel": {
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": vid,
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": channel}}]},
                {"metadataParts": [{"text": {"content": when}}]}]}}}},
        "contentImage": {"thumbnailViewModel": {
            "image": {"sources": [{"url": thumb, "width": 720, "height": 404}]},
            "overlays": [{"thumbnailBottomOverlayViewModel": {"badges": [
                {"thumbnailBadgeViewModel": {"text": length}}]}}]}}}}


# 名字也会以字符串形式出现在页面配置里 —— 只收 dict 值，别把字符串当 renderer
newgen = page({"a": "lockupViewModel",
               "deep": {"wrap": {"nested": [lockup(
                   "CCC3334445", "新代的一支", "丙频道", "5 个月前", "17:44",
                   "https://i.ytimg.com/vi/CCC3334445/hq720.jpg")]}}})
ncards = wb._yt_cards(wb._json_after(newgen, "ytInitialData"))
chk("新代 lockupViewModel 也认得出（埋在几层包装里都认）",
    len(ncards) == 1 and ncards[0]["title"] == "新代的一支", ncards)
chk("新代那条的频道 / 时间 / 时长各归各位",
    ncards and ncards[0]["channel"] == "丙频道" and ncards[0]["when"] == "5 个月前"
    and ncards[0]["length"] == "17:44", ncards[:1])
chk("「lockupViewModel」以字符串出现时没有被当成 renderer（不炸就是证据）",
    isinstance(ncards, list), ncards)

# ── D 脏东西进不来 ─────────────────────────────────────────────
DIRTY = page({"x": [card('"><script>alert(1)</script>', "坏 id", "丙", "1:00", "现在"),
                    card("short", "id 太短", "丁", "1:00", "现在"),
                    {"videoRenderer": {"videoId": "GOODID12345",
                                       "title": {"runs": [{"text": "坏图床"}]},
                                       "thumbnail": {"thumbnails": [
                                           {"url": "https://evil.cn/a.jpg", "width": 9999},
                                           {"url": "http://i.ytimg.com/vi/x/a.jpg", "width": 500},
                                           {"url": "https://i.ytimg.com/vi/x/b.jpg", "width": 480}]}}}]})
dcards = wb._yt_cards(wb._json_after(DIRTY, "ytInitialData"))
chk("形状不对的 videoId 不收（脚本注入也进不了这一页）",
    all(c["id"] == "GOODID12345" for c in dcards) and len(dcards) == 1, dcards)
chk("缩略图只认自家图床、且只走 https",
    dcards and dcards[0]["thumb"] == "https://i.ytimg.com/vi/x/b.jpg", dcards[:1])
chk("_yt_img 一张都不合用时回空串（不硬引外来图）",
    wb._yt_img([{"url": "https://evil.cn/a.jpg", "width": 9999}]) == ""
    and wb._yt_img([]) == "" and wb._yt_img(None) == "")
html = wb.yt_static(page({"z": [card("QQQQ11112222", "<b>尖括号</b> & 引号\"", "丁频道", "2:00", "今天")]}),
                    RESULTS)
chk("标题里的尖括号与引号都被转义（不变成标签）",
    "&lt;b&gt;" in html and "引号&quot;" in html and "<b>尖括号</b>" not in html, html[:200])
chk("时长换算成人看得懂的（watch 那页只有秒数）",
    wb._yt_len(3845) == "1:04:05" and wb._yt_len(65) == "1:05", (wb._yt_len(3845), wb._yt_len(65)))
chk("秒数缺失 / 为 0 时不硬编一个 0:00 上去",
    wb._yt_len(None) == "" and wb._yt_len(0) == "" and wb._yt_len("x") == "")

MANY = page({"z": [card("V%06d" % i, "第 %d 支" % i, "批量", "3:00", "今天") for i in range(60)]})
mcards = wb._yt_cards(wb._json_after(MANY, "ytInitialData"))
chk("一次回几十本时切到 24 张（这一屏不许堆成长隧道）",
    len(mcards) == wb._YT_CARDS == 24, len(mcards))

# ── E watch 把主角放最前 / 认不出也给一页能搜的壳 ───────────────
PLAYER = json.dumps({"videoDetails": {"videoId": "dQw4w9WgXcQ",
                                      "title": "一首老歌 · 官方音频",
                                      "author": "某频道",
                                      "lengthSeconds": "217",
                                      "thumbnail": {"thumbnails": [
                                          {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                                           "width": 480, "height": 360}]}}}, ensure_ascii=False)
watch_html = wb.yt_static(page({"r": [card("RELATED999", "相关的一支", "戊频道", "9:09", "3 天前")]},
                               extra="var ytInitialPlayerResponse = %s;" % PLAYER), WATCH)
chk("watch 那一页把主角提到最前（只列「相关」等于没答到点上）",
    "card hero" in watch_html and watch_html.index("一首老歌") < watch_html.index("相关的一支"),
    watch_html[:200])
chk("主角的时长从秒数换算出来了（3:37）", "3:37" in watch_html, watch_html[:300])
SHELL = wb.yt_static(page({"only": "这一页什么都认不出"}), "https://www.youtube.com/")
chk("首页那种「认不出卡片」的页也给一页话，不留空白",
    "跑不动" in SHELL and "先在上面搜" in SHELL, SHELL[:300])
chk("这一页有搜索框（搜到的结果就能挑）",
    'name="search_query"' in SHELL and 'action="https://www.youtube.com/results"' in SHELL)
chk("静态页讲清了「为什么是这一页」（不让人以为是坏掉的油管）",
    "为什么是这一页" in SHELL and "不透明源" in SHELL)
chk("认不出卡片时那句「没认出能列的视频」在（不是空网格）",
    "没认出能列的视频" in SHELL, SHELL[-400:])

# ── F 「转成笔记」那颗：链接回界面，不是再取一页 ──────────────────
chk("每张卡都挂了那颗「转成笔记」", "转成笔记" in html and html.count("转成笔记") >= 2)
chk("它带 data-gz-use（走的是「交给界面办事」那一路）", 'data-gz-use' in html)
chk("它的 href 是油管绝对地址（不是相对路径落到我们的 /api/browse 上）",
    'href="https://www.youtube.com/watch?v=QQQQ11112222" data-gz-use' in html, html[:300])
use = wb._LINK_SCRIPT
chk("注入的那段脚本认得 data-gz-use：回传 gzUseLink 而不是 gzBrowse",
    "data-gz-use" in use and "gzUseLink" in use, use[:400])
chk("界面收得到 gzUseLink，并把它交给视频那一屏",
    "d.gzUseLink" in UI and "vidUseLink(d.gzUseLink)" in UI)
chk("vidUseLink 只做「送去视频屏 + 填进这行 + 当场开跑」这一件事",
    re.search(r"function vidUseLink\(url\)\s*\{.*?setView\('video'\).*?VIDEO\.ta\.value = url"
              r".*?vidStart\(\);\s*\}", UI, re.S) is not None)
chk("非 http(s) 的地址不接（iframe 里的东西不可全信）",
    "/^https?:\\/\\//i.test(url)" in UI)

# ── G shape 这一路确实换成了静态页 ─────────────────────────────
r = wb.shape(page({"z": [card("SHAPETEST1", "走 shape 的一支", "己频道", "4:04", "今天")]}
                  ).encode("utf-8"), "text/html", "utf-8", RESULTS)
body = r["body"].decode("utf-8")
chk("shape() 出来的就是那页静态目录（ok 且带卡片）",
    r["ok"] and "YouTube · 静态目录" in body and "走 shape 的一支" in body, body[:200])
chk("静态页也照旧补了 <base> 与那段回传脚本",
    "<base href=" in body and "gzUseLink" in body)
plain = wb.shape(b"<html><body>\xe5\x95\xb5\xe7\xab\x99</body></html>", "text/html", "utf-8",
                 "https://www.bilibili.com/video/BV1xx")
chk("非油管的 HTML 没被换成我们那一页",
    "YouTube · 静态目录" not in plain["body"].decode("utf-8"))

# ── H 不残留上一页：那层提示必须是不透明的 ───────────────────────
css = re.search(r"\.bzload\{([^}]*)\}", UI)
chk("「正在取回」这一层还在", bool(css), css)
chk("它铺的是不透明白（隔着它不该再看见旧页面）",
    bool(css) and "background:var(--paper)" in css.group(1) and "rgba" not in css.group(1),
    css.group(1) if css else "")
chk("它压得住 iframe（z-index 在场且不是负的）",
    bool(css) and re.search(r"z-index:\s*[1-9]\d*\b", css.group(1)) is not None,
    css.group(1) if css else "")
chk("取的时候接住点击（别让人点到底下那张旧页）",
    bool(css) and "pointer-events:auto" in css.group(1), css.group(1) if css else "")
off = re.search(r"\.bzload\.off\{([^}]*)\}", UI)
chk("收掉时有 off 态：淡出且不挡点击",
    bool(off) and "opacity:0" in off.group(1) and "pointer-events:none" in off.group(1),
    off.group(1) if off else "")
chk("加载那一句提了「静态目录」（用户知道这不是坏掉的油管）",
    "静态目录" in (re.search(r'<div class="bzload".*?</div>', UI, re.S).group(0)
                   if re.search(r'<div class="bzload".*?</div>', UI, re.S) else ""),
    UI[UI.find('id="bzLoad"'):UI.find('id="bzLoad"') + 400])

bad = [c for c in checks if not c[0]]
print("\n油管静态目录体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
