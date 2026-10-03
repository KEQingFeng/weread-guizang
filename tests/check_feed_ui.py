# -*- coding: utf-8 -*-
"""订阅那一屏的真机走查：把它当一个小工具用，而不是「有没有渲染出来」。

为什么单开一份、还自己起服务：这一套要真的订源、真的抓、真的改标签删条目，
和别的套件共用那份沙盒数据会互相踩（上一轮的教训就是取书留了本空书在书架上）。
所以这儿另起一个 ui_server，数据目录是系统临时目录里的一个沙盒，跑完整包删掉 ——
用户真实的 ~/Documents/归藏 和仓库 cache/ 一个字节都不碰。

验的东西（都对应界面上真能点的东西）：
  · 订上 / 刷新 / 铺出条目 / 分页「再来一批」；后台轮询不会把列表重铺（首行不跳、不滚回顶上）；
  · 三种筛选（范围 / 分组 / 标签）+ 搜索 + 清空；正文右栏渲染、Esc 收回；
  · 勾选 → 批量条 → 这一屏全选 → 标已读；标签片点开来能改（弹层的框必须可打字）；
  · 「全标已读」只在能安全按刀时出现，按完能撤销；
  · 建组 / 改名 / 归组 / 退订 / OPML 导入导出 / 清理试算；
  · 单条「收进书架」→「去书库」真的打开阅读器；
  · 左栏收起后还展得开（宽窗那颗按钮、窄窗顶上那颗），窄屏不横向溢出；
  · 全程零 emoji、零 console 报错。
"""
import atexit
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent
import selftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FAIL = []
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-ui-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 夹具：三个源，一个有正文、一个只有摘要、一个是 Atom ─────────────
FIX_PORT = free_port()
FIX_BASE = "http://127.0.0.1:%d" % FIX_PORT
ROUTES = {}


def mk_rss(title, n, with_body=True):
    items = []
    for i in range(1, n + 1):
        body = ("<p>%s 第 %02d 篇的正文。这一段用来验右栏排版，够长才看得出滚动。</p>" % (title, i)) \
            + ("<p>补白补白补白补白。</p>" * 5)
        items.append(
            "<item><title>%s 第 %02d 篇</title><link>%s/p%d</link>"
            "<guid isPermaLink=\"false\">%s-%d</guid><author>作者%d</author>"
            "<pubDate>Thu, %02d Oct 2025 0%d:00:00 GMT</pubDate>"
            "<description>%s 第 %d 篇的摘要，中栏那行小字就是它。</description>%s</item>"
            % (title, i, FIX_BASE, i, title, i, i % 5 + 1, 6 + (i % 20), i % 9 + 1,
               title, i,
               ("<content:encoded><![CDATA[%s]]></content:encoded>" % body) if with_body else ""))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            '<channel><title>%s</title><link>%s/</link><description>演示源</description>%s'
            '</channel></rss>' % (title, FIX_BASE, "".join(items))).encode("utf-8")


def mk_atom(title, n):
    es = "".join("<entry><title>%s 想法 %02d</title><link href=\"%s/n%d\"/>"
                 "<id>%s-n%d</id><updated>2025-10-0%dT10:00:00Z</updated>"
                 "<summary>%s 第 %d 条的摘要</summary></entry>"
                 % (title, i, FIX_BASE, i, title, i, i % 9 + 1, title, i)
                 for i in range(1, n + 1))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>%s</title><link href="%s/"/>'
            '<id>atom-%s</id><updated>2025-10-06T10:00:00Z</updated>%s</feed>'
            % (title, FIX_BASE, title, es)).encode("utf-8")


def mk_home():
    # 站点首页：discover 靠它找到背后的订阅地址，所以这一页得写一个 link
    return ('<!doctype html><html><head><title>演示站</title>'
            '<link rel="alternate" type="application/rss+xml" href="%s/tech.xml">'
            '</head><body><p>这是一个演示站。</p></body></html>' % FIX_BASE).encode("utf-8")


class FixHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        r = ROUTES.get(path)
        if r is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype = r
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ROUTES = {"/": (mk_home(), "text/html; charset=utf-8"),
          "/tech.xml": (mk_rss("技术周刊", 260), "application/rss+xml"),
          "/design.xml": (mk_rss("设计手记", 40, with_body=False), "application/rss+xml"),
          "/notes.xml": (mk_atom("碎碎念", 15), "application/atom+xml")}
_fixsrv = http.server.ThreadingHTTPServer(("127.0.0.1", FIX_PORT), FixHandler)
threading.Thread(target=_fixsrv.serve_forever, daemon=True).start()

# ── 沙盒应用服务（自己的数据目录，跑完连目录一起删） ────────────────
PY = sys.executable
env = dict(os.environ, GUIZANG_SELFTEST_DIR=SANDBOX,
           GUIZANG_DATA=os.path.join(SANDBOX, "cache"),
           GUIZANG_BOOKS=os.path.join(SANDBOX, "books"),
           GUIZANG_SHOT_DIR=os.path.join(SANDBOX, "shots"))
for d in ("cache", "books", "shots"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)
subprocess.run([PY, str(HERE / "seed.py"), "--force"], cwd=REPO, env=env, capture_output=True)

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
print("订阅走查沙盒：", BASE, " 夹具源：", FIX_BASE)


