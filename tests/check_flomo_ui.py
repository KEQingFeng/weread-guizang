# -*- coding: utf-8 -*-
"""便签那一屏的真机走查：flomo 导出包 → 时间线 → 收成书 → 记忆画像 → 忘掉 / 清空。

为什么单开一份、并且自己起服务：这一套要把导入这条整路走到底（选文件、铺卡、
点收进书架、翻阅读器、清本地），改的是真账本 cache/flomo/notes.json，和别的套件
共用那份沙盒一定会互相踩。所以这里另起一个 ui_server，数据目录落在系统临时目录，
跑完整包删掉 —— 用户真实的 ~/Documents/归藏 与仓库 cache/ 一个字节都不碰。

笔记内容全部来自 tests/flomo_fixture.py，那里面每一条正文都是现编的：**用户的真实
笔记一个字都不许进这套件**（会推到 GitHub 上）。所以这里断言的是「形状与条数」，
不是某个人的原话。

后端契约（解析 / 去重 / 附件 / 画像的纯逻辑）归 tests/check_flomo_notes.py，
这一份只管界面与 HTTP 口：
  · 空账首启 → 点「导入 flomo 导出包」选到文件 → 时间线铺出 9 张卡；
  · 一天一根分隔线、卡头时间精确到分、字数与标签都摆在明面上（用户要「完整呈现时间、标签等字段」）；
  · 长笔记自动折叠 + 「展开全文 / 收起」；图真的从 /api/flomo/att/ 取回来；
  · 标签药丸筛 / 搜索防抖 / 「再看 N 条」分页；
  · 收进书架 → 卡变「去书库」→ 真打开阅读器；书库里那一本的 module 是 flomo（不串门）；
  · 标签弹层可打字、存得回去；记忆画像弹层里有那份 Markdown；
  · 2.6 秒一次的 /api/state 轮询不许把时间线重铺（同一批 DOM 节点、滚动位置不动）——
    这是这一轮「点一下页面就闪一下」那类毛病的守门；
  · 忘掉一条 → 少一张卡；清空 → 回到教学空态，磁盘上的图与画像跟着一起没；
  · 零 emoji、零 console 报错、三种视口不横向溢出。
"""
import atexit
import base64
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
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent
from playwright.sync_api import sync_playwright  # noqa: E402
from flomo_fixture import export_zip, N_MEMOS  # noqa: E402

