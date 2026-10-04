# -*- coding: utf-8 -*-
"""「界面是新的、本机后端是旧的」那条横幅的真机走查。

为什么单开一份：2026-10-04 用户报的三条「画板 / 思维导图 / flomo 导入后端没启动」，
根因不是后端没实现 —— 是端口上挂着一个几周前起来的旧进程。界面每次请求从磁盘现读
（永远是新的），路由表却是进程起来那一刻装进内存的，于是新功能一律 404，而页面当时
只会说一句「本机服务没在跑」，把人带去查一件本来没事的事。横幅是这条根因唯一对用户
露面的地方，所以它得真的做到：认得出旧后端、说清是谁旧、一键换得掉、换不掉时给明白
该干什么、点了「先不管」就别再烦人。

这里只改后端「答的话」，不改它的行为：
  · /api/state 的 version/code 是拦下来改写的（沙盒后端自己跑的就是 1.0.5，问不出 0.9.8）；
  · /api/restart 的三种回执（换成 / 回了原因 / 压根没这条接口 / 连不上）也是造的。
真的换班（旧进程退、接班人绑同一个端口、任务在跑就不动、不是归藏就不碰）
归 tests/test_backend_identity.py —— 那份端到端起真进程，这一份管的是话说得对不对。

检查的事：
  1 版本对得上 → 横幅不出现（不能一进门就喊有问题）；
  2 后端报 0.9.8 → 横幅出现，同时报出两个版本号，且不出现那句假话「本机服务没在跑」；
  3 这时候打一个旧后端没有的接口 → gzApi 认成 stale，文案与横幅同一把尺；
  4 版本对得上时打不存在的接口 → 说的是「认不得这条接口」，仍然给一键；
  5 「先不管」→ 横幅收起来，之后几轮轮询都不再冒出来；
  6 点「换新后端」后端回了原因（任务在跑）→ 用的是它那句原因，而且 2.6 秒后
    下一次轮询不许把它盖回通用文案；
  7 旧后端没有 /api/restart（404）→ 给手动路径那句，按钮回到可点，页面不刷新；
  8 服务直接连不上 → 说连不上，不含「不认这一键」；
  9 一键换班成功 → 页面真的刷新，刷新后横幅不再出现；
 10 零 emoji、零 console 报错、窄视口不横向溢出、横幅截图留档。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent
from playwright.sync_api import sync_playwright  # noqa: E402

FAIL = []
PASSED = [0]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")
LIE = "本机服务没在跑"          # 这句只许留在注释里，界面不能说（后端在跑，只是旧）
OLD_VER = "0.9.8"
OLD_CODE = "0ldc0de0ldc0deadbeef"
PAGE_VER = "1.0.5"              # 与 ui.html 的 GUIZANG_PAGE 对齐；对不上由静态门禁兜
SANDBOX = tempfile.mkdtemp(prefix="gz-swapbar-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))

# 拦截器看这两个值决定怎么答：stale = /api/state 报旧版本；restart = 那一键的回执。
FAKE = {"stale": False, "restart": "ok"}


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 沙盒服务（真代码，只是问不出旧版本，所以版本得由拦截器改）────────────
PY = sys.executable
SHOTS = os.environ.get("GUIZANG_SHOT_DIR") or os.path.join(SANDBOX, "shots")
os.makedirs(SHOTS, exist_ok=True)
env = dict(os.environ, GUIZANG_SELFTEST_DIR=SANDBOX,
           GUIZANG_DATA=os.path.join(SANDBOX, "data"),
           GUIZANG_BOOKS=os.path.join(SANDBOX, "books"),
           GUIZANG_SHOT_DIR=SHOTS)
for d in ("data", "books"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)

srv_port = free_port()
logp = os.path.join(SANDBOX, "server.log")
logf = open(logp, "w")
srv = subprocess.Popen([PY, "ui_server.py", "--port", str(srv_port)],
                       cwd=str(REPO), env=env, stdout=logf, stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
BASE = ""
for _ in range(160):
    try:
        m = re.search(r"http://127\.0\.0\.1:(\d+)", open(logp).read())
        if m:
            cand = "http://127.0.0.1:%s" % m.group(1)
            json.loads(urllib.request.urlopen(cand + "/api/state", timeout=1).read())
            BASE = cand
            break
    except Exception:
        pass
    time.sleep(0.25)
if not BASE:
    print("沙盒服务没起来：", open(logp).read()[-1500:])
    sys.exit(1)
print("横幅走查沙盒：", BASE)


def bar_state(page):
    """横幅此刻什么样：出不出现、写的是什么、按钮能不能点。"""
    return page.evaluate("""() => {
      const bar = document.getElementById('swapbar');
      const span = document.getElementById('swaptext');
      const go = document.getElementById('swapgo');
      return {show: !!bar && bar.classList.contains('show'),
              vis: !!bar && getComputedStyle(bar).display !== 'none',
              text: span ? span.textContent : '',
              dis: go ? !!go.disabled : true,
              why: GZ.why, hid: GZ.hid, stale: GZ.stale,
              back: GZ.back, code: GZ.code};
    }""")


def wait_bar(page, want, timeout=8.0):
    """等横幅出现 / 收起。轮询是 2.6 秒一次，别用固定 sleep 赌运气。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = bar_state(page)
        if st["show"] == want and st["vis"] == want:
            return st
        time.sleep(0.2)
    return bar_state(page)


