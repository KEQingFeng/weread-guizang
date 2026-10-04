#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 小结真机走查：一个入口、两种模式、三种内容，外加导图、存为笔记与复制。

静态那份（tests/check_ai_sum.py）管的是规则本身；这一份管「屏幕上真点得动吗」：
后端与提示词都验过了，剩下的风险全在接缝上 —— 按钮接的是哪个函数、流式的那一段
有没有真的一段一段出来、切模式有没有换提示词、重开有没有回读缓存而不是再问一次、
画完导图开的是不是刚存的那张、断在半路时屏上留没留字。这些不点不知道。

上游用本机一个假的 OpenAI 兼容服务顶掉（流式走 chunked SSE），于是：
  · 「一次只读当前这一章」是拿请求体里的正文逐字对出来的，不是看界面像不像；
  · 「没配接口不发问」「半路失败不覆盖旧那份」这类承诺都有可失败的断言；
  · 全程不联网，跑完把配置摘干净，别的套件不会看见一个连着的假上游。

前提：run_all.sh 起好的沙盒服务 + seed 铺的 GAPBOOK1（微信读书）与 clip_SE_POST（剪藏）。
"""
import json
import os
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402
from seed import book_dir as where  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = selftest.need_base(1)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")
BOOK = "GAPBOOK1"
CLIP = "clip_SE_POST"
MODEL = "本机假模型"
CH3, CH4 = "0002.md", "0003.md"
TOK3 = "只有第三章有这句甲标"
TOK4 = "只有第四章有这句乙标"
FIRST = "剪藏这篇的第一段独有句"
LAST = "剪藏这篇的最后一段独有句"

BRIEF_PIECES = ["内存分配慢的根因是碎片", "，不是速度。", "先量再改：", "第一页量分配，",
                "第二页量回收，", "结论落在对照表上。"]
LEARN_PIECES = ["## 一句话核心\n", "先量再改，别猜。\n\n", "## 术语对照\n",
                "碎片 → 空隙多到塞不进一块完整的。\n\n", "## 检验你\n",
                "为什么慢？\n我的回答：＿＿＿"]
MAP_JSON = json.dumps({
    "title": "AI 画的那张",
    "root": {"label": "内存管理", "kids": [
        {"label": "碎片", "kids": [{"label": "外部碎片", "kids": []}]},
        {"label": "分配策略", "kids": [{"label": "首次适应", "kids": []}]},
        {"label": "回收", "kids": [{"label": "合并空闲块", "kids": []}]},
        {"label": "度量", "kids": [{"label": "先量再改", "kids": []}]}]},
}, ensure_ascii=False)

STATE = {"mode": "ok", "hits": [], "gap": 0.15}

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf8"))


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf8"))


# ── 假上游：/v1/chat/completions，流式按 chunked 一段一段吐 ──────────
class FakeAI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write_chunk(self, data):
        self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        msgs = body.get("messages") or []
        hit = {"stream": bool(body.get("stream")), "model": body.get("model"),
               "system": msgs[0]["content"] if msgs else "",
               "user": msgs[1]["content"] if len(msgs) > 1 else ""}
        STATE["hits"].append(hit)
        try:
            if STATE["mode"] == "error":
                payload = json.dumps({"error": {"message": "上游今天不干活"}}).encode("utf8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if not hit["stream"]:
                payload = json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": MAP_JSON}}]}).encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            pieces = LEARN_PIECES if "## 检验你" in hit["system"] else BRIEF_PIECES
            if STATE["mode"] == "cut":
                pieces = pieces[:2]
            for piece in pieces:
                self._write_chunk(("data: " + json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": piece}}]}) + "\n\n").encode("utf8"))
                time.sleep(STATE["gap"])
            if STATE["mode"] == "cut":
                # 半路拔线：不给结束块，让客户端读到 IncompleteRead —— 真上游挂掉就这样
                try:
                    self.request.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
                self.close_connection = True
                return
            self._write_chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass    # 用户按了「停下」，这边断了才是对的
        except Exception:
            pass

    def log_message(self, *a):
        pass


def start_fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


JS = """() => {
  const l = document.getElementById('aiSumLayer');
  const body = document.getElementById('aiSumBody');
  const st = (typeof SUM !== 'undefined') ? SUM : {};
  return {
    open: !!(l && l.classList.contains('open')),
    tag: (document.getElementById('aiSumTag') || {}).textContent || '',
    info: (document.getElementById('aiSumInfo') || {}).textContent || '',
    tip: (document.getElementById('aiSumTip') || {}).textContent || '',
    text: body ? body.textContent : '',
    html: body ? body.innerHTML.slice(0, 400) : '',
    h2: body ? body.querySelectorAll('h2').length : -1,
    chips: [...document.querySelectorAll('#aiSumStyle button')].map(b => b.textContent + ':' + b.className),
    wait: !!document.querySelector('#aiSumBody .sumwait'),
    bad: !!document.querySelector('#aiSumBody .summsg.bad'),
    run: (document.getElementById('aiSumRun') || {}).textContent || '',
    mapDisabled: !!(document.getElementById('aiSumMap') || {}).disabled,
    noteDisabled: !!(document.getElementById('aiSumNote') || {}).disabled,
    busy: st.busy, style: st.style, scope: st.scope, module: st.module,
    book: st.book, chapter: st.chapter, sumText: st.text || '',
    overflow: l ? (l.scrollWidth - l.clientWidth) : -1
  };
}"""


def ai_file(book, name):
    return pathlib.Path(where(book)) / "_ai" / name


def note_entries(book):
    """盘上这本书的条目（读到空或读不到都回空表）。"""
    try:
        return (get("/api/mynotes?book=" + book).get("doc") or {}).get("entries") or []
    except Exception:
        return []


def wait_note_entries(book, want):
    """前端存笔记走的是 900ms 静默（ntTouch → ntSave），点完「保存」立刻读一定读到旧的，
    所以这里轮询到条数够了再断言 —— 不然验的是时序不是功能。"""
    got = []
    for _ in range(40):
        got = note_entries(book)
        if len(got) >= want:
            return got
        time.sleep(0.15)
    return got


def wait_stream(pg, want_done=True, limit=90):
    """等屏上的字不再变（或等它落定），顺带把中途看到的长度序列记下来。"""
    lens = []
    for _ in range(limit):
        pg.wait_for_timeout(90)
        s = pg.evaluate(JS)
        lens.append(len(s["sumText"]))
        if not s["busy"] and (not want_done or lens[-1] > 0):
            return s, lens
    return pg.evaluate(JS), lens


def main():
    # 夹具：把第三、第四章各写一句独有话，「只读了当前这一章」才对得出来
    ch_dir = pathlib.Path(where(BOOK)) / "chapters"
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / CH3).write_text("# 第3章 小结试验\n\n%s。\n\n补白一句。\n" % TOK3, encoding="utf-8")
    (ch_dir / CH4).write_text("# 第4章 别的章\n\n%s。\n" % TOK4, encoding="utf-8")
    cdir = pathlib.Path(where(CLIP)) / "chapters"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "0000.md").write_text("# 剪藏一篇\n\n%s。\n\n中间。\n\n%s。\n" % (FIRST, LAST),
                                  encoding="utf-8")

    srv, fake_url = start_fake()
    cfg = post("/api/action", {"action": "agent.save", "url": fake_url, "key": "",
                               "model": MODEL})
    chk("把上游指到假服务（配置走的就是设置页那一个口）",
        cfg.get("ok") is True and cfg.get("agent", {}).get("model") == MODEL, cfg)

    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch(args=["--no-sandbox"])
            pg = br.new_page(viewport={"width": 1440, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
            pg.goto(BASE + "/", wait_until="domcontentloaded")
            pg.evaluate("() => localStorage.clear()")
            pg.reload(wait_until="domcontentloaded")
            pg.wait_for_timeout(1200)
            pg.evaluate("() => setView('shelf')")
            pg.wait_for_timeout(300)

            # ── 1. 读书：一个入口，只读当前这一章 ─────────────────────
            pg.evaluate("() => openReader('%s', '小结试验本', 'shelf')" % BOOK)
            pg.wait_for_selector("#rdBody", timeout=9000)
            pg.wait_for_timeout(900)
            pg.evaluate("() => rdGo(2)")
            pg.wait_for_timeout(600)
            chk("阅读页顶栏有「小结」这一枚（三种内容共用一个入口）",
                pg.evaluate("() => !!document.getElementById('rdSum')"))
            STATE["hits"].clear()
            pg.click("#rdSum")
            pg.wait_for_timeout(120)
            mid = pg.evaluate(JS)
            chk("点小结开的是这一层，不是跳走一个新视图", mid["open"], mid)
            done, lens = wait_stream(pg)
            chk("流式是一段一段吐到屏上的（一屏一次到位就是假流）",
                len(set(l for l in lens if 0 < l < len("".join(BRIEF_PIECES)))) >= 2, lens)
            full = "".join(BRIEF_PIECES)
            chk("吐完的内容整段落在屏上，且与上游给的逐字一致",
                done["sumText"].strip() == full, done["sumText"])
            chk("范围胶囊说的是「这一章」并带上章名",
                done["tag"].startswith("这一章 · ") and "第3章" in done["tag"], done["tag"])
            chk("落定后底部报「已存进这本书的 _ai/」",
                "已存进这本书的 _ai/" in done["info"], done["info"])
            f3 = ai_file(BOOK, "brief-0002.md")
            chk("盘上真有那一份，且就是屏上这段",
                f3.is_file() and f3.read_text(encoding="utf8") == full, f3)
            hit = STATE["hits"][-1]
            chk("喂给模型的只有当前这一章（隔壁章那句独有话不许出现在请求里）",
                TOK3 in hit["user"] and TOK4 not in hit["user"], hit["user"][:200])
            chk("请求带的是「归纳」那套提示词，模型名是设置里那一个",
                "信息编辑" in hit["system"] and hit["model"] == MODEL, hit["model"])
            chk("这一发是流式（stream:true），并且只发了这一发",
                hit["stream"] is True and len(STATE["hits"]) == 1, len(STATE["hits"]))
            pg.screenshot(path=str(selftest.SHOTS / "ai-summary.png"))

            # ── 2. 中途「停下」：不覆盖旧的、屏上留字 ─────────────────
            pg.click("#aiSumRun")            # 重新生成，立刻处于 busy
            pg.wait_for_timeout(200)
            busy = pg.evaluate(JS)
            chk("正在吐的时候那颗钮写着「停下」", busy["run"] == "停下" and busy["busy"], busy)
            pg.click("#aiSumRun")            # 再点就是停
            pg.wait_for_timeout(300)
            stopped, _ = wait_stream(pg, want_done=False, limit=30)
            chk("点停下之后不再在吐（busy 落回 false）", stopped["busy"] is False, stopped)
            chk("停下时报一句「这次没落档」", "停了" in stopped["info"], stopped["info"])
            chk("半路停下的内容没写盘（存的是问完的那一份）",
                f3.read_text(encoding="utf8") == full, f3.read_text(encoding="utf8")[:60])

            # ── 3. 换模式：提示词跟着换，另存一份 ─────────────────────
            STATE["hits"].clear()
            pg.click('#aiSumStyle button[data-k="learn"]')
            done2, _ = wait_stream(pg)
            learn = "".join(LEARN_PIECES)
            chk("切到学习理解后重新问了一发（两种模式各存一份）",
                len(STATE["hits"]) == 1 and "## 检验你" in STATE["hits"][-1]["system"],
                len(STATE["hits"]))
            chk("学习理解那份的五个小节真渲染成了标题（Markdown 在流里也照常解析）",
                done2["h2"] >= 3 and "我的回答：＿＿＿" in done2["text"], done2)
            f3l = ai_file(BOOK, "learn-0002.md")
            chk("学习理解单独存一份，不覆盖归纳那份",
                f3l.is_file() and f3l.read_text(encoding="utf8") == learn
                and f3.read_text(encoding="utf8") == full, f3l)
            chk("顶上说明跟着模式换（用户得知道这次问的是哪一种）",
                "拆开讲透" in done2["tip"], done2["tip"])
            pg.screenshot(path=str(selftest.SHOTS / "ai-summary-learn.png"))

            # ── 4. 关掉重开：读缓存，不再问一次 ───────────────────────
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(200)
            chk("Esc 收掉这一层", pg.evaluate(JS)["open"] is False)
            STATE["hits"].clear()
            pg.click("#rdSum")
            pg.wait_for_timeout(500)
            back = pg.evaluate(JS)
            chk("重开直接把上次那份铺上，不再问模型（问一次就烧一次钱）",
                STATE["hits"] == [] and back["open"] and back["sumText"].strip() == learn,
                {"hits": len(STATE["hits"]), "len": len(back["sumText"])})
            chk("重开时模式还记着学习理解（不静默退回默认）", back["style"] == "learn", back)
            pg.click('#aiSumStyle button[data-k="brief"]')
            pg.wait_for_timeout(400)
            chk("切回归纳也是读缓存（两份都在盘上，各归各的）",
                STATE["hits"] == [] and pg.evaluate(JS)["sumText"].strip() == full,
                len(STATE["hits"]))

            # ── 5. 一键导图：画完开的是刚存的那张，还能存 SVG ─────────
            STATE["hits"].clear()
            pg.click("#aiSumMap")
            pg.wait_for_timeout(1200)
            mm_path = pathlib.Path(where(BOOK)) / "mindmap.json"
            chk("导图落进这本书的 mindmap.json", mm_path.is_file(), mm_path)
            doc = json.loads(mm_path.read_text(encoding="utf8")) if mm_path.is_file() else {}
            labels = json.dumps(doc, ensure_ascii=False)
            chk("存的就是 AI 画的那棵树（分支名逐字对得上）",
                "首次适应" in labels and "外部碎片" in labels, labels[:180])
            # 节点抓 #mmNodes 下的 .mmnode、说明抓 #mmTip：这层的空状态卡里也放着
            # 一个 class="mmtip" 的 span，按 class 全文抓会抓到那个没显示的。
            opened = pg.evaluate("""() => ({
              sum: document.getElementById('aiSumLayer').classList.contains('open'),
              map: document.getElementById('ntMapLayer').classList.contains('open'),
              // 节点在 #mmNodes 里（class 是 mmnode），顶上那句说明写在 #mmTip
              nodes: document.querySelectorAll('#mmNodes .mmnode').length,
              tip: (document.getElementById('mmTip') || {}).textContent || '',
              edges: document.querySelectorAll('#mmEdges path').length
            })""")
            chk("画完自动把脑图层打开、小结层收掉（两层同 z-index，不关会盖住）",
                opened["map"] and not opened["sum"], opened)
            chk("脑图里看得见 AI 给的分支", opened["nodes"] >= 4, opened)
            chk("分支之间真连了线（只有方块没连线就是布局没跑起来）",
                opened["edges"] >= 4, opened)
            chk("顶上说明这是 AI 画的一张、原来那张还在 .prev",
                "AI" in opened["tip"] and ".prev" in opened["tip"], opened["tip"])
            pg.screenshot(path=str(selftest.SHOTS / "ai-mindmap.png"))
            got = {"name": None, "path": None}
            try:
                with pg.expect_download(timeout=8000) as dl:
                    pg.click("#ntMapDl")
                d = dl.value
                p = str(selftest.SHOTS / ("ai-map-" + d.suggested_filename))
                d.save_as(p)
                got = {"name": d.suggested_filename, "path": p}
            except Exception as e:
                chk("「存一份 SVG」点得动", False, str(e)[:160])
            svg_ok = got["path"] and os.path.isfile(got["path"]) \
                and os.path.getsize(got["path"]) > 500 \
                and "<svg" in open(got["path"], encoding="utf8", errors="replace").read(400)
            chk("导出的那张 SVG 真落盘、不是空文件", bool(svg_ok), got)
            pg.click("#ntMapClose")
            pg.wait_for_timeout(300)
            pg.click("#rdSum")
            pg.wait_for_timeout(250)
            pg.click("#aiSumMap")
            pg.wait_for_timeout(1200)
            chk("再画一次，上一张换成 .prev 留着（手画的不许点一下就没了）",
                (pathlib.Path(where(BOOK)) / "mindmap.json.prev").is_file())
            pg.click("#ntMapClose")
            pg.wait_for_timeout(300)

            # ── 6. 存为笔记 / 复制 ────────────────────────────────────
            hits_before = len(STATE["hits"])
            pg.click("#rdSum")
            pg.wait_for_timeout(300)
            chk("重新点开小结只回盘上那一份，不再向上游要一遍（要一遍就是花钱重问）",
                len(STATE["hits"]) == hits_before,
                {"before": hits_before, "after": len(STATE["hits"])})
            before = note_entries(BOOK)
            pg.click("#aiSumNote")
            pg.wait_for_timeout(400)
            ed = pg.evaluate("""() => ({
              open: document.getElementById('ntEdLayer').classList.contains('open'),
              title: document.getElementById('ntEdTitle').value,
              body: document.getElementById('ntEdTa').value
            })""")
            chk("存为笔记是把这段填进条目编辑器（用户一般会改两句再存）",
                ed["open"] and ed["body"].strip() == full, ed)
            chk("标题带上模式名，翻笔记时认得出是哪一种小结",
                "归纳小结" in ed["title"], ed["title"])
            pg.click("#ntEdSave")
            # 存盘走的是 900ms 静默（ntTouch 里那个 setTimeout），点完立刻读必然读到旧的。
            after = wait_note_entries(BOOK, len(before) + 1)
            chk("存完笔记里真多了一条", len(after) == len(before) + 1,
                {"before": len(before), "after": len(after)})
            chk("存进去的正文就是屏上那段",
                any((e.get("body") or "").strip() == full for e in after), [e.get("title") for e in after])
            # 「保存」这颗钮自己就把编辑器收了（ntEdSave(false)），这里别再补一发 Esc：
            # 那会儿没有浮窗开着，Esc 会一路冒到阅读器那个处理函数，把人从阅读页踢出去。
            # 剪板 API 在非安全上下文要显式授权，否则 readText 直接 reject —— 授权调错名字
            # （add_permissions 根本没有）会被 except 吞掉，看起来像「复制没生效」。
            try:
                pg.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
            except Exception as e:
                chk("给页面开剪贴板权限", False, str(e)[:120])
            pg.click("#rdSum")
            pg.wait_for_timeout(300)
            pg.click("#aiSumCopy")
            pg.wait_for_timeout(400)
            clip_text = pg.evaluate(
                "async () => { try { return await navigator.clipboard.readText(); }"
                " catch (e) { return 'ERR:' + e.message; } }")
            chk("复制出来的就是这份小结", clip_text.strip() == full, clip_text[:80])

            # ── 7. 剪藏那一类：整篇读 ─────────────────────────────────
            STATE["hits"].clear()
            pg.evaluate("() => setView('shelf')")
            pg.wait_for_timeout(300)
            pg.evaluate("() => openReader('%s', '剪藏的一篇', 'shelf')" % CLIP)
            pg.wait_for_selector("#rdBody", timeout=9000)
            pg.wait_for_timeout(900)
            pg.click("#rdSum")
            done3, _ = wait_stream(pg)
            chk("剪藏这类胶囊写「整篇 · 剪藏的文章」（范围由后端按来路定）",
                done3["tag"].startswith("整篇") and "剪藏" in done3["tag"], done3["tag"])
            h = STATE["hits"][-1]
            chk("整篇读：首段与末段的独有话都进了请求",
                FIRST in h["user"] and LAST in h["user"], h["user"][:200])
            chk("整篇读时提示词按整篇来说", "按整篇来总结" in h["user"], h["user"][:120])
            fw = ai_file(CLIP, "brief-whole.md")
            chk("剪藏那格的 _ai/ 里存了一份（书名旁边，不是全局一个仓库）",
                fw.is_file() and fw.read_text(encoding="utf8") == "".join(BRIEF_PIECES), fw)

            # ── 8. 上游 500：屏上要说人话，不许静默 ────────────────────
            pg.evaluate("() => document.getElementById('aiSumClose').click()")
            pg.wait_for_timeout(200)
            STATE["mode"] = "error"
            pg.evaluate("() => document.getElementById('aiSumLayer').classList.add('open')")
            pg.evaluate("""() => { SUM.text=''; SUM.book='%s'; SUM.chapter='';
                                    SUM.scope='whole'; SUM.module='clip';
                                    sumChips(); sumRun(); }""" % CLIP)
            pg.wait_for_timeout(1500)
            bad = pg.evaluate(JS)
            chk("上游回 500 时，屏上是那句人话（含状态码与上游自己的话）",
                bad["bad"] and "500" in bad["text"] and "上游今天不干活" in bad["text"], bad["text"])
            chk("失败时按钮回到「重新生成」（不能卡在停下）",
                bad["run"] == "重新生成" and bad["busy"] is False, bad)

            # ── 9. 半路断了：已经吐出来的字留着，旧的那份不许被覆盖 ─────
            STATE["mode"] = "cut"
            f0 = ai_file(CLIP, "brief-whole.md")
            keep = f0.read_text(encoding="utf8")
            pg.click("#aiSumRun")
            pg.wait_for_timeout(1600)
            cut = pg.evaluate(JS)
            chk("半路断了，已经吐出来的那几个字还在屏上",
                cut["sumText"].startswith(BRIEF_PIECES[0]), cut["sumText"])
            chk("断的时候底部说一句「这次没存」", "断了" in cut["info"], cut["info"])
            chk("断的不覆盖上一次那份好的", f0.read_text(encoding="utf8") == keep,
                f0.read_text(encoding="utf8")[:40])

            # ── 10. 这层的卫生：溢出、emoji、点外关闭 ──────────────────
            STATE["mode"] = "ok"
            lay = pg.evaluate("""() => {
              const l = document.getElementById('aiSumLayer');
              const t = l.textContent;
              return {ov: l.scrollWidth - l.clientWidth, txt: t.slice(0, 200)};
            }""")
            chk("这一层不横向溢出", lay["ov"] <= 1, lay)
            chk("界面上一个 emoji 都没有",
                not EMOJI.search(pg.evaluate(
                    "() => document.getElementById('aiSumLayer').textContent")))
            # 上一层还开着（第 9 步断流那份留在屏上），而它是 position:fixed 满屏的，
            # 「小结」那颗钮在它底下 —— 直接点必然被拦。先用它自己的关闭钮收掉，
            # 再从入口开一遍：这样下面验的才是「刚开的这一层」，也顺手证明入口没被锁死。
            pg.evaluate("() => document.getElementById('aiSumClose').click()")
            pg.wait_for_timeout(250)
            pg.click("#rdSum")
            pg.wait_for_timeout(400)
            again = pg.evaluate(
                "() => document.getElementById('aiSumLayer').classList.contains('open')")
            chk("收掉之后还能从「小结」这颗钮再开一遍（入口不许被这一层自己锁死）",
                again is True, again)
            # 点外关闭：这几层的判定写的是 e.target === 这一层自己（见 ui.html 的
            # ntFloatsWire），也就是「点到那块暗纱上」才算。往全局的 .veil（设置弹窗的底）
            # 上 dispatch 一个事件根本打不中它 —— 用真鼠标点到面板以外的角落才是用户那一下。
            spot = pg.evaluate("""() => {
              const l = document.getElementById('aiSumLayer');
              const pr = l.querySelector('section').getBoundingClientRect();
              return pr.left > 60 ? {x: 20, y: 20}
                                  : {x: Math.round(pr.right + 12), y: 20};
            }""")
            pg.mouse.click(spot["x"], spot["y"])
            pg.wait_for_timeout(320)
            gone = pg.evaluate(
                "() => document.getElementById('aiSumLayer').classList.contains('open')")
            chk("点面板外面那圈底就把这层收掉（开着关不掉等于把阅读器锁死）",
                gone is False, spot)
            pg.keyboard.press("Escape")
            br.close()
            srv.shutdown()

        chk("全程没有页面报错", not errs, errs[:4])
    finally:
        # 把上游摘干净：别的套件不该看见一个连着的假服务
        try:
            post("/api/action", {"action": "agent.save", "url": "", "key": "", "model": ""})
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass

    bad = [n for ok, n, _ in checks if not ok]
    print("\n通过 %d / %d" % (len(checks) - len(bad), len(checks)))
    if bad:
        print("失败：" + "、".join(bad))
    sys.exit(1 if bad else 0)


main()