FAIL = []
PASSED = [0]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➯️⬀-⯿]")
SANDBOX = tempfile.mkdtemp(prefix="gz-flomo-ui-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 沙盒服务 ────────────────────────────────────────────────────────
PY = sys.executable
# 截图默认跟着沙盒一起删掉（一次跑完不留垃圾）。外面递一个 GUIZANG_SHOT_DIR 就留住，
# 好让「这一屏到底好不好看」这件事有人真的看一眼图，而不是只信那几条断言。
SHOTS = os.environ.get("GUIZANG_SHOT_DIR") or os.path.join(SANDBOX, "shots")
os.makedirs(SHOTS, exist_ok=True)
env = dict(os.environ, GUIZANG_SELFTEST_DIR=SANDBOX,
           GUIZANG_DATA=os.path.join(SANDBOX, "data"),
           GUIZANG_BOOKS=os.path.join(SANDBOX, "books"),
           GUIZANG_SHOT_DIR=SHOTS)
for d in ("data", "books"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)

# 服务那边是 DATA_DIR = GUIZANG_DATA，再往里一层 CACHE_DIR = DATA_DIR/cache，
# 便签又落在 CACHE_DIR/flomo —— 沙盒里拼同一条链，差一层就检查看不到写的文件。
FLOMO = os.path.join(SANDBOX, "data", "cache", "flomo")
ATT = os.path.join(FLOMO, "att")
PORTRAIT = os.path.join(FLOMO, "portrait")
ZIP_PATH = os.path.join(SANDBOX, "flomo-demo.zip")
with open(ZIP_PATH, "wb") as f:
    f.write(export_zip())

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
print("便签走查沙盒：", BASE)


def get(qs=""):
    with urllib.request.urlopen(BASE + "/api/flomo/notes" + ("?" + qs if qs else ""),
                                timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def post(body):
    req = urllib.request.Request(BASE + "/api/flomo/notes",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def raw(path, timeout=15):
    """直接打一个 GET，把状态码和字节都带回来 —— 附件与穿越那两条要看的是 HTTP 本身。"""
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def ls(path):
    """列一个可能压根没建出来的目录：对「本机不该留着私人图片」来说，
    目录不存在和目录是空的算同一件事，不值得为此抛一次异常断掉整条走查。"""
    try:
        return os.listdir(path)
    except OSError:
        return []


# ── 后端契约（只读与筛，界面用的就是这几个口） ──────────────────────
d = get("mode=list")
chk("首启空账：时间线是空的而不是一句报错", d.get("ok") and d.get("memos") == []
    and d.get("total") == 0, d)
chk("首启空账：计数报 0 条（界面上那句「还没导入」靠它）",
    (d.get("stats") or {}).get("memos") == 0, d.get("stats"))
d = get("mode=portrait")
chk("首启空账：记忆画像算得出来（不炸、给 0 条）",
    d.get("ok") and (d.get("portrait") or {}).get("memos") == 0, d)

r = post({"act": "import", "name": "flomo-demo.zip",
          "data": base64.b64encode(open(ZIP_PATH, "rb").read()).decode()})
chk("HTTP 导入：9 条全进、一条不剩", r.get("ok") and r.get("added") == N_MEMOS
    and r.get("total") == N_MEMOS, r)
chk("HTTP 导入：两张图各落一份（同一张图换路径也不算两张）", r.get("atts") == 2, r)
chk("账本落在 cache/flomo/notes.json", os.path.isfile(os.path.join(FLOMO, "notes.json")))
chk("附件落在 cache/flomo/att/，正好两份",
    len([x for x in os.listdir(ATT) if not x.endswith(".tmp")]) == 2,
    os.listdir(ATT) if os.path.isdir(ATT) else None)

d = get("mode=list")
first = (d.get("memos") or [{}])[0]
chk("时间线按时间倒序铺（最新那条是 9月1日）",
    first.get("date") == "2026-09-01", first)
chk("每条都带时间字段（日期 + 几点几分，用户要的「完整呈现时间」）",
    all(k in first for k in ("id", "date", "clock", "md", "tags", "words", "atts")), first)
chk("接口不外泄内部字段（imgs 是本机路径，不给界面）", "imgs" not in first, list(first))
chk("按标签筛：#SOP 两条", get("mode=list&tag=SOP").get("total") == 2)
chk("按标签筛认层级前缀：#灵感 三条（含 灵感/写作）",
    get("mode=list&tag=%E7%81%B5%E6%84%9F").get("total") == 3)
chk("搜正文：命中一条", get("mode=list&q=%E5%A4%8D%E5%88%A9").get("total") == 1)
chk("搜不到的词老实给 0，不拿全表糊", get("mode=list&q=zzz").get("total") == 0)
tg = get("mode=tags")
chk("标签计数给得出（界面药丸上那个小数就靠它）",
    any(x.get("tag") == "SOP" and x.get("n") == 2 for x in (tg.get("tags") or [])),
    tg.get("tags"))
chk("上级路径自己也是一颗药丸（打了 读书/神经科学 的，看 读书 也该捞得到）",
    any(x.get("tag") == "读书" and x.get("n") == 2 for x in (tg.get("tags") or [])),
    tg.get("tags"))
# 药丸上的小数与筛选用的是同一把尺 —— 不一致就是「写着 1 条、点下去出 2 条」那种怪事。
mismatch = [x for x in (tg.get("tags") or [])
            if x["n"] != get("mode=list&tag=" + urllib.parse.quote(x["tag"])).get("total")]
chk("每一颗药丸写着的条数 = 点它筛出的条数", not mismatch, mismatch)
st = (get("mode=stats") or {}).get("stats") or {}
chk("骨架数字对得上：9 条 / 4 天 / 2 张图",
    (st.get("memos"), st.get("days"), st.get("images")) == (N_MEMOS, 4, 2), st)
pic = get("mode=portrait")
chk("记忆画像：条数与标签都在，且落盘到 cache/flomo/portrait/",
    pic.get("ok") and os.path.isfile(os.path.join(PORTRAIT, "portrait.json"))
    and os.path.isfile(os.path.join(PORTRAIT, "portrait.md")), pic.get("file"))
chk("记忆画像那份 Markdown 里一条笔记原文也没有（隐私红线）",
    not any(w in (pic.get("md") or "") for w in ("复利", "买米", "换滤芯", "先写下来再改")),
    (pic.get("md") or "")[:120])

with_att = [m for m in get("mode=list")["memos"] if m.get("atts")]
chk("有两条笔记带着图（时间线才谈得上缩略图）", len(with_att) == 2, len(with_att))
code, blob = raw("/api/flomo/att/" + urllib.parse.quote(with_att[0]["atts"][0]))
chk("附件取回来了（200 + 真是那张 PNG）", code == 200 and blob[:4] == b"\x89PNG", code)
code, _ = raw("/api/flomo/att/..%2Fnotes.json")
chk("附件口不接受路径穿越（.. 进不去别人的账本）", code != 200, code)
code, _ = raw("/api/flomo/att/not-exist.png")
chk("要一个没有的附件给 404，不把整目录列出来", code == 404, code)

bad = post({"act": "nonsense"})
chk("不认识的 act 回一句人话（不 500）", bad.get("ok") is False
    and "不认得" in (bad.get("msg") or ""), bad)
miss = post({"act": "forget", "id": "fm_000000000000"})
chk("忘一条没有的给 False（界面不许报成功）", miss.get("ok") is False, miss)
imp2 = post({"act": "import", "name": "flomo-demo.zip",
             "data": base64.b64encode(open(ZIP_PATH, "rb").read()).decode()})
chk("重复导入认得出人：一条不新增、图不重写",
    imp2.get("added") == 0 and imp2.get("existed") == N_MEMOS and imp2.get("patched") == 0, imp2)

# ── 真机：从这一句「清空」开始，界面自己把这一格重新铺一遍 ────────────
w = post({"act": "clear"})
chk("清空报得出清了几条", w.get("ok") and w.get("n") == N_MEMOS, w)
chk("清空把附件与画像一起抹掉（界面那句话不许说谎）",
    ls(ATT) == [] and ls(PORTRAIT) == [], (ls(ATT), ls(PORTRAIT)))

OVERFLOW = r"""() => {
  const win = document.documentElement.clientWidth;
  const trace = el => {
    const bits = [];
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) s += '#' + n.id;
      else if (n.className && typeof n.className === 'string')
        s += '.' + n.className.trim().split(/\s+/).slice(0, 2).join('.');
      bits.unshift(s);
    }
    return bits.join(' > ');
  };
  const off = [...document.querySelectorAll('#view *')].filter(e => {
    if (!e.getClientRects().length) return false;
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    if (r.width < 6 || r.height < 4) return false;
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    const clipped = [...e.parentElement ? [e.parentElement] : []]
      .some(p => /(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX));
    return (r.right > win + 1 || r.left < -1) && !clipped;
  }).map(e => {
    const r = e.getBoundingClientRect();
    return (trace(e) || e.tagName) + ' 溢出' + Math.round(Math.max(r.right - win, -r.left)) + 'px';
  });
  return {doc: document.documentElement.scrollWidth, win, off: [...new Set(off)].slice(0, 6)};
}"""


def sweep(page, tag):
    t = page.evaluate(OVERFLOW)
    chk("%s：整页没有横向溢出" % tag, t["doc"] <= t["win"] + 1, t)
    chk("%s：没有元素戳出视口" % tag, not t["off"], t["off"])
    hits = sorted(set(EMOJI.findall(page.evaluate("() => document.body.innerText"))))
    chk("%s：屏幕上一个 emoji 也没有" % tag, not hits, hits)


def flush(page):
    """把在跑的动画推到终点：headless Chromium 的动画时钟会整段卡住，读到的计算值
    停在起始值 —— 「铺开了没有」量的是动画不是版式。与订阅那份套件同一写法。"""
    page.evaluate("""async () => {
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} });
    }""")
    page.wait_for_timeout(80)


def settle(page, expr, ms=15000):
    """等一个条件成立，等不到只回 False —— 不用 wait_for_function 抛异常炸断整条走查。"""
    try:
        page.wait_for_function(expr, timeout=ms)
        return True
    except Exception:
        return False


def cards(page):
    return page.locator("#fmScr .fmcard").count()


def whisper(page):
    return page.evaluate("() => (document.querySelector('.whisper')||{}).innerText || ''")


def sheet_act(page, label):
    page.locator('#sheetActs button', has_text=label).first.click()


def card_btn(page, idx, label):
    return page.locator("#fmScr .fmcard").nth(idx).locator(".acts button", has_text=label).first


def pill(page, tag):
    """按标签原文点那一颗药丸。

    不能用 has_text：它是子串匹配，「SOP」会先命中「SOP/家务」，点到的不是想点的那一颗，
    于是「筛出几条」量的其实是另一个标签。药丸的文本节点就是标签，后面才挂 <i> 里的条数。
    """
    return page.evaluate("""(t) => {
      const b = [...document.querySelectorAll('#fmTags .tg')]
        .find(x => x.firstChild && x.firstChild.textContent.trim() === t);
      if (b) b.click();
      return !!b;
    }""", tag)


def shot(page, name):
    """等动画走完再拍。

    这几张图不只是走查留底 —— 是要进 README 的。淡入没走完就按快门，拍出来整屏半透明，
    看着像界面自己没渲染好（上一轮 README 的图就为此重拍过一次）。
    """
    page.wait_for_timeout(900)
    page.screenshot(path=os.path.join(str(env["GUIZANG_SHOT_DIR"]), name))


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("PAGEERROR " + str(e)))

    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_timeout(900)
    page.click('#nav button[data-v="flomo"]')
    page.wait_for_timeout(600)

    # 空那一格得先教用户怎么把笔记弄进来，不能只给一句「这里没东西」。
    chk("空态讲的是「这一格还是空的」+ 怎么导入",
        "这一格还是空的" in page.inner_text("#fmScr")
        and "导入 flomo 导出包" in page.inner_text("#fmScr"),
        page.inner_text("#fmScr")[:120])
    chk("计数行说的是「还没导入」", "还没导入" in page.inner_text("#fmCnt"),
        page.inner_text("#fmCnt"))

    # 真点一次文件选择器：整条导入链路（按钮 → <input type=file> → base64 → POST → 重铺）
    with page.expect_file_chooser() as fc:
        page.click("#fmImport")
    fc.value.set_files(ZIP_PATH)
    ok = settle(page, "() => document.querySelectorAll('#fmScr .fmcard').length === %d" % N_MEMOS,
                ms=25000)
    chk("点「导入 flomo 导出包」选那个 zip → 时间线铺出 %d 张卡" % N_MEMOS, ok, cards(page))
    chk("导入的回执说清了几条、几张图",
        ("导入 9 条" in whisper(page) or "9 条" in whisper(page)) and "2 张图" in whisper(page),
        whisper(page))
    flush(page)

    chk("一天一根分隔线（4 天 4 根）", page.locator("#fmScr .fmday").count() == 4,
        page.locator("#fmScr .fmday").count())
    chk("分隔线写了日期、星期与这一天的条数",
        re.search(r"9月1日 星期. · 1 条", page.inner_text("#fmScr")),
        page.inner_text("#fmScr")[:60])
    hd0 = page.locator("#fmScr .fmcard").nth(0).locator(".fmhd").inner_text()
    chk("卡头把时间精确到分（用户要的「完整呈现时间」不止是个日期）",
        re.search(r"9月1日 10:00", hd0), hd0)
    chk("卡头有字数（时间/标签/字数都摆在明面上）", "字" in hd0, hd0)
    chk("计数行报出了体量：条数 / 天数 / 标签数 / 图数",
        all(x in page.inner_text("#fmCnt") for x in ("9 条", "4 天", "个标签", "2 张图")),
        page.inner_text("#fmCnt"))
    pills = page.evaluate("() => [...document.querySelectorAll('#fmTags .tg')]"
                          ".map(b => b.innerText.trim())")
    chk("药丸条铺出了标签与计数", any("SOP" in x and "2" in x for x in pills), pills)
    chk("第一张卡就是最新那条",
        "早上一句话的灵感" in page.locator("#fmScr .fmcard").nth(0).inner_text(),
        page.locator("#fmScr .fmcard").nth(0).inner_text()[:60])
    chk("正文里不泄漏导出包的本机路径（图引用换成缩略图）",
        "file/2026" not in page.evaluate("() => document.querySelector('#fmScr').innerText"))
    chk("Markdown 渲染过了（列表成列表、加粗成了粗体）",
        page.locator("#fmScr .fmbd li").count() >= 2
        and page.locator("#fmScr .fmbd strong").count() >= 1)
    # flomo 的导出把标签就写在正文末尾，卡下面那排药丸又列一遍 —— 同一条里出现两次，
    # 看着像没清干净。这里量的是「一张卡里这个标签出现几次」：只许出现一次（那一颗药丸）。
    tagdup = page.evaluate(r"""() => {
      const c = [...document.querySelectorAll('#fmScr .fmcard')]
        .find(x => x.innerText.includes('外层第一段'));
      if (!c) return null;
      const re = /#读书\/神经科学/g;
      return {inCard: (c.innerText.match(re) || []).length,
              inBody: (c.querySelector('.fmbd').innerText.match(re) || []).length,
              chip: !!c.querySelector('.fmtg i')};
    }""")
    chk("标签只在下面那一排列一次（正文里那串井号不再重复）",
        tagdup and tagdup["inCard"] == 1 and tagdup["inBody"] == 0 and tagdup["chip"], tagdup)
    chk("不是标签的井号留着（「#3」是个序号，不该被顺手抹掉）",
        "#3" in page.evaluate("() => [...document.querySelectorAll('#fmScr .fmbd')]"
                             ".map(x => x.innerText).join('\\n')"))

    # 长笔记自动折叠：这是这一轮明确要的「支持长笔记自动折叠」。
    clamped = page.locator("#fmScr .fmbd.clamp")
    chk("长的那条被折住了（短的不折）", clamped.count() == 1, clamped.count())
    # 折不折是「按字数判」的，动的是 .fmbd 那一层，所以按 .fmmore 那颗反查它自己的正文，
    # 别去数第几张卡 —— 卡片顺序一变整套就跟着假失败。
    body_h = """() => {
      const more = document.querySelector('#fmScr .fmmore');
      if (!more) return -1;
      const bd = more.previousElementSibling;
      return {h: Math.round(bd.getBoundingClientRect().height), clamp: bd.classList.contains('clamp'),
              label: more.textContent, chars: bd.innerText.length};
    }"""
    if clamped.count():
        a = page.evaluate(body_h)
        chk("折叠后确实矮下去了（一屏还能看见后面几条）", 0 < a["h"] <= 180 and a["clamp"], a)
        page.locator("#fmScr .fmmore").first.click()
        page.wait_for_timeout(200)
        b = page.evaluate(body_h)
        # 要长出 30px 以上才算真放开：夹具那条不够长时，折与不折量出来是同一个数，
        # 那种「PASS」比没有更坏（它假装验过一件没验的事）。
        chk("「展开全文」真的把那一条放开（长高了、标记摘掉了）",
            b["h"] >= a["h"] + 30 and not b["clamp"], (a, b))
        chk("钮上的字跟着变成「收起」", b["label"] == "收起", b)
        page.locator("#fmScr .fmmore").first.click()
        page.wait_for_timeout(200)
        c = page.evaluate(body_h)
        chk("再点一下收回去（时间线还得能扫）",
            c["clamp"] and c["h"] <= 180 and c["h"] < b["h"], (b, c))

    # 图：懒加载，先滚到那张卡再验真取回来了（走的是 /api/flomo/att/）。
    chk("两条带图的笔记各有一张缩略图",
        page.locator("#fmScr .fmimg img").count() == 2,
        page.locator("#fmScr .fmimg img").count())
    page.evaluate("() => { const i = document.querySelector('#fmScr .fmimg img');"
                  "if (i) i.scrollIntoView({block:'center'}); }")
    ok = settle(page, "() => [...document.querySelectorAll('#fmScr .fmimg img')]"
                      ".every(i => i.complete && i.naturalWidth > 0)", ms=10000)
    chk("笔记里的图真的从本机取回来了（不是裂图）", ok,
        page.evaluate("() => [...document.querySelectorAll('#fmScr .fmimg img')]"
                      ".map(i => [i.currentSrc.slice(-24), i.naturalWidth])"))
    page.evaluate("() => { document.querySelector('#fmScr').scrollTop = 0 }")

    shot(page, "flomo-timeline.png")
    sweep(page, "便签 1440")

    # 搜索：320ms 防抖，所以「填完立刻有结果」这种断言必假，等条件而不是等时间。
    page.fill("#fmQ", "复利")
    chk("搜索命中的只剩那一条", settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard').length === 1", ms=8000),
        cards(page))
    page.fill("#fmQ", "")
    chk("清空搜索回到全部", settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard').length === %d" % N_MEMOS, ms=8000),
        cards(page))
    # 「SOP」这一颗是上级路径（笔记上打的是 SOP/家务、SOP/flomo）。药丸条得给得出这一颗，
    # 而且它写着几条，点下去就得筛出几条 —— 数字与结果不一致，用户看到的是「筛选坏了」。
    n_sop = page.evaluate("""() => {
      const b = [...document.querySelectorAll('#fmTags .tg')]
        .find(x => x.firstChild && x.firstChild.textContent.trim() === 'SOP');
      return b ? Number((b.querySelector('i') || {}).textContent || '-1') : -1;
    }""")
    chk("上级标签那一颗药丸给得出，并且写着两条", n_sop == 2, n_sop)
    chk("点它就筛出那两条", pill(page, "SOP") and settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard').length === 2", ms=8000),
        cards(page))
    chk("筛的时候计数行说清「筛出 2 / 9」",
        "筛出 2" in page.inner_text("#fmCnt"), page.inner_text("#fmCnt"))
    chk("当前那颗药丸亮着",
        page.locator("#fmTags .tg.on").count() == 1,
        page.evaluate("() => [...document.querySelectorAll('#fmTags .tg.on')]"
                      ".map(b => b.innerText)"))
    chk("再点一下就回到整格", pill(page, "SOP") and settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard').length === %d" % N_MEMOS, ms=8000),
        cards(page))
    # 叶子那一颗：写着几条就是几条，不许把只打了根标签的那条拽进来（「灵感」3 条，
    # 「灵感/写作」只有 2 条 —— 上一版两个方向都算，这里会点出 3 条，看着就是药丸在骗人）。
    chk("点叶子标签「灵感/写作」筛出它自己的那两条", pill(page, "灵感/写作") and settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard').length === 2", ms=8000),
        cards(page))
    chk("叶子筛出的那两条身上都带着这个叶子标签",
        all("灵感/写作" in (c.get("tags") or [])
            for c in (get("mode=list&tag=" + urllib.parse.quote("灵感/写作")).get("memos") or [])))
    pill(page, "灵感/写作")
    page.wait_for_timeout(600)

    # 反闪烁金丝雀：轮询每 2.6 秒来一次，它绝不该把时间线重铺（节点换了就是用户说的「闪」）。
    page.evaluate("""() => {
      const scr = document.querySelector('#fmScr');
      window.__c1 = scr.querySelector('.fmcard');
      window.__c2 = document.querySelector('#fmTags .tg');
      window.__c1.__mark = 1;
      scr.scrollTop = 240;
    }""")
    page.wait_for_timeout(3400)
    keep = page.evaluate("""() => {
      const scr = document.querySelector('#fmScr');
      return {sameCard: scr.querySelector('.fmcard') === window.__c1,
              mark: window.__c1 && window.__c1.__mark === 1,
              samePill: document.querySelector('#fmTags .tg') === window.__c2,
              top: scr.scrollTop};
    }""")
    chk("轮询 2.6 秒后卡片还是同一批 DOM 节点（重铺就是「页面自己在那儿闪」）",
        keep.get("sameCard") and keep.get("mark"), keep)
    chk("轮询没有把标签药丸重画", keep.get("samePill"), keep)
    chk("轮询没有把滚动位置顶回顶上", keep.get("top", 0) >= 200, keep)
    page.evaluate("() => document.querySelector('#fmScr').scrollTop = 0")
    page.wait_for_timeout(120)

    # 收进书架 → 卡变「去书库」→ 真打开阅读器；书库那一本的 module 必须是 flomo。
    # 挑「有字也有图」的那一条（不是只有图的那条）：阅读器里要比正文，纯图那条没有正文可比。
    img_idx = page.evaluate("""() => [...document.querySelectorAll('#fmScr .fmcard')]
        .findIndex(c => c.querySelector('.fmimg img') && c.innerText.includes('带图的一条'))""")
    chk("找到那条「有字也有图」的笔记当样本", img_idx >= 0, img_idx)
    card_btn(page, img_idx, "收进书架").click()
    ok = settle(page,
        "() => document.querySelectorAll('#fmScr .fmcard.pushed').length === 1", ms=10000)
    chk("「收进书架」把这一条变成书（卡片标出已收）", ok,
        page.locator("#fmScr .fmcard.pushed").count())
    chk("回执说的是收成一本《…》",
        "已收成一本" in whisper(page) or "已收进书架" in whisper(page), whisper(page))
    books = {b["id"]: b for b in state()["books"]}
    fm_books = [b for b in books.values() if b.get("module") == "flomo"]
    chk("这一本住在「便签」那一格（别的格子看不见它）",
        len(fm_books) == 1, [b.get("id") for b in books.values()])
    if fm_books:
        chk("收进去的那本带着自己的图", fm_books[0].get("images", 0) >= 1, fm_books[0])
        chk("收进去的那本只有 1 章（一条笔记就是一章）", fm_books[0].get("chapters") == 1,
            fm_books[0])
        chk("书名不带末尾那个标签（标签在书开头那行列一次就够）",
            fm_books[0].get("title") == "带图的一条，图在 files 那层。", fm_books[0].get("title"))
    card_btn(page, img_idx, "去书库").click()
    ok = settle(page, "() => !!document.querySelector('#rdWrap') "
                      "&& document.querySelectorAll('#rdList .rdch').length >= 1", ms=20000)
    chk("「去书库」真的翻开阅读器（章节列出来了）", ok,
        page.evaluate("() => ({rd: !!document.querySelector('#rdWrap'),"
                      "ch: document.querySelectorAll('#rdList .rdch').length})"))
    body = page.evaluate("() => (document.querySelector('#rdBody')||{}).innerText || ''")
    chk("阅读器里读到的就是那条笔记的正文", "带图的一条" in body, body[:120])
    # 那本书是「标题 + 时间标签行 + 正文」三块拼的，标签只在中间那行列一次；
    # 正文末尾原样带着「#读书」的话，一本书里同一个标签出现两次，看着就是没清干净。
    chk("书里那个标签只列一次（正文末尾那份不再重复）", body.count("#读书") == 1, body[:200])
    shot(page, "flomo-reader.png")
    page.click("#rdBack")
    page.wait_for_timeout(500)
    chk("阅读器里的「返回」回到便签那一格",
        page.locator('#nav button[data-v="flomo"].on').count() == 1,
        page.evaluate("() => v"))

    # 标签弹层：要能打字、要存得回去（这一屏最容易漏的就是 readonly 没解开）。
    tag_id = page.evaluate("() => document.querySelector('#fmScr .fmcard').dataset.id")
    page.locator("#fmScr .fmcard").nth(0).locator(".acts button", has_text="标签").first.click()
    page.wait_for_timeout(250)
    chk("标签弹层打开了", page.evaluate("() => document.querySelector('#veil').classList"
                                        ".contains('open')"))
    chk("弹层里那一栏真能打字（readonly 没解开的老 bug）",
        page.evaluate("() => !document.querySelector('#sheetBody').readOnly"))
    was = page.evaluate("() => document.querySelector('#sheetBody').value")
    chk("弹层里预先摊着这条笔记现有的标签（不是空栏让你重新想）",
        "灵感/写作" in was, was)
    # 只往上加一条，不整栏替换：原标签是本机账上真实的东西，走查不该把它顺手抹掉，
    # 抹掉后面「按标签筛」「记忆画像」那几条量的就不是同一份账了。
    page.fill("#sheetBody", (was.strip() + "\n走查临时").strip())
    sheet_act(page, "存好")
    ok = settle(page, "() => { const c = document.querySelector('#fmScr .fmcard');"
                      " return !!c && c.innerText.includes('走查临时')"
                      " && c.innerText.includes('灵感/写作'); }", ms=10000)
    chk("存好的标签立刻回到那张卡上（旧的还留着）", ok,
        page.locator("#fmScr .fmcard").nth(0).inner_text())
    chk("弹层自己收掉了", page.evaluate("() => !document.querySelector('#veil').classList"
                                        ".contains('open')"))
    tagcount = get("mode=tags").get("tags") or []
    chk("新标签进了标签账（药丸下一轮就会多一颗）",
        any(x.get("tag") == "走查临时" and x.get("n") == 1 for x in tagcount), tagcount)
    # 还原：走查临时 只是这一趟的样本，别留在时间线里影响后面的断言。
    back = post({"act": "tag", "id": tag_id, "tag": "走查临时", "remove": True})
    chk("还原得了：临时标签贴得上也撕得掉", back.get("ok"), back)
    page.wait_for_timeout(200)

    # 记忆画像：Agent 每次做事前先读的那一份，界面给一个窗口看见它。
    page.click("#fmPic")
    ok = settle(page, "() => document.querySelector('#veil').classList.contains('open')"
                      " && document.querySelector('#sheetBody').value.length > 40", ms=10000)
    md = page.evaluate("() => document.querySelector('#sheetBody').value") if ok else ""
    chk("「记忆画像」把那份 Markdown 摊开了", ok, md[:100])
    chk("画像里只有数字与标签，末尾那句诚实声明在",
        "不含任何一条笔记原文" in md and "9 条" in md, md[-160:])
    chk("画像没把笔记原文带进来（隐私红线，界面上也一样）",
        not any(w in md for w in ("复利", "买米", "换滤芯")), md[:100])
    shot(page, "flomo-portrait.png")
    sheet_act(page, "关掉")
    page.wait_for_timeout(200)

    # 右上角那颗 ⋯：这一格也要有「新建文件夹 / 管理分类 / 管理标签」。
    page.click('.chead .kbtn[data-mod="flomo"]')
    page.wait_for_timeout(250)
    menu = page.evaluate("() => [...document.querySelectorAll('.kmenu button,.kmenu div')]"
                         ".map(b => b.innerText.trim()).filter(x => x)")
    chk("便签这一格的 ⋯ 菜单给得出分类与标签的入口",
        any("新建文件夹" in x for x in menu) and any("管理分类" in x for x in menu)
        and any("管理标签" in x for x in menu), menu)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # 忘掉一条：卡片当场少一张，磁盘上那条真的不在了。
    n_before = cards(page)
    page.locator("#fmScr .fmcard").nth(0).locator(".acts button", has_text="忘掉").first.click()
    page.wait_for_timeout(250)
    chk("「忘掉」先问一句（这是删东西）",
        "flomo 里的原文一个字都不动" in page.evaluate("() => document.querySelector('#sheetText')"
                                                      ".innerText"))
    sheet_act(page, "忘掉")
    ok = settle(page, "() => document.querySelectorAll('#fmScr .fmcard').length === %d" % (n_before - 1),
                ms=10000)
    chk("忘掉之后这一张当场从时间线里没了", ok, (n_before, cards(page)))
    chk("回执说清了只动本机、flomo 不动",
        "本机已忘掉" in whisper(page), whisper(page))

    # 清空：整格回到空态，磁盘上附件与画像一起抹掉（书库里那本留着）。
    page.click("#fmWipe")
    page.wait_for_timeout(250)
    txt = page.evaluate("() => document.querySelector('#sheetText').innerText")
    chk("清空前先报出要清几条", "%d 条" % (n_before - 1) in txt, txt)
    sheet_act(page, "确认清掉")
    ok = settle(page, "() => !document.querySelectorAll('#fmScr .fmcard').length "
                      "&& document.querySelector('#fmScr').innerText.includes('这一格还是空的')",
                ms=12000)
    chk("清空后回到教学空态", ok, page.inner_text("#fmScr")[:80])
    chk("清空真的把本机的图与画像抹了（界面那句「附件里的图片和记忆画像」不许说谎）",
        ls(ATT) == [] and ls(PORTRAIT) == [], (ls(ATT), ls(PORTRAIT)))
    # 那句回执还许了另一半：「书库里已经收进去的那些书也不受影响」—— 那就得真留着，
    # 而且书库接口还认它（只看目录存在等于没验：书目从目录来，也从 _meta 来）。
    keep = [x for x in ls(os.path.join(str(env["GUIZANG_BOOKS"]), "便签"))
            if x.startswith("flomo_")]
    still = [b for b in state()["books"] if b.get("module") == "flomo"]
    chk("清掉这一格的账，不碰已经收进书库的那本书",
        len(keep) == 1 and len(still) == 1,
        (keep, [b.get("id") for b in still]))
    st_after = get("mode=stats").get("stats") or {}
    chk("清空后计数归零（侧边栏那颗角标也跟着掉）", st_after.get("memos") == 0, st_after)
    page.wait_for_timeout(3000)
    badge = page.evaluate("() => (document.querySelector('#bgFlomo')||{}).innerText || ''")
    chk("侧边栏的便签角标没有残留旧数字", badge.strip() in ("", "0"), badge)

    # 窄屏：这一格工具条有输入框、两颗按钮和一行小字，最容易在 390 上挤成一列字。
    for wdt, hgt, tag in ((720, 860, "便签 720"), (390, 780, "便签 390")):
        page.set_viewport_size({"width": wdt, "height": hgt})
        page.wait_for_timeout(400)
        flush(page)
        sweep(page, tag)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(300)

    chk("全程没有 console 报错", not errors, errors[:6])
    browser.close()

print()
print("便签真机：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