def on_state(route):
    resp = route.fetch()
    if not FAKE["stale"]:
        return route.fulfill(response=resp)
    try:
        d = json.loads(resp.body())
    except Exception:
        return route.fulfill(response=resp)
    d["version"] = OLD_VER
    d["code"] = OLD_CODE
    return route.fulfill(status=200,
                         headers={"Content-Type": "application/json; charset=utf-8"},
                         body=json.dumps(d).encode("utf-8"))


def fulfill_json(route, obj, status=200):
    return route.fulfill(status=status,
                         headers={"Content-Type": "application/json; charset=utf-8"},
                         body=json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def on_restart(route):
    mode = FAKE["restart"]
    if mode == "ok":
        # 接班人就是手上这份代码：从此 /api/state 别再假装旧，让真后端答话。
        FAKE["stale"] = False
        return fulfill_json(route, {"ok": True, "pid": 424242, "port": srv_port,
                                    "from": OLD_VER,
                                    "msg": "后端正在换成新进程，页面几秒后自己刷新（在跑的任务会中断）"})
    if mode == "refused":
        return fulfill_json(route, {"ok": False,
                                    "msg": "端口 %d 上的旧后端（%s）正在跑任务，不打断它" % (srv_port, OLD_VER)})
    if mode == "missing":
        # 比界面旧的后端根本没有这条接口：只有干巴巴一句 not found。
        return route.fulfill(status=404, headers={"Content-Type": "text/plain"},
                             body=b"not found")
    if mode == "gone":
        return route.abort("connectionrefused")
    return route.continue_()


console_errors = []
muted = []          # 这一份故意造出来的网络失败，不算界面报错
pageerrors = []

# 走查里主动造了两种「后端答不上来」：把 /api/restart 掐断、打一个不存在的接口。
# Chromium 会把这两件事也写进 console，得跟真正的界面报错分开 —— 否则这一检就只是在
# 数自己造的噪声。真正要守的是 JS 层的未捕获异常（pageerror）与别 kinds 的 console 错误。
IGNORE = ("ERR_CONNECTION_REFUSED", "status of 404")


def on_console(m):
    if m.type != "error":
        return
    if any(k in m.text for k in IGNORE):
        muted.append(m.text)
    else:
        console_errors.append(m.text)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 820})
    page.on("console", on_console)
    page.on("pageerror", lambda e: pageerrors.append(str(e)))
    page.route(re.compile(r"/api/state"), on_state)
    page.route(re.compile(r"/api/restart"), on_restart)

    # ── 1 版本对得上：不该有横幅 ──────────────────────────────────────
    page.goto(BASE + "/", wait_until="load")
    page.wait_for_timeout(1200)
    st = bar_state(page)
    chk("版本对得上时横幅不出现", not st["show"], st)
    chk("界面认到后端版本 " + PAGE_VER, st["back"] == PAGE_VER, st["back"])
    chk("后端报得出代码指纹", bool(st["code"]) and st["code"] != OLD_CODE, st["code"])

    # ── 2 后端是旧的：横幅出现、两个版本号都在、不说假话 ──────────────
    FAKE["stale"] = True
    page.evaluate("drawState(true)")
    st = wait_bar(page, True)
    txt = st["text"]
    chk("旧后端在页顶横幅里现形", st["show"] and st["vis"], st)
    chk("横幅同时报出后端与界面的版本", OLD_VER in txt and PAGE_VER in txt, txt)
    chk("横幅给的是「换新后端」那条出路", "换新后端" in txt, txt)
    chk("横幅不说「本机服务没在跑」这句假话", LIE not in txt, txt)
    chk("横幅零 emoji", not EMOJI.search(txt), txt)
    page.screenshot(path=os.path.join(SHOTS, "swapbar-stale.png"))

    # ── 3 旧后端打新接口：认成 stale，文案与横幅同一把尺 ──────────────
    r = page.evaluate("() => gzApi('/api/__ghost__')")
    chk("旧后端没有的接口认成 stale（不是连不上）", r.get("gz") == "stale", r)
    chk("这条说法里点名了两版号与一键", OLD_VER in r.get("msg", "") and PAGE_VER in r.get("msg", ""), r)
    chk("stale 的说法也不含那句假话", LIE not in r.get("msg", ""), r)
    st = bar_state(page)
    chk("打不通之后横幅还在那儿", st["show"], st)

    # ── 4 版本对得上时打不存在的接口：说的是「认不得这条接口」─────────
    FAKE["stale"] = False
    page.evaluate("GZ.why=''; drawState(true)")
    st = wait_bar(page, False)
    chk("版本对上后横幅自己收起来", not st["show"], st)
    r = page.evaluate("() => gzApi('/api/__ghost__')")
    chk("后端没这条接口时报出路径", "/api/__ghost__" in r.get("msg", ""), r)
    chk("这种情形仍然给一键", "换新后端" in r.get("msg", "") or "旧" in r.get("msg", ""), r)
    chk("这条也不说「本机服务没在跑」", LIE not in r.get("msg", ""), r)
    st = bar_state(page)
    chk("404 之后横幅把话说出来", st["show"] and st["vis"], st)

    # ── 5 先不管：咽回去，之后几轮轮询都不再冒 ─────────────────────────
    FAKE["stale"] = True
    page.evaluate("drawState(true)")
    st = wait_bar(page, True)
    chk("重新变旧时横幅再次出现", st["show"], st)
    page.click("#swapx")
    st = bar_state(page)
    chk("「先不管」把横幅收起来", not st["show"] and st["hid"], st)
    page.wait_for_timeout(3000)          # 跨过一轮 2.6 秒轮询
    st = bar_state(page)
    chk("收起来之后轮询不再把它弹回来", not st["show"], st)
    chk("先不管只咽话、不改判定", st["stale"] is True, st)
    page.evaluate("GZ.hid=false")        # 后面的场景要能看见横幅

    # ── 6 后端回了原因：用它那句，且不许被下一次轮询盖掉 ────────────────
    FAKE["restart"] = "refused"
    page.evaluate("GZ.why=''; drawState(true)")
    wait_bar(page, True)
    before = bar_state(page)["text"]
    chk("点之前横幅是通用的那句", OLD_VER in before, before)
    page.click("#swapgo")
    page.wait_for_timeout(500)
    st = bar_state(page)
    chk("换成不成时用的是后端那句原因", "正在跑任务" in st["text"], st)
    chk("那句原因把端口也说出来了", str(srv_port) in st["text"], st)
    chk("没换成时按钮回到可点", not st["dis"], st)
    chk("后端给的 msg 存进了 GZ.why", "正在跑任务" in (st["why"] or ""), st)
    page.wait_for_timeout(3200)          # 一整轮 2.6 秒轮询
    st = bar_state(page)
    chk("下一轮轮询没把那句原因盖回通用文案", "正在跑任务" in st["text"], st)
    chk("横幅还挂着，用户看得见原因", st["show"], st)

    # ── 7 旧后端没这条接口：给手动路径，页面不刷新 ─────────────────────
    FAKE["restart"] = "missing"
    page.evaluate("window.__mark = 1")
    page.click("#swapgo")
    page.wait_for_timeout(600)
    st = bar_state(page)
    chk("没有这条接口时说清是这一键不支持", "不认「换新后端」" in st["text"], st)
    chk("手动路径讲明白该做什么", "退出归藏" in st["text"] and "再打开" in st["text"], st)
    chk("手动路径那句不提「本机服务没在跑」", LIE not in st["text"], st)
    page.wait_for_timeout(2000)          # 一次轮询 + 一点余量：确认没偷偷刷新
    st = bar_state(page)
    chk("这条路径下页面没有刷新", page.evaluate("window.__mark") == 1, st)
    chk("刷新没发生所以横幅文案还是那句", "不认「换新后端」" in st["text"], st)

    # ── 8 服务连不上：说连不上，别扯「不认这一键」 ─────────────────────
    FAKE["restart"] = "gone"
    page.evaluate("drawState(true)")     # 通用文案会来，但 GZ.why 还占着 → 先看点击后的
    page.click("#swapgo")
    page.wait_for_timeout(600)
    st = bar_state(page)
    chk("连不上时说的是连不上", "连不上本机服务" in st["text"], st)
    chk("连不上那句不装作是接口的问题", "不认「换新后端」" not in st["text"], st)
    chk("连不上之后按钮仍可再试", not st["dis"], st)

    # ── 9 一键换班成功：页面真的刷新，刷新后横幅不再出现 ────────────────
    FAKE["restart"] = "ok"
    page.evaluate("GZ.why=''; drawState(true)")
    wait_bar(page, True)
    with page.expect_navigation(timeout=15000):
        page.click("#swapgo")
    page.wait_for_load_state("load")
    page.evaluate("drawState(true)")
    st = bar_state(page)
    chk("换班后页面自己刷新了", page.evaluate("window.__mark") is None, st)
    chk("刷新后 GZ 是干净的（没卡在 swapping）", not st["stale"] and st["why"] == "", st)
    page.wait_for_timeout(1000)
    st = bar_state(page)
    chk("刷新后横幅不再出现", not st["show"], st)
    chk("刷新后认到的还是本机这个新后端", st["back"] == PAGE_VER, st["back"])

    # ── 10 收尾：整页零 emoji、窄视口不溢出、留一张图 ──────────────────
    st = bar_state(page)
    FAKE["stale"] = True
    page.evaluate("GZ.hid=false; GZ.why=''; drawState(true)")
    wait_bar(page, True)
    body = page.evaluate("document.body.innerText")
    chk("横幅出现在页面上时整页文案零 emoji", not EMOJI.search(body),
        [c for c in body if EMOJI.match(c)][:8])
    page.set_viewport_size({"width": 720, "height": 700})
    page.wait_for_timeout(400)
    chk("窄视口横幅不横向溢出",
        page.evaluate("document.getElementById('swapbar').getBoundingClientRect().right")
        <= 720 + 0.6,
        page.evaluate("document.getElementById('swapbar').getBoundingClientRect().right"))
    page.screenshot(path=os.path.join(SHOTS, "swapbar-narrow.png"), full_page=False)
    print("截图：", os.path.join(SHOTS, "swapbar-stale.png"),
          "与", os.path.join(SHOTS, "swapbar-narrow.png"))
    browser.close()

chk("console 零报错", not console_errors, console_errors[:4])
chk("页面零未捕获异常", not pageerrors, pageerrors[:4])

print()
if FAIL:
    print("横幅走查有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("横幅这一套全对上了：认得出旧后端、说清两版号、三种失败各说各的话、"
          "先不管真能咽回去、换成了就自己刷新。")
sys.exit(len(FAIL))