def api(body=None, path="/api/feed"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def gp(qs):
    """只读那几条走 GET /api/feed?…（feed_view）—— 把 mode 塞进 POST 的 body 里，
    后端只会当它是个不认识的 act，回一句「订阅这块没说要干什么」。"""
    with urllib.request.urlopen(BASE + "/api/feed?" + qs, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_entries(want=1, secs=90):
    """等后台抓取把条目铺进库里（订完不自己抓，得按一下刷新，所以这儿先起再等）。"""
    api({"act": "refresh"})
    for _ in range(int(secs / 0.5)):
        try:
            d = gp("mode=entries&limit=1")
        except Exception:
            d = {}
        if (d.get("total") or 0) >= want:
            return d.get("total")
        time.sleep(0.5)
    return -1


# ── 后端：这一轮新接上的两刀（read_all 的边界、tag_set 的整组替换） ──
r = api({"act": "add", "url": FIX_BASE + "/tech.xml", "group": "技术"})
chk("后端：订上一个源", r.get("ok") is True, r)
tech_id = (r.get("feed") or {}).get("id") or ""
chk("后端：订完认得出这个源的 id（界面上那颗「这一源」都靠它）", bool(tech_id), r)
api({"act": "add", "url": FIX_BASE + "/design.xml", "group": "设计"})
api({"act": "add", "url": FIX_BASE + "/notes.xml"})
TOTAL = wait_entries(315)
chk("后端：三个源都抓下来了（260 + 40 + 15）", TOTAL >= 315, TOTAL)

lst = gp("mode=list")
chk("后端：mode=list 报得出源数与未读数",
    (lst.get("summary") or {}).get("subs") == 3
    and "unread" in (lst.get("summary") or {}), lst.get("summary"))
eid = (gp("mode=entries&limit=3").get("entries") or [{}])[0]
chk("后端：条目带 id / tags / read 这些界面要用的字段",
    {"id", "title", "read", "tags", "starred", "later", "pushed"} <= set(eid), sorted(eid))

t = api({"act": "tag_set", "id": eid["id"], "tags": "值得细读, 明天再看"})
chk("后端：tag_set 是整组替换（一次给两个）", sorted(t.get("tags") or []) == ["值得细读", "明天再看"], t)
t2 = api({"act": "tag_set", "id": eid["id"], "tags": "只留这个"})
chk("后端：tag_set 再写一次会把上一组换掉", t2.get("tags") == ["只留这个"], t2)
t3 = api({"act": "tag_set", "id": eid["id"], "tags": ""})
chk("后端：tag_set 清空就是没标签", t3.get("tags") == [], t3)

ra = api({"act": "read_all", "id": tech_id, "read": True})
chk("后端：按源整档标已读，报了条数也给撤销凭据",
    ra.get("ok") is True and int(ra.get("n") or 0) > 0 and ra.get("undo_token"), ra)
un = api({"act": "undo", "token": ra.get("undo_token")})
chk("后端：这一刀撤销得回来", un.get("ok") is True, un)
bad = api({"act": "read_all", "id": "no-such-feed"})
chk("后端：整档标已读认不出源时会说人话（不假装做了）",
    bad.get("ok") is False or int(bad.get("n") or 0) == 0, bad)

# ── 真机：界面那一圈 ──────────────────────────────────────
OVERFLOW = """() => {
  const win = document.documentElement.clientWidth + 1;
  // 「戳出视口」要的是真能把整页撑宽的那种。被祖先裁掉的东西撑不宽：
  // .ambient 那层背景色晕是 position:fixed + overflow:hidden 里塞几个比视口还大的圆，
  // 圆的一部分天生就在视口外（这正是它好看的地方），但它永远滚不出横向滚动条。
  // 上一版只放过「自己是 fixed」的元素，没放过「祖先裁它」的元素，于是每次都冤枉这一层。
  const clipped = e => {
    for (let n = e.parentElement; n; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.position === 'fixed') return true;
      if (/hidden|clip|auto|scroll/.test(s.overflowX)) {
        const r = n.getBoundingClientRect();
        if (r.left >= -1 && r.right <= win) return true;
      }
    }
    return false;
  };
  const trace = e => {
    const out = [];
    for (let n = e; n && out.length < 4; n = n.parentElement) {
      if (!n.tagName || n.tagName === 'BODY' || n.tagName === 'HTML') break;
      out.push(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '')
               + (n.className && typeof n.className === 'string'
                  ? '.' + n.className.trim().split(/\\s+/)[0] : ''));
    }
    return out.join(' < ');
  };
  const off = [...document.querySelectorAll('*')].filter(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && (r.right > win || r.left < -1) &&
           getComputedStyle(e).position !== 'fixed' && !e.closest('[hidden]') &&
           !e.closest('.pop') && getComputedStyle(e).visibility !== 'hidden' &&
           !clipped(e);
  }).map(e => {
    // 只报「I」这种没名字的元素等于没报：门禁 FAIL 要能直接指出是谁、越界多少、在哪条链上，
    // 不然每次都得重跑一遍带插桩的版本才知道去改哪一行。
    const r = e.getBoundingClientRect();
    const over = Math.round(Math.max(r.right - win, -r.left));
    return (trace(e) || e.tagName) + ' 溢出' + over + 'px';
  });
  return {doc: document.documentElement.scrollWidth, win, off: [...new Set(off)].slice(0, 6)};
}"""


def sweep(page, tag):
    t = page.evaluate(OVERFLOW)
    chk(f"{tag}：整页没有横向溢出", t["doc"] <= t["win"] + 1, t)
    chk(f"{tag}：没有元素戳出视口", not t["off"], t["off"])
    hits = sorted(set(EMOJI.findall(page.evaluate("() => document.body.innerText"))))
    chk(f"{tag}：屏幕上一个 emoji 也没有", not hits, hits)


def cols(page):
    return page.evaluate("() => [...document.querySelectorAll('.fewrap > *')]"
                         ".map(e => e.className + ':' + Math.round(e.getBoundingClientRect().width))")


def flush(page):
    """把在跑的动画推到终点。headless Chromium 的动画时钟会整段卡住：transition
    建好了却不推进，读到的计算值还停在起始值 —— 于是「铺开了没有」量的是动画，
    不是版式。与 check_media_views.py 同一份写法。"""
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


def read_w(page):
    return page.evaluate("() => Math.round(document.querySelector('.feread')"
                         ".getBoundingClientRect().width)")


def sheet_act(page, label):
    page.locator('#sheetActs button', has_text=label).first.click()


def settle(page, expr, ms=15000):
    """等一个条件成立，等不到只回 False。

    为什么不用 page.wait_for_function：它超时是抛异常，整条走查当场炸断，
    后面几十条断言一条也跑不到 —— 门禁要的是「这一条 FAIL，其余照跑」。
    搜索这类有防抖的控件尤其需要它：填完字要等 320ms 再去打后端。
    """
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def side_item(page, text):
    return page.locator('#fSide .feitem', has_text=text).first


def row_btn(page, nth, label):
    return page.locator('#fList .frow').nth(nth).locator('.acts button', has_text=label).first


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("PAGEERROR " + str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(900)
    page.click('#nav button[data-v="feed"]')
    page.wait_for_function("""() => document.querySelectorAll('#fList .frow').length > 5""",
                           timeout=40000)
    page.wait_for_timeout(400)

    c = cols(page)
    chk("三栏都铺出来了（筛选 / 清单 / 正文）", len(c) == 3, c)
    chk("左栏与中栏都有宽度", all(int(x.split(":")[-1]) > 100 for x in c[:2]), c)
    chk("清单里有条目", page.locator("#fList .frow").count() >= 5,
        page.locator("#fList .frow").count())
    chk("左栏列出了三个源", page.locator('#fSubs .feitem').count() == 3,
        page.locator('#fSubs .feitem').count())
    chk("左栏有那两个分组", page.evaluate(
        "() => ['技术','设计'].every(n => [...document.querySelectorAll('#fSide .feitem')]"
        ".some(e => e.innerText.includes(n)))"))
    tail = page.evaluate("() => (document.querySelector('.fetail')||{}).innerText || ''")
    chk("尾巴说清铺了多少、还剩多少", "已铺" in tail and "再来一批" in tail, tail)

    # ── 打开一条读，Esc 收回 ──
    page.locator("#fList .frow").first.click()
    page.wait_for_function("() => !!document.querySelector('#fRead .rdbd .md')", timeout=20000)
    chk("右栏标题出来了", bool(page.evaluate("() => (document.querySelector('#fRead .tt')||{}).innerText")))
    chk("右栏正文渲染出来了（抓回来的当资料处理）",
        page.evaluate("() => (document.querySelector('#fRead .rdbd .md').innerText || '').length") > 80)
    # 列宽是过渡出来的（.fewrap.feopen 把 --fe-read 从 0 推到 42%），在 headless 里
    # 动画时钟会整段不推进：直接量会量到 0，单独跑手慢一点反而是绿的。
    # 先 flush 到终点，仍窄就用 settle 等它铺满，最后把量到的数带进诊断。
    flush(page)
    rw = read_w(page)
    if rw <= 300:
        settle(page, "() => Math.round(document.querySelector('.feread')"
                     ".getBoundingClientRect().width) > 300", 4000)
        flush(page)
        rw = read_w(page)
    chk("右栏宽度铺开了", rw > 300, rw)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    chk("Esc 把右栏收回去了", not page.evaluate(
        "() => !!document.querySelector('#fRead .tt')"))

    # ── 勾选 → 批量 ──
    page.locator("#fList .frow input.ck").first.click()
    page.wait_for_function("() => !document.querySelector('#fBatch').hidden", timeout=8000)
    bt = page.evaluate("() => document.querySelector('#fBatch').innerText")
    chk("勾一条就出批量条，并报出几条", "已选 1 条" in bt, bt[:120])
    page.locator("#fBatch button", has_text="这一屏全选").first.click()
    page.wait_for_timeout(600)
    n_sel = int(re.search(r"已选 (\d+) 条", page.evaluate(
        "() => document.querySelector('#fBatch').innerText")).group(1))
    chk("「这一屏全选」把铺出来的都收进来了",
        n_sel == page.locator("#fList .frow").count(), (n_sel, page.locator("#fList .frow").count()))
    page.locator("#fBatch button", has_text="标已读").first.click()
    page.wait_for_timeout(1500)
    chk("批量标已读之后，界面不再把这些条当未读", page.evaluate(
        "() => [...document.querySelectorAll('#fList .frow.cur, #fList .frow')].slice(0,5)"
        ".every(e => !e.querySelector('.nm.unread'))"))
    page.locator("#fBatch button", has_text="取消选择").first.click()
    page.wait_for_timeout(400)
    chk("取消选择把批量条收了", page.evaluate("() => document.querySelector('#fBatch').hidden"))

    # ── 标签：点标签片能改（弹层里那个框必须打得进字） ──
    page.locator("#fList .frow").nth(1).locator(".acts button", has_text="打开").first.click()
    page.wait_for_function("() => !!document.querySelector('#fRead .rdh .acts')", timeout=15000)
    page.locator("#fRead .rdh .acts button", has_text="标签").first.click()
    page.wait_for_timeout(400)
    chk("「标签」弹层开了", page.evaluate("() => document.querySelector('#veil').classList.contains('open')"))
    chk("弹层里那个框看得见", page.evaluate("() => !document.querySelector('#sheetBody').hidden"))
    # 读的是 readOnly（驼峰）。上一版这里跟着实现一起写成了小写 .readonly，
    # 那个名字在 DOM 上恒为 undefined，于是否定一下永远「通过」，界面上打不进字也照样报绿。
    chk("弹层里那个框打得进字（不是 readonly）", page.evaluate(
        "() => !document.querySelector('#sheetBody').readOnly"
        " && !document.querySelector('#sheetBody').hasAttribute('readonly')"))
    page.fill("#sheetBody", "手工改的标签")
    chk("打进去的字真的留在框里", page.evaluate(
        "() => document.querySelector('#sheetBody').value === '手工改的标签'"))
    sheet_act(page, "存好")
    page.wait_for_function("() => !document.querySelector('#veil').classList.contains('open')",
                           timeout=15000)
    chk("改标签落到了后端", "手工改的标签" in json.dumps(
        gp("mode=entries&limit=400"),
        ensure_ascii=False))
    chk("左栏出现了这个标签", page.evaluate(
        "() => document.querySelector('#fSide').innerText.includes('手工改的标签')"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 输入型弹层都该打得进字：建组 / 改名 / 导入 / 挂标签
    for label, opener in (("新建一组", lambda: page.locator("#fSide .fegrp button", has_text="新建").first.click()),
                          ("给这个源改个名字", lambda: page.locator('#fSubs .feitem').first.locator(
                              "button", has_text="改").first.click()),
                          ("导入 OPML", lambda: page.click("#fImp"))):
        opener()
        page.wait_for_timeout(400)
        t = page.evaluate("() => document.querySelector('#sheetTitle').innerText")
        chk(f"弹层「{label}」开得出来", label in t, t)
        chk(f"「{label}」那个框打得进字", page.evaluate(
            "() => !document.querySelector('#sheetBody').readOnly"
            " && !document.querySelector('#sheetBody').hasAttribute('readonly')"
            " && !document.querySelector('#sheetBody').hidden"))
        page.fill("#sheetBody", "试打")
        chk(f"「{label}」打完确实在框里", page.evaluate(
            "() => document.querySelector('#sheetBody').value === '试打'"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    # ── 建组（打字 + 点按钮）与归组（点按钮，不再数第几行） ──
    page.locator("#fSide .fegrp button", has_text="新建").first.click()
    page.wait_for_timeout(300)
    page.fill("#sheetBody", "夜里翻")
    sheet_act(page, "建好")
    page.wait_for_function("() => document.querySelector('#fSide').innerText.includes('夜里翻')",
                           timeout=20000)
    chk("新建的组出现在左栏", True, "夜里翻")
    page.locator('#fSubs .feitem').first.locator("button", has_text="组").first.click()
    page.wait_for_timeout(400)
    chk("归组弹层给的是可点的组名按钮（不是让用户数行）", page.evaluate(
        "() => [...document.querySelectorAll('#sheetActs button')]"
        ".map(b => b.innerText.trim()).includes('夜里翻')"))
    page.locator('#sheetActs button', has_text="夜里翻").first.click()
    page.wait_for_function(
        """() => [...document.querySelectorAll('#fSubs .feitem')].some(
             e => e.innerText.includes('碎碎念') || e.innerText.includes('技术周刊'))""",
        timeout=20000)
    g = gp("mode=list")
    grp = {x.get("group"): x.get("feeds") for x in (g.get("groups") or [])}
    chk("移动真的落到后端（那一组现在有一个源）", grp.get("夜里翻") == 1, grp)
    chk("归组之后弹层自己收了", page.evaluate(
        "() => !document.querySelector('#veil').classList.contains('open')"))

    # ── 搜索 / 筛选 / 清空 ──
    # 「填完字等列表有行」是条废等待：搜索前那整档本来就铺着几百行，等待当场就满足，
    # 断言拍到的是还没筛过的旧列表 —— 于是永远 FAIL（或者更糟：永远看起来像过了）。
    # 搜索有 320ms 防抖 + 一次后端往返，所以要等的是「筛好了」这个状态本身。
    rows0 = page.locator("#fList .frow").count()
    page.fill("#fQ", "第 12 篇")
    hit = settle(page, """() => {
        const rs = [...document.querySelectorAll('#fList .frow .nm')];
        return rs.length > 0 && rs.every(e => e.innerText.includes('第 12 篇'));
    }""")
    titles = page.evaluate("() => [...document.querySelectorAll('#fList .frow .nm')]"
                           ".map(e => e.innerText).slice(0, 4)")
    n_hit = page.locator("#fList .frow").count()
    chk("搜标题命中了", hit and n_hit > 0, titles)
    chk("搜索是真的收窄了（不是把整档重铺一遍）", n_hit < rows0, (rows0, n_hit))
    back = gp("mode=entries&limit=400&q=%s" % urllib.parse.quote("第 12 篇"))
    page_size = int(page.evaluate("() => FEED_PAGE"))
    chk("后端与界面给的是同一批命中",
        n_hit == min(int(back.get("total") or 0), page_size), (back.get("total"), n_hit))
    chk("筛着的时候不给「全标已读」（那一刀会切到没看见的）",
        page.evaluate("() => document.querySelector('#fReadAll').hidden"))
    page.click("#fClear")
    page.wait_for_timeout(900)
    chk("清空筛选回到整档", page.evaluate("() => document.querySelector('#fQ').value") == "")

    side_item(page, "未读").click()
    page.wait_for_timeout(1400)
    chk("切到「未读」这一档", page.evaluate(
        "() => [...document.querySelectorAll('#fSide .feitem.on')].some(e => e.innerText.includes('未读'))"))
    ra_vis = page.evaluate("() => !document.querySelector('#fReadAll').hidden")
    chk("未读这一档给「全标已读」", ra_vis, page.evaluate(
        "() => document.querySelector('#fReadAll').title"))
    before_un = gp("mode=list").get("summary", {}).get("unread") or 0
    page.click("#fReadAll")
    page.wait_for_function("() => document.querySelector('#fBar') && !document.querySelector('#fBar').hidden",
                           timeout=20000)
    page.wait_for_timeout(1200)
    after_un = gp("mode=list").get("summary", {}).get("unread") or 0
    chk("整档标已读真的把未读清了", after_un < before_un, (before_un, after_un))
    chk("进度条那一栏说「可以做回来」", page.evaluate(
        "() => document.querySelector('#fBar').innerText.includes('这一步可以做回来')"))
    page.locator("#fBar button", has_text="撤销").first.click()
    page.wait_for_function("() => document.querySelector('#fBar').hidden", timeout=25000)
    back_un = gp("mode=list").get("summary", {}).get("unread") or 0
    chk("撤销之后未读回来了", back_un >= before_un - 2, (before_un, after_un, back_un))

    # ── 分页、轮询不重铺 ──
    side_item(page, "全部文章").click()
    page.wait_for_timeout(1200)
    n0 = page.locator("#fList .frow").count()
    first0 = page.evaluate("() => document.querySelector('#fList .frow .nm').innerText")
    page.locator(".fetail button", has_text="再来一批").first.click()
    page.wait_for_function("() => document.querySelectorAll('#fList .frow').length > %d" % n0,
                           timeout=25000)
    chk("「再来一批」把下一批接在后面（不是整栏重铺）",
        page.evaluate("() => document.querySelector('#fList .frow .nm').innerText") == first0,
        (n0, page.locator("#fList .frow").count()))
    scroll = page.evaluate("() => { const b = document.querySelector('#fList');"
                           " b.scrollTop = 1500; return b.scrollTop; }")
    page.wait_for_timeout(7000)
    chk("后台轮询没有把清单滚回顶上", page.evaluate(
        "() => document.querySelector('#fList').scrollTop") >= scroll - 1, scroll)
    chk("后台轮询没有改首行", page.evaluate(
        "() => document.querySelector('#fList .frow .nm').innerText") == first0)

    # ── 收进书架 → 去书库（阅读器真的打开） ──
    row_btn(page, 2, "收进书架").click()
    page.wait_for_function("""() => [...document.querySelectorAll('#fList .frow')]
        .some(e => e.innerText.includes('已入库'))""", timeout=40000)
    chk("单条收进书架之后这一行标了「已入库」", True)
    row_btn(page, 2, "去书库").click()
    page.wait_for_function("() => document.body.classList.contains('reading')", timeout=25000)
    chk("「去书库」打开了阅读器", page.evaluate(
        "() => !!document.querySelector('.feread, #reader, .rdwrap') || document.body.classList.contains('reading')"))
    page.screenshot(path=os.path.join(SANDBOX, "shots", "feed-in-reader.png"))
    page.evaluate("() => { const b = document.querySelector('#rdBack') || document.querySelector('.rdback');"
                  " if (b) b.click(); }")
    page.wait_for_timeout(900)

    # ── OPML 导出 / 导入（导出的能再导进来，是这条线自洽的唯一凭据） ──
    page.click('#nav button[data-v="feed"]')
    page.wait_for_timeout(900)
    page.click("#fExp")
    shown = settle(page, "() => (document.querySelector('#sheetBody').value || '').includes('<opml')",
                   ms=20000)
    opml = page.evaluate("() => document.querySelector('#sheetBody').value")
    chk("点「导出」先把 OPML 摊开给人看（不是只写一个文件报一句路径）", shown, opml[:120])
    chk("OPML 导出给得出整份文档", "<opml" in opml and "</opml>" in opml, opml[:120])
    chk("OPML 里带着这三个源", opml.lower().count("xmlurl") >= 3, opml[:200])
    chk("摊开的那份是给人看/复制的（框本身打不进字）", page.evaluate(
        "() => document.querySelector('#sheetBody').readOnly"))
    sheet_act(page, "存成文件")
    written = settle(page, "() => document.body.innerText.includes('已写到')", ms=15000)
    chk("「存成文件」真的写到了本机，并把路径说给人听", written,
        page.evaluate("() => document.body.innerText").splitlines()[-1][:120])
    chk("文件确实在数据目录里（沙盒，不碰用户目录）", os.path.exists(
        os.path.join(SANDBOX, "cache", "订阅.opml")))
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    page.click("#fImp")
    page.wait_for_timeout(400)
    page.fill("#sheetBody", '<opml version="2.0"><body><outline text="演示" title="演示"'
                            ' xmlUrl="%s/tech.xml"/></body></opml>' % FIX_BASE)
    sheet_act(page, "开始导入")
    page.wait_for_function("() => document.querySelector('#fSubs').innerText.includes('技术周刊')",
                           timeout=30000)
    chk("重复导入同一份 OPML 不会把已订的源订第二遍",
        page.locator('#fSubs .feitem', has_text="技术周刊").count() == 1,
        page.locator('#fSubs .feitem').count())

    # ── 清理试算：先算再说，不动数据 ──
    page.click("#fSet")
    page.wait_for_timeout(500)
    chk("偏好弹层里那四样是给人读的（不是给用户误打的）", page.evaluate(
        "() => document.querySelector('#sheetBody').readOnly"))
    page.locator("#sheetActs button", has_text="清掉旧条目").first.click()
    page.wait_for_function("() => document.querySelector('#sheetTitle').innerText.includes('清掉旧条目')",
                           timeout=15000)
    page.locator("#sheetActs button", has_text="试算一下").first.click()
    page.wait_for_function("() => document.querySelector('#sheetTitle').innerText.includes('试算')",
                           timeout=20000)
    chk("试算给的是「会删多少条」，不是直接删", page.evaluate(
        "() => document.querySelector('#sheetTitle').innerText.includes('会删掉')"))
    chk("试算那一步给的是「先看再删」，退出键和动手键分开", page.evaluate(
        "() => { const t = [...document.querySelectorAll('#sheetActs button')]"
        "     .map(b => b.innerText.trim()); "
        "     return t.includes('真删') && t.some(x => x !== '真删'); }"),
        page.evaluate("() => [...document.querySelectorAll('#sheetActs button')]"
                      ".map(b => b.innerText.trim())"))
    page.locator("#sheetActs button", has_text="先不动").first.click()
    page.wait_for_timeout(400)
    chk("试算之后源还在（没被误删）", page.locator('#fSubs .feitem').count() >= 3,
        page.locator('#fSubs .feitem').count())
    chk("试算也没删掉任何条目（数据一字没动）", int(
        gp("mode=entries&limit=400").get("total") or 0) > 0,
        gp("mode=entries&limit=400").get("total"))

    # ── 左栏折叠 / 展开：不能把自己折死 ──
    page.click("#fFoldNav")
    page.wait_for_timeout(500)
    chk("收起左栏之后，顶栏给了一颗「看哪些」", page.evaluate(
        "() => !document.querySelector('#fUnfold').hidden"))
    page.click("#fUnfold")
    page.wait_for_timeout(500)
    chk("点「看哪些」能把左栏展回来", page.evaluate(
        "() => !document.querySelector('#fWrap').classList.contains('nonav')"))

    # ── 溢出探针自检 ──
    # 探针每放宽一条规则，就得证明它还能抓到真东西：现场塞一个 200vw 的越界块，
    # 看它会不会被报出来。否则「没有元素戳出视口」会因为过滤条件写太宽而永远绿 ——
    # 这一轮就是为了放过 .ambient 那层色晕才加的裁剪判定，不自检等于没改。
    page.evaluate("""() => {
      const d = document.createElement('div');
      d.id = 'gzProbeCanary';
      d.style.cssText = 'position:absolute; left:0; top:0; width:200vw; height:2px';
      document.body.append(d);
    }""")
    canary = page.evaluate(OVERFLOW)["off"]
    page.evaluate("() => document.querySelector('#gzProbeCanary').remove()")
    chk("溢出探针抓得到真越界的东西（放宽的判定没把它一起放过）",
        any("gzProbeCanary" in x for x in canary), canary)

    sweep(page, "订阅（宽窗）")
    page.screenshot(path=os.path.join(SANDBOX, "shots", "feed-wide.png"))

    # ── 窄屏：只留清单，读文章时不给多出来的一列筛子 ──
    page.set_viewport_size({"width": 720, "height": 860})
    page.wait_for_timeout(900)
    chk("窄屏没横向溢出", page.evaluate(OVERFLOW)["doc"] <= page.evaluate(OVERFLOW)["win"] + 1,
        page.evaluate(OVERFLOW))
    page.locator("#fList .frow").first.click()
    page.wait_for_function("() => !!document.querySelector('#fRead .rdbd')", timeout=20000)
    widths = page.evaluate("""() => [...document.querySelectorAll('.fewrap > *')]
        .map(e => e.className.split(' ')[0] + ':' + Math.round(e.getBoundingClientRect().width))""")
    chk("窄屏读文章时只占一列（清单让位，不插筛选栏）",
        sum(1 for w in widths if int(w.split(":")[-1]) > 60) <= 2, widths)
    sweep(page, "订阅（窄屏）")
    page.screenshot(path=os.path.join(SANDBOX, "shots", "feed-narrow.png"))

    chk("全程没有报错", not errors, errors[:6])
    browser.close()

# ── 删干净：这一套的订阅数据只活在这个临时目录里 ────────────────
for s in (gp("mode=list").get("subs") or []):
    api({"act": "remove", "id": s.get("id")})
chk("收尾：源都退掉了", (gp("mode=list").get("summary") or {}).get("subs") in (0, None),
    gp("mode=list").get("summary"))

print()
print(f"订阅走查：{'全部通过' if not FAIL else str(len(FAIL)) + ' 项失败 -> ' + ' | '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
